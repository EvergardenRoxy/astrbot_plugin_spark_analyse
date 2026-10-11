import asyncio
import hashlib
import json
import re
import shutil
import sqlite3
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Reply
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.agent.tool import FunctionTool, ToolSet

from .spark_core.session import ReportSession, LoadTimeout
from .spark_core.profile import ProfileError
from .spark_core.history import History, compare
from .spark_core.transport import LINK, is_report_file
from .spark_core.output import plain_text
from .spark_core.cache import ProfileCache
from .spark_core.tasks import cancel_bounded
from .spark_core.briefing import is_legacy_reply, reply_requirements, system_prompt, user_payload

# Words that start an analysis when a report link is posted without /spark.
AUTO_KEYWORDS = re.compile(r'分析|诊断|排查|卡顿|掉tps|analy(?:[sz]e|sis)', re.I)
# `compare` as a word of its own: not part of "comparison" or a report id. \b would not work, because CJK
# characters count as word characters ("调整后compare").
COMPARE = re.compile(r'(?<![A-Za-z0-9_])compare(?![A-Za-z0-9_])', re.I)
HISTORY_ERRORS = (sqlite3.Error, OSError, ValueError, KeyError, TypeError, AttributeError)


def report_files(chain):
    """.sparkprofile attachments in a message chain, one per file name."""
    files = {}
    for component in chain or []:
        if isinstance(component, File) and is_report_file(component.name):
            files.setdefault(component.name, component)
    return list(files.values())


def quoted_report_files(chain):
    """.sparkprofile attachments in the messages this one replies to."""
    return report_files([component for quoted in chain if isinstance(quoted, Reply) for component in quoted.chain or []])


def attachment(component):
    """(local path, download URL) of a File; adapters put a URL in either field, or a path, or both."""
    local, url = str(component.file_ or ''), str(component.url or '')
    if local.startswith(('https://', 'http://')):
        local, url = '', url or local
    return local, url


@register('astrbot_plugin_spark', 'Evergarden_Roxy', '隔离解析Spark报告并使用专用模型分析', '1.0.6')
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
        self.guide = Path(__file__).with_name('diagnosis_guide.md').read_text(encoding='utf-8')
        self.default_reply_prompt = Path(__file__).with_name('reply_prompt.txt').read_text(encoding='utf-8')
        self.active = set()
        self.inflight_reports = set()
        self.tasks = set()
        self.sessions = set()
        self.gate = asyncio.Semaphore(1)

    async def initialize(self):
        # AstrBot awaits this inside its plugin-load try, so nothing may escape. Plugins load one after
        # another: the purge runs off the event loop and waits at most 1 s for a lock. Only this plugin
        # writes the file, so a longer lock is external; skip and retry on the next load.
        try:
            await asyncio.to_thread(self.history.purge, 1)
        except Exception as exc:
            logger.warning('Spark history purge failed: %s', type(exc).__name__)
        # A saved reply prompt equal to an old shipped default was never customised: switch it to the current
        # default and save, so the config page shows the text actually used. Analysis applies the same rule
        # even if saving fails.
        try:
            if is_legacy_reply(self.config.get('analysis_prompt')):
                self.config['analysis_prompt'] = self.default_reply_prompt
                save = getattr(self.config, 'save_config', None)
                if save:
                    save()
                logger.info('Spark replaced the previous default reply prompt with the current one')
        except Exception as exc:
            logger.warning('Spark reply prompt update failed: %s', type(exc).__name__)

    def owner(self, event):
        return hashlib.sha256((event.unified_msg_origin+'\0'+event.get_sender_id()).encode()).hexdigest()

    def allowed(self, event):
        allow = self.config.get('allowed_origins', [])
        if allow and event.unified_msg_origin not in allow:
            return False
        mode = self.config.get('access_mode', 'admin_only')
        admin = event.is_admin()
        if mode == 'admin_only':
            return admin
        if mode == 'admin_and_whitelist':
            return admin or str(event.get_sender_id()) in {str(x) for x in self.config.get('user_whitelist', [])}
        return mode == 'all'

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def auto_analyze(self, event: AstrMessageEvent):
        text = event.message_str
        if re.match(r'spark(?:\s|$)', text.lstrip('/')):
            return
        if not self.config.get('auto_analyze', True):
            return
        # A .sparkprofile attachment, in the message or in the one it replies to, is a request on its own: QQ cannot
        # send text with a file, so a question about it arrives as a reply. A link needs a keyword, and wins over a
        # quoted file.
        chain, link = event.get_messages(), LINK.search(text)
        if not (report_files(chain) or (link and AUTO_KEYWORDS.search(text)) or (not link and quoted_report_files(chain))):
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
            logger.info('Spark access denied; mode=%s', self.config.get('access_mode', 'admin_only'))
            yield event.plain_result('此会话或用户没有Spark分析权限。')
            event.stop_event()
            return
        if text == 'forget':
            try:
                count = await asyncio.to_thread(self.history.delete, self.owner(event))
            except (sqlite3.Error, OSError) as exc:
                # The delete is one transaction, so nothing was removed. A lock clears on retry; a damaged
                # or unreadable file needs an admin.
                logger.warning('Spark history forget failed: %s', type(exc).__name__)
                busy = isinstance(exc, sqlite3.OperationalError) and 'locked' in str(exc)
                yield event.plain_result('历史数据库正忙，记录未清除，请稍后再试。' if busy else
                                         '历史数据库无法读取，记录未清除，请联系管理员检查历史数据库文件。')
            else:
                yield event.plain_result(f'已清除当前发送者在此会话的历史记录：{count}条。')
            event.stop_event()
            return
        async for result in self.handle(event, text):
            yield result

    @filter.llm_tool(name='spark_analyze')
    async def analyze_tool(self, event: AstrMessageEvent, report_url: str = '', observation: str = ''):
        '''把Spark性能报告交给插件分析。插件在后台下载报告并用专用分析模型分析，进度和结论会直接发给用户；本工具没有返回内容，调用后不要重复调用，也不要自己推测分析结论。

        Args:
            report_url(string): 官方报告链接，格式为 https://spark.lucko.me/报告ID。用户发送或引用的是 .sparkprofile 文件时留空，插件直接读取该文件。
            observation(string): 用户描述的现象，可选。可附 server=服务器标签、problem=问题标签；需要与上次结果对比时加 compare。
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
        # No return value: AstrBot then ends the main agent's turn without another model call,
        # so the plugin's own messages are the whole reply.

    async def handle(self, event, text, *, stop=True):
        # stop=False for the tool path: the event belongs to the main chat agent, and
        # stopping it aborts the main model's reply.
        sources = self.sources(event, text)
        owner = self.owner(event)
        providers = self.providers()
        refusal = self.refusal(event, sources, owner, providers, stop)
        if refusal is not None:
            if refusal:
                yield event.plain_result(refusal)
            if stop:
                event.stop_event()
            return
        kind, source = sources[0]
        report = self.report_key(sources[0])
        self.active.add(owner)
        self.inflight_reports.add(report)
        task = asyncio.current_task()
        self.tasks.add(task)
        session = None
        trace = owner[:8]
        logger.info('Spark [%s] accepted; report=%s', trace, report)
        try:
            async with self.gate:
                acknowledgement = str(self.config.get('acknowledgement_text', '已收到Spark报告，开始分析流程。')).strip()
                if acknowledgement:
                    yield event.plain_result(acknowledgement[:1000])
                session = self.new_session()
                self.sessions.add(session)
                logger.info('Spark [%s] download/parse start', trace)
                if kind == 'link':
                    overview = await session.load('https://spark.lucko.me/'+source)
                else:
                    overview = await session.load_file(*attachment(source))
                logger.info('Spark [%s] download/parse success; threads=%s', trace, len(overview.get('threads', [])))
                tags = self.tags(text, overview)
                comparison, previous, skipped = None, None, None
                if COMPARE.search(text):
                    comparison, previous, skipped = await self.comparison(owner, overview, tags, trace)
                prompt = user_payload(text, overview, comparison, previous, skipped)
                instructions = system_prompt(self.policy, self.guide,
                                             reply_requirements(self.config.get('analysis_prompt', ''), self.default_reply_prompt))
                for attempt, provider_id in enumerate(providers):
                    logger.info('Spark [%s] model start; attempt=%s/%s provider=%s', trace, attempt+1, len(providers), provider_id)
                    result, error, stuck = await self.ask(event, provider_id, prompt, instructions, session, owner, trace)
                    if error is None:
                        break
                    logger.warning('Spark provider attempt %s failed: %s', attempt+1, type(error).__name__)
                    # A model task that ignored cancellation could still send a request: no fallback then.
                    if stuck or attempt+1 == len(providers):
                        raise error
                    yield event.plain_result(f'分析模型第{attempt+1}次尝试失败（{type(error).__name__}），使用下一个模型继续尝试。')
                history_id = await self.save_history(owner, tags, overview, result, trace)
                logger.info('Spark [%s] history saved=%s; sending final result', trace, history_id or False)
                yield event.plain_result(plain_text(result)+self.footnotes(skipped, history_id, tags['typed']))
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
            self.inflight_reports.discard(report)
            self.tasks.discard(task)
            if stop:
                event.stop_event()
            logger.info('Spark [%s] cleanup complete', trace)

    def providers(self):
        """The analysis model, then the fallbacks in order; empty when no analysis model is set."""
        first = str(self.config.get('analysis_provider_id', '')).strip()
        if not first:
            return []
        return list(dict.fromkeys([first] + [
            str(item['provider_id']).strip()
            for item in self.config.get('fallback_providers', [])
            if isinstance(item, dict) and item.get('provider_id')]))

    @staticmethod
    def sources(event, text):
        """The reports a request names: ('link', report id) and ('file', File) entries.

        A file in the quoted message counts only when the message itself names no report: QQ cannot send text
        with a file, so a question about it is sent as a reply to the file message.
        """
        chain = event.get_messages()
        found = [('link', key) for key in dict.fromkeys(LINK.findall(text))]
        found += [('file', component) for component in report_files(chain)]
        return found or [('file', component) for component in quoted_report_files(chain)]

    @staticmethod
    def report_key(source):
        """In-flight key and log label: the report id, or the file name (user text, so shortened)."""
        kind, value = source
        return value if kind == 'link' else 'file:'+str(value.name)[:80]

    def refusal(self, event, sources, owner, providers, stop):
        """Why this request cannot start: a message, '' to refuse silently, or None to go ahead."""
        if not self.allowed(event):
            logger.info('Spark access denied; mode=%s', self.config.get('access_mode', 'admin_only'))
            return '此会话或用户没有Spark分析权限。'
        if len(sources) != 1:
            return ('请提供一个 https://spark.lucko.me/报告ID ，或一个 .sparkprofile 文件（不能给文件附文字时，回复文件消息发送 /spark）。'
                    '可附 server=服务器标签 problem=问题标签；比较时添加 compare。')
        if not providers:
            return '请先在插件配置中选择“分析模型”；不会回落到主聊天模型。'
        if self.report_key(sources[0]) in self.inflight_reports:
            logger.info('Spark duplicate in-flight report skipped')
            # The tool path gets no main-model reply, so silence would leave the user with nothing.
            return '' if stop else '这份Spark报告正在分析中，完成后会直接发送结果。'
        if owner in self.active or self.gate.locked():
            return '已有Spark分析正在执行，请稍后再试。'
        return None

    def new_session(self):
        download_timeout = max(15, min(900, int(self.config.get('download_timeout_seconds', 120))))
        parse_timeout = max(15, min(300, int(self.config.get('parse_timeout_seconds', 90))))
        proxy = str(self.config.get('download_proxy_url', '')).strip() if self.config.get('download_proxy_enabled', False) else None
        return ReportSession(self.root, download_timeout=download_timeout, parse_timeout=parse_timeout, cache=self.cache, proxy=proxy)

    @staticmethod
    def tags(text, overview):
        """History tags: the typed server=/problem=, otherwise derived from the report and the wording."""
        server_match = re.search(r'(?:^|\s)server=([^\s]{1,80})', text)
        problem_match = re.search(r'(?:^|\s)problem=([^\s]{1,80})', text)
        return {'server': server_match.group(1) if server_match else overview.get('runtime', {}).get('server_hint', {}).get('tag', ''),
                'problem': problem_match.group(1) if problem_match else (
                    'JVM与GC' if re.search(r'JVM|GC|启动参数|堆内存', text, re.I) else '性能分析'),
                'problem_typed': bool(problem_match),
                # compare finds the previous record by tags, so the result note repeats the ones the user typed.
                'typed': ' '.join(m.group(0).strip() for m in (server_match, problem_match) if m)}

    async def comparison(self, owner, overview, tags, trace):
        """(comparison, previous result, None), or (None, None, why no comparison was made)."""
        if not self.history.enabled:
            return None, None, '未开启“保存分析历史”'
        if not tags['server']:
            return None, None, '报告缺少系统信息，无法自动识别服务器，请加上 server=服务器标签'
        # Comparison is optional context; a broken history store must not fail the analysis.
        try:
            # A derived problem tag depends on the wording, so only a typed problem= has to match.
            rows = await asyncio.to_thread(self.history.list, owner, tags['server'],
                                           tags['problem'] if tags['problem_typed'] else '')
            if rows:
                return compare(rows[0]['overview'], overview), rows[0]['result'], None
        except HISTORY_ERRORS as exc:
            logger.warning('Spark [%s] history comparison skipped: %s', trace, type(exc).__name__)
            return None, None, '历史记录读取失败'
        return None, None, ('没有找到可对比的记录：需要同一发送者在此会话中、使用同一服务器标签'
                            + ('和问题标签' if tags['problem_typed'] else '') + '，30 天内保存过分析结果')

    def query_tool(self, session, owner, trace, calls):
        """spark_query bound to this report and requester; calls[0] counts uses, at most 8 per model attempt."""
        async def query(tool_event, view='hotspots', thread=0, node=-1, window=None, search='', offset=0):
            if self.owner(tool_event) != owner:
                return json.dumps({'error': '会话不匹配'})
            calls[0] += 1
            if calls[0] > 8:
                return json.dumps({'error': '查询预算已耗尽，请依据已有证据总结'})
            logger.debug('Spark [%s] query #%s view=%s thread=%s node=%s window=%s offset=%s', trace, calls[0], view, thread, node, window, offset)
            answer = await session.query(view=view, thread=thread, node=node, window=window, search=search, offset=offset)
            logger.debug('Spark [%s] query complete; rows=%s error=%s', trace, len(answer.get('rows', [])), bool(answer.get('error')))
            return json.dumps(answer, ensure_ascii=False)

        return FunctionTool(name='spark_query', description='只读查询本次报告的调用树，用于补充证据。triage、evidence_pack 和 world 已足够回答时不要调用。每页最多 20 行，next_offset 不为空表示还有下一页。',
            parameters={'type':'object','properties':{
                'view':{'type':'string','enum':['hotspots','children','callers','search'],
                        'description':'hotspots：按自身耗时（self）列出热点；children：按含子调用耗时（inclusive）列出 node 的子节点；callers：还原 node 到线程根的调用路径；search：按方法名字面搜索'},
                'thread':{'type':'integer','description':'线程 ID，取自 overview.threads 或 evidence_pack；服务端主线程通常名为 Server thread'},
                'node':{'type':'integer','description':'节点 ID，取自之前的结果；children 时 -1 表示线程根'},
                'window':{'type':'integer','description':'时间窗口 ID，取自 overview.windows；不填表示全部窗口'},
                'search':{'type':'string','maxLength':120,'description':'view=search 时要查找的方法名片段（字面匹配）'},
                'offset':{'type':'integer','minimum':0,'maximum':1000,'description':'分页起点，取上一页的 next_offset'}},
                'required':['view','thread']}, handler=query)

    async def ask(self, event, provider_id, prompt, instructions, session, owner, trace):
        """One analysis-model attempt: (reply, None, False) on success, otherwise (None, error, stuck).
        stuck is True when the model task outlived cancellation and could still send a request."""
        calls = [0]
        remaining = set()
        try:
            model_task = asyncio.create_task(self.context.tool_loop_agent(
                event=event, chat_provider_id=provider_id, prompt=prompt,
                system_prompt=instructions, tools=ToolSet([self.query_tool(session, owner, trace, calls)]), max_steps=10,
                tool_call_timeout=35))
            try:
                for tick in range(10):
                    done, _ = await asyncio.wait({model_task}, timeout=30)
                    if done:
                        break
                    logger.info('Spark [%s] model waiting; provider=%s elapsed=%ss queries=%s', trace, provider_id, (tick+1)*30, calls[0])
                if not model_task.done():
                    raise TimeoutError('分析模型超过300秒')
                response = await model_task
            finally:
                remaining = await cancel_bounded({model_task})
                self.background_tasks.update(remaining)
                for stuck in remaining:
                    stuck.add_done_callback(self.background_tasks.discard)
            result = response.completion_text
            if not result or not result.strip():
                raise ValueError('分析模型没有返回文字结论')
            # The model name the provider reports serving, when it returns one (OpenAI-style responses do).
            # Lets the operator spot a provider silently routing to a weaker model.
            served = getattr(getattr(response, 'raw_completion', None), 'model', None)
            logger.info('Spark [%s] model success; provider=%s served_model=%s queries=%s',
                        trace, provider_id, str(served)[:80] if served else 'unknown', calls[0])
            return result, None, False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return None, exc, bool(remaining)

    async def save_history(self, owner, tags, overview, result, trace):
        try:
            return await asyncio.to_thread(self.history.save, owner, tags['server'], tags['problem'], overview, result)
        except (sqlite3.Error, OSError) as exc:
            # The model already answered; a failed history write must not discard the result.
            logger.warning('Spark [%s] history save failed: %s', trace, type(exc).__name__)
            return None

    @staticmethod
    def footnotes(skipped, history_id, typed):
        notes = []
        if skipped:
            notes.append(f'未进行历史对比：{skipped}。')
        if history_id:
            notes.append(f'已保存本次结果；之后再次分析时加上 compare{"，并带上 "+typed if typed else ""}，可与本次对比。')
        return ''.join(f'\n（{note}）' for note in notes)

    async def terminate(self):
        remaining = await cancel_bounded(self.background_tasks | self.tasks)
        if remaining:
            logger.warning('Spark terminate: %s tasks did not stop within 5s', len(remaining))
        for session in list(self.sessions):
            try:
                await session.close()
            except Exception as exc:
                logger.warning('Spark terminate: session close failed: %s', type(exc).__name__)
