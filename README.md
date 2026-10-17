# yoke

Multi-agent loop for large work items. Claude (primary) gathers requirements, plans, splits tickets and validates; Antigravity `agy` (secondary) codes, writes tests and docs. Agents are pluggable per role via `yoke/adapters.py`.

## How it works

1. Interactive: requirements, `plan.md`, one read-only secondary plan review, ticket split. You approve the plan and tickets (hash-bound).
2. Unattended: a detached runner takes each ticket through code, review, QA, validate, docs. Failures retry with feedback up to a limit, then escalate to you. Independent tickets run in parallel, one git worktree each (`--parallel N`).
3. A ticket is committed to feature branch `yoke/<run_id>` only when QA passes and the validator approves. The runner never pushes; `yoke pr` does, after `approve final`.
4. State and audit trail (prompts, commands, outputs) live in `~/.yoke/runs/<run_id>/` (`$YOKE_HOME` overrides), outside the target repo. Any session can resume from disk.

A local Ollama model (`tev1:4b`) handles small routing/triage decisions; fallback is a small Claude call, so the loop never blocks.

## Prerequisites

Python >= 3.9, `git`, `claude`, `agy`, `ollama` with `tev1:4b`, `gh` (only for GitHub issues and `yoke pr`). Check with `yoke doctor [--live]`.

## Install

yoke is used from inside coding agents, as a plugin. Fastest, for Claude Code and Antigravity (installs the CLI too, asks user-wide vs this repo):

```bash
npx github:bharath-gp/agents-yoke                # flags: --scope user|project|local, --only claude,agy, --no-cli, --dry-run
```

Then in the agent run `/yoke:setup` (agy: the `setup` skill). Scopes: `user` = all your projects; `project` = this repo only, shared through git (`.claude/settings.json`); `local` = this repo only, not shared. agy has no scope option, so it is always user-wide.

Claude Code without npx:

```
/plugin marketplace add bharath-gp/agents-yoke
/plugin install yoke@yoke        # pick user or project scope
/yoke:setup
```

`/yoke:setup` installs the `yoke` CLI if missing (`uv tool install` from GitHub, `pipx` fallback), runs the environment check and records success, either user-wide (`~/.yoke/setup.json`) or for the current repo only (`~/.yoke/projects/`). Every other skill (`/yoke:grill plan tickets run resume status finish`) first runs `yoke setup --check` and tells you to run `/yoke:setup` if it fails. A SessionStart hook shows the same hint. Rerun setup or the installer to update. The repo must be readable with your git credentials (it is private until made public).

Development: `uv tool install --editable .`, then `claude plugin marketplace add . && claude plugin install yoke@yoke`.

### Role mapping and review

Agent roles are configured via `yoke setup --primary <agent> --secondary <agent> --map <role>=<who>`. Supported roles include `requirements`, `planner`, `wayfinder`, `orchestrator`, `plan_review`, `code`, `review`, `qa`, `validate`, `docs`, `search`, `research`, and `factcheck`.

The `review` role defaults to `auto`, resolving dynamically to the opposite of the `code` agent (`claude` <-> `agy`), ensuring code review is performed by a different agent than the implementer. For custom code agent names outside `AGENTS`, it falls back to `primary` (or `secondary` if `primary` matches `code`). `yoke setup` warns if an explicit override assigns the same agent to both code and review. Use `--map review=auto` to restore automatic resolution.

During ticket execution, the `review` stage runs an iterative review loop (up to `max_review_rounds` revisions). Blocker and major findings trigger coder revisions using the resumed session. If findings remain unresolved when reaching the revision cap, the ticket proceeds without failing: unresolved findings are recorded in ticket state, posted as issue comments, and surfaced in the `yoke pr` description under "Unresolved review findings".

## Typical flow

The skills run these for you; the raw commands are for scripting and debugging.

```bash
yoke init --requirements req.md   # prints run_id; write plan.md into ~/.yoke/runs/<run_id>/
yoke review-plan                  # one read-only secondary review round
yoke approve plan                 # also: tickets (creates GitHub issues if tracker=github), final
yoke tickets import tickets.json  # validate, classify untyped tickets; `yoke tickets show`
yoke route T-001                  # stages and agents for a ticket
yoke run --detach --parallel 2    # background runner; always resumes from disk
yoke wait                         # blocks until escalation, done, needs_user, stopped, died or timeout
yoke status [--json|--brief]      # also: escalations, unblock T-001 [--reset-attempts], stop
yoke approve final && yoke pr     # push branch, open PR
yoke clean                        # remove worktrees (keeps branches and audit)
```

Subcommands take `--run <id>`; default is the latest run for the current repo. `yoke mcp` is a stdio MCP server exposing the router.

## Research agents

```bash
yoke search "how does X work"            # ranked sources (claude, web tools)
yoke research "topic" --depth deep       # search -> research (agy) -> fact-check loop, writes docs/research/<topic>.md
yoke factcheck docs/some-file.md         # per-claim verdicts for any markdown file in the repo
```

`research` is also a ticket type (`search`, `research`, `factcheck` stages). Web tools come from `search.tools` in the config; depth presets from `research.depths`.

## Tests

```bash
python3 -m unittest discover -s tests
tests/e2e.sh [normal|fallback]    # manual: real claude + agy + ollama on a disposable repo, spends quota
```

## Docs

- [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md): what the system must do.
- [docs/PLAN.md](docs/PLAN.md): design decisions, plus per-milestone notes on what was built.
- [CLAUDE.md](CLAUDE.md): guidance for Claude Code sessions in this repo.

## License

MIT. See [LICENSE](LICENSE).
