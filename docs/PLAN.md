# Implementation Plan: agyloop (Primary/Secondary Agent Loop)

Source of truth for requirements: [REQUIREMENTS.md](REQUIREMENTS.md). This plan resolves every "open item" there into a decision and splits the work into milestones (M0–M9), each sized for one session. A later session should be able to pick the next unchecked milestone, read only this file plus the code, and implement it.

Status legend per milestone: `[ ]` todo, `[~]` in progress, `[x]` done. Update the checkbox and the "Notes" line when a milestone finishes.

**How agents should use this file:** read the [Table of contents](#toc), then jump to the milestone you are implementing. Each milestone lists the decision sections it depends on; [Appendix A](#appA) maps topics to sections and [Appendix B](#appB) maps source files to sections and milestones. Anchors (`#s2-8`, `#m4`, ...) are stable; do not rename them.

<a id="toc"></a>
## Table of contents

1. [Environment facts](#s1) — probed CLI flags, output shapes, local models
2. [Decisions on open items](#s2)
   - 2.1 [Shape of the system](#s2-1) — runner is the loop, thin orchestrator
   - 2.2 [Where state lives](#s2-2) — run dir layout under `~/.agyloop`
   - 2.3 [Approvals bound to hashes](#s2-3)
   - 2.4 [Agent adapters](#s2-4) — Claude/agy flag mapping
   - 2.5 [Roles](#s2-5)
   - 2.6 [Config](#s2-6) — defaults, budgets, type→stages routing
   - 2.7 [Schemas](#s2-7) — ticket and per-stage result fields
   - 2.8 [Per-ticket pipeline](#s2-8) — state machine, close gate, retries
   - 2.9 [Git, worktrees, commits, merging](#s2-9)
   - 2.10 [Decision router + Ollama](#s2-10) — fallback chain, MCP tools
   - 2.11 [Orchestrator handoff & compaction](#s2-11)
   - 2.12 [Process control, budgets](#s2-12)
   - 2.13 [Audit record](#s2-13)
   - 2.14 [Tracker: local vs GitHub](#s2-14)
   - 2.15 [Packaging](#s2-15) — repo layout, install
   - 2.16 [CLI surface](#s2-16)
3. [Milestones](#s3)
   | | Milestone | Depends on sections |
   |---|---|---|
   | [M0](#m0) | Spikes and scaffold | [1](#s1), [2.4](#s2-4), [2.10](#s2-10) |
   | [M1](#m1) | Core: config, state, adapters, audited calls | [2.2](#s2-2), [2.4](#s2-4), [2.6](#s2-6), [2.7](#s2-7), [2.12](#s2-12), [2.13](#s2-13) |
   | [M2](#m2) | Front half: init, plan review, approvals | [2.3](#s2-3), [2.5](#s2-5), [2.16](#s2-16) |
   | [M3](#m3) | Tickets + router | [2.6](#s2-6), [2.7](#s2-7), [2.10](#s2-10) |
   | [M4](#m4) | Sequential pipeline + git | [2.8](#s2-8), [2.9](#s2-9), [2.12](#s2-12) |
   | [M5](#m5) | Parallel tickets | [2.9](#s2-9) |
   | [M6](#m6) | MCP server | [2.10](#s2-10) |
   | [M7](#m7) | Plugin packaging, skills, hooks | [2.1](#s2-1), [2.11](#s2-11), [2.15](#s2-15) |
   | [M8](#m8) | GitHub tracker | [2.14](#s2-14) |
   | [M9](#m9) | Resume, handoff, end-to-end | [2.11](#s2-11), [4](#s4) |
4. [Verification matrix](#s4)
5. [Out of scope](#s5)
- [Appendix A: Topic index](#appA)
- [Appendix B: File index](#appB)

---

<a id="s1"></a>
## 1. Environment facts (probed 2026-10-15)

| Tool | Version | Notes |
|---|---|---|
| `agy` | 1.3.5 at `~/.local/bin/agy` | `-p`, `--output-format json`, `--json-schema <string-or-path>`, `--mode plan\|accept-edits\|auto`, `--sandbox`, `--conversation <id>`, `--model`, `--effort low..max`, `--print-timeout`, `--add-dir`, `--project`, `plugin {install,import,validate,list}`, `mcp add [-e K=V] <name> <cmd> [args]`. `agy models` lists `gemini-3.8-flash-*`, `claude-opus-5-5-*`, `claude-sonnet-5-5-*`, `gpt-oss-120b-medium`. |
| `claude` | 2.1.300 | `-p`, `--output-format json`, `--json-schema <schema>`, `--permission-mode`, `--allowedTools`, `--disallowedTools`, `--resume <id>`, `--session-id <uuid>`, `--model`, `--effort`, `--add-dir`, `--plugin-dir`, `--mcp-config`, `--bg`. |
| `ollama` | 0.40.4-rc1 | Local models: `tev1:4b`, `qwen3.5:9b-mlx`, `nimble-v3:9b-mxfp8`, `qwen3.8:27b-mlx`. HTTP API on `127.0.0.1:11434`. |
| `gh` | 2.97.0 | For GitHub tracker mode. |
| Python | system 3.9.6; pyenv 3.13; `uv` present | Target **Python ≥ 3.9, stdlib only** (no `tomllib`, no `match`, no `X \| Y` type unions at runtime). |
| Hardware | Apple M4, 32 GB | Fine for 4B–9B router models; 27B usable but slow. |

Observed `agy -p --output-format json --json-schema ...` output (one JSON object on stdout):

```json
{"conversation_id":"f2ddb28b-...","status":"SUCCESS","response":"<free text>",
 "duration_seconds":24.37,"num_turns":2,"structured_output":{"ok":true},
 "json_schema":{...},"usage":{"input_tokens":23414,"output_tokens":1926,"thinking_tokens":1493,"cache_read_tokens":35266,"total_tokens":25340}}
```

- `structured_output` is clean even though the user's global agy rules add persona/prompt-optimization noise to `response`. **Always read `structured_output`, never parse `response`.**
- agy does **not** report the model used → audit records `observed.model = "unreported"`.
- In `--mode plan` agy wrote a plan artifact under `~/.gemini/antigravity-cli/brain/<conversation_id>/`. Harmless; note it in docs.

Verified in M0 (claude 2.1.300) `claude -p --output-format json --json-schema <schema-string>`: one object with `type` ("result"), `subtype` ("success"), `is_error`, `result` (free text), `session_id`, `structured_output`, `num_turns`, `duration_ms`, `total_cost_usd`, `usage`, `modelUsage` (keys = observed model ids, e.g. `claude-sonnet-5-5`), `permission_denials`, `terminal_reason`. `--resume <session_id>` with `-p --json-schema` works and returns the **same** `session_id`.

Verified in M0 (agy 1.3.5/1.3.6):
- `--print-timeout` takes a Go duration with unit (`180s`), a bare number is a usage error (exit 2).
- **Headless permissions.** In `-p` mode agy cannot prompt, so any tool outside its allow list is auto-denied: agy prints `jetski: no output produced ...` on stderr, exits 0, JSON has `status: "SUCCESS"`, **no `structured_output`** and a `denied_actions: [{action, display_name}]` array. The adapter must treat missing `structured_output` or non-empty `denied_actions` as an error, never as success.
- `--mode accept-edits` + `--sandbox`: file writes inside cwd work; shell commands only run if allowed by `permissions.allow` rules (`command(<prefix>)`) in `~/.gemini/antigravity-cli/settings.json` or per-project `permissionGrants.permissionGrants.allow` in `~/.gemini/config/projects/<id>.json`. Observed allowed on this machine: `ls`, `git add`, `git commit`, `git status/log/show/diff`, `uv`, `gh`; denied: `echo`, `git config`. `--mode auto` is **not available for this account** (agy warns and falls back to the default mode, which denies writes), so it is not an option.
- Writes outside cwd (`../x`, `/tmp/x`) are denied under `--sandbox` (confined to the workspace).
- `--conversation <id>` resume works in `-p` with `--json-schema` and keeps the same `conversation_id`.
- `GIT_CONFIG_COUNT/KEY_n/VALUE_n` set in the agy process env reach the shell commands agy runs (a `core.hooksPath` pre-commit hook fired on an agy-run `git commit`).
- agy keeps per-folder project records under `~/.gemini/config/projects/`; `--project`/`--new-project` were not needed for the probes.

---

<a id="s2"></a>
## 2. Decisions on open items

<a id="s2-1"></a>
### 2.1 Shape of the system
- **One stdlib Python package `agyloop`** + a **CLI `agyloop`** does all deterministic work: state, adapters, pipeline, git, router, budgets, audit.
- **The runner is the loop.** `agyloop run` is a plain, detached Python process that walks the ticket state machine. It is resumable from disk at any point.
- **The "Orchestrator" primary session is thin.** It is a Claude (or agy) chat that started the runner and calls `agyloop wait`. `wait` blocks until an event needs attention (escalation, run finished, runner died) and prints a ≤20-line summary. The session never reads diffs or logs unless the user asks. This satisfies "orchestrator does no heavy lifting, reads summaries only", and makes session handoff trivial: any fresh session runs `/agyloop:resume`.
- **Front half (grill → plan → review → tickets) runs inside the interactive primary session** via plugin skills that call `agyloop` subcommands for the mechanical parts (secondary plan review, schema validation, hashing approvals).

<a id="s2-2"></a>
### 2.2 Where state lives
`$AGYLOOP_HOME` (default `~/.agyloop`) `/runs/<run_id>/` — **outside the target repo** (audit requirement). `run_id = <repo-basename>-<YYYYMMDD-HHMMSS>`.

```
runs/<run_id>/
  run.json              # repo path, base sha, feature branch, config snapshot, runner pid, state
  requirements.md
  plan.md
  plan-review/round-<n>/{prompt.md,command.json,stdout.log,stderr.log,result.json,meta.json}
  plan-review/arbitration.md   # primary's decisions per finding
  tickets.json          # list of ticket objects (schema: ticket-list)
  approvals.json        # {"plan": {"sha256":..., "at":...}, "tickets": {...}, "final": {...}}
  status.json           # derived summary, rewritten after every state change
  events.jsonl          # append-only event log (one JSON per line)
  escalations.json      # open escalations
  tickets/T-001/
    state.json          # status, attempt, current stage, session ids per role, commit sha
    attempt-1/<stage>/{prompt.md,command.json,stdout.log,stderr.log,result.json,meta.json}
  worktrees/T-001/      # git worktree for the ticket
  worktrees/_integration/  # worktree on the feature branch, used for merges
  runner.log
  runner.lock           # pid; stale lock = pid not alive
```

All writes are atomic (`write tmp` + `os.replace`). State mutations go through one `threading.Lock` in `state.py`.

<a id="s2-3"></a>
### 2.3 Approvals bound to hashes
`agyloop approve plan` stores `sha256(plan.md)`; `agyloop approve tickets` stores `sha256(canonical tickets.json)`. Every later command that depends on them recomputes and **refuses** if the hash changed ("plan changed after approval; re-approve"). `final` approval is recorded before any `agyloop pr`/push helper runs.

<a id="s2-4"></a>
### 2.4 Agent adapters (agent-agnostic)
`adapters.py` with one class per CLI, same interface:

```python
class Adapter:
    name: str
    def build_cmd(self, call: CallSpec) -> list[str]   # pure, unit-testable
    def parse(self, stdout: str, exit_code: int) -> CallResult  # structured_output, session_id, observed_model, usage, error
```

`CallSpec`: `prompt, schema_path, cwd, mode ("read"|"write"), model, effort, resume_session, timeout_s, extra_dirs, env`.

| | `ClaudeAdapter` | `AgyAdapter` |
|---|---|---|
| base | `claude -p <prompt> --output-format json --json-schema <schema-json-string>` | `agy -p <prompt> --output-format json --json-schema <schema-path>` |
| read-only | `--permission-mode plan --allowedTools "Read Grep Glob Bash(git diff:*) Bash(git log:*) Bash(git show:*)"` | `--mode plan --sandbox` |
| write | `--permission-mode acceptEdits --allowedTools <config allowlist> --disallowedTools "Bash(git push:*)" "Bash(git commit:*)"` | `--mode accept-edits --sandbox` (M0: `auto` unavailable; shell needs user allow rules, see below) |
| resume | `--resume <session_id>` | `--conversation <conversation_id>` |
| model/effort | `--model`, `--effort` | `--model`, `--effort` |
| session id | `session_id` | `conversation_id` |
| observed model | keys of `modelUsage` | `"unreported"` |
| never | `--dangerously-skip-permissions` | `--dangerously-skip-permissions` |

**agy shell access (M0 finding, deviation).** agy's headless `accept-edits` edits files but only runs shell commands the user has allow-listed. agyloop will not edit the user's agy settings. Decision: the coder stage only edits files; the runner (not the agent) runs `test_command` and commits, which the close gate already requires. QA/docs stages that need shell use agy only if the user adds `command(...)` rules; `agyloop doctor` (M1+) prints the exact rules to add. `AgyAdapter.parse` must fail the call when `structured_output` is absent or `denied_actions` is non-empty, so a permission denial escalates instead of passing silently.

`FakeAdapter` (tests) runs `tests/fake_cli.py` which replays scripted JSON responses keyed by stage, so the whole loop is testable offline (claudex-loop style).

Adding a new agent later = one adapter class + a config entry. Nothing else knows CLI flags.

<a id="s2-5"></a>
### 2.5 Roles
Default role map (config `roles`):

```json
{"primary": "claude", "secondary": "agy",
 "by_role": {"requirements": "primary", "planner": "primary", "wayfinder": "primary",
             "orchestrator": "primary", "plan_review": "secondary",
             "code": "secondary", "qa": "secondary", "validate": "primary", "docs": "secondary"}}
```
`by_role` values may be `primary`, `secondary`, or a concrete agent name. CLI flags `--primary`/`--secondary` override at `agyloop init`.

<a id="s2-6"></a>
### 2.6 Config
JSON (3.9 has no tomllib). Merge order: package `agyloop/defaults.json` ← `~/.agyloop/config.json` ← `<repo>/.agyloop.json` ← CLI flags. Snapshot of the merged config is written to `run.json` at `init` so a resumed run uses the same settings.

Default budgets/limits:

| Key | Default |
|---|---|
| `max_attempts_per_ticket` | 3 |
| `max_plan_review_rounds` | 3 |
| `max_tickets` | 40 |
| `max_agent_calls_total` | 300 |
| `parallel` | 1 (CLI `--parallel N`) |
| `timeouts_s` | plan_review 900, code 2400, qa 1800, validate 1200, docs 900, router_fallback 120 |
| `kill_grace_s` | 15 |
| `router.ollama_url` | `http://127.0.0.1:11434` |
| `router.model` | `qwen3.5:9b-mlx` (fast alt: `tev1:4b`) |
| `router.min_confidence` | 0.7 |
| `router.fallback` | `{"agent": "claude", "model": "claude-haiku-5-5", "effort": "low"}` |
| `test_command` | `null` (QA result supplies it; repo config may pin it) |
| `tracker` | `"auto"` (`github` if `gh repo view` succeeds and remote is GitHub, else `local`) |

Type → stages routing (config `routing`, overridable per type with model/effort per stage):

| type | stages |
|---|---|
| feature | code, qa, validate, docs |
| bugfix | code, qa, validate, docs |
| refactor | code, qa, validate |
| test | qa, validate |
| docs | docs, validate |
| chore | code, validate |
| spike | code, validate (no commit unless validate approves) |

<a id="s2-7"></a>
### 2.7 Schemas (`agyloop/schemas/*.json`, JSON Schema draft-07 subset)
Validation done by a ~60-line stdlib validator in `schema.py` supporting `type, properties, required, items, enum, additionalProperties:false, minItems, minLength, pattern`. Same files are passed to `--json-schema`. Every agent result is validated again by the runner; invalid → counts as a failed call (one automatic re-ask with the validation error, then attempt failure).

- **ticket**: `id` (`^T-\d{3}$`), `title`, `type` (enum above), `description`, `exit_criteria` (≥1 strings), `depends_on` (ids), `files_hint` (strings), `plan_excerpt` (string, the relevant plan section verbatim), `notes` (optional).
- **ticket-list**: `{"tickets": [ticket...]}`; runner also checks: unique ids, deps exist, no cycles, count ≤ `max_tickets`.
- **plan-review**: `verdict` (`approve|changes_requested`), `findings[]` `{severity: blocker|major|minor, section, issue, suggestion}`, `summary`.
- **code**: `status` (`done|blocked`), `summary`, `files_changed[]`, `assumptions[]`, `open_questions[]`, `notes_for_qa`, `blocked_reason` (when blocked).
- **qa**: `status` (`pass|fail|blocked`), `test_command` (string the runner can re-run), `tests_added[]`, `commands_run[]` `{cmd, exit_code}`, `failures[]`, `summary`.
- **validate**: `verdict` (`approve|reject`), `exit_criteria[]` `{criterion, met: bool, evidence}`, `issues[]` `{severity, file, description}`, `feedback_for_coder`, `summary`.
- **docs**: `status` (`done|not_needed`), `files_changed[]`, `summary`.
- **router-***: `classify {type, confidence, reason}`, `triage {category: test_failure|env_problem|spec_gap|agent_error|other, retryable: bool, confidence, hint}`, `summary {text}`, `commit_message {subject, body}`, `assert {answer: bool, confidence, reason}`.

<a id="s2-8"></a>
### 2.8 Per-ticket pipeline (state machine)
```
pending → (deps closed) → in_progress
  for stage in stages(type):
    code:     run secondary (resume session on retry, feedback appended)
              blocked → escalate
    qa:       run secondary; then RUNNER re-runs result.test_command in worktree (independent check)
              fail (agent or rerun) → feedback → attempt += 1 → back to code
    validate: run primary read-only; prompt includes ticket, exit criteria, base sha, prior results;
              validator inspects `git diff <base_sha>` itself
              reject → feedback_for_coder → attempt += 1 → back to code
    docs:     run secondary
  close: all gates true → commit in ticket worktree → merge into feature branch → closed
attempt > max_attempts → blocked + escalation; dependents stay pending (shown as "waiting on blocked T-xxx")
```
Close gate (checked explicitly, logged): code `done` ∧ QA `pass` ∧ runner test rerun exit 0 ∧ validate `approve` with every `exit_criteria.met` true ∧ docs `done|not_needed` (only for stages in this ticket's route).

Retry feedback is a file `attempt-<n>/feedback.md` built from QA failures / validator issues / router triage hint, inlined into the next code prompt, and the coder session is resumed by ID.

Context handoff: prompts **inline** the ticket JSON, plan excerpt, and prior-stage result JSON (small). Copies are saved in the attempt dir for audit. Inlining avoids sandbox path problems with files outside the worktree.

Prompt templates: `agyloop/prompts/<stage>.md` using `string.Template` (`$ticket`, `$plan_excerpt`, `$prior_results`, `$feedback`, `$base_sha`, `$worktree`) — `$` avoids clashing with JSON braces. Every worker prompt states: stay inside the worktree, do not commit, do not push, do not touch git config, return only the schema object.

<a id="s2-9"></a>
### 2.9 Git, worktrees, commits, merging
- At `agyloop run` (first start): require clean target repo state for the base (warn if dirty; base = `HEAD` sha). Create feature branch `agyloop/<run_id>` at base, plus integration worktree `worktrees/_integration` on it.
- Per ticket: `git worktree add worktrees/T-001 -b agyloop/<run_id>/T-001 <feature-branch tip>`. Same path for `parallel=1` and `parallel>1` (one code path).
- Commit: runner (never the agent) does `git add -A && git commit` in the ticket worktree with message from router `commit_message` (fallback: `T-001: <title>`), trailer `Agyloop-Ticket: T-001`.
- **Merge strategy** (serialized by a merge lock): in ticket worktree `git rebase <feature-branch>`; if conflict → `git rebase --abort`, feedback "rebase conflict on files X; rebase onto <feature-branch> and resolve" → attempt += 1 → code stage (coder resolves). If rebase changed anything, rerun test command. Then in `_integration`: `git merge --ff-only agyloop/<run_id>/T-001`. Result: linear history, one commit per ticket.
- Cleanup: remove ticket worktree after close (keep branch until final review). `agyloop clean` removes all worktrees/branches of a run after the user is done.
- **No push, ever, by the runner.** Push protection for workers: env `GIT_CONFIG_COUNT=1, GIT_CONFIG_KEY_0=core.hooksPath, GIT_CONFIG_VALUE_0=<pkg>/githooks` where `githooks/pre-push` exits 1; plus Claude `--disallowedTools "Bash(git push:*)"`; plus agy `--sandbox`; plus prompt rule. Post-call check: `git -C wt log` must show no new commits by the agent (warn + record if it did).

<a id="s2-10"></a>
### 2.10 Decision router + Ollama (MCP from day one)
- `router.py` = plain functions `classify_ticket, route_ticket, triage_failure, summarize, commit_message, assert_check, health`. Each returns `{"result":..., "source": "ollama"|"fallback"|"default", "model":..., "confidence":..., "fallback_reason":...}` and logs an event.
- **Two Ollama paths (M0 spike, rerun after learning tev1/nimble are decision models).**
  - *Decision questions* (`classify_ticket`, `triage_failure`, `assert_check`): `POST /v1/systemone` with `{"model","state","questions":{name:{"type":"choice"|"noul"|"score","instructions","criteria"}}}` (up to 64 questions per call). Response `answers.<name>`: `choice` + `probabilities` + `confidence`, or `noul` (P(true)), or `score`; plus `usage`. Single forward pass, no generation. Add a `none`/`other` option. `state` is data, so pass ticket/log text there.
  - *Generative* (`summarize`, `commit_message`): `POST /api/chat` with `format: <json schema>`, `stream:false`, **`think:false`**, `options.temperature:0` on `qwen3.5:9b-mlx` (~1.4 s with think off, 25-92 s on; 5/5 on classification).
  - Both: 60 s timeout, `urllib.request`.
- **Default decision model: `tev1:4b`.** 10/10 on 10 ticket-type classifications at ~0.75 s each (first call ~2.9 s load), 4/5 on failure triage (the miss was an ambiguous sample). `nimble-v3:9b-mxfp8`: same scores, ~1.6 s, and **CC BY-NC 4.0 (noncommercial only)**, so keep it as an optional config override, not the default. `tev1:4b` runs with `num_ctx` 2048 (its readme: keep state short, longest trained input ~1.5k tokens): truncate `state` to ~1500 tokens, tail-biased for logs. Use `probabilities`/`confidence` for `min_confidence`; noul values are not calibrated (retryable read 0.28-0.44 on failures), so threshold them on your own data. Neither model supports `/api/chat` (HTTP 400).
- **Fallback chain, never blocks:** Ollama unreachable / HTTP error / invalid JSON / schema-invalid / `confidence < min_confidence` → small primary-LLM call (`claude -p --model claude-haiku-5-5 --effort low --json-schema ...`, audited like any call) → if that fails too → deterministic default (`classify`: `feature`; `triage`: `retryable=true, category=other`; `summary`: truncation; `commit_message`: `T-xxx: title`; `assert`: escalate as "unknown"). Every fallback logged with reason.
- `route_ticket` is deterministic: `routing[type]` + role map + per-stage model/effort. No LLM.
- `mcp_server.py`: stdlib JSON-RPC 2.0 over stdio implementing MCP `initialize`, `notifications/initialized`, `tools/list`, `tools/call`, `ping`. Exposes the router functions as tools `agyloop_classify_ticket`, `agyloop_triage_failure`, `agyloop_summarize`, `agyloop_commit_message`, `agyloop_assert`, `agyloop_health`, plus read-only `agyloop_status` (run summary). The runner imports `router.py` directly (no MCP hop); MCP is for agent sessions.

<a id="s2-11"></a>
### 2.11 Orchestrator handoff & compaction
- Runner is independent of any chat session; killing the orchestrator session loses nothing.
- `agyloop wait [--run R] [--timeout 3000]` returns on: escalation opened, run finished, runner died, or timeout (prints "still running: k/n closed"). The orchestrator skill loops `wait` and, on escalation, presents it to the user.
- **Automatic handoff**: plugin `SessionStart` hook (matchers `startup|resume|compact`) runs `agyloop status --brief --active`, injecting ≤15 lines about active runs into any new or compacted session. So compaction (Claude auto-compact) and fresh sessions both re-acquire state automatically.
- `/agyloop:resume`: `agyloop status`; if runner pid dead and run not finished → `agyloop run --resume --detach`; then continue the `wait` loop.
- Runner crash recovery on `--resume`: kill recorded worker process groups still alive; for each `in_progress` ticket, restart the current stage (same attempt; the half-done call's dir is renamed `<stage>.interrupted`); worktree changes are kept.

<a id="s2-12"></a>
### 2.12 Process control, budgets
- Each agent call: `subprocess.Popen(cmd, cwd=..., stdout=file, stderr=file, start_new_session=True, env=...)`; `wait(timeout)`; on timeout `os.killpg(SIGTERM)`, after `kill_grace_s` `SIGKILL`; meta records `timed_out: true`. pid stored in `meta.json` while running.
- Runner detach: `agyloop run --detach` re-execs itself with `start_new_session=True`, stdout to `runner.log`, writes pid to `runner.lock`.
- Budget counters in `run.json`: total agent calls; when `max_agent_calls_total` hit → stop scheduling, escalate "budget exhausted". No token/cost cap (record `usage`/`total_cost_usd` only).

<a id="s2-13"></a>
### 2.13 Audit record (`meta.json` per call)
`{call_id, stage, ticket, attempt, agent, adapter, cmd (argv, prompt replaced by path), cwd, requested {model, effort, mode}, observed {model, session_id, usage, cost}, started_at, ended_at, duration_s, exit_code, timed_out, schema_valid, error}` — plus `prompt.md`, `command.json`, `stdout.log`, `stderr.log`, `result.json` (the validated structured output).

<a id="s2-14"></a>
### 2.14 Tracker: local vs GitHub
- `local` (default when no GitHub remote): tickets in `tickets.json` only.
- `github`: on `approve tickets`, create one issue per ticket (`gh issue create --title "T-001: ..." --body <rendered> --label agyloop`) — the approval prompt states this will happen; issue numbers saved on tickets. Comments on block/escalation; issues are closed by the PR (`Closes #n` lines). Pushing the feature branch and `gh pr create` happen **only** via `agyloop pr` after `approve final`, which the user runs explicitly. Never auto-push, never merge.

<a id="s2-15"></a>
### 2.15 Packaging
Repo layout (final):

```
.claude-plugin/plugin.json        # name "agyloop", version, description
.claude-plugin/marketplace.json   # single-plugin local marketplace for `claude plugin install`
.mcp.json                         # {"agyloop-router": {"command": "agyloop", "args": ["mcp"]}}
hooks/hooks.json                  # SessionStart → agyloop status --brief --active
skills/grill/SKILL.md             # requirements gathering → requirements.md
skills/plan/SKILL.md              # planner + secondary review rounds + arbitration + plan approval
skills/tickets/SKILL.md           # wayfinder → tickets.json → ticket approval
skills/run/SKILL.md               # start runner, orchestrator wait loop, escalations
skills/resume/SKILL.md            # handoff entry point
skills/status/SKILL.md
skills/finish/SKILL.md            # final review gate, optional PR
pyproject.toml                    # name agyloop, requires-python >=3.9, no deps, script agyloop=agyloop.cli:main
agyloop/__init__.py
agyloop/cli.py                    # argparse subcommands
agyloop/config.py  agyloop/defaults.json
agyloop/state.py                  # run dir, atomic json, locks, events, status rendering
agyloop/schema.py  agyloop/schemas/*.json
agyloop/adapters.py               # Claude, Agy, Fake
agyloop/calls.py                  # run_call(): audit dir, Popen, timeout/kill, parse, validate, re-ask
agyloop/prompts/*.md
agyloop/planreview.py             # bounded plan review rounds
agyloop/pipeline.py               # per-ticket state machine
agyloop/scheduler.py              # deps, parallel pool, budgets, escalations
agyloop/gitops.py                 # worktrees, commit, rebase/ff merge, cleanup
agyloop/router.py                 # ollama + fallback
agyloop/mcp_server.py
agyloop/tracker.py                # local / github (gh)
agyloop/githooks/pre-push
tests/fake_cli.py  tests/test_*.py  tests/e2e.sh
README.md
```

Install:
- CLI: `uv tool install --editable /path/to/claude-agy-loop` (puts `agyloop` on PATH). Skills and `.mcp.json` call `agyloop` from PATH so they work in any host agent.
- Claude Code: `claude plugin marketplace add /path/to/claude-agy-loop` then `claude plugin install agyloop@agyloop` (or `--plugin-dir` for dev).
- agy: `agy plugin import claude` (imports installed Claude plugins) or `agy plugin install <path>`; validate with `agy plugin validate .`. Router MCP fallback: `agy mcp add agyloop-router agyloop mcp`. M7 verifies which path works and documents it.
- `agyloop doctor`: checks `claude`/`agy`/`ollama`/`gh`/`git` presence + versions, auth (one tiny `-p` call each, opt-in `--live`), Ollama model pulled, Python version. Prints fixes.

<a id="s2-16"></a>
### 2.16 CLI surface (final)
```
agyloop init [--repo PATH] [--primary claude] [--secondary agy] [--requirements FILE]   → prints run_id
agyloop review-plan [--run R]            # one secondary read-only review round on plan.md → result path + summary
agyloop approve plan|tickets|final [--run R]
agyloop tickets import FILE [--run R]    # validate schema+deps, classify missing types via router, write tickets.json
agyloop tickets show [--run R]           # table: id, type, stages, deps, title
agyloop run [--run R] [--parallel N] [--detach] [--resume]
agyloop wait [--run R] [--timeout S]
agyloop status [--run R] [--brief] [--active] [--json]
agyloop escalations [--run R]
agyloop unblock T-xxx --note TEXT | --skip | --reset-attempts   # user responses to escalations
agyloop stop [--run R]                   # SIGTERM runner; workers killed; resumable
agyloop pr [--run R]                     # requires approve final; push branch + gh pr create
agyloop clean [--run R]
agyloop route T-xxx [--run R]            # debug: show stages/agents/models for a ticket
agyloop mcp                              # stdio MCP server
agyloop doctor [--live]
```
`--run` defaults to the most recent run whose `repo` == cwd's git toplevel.

---

<a id="s3"></a>
## 3. Milestones

Each milestone ends with: tests green (`python3 -m unittest discover -s tests`), checkbox updated here, one commit.

<a id="m0"></a>
### M0 — Spikes and scaffold `[x]`
Goal: remove unknowns before building on them.
1. Probe `claude -p --output-format json --json-schema '<schema>'` → record real field names (session id, structured output, model usage) in §1.
2. agy write mode: in a throwaway git repo, run `agy -p "create hello.txt and run 'ls'" --sandbox --mode accept-edits` and again with `--mode auto`. Determine which mode edits files **and** runs shell (tests) non-interactively without hanging or needing skip-permissions. Record chosen mode in §2.4.
3. agy workspace scope: confirm edits are confined to cwd under `--sandbox`; check whether `--project`/`--new-project` is needed to avoid agy reusing an unrelated project context. Record.
4. Confirm agy `--conversation <id>` resume works in `-p` mode with `--json-schema`.
5. Confirm Claude `--resume <id>` works with `-p --json-schema`.
6. Confirm env `GIT_CONFIG_*` reaches agy's sandboxed shell (pre-push hook fires).
7. Ollama: `POST /api/chat` with `format: <schema>` on `qwen3.5:9b-mlx` and `tev1:4b`; record latency and schema adherence on 5 sample ticket classifications. Pick default router model.
8. Scaffold `pyproject.toml`, `agyloop/__init__.py`, `agyloop/cli.py` (argparse skeleton, `doctor` only), `tests/`.
- Done when: §1/§2.4/§2.10 updated with findings; `agyloop doctor` runs.
- Notes: Probes done, findings in §1/§2.4/§2.10. Deviations later tickets depend on: (a) agy `--mode auto` unavailable, use `accept-edits`; (b) agy shell needs user allow rules, so coder edits only and the runner runs tests/commits; (c) agy denials show as exit 0 + `status: SUCCESS` + no `structured_output` + `denied_actions`; (d) router has two Ollama paths: `/v1/systemone` with `tev1:4b` for decisions, `/api/chat` with `think:false` on `qwen3.5:9b-mlx` for generative calls (see §2.10). Scaffold: `pyproject.toml`, `agyloop/cli.py` (`doctor [--live]`), `tests/test_cli.py`.

<a id="m1"></a>
### M1 — Core: config, state, adapters, audited calls `[ ]`
Files: `config.py`, `defaults.json`, `state.py`, `schema.py`, `adapters.py`, `calls.py`, `tests/fake_cli.py`, `tests/test_core.py`.
- `config.load(repo) -> dict` (deep merge order §2.6), `config.agent_for(cfg, role)`.
- `state.Run` class: `create(repo, cfg)`, `load(run_id)`, `latest_for_repo(path)`, `write_json(rel, obj)` (atomic), `read_json`, `event(kind, **data)`, `render_status()`.
- `schema.validate(obj, schema) -> list[str]` errors.
- `adapters.ClaudeAdapter/AgyAdapter/FakeAdapter` with `build_cmd`/`parse` (§2.4).
- `calls.run_call(run, spec, *, stage, ticket=None, attempt=None) -> CallResult`: creates audit dir, writes prompt/command, Popen with process group, timeout kill, parses, validates schema, one re-ask on schema failure (resume same session, prompt = errors), writes meta/result, increments budget counter, emits events.
- `fake_cli.py`: reads `FAKE_SCRIPT` (JSON file mapping `stage` → list of responses consumed in order; supports `{"sleep": 999}` for timeout tests and `{"exit": 1}`), prints adapter-shaped JSON.
- Tests: cmd building for both adapters (no `--dangerously-skip-permissions` ever; read mode flags); parse of recorded real outputs from M0; timeout kills process group; schema re-ask; atomic write; config merge.
- Notes:

<a id="m2"></a>
### M2 — Front half: init, plan review, approvals `[ ]`
Files: `planreview.py`, `prompts/plan_review.md`, `schemas/plan-review.json`, `cli.py` (`init`, `review-plan`, `approve`), `tests/test_planreview.py`.
- `init`: create run, copy requirements, snapshot config, print run_id.
- `review-plan`: one round; secondary in read mode, cwd = target repo (read-only), prompt inlines requirements + plan; saves `plan-review/round-<n>/`; refuses when `n > max_plan_review_rounds` (tells primary to arbitrate and ask the user).
- Arbitration is done by the primary session (skill), which appends decisions to `plan-review/arbitration.md` (`agyloop` does not do arbitration).
- `approve plan|tickets|final`: hash binding (§2.3); `approve tickets` requires plan approved and hash unchanged.
- Tests: rounds bounded, hash mismatch refusal, fake secondary review flow.
- Notes:

<a id="m3"></a>
### M3 — Tickets + router `[ ]`
Files: `router.py`, `schemas/ticket*.json`, `schemas/router-*.json`, `cli.py` (`tickets import|show`, `route`), `tests/test_router.py`, `tests/test_tickets.py`.
- `tickets import`: schema check, unique ids, deps exist, cycle check (DFS), `≤ max_tickets`; missing/invalid `type` → `router.classify_ticket`; write `tickets.json`; invalidates any previous tickets approval.
- `router.*` per §2.10 with fallback chain; `route_ticket` deterministic.
- Tests: Ollama down (bad port) → fallback used and logged; fallback down (fake CLI exit 1) → deterministic default; low confidence → fallback; cycle detection; route table per type.
- Notes:

<a id="m4"></a>
### M4 — Sequential pipeline + git `[ ]`
Files: `gitops.py`, `pipeline.py`, `scheduler.py` (parallel=1 path), `prompts/{code,qa,validate,docs}.md`, `schemas/{code,qa,validate,docs}.json`, `githooks/pre-push`, `cli.py` (`run`, `status`, `escalations`, `unblock`, `stop`), `tests/test_pipeline.py`, `tests/test_gitops.py`.
- `gitops`: `ensure_feature_branch`, `add_worktree`, `commit_all`, `rebase_onto`, `ff_merge`, `remove_worktree`, `head_sha`, `agent_made_commits(wt, since_sha)`. All via `subprocess.run(["git", "-C", ...])`.
- `pipeline.run_ticket(run, ticket)` implements §2.8 incl. runner test rerun, feedback files, session resume on retry, close gate logging, escalation creation.
- `scheduler.run(run)`: refuse unless plan+tickets approvals valid; topological pick of next ready ticket; budgets; finish state `done` / `needs_user`; writes `status.json` after each transition.
- `run --detach` and `runner.lock` handling; `stop`.
- `unblock --note` appends note to feedback, resets status to pending (attempts kept unless `--reset-attempts`); `--skip` marks skipped (dependents become blocked-on-skipped → escalate).
- Tests (temp git repos + fake CLI): happy path 3 tickets → 3 commits on feature branch, linear; QA fail then pass; validate reject then approve (coder session id reused); max attempts → blocked + escalation, independent ticket still closes; agent commit detected; runner test rerun failing overrides agent "pass".
- Notes:

<a id="m5"></a>
### M5 — Parallel tickets `[ ]`
Files: `scheduler.py`, `gitops.py`, `tests/test_parallel.py`.
- `ThreadPoolExecutor(max_workers=parallel)`; a ticket is ready when all deps closed; merge lock around rebase+ff; state lock already in `state.py`.
- Rebase conflict → feedback → coder → retry (§2.9).
- Tests: 3 independent tickets with parallel=3 finish with 3 linear commits; two tickets editing the same line → one closes, other gets conflict feedback, then closes after fake coder "resolves"; timing test shows concurrency (fake sleep).
- Notes:

<a id="m6"></a>
### M6 — MCP server `[ ]`
Files: `mcp_server.py`, `.mcp.json`, `cli.py` (`mcp`), `tests/test_mcp.py`.
- Newline-delimited JSON-RPC over stdio; protocol version echo from `initialize`; tool input schemas from router schemas.
- Tests: spawn `agyloop mcp`, send initialize/tools/list/tools/call (Ollama-down path), assert responses.
- Manual check: `claude --mcp-config .mcp.json -p "call agyloop_health"`; `agy mcp add agyloop-router agyloop mcp` then list tools.
- Notes:

<a id="m7"></a>
### M7 — Plugin packaging, skills, hooks `[ ]`
Files: `.claude-plugin/{plugin.json,marketplace.json}`, `skills/*/SKILL.md`, `hooks/hooks.json`, `README.md`.
- Skills are host-agnostic: instructions + `agyloop` commands, no Claude-only tool names required.
  - `grill`: interview loop (one question at a time, propose defaults), write `requirements.md`, run `agyloop init --requirements`.
  - `plan`: write `plan.md` in run dir; loop `agyloop review-plan` ≤ max rounds; for each finding decide accept/reject with reason in `arbitration.md`, revise plan; show plan + unresolved disagreements to user; on explicit yes, `agyloop approve plan`.
  - `tickets`: split plan into tickets JSON (exit criteria must be checkable, plan_excerpt verbatim, deps minimal); `agyloop tickets import`; show `agyloop tickets show`; state tracker side effects (GitHub issues); on yes, `agyloop approve tickets`.
  - `run`: ask parallel N (default 1); `agyloop run --detach --parallel N`; loop `agyloop wait`; on escalation present it and act via `unblock` after user decides; on done → `finish`.
  - `resume`, `status`, `finish` (summary of commits, `git diff --stat base..feature`, open issues; on yes `agyloop approve final`; offer `agyloop pr` only for GitHub and only on explicit yes).
- `hooks.json`: SessionStart → `agyloop status --brief --active` (exit 0 silently when no runs).
- Verify: `claude plugin validate` (if available) / `--plugin-dir` smoke; `agy plugin validate .`; try `agy plugin import claude` and `agy plugin install <path>`; document what works.
- Notes:

<a id="m8"></a>
### M8 — GitHub tracker `[ ]`
Files: `tracker.py`, `cli.py` (`pr`), `tests/test_tracker.py` (gh faked via PATH shim).
- `tracker.detect(repo)`, `create_issues(tickets)`, `comment(ticket, text)`, `pr(run)` (push feature branch with `git push -u origin`, `gh pr create --base <default> --head agyloop/<run_id> --body` with `Closes #n` lines). `pr` refuses without valid final approval.
- Notes:

<a id="m9"></a>
### M9 — Resume, handoff, end-to-end `[ ]`
Files: `scheduler.py` (crash recovery §2.11), `cli.py` (`wait`, `clean`), `tests/test_resume.py`, `tests/e2e.sh`.
- Tests: kill runner mid-stage (fake sleep) → `run --resume` reruns stage, completes; worker pgid killed on resume; `wait` returns on escalation.
- `tests/e2e.sh` (manual, real CLIs): create disposable repo in `$TMPDIR` (tiny Python lib), canned requirements + 3-ticket tickets.json (one depends on another), run with real claude+agy+ollama, parallel 2; then repeat with Ollama stopped (fallback path); check 3 commits, no push, audit dirs complete.
- Notes:

---

<a id="s4"></a>
## 4. Verification matrix (REQUIREMENTS "Verification")

| Requirement | Covered by |
|---|---|
| Fake-CLI unit tests | M1–M6, M8, M9 unit tests |
| Disposable-repo E2E, 3 tickets | M9 `tests/e2e.sh` |
| Ollama-down fallback | M3 tests + M9 e2e second pass |
| Resume-from-disk | M9 `test_resume.py` |
| Never push / sandboxed workers | M1 cmd tests (flags), M4 pre-push + agent-commit detection |
| Hashed approvals | M2 tests |
| Budgets (attempts, wall-clock, totals) | M1 timeout test, M4 attempts, M4 total-calls test |
| Audit outside repo | M1 call test asserts files under `$AGYLOOP_HOME` |

<a id="s5"></a>
## 5. Deliberately out of scope (add when needed)
- Token/cost caps (requirements say none initially; usage is recorded).
- Auto-merge of PRs or pushing without `agyloop pr`.
- Web dashboard (status.json + `agyloop status` suffice).
- More than two agents in one run beyond per-role overrides.

<a id="appA"></a>
## Appendix A: Topic index

| Topic | Section |
|---|---|
| agy JSON output shape, `structured_output` | [1](#s1) |
| Approvals, plan/ticket hash binding | [2.3](#s2-3) |
| Agent CLI flags (read vs write, resume, model/effort) | [2.4](#s2-4) |
| Agent commit detection, push protection | [2.9](#s2-9) |
| Audit `meta.json` fields | [2.13](#s2-13) |
| Budgets, timeouts, defaults | [2.6](#s2-6), [2.12](#s2-12) |
| Close gate (when a ticket is done) | [2.8](#s2-8) |
| Config merge order | [2.6](#s2-6) |
| Context handoff between stages (inlined prompts) | [2.8](#s2-8) |
| Crash recovery, `--resume` | [2.11](#s2-11), [M9](#m9) |
| Escalation, `unblock` | [2.8](#s2-8), [2.16](#s2-16), [M4](#m4) |
| Fallback chain (Ollama → small Claude → default) | [2.10](#s2-10) |
| GitHub issues and PR | [2.14](#s2-14), [M8](#m8) |
| Human gates (plan, tickets, escalation, final) | [2.3](#s2-3), [M7](#m7) |
| Install (Claude plugin, agy, CLI) | [2.15](#s2-15) |
| MCP tool names | [2.10](#s2-10) |
| Merge strategy, rebase conflicts | [2.9](#s2-9), [M5](#m5) |
| Orchestrator session, `wait`, SessionStart hook | [2.1](#s2-1), [2.11](#s2-11) |
| Parallel execution | [2.9](#s2-9), [M5](#m5) |
| Plan review rounds, arbitration | [M2](#m2), [M7](#m7) |
| Prompt templates and variables | [2.8](#s2-8) |
| Retry feedback, session resume | [2.8](#s2-8) |
| Role map, overrides | [2.5](#s2-5) |
| Run directory layout | [2.2](#s2-2) |
| Schemas (ticket, stage results, router) | [2.7](#s2-7) |
| Type → stages routing table | [2.6](#s2-6) |
| Worktrees, feature branch, commits | [2.9](#s2-9) |

<a id="appB"></a>
## Appendix B: File index

| File | Purpose | Spec | Built in |
|---|---|---|---|
| `pyproject.toml` | package, `agyloop` script | [2.15](#s2-15) | [M0](#m0) |
| `agyloop/cli.py` | argparse subcommands | [2.16](#s2-16) | [M0](#m0)–[M9](#m9) |
| `agyloop/config.py`, `defaults.json` | config merge, role lookup | [2.5](#s2-5), [2.6](#s2-6) | [M1](#m1) |
| `agyloop/state.py` | run dir, atomic JSON, events, status | [2.2](#s2-2) | [M1](#m1) |
| `agyloop/schema.py`, `schemas/*.json` | stdlib validator, schemas | [2.7](#s2-7) | [M1](#m1)–[M4](#m4) |
| `agyloop/adapters.py` | Claude / agy / Fake adapters | [2.4](#s2-4) | [M1](#m1) |
| `agyloop/calls.py` | audited call, timeout kill, re-ask | [2.12](#s2-12), [2.13](#s2-13) | [M1](#m1) |
| `agyloop/planreview.py`, `prompts/plan_review.md` | bounded plan review | [2.3](#s2-3) | [M2](#m2) |
| `agyloop/router.py` | Ollama router + fallback | [2.10](#s2-10) | [M3](#m3) |
| `agyloop/gitops.py`, `githooks/pre-push` | worktrees, commit, merge, push block | [2.9](#s2-9) | [M4](#m4), [M5](#m5) |
| `agyloop/pipeline.py`, `prompts/{code,qa,validate,docs}.md` | per-ticket state machine | [2.8](#s2-8) | [M4](#m4) |
| `agyloop/scheduler.py` | deps, pool, budgets, resume | [2.8](#s2-8), [2.11](#s2-11), [2.12](#s2-12) | [M4](#m4), [M5](#m5), [M9](#m9) |
| `agyloop/mcp_server.py`, `.mcp.json` | stdio MCP server | [2.10](#s2-10) | [M6](#m6) |
| `.claude-plugin/*`, `skills/*/SKILL.md`, `hooks/hooks.json` | plugin, skills, SessionStart hook | [2.11](#s2-11), [2.15](#s2-15) | [M7](#m7) |
| `agyloop/tracker.py` | local / GitHub tracker, `pr` | [2.14](#s2-14) | [M8](#m8) |
| `tests/fake_cli.py`, `tests/test_*.py`, `tests/e2e.sh` | fake CLI, unit + e2e tests | [4](#s4) | [M1](#m1)–[M9](#m9) |
