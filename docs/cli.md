# CLI reference

The skills run these commands for you. Use them directly for scripting and debugging. Commands that act on a run take `--run <id>`; the default is the latest run for the current repo. Run `yoke <command> --help` for every flag.

## Contents

- [Setup](#setup)
- [Plan and tickets](#plan-and-tickets)
- [Run](#run)
- [Inspect](#inspect)
- [Finish](#finish)
- [Research](#research)
- [Safety and integration](#safety-and-integration)

## Setup

| Command | What it does |
|---|---|
| `yoke setup [--scope user\|project] [--live]` | Check the environment and record setup. `--live` also makes one tiny call per agent. |
| `yoke setup --primary AGENT --secondary AGENT [--map ROLE=WHO]...` | Choose agents per role. See [agent roles](configuration.md#agent-roles). |
| `yoke setup --grant-agy-read` | Add `read_file` to agy's allowed permissions. |
| `yoke setup --check` | Report whether setup was completed. |
| `yoke doctor [--live]` | Check required tools without recording setup. |
| `yoke models --list \| --show [--json]` | List available models, or show the effective model per agent and tier. |
| `yoke models --set AGENT.TIER=MODEL [--effort E] [--scope user\|project\|repo]` | Set a model. Repeatable. |
| `yoke models --reset [AGENT[.TIER]] [--scope ...]` | Remove model settings. |

## Plan and tickets

| Command | What it does |
|---|---|
| `yoke init --requirements FILE [--repo PATH]` | Create a run and print its id. Write `plan.md` into the run directory next. |
| `yoke review-plan` | Run one read-only review round on `plan.md`. Prints the verdict, debate summary and warnings for missing paths or a broken `## Contents` list. |
| `yoke approve plan` | Record plan approval. |
| `yoke tickets chart [--feedback "NOTE"]` | Chart the approved plan into milestones and tickets. |
| `yoke tickets import FILE` | Validate and import a hand-written `tickets.json`. |
| `yoke tickets show` | Show tickets grouped by milestone. |
| `yoke review-tickets` | Run one read-only readiness review per ticket, when `ticket_review` is on. |
| `yoke approve tickets [--accept-unready REASON]` | Record ticket approval. Creates GitHub milestones and issues when the tracker is on. |
| `yoke route T-001` | Show the stages and agents for a ticket. |

Ticket ids continue after the highest GitHub issue or pull request number, so `T-0057` follows `#56`.

## Run

| Command | What it does |
|---|---|
| `yoke run --detach [--parallel N]` | Start the background runner. It always resumes from disk. `--parallel` is saved for the run. |
| `yoke run --resume --detach` | Restart a runner that died. |
| `yoke wait [--timeout S]` | Block until an escalation, run end, runner death or timeout (default 3000 s). Rate-limit pauses do not end the wait. |
| `yoke escalations` | List open escalations. |
| `yoke unblock T-001 --note TEXT \| --skip \| --reset-attempts` | Answer a ticket escalation. |
| `yoke unblock --milestone MS-01 [--note TEXT \| --waive REASON]` | Retry or waive a milestone gate. |
| `yoke nudge T-001 --note TEXT` | Add guidance to a ticket's next attempt or review round. |
| `yoke stop` | Stop the runner and its workers. |

## Inspect

| Command | What it does |
|---|---|
| `yoke status [--json \| --brief] [--active]` | Run summary: tickets, escalations, usage, top spenders and any rate-limit wait. `--brief` prints at most 15 lines. |
| `yoke runs [--active] [--json]` | List runs for this repo with goal and next step. |
| `yoke map` | Destination, out-of-scope items, milestone gates, frontier and decisions log. |
| `yoke dashboard [--port N]` | Serve the web dashboard on 127.0.0.1 (default port 8765). |

## Finish

| Command | What it does |
|---|---|
| `yoke approve final` | Record final approval. |
| `yoke pr` | Push the feature branch and open a pull request. Needs final approval. The only command that pushes. |
| `yoke clean` | Remove the run's worktrees. Keeps branches and the audit trail. |

## Research

| Command | What it does |
|---|---|
| `yoke search "QUERY" [--depth D]` | Return ranked sources for a question. |
| `yoke research "TOPIC" [--depth quick\|normal\|deep]` | Search, research and fact-check, then write `docs/research/<topic>.md`. |
| `yoke factcheck FILE` | Give a verdict for each claim in a Markdown file in the repo. |

See [research agents](features.md#research-agents).

## Safety and integration

| Command | What it does |
|---|---|
| `yoke guard tool [--active] [--mode chat\|agent]` | Check a tool call read from stdin (PreToolUse JSON). Prints nothing on allow and always exits 0. Used by the plugin's hook. |
| `yoke guard check-diff` | Check the staged diff for secrets and risky changes. |
| `yoke mcp` | Serve the router as a stdio MCP server. |

See [safety guard](configuration.md#safety-guard).
