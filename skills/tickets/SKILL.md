---
name: tickets
description: Chart an approved yoke plan into milestones and tickets, inspect the map, and get the user's approval. Use after the plan is approved.
---

# tickets

Gate: run `yoke setup --check` first. If it fails or `yoke` is not found, stop and tell the user to run `/yoke:setup`.
If the user gives a run id, pass `--run <id>` to every `yoke` command.

1. Chart tickets: run `yoke tickets chart` to have the wayfinder map the approved plan into destination, milestones, and tickets in `tickets.json`. Alternatively, write `tickets.json` per schema `yoke/schemas/ticket-list.json` and run `yoke tickets import <path-to-tickets.json>`.
   - Each ticket: `id` (T-001), `title`, `type` (omit if unsure; router classifies), `description`, `exit_criteria`, `depends_on`, `files_hint`, `plan_excerpt`, and optional `milestone`.
   - Exit criteria must be checkable by a command or specific observation, not "works well".
   - `plan_excerpt` is copied verbatim from `plan.md`.
   - Keep `depends_on` minimal so independent tickets can run in parallel.
2. Inspect the map: run `yoke map` to show the destination, out-of-scope boundaries, milestones, tickets, and frontier (or `yoke tickets show` for the tickets table).
3. If adjustments are needed, re-run with feedback: `yoke tickets chart --feedback "<notes>"`. Repeat until satisfied with the ticket breakdown and milestones.
4. State the side effects of approving: when the repo has a GitHub remote, `yoke approve tickets` creates GitHub milestones and one GitHub issue per ticket attached to its milestone. It never pushes code.
5. Only on the user's explicit yes, run `yoke approve tickets`. Editing the tickets afterwards voids the approval.
6. Next step: the `run` skill.
