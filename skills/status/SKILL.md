---
name: status
description: Show the state of the current yoke run in a few lines. Use when the user asks how the yoke run is going.
---

# status

Gate: run `yoke setup --check` first. If it fails or `yoke` is not found, stop and tell the user to run `/yoke:setup`.
If the user gives a run id, pass `--run <id>` to every `yoke` command.

Run `yoke status` (add `--json` for scripts) and `yoke escalations`. Report run state, closed/total tickets, agent calls used and any open escalation in a few lines. Read nothing else unless the user asks.

If the run is in the `waiting_limit` state, it is paused waiting for a provider rate limit or quota reset; report the `resume_at` time and target ticket from `waiting_limit`.

To guide a pending or in-progress ticket without stopping the runner, use `yoke nudge T-xxx --note TEXT` (optionally `--run <id>`). It queues the note in the run directory and appends it to feedback at the next attempt boundary or review round exactly once.
