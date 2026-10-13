import asyncio
import os
import re
import zlib
import time
from urllib.parse import urlsplit
from urllib.request import url2pathname
from astrbot.api import logger
import aiohttp
from .profile import ProfileError
from yarl import URL


def validate_proxy(value):
    try:
        url = URL(value)
        if url.scheme not in ('http', 'https') or not url.host or url.port is None or url.path not in ('', '/') or url.query_string or url.fragment:
            raise ValueError()
    except (ValueError, TypeError):
        raise ProfileError('下载代理地址无效；请填写HTTP/HTTPS代理地址，不支持SOCKS或路径参数') from None
    return str(url)

# A trailing '.' or '/' ends the link (sentence punctuation); '/x' or '.x' means a longer, unsupported URL.
LINK = re.compile(r'https://spark\.lucko\.me/([A-Za-z0-9]{6,64})(?![A-Za-z0-9_-]|/[A-Za-z0-9]|\.[A-Za-z0-9])')


# Observable (tasgon/observable) report pages live at /p/<id>, optionally with a tab name after the id.
OBS_TABS = ('individual', 'chunks', 'aggregate', 'traces', 'info')
OBS_LINK = re.compile(r'https://observable\.tas\.sh/p/([A-Za-z0-9_-]{4,64})(?:/(?:' + '|'.join(OBS_TABS) + r'))?/?'
                      r'(?![A-Za-z0-9_-]|/[A-Za-z0-9]|\.[A-Za-z0-9])')
OBS_DECODED_LIMIT = 48*1024*1024


def report_id(url):
    match = re.fullmatch(r'https://spark\.lucko\.me/([A-Za-z0-9]{6,64})/?', url)
    if not match:
        raise ProfileError('只接受官方HTTPS spark报告链接，不接受自定义URL')
    return match.group(1)


async def download(url, path, compressed_limit=16*1024*1024, decoded_limit=128*1024*1024, timeout_seconds=120, proxy=None):
    proxy = validate_proxy(proxy) if proxy is not None else None
    logger.info('Spark download route=%s', 'plugin_proxy' if proxy else 'direct')
    stats = {'phase': '连接/等待响应头', 'received': 0, 'written': 0, 'started': time.monotonic()}
    try:
        await _download(url, path, compressed_limit, decoded_limit, timeout_seconds, stats, proxy)
    except TimeoutError as exc:
        elapsed = time.monotonic()-stats['started']
        detail = f"阶段={stats['phase']}，已接收={stats['received']}字节，已解压={stats['written']}字节，耗时={elapsed:.1f}秒"
        logger.warning('Spark download timeout: %s', detail)
        raise TimeoutError(detail) from exc


async def _download(url, path, compressed_limit, decoded_limit, timeout_seconds, stats, proxy=None):
    key = report_id(url)
    await _get(f'https://spark-usercontent.lucko.me/{key}', path, compressed_limit, decoded_limit, timeout_seconds, stats, proxy,
               content_type=('application/x-spark-sampler',), type_error='报告类型不是sampler', observable=False,
               too_big=SPARK_DECODED_TOO_BIG)


SPARK_DECODED_TOO_BIG = ('解压体积超限：报告解压后超过 128 MiB，通常是采样时间太长（例如连续采样数小时）。'
                         '请缩短采样时间后重新上传，例如 /spark profiler start --timeout 300（5 分钟后自动停止）')
COMPRESSED_TOO_BIG = ('下载体积超限：报告文件超过 16 MiB，通常是采样时间太长。'
                      '请缩短采样时间后重新上传，例如 /spark profiler start --timeout 300（5 分钟后自动停止）')
OBS_DECODED_TOO_BIG = '解压体积超限：Observable 报告解压后超过 48 MiB，通常是采样时间太长或实体过多。请缩短采样时间后重新采集'


def observable_id(url):
    match = re.fullmatch(OBS_LINK, url)
    if not match:
        raise ProfileError('只接受官方HTTPS Observable报告链接 https://observable.tas.sh/p/报告ID')
    return match.group(1)


async def download_observable(url, path, compressed_limit=16*1024*1024, decoded_limit=OBS_DECODED_LIMIT, timeout_seconds=120, proxy=None):
    """Fetch the JSON behind an Observable report page: the site's own data endpoint, fixed host, no redirects."""
    proxy = validate_proxy(proxy) if proxy is not None else None
    logger.info('Observable download route=%s', 'plugin_proxy' if proxy else 'direct')
    stats = {'phase': '连接/等待响应头', 'received': 0, 'written': 0, 'started': time.monotonic()}
    key = observable_id(url)
    try:
        await _get(f'https://observable.tas.sh/v1/get/{key}', path, compressed_limit, decoded_limit, timeout_seconds, stats, proxy,
                   content_type=('application/json', 'text/plain'), type_error='报告类型不是Observable数据', observable=True,
                   too_big=OBS_DECODED_TOO_BIG)
    except TimeoutError as exc:
        elapsed = time.monotonic()-stats['started']
        detail = f"阶段={stats['phase']}，已接收={stats['received']}字节，已解压={stats['written']}字节，耗时={elapsed:.1f}秒"
        logger.warning('Observable download timeout: %s', detail)
        raise TimeoutError(detail) from exc


async def _get(url, path, compressed_limit, decoded_limit, timeout_seconds, stats, proxy, *, content_type, type_error, observable, too_big):
    timeout = aiohttp.ClientTimeout(total=timeout_seconds, connect=min(30, timeout_seconds))
    # Ask only for gzip, the one coding decoded here. aiohttp's default header also offers br and zstd when those
    # libraries are installed, and a server or CDN that picks one would fail the download.
    headers = {'Accept-Encoding': 'gzip'}
    async with aiohttp.ClientSession(timeout=timeout, auto_decompress=False, trust_env=False) as session:
        async with session.get(url, allow_redirects=False, proxy=proxy, headers=headers) as response:
            if response.status != 200:
                # Observable answers an unknown or expired id with a 500 rather than a 404.
                gone = '，Observable 报告可能不存在或已过期，请确认链接' if observable and response.status in (404, 500) else ''
                raise ProfileError(f'报告下载失败 HTTP {response.status}{gone}')
            if response.headers.get('Content-Type', '').split(';')[0].strip().lower() not in content_type:
                raise ProfileError(type_error)
            encoding = response.headers.get('Content-Encoding', '').lower()
            if encoding not in ('', 'identity', 'gzip'):
                raise ProfileError('不支持的HTTP压缩格式')
            decoder = zlib.decompressobj(16+zlib.MAX_WBITS) if encoding == 'gzip' else None
            stats['phase'] = '读取响应体'
            logger.info('Spark download response; encoding=%s content_length=%s', encoding or 'identity', response.headers.get('Content-Length', 'unknown'))
            received = written = 0
            last_progress = time.monotonic()
            with path.open('wb') as output:
                async for chunk in response.content.iter_chunked(65536):
                    received += len(chunk)
                    stats['received'] = received
                    if received > compressed_limit:
                        raise ProfileError(COMPRESSED_TOO_BIG)
                    data = decoder.decompress(chunk, decoded_limit-written+1) if decoder else chunk
                    written += len(data)
                    stats['written'] = written
                    now = time.monotonic()
                    if now-last_progress >= 30:
                        logger.info('Spark download progress; received_bytes=%s decoded_bytes=%s elapsed=%.1fs', received, written, now-stats['started'])
                        last_progress = now
                    if written > decoded_limit or (decoder and decoder.unconsumed_tail):
                        raise ProfileError(too_big)
                    output.write(data)
                if decoder and (not decoder.eof or decoder.unused_data):
                    raise ProfileError('gzip流截断或存在额外数据')
            if not written:
                raise ProfileError('报告为空')


# `spark profiler stop --save-to-file` writes the raw SamplerData protobuf, with no transport compression, so a
# file has a single limit: the decoded limit of a downloaded report.
REPORT_FILE_SUFFIX = '.sparkprofile'
FILE_TOO_LARGE = ('文件体积超限：.sparkprofile 文件超过 128 MiB，通常是采样时间太长。请缩短采样时间后重新导出，'
                  '例如 /spark profiler start --timeout 300（5 分钟后自动停止）')
FILE_UNREADABLE = '无法读取聊天中的文件，请重新发送'


def is_report_file(name):
    return str(name or '').lower().endswith(REPORT_FILE_SUFFIX)


async def fetch_file(local, url, path, limit=128*1024*1024, timeout_seconds=120):
    """Copy a chat attachment to path: the adapter's local file when it exists, otherwise its download URL.

    Both come from the platform adapter, never from message text. The URL can carry a token (Telegram puts the
    bot token in it), so it is never logged or shown. It is fetched the way AstrBot fetches attachments
    (environment proxy settings, redirects followed), but bounded in size and time.
    """
    local = url2pathname(urlsplit(local).path) if local.startswith('file://') else local
    if local and os.path.isfile(local):
        logger.info('Spark file route=local')
        await asyncio.to_thread(_copy, local, path, limit)
    elif url.startswith(('https://', 'http://')):
        logger.info('Spark file route=url')
        await _fetch(url, path, limit, timeout_seconds)
    else:
        raise ProfileError(FILE_UNREADABLE)
    if not path.stat().st_size:
        raise ProfileError('报告文件为空')


def _copy(source, path, limit):
    copied = 0
    with open(source, 'rb') as reader, path.open('wb') as output:
        while chunk := reader.read(1024*1024):
            copied += len(chunk)
            if copied > limit:
                raise ProfileError(FILE_TOO_LARGE)
            output.write(chunk)


async def _fetch(url, path, limit, timeout_seconds):
    stats = {'phase': '连接/等待响应头', 'received': 0, 'started': time.monotonic()}
    timeout = aiohttp.ClientTimeout(total=timeout_seconds, connect=min(30, timeout_seconds))
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(url, headers={'Accept-Encoding': 'identity'}) as response:
                if response.status != 200:
                    raise ProfileError(f'文件下载失败 HTTP {response.status}，请重新发送文件')
                if (response.content_length or 0) > limit:
                    raise ProfileError(FILE_TOO_LARGE)
                stats['phase'] = '读取响应体'
                with path.open('wb') as output:
                    async for chunk in response.content.iter_chunked(65536):
                        stats['received'] += len(chunk)
                        if stats['received'] > limit:
                            raise ProfileError(FILE_TOO_LARGE)
                        output.write(chunk)
    except TimeoutError as exc:
        elapsed = time.monotonic()-stats['started']
        detail = f"阶段={stats['phase']}，已接收={stats['received']}字节，耗时={elapsed:.1f}秒"
        logger.warning('Spark file download timeout: %s', detail)
        raise TimeoutError(detail) from exc
    except aiohttp.ClientError as exc:
        # aiohttp messages include the URL; show the type only.
        raise ProfileError(f'文件下载失败（{type(exc).__name__}），请重新发送文件') from None
