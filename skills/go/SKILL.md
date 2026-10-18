---
name: go
description: Run the full yoke workflow from interview to PR in one session.
---

# go

Gate: run `yoke setup --check` first. If it fails or `yoke` is not found, stop and tell the user to run `/yoke:setup`.

Self-contained workflow chaining grill, plan, tickets, run, and finish in one chat session.

Argument: optional work description, used to seed the interview.

0. State that run artifacts (`requirements.md`, `plan.md`, `plan-review/arbitration.md`, `tickets.json`) live in the run dir `$YOKE_HOME/runs/<run_id>/` (default `~/.yoke/runs/<run_id>/`), never in the repo.
1. Interview: ask ONE question at a time, proposing a default answer with each question. Stop when all open points are resolved. Write requirements to a file outside the target repo (e.g. `/tmp/requirements.md`). Run `yoke init --requirements /tmp/requirements.md` from the repo, capture the run id R, and state R to the user. Always create a new run; never look for an existing one. From here every yoke command carries `--run R`.
2. Plan: write `plan.md` in `$YOKE_HOME/runs/<run_id>/` based on `requirements.md`. Run up to the configured review rounds of `yoke review-plan --run R`. For each finding in `result.json`, record accept or reject with a reason in `plan-review/arbitration.md`, and revise `plan.md` for accepted findings.
3. Plan gate (one prompt): show the plan and every rejected finding with the reason. Ask one question: approve the plan, and what max parallel tickets N (default 3; only an upper bound)? State in that same prompt that approving also approves the tickets automatically (auto-approved tickets) and, with a GitHub remote, creates one GitHub issue per ticket (GitHub issues). On an explicit yes, run `yoke approve plan --run R`.
4. Tickets without a gate: write `tickets.json` in the run dir per ticket rules (checkable exit criteria, verbatim `plan_excerpt`, minimal `depends_on`).
   - Run `yoke tickets import --run R <file>`. Fix every reported error and re-run until clean.
   - Run `yoke tickets show --run R` and print the table for information.
   - Run `yoke approve tickets --run R` without asking.
5. Runner execution:
   - Start runner: `yoke run --run R --detach --parallel N`.
   - Wait loop: loop `yoke wait --run R`. When wait reports done, go to step 6.
   - Escalations: on an escalation, inspect with `yoke escalations --run R`, ask the user, and act with `yoke unblock T-xxx --run R ...`. Then continue the wait loop.
   - Runner death: if the runner died and the run is not finished, restart with `yoke run --run R --resume --detach`.
6. Final:
   - Confirm `yoke status --run R` says done.
   - Summarize commits, diff stat, and open or skipped tickets. Ask whether to approve the result.
   - Only on an explicit yes, run `yoke approve final --run R`.
   - Only for a GitHub repo and on an explicit yes, run `yoke pr --run R`.
7. Rules: stops are only plan approval, escalations, final approval, and PR confirmation. Never push except via `yoke pr --run R`. Never pass permission-bypass flags to any agent.
