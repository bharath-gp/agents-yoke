# Decision models in yoke

Design notes for putting the local Ollama decision model (`tev1:4b` via `/v1/systemone`) to work across yoke. Discovery output, 2026-10-22; becomes milestones in [PLAN.md](../PLAN.md).

## Contents

- [Why the model is idle today](#why-the-model-is-idle-today)
- [Shared rules](#shared-rules)
- [Safety guard](#safety-guard)
- [Branch points](#branch-points)
- [Model and effort pick](#model-and-effort-pick)
- [Skill pick](#skill-pick)
- [Browser testing](#browser-testing)
- [Open items](#open-items)

## Why the model is idle today

Ollama is reachable and answers well (a probe classified a docs ticket `docs` at confidence 0.74). It is simply never asked:

- `classify_ticket` runs only when a ticket lacks a valid `type` (`tickets.import_tickets`); wayfinder always sets one.
- `triage_failure` runs only on the last attempt, when feedback repeats and hypotheses did not handle it (`pipeline._maybe_triage_hint`).
- `assert_check` is MCP-only; the runner never calls it.
- `commit_message` and `summarize` skip Ollama by design: `/v1/systemone` decides, it does not generate (`output_tokens: 0`).
- Model, effort and stage choice is a config lookup (`router.route_ticket`).

Across the last six runs every `router` event is `commit_message` from the Haiku fallback.

## Shared rules

- Every decision is a `choice` (pick one of N) or `noul` (probability) question. Never generation.
- Act immediately: when confidence is at or above the threshold, the model's answer is used. Below it, today's logic (or the Haiku fallback, then the deterministic default) decides, as in `router._decide`.
- Thresholds are per decision point: `router.thresholds.<point>`, default `router.min_confidence` (0.7). Riskier outcomes get higher thresholds.
- Every decision logs a `router` event (point, source, choice, confidence, threshold, fallback reason). The dashboard shows counts and sources per point.
- State fed to the model stays within ~1500 tokens (`STATE_CHARS`); callers prune their input.

## Safety guard

Runs in four places:

1. Pre-commit diff scan (runner, before every ticket commit). It catches secrets or keys added, `.env`/credential files touched, mass deletions, CI or hook files changed, and new outbound network calls. It covers agy, whose only risk is what it writes.
2. Agent tool calls: a PreToolUse hook on claude agents yoke spawns.
3. The user's chat session: the same hook, installed by the yoke plugin and active while a run is active.
4. QA `test_command`: scored before the runner executes it in a real shell.

Flow: a deterministic fast path allows obvious safe calls (Grep, Glob, Read of non-secret paths inside the repo) with no model call. A deterministic denylist (for example `~/.ssh`, `.env*`, `rm -rf` outside the worktree, `curl | sh`) blocks. Everything else goes to the model as `noul` questions: reads secrets, destructive, exfiltrates.

On a flag (tiered):

- Denylist hit, or model confidence >= `thresholds.safety`: block. Agents get the reason as feedback.
- Grey zone: in the chat session, the hook returns `ask`. In the runner, the ticket escalates with the reason.
- Every flag is logged and shown on the dashboard.

## Branch points

| Point | Question | Choices -> action |
|---|---|---|
| Test failure category | runner re-run of `test_command` fails | product -> coder; testware -> QA; environment -> rerun once, then escalate |
| After failed attempt | what next | retry with feedback; split ticket; replan; escalate now |
| Milestone gate failure | route | auto `F-xxx` fix ticket; escalate (spec gap or cross-ticket design) |
| Validator reject | which stage reruns | coder; QA (tests weak); docs; escalate |

Below threshold, each point keeps today's behaviour. The test-failure category replaces the plain flaky check and feeds `triage_failure`'s categories.

## Model and effort pick

Per ticket and stage, the model picks tier (`cheap` or `plan`, from `yoke models`) and effort (`low`, `medium`, `high`) from the ticket's complexity and risk. The agent CLI stays fixed by `routing`. A retry after failure bumps tier or effort one step. Explicit `routing.<type>.overrides` still win.

## Skill pick

Candidates come from installed skills (`~/.claude/skills`, plugin skills, repo `.claude/skills`): name plus description, filtered by a config allowlist. Per ticket and stage there is one `noul` relevance question per candidate. The top 0-3 at or above threshold are named in the prompt.

Applies to claude and agy. `agy --help` shows skill expansion is on in print mode, and `agy plugin install` loads plugin skills. Verify with a real agy call. If agy cannot invoke them, inline the skill text into its prompt.

## Browser testing

- A new `e2e` QA stage for web tickets drives a browser through Playwright. It is an optional extra (`pip install yoke[e2e]`); core stays stdlib. Without Playwright the stage is skipped with an event.
- Picker: each step prunes the accessibility tree to ~20 candidate elements, and the model picks the next action (`choice`).
- Verdict: after each step, a `noul` question asks "does the page show the expected outcome?" against the exit criterion.
- If any step has low confidence, that step goes to a claude agent with a screenshot.
- The model never types secrets. Test credentials come from project fixtures, as in the safety rules.

## Open items

- Calibrate `noul` thresholds. M0 spike failures read 0.28-0.44, and `RETRYABLE_MIN` is uncalibrated.
- Measure hook latency per tool call on `tev1:4b`. Decide whether `nimble-v3:9b` is worth it for the safety guard.
- Verify that agy invokes skills headless.
- Define the e2e stage's routing (which ticket types; how web tickets are detected).
