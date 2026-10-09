import unittest
from pathlib import Path
from unittest.mock import patch
from spark_core.transport import download

class DownloadDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_keeps_phase_and_progress(self):
        async def stalled(url, path, compressed_limit, decoded_limit, timeout_seconds, stats, proxy):
            self.assertEqual(compressed_limit, 16*1024*1024)
            self.assertEqual(decoded_limit, 128*1024*1024)
            self.assertEqual(timeout_seconds, 900)
            stats.update(phase='读取响应体', received=65536, written=200000)
            raise TimeoutError()
        with patch('spark_core.transport._download', stalled):
            with self.assertRaisesRegex(TimeoutError, '读取响应体.*65536.*200000'):
                await download('https://spark.lucko.me/SyntheticReportA', Path('unused'), timeout_seconds=900)
