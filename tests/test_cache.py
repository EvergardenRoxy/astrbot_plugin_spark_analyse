import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from spark_core.cache import ProfileCache

class CacheTests(unittest.TestCase):
    def test_sliding_expiry_and_independent_blob(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source, target = root/'raw', root/'copy'
            source.write_bytes(b'profile')
            cache = ProfileCache(root/'profiles.sqlite3', 72)
            with patch('spark_core.cache.time.time', return_value=100): cache.put('abcdef', source)
            with patch('spark_core.cache.time.time', return_value=100+71*3600):
                self.assertTrue(cache.get('abcdef', target))
            with patch('spark_core.cache.time.time', return_value=100+140*3600):
                self.assertTrue(cache.get('abcdef', target))
            with patch('spark_core.cache.time.time', return_value=100+213*3600):
                self.assertFalse(cache.get('abcdef', target))
            self.assertEqual(target.read_bytes(), b'profile')
            self.assertFalse((root/'history.sqlite3').exists())
