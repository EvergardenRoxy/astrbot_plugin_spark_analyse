import unittest
from spark_core.output import MAX_REPLY_CHARS, bound_reply, plain_text

class OutputTests(unittest.TestCase):
    def test_markdown_to_plain_text_keeps_values(self):
        raw = '## 结论\n**机器负载**\n| 路径 | 占比 |\n|---|---:|\n| `Create` | **11.48%** |\n\n- 暂停再恢复\n[报告](https://spark.lucko.me/abc123)'
        text = plain_text(raw)
        self.assertIn('路径：Create；占比：11.48%', text)
        self.assertIn('https://spark.lucko.me/abc123', text)
        for marker in ('##', '**', '`', '|---'): self.assertNotIn(marker, text)

    def test_plain_text_unchanged(self):
        self.assertEqual(plain_text('结论：机器负载较重\n① 暂停一处生产线'), '结论：机器负载较重\n① 暂停一处生产线')

    def test_report_links_stay_and_others_are_defanged(self):
        text = bound_reply(plain_text('见 [报告](https://spark.lucko.me/abc123) 和 [工具](https://evil.example/a.jar)，'
                                      '或 https://observable.tas.sh/p/Ab12cd 与 www.evil.example/x'))
        self.assertIn('https://spark.lucko.me/abc123', text)
        self.assertIn('https://observable.tas.sh/p/Ab12cd', text)
        self.assertIn('工具（evil[.]example/a.jar）', text)
        self.assertIn('www[.]evil[.]example/x', text)
        self.assertNotIn('https://evil', text)
        self.assertTrue(text.endswith('已改为不可点击的形式）'))

    def test_lookalike_hosts_are_not_trusted(self):
        for url in ('https://spark.lucko.me.evil.example/a', 'https://spark.lucko.me@evil.example/a',
                    'http://[bad/a', 'http://[::1]:8080/a', 'HTTPS://EVIL.EXAMPLE/a'):
            with self.subTest(url=url):
                self.assertNotIn(url, bound_reply('链接 '+url))

    def test_reply_without_links_is_unchanged_and_long_reply_is_cut(self):
        self.assertEqual(bound_reply('结论：不卡'), '结论：不卡')
        long = bound_reply('字'*(MAX_REPLY_CHARS+500))
        self.assertTrue(long.startswith('字'*MAX_REPLY_CHARS))
        self.assertTrue(long.endswith('（回复过长，已截断）'))
        self.assertLess(len(long), MAX_REPLY_CHARS+20)
