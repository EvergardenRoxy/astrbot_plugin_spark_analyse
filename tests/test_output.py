import unittest
from spark_core.output import plain_text

class OutputTests(unittest.TestCase):
    def test_markdown_to_plain_text_keeps_values(self):
        raw = '## 结论\n**机器负载**\n| 路径 | 占比 |\n|---|---:|\n| `Create` | **11.48%** |\n\n- 暂停再恢复\n[报告](https://spark.lucko.me/abc123)'
        text = plain_text(raw)
        self.assertIn('路径：Create；占比：11.48%', text)
        self.assertIn('https://spark.lucko.me/abc123', text)
        for marker in ('##', '**', '`', '|---'): self.assertNotIn(marker, text)

    def test_plain_text_unchanged(self):
        self.assertEqual(plain_text('结论：机器负载较重\n① 暂停一处生产线'), '结论：机器负载较重\n① 暂停一处生产线')
