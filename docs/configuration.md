# Configuration

yoke works with its defaults. Change them when you want different agents or models, stricter review, spending caps or extra checks.

## Contents

- [Where configuration lives](#where-configuration-lives)
- [Agent roles](#agent-roles)
- [Models and tiers](#models-and-tiers)
- [Ticket stages](#ticket-stages)
- [Code review](#code-review)
- [Plan and ticket review](#plan-and-ticket-review)
- [Retries and test fixes](#retries-and-test-fixes)
- [Write boundary](#write-boundary)
- [Budgets](#budgets)
- [Rate limits](#rate-limits)
- [Gate hooks](#gate-hooks)
- [Run notifications](#run-notifications)
- [Decision models](#decision-models)
- [Safety guard](#safety-guard)
- [Skills for agents](#skills-for-agents)
- [Browser end-to-end stage](#browser-end-to-end-stage)
- [Other settings](#other-settings)

## Where configuration lives

Configuration is JSON, merged in this order (later wins):

1. Built-in defaults: [`yoke/defaults.json`](../yoke/defaults.json). Read it for every key and its default.
2. User config: `~/.yoke/config.json`.
3. Repo config: `<repo>/.yoke.json`.

A run keeps the configuration it was created with.

Some keys run shell commands or set safety policy. yoke accepts them only from user config and ignores them in repo config with a warning, so a cloned repo cannot change them:

- `gate_hooks`
- `notify_command`
- `guard`
- `router.thresholds.safety`

The environment variable `YOKE_OLLAMA_URL` overrides `router.ollama_url`.

## Agent roles

Each role is played by the `primary` agent, the `secondary` agent, or a named agent (`claude` or `agy`). Set them with `yoke setup` or under `roles` in config:

```sh
yoke setup --primary claude --secondary agy --map code=claude --map review=auto
```

| Role | Default | Used for |
|---|---|---|
| `requirements`, `planner`, `wayfinder`, `orchestrator` | primary | Interview, plan, chart tickets. |
| `plan_review`, `readiness` | secondary | Review the plan and the tickets. |
| `code`, `qa`, `docs` | secondary | Write code, tests and docs. |
| `review` | `auto` | Review each ticket's diff. `auto` picks the agent that is not the coder. |
| `validate` | primary | Check exit criteria and approve the ticket. |
| `search`, `factcheck` / `research` | primary / secondary | Research agents. |
| `pr_body` | primary | Fill the pull request template. |
| `hypotheses` / `hypotheses_rank` | secondary / primary | Explain repeated failures. |
| `e2e` | primary | Fallback for the browser stage. |

`yoke setup` warns if you assign the same agent to `code` and `review`.

## Models and tiers

Each agent has two tiers. The `plan` tier serves `requirements`, `planner`, `plan_review`, `wayfinder` and `readiness`. The `cheap` tier serves every other role.

```json
{
  "models": {
    "claude": {
      "plan": {"model": "opus", "effort": "high"},
      "cheap": {"model": "sonnet", "effort": "low"}
    }
  }
}
```

Set models with `/yoke:models` or `yoke models --set claude.plan=opus --effort high`. `yoke models --list` shows the choices per agent.

For `code`, `review`, `qa`, `validate` and `docs`, yoke picks the tier and effort per ticket when no override sets them. It asks the local decision model, using the ticket's size and risk. If the answer is below the confidence threshold, it uses the cheap tier. With `router.retry_bump` (default `true`), each retry raises the effort one step (`low`, `medium`, `high`) and then moves to the plan tier.

If a call fails because of its model, yoke retries it once with the agent's default model.

## Ticket stages

`routing` sets the stages for each ticket type. To pin an agent, model or effort for one stage, add `overrides`:

```json
{
  "routing": {
    "feature": {
      "stages": ["code", "review", "qa", "validate", "docs"],
      "overrides": {"code": {"agent": "claude", "model": "opus", "effort": "high"}}
    }
  }
}
```

An override bypasses the dynamic model pick for that stage. Add `e2e` to a type's stages to turn on the [browser end-to-end stage](#browser-end-to-end-stage).

## Code review

The `review` stage reviews each ticket's diff, then sends blocking findings back to the coder, for up to `max_review_rounds` rounds (default 2).

| Key | Default | Effect |
|---|---|---|
| `review_lenses` | `["security", "performance", "tests"]` | One read-only review per lens. Findings are merged by file and line. `[]` runs a single general review. |
| `review_mode` | `"adversarial"` | Blocker and major findings go to debate: the coder rebuts, and a plan-tier judge rules on disputes. Only upheld findings go back to the coder. `"independent"` skips the debate. |
| `debate_rounds` | `1` | Above 1, the reviewer can reply to the rebuttal before the judge rules. |
| `goldfish_context` | `true` | Reviewers, the judge and the validator do not see the coder's and QA's own reports. They see the diff, the runner's test evidence and the ticket. |
| `zoom_out_after_fix` | `false` | Opt-in flag to run a zoom-out review after code fixes. |
| `escalate_findings` | `false` | Opt-in flag enabling review rebuttals to escalate findings. |

Findings the judge overruled, or that were fixed, are remembered for the ticket. A lens that raises the same finding again is ignored. Findings still open at the round cap do not fail the ticket. They are recorded and listed in the pull request under "Unresolved review findings".

## Plan and ticket review

| Key | Default | Effect |
|---|---|---|
| `max_plan_review_rounds` | `3` | Review rounds per plan. |
| `plan_review_lenses` | `[]` | One review per lens: `zoom_out` (simpler, reuse), `comprehension` (can a new reader restate it), `critic` (failure modes, requirements), `readiness` (no decisions left to the coder). `[]` runs a single review. |
| `ticket_review` | `false` | Review each ticket for readiness before approval. `yoke approve tickets` then refuses tickets that are not ready. |
| `ticket_review_max` | `30` | Most tickets reviewed per round. |
| `max_ticket_review_rounds` | `2` | Readiness review rounds. |

The rubric for each lens is in [`yoke/standards/`](../yoke/standards/).

## Retries and test fixes

| Key | Default | Effect |
|---|---|---|
| `max_attempts_per_ticket` | `3` | Attempts before a ticket escalates. |
| `test_command` | `null` | Command the runner uses to test a ticket. When unset, it uses the command QA reported. |
| `max_test_fix_rounds` | `2` | After `code`, the runner runs the tests and resumes the coder on failure, up to this many rounds. `0` turns it off. |
| `hypotheses` | `0` | On the last attempt, when a ticket fails the same way twice, run this many read-only calls that each propose a cause (spec gap, environment, test bug, code bug). The primary agent ranks them and the best fix plan is added to the coder's feedback as advice. |
| `parallel` | `1` | Tickets in flight at once. `yoke run --parallel N` sets it per run. Tickets whose files overlap a running ticket wait. |
| `timeouts_s` | per stage | Seconds before a call is killed. `timeouts_s.test` (600) limits test runs. |

## Write boundary

A ticket may list the files it will change in `files` (`[{"path": ..., "action": "create" | "modify" | "delete"}]`). At close, the runner checks that the ticket's changes stay inside that list and `write_boundary_extra`. Changes outside it fail the close gate and go back to the coder.

| Key | Default |
|---|---|
| `write_boundary` | `true` |
| `write_boundary_extra` | `["tests/**", "docs/**", "README.md"]` |

Tickets without `files` are not checked. Gitignored files are not checked.

## Budgets

```json
{
  "max_agent_calls_total": 300,
  "budget": {"max_cost_usd": 25.0, "max_tokens": 1000000}
}
```

`max_cost_usd` and `max_tokens` default to `null` (off). `max_cost_usd` counts reported cost only; agy reports none. When a cap is reached, the runner starts no new calls, lets running calls finish and opens one escalation.

## Rate limits

When an agent's output matches a rate-limit pattern, the runner pauses all new calls, waits until the reset time (or backs off), then retries the same call. The retry does not count as an attempt. `yoke status` and the dashboard show `waiting for limit until <time>`.

| Key | Default | Effect |
|---|---|---|
| `limits.patterns.<agent>` | see defaults | Regular expressions that mark a rate limit. Extend them if a provider changes its messages. |
| `limits.max_wait_s` | `7200` | Longest single wait. |
| `limits.max_waits` | `3` | Waits for one call before it escalates. |
| `limits.backoff_s` | `30` | Base backoff when no reset time is given. |

The default patterns are tested against sample output only, not against live providers.

## Gate hooks

Run your own checks after stages or before a ticket is committed. User config only.

```json
{
  "gate_hooks": {
    "after_stage": {"code": ["pytest tests/unit -q"], "qa": ["npm run lint"]},
    "before_close": ["git diff --check"]
  }
}
```

Hooks run in the ticket's worktree with `YOKE_TICKET`, `YOKE_STAGE` and `YOKE_ATTEMPT` set. Exit 0 passes. Exit 2 fails the gate and sends the output back to the coder. Any other exit, or a timeout (`timeouts_s.gate_hook`, 300), is a hook error.

## Run notifications

Run a command when a run finishes or needs you. User config only.

```json
{
  "notify_command": "osascript -e \"display notification \\\"$YOKE_MESSAGE\\\" with title \\\"yoke\\\"\""
}
```

The command gets `YOKE_EVENT` (`run_done` or `run_needs_user`), `YOKE_RUN` and `YOKE_MESSAGE` as environment variables. It has 10 seconds. A failure is logged and never stops the run.

## Decision models

A local Ollama model (`router.model`, default `tev1:4b`) makes small, frequent decisions. Below its confidence threshold, or when Ollama is down, yoke uses a deterministic default, so a run never blocks on it.

| Decision | Threshold | Decides |
|---|---|---|
| `safety` | 0.7 | Whether a tool call, command or diff is risky. |
| `test_failure` | 0.7 | Whether a test failure is in the product (retry code), the tests (rerun QA) or the environment (rerun once, then escalate). |
| `after_failure` | 0.85 | After a failed attempt: retry, or escalate (also for split and replan). |
| `gate_failure` | 0.85 | After a failed milestone exit check: add fix tickets or escalate. |
| `validator_reject` | 0.75 | Which stage restarts after a validator rejection. |
| `model_tier`, `effort` | 0.7 | Tier and effort per stage. |
| `skill` | 0.6 | Which skills to give the agent. |
| `e2e_pick`, `e2e_verdict` | 0.6, 0.8 | Browser stage actions and verdicts. |
| `source_screen`, `claim_screen` | 0.75, 0.8 | Research source and claim screening. |

Set thresholds under `router.thresholds`. A decision without a threshold uses `router.min_confidence` (0.7). The dashboard shows how often each decision came from the model or a default.

## Safety guard

The guard checks four things:

1. Each ticket's staged diff before commit: secrets, sensitive files, large deletions, CI files and network calls.
2. Claude's tool calls during a run, through a PreToolUse hook.
3. Your own chat session's tool calls while a run is active, through the plugin's hook.
4. The test command before the runner runs it.

Clear violations (`deny_paths`, `deny_commands`, `secret_patterns`, `sensitive_files`) are denied. Grey cases (`grey_files`, `network_patterns`, more than `max_deleted_lines` deleted) go to the decision model. In chat, a grey case asks you. In the runner, it escalates. Configure it under `guard`, in user config only. `guard.chat_hook: false` turns off the chat check.

## Skills for agents

yoke can give agents relevant skills from `~/.claude/skills`, `<repo>/.claude/skills` and installed plugins, for the `code`, `qa` and `docs` stages.

| Key | Default | Effect |
|---|---|---|
| `skills.allowlist` | `[]` | Glob patterns of skill names agents may get. Empty gives none. |
| `skills.max` | `3` | Most skills per call. |
| `skills.agy_mode` | `"inline"` | agy gets the skill text inline. Claude gets names to invoke. |
| `skills.inline_chars` | `4000` | Cap on inlined text. |
| `skills.roots` | `[]` | Replace the default search folders. |

## Browser end-to-end stage

The optional `e2e` stage opens your app in a browser and checks each exit criterion. It needs Playwright:

```sh
uv tool install --editable '.[e2e]' && playwright install chromium
```

Without Playwright, the stage is skipped and passes. Add `e2e` to a ticket type's stages and set:

| Key | Default | Effect |
|---|---|---|
| `e2e.url` | `null` | App URL. Must be on localhost. |
| `e2e.start_command` | `null` | Command that starts the app. |
| `e2e.ready_timeout_s` | `60` | Seconds to wait for the app. |
| `e2e.max_steps` | `12` | Clicks per criterion. |
| `e2e.candidates` | `20` | Elements considered per step. |

## Other settings

| Key | Default | Effect |
|---|---|---|
| `router.commit_message` | `"agent"` | `"agent"` writes commit messages with a model. `"default"` uses `<id>: <title>` and the validator summary. |
| `agents.claude.setting_sources` | `"project,local"` | Claude settings sources yoke's calls load. Your user `~/.claude/settings.json` does not apply. `""` drops the flag. |
| `tracker` | `"auto"` | `"github"`, `"local"` or `"auto"` (GitHub when the remote is on github.com and `gh` works). |
| `max_fix_tickets_per_milestone` | `2` | Fix tickets per failed milestone gate. |
| `max_tickets` | `40` | Most tickets per run. |
| `research` | see defaults | Research depths and screening. See [research agents](features.md#research-agents). |
