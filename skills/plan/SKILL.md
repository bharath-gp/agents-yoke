---
name: plan
description: Write plan.md for a yoke run, run bounded secondary-agent review rounds, arbitrate findings, and get the user's approval of the plan. Use after grill.
---

# plan

Gate: run `yoke setup --check` first. If it fails or `yoke` is not found, stop and tell the user to run `/yoke:setup`.

The run dir is `$YOKE_HOME/runs/<run_id>/` (default `~/.yoke/runs/<run_id>/`); `yoke status` shows the run id. If the user gives a run id, pass `--run <id>` to every `yoke` command.

1. Write `plan.md` in the run dir from `requirements.md`. Include: approach, milestones or work breakdown, how each piece is verified, risks. Be concrete enough that tickets can quote it verbatim.
2. Review loop, at most the configured max rounds (default 2):
   - Run `yoke review-plan`. It asks the secondary agent for a read-only review and prints the round dir, verdict and finding count. Read `result.json` in that round dir.
   - For EACH finding decide accept or reject and give a reason. Append the decisions to `plan-review/arbitration.md` in the run dir.
   - Revise `plan.md` for accepted findings. Stop early when the verdict is approve or no finding is accepted.
3. Show the user the plan, plus every unresolved disagreement (rejected findings with your reason). Do not hide rejected findings.
4. Only on the user's explicit yes, run `yoke approve plan`. The approval is bound to the sha256 of `plan.md`; editing the plan afterwards voids it and you must ask again.
5. Next step: the `tickets` skill.
