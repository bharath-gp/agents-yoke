# CLAUDE.md

<!-- Maintainers: milestone status lives in docs/PLAN.md section 3 (the [x] checkboxes). Don't add a milestone count here; it goes stale. Top-level sections use `#` so the `## Contents` rule below doesn't apply to this file. -->

`yoke`: Claude plans and validates, Antigravity (`agy`) codes, runs QA and writes docs. [docs/PLAN.md](docs/PLAN.md) is the source of truth for design; each milestone's "Notes" in section 3 records deviations from it. [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) is discovery output. Usage lives in [README.md](README.md).

# Commands

```bash
python3 -m unittest discover -s tests                 # all tests
python3 tests/run_parallel.py                        # all tests, parallel (used by .yoke.json)
python3 -m unittest tests.test_core.TestName.test_x   # single test
uv tool install --editable .                          # put `yoke` on PATH
tests/e2e.sh [normal|fallback]                        # manual, real agents, spends quota
```

# Conventions

- Python >= 3.9, stdlib only: no `tomllib` (config is JSON), no `match`, no `X | Y` runtime type unions. System python is 3.9.6.
- Prompt templates use `string.Template` (`$ticket`, `$upstream`), not `{}`, to avoid JSON brace clashes.
- State and audit records live under `$YOKE_HOME` (default `~/.yoke/runs/<run_id>/`), outside the target repo, written atomically.
- Approvals are bound to sha256 of `plan.md` and canonical `tickets.json`; a changed hash invalidates them. GitHub issue numbers go in `issues.json` and milestone numbers go in `gh_milestones.json`, not in `tickets.json`.
- Markdown docs with more than three `##` sections (repo docs, `plan.md`, agent-written docs) start with a `## Contents` list of `[title](#anchor)` links to every `##` section; keep it in sync. `planreview.toc_errors` is the checker, and `yoke approve plan` refuses a plan that fails it.
- Keep PLAN.md anchors (`#s2-8`, `#m4`, ...) stable.
- Bump the version in `.claude-plugin/plugin.json` and `pyproject.toml` (keep them equal) in any change to the plugin's core logic or behaviour (code, prompts, schemas, defaults, skills, hooks); installed copies only pull new versions when it changes. Docs-only changes need no bump. Plugin files are `.claude-plugin/`, `hooks/`, `skills/`.

# Gotchas

- Never pass `--dangerously-skip-permissions` to any agent CLI. The runner never pushes; only `yoke pr` pushes, after `approve final`.
- Always read an agent's `structured_output` (agy and claude `--json-schema`), never parse the free-text `response`.
- agy runs headless with no shell: code/qa/docs prompts say so; the runner runs tests and commits, never the agent.
- Every agent call goes through `calls.run_call`: audit dir, process-group timeout kill, schema validation, one re-ask, and with `budget=True` the call budget check (`calls.check_budget`).
- Single writers: `ticketstate.py` owns the ticket record (`tickets/<tid>/state.json`, its status transitions `start`, `close`, `await_gate`, `block`, `unblock`, and `escalations.json`); no other module writes ticket status or escalations. `pause.py` is the only writer of rate-limit pause state (`RunPauseState`, `run.json` `waiting_limit`/`limit_waits`). `set_milestone_state` writes milestone gate state.
- The milestone gate rule and `evaluate_gate` verdict live in `milestones.py`; `scheduler._gate_step` applies it.
- `router.py` never blocks: Ollama, then a small Claude call, then a deterministic default.
- `dashboard.py` is a local read-only HTTP server on 127.0.0.1.
- `screen.py` screening and `yoke drift` are advisory.

# Architecture

- `yoke run` is a detached runner walking a per-ticket state machine (`scheduler.py`, `pipeline.py`), resumable from disk (`scheduler.recover`). The chat session only calls `yoke wait` and reads short summaries.
- Agent CLIs sit behind `adapters.py`. `pipeline.build_spec` builds a stage's `CallSpec` (prompt, tools, tier/effort) without running it. `FakeAdapter` plus `tests/fake_cli.py` replay scripted responses so the loop is testable offline.
- Ticket type picks stages from the `routing` config (code, review, qa, validate, docs). Close gate: coder `done`, QA `pass`, runner re-run of QA's `test_command` exits 0, validator `approve` with all exit criteria met, docs done. Failures retry with feedback (coder session resumed by ID) up to `max_attempts_per_ticket`, then escalate.
- The runner enforces sequential milestone gates with integration exit checks and auto-generates fix tickets (`F-xxx`) on failure.
- Git (`gitops.py`): feature branch `yoke/<run_id>`, one worktree and branch `yoke/<run_id>-T-001` per ticket, runner commits, rebase then `--ff-only` merge under a merge lock; a rebase conflict is re-applied with `cherry-pick -n` for the coder to resolve.
- `router.py` is imported by the runner and exposed over stdio MCP by `mcp_server.py` (`.mcp.json`).
