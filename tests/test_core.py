import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from spark_core.profile import Profile, ProfileError
from spark_core.proto.spark_sampler_pb2 import SamplerData
from spark_core.history import History, compare
from spark_core.transport import LINK, report_id


def sample():
    d = SamplerData()
    d.metadata.platform_metadata.name = 'NeoForge'
    d.metadata.platform_metadata.minecraft_version = '1.21.1'
    d.metadata.interval = 4000
    d.time_windows.extend([10, 11])
    t = d.threads.add(name='Server thread', times=[100, 200], children_refs=[1])
    t.children.add(class_name='test.Mod', method_name='work', times=[30, 40])
    t.children.add(class_name='net.Tick', method_name='tick', times=[100, 200], children_refs=[0])
    return d


class CoreTests(unittest.TestCase):
    def test_self_and_denominator(self):
        p = Profile(sample().SerializeToString())
        rows = p.query()['rows']
        self.assertEqual(rows[0]['node'], 1)
        self.assertEqual(rows[0]['self_ms'], 230)
        self.assertAlmostEqual(rows[0]['self_pct'], 230/3)
        self.assertEqual(p.query(window=10)['denominator_ms'], 100)
        self.assertEqual([r['node'] for r in p.query(view='callers', node=0)['rows']], [1, 0])

    def test_invalid_refs_and_mode(self):
        for change in ('cycle', 'mode', 'nan', 'size'):
            d = sample()
            if change == 'cycle': d.threads[0].children[0].children_refs.append(1)
            if change == 'mode': d.metadata.sampler_mode = 1
            if change == 'nan': d.threads[0].times[0] = float('nan')
            if change == 'size': d.threads[0].children[0].times.pop()
            with self.assertRaises(ProfileError): Profile(d.SerializeToString())

    def test_inconsistent_report_gets_actionable_message(self):
        d = sample()
        d.threads[0].children[0].times[:] = [500, 500]  # child larger than its parent
        with self.assertRaisesRegex(ProfileError, '报告内部数据不一致.*重新采样'):
            Profile(d.SerializeToString())

    def test_unknown_zero(self):
        p = Profile(sample().SerializeToString())
        self.assertIsNone(p.overview()['health']['tps_1m'])
        with self.assertRaises(ProfileError): p.query(window=99)
        with self.assertRaises(ProfileError): p.query(thread=-1)

    def test_fixed_url(self):
        self.assertEqual(report_id('https://spark.lucko.me/SyntheticReport001'), 'SyntheticReport001')
        for url in ('http://spark.lucko.me/SyntheticReport001', 'https://localhost/foobar', 'https://spark.lucko.me/foobar?url=http://localhost', 'https://spark.lucko.me@localhost/foobar'):
            with self.assertRaises(ProfileError): report_id(url)

    def test_link_boundaries(self):
        # Sentence punctuation and one trailing slash end a link; path or file suffixes reject it.
        cases = {
            'AbCdEf1234': ['AbCdEf1234'], 'AbCdEf1234。': ['AbCdEf1234'], 'AbCdEf1234.': ['AbCdEf1234'],
            'AbCdEf1234/': ['AbCdEf1234'], 'AbCdEf1234?x=1': ['AbCdEf1234'], 'AbCdEf1234,卡顿': ['AbCdEf1234'],
            'AbCdEf1234/extra': [], 'AbCdEf1234.json': [], 'AbCdEf1234-': [], 'AbCdEf1234_x': [], 'abc': []}
        for tail, expected in cases.items():
            with self.subTest(tail=tail):
                self.assertEqual(LINK.findall('看下 https://spark.lucko.me/'+tail), expected)

    def test_history_retention_while_disabled(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'history.sqlite3'
            h = History(path)
            self.assertEqual(h.purge(), 0)
            self.assertEqual(h.delete('a'), 0)
            self.assertFalse(path.exists())
            h.enabled = True
            overview = Profile(sample().SerializeToString()).overview()
            for owner in ('a', 'a', 'b'):
                h.save(owner, 's', 'p', overview, 'ok')
            with sqlite3.connect(path) as db:
                db.execute("UPDATE reviews SET created=0 WHERE rowid=1")
            db.close()
            h.enabled = False
            self.assertEqual(h.purge(), 1)
            self.assertEqual(h.delete('a'), 1)
            with sqlite3.connect(path) as db:
                owners = [r[0] for r in db.execute('SELECT owner FROM reviews')]
            db.close()
            self.assertEqual(owners, ['b'])

    def test_history_off_and_isolation(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'history.sqlite3'
            h = History(path)
            overview = Profile(sample().SerializeToString()).overview()
            self.assertIsNone(h.save('a','s','p',overview,'ok'))
            self.assertEqual(h.list('a','s'), [])
            self.assertFalse(path.exists())
            h.enabled = True
            h.save('a','s','p',overview,'ok')
            self.assertEqual(len(h.list('a','s','p')), 1)
            self.assertEqual(h.list('b','s','p'), [])
            self.assertEqual(h.list('a','other','p'), [])
            self.assertEqual(h.delete('b'), 0)
            self.assertEqual(h.delete('a'), 1)

    @unittest.skipUnless(hasattr(sqlite3.Connection, 'setconfig'), 'needs sqlite3.Connection.setconfig (Python 3.12+)')
    def test_history_sql_has_no_double_quoted_literal(self):
        # SQLite built with SQLITE_DQS=0 rejects "" as a string literal.
        real_connect = sqlite3.connect
        def strict_connect(*args, **kwargs):
            db = real_connect(*args, **kwargs)
            db.setconfig(sqlite3.SQLITE_DBCONFIG_DQS_DML, False)
            return db
        with tempfile.TemporaryDirectory() as root:
            h = History(Path(root)/'history.sqlite3', True)
            h.save('a', 's', 'p', Profile(sample().SerializeToString()).overview(), 'ok')
            with patch('spark_core.history.sqlite3.connect', strict_connect):
                self.assertEqual(len(h.list('a', 's', 'p')), 1)
                self.assertEqual(len(h.list('a', 's')), 1)

    def test_compare_gate(self):
        before = Profile(sample().SerializeToString()).overview()
        after = Profile(sample().SerializeToString()).overview()
        after['sampling']['interval_us'] = 1000
        self.assertFalse(compare(before, after)['comparable_sampling'])

    def test_compare_tolerates_older_overview_shape(self):
        current = Profile(sample().SerializeToString()).overview()
        result = compare({'platform': {'name': 'NeoForge'}}, current)
        self.assertFalse(result['comparable_sampling'])
        self.assertIn('sampling.mode', result['different_conditions'])
        self.assertTrue(all(v is None for v in result['health_delta'].values()))

    def test_evidence_pack_coverage_and_paths(self):
        p = Profile(sample().SerializeToString())
        thread = p.evidence_pack()['threads'][0]
        self.assertEqual(thread['selected_self_ms'], 300)
        self.assertEqual(thread['selected_self_coverage_pct'], 100)
        self.assertEqual([r['node'] for r in thread['hotspots'][1]['path']], [1, 0])
        self.assertEqual(thread['remaining_sampled_ms'], 0)

    def test_evidence_pack_keeps_main_thread(self):
        d = sample()
        for i in range(6):
            d.threads.add(name=f'worker-{i}', times=[1000, 2000])
        pack = Profile(d.SerializeToString()).evidence_pack()
        self.assertEqual(pack['threads'][0]['name'], 'Server thread')
        self.assertEqual(len(pack['threads']), 4)
        self.assertEqual(pack['omitted_threads'], 3)

    def test_world_statistics_are_bounded_summaries(self):
        d = sample()
        w = d.metadata.platform_statistics.world
        w.total_entities = 40
        for i in range(15):
            w.entity_counts[f'mod:type{i:02d}'] = i + 1
        world = w.worlds.add(name='overworld', total_entities=30)
        region = world.regions.add(total_entities=30)
        region.chunks.add(x=2, z=-3, total_entities=20, entity_counts={'minecraft:item': 18, 'minecraft:bat': 2})
        region.chunks.add(x=0, z=0, total_entities=10, entity_counts={'minecraft:cow': 10})
        w.worlds.add(name='the_nether', total_entities=10)
        w.game_rules.add(name='randomTickSpeed', default_value='3').world_values['overworld'] = '30'
        w.game_rules.add(name='doMobSpawning', default_value='true').world_values['overworld'] = 'true'
        w.game_rules.add(name='keepInventory', default_value='false').world_values['overworld'] = 'true'
        stats = Profile(d.SerializeToString()).overview()['world']
        self.assertEqual(stats['total_entities'], 40)
        self.assertEqual(stats['entity_type_count'], 15)
        self.assertEqual(len(stats['entity_types']), 10)
        self.assertEqual(stats['entity_types'][0], ['mod:type14', 15])
        self.assertEqual(stats['dimensions'], [{'name': 'overworld', 'entities': 30}, {'name': 'the_nether', 'entities': 10}])
        # Chunk coordinates plus the block position of the chunk centre, for checking in game.
        self.assertEqual(stats['busiest_chunks'][0], {'dimension': 'overworld', 'chunk_x': 2, 'chunk_z': -3,
                                                      'block_x': 40, 'block_z': -40, 'entities': 20,
                                                      'entity_types': [['minecraft:item', 18], ['minecraft:bat', 2]]})
        # Only performance-relevant rules that differ from their default.
        self.assertEqual(stats['changed_game_rules'], {'randomTickSpeed': {'default': '3', 'worlds': {'overworld': '30'}}})
        self.assertIsNone(Profile(sample().SerializeToString()).overview()['world'])

    def test_window_health_reports_entity_counts(self):
        d = sample()
        ws = d.time_window_statistics[10]
        ws.players, ws.entities, ws.tile_entities, ws.chunks = 3, 316, -1, 7193
        row = Profile(d.SerializeToString()).overview()['window_health'][0]
        # Spark writes -1 when it could not count; that is unknown, not a count.
        self.assertEqual((row['players'], row['entities'], row['tile_entities'], row['chunks']), (3, 316, None, 7193))

    def test_wait_time_is_split_into_idle_and_other(self):
        d = sample()
        del d.threads[:]
        t = d.threads.add(name='Server thread', times=[100, 100], children_refs=[0, 3])
        t.children.add(class_name='net.minecraft.server.MinecraftServer', method_name='runServer', times=[60, 60], children_refs=[1])
        t.children.add(class_name='net.minecraft.server.MinecraftServer', method_name='waitUntilNextTick', times=[60, 60], children_refs=[2])
        t.children.add(class_name='jdk.internal.misc.Unsafe', method_name='park', times=[60, 60])
        t.children.add(class_name='net.minecraft.server.MinecraftServer', method_name='tickServer', times=[40, 40], children_refs=[4, 5])
        t.children.add(class_name='jdk.internal.misc.Unsafe', method_name='park', times=[10, 10])
        t.children.add(class_name='net.minecraft.world.level.Level', method_name='tickBlockEntities', times=[30, 30])
        thread = Profile(d.SerializeToString()).evidence_pack()['threads'][0]
        # Parking between ticks is spare capacity; parking inside a tick is a stall.
        self.assertEqual((thread['idle_between_ticks_ms'], thread['other_wait_ms']), (120, 20))

    def test_runtime_jvm_and_heuristic_identity(self):
        d = sample()
        system = d.metadata.system_statistics
        system.os.name = 'Windows Server 2022 Datacenter'
        system.cpu.model_name = 'AMD Ryzen 9 9950X'
        system.cpu.threads = 6
        system.java.version = '17.0.20.1'
        system.java.vm_args = '-Xms4G -Xmx8G -XX:+UseG1GC'
        d.metadata.platform_statistics.memory.heap.max = 8589934592
        d.metadata.system_statistics.gc['G1 Young Generation'].total = 7
        before = Profile(d.SerializeToString()).overview()['runtime']
        self.assertIn('-Xmx8G', before['system']['java']['vm_args'])
        self.assertEqual(before['heap']['heap']['max'], '8589934592')
        self.assertIn('G1 Young Generation', before['system_gc'])
        system.java.version = '21'
        after = Profile(d.SerializeToString()).overview()['runtime']
        self.assertEqual(before['server_hint']['tag'], after['server_hint']['tag'])
        self.assertEqual(after['server_hint']['confidence'], 'heuristic_not_unique')

if __name__ == '__main__': unittest.main()
