# yoke

Multi-agent loop for large work items. Claude (primary) gathers requirements, plans, splits tickets and validates; Antigravity `agy` (secondary) codes, writes tests and docs. Agents are pluggable per role via `yoke/adapters.py`.

## How it works

1. Interactive: requirements, `plan.md`, one read-only secondary plan review, ticket split. You approve the plan and tickets (hash-bound).
2. Unattended: a detached runner takes each ticket through code, review, QA, validate, docs. Failures retry with feedback up to a limit, then escalate to you. Independent tickets run in parallel, one git worktree each (`--parallel N`). Upstream handoff notes (interface changes, new files, gotchas) from closed ancestor tickets are automatically stored at `tickets/<T>/handoff.json` and inlined into dependent `code`, `QA`, and `validate` prompts (`$upstream`).
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

Agent roles are configured via `yoke setup --primary <agent> --secondary <agent> --map <role>=<who>`. Supported roles include `requirements`, `planner`, `wayfinder`, `orchestrator`, `plan_review`, `code`, `review`, `qa`, `validate`, `docs`, `search`, `research`, and `factcheck`.

The `review` role defaults to `auto`, resolving dynamically to the opposite of the `code` agent (`claude` <-> `agy`), ensuring code review is performed by a different agent than the implementer. For custom code agent names outside `AGENTS`, it falls back to `primary` (or `secondary` if `primary` matches `code`). `yoke setup` warns if an explicit override assigns the same agent to both code and review. Use `--map review=auto` to restore automatic resolution.

During ticket execution, the `review` stage runs an iterative review loop (up to `max_review_rounds` revisions). Blocker and major findings trigger coder revisions using the resumed session. If findings remain unresolved when reaching the revision cap, the ticket proceeds without failing: unresolved findings are recorded in ticket state, posted as issue comments, and surfaced in the `yoke pr` description under "Unresolved review findings".

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

### Budget caps and limits

Run-level execution budgets can be configured in `~/.yoke/config.json` or `<repo>/.yoke.json`:

- `max_agent_calls_total`: Maximum total agent invocations across all tickets (default: 300).
- `budget.max_cost_usd`: Maximum spend cap in USD (default: `null`, disabled). Checks recorded Claude cost from `usage.summarize` (falling back to total cost).
- `budget.max_tokens`: Maximum total token usage cap across all agents (default: `null`, disabled).

When a budget cap is exceeded, the runner stops scheduling new work, allows in-flight calls to finish cleanly, and opens a single budget escalation. Resuming after a budget escalation does not re-escalate.

Agent rate limits and quotas are configured under `limits`:

- `limits.patterns`: Mapping of agent name (`claude`, `agy`) to regex patterns detecting rate limits, quota limits, or resource exhaustion in agent stdout and error messages.
- `limits.max_wait_s`: Maximum wait time in seconds when a limit is hit (default: 7200).
- `limits.max_waits`: Maximum consecutive wait cycles before opening an escalation (default: 3).

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
"gate_hooks": {
  "after_stage": {
    "code": [],
    "qa": []
  },
  "before_close": []
}
```

- **User config only**: For security, `gate_hooks` is accepted exclusively from user configuration (`~/.yoke/config.json`). Any `gate_hooks` specified in repo configuration (`<repo>/.yoke.json`) is rejected with a warning log to prevent arbitrary command execution from untrusted repositories.
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
yoke review-plan                  # one read-only secondary review round
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
yoke dashboard [--port N]         # serve local read-only web UI on 127.0.0.1 (default port 8765)
yoke approve final && yoke pr     # push branch, open PR
yoke clean                        # remove worktrees (keeps branches and audit)
```

Subcommands take `--run <id>`; default is the latest run for the current repo. `yoke mcp` is a stdio MCP server exposing the router.

## Implementation map & milestone gates

- **Wayfinder charting (`yoke tickets chart`)**: Maps the approved `plan.md` into destination, out-of-scope boundaries, milestones with optional integration `exit_check` commands, and tickets. Pass `--feedback "<notes>"` to refine the breakdown.
- **Implementation map (`yoke map`)**: Renders destination, out-of-scope items, per-milestone gate status, tickets with waiting/blocking reasons, execution frontier, and decisions log (`map.md`).
- **Sequential milestone barrier**: The runner executes tickets milestone-by-milestone. Tickets belonging to later milestones do not become ready until all preceding milestones pass or are waived.
- **Exit checks & fix tickets**: When all tickets in a milestone complete, the runner runs its `exit_check` in the integration worktree. If the check fails, the runner auto-generates fix tickets (`F-001`, `F-002` up to `max_fix_tickets_per_milestone`, default 2). If fixes are exhausted, the gate escalates.
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
  - **Usage & Cost**: Aggregated cost and token counts (total, input, output) with missing agent cost rendered as "unreported", plus a breakdown table of the top 3 spending tickets.
  - **Ticket table**: Real-time ticket statuses, attempt counts, active stage and agent, last update timestamps, and explicit `waiting_for` dependency reasons.
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
