import asyncio
import hashlib
import json
import re
import shutil
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.agent.tool import FunctionTool, ToolSet

from .spark_core.session import ReportSession, LoadTimeout
from .spark_core.profile import ProfileError
from .spark_core.history import History, compare
from .spark_core.transport import LINK
from .spark_core.output import plain_text
from .spark_core.cache import ProfileCache
from .spark_core.tasks import cancel_bounded


@register('astrbot_plugin_spark', 'Evergarden_Roxy', '隔离解析Spark报告并使用专用模型分析', '1.0.2')
class SparkPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.root = StarTools.get_data_dir('astrbot_plugin_spark')
        # One session runs at a time and none exists yet, so any spark-* directory was left by a
        # crashed host. Removing it also makes an orphaned worker exit.
        for stale in self.root.glob('spark-*'):
            shutil.rmtree(stale, ignore_errors=True)
        self.cache = ProfileCache(self.root/'profiles.sqlite3', config.get('profile_cache_hours', 72))
        self.background_tasks = set()
        self.history = History(self.root/'history.sqlite3', bool(config.get('history_enabled', False)))
        self.policy = Path(__file__).with_name('analysis_policy.md').read_text(encoding='utf-8')
        self.default_reply_prompt = Path(__file__).with_name('reply_prompt.txt').read_text(encoding='utf-8')
        self.active = set()
        self.inflight_reports = set()
        self.tasks = set()
        self.sessions = set()
        self.gate = asyncio.Semaphore(1)

    def owner(self, event):
        return hashlib.sha256((event.unified_msg_origin+'\0'+event.get_sender_id()).encode()).hexdigest()

    def allowed(self, event):
        allow = self.config.get('allowed_origins', [])
        if allow and event.unified_msg_origin not in allow:
            return False
        mode = self.config.get('access_mode', 'all')
        admin = event.is_admin()
        if mode == 'admin_only':
            return admin
        if mode == 'admin_and_whitelist':
            return admin or str(event.get_sender_id()) in {str(x) for x in self.config.get('user_whitelist', [])}
        return mode == 'all'

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def auto_analyze(self, event: AstrMessageEvent):
        text = event.message_str
        if text.lstrip('/').startswith('spark '):
            return
        if not self.config.get('auto_analyze', True):
            return
        links = LINK.findall(text)
        if not links or not re.search(r'分析|诊断|排查|卡顿|掉tps|analy[sz]e', text, re.I):
            return
        # Silent for non-permitted users so the message still reaches other handlers.
        if not self.allowed(event):
            return
        async for result in self.handle(event, text):
            yield result

    @filter.command('spark')
    async def spark_command(self, event: AstrMessageEvent):
        text = re.sub(r'^/?spark(?:\s+|$)', '', event.message_str.strip(), count=1)
        if not self.allowed(event):
            logger.info('Spark access denied; mode=%s', self.config.get('access_mode', 'all'))
            yield event.plain_result('此会话或用户没有Spark分析权限。')
            event.stop_event()
            return
        if text == 'forget':
            count = await asyncio.to_thread(self.history.delete, self.owner(event))
            yield event.plain_result(f'已清除当前发送者在此会话的历史记录：{count}条。关闭存储时不会打开数据库；旧库须启用后清除。')
            event.stop_event()
            return
        async for result in self.handle(event, text):
            yield result

    @filter.llm_tool(name='spark_analyze')
    async def analyze_tool(self, event: AstrMessageEvent, report_url: str, observation: str = ''):
        '''加载Spark报告并交给插件配置的专用分析模型；失败按备用模型顺序重试，不由主聊天模型分析原始数据。

        Args:
            report_url(string): 官方https://spark.lucko.me/报告ID链接。
            observation(string): 用户现象，可附server=标签 problem=标签 compare。
        '''
        if not self.allowed(event):
            yield '此会话或用户没有Spark分析权限。'
            return
        async def run():
            try:
                async for result in self.handle(event, report_url+' '+observation, stop=False):
                    await event.send(result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning('Spark background delivery failed: %s', type(exc).__name__)
        task = asyncio.create_task(run())
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)
        yield 'Spark分析已转交插件后台执行，完成后直接发送结果；不要重复提交或自行假设分析结论。'

    async def handle(self, event, text, *, stop=True):
        # stop=False for the tool path: the event belongs to the main chat agent, and
        # stopping it aborts the main model's reply.
        if not self.allowed(event):
            logger.info('Spark access denied; mode=%s', self.config.get('access_mode', 'all'))
            yield event.plain_result('此会话或用户没有Spark分析权限。')
            if stop:
                event.stop_event()
            return
        links = list(dict.fromkeys(LINK.findall(text)))
        if len(links) != 1:
            yield event.plain_result('请提供一个 https://spark.lucko.me/报告ID 。可附 server=服务器标签 problem=问题标签；比较时添加 compare。')
            if stop:
                event.stop_event()
            return
        provider = str(self.config.get('analysis_provider_id', '')).strip()
        if not provider:
            yield event.plain_result('请先在插件配置选择Spark专用分析模型；不会回落到主聊天模型。')
            if stop:
                event.stop_event()
            return
        owner = self.owner(event)
        if links[0] in self.inflight_reports:
            logger.info('Spark duplicate in-flight report skipped')
            if stop:
                event.stop_event()
            return
        if owner in self.active or self.gate.locked():
            yield event.plain_result('已有Spark分析正在执行，请稍后再试。')
            if stop:
                event.stop_event()
            return
        self.active.add(owner)
        self.inflight_reports.add(links[0])
        task = asyncio.current_task()
        self.tasks.add(task)
        session = None
        trace = owner[:8]
        logger.info('Spark [%s] accepted; report=%s', trace, links[0])
        try:
            async with self.gate:
                acknowledgement = str(self.config.get('acknowledgement_text', '已收到Spark报告，开始分析流程。')).strip()
                if acknowledgement:
                    yield event.plain_result(acknowledgement[:1000])
                download_timeout = max(15, min(900, int(self.config.get('download_timeout_seconds', 120))))
                parse_timeout = max(15, min(300, int(self.config.get('parse_timeout_seconds', 90))))
                proxy = str(self.config.get('download_proxy_url', '')).strip() if self.config.get('download_proxy_enabled', False) else None
                session = ReportSession(self.root, download_timeout=download_timeout, parse_timeout=parse_timeout, cache=self.cache, proxy=proxy)
                self.sessions.add(session)
                logger.info('Spark [%s] download/parse start', trace)
                overview = await session.load('https://spark.lucko.me/'+links[0])
                logger.info('Spark [%s] download/parse success; threads=%s', trace, len(overview.get('threads', [])))
                server_match = re.search(r'(?:^|\s)server=([^\s]{1,80})', text)
                problem_match = re.search(r'(?:^|\s)problem=([^\s]{1,80})', text)
                server = server_match.group(1) if server_match else overview.get('runtime', {}).get('server_hint', {}).get('tag', '')
                problem = problem_match.group(1) if problem_match else (
                    'JVM与GC' if re.search(r'JVM|GC|启动参数|堆内存', text, re.I) else '性能分析')
                history = await asyncio.to_thread(self.history.list, owner, server, problem) if server else []
                comparison = compare(history[0]['overview'], overview) if history and 'compare' in text.lower() else None
                calls = 0

                async def query(tool_event, view='hotspots', thread=0, node=-1, window=None, search='', offset=0):
                    nonlocal calls
                    if self.owner(tool_event) != owner:
                        return json.dumps({'error': '会话不匹配'})
                    calls += 1
                    if calls > 8:
                        return json.dumps({'error': '查询预算已耗尽，请依据已有证据总结'})
                    logger.debug('Spark [%s] query #%s view=%s thread=%s node=%s window=%s offset=%s', trace, calls, view, thread, node, window, offset)
                    answer = await session.query(view=view, thread=thread, node=node, window=window, search=search, offset=offset)
                    logger.debug('Spark [%s] query complete; rows=%s error=%s', trace, len(answer.get('rows', [])), bool(answer.get('error')))
                    return json.dumps(answer, ensure_ascii=False)

                tool = FunctionTool(name='spark_query', description='只读查询本次已加载报告；hotspots按self排序，children按inclusive排序；callers还原路径，search字面搜索。thread/node/window从概览或先前结果选取。',
                    parameters={'type':'object','properties':{
                        'view':{'type':'string','enum':['hotspots','children','callers','search']},
                        'thread':{'type':'integer'},'node':{'type':'integer'},
                        'window':{'type':'integer'},'search':{'type':'string','maxLength':120},
                        'offset':{'type':'integer','minimum':0,'maximum':1000}},'required':['view','thread']}, handler=query)
                prompt = json.dumps({'user_observation': text[:2000], 'overview': overview,
                                     'history_comparison': comparison,
                                     'previous_review_untrusted': history[0]['result'][:6000] if comparison else None}, ensure_ascii=False)
                providers = list(dict.fromkeys([provider] + [
                    str(item['provider_id']).strip()
                    for item in self.config.get('fallback_providers', [])
                    if isinstance(item, dict) and item.get('provider_id')]))
                for attempt, provider_id in enumerate(providers):
                    remaining = set()
                    calls = 0
                    try:
                        logger.info('Spark [%s] model start; attempt=%s/%s provider=%s', trace, attempt+1, len(providers), provider_id)
                        model_task = asyncio.create_task(self.context.tool_loop_agent(
                            event=event, chat_provider_id=provider_id, prompt=prompt,
                            system_prompt=self.policy+'\n\n回复要求：\n'+(str(self.config.get('analysis_prompt', '')).strip() or self.default_reply_prompt), tools=ToolSet([tool]), max_steps=10,
                            tool_call_timeout=35))
                        try:
                            for tick in range(10):
                                done, _ = await asyncio.wait({model_task}, timeout=30)
                                if done:
                                    break
                                logger.info('Spark [%s] model waiting; provider=%s elapsed=%ss queries=%s', trace, provider_id, (tick+1)*30, calls)
                            if not model_task.done():
                                raise TimeoutError('分析模型超过300秒')
                            response = await model_task
                        finally:
                            remaining = await cancel_bounded({model_task})
                            # The in-flight exception propagates; `except Exception` below stops
                            # falling back while a stuck task could still send a duplicate request.
                            self.background_tasks.update(remaining)
                            for stuck in remaining:
                                stuck.add_done_callback(self.background_tasks.discard)
                        result = response.completion_text
                        if not result or not result.strip():
                            raise ValueError('分析模型没有返回文字结论')
                        logger.info('Spark [%s] model success; provider=%s queries=%s', trace, provider_id, calls)
                        break
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.warning('Spark provider attempt %s failed: %s', attempt+1, type(exc).__name__)
                        if remaining or attempt+1 == len(providers):
                            raise
                        yield event.plain_result(f'分析模型第{attempt+1}次尝试失败（{type(exc).__name__}），使用下一个模型继续尝试。')
                history_id = await asyncio.to_thread(self.history.save, owner, server, problem, overview, result)
                suffix = f'\n历史记录：{history_id}（server={server or "未标记"}，problem={problem or "未标记"}）' if history_id else ''
                logger.info('Spark [%s] history saved=%s; sending final result', trace, bool(history_id))
                yield event.plain_result(plain_text(result)+suffix)
                logger.info('Spark [%s] result delivered', trace)
        except asyncio.CancelledError:
            raise
        except LoadTimeout as exc:
            logger.warning('Spark [%s] load failed: %s', trace, str(exc))
            yield event.plain_result('Spark分析未完成：'+str(exc)+'。请查看下载完成/解析开始日志定位；未生成成功审查记录。')
        except ProfileError as exc:
            logger.warning('Spark [%s] report rejected: %s', trace, exc)
            yield event.plain_result('Spark分析未完成：'+str(exc)+'。未回落主LLM、未生成成功审查记录。')
        except Exception as exc:
            logger.warning('Spark analysis failed: %s', type(exc).__name__)
            yield event.plain_result('Spark分析未完成：'+type(exc).__name__+'。未回落主LLM、未生成成功审查记录。请检查链接、报告类型、体积或专用模型配置。')
        finally:
            if session:
                try:
                    await session.close()
                except Exception as exc:
                    logger.warning('Spark [%s] cleanup failed: %s', trace, type(exc).__name__)
                self.sessions.discard(session)
            self.active.discard(owner)
            self.inflight_reports.discard(links[0])
            self.tasks.discard(task)
            if stop:
                event.stop_event()
            logger.info('Spark [%s] cleanup complete', trace)

    async def terminate(self):
        remaining = await cancel_bounded(self.background_tasks | self.tasks)
        if remaining:
            logger.warning('Spark terminate: %s tasks did not stop within 5s', len(remaining))
        for session in list(self.sessions):
            try:
                await session.close()
            except Exception as exc:
                logger.warning('Spark terminate: session close failed: %s', type(exc).__name__)
