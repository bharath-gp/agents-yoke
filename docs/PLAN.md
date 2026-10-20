# Implementation Plan: yoke (Primary/Secondary Agent Loop)

Source of truth for requirements: [REQUIREMENTS.md](REQUIREMENTS.md). This plan resolves every "open item" there into a decision and splits the work into milestones (M0–M9), each sized for one session. A later session should be able to pick the next unchecked milestone, read only this file plus the code, and implement it.

Status legend per milestone: `[ ]` todo, `[~]` in progress, `[x]` done. Update the checkbox and the "Notes" line when a milestone finishes.

**How agents should use this file:** read the [Table of contents](#toc), then jump to the milestone you are implementing. Each milestone lists the decision sections it depends on; [Appendix A](#appA) maps topics to sections and [Appendix B](#appB) maps source files to sections and milestones. Anchors (`#s2-8`, `#m4`, ...) are stable; do not rename them.

<a id="toc"></a>
## Table of contents

1. [Environment facts](#s1) — probed CLI flags, output shapes, local models
2. [Decisions on open items](#s2)
   - 2.1 [Shape of the system](#s2-1) — runner is the loop, thin orchestrator
   - 2.2 [Where state lives](#s2-2) — run dir layout under `~/.yoke`
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
   | [M10](#m10) | Search, research, fact-check agents | [m10](#m10) |
   | [M11](#m11) | Code review stage & iterative revisions | [2.5](#s2-5), [2.6](#s2-6), [2.8](#s2-8) |
   | [M12](#m12) | Run listing, `go` skill, multi-run resume | [2.11](#s2-11) |
   | [M13](#m13) | Model selection & configuration | [2.6](#s2-6) |
   | [M14](#m14) | Milestones, destination map, and charting | [2.5](#s2-5), [2.7](#s2-7), [2.8](#s2-8), [2.14](#s2-14), [2.16](#s2-16) |
   | [M15](#m15) | Web dashboard (read-only UI & HTTP server) | [2.2](#s2-2), [2.8](#s2-8), [2.16](#s2-16) |
   | [M16](#m16) | Per-run usage ledger and usage.json | [2.2](#s2-2), [2.8](#s2-8), [2.12](#s2-12), [2.13](#s2-13) |
   | [M17](#m17) | Gate hooks runner & pipeline close gate | [2.6](#s2-6), [2.8](#s2-8) |
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
| `ollama` | 0.40.4-rc1 | Local models: `tev1:4b`, `nimble-v3:9b-mxfp8`, `qwen3.5:9b-mlx` and `qwen3.8:27b-mlx` (chat models, unused by yoke). HTTP API on `127.0.0.1:11434`. |
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
- **One stdlib Python package `yoke`** + a **CLI `yoke`** does all deterministic work: state, adapters, pipeline, git, router, budgets, audit.
- **The runner is the loop.** `yoke run` is a plain, detached Python process that walks the ticket state machine. It is resumable from disk at any point.
- **The "Orchestrator" primary session is thin.** It is a Claude (or agy) chat that started the runner and calls `yoke wait`. `wait` blocks until an event needs attention (escalation, run finished, runner died) and prints a ≤20-line summary. The session never reads diffs or logs unless the user asks. This satisfies "orchestrator does no heavy lifting, reads summaries only", and makes session handoff trivial: any fresh session runs `/yoke:resume`.
- **Front half (grill → plan → review → tickets) runs inside the interactive primary session** via plugin skills that call `yoke` subcommands for the mechanical parts (secondary plan review, schema validation, hashing approvals).

<a id="s2-2"></a>
### 2.2 Where state lives
`$YOKE_HOME` (default `~/.yoke`) `/runs/<run_id>/` — **outside the target repo** (audit requirement). `run_id = <repo-basename>-<YYYYMMDD-HHMMSS>`.

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
  usage.json            # per-run token usage & cost summary, written at ticket close and run end
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
`yoke approve plan` stores `sha256(plan.md)`; `yoke approve tickets` stores `sha256(canonical tickets.json)`. Every later command that depends on them recomputes and **refuses** if the hash changed ("plan changed after approval; re-approve"). `final` approval is recorded before any `yoke pr`/push helper runs.

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

**agy shell access (M0 finding, deviation).** agy's headless `accept-edits` edits files but only runs shell commands the user has allow-listed. yoke will not edit the user's agy settings. Decision: the coder stage only edits files; the runner (not the agent) runs `test_command` and commits, which the close gate already requires. QA/docs stages that need shell use agy only if the user adds `command(...)` rules; `yoke doctor` (M1+) prints the exact rules to add. `AgyAdapter.parse` must fail the call when `structured_output` is absent or `denied_actions` is non-empty, so a permission denial escalates instead of passing silently.

`FakeAdapter` (tests) runs `tests/fake_cli.py` which replays scripted JSON responses keyed by stage, so the whole loop is testable offline (claudex-loop style).

Adding a new agent later = one adapter class + a config entry. Nothing else knows CLI flags.

<a id="s2-5"></a>
### 2.5 Roles
Default role map (config `roles`):

```json
{"primary": "claude", "secondary": "agy",
 "by_role": {"requirements": "primary", "planner": "primary", "wayfinder": "primary",
             "orchestrator": "primary", "plan_review": "secondary",
             "code": "secondary", "review": "auto", "qa": "secondary", "validate": "primary", "docs": "secondary",
             "search": "primary", "research": "secondary", "factcheck": "primary", "pr_body": "primary"}}
```
`by_role` values may be `primary`, `secondary`, a concrete agent name, or `auto` (for `review` only, resolving to the opposite of `code`). `wayfinder` maps `plan.md` into destination, milestones, and tickets (`chart.py`). CLI flags `--primary`/`--secondary` override at `yoke init`.

<a id="s2-6"></a>
### 2.6 Config
JSON (3.9 has no tomllib). Merge order: package `yoke/defaults.json` ← `~/.yoke/config.json` ← `<repo>/.yoke.json` ← CLI flags. Snapshot of the merged config is written to `run.json` at `init` so a resumed run uses the same settings.

Default budgets/limits:

| Key | Default |
|---|---|
| `max_attempts_per_ticket` | 3 |
| `max_plan_review_rounds` | 3 |
| `max_review_rounds` | 2 |
| `review_lenses` | `["security", "performance", "tests"]` |
| `max_tickets` | 40 |
| `max_agent_calls_total` | 300 |
| `parallel` | 1 (CLI `--parallel N`) |
| `timeouts_s` | plan_review 900, code 2400, review 1200, qa 1800, validate 1200, docs 900, router_fallback 120 |
| `kill_grace_s` | 15 |
| `router.ollama_url` | `http://127.0.0.1:11434` |
| `router.model` | `tev1:4b` (decision model, `/v1/systemone`; optional override `nimble-v3:9b-mxfp8`, noncommercial) |
| `router.llm_model` | `claude-haiku-5-5` (cheap agent call for generative router functions and decision fallback) |
| `router.min_confidence` | 0.7 |
| `router.fallback` | `{"agent": "claude", "model": "claude-haiku-5-5", "effort": "low"}` |
| `test_command` | `null` (QA result supplies it; repo config may pin it) |
| `tracker` | `"auto"` (`github` if `gh repo view` succeeds and remote is GitHub, else `local`) |
| `models` | `{}` (`{<agent>: {"plan": {"model": str, "effort": str}, "cheap": {...}}}`) |
| `budget` | `{"max_cost_usd": null, "max_tokens": null}` (optional caps, default null: disabled) |
| `limits` | `{"max_waits": 3, "backoff_s": 30, "max_wait_s": 7200, "patterns": {...}}` (rate limit pause and same-call retry) |
| `gate_hooks` | `{"after_stage": {"code": [], "qa": []}, "before_close": []}` (user config `~/.yoke/config.json` only; refused from repo config) |

Type → stages routing (config `routing`, overridable per type with model/effort per stage):

| type | stages |
|---|---|
| feature | code, review, qa, validate, docs |
| bugfix | code, review, qa, validate, docs |
| refactor | code, review, qa, validate |
| test | qa, validate |
| docs | docs, validate |
| chore | code, review, validate |
| spike | code, review, validate (no commit unless validate approves) |

<a id="s2-7"></a>
### 2.7 Schemas (`yoke/schemas/*.json`, JSON Schema draft-07 subset)
Validation done by a ~60-line stdlib validator in `schema.py` supporting `type, properties, required, items, enum, additionalProperties:false, minItems, minLength, pattern`. Same files are passed to `--json-schema`. Every agent result is validated again by the runner; invalid → counts as a failed call (one automatic re-ask with the validation error, then attempt failure).

- **ticket**: `id` (`^T-\d{3}$` or `^F-\d{3}$`), `title`, `type` (enum above), `description`, `exit_criteria` (≥1 strings), `depends_on` (ids), `files_hint` (strings), `plan_excerpt` (string, the relevant plan section verbatim), `milestone` (optional, `^MS-\d{2}$`), `notes` (optional), `doc_path` (optional).
- **ticket-list**: `{"destination"?: str, "out_of_scope"?: [str...], "milestones"?: [{"id": "^MS-\\d{2}$", "title": str, "exit_check": str|null}...], "tickets": [ticket...]}`; runner also checks: unique ids, deps exist, no cycles, count ≤ `max_tickets`, milestone validity.
- **chart**: same schema as `ticket-list` returned by wayfinder during `yoke tickets chart`, validated against `schemas/chart.json`.
- **plan-review**: `verdict` (`approve|changes_requested`), `findings[]` `{severity: blocker|major|minor, section, issue, suggestion}`, `summary`.
- **code**: `status` (`done|blocked`), `summary`, `files_changed[]`, `assumptions[]`, `open_questions[]`, `notes_for_qa`, `blocked_reason` (when blocked), optional `handoff` (`{interface_changes, new_files, gotchas}`).
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
    review:   run review agent (auto resolves to opposite of code agent)
    qa:       run secondary; then RUNNER re-runs result.test_command in worktree (independent check)
              fail (agent or rerun) → feedback → attempt += 1 → back to code
    validate: run primary read-only; prompt includes ticket, exit criteria, base sha, prior results;
              validator inspects `git diff <base_sha>` itself
              reject → feedback_for_coder → attempt += 1 → back to code
    docs:     run secondary
  close: all gates true → commit in ticket worktree → merge into feature branch → closed
attempt > max_attempts → blocked + escalation; dependents stay pending (shown as "waiting on blocked T-xxx")
```
Close gate (checked explicitly, logged): code `done` ∧ QA `pass` ∧ runner test rerun exit 0 ∧ validate `approve` with every `exit_criteria.met` true ∧ docs `done|not_needed` (only for stages in this ticket's route) ∧ gate hooks (`after_stage` code/qa, `before_close`) passing. Hook failures return output tail as retry feedback to the coder.

Retry feedback is a file `attempt-<n>/feedback.md` built from QA failures / validator issues / router triage hint, inlined into the next code prompt, and the coder session is resumed by ID.

Review stage loop (`_review_loop`): Bounded review-revision loop (up to `max_review_rounds` revisions, defaulting to 2). The runner decides blocking based on severity (blocker and major findings block; minor and nit findings do not block and are passed as optional suggestions). Blocker and major findings trigger coder revisions using the saved coder session (`resume=st['sessions']['code']`) under audit labels `review-r<n>` and `code-r<n>`. If findings remain unresolved when reaching the revision cap, the ticket does not fail: unresolved blocker/major findings are recorded in `st['review']['unresolved']`, emitted as a `review_unresolved` audit event, posted as comments to the ticket issue via `tracker.comment`, and forwarded via `results['review']` to validate and qa prompts. Furthermore, `yoke pr` formats any unresolved review findings from closed tickets (capped at 50000 characters) into an 'Unresolved review findings' section in the pull request description.

Context handoff: prompts **inline** the ticket JSON, plan excerpt, and prior-stage result JSON (small). Copies are saved in the attempt dir for audit. Inlining avoids sandbox path problems with files outside the worktree. Upstream handoff notes (interface changes, new files, gotchas) from closed `depends_on` ancestors are atomically stored at `tickets/<T>/handoff.json` on close and inlined into `code`, `qa`, and `validate` prompts via `$upstream` (capped at 4000 characters and labelled as data from another agent, not instructions).

Prompt templates: `yoke/prompts/<stage>.md` using `string.Template` (`$ticket`, `$plan_excerpt`, `$prior_results`, `$feedback`, `$base_sha`, `$worktree`, `$upstream`): `$` avoids clashing with JSON braces. Every worker prompt states: stay inside the worktree, do not commit, do not push, do not touch git config, return only the schema object.

Milestone gates and fix tickets:
- Sequential milestone barrier: tickets belonging to milestone `MS-k` are ready only when all preceding milestones `MS-i` (`i < k`) have passed or been waived (`passed|waived`).
- Milestone exit checks: when all tickets in milestone `MS-k` are closed or skipped, the runner evaluates its `exit_check` in integration worktree `worktrees/_integration`. If exit check passes (rc 0), milestone status becomes `passed`, logged as `milestone_passed`, and the milestone closes on GitHub.
- If exit check fails, the runner auto-generates a fix ticket (`F-xxx`, type bugfix, exit criteria checking that the exit check command exits 0) attached to the milestone, up to `max_fix_tickets_per_milestone` (default 2). Fix tickets are executed and merged like standard tickets.
- If fix tickets are exhausted without passing, the milestone fails and escalates. The user can unblock with `yoke unblock --milestone MS-xx [--note TEXT]` (resets fix count to retry) or `yoke unblock --milestone MS-xx --waive REASON` (records waiver reason in `map.md` decisions log and marks the milestone `waived`).

<a id="s2-9"></a>
### 2.9 Git, worktrees, commits, merging
- At `yoke run` (first start): require clean target repo state for the base (warn if dirty; base = `HEAD` sha). Create feature branch `yoke/<run_id>` at base, plus integration worktree `worktrees/_integration` on it.
- Per ticket: `git worktree add worktrees/T-001 -b yoke/<run_id>-T-001 <feature-branch tip>` (not `yoke/<run_id>/T-001`: git cannot nest a ref under an existing branch name). Same path for `parallel=1` and `parallel>1` (one code path).
- Commit: runner (never the agent) does `git add -A && git commit` in the ticket worktree with message from router `commit_message` (fallback: `T-001: <title>`), trailer `Yoke-Ticket: T-001`.
- **Merge strategy** (serialized by a merge lock): in ticket worktree `git rebase <feature-branch>`; if conflict → `git rebase --abort`, feedback "rebase conflict on files X; rebase onto <feature-branch> and resolve" → attempt += 1 → code stage (coder resolves). If rebase changed anything, rerun test command. Then in `_integration`: `git merge --ff-only yoke/<run_id>/T-001`. Result: linear history, one commit per ticket.
- **Files hint overlap deferral (T-0068)**: when `parallel > 1`, `scheduler._loop` defers any ready ticket whose `files_hint` overlaps (exact path or directory prefix) a currently running ticket's `files_hint`. Emits a deduplicated `ticket_deferred` event per (ticket, running) pair. Tickets with empty hints never block or defer.
- Cleanup: remove ticket worktree after close (keep branch until final review). `yoke clean` removes all worktrees/branches of a run after the user is done.
- **No push, ever, by the runner.** Push protection for workers: env `GIT_CONFIG_COUNT=1, GIT_CONFIG_KEY_0=core.hooksPath, GIT_CONFIG_VALUE_0=<pkg>/githooks` where `githooks/pre-push` exits 1; plus Claude `--disallowedTools "Bash(git push:*)"`; plus agy `--sandbox`; plus prompt rule. Post-call check: `git -C wt log` must show no new commits by the agent (warn + record if it did).

<a id="s2-10"></a>
### 2.10 Decision router + Ollama (MCP from day one)
- `router.py` = plain functions `classify_ticket, route_ticket, triage_failure, summarize, commit_message, assert_check, health`. Each returns `{"result":..., "source": "ollama"|"fallback"|"default", "model":..., "confidence":..., "fallback_reason":...}` and logs an event.
- **Ollama only runs decision models; generative work goes to the agents (user decision).** No chat/LLM model is used locally.
  - *Decision questions* (`classify_ticket`, `triage_failure`, `assert_check`): `POST /v1/systemone` with `{"model","state","questions":{name:{"type":"choice"|"noul"|"score","instructions","criteria"}}}` (up to 64 questions per call). Response `answers.<name>`: `choice` + `probabilities` + `confidence`, or `noul` (P(true)), or `score`; plus `usage`. Single forward pass, no generation. Add a `none`/`other` option. `state` is data, so pass ticket/log text there.
  - *Generative* (`summarize`, `commit_message`): a cheap agent call (`claude -p --model <router.llm_model> --effort low --json-schema ...`), audited like any call; on failure the deterministic default.
  - Ollama call: 60 s timeout, `urllib.request`.
- **Default decision model: `tev1:4b`.** 10/10 on 10 ticket-type classifications at ~0.75 s each (first call ~2.9 s load), 4/5 on failure triage (the miss was an ambiguous sample). `nimble-v3:9b-mxfp8`: same scores, ~1.6 s, and **CC BY-NC 4.0 (noncommercial only)**, so keep it as an optional config override, not the default. `tev1:4b` runs with `num_ctx` 2048 (its readme: keep state short, longest trained input ~1.5k tokens): truncate `state` to ~1500 tokens, tail-biased for logs. Use `probabilities`/`confidence` for `min_confidence`; noul values are not calibrated (retryable read 0.28-0.44 on failures), so threshold them on your own data. Neither model supports `/api/chat` (HTTP 400).
- **Fallback chain for decision functions, never blocks:** Ollama unreachable / HTTP error / invalid JSON / schema-invalid / `confidence < min_confidence` → small primary-LLM call (`claude -p --model claude-haiku-5-5 --effort low --json-schema ...`, audited like any call) → if that fails too → deterministic default (`classify`: `feature`; `triage`: `retryable=true, category=other`; `summary`: truncation; `commit_message`: `T-xxx: title`; `assert`: escalate as "unknown"). Every fallback logged with reason.
- `route_ticket` is deterministic: `routing[type]` + role map + per-stage model/effort. No LLM.
- `mcp_server.py`: stdlib JSON-RPC 2.0 over stdio implementing MCP `initialize`, `notifications/initialized`, `tools/list`, `tools/call`, `ping`. Exposes the router functions as tools `yoke_classify_ticket`, `yoke_triage_failure`, `yoke_summarize`, `yoke_commit_message`, `yoke_assert`, `yoke_health`, plus read-only `yoke_status` (run summary). The runner imports `router.py` directly (no MCP hop); MCP is for agent sessions.

<a id="s2-11"></a>
### 2.11 Orchestrator handoff & compaction
- Runner is independent of any chat session; killing the orchestrator session loses nothing.
- `yoke wait [--run R] [--timeout 3000]` returns on: escalation opened, run finished, runner died, or timeout (prints "still running: k/n closed"). It keeps waiting through rate limit pauses (`waiting_limit`) since they are not escalations. The orchestrator skill loops `wait` and, on escalation, presents it to the user.
- **Automatic handoff**: plugin `SessionStart` hook (matchers `startup|resume|compact`) runs `yoke status --brief --active`, injecting ≤15 lines about active runs into any new or compacted session. So compaction (Claude auto-compact) and fresh sessions both re-acquire state automatically.
- `/yoke:resume`: `yoke status`; if runner pid dead and run not finished → `yoke run --resume --detach`; then continue the `wait` loop.
- Runner crash recovery on `--resume`: kill recorded worker process groups still alive; for each `in_progress` ticket, restart the current stage (same attempt; the half-done call's dir is renamed `<stage>.interrupted`); worktree changes are kept. If `run.json` has `waiting_limit`, `scheduler.recover` resumes the pool wait (capped by `limits.max_wait_s`), sleeping in slices; upon wake, in-progress tickets re-run their attempt with `limit_waits` count preserved for `max_waits` escalation.

<a id="s2-12"></a>
### 2.12 Process control, budgets
- Each agent call: `subprocess.Popen(cmd, cwd=..., stdout=file, stderr=file, start_new_session=True, env=...)`; `wait(timeout)`; on timeout `os.killpg(SIGTERM)`, after `kill_grace_s` `SIGKILL`; meta records `timed_out: true`. pid stored in `meta.json` while running.
- Runner detach: `yoke run --detach` re-execs itself with `start_new_session=True`, stdout to `runner.log`, writes pid to `runner.lock`.
- Budget counters in `run.json`: total agent calls; when `max_agent_calls_total` hit or optional `budget` (`max_cost_usd`, `max_tokens`) exceeded → stop scheduling, let in-flight calls finish, escalate "budget cap exhausted". One-shot escalation: resume does not re-escalate. Agents counted: `max_tokens` checks total tokens across all agents (`totals.total_tokens`); `max_cost_usd` checks recorded Claude cost (`by_agent.claude.cost`, falling back to `totals.cost` if claude entry is absent).

<a id="s2-13"></a>
### 2.13 Audit record (`meta.json` per call)
`{call_id, stage, ticket, attempt, agent, adapter, cmd (argv, prompt replaced by path), cwd, requested {model, effort, mode}, observed {model, session_id, usage, cost}, started_at, ended_at, duration_s, exit_code, timed_out, schema_valid, error}` — plus `prompt.md`, `command.json`, `stdout.log`, `stderr.log`, `result.json` (the validated structured output).

<a id="s2-14"></a>
### 2.14 Tracker: local vs GitHub
- `local` (default when no GitHub remote): tickets in `tickets.json` only.
- `github`: on `approve tickets`, creates GitHub milestones and issues.
  - When milestones exist in `tickets.json`: creates one GitHub milestone per yoke milestone (`gh api repos/{owner}/{repo}/milestones -f title=<title>`, reading `.number`; on HTTP 422, looks up existing milestones by title via `--paginate`). Numbers are saved in `gh_milestones.json` (kept separate from `tickets.json` and `issues.json`).
  - Creates one issue per ticket (`gh issue create --title "T-001: ..." --body <rendered> --label yoke`), passing `--milestone <title>` if associated with a milestone (approval prompt states this will happen; issue numbers saved in `issues.json`).
  - Gate hooks: when a milestone gate passes, closes the GitHub milestone (`gh api -X PATCH .../milestones/N -f state=closed`). If exit checks fail, any generated fix tickets have issues created in the same milestone. Gate-time tracker actions are guarded: failures log `tracker_error` and never raise, keeping the runner resilient.
  - Comments on block/escalation; issues are closed by the PR (`Closes #n` lines). Pushing the feature branch and `gh pr create` happen **only** via `yoke pr` after `approve final`, which the user runs explicitly. Never auto-push, never merge. The PR title is derived from the plan (`prbody.title`), falling back to `yoke: <run_id>`. The PR body (`tracker.pr_body`) assembles the filled template (or built-in headings), `Closes #N` lines for tracked issues (reading only `issues.json`), and an unresolved review findings section (up to 20 findings not matched by `ACCEPT` lines in arbitration). PR creation never blocks on template fill errors.

<a id="s2-15"></a>
### 2.15 Packaging
Repo layout (final):

```
.claude-plugin/plugin.json        # name "yoke", version, description
.claude-plugin/marketplace.json   # single-plugin local marketplace for `claude plugin install`
.mcp.json                         # {"yoke-router": {"command": "yoke", "args": ["mcp"]}}
hooks/hooks.json                  # SessionStart → yoke status --brief --active
skills/grill/SKILL.md             # requirements gathering → requirements.md
skills/plan/SKILL.md              # planner + secondary review rounds + arbitration + plan approval
skills/tickets/SKILL.md           # wayfinder → tickets.json → ticket approval
skills/run/SKILL.md               # start runner, orchestrator wait loop, escalations
skills/resume/SKILL.md            # handoff entry point
skills/status/SKILL.md
skills/models/SKILL.md            # model discovery, inspect, configure, and reset
skills/go/SKILL.md                # end-to-end wrapper running grill, plan, tickets, and run
skills/finish/SKILL.md            # final review gate, optional PR
pyproject.toml                    # name yoke, requires-python >=3.9, no deps, script yoke=yoke.cli:main
yoke/__init__.py
yoke/cli.py                    # argparse subcommands
yoke/config.py  yoke/defaults.json
yoke/state.py                  # run dir, atomic json, locks, events, status rendering
yoke/schema.py  yoke/schemas/*.json
yoke/adapters.py               # Claude, Agy, Fake
yoke/calls.py                  # run_call(): audit dir, Popen, timeout/kill, parse, validate, re-ask
yoke/prompts/*.md
yoke/planreview.py             # bounded plan review rounds
yoke/pipeline.py               # per-ticket state machine
yoke/scheduler.py              # deps, parallel pool, budgets, escalations
yoke/gitops.py                 # worktrees, commit, rebase/ff merge, cleanup
yoke/router.py                 # ollama + fallback
yoke/mcp_server.py
yoke/tracker.py                # local / github (gh)
yoke/githooks/pre-push
tests/fake_cli.py  tests/test_*.py  tests/e2e.sh
README.md
```

Install:
- CLI: `uv tool install --editable /path/to/agents-yoke` (puts `yoke` on PATH). Skills and `.mcp.json` call `yoke` from PATH so they work in any host agent.
- Claude Code: `claude plugin marketplace add /path/to/agents-yoke` then `claude plugin install yoke@yoke` (or `--plugin-dir` for dev).
- agy: `agy plugin import claude` (imports installed Claude plugins) or `agy plugin install <path>`; validate with `agy plugin validate .`. Router MCP fallback: `agy mcp add yoke-router yoke mcp`. M7 verifies which path works and documents it.
- `yoke doctor`: checks `claude`/`agy`/`ollama`/`gh`/`git` presence + versions, auth (one tiny `-p` call each, opt-in `--live`), Ollama model pulled, Python version. Prints fixes.

<a id="s2-16"></a>
### 2.16 CLI surface (final)
```
yoke init [--repo PATH] [--primary claude] [--secondary agy] [--requirements FILE]   → prints run_id
yoke review-plan [--run R]            # one secondary read-only review round on plan.md → result path + summary
yoke approve plan|tickets|final [--run R]
yoke tickets chart [--feedback TEXT] [--run R] # wayfinder maps approved plan to destination, milestones, and tickets
yoke tickets import FILE [--run R]    # validate schema+deps, classify missing types via router, write tickets.json
yoke tickets show [--run R]           # table: id, type, stages, deps, title grouped by milestone
yoke map [--run R]                    # render destination, out-of-scope, milestone gates, frontier, decisions log
yoke run [--run R] [--parallel N] [--detach] [--resume]
yoke wait [--run R] [--timeout S]     # persists through waiting_limit; returns on escalation, done, stopped, died
yoke status [--run R] [--brief] [--active] [--json] # reports usage totals, top spenders, and waiting_limit
yoke escalations [--run R]
yoke unblock [T-xxx] [--note TEXT] [--skip] [--reset-attempts] | --milestone MS-xx [--note TEXT] [--waive REASON]   # user responses to escalations
yoke stop [--run R]                   # SIGTERM runner; workers killed; resumable
yoke pr [--run R]                     # requires approve final; push branch + gh pr create
yoke clean [--run R]
yoke route T-xxx [--run R]            # debug: show stages/agents/models for a ticket
yoke dashboard [--port N]             # serve local read-only web UI on 127.0.0.1 (shows waiting_limit banner and status)
yoke models [--list] [--show] [--set AGENT.TIER=MODEL] [--effort E] [--scope user|project|repo] [--reset [AGENT[.TIER]]]
yoke mcp                              # stdio MCP server
yoke doctor [--live]
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
8. Scaffold `pyproject.toml`, `yoke/__init__.py`, `yoke/cli.py` (argparse skeleton, `doctor` only), `tests/`.
- Done when: §1/§2.4/§2.10 updated with findings; `yoke doctor` runs.
- Notes: Probes done, findings in §1/§2.4/§2.10. Deviations later tickets depend on: (a) agy `--mode auto` unavailable, use `accept-edits`; (b) agy shell needs user allow rules, so coder edits only and the runner runs tests/commits; (c) agy denials show as exit 0 + `status: SUCCESS` + no `structured_output` + `denied_actions`; (d) Ollama runs only decision models (`/v1/systemone`, default `tev1:4b`); generative router calls (`summarize`, `commit_message`) go to a cheap agent model (`router.llm_model`), no local chat model (see §2.10). Scaffold: `pyproject.toml`, `yoke/cli.py` (`doctor [--live]`), `tests/test_cli.py`.

<a id="m1"></a>
### M1 — Core: config, state, adapters, audited calls `[x]`
Files: `config.py`, `defaults.json`, `state.py`, `schema.py`, `adapters.py`, `calls.py`, `tests/fake_cli.py`, `tests/test_core.py`.
- `config.load(repo) -> dict` (deep merge order §2.6), `config.agent_for(cfg, role)`.
- `state.Run` class: `create(repo, cfg)`, `load(run_id)`, `latest_for_repo(path)`, `write_json(rel, obj)` (atomic), `read_json`, `event(kind, **data)`, `render_status()`.
- `schema.validate(obj, schema) -> list[str]` errors.
- `adapters.ClaudeAdapter/AgyAdapter/FakeAdapter` with `build_cmd`/`parse` (§2.4).
- `calls.run_call(run, spec, *, stage, ticket=None, attempt=None) -> CallResult`: creates audit dir, writes prompt/command, Popen with process group, timeout kill, parses, validates schema, one re-ask on schema failure (resume same session, prompt = errors), writes meta/result, increments budget counter, emits events.
- `fake_cli.py`: reads `FAKE_SCRIPT` (JSON file mapping `stage` → list of responses consumed in order; supports `{"sleep": 999}` for timeout tests and `{"exit": 1}`), prints adapter-shaped JSON.
- Tests: cmd building for both adapters (no `--dangerously-skip-permissions` ever; read mode flags); parse of recorded real outputs from M0; timeout kills process group; schema re-ask; atomic write; config merge.
- Notes: Built `config.py` + `defaults.json`, `state.py`, `schema.py`, `adapters.py`, `calls.py`, `tests/fake_cli.py`, `tests/test_core.py` (26 tests green). Deviations later tickets depend on: (a) signature is `calls.run_call(run, adapter, spec, *, stage, ticket=None, attempt=None)`, the caller passes the adapter (`adapters.get(config.agent_for(cfg, role))`, or `FakeAdapter(cli)` in tests); (b) `CallSpec` gained `allowed_tools` (claude write allowlist, default `agents.claude.write_allowed_tools` in defaults.json); (c) `run_call` sets env `YOKE_STAGE` for every call (FakeAdapter keys scripts by it) and audit dirs are `tickets/<T>/attempt-<n>/<stage>/`, `plan-review/round-<attempt>/`, else `calls/<stage>-<call_id>/`; the schema re-ask lands in `<dir>/reask/` and counts as a call; `result.json` is written only for schema-valid output; (d) `Run.event()` also rewrites `status.json`; `Run.update(**kv)` patches `run.json`, `Run.incr_calls()` is the budget counter (no limit enforcement yet, scheduler's job); (e) `schemas/*.json` do not exist yet, `schema.load_schema(name)` is ready for them; each milestone adds its own; (f) FakeAdapter response shapes: `{structured_output, session_id}`, `{sleep, child_pidfile}`, `{exit}`, `{raw}`; (g) not built: `interrupted` call-dir renaming (M9), `status --brief` formatting (M7 hook), Run lock file / pid handling (M4).

<a id="m2"></a>
### M2 — Front half: init, plan review, approvals `[x]`
Files: `planreview.py`, `prompts/plan_review.md`, `schemas/plan-review.json`, `cli.py` (`init`, `review-plan`, `approve`), `tests/test_planreview.py`.
- `init`: create run, copy requirements, snapshot config, print run_id.
- `review-plan`: one round; secondary in read mode, cwd = target repo (read-only), prompt inlines requirements + plan; saves `plan-review/round-<n>/`; refuses when `n > max_plan_review_rounds` (tells primary to arbitrate and ask the user).
- Arbitration is done by the primary session (skill), which appends decisions to `plan-review/arbitration.md` (`yoke` does not do arbitration).
- `approve plan|tickets|final`: hash binding (§2.3); `approve tickets` requires plan approved and hash unchanged.
- Tests: rounds bounded, hash mismatch refusal, fake secondary review flow.
- Notes: Built `planreview.py`, `prompts/plan_review.md`, `schemas/plan-review.json`, `cli.py` (`init`, `review-plan`, `approve`), `tests/test_planreview.py` (12 tests, 38 total green). Later tickets depend on: (a) `planreview.check_approval(run, kind)` raises `ApprovalError` unless `kind` and its dependency chain (final -> tickets -> plan) are approved at the current hash; `yoke run` (M4) and `yoke pr` (M8) must call it; (b) the tickets hash is sha256 of canonical JSON (`sort_keys`, compact separators), so reformatting `tickets.json` does not invalidate; `final` is bound to the tickets hash; (c) `approvals.json` is `{kind: {sha256, at}}`; (d) the primary writes `plan.md` and `tickets.json` straight into the run dir; `review-plan` reads `plan.md` and optional `requirements.md`; (e) review rounds are counted by `plan-review/round-*` dirs, so a failed round still consumes one; (f) `cli._run(args)` resolves `--run` or the latest run for cwd's git toplevel, reuse it for later subcommands; (g) prompt template vars are `$requirements $plan $round $prior`.

<a id="m3"></a>
### M3 — Tickets + router `[x]`
Files: `router.py`, `schemas/ticket*.json`, `schemas/router-*.json`, `cli.py` (`tickets import|show`, `route`), `tests/test_router.py`, `tests/test_tickets.py`.
- `tickets import`: schema check, unique ids, deps exist, cycle check (DFS), `≤ max_tickets`; missing/invalid `type` → `router.classify_ticket`; write `tickets.json`; invalidates any previous tickets approval.
- `router.*` per §2.10 with fallback chain; `route_ticket` deterministic.
- Tests: Ollama down (bad port) → fallback used and logged; fallback down (fake CLI exit 1) → deterministic default; low confidence → fallback; cycle detection; route table per type.
- Notes: Built `router.py`, `tickets.py`, `schemas/{ticket,ticket-list,router-*}.json`, `cli.py` (`tickets import FILE|show`, `route T-001`, both take `--run`, default latest run for cwd), `schema.schema_path(name)`, `tests/test_router.py`, `tests/test_tickets.py` (stub Ollama HTTP server + FakeAdapter, 41 tests green). Deviations later tickets depend on: (a) router functions take `run` first: `classify_ticket(run, ticket, adapter=None)`, `triage_failure(run, log, adapter=None)`, `assert_check(run, claim, evidence, adapter=None)`, `summarize(run, text, adapter=None)`, `commit_message(run, ticket, diffstat='', adapter=None)`; `route_ticket(cfg, ticket)` and `health(cfg)` take cfg only; `adapter` defaults to `adapters.get(router.fallback.agent)`; fallback calls use stage `router` (audit `calls/router-<id>/`, `YOKE_STAGE=router`), model `router.llm_model`; (b) real `/v1/systemone` shapes: `choice` needs `criteria` as `{option: description|null}` (a list is HTTP 400), noul answer key is `noul`; Ollama `choice` `other` is not a valid ticket type so it is schema-invalid and falls back; (c) triage `retryable` = noul >= 0.2 (`RETRYABLE_MIN`, uncalibrated, tune later); assert default is `answer=false, confidence=0, reason="unknown"`; (d) `route_ticket` returns `{type, stages:[{stage, agent, ...overrides}]}`, overrides read from `routing[type].overrides[stage]` (e.g. model/effort), none in defaults.json; (e) `tickets import` classifies any ticket whose `type` is missing or not in the enum, then validates, and on success drops `approvals.json["tickets"]`; nothing is written on error; (f) `mcp_server.py` is M6's, not built here.

<a id="m4"></a>
### M4 — Sequential pipeline + git `[x]`
Files: `gitops.py`, `pipeline.py`, `scheduler.py` (parallel=1 path), `prompts/{code,qa,validate,docs}.md`, `schemas/{code,qa,validate,docs}.json`, `githooks/pre-push`, `cli.py` (`run`, `status`, `escalations`, `unblock`, `stop`), `tests/test_pipeline.py`, `tests/test_gitops.py`.
- `gitops`: `ensure_feature_branch`, `add_worktree`, `commit_all`, `rebase_onto`, `ff_merge`, `remove_worktree`, `head_sha`, `agent_made_commits(wt, since_sha)`. All via `subprocess.run(["git", "-C", ...])`.
- `pipeline.run_ticket(run, ticket)` implements §2.8 incl. runner test rerun, feedback files, session resume on retry, close gate logging, escalation creation.
- `scheduler.run(run)`: refuse unless plan+tickets approvals valid; topological pick of next ready ticket; budgets; finish state `done` / `needs_user`; writes `status.json` after each transition.
- `run --detach` and `runner.lock` handling; `stop`.
- `unblock --note` appends note to feedback, resets status to pending (attempts kept unless `--reset-attempts`); `--skip` marks skipped (dependents become blocked-on-skipped → escalate).
- Tests (temp git repos + fake CLI): happy path 3 tickets → 3 commits on feature branch, linear; QA fail then pass; validate reject then approve (coder session id reused); max attempts → blocked + escalation, independent ticket still closes; agent commit detected; runner test rerun failing overrides agent "pass".
- Notes: Built `gitops.py`, `pipeline.py`, `scheduler.py`, `runcmds.py` (CLI handlers for `run [--detach]`, `status [--json]`, `escalations`, `unblock`, `stop`; `cli.py` only gained `runcmds.add_parsers(sub)` plus a 2-line dispatch), `prompts/{code,qa,validate,docs}.md`, `schemas/{code,qa,validate,docs}.json`, `githooks/pre-push`, `tests/test_gitops.py`, `tests/test_pipeline.py` (72 tests green); `tests/fake_cli.py` responses gained `write` ({path: text} created in cwd) and `commit` (simulate an agent committing). Deviations later tickets depend on: (a) ticket branch is `<feature-branch>-T-001` (e.g. `yoke/<run_id>-T-001`), not `yoke/<run_id>/T-001`, because git cannot hold both `yoke/<run_id>` and `yoke/<run_id>/...` refs; (b) entry points are `scheduler.run(run, adapter_for=None, env=None)` and `pipeline.run_ticket(run, ticket, adapter_for=None, env=None, merge_lock=None)` where `adapter_for(role)` returns an adapter (role `router` for commit messages; default built from config); M5 passes a shared `threading.Lock` as `merge_lock` and wraps `run_ticket` in the pool; (c) per-ticket `state.json` is `{status: pending|in_progress|closed|blocked|skipped, attempt, sessions:{code}, base_sha, commit}`; feedback for attempt n is `tickets/T/attempt-n/feedback.md`; retry reuses the coder session via `sessions.code`; (d) `escalations.json` holds only open entries `{id, ticket, reason, at}` (ticket null = budget); `unblock` deletes the ticket's entries; the blocked attempt re-runs with the same number (attempts kept), `--reset-attempts` sets it to 1; (e) run states are `running|done|needs_user|stopped`, `yoke run` exits 0 only for `done` (2 for `needs_user`), the runner lock is `runner.lock` (pid, stale = dead pid), SIGTERM raises SystemExit so the state becomes `stopped`; `stop` also SIGTERMs worker process groups found via meta.json without `ended_at`; (f) an agent commit is detected after the code stage, logged as event `agent_commit` and undone with `git reset --soft` so the runner's commit stays the only one; before validate the runner runs `git add -A` so new files show in `git diff <base_sha>`; a rebase conflict returns feedback only (aborted rebase), the coder-side resolution is M5's; (g) not built: router `triage_failure` hints in feedback (feedback is QA failures, validator issues, runner rerun tail), `wait`, `status --brief/--active` (M7), interrupted-call dir renaming (M9), `--detach` is untested (no test spawns the re-exec), the test rerun has no process-group kill.

<a id="m5"></a>
### M5 — Parallel tickets `[x]`
Files: `scheduler.py`, `gitops.py`, `tests/test_parallel.py`.
- `ThreadPoolExecutor(max_workers=parallel)`; a ticket is ready when all deps closed; merge lock around rebase+ff; state lock already in `state.py`.
- Rebase conflict → feedback → coder → retry (§2.9).
- Tests: 3 independent tickets with parallel=3 finish with 3 linear commits; two tickets editing the same line → one closes, other gets conflict feedback, then closes after fake coder "resolves"; timing test shows concurrency (fake sleep).
- Notes: Built the pool in `scheduler._loop` (`ThreadPoolExecutor(max_workers=cfg["parallel"])`, one shared `threading.Lock` passed to `run_ticket` as `merge_lock`), `gitops.reapply_on`, a `_WT_LOCK` around `worktree add/remove`, `yoke run --parallel N` (saved into `run.json` config so `--detach` re-exec keeps it; 2 lines in `runcmds.py`), `tests/test_parallel.py` (4 tests, 84 total green). Deviations later tickets depend on: (a) rebase conflict no longer just aborts: under the merge lock the runner resets the ticket worktree to the feature tip and re-applies the ticket commit with `cherry-pick -n`, so conflict markers sit in the working tree for the coder; `base_sha` becomes the new tip and `commit` is cleared (next close makes a fresh single commit); the conflict consumes an attempt and feedback is `Rebase conflict on files ...`; (b) the budget check is not atomic across workers, so up to `parallel-1` calls can overshoot `max_agent_calls_total`; on exhaustion no new tickets start, in-flight ones stop at their next call, one budget escalation is recorded; (c) `tests/fake_cli.py` now flocks its position file (`<script>.lock`), but with parallel tickets the per-stage responses go to whichever ticket calls first, so parallel tests must use interchangeable responses.\n
<a id="m6"></a>
### M6 — MCP server `[x]`
Files: `mcp_server.py`, `.mcp.json`, `cli.py` (`mcp`), `tests/test_mcp.py`.
- Newline-delimited JSON-RPC over stdio; protocol version echo from `initialize`; tool input schemas from router schemas.
- Tests: spawn `yoke mcp`, send initialize/tools/list/tools/call (Ollama-down path), assert responses.
- Manual check: `claude --mcp-config .mcp.json -p "call yoke_health"`; `agy mcp add yoke-router yoke mcp` then list tools.
- Notes: Built `mcp_server.py` (`handle`, `serve`, `call_tool`), `.mcp.json`, `cli.py` (`mcp`, 4 lines), `tests/test_mcp.py` (8 tests, 61 total green; subprocess with PATH = git only so the fallback agent fails offline). Deviations later tickets depend on: (a) tool input schemas are inline in `mcp_server.TOOLS` (router `schemas/router-*.json` describe outputs, not inputs); (b) the server needs no `initialize` state and answers any method order; protocol version is echoed, default `2025-03-26`; (c) run resolution is `YOKE_RUN`, else latest run for cwd's git toplevel; `yoke_health` works with no run (uses default config), every other tool returns an `isError` result when there is no run, as do unknown tools, missing required args and any handler exception; (d) results are one text block holding the router JSON (`{result, source, ...}`), `yoke_status` returns `Run.status()`; (e) manual: `claude --mcp-config` with `yoke_health` returned `{"ollama": true, "model": true}`; `agy mcp add yoke-router yoke mcp` NOT run (it edits the user's global agy config; M7/README should document it); `yoke` is not on PATH in this checkout, so `.mcp.json` assumes `uv tool install --editable .`.

<a id="m7"></a>
### M7 — Plugin packaging, skills, hooks `[x]`
Files: `.claude-plugin/{plugin.json,marketplace.json}`, `skills/*/SKILL.md`, `hooks/hooks.json`, `README.md`.
- Skills are host-agnostic: instructions + `yoke` commands, no Claude-only tool names required.
  - `grill`: interview loop (one question at a time, propose defaults), write `requirements.md`, run `yoke init --requirements`.
  - `plan`: write `plan.md` in run dir; loop `yoke review-plan` ≤ max rounds; for each finding decide accept/reject with reason in `arbitration.md`, revise plan; show plan + unresolved disagreements to user; on explicit yes, `yoke approve plan`.
  - `tickets`: split plan into tickets JSON (exit criteria must be checkable, plan_excerpt verbatim, deps minimal); `yoke tickets import`; show `yoke tickets show`; state tracker side effects (GitHub issues); on yes, `yoke approve tickets`.
  - `run`: ask parallel N (default 1); `yoke run --detach --parallel N`; loop `yoke wait`; on escalation present it and act via `unblock` after user decides; on done → `finish`.
  - `resume`, `status`, `finish` (summary of commits, `git diff --stat base..feature`, open issues; on yes `yoke approve final`; offer `yoke pr` only for GitHub and only on explicit yes).
- `hooks.json`: SessionStart → `yoke status --brief --active` (exit 0 silently when no runs).
- Verify: `claude plugin validate` (if available) / `--plugin-dir` smoke; `agy plugin validate .`; try `agy plugin import claude` and `agy plugin install <path>`; document what works.
- Notes: Built `.claude-plugin/{plugin.json,marketplace.json}`, `hooks/hooks.json`, `skills/{grill,plan,tickets,run,resume,status,finish}/SKILL.md`, `yoke status --brief [--active]` (`runcmds.brief`, 3 lines), `tests/test_plugin.py` (8 tests, 104 total green). Install paths verified on claude 2.1.300 / agy 1.3.6 (then everything removed): (a) claude: `claude plugin validate .` passes; `claude plugin marketplace add <repo>` + `claude plugin install yoke@yoke` WORKS (7 skills, 1 hook, MCP server `yoke` from `.mcp.json`); (b) agy: `agy plugin install <repo path>` WORKS and reads `.claude-plugin/` (7 skills, 1 MCP server, 1 hook processed); `agy plugin import claude` does NOT work here ("No claude extensions found", it does not see plugins installed via a Claude marketplace); `agy plugin validate .` errors "missing plugin.json" (it wants the file at the root, yet `install` accepts the repo), so do not rely on it; `agy mcp add` was not run. Nothing was left installed: marketplace, plugin and agy plugin were removed and `claude plugin list`, `claude plugin marketplace list`, `agy plugin list` and `agy mcp list` show no yoke (also deleted the orphan `~/.claude/plugins/cache/yoke`). Deviations later tickets depend on: (a) the MCP server is named `yoke` (M6's `.mcp.json`), not `yoke-router`; (b) hook command is `command -v yoke >/dev/null 2>&1 && yoke status --brief --active || true`, silent when `yoke` is missing or no run; (c) `--active` = latest run for cwd's git toplevel that is not `done`, or is `done` without a `final` approval; with `--active` the command never fails and prints nothing when idle; `--brief` is 3 lines (run/state, tickets and calls, next-step hint: /yoke:run, /yoke:resume or /yoke:finish); (d) skills call `yoke wait`, which is M9's: until M9 lands the run skill's loop fails on that command (poll `yoke status` meanwhile); (e) skills are plain markdown + `yoke` commands and tests assert each named `yoke <sub>` is in a fixed known set, so M9 adding `wait`/`clean` needs no change there.

<a id="m8"></a>
### M8 — GitHub tracker `[x]`
Files: `tracker.py`, `cli.py` (`pr`), `tests/test_tracker.py` (gh faked via PATH shim).
- `tracker.detect(repo)`, `create_issues(tickets)`, `comment(ticket, text)`, `pr(run)` (push feature branch with `git push -u origin`, `gh pr create --base <default> --head yoke/<run_id> --body` with `Closes #n` lines). `pr` refuses without valid final approval.
- Notes: Built `tracker.py` (`detect(repo, cfg)`, `create_issues(run)`, `comment(run, tid, text)`, `pr(run)`, `TrackerError`), `cli.py` (`pr`; `approve tickets` creates issues; 14 lines), one hook line in `pipeline.escalate` (comments on the ticket's issue, never raises), `tests/test_tracker.py` (gh faked by a PATH shim, real git with a bare remote; 92 tests green). Live check on private `bharath-gp/yoke-scratch` (https://github.com/bharath-gp/yoke-scratch): issues, label, push, PR with `Closes #n`, escalation comment all worked. Deviations later tickets depend on: (a) issue numbers are stored in `issues.json` `{T-001: {number, url}}`, NOT on tickets (tickets.json is hash-approved; editing it would void the approval); (b) `create_issues` runs inside `yoke approve tickets` when `tracker.detect` is github, skips tickets already in `issues.json`, and a gh failure leaves the approval recorded, exits 1 and is resumed by re-running `yoke approve tickets`; creates label `yoke` with `gh label create --force`; (c) `detect`: config `local`/`github` wins, `auto` = origin URL contains github.com and `gh repo view` succeeds; (d) `pr` also requires tracker github, resolves the base via `gh repo view` default branch, pushes `run.json` `branch` with `git push -u origin`, stores `pr_url` in run.json, and is the only code that pushes; (e) comments only on escalations with a ticket id (not on budget escalations); (f) a second `yoke pr` pushes again and returns the open PR for the branch without editing its title or body.
- Notes (PR template fill): Built `prbody.py` and `tracker.pr_body`. `yoke pr` fills repo PR template using template lookup order (.github/PULL_REQUEST_TEMPLATE/ directory matching ticket type priority feature > bugfix > refactor > test > docs > chore > spike > research, .github/pull_request_template.md, .github/PULL_REQUEST_TEMPLATE.md, pull_request_template.md, docs/pull_request_template.md). Fills via agent (`pr_body` role, read mode) with deterministic fallback on error or invalid schema (logging `pr_fill_fallback` event). Checklist items are ticked only when verified (tests passing close gates), leaving unverified items unticked. If no template is found, uses built-in structure (Summary, Why, What changed, Verification, Review focus). PR title is derived from `plan.md` first '# ' heading (stripping 'Plan:'), with Closes #N lines and unresolved review findings (up to 20 findings not matched by `ACCEPT` lines in arbitration) appended.



<a id="m9"></a>
### M9 — Resume, handoff, end-to-end `[x]`
Files: `scheduler.py` (crash recovery §2.11), `cli.py` (`wait`, `clean`), `tests/test_resume.py`, `tests/e2e.sh`.
- Tests: kill runner mid-stage (fake sleep) → `run --resume` reruns stage, completes; worker pgid killed on resume; `wait` returns on escalation.
- `tests/e2e.sh` (manual, real CLIs): create disposable repo in `$TMPDIR` (tiny Python lib), canned requirements + 3-ticket tickets.json (one depends on another), run with real claude+agy+ollama, parallel 2; then repeat with Ollama stopped (fallback path); check 3 commits, no push, audit dirs complete.
- Notes: Built `scheduler.recover` (called by every `scheduler.run`: kills worker process groups from `meta.json` without `ended_at`, guarded by `getpgid(pid)==pid`; renames the stage dir to `<stage>.interrupted[N]`; then the whole attempt of each in_progress ticket restarts, same attempt number, worktree kept, coder session reused only from attempt 2 on), `scheduler.kill_workers` (moved from runcmds; `stop` uses it), `yoke wait [--timeout 3000]` (`runcmds.wait_for`; reasons escalation|done|needs_user|stopped|died|timeout; exit 0 for done/stopped/timeout, 2 otherwise), `yoke clean` (removes worktrees, keeps branches and audit; refuses a live runner), `tests/test_resume.py` (7 tests, 113 total green), `tests/e2e.sh [normal|fallback]`. E2E on real claude 2.1.300 + agy 1.3.6 + ollama tev1:4b, 3 tickets (one dep, one untyped), parallel 2: normal pass PASS (13 calls, 3 commits, nothing pushed to a bare origin, classify via ollama); fallback pass PASS (router Ollama URL pointed at a dead port instead of stopping the server, so Ollama was never touched; classify went to the claude-haiku fallback; 3 commits, nothing pushed). Findings: (a) first e2e exposed #12 (agy tried `RunCommand` in headless mode, each denial burned an attempt; fixed by no-shell prompts); (b) agy still has rare transient denials (e.g. `ViewFile`) that cost one attempt and recover on retry, seen once in the fallback pass, so e2e audit checks require every call audited with `ended_at`, not every call successful; (c) restart after a crash re-runs the attempt from the first stage, not only the interrupted one (a ponytail simplification); (d) not tested live: `--detach` plus `yoke wait` against a real detached runner, and a real kill/resume with real agents (covered with the fake CLI).

<a id="m10"></a>
### M10: Search, research, fact-check agents `[x]`
Files: `research.py`, `pipeline.py`, `adapters.py`, `defaults.json`, `schemas/{search,research,factcheck}.json`, `prompts/{search,research,factcheck}.md`, `tests/test_research.py`.
- Roles `search` (claude), `research` (agy), `factcheck` (claude); new ticket type `research` routes `search -> research -> factcheck`. Standalone: `yoke search QUERY`, `yoke research TOPIC [--depth quick|normal|deep]`, `yoke factcheck FILE`.
- Search tools: config `search.tools` (ordered; default WebSearch, WebFetch, Skill, `mcp__firecrawl`) is added to claude's read-mode allowlist for the `search` and `factcheck` stages only; the prompt tells the agent to use the first that works and report `tools_used`. agy has no shell and cannot search, so the researcher works from the searcher's verbatim excerpts.
- Researcher writes `docs/research/<slug>.md` and returns a claims list; the runner checks the file exists. Fact checker gives each claim supported|unsupported|refuted|unverifiable and each source ok|dead|mismatch|irrelevant. Runner (not the agent's `verdict`) fails the attempt on any refuted/unsupported claim, dead/mismatch source, or an empty claims list; unverifiable passes. Failure feedback goes to the next attempt, which reruns all three stages, up to `max_attempts_per_ticket`, then escalates.
- Depth presets in `research.depths` (sources, search rounds); `research.default_depth` picks one. Ticket flow uses the default; the doc is committed by the runner. Standalone runs in the repo checkout, leave the doc uncommitted, and create a run with `kind: "research"` (hidden from `yoke status` and the SessionStart hook).
- Notes: (a) the search tool "auto-detect" is the agent choosing among `search.tools`, not the runner probing for MCP servers; (b) fact checking is automatic only in the `research` pipeline; `yoke factcheck` is the manual entry for any other output; (c) a retry re-searches, so every attempt costs three calls; (d) no plugin skills (`/yoke:research`) yet; (e) not run against real claude/agy, only the fake CLI.

<a id="m11"></a>
### M11: Code review stage & iterative revisions `[x]`
Files: `yoke/prompts/review.md`, `yoke/schemas/review.json`, `yoke/config.py`, `yoke/pipeline.py`, `yoke/tracker.py`, `yoke/calls.py` (audit `label`), `yoke/cli.py` (setup output), `tests/test_review.py`, `tests/test_prompts.py`, `tests/test_pipeline.py`, `tests/test_cli.py`, `tests/test_core.py`, `tests/test_tracker.py`.
- Automated code review stage inserted directly after `code` for feature, bugfix, refactor, chore, and spike tickets.
- Role `review` defaults to `auto`, resolving dynamically to the opposite of `code` (`claude` <-> `agy`). Overridable via `yoke setup --map review=<who>`.
- Iterative review loop (`_review_loop`) runs up to `max_review_rounds` revisions (default 2). Blocker and major findings trigger coder revisions using resumed session. Minor and nit findings are treated as advisory and do not block.
- Notes: Built `yoke/prompts/review.md`, `yoke/schemas/review.json`, config resolution for `auto` role mapping, iterative review loop with resumed coder sessions, unresolved findings reporting in tracking issues and PR description. Deviations: (a) `auto` role setting resolves dynamically to the opposite agent rather than requiring explicit configuration; (b) advisory-after-cap (unresolved blocker/major findings can still land; they reach qa/validate via `prior_results`): when the revision cap (`max_review_rounds`) is reached with remaining blocker/major findings, the ticket does not fail or block; unresolved findings are recorded in ticket state, emitted as `review_unresolved` audit event, commented on the tracking issue, and surfaced in the PR description under "Unresolved review findings".

<a id="m12"></a>
### M12: Run listing, `go` skill, and multi-run resume (0.3.0) `[x]`
Files: `yoke/state.py`, `yoke/runcmds.py`, `skills/go/SKILL.md`, `skills/resume/SKILL.md`, `skills/*/SKILL.md`, `tests/test_runs.py`, `tests/test_plugin.py`, `README.md`, `docs/PLAN.md`, `.claude-plugin/plugin.json`, `pyproject.toml`.
- Added run listing and inspection helpers (`Run.list_for_repo`, `Run.summary_dict`, `yoke runs [--active] [--json]`).
- Added `/yoke:go` all-in-one skill running grill -> plan -> tickets -> run -> finish with a single human gate at plan approval.
- Rewrote `/yoke:resume` to support run selection and targeting via `[run_id]`, integrated with `yoke runs --active`.
- Updated all skills with `--run` routing and bumped package version to 0.3.0.

<a id="m13"></a>
### M13: Model selection & configuration `[x]`
Files: `yoke/models.py`, `yoke/config.py`, `yoke/calls.py`, `yoke/cli.py` (`models`), `skills/models/SKILL.md`, `skills/grill/SKILL.md`, `skills/go/SKILL.md`, `README.md`, `docs/PLAN.md`, `tests/test_models.py`.
- Model tiers (`plan`, `cheap`): config schema and resolution in `config.py` mapping `plan` tier to `requirements`, `planner`, and `plan_review`, and `cheap` tier to all execution stages (`code`, `review`, `qa`, `validate`, `docs`).
- Model discovery and CLI: `models.py` queries `agy models` dynamically with static fallback, static catalog for `claude`; `yoke models` CLI supports `--list`, `--show`, `--set AGENT.TIER=MODEL`, `--effort E`, `--scope user|project|repo`, and `--reset`.
- Model fallback on error: `calls.py` automatically retries once without explicit model or effort in audit directory `fallback/` when an agent call fails with a model error, logging a `model_fallback` event.
- Interactive model skills: `/yoke:models` allows inspecting, configuring, or resetting models; `/yoke:grill` and `/yoke:go` prompt to confirm or update agent models before planning.
- Notes: Built `yoke/models.py` (model listing with `agy models` CLI call and static fallback catalog), updated `config.py` for model configuration merging across scopes, implemented automatic single-retry fallback on model errors in `calls.py`, added `yoke models` subcommand in `cli.py`, created `skills/models/SKILL.md`, and integrated model selection steps into grill and go skills.

<a id="m14"></a>
### M14: Milestones, destination map, and charting (0.5.0) `[x]`
Files: `yoke/milestones.py`, `yoke/chart.py`, `yoke/schemas/chart.json`, `yoke/prompts/chart.md`, `yoke/runcmds.py`, `yoke/scheduler.py`, `yoke/tracker.py`, `yoke/cli.py`, `skills/tickets/SKILL.md`, `skills/go/SKILL.md`, `skills/run/SKILL.md`, `skills/finish/SKILL.md`, `tests/test_milestones.py`, `tests/test_chart.py`, `README.md`, `CLAUDE.md`, `docs/PLAN.md`.
- Milestone extraction and mapping: `milestones.py` handles destination, out-of-scope boundaries, milestone list, ticket merging with fix tickets, and milestone state persistence (`milestones.json`).
- Wayfinder charting: `chart.py` and `yoke tickets chart [--feedback TEXT]` invoke the `wayfinder` role to map `plan.md` to destination, milestones, and tickets adhering to `schemas/chart.json`, audited under `chart/round-<n>`.
- Implementation map and frontier: `yoke map` renders destination, out-of-scope items, per-milestone gate status, exit checks, tickets with status/blockers, execution frontier, and decisions log (`map.md`).
- Milestone gate scheduling and fix tickets: `scheduler.py` enforces sequential milestone barriers; executes integration `exit_check` when a milestone's tickets complete; auto-generates fix tickets (`F-xxx`) on check failures up to `max_fix_tickets_per_milestone`; escalates on exhaustion.
- Escalation and unblock: `unblock --milestone MS-xx [--note TEXT]` resets fix counts to re-test; `unblock --milestone MS-xx --waive REASON` logs waivers to `map.md` and marks the gate waived.
- GitHub tracker integration: creates GitHub milestones upon ticket approval, associates ticket issues with milestones, creates fix ticket issues, and closes GitHub milestones when gates pass.
- Notes: Built `yoke/milestones.py`, `yoke/chart.py`, added `yoke tickets chart` and `yoke map` commands, updated `scheduler.py` with milestone barriers and exit check runner re-testing, enhanced `tracker.py` with milestone lifecycle integration, updated all relevant skills (`tickets`, `go`, `run`, `finish`), and bumped package version to 0.5.0.
- Deviation (0.7.0, ticket numbering): ticket ids no longer start at T-001 each run. On the first `yoke tickets chart` the runner stores `ticket_offset` in `run.json` = highest GitHub issue or PR number (`gh issue list` + `gh pr list`, all states), so tickets are `T-<offset+1>`, `T-<offset+2>`, ... (4 digits, `depends_on` remapped; agent-supplied ids are placeholders). `gh` failing or tracker `local` gives offset 0 (T-0001) plus a warning. Re-charting reuses the stored offset, so `tickets.json` and approval hashes stay stable. `id_counter` in `run.json` is a shared counter: fix tickets take `F-<next>` from it (`Run.alloc_number`, also scans `tickets.json`/`fixes.json`, so legacy runs without an offset and recovery never duplicate). `approve tickets` warns when a created issue number differs from the ticket number; ids are never renamed and the real number stays in `issues.json`.

<a id="m15"></a>
### M15: Web dashboard (read-only UI & HTTP server) (0.6.0) `[x]`
Files: `yoke/dashboard.py`, `yoke/dashboard.html`, `yoke/cli.py` (`dashboard`), `skills/dashboard/SKILL.md`, `tests/test_dashboard.py`, `tests/test_plugin.py`, `README.md`, `CLAUDE.md`, `docs/PLAN.md`, `.claude-plugin/plugin.json`, `pyproject.toml`.
- Dashboard data layer: `dashboard.py` exposes `list_runs(home)` and `run_detail(run_id, home)` assembling state, tickets, milestone gates from `milestones.json`, open escalations, decisions and gate waivers from `map.md`, and reverse-chronological event streams with tail-reading optimization (`read_events_tail`).
- Local HTTP server: `serve(host, port, home)` serves single-file UI at `GET /` and JSON endpoints `GET /api/runs` and `GET /api/runs/<id>`. Includes 404 for unknown endpoints or runs, automatic port increment retry (`bind_server`), and `yoke dashboard [--port N]` CLI subcommand.
- Single-file web interface: `dashboard.html` with zero external dependencies, dark and light theme support via `prefers-color-scheme`, responsive layout, active run badge, stage strip breadcrumbs, progress bar, milestone gate cards, ticket status table, escalations, event stream, and decisions log. Uses URL hash routing (`#<run_id>`) and periodic 3-second polling with visibility awareness. Safe rendering via `textContent`.
- Plugin skill: `/yoke:dashboard [--port N]` skill for Claude and Antigravity agents.
- Notes: Built `yoke/dashboard.py`, `yoke/dashboard.html`, added `yoke dashboard` command in `cli.py`, created `skills/dashboard/SKILL.md`, and bumped package version to 0.6.0. Deviations: (a) read-only design: mutations, approvals, and escalations must still go through CLI or skills to preserve hash-bound verification and audit trail integrity; (b) tail reading: `read_events_tail` seeks from the end of `events.jsonl` rather than loading the whole file into memory.

<a id="m16"></a>
### M16: Per-run usage ledger and usage.json `[x]`
Files: `yoke/usage.py`, `yoke/pipeline.py`, `yoke/scheduler.py`, `tests/test_usage.py`, `docs/PLAN.md`, `README.md`.
- Usage aggregation: `usage.summarize(run)` recursively walks all call `meta.json` files under the run directory (including `reask/`, `fallback/`, and `.interrupted` attempt directories) and aggregates token usage fields and observed costs by stage, ticket, agent, and model, along with overall totals.
- Unreported cost handling: missing costs (such as from `agy`) are recorded as `"unreported"` (never 0). Mixed buckets record total cost and `unreported_calls`. Spenders are ranked by cost, falling back to total tokens if any ticket has unreported costs.
- Atomic persistence: `usage.write_usage(run)` writes `usage.json` atomically via `run.write_json` at each ticket close in `pipeline.py` and at run end in `scheduler.py`.
- Status, wait summary, and dashboard UI: `render_status()` in `state.py` adds usage totals and top 3 spenders to `yoke status` and `yoke wait`; missing agy cost renders as `"unreported"`, and partial/mixed costs display `(+N unreported)`. The dashboard `/api/runs/<id>` endpoint and `dashboard.html` display totals and a top 3 spenders table.
- Notes: Built `yoke/usage.py`, hooked into `pipeline.run_ticket` and `scheduler.run`, covered with unit and integration tests in `tests/test_usage.py`.

<a id="m17"></a>
### M17: Gate hooks runner & pipeline close gate `[x]`
Files: `yoke/hooks.py`, `yoke/config.py`, `yoke/defaults.json`, `yoke/pipeline.py`, `tests/test_gate_hooks.py`, `README.md`, `docs/PLAN.md`.
- Hook execution runner (`yoke/hooks.py`): executes hook commands in worktrees with `YOKE_TICKET`, `YOKE_STAGE`, `YOKE_ATTEMPT` environment variables. Process group terminated on timeout via SIGTERM followed by SIGKILL. Exit 0 maps to `pass`, exit 2 maps to `fail` (capturing stdout/stderr tail up to 1000 characters), and other non-zero or timeout maps to `gate_hook_error`.
- User-config-only gate hooks (`yoke/config.py`): `gate_hooks` loaded strictly from `~/.yoke/config.json`; definitions in repo `.yoke.json` are refused and logged with a warning.
- Defaults (`yoke/defaults.json`): defaults to `{"after_stage": {"code": [], "qa": []}, "before_close": []}`.
- Pipeline close gate integration (`yoke/pipeline.py`): executes `after_stage` hooks following `code` and `qa` stages and `before_close` hooks prior to commit. Failing hooks prevent commit and feed stderr/stdout tails back to the coder as retry feedback; results are recorded under `gate['gate_hooks']` in `close_gate` events and hook outputs are saved under `tickets/<id>/attempt-N/hooks/<stage>.log` and `.meta.json`.
- Notes: Built `yoke/hooks.py` (`run_hook`, `run_hooks`, `HookResult`), wired hooks into `pipeline.py` (`_run_gate_hook_stage` in attempt and close gates), updated `config.load` to reject repo `gate_hooks` securely, added defaults in `defaults.json`, documented in `README.md`, and added full test suite in `tests/test_gate_hooks.py`.

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
| Audit outside repo | M1 call test asserts files under `$YOKE_HOME` |

<a id="s5"></a>
## 5. Deliberately out of scope (add when needed)
- Token/cost caps (previously out of scope; optional budget caps `max_cost_usd` and `max_tokens` supported as of T-0060, disabled by default).
- Auto-merge of PRs or pushing without `yoke pr`.
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
| Model discovery and config (`yoke models`) | [2.6](#s2-6) |
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
| `pyproject.toml` | package, `yoke` script | [2.15](#s2-15) | [M0](#m0) |
| `yoke/cli.py` | argparse subcommands | [2.16](#s2-16) | [M0](#m0)–[M9](#m9) |
| `yoke/config.py`, `defaults.json` | config merge, role lookup | [2.5](#s2-5), [2.6](#s2-6) | [M1](#m1) |
| `yoke/models.py` | model discovery (`agy models`, claude static) | [2.6](#s2-6) | [M13](#m13) |
| `yoke/state.py` | run dir, atomic JSON, events, status | [2.2](#s2-2) | [M1](#m1) |
| `yoke/schema.py`, `schemas/*.json` | stdlib validator, schemas | [2.7](#s2-7) | [M1](#m1)–[M4](#m4) |
| `yoke/milestones.py` | milestone extraction, ticket merging, fix tickets | [2.7](#s2-7) | [M14](#m14) |
| `yoke/chart.py`, `prompts/chart.md`, `schemas/chart.json` | charting step: wayfinder maps plan to milestones/tickets | [2.16](#s2-16) | [M14](#m14) |
| `yoke/adapters.py` | Claude / agy / Fake adapters | [2.4](#s2-4) | [M1](#m1) |
| `yoke/calls.py` | audited call, timeout kill, re-ask | [2.12](#s2-12), [2.13](#s2-13) | [M1](#m1) |
| `yoke/planreview.py`, `prompts/plan_review.md` | bounded plan review | [2.3](#s2-3) | [M2](#m2) |
| `yoke/router.py` | Ollama router + fallback | [2.10](#s2-10) | [M3](#m3) |
| `yoke/gitops.py`, `githooks/pre-push` | worktrees, commit, merge, push block | [2.9](#s2-9) | [M4](#m4), [M5](#m5) |
| `yoke/pipeline.py`, `prompts/{code,review,review_*,qa,validate,docs}.md` | per-ticket state machine | [2.8](#s2-8) | [M4](#m4), [M11](#m11), [M17](#m17) |
| `yoke/scheduler.py` | deps, pool, budgets, resume | [2.8](#s2-8), [2.11](#s2-11), [2.12](#s2-12) | [M4](#m4), [M5](#m5), [M9](#m9) |
| `yoke/mcp_server.py`, `.mcp.json` | stdio MCP server | [2.10](#s2-10) | [M6](#m6) |
| `.claude-plugin/*`, `skills/*/SKILL.md`, `hooks/hooks.json` | plugin, skills, SessionStart hook | [2.11](#s2-11), [2.15](#s2-15) | [M7](#m7) |
| `yoke/prbody.py` | PR template matching and body fill | [2.14](#s2-14) | [M8](#m8) |
| `yoke/tracker.py` | local / GitHub tracker, `pr_body`, `pr` | [2.14](#s2-14) | [M8](#m8) |
| `yoke/usage.py` | per-run token usage & cost summary (`usage.json`) | [2.2](#s2-2), [2.13](#s2-13) | [M16](#m16) |
| `yoke/hooks.py` | hook runner with process-group kill on timeout | [2.6](#s2-6), [2.8](#s2-8) | [M17](#m17) |
| `yoke/dashboard.py`, `yoke/dashboard.html` | read-only dashboard server and UI | [2.16](#s2-16) | [M15](#m15) |
| `tests/fake_cli.py`, `tests/test_*.py`, `tests/e2e.sh` | fake CLI, unit + e2e tests | [4](#s4) | [M1](#m1)–[M9](#m9) |
