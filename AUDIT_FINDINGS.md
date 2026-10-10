# Audit findings: astrbot_plugin_spark_analyse

```yaml
repo: EvergardenRoxy/astrbot_plugin_spark_analyse
audited_commit: e15f67f   # main, v1.0.2
audit_date: 2026-10-10
plugin_declares: AstrBot >=4.16.0
read_against: AstrBot 4.28.2 (PyPI wheel; the declared floor 4.16.0 was NOT tested)
python_used: 3.13 (AstrBot 4.28.2 itself requires >=3.12)
baseline_tests: 51 passed, 0 failed
code_changed_by_audit: none   # this file is the only addition; probes ran in a scratch dir
fix_validation: |
  Proposed fixes for F1, F2 (worker exit + startup sweep) and F5 were applied to a scratch copy
  of the repo together with the tests in Appendix A. Result: on the ORIGINAL code exactly the new
  tests fail and the 51 existing tests pass; with the fixes all 56 tests pass.
  The F6 regex was checked against its table (0 mismatches). F3, F4, F8 and X1-X4 were NOT
  implemented or run; they are described only.
```

## 0. How to use this document (read first)

This file is written to be consumed by an LLM/engineer that will implement the fixes. Each finding is self-contained.

**Evidence tags**

| Tag | Meaning |
|---|---|
| `REPRODUCED` | I ran code and observed the behavior. |
| `SOURCE-VERIFIED` | I read the AstrBot 4.28.2 source and traced it. I did NOT run a live AstrBot. |
| `BY-INSPECTION` | Visible from this repo's code only. Not executed. Lowest confidence. |

**Line numbers** refer to commit `e15f67f`. A grep anchor is given for each so you can re-locate after edits.

**Run the tests** (from repo root; baseline is 51 passing):

```bash
pip install -r requirements.txt          # aiohttp>=3.10,<4  protobuf>=6.31.1,<7
python -m unittest discover -s tests     # no PYTHONPATH needed
```

Plugin tests do not use a real AstrBot. They use a stub `tests/astrbot/api.py` (`logger = Mock()`) and hand-written SDK doubles in `tests/test_plugin.py` (`Event`, `FakeSession`). `Event.stop_event()` only sets `event.stopped = True`, which is enough to assert on stop behavior.

**Project constraints that must be preserved**

- Log only via `from astrbot.api import logger` (enforced by `tests/test_logging_scope.py`). Do not import stdlib `logging`.
- Text shown in chat is Chinese. Chat must never show raw exception messages except `ProfileError` / `LoadTimeout` text, because other messages can contain local paths. For any other exception only `type(exc).__name__` is shown.
- Do not loosen download or parse safety limits: official host only, no redirects, `trust_env=False`, 16 MiB compressed / 128 MiB decoded, node/depth limits.
- No new runtime dependencies.
- Do not bump the version. Version lives in 4 hand-synced places (`metadata.yaml:4`, `@register(...)` in `main.py:21`, `tools/package_plugin.py:9`, `CHANGELOG.md`). Add changes under a new `## Unreleased` heading in `CHANGELOG.md` and let the maintainer cut the release.
- Do not create a PR or rewrite git history unless the maintainer explicitly asks (see F7).

## 1. Summary

| ID | Severity | Evidence | Area | One line |
|---|---|---|---|---|
| F1 | medium | SOURCE-VERIFIED | `main.py` tool mode | Background task calls `event.stop_event()` on the main chat agent's event, aborting the main model's reply. |
| F2 | medium | REPRODUCED | `session.py`, `worker.py` | After the host is killed, the worker process and the temp dir (with the decoded report) leak forever. |
| F3 | medium-low | BY-INSPECTION | `history.py` | With history disabled, old rows are never purged, contradicting "retained 30 days". |
| F4 | low-medium | BY-INSPECTION | `main.py` `auto_analyze` | Fires on any group chatter, no wake/@ needed; non-permitted users get a denial reply and the event is swallowed. |
| F5 | low | REPRODUCED | `main.py` `handle()` | Variable `task` shadowed; `raise` inside `finally` swallows `CancelledError`. |
| F6 | low | REPRODUCED | `transport.py` | `LINK` regex rejects a link followed by `.` or `/`; disagrees with `report_id()`. |
| F7 | low | REPRODUCED | git history | A real report id is still in two old commits (HEAD is clean). Maintainer decision. |
| F8 | low | REPRODUCED | `tools/`, packaging | Dev tooling is stale or broken; stdlib `profile` is shadowed in the worker; version in 4 places; no CI. |
| X1-X4 | low | BY-INSPECTION | various | Smaller robustness items (section 4). |

Suggested order: F5 + F1 + F4 together (all edit `main.py` `handle()`/entry points), then F2 + X1 + X2 (session/worker), then F6, F3, F8, X3, X4. F7 is independent and needs a human decision.

---

## 2. Findings

### F1. Tool mode calls `stop_event()` on the main agent's event (medium, SOURCE-VERIFIED)

**Location** (anchor: `event.stop_event()` and `async def analyze_tool`)

- `main.py:83-104` `analyze_tool`; inner `run()` at `:93-101` iterates `self.handle(event, ...)` in a background `asyncio.create_task`.
- `handle()` calls `event.stop_event()` at `:110` (permission denied), `:115` (not exactly one link), `:120` (no provider configured), `:125` (duplicate in-flight report), `:129` (busy), and in the `finally` at `:248`.
- `:72` and `:77` are inside `spark_command` only and are correct. Leave them.

**Problem.** The `llm_tool` receives the same `event` object that the main chat agent is currently running on. The tool returns a string immediately (`:104`), so the main agent keeps going and asks the main model for a follow-up reply. Meanwhile the background task runs `handle()`. The early-return branches finish within milliseconds and call `event.stop_event()`.

**Evidence** (paths are inside the AstrBot 4.28.2 package):

- `astrbot/core/platform/astr_message_event.py:348-354`: `stop_event()` sets `self._force_stopped = True`. The flag is sticky.
- `astrbot/core/astr_agent_run_util.py:26-27`: `_should_stop_agent` is `event.is_stopped() or agent_stop_requested`.
- `astrbot/core/astr_agent_run_util.py:152-153` and `:356-361`: a `_watch_agent_stop_signal` task polls every 0.5 s and calls `agent_runner.request_stop()` when stopped. Per-step checks are at `:157-158` and `:182-183`.
- `astrbot/core/agent/runners/tool_loop_agent_runner.py:1510-1544` `_finalize_aborted_step`: appends the synthetic messages `"Stop output."` / `"Output stopped."` to the conversation, sets the final response to `"Output stopped."`, returns an `aborted` response. The model's real follow-up is dropped.
- `astrbot/core/pipeline/process_stage/method/agent_sub_stages/internal.py:517`: history is saved when `not event.is_stopped() or agent_runner.was_aborted()`, so the aborted turn is saved with the synthetic pair instead of the real answer.

**Impact.** In tool mode, any fast-failure path (no provider configured, permission denied, wrong link count, busy, duplicate) makes the main chat agent abort within about 0.5 s. The user still receives the plugin's own message via `event.send`, but the main model's reply is replaced and the stored conversation history is corrupted. The success-path `stop_event()` at `:248` arrives after minutes and normally lands after the main agent finished, so it is usually harmless, but a slow main model can still be hit.

**Why tests miss it.** `Event.stop_event()` in `tests/test_plugin.py` is a plain flag and nothing consumes it.

**Recommended fix.** Make stopping the event opt-out for the tool path.

```python
# main.py
async def handle(self, event, text, *, stop: bool = True):
    ...
    # replace every `event.stop_event()` inside handle() (lines 110,115,120,125,129,248) with:
    if stop:
        event.stop_event()

# in analyze_tool.run():
async for result in self.handle(event, report_url+' '+observation, stop=False):
    await event.send(result)
```

`auto_analyze` and `spark_command` keep the default `stop=True`.

**Acceptance criteria** (add to `tests/test_plugin.py`, using `Event.stopped`):

1. Tool path, fast failure (no `analysis_provider_id`): after `await asyncio.gather(*plugin.background_tasks)`, `event.stopped is False`, and the failure message was passed to `event.send`.
2. Tool path, success (reuse `test_tool_returns_before_analysis_completes`): `event.stopped is False` at the end.
3. Command path unchanged: `test_scheduler_does_not_stop_after_progress` still passes (`event.stopped is True` at the end).

---

### F2. Orphaned worker and leaked temp dir after the host dies (medium, REPRODUCED)

**Location** (anchors: `mkdtemp(prefix='spark-'`, `while not (directory/'stop')`, `async def close`)

- `spark_core/session.py:22` creates `root/spark-XXXX/` containing `profile.bin` (the full decoded report, up to 128 MiB).
- `spark_core/worker.py:31` loops forever until a `stop` file appears. Nothing ever writes `stop`. The session only ever kills the process.
- `spark_core/session.py:98-104` `close()` is the only cleanup. There is no startup sweep anywhere (`grep -rn "spark-\|rmtree" main.py spark_core` finds only these two spots).

**Reproduction** (done):

1. Child process creates `ReportSession`, loads a real synthetic report (real worker subprocess), prints the worker pid and dir, then `os.kill(os.getpid(), SIGKILL)` (simulates OOM-kill, `docker kill`, crash).
2. Three seconds later: the worker is still alive with `ppid=1`, and `spark-xxxx/profile.bin` and `ready.json` are still on disk.

**Impact.** Disk leak per crash (up to 128 MiB each), a leaked busy-polling process (wakes every 50 ms), and user report data left on disk, which contradicts the README "数据存储" section that lists only two SQLite files.

**Recommended fix** (do all three):

1. `worker.py` loop: exit when its directory no longer exists, and on POSIX exit when the parent changes.

   ```python
   parent = os.getppid()
   while not (directory/'stop').exists():
       if not directory.exists() or (sys.platform != 'win32' and os.getppid() != parent):
           return
       ...
   ```

   Do not rely on `getppid()` alone: on Windows it keeps returning the dead parent's pid.
2. Startup sweep in `SparkPlugin.__init__` (`main.py:26` area): `shutil.rmtree(p, ignore_errors=True)` for every `self.root.glob('spark-*')` directory. This is safe because the gate allows one session at a time and none exists at init. Removing the dir also makes any orphaned worker exit via step 1.
3. `ReportSession.close()`: make `shutil.rmtree` run in a `finally`, so a timeout in `wait_for(self.process.wait(), 5)` does not skip it (see X1).

**Acceptance criteria**

- Test: pre-create `root/spark-stale/profile.bin`, construct `SparkPlugin`, assert the directory is gone.
- Test: start `worker.py` on a dir containing a valid `profile.bin`, wait for `ready.json`, `shutil.rmtree` the dir, assert the process exits within about 2 s.
- All 51 existing tests still pass (note `test_worker_hides_messages_that_may_contain_paths` runs the worker on a dir without `profile.bin`; the error path must keep writing `ready.json` as it does now).

---

### F3. History is never purged while the feature is disabled (medium-low, BY-INSPECTION)

**Location** (anchors: `if not self.enabled`, `DELETE FROM reviews WHERE created`)

- `spark_core/history.py:19` is the only retention purge and runs inside `connect()`.
- `history.py:25`, `:35`, `:43` return early when `enabled` is false, so `connect()` is never reached.
- `main.py:75-76` `/spark forget` tells the user "关闭存储时不会打开数据库；旧库须启用后清除".

**Problem.** README and the config hint promise "保留 30 天". If an admin turns history off, existing rows (hashed owner, hardware overview, full LLM result text) stay on disk indefinitely, and a user cannot erase their own rows with `/spark forget` either.

**This is partly documented** (the config hint says disabling does not delete existing history), so treat it as a product/privacy decision. Two options:

- **Option A (recommended, small):** when `path.exists()`, always run the expiry purge at plugin init (never create the DB if absent; existing tests assert `history.sqlite3` does not exist when disabled). Also let `History.delete()` work when the file exists even if disabled. Update the `forget` message, README "历史比较" and the `history_enabled` hint accordingly.
- **Option B (docs only):** keep behavior; state clearly in README and the config hint that data written earlier is kept until history is re-enabled and `/spark forget` or retention runs.

**Acceptance criteria (Option A):** create a DB with an expired row and a fresh row while enabled, then construct the plugin with `history_enabled=False`: the expired row is gone, the fresh one remains, and no DB file is created when none existed.

---

### F4. `auto_analyze` has no wake condition and answers denials (low-medium, BY-INSPECTION)

**Location** (anchor: `async def auto_analyze`)

- `main.py:53-64` is registered with `filter.event_message_type(filter.EventMessageType.ALL, priority=10)`.
- It runs `handle()` whenever the message contains a spark link plus any of `分析|诊断|排查|卡顿|掉tps|analy[sz]e`.
- `handle()` then replies "此会话或用户没有Spark分析权限。" and calls `event.stop_event()` (`main.py:107-111`) for non-permitted users.
- `tests/test_plugin.py::test_access_modes_and_all_entry_points` currently asserts that `auto_analyze` yields a message containing "权限". The test encodes the present behavior as intended, so confirm intent with the maintainer before changing it.

**Facts from AstrBot source.** `EventMessageType.ALL` passes for group, private and other message types (`astrbot/core/star/filter/event_message_type.py`), so the plugin sees every message in every chat. The main-LLM stage requires `event.is_at_or_wake_command` (`astrbot/core/pipeline/process_stage/stage.py:58`), but this plugin's own analysis does not check it.

**Problems**

1. Two people discussing a spark link ("你看下这个卡顿 https://spark.lucko.me/...") trigger an LLM analysis and a long group reply, charged to the operator.
2. With `access_mode=admin_only` (a likely production setting), every non-admin message with a link and one of those keywords gets a denial reply and the event is swallowed (`stop_event`), so other plugins and the default LLM never see it.
3. Defaults are `access_mode=all` and `auto_analyze=true`. The only limit is one analysis at a time globally, so one user can keep everyone else on "已有Spark分析正在执行".

**Recommended fix**

- **B (do this one):** in `auto_analyze`, if `not self.allowed(event)`: `return` silently, with no yield and no `stop_event()`. Denial replies stay for the explicit `/spark` command and the tool. Update `test_access_modes_and_all_entry_points`: `auto_analyze` yields nothing and leaves `event.stopped` False; `spark_command` and `analyze_tool` still reply with the permission message.
- **A (product decision, ask first):** new bool config `auto_analyze_requires_wake` that makes `auto_analyze` return early unless `getattr(event, 'is_at_or_wake_command', True)`. Add it to `_conf_schema.json` (Chinese `description`/`hint`) and README "常用设置". The `getattr` default keeps the stub `Event` working.
- **C (optional):** per-owner cooldown, for example a dict `owner -> last start time` and a 60 s minimum interval.

---

### F5. Cancellation path in `handle()` (low, REPRODUCED)

**Location** (anchors: `task = asyncio.current_task()`, `for task in remaining`, `raise RuntimeError('模型任务未及时取消`)

- `main.py:133` binds `task = asyncio.current_task()` and `:134` adds it to `self.tasks`.
- `main.py:207` `for task in remaining:` rebinds the same name. `main.py:247` `self.tasks.discard(task)` then discards the wrong object, so the real task stays in `self.tasks` forever.
- `main.py:209` raises `RuntimeError` inside a `finally`. If the exception in flight is `CancelledError`, it is replaced, so the cancellation is swallowed. The generator then goes through `except Exception` and yields "Spark分析未完成：RuntimeError…" to the chat.

**Reproduction** (done; the model task ignores cancellation for more than 5 s, `cancel_bounded` timeout patched to 0.05 s): after `runner.cancel()`, `runner.cancelled()` is `False`, a failure message is sent, and `len(plugin.tasks) == 1` (expected 0). Trigger is narrow, hence low severity.

**Recommended fix**

```python
finally:
    remaining = await cancel_bounded({model_task})
    for stuck in remaining:                      # renamed, no longer shadows `task`
        self.background_tasks.add(stuck)
        stuck.add_done_callback(self.background_tasks.discard)
    # no `raise RuntimeError` here: the `except Exception` below already re-raises
    # when `remaining` is non-empty, which preserves "stop falling back to avoid duplicate requests".
```

The only visible change is that the shown error type becomes the original exception's type instead of `RuntimeError`.

**Acceptance test** (add to `PluginTests` in `tests/test_plugin.py`; the `finally` guarantees the stubborn task is released so the loop can tear down even if the assertion fails):

```python
async def test_cancel_with_uncancellable_model_task(self):
    started, stop = asyncio.Event(), asyncio.Event()
    async def stubborn_agent(**kwargs):
        started.set()
        while not stop.is_set():
            try: await asyncio.wait_for(stop.wait(), 3600)
            except asyncio.CancelledError: pass
    plugin = self.module.SparkPlugin(types.SimpleNamespace(tool_loop_agent=stubborn_agent), {'analysis_provider_id': 'test'})
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
```

---

### F6. `LINK` regex vs `report_id()` mismatch (low, REPRODUCED)

**Location** (anchors: `^LINK = re.compile`, `def report_id`)

- `spark_core/transport.py:19` `LINK` lookahead excludes `/ . _ -` after the id, so a link followed by an ASCII period or a trailing slash does not match at all.
- `spark_core/transport.py:23` `report_id()` accepts one trailing `/`.

**Effect.** "…/AbCdEf1234." (sentence-ending period) or "…/AbCdEf1234/" gives no match. In auto mode the bot stays silent. With `/spark` it answers "请提供一个 https://spark.lucko.me/报告ID ", which confuses a user who did provide a link. Chinese `。` and `,` already work.

**Recommended fix.** Replace the lookahead; it was tested against the table below with 0 mismatches:

```python
LINK = re.compile(r'https://spark\.lucko\.me/([A-Za-z0-9]{6,64})(?![A-Za-z0-9_-]|/[A-Za-z0-9]|\.[A-Za-z0-9])')
```

| input (after `https://spark.lucko.me/`) | expected `findall` |
|---|---|
| `AbCdEf1234` | `['AbCdEf1234']` |
| `AbCdEf1234。` | `['AbCdEf1234']` |
| `AbCdEf1234.` | `['AbCdEf1234']` (currently `[]`) |
| `AbCdEf1234/` | `['AbCdEf1234']` (currently `[]`) |
| `AbCdEf1234?x=1` | `['AbCdEf1234']` |
| `AbCdEf1234,卡顿` | `['AbCdEf1234']` |
| `AbCdEf1234/extra` | `[]` |
| `AbCdEf1234.json` | `[]` |
| `AbCdEf1234-` | `[]` |
| `AbCdEf1234_x` | `[]` |
| `abc` (too short) | `[]` |

Add this table as a test in `tests/test_core.py`. `report_id()` is unchanged. Note `main.py:112` dedupes links with `dict.fromkeys`, so the id forms still collapse.

---

### F7. A real report id remains in git history (low, REPRODUCED; needs a human decision)

- `git log --all -S95tLrddUhW` shows commits `e6703ce` (v0.1.9) and `98d2ef1` ("Security: remove real report references from public sources"). HEAD is clean (`git grep` finds nothing). The id appears in the diffs of those two commits (34 matching lines in `git log --all -p`), in tests and README.
- No API keys, tokens, or credentials were found in history or the tree. The "secret" grep hits were test fixtures and a `contextvars` token in removed code.

**Instruction to the implementer: do nothing automatically.** Removing it requires rewriting public history (`git filter-repo` plus force-push), which breaks clones and forks. Present these options to the maintainer: (a) leave it, if the report is expired or non-sensitive; (b) check whether the report is still online and what it exposes (hardware, JVM args, thread names, server hints); (c) rewrite history only on explicit approval.

---

### F8. Dev tooling, packaging and module naming (low, REPRODUCED)

| Item | Fact | Recommended action |
|---|---|---|
| `tools/release_descriptions.py:18` | `assert set(s) == set(texts)` fails with `AssertionError`, because `download_proxy_enabled` and `download_proxy_url` exist in `_conf_schema.json` but not in the script. Even if the assert were removed, it would overwrite newer hints with older text. | Delete the script. `_conf_schema.json` is now the source of truth. |
| `tools/check_sample.py` | Raises `ModuleNotFoundError: astrbot` outside AstrBot. Works with `PYTHONPATH=tests`. | Document `PYTHONPATH=tests python tools/check_sample.py <url>` in the file docstring. |
| Version in 4 places | `metadata.yaml:4`, `main.py:21`, `tools/package_plugin.py:9`, `CHANGELOG.md`. | Make `package_plugin.py` read the version from `metadata.yaml` (`version: v1.0.2`). Keep `@register` literal. |
| No CI | No `.github/`. | Add a workflow: Python 3.12 and 3.13, `pip install -r requirements.txt`, `python -m unittest discover -s tests`. |
| stdlib shadowing | `spark_core/profile.py` shadows stdlib `profile`. `worker.py` runs as a script, so `sys.path[0]` is `spark_core/`. Verified: in that mode `import profile` resolves to the plugin file (and fails with a relative-import error). Not currently triggering any bug. | In `spark_core/worker.py:8` replace `sys.path.insert(0, ...)` with `sys.path[0] = str(Path(__file__).resolve().parents[1])`. Verified: stdlib `profile` then resolves correctly. Do not rename `profile.py`; tests import `spark_core.profile`. |

---

## 3. Verified non-issues (do not re-investigate)

- **Large reports are fine.** Synthetic profile near the 128 MiB decoded limit (125 MiB, 490,000 nodes x 30 windows): worker ready in 34 s (default parse timeout 90 s), first `hotspots` query 3.8 s (query timeout 30 s), within the 1.5 GiB `RLIMIT_AS`. Scaling is linear (50k nodes 3.3 s, 200k 14.6 s, 400k 28 s). Measured on a 4-core 2.8 GHz Xeon, so slow VPSes have roughly 2.6x headroom.
- **Logger format is correct.** `astrbot.api.logger` is a proxy to a stdlib `logging.Logger` (`astrbot/api/__init__.py`), so `%s` arguments work.
- **SDK contract matches 4.28.2.** `Context.tool_loop_agent` is keyword-only with `event, chat_provider_id, prompt, tools, system_prompt, max_steps, tool_call_timeout`; `FunctionTool(handler=...)` is invoked as `handler(event, **args)`; an `llm_tool` async generator that yields a plain `str` is converted to the tool result (`astrbot/core/astr_agent_tool_exec.py`).
- **Dependencies resolve.** `pip install --dry-run astrbot-4.28.2 "aiohttp>=3.10,<4" "protobuf>=6.31.1,<7"` reports no conflict.
- **Download and sandbox design is sound** (by code and tests): official host only, no redirects, no env proxy, gzip bomb / truncation / concatenation tests, proxy validation that does not leak credentials, parser in a subprocess with `RLIMIT_AS`, bounded timeouts, path-free error messages.
- **CHANGELOG 1.0.2 claims** about the SQLite DQS fix, duplicate-link handling, failure reasons and cache degradation match the code and tests. The "41 to 51 tests" claim matches the 51 counted.

## 4. Smaller items (BY-INSPECTION, low)

- **X1.** `session.py:98-104` `close()`: if `wait_for(self.process.wait(), timeout=5)` raises, `shutil.rmtree` is skipped and the dir leaks. Also `main.py:251-256` `terminate()`: the first `session.close()` exception aborts the loop, so remaining sessions are not closed. Wrap each in `try/except` and use `finally` for `rmtree(..., ignore_errors=True)`.
- **X2.** `worker.py:49` writes the error `ready.json` non-atomically, while the success path at `:28-29` uses tmp + replace. A reader polling `session.py:35` could parse a half-written file and surface a generic `JSONDecodeError`. Use tmp + `replace`.
- **X3.** `history.compare()` (`history.py:50-62`, caller `main.py:157`) indexes `old['platform']` / `old['sampling']` directly. If a stored overview from an older plugin version has a different shape, this raises `KeyError` after the report was already downloaded. Use `.get(key, {})` and wrap the call so a failed comparison degrades to `comparison=None`.
- **X4.** `_conf_schema.json:90` says "管理员和用户名单" mode, while the option shown in the UI is the raw value `admin_and_whitelist` (`:81`). Align the wording.

## 5. Not verified / open questions for the maintainer

1. No end-to-end run inside a live AstrBot, and no live request to spark.lucko.me. F1 rests on source reading only.
2. AstrBot floor 4.16.0 was not tested; everything was read against 4.28.2.
3. CHANGELOG says one real report was rejected because child time exceeded parent time (self-time summing to about 5280%). I do not have that report, so I cannot tell whether the strict fail-closed validation (`spark_core/profile.py:60-64`) rejects legitimate reports from other Spark versions or engines. Consider logging a rejection counter or documenting the behavior.
4. F4: is "analyze any message that has a link and an intent keyword, without needing @" intended? The current test asserts it, but that may only record behavior.
5. F3: purge-on-start (Option A) or documentation-only (Option B)?
6. F7: leave, check, or rewrite history?

## 6. Do not

- Do not skip or delete existing tests to get green; change a test only where this document says its assertion encodes behavior being fixed (F4).
- Do not add stdlib `logging`, new dependencies, or network calls at plugin startup.
- Do not commit anything under `dist/`, `*.sqlite3` or `*.bin` (all gitignored).

---

## Appendix A. Validated test code (add these; they fail before the fix, pass after)

Helpers `Event`, `FakeSession`, `self.module`, `self.temp` already exist in `tests/test_plugin.py` (`PluginTests`). Imports needed there are already present (`asyncio`, `types`, `patch`, `Path`).

### A.1 F1: tool path must not stop the main event (add to `PluginTests`)

```python
async def test_tool_fast_failure_does_not_stop_main_event(self):
    plugin = self.module.SparkPlugin(types.SimpleNamespace(), {})     # no provider configured
    event, sent = Event(), []
    async def send(result): sent.append(result)
    event.send = send
    result = [r async for r in plugin.analyze_tool(event, 'https://spark.lucko.me/SyntheticReport001')]
    await asyncio.gather(*list(plugin.background_tasks))
    self.assertIn('后台', result[0])
    self.assertTrue(any('不会回落' in r for r in sent))
    self.assertFalse(event.stopped)

async def test_tool_success_does_not_stop_main_event(self):
    async def agent(**kwargs): return types.SimpleNamespace(completion_text='后台结果')
    plugin = self.module.SparkPlugin(types.SimpleNamespace(tool_loop_agent=agent), {'analysis_provider_id': 'test'})
    event, sent = Event(), []
    async def send(result): sent.append(result)
    event.send = send
    with patch.object(self.module, 'ReportSession', FakeSession):
        [r async for r in plugin.analyze_tool(event, 'https://spark.lucko.me/SyntheticReport001')]
        await asyncio.gather(*list(plugin.background_tasks))
    self.assertIn('后台结果', sent)
    self.assertFalse(event.stopped)
```

Validated code change for F1 (`main.py`): `async def handle(self, event, text, *, stop=True):`, guard each of the six `event.stop_event()` calls inside `handle()` with `if stop:`, and call `self.handle(event, report_url+' '+observation, stop=False)` from `analyze_tool.run()`.

### A.2 F5: cancellation with an uncancellable model task

The test is the code block in F5 above (name `test_cancel_with_uncancellable_model_task`). Validated code change for F5: rename the loop variable `task` to `stuck` in the `for ... in remaining:` loop (`main.py:207`) and delete the `raise RuntimeError(...)` at `main.py:209`.

### A.3 F2: worker exits when its directory vanishes; startup sweep

Add to `tests/test_load_timeout.py` (it already imports `subprocess`, `sys`, `tempfile`, `unittest`, `Path`):

```python
class WorkerLifecycleTests(unittest.TestCase):
    def test_worker_exits_when_directory_removed(self):
        import shutil, time
        from test_core import sample
        worker = Path(__file__).resolve().parents[1]/'spark_core'/'worker.py'
        root = tempfile.mkdtemp()
        directory = Path(root)/'spark-x'
        directory.mkdir()
        (directory/'profile.bin').write_bytes(sample().SerializeToString())
        proc = subprocess.Popen([sys.executable, str(worker), str(directory)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.time() + 30
            while not (directory/'ready.json').exists() and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue((directory/'ready.json').exists())
            shutil.rmtree(directory)
            for _ in range(60):
                if proc.poll() is not None: break
                time.sleep(0.05)
            self.assertIsNotNone(proc.poll(), 'orphaned worker kept running after its directory was removed')
        finally:
            if proc.poll() is None: proc.kill()
            proc.wait()
            shutil.rmtree(root, ignore_errors=True)
```

Add to `PluginTests`:

```python
async def test_startup_sweeps_stale_session_dirs(self):
    stale = Path(self.temp.name)/'spark-stale'
    stale.mkdir()
    (stale/'profile.bin').write_bytes(b'left behind by a crashed host')
    keep = Path(self.temp.name)/'profiles.sqlite3'
    keep.write_bytes(b'not a session dir')
    self.module.SparkPlugin(types.SimpleNamespace(), {})
    self.assertFalse(stale.exists())
    self.assertTrue(keep.exists())
```

Validated code changes for F2:

- `spark_core/worker.py`: add `import os`; before the `while` loop set `parent = os.getppid()`; first statements inside the loop:
  `if not directory.exists() or (sys.platform != 'win32' and os.getppid() != parent): return`
- `main.py`: add `import shutil`; right after `self.root = StarTools.get_data_dir('astrbot_plugin_spark')` add
  `for stale in self.root.glob('spark-*'): shutil.rmtree(stale, ignore_errors=True)`
- Not yet validated: the `finally`-guarded `rmtree` in `ReportSession.close()` (step 3 / X1).
