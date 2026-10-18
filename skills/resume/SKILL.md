---
name: resume
description: Pick up an existing yoke run in a new or compacted session. Use when a session start note mentions an active yoke run.
---

# resume

Gate: run `yoke setup --check` first. If it fails or `yoke` is not found, stop and tell the user to run `/yoke:setup`.

Argument: optional run id.

1. If no run id was given: run `yoke runs --active`. Show the list (id, goal, state, tickets closed/total, next) and ask the user which run to resume. If none are active, report that no active runs were found and stop.
2. If a run id R was given (or selected): run `yoke runs --json`, find R, and branch on `next`:
   - `plan`: follow the `plan` skill with `--run R`.
   - `tickets`: follow the `tickets` skill with `--run R`.
   - `resume`: restart the runner with `yoke run --run R --resume --detach`, then enter the wait loop.
   - `wait`: enter the wait loop (`yoke wait --run R`).
   - `finish`: follow the `finish` skill with `--run R`.
   - `done`: report that the run is already finished.
3. Wait loop: run `yoke wait --run R`. If open escalations exist, show them (`yoke escalations --run R`), ask the user how to proceed, and unblock (`yoke unblock T-xxx --run R ...`) as in the `run` skill. If the runner died and the run is not finished, restart with `yoke run --run R --resume --detach`.
