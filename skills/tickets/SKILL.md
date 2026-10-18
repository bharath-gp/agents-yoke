---
name: tickets
description: Split an approved yoke plan into tickets, import and show them, and get the user's approval. Use after the plan is approved.
---

# tickets

Gate: run `yoke setup --check` first. If it fails or `yoke` is not found, stop and tell the user to run `/yoke:setup`.
If the user gives a run id, pass `--run <id>` to every `yoke` command.

1. Write `tickets.json` (a list of ticket objects) in the run dir, matching the ticket-list schema in `yoke/schemas/ticket-list.json`. Each ticket: `id` (T-001), `title`, `type` (omit if unsure; the router classifies), `description`, `exit_criteria`, `depends_on`, `files_hint`, `plan_excerpt`.
   - Exit criteria must be checkable by a command or a specific observation, not "works well".
   - `plan_excerpt` is copied verbatim from `plan.md`.
   - Keep `depends_on` minimal so independent tickets can run in parallel (the runner runs `parallel` tickets at once). Add a dependency only when a ticket needs code another ticket creates, not merely because it comes later in the plan or touches the same area. Do not chain tickets T-001 -> T-002 -> T-003 by default; before writing `depends_on`, ask of each edge whether the ticket fails to build or test without that other ticket's files. Put shared groundwork (types, config keys) in one early ticket and let the rest depend only on it. A final docs ticket may depend on all the others.
2. Run `yoke tickets import <path-to-tickets.json>`. Fix every reported error and re-run; nothing is imported on error.
3. Run `yoke tickets show` and present the table to the user.
4. State the side effects of approving: when the repo has a GitHub remote, `yoke approve tickets` creates one GitHub issue per ticket. It never pushes code.
5. Only on the user's explicit yes, run `yoke approve tickets`. Editing the tickets afterwards voids the approval.
6. Next step: the `run` skill.
