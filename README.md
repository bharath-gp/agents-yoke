# yoke

Multi-agent loop for large work items. Claude (primary) gathers requirements, plans, splits tickets and validates; Antigravity `agy` (secondary) codes, writes tests and docs. Agents are pluggable per role via `yoke/adapters.py`.

## Contents

- [How it works](#how-it-works)
- [Prerequisites](#prerequisites)
- [Install](#install)
- [Decision models](#decision-models)
- [Typical flow](#typical-flow)
- [Implementation map & milestone gates](#implementation-map--milestone-gates)
- [Web dashboard (yoke dashboard)](#web-dashboard-yoke-dashboard)
- [GitHub tracker & milestones](#github-tracker--milestones)
- [Pull requests (yoke pr)](#pull-requests-yoke-pr)
- [Research agents](#research-agents)
- [Tests](#tests)
- [Docs](#docs)
- [License](#license)

## How it works

1. Interactive: requirements, `plan.md`, one read-only secondary plan review, ticket split. You approve the plan and tickets (hash-bound).
2. Unattended: a detached runner takes each ticket through code, review, QA, validate, docs. Following the `code` stage, a test-fix loop can re-run the test command and resume the coder to fix failures before advancing to review and QA. The validator runs no tests and evaluates diffs alongside verified test evidence from the runner. Failures retry with feedback up to a limit, then escalate to you. Independent tickets run in parallel, one git worktree each (`--parallel N`). When `parallel > 1`, ready tickets whose `files_hint` overlaps (exact file path or directory prefix) any currently running ticket are deferred until that ticket finishes, emitting a `ticket_deferred` audit event (tickets with empty hints never block). Upstream handoff notes (interface changes, new files, gotchas) from closed ancestor tickets are automatically stored at `tickets/<T>/handoff.json` and inlined into dependent `code`, `QA`, and `validate` prompts (`$upstream`). Pending or in-progress tickets can be steered with `yoke nudge T-xxx --note TEXT`, which appends user guidance to the next attempt or review round feedback without touching `tickets.json` or interrupting in-flight calls.
3. A ticket is committed to feature branch `yoke/<run_id>` only when QA passes and the validator approves. The runner never pushes; `yoke pr` does, after `approve final`.
4. State and audit trail (prompts, commands, outputs) live in `~/.yoke/runs/<run_id>/` (`$YOKE_HOME` overrides), outside the target repo. Any session can resume from disk. An atomic usage ledger (`usage.json`) tracks token usage and costs by stage, ticket, agent and model at each ticket close and at run end; status, wait summaries, and the dashboard surface usage totals and top spenders.

A local Ollama model (`tev1:4b`) handles small routing/triage decisions; fallback is a small Claude call, so the loop never blocks.

## Prerequisites

Python >= 3.9, `git`, `claude`, `agy`, `ollama` with `tev1:4b`, `gh` (only for GitHub issues and `yoke pr`). Check with `yoke doctor [--live]`.

## Install

yoke is used from inside coding agents, as a plugin. Fastest, for Claude Code and Antigravity (installs the CLI too, asks user-wide vs this repo):

```bash
npx github:bharath-gp/agents-yoke                # flags: --scope user|project|local, --only claude,agy, --no-cli, --dry-run
```

Then in the agent run `/yoke:setup` (agy: the `setup` skill). Scopes: `user` = all your projects; `project` = this repo only, shared through git (`.claude/settings.json`); `local` = this repo only, not shared. agy has no scope option, so it is always user-wide.

Claude Code without npx:

```
/plugin marketplace add bharath-gp/agents-yoke
/plugin install yoke@yoke        # pick user or project scope
/yoke:setup
```

`/yoke:setup` installs the `yoke` CLI if missing (`uv tool install` from GitHub, `pipx` fallback), runs the environment check and records success, either user-wide (`~/.yoke/setup.json`) or for the current repo only (`~/.yoke/projects/`). Every other skill (`/yoke:go grill plan tickets run resume status finish models dashboard`) first runs `yoke setup --check` and tells you to run `/yoke:setup` if it fails. A SessionStart hook shows the same hint. Rerun setup or the installer to update. The repo must be readable with your git credentials (it is private until made public).

Development: `uv tool install --editable .`, then `claude plugin marketplace add . && claude plugin install yoke@yoke`.

### Role mapping and review

Agent roles are configured via `yoke setup --primary <agent> --secondary <agent> --map <role>=<who>`. Supported roles include `requirements`, `planner`, `wayfinder`, `orchestrator`, `plan_review`, `code`, `review`, `qa`, `validate`, `docs`, `search`, `research`, `factcheck`, `pr_body`, `hypotheses`, and `hypotheses_rank`.

The `review` role defaults to `auto`, resolving dynamically to the opposite of the `code` agent (`claude` <-> `agy`), ensuring code review is performed by a different agent than the implementer. For custom code agent names outside `AGENTS`, it falls back to `primary` (or `secondary` if `primary` matches `code`). `yoke setup` warns if an explicit override assigns the same agent to both code and review. Use `--map review=auto` to restore automatic resolution.

### Review lenses

During ticket execution, the `review` stage runs an iterative review loop (up to `max_review_rounds` revisions). Review lenses are **enabled by default** (`review_lenses` defaults to `["security", "performance", "tests"]`):

```json
{
  "review_lenses": [
    "security",
    "performance",
    "tests"
  ]
}
```

- **Dedicated lens prompts**: Dedicated lens prompts (`review_security.md`, `review_performance.md`, `review_tests.md`) evaluate changes focusing on vulnerability checks, performance bottlenecks, and test coverage.
- **Concurrent read-only evaluation**: Each lens runs independently in read-only mode without seeing findings from other lenses. When `parallel > 1`, lenses execute concurrently up to `min(len(lenses), parallel)` worker threads.
- **Deduplication**: Findings across lenses are merged and deduplicated by file and line, preserving the highest severity.
- **Opting out**: To turn off multi-lens review and restore legacy single-review execution byte-for-byte, set `review_lenses: []` in `~/.yoke/config.json` or `<repo>/.yoke.json`:

```json
{
  "review_lenses": []
}
```

Each lens call counts against `max_agent_calls_total`. Blocker and major findings trigger coder revisions using the resumed session. If findings remain unresolved when reaching the revision cap, the ticket proceeds without failing: unresolved findings are recorded in ticket state, posted as issue comments, and surfaced in the `yoke pr` description under "Unresolved review findings".

#### Adversarial debate

Yoke supports adversarial review debates during ticket review and plan review:

```json
{
  "review_mode": "adversarial",
  "debate_rounds": 1,
  "timeouts_s": {
    "rebut": 900,
    "reply": 900,
    "judge": 900,
    "plan_rebut": 900,
    "plan_judge": 900
  }
}
```

- **Review mode**: Configured via `review_mode` (`"adversarial"` by default). Set to `"independent"` to run reviews without adversarial debate rounds. Any unknown or missing value defaults to `"adversarial"`.
- **Debate rounds**: Configured via `debate_rounds` (default: `1`). Controls the number of debate exchanges, clamped to at least `1`.
- **Debate timeouts**: Timeout limits in seconds under `timeouts_s`: `rebut` (900s), `reply` (900s), `judge` (900s), `plan_rebut` (900s), and `plan_judge` (900s).
- **Debate schemas**: Governed by strict JSON schemas (`yoke/schemas/rebut.json`, `yoke/schemas/reply.json`, `yoke/schemas/judge.json`) validating structured responses (`concede` or `dispute`), replies (`maintain` or `withdraw`), and judge rulings (`upheld` or `overruled`).
- **Ticket code review debate lifecycle**: When `review_mode` is `"adversarial"`, merged and deduplicated blocking findings (`blocker` and `major`) undergo an adversarial debate flow: lenses attack, the coder rebuts on its resumed session, an optional reviewer reply occurs when `debate_rounds > 1`, and a plan-tier judge rules on disputes:
  - **Rebut (`rebut`)**: The coder agent evaluates blocking findings in read mode using prompt extra variables (`findings`, `transcript`). Rebut reuses the existing coder session via `--resume`; if the resumed call fails, it automatically retries with a fresh session (`rebut-r<n>-<k>-fresh`). Missing IDs default to `dispute`. Conceded findings exit the debate as upheld.
  - **Reply (`reply`)**: When `debate_rounds > 1` and disputes remain, the reviewer agent evaluates the debate transcript in read mode (`maintain` or `withdraw`). Withdrawn findings exit as overruled (`"withdrawn by reviewer"`). Missing IDs default to `maintain`.
  - **Judge (`judge`)**: Any disputed findings still open after exchange rounds proceed to a plan-tier judge call in read mode (`judge-r<n>`) to rule on disputes. The judge issues rulings (`upheld` or `overruled` with reason). Missing rulings default to `upheld`. If all findings are conceded or withdrawn earlier, the judge call is skipped.
  - **Rulings and fix rounds**: Only conceded or judge-upheld blocker/major findings remain blocking and drive coder revision rounds (`code-r<n>`). Overruled findings are excluded from coder feedback and appended to `st['review']['overruled']` (`finding` and `reason`).
  - **Artifacts and audit**: For review pass `n`, the debate record is saved to `tickets/<tid>/attempt-<n>/debate-r<n>.json` (`{round, findings, transcript, rulings, upheld, overruled}`), and a `review_debate` audit event is emitted (`ticket`, `attempt`, `round`, `upheld`, `overruled`).
- **Plan review debate lifecycle**: When `review_mode` is `"adversarial"`, blocking findings (`blocker` and `major`) identified during a plan review round (`review_round` / `yoke review-plan`) trigger an automated planner rebuttal and plan-tier judge pass:
  - **Plan Rebut (`plan_rebut`)**: The planner agent evaluates blocking findings (numbered `p1..pn`) in read mode using the `rebut` schema and `plan_rebut.md` prompt. If the rebuttal call fails or schema validation fails, all blocking findings are treated as upheld (fail safe). Missing IDs default to disputed. Conceded findings exit as upheld.
  - **Plan Judge (`plan_judge`)**: Disputed IDs proceed to the plan review agent acting as judge in read mode using the `judge` schema and `plan_judge.md` prompt. The judge issues rulings (`upheld` or `overruled` with reason). A missing ruling or failed judge call defaults to upheld. If all findings are conceded during rebut, the judge call is skipped.
  - **Artifacts and audit**: For plan review round `n`, debate details are saved to `plan-review/round-<n>/debate.json` (`{upheld, overruled, rebuttals, rulings}`), audit calls are logged to `plan-review/round-<n>/rebut` and `plan-review/round-<n>/judge`, and a `plan_debate` audit event is emitted (`round`, `upheld`, `overruled`). When `debate.json` exists, `yoke review-plan` prints `debate: U upheld, O overruled (see <path>)` after the verdict line. Nested directories preserve the round count in `next_round`.

### Model selection and tiers

Models can be configured per agent across two tiers: `plan` (used for `requirements`, `planner`, `plan_review`, `wayfinder`) and `cheap` (used for all other execution stages such as `code`, `qa`, `docs`, `review`, and `validate`):

```json
"models": {
  "claude": {
    "plan": {"model": "claude-sonnet-5-5", "effort": "high"},
    "cheap": {"model": "claude-haiku-5-5", "effort": "low"}
  }
}
```

#### Dynamic tier and effort selection

For execution stages (`code`, `review`, `qa`, `validate`, `docs`), when neither `model` nor `effort` is explicitly specified in routing overrides, yoke dynamically selects model tier and effort:
- **Ollama decision pick**: Yoke queries Ollama (`tev1:4b`) to determine the model tier (`cheap` vs `plan`) and reasoning effort (`low`, `medium`, `high`) using ticket metadata (title, type, description, design, steps count, risks, and files hint count) and attempt number.
- **Independent thresholds**: Each question is accepted independently against its configured confidence threshold (`router.thresholds.model_tier` and `router.thresholds.effort`, default 0.7). If Ollama fails or confidence is below threshold, yoke falls back to defaults (`cheap` tier, or `plan` for roles in `PLAN_ROLES`), preserving deterministic offline behavior.
- **Retry bumping**: On retries (`attempt > 1`), if `router.retry_bump` is enabled (default `true`), reasoning effort is bumped by one step per attempt (`low` -> `medium` -> `high`). Bumping past `high` escalates the model tier to `plan`.
- **Precedence**: Explicit routing overrides in `routing.<ticket_type>.overrides.<stage>` always take precedence and bypass the Ollama pick entirely.

When an agent call fails due to a model error, yoke automatically retries once without a model (clearing both model and effort) under a `/fallback` audit directory and logs a `model_fallback` event.

Configure and inspect models via `yoke models`:

```bash
yoke models --list [--json]                                      # list available choices per agent
yoke models --show [--json]                                      # print effective plan/cheap models per agent
yoke models --set AGENT.TIER=MODEL [--effort E] [--scope user|project|repo]  # configure model (repeatable)
yoke models --reset [AGENT[.TIER]] [--scope user|project|repo]   # remove model configurations
```

- `--list`: queries available models for each agent (`agy models` dynamically, static list for `claude`).
- `--show`: displays effective `plan` and `cheap` models and effort settings.
- `--set`: sets model choices in user config (`~/.yoke/config.json`) or repo config (`<repo>/.yoke.json`).
- `--reset`: clears model settings for an agent, tier, or all entries.

In agent sessions, run `/yoke:models` (agy: the `models` skill) to interactively inspect, configure, or reset model choices. When kicking off runs with `/yoke:grill` or the end-to-end wrapper `/yoke:go`, a model selection prompt precedes `yoke init` to confirm or update agent models before planning.

### Budget configuration

Run-level execution budgets can be configured in `~/.yoke/config.json` (user-wide) or `<repo>/.yoke.json` (per-repo):

```json
{
  "max_agent_calls_total": 300,
  "budget": {
    "max_cost_usd": 25.00,
    "max_tokens": 1000000
  }
}
```

- `max_agent_calls_total`: Maximum total agent invocations across all tickets (default: 300).
- `budget.max_cost_usd`: Maximum spend cap in USD (default: `null`, disabled). Checks recorded Claude cost from `usage.summarize` (falling back to total cost).
- `budget.max_tokens`: Maximum total token usage cap across all agents (default: `null`, disabled).

When a budget cap is exceeded, the runner stops scheduling new work, allows in-flight calls to finish cleanly, and opens a single budget escalation. Resuming after a budget escalation does not re-escalate.

### Rate limits configuration

Agent rate limits and quotas are configured under `limits` in `~/.yoke/config.json` or `<repo>/.yoke.json`:

```json
{
  "limits": {
    "patterns": {
      "claude": [
        "(?i)rate[ _]?limit",
        "(?i)quota[ _]?exceeded",
        "(?i)usage[ _]?limit",
        "(?i)too many requests",
        "(?i)overloaded_error",
        "(?i)credit balance"
      ],
      "agy": [
        "(?i)resource_exhausted",
        "(?i)rate[ _]?limit",
        "(?i)quota[ _]?exceeded",
        "(?i)too many requests"
      ]
    },
    "max_wait_s": 7200,
    "max_waits": 3,
    "backoff_s": 30
  }
}
```

- **Unverified patterns warning**: Default regex patterns in `limits.patterns` are unverified against live provider rate limits (verified only using synthetic CLI output fixtures). Provider error messages and status formats may change over time.
- **Overriding limits**: You can override or extend `limits.patterns` for any agent (`claude`, `agy`) in `~/.yoke/config.json` or `<repo>/.yoke.json` to match custom or updated provider rate limit strings.
- `limits.max_wait_s`: Maximum backoff wait time in seconds when a limit is hit (default: 7200).
- `limits.max_waits`: Maximum consecutive wait cycles before opening an escalation (default: 3).
- `limits.backoff_s`: Base backoff interval in seconds (default: 30).

When an agent call matches a limit pattern, the adapter classifies the output into `CallResult.limit` with the raw text and any parsed reset timestamp. Calls stopped by rate limits do not count as failed attempts or schema errors, preventing unnecessary schema re-asks.

On `CallResult.limit`, the runner executes a pause and retry cycle:
1. Records a `limit_hit` event in the run audit log and records `waiting_limit` with `resume_at` in `run.json` (computed from the parsed reset time or exponential backoff capped by `limits.max_wait_s`).
2. Pauses the worker pool: no new agent calls are scheduled while in-flight calls finish cleanly.
3. Sleeps in short slices so `yoke stop` and `SIGTERM` interrupt cleanly.
4. Re-issues the same call with the same attempt number and session ID under the `limit-retry` audit label, uncharged against `agent_calls`.
5. Escalates once with the raw limit message after `limits.max_waits` consecutive waits for a single call.
6. Resumes in-progress waits automatically on restart via `scheduler.recover`.

During a rate limit wait:
- `yoke status` and `yoke status --brief` display `waiting for limit until <resume_at> (<ticket>)` alongside usage totals.
- `yoke wait` continues waiting (rate limit pauses do not trigger an early return, since they are not escalations).
- The web dashboard displays a prominent banner in the run detail view and an indicator row in the run picker.

### Gate hooks

Gate verification commands can be configured under `gate_hooks`:

```json
{
  "gate_hooks": {
    "after_stage": {
      "code": ["pytest tests/unit/test_quick.py"],
      "qa": ["npm run lint"]
    },
    "before_close": ["git diff --check"]
  }
}
```

- **User config only**: For security reasons, `gate_hooks` is accepted exclusively from user configuration (`~/.yoke/config.json`). Any `gate_hooks` defined in repo configuration (`<repo>/.yoke.json`) is strictly rejected with a warning log to prevent arbitrary command execution from untrusted repositories.
- **Execution environment**: Hooks run in the ticket worktree with environment variables:
  - `YOKE_TICKET`: Ticket identifier (e.g. `T-0065`).
  - `YOKE_STAGE`: Pipeline stage name (e.g. `code`, `qa`, `before_close`).
  - `YOKE_ATTEMPT`: Attempt number.
- **Exit codes & status**:
  - `0`: Pass (`status: pass`).
  - `2`: Gate check failure (`status: fail`, capturing stdout/stderr tail up to 1000 characters).
  - Other non-zero or timeout: Hook execution error (`status: gate_hook_error`).
- **Pipeline integration**: `after_stage` hooks run following `code` and `qa` stages; `before_close` hooks run prior to commit. Failures feed output tails back to the coder as feedback, and results are recorded in attempt audit logs (`attempt-N/hooks/<stage>.log`) and `close_gate` events.
- **Process control**: On timeout, the entire process group is terminated using SIGTERM followed by SIGKILL.

### Test-fix loop

Following the `code` stage and any `after_stage.code` gate hooks, the runner can re-run the test command and resume the coder to fix failures before advancing to review and QA:

```json
{
  "max_test_fix_rounds": 2
}
```

- `max_test_fix_rounds`: Maximum number of resume rounds for test fixes (default: `2`). Set to `0` to disable the loop.
- **Command source**: Uses `test_command` from run configuration (`run.cfg["test_command"]`), or falls back to `last_test_cmd` from a preceding QA attempt.
- **Safety check**: Verifies the command with `guard.check_command` prior to execution. Denied commands return retryable feedback; other non-allow decisions escalate.
- **Execution flow**: The runner stages changes (`git add -A`), re-runs the test command, and emits a `test_fix` audit event (`round=0`). If the test passes (`rc == 0`), execution proceeds directly. If it fails, the runner resumes the coder (audit labels `code-t1`, `code-t2`, ...) with test failure output for up to `max_test_fix_rounds` rounds until the test passes or rounds are exhausted.

### Failure hypotheses fan-out

When repeated failures occur across attempts on a ticket, yoke can optionally generate competing hypotheses and rank them to provide advisory guidance:

```json
{
  "hypotheses": 4
}
```

- `hypotheses`: Number of competing hypothesis calls to execute upon repeating failure on the final attempt (default: `0`, disabled).
- **Trigger condition**: Activates on the final attempt (`attempt == max_attempts_per_ticket`, with `attempt >= 2`) when the failure evidence matches the preceding attempt (for example, identical QA test failures, matching runner test failures, or recurring validator issues). When `hypotheses` is `0`, standard router triage hint is used instead.
- **Fan-out calls**: Runs `N` read-only calls (`hypotheses` role, default `secondary`) seeded with failure evidence and cycled round-robin across four categories: `spec gap`, `environment`, `test bug`, and `code bug`. Each candidate returns structured output (`schemas/hypotheses.json`) with `hypothesis`, `evidence`, `fix_plan`, and `confidence` (0.0 to 1.0).
- **Primary ranking call**: Followed by a single ranking call (`hypotheses_rank` role, default `primary`) evaluating candidates against failure evidence to select the winning hypothesis and synthesized fix plan. If ranking fails, the candidate with highest confidence is selected as a fallback. If the fan-out yields no candidates or encounters an error, yoke falls back to standard router triage.
- **Feedback integration**: The winning fix plan is appended to retry feedback as `Hypothesis fix plan (advisory only): ...` for the coder on the final attempt.
- **Audit and Budgeting**: Fan-out calls and the ranking call are saved under the attempt audit directory (`tickets/<tid>/attempt-<N>/hypotheses-1`, `hypotheses-rank`) and counted against `max_agent_calls_total`.
- **Pass/Fail independence**: Confidence scores are strictly advisory and never decide pass or fail; close gates strictly evaluate verified test suite execution and validator criteria.

### Runtime ticket steering (`yoke nudge`)

Pending or in-progress tickets can be steered dynamically without modifying `tickets.json` or interrupting in-flight calls:

```bash
yoke nudge <ticket_id> --note "<guidance text>"
```

- **Runtime injection**: Appends guidance directly to ticket runtime state without modifying `tickets.json` or aborting active agent operations.
- **Feedback loop**: The note is injected into prompt feedback on the ticket's next attempt or review revision round.
- **Use cases**: Guiding an agent back on track when monitoring progress, clarifying ambiguities, or suggesting alternative approaches without restarting the run.

### Run completion notification (`notify_command`)

Yoke can run an external shell command whenever a run finishes successfully (`run_done`) or needs user attention (`run_needs_user`):

```json
{
  "notify_command": "osascript -e \"display notification \\\"$YOKE_MESSAGE\\\" with title \\\"yoke\\\"\""
}
```

- **Configuration**: Set `notify_command` in `~/.yoke/config.json` only. Defaults to `null` (disabled). Like `gate_hooks`, it runs a shell command, so a `notify_command` in repo config (`<repo>/.yoke.json`) is refused with a warning log.
- **Trigger events**: Invoked from `scheduler.run`'s finalization block after emitting `run_done` or `run_needs_user`.
- **Environment variables**: Values are passed strictly through environment variables to prevent command injection:
  - `YOKE_EVENT`: Event name (`run_done` or `run_needs_user`).
  - `YOKE_RUN`: Run ID (e.g. `agents-yoke-20261026-125803`).
  - `YOKE_MESSAGE`: Human-readable summary: `run <run_id> done` or `run <run_id> needs you: <reason>` (with escalation reason truncated to 200 characters, defaulting to `needs user` if absent).
- **Execution and error handling**: Executed via `run_hook` with a 10-second timeout in the run directory. Existing `os.environ` is preserved so standard system utilities and `PATH` remain available. If the command exits non-zero, times out, or fails, a `notify_failed` event is logged to `events.jsonl` (with error output truncated to 500 characters). Notification failures never raise exceptions or disrupt the run.

## Decision models

yoke routes small, high-frequency decisions to a local Ollama decision model (`tev1:4b`, using `POST /v1/systemone` single forward passes) with configured confidence thresholds and deterministic fallbacks.

### Decision points

| Decision point | Default threshold | Question type | What it decides |
|---|---|---|---|
| `safety` | 0.7 | noul | Checks tool calls, commands, and staged diffs for secrets, destructive actions, and exfiltration. |
| `test_failure` | 0.7 | choice | Classifies QA test command failures as `product` (code retry), `testware` (QA restart), or `environment` (single rerun, then escalate). |
| `after_failure` | 0.85 | choice | Evaluates next action after a failed attempt as `retry`, `split` (escalates), `replan` (escalates), or `escalate`. |
| `gate_failure` | 0.85 | choice | Evaluates milestone exit check failure as `fix_ticket` or `escalate`. |
| `validator_reject` | 0.75 | choice | Routes validator rejection restart stage to `coder`, `qa`, `docs`, or `escalate`. |
| `model_tier` | 0.7 | choice | Dynamic model tier selection (`cheap` vs `plan`) for execution stages. |
| `effort` | 0.7 | choice | Dynamic reasoning effort selection (`low`, `medium`, `high`) for execution stages. |
| `skill` | 0.6 | noul | Scores and selects discovered allowlisted skills for `$skills` prompt injection. |
| `e2e_pick` | 0.6 | choice | Selects interactive element candidate action during browser e2e verification. |
| `e2e_verdict` | 0.8 | noul | Evaluates whether page text satisfies an exit criterion during browser e2e verification. |
| `source_screen` | 0.75 | choice | Screens search result sources for relevance and credibility (keep vs discard). |
| `claim_screen` | 0.8 | choice | Screens research claims against cited kept source excerpts (supported vs unsupported). |

### Configuration

Decision thresholds, safety guard, skill discovery, and browser e2e settings can be configured in `~/.yoke/config.json`:

```json
{
  "router": {
    "ollama_url": "http://127.0.0.1:11434",
    "model": "tev1:4b",
    "min_confidence": 0.7,
    "thresholds": {
      "safety": 0.7,
      "test_failure": 0.7,
      "after_failure": 0.85,
      "gate_failure": 0.85,
      "validator_reject": 0.75,
      "model_tier": 0.7,
      "effort": 0.7,
      "skill": 0.6,
      "e2e_pick": 0.6,
      "e2e_verdict": 0.8,
      "source_screen": 0.75,
      "claim_screen": 0.8
    },
    "retry_bump": true,
    "commit_message": "agent"
  },
  "guard": {
    "enabled": true,
    "chat_hook": true,
    "ask_floor": 0.4,
    "ollama_timeout_s": 5,
    "max_deleted_lines": 500,
    "fast_allow_tools": ["Read", "Grep", "Glob"],
    "deny_paths": [".env*", "~/.ssh/*", "~/.aws/*", "*.pem", "*.key", ".git/hooks/*"],
    "deny_commands": [
      "(?i)rm\\s+-rf?\\s+",
      "(?i)(curl|wget)\\b.*\\|\\s*(ba)?sh",
      "(?i)curl\\b.*-d\\s*@",
      "(?i)\\b(scp|nc)\\b"
    ],
    "secret_patterns": [
      "(?i)AKIA[0-9A-Z]{16}",
      "-----BEGIN [A-Z ]*PRIVATE KEY-----",
      "(?i)gh[pousr]_[A-Za-z0-9_]{36,}"
    ],
    "sensitive_files": [
      ".env*",
      "~/.ssh/*",
      "~/.aws/*",
      "*.pem",
      "*.key",
      ".git/hooks/*"
    ],
    "grey_files": [
      ".github/workflows/*",
      ".gitlab-ci.yml",
      ".githooks/*"
    ],
    "network_patterns": [
      "(?i)\\b(curl|wget|scp|nc|ssh|rsync|requests)\\b"
    ]
  },
  "skills": {
    "allowlist": [],
    "max": 3,
    "agy_mode": "inline",
    "inline_chars": 4000,
    "roots": []
  },
  "e2e": {
    "url": "http://localhost:3000",
    "start_command": "npm run dev",
    "ready_timeout_s": 60,
    "max_steps": 12,
    "candidates": 20
  },
  "agents": {
    "claude": {
      "setting_sources": "project,local"
    }
  },
  "timeouts_s": {
    "test": 600
  },
  "max_test_fix_rounds": 2,
  "notify_command": null
}
```

- **Thresholds & retry bump**: `router.thresholds` sets per-point confidence cutoffs. Points omitted from `thresholds` fall back to `router.min_confidence` (default: 0.7). When `router.retry_bump` is enabled (default: `true`), repeated failed attempts dynamically step up reasoning effort (`low` -> `medium` -> `high`) and model tier (`cheap` -> `plan`).
- **Commit message generation (`router.commit_message`)**: Controls how git commit messages are generated upon ticket close (`"agent"` or `"default"`, default `"agent"`). In `"agent"` mode, an LLM generates the commit subject and body with deterministic fallback on failure. In `"default"` mode, commit messages are built deterministically without an agent call: the subject is `<id>: <title>` (truncated to 72 characters) and the body is the validator summary (falling back to the diffstat).
- **Claude setting sources (`agents.claude.setting_sources`)**: Controls which setting sources Claude CLI calls inherit (default: `"project,local"`). Setting to `""` disables the `--setting-sources` flag. Note: global user settings in `~/.claude/settings.json` no longer apply inside yoke calls.
- **Test timeout (`timeouts_s.test`)**: Execution timeout in seconds for runner test command re-runs (default: `600`). If a test command hangs or exceeds this limit, its entire process group is terminated cleanly.
- **Test-fix loop (`max_test_fix_rounds`)**: Maximum number of resume rounds for test fixes immediately after the `code` stage (default: `2`). Setting to `0` disables the test-fix loop.
- **Run notifications (`notify_command`)**: External command executed upon run completion (`run_done`) or escalation (`run_needs_user`), default: `null`. Payload values are passed strictly through environment variables `YOKE_EVENT`, `YOKE_RUN`, and `YOKE_MESSAGE`. Example: `osascript -e "display notification \"$YOKE_MESSAGE\" with title \"yoke\""`.
- **User config only**: For security reasons, `guard` and `router.thresholds.safety` are strictly enforced from user configuration (`~/.yoke/config.json`). Any definitions in repository configuration (`<repo>/.yoke.json`) are refused with a warning log to prevent untrusted repositories from tampering with safety policies.
- **Environment override**: The `YOKE_OLLAMA_URL` environment variable overrides `router.ollama_url` across all runs, applied over user config and prior to repo config (used in tests to point to a dead port for offline verification).
- **Dashboard decisions card**: The web dashboard aggregates decision points by source count (`ollama`, `none`, `default`, `fallback`) and tracks recent flagged guard events (`deny`, `ask`, `escalate`) with reasons and target paths in the Decision models card.

### Safety guard

The safety guard (`yoke/guard.py`) enforces security boundaries across four targets:
1. **Pre-commit diff scan**: Staged worktree changes are inspected before commit in `pipeline._close` via `guard.scan_diff` and `guard.diff_inputs`. Deterministic denylist checks block modifications to `sensitive_files` or added secrets matching `secret_patterns` without echoing secret text in logs. Grey zone diffs (deletions exceeding `max_deleted_lines`, CI or hook files in `grey_files`, or network calls in `network_patterns`) are scored with model flags via Ollama. Clean diffs allow immediately without a model call.
2. **Claude agent PreToolUse via `--settings`**: When `guard.enabled` is active, `scheduler.run` writes an atomic `agent-settings.json` file configuring the PreToolUse hook (`yoke guard tool --run <run_id> --mode agent`). Claude agent invocations receive `--settings <path>` to enforce tool safety policies during execution.
3. **Chat PreToolUse hook**: `hooks/hooks.json` registers a `PreToolUse` hook running `command -v yoke >/dev/null 2>&1 && yoke guard tool --active --mode chat || true` with a 10-second timeout, protecting host agent sessions during active runs without blocking tool execution. Setting `guard.chat_hook: false` in user config disables the chat hook check.
4. **QA test_command check**: Prior to re-executing QA's `test_command`, the runner verifies the command with `guard.check_command`. Denied commands return retryable feedback to the coder (`Safety guard refused test command `<cmd>`: <reason>`), while non-allow decisions escalate.

**Tiering policy**:
- **Denylist / confident flag**: Evaluates immediately to `deny` (cleanly refuses action and returns feedback or exits).
- **Grey zone**: Returns `ask` (confirmation prompt) in chat sessions, `escalate` in runner diff and QA command checks, and `no opinion` in runner agent hooks (where tool allowlists still apply).

**Guard CLI (`yoke guard`)**:
- `yoke guard tool [--run R] [--active] [--mode chat|agent]`: Consumes PreToolUse JSON payload (`tool_name`, `tool_input`, `cwd`) on stdin and evaluates the tool call against safety guard policies. If the call is denied or flagged for confirmation, it prints hook-specific JSON output (`deny` or `ask`). On allow or when unhandled, it outputs nothing. It catches all internal errors and always exits with code 0. In `chat` mode (default), the check is silent unless a run is active for the repository and `guard.chat_hook` is enabled (default: `true`).
- `yoke guard check-diff [--run R]`: Runs safety analysis on repository diffs via `guard.scan_diff` and outputs JSON results, loading configuration from the specified run or local repository settings.

### Branch-point decisions

The runner leverages Ollama decision models at key pipeline branch points:
- **Test failure triage (`test_failure`)**: When the runner test command re-run fails (non-zero exit code), Ollama classifies the failure category under decision point `test_failure` (`product`, `testware`, or `environment`):
  - `environment`: sets `env_rerun = True` and re-runs the test command once. If the rerun fails a second time, it escalates to the user (`Escalate("environment failure running `<cmd>`: <tail[-500:]>")`). If the rerun passes, execution proceeds normally.
  - `testware`: marks the ticket to restart at QA (`start_stage = "qa"`) on the next attempt with feedback prefix `Decision: testware failure, QA reruns.\n`, skipping code and review.
  - `product` or below threshold (`source == "none"`): keeps standard feedback and restarts at the beginning (`code` stage).
- **Validator reject routing (`validator_reject`)**: When the validator rejects a ticket, Ollama evaluates the failure under decision point `validator_reject` (`coder`, `qa`, `docs`, or `escalate`):
  - `escalate`: raises an escalation (`Escalate("validator reject, decision model escalated: <summary>")`).
  - `qa` or `docs`: if the routed stage appears in ticket stages before `validate`, sets `start_stage` to restart at that stage on the next attempt and prefixes retry feedback with `Decision: validator reject routed to <stage>.\n`. If `docs` is routed but positioned after `validate`, feedback is prefixed with `Decision: validator reject routed to docs.\n` and execution falls back to the beginning (`code` stage).
  - `coder` or below threshold (`source == "none"`): keeps standard feedback and restarts at the beginning (`code` stage).
- **After-failure decision (`after_failure`)**: Following any failed attempt (`attempt >= 1`), Ollama evaluates next actions under decision point `after_failure` (`retry`, `split`, `replan`, or `escalate`) before checking the attempt cap:
  - `split`, `replan`, or `escalate`: escalates immediately with the model recommendation (`Escalate("decision model recommends <choice> after attempt <attempt>: <feedback>")`). The runner does not rewrite tickets automatically on split or replan.
  - `retry` or below threshold (`source == "none"`): proceeds with standard retry up to `max_attempts_per_ticket`.
- **Milestone gate failure decision (`gate_failure`)**: When a milestone `exit_check` fails, Ollama evaluates the failure under decision point `gate_failure` (`fix_ticket` or `escalate`):
  - `escalate`: marks the milestone as failed, records an escalation (`pipeline.escalate`), and returns `needs_user`.
  - `fix_ticket` or below threshold (`source == "none"`): falls through to the standard fix-ticket flow (up to `max_fix_tickets_per_milestone`).
- **Start-stage execution**: Tickets can resume execution at later stages (`start_stage`) using persisted `last_results`, `last_test_cmd`, and `last_rerun_rc` in `state.json`. When all prior stages have saved results, preceding stages are skipped, emitting a `stage_skip` audit event (`ticket`, `attempt`, `start_stage`). Pre-seeded QA command and exit code are preserved across skipped stages so close gates evaluate the passing QA re-run.

### Skill catalog and discovery

Yoke discovers available `SKILL.md` files and provides them to agents based on the `skills` configuration:
- **Skill discovery roots**: By default, yoke searches `~/.claude/skills`, `<repo>/.claude/skills`, and cached plugin directories under `~/.claude/plugins/cache/*/*/*/skills` (selecting the newest version per plugin by directory modification time and prefixing skill names as `<plugin>:<name>`). Custom roots can be specified in `skills.roots` to override default locations.
- **Allowlist filtering (`skills.allowlist`)**: `skills.allowlist` defines glob patterns (using `fnmatch`) matched against skill names. Skills are only exposed if they match at least one pattern. An empty allowlist (`[]`, the default) returns no skills.
- **Agent rendering (`skills.agy_mode`)**: Discovered skills are formatted for agent prompts:
  - `claude`: lists relevant skill names and descriptions (`Relevant skills (invoke with the Skill tool):\n- name: description`).
  - `agy`: when `skills.agy_mode` is `"inline"` (the default), inlines skill markdown bodies stripped of YAML frontmatter (`### name\n<body>`), truncated to `skills.inline_chars` (default 4000) total length. When `skills.agy_mode` is set to any other value, falls back to the Claude-style name list.
  - When no skills match or are picked, renders `(none)`.
- **Dynamic skill picking (`skill`)**: For `code`, `qa`, and `docs` stages, yoke evaluates discovered skills using Ollama via `router.pick_skills`. Up to 64 candidate skills are queried in a single batched call with `noul` relevance questions (`s0..sN`). Candidates with scores meeting or exceeding `router.thresholds.skill` (default: 0.6) are sorted descending by score and capped at `skills.max` (default: 3). If Ollama fails, skill picking fails closed and returns an empty list. Read-only stages (`review`, `validate`) skip skill picking entirely.
- **Prompt injection and tool permissions**: In `code`, `qa`, and `docs` stages, picked skills are rendered into the `$skills` prompt variable under `## Skills`. If no skills are picked or available, `(none)` is injected. When picked skills are non-empty and the active agent is `claude`, the `Skill` tool is dynamically appended to `write_allowed_tools`.

### Browser E2E verification stage

Yoke supports an optional automated browser end-to-end verification stage (`e2e`) driven by Playwright and guided by Ollama with agent fallback:
- **Installation**: Install the optional e2e extra via `uv tool install --editable '.[e2e]'` (or `pip install '.[e2e]'`) and run `playwright install chromium`. If Playwright is not installed, the stage is skipped cleanly with status `pass`, summary `skipped: playwright not installed`, and an `e2e_skipped` audit event (`ticket`, `reason`).
- **Opt-in pipeline stage**: Enabled by adding `e2e` to `routing.<ticket_type>.stages` (for example, `["code", "review", "qa", "e2e", "validate", "docs"]`).
- **Configuration keys**:
  - `e2e.url`: Target web application URL (required).
  - `e2e.start_command`: Command to start local development server (optional).
  - `e2e.ready_timeout_s`: Server readiness polling timeout in seconds (default: 60).
  - `e2e.max_steps`: Maximum interaction steps per criterion (default: 12).
  - `e2e.candidates`: Maximum DOM candidate elements collected per step (default: 20).
- **Localhost safety**: Target URLs must resolve to loopback (`localhost`, `127.0.0.1`, `::1`, or `*.localhost`). Non-local URLs are rejected immediately with status `blocked`.
- **Server lifecycle**: If `e2e.start_command` is specified, yoke launches the server in a separate process group and polls `e2e.url` until responsive (`HTTP < 500`) within `ready_timeout_s`. The driver and server process group are always cleanly terminated in a `finally` block.
- **Criteria evaluation loop**: For each criterion in `ticket.exit_criteria`, yoke attempts verification across up to `e2e.max_steps` (default 12):
  - **Verdict (`e2e_verdict`)**: Evaluates page text against criterion using Ollama (`noul` question). If score meets or exceeds `router.thresholds.e2e_verdict` (default 0.8), the criterion is marked met.
  - **Picker (`e2e_pick`)**: If verdict is not reached, collects up to `candidates` interactive elements (filtering out text inputs: `input`, `textarea`, `select`, `textbox`, `combobox`) and queries Ollama to select next action or `"none"`. On confident pick (`thresholds.e2e_pick`, default 0.6), clicks element and logs an `e2e_step` event.
  - **Agent fallback**: If criteria remain unmet after step exhaustion or low-confidence picks, yoke captures a screenshot and invokes an agent in read mode (`e2e` role, default primary `claude`) using `e2e.json` schema and prompt `e2e.md` with candidate elements, page text, and screenshot path.
- **Pipeline integration & close gate**: On failure, `e2e` returns retry feedback (`e2e failed:\n- <criterion>: <evidence>`); blocked runs raise an escalation. When `e2e` is in stages, `close_gate` verifies `results["e2e"]["status"] == "pass"`.


## Typical flow

- **/yoke:go**: run the complete workflow (grill, plan, tickets, run, finish) in one session with a single human gate at plan approval (auto-approves tickets and parallel runner).
- Or run individual skills step-by-step: `/yoke:grill` -> `/yoke:plan` -> `/yoke:tickets` -> `/yoke:run` -> `/yoke:finish`.
- Use `/yoke:resume [run_id]` to inspect active runs (`yoke runs --active`) or pick up an interrupted run with run id, and `/yoke:status [run_id]` to check progress.

### Skills

yoke provides the following skills for host agents (Claude Code and Antigravity):

| Skill | Description |
|---|---|
| `/yoke:go` | Complete workflow from requirements through finish in a single session with a single human gate at plan approval. |
| `/yoke:setup` | Check prerequisites and configure agent roles. |
| `/yoke:grill` | Requirements gathering interview writing `requirements.md`. |
| `/yoke:plan [run_id]` | Implementation plan drafting, bounded secondary review rounds, arbitration, and plan approval. |
| `/yoke:tickets [run_id]` | Chart approved plan into milestones/tickets via wayfinder, inspect map, and ticket approval. |
| `/yoke:run [run_id]` | Start the detached background runner and orchestrator wait loop. |
| `/yoke:resume [run_id]` | Inspect active runs (`yoke runs --active`) or pick up an interrupted run with run id. |
| `/yoke:models` | Inspect, set or reset the plan-tier and cheap-tier models per agent. |
| `/yoke:status [run_id]` | Check run progress, escalations, and active run states. |
| `/yoke:dashboard [--port N]` | Serve local read-only web dashboard showing yoke runs, ticket status, and progress. |
| `/yoke:finish [run_id]` | Final review gate: review commits, diffs, open issues, final approval, and open pull request. |

The skills run these for you; the raw commands below are for scripting and debugging:

```bash
yoke init --requirements req.md   # prints run_id; write plan.md into ~/.yoke/runs/<run_id>/
yoke review-plan                  # one read-only secondary review round (prints debate summary when debate ran)
yoke approve plan                 # also: tickets (creates GitHub milestones/issues if tracker=github), final
yoke tickets chart [--feedback "note"] # map approved plan to milestones/tickets via wayfinder
                                  # ticket ids continue after the highest GitHub issue/PR number (T-0057 after #56)
yoke tickets import tickets.json  # validate milestones/tickets and warn; `yoke tickets show` groups by milestone
yoke map                          # render destination, out-of-scope, milestone gates, frontier, decisions log
yoke route T-001                  # stages and agents for a ticket
yoke run --detach --parallel 2    # background runner; always resumes from disk
yoke wait                         # blocks until escalation, done, needs_user, stopped, died or timeout (persists through waiting_limit; reports usage totals and top 3 spenders)
yoke runs [--active] [--json]     # list runs for this repo with goal and next step
yoke status [--json|--brief]      # also: escalations, unblock [T-001|--milestone MS-01], stop; --active lists active runs; reports usage totals, top 3 spenders, and waiting_limit
yoke nudge T-001 --note "text"    # send note to pending or in-progress ticket for next attempt/review round
yoke guard tool --active --mode chat # inspect tool call from stdin; silent on allow, never exits non-zero
yoke guard check-diff [--run R]    # inspect staged diff against secret and safety patterns
yoke dashboard [--port N]         # serve local read-only web UI on 127.0.0.1 (default port 8765)
yoke approve final && yoke pr     # push branch, open PR
yoke clean                        # remove worktrees (keeps branches and audit)
```

Subcommands take `--run <id>`; default is the latest run for the current repo. `yoke mcp` is a stdio MCP server exposing the router.

## Implementation map & milestone gates

- **Wayfinder charting (`yoke tickets chart`)**: Maps the approved `plan.md` into destination, out-of-scope boundaries, milestones with optional integration `exit_check` commands, and tickets. Pass `--feedback "<notes>"` to refine the breakdown.
- **Design-carrying tickets**: the `plan` skill writes a sectioned plan (context, architecture, decisions, interfaces, per-ticket work items with steps, named tests and impacted existing tests). Wayfinder copies that into optional ticket fields `design`, `interfaces`, `steps`, `test_plan`, `impacted_tests`, `risks`, which coder, QA and validator see in the ticket JSON. Import warns when a feature, bugfix or refactor ticket has no `steps` or `test_plan`, or when an in-milestone `depends_on` chain has 4 or more tickets (or fully chains a milestone of 3 or more tickets).
- **Contents index**: `plan.md` must start with a `## Contents` list of `[title](#anchor)` links to every `##` section, so agents read the index and jump to the sections they need. `yoke approve plan` refuses a plan whose index is missing, has a dead link, or leaves out a section; `yoke review-plan` warns. Tickets name the sections they rely on in `plan_sections`. Docs and research agents write the same index into long Markdown they produce.
- **Implementation map (`yoke map`)**: Renders destination, out-of-scope items, per-milestone gate status, tickets with waiting/blocking reasons, execution frontier, and decisions log (`map.md`).
- **Start early, merge after gate**: A ticket of a later milestone starts as soon as its dependencies are closed and a parallel slot is free (the earliest milestone gets slots first). It cannot commit or merge until all preceding milestones pass or are waived; until then it waits as `awaiting_gate` with its worktree kept, then rebases onto the post-gate tip and merges.
- **Exit checks & fix tickets**: When all tickets in a milestone complete, the runner runs its `exit_check` in the integration worktree. If the check fails, Ollama evaluates the failure under decision point `gate_failure`. A confident `escalate` decision marks the milestone as failed and escalates immediately to the user; otherwise (`fix_ticket` or fallback), the runner auto-generates fix tickets (`F-001`, `F-002` up to `max_fix_tickets_per_milestone`, default 2). If fixes are exhausted, the gate escalates.
- **Unblocking milestones**: Answer gate escalations with `yoke unblock --milestone MS-xx [--note TEXT]` to retry the gate check or `yoke unblock --milestone MS-xx --waive REASON` to log the waiver to `map.md` and proceed.

## Web dashboard (`yoke dashboard`)

`yoke dashboard [--port N]` (or the `/yoke:dashboard` skill in agent sessions) serves a local, read-only web interface on `127.0.0.1` (default port `8765`):

- **Skill integration**: In agent sessions, run `/yoke:dashboard [--port N]` (agy: the `dashboard` skill) to launch or inspect the dashboard server.

- **Self-contained**: Single-file interface (`yoke/dashboard.html`) with inline CSS and JavaScript, using no CDNs or external network dependencies.
- **Run picker**: Sidebar listing all runs with live state badges, goal descriptions, progress counters, runner active indicators, and rate limit wait notices (`waiting for limit until <time>`). The selected run is tracked in the URL hash (`#<run_id>`).
- **Detail view**:
  - **Run overview**: Displays run ID, state, feature branch, runner heartbeat, agent calls against total budget, and an active `waiting for limit until <time>` banner during rate limit pauses.
  - **Stage strip**: Visual breadcrumbs across lifecycle stages (`grill`, `plan`, `tickets`, `run`, `finish`).
  - **Progress & Milestones**: Overall done vs remaining progress bar, plus milestone cards showing gate status, exit checks, and fix ticket counts.
  - **Usage & Cost**: Headline tokens (input + output, shown as `3.5M` / `143K`) with cache tokens separate, cost to 2 decimals, a **By Agent** table (`claude` vs `agy`), a **By Stage** table (code, qa, review, validate, docs, plan_review, wayfinder, router, pr_body) and the top 3 spending tickets. agy reports no cost, so it shows `unreported` and totals read `(+N unreported)`; yoke never estimates it.
  - **Decision models**: Table of decision points by source counts (ollama, none, default, fallback, or dynamic sources) and recent flagged guard events (deny, ask, escalate) with timestamps, target paths, and reasons.
  - **Interactive session**: your own Claude Code chat on the repo (grill, plan, supervising) has no runner call, so it is read from `~/.claude/projects/<repo>` and shown as `interactive`. It counts messages within the run's window (creation to last event), skips sessions yoke spawned, and prorates the session's recorded cost to that window, marked `~`. Attribution is by time, so unrelated chats on the same repo during a run are included; subagent transcripts are not read.
  - **Ticket table**: Real-time ticket statuses, attempt counts, active stage and agent, tokens, cost and claude/agy split per ticket, last update timestamps, and explicit `waiting_for` dependency reasons.
  - **Projects**: a collapsible panel under the run picker (`GET /api/projects`) rolls runs up by target repo: runs, tokens, cost and agent split.
  - **Escalations & Events**: Panel listing open escalations alongside a reverse-chronological event stream.
  - **Decisions log**: Rendered implementation decisions and gate waivers from `map.md`.
- **Live updates & safety**: Automatically polls `/api/runs` and `/api/runs/<id>` every 3 seconds with visible disconnected indicators and polling paused when the browser tab is hidden. All data is safely rendered using `textContent` (never `innerHTML`). Automatically adapts to dark and light modes via `prefers-color-scheme`.

## GitHub tracker & milestones

When `tracker` is `github` (or `auto` on GitHub remotes with `gh` available):
- **Milestones on approve**: `yoke approve tickets` creates corresponding GitHub milestones for all milestones in `tickets.json` (skipping existing ones and recording milestone numbers in `gh_milestones.json`), then attaches tickets to milestones using `--milestone <title>`.
- **Milestone gate automation**: When a milestone gate passes, `tracker.close_milestone` closes the GitHub milestone (`state=closed`). If gate exit checks fail, any generated fix tickets are created as GitHub issues in the same milestone via `tracker.create_fix_issue`.
- **Resilient execution**: All gate-time tracker actions are guarded and log `tracker_error` events on failure without raising exceptions or interrupting the runner loop.

## Pull requests (`yoke pr`)

`yoke pr` pushes the feature branch and creates the pull request on GitHub once final approval is granted (`yoke approve final`).

- **Title from plan**: Derived from the first `# ` heading in `plan.md` with any leading `Plan:` prefix stripped (falls back to `yoke: <run_id>`).
- **PR template lookup**: Searches for a PR template in the target repository in the following order:
  1. `.github/PULL_REQUEST_TEMPLATE/` directory (selects the file matching the highest priority ticket type: `feature` > `bugfix` > `refactor` > `test` > `docs` > `chore` > `spike` > `research`, or the first sorted `.md` file).
  2. `.github/pull_request_template.md`
  3. `.github/PULL_REQUEST_TEMPLATE.md`
  4. `pull_request_template.md` in the repository root.
  5. `docs/pull_request_template.md`
- **Agent fill with deterministic fallback**: An agent with role `pr_body` (read mode) fills the template sections from run facts. If the agent call fails, times out, or produces invalid output, yoke falls back to deterministic markdown filling (preserving template headings and mapping facts) and logs a `pr_fill_fallback` event.
- **Checklists ticked only when verified**: Checklist items (`- [ ]`) are only ticked if the item text matches verified facts (such as test suite verification passing close gates). Unverified items are left unchecked or unticked during post-validation.
- **Built-in structure when no template exists**: When no template is found, yoke generates a standard PR body with headings: `Summary`, `Why`, `What changed`, `Verification`, and `Review focus`.
- **Closes issues and unresolved findings**: Appends `Closes #N` lines for tracked issues, along with an `## Unresolved review findings` section listing up to 20 findings from plan review rounds not marked as accepted in arbitration.


## Research agents

```bash
yoke search "how does X work"            # ranked sources (claude, web tools)
yoke research "topic" --depth deep       # search -> research (agy) -> fact-check loop, writes docs/research/<topic>.md
yoke factcheck docs/some-file.md         # per-claim verdicts for any markdown file in the repo
```

`research` is also a ticket type (`search`, `research`, `factcheck` stages). Web tools come from `search.tools` in the config; depth presets from `research.depths`.

### Research screening

Search sources and research claims can be screened by the local decision model (`tev1:4b`) using deterministic heuristics and confidence threshold checks:
- **Configuration (`research.screen`)**: Configured under `research.screen` (`enabled: true`, `skip_factcheck: true`, `escalate_on_unsure: true`, `min_confidence: {"sources": 0.75, "claims": 0.8}`, `max_items: 25`).
- **Source screening (`source_screen`)**: Filters invalid schemes, duplicates, and empty excerpts via heuristics, then queries the decision model to keep or discard candidate sources. If all candidate sources are discarded, search fails with per-source feedback.
- **Claim screening (`claim_screen`)**: Evaluates whether cited kept sources support each claim against excerpt evidence. If any claim is unsupported, research fails with per-claim feedback.
- **Fact-check skip and narrowing**: In ticket execution (active when `research.screen.enabled` is true and `"research"` is in ticket stages):
  - When all claims are confidently supported without escalations or unsure sources, the fact-check stage is bypassed with synthetic results (`factcheck_skipped: True`, emitting a `screen` event).
  - When claims escalate or sources are unsure, the fact-check agent evaluates only the escalated claim IDs, unsure sources, and unverified statements; previously screened supported claims are merged back into results before verification checks.
  - Standalone `yoke factcheck FILE` remains unscreened.

## Tests

```bash
python3 -m unittest discover -s tests
tests/e2e.sh [normal|fallback]    # manual: real claude + agy + ollama on a disposable repo, spends quota
```

## Docs

- [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md): what the system must do.
- [docs/PLAN.md](docs/PLAN.md): design decisions, plus per-milestone notes on what was built.
- [CLAUDE.md](CLAUDE.md): guidance for Claude Code sessions in this repo.

## License

MIT. See [LICENSE](LICENSE).
