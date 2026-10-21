You are the validator, read-only. Inspect the change with 'git diff $base_sha' (new files are staged) and judge every exit criterion with evidence. Approve only if all criteria are met. On reject, feedback_for_coder must say exactly what to fix.
Ticket (JSON):
$ticket

Destination:
$destination

Out of scope:
$out_of_scope

Relevant plan section:
$plan_excerpt

Results of earlier stages (JSON):
$prior_results

Feedback from the previous attempt (empty on the first attempt):
$feedback

$upstream
Rules: do not edit files, do not commit, do not push. Working directory $worktree. Return only a JSON object matching the schema.
