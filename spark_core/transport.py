import re
import zlib
import time
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

LINK = re.compile(r'https://spark\.lucko\.me/([A-Za-z0-9]{6,64})(?![A-Za-z0-9/._-])')


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
    timeout = aiohttp.ClientTimeout(total=timeout_seconds, connect=min(30, timeout_seconds))
    async with aiohttp.ClientSession(timeout=timeout, auto_decompress=False, trust_env=False) as session:
        async with session.get(f'https://spark-usercontent.lucko.me/{key}', allow_redirects=False, proxy=proxy) as response:
            if response.status != 200:
                raise ProfileError(f'报告下载失败 HTTP {response.status}')
            if response.headers.get('Content-Type', '').split(';')[0] != 'application/x-spark-sampler':
                raise ProfileError('报告类型不是sampler')
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
                        raise ProfileError('下载体积超限')
                    data = decoder.decompress(chunk, decoded_limit-written+1) if decoder else chunk
                    written += len(data)
                    stats['written'] = written
                    now = time.monotonic()
                    if now-last_progress >= 30:
                        logger.info('Spark download progress; received_bytes=%s decoded_bytes=%s elapsed=%.1fs', received, written, now-stats['started'])
                        last_progress = now
                    if written > decoded_limit or (decoder and decoder.unconsumed_tail):
                        raise ProfileError('解压体积超限')
                    output.write(data)
                if decoder and (not decoder.eof or decoder.unused_data):
                    raise ProfileError('gzip流截断或存在额外数据')
            if not written:
                raise ProfileError('报告为空')
