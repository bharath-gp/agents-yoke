# CLAUDE.md

## State of the repo

`yoke`: Claude plans and validates, Antigravity (`agy`) codes, runs QA and writes docs. Milestones M0-M15 are done. [docs/PLAN.md](docs/PLAN.md) is the source of truth for design; each milestone's "Notes" in section 3 records deviations from it. [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) is discovery output. Keep PLAN.md anchors (`#s2-8`, `#m4`, ...) stable. Usage lives in [README.md](README.md).

## Commands

```bash
python3 -m unittest discover -s tests                 # all tests
python3 -m unittest tests.test_core.TestName.test_x   # single test
uv tool install --editable .                          # put `yoke` on PATH
tests/e2e.sh [normal|fallback]                        # manual, real agents, spends quota
```

## Constraints that shape every file

- Python >= 3.9, stdlib only: no `tomllib` (config is JSON), no `match`, no `X | Y` runtime type unions. System python is 3.9.6.
- Never pass `--dangerously-skip-permissions` to any agent CLI. The runner never pushes; only `yoke pr` pushes, after `approve final`.
- Always read an agent's `structured_output` (agy and claude `--json-schema`), never parse the free-text `response`.
- agy runs headless with no shell: code/qa/docs prompts say so; the runner runs tests and commits, never the agent.
- Prompt templates use `string.Template` (`$ticket`), not `{}`, to avoid JSON brace clashes.
- State and audit records live under `$YOKE_HOME` (default `~/.yoke/runs/<run_id>/`), outside the target repo, written atomically.
- Approvals are bound to sha256 of `plan.md` and canonical `tickets.json`; a changed hash invalidates them. GitHub issue numbers go in `issues.json` and milestone numbers go in `gh_milestones.json`, not in `tickets.json`.
- Bump the version in `.claude-plugin/plugin.json` and `pyproject.toml` (keep them equal) in any change to the plugin's core logic or behaviour (code, prompts, schemas, defaults, skills, hooks); installed copies only pull new versions when it changes. Docs-only changes need no bump.

## Architecture

- `yoke run` is a detached runner walking a per-ticket state machine (`scheduler.py`, `pipeline.py`), resumable from disk (`scheduler.recover`). The chat session only calls `yoke wait` and reads short summaries; the plugin's SessionStart hook injects `yoke status --brief --active`.
- Agent CLIs sit behind `adapters.py`. Every call goes through `calls.run_call`: audit dir, process-group timeout kill, schema validation, one re-ask. `FakeAdapter` plus `tests/fake_cli.py` replay scripted responses so the loop is testable offline.
- Ticket type picks stages from the `routing` config (code, review, qa, validate, docs). Close gate: coder `done`, QA `pass`, runner re-run of QA's `test_command` exits 0, validator `approve` with all exit criteria met, docs done. Failures retry with feedback (coder session resumed by ID) up to `max_attempts_per_ticket`, then escalate.
- Milestones and charting: wayfinder maps plan to destination, milestones, and tickets (`chart.py`); runner enforces sequential milestone gates with integration exit checks and auto-generates fix tickets (`F-xxx`) on failure; `yoke map` renders destination, out-of-scope boundaries, gate status, frontier, and decisions log.
- Git (`gitops.py`): feature branch `yoke/<run_id>`, one worktree and branch `yoke/<run_id>-T-001` per ticket, runner commits, rebase then `--ff-only` merge under a merge lock; a rebase conflict is re-applied with `cherry-pick -n` for the coder to resolve.
- `router.py` (Ollama, then small Claude call, then deterministic default; never blocks) is imported by the runner and exposed over stdio MCP by `mcp_server.py` (`.mcp.json`).
- `tracker.py`: GitHub milestones and issues on `approve tickets`, gate close/fix tracking, `yoke pr`; plugin files are `.claude-plugin/`, `hooks/`, `skills/`.
- `dashboard.py`: local read-only HTTP server and JSON API (`/api/runs`, `/api/runs/<id>`) serving single-file UI (`dashboard.html`) on 127.0.0.1; run picker, stage strip, milestone gate status, ticket table, and live polling.
