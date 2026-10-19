---
name: run
description: Start the yoke runner on approved tickets and supervise it with a wait loop, relaying escalations to the user. Use after tickets are approved.
---

# run

Gate: run `yoke setup --check` first. If it fails or `yoke` is not found, stop and tell the user to run `/yoke:setup`.

You are a thin orchestrator. Do not read diffs or logs unless the user asks. If the user gives a run id, pass `--run <id>` to every `yoke` command.

1. Ask the user how many tickets to run in parallel (default 1).
2. Start the runner: `yoke run --detach --parallel N`. It refuses without approved plan and tickets.
3. Loop `yoke wait`. It blocks until an escalation opens, the run finishes, the runner dies, or it times out (it then prints "still running: k/n closed"; call it again).
4. On an escalation: show it (`yoke escalations`), ask the user what to do, then act with one of `yoke unblock T-xxx --note TEXT`, `--skip`, `--reset-attempts`, or for milestone gate failures `yoke unblock --milestone MS-xx [--note TEXT]` / `--waive REASON`, and go back to step 3.
5. If the runner died and the run is not finished, use the `resume` skill.
6. When the run is done, use the `finish` skill.
7. Never push, merge or pass permission-bypass flags to any agent.
