import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from spark_core.transport import download, validate_proxy
from spark_core.profile import ProfileError

class ProxyTests(unittest.IsolatedAsyncioTestCase):
    def test_validate_without_leaking_credentials(self):
        self.assertEqual(validate_proxy('http://localhost:8080'), 'http://localhost:8080')
        for value in ('', 'socks5://localhost:1080', 'http://user:secret@host:bad', 'http://host/path'):
            with self.assertRaises(ProfileError) as caught:
                validate_proxy(value)
            self.assertNotIn('secret', str(caught.exception))

    async def test_explicit_proxy_and_direct_ignore_environment(self):
        seen = []
        class Content:
            async def iter_chunked(self, size):
                yield b'synthetic'
        class Response:
            status = 200
            headers = {'Content-Type': 'application/x-spark-sampler'}
            content = Content()
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
        class Session:
            def __init__(self, **kwargs):
                self_kwargs = kwargs
                assert self_kwargs['trust_env'] is False
                assert self_kwargs['auto_decompress'] is False
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            def get(self, url, **kwargs):
                seen.append((url, kwargs))
                return Response()
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict('os.environ', {'HTTPS_PROXY': 'http://environment.invalid:8080'}), patch('spark_core.transport.aiohttp.ClientSession', Session):
                await download('https://spark.lucko.me/SyntheticReportA', Path(directory)/'direct')
                await download('https://spark.lucko.me/SyntheticReportA', Path(directory)/'proxy', proxy='http://localhost:8080')
        self.assertIsNone(seen[0][1]['proxy'])
        self.assertEqual(seen[1][1]['proxy'], 'http://localhost:8080')
        self.assertTrue(all(not kwargs['allow_redirects'] for _, kwargs in seen))
        self.assertTrue(all(url.startswith('https://spark-usercontent.lucko.me/') for url, _ in seen))
