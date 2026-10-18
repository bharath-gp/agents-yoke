---
name: go
description: Thin end-to-end wrapper running grill, plan, tickets, and run in sequence.
---

# go

Gate: run `yoke setup --check` first. If it fails or `yoke` is not found, stop and tell the user to run `/yoke:setup`.

1. Run the `grill` skill interview (steps 1-3) with the user to establish requirements.
1b. Model selection: run `yoke models --list` and `yoke models --show` to inspect current and available models. Ask ONE question to confirm or pick plan-tier and cheap-tier models for primary and secondary agents (defaults = current); accept defaults to skip. If the user requests changes, apply with `yoke models --set AGENT.TIER=MODEL`.
2. Write requirements to a file outside the target repo (for example `/tmp/requirements.md`) and run `yoke init --requirements /tmp/requirements.md` from the target repo. It prints the run id. Report the run id to the user.
3. Hand off and proceed through the `plan`, `tickets`, and `run` skills.
