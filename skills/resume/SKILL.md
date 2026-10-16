---
name: resume
description: Pick up an existing yoke run in a new or compacted session. Use when a session start note mentions an active yoke run.
---

# resume

Gate: run `yoke setup --check` first. If it fails or `yoke` is not found, stop and tell the user to run `/yoke:setup`.

1. Run `yoke status`.
2. If the state is not done and the runner is not alive (the status or SessionStart note says so), restart it: `yoke run --resume --detach`.
3. If there are open escalations, show them (`yoke escalations`) and ask the user how to proceed, as in the `run` skill.
4. Continue the `yoke wait` loop from the `run` skill. When the state is done, use the `finish` skill.
