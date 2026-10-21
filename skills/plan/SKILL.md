---
name: plan
description: Write plan.md for a yoke run, run bounded secondary-agent review rounds, arbitrate findings, and get the user's approval of the plan. Use after grill.
---

# plan

Gate: run `yoke setup --check` first. If it fails or `yoke` is not found, stop and tell the user to run `/yoke:setup`.

The run dir is `$YOKE_HOME/runs/<run_id>/` (default `~/.yoke/runs/<run_id>/`); `yoke status` shows the run id. If the user gives a run id, pass `--run <id>` to every `yoke` command.

1. Explore before writing. Read the code the work touches: entry points, the functions you will change and every caller (grep), the config and schema files involved, and the existing tests that exercise that code (grep the test dir for the module and function names). Note the test helpers and fakes the tests use. Downstream agents run headless with no shell and cannot re-plan, so anything you leave vague they will guess, and a wrong guess costs a full retry.
   Then write `plan.md` in the run dir from `requirements.md`. Right after the title, put a `## Contents` index: one `[title](#anchor)` link per `##` section and per work item, so agents read the index and jump to what they need instead of reading the whole plan (long documents lose detail in the middle). Give each work item a short stable anchor with `<a id="w1"></a>` on the line above its heading; tickets reference these anchors in `plan_sections`. Then these sections, in order:
   - **Context**: current behaviour and the files/functions involved, as found in the code (`path:function`), not as remembered.
   - **Architecture**: components added or changed, how data and control flow between them, where state lives. A short diagram in text is fine.
   - **Decisions**: each design choice with the alternative rejected and why. No open questions: resolve them with the user before writing.
   - **Interfaces**: exact function signatures, schema fields, config keys and defaults, CLI flags, event names and payloads, file paths. These are the contracts tickets share; name them once here.
   - **Work items**: one `###` subsection per future ticket. Each lists: files to change; ordered implementation steps naming the functions to add or edit; the interfaces it creates or consumes; named test cases (`tests/test_x.py::TestY::test_z` and what each asserts, which fake or helper to use); existing tests that this change will break or must keep passing, and how to update them; exit criteria as commands or observations; work-item-specific risks. Size each to one coder session; split anything touching more than about 5 files.
   - **Cross-cutting**: docs, version bumps, shared fixtures, test-suite hygiene (no leaked files or warnings that fail the suite).
   - **Verification**: the commands that prove the whole change, and what the milestone exit checks run.
   - **Risks** and **Out of scope**.
   Be concrete enough that tickets can quote each work item verbatim.
2. Review loop, at most the configured max rounds (default 2):
   - Run `yoke review-plan`. It asks the secondary agent for a read-only review and prints the round dir, verdict and finding count. Read `result.json` in that round dir.
   - For EACH finding decide accept or reject and give a reason. Append the decisions to `plan-review/arbitration.md` in the run dir.
   - Revise `plan.md` for accepted findings. Stop early when the verdict is approve or no finding is accepted.
3. Show the user the plan, plus every unresolved disagreement (rejected findings with your reason). Do not hide rejected findings.
4. Only on the user's explicit yes, run `yoke approve plan`. It refuses when the Contents index is missing, links an anchor that does not exist, or leaves out a `##` section; fix the index and ask again. The approval is bound to the sha256 of `plan.md`; editing the plan afterwards voids it and you must ask again.
5. Next step: the `tickets` skill.
