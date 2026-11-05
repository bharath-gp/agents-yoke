# Getting started

This guide walks through a yoke run from setup to pull request: what each step does, what you approve, and how to recover when something stops.

## Contents

- [Install options](#install-options)
- [The workflow](#the-workflow)
- [Approvals](#approvals)
- [While the runner works](#while-the-runner-works)
- [Answer an escalation](#answer-an-escalation)
- [Resume a run](#resume-a-run)
- [Where run state lives](#where-run-state-lives)
- [Next steps](#next-steps)

## Install options

The npx installer installs the plugin for Claude Code and Antigravity and the `yoke` CLI:

```sh
npx github:bharath-gp/agents-yoke [--scope user|project|local] [--only claude,agy] [--no-cli] [--dry-run]
```

| Scope | Effect |
|---|---|
| `user` | All your projects. |
| `project` | This repo only, shared through git (`.claude/settings.json`). |
| `local` | This repo only, not shared. |

Antigravity has no scope option, so its plugin is always installed for your user.

`/yoke:setup` installs the CLI if it is missing (`uv tool install` from GitHub, with `pipx` as fallback). It then runs `yoke setup`, which checks for `claude`, `agy` and `git` (required) and `gh` and `ollama` (optional). It asks you to choose the primary and secondary agent and records success for your user (`~/.yoke/setup.json`) or for the current repo only. Every other skill runs `yoke setup --check` first and asks you to run `/yoke:setup` if it fails.

If setup reports `agy read_file not in permissions.allow`, let it add `read_file` to agy's allowed permissions. agy runs headless and denies file reads that are not allowed, which wastes a whole agent call each time.

To update yoke, run the installer or `/yoke:setup` again.

## The workflow

| Step | Skill | What happens | Output |
|---|---|---|---|
| 1 | `/yoke:grill` | The agent interviews you one question at a time and confirms the models to use. `yoke init` creates the run. | `requirements.md`, a run id |
| 2 | `/yoke:plan` | The agent reads the code and writes a sectioned plan. The secondary agent reviews it, for up to `max_plan_review_rounds` rounds (default 3). Disputed findings go through a rebuttal and a judge. You see every finding and approve. | `plan.md`, `plan-review/` |
| 3 | `/yoke:tickets` | The wayfinder charts the plan into a destination, milestones and tickets. You inspect `yoke map` and approve. | `tickets.json`, `map.md` |
| 4 | `/yoke:run` | You choose the number of parallel tickets. The detached runner takes each ticket through its stages. The chat loops on `yoke wait`. | Commits on `yoke/<run_id>` |
| 5 | `/yoke:finish` | The agent summarises commits, diff stat, open tickets and the decisions log. You give final approval and optionally open a pull request. | Pull request |

`/yoke:go` runs all five steps in one session. It approves tickets for you after plan approval and asks for the parallel ticket count in the same prompt. It stops only for plan approval, tickets still not ready after review, escalations, final approval and the pull request.

A ticket's stages depend on its type:

| Type | Stages |
|---|---|
| `feature`, `bugfix` | code, review, qa, validate, docs |
| `refactor` | code, review, qa, validate |
| `chore`, `spike` | code, review, validate |
| `test` | qa, validate |
| `docs` | docs, validate |
| `research` | search, research, factcheck |

You can change the stages per type with `routing` in [configuration](configuration.md).

## Approvals

yoke records three approvals, each from an explicit yes in chat:

| Command | Bound to | Side effects |
|---|---|---|
| `yoke approve plan` | sha256 of `plan.md` | Refused if the plan's `## Contents` list is missing, has a dead link or leaves out a section. |
| `yoke approve tickets` | sha256 of canonical `tickets.json` | On a GitHub repo, creates GitHub milestones and one issue per ticket. Refused if ticket readiness review is on and tickets are not ready (override with `--accept-unready REASON`). |
| `yoke approve final` | The finished run | Allows `yoke pr`. |

Editing `plan.md` or `tickets.json` after approval voids that approval. `yoke run` refuses to start without approved plan and tickets.

## While the runner works

The runner keeps going while your chat session is closed. Use these to check on it:

- `/yoke:status` or `yoke status --brief`: progress, open escalations, usage and cost.
- `yoke map`: milestones, gate status, what each waiting ticket waits for, and the decisions log.
- `/yoke:dashboard`: a local web view of runs, tickets and cost. See [features](features.md#web-dashboard).

To steer a pending or running ticket without restarting it, send a note. The coder sees it on the next attempt or review round:

```sh
yoke nudge T-001 --note "Use the existing retry helper in http/client.py"
```

To stop the runner, run `yoke stop`.

## Answer an escalation

The runner escalates when a ticket fails `max_attempts_per_ticket` times (default 3), when a review finding is escalated (`review_escalate`), when a milestone gate fails after its fix tickets, when a budget cap is reached, or when the safety guard needs a decision. `yoke wait` returns, and the agent shows you the escalation. Answer it in chat, or run one of these yourself:

| Situation | Command |
|---|---|
| Retry a ticket with guidance | `yoke unblock T-001 --note "TEXT"` |
| Skip a ticket | `yoke unblock T-001 --skip` |
| Give a ticket a fresh set of attempts | `yoke unblock T-001 --reset-attempts` |
| Retry a milestone gate | `yoke unblock --milestone MS-01 [--note "TEXT"]` |
| Accept a failing milestone gate | `yoke unblock --milestone MS-01 --waive "REASON"` |

`yoke escalations` lists open escalations, displaying ticket, kind tag (when present), and reason.

## Resume a run

Everything is on disk, so any session can pick up a run. In a new session, the plugin shows a short status note for active runs. Then run:

```text
/yoke:resume [run_id]
```

Without a run id, it lists active runs and asks which one to resume. It then continues at the right step: plan, tickets, the wait loop, or finish. If the runner died, it restarts it with `yoke run --resume --detach`.

## Where run state lives

Each run lives in `~/.yoke/runs/<run_id>/` (set `$YOKE_HOME` to move it), never in your repo. Key files:

| Path | Contents |
|---|---|
| `requirements.md`, `plan.md`, `tickets.json` | Inputs you approved. |
| `plan-review/` | Review rounds (including each round's saved `plan.md`), debates and your arbitration notes. |
| `map.md` | Implementation map and decisions log. |
| `run.json`, `events.jsonl` | Run state and the event stream. |
| `tickets/<id>/` | Per-ticket state and every attempt's prompts, commands and outputs. |
| `usage.json` | Tokens and cost by stage, ticket, agent and model. |

Each ticket works in its own git worktree on branch `yoke/<run_id>-<ticket>`. The runner commits and merges into `yoke/<run_id>`. `yoke clean` removes the worktrees and keeps the branches and audit trail.

## Next steps

- [Configuration](configuration.md): change agents, models, review, budgets and safety settings.
- [CLI reference](cli.md): every command.
- [Features](features.md): milestone gates, GitHub tracking, pull requests and research.
