import gzip
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from spark_core.transport import download
from spark_core.profile import ProfileError

class TransportSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def fetch(self, payload, headers=None, status=200, **limits):
        class Content:
            async def iter_chunked(self, size):
                for offset in range(0, len(payload), 16):
                    yield payload[offset:offset+16]
        class Response:
            content = Content()
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
        Response.status = status
        Response.headers = headers or {'Content-Type': 'application/x-spark-sampler'}
        class Session:
            def __init__(self, **kwargs):
                assert kwargs['trust_env'] is False
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            def get(self, url, **kwargs):
                assert kwargs['allow_redirects'] is False
                assert 'ssl' not in kwargs  # aiohttp default certificate verification
                return Response()
        with tempfile.TemporaryDirectory() as directory:
            with patch('spark_core.transport.aiohttp.ClientSession', Session):
                path = Path(directory)/'raw'
                await download('https://spark.lucko.me/SyntheticReportA', path, **limits)
                return path.read_bytes()

    async def test_gzip_valid(self):
        raw = b'valid synthetic data'*100
        self.assertEqual(await self.fetch(gzip.compress(raw), {'Content-Type':'application/x-spark-sampler', 'Content-Encoding':'gzip'}), raw)

    async def test_gzip_bomb_is_bounded(self):
        with self.assertRaisesRegex(ProfileError, '解压体积超限'):
            await self.fetch(gzip.compress(b'x'*100000), {'Content-Type':'application/x-spark-sampler', 'Content-Encoding':'gzip'}, decoded_limit=1000)

    async def test_oversized_report_message_explains_the_remedy(self):
        # Long profiles (hours, many time windows) exceed the limit; tell the user how to get a usable report.
        with self.assertRaisesRegex(ProfileError, r'--timeout 300'):
            await self.fetch(gzip.compress(b'x'*100000), {'Content-Type':'application/x-spark-sampler', 'Content-Encoding':'gzip'}, decoded_limit=1000)

    async def test_truncated_and_concatenated_gzip_rejected(self):
        raw = gzip.compress(b'synthetic')
        for payload in (raw[:-4], raw+raw):
            with self.subTest(length=len(payload)):
                with self.assertRaises(ProfileError):
                    await self.fetch(payload, {'Content-Type':'application/x-spark-sampler', 'Content-Encoding':'gzip'})

    async def test_transfer_and_identity_decoded_limits(self):
        for limits in ({'compressed_limit':10}, {'decoded_limit':10}):
            with self.assertRaises(ProfileError):
                await self.fetch(b'x'*100, **limits)

    async def test_redirect_type_encoding_and_empty_rejected(self):
        cases = [({}, 302, b'x'), ({'Content-Type':'text/html'}, 200, b'x'), ({'Content-Type':'application/x-spark-sampler','Content-Encoding':'br'}, 200, b'x'), ({},200,b'')]
        for headers, status, payload in cases:
            with self.subTest(status=status, headers=headers):
                with self.assertRaises(ProfileError):
                    await self.fetch(payload, headers, status)

    async def test_nonofficial_url_rejected_before_connection(self):
        for url in ('http://127.0.0.1/a', 'https://spark.lucko.me.evil.test/abcdef', 'https://spark.lucko.me/abcdef?url=http://localhost', 'https://spark.lucko.me/../abcdef'):
            with self.subTest(url=url), patch('spark_core.transport.aiohttp.ClientSession') as session:
                with self.assertRaises(ProfileError):
                    await download(url, Path('unused'))
                session.assert_not_called()
