---
name: finish
description: Final review gate for a finished yoke run: summarize commits and open issues, record final approval, optionally open a PR. Use when the run state is done.
---

# finish

Gate: run `yoke setup --check` first. If it fails or `yoke` is not found, stop and tell the user to run `/yoke:setup`.
If the user gives a run id, pass `--run <id>` to every `yoke` command.

1. Confirm `yoke status` says the run is done.
2. Summarize for the user: run `yoke map` to show the decisions log and milestone map, the commits on the feature branch (`git log --oneline <base_sha>..<branch>`), `git diff --stat <base_sha>..<branch>`, and any ticket that was skipped or left open. Branch and base sha come from `yoke status --json` and the run's `run.json`.
3. Ask whether to approve the result. Only on an explicit yes, run `yoke approve final`.
4. Only for a GitHub repo, and only if the user explicitly says yes to a PR, run `yoke pr`. It is the only command that pushes. Never push by any other means.
