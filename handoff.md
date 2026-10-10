# Engineering handoff: `claude/keen-bohr-d01us1`

Audience: engineers or LLM agents debugging or reviewing this branch. This is a record of what was changed,
why, what evidence the decisions rest on, and where the risk is. It is not user documentation (see README /
CHANGELOG for that). `checklist.md` is the plain-language Chinese companion for users and repository reviewers
(§14); keep the two consistent when either changes.

## 1. Context

| Item | Value |
|---|---|
| Base | `main` @ `e15f67f` (v1.0.2) |
| Inputs | `AUDIT_FINDINGS.md` (finding IDs F1-F8, X1-X4), `FIX_PLAN.md` (batching, decisions D1-D5, execution status). Both were removed from the tree at release; read them with `git show c47cc0f:AUDIT_FINDINGS.md` / `git show c47cc0f:FIX_PLAN.md`. |
| Decisions applied | D1=A (purge history while disabled), D2=B (auto-analyze silent for non-permitted users), D4=delete stale tool, D5=add CI. D3/F7 untouched. |
| Extra finding | S1: a failed history write discarded a finished analysis (reproduced before fixing) |
| Version | 1.0.5 (§13), merged into `main` via PR #3 (`525634f`, a merge commit of `v1.0.5`, which carries `v1.0.4`). 1.0.3 (§7) was merged via PR #2 (`9a9bad2`). Release branches: `v1.0.3`, `v1.0.4`, `v1.0.5`. |
| Tests | 51 -> 67 (1.0.3) -> 71 (1.0.4) -> 99 (1.0.5, §10-§12), all passing locally (Python 3.13) and in CI (3.12, 3.13) |
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
| `c4c1311` | Release prep for 1.0.3 (§7); merged into `main` as PR #2 |
| `33b147c` | Review round 2 items 3 and 4: default `admin_only`, trimmed 1.0.3 CHANGELOG (§8) |
| `8090d18` | Review round 2 item 2: `/spark forget` guard, startup purge in `initialize()` (§8) |
| `1106402` | Release prep for 1.0.4 (§9). Branch `v1.0.4` holds the same tree as one commit on `main`. |
| `e07adbe` | Prompt rewrite and model-input changes (§10) |
| `af5fd32` | Review item 1: worker `sys.path` under safe-path modes (§11) |
| `b9f0466` | Reorientation: quick triage, multi-platform wait rule, oversized-report messages (§12) |
| `170f6d2` | Release prep for 1.0.5 (§13). Branch `v1.0.5` holds the same tree as one commit on `v1.0.4`. |
| `39331aa` | Merge of `main` (PR #3) into the work branch; tree unchanged (§13) |
| last commit | `checklist.md` split (§14); review round 3 findings verified and proposals recorded, not implemented (§15) |

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
- The startup purge runs in `initialize()` (originally in `__init__`; moved in review round 2, see §8):
  `await asyncio.to_thread(self.history.purge, 1)` inside `except Exception`. A corrupt or locked DB must
  never fail or stall plugin load.
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
  follows it (for example `astrbot_plugin_spark-v1.0.4.zip`). The zip uses an explicit file list:
  `handoff.md` and `CHANGELOG.md` are not packaged (`README.md`,
  `THIRD_PARTY.md`, `analysis_policy.md` and, since §10, `diagnosis_guide.md` are).
- `test_version_is_consistent`: `metadata.yaml` (leading `v` stripped) == the `@register` literal == the
  first `## X.Y.Z - ` heading in CHANGELOG. `## Unreleased` does not match that pattern by design.
- `worker.py`: the plugin root goes first on `sys.path` and the script dir is dropped, so `spark_core/profile.py`
  no longer shadows stdlib `profile`. Verified by simulating script-mode `sys.path` (the old setup imported the
  plugin file; the new one resolves stdlib). Superseded by `import_path()`, which only replaces entry 0 when it is
  the script dir (§11).
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
| Review item 1 (§11) | `test_script_directory_is_replaced`, `test_safe_path_entries_are_kept`, `test_worker_runs_with_dependencies_only_on_pythonpath` |
| Review item 2 (§8) | `test_locked_history_does_not_stall_loading`, `test_forget_reports_unreadable_history`, `test_forget_reports_busy_history` |
| X3 / S1 | `test_compare_tolerates_older_overview_shape`, `test_history_lookup_failure_skips_comparison`, `test_history_save_failure_keeps_result` |
| F8 | `test_version_is_consistent` |
| Default access (§8) | `test_default_access_is_admin_only` |
| Triage, prompts, legacy migration (§10, §12) | all of `tests/test_briefing.py`; legacy and payload tests in `tests/test_plugin.py` |
| Report data (§10, §12) | `test_core.py`: world statistics, window counts, `test_wait_time_counts_outermost_wait_frames`, `test_platform_brand_is_reported` |
| Oversized-report message (§12) | `test_oversized_report_message_explains_the_remedy` |

## 4. How to re-verify

```bash
pip install -r requirements.txt
python -m unittest discover -s tests        # expect 99 OK
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
- Review round 3 (§15): five findings verified; proposals await the maintainer's decision.
- AstrBot desktop client (`ASTRBOT_DESKTOP_CLIENT=1`): out of scope by maintainer decision. See §11 for the
  two source-read risks (worker cannot see `data/site-packages`; `sys.executable` may not be Python when frozen).
- Prompt rewrite phase 2 (§10): per-category tick breakdown. Shelved by maintainer decision (no new features
  for now, §12). A scratch prototype existed and was never committed.
- The prompts (§10, §12) have not been evaluated against a live model yet. `tools/dump_prompt.py` printed the
  exact model input; it was removed at 1.0.5 release prep and can be restored with
  `git show b9f0466:tools/dump_prompt.py`.
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
- Do not classify waits (or anything else) by method names that only some platforms or versions use.
  Between-tick idle versus in-tick wait comes from MSPT in `triage()` (§12).
- Load thresholds live only in `spark_core/briefing.py`. Prompt files refer to `triage.load` / `triage.spikes` /
  `triage.wait_kind`
  and must not restate the numbers (CHANGELOG may, as a record).
- Every file `main.py` reads with `with_name(...)` must be in `tools/package_plugin.py`'s list
  (`test_prompt_files_are_packaged`); otherwise the release zip crashes at load.
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

Maintainer review of the branch raised four items. Items 3, 4 and then 2 were implemented; item 1 is
proposed only, pending the maintainer's go-ahead. `v1.0.3` had already been merged (PR #2) when this round
started, so this round ships as 1.0.4 (§9).

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
- **Item 1, `worker.py` `sys.path[0] = root` is unconditional (implemented later, §11):** with `PYTHONSAFEPATH=1` (or `-I`),
  `sys.path[0]` is not the script dir but a `PYTHONPATH` entry or the stdlib zip, and gets overwritten.
- **Item 2, `/spark forget` error guard and sqlite timeouts (implemented):**
  - **Reproduced first:** `/spark forget` on a corrupt file raised an uncaught `DatabaseError`. With another
    connection holding `BEGIN EXCLUSIVE`, plugin construction blocked for 5.01 s (sqlite's default busy
    timeout), and asyncio reported the event loop blocked for 5.015 s.
  - **Startup purge** moved from `__init__` to `async def initialize()`, run via `asyncio.to_thread` with a 1 s
    lock timeout, inside `except Exception` with a warning log.
    - Constraints: AstrBot awaits `star_cls.initialize()` inside the plugin-load `try` (`star_manager.py:1420`
      in 4.28.2, `:656` in 4.16.0), so anything escaping fails the plugin load. `Star.initialize` is an empty
      no-op in both versions, so there is no `super()` call to make.
    - Why 1 s: this plugin is the only writer of `history.sqlite3`. The realistic contention is AstrBot's
      reload on every config save, where the old instance may be finishing a single-row write (milliseconds).
      A lock held longer than 1 s means an external holder, where waiting does not help. Plugins load
      sequentially, so each second here delays every later plugin. A skipped purge is retried on the next load.
  - **`History.connect(timeout=5)` / `purge(timeout=5)`:** the timeout is now an explicit parameter. All
    other callers (`list`, `save`, `delete`) keep 5 s, the sqlite default. They run in worker threads, so the
    loop is not blocked, and the user is waiting for the result.
  - **`/spark forget`** catches `(sqlite3.Error, OSError)`, logs only the type name, and replies with one of
    two messages. Both state that nothing was deleted, which is accurate because the delete is a single transaction:
    - `OperationalError` whose message contains `locked` -> "历史数据库正忙，记录未清除，请稍后再试。"
      Verified once against a real lock (sqlite's text is "database is locked").
    - Anything else (corrupt file, disk or permission error) -> "历史数据库无法读取，记录未清除，请联系管理员检查历史数据库文件。"
    - `str(exc)` is only used to classify, never shown in chat (the path-leak rule still holds).
  - The report cache DB needed no change: it does not touch the DB at init, and it only runs in worker
    threads (30 s timeout).
  - **Proposal for item 1 (implemented in §11):** replace `sys.path[0]` only when it resolves to the script
    directory, otherwise `insert(0, root)`. Reproduced in a scratch copy: under `PYTHONSAFEPATH=1` the
    current code overwrote a `PYTHONPATH` entry; the conditional version kept it, and resolved stdlib
    `profile` in both modes. Suggested tests: put the decision in a function that is only called when the
    worker runs as a script, so importing it does not touch the test process's `sys.path`; unit-test both
    branches; add one subprocess smoke test with `PYTHONSAFEPATH=1`.

## 9. Release prep for 1.0.4

Requested by the maintainer after review round 2. `main` already contained 1.0.3 (`9a9bad2`, PR #2), so 1.0.4
is exactly the round-2 work (`33b147c`, `8090d18`) plus this prep.

- **Version** 1.0.3 -> 1.0.4 in `metadata.yaml`, the `@register(...)` literal and a new CHANGELOG heading
  `## 1.0.4 - 2026-10-11` (maintainer's local date, KST). `test_version_is_consistent` checks all three.
- **CHANGELOG split:** round-2 items had been appended to the 1.0.3 section while 1.0.3 was still unmerged.
  They were moved into the 1.0.4 section: default `admin_only`, the `/spark forget` guard, and the startup
  stall. The 1.0.3 test count was restored to 51 -> 67, the count at `c4c1311`; 1.0.4 records 67 -> 71.
  The 1.0.3 section stays in its trimmed, user-facing form, as review item 3 asked.
- **AI statement** (README and CHANGELOG header) now says Opus 5.5 worked on 1.0.3 and 1.0.4.
- **Release branch `v1.0.4`:** created from `origin/main` (`9a9bad2`) with a single commit whose tree is
  identical to the tip of `claude/keen-bohr-d01us1` (built with `git read-tree -u --reset`; verified with an
  empty `git diff`). The PR diff therefore contains only the 1.0.4 changes. The per-change history stays on
  `claude/keen-bohr-d01us1` and in this file. The file set is unchanged from 1.0.3: no files were added or removed.
- **Note for the next round:** once `v1.0.4` is merged, `claude/keen-bohr-d01us1` is not an ancestor of
  `main` (the release commit is a squash). Restart the work branch from `main` before new work, so later
  diffs do not repeat these commits.

## 10. Prompt rewrite and model-input changes (1.0.5)

Maintainer feedback: replies analysed too much, buried the conclusion and were verbose. Requested: a built-in
system prompt that helps the model reach a conclusion faster. Phase 1 of the agreed plan is implemented
(proposal items A, B1-B3, B5, C, D); phase 2 (B4, per-category tick breakdown) was later shelved (§12).

**Evidence the redesign rests on.** A maintainer-supplied report and the reply it produced, reproduced in a
scratch directory with the plugin's own loader. The report: NeoForge 1.21.1, 33.5 s, 3 players, TPS ~20,
MSPT median 11.6 / max 1806 ms. Its link is deliberately not recorded anywhere in the repo (same concern as F7).
- **Reply template:** it forced four sections and 500-900 characters, so a healthy report still got three
  "main problems". About 20 "不能…" rules in the policy turned into 6 hedges in the reply.
- **No verdict rule:** "60% waiting" was presented as problem ①, although it was idle capacity. The real
  finding (an occasional 1.8 s spike that this short sample could not explain) was buried.
- **Data dropped by the parser:** the report carries `PlatformStatistics.world` (316 entities, types, 7
  dimensions, per-chunk counts with coordinates), but the parser never read it. The model then claimed the
  report had no entity data.
- **Payload noise:** `vm_args` was 6.5 KB of the 15 KB payload.
- **Weak precomputed hotspots:** the evidence pack ranks by self time, so apart from the park it surfaced
  1% leaf methods (`Iterators.hasNext`). Readable categories cost the model extra queries.

**Prompt layers** (`system_prompt()` in `briefing.py`; order policy -> guide -> reply):
- `analysis_policy.md` (built-in, rewritten): role and data trust, a 4-step procedure ("read triage first,
  do not re-judge it", "usually 0-2 queries"), and the 8 rules that prevent wrong conclusions. Shorter than
  before, with the hedging rules consolidated.
- `diagnosis_guide.md` (built-in, new): how to read each triage label, common sources -> first action, and
  follow-up spark commands. Every command and flag was checked against spark's Command-Usage docs
  (`--only-ticks-over`, `--timeout`, `--thread *`, `--alloc`, `tickmonitor --threshold-tick`, `gc`,
  `gcmonitor`). It contains no triage threshold numbers.
- `reply_prompt.txt` / `analysis_prompt` default (user-editable, rewritten): no platform or audience. The
  first sentence is the verdict. A healthy or mildly loaded report without spikes gets 3-5 sentences.
  Problems get <= ~800 characters with a fixed structure: 结论 / 主要原因 (<= 3) / 建议 (<= 3) /
  采样限制. (Superseded in §12 by 现状 / 疑似问题点 / 建议先做的检查.) `test_shipped_prompt_files_agree` keeps the file and the schema default identical and checks
  that "QQ", "服主" and "群" are absent.

**Load classes** (`briefing.py`, maintainer-set; the only place these numbers live):
- sustained: TPS < 15 or MSPT median > 70 ms
- mild: TPS < 19.5 or median > 40 ms. Common on multiplayer servers because vanilla ticks every dimension on
  one thread, so the guide says it is normally not urgent.
- spike: max > 500 ms and > 5 × median

`triage()` also derives `spike_ratio`, the idle and other-wait percentages, `chunks_per_player`, entity count
and sample length, plus a Chinese `summary` sentence. `model_overview()` puts `triage` first in the overview.

**Data changes** (`profile.py`; worker side, bounded):
- `world_statistics()` -> `overview.world`: total entities, top-10 types, up to 12 dimensions, the 5 busiest
  chunks with chunk and centre block coordinates and top-3 types. Also performance game rules
  (`randomTickSpeed`, `doMobSpawning`, `maxEntityCramming`, `spawnChunkRadius`), only where a world differs
  from the default. `None` when the report has no world stats. About 1.6 KB on the sample.
- `window_health` gains `entities` and `tile_entities` via `present_count()`, which maps Spark's -1
  ("could not count") to `None`.
- `waiting()` adds `idle_between_ticks_ms` and `other_wait_ms` per evidence thread. It sums the inclusive time
  of the outermost JDK wait frame (park, sleep, wait). The time counts as idle when an ancestor is
  `MinecraftServer.waitUntilNextTick` / `waitForTasks`; the sample's park path went through `waitForTasks`.
  - Superseded in §12: the split was removed and replaced by an MSPT rule.
  - Limitation: it only works with readable (Mojang) method names. On older Forge (SRG ids) between-tick idle
    lands in `other_wait_ms`, and the triage summary then says the two cannot be told apart instead of calling it a stall.
- `model_overview()` replaces `vm_args` with `{key_flags, other_flag_count, note}` unless the question
  matches `JVM_QUESTION`. The history row and the problem label still use the full overview and the old regex.

**Measured on the sample:** user message 15 KB -> 11.3 KB (including +1.6 KB world and the triage block);
system prompt 4.2 KB -> 7.1 KB (the guide). Triage: load `none`, spikes `true` (155.7×), idle 60.4%, other
wait 0%, about 2398 chunks per player, 316 entities.

**Legacy prompt migration (D):** `LEGACY_REPLY_DIGESTS` holds SHA-256 hashes of whitespace-stripped shipped
defaults. Every historical `analysis_prompt` default and `reply_prompt.txt` in git normalises to one hash.
- `reply_requirements()` treats an empty or legacy value as "use the current default" at analysis time, so
  behaviour is correct even if saving fails.
- `initialize()` also rewrites and `save_config()`s the legacy value, so the config page shows the text
  actually used. `save_config` exists in 4.16.0 and 4.28.2; the call is guarded with `getattr`. Custom text is
  never touched.
- When the default changes again, add the hash of the previous default to `LEGACY_REPLY_DIGESTS`.

**Other:**
- `spark_query` got per-parameter descriptions and "do not call when triage/evidence/world suffice".
- The success log adds `served_model` from `response.raw_completion.model` (the `LLMResponse.raw_completion`
  field exists in 4.16.0 and 4.28.2; read with `getattr`, so other providers log `unknown`). It is the name the
  provider reports, so a provider that misreports cannot be detected this way.
- The history note shown in chat no longer exposes the record id (it is still logged). It repeats the
  `server=` / `problem=` tags the user typed, because `compare` matches records by tags.
- `tools/dump_prompt.py <url> [question]` printed the exact system prompt and user message (removed at 1.0.5
  release prep, §13).

**Tests (+22):** `tests/test_briefing.py` covers the load classes and spike boundaries, summary wording,
unclassified waiting, vm_args trimming and the JVM-question exception, payload rules, legacy detection
(including CRLF and whitespace variants), prompt order, schema/file agreement, and the packaging list.
`test_core.py` covers world statistics, window counts and the wait split. `test_plugin.py` covers legacy
replacement (saved and unsaved), keeping custom text, the served-model log, triage-first payload, tool
parameter descriptions and the new history note. Each new test failed on the code before its change.

## 11. Review item 1: worker `sys.path` under safe-path modes (1.0.5)

- **Re-verified before implementing:** the real worker was run in a package-free venv, so protobuf was reachable
  only through `PYTHONPATH`.
  - Normal start: the current code worked.
  - With `PYTHONSAFEPATH=1`: it died with `ModuleNotFoundError: No module named 'google'` and wrote no
    `ready.json`. Users see "解析worker异常退出".
  - The conditional version worked in both modes.
  - (A first attempt used the system Python, which turned out to have protobuf installed, so it proved
    nothing and was discarded.)
- **Fix:** `import_path(entries, here)` replaces entry 0 only when its realpath is the script directory;
  otherwise it prepends the plugin root and keeps every entry. It is applied only under
  `if __name__ == '__main__'`, so importing `spark_core.worker` (in tests) leaves the caller's `sys.path`
  alone; before this change, importing the module rewrote it.
- **Tests:** two pure tests for the two branches, plus `test_worker_runs_with_dependencies_only_on_pythonpath`,
  which runs the worker as `python -S -P` with dependencies only on `PYTHONPATH`. That reproduces the failure
  in CI without any special environment, and failed on the old code.
- **Who could hit it:** AstrBot started with `PYTHONSAFEPATH` set and dependencies on `PYTHONPATH`, or a Python
  whose `._pth` file puts the stdlib zip first (Windows embeddable; inferred, not tested). Standard pip/uv/Docker
  installs were never affected.
- **Out of scope (maintainer decision): AstrBot desktop client.** Read from AstrBot 4.28.2, not tested:
  - With `ASTRBOT_DESKTOP_CLIENT=1`, plugin requirements are installed with `pip --target data/site-packages`
    and added to `sys.path` only inside the AstrBot process (`pip_installer.py`), so the worker subprocess
    would not see protobuf.
  - AstrBot also handles `sys.frozen` (`process_restart.py`). If the desktop build is frozen,
    `sys.executable` is the app itself and the worker cannot start.
  - Neither is addressed by this fix.

## 12. Reorientation: quick triage, multi-platform (1.0.5)

**Maintainer direction** (given after phase 1):
- The plugin is for quick triage: describe the current state and point at suspected problems. Root-cause
  analysis is for experienced people, and the reply says so in one closing sentence.
- It must hold across server platforms and versions. No rule tuned to the one sample at hand.
- Plugin servers (Paper, Folia, Leaf) are supported, not only modern mod servers.
- No new features for now. Phase 2 (per-category breakdown) is shelved.

**Evidence.** Three more maintainer-supplied reports were loaded in scratch with the plugin's own code. Their
links are not recorded (same concern as F7). Behaviour of the §10 code:

| Platform | What happened |
|---|---|
| Folia 26.1.2 | Loaded. Spark reports `name=Bukkit`, `brand=Folia`; the parser ignored `brand`, so the model could not tell Folia from Paper. The tick thread is `Folia Region Scheduler Thread (x5)`. 34.8% of its samples park in the region scheduler's idle loop (`EDFSchedulerThreadPool$TickThreadRunner.run`), not under `waitForTasks`, so idle capacity was reported as "other wait". |
| Forge 47.4.26 (MC 1.20.1) | Loaded. Runtime method names are SRG ids (`m_12345_`), so `waitUntilNextTick` never matched and all 74.8% of waiting was "other". Most of it is a multithreading mod's worker wait, injected through a mixin into the server task loop: a real stall inside the tick. |
| Leaf 1.21.8 | Refused: 149.8 MiB decoded (one thread, 61 windows, about 17 h of sampling). The chat message was only "报告超限". |
| NeoForge 1.21.1 (the §10 sample) | Correct before and after. |

The name-based idle rule therefore failed on two of four platforms, in opposite directions.

**Wait rule (replaces the name-based split):**
- `Profile.waiting()` returns one number: the sampled ms under the outermost JDK wait frame (park, sleep,
  wait). Evidence threads carry it as `wait_ms`. `TICK_IDLE_FRAMES`, `idle_between_ticks_ms` and
  `other_wait_ms` are gone.
- `briefing.triage()` labels it `wait_kind` from the MSPT median:
  - below 40 ms -> `between_ticks`: the tick ends well inside the 50 ms budget, so the thread sleeps until
    the next one.
  - 50 ms or more -> `in_tick`: there is no time left between ticks, so waits happen inside ticks.
  - 40-50 ms, or no MSPT -> `unknown`: the model must not call the wait idle or a cause.
- TPS is not used for this, because spikes alone can pull TPS down while the median tick is short. Folia
  shows this: TPS 19.0 (load `mild`) with a 7.2 ms median.
- The rule relies only on spark's own MSPT statistics, which every platform reports. Constants:
  `TICK_BUDGET_MS = 50`, `IDLE_WAIT_BELOW_MSPT = 40` (the same 40 as the mild-load MSPT threshold).
- For `in_tick`, the guide tells the model to read the wait hotspot's call path and suggest
  `/spark profiler start --thread *`, since the real work is on another thread.
- Results: NeoForge `between_ticks` (60.4%), Folia `between_ticks` (36.3%), Forge `in_tick` (74.8%).

**Platform field:** `overview.platform.brand` (truncated at 120, included in `platform_truncated`). It is an
empty string when the report has none, as with other platform strings; the Forge sample has none.

**Oversized reports:** the limits are unchanged (16 MiB compressed, 128 MiB decoded, 1,000,000 nodes). Each
`ProfileError` now says that the profile was probably too long and gives `/spark profiler start --timeout 300`.
This covers `transport.py` (both size checks), `worker.py` (decoded file) and `profile.py` (node limit).
All stay under the worker's 200-character error cap. Checked by running the real worker on the Leaf file.

**Prompt files:**
- `analysis_policy.md`:
  - Role covers mod and plugin servers; task is "快速理清现状、指出疑似问题点和应先做的检查，不追究根因".
  - Step 2 checks `platform` name, brand and minecraft first.
  - Rule 2 defers wait interpretation to `triage.wait_kind`.
  - Rule 8: advice must fit the report's platform (no Paper settings on a mod server and vice versa). Never
    advise uninstalling mods or plugins or deleting worlds.
- `diagnosis_guide.md`:
  - Readings for `wait_kind`.
  - New "平台差异" section: SRG names on older Forge (judge by class, source and path, do not guess the
    method's meaning); `source` is the plugin name on plugin servers; Paper config direction only, without
    invented setting names; Folia region threads.
  - Script and event-bus sources now include Skript and plugin listeners.
  - The `--timeout` line warns that multi-hour profiles can exceed the size limit.
- `reply_prompt.txt` and the schema default:
  - Structure is 现状 / 疑似问题点 (<= 3) / 建议先做的检查 (<= 3), plus one closing sentence pointing to an
    experienced server administrator or developer.
  - Wording avoids "服主" (no audience restriction).
  - Healthy reports still get 3-5 sentences; problems get <= ~800 characters.
- `LEGACY_REPLY_DIGESTS` gains the hash of the unreleased §10 default, so a config saved from a branch build
  follows the new default.

**Tests (+3, 99 total):**
- `test_platform_brand_is_reported`.
- `test_wait_time_counts_outermost_wait_frames` replaces the split test. It covers a nested
  `Object.wait` -> park counted once.
- `test_wait_kind_follows_the_tick_budget` replaces the unclassified-wait test. It covers both boundaries,
  the TPS-dip case and missing MSPT.
- `test_built_in_rules_cover_plugin_servers` checks that the prompt files mention plugin servers and no longer
  mention the removed fields.
- `test_oversized_report_message_explains_the_remedy`.
- `test_shipped_prompt_files_agree` now checks the new section names and the pointer sentence.

**Not done (maintainer decision):**
- No category breakdown, and no new commands or settings.
- Leaf-size reports are still refused rather than partially analysed.
- None of these prompts has been evaluated against a live model yet (see §5).

## 13. Release prep for 1.0.5

Requested by the maintainer after §12: keep only what the merge needs, fill in CHANGELOG and this file, and
create branch `v1.0.5`. 1.0.5 is §10-§12 (`e07adbe`, `af5fd32`, `b9f0466`) plus this prep.

- **Version** 1.0.4 -> 1.0.5 in `metadata.yaml`, the `@register(...)` literal and the CHANGELOG heading
  (`## Unreleased` -> `## 1.0.5 - 2026-10-11`, the maintainer's local date, KST). `test_version_is_consistent`
  checks all three.
- **CHANGELOG:** the section is user-facing. A "版本号升至 1.0.5" line replaces the `dump_prompt.py` line, and
  an 未改动 subsection records that dependencies and limits are unchanged and that only the
  `analysis_prompt` default changed (old defaults migrate automatically, custom text is kept).
- **AI statement** (README and CHANGELOG header) now says Opus 5.5 worked on 1.0.3 to 1.0.5, including the
  prompt rewrite.
- **Files:**
  - Removed `tools/dump_prompt.py`. It was new in this cycle and only a development aid; nothing imports it
    and packaging does not need it. Restore it with `git show b9f0466:tools/dump_prompt.py`.
  - Added since 1.0.4, all needed: `diagnosis_guide.md` (read at load, packaged), `spark_core/briefing.py`
    (runtime) and `tests/test_briefing.py`.
  - `tools/build_schema.py` and `tools/check_sample.py` predate this work and were kept, as decided in §7.
- **Release branch `v1.0.5`:** `v1.0.4` was still unmerged, so `v1.0.5` was created from `origin/v1.0.4`
  (`2d36ac2`). It has a single commit whose tree is identical to the tip of `claude/keen-bohr-d01us1` (built
  with `git read-tree -u --reset`; verified with an empty `git diff`). The commit's own diff therefore contains
  only the 1.0.5 changes.
  - If `v1.0.4` is merged with a merge commit first, a PR from `v1.0.5` shows only that one commit.
  - If `v1.0.4` is squash-merged instead, the merge base stays at `9a9bad2`, so the `v1.0.5` PR would list the
    1.0.4 changes again (GitHub diffs against the merge base). Merging `main` into `v1.0.5` first fixes that
    without conflicts: both sides made identical changes from the same base.
- **Next round:** restart the work branch from `main` once both release branches are merged (see §9).
  - Done differently after PR #3 merged `v1.0.5` (with `v1.0.4`) into `main`: `main` was merged into the work
    branch (`39331aa`) instead of resetting it. A reset would have dropped the only refs to `33b147c`, `8090d18`,
    `1106402`, `e07adbe`, `af5fd32`, `b9f0466` and `170f6d2`, which this file cites (including
    `git show b9f0466:tools/dump_prompt.py`). The trees were identical, so the merge changed no file, and the
    next release branch can again be cut from `main` with one commit whose tree equals the work-branch tip.

## 14. Documentation split: `checklist.md`

Maintainer request: `handoff.md` stays the machine-oriented record of what was done each time; a new
`checklist.md` explains the work to users and repository reviewers in plain language.

- `checklist.md` (Chinese, like README and CHANGELOG): role of each record file; current state; how to run the
  checks; the manual pre-release checks as a checkbox list (same items as §4 plus the §10/§12 prompt and
  platform checks); the boundaries the plugin keeps (§6 in user terms); per-version "why / what / how to check"
  for 1.0.3-1.0.5; known limitations (§5).
- It cites no real report link or id (F7). Compatibility numbers come from §12; the memory figure comes from §15.
- It is a repository document only: not in `tools/package_plugin.py`'s list, so not shipped in the zip.
- CHANGELOG gets an `## Unreleased` line for it. The maintainer's README author note is left verbatim.
- When work changes behaviour, update `checklist.md` in the same commit: the per-version section, and the manual
  checks or limitations if they move.

## 15. Review round 3 (verified; proposals only, not implemented)

The maintainer passed on five review findings: check them, propose fixes, implement after their review. All
five reproduce on `39331aa`. The proposals add no features. Suggested order: R3-5, R3-3, R3-1, R3-2, then R3-4
(optional).

**R3-1: `compare` can do nothing without saying so** (`handle()`, anchor `history comparison skipped`).
- Verified:
  - A lookup happens only when `server` is non-empty: the typed `server=`, else `runtime.server_hint.tag`, which
    is empty when the report has no OS or CPU data.
  - `History.list` returns `[]` while history is disabled.
  - Without `problem=`, the tag is `JVM与GC` when the text matches `JVM|GC|启动参数|堆内存`, else `性能分析`.
    "掉TPS" then "JVM 调整后 compare" therefore look up different tags and find nothing.
  - In every such case `comparison` is `None`, the model gets `history_comparison: null`, and the user is not
    told. A history read error is only logged.
  - `'compare' in text.lower()` also matches "comparison", "compared", or a report id containing the letters.
- Proposal:
  - Detect the keyword as a standalone ASCII word: `(?<![A-Za-z0-9_])compare(?![A-Za-z0-9_])`, case-insensitive.
    Python's `\b` does not work here, because CJK characters count as word characters ("调整后compare").
  - When `compare` is asked and no `problem=` was typed, match on owner and server only (`History.list(...,
    problem='')`, already supported) and take the latest row. A typed `problem=` keeps exact matching. Saved rows
    keep the auto tag.
  - When `compare` is asked but no comparison happens, give the reason in one chat line after the result, and
    pass the same reason to the model so the reply does not contradict it. Reasons: history disabled; no earlier
    record for this sender and server tag within 30 days; no server tag (add `server=`); history unreadable.
  - Tests: no false trigger on "comparison"; differing auto problem tags still compare; each skip reason reaches
    the chat.

**R3-2: parse memory near the size limit** (`worker.py`, `RLIMIT_AS` 1.5 GiB, not on Windows).
- Verified that the cap is skipped on `win32`. Measured in scratch (`resource.ru_maxrss`, same `Profile` +
  `overview()` + `evidence_pack()` path as the worker; the size check was bypassed for the Leaf file):

| Input | Shape | Peak RSS | Time | Under the 1.5 GiB cap |
|---|---|---|---|---|
| Leaf report, 149.8 MiB (real) | 1 thread, ~254k nodes, 61 windows | 449 MiB | 22-25 s | parses |
| Synthetic, 125.5 MiB | 999,000 nodes, 2 windows, long names | 781 MiB | 12 s | parses |
| Synthetic, 134.1 MiB | 999,000 nodes, 12 windows | 815 MiB | 25.5 s | parses |

- So the node limit, not the byte limit, drives memory, and the cap leaves about 1.9× headroom. Parse time can
  exceed a parse timeout set to its 15 s minimum; the default 90 s is enough.
- Failure modes under a tighter cap, same 125.5 MiB file:
  - 300 MiB: protobuf raises `DecodeError`. That is indistinguishable from a corrupt report, and the session also
    drops the cached download.
  - 500 MiB: the interpreter died with no output; users would see "解析worker异常退出".
  - 700 MiB: `MemoryError`; chat shows "报告解析失败（MemoryError）".
- Proposal: in `worker.py`, map `MemoryError` to a plugin-authored message (memory ran out while parsing, the
  profile was probably too long, `--timeout 300`). Add a unit test that patches `Profile` to raise it.
  - The crash and `DecodeError` cases cannot be told apart reliably, so leave them as they are.
  - No Windows cap: a Job Object via `ctypes` is real complexity for a measured peak under 1 GiB; document it
    instead (done in `checklist.md`).
  - No near-limit test in CI: it needs about 30 s and about 1 GiB of RAM. The table above is the record.

**R3-3: report ids in info logs.**
- Verified: `main.py` (`accepted; report=%s`) and `session.py` (`raw cache %s; report=%s`) log the bare id. A spark
  link is a capability URL: anyone with the id can open the report, which includes JVM arguments, the mod or
  plugin list and system details. Operators paste logs into issues and chats when asking for help.
- Proposal: log a fingerprint instead, the first 10 hex characters of `sha256(report_id)`, in both places. Lines
  still correlate, and an operator can match a link by hashing it.
  - The cache DB keeps the raw id as its key: it is local data needed for lookup, not a log.
  - Test: capture logs through a full analysis and assert that the id never appears.

**R3-4: `handle()` is about 180 lines** (one async generator: checks, download, history, a nested tool, the
fallback loop, cancellation, cleanup).
- Proposal (optional; no behaviour change; separate commit, done last): extract the parts that do not yield.
  - Helpers: `_tags(text, overview)`; `_history_context(...)`, which also absorbs R3-1;
    `_query_tool(session, owner, trace)`, returning the tool and its call counter; `_ask(...)`, holding one
    provider attempt with the 300 s wait and `cancel_bounded`; and `_save_history(...)`.
  - `handle()` keeps the pre-checks, the bookkeeping, every `yield` and the `except`/`finally`; roughly 80 lines.
  - Risk to watch: the §2.4 guard, "no fallback while a stuck task could still send", must keep its `remaining`
    semantics.
  - The 33 plugin tests cover these paths; expect no test edits beyond patch targets.

**R3-5: auto-trigger keywords.**
- Verified: `analy[sz]e` does not match "analysis".
- Proposal: `analy(?:[sz]e|sis)`, plus a test case. The config hint lists only Chinese examples and "等", so it
  needs no edit.
