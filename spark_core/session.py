import asyncio
import json
import shutil
import sqlite3
import sys
import tempfile
from astrbot.api import logger

class LoadTimeout(TimeoutError):
    pass
from pathlib import Path
from .transport import download, fetch_file, report_id
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

    async def cache_call(self, name, *args, default=None):
        # The raw cache only saves a download; a broken cache must never fail the analysis.
        if not self.cache:
            return default
        try:
            return await asyncio.to_thread(getattr(self.cache, name), *args)
        except (sqlite3.Error, OSError, ValueError) as exc:
            logger.warning('Spark raw cache %s failed: %s', name, type(exc).__name__)
            return default

    async def load(self, url):
        key = report_id(url)
        target = self.directory/'profile.bin'
        hit = await self.cache_call('get', key, target, default=False)
        logger.info('Spark raw cache %s; report=%s', 'hit' if hit else 'miss', key)
        if not hit:
            logger.info('Spark download start; timeout=%ss', self.download_timeout)
            try:
                await download(url, target, timeout_seconds=self.download_timeout, proxy=self.proxy)
            except TimeoutError as exc:
                raise LoadTimeout(f'下载阶段超时（{self.download_timeout}秒）；尚未调用分析模型；{exc}') from exc
            await self.cache_call('put', key, target)
        logger.info('Spark download success; decoded_bytes=%s; parse start timeout=%ss',
                    target.stat().st_size, self.parse_timeout)
        return await self.parse(key)

    async def load_file(self, local='', url=''):
        """A .sparkprofile attachment. Not cached: the user still has the file and sends it again."""
        target = self.directory/'profile.bin'
        logger.info('Spark file fetch start; timeout=%ss', self.download_timeout)
        try:
            await fetch_file(local, url, target, timeout_seconds=self.download_timeout)
        except TimeoutError as exc:
            raise LoadTimeout(f'文件获取阶段超时（{self.download_timeout}秒）；尚未调用分析模型；{exc}') from exc
        logger.info('Spark file fetch success; bytes=%s; parse start timeout=%ss',
                    target.stat().st_size, self.parse_timeout)
        return await self.parse()

    async def parse(self, key=None):
        """Start the worker on profile.bin and wait for its overview; key names the cache entry it came from."""
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
            if self.overview['error'].startswith('DecodeError:') and key:
                await self.cache_call('discard', key)
            kind, _, reason = self.overview['error'].partition(': ')
            raise ProfileError(reason or f'报告解析失败（{kind}）')
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
        try:
            if self.process and self.process.returncode is None:
                try:
                    self.process.kill()
                except ProcessLookupError:
                    pass
                await asyncio.wait_for(self.process.wait(), timeout=5)
        finally:
            # The directory holds the full decoded report; remove it even if the worker wait failed.
            shutil.rmtree(self.directory, ignore_errors=True)
