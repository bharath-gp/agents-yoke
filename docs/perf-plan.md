# yoke performance fix plan

This plan implements the options in [perf-report.md](perf-report.md). It is written for a future session to execute phase by phase. Each phase is a self-contained PR made of small commits, and it lands with its own milestone entry in [PLAN.md](PLAN.md) section 3 (M30 onwards).

## Contents

- [How to use this plan](#how-to-use-this-plan)
- [Baseline and how to measure](#baseline-and-how-to-measure)
- [Phase overview](#phase-overview)
- [Phase 0: fast, quiet test suite (repo only)](#phase-0-fast-quiet-test-suite-repo-only)
- [Phase 1 (M30): validator stops running tests, isolated Claude calls](#phase-1-m30-validator-stops-running-tests-isolated-claude-calls)
- [Phase 2 (M31): hang-proof, readable runner test re-runs](#phase-2-m31-hang-proof-readable-runner-test-re-runs)
- [Phase 3 (M32): test-fix loop right after code](#phase-3-m32-test-fix-loop-right-after-code)
- [Phase 4 (M33): wider charts for real parallelism](#phase-4-m33-wider-charts-for-real-parallelism)
- [Phase 5 (M34): notifications and commit-message cost](#phase-5-m34-notifications-and-commit-message-cost)
- [Phase 6 (decision only): overlapping milestones](#phase-6-decision-only-overlapping-milestones)
- [Rules that apply to every phase](#rules-that-apply-to-every-phase)

## How to use this plan

1. Start a session with: "Implement Phase N of docs/perf-plan.md."
2. Read the phase section, then the files it lists. Line numbers are as of commit `a7dd851` (version 0.11.0). Re-check them with `grep` before editing.
3. Create a branch: `perf/phase-N-<slug>`.
4. Make the commits in the order listed. Run `python3 -m unittest discover -s tests` before each commit.
5. Add the phase's milestone entry to `docs/PLAN.md` section 3, using the M29 format (Files, bullets, Exit criteria, Notes). Add a matching `## Contents`/TOC line if the PLAN TOC lists milestones. Keep the existing anchors.
6. Bump the version if the phase says so. Open a PR with `yoke`-style attribution.
7. Tick the phase's checkbox in [Phase overview](#phase-overview) of this file in the same PR.

Phases 0, 1 and 2 are independent and can run in any order. Phase 3 needs Phase 2. Phase 4 and Phase 5 are independent of the rest.

Real agent calls and `tests/e2e.sh` spend quota. Ask the user before any probe that a phase lists.

## Baseline and how to measure

Baseline runs are `agents-yoke-20261020-092428`, `agents-yoke-20261022-231652` and `agents-yoke-20261025-181943` under `~/.yoke/runs/`.

| Metric | 1020 | 1022 | 1025 |
|---|---|---|---|
| Total time | 13.96h | 10.86h | 4.66h |
| Ticket agent time | 540m | 431m | 186m |
| Share of agent time in attempt >= 2 | 29% | 32% | 19% |
| Validate total / of which test runs and polling | 64m / 55m | 109m / 81m | 56m / 52m |
| Effective concurrency (`parallel`) | 1.00 (1) | 1.05 (3) | 1.10 (3) |
| Full suite, single process | 175-205s | | |

To measure a new run:

```bash
python3 scripts/run_timing.py ~/.yoke/runs/<run_id>
```

`scripts/run_timing.py` prints these values:

- total, pre-runner, runner, blocked-on-user and done-to-PR time
- agent time per stage
- retry counts
- per-ticket span against agent time
- effective concurrency, which is the sum of ticket spans divided by the wall time with a ticket in flight

To see validator overhead, compare `duration_s` in `tickets/*/attempt-*/validate/meta.json` with `duration_api_ms` in the `stdout.log` next to it. A healthy validator has wall time close to API time plus a few tool calls.

## Phase overview

| Phase | Fixes (report option) | Bump | Expected gain on a large run | Status |
|---|---|---|---|---|
| 0 | C1: parallel, quiet test suite | no | about 2.5 min per suite run; 0.5h+ | [x] |
| 1 (M30) | B + E: validator stops testing; isolated Claude CLI | 0.12.0 | 50-80 min plus tokens | [x] |
| 2 (M31) | C2: test timeout, group kill, failure-focused tail | 0.12.1 | caps hangs at 10 min; clearer feedback | [x] |
| 3 (M32) | A: test-fix loop after code | 0.13.0 | 1.5-2.5h; fewer escalations | [x] |
| 4 (M33) | D1: wider charts, parallel default advice | 0.13.1 | up to the deps-only critical path | [x] |
| 5 (M34) | F + G: notifications; commit message without an agent call | 0.13.2 | human wait; 2-4 min | [x] |
| 6 | D2: overlap milestones | design decision | more parallelism | [ ] |

Milestone numbers shifted by one when this work merged, because `main` took M30 for adversarial review: Phases 1-5 are [M31](PLAN.md#m31)-[M35](PLAN.md#m35) in PLAN.md. The headings below keep their original numbers so their anchors stay stable.

The versions assume the phases land in order. Use the next free patch or minor version at the time, and keep `pyproject.toml` and `.claude-plugin/plugin.json` equal.

## Phase 0: fast, quiet test suite (repo only)

**Goal:** the full suite runs in about 40-60s instead of about 205s, and its output shows failures only.

**No version bump.** This phase touches tests and `.yoke.json` only, not yoke core.

**Files:** `tests/run_parallel.py` (new), `.yoke.json`, the slow tests in `tests/test_review.py` and `tests/test_models.py`, the tests that leak output, `CLAUDE.md` (Commands block), `README.md` if it documents the test command.

### Commit 0.1: parallel test runner

1. Write `tests/run_parallel.py` with stdlib only:
   - Discover `tests/test_*.py`.
   - Run each module as `python3 -m unittest -q tests.<module>` in a `concurrent.futures.ThreadPoolExecutor`, with each worker running a `subprocess.run`. Use `max_workers=os.cpu_count()`.
   - Start the longest modules first: `test_review`, `test_models`, `test_parallel`, `test_tracker`, `test_branch_points` and `test_gates`.
   - Print each failing module's output in full, then one summary line: `Ran N tests in Xs` and `OK` or `FAILED (modules: ...)`.
   - Exit 1 if any module failed.
   - Optional `-k <substring>` to filter modules.
2. Before relying on it, check that no two modules share fixed paths. `grep -rn "/tmp\|gettempdir\|YOKE_HOME" tests` should show that every test uses `tempfile.mkdtemp`/`TemporaryDirectory` or a per-test `YOKE_HOME`. Fix any that do not.
3. Check: `python3 tests/run_parallel.py` passes and prints the same test count as `python3 -m unittest discover -s tests`. Record both timings in the commit message.

### Commit 0.2: silence test noise

1. Tests that drive `tracker` print `warning: %s became issue #%d (ids are not renumbered)` from `yoke/tracker.py:150`. Capture stdout in those tests with `contextlib.redirect_stdout(io.StringIO())`, or assert on the text. Do not change `tracker.py` in this phase.
2. Tests that print `yoke doctor`/setup check lines (`python 3.9.6 ok`, `claude ok ...`), likely in `tests/test_plugin.py`, `tests/test_cli.py` and `tests/test_core.py`: capture the output the same way.
3. `ResourceWarning: unclosed file` warnings around `events.jsonl` (seen in the runner feedback for T-0066, T-0083 and T-0088): find the open without `with` (`grep -rn "open(" yoke | grep -v "with "`). Close it in a test helper. If the leak is in yoke core, leave it for Phase 2 and note it.
4. Check: `python3 -m unittest discover -s tests 2>&1 | grep -c "renumbered\| ok "` prints 0.

### Commit 0.3: speed up the slowest tests

1. Profile with the loop from the perf report: a `unittest.TextTestResult` subclass that times `startTest`/`stopTest`. Targets:
   - `test_review.TestReviewStage.test_pr_body_unresolved_findings_surfaced_or_omitted`: 13.2s
   - `test_review.TestMultiLensReview.test_unresolved_findings_flow_to_validate_and_pr`: 7.8s
   - `test_models.TestModelsCLI.test_set_scopes`, `test_repeatable_set`, `test_reset_all_entries`: 5-7s each
2. Typical causes: real `subprocess` calls of `yoke` or `fake_cli.py` per step, real `sleep`, or Ollama/HTTP timeouts that are not stubbed. Replace them with in-process calls or patched `time.sleep`. Patch `router` Ollama calls to fail fast (stub `router`'s HTTP call with `mock.patch`; find how `tests/test_router.py` fakes Ollama and reuse it).
3. Do not weaken assertions. Each change must keep the test's intent.

### Commit 0.4: use it

1. Add `"test_command": "python3 tests/run_parallel.py"` to `.yoke.json`. `pipeline.py:965` already prefers `run.cfg["test_command"]` over QA's. Milestone `exit_check` commands chosen by the wayfinder are unchanged.
2. Add `python3 tests/run_parallel.py` to the `CLAUDE.md` Commands block, next to the discover command.

**Exit criteria:**

- `python3 tests/run_parallel.py` runs in at most 60s on this machine, with the same test count as discover.
- Discover output has no "renumbered" or doctor lines.
- `.yoke.json` has `test_command`.

## Phase 1 (M30): validator stops running tests, isolated Claude calls

**Goal:** the validator judges the diff and the runner's test evidence, and runs no tests. Claude agent calls stop inheriting the user's personal settings.

**Bump:** 0.11.0 → 0.12.0 (prompts plus adapter behaviour).

**Files:**

- `yoke/prompts/validate.md`
- `yoke/pipeline.py` (`_attempt_body` around line 1009, `_call` `extra_vars`)
- `yoke/adapters.py` (`ClaudeAdapter.build_cmd`, lines 89-110; `CLAUDE_READ_TOOLS`, line 9)
- `yoke/defaults.json` (`agents.claude`)
- `tests/test_prompts.py`, `tests/test_core.py` (adapter `build_cmd` tests near line 265), `tests/test_pipeline.py`
- `docs/PLAN.md` (M30), `README.md` (if it describes validate), `pyproject.toml`, `.claude-plugin/plugin.json`

### Commit 1.1: give the validator the runner's test evidence

1. In `_attempt_body`, the `validate` stage goes through the generic `call(...)` at line 946. Pass `extra_vars` for validate only. Use `test_evidence`, which holds either:
   - `"The runner ran \`%s\` in this worktree and it exited %s." % (test_cmd, rerun_rc)`, or
   - `"No test command ran for this ticket type."` when `test_cmd` is None, for example for `chore` or `docs` routes.

   The generic call does not take `extra_vars` today. Add the kwarg to that one `call(...)` line, set to `{"test_evidence": ...}` when `stage == "validate"`, else `None`.
2. Edit `validate.md` (it is a `string.Template`, so use `$test_evidence`):
   - Add a line: "Test evidence: $test_evidence"
   - Add: "Do not run tests, builds or any long-running command, and do not start background tasks. Use Read, Grep, Glob and git diff/log/show only. Judge test-related criteria from the test evidence above and the diff."
3. Tests:
   - A `test_prompts.py` case that renders `validate.md` and asserts the `$test_evidence` text is substituted, with no `$` left.
   - A `test_pipeline.py` case with `FakeAdapter` that checks the validate prompt (audit `prompt.md`) contains the test command and exit code after a passing QA rerun.

### Commit 1.2: block test commands in read-only Claude calls

1. Diagnose first. Ask the user before this probe, which makes 2-4 cheap haiku calls. Run, from a temp dir:

   ```
   claude -p "Run: python3 -c 'print(1)'" --model haiku --output-format json --permission-mode plan --allowedTools "Read Grep Glob Bash(git diff:*)"
   ```

   Run it with and without `--setting-sources project`. Record which variant ran `python3` (`permission_denials` in the JSON). This tells you whether Commit 1.3 alone stops the validator, or whether the user's `rtk hook claude` PreToolUse hook (in `~/.claude/settings.json`) or plan-mode read-only auto-approval let it run.
2. Defence in depth, whatever the probe shows. In `build_cmd`'s read branch (line 98), add `--disallowedTools` with a module constant `CLAUDE_READ_DENY`:

   ```
   Bash(python3:*) Bash(python:*) Bash(pytest:*) Bash(sleep:*) Bash(npm:*) Bash(make:*)
   ```

   Deny rules beat allow rules, so `Bash(git diff:*)` keeps working. Do not deny bare `Bash`: that would block git diff.
3. Test: an adapter `build_cmd` test for `mode="read"` asserts that `--disallowedTools` is present and that the git allow list is unchanged. The write mode command must be unchanged apart from Commit 1.3.

### Commit 1.3: isolate Claude CLI calls from user settings

1. In `ClaudeAdapter.build_cmd`, add `["--setting-sources", cfg_value]` to every command. Read the value from a new default `agents.claude.setting_sources` (default `"project,local"`) in `defaults.json`. An empty string means "do not pass the flag", which is the escape hatch. `build_cmd` gets only `call`, so put the value on `CallSpec` as a new field `setting_sources`, set from `cfg` at every `CallSpec(` site: `pipeline.py:544`, `chart.py:102`, `planreview.py:159`, `router.py:35` and `prbody.py:653`. Default the field to `"project,local"` in the dataclass so missed sites still get it.
2. The run's guard hook comes in through `--settings <agent-settings.json>`, a flag source rather than a setting source. Verify it still fires. Ask the user before this probe, which is one haiku call. Run a call with both flags and check that a `guard` event lands in a scratch run's `events.jsonl`. **Use a scratch run, never a finished run:** `yoke guard` appends events to the run named by `--run`. If the hook does not fire, stop and record that in the M30 Notes. The alternative is to inline the guard hook via `--settings` JSON (the hook JSON as a string), which also works.
3. Test: `build_cmd` includes `--setting-sources project,local` by default, and omits it when the config value is `""`.

### Commit 1.4: docs, PLAN M30, version bump

1. Write the M30 entry in `docs/PLAN.md`:
   - Files list.
   - Bullets: validator gets test evidence; read-only deny list; `setting_sources`.
   - Exit criteria (below).
   - Notes: the probe results from 1.2 and 1.3.
2. Set version 0.12.0 in both files.

**Exit criteria:**

- The unit tests above pass.
- The probe in 1.2 shows `python3` denied in read mode.
- The guard hook still fires with `--setting-sources`.
- On the next real run, validate wall time per call is within about 2x of `duration_api_ms`, and `scripts/run_timing.py` shows the validate total down by 70% or more.

**Risk:** a user who relied on global allow rules inside yoke calls loses them. That is intended. Note it in the README config section.

## Phase 2 (M31): hang-proof, readable runner test re-runs

**Goal:** a hung test command costs at most `timeouts_s.test` (default 600s) and leaves no stray processes. Feedback shows the failing tests, not the last 2000 characters of noise.

**Bump:** next patch, for example 0.12.1.

**Files:** `yoke/pipeline.py` (`_rerun`, lines 690-697), `yoke/scheduler.py` (exit_check, line 299, uses `_rerun`), `yoke/hooks.py` (`run_hook`, which already has a process-group kill), `yoke/defaults.json` (`timeouts_s.test`), `yoke/config.py` if timeouts are validated there, `tests/test_pipeline.py` or `tests/test_gates.py`, `docs/PLAN.md` (M31).

### Commit 2.1: timeout and process-group kill

1. Rewrite `_rerun(run, cmd, wt)` on top of `hooks.run_hook(cmd, cwd=wt, timeout_s=cfg["timeouts_s"].get("test", 600), kill_grace_s=cfg.get("kill_grace_s", 1.0))`. It already uses `start_new_session=True` and `killpg`. Keep the return shape `(rc, text)`, with `rc=-1` and `"timed out after %ds"` on timeout. Check that `HookResult` exposes the full output (`res.output`) and the exit code. If `run_hook` sets env vars that a test command must not see, document them.
2. Remove the `# ponytail: no process-group kill` comment, since it no longer applies.
3. Add `"test": 600` to `timeouts_s` in `defaults.json`.
4. Test: `_rerun` with `cmd = "python3 -c 'import time; time.sleep(30)'"` and `timeouts_s.test = 1` returns `-1` within about 3s. A child started by the command, for example `sh -c 'sleep 30 & wait'`, is gone afterwards. The pattern is in `tests/test_core.py::TestRunCall::test_timeout_kills_process_group`.

### Commit 2.2: failure-focused feedback text

1. Add `_failure_text(output, limit=4000)` in `pipeline.py`:
   - If the output contains unittest `FAIL:`/`ERROR:` blocks (lines from `^(FAIL|ERROR): ` up to the next `^-{20,}$` or `^={20,}$` separator), return those blocks plus the final `Ran N tests` and `FAILED (...)` lines, trimmed to `limit`.
   - Otherwise fall back to the pytest `^FAILED |^E  ` lines and the summary.
   - Otherwise return the last `limit` characters, which is today's behaviour.
2. `_rerun` returns `_failure_text(output)` instead of `(stdout+stderr)[-2000:]`. Callers that log the tail (the escalation reasons and `scheduler` exit_check) work unchanged.
3. Tests: feed a captured unittest output with 50 noise lines plus one `ERROR:` block. The result must contain the traceback's last line, for example `KeyError: 'judge'` from the T-0105 escalation, and must not start with noise.

### Commit 2.3: PLAN M31 and bump

Write M31 and Notes, bump the version.

**Exit criteria:**

- The tests above pass.
- `grep -n "timeouts_s\"\]\[\"qa\"\]" yoke/pipeline.py` finds nothing in `_rerun`.

## Phase 3 (M32): test-fix loop right after code

**Goal:** catch test failures right after `code`, while the coder session is warm, instead of after review, QA and validate. This removes the main cause of retries and escalations.

**Bump:** minor, for example 0.13.0 (state machine behaviour).

**Depends on:** Phase 2 (`_rerun` timeout and `_failure_text`). Phase 0's `test_command` in `.yoke.json` makes it apply from attempt 1 on this repo.

**Files:** `yoke/pipeline.py` (`_attempt_body`, the code branch at lines 951-959), `yoke/defaults.json` (`max_test_fix_rounds`), `yoke/config.py` (validation, if any), `yoke/prompts/code.md` (a short note that test output may follow), `tests/test_pipeline.py` (new class `TestTestFixLoop`), `tests/fake_cli.py` scripts if needed, `docs/PLAN.md` (M32 plus a Notes line under [2.8](PLAN.md#s2-8)), `README.md` (pipeline description).

### Design

Pick the test command in this order:

1. `run.cfg.get("test_command")`
2. `st.get("last_test_cmd")`, QA's command from a previous attempt
3. otherwise skip the loop. QA-first behaviour is unchanged.

Run the loop after `_post_code` and the `after_stage.code` hooks:

```
for k in 1..max_test_fix_rounds:
    rc, text = _rerun(run, cmd, wt)          # guard.check_command first, as at line 966
    event("test_fix", ticket, attempt, round=k, rc=rc)
    if rc == 0: break
    coder_res = _call(..., "code", ..., feedback=prior_feedback + "\n\nRunner ran `cmd` after your change and it exited rc:\n" + text,
                      resume=st["sessions"].get("code"), label="code-t%d" % k)
    if not coder_res.ok: return "code call failed: ..."
    results["code"] = coder_res.structured_output; _post_code(...); git add -A
else:
    final rc, text = _rerun(...); if rc != 0: return feedback as today (normal retry path)
```

- `max_test_fix_rounds` defaults to 2. Setting it to 0 disables the loop.
- Do not set `rerun_rc` from this loop. The QA rerun and the close-gate logic stay as they are, so the close gate is unchanged: QA pass plus a runner rerun exit 0 still happen after QA. The loop only makes the later rerun very likely to pass.
- Count the loop's calls against `max_agent_calls_total` as usual. `_call` already checks the budget.
- Do not run the loop on `code` rounds inside `_review_loop`. `before_close` hooks and the QA rerun cover those.

### Commits

1. **3.1** Add the `max_test_fix_rounds` default and the loop, with the `test_fix` event. Add a dashboard/status label if `yoke status` prints event kinds (`grep -n "stage_retry" yoke/*.py` shows where kinds are rendered).
2. **3.2** Tests in `TestTestFixLoop`:
   - The first rerun fails and the second passes, giving one `code-t1` call and attempt 1 closing. Assert there is no attempt 2 directory.
   - Both rounds fail, so the attempt returns the rerun feedback and attempt 2 starts, as in the old behaviour.
   - No test command gives no loop and no `test_fix` events.
   - `max_test_fix_rounds = 0` gives no loop.
   - A guard deny on the test command returns the guard feedback, as at line 967.

   Simulate test results with a `test_command` such as `python3 -c "import os,sys; sys.exit(0 if os.path.exists('ok') else 1)"`, where a scripted fake coder writes `ok`.
3. **3.3** `code.md`: one sentence saying feedback may contain the runner's test output and the coder must fix the code or test, not weaken assertions. Then the PLAN M32 entry, a Notes line under section 2.8, the README pipeline text and the bump.

**Exit criteria:**

- The tests pass.
- On the next real run, `scripts/run_timing.py` shows the attempt >= 2 share below 10%, and no escalation has the cause "QA had reported pass".

**Risk:** the coder may game tests. Mitigation: the review lenses (`review_tests`) and the validator still run after the loop. The `code.md` sentence forbids weakening assertions.

## Phase 4 (M33): wider charts for real parallelism

**Goal:** charted tickets form a wide graph, so `parallel: 3` gives real concurrency.

**Bump:** patch.

**Files:** `yoke/tickets.py` (`warnings`, line 104), `yoke/prompts/chart.md` (line 7 already says to keep dependencies minimal), `skills/tickets/SKILL.md` (shows warnings, offers a re-chart), `skills/run/SKILL.md` (line 12: "default 1"), `tests/test_tickets.py` and `tests/test_chart.py`, `docs/PLAN.md` (M33).

### Commits

1. **4.1** Add a chain-length warning in `tickets.warnings`:
   - Compute the longest `depends_on` path inside each milestone, ignoring cross-milestone dependencies, which milestone order already enforces.
   - Warn: `milestone MS-02 has a dependency chain of N tickets (T-a -> T-b -> ...); only true code dependencies belong in depends_on`.
   - Trigger when N >= 4, or when N equals the milestone ticket count and the count is >= 3.

   Tests: one case for a chain, one for a wide milestone with no warning. Run 1022's MS-01 (T-0081 → 82 → 83 → 84 → 85) is the reference shape.
2. **4.2** `chart.md`:
   - Add a rule: "A dependency means the ticket cannot be written or tested without code from the other ticket. Shared files alone are not a dependency: use files_hint, and the runner serializes overlapping tickets. Prefer several independent tickets per milestone over a chain."
   - Add a rule for intermediate milestone `exit_check`: name targeted test modules, not the full suite. Run 1025 already did this.

   Update the `test_prompts.py` expectations if they pin the text.
3. **4.3** `skills/run/SKILL.md`: when asking how many tickets to run in parallel, recommend 3 when the charted milestones have independent tickets, and keep 1 as the safe default for chains. `skills/tickets/SKILL.md`: when the chain warning shows, offer one re-chart round with the warning as feedback (`chart_round(feedback=...)` exists). Then PLAN M33 and the bump.

**Exit criteria:**

- Tests pass.
- Re-importing run 1022's `tickets.json` through `tickets.warnings` flags MS-01. Use a test fixture copy, not the live run.

**Risk:** more parallel tickets mean more rebase conflicts (`reapply_on`) and more merge-lock waits. The lock is held over a full test run (`pipeline.py:1131`), so Phase 0 should land first.

## Phase 5 (M34): notifications and commit-message cost

**Bump:** patch.

### Commit 5.1: notify when the run needs the user or is done

1. Find where `run_needs_user` and `run_done` are emitted: `grep -n "run_needs_user\|run_done" yoke/*.py`.
2. Add an optional config `notify_command` (default `null`), for example `"osascript -e 'display notification \"$message\" with title \"yoke\"'"`.
   - Run it through `hooks.run_hook` with a 10s timeout and env `YOKE_EVENT`, `YOKE_RUN` and `YOKE_MESSAGE`. Never pass these values through the shell: substitute via env only, to avoid injection from ticket titles.
   - A failure only logs a `notify_failed` event.
3. Document it in the README config section.
4. Test: with `notify_command` set to a script that writes the env to a file, a fake run that escalates writes `YOKE_EVENT=run_needs_user`.

### Commit 5.2: commit message without an agent call

1. `router.commit_message` (`yoke/router.py:546`) always calls the fallback agent. That was 9/9 `source: fallback` in run 1025, at about 10-15s each.
2. Add a config `router.commit_message: "agent" | "default"`, with default `"agent"` to keep behaviour. With `"default"`:
   - Build the subject `"%s: %s" % (id, title)`, trimmed to 72 characters.
   - Build the body from the ticket's validator summary if it is available. Pass `results` from `_close`, which already has it at `pipeline.py:1116`. Otherwise use the diffstat.
3. Set `"default"` in this repo's `.yoke.json`.
4. Test both modes.

### Commit 5.3

PLAN M34 and the bump.

## Phase 6 (decision only): overlapping milestones

Do not implement this without a user decision. The milestone design (sequential gates, [M14](PLAN.md#m14)) guarantees that milestone k+1 builds on a passed gate.

Option to put to the user:

- Let a ticket of milestone k+1 start while milestone k's `exit_check` runs or its fixes are open, when all of the ticket's `depends_on` are closed.
- If the gate fails and fix tickets (`F-xxx`) land, rebase the running tickets in k+1 at close time. The `reapply_on` conflict path already exists.

Gain: run 1020 has a gated critical path of 11.7h against 3.7h on deps alone, though that run used `parallel` 1.

Cost:

- `scheduler._loop` readiness rule (lines 207-215)
- `milestones.ticket_waiting_for`
- dashboard gate display
- many tests
- a PLAN section 2.8 / M14 design change

Decide after Phases 3 and 4 have run once, using `scripts/run_timing.py` numbers.

## Rules that apply to every phase

- Python >= 3.9 and stdlib only. No `match`, no `X | Y` unions at runtime, no `tomllib`.
- Never add `--dangerously-skip-permissions`.
- Read `structured_output` only.
- The runner runs tests and commits, never agy.
- Prompts are `string.Template` (`$var`).
- State writes go through the existing atomic helpers (`run.write_json`, `state._write_atomic`).
- Do not change approval hashing. These phases do not touch `plan.md` or `tickets.json` hashing.
- Keep PLAN.md anchors stable and add new ones (`m30` ...) in the same style.
- Markdown docs with more than three `##` sections need a `## Contents` list. Check with `python3 -c "from yoke import planreview as p; print(p.toc_errors(open('<file>').read()))"`.
- Bump `pyproject.toml` and `.claude-plugin/plugin.json` together in every phase marked with a bump.
- Never write probe events into a finished run. Any `yoke guard`/router probe must use a scratch run (`YOKE_HOME=$(mktemp -d)`).
