---
name: dashboard
description: Serve a local, read-only web dashboard showing yoke runs, ticket status, and progress.
---

# dashboard

Gate: run `yoke setup --check` first. If it fails or `yoke` is not found, stop and tell the user to run `/yoke:setup`.

1. Optional argument: `--port <port>` (default port is 8765).
2. Start `yoke dashboard` (or `yoke dashboard --port N` if a custom port was requested) as a background shell process. This runs as a foreground command kept alive by the host's background-run facility (no new daemon code).
3. Check the background task's output for `dashboard: http://127.0.0.1:<port>`. If the port is already taken (the command exits with an address in use error), report it to the user and offer to rerun with `--port <port>`.
4. Confirm the server is up by fetching `/api/runs` (e.g. `http://127.0.0.1:<port>/api/runs`). Treat a failed fetch or early task exit as a startup failure.
5. Print the dashboard URL: `http://127.0.0.1:<port>`.
6. Tell the user that the dashboard is read-only and explain how to stop it (stop the background task).
