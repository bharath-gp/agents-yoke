---
name: grill
description: Interview the user to pin down requirements for a large work item, write requirements.md and start a yoke run. Use when starting new work with yoke.
---

# grill

Gate: run `yoke setup --check` first. If it fails or `yoke` is not found, stop and tell the user to run `/yoke:setup`.

Goal: a requirements file the plan can be built from, then a run.

1. Ask ONE question at a time. With each question, propose a default answer and say why; the user can just accept it.
2. Cover: goal and non-goals, users and success criteria, constraints (stack, compatibility, deadlines), existing code to reuse, how to verify the result, risks and unknowns. Skip anything already answered in the repo or earlier replies.
3. Stop when every open point has an answer or an explicit "unknown, decide in plan". Do not ask for approval of design yet; that is the plan stage.
4. Write the answers to a file outside the target repo (for example `/tmp/requirements.md`): sections for Goal, Non-goals, Constraints, Success criteria, Open items.
5. Run `yoke init --requirements /tmp/requirements.md` from the target repo. It prints the run id. Tell the user the run id and that the next step is the `plan` skill.
