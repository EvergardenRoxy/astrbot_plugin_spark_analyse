import json
import unittest
from pathlib import Path
from spark_core import briefing
from spark_core.profile import Profile
from test_core import sample

ROOT = Path(__file__).resolve().parents[1]

# The reply prompt that shipped as the default up to 1.0.4. A saved config holding it was never customised.
LEGACY_DEFAULT_REPLY = '''你面向QQ群里的Minecraft服主回复，不是在写技术审计报告。只输出中文纯文本，禁止Markdown标题、加粗、反引号、表格、代码块。不展示JSON、原始字段名、大段Java方法名、节点编号或分母算式。

默认控制在500～900个中文字符左右；无需凑字数。按以下顺序组织，用短段落和①②③：
结论：先用1～2句话回答卡在哪里、先查什么。区分“已观察到”和“候选原因”，不能确定时直说。
主要问题：至多3项，每项用玩家能理解的名字（例如机器物流、区块活动、玩家数据保存），说明关键数值和意义。占比要写“主线程采样占比”，不要写成CPU占比。最关键的不确定性紧跟对应判断，不另写大篇免责条款。
先怎么做：给一个低风险、可逆的验证行动，说明如何对照采样判断有效。不要同时建议一堆调整。
补充：只写影响结论的采样限制，例如只采主线程或无法确认具体机器地点。

不要重复开场确认；不要泛泛解释profiling术语。环境和TPS/MSPT压缩成一行即可。对用户现象没有证据时明确承认，不把高占比自动解释成异常。证据核对必须在分析过程完成，聊天只保留必要数字和短证据描述，用户明确要技术详情才展开节点、窗口及路径。
'''


def overview(tps=20.0, median=11.6, peak=30.0, players=None, chunks=None, idle=0.0, other=0.0):
    profile = Profile(sample().SerializeToString())
    result = profile.overview()
    result['evidence_pack'] = profile.evidence_pack()
    result['evidence_pack']['threads'][0].update(denominator_ms=1000.0, idle_between_ticks_ms=idle, other_wait_ms=other)
    result['health'].update(tps_1m=tps, mspt_median=median, mspt_max=peak)
    result['window_health'] = [{'window': 10, 'players': players, 'chunks': chunks, 'entities': None}]
    return result


class TriageTests(unittest.TestCase):
    def test_load_classes_follow_maintainer_thresholds(self):
        cases = [((20.0, 11.6), 'none'), ((19.6, 39.0), 'none'), ((19.4, 20.0), 'mild'), ((20.0, 45.0), 'mild'),
                 ((15.0, 70.0), 'mild'), ((14.9, 30.0), 'sustained'), ((20.0, 70.5), 'sustained'),
                 ((None, None), 'unknown'), ((None, 80.0), 'sustained')]
        for (tps, median), expected in cases:
            with self.subTest(tps=tps, median=median):
                self.assertEqual(briefing.triage(overview(tps, median))['load'], expected)

    def test_spikes_need_both_ratio_and_absolute_size(self):
        cases = [((11.6, 1806.4), True), ((100.0, 499.0), False), ((200.0, 900.0), False),
                 ((100.0, 600.0), True), ((None, 900.0), None), ((50.0, None), None)]
        for (median, peak), expected in cases:
            with self.subTest(median=median, peak=peak):
                self.assertIs(briefing.triage(overview(20.0, median, peak))['spikes'], expected)

    def test_signals_and_summary(self):
        signals = briefing.triage(overview(20.45, 11.6, 1806.36, players=3, chunks=7193, idle=604.0))
        self.assertEqual((signals['load'], signals['spikes']), ('none', True))
        self.assertEqual(signals['spike_ratio'], 155.7)
        self.assertEqual(signals['idle_between_ticks_pct'], 60.4)
        self.assertEqual(signals['chunks_per_player'], 2397.7)
        for phrase in ('未见持续过载', '偶发尖峰', '1806', '60.4%', '7193'):
            self.assertIn(phrase, signals['summary'])

    def test_unclassified_waiting_is_not_called_idle(self):
        # On obfuscated (Forge 1.20.1) names, between-tick idle cannot be told apart from stalls.
        signals = briefing.triage(overview(other=600.0))
        self.assertEqual(signals['idle_between_ticks_pct'], 0.0)
        self.assertIn('无法区分', signals['summary'])

    def test_no_players_gives_no_ratio(self):
        self.assertIsNone(briefing.triage(overview(players=0, chunks=500))['chunks_per_player'])


class OverviewTests(unittest.TestCase):
    ARGS = '-Xms4G -Xmx8G -XX:+UseG1GC -XX:MaxGCPauseMillis=50 -Dfoo=bar -XX:+UnlockExperimentalVMOptions --add-opens=java.base/x=ALL'

    def test_vm_args_summary_keeps_memory_and_gc_flags(self):
        summary = briefing.summarize_vm_args(self.ARGS)
        self.assertEqual(summary['key_flags'], ['-Xms4G', '-Xmx8G', '-XX:+UseG1GC', '-XX:MaxGCPauseMillis=50'])
        self.assertEqual(summary['other_flag_count'], 3)

    def test_full_vm_args_only_for_jvm_questions(self):
        source = overview()
        source['runtime']['system'].setdefault('java', {})['vm_args'] = self.ARGS
        trimmed = briefing.model_overview(source, '服务器卡顿怎么办')
        self.assertIsInstance(trimmed['runtime']['system']['java']['vm_args'], dict)
        self.assertEqual(source['runtime']['system']['java']['vm_args'], self.ARGS)
        for question in ('看看JVM参数', '启动参数有问题吗', 'Xmx 设多少'):
            with self.subTest(question=question):
                self.assertEqual(briefing.model_overview(source, question)['runtime']['system']['java']['vm_args'], self.ARGS)

    def test_triage_comes_first(self):
        self.assertEqual(next(iter(briefing.model_overview(overview(), ''))), 'triage')

    def test_payload_keeps_previous_review_only_with_comparison(self):
        payload = json.loads(briefing.user_payload('卡', overview(), None, '旧结论'))
        self.assertIsNone(payload['previous_review_untrusted'])
        payload = json.loads(briefing.user_payload('卡', overview(), {'comparable_sampling': True}, '旧结论'))
        self.assertEqual(payload['previous_review_untrusted'], '旧结论')


class PromptTests(unittest.TestCase):
    def test_legacy_default_reply_is_recognised(self):
        self.assertTrue(briefing.is_legacy_reply(LEGACY_DEFAULT_REPLY))
        self.assertTrue(briefing.is_legacy_reply(LEGACY_DEFAULT_REPLY.replace('\n', '\r\n') + '  \n'))
        self.assertFalse(briefing.is_legacy_reply(LEGACY_DEFAULT_REPLY + '另外请说明GC。'))
        self.assertFalse(briefing.is_legacy_reply(''))

    def test_reply_requirements_follow_current_default_unless_customised(self):
        self.assertEqual(briefing.reply_requirements('', 'NEW'), 'NEW')
        self.assertEqual(briefing.reply_requirements(LEGACY_DEFAULT_REPLY, 'NEW'), 'NEW')
        self.assertEqual(briefing.reply_requirements('  我的要求  ', 'NEW'), '我的要求')

    def test_system_prompt_order(self):
        text = briefing.system_prompt('POLICY', 'GUIDE', 'REPLY')
        self.assertLess(text.index('POLICY'), text.index('GUIDE'))
        self.assertLess(text.index('GUIDE'), text.index('回复要求'))
        self.assertTrue(text.endswith('REPLY'))

    def test_shipped_prompt_files_agree(self):
        schema = json.loads((ROOT/'_conf_schema.json').read_text(encoding='utf-8'))
        reply = (ROOT/'reply_prompt.txt').read_text(encoding='utf-8')
        self.assertEqual(schema['analysis_prompt']['default'], reply)
        self.assertFalse(briefing.is_legacy_reply(reply))
        # Maintainer decision: no platform or audience restriction in the default reply prompt.
        for word in ('QQ', '服主', '群'):
            self.assertNotIn(word, reply)

    def test_prompt_files_are_packaged(self):
        main = (ROOT/'main.py').read_text(encoding='utf-8')
        package = (ROOT/'tools'/'package_plugin.py').read_text(encoding='utf-8')
        import re
        for name in re.findall(r"with_name\('([^']+)'\)", main):
            with self.subTest(name=name):
                self.assertIn(f"'{name}'", package)


if __name__ == '__main__': unittest.main()
