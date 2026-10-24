"""Minecraft-specific regression cases from the official viewer comparison."""
import asyncio
import json
import tempfile
import unittest
from spark_core.briefing import triage, user_payload
from spark_core.history import compare
from spark_core.profile import Profile, ProfileError
from spark_core.session import ReportSession
from test_core import sample


def overview(data):
    p = Profile(data.SerializeToString())
    return {**p.overview(), 'evidence_pack': p.evidence_pack()}


def healthy(target=20, tps=20, median=10):
    d = sample()
    s = d.metadata.platform_statistics
    s.tps.game_target_tps, s.tps.last1m = target, tps
    s.mspt.game_max_ideal_mspt = 1000 // target
    s.mspt.last1m.median, s.mspt.last1m.max = median, median * 2
    return d


class MinecraftAnalysisTests(unittest.TestCase):
    def test_custom_tick_rates_use_report_targets(self):
        for target, actual, median, expected in ((10, 10, 60, 'none'), (40, 33, 30, 'mild'),
                                                  (20, 14, 30, 'sustained')):
            with self.subTest(target=target):
                s = triage(overview(healthy(target, actual, median)))
                self.assertEqual(s['load'], expected)
                self.assertEqual(s['target_tps'], target)
                self.assertEqual(s['tick_budget_ms'], 1000 // target)

    def test_missing_target_falls_back_and_budget_can_be_derived(self):
        d = healthy(10, 10, 60)
        d.metadata.platform_statistics.mspt.game_max_ideal_mspt = 0
        s = triage(overview(d))
        self.assertEqual((s['load'], s['tick_budget_ms']), ('none', 100))
        d.metadata.platform_statistics.tps.game_target_tps = 0
        self.assertEqual(triage(overview(d))['target_tps'], 20)

    def test_filtered_samples_do_not_become_idle_capacity(self):
        d = healthy()
        d.metadata.data_aggregator.type = 1
        d.metadata.data_aggregator.tick_length_threshold = 100
        d.metadata.start_time, d.metadata.end_time = 1000000, 1300000
        d.threads[0].children[0].class_name = 'jdk.internal.misc.Unsafe'
        d.threads[0].children[0].method_name = 'park'
        s = triage(overview(d))
        self.assertEqual(s['wait_kind'], 'unknown')
        self.assertEqual(s['wait_reason'], 'filtered_ticks')
        self.assertNotIn('这些主要是 tick 之间的空闲', s['summary'])
        self.assertIn('慢 tick', s['summary'])

    def test_rolling_median_cannot_prove_where_waits_happened(self):
        for median in (10, 60):
            s = triage(overview(healthy(median=median)))
            self.assertEqual(s['wait_kind'], 'unknown')
            self.assertEqual(s['metric_scope'], 'last_1m_at_report_end')

    def test_workers_and_grouped_threads_are_not_tick_threads(self):
        d = healthy()
        d.threads[0].name = 'Async Worker'
        s = triage(overview(d))
        self.assertIsNone(s['tick_thread'])
        self.assertIsNone(s['wait_pct'])
        d.threads[0].name = 'Server thread'
        d.metadata.data_aggregator.thread_grouper = 2
        self.assertIsNone(triage(overview(d))['tick_thread'])

    def test_folia_region_threads_keep_individual_roles(self):
        d = healthy()
        d.metadata.platform_metadata.brand = 'Folia'
        d.threads[0].name = 'Region Scheduler Thread #0'
        d.threads.add(name='Region Scheduler Thread #1', times=[200, 200])
        v = overview(d)
        self.assertTrue(all(t['role'] == 'region_tick' for t in v['evidence_pack']['threads']))
        s = triage(v)
        self.assertIsNone(s['tick_thread'])
        self.assertEqual(len(s['region_threads']), 2)

    def test_clients_and_proxies_do_not_get_server_load_verdicts(self):
        for kind in (1, 2, 3):
            d = healthy(tps=5, median=200)
            d.metadata.platform_metadata.type = kind
            s = triage(overview(d))
            self.assertEqual(s['load'], 'unknown')
            self.assertIsNone(s['tick_thread'])

    def test_method_line_class_source_precedence(self):
        d = sample()
        n = d.threads[0].children[0]
        n.method_desc, n.line_number = '()V', 42
        d.class_sources[n.class_name] = 'base'
        d.line_sources[n.class_name+';42'] = 'line-injector'
        d.method_sources[n.class_name+';work;()V'] = 'method-injector'
        for expected, evidence in (('method-injector', 'method_mapping'), ('line-injector', 'line_mapping'),
                                   ('base', 'class_mapping')):
            row = next(r for r in Profile(d.SerializeToString()).query()['rows'] if r['node'] == 0)
            self.assertEqual((row['source'], row['source_evidence']), (expected, evidence))
            self.assertEqual(row['method_desc'], '()V')
            if d.method_sources:
                d.method_sources.clear()
            else:
                d.line_sources.clear()

    def test_distributed_method_self_cost_is_aggregated_with_a_real_path(self):
        d = sample()
        t = d.threads[0]
        del t.children[:]
        t.children_refs[:] = range(7)
        t.times[:] = [100, 100]
        for i in range(7):
            t.children.add(class_name='A' if i < 6 else 'B', method_name='work', method_desc='()V',
                           times=[10 if i < 6 else 40, 10 if i < 6 else 40])
        p = Profile(d.SerializeToString())
        first = p.query(view='methods')['rows'][0]
        self.assertEqual((first['method'], first['self_ms'], first['occurrences']), ('A.work', 120, 6))
        self.assertEqual(first['representative_self_ms'], 20)
        self.assertNotIn('inclusive_ms', first)  # No sum of recursive inclusive costs.
        self.assertEqual(p.query(view='methods', window=10)['rows'][0]['self_ms'], 60)
        pack = p.evidence_pack()['threads'][0]
        self.assertEqual(pack['hotspots'][0]['path'][-1]['node'], first['node'])
        self.assertEqual(pack['selected_self_coverage_pct'], 100)

    def test_overloads_and_injected_sources_stay_separate(self):
        d = sample()
        t = d.threads[0]
        t.times[:] = [110, 210]
        n = t.children.add(class_name='test.Mod', method_name='work', method_desc='(I)V', times=[10, 10])
        t.children_refs.append(2)
        p = Profile(d.SerializeToString())
        rows = p.query(view='methods')['rows']
        self.assertEqual(len([r for r in rows if r['method'] == 'test.Mod.work']), 2)

    def test_same_method_with_different_line_sources_is_not_misattributed(self):
        d = sample()
        t = d.threads[0]
        t.times[:] = [110, 210]
        t.children[0].line_number = 1
        t.children.add(class_name='test.Mod', method_name='work', line_number=2, times=[10, 10])
        t.children_refs.append(2)
        d.line_sources['test.Mod;1'], d.line_sources['test.Mod;2'] = 'injector-a', 'injector-b'
        rows = Profile(d.SerializeToString()).query(view='methods')['rows']
        self.assertEqual({r['source'] for r in rows if r['method'] == 'test.Mod.work'}, {'injector-a', 'injector-b'})

    def test_per_tick_cost_uses_included_ticks_for_filtered_sampling(self):
        d = healthy()
        d.metadata.number_of_ticks = 1000
        d.metadata.data_aggregator.type = 1
        d.metadata.data_aggregator.number_of_included_ticks = 10
        p = Profile(d.SerializeToString())
        row = p.query(view='methods')['rows'][0]
        self.assertEqual(row['self_ms_per_tick'], 23)
        # A window's tick count may include unprofiled ticks; do not invent a filtered divisor.
        d.time_window_statistics[10].ticks = 100
        self.assertIsNone(Profile(d.SerializeToString()).query(view='methods', window=10)['rows'][0]['self_ms_per_tick'])

    def test_window_query_validates_ids_and_pages(self):
        p = Profile(sample().SerializeToString())
        for args in ({'window': True}, {'offset': True}, {'offset': 1001}):
            with self.subTest(args=args), self.assertRaises(ProfileError):
                p.query(view='windows', **args)

    def test_earlier_spike_is_visible_after_recent_recovery(self):
        d = healthy()
        w = d.time_window_statistics[10]
        w.tps, w.mspt_median, w.mspt_max = 20, 10, 1500
        s = triage(overview(d))
        self.assertFalse(s['spikes'])
        self.assertTrue(s['window_spikes'])
        self.assertIn('采样窗口曾记录长 tick', s['summary'])

    def test_player_chunk_ratio_uses_the_same_snapshot(self):
        d = healthy()
        d.time_window_statistics[10].players, d.time_window_statistics[10].chunks = 10, 200
        d.time_window_statistics[11].players, d.time_window_statistics[11].chunks = 2, 1000
        s = triage(overview(d))
        self.assertEqual((s['players'], s['chunks'], s['chunks_per_player']), (2, 1000, 500))

    def test_invalid_health_numbers_are_unknown(self):
        d = healthy()
        d.metadata.platform_statistics.mspt.last1m.median = float('nan')
        d.time_window_statistics[10].tps = float('inf')
        v = overview(d)
        self.assertIsNone(v['health']['mspt_median'])
        self.assertIsNone(v['window_health'][0]['tps'])
        json.dumps(v, allow_nan=False)

    def test_recursive_method_self_is_counted_once_per_frame(self):
        d = sample()
        t = d.threads[0]
        t.children[1].class_name, t.children[1].method_name = 'test.Mod', 'work'
        row = Profile(d.SerializeToString()).query(view='methods')['rows'][0]
        self.assertEqual((row['self_ms'], row['occurrences']), (300, 2))

    def test_bad_last_window_survives_bounded_overview(self):
        d = healthy()
        d.time_windows[:] = range(121)
        d.threads[0].times[:] = [100] * 121
        for n in d.threads[0].children:
            n.times[:] = [10] * 121
        for i in range(121):
            w = d.time_window_statistics[i]
            w.tps, w.mspt_median, w.mspt_max = (20, 10, 20) if i < 120 else (5, 180, 1500)
        p = Profile(d.SerializeToString())
        v = p.overview()
        self.assertEqual(len(v['window_health']), 120)
        self.assertEqual(v['window_summary']['worst_tps']['window'], 120)
        self.assertEqual(v['window_summary']['peak_mspt']['mspt_max'], 1500)
        self.assertEqual(v['window_summary']['windows_with_tps'], 121)
        row = p.query(view='windows', offset=120)['rows'][0]
        self.assertEqual((row['window'], row['tps']), (120, 5))
        payload = json.loads(user_payload('', {**v, 'evidence_pack': p.evidence_pack()}))
        self.assertIn('采样窗口', payload['overview']['triage']['summary'])

    def test_history_compares_tick_targets(self):
        old, new = overview(healthy()), overview(healthy(10, 10, 10))
        c = compare(old, new)
        self.assertFalse(c['comparable_sampling'])
        self.assertIn('sampling.target_tps', c['different_conditions'])


class MinecraftWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_evidence_and_queries_work_through_real_worker(self):
        with tempfile.TemporaryDirectory() as root:
            session = ReportSession(root)
            try:
                (session.directory/'profile.bin').write_bytes(healthy().SerializeToString())
                v = await asyncio.wait_for(session.parse(), 10)
                self.assertEqual(v['sampling']['target_tps'], 20)
                self.assertEqual(v['evidence_pack']['hotspot_view'], 'methods')
                for view in ('methods', 'windows'):
                    r = await session.query(view=view, thread=0)
                    self.assertNotIn('error', r)
                    self.assertLessEqual(len(r['rows']), 20)
            finally:
                await session.close()
