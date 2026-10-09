"""SDK boundary doubles, not a claim of a running AstrBot installation."""
import asyncio
import importlib.util
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
    def is_admin(self): return getattr(self, 'admin', False)
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
        self.assertIn('历史记录', result[-1])
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
        self.assertEqual(delivered, ['已收到Spark报告，开始分析流程。', '最终分析'])
        self.assertTrue(event.stopped)
        self.assertTrue(any('model start' in item[0] for item in self.logs))

    async def test_access_modes_and_all_entry_points(self):
        plugin = self.module.SparkPlugin(types.SimpleNamespace(), {'access_mode':'admin_only'})
        event = Event()
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
        event.message_str = '帮我分析 https://spark.lucko.me/SyntheticReport001'
        for handler in (plugin.auto_analyze(event), plugin.spark_command(event), plugin.analyze_tool(event, 'https://spark.lucko.me/SyntheticReport001')):
            results = [r async for r in handler]
            self.assertIn('权限', results[0])

    async def test_duplicate_inflight_report_is_quiet(self):
        plugin = self.module.SparkPlugin(types.SimpleNamespace(), {'analysis_provider_id':'test'})
        plugin.inflight_reports.add('SyntheticReport001')
        result = [r async for r in plugin.handle(Event(), 'https://spark.lucko.me/SyntheticReport001')]
        self.assertEqual(result, [])
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
        self.assertIn('inclusive包含子调用', captured['system_prompt'])
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
            self.assertIn('后台', result[0])
            await asyncio.wait_for(started.wait(), 1)
            self.assertNotIn('后台结果', sent)
            release.set()
            await asyncio.gather(*list(plugin.background_tasks))
        self.assertIn('后台结果', sent)

if __name__ == '__main__': unittest.main()
