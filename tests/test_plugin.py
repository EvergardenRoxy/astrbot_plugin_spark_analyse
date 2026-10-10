"""SDK boundary doubles, not a claim of a running AstrBot installation."""
import asyncio
import importlib
import importlib.util
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

class Event:
    unified_msg_origin = 'platform:private:alice'
    message_str = '/spark https://spark.lucko.me/SyntheticReport001 server=test problem=lag compare'
    def get_sender_id(self): return 'alice'
    def plain_result(self, text): return text
    stopped = False
    # Default access is admin_only, so the test user is an admin unless a test says otherwise.
    admin = True
    def is_admin(self): return self.admin
    def stop_event(self): self.stopped = True

class FakeSession:
    def __init__(self, root, **kwargs): self.closed = False
    async def load(self, url):
        from test_core import sample
        from spark_core.profile import Profile
        return Profile(sample().SerializeToString()).overview()
    async def query(self, **args): return {'rows': [], 'denominator_ms': 100}
    async def close(self): self.closed = True

class PluginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        api = types.ModuleType('astrbot.api')
        api.AstrBotConfig = dict
        self.logs = []
        api.logger = types.SimpleNamespace(**{level: (lambda *a: self.logs.append(a)) for level in ('warning', 'info', 'debug')})
        event = types.ModuleType('astrbot.api.event')
        decorator = lambda *a, **k: lambda f: f
        event.filter = types.SimpleNamespace(llm_tool=decorator, command=decorator, event_message_type=decorator, EventMessageType=types.SimpleNamespace(ALL=0))
        event.AstrMessageEvent = Event
        star = types.ModuleType('astrbot.api.star')
        class Star:
            def __init__(self, context): self.context = context
        star.Star = Star
        star.Context = object
        star.StarTools = types.SimpleNamespace(get_data_dir=lambda name: Path(self.temp.name))
        star.register = decorator
        tool = types.ModuleType('astrbot.core.agent.tool')
        tool.FunctionTool = lambda **kw: types.SimpleNamespace(**kw)
        tool.ToolSet = lambda tools: tools
        self.modules = patch.dict(sys.modules, {'astrbot':types.ModuleType('astrbot'), 'astrbot.api':api, 'astrbot.api.event':event, 'astrbot.api.star':star, 'astrbot.core.agent.tool':tool})
        self.modules.start()
        package = types.ModuleType('spark_test_plugin')
        package.__path__ = [str(ROOT)]
        sys.modules['spark_test_plugin'] = package
        spec = importlib.util.spec_from_file_location('spark_test_plugin.main', ROOT/'main.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def tearDown(self):
        self.modules.stop()
        self.temp.cleanup()

    async def test_missing_provider_does_not_call_main(self):
        context = types.SimpleNamespace()
        plugin = self.module.SparkPlugin(context, {})
        result = [r async for r in plugin.spark_command(Event())]
        self.assertIn('不会回落', result[0])
        self.assertFalse((Path(self.temp.name)/'history.sqlite3').exists())

    async def test_explicit_provider_and_only_bound_tool(self):
        captured = {}
        async def agent(**kwargs):
            captured.update(kwargs)
            result = await kwargs['tools'][0].handler(kwargs['event'], view='hotspots', thread=0)
            self.assertIn('denominator_ms', result)
            return types.SimpleNamespace(completion_text='有证据的候选分析')
        context = types.SimpleNamespace(tool_loop_agent=agent)
        plugin = self.module.SparkPlugin(context, {'analysis_provider_id':'dedicated', 'history_enabled':True})
        with patch.object(self.module, 'ReportSession', FakeSession):
            result = [r async for r in plugin.spark_command(Event())]
        self.assertEqual(captured['chat_provider_id'], 'dedicated')
        self.assertEqual(len(captured['tools']), 1)
        self.assertEqual(len(plugin.history.list(plugin.owner(Event()), 'test', 'lag')), 1)
        self.assertIn('compare，并带上 server=test problem=lag', result[-1])
        self.assertNotIn(plugin.history.list(plugin.owner(Event()), 'test', 'lag')[0]['id'], result[-1])
        self.assertFalse(plugin.sessions)
        self.assertFalse(plugin.active)

    async def test_failure_not_saved_or_fallback(self):
        calls = []
        async def agent(**kwargs):
            calls.append(kwargs['chat_provider_id'])
            raise RuntimeError('provider error')
        plugin = self.module.SparkPlugin(types.SimpleNamespace(tool_loop_agent=agent), {'analysis_provider_id':'dedicated', 'history_enabled':True})
        with patch.object(self.module, 'ReportSession', FakeSession):
            result = [r async for r in plugin.spark_command(Event())]
        self.assertEqual(calls, ['dedicated'])
        self.assertIn('未回落', result[-1])
        self.assertFalse((Path(self.temp.name)/'history.sqlite3').exists())

    async def test_ordered_fallback_and_tool_entry(self):
        calls = []
        async def agent(**kwargs):
            calls.append(kwargs['chat_provider_id'])
            if len(calls) < 3:
                raise RuntimeError('discount provider unavailable')
            return types.SimpleNamespace(completion_text='备用模型成功')
        plugin = self.module.SparkPlugin(types.SimpleNamespace(tool_loop_agent=agent), {
            'analysis_provider_id':'first', 'fallback_providers':[{'provider_id':'second'}, {'provider_id':'third'}], 'history_enabled':True})
        with patch.object(self.module, 'ReportSession', FakeSession):
            results = [r async for r in plugin.handle(Event(), 'https://spark.lucko.me/SyntheticReport001 server=test problem=lag')]
        self.assertEqual(calls, ['first', 'second', 'third'])
        self.assertEqual(sum('使用下一个模型' in r for r in results), 2)
        self.assertEqual(len(plugin.history.list(plugin.owner(Event()), 'test', 'lag')), 1)

    async def test_all_fallbacks_fail_without_history(self):
        calls = []
        async def agent(**kwargs):
            calls.append(kwargs['chat_provider_id'])
            raise TimeoutError()
        plugin = self.module.SparkPlugin(types.SimpleNamespace(tool_loop_agent=agent), {
            'analysis_provider_id':'first', 'fallback_providers':[{'provider_id':'second'}], 'history_enabled':True})
        with patch.object(self.module, 'ReportSession', FakeSession):
            results = [r async for r in plugin.handle(Event(), 'https://spark.lucko.me/SyntheticReport001')]
        self.assertEqual(calls, ['first', 'second'])
        self.assertIn('未生成成功', results[-1])
        self.assertFalse((Path(self.temp.name)/'history.sqlite3').exists())

    async def test_scheduler_does_not_stop_after_progress(self):
        called = []
        async def agent(**kwargs):
            called.append(True)
            return types.SimpleNamespace(completion_text='最终分析')
        plugin = self.module.SparkPlugin(types.SimpleNamespace(tool_loop_agent=agent), {'analysis_provider_id':'test'})
        event = Event()
        delivered = []
        with patch.object(self.module, 'ReportSession', FakeSession):
            async for result in plugin.spark_command(event):
                # AstrBot scheduler checks stop before sending/resuming the generator.
                if event.stopped:
                    break
                delivered.append(result)
        self.assertTrue(called)
        self.assertEqual(delivered, ['已收到Spark报告，开始分析流程。', '最终分析\n（未进行历史对比：未开启“保存分析历史”。）'])
        self.assertTrue(event.stopped)
        self.assertTrue(any('model start' in item[0] for item in self.logs))

    async def test_access_modes_and_all_entry_points(self):
        plugin = self.module.SparkPlugin(types.SimpleNamespace(), {'access_mode':'admin_only'})
        event = Event()
        event.admin = False
        self.assertFalse(plugin.allowed(event))
        event.admin = True
        self.assertTrue(plugin.allowed(event))
        event.admin = False
        plugin.config['access_mode'] = 'admin_and_whitelist'
        plugin.config['user_whitelist'] = ['alice']
        self.assertTrue(plugin.allowed(event))
        plugin.config['user_whitelist'] = []
        self.assertFalse(plugin.allowed(event))
        plugin.config['access_mode'] = 'all'
        self.assertTrue(plugin.allowed(event))
        plugin.config['access_mode'] = 'admin_only'
        def fresh():
            event = Event()
            event.admin = False
            event.message_str = '帮我分析 https://spark.lucko.me/SyntheticReport001'
            return event
        # Auto analysis ignores non-permitted users so other handlers still see the message.
        event = fresh()
        self.assertEqual([r async for r in plugin.auto_analyze(event)], [])
        self.assertFalse(event.stopped)
        for handler in (plugin.spark_command, lambda e: plugin.analyze_tool(e, 'https://spark.lucko.me/SyntheticReport001')):
            results = [r async for r in handler(fresh())]
            self.assertIn('权限', results[0])

    async def test_default_access_is_admin_only(self):
        # Code fallback and schema default must agree; AstrBot fills missing keys from the schema.
        schema = __import__('json').loads((ROOT/'_conf_schema.json').read_text(encoding='utf-8'))
        self.assertEqual(schema['access_mode']['default'], 'admin_only')
        plugin = self.module.SparkPlugin(types.SimpleNamespace(), {})
        member = Event()
        member.admin = False
        self.assertFalse(plugin.allowed(member))
        self.assertTrue(plugin.allowed(Event()))

    async def test_duplicate_inflight_report_is_quiet(self):
        plugin = self.module.SparkPlugin(types.SimpleNamespace(), {'analysis_provider_id':'test'})
        plugin.inflight_reports.add('SyntheticReport001')
        result = [r async for r in plugin.handle(Event(), 'https://spark.lucko.me/SyntheticReport001')]
        self.assertEqual(result, [])
        self.assertFalse(plugin.active)

    async def test_same_link_repeated_is_one_report(self):
        async def agent(**kwargs):
            return types.SimpleNamespace(completion_text='重复链接也能分析')
        plugin = self.module.SparkPlugin(types.SimpleNamespace(tool_loop_agent=agent), {'analysis_provider_id':'test'})
        link = 'https://spark.lucko.me/SyntheticReport001'
        with patch.object(self.module, 'ReportSession', FakeSession):
            results = [r async for r in plugin.handle(Event(), f'帮我分析 {link} 就是 {link} 这个')]
        self.assertEqual(results[-1], '重复链接也能分析')

    async def test_different_links_are_still_rejected(self):
        plugin = self.module.SparkPlugin(types.SimpleNamespace(), {'analysis_provider_id':'test'})
        text = 'https://spark.lucko.me/SyntheticReport001 https://spark.lucko.me/SyntheticReport002'
        results = [r async for r in plugin.handle(Event(), text)]
        self.assertEqual(len(results), 1)
        self.assertIn('请提供一个', results[0])

    async def test_report_rejection_reason_is_shown(self):
        # Must be the class main.py imported, not the top-level test copy.
        profile_error = importlib.import_module('spark_test_plugin.spark_core.profile').ProfileError
        class RejectSession(FakeSession):
            async def load(self, url): raise profile_error('报告类型不是sampler')
        plugin = self.module.SparkPlugin(types.SimpleNamespace(), {'analysis_provider_id':'test', 'history_enabled':True})
        with patch.object(self.module, 'ReportSession', RejectSession):
            results = [r async for r in plugin.spark_command(Event())]
        self.assertIn('报告类型不是sampler', results[-1])
        self.assertIn('未生成成功', results[-1])
        self.assertFalse((Path(self.temp.name)/'history.sqlite3').exists())
        self.assertFalse(plugin.active)

    async def test_custom_ack_and_load_timeout_no_model(self):
        class TimeoutSession(FakeSession):
            async def load(self, url):
                raise self_error('下载阶段超时（120秒）；尚未调用分析模型')
        self_error = self.module.LoadTimeout
        plugin = self.module.SparkPlugin(types.SimpleNamespace(), {'analysis_provider_id':'test', 'acknowledgement_text':'收到，正在检查'})
        with patch.object(self.module, 'ReportSession', TimeoutSession):
            result = [r async for r in plugin.spark_command(Event())]
        self.assertEqual(result[0], '收到，正在检查')
        self.assertIn('下载阶段超时', result[1])
        self.assertFalse(plugin.active)
        self.assertFalse(plugin.inflight_reports)

    async def test_editable_prompt_and_plain_delivery_raw_history(self):
        captured = {}
        async def agent(**kwargs):
            captured.update(kwargs)
            return types.SimpleNamespace(completion_text='## 结论\n**机器负载**')
        plugin = self.module.SparkPlugin(types.SimpleNamespace(tool_loop_agent=agent), {
            'analysis_provider_id':'test', 'analysis_prompt':'用简短中文回答', 'history_enabled':True})
        with patch.object(self.module, 'ReportSession', FakeSession):
            result = [r async for r in plugin.spark_command(Event())]
        self.assertIn('用简短中文回答', captured['system_prompt'])
        self.assertIn(plugin.policy.strip(), captured['system_prompt'])
        self.assertIn(plugin.guide.strip(), captured['system_prompt'])
        self.assertNotIn('##', result[-1])
        self.assertNotIn('**', result[-1])
        stored = plugin.history.list(plugin.owner(Event()), 'test', 'lag')[0]['result']
        self.assertEqual(stored, '## 结论\n**机器负载**')

    async def test_tool_returns_before_analysis_completes(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def agent(**kwargs):
            started.set()
            await release.wait()
            return types.SimpleNamespace(completion_text='后台结果')
        plugin = self.module.SparkPlugin(types.SimpleNamespace(tool_loop_agent=agent), {'analysis_provider_id':'test'})
        event = Event()
        sent = []
        async def send(result): sent.append(result)
        event.send = send
        with patch.object(self.module, 'ReportSession', FakeSession):
            result = [r async for r in plugin.analyze_tool(event, 'https://spark.lucko.me/SyntheticReport001')]
            # No return value: AstrBot ends the main agent's turn without a follow-up model call.
            self.assertEqual(result, [])
            await asyncio.wait_for(started.wait(), 1)
            self.assertNotIn('后台结果', sent)
            release.set()
            await asyncio.gather(*list(plugin.background_tasks))
        self.assertIn('后台结果', sent)
        self.assertEqual(sent[0], '已收到Spark报告，开始分析流程。')

    async def test_tool_duplicate_report_gets_notice(self):
        # The main model no longer replies after the tool, so a duplicate must not be silent there.
        plugin = self.module.SparkPlugin(types.SimpleNamespace(), {'analysis_provider_id':'test'})
        plugin.inflight_reports.add('SyntheticReport001')
        event, sent = Event(), []
        async def send(result): sent.append(result)
        event.send = send
        result = [r async for r in plugin.analyze_tool(event, 'https://spark.lucko.me/SyntheticReport001')]
        await asyncio.gather(*list(plugin.background_tasks))
        self.assertEqual(result, [])
        self.assertEqual(len(sent), 1)
        self.assertIn('正在分析', sent[0])
        self.assertFalse(event.stopped)

    async def test_startup_sweeps_stale_session_dirs(self):
        # A crashed host leaves spark-* directories holding the full decoded report.
        stale = Path(self.temp.name)/'spark-stale'
        stale.mkdir()
        (stale/'profile.bin').write_bytes(b'left behind by a crashed host')
        keep = Path(self.temp.name)/'profiles.sqlite3'
        keep.write_bytes(b'not a session dir')
        self.module.SparkPlugin(types.SimpleNamespace(), {})
        self.assertFalse(stale.exists())
        self.assertTrue(keep.exists())

    async def test_terminate_closes_every_session(self):
        attempts = []
        class Session:
            def __init__(self, broken): self.broken = broken
            async def close(self):
                attempts.append(self)
                if self.broken: raise OSError('busy')
        plugin = self.module.SparkPlugin(types.SimpleNamespace(), {})
        plugin.sessions.update({Session(True), Session(False), Session(True)})
        await plugin.terminate()
        # Set order is arbitrary, so require every close attempt rather than a specific survivor.
        self.assertEqual(len(attempts), 3)

    async def test_history_retention_and_forget_while_disabled(self):
        async def agent(**kwargs): return types.SimpleNamespace(completion_text='ok')
        path = Path(self.temp.name)/'history.sqlite3'
        enabled = self.module.SparkPlugin(types.SimpleNamespace(tool_loop_agent=agent), {'analysis_provider_id':'test', 'history_enabled':True})
        with patch.object(self.module, 'ReportSession', FakeSession):
            for _ in range(2):
                [r async for r in enabled.spark_command(Event())]
        with sqlite3.connect(path) as db:
            db.execute("UPDATE reviews SET created=0 WHERE rowid=1")
        db.close()
        disabled = self.module.SparkPlugin(types.SimpleNamespace(), {})
        await disabled.initialize()
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0], 1)
        db.close()
        event = Event()
        event.message_str = '/spark forget'
        result = [r async for r in disabled.spark_command(event)]
        self.assertIn('1条', result[0])
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0], 0)
        db.close()

    async def test_corrupt_history_database_does_not_block_loading(self):
        (Path(self.temp.name)/'history.sqlite3').write_bytes(b'this is not a sqlite database'*50)
        plugin = self.module.SparkPlugin(types.SimpleNamespace(), {})
        # AstrBot awaits initialize() inside its plugin-load try: anything escaping fails the load.
        await plugin.initialize()
        self.assertTrue(any('history purge failed' in item[0] for item in self.logs))

    async def test_locked_history_does_not_stall_loading(self):
        path = Path(self.temp.name)/'history.sqlite3'
        holder = sqlite3.connect(path)
        holder.execute('CREATE TABLE reviews (id TEXT PRIMARY KEY, owner TEXT, server TEXT, problem TEXT, created REAL, overview TEXT, result TEXT)')
        holder.commit()
        holder.execute('BEGIN EXCLUSIVE')
        ticks = 0
        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1
        running = asyncio.create_task(ticker())
        try:
            started = asyncio.get_running_loop().time()
            plugin = self.module.SparkPlugin(types.SimpleNamespace(), {})
            await plugin.initialize()
            elapsed = asyncio.get_running_loop().time() - started
        finally:
            running.cancel()
            holder.rollback()
            holder.close()
        # Bounded wait for the lock, and the event loop kept running meanwhile.
        self.assertLess(elapsed, 3)
        self.assertGreater(ticks, 10)
        self.assertTrue(any('history purge failed' in item[0] for item in self.logs))

    async def test_forget_reports_unreadable_history(self):
        (Path(self.temp.name)/'history.sqlite3').write_bytes(b'this is not a sqlite database'*50)
        plugin = self.module.SparkPlugin(types.SimpleNamespace(), {})
        event = Event()
        event.message_str = '/spark forget'
        result = [r async for r in plugin.spark_command(event)]
        self.assertEqual(len(result), 1)
        self.assertIn('未清除', result[0])
        self.assertIn('管理员', result[0])
        self.assertTrue(event.stopped)
        self.assertTrue(any('forget failed' in item[0] for item in self.logs))

    async def test_forget_reports_busy_history(self):
        plugin = self.module.SparkPlugin(types.SimpleNamespace(), {})
        event = Event()
        event.message_str = '/spark forget'
        with patch.object(plugin.history, 'delete', side_effect=sqlite3.OperationalError('database is locked')):
            result = [r async for r in plugin.spark_command(event)]
        self.assertIn('未清除', result[0])
        self.assertIn('稍后', result[0])

    async def test_history_save_failure_keeps_result(self):
        async def agent(**kwargs): return types.SimpleNamespace(completion_text='模型结论')
        plugin = self.module.SparkPlugin(types.SimpleNamespace(tool_loop_agent=agent), {'analysis_provider_id':'test', 'history_enabled':True})
        with patch.object(self.module, 'ReportSession', FakeSession), \
             patch.object(plugin.history, 'save', side_effect=sqlite3.OperationalError('database is locked')):
            result = [r async for r in plugin.spark_command(Event())]
        self.assertTrue(result[-1].startswith('模型结论'))
        self.assertNotIn('已保存本次结果', result[-1])

    async def test_history_lookup_failure_skips_comparison(self):
        captured = {}
        async def agent(**kwargs):
            captured.update(kwargs)
            return types.SimpleNamespace(completion_text='模型结论')
        plugin = self.module.SparkPlugin(types.SimpleNamespace(tool_loop_agent=agent), {'analysis_provider_id':'test', 'history_enabled':True})
        with patch.object(self.module, 'ReportSession', FakeSession), \
             patch.object(plugin.history, 'list', side_effect=sqlite3.DatabaseError('file is not a database')):
            result = [r async for r in plugin.spark_command(Event())]
        self.assertTrue(result[-1].startswith('模型结论'))
        self.assertIn('未进行历史对比：历史记录读取失败', result[-1])
        self.assertIsNone(self.module.json.loads(captured['prompt'])['history_comparison'])

    async def test_legacy_default_reply_prompt_is_replaced(self):
        from test_briefing import LEGACY_DEFAULT_REPLY
        saved = []
        class Config(dict):
            def save_config(self): saved.append(self['analysis_prompt'])
        captured = {}
        async def agent(**kwargs):
            captured.update(kwargs)
            return types.SimpleNamespace(completion_text='ok')
        config = Config(analysis_provider_id='test', analysis_prompt=LEGACY_DEFAULT_REPLY)
        plugin = self.module.SparkPlugin(types.SimpleNamespace(tool_loop_agent=agent), config)
        await plugin.initialize()
        # Persisted, so the config page shows the text that is actually used.
        self.assertEqual(saved, [plugin.default_reply_prompt])
        with patch.object(self.module, 'ReportSession', FakeSession):
            [r async for r in plugin.spark_command(Event())]
        self.assertTrue(captured['system_prompt'].endswith(plugin.default_reply_prompt.strip()))

    async def test_legacy_reply_prompt_is_not_used_even_if_unsaved(self):
        from test_briefing import LEGACY_DEFAULT_REPLY
        captured = {}
        async def agent(**kwargs):
            captured.update(kwargs)
            return types.SimpleNamespace(completion_text='ok')
        plugin = self.module.SparkPlugin(types.SimpleNamespace(tool_loop_agent=agent), {
            'analysis_provider_id': 'test', 'analysis_prompt': LEGACY_DEFAULT_REPLY})
        with patch.object(self.module, 'ReportSession', FakeSession):
            [r async for r in plugin.spark_command(Event())]
        self.assertNotIn('QQ', captured['system_prompt'])

    async def test_custom_reply_prompt_is_kept(self):
        saved = []
        class Config(dict):
            def save_config(self): saved.append(True)
        config = Config(analysis_prompt='只用三句话回答')
        plugin = self.module.SparkPlugin(types.SimpleNamespace(), config)
        await plugin.initialize()
        self.assertEqual((config['analysis_prompt'], saved), ('只用三句话回答', []))

    async def test_served_model_is_logged(self):
        async def agent(**kwargs):
            return types.SimpleNamespace(completion_text='ok', raw_completion=types.SimpleNamespace(model='routed-mini'))
        plugin = self.module.SparkPlugin(types.SimpleNamespace(tool_loop_agent=agent), {'analysis_provider_id': 'test'})
        with patch.object(self.module, 'ReportSession', FakeSession):
            [r async for r in plugin.spark_command(Event())]
        self.assertTrue(any('served_model' in item[0] and 'routed-mini' in item for item in self.logs))

    async def test_model_input_has_triage_and_tool_guidance(self):
        captured = {}
        async def agent(**kwargs):
            captured.update(kwargs)
            return types.SimpleNamespace(completion_text='ok')
        plugin = self.module.SparkPlugin(types.SimpleNamespace(tool_loop_agent=agent), {'analysis_provider_id': 'test'})
        with patch.object(self.module, 'ReportSession', FakeSession):
            [r async for r in plugin.spark_command(Event())]
        overview = __import__('json').loads(captured['prompt'])['overview']
        self.assertEqual(next(iter(overview)), 'triage')
        tool = captured['tools'][0]
        self.assertIn('不要调用', tool.description)
        for name, schema in tool.parameters['properties'].items():
            with self.subTest(parameter=name):
                self.assertTrue(schema.get('description'))

    async def test_tool_fast_failure_does_not_stop_main_event(self):
        # The tool shares the main agent's event; stopping it aborts the main model's reply.
        plugin = self.module.SparkPlugin(types.SimpleNamespace(), {})
        event, sent = Event(), []
        async def send(result): sent.append(result)
        event.send = send
        result = [r async for r in plugin.analyze_tool(event, 'https://spark.lucko.me/SyntheticReport001')]
        await asyncio.gather(*list(plugin.background_tasks))
        self.assertEqual(result, [])
        self.assertTrue(any('不会回落' in r for r in sent))
        self.assertFalse(event.stopped)

    async def test_tool_success_does_not_stop_main_event(self):
        async def agent(**kwargs): return types.SimpleNamespace(completion_text='后台结果')
        plugin = self.module.SparkPlugin(types.SimpleNamespace(tool_loop_agent=agent), {'analysis_provider_id':'test'})
        event, sent = Event(), []
        async def send(result): sent.append(result)
        event.send = send
        with patch.object(self.module, 'ReportSession', FakeSession):
            [r async for r in plugin.analyze_tool(event, 'https://spark.lucko.me/SyntheticReport001')]
            await asyncio.gather(*list(plugin.background_tasks))
        self.assertIn('后台结果', sent)
        self.assertFalse(event.stopped)

    async def test_cancel_with_uncancellable_model_task(self):
        started, stop = asyncio.Event(), asyncio.Event()
        async def stubborn_agent(**kwargs):
            started.set()
            while not stop.is_set():
                try: await asyncio.wait_for(stop.wait(), 3600)
                except asyncio.CancelledError: pass
        plugin = self.module.SparkPlugin(types.SimpleNamespace(tool_loop_agent=stubborn_agent), {'analysis_provider_id':'test'})
        real = self.module.cancel_bounded
        async def run():
            async for _ in plugin.handle(Event(), 'https://spark.lucko.me/SyntheticReport001'): pass
        try:
            with patch.object(self.module, 'ReportSession', FakeSession), \
                 patch.object(self.module, 'cancel_bounded', lambda tasks: real(tasks, timeout=0.05)):
                runner = asyncio.create_task(run())
                await asyncio.wait_for(started.wait(), 5)
                await asyncio.sleep(0.05)
                runner.cancel()
                await asyncio.wait({runner}, timeout=5)
            self.assertTrue(runner.cancelled())
            self.assertEqual(plugin.tasks, set())
        finally:
            stop.set()
            await asyncio.sleep(0.1)

    async def test_stuck_model_task_blocks_fallback(self):
        # A model task that survives cancellation could still send a request, so no fallback may start.
        calls = []
        async def agent(**kwargs):
            calls.append(kwargs['chat_provider_id'])
            raise RuntimeError('provider error')
        stuck = asyncio.get_running_loop().create_future()
        async def leave_stuck(tasks): return {stuck}
        plugin = self.module.SparkPlugin(types.SimpleNamespace(tool_loop_agent=agent), {
            'analysis_provider_id':'first', 'fallback_providers':[{'provider_id':'second'}]})
        try:
            with patch.object(self.module, 'ReportSession', FakeSession), \
                 patch.object(self.module, 'cancel_bounded', leave_stuck):
                results = [r async for r in plugin.handle(Event(), 'https://spark.lucko.me/SyntheticReport001')]
            self.assertEqual(calls, ['first'])
            self.assertIn('RuntimeError', results[-1])
            self.assertIn(stuck, plugin.background_tasks)
        finally:
            stuck.cancel()

    def test_compare_keyword_is_a_whole_word(self):
        for text in ('compare', 'COMPARE 一下', '调整后compare', 'server=a compare。', 'compare=上次'):
            with self.subTest(text=text):
                self.assertTrue(self.module.COMPARE.search(text))
        for text in ('comparison', 'compared', 'https://spark.lucko.me/xCompareYz1'):
            with self.subTest(text=text):
                self.assertFalse(self.module.COMPARE.search(text))

    async def test_compare_ignores_derived_problem_tag(self):
        # Without problem=, the tag is derived from the wording, so "掉TPS" and a JVM question get different tags.
        prompts = []
        async def agent(**kwargs):
            prompts.append(self.module.json.loads(kwargs['prompt']))
            return types.SimpleNamespace(completion_text='模型结论')
        plugin = self.module.SparkPlugin(types.SimpleNamespace(tool_loop_agent=agent), {'analysis_provider_id':'test', 'history_enabled':True})
        link = 'https://spark.lucko.me/SyntheticReport001'
        with patch.object(self.module, 'ReportSession', FakeSession):
            [r async for r in plugin.handle(Event(), f'{link} server=test 掉TPS')]
            result = [r async for r in plugin.handle(Event(), f'{link} server=test JVM 调整后 compare')]
            self.assertIsNotNone(prompts[-1]['history_comparison'])
            self.assertIsNone(prompts[-1]['history_comparison_unavailable'])
            self.assertNotIn('未进行历史对比', result[-1])
            # A typed problem= tag still has to match exactly.
            result = [r async for r in plugin.handle(Event(), f'{link} server=test problem=其他 compare')]
        self.assertIsNone(prompts[-1]['history_comparison'])
        self.assertIn('未进行历史对比：没有找到可对比的记录', result[-1])

    async def test_skipped_comparison_is_explained(self):
        prompts = []
        async def agent(**kwargs):
            prompts.append(self.module.json.loads(kwargs['prompt']))
            return types.SimpleNamespace(completion_text='模型结论')
        link = 'https://spark.lucko.me/SyntheticReport001'
        cases = [({}, f'{link} server=test compare', '未开启“保存分析历史”'),
                 ({'history_enabled': True}, f'{link} server=test compare', '没有找到可对比的记录'),
                 # The synthetic report has no OS or CPU data, so no server tag can be derived.
                 ({'history_enabled': True}, f'{link} compare', '请加上 server=')]
        for config, text, reason in cases:
            with self.subTest(reason=reason):
                plugin = self.module.SparkPlugin(types.SimpleNamespace(tool_loop_agent=agent), {'analysis_provider_id':'test', **config})
                with patch.object(self.module, 'ReportSession', FakeSession):
                    result = [r async for r in plugin.handle(Event(), text)]
                self.assertTrue(result[-1].startswith('模型结论'))
                self.assertIn('未进行历史对比：', result[-1])
                self.assertIn(reason, result[-1])
                self.assertIn(reason, prompts[-1]['history_comparison_unavailable'])
        # No compare keyword, no note.
        plugin = self.module.SparkPlugin(types.SimpleNamespace(tool_loop_agent=agent), {'analysis_provider_id':'test'})
        with patch.object(self.module, 'ReportSession', FakeSession):
            result = [r async for r in plugin.handle(Event(), f'{link} server=test comparison of last week')]
        self.assertEqual(result[-1], '模型结论')
        self.assertIsNone(prompts[-1]['history_comparison_unavailable'])

    async def test_auto_analyze_keywords(self):
        async def agent(**kwargs): return types.SimpleNamespace(completion_text='模型结论')
        plugin = self.module.SparkPlugin(types.SimpleNamespace(tool_loop_agent=agent), {'analysis_provider_id':'test'})
        for words, triggers in (('need analysis', True), ('please analyse', True), ('Analyze this', True),
                                ('帮我分析', True), ('analytics link', False), ('看看这个', False)):
            with self.subTest(words=words):
                event = Event()
                event.message_str = f'{words} https://spark.lucko.me/SyntheticReport001'
                with patch.object(self.module, 'ReportSession', FakeSession):
                    results = [r async for r in plugin.auto_analyze(event)]
                self.assertEqual(bool(results), triggers)

if __name__ == '__main__': unittest.main()
