# Engineering handoff: `claude/keen-bohr-d01us1`

Audience: engineers or LLM agents debugging or reviewing this branch. This is a record of what was changed,
why, what evidence the decisions rest on, and where the risk is. It is not user documentation (see README /
CHANGELOG for that).

## 1. Context

| Item | Value |
|---|---|
| Base | `main` @ `e15f67f` (v1.0.2) |
| Inputs | `AUDIT_FINDINGS.md` (finding IDs F1-F8, X1-X4), `FIX_PLAN.md` (batching, decisions D1-D5, execution status). Both were removed from the tree at release; read them with `git show c47cc0f:AUDIT_FINDINGS.md` / `git show c47cc0f:FIX_PLAN.md`. |
| Decisions applied | D1=A (purge history while disabled), D2=B (auto-analyze silent for non-permitted users), D4=delete stale tool, D5=add CI. D3/F7 untouched. |
| Extra finding | S1: a failed history write discarded a finished analysis (reproduced before fixing) |
| Version | 1.0.3 (see §7). Changes are under `## 1.0.3 - 2026-10-11` in `CHANGELOG.md`. |
| Tests | 51 -> 68, all passing locally (Python 3.13) and in CI (3.12, 3.13) |
| AstrBot evidence | Read from PyPI wheels, never executed: 4.28.2 and 4.16.0 in full; `internal.py` only for 4.20.0, 4.24.0, 4.26.0/.4/.8, 4.27.0, 4.28.0. **No live AstrBot run.** |

Method used per batch: write the tests -> confirm they fail on the old code -> change code -> run the full suite.
Code anchors below are greps, since line numbers drift. AstrBot line numbers refer to 4.28.2 unless stated.

### Commit map

| Commit | Scope |
|---|---|
| `0432789` | F1, F4, F5 (`main.py`) |
| `1a82b8a` | F2, X1, X2 (`worker.py`, `session.py`, `main.py`) |
| `e8f479b` | F6, F3, X3, S1, X4 (`transport.py`, `history.py`, `main.py`, config/README) |
| `32eaeea` | F8 (tooling, CI, `sys.path` fix, version test) |
| `f11fcfd` | Tool returns no value; duplicate notice on tool path |
| `4a1fd87` | Config page rewrite and reorder (`_conf_schema.json`, README) |
| `9c2099e` | This file rewritten as an English engineering record |
| `c47cc0f` | Maintainer's own README edit (author note); not touched afterwards |
| last commit | Release prep for 1.0.3 (§7) |

## 2. Changes, reasoning, evidence

### 2.1 F1: the tool path must not stop the shared event

- **Where:** `main.py` `handle(self, event, text, *, stop=True)`; every `stop_event()` inside `handle()` is
  under `if stop:`; `analyze_tool.run()` passes `stop=False`. `spark_command`'s own two `stop_event()` calls
  are deliberately unconditional.
- **Why:** an `llm_tool` receives the main agent's event. `stop_event()` is sticky. AstrBot's
  `_watch_agent_stop_signal` (`astr_agent_run_util.py:356-361`) checks at the start of every step and then
  every 0.5 s. `_iter_llm_responses_with_fallback` (`tool_loop_agent_runner.py:554`) checks before each
  provider call. On abort, `_finalize_aborted_step` (`:1510-1544`) writes synthetic "Stop output." /
  "Output stopped." messages into history. The fast-failure branches of `handle()` set the flag within
  milliseconds, so the main model's follow-up was skipped or cut off.
- **Note:** the permission branch inside `handle()` is effectively unreachable on the tool path, because
  `analyze_tool` checks `allowed()` first. It is kept as a defensive check.
- **Tests:** `test_tool_fast_failure_does_not_stop_main_event`, `test_tool_success_does_not_stop_main_event`.
  `test_scheduler_does_not_stop_after_progress` still asserts the command path stops.

### 2.2 Tool returns no value (`f11fcfd`)

- **Where:** `analyze_tool` no longer yields after scheduling the background task (anchor: `# No return value`).
  The permission-denied branch still yields text.
- **Goal:** remove the second main-model request that function calling normally makes after a tool result.
- **Mechanism (4.28.2), traced end to end:**
  1. `call_local_llm_tool` (`astr_agent_tool_exec.py:809-826`): an async generator that never yields
     produces a bare `yield` (None).
  2. `_execute_local` (`:705-722`): on None, sends `event.get_result()` if it has a chain, then yields None.
  3. `_handle_function_tools` (`tool_loop_agent_runner.py:1314-1330`): `resp is None` -> `_transition_state(DONE)`
     and appends the placeholder result "The tool has no return value, or has sent the result directly to the user."
  4. `run_agent` (`astr_agent_run_util.py:314`): `if agent_runner.done(): break`. No further step, so no LLM request.
  - 4.16.0 has the same logic (`tool_loop_agent_runner.py:622-630`, `astr_agent_tool_exec.py:428-444`).
- **Stale-result risk checked:** step 2 would re-send any result left on the event. `run_agent` does
  `set_result` -> `yield` -> `clear_result` (`astr_agent_run_util.py:281-288`) before the step generator
  resumes into tool execution, so the result is empty at tool time. An unsent result from an earlier stage
  cannot exist on this path: `ProcessStage` only enters the agent when `not event._has_send_oper`
  (`process_stage/stage.py:56-66`).
- **History behaviour (version dependent):** `final_llm_resp` stays None. It is only set by
  `_complete_with_assistant_response` (`:180-183`) or on error. `internal.py::_save_to_history`:
  - >= 4.27.0: branch `llm_response is None and req.tool_calls_result` saves the turn (user message, tool call, placeholder result).
  - 4.26.x: saves only when a checkpoint id exists; otherwise drops the turn.
  - 4.16.0 (`internal.py:336-351`): early return on `not llm_response`, so the turn is dropped.
  - The bisect (4.20.0, 4.24.0, 4.26.0/.4/.8 drop; 4.27.0, 4.28.0 save) puts the boundary at 4.27.0.
    Nothing errors; the analysis is unaffected. Documented in README and CHANGELOG.
- **Consequences reviewed:**
  - Duplicate in-flight report on the tool path used to be covered by the main model's reply. Now
    `handle()` yields a notice when `not stop` (anchor `正在分析中`). Command/auto paths stay quiet
    (`test_duplicate_inflight_report_is_quiet`).
  - An empty `acknowledgement_text` means no start notice at all in tool mode. Handled in the config hint only, not in code.
  - Permission denial still returns text, so that branch still costs one follow-up main-model call. This is
    intended: the main model relays the refusal.
  - Parallel tool calls in one model response: DONE is set, but the remaining tools in that batch still run.
    The turn then ends without the model summarizing them. Accepted edge case.
  - AstrBot logs a warning per call ("spark_analyze 没有返回值…"). Expected, not an error.
  - Any text the main model emitted alongside the tool call is still delivered.
- **Alternatives rejected:**
  - `FunctionTool.is_background_task` (`astr_agent_tool_exec.py:161-182`) returns
    "Background task submitted…" to the model, so a follow-up call still happens. It is also not exposed through `filter.llm_tool`.
  - Version-gating (return text on < 4.27): extra complexity for a history-only effect.
- **Keep:** `analyze_tool` must stay an async generator. The `yield` in the denial branch is what makes it
  one, so do not remove that yield without re-checking registration.
- **Tests:** `test_tool_returns_before_analysis_completes` (now asserts `result == []` and that the plugin's
  ack is the first message sent), `test_tool_duplicate_report_gets_notice`, plus the two F1 tests.

### 2.3 F4: auto-analyze ignores non-permitted users

- **Where:** `auto_analyze`, anchor `Silent for non-permitted users`.
- **Why:** this handler is registered for `EventMessageType.ALL` with no wake condition. It used to reply
  with a denial and `stop_event()`, which swallowed the message for other plugins and the default LLM.
- The check is placed after the link/keyword match, so ordinary chat does not pay for `allowed()`.
- **Test:** `test_access_modes_and_all_entry_points` was changed (the only pre-existing assertion that
  encoded the old behaviour). Each entry point now gets a fresh `Event`, because `stopped` is sticky.

### 2.4 F5: cancellation path in `handle()`

- **Where:** the `finally` after the model wait. The loop variable was renamed to `stuck`, and the
  `raise RuntimeError(...)` was removed.
- **Why the guard still holds:** the in-flight exception propagates to
  `except Exception: if remaining or attempt+1 == len(providers): raise`, so there is still no fallback
  while a stuck model task could send a duplicate request. `remaining` is reset per attempt before the `try`.
- **Visible change:** the error type shown in chat is the original one (usually `TimeoutError`) instead of `RuntimeError`.
- **Test:** `test_cancel_with_uncancellable_model_task`. It patches `cancel_bounded` to time out after 0.05 s.

### 2.5 F2 / X1 / X2: worker and temp-dir lifecycle

- **worker.py:** `parent = os.getppid()` is captured at the **top of `run()`**, which deviates from the
  audit's snippet. Parsing can take up to 90 s; capturing after parse would record the reaper's pid if the
  host died mid-parse. The query loop exits if the directory is gone, or (non-Windows) if the parent pid
  changed. The directory check is the portable signal, because `getppid()` does not change on Windows.
- **main.py `__init__`:** `rmtree` every `root/spark-*` (anchor `for stale in`). This assumes one plugin
  instance per data dir and no live session at init (the gate allows one session). Note that AstrBot reloads
  the plugin on every config save (`dashboard/services/config_service.py:1017`), so the sweep also runs then,
  after the old instance's `terminate()`. If `terminate()` timed out (5 s), the sweep kills the old worker.
  Accepted.
- **session.py `close()`:** kill + wait inside `try`, with `rmtree(ignore_errors=True)` in `finally`.
  `ProcessLookupError` is ignored. It still raises after cleanup; `handle()` logs "cleanup failed".
  `terminate()` catches per session.
- **X2:** the error `ready.json` is written via `ready.tmp` + `replace`, the same as the success path.
- **Validation beyond unit tests:** a child process loaded a real worker and SIGKILLed itself. The worker
  then showed state `Z`: it had exited, and container PID 1 just does not reap orphans.
- **Tests:** `test_worker_exits_when_directory_removed` (real subprocess),
  `test_startup_sweeps_stale_session_dirs`, `test_close_removes_directory_when_worker_wait_fails`,
  `test_terminate_closes_every_session`. That last one counts close attempts, because set iteration order
  would otherwise let the old code pass by luck.

### 2.6 F6: link regex

- New lookahead `(?![A-Za-z0-9_-]|/[A-Za-z0-9]|\.[A-Za-z0-9])`: a trailing `.` or `/` ends the link, while
  `/x` and `.x` mean a longer, unsupported URL. `report_id()` is unchanged.
- **Test:** `test_link_boundaries` (the audit's 11-row table, `subTest` per row).

### 2.7 F3: history retention while disabled

- `History.purge()` returns 0 if the file is absent (never creates it); otherwise it opens `connect()`
  (which runs the expiry `DELETE`) and returns `total_changes`.
- `History.delete()` is gated on the file existing, not on `enabled`, so `/spark forget` works when disabled.
- `__init__` calls `purge()` synchronously (`__init__` cannot await; the DB is small), wrapped in
  `except (sqlite3.Error, OSError)`. A corrupt DB must not block plugin load.
- **Limitation:** while disabled, expiry only runs at load or reload.
- **Tests:** `test_history_retention_while_disabled`, `test_history_retention_and_forget_while_disabled`,
  `test_corrupt_history_database_does_not_block_loading`. The existing "no DB file when disabled" assertion
  in `test_missing_provider_does_not_call_main` still holds.

### 2.8 X3 + S1: history failures degrade instead of failing the analysis

- History lookup and comparison are wrapped in one `try`:
  `except (sqlite3.Error, OSError, ValueError, KeyError, TypeError, AttributeError)` (anchor
  `history comparison skipped`). `JSONDecodeError` is a `ValueError`.
- `compare()` uses a `section()` helper, so a missing section compares as `{}`. Missing fields therefore show
  up as `different_conditions`, so `comparable_sampling` errs towards False (the safe direction).
- S1: `history.save` is wrapped in `except (sqlite3.Error, OSError)` (anchor `history save failed`). The
  result is still delivered, without the history id suffix.
- **Tests:** `test_compare_tolerates_older_overview_shape`, `test_history_save_failure_keeps_result`,
  `test_history_lookup_failure_skips_comparison`.

### 2.9 Config page (`4a1fd87`, plus X4 in `e8f479b`)

- **Generation:** the file was generated by a script from the old file. The script asserted that every
  field except `description`, `hint`, `obvious_hint`, `labels` and `templates` is identical. `templates`
  was checked separately (only its display text changed). So **keys, types, defaults, sliders,
  `_special` and the default `analysis_prompt` are unchanged**. Existing user configs are unaffected.
  (Superseded for one field in review round 2: `access_mode` default is now `admin_only`, see §8.)
- **Order:** JSON key order is the UI order. Groups: model -> access -> trigger -> reply -> data -> network.
- **`labels` on `access_mode`** and **`obvious_hint` on `analysis_provider_id`:** support confirmed in the
  4.28.2 dashboard bundle (`dashboard/dist/assets/ProviderSelectMenu-*.js`:
  `options.map((z,q)=>({title:labels[q]||z,value:z}))`; `obvious_hint` renders "‼️"). The 4.16.0 wheel
  ships no dashboard dist, so this is unverified there. An unsupported renderer ignores both keys and shows raw values.
- **Facts used in hints, all verified:** `/sid` exists (`builtin_stars/builtin_commands/main.py`, in both
  versions); slider ranges match the code clamps (download 15-900 s, parse 15-300 s); cache limits are
  64 entries / 512 MiB (`cache.py`); auto-analyze keywords match the regex in `auto_analyze`; config save
  reloads the plugin, so the old "reload the plugin" instruction was dropped.
- The chat message for a missing provider now names the setting "分析模型". It keeps the substring
  `不会回落` that tests assert on.

### 2.10 F8: tooling

- Deleted `tools/release_descriptions.py`: its assert failed against the current schema, and it would have
  overwritten newer text.
- `tools/package_plugin.py` reads `^version:` from `metadata.yaml` (regex; no PyYAML). The output name
  follows it (`astrbot_plugin_spark-v1.0.3.zip` now). The zip uses an explicit file list:
  `handoff.md` and `CHANGELOG.md` are not packaged (`README.md`,
  `THIRD_PARTY.md` and `analysis_policy.md` are).
- `test_version_is_consistent`: `metadata.yaml` (leading `v` stripped) == the `@register` literal == the
  first `## X.Y.Z - ` heading in CHANGELOG. `## Unreleased` does not match that pattern by design.
- `worker.py`: `sys.path[0] = <plugin root>` replaces the script dir, so `spark_core/profile.py` no longer
  shadows stdlib `profile`. Verified by simulating script-mode `sys.path` (the old setup imported the plugin
  file; the new one resolves stdlib).
- `.github/workflows/tests.yml`: Python 3.12/3.13 matrix, `permissions: contents: read`, runs on every push and PR.

## 3. Test map

| Finding | Tests |
|---|---|
| F1 | `test_tool_fast_failure_does_not_stop_main_event`, `test_tool_success_does_not_stop_main_event` |
| Tool returns no value | `test_tool_returns_before_analysis_completes`, `test_tool_duplicate_report_gets_notice` |
| F4 | `test_access_modes_and_all_entry_points` (modified) |
| F5 | `test_cancel_with_uncancellable_model_task` |
| F2 | `test_worker_exits_when_directory_removed`, `test_startup_sweeps_stale_session_dirs` |
| X1 | `test_close_removes_directory_when_worker_wait_fails`, `test_terminate_closes_every_session` |
| F6 | `test_link_boundaries` |
| F3 | `test_history_retention_while_disabled`, `test_history_retention_and_forget_while_disabled`, `test_corrupt_history_database_does_not_block_loading` |
| X3 / S1 | `test_compare_tolerates_older_overview_shape`, `test_history_lookup_failure_skips_comparison`, `test_history_save_failure_keeps_result` |
| F8 | `test_version_is_consistent` |
| Default access (§8) | `test_default_access_is_admin_only` |

## 4. How to re-verify

```bash
pip install -r requirements.txt
python -m unittest discover -s tests        # expect 68 OK
python tools/package_plugin.py && rm -rf dist
# Re-read AstrBot sources (read-only; do not execute):
pip download astrbot==4.28.2 --no-deps -d /tmp/ab && python -m zipfile -e /tmp/ab/astrbot-4.28.2-*.whl /tmp/ab/src
```

**Not yet verified live (do this before release):**
1. Tool mode: after `spark_analyze`, the main model sends nothing further; the plugin's ack and result arrive;
   the conversation history contains the turn (>= 4.27.0).
2. Tool mode with no provider configured: only the plugin's message appears; no "Output stopped." in history.
3. The config page renders the Chinese option labels and the ‼️ marker; check on the oldest AstrBot version you support.

## 5. Open items

- F7 (a real report id in old commits): needs a maintainer decision; rewriting requires a force-push.
- Merge `v1.0.3` into `main`: no PR was created; that is the maintainer's call.
- Next version bump: update `metadata.yaml`, `@register` and the CHANGELOG heading together (enforced by
  `test_version_is_consistent`). Start the next CHANGELOG section as `## Unreleased`, which the test ignores.
- F4 options A (require @/wake) and C (per-user cooldown): not implemented.
- Audit §5 Q3 (strict child > parent rejection may refuse valid reports from other Spark builds): untouched.

## 6. Invariants for future changes

- Log only through `astrbot.api.logger`. `tests/test_logging_scope.py` enforces this.
- Chat shows raw text only for `ProfileError` / `LoadTimeout`; any other exception shows `type(exc).__name__`
  only, because messages can contain local paths.
- Do not relax download/parse limits: official host only, no redirects, `trust_env=False`, 16 MiB
  compressed / 128 MiB decoded, node/depth limits, worker `RLIMIT_AS`.
- Never call `stop_event()` on the tool path. Never return text from `analyze_tool`'s success path without
  re-reading §2.2, since doing so brings back the extra main-model call.
- Config schema: keys and types are a compatibility surface for existing installs. A default change only reaches
  new installs (AstrBot fills missing keys only), so treat it as a product decision and note it in CHANGELOG.
- Default access is `admin_only`. The code fallback (`config.get('access_mode', 'admin_only')`, 3 places in
  `main.py`) must match the schema default; `test_default_access_is_admin_only` checks both.

## 7. Release prep for 1.0.3

Requested by the maintainer after reviewing the branch. Done on `claude/keen-bohr-d01us1`; branch `v1.0.3` was
then created at the same commit (the naming follows the existing `v1.0.2` release branch).

- **Version** 1.0.2 -> 1.0.3 in the three hand-edited places: `metadata.yaml` (`version: v1.0.3`), the
  `@register(...)` literal in `main.py`, and the CHANGELOG heading (`## Unreleased` -> `## 1.0.3 - 2026-10-11`;
  the date is the maintainer's local date, KST). `tools/package_plugin.py` derives the zip name from `metadata.yaml`.
  The CHANGELOG line "version still 1.0.2" under 未改动 was replaced by a "version bumped" line under 变更.
- **Files kept:** plugin runtime files (`main.py`, `spark_core/`, `metadata.yaml`, `_conf_schema.json`,
  `requirements.txt`, `analysis_policy.md`, `reply_prompt.txt`, `logo.png`), docs/legal (`README.md`,
  `CHANGELOG.md`, `LICENSE`, `THIRD_PARTY.md`), dev tooling (`tools/`, `.github/workflows/tests.yml`,
  `.gitignore`), `tests/` and this file. **Removed:** `AUDIT_FINDINGS.md` and `FIX_PLAN.md` (process
  documents, still in git history; see §1). `tools/` and CI were judged necessary: packaging, protobuf
  regeneration and the test gate depend on them.
- **AI statement:** README and CHANGELOG now name Claude Opus 5.5 (via Claude Code) next to the existing
  Codex / Sonnet 5.5 entries, with a badge in the same style, plus a one-line note at the top of the 1.0.3
  CHANGELOG section that points here. The model was confirmed from the session metadata (configured and
  last-served model both Opus 5.5) before writing it. The maintainer's own "作者注" block in README
  (`c47cc0f`) was left verbatim.

## 8. Review round 2 (after release prep)

Maintainer review of the branch raised four items. Items 3 and 4 were implemented; items 1 and 2 were only
proposed, pending the maintainer's choice. Branch `v1.0.3` still points at `c4c1311` and does not have
this round.

- **Item 4, default access (implemented):** the `access_mode` default changed from `all` to `admin_only`
  (maintainer decision, not open for discussion). Changed in the schema default, the hint, the 3 code
  fallbacks in `main.py`, and README.
  - Existing installs keep their saved value. Verified in `AstrBotConfig.check_config_integrity` (4.28.2 and
    4.16.0): only missing keys get defaults.
  - Test double: `Event.admin` now defaults to `True`. Under the new default, an admin is the minimal working
    user, so tests unrelated to permissions keep passing unchanged. Permission tests set `admin = False`
    explicitly. Before the code change, the full suite was run with the new test double: only the new default
    test failed.
  - `auto_analyze` stays on by default; with `admin_only` it only fires for admins, and is silent for others (F4).
- **Item 3, CHANGELOG (implemented):** the 1.0.3 section was cut from ~7.8 KB to ~2.8 KB of user-facing
  bullets. Nothing that was dropped is lost: AstrBot internals, version bisect, hot-reload note and per-test
  lists all live in §2-§3 of this file.
- **Item 1, `worker.py` `sys.path[0] = root` is unconditional (proposed):** with `PYTHONSAFEPATH=1` (or `-I`),
  `sys.path[0]` is not the script dir but a `PYTHONPATH` entry or the stdlib zip, and gets overwritten.
- **Item 2, `/spark forget` has no error guard, and sqlite has no explicit timeout (proposed):** `History.delete()`
  raises `DatabaseError` on a corrupt file and nothing catches it in `spark_command`. `purge()` runs
  synchronously in `__init__` with sqlite's default 5 s busy timeout, which can stall plugin load on a locked DB.
  - Note for whoever implements it: AstrBot awaits `star_cls.initialize()` inside the plugin-load `try`
    (`star_manager.py:1420` in 4.28.2, `:656` in 4.16.0), so an exception escaping `initialize()` fails the
    plugin load.
