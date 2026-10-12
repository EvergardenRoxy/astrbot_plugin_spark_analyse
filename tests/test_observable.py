"""Observable report parsing: shape checks, arithmetic, bounds. Synthetic data only."""
import asyncio
import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from spark_core.observable import ObservableProfile, normalize_type, parse_system_report
from spark_core.cache import ProfileCache
from spark_core.profile import ProfileError
from spark_core.session import ReportSession
from spark_core.transport import OBS_LINK, download_observable, observable_id

NOW = 1_760_000_000_000


def trace(count=10, frames=(('a.B', 'c', 4), ('a.D', 'e', 6)), root=('net.Level', 'tick')):
    """A trace map in the uploaded shape: root with `frames` as direct children (class, method, count)."""
    return {'className': root[0], 'methodName': root[1], 'count': count,
            'children': [{'className': c, 'methodName': m, 'count': n, 'children': []} for c, m, n in frames]}


def entry(kind='minecraft:zombie', x=8, y=64, z=8, rate=1_000_000.0, ticks=100, **extra):
    return {'position': {'x': x, 'y': y, 'z': z}, 'type': kind, 'rate': rate, 'ticks': ticks, 'traces': trace(), **extra}


def report(wrapped=True, **changes):
    data = {'entities': {'minecraft:overworld': [entry(), entry('minecraft:creeper', x=40, rate=2_000_000.0, ticks=50),
                                                 entry('create:contraption', x=-10, z=-10, rate=4_000_000.0, ticks=100)]},
            'blocks': {'minecraft:overworld': [entry('block.minecraft.hopper', x=100, z=100, rate=500_000.0, ticks=200)],
                       'minecraft:the_nether': [entry('minecraft:furnace', rate=100_000.0, ticks=100)]},
            'traces': trace(40, (('net.Server', 'tickEntities', 30), ('net.Server', 'tickBlocks', 10))), 'ticks': 200}
    data.update(changes.pop('data', {}))
    diagnostics = {'user': '00000000-0000-0000-0000-000000000000', 'start': NOW, 'duration': 10_000,
                   'minecraftVersion': '1.21.1', 'modLoader': 'neoforge', 'observableVersion': '1.2.3',
                   'additionalDiagnostics': {'System Report': '\tMinecraft Version: 1.21.1\n\tJVM Flags: -Xmx4G -XX:+UseG1GC\n\tSecret Token: x\n\tCPUs: 8',
                                             'Mods': "'Create' (version: 6.0)\n'Sodium' (version: 0.6)"}}
    diagnostics.update(changes.pop('diagnostics', {}))
    return json.dumps({'data': data, 'diagnostics': diagnostics} if wrapped else data).encode()


class ObservableTests(unittest.TestCase):
    def overview(self, **changes):
        return ObservableProfile(report(**changes)).overview()

    def test_cost_arithmetic(self):
        view = self.overview()
        # rate is ns per own tick: zombie 1ms*100/200 = 0.5; creeper 2ms*50/200 = 0.5; contraption 4ms*100/200 = 2;
        # hopper 0.5ms*200/200 = 0.5; furnace 0.1ms*100/200 = 0.05 (ms per server tick).
        self.assertEqual(view['totals']['entity_ms_per_tick'], 3.0)
        self.assertEqual(view['totals']['block_ms_per_tick'], 0.55)
        self.assertEqual(view['totals']['observed_ms_per_tick'], 3.55)
        self.assertAlmostEqual(view['totals']['tick_budget_pct'], 7.1)
        self.assertEqual(view['entity_types'][0]['type'], 'create:contraption')
        self.assertEqual(view['entity_types'][0]['share_pct'], 56.3)
        self.assertEqual(view['entity_types'][0]['worst_single_us_per_own_tick'], 4000.0)
        self.assertEqual(view['dimensions'][0]['dimension'], 'minecraft:overworld')
        self.assertEqual(view['dimensions'][0]['ms_per_tick'], 3.5)
        self.assertEqual(view['hot_entries'][0]['type'], 'create:contraption')

    def test_block_translation_keys_become_resource_ids(self):
        self.assertEqual(normalize_type('block.minecraft.hopper'), 'minecraft:hopper')
        self.assertEqual(normalize_type('block.create.mechanical_press'), 'create:mechanical_press')
        self.assertEqual(normalize_type('minecraft:furnace'), 'minecraft:furnace')
        self.assertEqual({t['type'] for t in self.overview()['block_types']}, {'minecraft:hopper', 'minecraft:furnace'})

    def test_chunks_use_floor_division_for_negative_coordinates(self):
        chunks = {(c['chunk_x'], c['chunk_z']): c for c in self.overview()['hot_chunks']}
        self.assertIn((-1, -1), chunks)
        self.assertEqual(chunks[(-1, -1)]['block_x'], -8)
        self.assertEqual(chunks[(-1, -1)]['top_types'][0][0], 'create:contraption')

    def test_tps_is_estimated_from_ticks_and_duration(self):
        self.assertEqual(self.overview()['runtime']['tps_estimate'], 20.0)  # 200 ticks / 10 s
        self.assertEqual(self.overview(diagnostics={'duration': 20_000})['runtime']['tps_estimate'], 10.0)
        self.assertEqual(self.overview(diagnostics={'duration': 5_000})['runtime']['tps_estimate'], 20.0)  # capped
        self.assertIsNone(self.overview(diagnostics={'duration': None})['runtime']['tps_estimate'])

    def test_bare_data_from_exported_file_is_accepted(self):
        view = ObservableProfile(report(wrapped=False)).overview()
        self.assertEqual(view['totals']['entity_entries'], 3)
        self.assertIsNone(view['runtime']['minecraft'])
        self.assertIsNone(view['runtime']['tps_estimate'])

    def test_player_uuid_is_not_forwarded(self):
        self.assertNotIn('00000000-0000', json.dumps(self.overview()))

    def test_system_report_is_filtered(self):
        view = self.overview()
        self.assertEqual(view['runtime']['system'], {'Minecraft Version': '1.21.1', 'CPUs': '8'})
        self.assertEqual(view['runtime']['jvm_flags'], '-Xmx4G -XX:+UseG1GC')
        self.assertNotIn('Secret', json.dumps(view))
        self.assertEqual(parse_system_report(None), ({}, None))
        self.assertEqual(view['runtime']['mod_count'], 2)

    def test_trace_frames_are_aggregated_by_self_samples(self):
        view = self.overview()
        server = view['trace']['server_top_self_frames']
        self.assertEqual(server[0]['frame'], 'net.Server.tickEntities')
        self.assertEqual(server[0]['samples'], 30)
        zombie = view['trace']['by_type']['create:contraption']
        self.assertEqual(zombie['top_self_frames'][0]['frame'], 'a.D.e')
        self.assertEqual(zombie['top_self_frames'][0]['pct'], 60.0)

    def test_report_without_traces_still_works(self):
        view = self.overview(data={'traces': None})
        self.assertEqual(view['trace']['server_samples'], 0)

    def test_malformed_reports_are_rejected_with_plugin_text(self):
        bad = [b'not json', b'[]', b'{}', b'{"data": {"entities": [], "blocks": {}, "ticks": 1}}',
               report(data={'ticks': 0}), report(data={'ticks': -1}), report(data={'ticks': 'x'}),
               report(data={'ticks': True})]
        for raw in bad:
            with self.subTest(raw=raw[:40]):
                with self.assertRaises(ProfileError):
                    ObservableProfile(raw).overview()

    def test_bad_numbers_and_positions_are_rejected(self):
        for change in ({'rate': -1.0}, {'rate': 'fast'}, {'ticks': -3}, {'position': {'x': 1.5, 'y': 0, 'z': 0}},
                       {'position': None}, {'type': 7}):
            data = json.loads(report())
            data['data']['entities']['minecraft:overworld'][0].update(change)
            with self.subTest(change=change):
                with self.assertRaises(ProfileError):
                    ObservableProfile(json.dumps(data).encode()).overview()

    def test_empty_report_is_rejected(self):
        with self.assertRaisesRegex(ProfileError, '没有任何实体或方块'):
            ObservableProfile(report(data={'entities': {}, 'blocks': {}}))

    def test_deep_and_large_traces_are_bounded(self):
        node = {'className': 'a.B', 'methodName': 'c', 'count': 1, 'children': []}
        for _ in range(700):
            node = {'className': 'a.B', 'methodName': 'c', 'count': 1, 'children': [node]}
        with self.assertRaisesRegex(ProfileError, '深度'):
            ObservableProfile(report(data={'traces': node})).overview()
        wide = trace(2000, [('a.B', 'c', 1)]*1001)
        with patch('spark_core.observable.MAX_NODES', 1000):
            with self.assertRaisesRegex(ProfileError, '100 万'):
                ObservableProfile(report(data={'traces': wide})).overview()
        self.assertEqual(ObservableProfile(report(data={'traces': wide})).overview()['trace']['server_samples'], 2000)

    def test_output_is_capped(self):
        many = {'minecraft:overworld': [entry(f'mod:thing{i}', x=i*32, z=i*32, rate=1000.0+i) for i in range(500)]}
        view = ObservableProfile(report(data={'entities': many})).overview()
        self.assertEqual(len(view['entity_types']), 12)
        self.assertEqual(view['entity_type_count'], 500)
        self.assertEqual(len(view['hot_entries']), 10)
        self.assertEqual(len(view['hot_chunks']), 8)
        self.assertLess(len(json.dumps(view)), 60_000)

    def test_hostile_nesting_does_not_crash_the_parser(self):
        with self.assertRaises(ProfileError):
            ObservableProfile(b'{"data":'*5000+b'1'+b'}'*5000)

    def test_query_is_not_offered(self):
        with self.assertRaises(ProfileError):
            ObservableProfile(report()).query(view='hotspots')


class LinkTests(unittest.TestCase):
    def found(self, text):
        return OBS_LINK.findall(text)

    def test_link_forms(self):
        base = 'https://observable.tas.sh/p/'
        for text, expected in ((base+'Ab12_-xyz', ['Ab12_-xyz']), (base+'Ab12cd/chunks', ['Ab12cd']), (base+'Ab12cd/info/', ['Ab12cd']),
                               (f'看这个 {base}Ab12cd。', ['Ab12cd']), (base+'Ab12cd.', ['Ab12cd']), (f'({base}Ab12cd)', ['Ab12cd']),
                               (f'{base}Ab12cd {base}Zz99yy', ['Ab12cd', 'Zz99yy'])):
            with self.subTest(text=text):
                self.assertEqual(self.found(text), expected)

    def test_not_a_report_link(self):
        base = 'https://observable.tas.sh/'
        for text in (base, base+'p/', base+'p/abc', base+'p/Ab12cd/other', base+'p/Ab12cd/chunks/x', base+'p/Ab12cd.json',
                     base+'v1/get/Ab12cd', 'http://observable.tas.sh/p/Ab12cd', 'https://observable.tas.sh.evil.com/p/Ab12cd',
                     'https://evil.com/observable.tas.sh/p/Ab12cd', 'https://www.observable.tas.sh/p/Ab12cd', base+'p/'+'a'*65):
            with self.subTest(text=text):
                self.assertEqual(self.found(text), [])

    def test_spark_links_are_not_observable_links(self):
        from spark_core.transport import LINK
        self.assertEqual(LINK.findall('https://observable.tas.sh/p/Ab12cdef'), [])
        self.assertEqual(OBS_LINK.findall('https://spark.lucko.me/Ab12cdef'), [])

    def test_observable_id_only_takes_the_official_host(self):
        self.assertEqual(observable_id('https://observable.tas.sh/p/Ab12cd'), 'Ab12cd')
        for url in ('https://observable.tas.sh/p/Ab12cd?x=1', 'https://localhost/p/Ab12cd', 'https://observable.tas.sh@localhost/p/Ab12cd'):
            with self.assertRaises(ProfileError):
                observable_id(url)


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def fetch(self, payload, headers=None, status=200, **limits):
        seen = {}
        class Content:
            async def iter_chunked(self, size):
                for offset in range(0, len(payload), 64):
                    yield payload[offset:offset+64]
        class Response:
            content = Content()
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
        Response.status = status
        Response.headers = headers or {'Content-Type': 'application/json'}
        class Session:
            def __init__(self, **kwargs): seen['session'] = kwargs
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            def get(self, url, **kwargs):
                seen['url'], seen['get'] = url, kwargs
                return Response()
        with tempfile.TemporaryDirectory() as directory:
            with patch('spark_core.transport.aiohttp.ClientSession', Session):
                path = Path(directory)/'raw'
                await download_observable('https://observable.tas.sh/p/Ab12cd', path, **limits)
                return path.read_bytes(), seen

    async def test_fixed_data_endpoint_and_safe_session(self):
        raw, seen = await self.fetch(report())
        self.assertEqual(raw, report())
        self.assertEqual(seen['url'], 'https://observable.tas.sh/v1/get/Ab12cd')
        self.assertIs(seen['session']['trust_env'], False)
        self.assertIs(seen['session']['auto_decompress'], False)
        self.assertIs(seen['get']['allow_redirects'], False)
        self.assertEqual(seen['get']['headers'], {'Accept-Encoding': 'gzip'})

    async def test_gzip_and_content_type_parameters(self):
        raw = report()
        got, _ = await self.fetch(gzip.compress(raw), {'Content-Type': 'application/json; charset=utf-8', 'Content-Encoding': 'gzip'})
        self.assertEqual(got, raw)

    async def test_rejections(self):
        cases = [(b'x', {'Content-Type': 'text/html'}, 200, '不是Observable'), (b'x', None, 404, 'HTTP 404.*不存在或已过期'), (b'x', None, 500, 'HTTP 500.*不存在或已过期'), (b'x', None, 403, 'HTTP 403$'),
                 (b'x', {'Content-Type': 'application/json', 'Content-Encoding': 'br'}, 200, '压缩格式'), (b'', None, 200, '为空')]
        for payload, headers, status, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ProfileError, message):
                    await self.fetch(payload, headers, status)

    async def test_plain_text_json_is_accepted(self):
        got, _ = await self.fetch(report(), {'Content-Type': 'text/plain;charset=UTF-8'})
        self.assertEqual(got, report())

    async def test_size_limits_are_enforced(self):
        with self.assertRaisesRegex(ProfileError, '16 MiB'):
            await self.fetch(b'a'*500, compressed_limit=100)
        bomb = gzip.compress(b'0'*100_000)
        with self.assertRaisesRegex(ProfileError, '48 MiB'):
            await self.fetch(bomb, {'Content-Type': 'application/json', 'Content-Encoding': 'gzip'}, decoded_limit=1000)

    async def test_download_proxy_must_be_valid(self):
        with self.assertRaises(ProfileError):
            await download_observable('https://observable.tas.sh/p/Ab12cd', Path('unused'), proxy='socks5://127.0.0.1:1')


class SessionTests(unittest.IsolatedAsyncioTestCase):
    """Real worker subprocess, with only the network replaced."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def downloader(self, payload, calls):
        async def fake(url, path, **kwargs):
            calls.append(url)
            path.write_bytes(payload)
        return fake

    async def test_load_observable_through_the_worker_and_cache(self):
        calls = []
        cache = ProfileCache(self.root/'cache.sqlite3', 72)
        with patch('spark_core.session.download_observable', self.downloader(report(), calls)):
            session = ReportSession(self.root, cache=cache)
            try:
                view = await session.load_observable('https://observable.tas.sh/p/Ab12cd')
            finally:
                await session.close()
            self.assertEqual(view['kind'], 'observable')
            self.assertNotIn('evidence_pack', view)
            self.assertEqual(view['totals']['observed_ms_per_tick'], 3.55)
            second = ReportSession(self.root, cache=cache)
            try:
                await second.load_observable('https://observable.tas.sh/p/Ab12cd')
            finally:
                await second.close()
        self.assertEqual(calls, ['https://observable.tas.sh/p/Ab12cd'])  # the second load came from the cache
        self.assertEqual([p for p in self.root.glob('spark-*')], [])

    async def test_bad_report_is_rejected_with_plugin_text_and_not_cached(self):
        cache = ProfileCache(self.root/'cache.sqlite3', 72)
        calls = []
        with patch('spark_core.session.download_observable', self.downloader(b'{"hello": 1}', calls)):
            for _ in range(2):
                session = ReportSession(self.root, cache=cache)
                try:
                    with self.assertRaisesRegex(ProfileError, '不是可分析的 Observable 报告'):
                        await session.load_observable('https://observable.tas.sh/p/Ab12cd')
                finally:
                    await session.close()
        self.assertEqual(len(calls), 2)

    async def test_oversized_observable_file_is_refused_before_reading(self):
        async def sparse(url, path, **kwargs):
            with path.open('wb') as f:
                f.truncate(49*1024*1024)
        with patch('spark_core.session.download_observable', sparse):
            session = ReportSession(self.root)
            try:
                with self.assertRaisesRegex(ProfileError, '48 MiB'):
                    await session.load_observable('https://observable.tas.sh/p/Ab12cd')
            finally:
                await session.close()

    async def test_query_on_an_observable_session_is_refused_by_the_worker(self):
        with patch('spark_core.session.download_observable', self.downloader(report(), [])):
            session = ReportSession(self.root)
            try:
                await session.load_observable('https://observable.tas.sh/p/Ab12cd')
                self.assertEqual((await session.query(view='hotspots', thread=0))['error'], 'Observable 报告不提供调用树查询')
            finally:
                await session.close()


if __name__ == '__main__':
    unittest.main()
