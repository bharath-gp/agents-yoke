# yoke

<p align="center">Claude plans and validates. Antigravity codes, tests and documents. You approve.</p>

<p align="center">
  <a href="#install">Install</a> &ensp;·&ensp;
  <a href="#run-your-first-work-item">Quickstart</a> &ensp;·&ensp;
  <a href="docs/getting_started.md">Documentation</a>
</p>

yoke is a plugin for Claude Code and Antigravity (`agy`) that takes a large work item from requirements to a pull request. One agent plans the work and checks the result. A second agent writes the code, tests and docs, one ticket at a time, in its own git worktree. A detached runner moves each ticket through code, review, QA, validation and docs. It stops only when it needs you.

Use it for work that spans many files and several days of tickets. For a small, clear change, use your normal coding workflow.

## Contents

- [Install](#install)
- [Run your first work item](#run-your-first-work-item)
- [How it works](#how-it-works)
- [Choose a skill](#choose-a-skill)
- [Learn more](#learn-more)
- [Develop yoke](#develop-yoke)
- [License](#license)

## Install

You need Python 3.9 or later, `git`, `claude` and `agy`. `gh` is needed only for GitHub issues and pull requests. `ollama` with the `tev1:4b` model is optional: it makes small routing decisions locally, and yoke falls back to a small Claude call without it.

Install the plugin for Claude Code and Antigravity, plus the `yoke` CLI:

```sh
npx github:bharath-gp/agents-yoke
```

The installer asks whether to install for all your projects or this repo only. See [getting started](docs/getting_started.md#install-options) for its flags.

Claude Code without npx:

```text
/plugin marketplace add bharath-gp/agents-yoke
/plugin install yoke@yoke
```

Then, in your agent, run setup once. It installs the CLI if it is missing and checks your environment:

```text
/yoke:setup
```

In Antigravity, use the `setup` skill. Start a new session after setup so the yoke MCP server connects.

## Run your first work item

Open your project in Claude Code and pick a work item that needs a plan, for example: "add webhook delivery with retries and deduplication". Each step below is a skill. In Antigravity, pick the skill of the same name.

**1. Pin down requirements**

```text
/yoke:grill Add webhook delivery to this project.
```

The agent asks one question at a time, confirms which models to use, writes `requirements.md` and creates a run.

**2. Write and review the plan**

```text
/yoke:plan
```

The agent reads the code and writes `plan.md`. The secondary agent reviews it. You see every finding, including the rejected ones, and approve the plan.

**3. Split the plan into tickets**

```text
/yoke:tickets
```

The plan becomes milestones and tickets. Check the map and approve. On a GitHub repo, approval also creates one issue per ticket.

**4. Run the tickets**

```text
/yoke:run
```

Choose how many tickets run in parallel. The runner works in the background. The chat only reports escalations, which you answer in chat.

**5. Review and open a pull request**

```text
/yoke:finish
```

Review the commits and diff, approve the result, and optionally open a pull request. `yoke pr` is the only step that pushes.

To do all five steps in one session, run `/yoke:go`. It stops for plan approval, escalations, final approval and the pull request.

See [getting started](docs/getting_started.md) for what happens at each step, where run state lives and how to resume.

## How it works

| Role | Default agent | Responsibility |
|---|---|---|
| **Primary** | Claude | Gather requirements, write the plan, chart tickets, validate each ticket, open the pull request. |
| **Secondary** | Antigravity (`agy`) | Review the plan, write code, run QA, write docs. |
| **Reviewer** | The agent that did not write the code | Review each ticket's diff through security, performance and tests lenses. |
| **You** | | Approve the plan, the tickets and the final result. Answer escalations. |

A ticket is committed to the feature branch `yoke/<run_id>` only when:

- the coder reports done,
- QA passes, and the runner's own re-run of the test command exits 0,
- the validator approves with every exit criterion met, and
- docs are done.

A failed ticket retries with feedback, then escalates to you. Approvals are bound to the hash of `plan.md` and `tickets.json`, so editing either voids its approval. Run state and the full audit trail live in `~/.yoke/runs/<run_id>/`, outside your repo. The runner never pushes.

You can change which agent plays each role. See [configuration](docs/configuration.md#agent-roles).

## Choose a skill

You can run each skill on its own. You do not have to follow the full sequence.

| Skill | Use it to… |
|---|---|
| [`/yoke:setup`](skills/setup/SKILL.md) | Install the CLI, check the environment, choose primary and secondary agents. |
| [`/yoke:go`](skills/go/SKILL.md) | Run the whole workflow, from interview to pull request, in one session. |
| [`/yoke:grill`](skills/grill/SKILL.md) | Turn an idea into `requirements.md` and start a run. |
| [`/yoke:plan`](skills/plan/SKILL.md) | Write `plan.md`, run review rounds and get your approval. |
| [`/yoke:tickets`](skills/tickets/SKILL.md) | Chart the plan into milestones and tickets and get your approval. |
| [`/yoke:run`](skills/run/SKILL.md) | Start the runner and relay its escalations to you. |
| [`/yoke:status`](skills/status/SKILL.md) | Show how the current run is going. |
| [`/yoke:resume`](skills/resume/SKILL.md) | Pick up a run in a new or compacted session. |
| [`/yoke:finish`](skills/finish/SKILL.md) | Review the result, give final approval and open a pull request. |
| [`/yoke:models`](skills/models/SKILL.md) | View or change the models and reasoning effort per agent. |
| [`/yoke:dashboard`](skills/dashboard/SKILL.md) | Open a local web dashboard of runs, tickets and cost. |

## Learn more

- [Getting started](docs/getting_started.md): the workflow step by step, approvals, escalations and resuming.
- [CLI reference](docs/cli.md): every `yoke` command, for scripting and debugging.
- [Configuration](docs/configuration.md): roles, models, review, budgets, rate limits, safety and hooks.
- [Features](docs/features.md): milestone gates, the dashboard, GitHub tracking, pull requests and research agents.

## Develop yoke

```sh
uv tool install --editable .
claude plugin marketplace add . && claude plugin install yoke@yoke
python3 -m unittest discover -s tests
```

`tests/e2e.sh [normal|fallback]` runs real agents on a disposable repo and spends quota.

## License

MIT. See [LICENSE](LICENSE).
