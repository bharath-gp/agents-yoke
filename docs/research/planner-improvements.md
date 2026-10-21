# Planner improvements: from past runs to detailed plans

Date: 2026-10-21. Input: the seven runs under `~/.yoke/runs/` (2026-10-17 to 2026-10-20), their `plan.md`, `tickets.json`, `events.jsonl` and per-attempt `feedback.md`. Implemented as PLAN.md [M23](../PLAN.md#m23) (0.9.0).

## Contents

- [1. What the runs show](#1-what-the-runs-show)
- [2. What the plans lacked](#2-what-the-plans-lacked)
- [3. Recommendations and what was done](#3-recommendations-and-what-was-done)
- [4. Not done (candidates for later)](#4-not-done-candidates-for-later)
- [4a. Contents index in every document](#4a-contents-index-in-every-document)
- [5. How to measure](#5-how-to-measure)

## 1. What the runs show

| Run | Tickets | Tickets retried | Plan size |
|---|---|---|---|
| 20261017-230500 | 3 | 2 | 8.1K |
| 20261018-120554 | 3 | 2 | 4.9K |
| 20261018-185626 | 2 | 1 | 9.9K |
| 20261018-213412 | 3 | 2 | 3.6K |
| 20261019-101257 | 5 | 3 | 12.8K |
| 20261019-180618 | 4 | 3 | 6.6K |
| 20261020-092428 | 16 | 9 (2 escalated) | 6.0K |
| **Total** | **36** | **22 (61%)** | |

Classification of the 30 `feedback.md` files that triggered a retry:

| Cause | Count |
|---|---|
| Runner's full-suite re-run failed after QA reported pass | 22 |
| agy tool permission denied (`ViewFile`, `ReadFile`) | 3 |
| Validator or review feedback | 2 |
| User nudge | 2 |
| Timeout | 1 |

The dominant failure is not "the coder built the wrong thing". It is "the change broke tests the ticket never mentioned", or "the new tests leaked state (open files, `ResourceWarning`, stray run dirs) into the suite". Examples from the feedback:

- `test_review.TestReviewStage` and `test_core.TestRunCall` broke when review lenses and model fallback changed shared code; tickets did not list them.
- `tests/test_hypotheses.py` called the real router instead of stubbing `yoke.pipeline.triage_failure`; the user had to nudge with the exact patch target.
- `test_nudge` read `code-r1/prompt.md`, a path the review loop does not use; the user nudged with where to look.
- A coder left an unrelated `_decide` hunk in `router.py` across two review rounds (scope drift).
- An indentation error in `scheduler._loop` after wrapping a loop body.

## 2. What the plans lacked

The plans were good at decisions and milestone order, thin on design a headless agent can execute:

1. **No impacted-tests inventory per work item.** The M16-M22 plan had one good "Existing test updates required" paragraph, but it was global; wayfinder spread it unevenly, and tickets like T-0060 got only `"python3 -m unittest discover -s tests passes"`.
2. **No named test cases or fakes.** Tickets said what to test, not where, with which helper (`FakeAdapter`, `tests/fake_cli.py` labels, `mock.patch` target). QA guessed, and guessed wrong in T-0072 and T-0074.
3. **Interfaces implied, not fixed.** Function names, event payloads and audit dir names lived in prose. Tickets that shared them could disagree.
4. **No ordered steps.** The ticket `description` was a paragraph; the coder re-derived the design, which is where scope drift and broken edits came from.
5. **Plan detail stopped at the plan.** Coder, QA and validator see only the ticket JSON and `plan_excerpt`. A one-line `plan_excerpt` (T-0060) loses everything else the plan said.
6. **Plan review did not check executability.** It checked requirements coverage and ordering, not whether a coder could act without guessing.

## 3. Recommendations and what was done

| # | Recommendation | Status |
|---|---|---|
| R1 | Plan skill explores code first (callers, existing tests, fakes), then writes fixed sections: Context, Architecture, Decisions, Interfaces, Work items, Cross-cutting, Verification, Risks, Out of scope | Done: `skills/plan/SKILL.md`, `skills/go/SKILL.md` |
| R2 | Each work item lists files, ordered steps, interfaces, named test cases with helpers, impacted existing tests, exit criteria, risks | Done: plan template |
| R3 | Carry design into tickets with optional fields `design`, `interfaces`, `steps`, `test_plan`, `impacted_tests`, `risks` | Done: `ticket.json`, `ticket-list.json`, `chart.json` |
| R4 | Wayfinder fills those fields from the plan and checks them against the code | Done: `prompts/chart.md` |
| R5 | Plan review treats guess-requiring work items, undefined shared interfaces, missing named tests, unlisted impacted tests, and oversized items as major | Done: `prompts/plan_review.md` |
| R6 | Coder follows steps and interface names, updates impacted tests, returns blocked instead of redesigning; QA implements `test_plan` and avoids leaking state; validator checks interface names and impacted tests | Done: `prompts/code.md`, `qa.md`, `validate.md` |
| R7 | Lint: warn on feature/bugfix/refactor tickets without `steps` or `test_plan` | Done: `tickets.warnings` |
| R8 | Show design and steps in the GitHub issue body | Done: `tracker._body` |

The new fields reach agents through the existing `$ticket` JSON, so no template variables, no pipeline changes, and no change to approval hashing beyond the fields' content. They are optional so hand-written and older `tickets.json` still import.

## 4. Not done (candidates for later)

- **QA self-check of the full suite.** 22 of 30 retries are the runner's full-suite re-run. A cheaper fix than better plans alone: let QA's `test_command` be the full suite by default, or have the runner run the suite once before QA so QA sees the existing breakage in its prompt. Needs a pipeline change; measure after M23 first.
- **Planner as a yoke call.** The plan is still written inline by the chat session. A read-only `planner` stage with a structured schema (per-work-item fields matching the ticket fields) would make the plan machine-checkable and let wayfinder copy rather than re-derive. Worth it if plans still come back thin.
- **Suite hygiene ticket.** The suite prints `ResourceWarning` from `open(...)` without `with` in `tests/test_tracker.py` and stray `warning: T-001 became issue #40` lines. Fixing that once removes noise from every failure message agents read.
- **agy read permission** (`ViewFile`/`ReadFile` denied) is an adapter grant issue, not a planning one.

## 4a. Contents index in every document

Long prompts lose detail in the middle, and agents re-read whole documents to find one section. Every plan, requirements file and long agent-written doc now starts with a `## Contents` list of `[title](#anchor)` links, and the prompts tell agents to read the index first and jump to what they need. `yoke approve plan` refuses a plan whose index is missing, has a dead link, or leaves out a `##` section (`planreview.toc_errors`). Tickets carry `plan_sections` anchors. The plan is still pasted whole into chart and plan-review prompts; slicing sections into prompts by anchor is a follow-up once anchors are reliable.

## 5. How to measure

Compare the next runs against the table in section 1: share of tickets needing a second attempt, and the share of retries caused by the full-suite re-run. The `events.jsonl` `call_start` events with `stage: code` and `attempt > 1` give the first number; the feedback classification script used here gives the second.
