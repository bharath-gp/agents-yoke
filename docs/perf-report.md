# yoke session performance report

Date: 2026-10-26. Scope: the three most recent large runs. Evidence comes from `~/.yoke/runs/<run_id>/` (`events.jsonl`, per-call `meta.json`, ticket `feedback.md`), from the Claude session transcripts of each validator and reviewer call (`~/.claude/projects/*yoke-runs*/<session_id>.jsonl`), from a local profile of the test suite, and from two small Claude CLI probes. No code was changed.

## Contents

- [1. Runs examined](#1-runs-examined)
- [2. Time breakdown by cause](#2-time-breakdown-by-cause)
- [3. Root causes ranked by impact](#3-root-causes-ranked-by-impact)
- [4. Improvement options](#4-improvement-options)
- [5. Recommended next step](#5-recommended-next-step)
- [Appendix: method and caveats](#appendix-method-and-caveats)

## 1. Runs examined

The total runs from `run_created` to `pr_opened`. "Human" means time the session waited on the user: interview and approvals before the runner, `run_needs_user` to `unblock` (escalations), and `run_done` to `pr_opened`.

| Run | Tickets / milestones | `parallel` | Total | Before runner (plan, chart, approvals) | Runner | of which blocked on user | `run_done` to PR |
|---|---|---|---|---|---|---|---|
| `agents-yoke-20261020-092428` | 19 / 8 | 1 | **13.96h** | 0.64h | 13.23h | 2.26h (2 escalations) | 0.09h |
| `agents-yoke-20261022-231652` | 15 / 5 | 3 | **10.86h** | 0.34h | 7.34h | 0 | 3.18h |
| `agents-yoke-20261025-181943` (last) | 9 / 4 | 3 | **4.66h** | 0.62h | 3.82h | 0.50h (1 escalation) | 0.22h |

Machine time (runner minus blocked-on-user) was 10.97h, 7.34h and 3.32h. Human waits added 2.99h, 3.52h and 1.34h.

## 2. Time breakdown by cause

### 2.1 Agent call time per stage

Source: the sum of `duration_s` over every `meta.json` in the run. `mean` is per call.

| Stage (agent, model) | Run 0920 | Run 1022 | Run 1025 |
|---|---|---|---|
| code (agy, gemini flash) | 249m, n=56, mean 4.4m | 135m, n=31, mean 4.4m | 56m, n=17, mean 3.3m |
| validate (claude, sonnet) | 64m, n=19, mean 3.4m | **109m**, n=16, mean 6.8m, max 20.0m | **56m**, n=9, mean 6.2m, max 19.2m |
| qa (agy) | 132m, n=31 | 100m, n=29 | 31m, n=13 |
| docs (agy) | 66m, n=21 | 52m, n=15 | 25m, n=10 |
| review (claude, 3 lenses in runs 1022 and 1025) | 29m, n=45 | 36m, n=90 | 18m, n=39 |
| router fallback (claude haiku, commit messages) | 4.5m | 3.3m | 2.0m |
| plan_review + wayfinder | 5.0m | 11.9m | 10.1m |

Inside tickets, agent calls cover 70-90% of each ticket's span. The rest is runner work: test re-runs, git, router and the merge lock.

### 2.2 Retries: attempt 2 and 3

| Run | Ticket agent time | Spent in attempt >= 2 | Review-round rework in attempt 1 |
|---|---|---|---|
| 0920 | 540m | **157m (29%)** | 54m (10%) |
| 1022 | 431m | **138m (32%)** | 28m (7%) |
| 1025 | 186m | 35m (19%) | 6m (3%) |

Retry causes, from `tickets/*/attempt-N/feedback.md`:

- Run 0920: 12 of 14 retries were `Runner re-ran 'python3 -m unittest discover -s tests' and it exited 1 (QA had reported pass)`. One more was a re-run that timed out (`exited -1 ... timed out`, T-0063). One was `code call failed: permission denied: ReadFile`.
- Run 1022: 9 of 9 retries were "QA had reported pass" but the runner's re-run failed.
- Run 1025: 1 of 2 retries had this cause. The other was `agy status ERROR` on T-0105.
- All three escalations (T-0072, T-0074, T-0105) were `max attempts (3) reached` with that same test re-run feedback. They cost 2.26h and 0.50h of human wait.

QA is agy, and agy has no shell. QA's `commands_run` is `[]` (for example T-0105 `state.json`), so its `pass` is a guess. The first real test run is the runner's re-run after code, review and QA ([`pipeline.py:972`](yoke/pipeline.py:972)). A failure there restarts the whole attempt: code, all 3 review lenses, qa, validate and docs.

### 2.3 The validator runs the test suite itself

The validator prompt ([`validate.md`](yoke/prompts/validate.md)) does not say that the runner already ran `test_command` and got exit 0. Sonnet runs the full suite anyway, sometimes several times, and polls background tasks. Source: the tool-call timings in the validator transcripts.

| Run | validate total | Bash running unittest | sleep/until polling | Idle after first `StructuredOutput` | Share |
|---|---|---|---|---|---|
| 0920 | 64m | 55m (n=56) | 0 | 0 | 86% |
| 1022 | 109m | 72m (n=60) | 9m | 0 | 74% |
| 1025 | 56m | 31m (n=35) | 15m | 6m | **93%** |

Example: T-0109 (docs ticket), `tickets/T-0109/attempt-1/validate`. Wall time was 19.2m, with `duration_api_ms` 57s and 3 turns. The suite was moved to the background at its 120s Bash timeout. Then came an `until grep` poll (400s), a second full suite (3m06s), and 6m of idle after `StructuredOutput` while `claude -p` waited for the orphaned poll task to be killed. T-0095 in run 1022 hit the 1200s validate timeout and then a `validate-retry` (`stage_retry` event), for 28.7m in total.

### 2.4 Test suite cost

Local profile: 697 tests, 204.8s, single process, 10 CPUs available. The escalation log shows `Ran 713 tests in 174.430s`.

- Slowest modules: `test_review` 36.1s, `test_models` 21.4s, `test_parallel` 15.9s, `test_tracker` 12.5s.
- 32 tests over 1s add up to 77.5s. The worst is `test_review.TestReviewStage.test_pr_body_unresolved_findings_surfaced_or_omitted` at 13.2s.
- The suite prints `yoke doctor`-style output and repeated `warning: T-001 became issue #40 (ids are not renumbered)` lines. The runner feeds back only the last 2000 characters ([`pipeline.py:695`](yoke/pipeline.py:695)), so this noise crowds out the failure. The validator itself noted "the full-suite output was swallowed by noise".

Each ticket runs the full suite about 3-6 times: the runner re-run after QA, the re-run after rebase under the merge lock when HEAD moved ([`pipeline.py:1131`](yoke/pipeline.py:1131)), 1-3 validator runs, plus the milestone `exit_check`.

`_rerun` uses the QA timeout of 1800s and has no process-group kill (`# ponytail: no process-group kill`). T-0063 lost 30m to a hung re-run.

### 2.5 Parallelism is nominal

Effective concurrency is the sum of ticket spans divided by the runner wall time with at least one ticket in flight.

| Run | Sum of ticket spans | Wall with a ticket in flight | Effective concurrency | Critical path with milestone gates | Critical path with deps only |
|---|---|---|---|---|---|
| 0920 (`parallel` 1) | 12.92h | 12.92h | 1.00 | 11.73h | 3.71h |
| 1022 (`parallel` 3) | 7.61h | 7.26h | **1.05** | 6.90h | 6.23h |
| 1025 (`parallel` 3) | 4.11h | 3.73h | **1.10** | 3.65h | 2.93h |

The charted tickets are mostly chains. In run 1022, T-0081 → 82 → 83 → 84 → 85 → 87 → 88 → 90 → 92 → 94 → 95 is 11 of the 15 tickets. Milestones run strictly in sequence ([`scheduler.py:207-215`](yoke/scheduler.py:207)). With `parallel` 3 the runs are still close to serial, because the critical path is the run. Scheduler idle time between tickets is small: 0.08-0.31h.

### 2.6 Smaller costs

- **Claude CLI inherits user settings.** Probe with a trivial `claude -p --model haiku` call. Default: 7.15s wall and 46.9k input tokens. With `--setting-sources project`: 4.68s wall and 28.9k input tokens. The difference is user hooks (caveman, ponytail), the global CLAUDE.md and the output style. It also enables behaviour such as moving long Bash calls to the background, which caused the 6m idle tail above. Across about 100-200 Claude calls per run, that is about 5-8 min plus token cost.
- **Commit message router.** All 9 `commit_message` decisions in run 1025 used `source: fallback`, a Claude haiku call of about 10-15s each, never Ollama. Total cost is about 2-4 min per run.
- **Guard hook.** The first `yoke guard tool` Bash check took 3.99s cold, then 0.05-0.09s. This is negligible.
- **Merge lock.** It is held across the post-rebase re-run of the full suite (about 3 min). With concurrency near 1 it rarely blocks today. It will matter once parallelism works.

## 3. Root causes ranked by impact

1. **Blind QA plus whole-attempt retries.** The first real test run comes after code, review and QA. A failure re-runs every stage. Cost: 29-32% of ticket agent time in runs 0920 and 1022 (about 2.3-2.6h), plus all 3 escalations (2.76h of human wait).
2. **No real parallelism.** Dependency chains from charting plus sequential milestone gates give a concurrency of 1.05-1.10 with `parallel` 3. Run 0920 used `parallel` 1. On deps alone, run 0920's critical path is 3.71h against the 11.73h it ran.
3. **The validator runs the test suite.** This duplicates the runner's gate and takes 74-93% of validate time: 55m, 81m and 52m per run.
4. **A slow, noisy, hang-prone test suite.** 175-205s per run of the suite, executed 3-6 times per ticket. Its noise hides failures, which feeds cause 1. One hang cost 30m.
5. **Human waits.** Escalations from cause 1, and run 1022 waited 3.18h between `run_done` and `approve final`. This is outside yoke's machine time, but notification and fewer escalations would shrink it.
6. **Per-call overhead.** Claude calls inherit user settings (+2.5s and +18k tokens per call), and commit messages always fall back to Claude. Each is only minutes per run.

## 4. Improvement options

Every option keeps the `CLAUDE.md` constraints: stdlib only, Python 3.9, no `--dangerously-skip-permissions`, `structured_output` only, the runner runs tests and commits, `string.Template` prompts, atomic writes, hash-bound approvals, stable PLAN anchors and the TOC rule. Any change to code, prompts or defaults needs the version bump in both `.claude-plugin/plugin.json` and `pyproject.toml`.

### A. Runner test loop right after code (fixes cause 1)

After the `code` stage, the runner runs `test_command` and feeds failures to the resumed coder session (`--resume` session id, as review rounds already do). It repeats up to `max_test_fix_rounds` (default 2) before review and QA. `test_command` comes from the config, the previous attempt's QA, or a ticket field. If none is known, the stage is skipped.

- **Expected gain:** most of the attempt >= 2 time, about 1.5-2.5h on runs like 0920 and 1022. Fewer escalations, so fewer human waits.
- **Effort:** M. Touches `_attempt_body`, `_review_loop`-style rounds, an events entry, the config default and tests.
- **Risks:** the coder may overfit to tests. The QA, validate and close gates are unchanged, so correctness gates stay intact.
- **Design:** a change to the per-ticket state machine ([PLAN 2.8](docs/PLAN.md#s2-8)). Record it in a new milestone's Notes. It fits "agy has no shell; runner runs tests".

### B. Stop the validator from running tests (fixes cause 3)

Pass the runner's result into `validate.md` (`$test_command`, `$rerun_rc`, a failure tail) with "Do not run tests; the runner already ran them". Keep `CLAUDE_READ_TOOLS` restricted to `git diff/log/show` for validate. Today it effectively runs `python3`, probably through inherited user permissions; see option E.

- **Expected gain:** 50-80 min of machine time per run. Validate drops from a mean of 6-7m to under 1m (`duration_api_ms` is only 15-57s).
- **Effort:** S. A prompt edit and one template variable in `_vars`/`_call`, plus a test.
- **Risks:** the validator loses independent test evidence. The close gate still requires the runner's re-run to exit 0 ([`_gate`](yoke/pipeline.py:1060)), so nothing is lost.
- **Design:** [PLAN 2.5](docs/PLAN.md#s2-5) roles and [2.8](docs/PLAN.md#s2-8) close gate. Validate judges the diff against exit criteria.

### C. Make the test suite fast, quiet and hang-proof (fixes cause 4)

1. Repo side (no yoke bump): set `test_command` in `.yoke.json` to a stdlib parallel runner, for example `tests/run_parallel.py`, which runs each test module in its own `subprocess`. The longest module bounds the run at about 40s instead of 205s. Also silence the doctor and "ids are not renumbered" output in tests, and shorten the 13s and 7s tests.
2. yoke side: add `timeouts_s.test` (for example 600) for `_rerun`, with a process-group kill reusing the `calls.run_call` kill logic. Keep the failure section of the output (`FAIL:`/`ERROR:` blocks) instead of the raw last 2000 characters.

- **Expected gain:** about 2.5 min per suite run × 3-6 runs per ticket. That is roughly 0.6h on run 1022 and caps hangs at 10m instead of 30m. Feedback gets clearer, which cuts retries.
- **Effort:** S-M.
- **Risks:** parallel modules can collide on shared temp paths. Tests use `tempfile`, so this is low risk; verify once.
- **Design:** [PLAN 2.12](docs/PLAN.md#s2-12) process control. The repo-side part does not touch yoke core.

### D. Real parallelism (fixes cause 2)

1. Change the `chart.md` wayfinder prompt to emit `depends_on` only for true code dependencies, prefer disjoint `files_hint`, and fan tickets out within a milestone. Use `planreview`-style validation to flag chains longer than N.
2. Optional and larger: let tickets of milestone k+1 whose `depends_on` are all closed start while milestone k's gate runs. This needs speculative work and a rebase if the gate spawns `F-xxx` fixes.
3. Default `parallel` to 3 (run 0920 used 1).

- **Expected gain:** up to the deps-only critical path. For run 1025 that is 3.65h → 2.93h. For a 0920-like run with `parallel` > 1, 11.7h → about 4-6h.
- **Effort:** item 1 is S-M (prompt plus tests). Item 2 is L.
- **Risks:** more rebase conflicts, which cost a coder round trip (`reapply_on`). Merge-lock contention grows, so do option C first: the lock is held over a full test run. Item 2 weakens the milestone guarantee.
- **Design:** item 2 changes the sequential milestone-gate design ([M14](docs/PLAN.md#m14), [M5](docs/PLAN.md#m5)) and needs a plan decision. Item 1 stays inside current design ([M19](docs/PLAN.md#m19) hints overlap).

### E. Isolate agent CLI calls from user settings (cause 6)

Add `--setting-sources project` (or `local,project`) to `ClaudeAdapter.build_cmd` ([`adapters.py:89`](yoke/adapters.py:89)). Keep `--settings <agent-settings.json>` for the guard hook.

- **Expected gain:** about 2.5s and 18k input tokens per Claude call, about 5-8 min and noticeable cost per run. It also removes user hooks and output styles that change agent behaviour, such as background Bash and polling.
- **Effort:** S.
- **Risks:** confirm that `--settings` hooks still load with `--setting-sources` restricted, using one probe. Users who rely on global permissions inside yoke calls lose them, which is intended.
- **Design:** [PLAN 2.4](docs/PLAN.md#s2-4) adapters.

### F. Fewer human waits (cause 5)

Send a notification (`PushNotification`/hook) on `run_needs_user` and `run_done`. A and C should cut escalations. `approve final` stays manual by design.

- **Gain:** variable, up to hours. Run 1022 waited 3.18h.
- **Effort:** S.
- **Design:** [PLAN 2.11](docs/PLAN.md#s2-11) and [M7](docs/PLAN.md#m7) hooks.

### G. Commit message on Ollama (cause 6)

Find out why `commit_message` always falls back (9/9 in run 1025) and fix the Ollama prompt or threshold.

- **Gain:** about 2-4 min per run plus Claude cost.
- **Effort:** S.
- **Design:** [PLAN 2.10](docs/PLAN.md#s2-10).

### Summary

| Option | Gain per run (large run) | Effort | Risk |
|---|---|---|---|
| A. Test loop after code | 1.5-2.5h, fewer escalations | M | low-med |
| B. Validator stops running tests | 50-80m | S | low |
| C. Fast, quiet, hang-proof tests | 0.5-1h, clearer feedback | S-M | low |
| D1. Wider charts + `parallel` 3 | 0.7h (1025) up to several hours | S-M | med |
| D2. Overlap milestones | more | L | high, design change |
| E. `--setting-sources project` | 5-8m plus tokens | S | low |
| F. Notifications | human wait | S | low |
| G. Commit message via Ollama | 2-4m | S | low |

## 5. Recommended next step

Implement **B and E** together as one small, low-risk change with a version bump. Then do **C1** in this repo's `.yoke.json` and tests, with no bump. Together they save about 1-1.5h per large run and make test feedback readable. After that, do **A**, the biggest lever, as a planned milestone with PLAN 2.8 Notes. Then do **D1**, with D2 kept for a separate design decision.

## Appendix: method and caveats

- The timing script walked every `meta.json` (`started_at`/`ended_at`, `duration_s`) and the `events.jsonl` `ticket_state`, `run_needs_user`/`unblock`, `approved` and `milestone_passed` events. Validator figures come from tool_use/tool_result timestamps in the Claude transcripts. 11-21 Claude calls per run had no transcript; these were router calls.
- The ticket span runs from the first `in_progress` event to `closed`. For T-0072, T-0074 and T-0105 it includes the user-blocked time.
- agy reports no token usage (`model: unreported`), so agy cost and latency internals are not visible.
- The guard probe during this investigation appended 8 `router`/`guard` events dated 2026-10-26 to `agents-yoke-20261025-181943/events.jsonl`. They are harmless, but you can delete those lines.
