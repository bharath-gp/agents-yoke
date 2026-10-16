---
name: status
description: Show the state of the current yoke run in a few lines. Use when the user asks how the yoke run is going.
---

# status

Gate: run `yoke setup --check` first. If it fails or `yoke` is not found, stop and tell the user to run `/yoke:setup`.

Run `yoke status` (add `--json` for scripts) and `yoke escalations`. Report run state, closed/total tickets, agent calls used and any open escalation in a few lines. Read nothing else unless the user asks.
