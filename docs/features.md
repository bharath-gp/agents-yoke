# Features

This page covers the parts of yoke you meet after the basic workflow: milestone gates, the web dashboard, GitHub tracking, pull requests and research agents.

## Contents

- [Milestones and the implementation map](#milestones-and-the-implementation-map)
- [Web dashboard](#web-dashboard)
- [GitHub tracking](#github-tracking)
- [Pull requests](#pull-requests)
- [Research agents](#research-agents)

## Milestones and the implementation map

`yoke tickets chart` asks the wayfinder to map the approved plan into:

- a destination and the out-of-scope boundaries,
- milestones, each with an optional integration `exit_check` command, and
- tickets, carrying the plan's design detail (`design`, `interfaces`, `steps`, `test_plan`, `files`, ...) so the coder, QA and validator see it.

Pass `--feedback "NOTE"` to re-chart. Charting and import warn about tickets that are missing `steps`, `test_plan` or `files`, about long dependency chains that block parallel work, and about paths that do not exist in the repo.

`yoke map` shows the destination, out-of-scope items, each milestone's gate status, what every waiting ticket is waiting for, the frontier of ready tickets and the decisions log. The same content is written to `map.md` in the run directory.

**Milestone gates.** Milestones merge in order. A ticket from a later milestone can start as soon as its dependencies close and a slot is free, but it cannot merge until every earlier milestone has passed or been waived. Until then it waits as `awaiting_gate`.

When all of a milestone's tickets close, the runner runs its `exit_check` on the integrated branch. If the check fails, the runner adds fix tickets (`F-001`, `F-002`, up to `max_fix_tickets_per_milestone`, default 2) or escalates. Answer a gate escalation with:

```sh
yoke unblock --milestone MS-01 --note "TEXT"     # retry the gate
yoke unblock --milestone MS-01 --waive "REASON"  # accept it; the waiver is logged in map.md
```

## Web dashboard

```sh
yoke dashboard [--port N]
```

Or run `/yoke:dashboard`. It serves a read-only page on `127.0.0.1` (default port 8765) and refreshes every 3 seconds. It works offline.

It shows:

- every run, with its state, goal, progress and any rate-limit wait,
- the stage strip (`grill`, `plan`, `tickets`, `run`, `finish`) and milestone gate status,
- a ticket table with status, attempts, current stage and agent, tokens, cost and what each ticket waits for,
- usage and cost by agent and by stage, and the top 3 spending tickets,
- a per-repo rollup of runs, tokens and cost,
- decision-model sources and recent safety-guard events,
- open escalations, the event stream and the decisions log.

agy reports no cost, so its cost shows as `unreported`. yoke never estimates it.

The dashboard also counts your own Claude Code chat on the repo during the run as an `interactive` row. It attributes chat by time, so other chats on the same repo during the run are included; its cost is marked `~`.

## GitHub tracking

When `tracker` is `github`, or `auto` on a github.com remote with `gh` working:

- `yoke approve tickets` creates a GitHub milestone for each milestone and an issue for each ticket.
- A passing milestone gate closes its GitHub milestone.
- Fix tickets from a failed gate become issues in the same milestone.

Tracker errors are logged and never stop the runner.

## Pull requests

After `yoke approve final`, `yoke pr` pushes the feature branch and opens a pull request. It is the only command that pushes.

- **Title**: the first `# ` heading of `plan.md`.
- **Body**: the repo's pull request template, filled from the run by the `pr_body` agent. yoke looks in `.github/PULL_REQUEST_TEMPLATE/`, `.github/pull_request_template.md`, `.github/PULL_REQUEST_TEMPLATE.md`, `pull_request_template.md` and `docs/pull_request_template.md`. Without a template it writes Summary, Why, What changed, Verification and Review focus. If the agent fails, yoke fills the template itself.
- **Checklists**: an item is ticked only when the run verified it.
- **Links**: `Closes #N` for each tracked issue, and an "Unresolved review findings" section for plan and code review findings that were not resolved.

## Research agents

```sh
yoke search "how does X work"          # ranked sources
yoke research "topic" --depth deep     # writes docs/research/<topic>.md
yoke factcheck docs/some-file.md       # a verdict per claim
```

`research` runs search, research and fact-check, then writes a cited Markdown document. Depths (`quick`, `normal`, `deep`) set the number of sources and search rounds under `research.depths`. Web tools come from `search.tools`.

`research` is also a ticket type, so a plan can include research tickets.

Inside a research ticket, the local decision model screens sources and claims first (`research.screen`, on by default). It drops irrelevant sources and checks each claim against its cited source. When every claim is confidently supported, the fact-check stage is skipped. Otherwise, the fact-checker checks only the uncertain claims. `yoke factcheck` on its own is not screened.

| Key | Default | Effect |
|---|---|---|
| `research.screen.enabled` | `true` | Screen sources and claims. |
| `research.screen.skip_factcheck` | `true` | Skip fact-check when every claim is supported. |
| `research.screen.escalate_on_unsure` | `true` | Send uncertain items to fact-check. |
| `research.screen.min_confidence.sources` / `.claims` | `0.75` / `0.8` | Confidence needed to keep a source or accept a claim. |
| `research.screen.max_items` | `25` | Most items screened per call. |
