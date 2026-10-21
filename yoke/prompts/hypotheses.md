You are investigating a failure, read-only. Formulate a competing hypothesis for the assigned category and failure evidence.

Category:
$category

Failure evidence:
$feedback

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

$upstream
Inspect the code in working directory $worktree (base commit $base_sha).
Formulate a concrete hypothesis explaining why the failure occurred under category '$category'.
Cite concrete evidence from the code or logs, provide a clear fix plan, and provide your confidence score (0.0 to 1.0).
Never decide pass or fail on confidence alone.

Rules: read-only, do not edit files, do not run commands, do not commit, do not push. Return only a JSON object matching the schema.
