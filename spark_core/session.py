import asyncio
import json
import shutil
import sys
import tempfile
import logging

logger = logging.getLogger('astrbot.plugin.astrbot_plugin_spark')

class LoadTimeout(TimeoutError):
    pass
from pathlib import Path
from .transport import download, report_id
from .profile import ProfileError


class ReportSession:
    def __init__(self, root, download_timeout=120, parse_timeout=90, cache=None, proxy=None):
        self.proxy = proxy
        self.cache = cache
        self.download_timeout = download_timeout
        self.parse_timeout = parse_timeout
        self.directory = Path(tempfile.mkdtemp(prefix='spark-', dir=root))
        self.process = None
        self.sequence = 0
        self.lock = asyncio.Lock()
        self.overview = None
        self.failed = False

    async def wait_file(self, path, timeout):
        async def wait():
            while not path.exists():
                if self.process.returncode is not None:
                    raise ProfileError('解析worker异常退出')
                await asyncio.sleep(0.05)
            return json.loads(path.read_text(encoding='utf-8'))
        return await asyncio.wait_for(wait(), timeout)

    async def load(self, url):
        key = report_id(url)
        target = self.directory/'profile.bin'
        hit = await asyncio.to_thread(self.cache.get, key, target) if self.cache else False
        logger.info('Spark raw cache %s; report=%s', 'hit' if hit else 'miss', key)
        if not hit:
            logger.info('Spark download start; timeout=%ss', self.download_timeout)
            try:
                await download(url, target, timeout_seconds=self.download_timeout, proxy=self.proxy)
            except TimeoutError as exc:
                raise LoadTimeout(f'下载阶段超时（{self.download_timeout}秒）；尚未调用分析模型；{exc}') from exc
            if self.cache:
                await asyncio.to_thread(self.cache.put, key, target)
        logger.info('Spark download success; decoded_bytes=%s; parse start timeout=%ss',
                    (self.directory/'profile.bin').stat().st_size, self.parse_timeout)
        self.process = await asyncio.create_subprocess_exec(
            sys.executable, str(Path(__file__).with_name('worker.py')), str(self.directory),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL)
        try:
            self.overview = await self.wait_file(self.directory/'ready.json', self.parse_timeout)
        except TimeoutError as exc:
            raise LoadTimeout(f'解析/证据包阶段超时（{self.parse_timeout}秒）；尚未调用分析模型') from exc
        logger.info('Spark parse ready; worker_pid=%s', self.process.pid)
        if 'error' in self.overview:
            if self.cache and self.overview['error'].startswith('DecodeError:'):
                await asyncio.to_thread(self.cache.discard, key)
            raise ProfileError(self.overview['error'])
        return self.overview

    async def query(self, **arguments):
        async with self.lock:
            if self.failed:
                raise ProfileError('查询会话已失效，请重新加载报告')
            i = self.sequence
            temp = self.directory/'request.tmp'
            temp.write_text(json.dumps(arguments, ensure_ascii=False), encoding='utf-8')
            temp.replace(self.directory/f'request-{i}.json')
            try:
                result = await self.wait_file(self.directory/f'response-{i}.json', 30)
            except (TimeoutError, asyncio.CancelledError):
                self.failed = True
                if self.process and self.process.returncode is None:
                    self.process.kill()
                raise
            self.sequence += 1
            (self.directory/f'request-{i}.json').unlink()
            (self.directory/f'response-{i}.json').unlink()
            return result

    async def close(self):
        self.failed = True
        if self.process and self.process.returncode is None:
            self.process.kill()
            await asyncio.wait_for(self.process.wait(), timeout=5)
        if self.directory.exists():
            shutil.rmtree(self.directory)
