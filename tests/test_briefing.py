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


def overview(tps=20.0, median=11.6, peak=30.0, players=None, chunks=None, wait=0.0):
    profile = Profile(sample().SerializeToString())
    result = profile.overview()
    result['evidence_pack'] = profile.evidence_pack()
    result['evidence_pack']['threads'][0].update(denominator_ms=1000.0, wait_ms=wait)
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
        signals = briefing.triage(overview(20.45, 11.6, 1806.36, players=3, chunks=7193, wait=604.0))
        self.assertEqual((signals['load'], signals['spikes']), ('none', True))
        self.assertEqual(signals['spike_ratio'], 155.7)
        self.assertEqual((signals['wait_pct'], signals['wait_kind']), (60.4, 'between_ticks'))
        self.assertEqual(signals['chunks_per_player'], 2397.7)
        for phrase in ('未见持续过载', '偶发尖峰', '1806', '60.4%', '空闲', '7193'):
            self.assertIn(phrase, signals['summary'])

    def test_wait_kind_follows_the_tick_budget(self):
        # A tick has a 50 ms budget. When the median tick ends well inside it, the thread waits for the next tick
        # (spare capacity); once the median exceeds it there is no time between ticks, so waits are inside ticks.
        # Only MSPT decides; TPS can dip from spikes alone (a Folia-like report: TPS 19.0, median 7.2 ms).
        cases = [((19.0, 7.2), 'between_ticks'), ((20.0, 39.9), 'between_ticks'), ((20.0, 40.0), 'unknown'),
                 ((19.0, 49.9), 'unknown'), ((19.0, 50.0), 'in_tick'), ((13.1, 54.7), 'in_tick'), ((20.0, None), 'unknown')]
        for (tps, median), expected in cases:
            with self.subTest(tps=tps, median=median):
                self.assertEqual(briefing.triage(overview(tps, median, wait=500.0))['wait_kind'], expected)
        self.assertIn('tick 内', briefing.triage(overview(13.1, 54.7, wait=747.0))['summary'])
        self.assertIn('无法判断', briefing.triage(overview(20.0, 45.0, wait=300.0))['summary'])

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
        # Maintainer decisions: no platform or audience restriction; fixed sections; point to experienced people.
        for word in ('QQ', '服主', '群'):
            self.assertNotIn(word, reply)
        for section in ('现状', '疑似问题点', '建议先做的检查', '有经验'):
            self.assertIn(section, reply)

    def test_built_in_rules_cover_plugin_servers(self):
        policy = (ROOT/'analysis_policy.md').read_text(encoding='utf-8')
        guide = (ROOT/'diagnosis_guide.md').read_text(encoding='utf-8')
        for word in ('Paper', 'Folia', '插件'):
            self.assertIn(word, policy)
        # Wait interpretation comes from triage, not from method names that only some platforms use.
        for text in (policy, guide):
            self.assertNotIn('idle_between_ticks', text)

    def test_prompt_files_are_packaged(self):
        main = (ROOT/'main.py').read_text(encoding='utf-8')
        package = (ROOT/'tools'/'package_plugin.py').read_text(encoding='utf-8')
        import re
        for name in re.findall(r"with_name\('([^']+)'\)", main):
            with self.subTest(name=name):
                self.assertIn(f"'{name}'", package)


def observable_view(**changes):
    from test_observable import ObservableProfile, report
    return ObservableProfile(report(**changes)).overview()


class ObservableBriefingTests(unittest.TestCase):
    def test_cost_classes_follow_the_tick_budget(self):
        view = observable_view()
        for budget, cost in ((7.1, 'light'), (19.9, 'light'), (20, 'notable'), (49.9, 'notable'), (50, 'heavy')):
            with self.subTest(budget=budget):
                view['totals']['tick_budget_pct'] = budget
                self.assertEqual(briefing.observable_triage(view)['cost'], cost)
        view['totals']['tick_budget_pct'] = None
        self.assertEqual(briefing.observable_triage(view)['cost'], 'unknown')

    def test_load_comes_from_the_tps_estimate(self):
        view = observable_view()
        for tps, load in ((20.0, 'none'), (19.5, 'none'), (19.4, 'mild'), (15, 'mild'), (14.9, 'sustained'), (None, 'unknown')):
            with self.subTest(tps=tps):
                view['runtime']['tps_estimate'] = tps
                self.assertEqual(briefing.observable_triage(view)['load'], load)

    def test_dominant_and_concentrated_types(self):
        triage = briefing.observable_triage(observable_view())
        self.assertEqual(triage['top_type'], 'create:contraption')
        self.assertTrue(triage['dominant_type'])
        self.assertTrue(triage['concentrated'])
        self.assertIn('create:contraption', triage['summary'])
        view = observable_view()
        view['entity_types'] = [{'type': f'm:t{i}', 'share_pct': 10} for i in range(10)]
        view['block_types'] = []
        triage = briefing.observable_triage(view)
        self.assertFalse(triage['dominant_type'])
        self.assertFalse(triage['concentrated'])
        self.assertIn('分散', triage['summary'])

    def test_empty_ranking_does_not_crash(self):
        view = observable_view()
        view.update(entity_types=[], block_types=[], hot_chunks=[])
        triage = briefing.observable_triage(view)
        self.assertIsNone(triage['top_type'])
        self.assertIsNone(triage['hot_chunk_share_pct'])

    def test_spark_only_payload_is_unchanged_by_the_new_parameters(self):
        payload = json.loads(briefing.user_payload('卡', overview()))
        self.assertEqual(set(payload), {'user_observation', 'overview', 'history_comparison',
                                        'history_comparison_unavailable', 'previous_review_untrusted'})

    def test_payload_shapes_for_each_report_kind(self):
        both = json.loads(briefing.user_payload('卡', overview(), observable=observable_view(), pairing={'how': 'same_message'}))
        self.assertEqual(both['report_kind'], 'spark+observable')
        self.assertEqual(next(iter(both['observable'])), 'triage')
        only = json.loads(briefing.user_payload('卡', None, observable=observable_view()))
        self.assertEqual(only['report_kind'], 'observable')
        self.assertNotIn('overview', only)

    def test_jvm_flags_are_summarised_unless_asked(self):
        view = observable_view()
        view['runtime']['jvm_flags'] = '-Xmx4G -Dfoo=secret -XX:+UseG1GC'
        trimmed = briefing.model_observable(view, '为什么卡')['runtime']['jvm_flags']
        self.assertEqual(trimmed['key_flags'], ['-Xmx4G', '-XX:+UseG1GC'])
        self.assertNotIn('secret', json.dumps(trimmed))
        self.assertIn('secret', briefing.model_observable(view, 'JVM参数有问题吗')['runtime']['jvm_flags'])
        self.assertEqual(view['runtime']['jvm_flags'], '-Xmx4G -Dfoo=secret -XX:+UseG1GC')  # input is not modified


class CorrelationTests(unittest.TestCase):
    NOW = 1_760_000_000_000

    def pair(self, spark_start=None, spark_end=None, **spark):
        s = overview()
        s['platform'].update({'name': 'NeoForge', 'minecraft': '1.21.1', **spark})
        s['sampling'].update(start_ms=spark_start, end_ms=spark_end)
        return briefing.correlate(s, observable_view())  # Observable window: NOW .. NOW+10 s

    def test_overlapping_windows(self):
        result = self.pair(self.NOW-60_000, self.NOW+5_000)
        self.assertEqual((result['verdict'], result['sampling_windows_overlap'], result['sampling_gap_seconds']), ('overlapping', True, 0))
        self.assertEqual((result['minecraft'], result['mod_loader']), ('same', 'same'))

    def test_near_and_apart_use_the_pairing_distance(self):
        end = self.NOW-briefing.PAIR_NEAR_SECONDS*1000
        self.assertEqual(self.pair(end-60_000, end)['verdict'], 'near')
        self.assertEqual(self.pair(end-120_000, end-1_000)['verdict'], 'apart')

    def test_unknown_times(self):
        self.assertEqual(self.pair()['verdict'], 'time_unknown')

    def test_different_version_or_loader_conflicts(self):
        self.assertEqual(self.pair(self.NOW, self.NOW+1000, minecraft='1.20.1')['verdict'], 'conflict')
        result = self.pair(self.NOW, self.NOW+1000, name='Fabric')
        self.assertEqual((result['verdict'], result['mod_loader']), ('conflict', 'different'))
        # Forge is not NeoForge.
        self.assertEqual(self.pair(self.NOW, self.NOW+1000, name='Forge')['verdict'], 'conflict')

    def test_bukkit_family_server_cannot_pair_with_observable(self):
        self.assertEqual(self.pair(self.NOW, self.NOW+1000, name='Bukkit', brand='Paper')['verdict'], 'conflict')

    def test_missing_fields_are_unknown_not_conflicting(self):
        result = self.pair(self.NOW, self.NOW+1000, minecraft='', name='', brand='')
        self.assertEqual((result['minecraft'], result['mod_loader'], result['verdict']), ('unknown', 'unknown', 'overlapping'))

    def test_cost_ratio_is_given_only_without_conflict(self):
        ok = self.pair(self.NOW, self.NOW+1000)
        self.assertEqual(ok['observed_ms_vs_spark_mspt_median_pct'], round(100*3.55/11.6, 1))
        self.assertNotIn('observed_ms_vs_spark_mspt_median_pct', self.pair(self.NOW, self.NOW+1000, minecraft='1.20.1'))


if __name__ == '__main__': unittest.main()
