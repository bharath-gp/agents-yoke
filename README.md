# agents-yoke

`yoke`: run very large work items through a multi-agent loop. A primary agent (Claude) gathers requirements, plans, splits the plan into tickets and validates results. A secondary agent (Antigravity `agy`) codes, writes tests and updates docs. Agents are pluggable per role through adapters.

**Status:** design complete, implementation not started.

## How it works

1. Interactive front half: requirements grilling, plan, bounded read-only plan review by the secondary agent, ticket split. You approve the plan and the ticket list.
2. Unattended back half: a detached runner works each ticket through code, QA, validate and docs, retrying with feedback up to a limit, then escalating to you. Independent tickets can run in parallel, one git worktree each.
3. A ticket is committed to a feature branch only when its exit criteria are met, QA passes and validation approves. Nothing is pushed or merged without your explicit command.
4. All state and a full audit trail (prompts, commands, outputs, models) live in `~/.yoke`, outside the target repo, so any new session can resume from disk.

A local Ollama decision model (tev1) handles small routing and triage decisions, with a fallback to a small Claude call so the loop never blocks.

## Docs

- [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md): what the system must do.
- [docs/PLAN.md](docs/PLAN.md): design decisions and milestones M0-M9.
- Progress: GitHub issue #1 (the map) and its milestone sub-issues.
- [CLAUDE.md](CLAUDE.md): guidance for Claude Code sessions in this repo.
