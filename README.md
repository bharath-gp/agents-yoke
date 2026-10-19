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

`/yoke:setup` installs the `yoke` CLI if missing (`uv tool install` from GitHub, `pipx` fallback), runs the environment check and records success, either user-wide (`~/.yoke/setup.json`) or for the current repo only (`~/.yoke/projects/`). Every other skill (`/yoke:go grill plan tickets run resume status finish models`) first runs `yoke setup --check` and tells you to run `/yoke:setup` if it fails. A SessionStart hook shows the same hint. Rerun setup or the installer to update. The repo must be readable with your git credentials (it is private until made public).

Development: `uv tool install --editable .`, then `claude plugin marketplace add . && claude plugin install yoke@yoke`.

### Role mapping and review

Agent roles are configured via `yoke setup --primary <agent> --secondary <agent> --map <role>=<who>`. Supported roles include `requirements`, `planner`, `wayfinder`, `orchestrator`, `plan_review`, `code`, `review`, `qa`, `validate`, `docs`, `search`, `research`, and `factcheck`.

The `review` role defaults to `auto`, resolving dynamically to the opposite of the `code` agent (`claude` <-> `agy`), ensuring code review is performed by a different agent than the implementer. For custom code agent names outside `AGENTS`, it falls back to `primary` (or `secondary` if `primary` matches `code`). `yoke setup` warns if an explicit override assigns the same agent to both code and review. Use `--map review=auto` to restore automatic resolution.

During ticket execution, the `review` stage runs an iterative review loop (up to `max_review_rounds` revisions). Blocker and major findings trigger coder revisions using the resumed session. If findings remain unresolved when reaching the revision cap, the ticket proceeds without failing: unresolved findings are recorded in ticket state, posted as issue comments, and surfaced in the `yoke pr` description under "Unresolved review findings".

### Model selection and tiers

Models can be configured per agent across two tiers: `plan` (used for `requirements`, `planner`, `plan_review`, `wayfinder`) and `cheap` (used for all other execution stages such as `code`, `qa`, `docs`, `review`, and `validate`):

```json
"models": {
  "claude": {
    "plan": {"model": "claude-sonnet-5-5", "effort": "high"},
    "cheap": {"model": "claude-haiku-5-5", "effort": "low"}
  }
}
```

When an agent call fails due to a model error, yoke automatically retries once without a model (clearing both model and effort) under a `/fallback` audit directory and logs a `model_fallback` event.

Configure and inspect models via `yoke models`:

```bash
yoke models --list [--json]                                      # list available choices per agent
yoke models --show [--json]                                      # print effective plan/cheap models per agent
yoke models --set AGENT.TIER=MODEL [--effort E] [--scope user|project|repo]  # configure model (repeatable)
yoke models --reset [AGENT[.TIER]] [--scope user|project|repo]   # remove model configurations
```

- `--list`: queries available models for each agent (`agy models` dynamically, static list for `claude`).
- `--show`: displays effective `plan` and `cheap` models and effort settings.
- `--set`: sets model choices in user config (`~/.yoke/config.json`) or repo config (`<repo>/.yoke.json`).
- `--reset`: clears model settings for an agent, tier, or all entries.

In agent sessions, run `/yoke:models` (agy: the `models` skill) to interactively inspect, configure, or reset model choices. When kicking off runs with `/yoke:grill` or the end-to-end wrapper `/yoke:go`, a model selection prompt precedes `yoke init` to confirm or update agent models before planning.


## Typical flow

- **/yoke:go**: run the complete workflow (grill, plan, tickets, run, finish) in one session with a single human gate at plan approval (auto-approves tickets and parallel runner).
- Or run individual skills step-by-step: `/yoke:grill` -> `/yoke:plan` -> `/yoke:tickets` -> `/yoke:run` -> `/yoke:finish`.
- Use `/yoke:resume [run_id]` to inspect active runs (`yoke runs --active`) or pick up an interrupted run with run id, and `/yoke:status [run_id]` to check progress.

### Skills

yoke provides the following skills for host agents (Claude Code and Antigravity):

| Skill | Description |
|---|---|
| `/yoke:go` | Complete workflow from requirements through finish in a single session with a single human gate at plan approval. |
| `/yoke:setup` | Check prerequisites and configure agent roles. |
| `/yoke:grill` | Requirements gathering interview writing `requirements.md`. |
| `/yoke:plan [run_id]` | Implementation plan drafting, bounded secondary review rounds, arbitration, and plan approval. |
| `/yoke:tickets [run_id]` | Split approved plan into tickets, validate dependencies, import, and ticket approval. |
| `/yoke:run [run_id]` | Start the detached background runner and orchestrator wait loop. |
| `/yoke:resume [run_id]` | Inspect active runs (`yoke runs --active`) or pick up an interrupted run with run id. |
| `/yoke:models` | Inspect, set or reset the plan-tier and cheap-tier models per agent. |
| `/yoke:status [run_id]` | Check run progress, escalations, and active run states. |
| `/yoke:finish [run_id]` | Final review gate: review commits, diffs, open issues, final approval, and open pull request. |

The skills run these for you; the raw commands below are for scripting and debugging:

```bash
yoke init --requirements req.md   # prints run_id; write plan.md into ~/.yoke/runs/<run_id>/
yoke review-plan                  # one read-only secondary review round
yoke approve plan                 # also: tickets (creates GitHub issues if tracker=github), final
yoke tickets chart [--feedback "note"] # map approved plan to milestones/tickets via wayfinder
yoke tickets import tickets.json  # validate milestones/tickets and warn; `yoke tickets show` groups by milestone
yoke route T-001                  # stages and agents for a ticket
yoke run --detach --parallel 2    # background runner; always resumes from disk
yoke wait                         # blocks until escalation, done, needs_user, stopped, died or timeout
yoke runs [--active] [--json]     # list runs for this repo with goal and next step
yoke status [--json|--brief]      # also: escalations, unblock [T-001|--milestone MS-01], stop; --active lists active runs
yoke approve final && yoke pr     # push branch, open PR
yoke clean                        # remove worktrees (keeps branches and audit)
```

Subcommands take `--run <id>`; default is the latest run for the current repo. `yoke mcp` is a stdio MCP server exposing the router.

## Pull requests (`yoke pr`)

`yoke pr` pushes the feature branch and creates the pull request on GitHub once final approval is granted (`yoke approve final`).

- **Title from plan**: Derived from the first `# ` heading in `plan.md` with any leading `Plan:` prefix stripped (falls back to `yoke: <run_id>`).
- **PR template lookup**: Searches for a PR template in the target repository in the following order:
  1. `.github/PULL_REQUEST_TEMPLATE/` directory (selects the file matching the highest priority ticket type: `feature` > `bugfix` > `refactor` > `test` > `docs` > `chore` > `spike` > `research`, or the first sorted `.md` file).
  2. `.github/pull_request_template.md`
  3. `.github/PULL_REQUEST_TEMPLATE.md`
  4. `pull_request_template.md` in the repository root.
  5. `docs/pull_request_template.md`
- **Agent fill with deterministic fallback**: An agent with role `pr_body` (read mode) fills the template sections from run facts. If the agent call fails, times out, or produces invalid output, yoke falls back to deterministic markdown filling (preserving template headings and mapping facts) and logs a `pr_fill_fallback` event.
- **Checklists ticked only when verified**: Checklist items (`- [ ]`) are only ticked if the item text matches verified facts (such as test suite verification passing close gates). Unverified items are left unchecked or unticked during post-validation.
- **Built-in structure when no template exists**: When no template is found, yoke generates a standard PR body with headings: `Summary`, `Why`, `What changed`, `Verification`, and `Review focus`.
- **Closes issues and unresolved findings**: Appends `Closes #N` lines for tracked issues, along with an `## Unresolved review findings` section listing up to 20 findings from plan review rounds not marked as accepted in arbitration.


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
