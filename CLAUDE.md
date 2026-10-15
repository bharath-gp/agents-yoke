# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## State of the repo

Pre-implementation. Only `docs/` exists: [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) (discovery output) and [docs/PLAN.md](docs/PLAN.md) (source of truth for design; resolves every open item in the requirements). The product is `yoke`, a primary/secondary agent loop: Claude plans and validates, Antigravity (`agy`) codes, runs QA and writes docs. Code is built milestone by milestone (M0-M9).

## Workflow

- Work is tracked on GitHub: map issue #1 (label `wayfinder:map`) with one `wayfinder:task` sub-issue per milestone, ordered by native GitHub blocking. One ticket per session; claim by assigning it, close with a resolution comment and add a line to the map's "Decisions so far".
- Before implementing a milestone, read its entry in PLAN.md section 3 and the decision sections it lists. PLAN.md anchors (`#s2-8`, `#m4`, ...) are stable; do not rename them.
- When a milestone finishes: tick its checkbox and fill its "Notes" line in PLAN.md, one commit. M0 must also update PLAN.md sections 1, 2.4 and 2.10 with probe findings; later milestones depend on them.

## Commands (once M0/M1 land)

```bash
python3 -m unittest discover -s tests                 # all tests
python3 -m unittest tests.test_core.TestName.test_x   # single test
uv tool install --editable .                          # put `yoke` on PATH
```

## Constraints that shape every file

- Python >= 3.9, stdlib only: no `tomllib` (config is JSON), no `match`, no `X | Y` runtime type unions. System python is 3.9.6.
- Never pass `--dangerously-skip-permissions` to any agent CLI, and the runner never pushes. Pushing happens only via `yoke pr` after `approve final`.
- Always read an agent's `structured_output` (agy and claude `--json-schema`), never parse the free-text `response`.
- Prompt templates use `string.Template` (`$ticket`), not `{}`, to avoid JSON brace clashes.
- State and audit records live under `$YOKE_HOME` (default `~/.yoke/runs/<run_id>/`), outside the target repo, written atomically.

## Architecture (needs several files to see)

- The runner (`yoke run`) is the loop: a detached Python process walking a per-ticket state machine, resumable from disk. The primary chat session is a thin orchestrator that only calls `yoke wait` and reads short summaries. A SessionStart hook re-injects active-run status into new or compacted sessions.
- Agent CLIs are hidden behind `adapters.py` (`build_cmd` pure, `parse` pure). Every agent call goes through `calls.run_call`, which owns the audit dir, process-group timeout kill, schema validation and one re-ask. A `FakeAdapter` plus `tests/fake_cli.py` replays scripted responses so the whole loop is testable offline.
- Per-ticket pipeline: ticket type picks stages from the `routing` config (code, qa, validate, docs). Close gate: coder `done` and QA `pass` and runner re-run of QA's `test_command` exits 0 and validator `approve` with all exit criteria met and docs done. Failures retry with feedback, resuming the coder session by ID, up to `max_attempts_per_ticket`, then escalate.
- Git: feature branch `yoke/<run_id>`, one worktree per ticket (same path for parallel=1 and N), the runner commits (never the agent), rebase then `--ff-only` merge under a merge lock for linear history.
- `router.py` (Ollama, then small Claude call, then deterministic default; never blocks) is imported directly by the runner and also exposed over stdio MCP by `mcp_server.py` for agent sessions.
- Approvals are bound to sha256 of `plan.md` and canonical `tickets.json`; a changed hash invalidates the approval.
