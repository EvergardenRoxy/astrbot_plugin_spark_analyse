import re
import zlib
import aiohttp
from .profile import ProfileError

LINK = re.compile(r'https://spark\.lucko\.me/([A-Za-z0-9]{6,64})(?![A-Za-z0-9/._-])')


def report_id(url):
    match = re.fullmatch(r'https://spark\.lucko\.me/([A-Za-z0-9]{6,64})/?', url)
    if not match:
        raise ProfileError('只接受官方HTTPS spark报告链接，不接受自定义URL')
    return match.group(1)


async def download(url, path, compressed_limit=16*1024*1024, decoded_limit=128*1024*1024, timeout_seconds=120):
    key = report_id(url)
    timeout = aiohttp.ClientTimeout(total=timeout_seconds, connect=min(30, timeout_seconds))
    async with aiohttp.ClientSession(timeout=timeout, auto_decompress=False, trust_env=False) as session:
        async with session.get(f'https://spark-usercontent.lucko.me/{key}', allow_redirects=False) as response:
            if response.status != 200:
                raise ProfileError(f'报告下载失败 HTTP {response.status}')
            if response.headers.get('Content-Type', '').split(';')[0] != 'application/x-spark-sampler':
                raise ProfileError('报告类型不是sampler')
            encoding = response.headers.get('Content-Encoding', '').lower()
            if encoding not in ('', 'identity', 'gzip'):
                raise ProfileError('不支持的HTTP压缩格式')
            decoder = zlib.decompressobj(16+zlib.MAX_WBITS) if encoding == 'gzip' else None
            received = written = 0
            with path.open('wb') as output:
                async for chunk in response.content.iter_chunked(65536):
                    received += len(chunk)
                    if received > compressed_limit:
                        raise ProfileError('下载体积超限')
                    data = decoder.decompress(chunk, decoded_limit-written+1) if decoder else chunk
                    written += len(data)
                    if written > decoded_limit or (decoder and decoder.unconsumed_tail):
                        raise ProfileError('解压体积超限')
                    output.write(data)
                if decoder and (not decoder.eof or decoder.unused_data):
                    raise ProfileError('gzip流截断或存在额外数据')
            if not written:
                raise ProfileError('报告为空')
