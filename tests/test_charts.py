import asyncio
import json
import os
import sqlite3
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from spark_core.charts import ChartManager, DeliveryLedger
from spark_core.profile import Profile, ProfileError
from spark_core.session import ReportSession
from spark_core.telemetry import MAX_POINTS, bounded_points, export, source_ranking
from test_core import sample
import test_plugin as plugin_test


def boundary_sample():
    d = sample()
    d.metadata.interval = 4000
    t = d.threads[0]
    t.times[:] = [144, 156]
    t.children[1].times[:] = [144, 156]
    t.children[0].times[:] = [148, 140]
    return d


class ChartDataTests(unittest.TestCase):
    def test_java_boundary_is_bounded_and_bad_window_cannot_be_queried(self):
        p = Profile(boundary_sample().SerializeToString())
        self.assertEqual(p.inconsistent_windows, {10})
        self.assertEqual(p.window_excess_ms, 4)
        self.assertTrue(p.query(view='methods')['rows'])
        with self.assertRaisesRegex(ProfileError, '该窗口'):
            p.query(window=10)
        self.assertEqual(p.query(view='windows', window=10)['rows'][0]['window'], 10)

    def test_quantization_is_not_unbounded_tolerance(self):
        for mode in ('async', 'large', 'aggregate', 'huge_interval', 'two_errors'):
            d = boundary_sample()
            if mode == 'async': d.metadata.sampler_engine = 1
            if mode == 'large': d.threads[0].children[0].times[0] = 149
            if mode == 'aggregate': d.threads[0].children[0].times[:] = [148, 157]
            if mode == 'huge_interval':
                d.metadata.interval = 1000000
                d.threads[0].children[0].times[0] = 160
            if mode == 'two_errors':
                t = d.threads.add(name='other', times=[144,156], children_refs=[0])
                t.children.add(class_name='x', method_name='x', times=[148,140])
            with self.subTest(mode=mode), self.assertRaises(ProfileError):
                Profile(d.SerializeToString())

    def test_no_metrics_uses_only_anchored_windows_and_real_snapshots(self):
        d = sample()
        d.metadata.platform_statistics.memory.heap.used = 123
        b = export(Profile(d.SerializeToString()))
        self.assertIsNone(b['range'])
        self.assertEqual(b['heap']['used'], 123)
        self.assertNotIn('heap_used', b['series'])
        w = d.time_window_statistics[10]
        w.start_time, w.end_time, w.tps = 100000, 160000, 20
        b = export(Profile(d.SerializeToString()))
        self.assertEqual(b['series']['tps']['points'], [[130000,20]])
        self.assertEqual(b['series']['tps']['origin'], 'window')

    def test_metrics_delta_nulls_gaps_and_numeric_export_privacy(self):
        d = sample()
        d.metadata.creator.name = 'private-user'
        d.metadata.server_configurations['secret'] = 'private-password'
        d.metadata.system_statistics.java.vm_args = '-Dsecret=private-key'
        q = d.metadata.metrics.tick_duration
        q.start_timestamp_ms = 100000
        q.timestamp_deltas_ms.extend([1000,1000,1000,20000])
        for value in (10, 20, float('nan'), 40): q.values.add(median=value)
        q2 = d.metadata.metrics.memory_usage_heap
        q2.start_timestamp_ms = 100000; q2.timestamp_deltas_ms.append(1000)
        q2.values.add(used=100, committed=200, max=-1)
        b = export(Profile(d.SerializeToString()))
        points = b['series']['tick_median']['points']
        self.assertEqual(points[:3], [[101000,10],[102000,20],[103000,None]])
        self.assertIn([103001,None], points)
        self.assertNotIn('heap_max', b['series'])
        encoded = json.dumps(b, allow_nan=False)
        for secret in ('private-user', 'private-password', 'private-key'):
            self.assertNotIn(secret, encoded)

    def test_malformed_metric_arrays_are_not_plotted(self):
        d = sample(); q = d.metadata.metrics.tps
        q.start_timestamp_ms = 100000; q.timestamp_deltas_ms.extend([1000,0]); q.values.extend([20,19])
        self.assertNotIn('tps', export(Profile(d.SerializeToString()))['series'])
        q.timestamp_deltas_ms[:] = [1000]
        self.assertNotIn('tps', export(Profile(d.SerializeToString()))['series'])

    def test_window_fallback_does_not_bridge_a_recording_gap(self):
        d=sample()
        for window,start in ((10,100000),(11,300000)):
            q=d.time_window_statistics[window];q.start_time=start;q.end_time=start+60000;q.tps=20
        points=export(Profile(d.SerializeToString()))['series']['tps']['points']
        self.assertEqual(points,[[130000,20],[299999,None],[330000,20]])

    def test_raw_point_budget_is_enforced_before_expansion(self):
        d=sample();q=d.metadata.metrics.tps
        q.start_timestamp_ms=100000;q.timestamp_deltas_ms.extend([1]*11);q.values.extend([20]*11)
        with patch('spark_core.telemetry.MAX_INPUT_POINTS',10),self.assertRaisesRegex(ProfileError,'点数'):
            export(Profile(d.SerializeToString()))

    def test_export_size_limit_and_no_embedded_chart_in_overview(self):
        p=Profile(sample().SerializeToString())
        with patch('spark_core.telemetry.MAX_BUNDLE_BYTES',10),self.assertRaisesRegex(ProfileError,'2 MiB'):
            export(p)
        self.assertNotIn('series',p.overview())
        self.assertNotIn('telemetry',p.overview())

    def test_font_and_chart_settings_are_packaged_defaults(self):
        root=Path(__file__).resolve().parents[1]
        schema=json.loads((root/'_conf_schema.json').read_text())
        self.assertIs(schema['chart_enabled']['default'],False)
        self.assertEqual(schema['chart_timezone']['default'],'UTC')
        self.assertTrue((root/'spark_core/chart_assets/SparkChartSans.otf').is_file())
        license=(root/'spark_core/chart_assets/OFL.txt').read_text()
        self.assertIn('OPEN FONT LICENSE',license)

    def test_decimation_keeps_extreme_and_gap_under_limit(self):
        points = [[i, 1.] for i in range(10000)]
        points[5371][1] = 99999; points[8888][1] = None
        result = bounded_points(points)
        self.assertLessEqual(len(result), MAX_POINTS)
        self.assertIn([5371,99999], result); self.assertIn([8888,None], result)
        self.assertEqual((result[0],result[-1]), (points[0],points[-1]))

    def test_decimation_does_not_join_two_gapped_extrema(self):
        points=[[i,float(i%17)] for i in range(10000)]
        for i in range(100,10000,4):points[i][1]=None
        result=bounded_points(points)
        self.assertLessEqual(len(result),MAX_POINTS)
        # Interior samples of a gapped bucket are isolated markers, with null on both sides.
        for i, point in enumerate(result[1:-1],1):
            if point[1] is not None and 150 < point[0] < 9800:
                self.assertIsNone(result[i-1][1]);self.assertIsNone(result[i+1][1])

    def test_sources_outermost_entry_and_cross_source_overlap(self):
        d = sample();t = d.threads[0]
        del t.children[:];t.times[:] = [100,100];t.children_refs[:] = [0,3]
        for name, times, refs in [('A',[80,80],[1]),('B',[60,60],[2]),('A',[30,30],[]),('C',[20,20],[])]:
            t.children.add(class_name=name, method_name='tick', times=times, children_refs=refs)
        d.class_sources.update({'A':'a','B':'b','C':'c'})
        result = source_ranking(Profile(d.SerializeToString()))
        self.assertEqual({r['source']:r['pct'] for r in result['rows']}, {'a':80,'b':60,'c':20})
        self.assertEqual(sum(r['pct'] for r in result['rows']),160)

    def test_sources_specificity_and_viewer_exclusions(self):
        d = sample();n = d.threads[0].children[0]
        n.method_desc, n.line_number = '()V', 2
        d.class_sources[n.class_name] = 'class'
        d.line_sources[n.class_name+';2'] = 'line'
        d.method_sources[n.class_name+';work;()V'] = 'method'
        for expected in ('method','line','class'):
            rows = source_ranking(Profile(d.SerializeToString()))['rows']
            self.assertEqual(rows[0]['source'],expected)
            if d.method_sources:d.method_sources.clear()
            else:d.line_sources.clear()
        for name in ('minecraft','java'):
            d.class_sources[n.class_name] = name
            self.assertEqual(source_ranking(Profile(d.SerializeToString()))['rows'],[])
        n.class_name = 'com.destroystokyo.paper.event.executor.asm.generated.Test'
        d.class_sources[n.class_name] = 'plugin'
        self.assertEqual(source_ranking(Profile(d.SerializeToString()))['rows'],[])

    def test_unknown_grouped_and_folia_do_not_fake_main_thread(self):
        d = sample();d.class_sources['test.Mod'] = 'mod'
        d.metadata.data_aggregator.thread_grouper = 1
        self.assertEqual(source_ranking(Profile(d.SerializeToString()))['rows'],[])
        d.metadata.thread_dumper.type = 1;d.metadata.thread_dumper.ids.append(1)
        self.assertTrue(source_ranking(Profile(d.SerializeToString()))['rows'])
        d.metadata.platform_metadata.brand = 'Folia'
        self.assertEqual(source_ranking(Profile(d.SerializeToString()))['rows'],[])

    def test_carpet_loop_wrapper_is_not_all_charged_to_carpet(self):
        d=sample();loop=d.threads[0].children[1]
        loop.class_name='net.minecraft.server.MinecraftServer';loop.method_name='xmodifiedRunLoop'
        d.class_sources[loop.class_name]='carpet';d.class_sources['test.Mod']='carpet'
        row=source_ranking(Profile(d.SerializeToString()))['rows'][0]
        self.assertEqual(row['sampled_ms'],70)
        loop.method_name='normalTick'
        self.assertEqual(source_ranking(Profile(d.SerializeToString()))['rows'][0]['sampled_ms'],300)

    def test_plugin_title_and_top7_limit(self):
        d=sample();d.metadata.platform_metadata.name='Paper';d.metadata.platform_metadata.brand='Leaf'
        t=d.threads[0];del t.children[:];t.children_refs[:]=range(10);t.times[:]=[100,100]
        for i in range(10):
            t.children.add(class_name=str(i),method_name='tick',times=[i+1,i+1]);d.class_sources[str(i)]=f'Plugin{i}'
        r=source_ranking(Profile(d.SerializeToString()))
        self.assertEqual(r['title'],'插件来源 Top 7');self.assertEqual(len(r['rows']),7)
        self.assertEqual(r['rows'][0]['source'],'Plugin9')


class LedgerTests(unittest.TestCase):
    def test_claim_failure_release_success_and_restart(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'ledger.db';l=DeliveryLedger(path)
            token=l.claim('chat','fingerprint');self.assertTrue(token)
            self.assertIsNone(l.claim('chat','fingerprint'))
            l.release('chat','fingerprint','wrong-token');self.assertIsNone(l.claim('chat','fingerprint'))
            l.release('chat','fingerprint',token);token=l.claim('chat','fingerprint')
            l.complete('chat','fingerprint',token)
            reloaded=DeliveryLedger(path);reloaded.recover()
            self.assertIsNone(reloaded.claim('chat','fingerprint'))
            self.assertTrue(reloaded.claim('another-chat','fingerprint'))
            self.assertTrue(reloaded.claim('chat','other-content'))

    def test_recover_only_discards_unfinished_claims(self):
        with tempfile.TemporaryDirectory() as root:
            l=DeliveryLedger(Path(root)/'ledger.db');l.claim('chat','pending');l.recover()
            self.assertTrue(l.claim('chat','pending'))

    def test_sent_rows_expire_after_retention_and_unsent_rows_do_not_count(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'ledger.db';l=DeliveryLedger(path,retention_days=30)
            l.complete('chat','old',l.claim('chat','old'))
            with patch('spark_core.charts.time.time',return_value=time.time()+29*86400):
                self.assertIsNone(l.claim('chat','old'))
            with patch('spark_core.charts.time.time',return_value=time.time()+31*86400):
                self.assertTrue(l.claim('chat','old'))
                self.assertNotIn(('chat','old'),l.sent)
            restarted=DeliveryLedger(path,retention_days=0);restarted.recover()
            self.assertEqual(sqlite3.connect(path).execute('SELECT COUNT(*) FROM chart_delivery WHERE sent=1').fetchone()[0],0)

    def test_ledger_without_timestamp_column_is_migrated_and_keeps_sent_rows(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'ledger.db';db=sqlite3.connect(path)
            db.execute('CREATE TABLE chart_delivery (chat TEXT, fingerprint TEXT, token TEXT, sent INTEGER NOT NULL, PRIMARY KEY(chat,fingerprint))')
            db.execute("INSERT INTO chart_delivery VALUES ('chat','old','t',1)");db.commit();db.close()
            l=DeliveryLedger(path);l.recover()
            self.assertIsNone(l.claim('chat','old'))


class SendEvent:
    unified_msg_origin='platform:group:test'
    def __init__(self):self.sent=[];self.fail=False
    def image_result(self,path):return ('image',path)
    def plain_result(self,text):return ('text',text)
    async def send(self,value):
        if self.fail and value[0]=='image':raise OSError('adapter failed')
        if value[0]=='image':assert Path(value[1]).exists()
        self.sent.append(value)


class FakeChartSession:
    def __init__(self):
        self.overview={'fingerprint':'same-content'};self.exports=0;self.closed=False
    async def export_telemetry(self,target):
        self.exports+=1;Path(target).write_text('{}')
    async def close(self):self.closed=True


class ChartLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.manager=ChartManager(self.temp.name)
    async def asyncTearDown(self):
        await self.manager.close();self.temp.cleanup()
    async def fake_render(self,directory,timezone,enabled):
        (directory/'chart.png').write_bytes(b'png-test');return True

    async def test_disabled_does_not_claim_export_render_or_send(self):
        event,s=SendEvent(),FakeChartSession()
        with patch.object(self.manager,'render',AsyncMock()) as renderer:
            await self.manager.deliver(event,s,lambda:False)
            renderer.assert_not_called()
        self.assertEqual(s.exports,0);self.assertEqual(event.sent,[])
        self.assertFalse(self.manager.ledger.path.exists())

    async def test_once_per_chat_even_after_restart_and_png_lifetime(self):
        event=SendEvent()
        with patch.object(self.manager,'render',side_effect=self.fake_render):
            await self.manager.deliver(event,FakeChartSession(),lambda:True)
            image=event.sent[0][1];self.assertTrue(Path(image).exists())
            self.assertFalse((Path(image).parent/'telemetry.json').exists())
            s=FakeChartSession();await self.manager.deliver(SendEvent(),s,lambda:True);self.assertEqual(s.exports,0)
        other=ChartManager(self.temp.name)
        try:
            await other.initialize()
            s=FakeChartSession();await other.deliver(SendEvent(),s,lambda:True);self.assertEqual(s.exports,0)
            second=SendEvent();second.unified_msg_origin='platform:group:other'
            with patch.object(other,'render',side_effect=self.fake_render):
                await other.deliver(second,FakeChartSession(),lambda:True)
            self.assertEqual(second.sent[0][0],'image')
        finally:await other.close()

    async def test_send_failure_can_retry(self):
        event=SendEvent();event.fail=True
        with patch.object(self.manager,'render',side_effect=self.fake_render):
            await self.manager.deliver(event,FakeChartSession(),lambda:True)
            event.fail=False
            await self.manager.deliver(event,FakeChartSession(),lambda:True)
        self.assertEqual(sum(v[0]=='image' for v in event.sent),1)

    async def test_ledger_error_fails_closed_without_rendering(self):
        s=FakeChartSession()
        with patch.object(self.manager.ledger,'claim',side_effect=sqlite3.DatabaseError('broken')), patch.object(self.manager,'render',AsyncMock()) as render:
            await self.manager.deliver(SendEvent(),s,lambda:True);render.assert_not_called()
        self.assertEqual(s.exports,0)

    async def test_successful_send_with_failed_ack_is_not_sent_twice_in_process(self):
        event=SendEvent()
        def failed_ack(*args):
            self.manager.ledger.sent.add((args[0],args[1]));raise sqlite3.OperationalError('locked')
        with patch.object(self.manager.ledger,'complete',side_effect=failed_ack),patch.object(self.manager,'render',side_effect=self.fake_render):
            await self.manager.deliver(event,FakeChartSession(),lambda:True)
            s=FakeChartSession();await self.manager.deliver(event,s,lambda:True)
        self.assertEqual(s.exports,0)
        self.assertEqual(sum(v[0]=='image' for v in event.sent),1)

    async def test_switch_off_or_deadline_kills_and_reaps_renderer(self):
        for shutdown in ('switch','timeout'):
            started,done=asyncio.Event(),asyncio.Event();state=[True]
            class Process:
                returncode=None
                async def wait(self):started.set();await done.wait();return self.returncode
                def kill(self):self.returncode=-9;done.set()
            process=Process()
            directory=Path(self.temp.name)/shutdown;directory.mkdir()
            with patch('spark_core.charts.asyncio.create_subprocess_exec',AsyncMock(return_value=process)):
                task=asyncio.create_task(self.manager.render(directory,'UTC',lambda:state[0],timeout=.01 if shutdown=='timeout' else 2))
                await asyncio.wait_for(started.wait(),2)
                if shutdown=='switch':
                    state[0]=False;self.assertFalse(await task)
                else:
                    with self.assertRaises(TimeoutError):await task
            self.assertEqual(process.returncode,-9);self.assertFalse(self.manager.processes)

    async def test_cancellation_during_spawn_does_not_orphan_renderer(self):
        started,release=asyncio.Event(),asyncio.Event()
        class Process:
            returncode=None
            def kill(self):self.returncode=-9
            async def wait(self):return self.returncode
        process=Process()
        async def spawning(*args,**kwargs):started.set();await release.wait();return process
        with patch('spark_core.charts.asyncio.create_subprocess_exec',side_effect=spawning):
            task=asyncio.create_task(self.manager.render(Path(self.temp.name),'UTC',lambda:True))
            await started.wait();task.cancel();release.set()
            with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(process.returncode,-9)

    async def test_switch_off_after_export_does_not_render_or_consume(self):
        state=[True];s=FakeChartSession()
        async def exporting(target):state[0]=False;Path(target).write_text('{}')
        s.export_telemetry=exporting
        with patch.object(self.manager,'render',AsyncMock()) as render:
            await self.manager.deliver(SendEvent(),s,lambda:state[0]);render.assert_not_called()
        state[0]=True
        with patch.object(self.manager,'render',side_effect=self.fake_render):
            e=SendEvent();await self.manager.deliver(e,FakeChartSession(),lambda:True)
        self.assertEqual(e.sent[0][0],'image')

    async def test_cancelled_export_releases_claim_and_files(self):
        started=asyncio.Event();s=FakeChartSession()
        async def exporting(target):started.set();await asyncio.Event().wait()
        s.export_telemetry=exporting
        task=asyncio.create_task(self.manager.deliver(SendEvent(),s,lambda:True))
        await asyncio.wait_for(started.wait(),2);task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertFalse(list(Path(self.temp.name).glob('chart-*')))
        self.assertTrue(self.manager.ledger.claim(__import__('hashlib').sha256(SendEvent.unified_msg_origin.encode()).hexdigest(),'same-content'))

    async def test_real_worker_export_and_render_with_packaged_font(self):
        s=ReportSession(self.temp.name)
        try:
            (s.directory/'profile.bin').write_bytes(sample().SerializeToString())
            await s.parse()
            out=Path(self.temp.name)/'render';out.mkdir()
            await s.export_telemetry(out/'telemetry.json');await s.close()
            with patch.dict(os.environ, {'PYTHONSAFEPATH':'1'}):
                self.assertTrue(await self.manager.render(out,'Asia/Seoul',lambda:True))
            from PIL import Image
            with Image.open(out/'chart.png') as image:self.assertEqual(image.size,(2400,1500))
        finally:await s.close()


class ChartEntryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture=plugin_test.PluginTests();self.fixture.setUp();self.module=self.fixture.module
    def tearDown(self):self.fixture.tearDown()

    async def test_all_original_entries_respect_switch_and_keep_text(self):
        for entry in ('command','auto','tool'):
            for enabled in (False,True):
                with self.subTest(entry=entry,enabled=enabled):
                    async def agent(**kwargs):return types.SimpleNamespace(completion_text='文字结论')
                    plugin=self.module.SparkPlugin(types.SimpleNamespace(tool_loop_agent=agent),
                                {'analysis_provider_id':'dedicated','chart_enabled':enabled})
                    e=plugin_test.Event();e.stopped=False;e.message_str='/spark https://spark.lucko.me/SyntheticReport001 分析'
                    e.send=AsyncMock()
                    async def deliver(event,session,switch,timezone):
                        if switch():await event.send('chart')
                    with patch.object(plugin.charts,'deliver',side_effect=deliver), patch.object(self.module,'ReportSession',plugin_test.FakeSession):
                        if entry=='command':result=[x async for x in plugin.spark_command(e)]
                        elif entry=='auto':
                            e.message_str='分析 https://spark.lucko.me/SyntheticReport001'
                            result=[x async for x in plugin.auto_analyze(e)]
                        else:
                            result=[x async for x in plugin.analyze_tool(e,'https://spark.lucko.me/SyntheticReport001')]
                            await asyncio.gather(*plugin.background_tasks)
                    charts=[call for call in e.send.call_args_list if call.args==('chart',)]
                    self.assertEqual(len(charts),int(enabled))
                    if entry=='tool':self.assertFalse(e.stopped)
                    else:self.assertTrue(any('文字结论' in value for value in result))
                    await plugin.terminate()

    async def test_file_then_link_in_same_chat_deduplicates_across_senders(self):
        class Session(plugin_test.FakeSession):
            async def load(self,url):
                self.overview=await super().load(url);return self.overview
            async def export_telemetry(self,target):Path(target).write_text('{}')
        class Event(plugin_test.Event,SendEvent):
            def get_sender_id(self):return self.sender
        async def agent(**kwargs):return types.SimpleNamespace(completion_text='文字结论')
        plugin=self.module.SparkPlugin(types.SimpleNamespace(tool_loop_agent=agent),{'analysis_provider_id':'dedicated','chart_enabled':True})
        async def rendering(directory,*args):(directory/'chart.png').write_bytes(b'png-test');return True
        try:
            with patch.object(self.module,'ReportSession',Session),patch.object(plugin.charts,'render',side_effect=rendering):
                e=Event();e.sender='alice';e.message_str='';e.messages=[plugin_test.File('first.sparkprofile','/attachment')]
                first=[x async for x in plugin.auto_analyze(e)]
                self.assertTrue(any('文字结论' in x for x in first))
                e2=Event();e2.sender='bob';e2.message_str='/spark https://spark.lucko.me/SyntheticReport001';e2.messages=[]
                second=[x async for x in plugin.spark_command(e2)]
                self.assertTrue(any('文字结论' in x for x in second))
                self.assertEqual(sum(isinstance(x,tuple) and x[0]=='image' for x in e.sent),1)
                self.assertEqual(sum(isinstance(x,tuple) and x[0]=='image' for x in e2.sent),0)
        finally:await plugin.terminate()

    async def test_model_failure_does_not_deliver_chart(self):
        class Session(plugin_test.FakeSession):
            async def load(self,url):self.overview=await super().load(url);return self.overview
        e=plugin_test.Event();e.send=AsyncMock()
        async def agent(**kwargs):raise RuntimeError('provider failed')
        plugin=self.module.SparkPlugin(types.SimpleNamespace(tool_loop_agent=agent),{'analysis_provider_id':'dedicated','chart_enabled':True})
        deliver=AsyncMock()
        with patch.object(self.module,'ReportSession',Session),patch.object(plugin.charts,'deliver',deliver):
            result=[x async for x in plugin.spark_command(e)]
        deliver.assert_not_awaited()
        self.assertTrue(any('未回落' in x for x in result))
        await plugin.terminate()
