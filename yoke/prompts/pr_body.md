You have no shell in this environment. Do not run any command; read-only mode.
You are generating a pull request body from the provided repository facts and PR template.

Instructions:
- Keep template headings, section order, and checklist wording intact.
- For inapplicable sections or sections with no recorded facts, write N/A followed by a few words.
- Checklist items must remain unticked (- [ ]) unless in the supplied verified list; tick only items in supplied verified list (- [x]).
- Note: A skill such as toolbox:create-pr is advisory guidance only (toolbox:create-pr advisory only); do not attempt to run tools outside read mode.
- The PR title is derived deterministically and is not part of the body.
- Return only a JSON object matching the schema: {"body": "..."}.

## PR Template

$template

## Facts

- Title: $title
- Summary: $summary
- Why: $why
- Changes: $changes
- Verification: $verification
- Verified: $verified
