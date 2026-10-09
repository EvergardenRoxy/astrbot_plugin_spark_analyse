"""Marketplace logger policy; intentionally no host-log filtering."""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class LoggingTests(unittest.TestCase):
    def test_runtime_uses_only_official_logger(self):
        files = [ROOT/'main.py', *(ROOT/'spark_core').rglob('*.py')]
        for path in files:
            with self.subTest(path=path.relative_to(ROOT)):
                tree = ast.parse(path.read_text(encoding='utf-8'))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        self.assertFalse(any(a.name == 'logging' or a.name.startswith('logging.') for a in node.names))
                    if isinstance(node, ast.ImportFrom):
                        self.assertFalse((node.module or '').startswith('logging'))
                        if any(a.name == 'logger' for a in node.names):
                            self.assertEqual(node.module, 'astrbot.api')
                    if isinstance(node, ast.Attribute):
                        self.assertNotIn(node.attr, ('getLogger', 'addFilter', 'removeFilter', 'setLevel'))
        self.assertFalse((ROOT/'spark_core/logging_scope.py').exists())
