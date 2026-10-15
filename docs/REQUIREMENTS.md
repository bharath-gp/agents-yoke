# Requirements: Primary/Secondary Agent Loop (Claude + Antigravity)

## Context
Run very big work items through a multi-agent loop: a primary agent (Claude today) plans and orchestrates, a secondary agent (Antigravity `agy` today) implements. Inspired by chaseai-yt/claudex-loop (stdlib Python runner, one CLI turn per call, schema-validated JSON results, hashed approvals, bounded rounds, independent review). This doc = discovery output only. Planning + implementation happen in later sessions.

## Facts established
- `agy` installed at ~/.local/bin/agy. Supports `-p/--print`, `--output-format json|stream-json`, `--json-schema`, `--mode plan|accept-edits|auto`, `--sandbox`, `--conversation <id>` resume, `--agent`, `--model`, `--effort`, `mcp` subcommand, `plugin` subcommand.
- claudex-loop: claude/codex CLIs driven by a runner that records prompt/command/stdout/result per call outside the target repo; reviews read-only; approvals bound to plan hash.
- Repo /Users/bharath/Code/agents-yoke is empty.

## Architecture (from diagram + answers)
Colour map: red = primary (Claude), yellow = secondary (agy), black = router.

Front half (interactive, in primary session):
1. Requirements gathering (grilling) -> requirements
2. Planner -> plan -> **secondary reviews plan read-only, bounded rounds**, primary arbitrates -> **user approves plan**
3. Wayfinder splits plan into tickets (with type + exit criteria) -> **user approves ticket list**

Back half (unattended after ticket approval):
- **Orchestrator** = long-lived primary session. Does no heavy lifting; only dispatches agents and reacts to results. Reads status/verdict summaries only.
- **Decision router + Ollama MCP server** (MCP from day one): local small models (Ollama) for minor decisions/assertions, ticket routing/type classification, result triage, summarising for orchestrator, commit-message/status chores. Router maps ticket type -> stages, provider, model, effort via config. Usable by both runner/orchestrator and agent sessions. Fallback if Ollama down/invalid/low-confidence: escalate to a small primary-LLM call, logged; never block.
- Per-ticket pipeline (stages vary by ticket type): **Code (secondary) -> QA (secondary) -> Validate (primary) -> Docs (secondary)**
  - Coding: implements ticket.
  - QA: writes/runs tests needed for product to work and to validate changes.
  - Validation: reviews all changes, assumptions and work done by the secondary agent against ticket exit criteria.
  - Docs: updates relevant documentation.
- Ticket closed (-> commit) only when: ticket exit criteria met AND code written AND QA tests pass AND validation approves AND docs updated.
- Failure: bounded retry (failure feedback to coder, session resumed by ID), then ticket blocked and loop escalates to user.

## Cross-cutting requirements
- **Agent-agnostic**: user picks primary and secondary agent (Claude/agy now; any two later) via adapters per CLI. Default role map: primary = requirements, planner, wayfinder, orchestrator, validation; secondary = coding, QA, docs; overridable per role.
- **Packaging**: Claude Code plugin; also installable in agy (agy has `plugin`/`mcp` subcommands) and other coding agents where possible. Works on any target repo.
- **Execution**: sequential first. Requirement captured: run independent tickets in parallel (detached processes, worktree per ticket); **number of parallel tickets is an input to the loop**.
- **Context handoff**: files + structured JSON (ticket file, plan excerpt, prior-stage result JSON; schema-validated outputs); sessions resumed by ID for retries.
- **State/resumability**: all state on disk; new orchestrator session resumes from disk any time; automatic handoff to next session where possible; provision for periodic compaction.
- **Tickets/commits**: local files + worktree + feature branch by default, one commit per closed ticket, never auto-push. For GitHub-hosted repos: GitHub Issues + PR.
- **Permissions**: workers run worktree-scoped and sandboxed (agy `--sandbox`, no skip-permissions, no push).
- **Human gates**: plan approval, ticket-list approval, escalation, final review. Nothing pushed/merged without user.
- **Budgets**: max attempts per ticket, max wall-clock per agent call (kill hung detached processes), max total tickets/iterations. No token/cost cap initially.
- **Audit**: record per call prompt, command, stdout/stderr, result, model/effort requested vs observed, outside target repo.

## Open items for planning phase
- Ticket JSON schema; result schemas per stage; plugin layout; Ollama model choice/hardware; Ollama MCP tool surface; how agy plugin install maps; agy/Claude auth + CLI version checks; default values for attempts/timeouts/iterations; orchestrator handoff mechanism (how a fresh session is spawned); parallel merge strategy.

## Verification (later)
Fake-CLI unit tests (like claudex), disposable-repo end-to-end run with trivial 3-ticket task, Ollama-down fallback test, resume-from-disk test.
