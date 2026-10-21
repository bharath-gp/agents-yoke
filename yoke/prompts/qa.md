You have no shell in this environment. Do not run any command; edit files only. The runner executes `test_command` itself. Write or extend tests that check the ticket's exit criteria; do not run them. Set test_command to one shell command the runner can re-run from the working directory; it must exit 0 only when everything passes. The runner's re-run is the real gate: report status pass if you believe the tests will pass, fail if you know they will not. Base commit: $base_sha.
Ticket (JSON):
$ticket

Relevant plan section:
$plan_excerpt

Results of earlier stages (JSON):
$prior_results

Feedback from the previous attempt (empty on the first attempt):
$feedback

$upstream
Rules: stay inside the working directory $worktree. Do not commit, do not push, do not touch git config. Return only a JSON object matching the schema.
