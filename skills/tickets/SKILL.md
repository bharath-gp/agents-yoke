---
name: tickets
description: Chart an approved yoke plan into milestones and tickets, inspect the map, and get the user's approval. Use after the plan is approved.
---

# tickets

Gate: run `yoke setup --check` first. If it fails or `yoke` is not found, stop and tell the user to run `/yoke:setup`.
If the user gives a run id, pass `--run <id>` to every `yoke` command.

1. Chart tickets: run `yoke tickets chart` to have the wayfinder map the approved plan into destination, milestones, and tickets in `tickets.json`. Alternatively, write `tickets.json` per schema `yoke/schemas/ticket-list.json` and run `yoke tickets import <path-to-tickets.json>`.
   - Each ticket: `id` (T-001), `title`, `type` (omit if unsure; router classifies), `description`, `exit_criteria`, `depends_on`, `files_hint`, `plan_excerpt`, and optional `milestone`.
   - `plan_sections`: anchors from the plan's Contents index that the ticket relies on.
   - Design detail, optional in the schema but expected on feature, bugfix and refactor tickets: `design`, `interfaces`, `steps`, `test_plan`, `impacted_tests`, `risks`. Coder, QA and validator see the ticket JSON, so this is how plan detail reaches them. Import warns when `steps` or `test_plan` is missing; fill them before approving.
   - Exit criteria must be checkable by a command or specific observation, not "works well".
   - `plan_excerpt` is copied verbatim from `plan.md`.
   - Keep `depends_on` minimal so independent tickets can run in parallel. Import warns when an in-milestone dependency chain has 4 or more tickets, or when a milestone of 3 or more tickets is chained end to end (only true code dependencies belong in `depends_on`).
2. Inspect the map: run `yoke map` to show the destination, out-of-scope boundaries, milestones, tickets, and frontier (or `yoke tickets show` for the tickets table).
3. If adjustments are needed, re-run with feedback: `yoke tickets chart --feedback "<notes>"`. After showing warnings, if a dependency chain warning appears, offer one re-chart round: `yoke tickets chart --run <id> --feedback "<warning text>"`. Repeat until satisfied with the ticket breakdown and milestones.
4. State the side effects of approving: when the repo has a GitHub remote, `yoke approve tickets` creates GitHub milestones and one GitHub issue per ticket attached to its milestone. It never pushes code.
5. Only on the user's explicit yes, run `yoke approve tickets`. Editing the tickets afterwards voids the approval.
6. Next step: the `run` skill.
