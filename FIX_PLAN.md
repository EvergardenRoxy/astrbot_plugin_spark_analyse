# 修改与优化方案（依据 AUDIT_FINDINGS.md）

> **执行状态（2026-10-10）**：维护者确认无其他意见，按建议选项实施：D1=A、D2=B、D4=删除、D5=添加 CI；D3（F7）未做任何操作，仍待维护者决定。
> 批次 1~4 全部完成，每批单独提交；测试由 51 项增至 66 项，全部通过（第 7 节预估为 62~64 项，实际多出：X1 增加了 `terminate()` 测试，F3 增加了 `History` 单元测试与“数据库损坏不影响加载”测试）。
> 与方案的偏差：F2 中 worker 记录父进程 pid 的时机由“进入查询循环前”提前到 `run()` 开头，否则宿主在解析期间（最长 90 秒）退出时检测不到。
> 以下为原方案正文，未改动。


```yaml
依据: AUDIT_FINDINGS.md（审查对象 e15f67f / v1.0.2）
方案基线: 5d04db4（main + 审查文档）
基线测试: 51 项全部通过（2026-10-10 复核，Python 3.13，aiohttp/protobuf 按 requirements.txt）
已复核: F6 正则对照表 11/11 符合；tools/release_descriptions.py 确实抛 AssertionError；S1（本方案新增）已复现
未复核: F1 依赖 AstrBot 4.28.2 源码阅读，本方案未在真实 AstrBot 中运行
```

## 0. 总体目标

1. **不破坏主聊天**：工具模式（`spark_analyze`）下插件后台任务不得中止主模型回复、污染对话历史（F1）。
2. **不残留进程与数据**：宿主崩溃后不留下孤儿解析进程和含完整报告的临时目录（F2、X1、X2）。
3. **兑现隐私承诺**：“保留 30 天”在关闭历史功能后仍然成立，用户能清除自己的记录（F3）。
4. **自动分析不打扰无关聊天**：无权限用户不收到拒绝回复、事件不被吞掉（F4）。
5. **取消与容错路径正确**：取消不被吞、任务集合不泄漏，次要功能（历史比较/保存）故障不拖垮已完成的分析（F5、X3、S1）。
6. **输入识别更宽容**：句末英文句号、末尾斜杠的链接能被识别（F6）。
7. **开发工具链可用**：删除失效脚本、版本号单一来源、补 CI（F8）。

## 1. 必须遵守的约束（摘自审查文档 §0、§6）

- 日志只用 `from astrbot.api import logger`，不引入标准库 `logging`（`tests/test_logging_scope.py` 会检查）。
- 聊天中只展示 `ProfileError` / `LoadTimeout` 的原文，其余异常只显示 `type(exc).__name__`。
- 不放宽下载与解析安全限制（官方域名、禁重定向、`trust_env=False`、16 MiB / 128 MiB、节点与深度限制）。
- 不新增运行时依赖；插件启动时不发起网络请求。
- **不改版本号**。所有变更写进 `CHANGELOG.md` 新增的 `## Unreleased` 小节，由维护者发版。
- 不删除、不跳过已有测试；只有 F4 明确允许修改 `test_access_modes_and_all_entry_points` 的断言。
- 不提交 `dist/`、`*.sqlite3`、`*.bin`。F7 不做任何自动处理。

## 2. 实施前需要维护者确认的事项

| # | 事项 | 选项 | 建议 |
|---|---|---|---|
| D1 | F3 关闭历史后的数据处理 | A：加载插件时清理过期记录，`forget` 在关闭状态下也可用；B：仅修改文档 | **A**（改动小，兑现“保留30天”） |
| D2 | F4 自动分析对无权限用户的处理 | B：静默忽略（需改一处现有测试）；A：新增配置“需要@或唤醒词才自动分析”；C：按用户冷却时间 | **先做 B**；A、C 视需求另议 |
| D3 | F7 历史提交中的真实报告 ID | a：保留；b：先确认该报告是否仍在线、暴露哪些信息；c：重写历史（需强推，影响所有克隆） | **b**，确认后再决定是否 c |
| D4 | F8 删除 `tools/release_descriptions.py` | 删除 / 改为与当前 schema 同步 | **删除**（`_conf_schema.json` 已是唯一来源） |
| D5 | F8 新增 GitHub Actions CI | 加 / 不加 | **加**（推送 `.github/workflows/` 可能需要具备 workflow 权限的令牌，推送失败时改由维护者手动添加） |

未确认前，批次 1 中 F4 只实施选项 B 以外的部分（即 F1、F5），批次 3 中 F3 暂缓。

## 3. 分批执行计划

| 批次 | 内容 | 主要文件 | 新增/修改测试 |
|---|---|---|---|
| 1 | F5 → F1 → F4(B) | `main.py` | +3 新增，改 1 处 |
| 2 | F2 → X1 → X2 | `spark_core/worker.py`、`spark_core/session.py`、`main.py` | +3 |
| 3 | F6、F3(A)、X3、S1、X4 | `transport.py`、`history.py`、`main.py`、`_conf_schema.json`、README | +4~5 |
| 4 | F8 各项 | `tools/`、`worker.py`、`.github/workflows/` | +1（可选） |
| — | F7 | 无代码 | 仅向维护者汇报 |

顺序理由：批次 1 的三项都改 `handle()` 及入口函数，合并改动可避免反复冲突；F5 先做，因为它重命名的变量也是 F1 要改的 `finally` 块。批次 2 的三项都围绕 session 生命周期。每批末尾统一更新 `CHANGELOG.md` 的 `## Unreleased`。

---

## 4. 任务明细

### 批次 1：`main.py` 入口与 `handle()`

#### F5 取消路径：变量遮蔽与 `finally` 中的 `raise`

- **目标**：外部取消分析时 `CancelledError` 能正常传播；`self.tasks` 不残留已结束的任务。
- **位置**：`main.py:133`（`task = asyncio.current_task()`）、`:207`（`for task in remaining:`）、`:209`（`raise RuntimeError('模型任务未及时取消…')`）、`:247`（`self.tasks.discard(task)`）。
- **修改方法**：
  1. `:207` 循环变量改名为 `stuck`，不再覆盖外层 `task`。
  2. 删除 `:209` 的 `raise RuntimeError(...)`。下方 `except Exception` 中已有 `if remaining or ...: raise`，“模型任务未取消时停止回退、避免重复请求”的语义不变。
- **可见变化**：模型超时且无法取消时，聊天中的错误类型由 `RuntimeError` 变为原始异常类型（通常是 `TimeoutError`）。
- **测试**：新增审查文档 F5 节中的 `test_cancel_with_uncancellable_model_task`（断言 `runner.cancelled()` 为真、`plugin.tasks == set()`）。

#### F1 工具模式不得 `stop_event()` 主事件

- **目标**：`analyze_tool` 后台任务的任何分支都不调用 `event.stop_event()`；`/spark` 命令与自动分析行为不变。
- **位置**：`handle()` 内 6 处 `event.stop_event()`（`:110`、`:115`、`:120`、`:125`、`:129`、`:248`）；`analyze_tool.run()` `:95`。`spark_command` 中 `:72`、`:77` 不动。
- **修改方法**：
  1. 签名改为 `async def handle(self, event, text, *, stop=True)`。
  2. 6 处统一改为 `if stop: event.stop_event()`。
  3. `analyze_tool.run()` 调用 `self.handle(event, report_url+' '+observation, stop=False)`。
- **测试**：新增审查文档附录 A.1 的两项测试（快速失败、成功路径后 `event.stopped is False`）；现有 `test_scheduler_does_not_stop_after_progress` 必须仍断言 `event.stopped is True`。
- **说明**：`analyze_tool` 已在入口检查权限，`handle()` 中的权限分支在工具路径下几乎不可达，保留作为防御即可。

#### F4（选项 B）自动分析对无权限用户静默

- **目标**：自动分析只对有权限的用户生效；无权限时不回复、不 `stop_event()`，让其他插件和默认 LLM 继续处理消息。显式 `/spark` 与工具仍回复权限提示。
- **位置**：`main.py:53-64` `auto_analyze`。
- **修改方法**：在链接与意图关键词判断之后、调用 `handle()` 之前，加入 `if not self.allowed(event): return`（不 yield，不记 info 日志，避免群聊刷日志；需要时用 `logger.debug`）。放在关键词判断之后，普通聊天不必走权限计算。
- **测试**：修改 `test_access_modes_and_all_entry_points` 末尾的循环——每个入口使用新的 `Event`；`auto_analyze` 结果为 `[]` 且 `event.stopped is False`；`spark_command`、`analyze_tool` 首条结果仍含“权限”。
- **选项 A（待 D2 确认）**：新增布尔配置 `auto_analyze_requires_wake`（默认 `false` 以保持兼容），为真时 `auto_analyze` 在 `not getattr(event, 'is_at_or_wake_command', True)` 时返回。需同步 `_conf_schema.json`（中文 description/hint）与 README “常用设置”。
- **选项 C（待 D2 确认）**：`owner -> 上次开始时间` 字典，同一用户 60 秒内不重复自动触发。

### 批次 2：解析会话生命周期

#### F2 宿主被杀后的孤儿 worker 与临时目录

- **目标**：宿主异常退出后，worker 自动退出；下次加载插件时清理遗留的 `spark-*` 目录（其中有完整解码报告）。
- **修改方法**（三步都做）：
  1. `spark_core/worker.py`：新增 `import os`；在 `while` 前记录 `parent = os.getppid()`；循环体开头加
     `if not directory.exists() or (sys.platform != 'win32' and os.getppid() != parent): return`。
     Windows 上 `getppid()` 不会变化，依赖目录存在性检查兜底。
  2. `main.py`：新增 `import shutil`；`self.root = ...` 之后
     `for stale in self.root.glob('spark-*'): shutil.rmtree(stale, ignore_errors=True)`。
     只匹配 `spark-*` 目录，不碰 `profiles.sqlite3`、`history.sqlite3`。删除目录也会让步骤 1 中仍存活的孤儿 worker 退出。
  3. `ReportSession.close()` 的 `rmtree` 放入 `finally`（与 X1 合并实施）。
- **测试**：附录 A.3 的 `WorkerLifecycleTests.test_worker_exits_when_directory_removed`（加入 `tests/test_load_timeout.py`）与 `test_startup_sweeps_stale_session_dirs`（加入 `PluginTests`）。
- **注意**：
  - `test_worker_hides_messages_that_may_contain_paths` 在没有 `profile.bin` 的目录运行 worker，错误路径必须照旧写 `ready.json`。
  - 热重载时若旧实例 `terminate()` 未能在 5 秒内停下任务，新实例的清扫会让旧 worker 退出、旧查询失败。旧实例本就在卸载，可接受，写入 CHANGELOG 说明即可。

#### X1 `close()` 与 `terminate()` 的清理健壮性

- **目标**：等待进程超时或某个 session 关闭失败时，临时目录仍被删除，其余 session 仍被关闭。
- **位置**：`spark_core/session.py:98-104`；`main.py:251-256`。
- **修改方法**：
  1. `close()`：`kill()` 与 `wait_for(..., 5)` 放进 `try`，`finally` 中 `shutil.rmtree(self.directory, ignore_errors=True)`。异常继续向上抛出，`handle()` 的 `finally` 已会记录“cleanup failed”。`kill()` 可额外捕获 `ProcessLookupError`（进程恰好已退出的竞态）。
  2. `terminate()`：对每个 session 单独 `try/except Exception`，失败时 `logger.warning('Spark terminate: session close failed: %s', type(exc).__name__)`，继续关闭下一个。
- **测试**：新增一项——用 `AsyncMock` 进程使 `wait()` 抛 `TimeoutError`，断言 `close()` 抛出异常后目录已不存在。

#### X2 错误分支的 `ready.json` 非原子写入

- **目标**：读取方不会读到半写入的 JSON。
- **位置**：`spark_core/worker.py:49`（错误分支）。
- **修改方法**：与成功路径一致，先写 `ready.tmp` 再 `replace` 为 `ready.json`。目录已被删除时（F2 场景）写入会失败，保持现有的 `raise` 即可。
- **测试**：现有 `test_worker_hides_messages_that_may_contain_paths` 与真实 worker 测试覆盖即可，无需新增。

### 批次 3：输入识别、历史数据与配置文案

#### F6 `LINK` 正则与 `report_id()` 不一致

- **目标**：`…/ID.`（英文句号结尾）与 `…/ID/` 能被识别；`…/ID/extra`、`…/ID.json` 等仍拒绝。
- **位置**：`spark_core/transport.py:19`。
- **修改方法**：替换为
  `LINK = re.compile(r'https://spark\.lucko\.me/([A-Za-z0-9]{6,64})(?![A-Za-z0-9_-]|/[A-Za-z0-9]|\.[A-Za-z0-9])')`。
  `report_id()` 不改。`main.py:112` 的 `dict.fromkeys` 去重仍有效。
- **测试**：把审查文档 F6 的 11 行对照表加入 `tests/test_core.py`（已在本方案复核阶段离线验证 11/11 通过）。

#### F3（选项 A，待 D1 确认）关闭历史后仍执行保留期清理

- **目标**：关闭历史功能后，已有记录仍按 30 天过期删除；`/spark forget` 在关闭状态下也能删除自己的记录；数据库不存在时不创建。
- **修改方法**：
  1. `spark_core/history.py`：新增 `purge()`，文件不存在时直接返回 0，否则 `connect()`（内含过期删除）并返回删除行数。
  2. `History.delete()`：前置判断由 `if not self.enabled` 改为 `if not self.path.exists(): return 0`。
  3. `main.py.__init__`：创建 `self.history` 后调用 `self.history.purge()`，并用 `try/except (sqlite3.Error, OSError)` 包住，失败只 `logger.warning`。**数据库损坏不得导致插件加载失败。**
  4. `spark_command` 的 `forget` 回复去掉“关闭存储时不会打开数据库；旧库须启用后清除”。
  5. README “历史比较”一节与 `_conf_schema.json` 中 `history_enabled` 的 hint 改为：关闭后不再写入新记录，已有记录仍在 30 天后（插件加载时）清理，也可用 `/spark forget` 清除。
- **测试**：启用状态下写入一条过期记录、一条新记录，再以 `history_enabled=False` 构造插件：过期记录被删、新记录保留；无数据库文件时构造插件不创建文件（现有 `test_missing_provider_does_not_call_main` 已断言）；关闭状态下 `forget` 能删除已有记录。现有 `test_history_off_and_isolation` 不受影响。
- **限制说明**：关闭状态下只在插件加载时清理。长期不重启时过期记录会延后删除，在文档中写明即可。

#### X3 + S1 历史比较与历史保存的故障降级

> S1 不在审查文档中，是编写本方案时发现的同类问题，已复现（REPRODUCED）：模型返回“模型结论”后，让 `history.save` 抛出 `sqlite3.OperationalError('database is locked')`，用户最终只收到“Spark分析未完成：OperationalError。…”。

- **X3 问题**：`compare()` 直接取 `old['platform']`、`old['sampling']`、`old['health']`，旧版本保存的概览结构不同时会抛 `KeyError`。此时报告已下载，整次分析却失败。`history.list()` 的 SQLite 错误也会让整次分析失败。
- **S1 问题**：`main.py:222` 的 `self.history.save(...)` 在模型**已成功给出结论之后**执行。数据库被锁、磁盘满等错误会让流程进入 `except Exception`，用户只看到“分析未完成”，已付费生成的结论被丢弃。这与 1.0.2 中“缓存故障不影响分析”的原则不一致。
- **修改方法**：
  1. `history.compare()` 中改用 `(old.get('platform') or {})`、`.get('sampling')`、`.get('health')` 等安全取值。
  2. `main.py:156-157` 的历史查询与比较包进一个 `try/except (sqlite3.Error, OSError, ValueError, KeyError, TypeError, AttributeError)`，失败时 `history=[]`、`comparison=None`，并记录 `logger.warning`（只写类型名）。
  3. `main.py:222` 的 `save` 包进 `try/except (sqlite3.Error, OSError)`，失败时 `history_id=None` 并 `logger.warning`，照常发送分析结论（不附“历史记录”后缀）。
- **测试**：① 旧格式概览（缺少 `sampling` 等键）调用 `compare` 不抛异常；② 让 `history.save` 抛 `sqlite3.OperationalError`，最终结果仍是模型结论；③ 让 `history.list` 抛错，分析照常完成且 `comparison` 为空。

#### X4 权限模式文案与界面选项不一致

- **位置**：`_conf_schema.json` 中 `user_whitelist.hint`。
- **修改方法**：把“仅在“管理员和用户名单”模式下生效”改为“仅在 admin_and_whitelist（管理员和用户名单）模式下生效”，与界面实际显示的选项值一致。README “常用设置”同步。
- **测试**：无（纯文案）。

### 批次 4：开发工具链（F8）

| 子项 | 修改方法 | 验证 |
|---|---|---|
| `tools/release_descriptions.py` 已失效 | 删除（待 D4）。`_conf_schema.json` 为文案唯一来源 | 仓库中无其他引用 |
| `tools/check_sample.py` 无法独立运行 | 在文件 docstring 写明 `PYTHONPATH=tests python tools/check_sample.py <报告URL>`（`tests/astrbot` 提供 logger 桩） | 不发起网络请求，只检查到 import 成功、无参数时给出提示 |
| 版本号分散 4 处 | `tools/package_plugin.py` 用正则 `^version:\s*(\S+)` 从 `metadata.yaml` 读取版本，生成 `astrbot_plugin_spark-{version}.zip`（不引入 PyYAML）。`@register` 保留字面量。可选：新增测试断言 `metadata.yaml` 与 `@register` 的版本一致 | 运行打包脚本，产物名仍为 `astrbot_plugin_spark-v1.0.2.zip`；打包产物不含 `FIX_PLAN.md`、`AUDIT_FINDINGS.md`（脚本使用显式文件列表，本来就不包含） |
| 没有 CI | 新增 `.github/workflows/tests.yml`：Python 3.12、3.13 矩阵；`pip install -r requirements.txt`；`python -m unittest discover -s tests`（待 D5） | 推送后 Actions 通过 |
| worker 中 `profile` 遮蔽标准库 | `spark_core/worker.py:8` 的 `sys.path.insert(0, ...)` 改为 `sys.path[0] = str(Path(__file__).resolve().parents[1])`，把脚本所在的 `spark_core/` 从路径首位移除。**不重命名** `profile.py` | 现有真实 worker 测试（拒绝原因、DecodeError、路径隐藏）全部通过 |

### 独立事项：F7 历史提交中的真实报告 ID

- 不做任何自动操作。向维护者汇报 D3 三个选项；若选择重写历史，须单独确认后使用 `git filter-repo` 并强推，同时通知所有 fork 与克隆者。

### 本轮不实施（记录备查）

- 审查文档 §5 第 3 点：严格的“子调用耗时大于父调用即拒绝”校验可能误拒其他 Spark 版本的报告。缺少样本，暂不改校验逻辑；可在后续版本增加拒绝计数日志后再评估。
- F4 选项 A、C：等 D2 确认。

## 5. 文档与 CHANGELOG 更新清单

在 `CHANGELOG.md` 顶部（`## 1.0.2` 之上）新增 `## Unreleased`，按“修复 / 变更 / 安全与隐私 / 测试”分类记录：

- 修复：F1、F2、F5、F6、X1、X2、X3、S1
- 变更：F4（自动分析对无权限用户静默）、F3（关闭历史后仍清理过期记录）、F8（工具链）
- 安全与隐私：F2（崩溃后不残留报告数据）、F3（保留期承诺兑现）
- 测试：新增项数与总数（见第 7 节）

需要同步的文档：README “历史比较”、“常用设置”（F3、X4，若做 F4 选项 A 还有新配置项）；`_conf_schema.json` 中 `history_enabled`、`user_whitelist` 的 hint。版本号四处均不改动。

## 6. 每批的验证流程

```bash
pip install -r requirements.txt
python -m unittest discover -s tests          # 每批结束必须全绿
python tools/package_plugin.py                # 批次 4 后检查打包
```

- 新测试先在**未修改的代码**上运行，确认它会失败，再实施修改，确认它通过。
- 每批自查：没有引入 `import logging`；聊天文本没有新增 `str(exc)`；没有放宽下载与解析限制。

## 7. 预计测试数量

| 来源 | 数量 |
|---|---|
| 基线 | 51 |
| F1 | +2 |
| F5 | +1 |
| F2 | +2 |
| X1 | +1 |
| F6 | +1 |
| F3（选项 A） | +1~2 |
| X3 + S1 | +3 |
| F8 版本一致性（可选） | +1 |
| F4 | 修改 1 项，不新增 |
| **合计** | **约 62~64** |

## 8. 风险与回滚

- 每批单独提交，提交信息注明对应编号（如 `Fix F1/F4/F5: ...`），出现问题可以按批 `git revert`。
- F1 的正确性依赖 AstrBot 源码阅读结论；建议发版前在真实 AstrBot（至少 4.28.x，最好也测声明的下限 4.16.0）中，用工具模式触发一次“未配置模型”的快速失败，确认主模型的回复不再变成 “Output stopped.”。
- F4(B) 会改变现有可见行为（无权限用户不再收到提示），需在 CHANGELOG 中写明。
- F2 的启动清扫基于“一个数据目录只对应一个插件实例”的前提；若有多实例共享同一数据目录，需改为只清理修改时间较早的目录。
