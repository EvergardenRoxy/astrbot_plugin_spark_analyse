import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from spark_core.session import ReportSession, LoadTimeout

class LoadTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_timeout_is_labeled(self):
        with tempfile.TemporaryDirectory() as root:
            s = ReportSession(root)
            try:
                with patch('spark_core.session.download', AsyncMock(side_effect=TimeoutError())):
                    with self.assertRaisesRegex(LoadTimeout, '下载阶段'):
                        await s.load('https://spark.lucko.me/95tLrddUhW')
                self.assertIsNone(s.process)
            finally:
                await s.close()

    async def test_cache_reused_after_parse_timeout(self):
        from pathlib import Path
        from spark_core.cache import ProfileCache
        with tempfile.TemporaryDirectory() as root:
            cache = ProfileCache(Path(root)/'profiles.sqlite3')
            downloads = []
            async def fake_download(url, path, **kwargs):
                downloads.append(url)
                path.write_bytes(b'complete-download')
            process = AsyncMock()
            process.returncode = 0
            for _ in range(2):
                s = ReportSession(root, cache=cache)
                try:
                    with patch('spark_core.session.download', fake_download), patch('asyncio.create_subprocess_exec', AsyncMock(return_value=process)), patch.object(s, 'wait_file', AsyncMock(side_effect=TimeoutError())):
                        with self.assertRaises(LoadTimeout): await s.load('https://spark.lucko.me/95tLrddUhW')
                finally:
                    await s.close()
            self.assertEqual(len(downloads), 1)

    async def test_parse_timeout_is_labeled(self):
        with tempfile.TemporaryDirectory() as root:
            s = ReportSession(root)
            async def fake_download(url, path, **kwargs): path.write_bytes(b'data')
            process = AsyncMock()
            process.returncode = 0
            try:
                with patch('spark_core.session.download', fake_download), patch('asyncio.create_subprocess_exec', AsyncMock(return_value=process)), patch.object(s, 'wait_file', AsyncMock(side_effect=TimeoutError())):
                    with self.assertRaisesRegex(LoadTimeout, '解析/证据包'):
                        await s.load('https://spark.lucko.me/95tLrddUhW')
            finally:
                await s.close()
