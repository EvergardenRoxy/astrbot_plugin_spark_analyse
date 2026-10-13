# 独立审查报告：astrbot_plugin_spark（Observable 支持分支）

- 报告 ID：`01Wcb9buwKLy6cYGYqV8BvCj`
- 审查对象：分支 `ccr-422bc249-ju8f1e`，提交 `a8853f4`（"Address independent review: keyword gate with files, hybrid servers, companion drop, partial failure, prompt fixes"）
- 审查日期：2026-10-13
- 审查方式：通读全部源码（`main.py`、`spark_core/*`、配置 schema、提示词、`tools/*`、CI），对可疑点在临时目录编写测试实际复现；查阅 AstrBot 4.28.2 源码确认适配器和 `tool_loop_agent` 的行为。**没有修改任何插件代码。**
- 测试基线：在 Python 3.13 上按 `requirements.txt` 安装依赖（aiohttp 3.14.5、protobuf 6.33.6、yarl 1.25.1），`python -m unittest discover -s tests`：**195 项全部通过**。

README“已知问题”和 `handoff.md` §20/§22 已登记的问题（K1–K3 等）不重复列为新发现，只注明仍未修复的项。标“已复现”的条目附有复现方法。

---

## 总览

| 编号 | 类别 | 严重度 | 标题 | 状态 |
|---|---|---|---|---|
| S1 | 安全 | 中 | 模型回复无长度上限，URL 原样转发 | 已复现，**已修复** |
| S2 | 安全 / 滥用 | 中（`access_mode=all` 时） | 无按用户的限流与配额，单一全局 gate 可被独占 | 代码确认，**部分修复**（短队列；冷却与次数上限按维护者决定暂不设置） |
| S3 | 隐私 | 低～中 | JVM 启动参数全文发给第三方模型并存入历史库 | 代码确认 |
| S4 | 安全（纵深防御） | 低 | 解析进程继承宿主全部环境变量，只有内存上限 | 代码确认 |
| S5 | 安全 | 低 | 适配器提供的本地路径与下载 URL 完全信任 | 代码 + AstrBot 源码确认 |
| S6 | 安全 / 资源 | 低 | `spark_query` 的 offset 未按 schema 限制 | 代码确认，**已修复** |
| S7 | 安全 / 日志 | 低 | 报告 ID 与未过滤的文件名写入 info 日志 | 代码确认 |
| S8 | 隐私 | 低 | 历史库 owner 哈希不加盐，可穷举还原 | 代码确认 |
| B1 | 逻辑错误 | 中 | 本条消息的 Observable 下载失败时被旧报告顶替 | 已复现，**已修复** |
| B2 | 逻辑错误 | 低 | 只有 Observable 时，模型失败提示写成“Spark分析未完成” | 已复现 |
| B3 | 健壮性 | 中（视环境） | Spark 下载未固定 `Accept-Encoding`，可能收到 br/zstd | 代码 + aiohttp 源码确认，**已修复** |
| B4 | 健壮性 | 中 | K2 仍未修：`setrlimit` 在 `try` 之外 | **已修复** |
| B5 | 资源 | 低 | 合并分析时两个解析进程同时存在 | 代码确认 |
| E1–E5 | 工程 | — | 结构、常量、测试缺口、CI 与依赖、发布准备 | — |

**修复进展**：B1、B3、S1、S6 已在同一分支修复并补测试，详见 CHANGELOG Unreleased 与 `handoff.md` §23；B4 已修复，S2 已加短队列（不设冷却和上限），见 `handoff.md` §24；本报告其余各节保留审查时的原文。

建议优先处理：**B1、B3、S1、S6、B4**（各几行代码，都能直接写测试）；其次 **S2**（若有人使用 `all` 模式）和 **S3**（脱敏）。

---

## 一、安全问题

### S1（中）模型回复无长度上限，URL 原样转发 —— 已复现

**位置**：`main.py:288`、`spark_core/output.py:616`

最终回复由 `plain_text(result)` 加脚注直接发出，没有长度上限。`plain_text` 把 Markdown 链接 `[文字](https://...)` 转成 `文字（https://...）`，链接保持可点击。

**复现**（临时测试，基于 `tests/test_plugin.py` 的替身）：

- 让替身模型返回 `'刷屏'*50000`：最终发送的回复长度为 **100000** 字符。
- `plain_text('点击 [官方修复工具](https://evil.example/x) 下载')` → `点击 官方修复工具（https://evil.example/x） 下载`。

**影响**：K1 的描述是“构造的报告可能让结论出错”。实际可达的影响更大：构造的报告（方法名、平台字段、Observable 的类型名和模组列表都会进入提示词）可以诱导模型输出长篇刷屏或钓鱼链接，由机器人以自己的身份发到群里。历史记录会截断到 24000 字（`history.py:175`），聊天发送却不截断。

**建议**：

1. 发送前截断到约 3000 字（可配置），超出时注明“回复过长已截断”。
2. 对不在白名单（`spark.lucko.me`、`observable.tas.sh`、`spark-usercontent.lucko.me` 等）内的 URL：删除，或改写成不可点击的形式并标注“不可信链接”。
3. 在测试里固定这两条行为。

### S2（中，`access_mode=all` 时）无按用户的限流与配额

**位置**：`main.py:70`（`self.gate = asyncio.Semaphore(1)`）、`main.py:402`（`self.gate.locked()` 时直接拒绝）

**问题**：

- 全局只有一个 gate。一个用户连续发报告（或在 tool 路径里反复让主模型调用 `spark_analyze`）就能一直占着它，其他人都会收到“已有Spark分析正在执行”。
- 每次分析最多 `max_steps=10` 步、300 秒，失败时还会依次换备用模型，消耗模型额度。
- `.sparkprofile` 文件不进缓存（`session.py:81-91`），每次都重新拉取，最多 128 MiB。

**建议**：按 owner 设冷却时间（例如 60 秒）和每日次数上限，可配置；考虑把“拒绝”改为长度很短的队列；在 README 说明 `all` 模式的成本风险。

### S3（低～中）JVM 启动参数的隐私

**位置**：

- `spark_core/briefing.py:678`、`:784`：用户问题匹配 `JVM_QUESTION`（含 JVM、GC、堆内存等）时，`vm_args` / `jvm_flags` **全文**交给分析模型；否则只保留关键参数。
- `spark_core/history.py:175`：历史库保存的是 `json.dumps(overview)`，即**未摘要**的完整 overview（包括完整 `vm_args`），保留 30 天。

**问题**：`-D` 系统属性里常见数据库密码、令牌、授权码等。`.sparkprofile` 是私有文件（没有上传到 spark），现在它的内容摘要会发给第三方模型服务商，README 没有说明数据去向。

**建议**：

1. 对 `-D<name>=<value>` 中 name 匹配 `password|passwd|pwd|token|secret|key|credential` 的项，把值替换为 `***`，在发给模型和写入历史前都执行。
2. 历史库只保存摘要后的 overview（`compare()` 只用到 `platform`、`sampling`、`health` 三节）。
3. README 补一句：“报告内容（含 `.sparkprofile` 文件）会发送给所配置的分析模型服务商”。

### S4（低，纵深防御）解析进程继承宿主全部环境变量

**位置**：`spark_core/session.py:95-98`，`create_subprocess_exec` 未传 `env`；`spark_core/worker.py:470-472` 只设置 `RLIMIT_AS`。

**问题**：解析进程处理的是不可信的 protobuf / JSON，却能看到 AstrBot 进程的全部环境变量（可能含 API 密钥），文件权限也和 AstrBot 相同。插件注册描述写的是“隔离解析”，实际只是进程与内存隔离，不是沙箱。

**建议**：

1. 只传最小环境：`PATH`、`PYTHONPATH`（handoff 指出依赖可能装在 `data/site-packages`，需要保留可导入性），Windows 上加 `SYSTEMROOT`。
2. 追加 `RLIMIT_CPU`（略高于 `parse_timeout`）与 `RLIMIT_FSIZE`（例如 256 MiB）。
3. 描述中注明“资源隔离，非沙箱”。

### S5（低）适配器提供的本地路径与下载 URL 完全信任

**位置**：`main.py:42-47`（`attachment()`）、`spark_core/transport.py:146-200`

**本地路径**：

- 接受任意相对 / 绝对路径与 `file://` URI，`os.path.isfile` 与 `open` 都会跟随符号链接。
- AstrBot 4.28.2 的 Satori 适配器直接用消息元素的 `src` 构造 `File(name=name, file=src)`（`satori_adapter.py:671-675`），路径是否可被外部影响取决于上游实现。
- 文件内容不会回显（解析失败只返回异常类型名），所以影响很小；但可以被用来让插件读取并分析主机上另一个位置的 `.sparkprofile`。

**下载 URL**（`_fetch`）：会跟随跳转，`trust_env=True`，非 200 时把 HTTP 状态码回显到聊天，相当于一个很弱的盲 SSRF 探测点。URL 来自适配器而不是消息文本，风险低。

**建议**：本地路径只接受位于 AstrBot 临时目录 / 数据目录下的普通文件（`Path.resolve()` 后检查前缀，不跟随符号链接）；`_fetch` 限制跳转次数（`max_redirects`），状态码只记日志、不回显。

### S6（低）`spark_query` 的 offset 未按 schema 限制

**位置**：`spark_core/profile.py:491`（`0 <= offset <= len(t.children)`）、`:509`（`heapq.nlargest(offset + 21, ...)`）；schema 在 `main.py:464` 声明 `maximum: 1000`。

**问题**：schema 的上限只是给模型的提示，代码实际允许到节点数（最多 100 万）。被报告内容诱导的模型可以传约 100 万的 offset，hotspots 视图会为每个节点生成行字典并全部保留，逼近 1.5 GiB 内存上限或撞上 30 秒查询超时。后果限于本次分析的查询失败、解析进程被杀。

**建议**：在 `Profile.query` 里强制 `offset <= 1000`，并补测试。顺带：`thread=True` 会被当作线程 1（`bool` 是 `int`），可一并改为 `type(thread) is int`。

### S7（低）日志

**位置**：`main.py:205`（`report=...`，文件名截断到 80 字符但不过滤控制字符）、`spark_core/session.py:52`、`:69`

**问题**：

- spark / Observable 的报告 ID 实际就是访问凭证（知道链接即可查看），以 info 级别写入日志，AstrBot WebUI 可以看到。
- 用户发送的文件名可以含 `\r\n`，用来伪造日志行。

**建议**：报告 ID 降到 debug 级别或只记前几位；文件名去掉控制字符后再写日志。

### S8（低）历史库 owner 哈希不加盐

**位置**：`main.py:96-97`：`sha256(unified_msg_origin + '\0' + sender_id)`

**问题**：会话标识格式固定，QQ 号空间很小，穷举即可还原，这只是假名化。

**建议**：首次运行时在数据目录生成随机盐并参与哈希。注意这会让已有历史记录与新 owner 对不上，需要一次迁移，或者在 CHANGELOG 中说明旧记录失效。

---

## 二、逻辑错误与健壮性

### B1（中）本条消息的 Observable 下载失败时被旧报告顶替 —— 已复现

**位置**：`main.py:228-256`

**原因**：同一条消息里有 Spark 和 Observable，Observable 加载失败时，`main.py:234` 把 `obs_source` 置为 `None`。随后 `main.py:243` 的 `if spark_source and obs_source` 不成立，流程进入跨消息配对分支，`recent_companion('observable', owner)` 取出了同一用户几分钟前分析过的 **另一份** Observable 报告。

**复现**：

1. 用户先发 `分析 https://observable.tas.sh/p/SyntheticObs01`（被记住）。
2. 再发 `分析 https://spark.lucko.me/SyntheticReport001 https://observable.tas.sh/p/OtherObsB`，替身让 `load_observable` 抛出 `ProfileError('报告下载失败 HTTP 500')`。
3. 结果：模型收到 `report_kind = spark+observable`，`pairing.how = recent_message`；回复末尾同时出现两条互相矛盾的脚注：
   - `（Observable 报告未能读取（报告下载失败 HTTP 500），本次只分析 Spark 报告）`
   - `（已结合你约 1 分钟前发送的 Observable 报告一并分析；如果不是同一台服务器，请忽略相关内容）`

**修复**：本次请求里 Observable 加载失败（`missing` 非空）时，跳过对 Observable 的 `recent_companion` 查找；补一条对应测试。

### B2（低）只有 Observable 时，模型失败提示写成“Spark分析未完成” —— 已复现

**位置**：`main.py:236`

Observable 加载成功后无条件执行 `label = 'Spark'`。之后如果模型调用失败，提示是 `Spark分析未完成：RuntimeError。…`，而用户只发了 Observable 报告。

**修复**：`label` 只在 Spark 来源存在时设回 `'Spark'`；或者加载完成后改用中性的“报告分析未完成”。

### B3（中，视环境）Spark 下载未固定 `Accept-Encoding`

**位置**：`spark_core/transport.py:95`（只有 `gzip_only=True` 的 Observable 请求设置 `Accept-Encoding: gzip`）、`:104-106`（只接受 identity / gzip）

**问题**：Spark 请求使用 aiohttp 默认请求头。aiohttp 3.14 在环境里装有 Brotli 或 zstd 时，默认 `Accept-Encoding` 会包含 `br` / `zstd`（`aiohttp/client_reqrep.py:103-105`）；而插件关闭了 `auto_decompress`，只会解 gzip。服务器或 CDN 一旦回 br，下载就会失败并提示“不支持的HTTP压缩格式”。这取决于部署环境是否装了这些库（其他插件常会带进来），我没有对线上服务器实测。

**修复**：一行改动，Spark 请求同样显式发送 `Accept-Encoding: gzip`；补一条断言请求头的测试。

### B4（中）K2 仍未修：`setrlimit` 在 `try` 之外

**位置**：`spark_core/worker.py:470-472`

环境已有更低的地址空间上限（systemd `LimitAS=`、`ulimit -v`）时，`setrlimit` 抛出 `ValueError`，解析进程在写出 `ready.json` 之前退出，每次分析都只显示“解析worker异常退出”。详见 `handoff.md` §20 K2。

**建议**：采用 handoff 的方案 (a)：捕获 `(ValueError, OSError)`，降为不超过现有硬上限的值或不设上限继续解析，并在 `ready.json` 里带上“内存上限未生效”的标记，由宿主写日志。

### B5（低）合并分析时两个解析进程同时存在

**位置**：`main.py:212-225`

先加载 Spark，它的解析进程要留给 `spark_query` 使用；接着再为 Observable 启动一个解析进程。两者各自最高 1.5 GiB，峰值约 3 GiB。`handoff.md` §22 已记录构造的 45 MB Observable JSON 可达约 1.14 GB RSS。

**修复**：先加载 Observable（其会话在解析后立即关闭，`main.py:315-330`），再加载 Spark。顺序调整后，B1 的部分失败语义需要相应调整（Spark 失败时整个请求失败，与现在一致）。

---

## 三、工程与可维护性

### E1 `handle()` 过长

`main.py:178-313` 约 135 行，下载、部分失败、跨消息配对、历史对比、模型重试和清理都在一个生成器里。B1、B2 都出在配对与 label 的交织处。建议把“来源加载结果 + 记忆 → (overview, observable, pairing, missing)”抽成纯函数 `resolve_pairing(...)`，单独做表驱动测试。

### E2 体积上限散落多处

- 128 MiB：`transport.py:41`、`:137`、`:146`，`worker.py:479`，`cache.py:555`、`:567`。
- 48 MiB：`transport.py:31`，`observable.py:219`，`worker.py:477-478`。
- 提示语也有多份，措辞不一致。

建议集中为 `spark_core/limits.py` 中的常量和提示语。另外：`profile.py:507`、`:523` 在函数内重复 `import heapq` / `import itertools`；`session.py:9-11` 把 `LoadTimeout` 定义夹在 import 中间。

### E3 K3 的测试缺口仍在

在 `tests/` 中搜索不到以下保护：

| 约束 | 位置 |
|---|---|
| 查询者身份校验（`会话不匹配`） | `main.py:446` |
| 每次尝试最多 8 次查询 | `main.py:448-450` |
| 100 万调用节点上限 | `profile.py:266` |
| 宿主退出（`getppid` 变化）后解析进程自行结束 | `worker.py:495` |
| `access_mode` 为未知值时拒绝 | `main.py:109` |

每项补一个测试即可，不需要改插件代码。本报告的 B1、B2、S1、S6 修复时也应各带一条测试。

### E4 CI 与依赖

- `.github/workflows/tests.yml` 的 actions 按 tag（`@v4`、`@v5`）引用，建议固定到 commit SHA。
- 没有 lint / 格式检查（如 ruff）；没有在依赖下限版本（aiohttp 3.10、protobuf 6.31.1）上跑测试。
- `yarl` 在 `transport.py:11` 直接导入，但没有写进 `requirements.txt`（目前靠 aiohttp 间接带入）。
- `protobuf>=6.31.1,<7`：生成的 `spark_sampler_pb2` 在 protobuf 7.36.1 下可以正常导入（仅验证了导入，未跑完整测试）。如果 AstrBot 环境里其他插件需要 protobuf 7，这个上限会造成依赖冲突，建议在 CI 中加一组 protobuf 7 测试后再决定是否放宽。
- `tools/build_schema.py` 从 GitHub 下载 proto 文件时没有校验哈希（仅开发用，风险低）。

### E5 发布前

- `metadata.yaml` 与 `@register`（`main.py:50`）的版本仍是 1.0.7，而 CHANGELOG Unreleased 已加入 Observable 支持。
- 默认确认语“已收到Spark报告，开始分析流程。”（`_conf_schema.json` 与 `main.py:208`）对 Observable 报告不准确。
- `handoff.md` 已有 1254 行，建议把已关闭的轮次归档，主文件只保留当前状态与未决问题。

---

## 四、已确认没有问题的点

以下是审查中重点核对过、结论为“没有问题”的地方，供后续审查参考：

- **下载目标固定**：Spark / Observable 的报告 ID 字符集为 `[A-Za-z0-9]` / `[A-Za-z0-9_-]`，拼接到固定主机的 URL，不可能出现路径穿越；`allow_redirects=False`，`trust_env=False`。
- **解压炸弹**：gzip 解码用 `max_length` 逐块限制，并检查 `unconsumed_tail` 与尾随数据；附件下载在 aiohttp 自动解压后按解码字节计数。
- **代理地址**：`validate_proxy` 拒绝非 HTTP(S)、带路径或参数的地址；日志与聊天里都不出现代理 URL。
- **工具面**：只有 Observable 报告时传 `tools=None`；AstrBot 4.28.2 的 `tool_loop_agent` 把它原样放进 `ProviderRequest.func_tool`，不会回落到全局工具集。
- **解析进程通信**：请求 / 响应通过临时文件 + `replace()` 原子写入；会话目录由 `mkdtemp` 创建（0700）；关闭时总会删除目录。
- **Observable 解析**：`NaN` / `Infinity` / 布尔值被 `_number` 拒绝；超长整数触发的 `ValueError` 被捕获；递归深度有上限并捕获 `RecursionError`；所有输出列表都有上限，整体证据包不超过 128 KiB。
- **正则**：`LINK`、`OBS_LINK`、`AUTO_KEYWORDS`、`COMPARE`、`normalize_type` 等在有界输入上没有灾难性回溯。
- **Python 版本**：AstrBot 4.28.2 要求 Python ≥ 3.12，`asyncio.TimeoutError` 与内置 `TimeoutError` 已统一，`except TimeoutError` 写法没有问题。

---

## 附：复现所用临时测试

放在仓库之外运行（`tests/` 目录下，`PYTHONPATH` 包含临时目录、`tests/` 与仓库根目录），继承 `tests/test_plugin.py` 的 `PluginTests` 替身：

```python
import types, json
from unittest.mock import patch
import test_plugin as tp

class Probe(tp.PluginTests):
    async def test_stale_companion_replaces_failed_obs(self):          # B1
        calls = []
        plugin = self.agent_plugin(calls)
        await self.say(plugin, f'分析 {self.OBS}')
        module = self.module
        class Broken(tp.FakeSession):
            async def load_observable(self, url):
                raise module.ProfileError('报告下载失败 HTTP 500')
        ev = tp.Event(); ev.message_str = f'分析 {self.SPARK} https://observable.tas.sh/p/OtherObsB'
        with patch.object(self.module, 'ReportSession', Broken):
            results = [r async for r in plugin.auto_analyze(ev)]
        payload = json.loads(calls[-1]['prompt'])
        print(payload.get('pairing', {}).get('how'), payload.get('report_kind'), results[-1])

    async def test_obs_only_model_failure_label(self):                  # B2
        async def agent(**kw): raise RuntimeError('boom')
        plugin = self.module.SparkPlugin(types.SimpleNamespace(tool_loop_agent=agent), {'analysis_provider_id': 't'})
        print((await self.say(plugin, f'分析 {self.OBS}'))[-1])

    async def test_output_not_capped(self):                             # S1
        async def agent(**kw): return types.SimpleNamespace(completion_text='刷屏'*50000)
        plugin = self.module.SparkPlugin(types.SimpleNamespace(tool_loop_agent=agent), {'analysis_provider_id': 't'})
        print(len((await self.say(plugin, f'分析 {self.SPARK}'))[-1]))

    async def test_plain_text_keeps_links(self):                        # S1
        print(self.module.plain_text('点击 [官方修复工具](https://evil.example/x) 下载'))
```

实际输出：

```
recent_message spark+observable 模型结论
（Observable 报告未能读取（报告下载失败 HTTP 500），本次只分析 Spark 报告）
（已结合你约 1 分钟前发送的 Observable 报告一并分析；如果不是同一台服务器，请忽略相关内容）
（已保存本次结果；之后再次分析时加上 compare，可与本次对比。）
Spark分析未完成：RuntimeError。未回落主LLM、未生成成功审查记录。请检查链接、报告类型、体积或专用模型配置。
100000
点击 官方修复工具（https://evil.example/x） 下载
```
