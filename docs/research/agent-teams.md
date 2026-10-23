# Research: Claude Code agent teams and what yoke can borrow

Status: research report, no code. Draft milestones M16-M21 are proposals for the maintainer to evaluate; `docs/PLAN.md` is untouched. Research date: 2026-10-19.

## Contents

- [1. Summary and top recommendations](#1-summary-and-top-recommendations)
- [2. Scope decisions and assumptions](#2-scope-decisions-and-assumptions)
- [3. How agent teams work](#3-how-agent-teams-work)
- [4. How people use them (community material)](#4-how-people-use-them-community-material)
- [5. Comparison with yoke](#5-comparison-with-yoke)
- [6. Ideas, ranked](#6-ideas-ranked)
- [7. High-impact / high-risk ideas](#7-high-impact--high-risk-ideas)
- [8. Rejected ideas](#8-rejected-ideas)
- [9. Draft milestone entries](#9-draft-milestone-entries)
- [10. Sources](#10-sources)

## 1. Summary and top recommendations

Agent teams (peer teammates, a shared task list, a mailbox) solve a problem yoke deliberately does not have: yoke's central runner already owns scheduling, claiming, dependency tracking, retries, isolation (one worktree per ticket) and the close gate. Most of the team machinery would duplicate or weaken what the runner does. The valuable ideas are the ones that sit around that machinery: quality-gate hooks, parallel adversarial review, competing-hypothesis debugging, cost awareness, file-ownership discipline, and a *runner-mediated* way for agents to hand information to each other.

Top recommendations, ranked with the maintainer's stated priority order (reliability, then speed, then cost, then autonomy; see section 2). Milestone numbers are labels, not priority order.

| # | Idea | Why | Draft |
|---|---|---|---|
| 1 | Quota/session-limit pause and resume | The maintainer's agents may stop when session limits are hit and can simply wait for the refresh. Yoke has no handling for this: a limit stop surfaces as a failed call, burns an attempt and can escalate a healthy ticket (no quota or rate-limit handling found in `yoke/*.py`, prompts, PLAN or README). Community reports quota exhaustion within minutes of using teams [7][8] | M22 |
| 2 | User-defined gate hooks (`TaskCompleted` / `TeammateIdle` analog) | Official feature, tiny effort, fits the close gate and retry-with-feedback loop exactly | M17 |
| 3 | Upstream handoff notes plus ownership-aware scheduling | Safe form of "teammates message each other"; removes avoidable rebase conflicts (speed) | M18 |
| 4 | Multi-lens parallel review | Official "parallel code review" use case; avoids single-reviewer anchoring; extra calls per ticket are accepted | M19 |
| 5 | Competing-hypothesis triage for stuck tickets | Official "competing hypotheses" use case; targets tickets that burn all attempts | M20 |
| 6 | Usage ledger and optional token/cost budget | Cost is third priority, but visibility is cheap and also feeds M22 | M16 |
| 7 | `yoke nudge` (steer a running ticket) | Small, mediated version of "message a teammate directly" | M21 |

Peer messaging, self-claiming workers, a persistent lead-less mesh, and auto-approved teammate plans are flagged high-impact / high-risk in section 6 and not drafted as milestones.

## 2. Scope decisions and assumptions

Grill-me session held (an earlier draft of this report wrongly skipped it). Three questions, one at a time, each with a recommended answer. The maintainer's answers:

1. **Priority when proposals conflict.** Recommended: reliability, then cost, then speed, then autonomy. Answer: *reliability first, speed second over cost; agents can stop when session limits are reached and wait until they refresh.* Resulting order: reliability and auditability, speed, cost, autonomy. Consequence: cost caps matter less than not wasting attempts when a quota stops an agent, so the ranking and the new M22 (quota pause and resume) follow from this answer.
2. **Which standing constraints a flagged proposal may relax.** Recommended: only the "no cost cap" rule. Answer: *cost/quota limits, the runner running user-configured commands, and extra agent calls per ticket* are all acceptable if clearly flagged. Consequence: M16 and M22 (quota handling), M17 (gate hooks) and M19/M20 (fan-out) may relax those rules; every other standing constraint stays.
3. **Agent CLIs beyond claude and agy.** Recommended and answered: *none*. Proposals stay adapter-agnostic but need no third CLI.

Two further notes: the experimental state of agent teams is taken as of the docs fetched on 2026-10-19; and the settled decisions in the task (sourcing, ranking, architecture-challenging ideas flagged, output location) were not re-asked.

## 3. How agent teams work

Source: official docs unless marked otherwise.

**Status.** Experimental, off by default; enabled with `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`. Teams are only spawned in interactive sessions; with `-p` (non-interactive) and in Agent SDK sessions Claude does not spawn teammates. This matters for yoke: every yoke agent call is `claude -p`, so agent teams cannot be used inside a yoke stage even if we wanted to. Yoke can only borrow the *ideas*. [1]

**Components.** A team lead (the main session, fixed for the session's lifetime), teammates (separate Claude Code instances, each with its own context window), a shared task list, and a mailbox. Mailboxes are JSON files at `~/.claude/teams/{team}/inboxes/{agent}.json`; team config at `~/.claude/teams/{team}/config.json` (removed at session end); tasks at `~/.claude/tasks/{team}/` (kept). One team per session, no nested teams. [1]

**Task coordination.** Tasks are `pending`, `in progress`, `completed`, with dependencies; a task with unresolved dependencies cannot be claimed. The lead can assign, or a teammate self-claims the next unassigned, unblocked task. Claiming uses file locking to avoid races. [1]

**Messaging.** Teammates message each other by name (one message per recipient); idle teammates notify the lead with their final answer. Messages carry a provenance notice, so a teammate cannot approve a permission prompt on the user's behalf or relay a denied action to bypass a check. In auto mode a classifier reviews each inter-agent message. [1]

**Context.** A teammate loads CLAUDE.md, MCP servers and skills but not the lead's conversation; everything else must be in the spawn prompt. [1]

**Plan approval.** A teammate spawned while the lead is in plan mode works read-only until it sends a plan; Claude Code approves it in the lead's session as soon as it arrives *without the lead reviewing it*. Edits still go through permission prompts. [1]

**Hooks (quality gates).** `TeammateIdle` (exit 2 keeps the teammate working with feedback), `TaskCreated` (exit 2 rolls back creation), `TaskCompleted` (exit 2 blocks completion). [1][2]

**Models and permissions.** Model is chosen per teammate (spawn prompt, then subagent definition, then env, then lead's model). Permissions are inherited from the lead at spawn, including `--dangerously-skip-permissions`; per-teammate modes cannot be set at spawn. Permission prompts from teammates bubble up to the lead. [1]

**Reusable roles.** Subagent definitions (tools, model, effort, body) can be referenced when spawning a teammate. [1]

**Known limits (official).** No session resumption for in-process teammates; task status can lag (teammates forget to mark done, blocking dependents); slow shutdown; the lead may declare the team finished early or start implementing itself; two teammates editing one file overwrites; split panes need tmux or iTerm2. [1]

**Cost (official).** The costs page has two relevant passages. "Agent team token costs" says usage scales with the number of active teammates and how long each runs; mitigations are Sonnet for teammates, small teams, focused spawn prompts, and shutting teammates down. A separate "Manage agent team costs" passage states agent teams use "approximately 7x more tokens than standard sessions when teammates run in plan mode". The docs say start with 3-5 teammates and 5-6 tasks per teammate. The costs page also documents a `--max-budget-usd` flag (confirmed in `claude --help` on this machine). [1][3]
Separately, Anthropic's multi-agent research-system post (a different product) reports agents use about 4x the tokens of chat and multi-agent systems about 15x. [9]

### The related Anthropic engineering experiment

Carlini's C-compiler post (Anthropic engineering blog, Feb 2026) describes a *different* shape that people also call "agent teams": 16 agents in Docker containers, each in a `while true` loop running `claude -p` with a prompt file, no orchestrator, tasks claimed by committing a lock file under `current_tasks/` to a shared bare git repo, merge conflicts resolved by the agents. About 2,000 sessions and roughly $20,000. Lessons stated by the author: invest in near-perfect verifiers, keep test output short and grep-friendly (`ERROR` plus reason on one line), print sampled progress because the model has no sense of time, give each agent a progress/README file, make parallelism easy by splitting work along independent failures (and use a known-good oracle when the task is one big monolith), and run specialized roles (dedupe, performance, docs, design critic). The author also warns that passing tests can mislead and calls the runs "uneasy" without human verification. [4]

## 4. How people use them (community material)

Community claims, not verified by us unless noted. Sources: [5]-[8] (two articles, one personal field report, and Hacker News threads and comments). Coverage is still limited: no Reddit, X or Discord threads were read, and many HN comments are one-liners.

**What works, as reported.**
- Parallel multi-lens PR review (security / performance / test coverage), competing-hypothesis debugging, and cross-layer features split by directory ownership. These match the official examples, so they repeat rather than independently confirm them. [5][6]
- Production incident investigation: one engineer gave two unstructured prompts and four teammates (infrastructure metrics, error tracking, recent code changes, Slack context) found a root cause in about 10 minutes versus an estimated 30-45 solo. Cross-agent messages did redirect the search (a teammate relayed "login failures" to the infrastructure agent, which narrowed to auth services). MCP integrations were the prerequisite. [7]
- Keeping the main thread's context clean: heavy reading happens in teammates and only summaries return; several commenters call this the main benefit, and say they were already doing it with subagents. [8]
- An adversarial `/red-team` skill that uses a team to criticise, grade and re-rank its own output, reported to raise quality (single anecdote). [8]
- Mixed models: Opus as orchestrator/architect/reviewer and Sonnet for investigation and coding. [8]

**Pitfalls and complaints.**
- Cost and quota: one commenter reports finishing the Claude token quota in about 3 minutes, "largely over delegation and result messages instead of code writing"; another wonders if a $20/month plan survives 10 minutes. The incident write-up measured roughly 4x the tokens of a single session (their estimate: $8-10 versus $2-3 in API-equivalent credits). [7][8]
- File conflicts and quality: commenters note teams rely on file locking and need file-disjoint tasks; third-party tools replace it with a git worktree per agent. One commenter does not trust agents on large tasks and prefers guiding design personally. [8]
- Observability: a Show HN author found Claude Code hooks are blocking, so many hook plugins degrade performance; background (fire-and-forget) hooks helped. [8]
- Official-limitation echoes: vague spawn prompts, the lead doing the work itself, stale task states, lost teammates after resume, idle teammates still billing, permission prompts, drift when unattended. [5][6]
- Speed claims are anecdotal: 19 versus 45 minutes, roughly half the time, 10 versus 30-45 minutes; all n=1. bestagent.dev states no rigorous public coding benchmark exists. [5][6][7]

**Cost multipliers: what is official and what is community.**
- Official, Claude Code costs doc: agent teams use "approximately 7x more tokens than standard sessions when teammates run in plan mode"; otherwise usage scales with team size. [3]
- Official, but a different product: Anthropic's multi-agent research system reports about 4x tokens for agents and 15x for multi-agent systems versus chat. This is not a Claude Code team measurement. [9]
- Community: "3-4x" for a 3-agent team (aibuilderclub, bestagent.dev calls it a third-party rule of thumb); about 4x measured by one incident write-up. [5][6][7]
- Our earlier draft claimed the 7x figure was not in the costs doc; that was wrong, it sits in the "Manage agent team costs" passage. The multipliers are best read as order-of-magnitude, with the official 7x as the documented figure for plan-mode teams.

**Common advice.** Subagents cover most parallel work; teams pay off when workers must share findings or exceed one context window. [1][5][6][8]

## 5. Comparison with yoke

Yoke facts below were checked against code in this repo (file references in parentheses).

| Concept | Agent teams | Yoke today | Gap or borrowable idea |
|---|---|---|---|
| Coordinator | Lead session, LLM-driven, can finish early or do work itself | Deterministic runner; the chat session only calls `yoke wait` (`scheduler.py`, PLAN 2.1) | Yoke is stricter and more reliable; nothing to borrow |
| Task list | Shared list, 3 states, dependencies | `tickets.json` (hash-approved) plus per-ticket `state.json`; `depends_on`; milestone barriers (`scheduler._loop`) | Yoke is richer (milestone gates, fix tickets) |
| Task claiming | Self-claim with file lock | Runner picks ready tickets and submits to a `ThreadPoolExecutor`; no agent chooses | Self-claim rejected (section 6) |
| Messaging | Mailbox, any-to-any | None between agents; context moves through inlined prompt JSON and per-attempt `feedback.md` (`pipeline._call`). `results` is per attempt, so a dependent ticket does not see upstream tickets' summaries | Mediated handoff notes (M18); nudge (M21) |
| Parallelism | Teammates in parallel, ~3-5 | `--parallel N`, default 1, one worktree per ticket (`gitops.py`) | Ownership-aware scheduling (M18); `files_hint` is advisory only today (used for issue bodies and size warnings in `tickets.py`/`tracker.py`, not for scheduling) |
| Isolation | Shared checkout unless worktree isolation is added | Worktree and branch per ticket, rebase then ff-merge under a lock, conflict re-applied with `cherry-pick -n` | Yoke ahead |
| Planning role | Optional plan mode; approval is automatic and unreviewed | Plan review by secondary, primary arbitrates, user approves plan and tickets, hash-bound (PLAN 2.3, M2) | Yoke ahead; a per-ticket plan stage is optional (M-none, see rejected) |
| Review | Multi-lens parallel reviewers | One review agent (opposite of coder), up to `max_review_rounds` revision loops (`_review_loop`) | Multi-lens review (M19) |
| Debugging | Competing hypotheses with debate | Retry with feedback, router `triage_failure` hint exists but is not wired into feedback (M4 note g; still true: `_attempt` builds feedback only from QA, validator, rerun tail) | Hypothesis fan-out (M20); wiring triage is cheap |
| Quality gates | `TeammateIdle`, `TaskCreated`, `TaskCompleted` hooks, exit 2 blocks | Hard-coded close gate: coder done, QA pass, runner test rerun, validator approve, docs (`_gate`, `_close`) | User-extensible gates (M17) |
| Hooks in general | Many hook events | Plugin `SessionStart` hook only (`hooks/hooks.json`) | Covered by M17 |
| Observability | Agent panel, transcripts, `Ctrl+T` task list | `events.jsonl`, `status.json`, audit dir per call, `yoke map`, read-only dashboard (M15) | Yoke ahead on audit; lacks a cost view |
| Cost control | Prose advice (Sonnet, small team, shut down) | Per-stage model tiers (M13), `max_agent_calls_total`, `max_attempts_per_ticket`; `meta.json` holds `observed.usage` and `cost` (`calls._exec`) but nothing sums them (no reference in `state.py`, `runcmds.py`, `dashboard.py`) | Ledger and budget (M16) |
| Recovery | No resume for in-process teammates | Resumable from disk, orphaned workers killed, stage dirs set aside as `.interrupted` (`scheduler.recover`) | Yoke ahead |
| Reusable roles | Subagent definitions | Role map in config plus `prompts/<stage>.md` | Equivalent; nothing to borrow |
| Permissions | Inherit lead's, including skip-permissions | Per-stage read/write modes, allowlists, never skip-permissions (`adapters.py`) | Yoke ahead |

### Where docs and code disagree

1. PLAN 2.7 gives ticket ids as `^T-\d{3}$` or `^F-\d{3}$`; `schemas/ticket.json` allows `^[TF]-\d{3,}$` (M14 deviation note explains the 4-digit ids; the 2.7 text was not updated).
2. PLAN 2.16 claims to be the "final" CLI surface but omits `setup`, `runs`, `search`, `research`, `factcheck` (added by M10-M13 per their notes and present in `cli.py` / `runcmds.py` / `research.py`).
3. PLAN 2.11 and 2.15 describe the SessionStart hook as `yoke status --brief --active`; `hooks/hooks.json` now also runs `yoke setup --check` first.
4. PLAN 2.8 pseudocode omits `_call_retry` (one in-place stage retry after a failed call, `pipeline.py`) and says recovery restarts "the current stage"; M9 notes already record that recovery restarts the whole attempt.
5. PLAN 2.12 says usage and cost are recorded; they are recorded per call in `meta.json` but never aggregated or surfaced (also true of the dashboard).
6. Not verified: whether `agy` reports usable `usage`/cost. `AgyAdapter.parse` stores `usage` and sets `observed_model="unreported"`, cost is not set; M16 therefore cannot assume a dollar figure for agy calls.

## 6. Ideas, ranked

Fit = how well it matches yoke's central-runner design and standing constraints. Effort: S (under 1 day), M (1-3 days), L (more). Benefit is expected value to yoke users.

| Rank | Idea | Fit | Effort | Benefit | Source of idea | Risk to design |
|---|---|---|---|---|---|---|
| 1 | Quota/session-limit pause and resume | High | M | High (reliability: stops wasted attempts and false escalations) | Maintainer answer, community quota complaints | Relaxes PLAN section 5 limits stance; limit signals from agy are unverified |
| 2 | Gate hooks (user commands at stage end / close) | High | S | High | Official hooks | Runs user commands; trust boundary |
| 3 | Upstream handoff notes (runner-mediated "mailbox") | High | M | Med-High | Mailbox, interface-change anecdote | Schema change to code output |
| 4 | Ownership-aware scheduling from `files_hint` | High | S | Med (fewer rebase conflicts) | "Avoid file conflicts" | None; advisory data may be wrong |
| 5 | Multi-lens parallel review | Med-High | M | Med | Official use case | Cost up per ticket |
| 6 | Competing-hypothesis triage for stuck tickets | Med | M | Med | Official use case | Cost spike on already failing tickets |
| 7 | Usage ledger plus optional budget (cost is third priority) | High | S-M | Med (visibility; feeds quota pause) | Cost docs, community | Relaxes PLAN section 5 |
| 8 | `yoke nudge` steering | Med | S | Low-Med | Talk to teammates directly | Changes input after approval; audit |
| 9 | Wire router `triage_failure` hint into retry feedback | High | S | Low-Med | Gap found while comparing | Router confidence is uncalibrated (M3 note) |
| 10 | Progress/handoff file per ticket for long tickets | Med | S | Low | C-compiler post | Overlaps M18 |
| 11 | Peer messaging between agents | Low | L | Unknown | Mailbox | **High-impact, high-risk** |
| 12 | Self-claiming workers / pull queue | Low | L | Low (runner already schedules) | Task list | **High-impact, high-risk** |
| 13 | Lead-less mesh with git lock files | Very low | L | Unknown | C-compiler post | **High-impact, high-risk** |
| 14 | Auto-approved teammate plans | Very low | S | Negative | Plan approval doc | **High-impact, high-risk** |

Ranking follows the maintainer's order (reliability, speed, cost, autonomy). Ideas 9 and 10 (triage hint wiring, per-ticket progress file) are small enough to fold into M20 and M18 rather than get their own milestone.

## 7. High-impact / high-risk ideas

These challenge yoke's core design. Each is flagged and not drafted as a milestone. If the maintainer wants one, it should be a separate experiment on a branch.

### 7.1 Free peer messaging between agents
What it is: any stage agent can write to another agent's inbox and read replies mid-task.
Impact: could let a coder and a QA agent converge without a retry round, and could enable debate-style review.
What it puts at risk:
- **Close gate and audit trail.** A conclusion reached in an unaudited exchange is not in `result.json` or `meta.json`; yoke's guarantee that every decision traces to a schema-validated call output (CLAUDE.md "always read `structured_output`") breaks unless every message is logged and replayable.
- **Central runner.** The runner can no longer predict call counts, so `max_agent_calls_total` and timeouts (`calls._exec`) stop bounding the work.
- **Headless agy.** agy runs headless without a shell and cannot poll a mailbox mid-call; messaging would require a runner-owned relay between calls, which is effectively M18.
- **Prompt injection.** Official docs add provenance tagging and a classifier because inter-agent text is untrusted [1]; yoke has no equivalent.
- **Plain `claude -p` cannot do it.** Teams are not available in non-interactive mode [1].
Safer variant: M18 (runner relays structured notes between calls).

### 7.2 Self-claiming workers
What it is: workers loop, pick the next unblocked ticket themselves.
Impact: removes the scheduler as a bottleneck; Anthropic's compiler run did this.
Risks: loses milestone barriers and fix-ticket generation (both live in `scheduler._loop`), lets an agent choose its own work outside approved `tickets.json` (approval hash is meaningless if agents may create tasks, cf. `TaskCreated` hook), and loses deterministic resume. The runner already parallelizes through a pool, so the benefit is small. Verdict: do not pursue.

### 7.3 Lead-less mesh with git lock files
What it is: Carlini's pattern. Risks: no per-ticket close gate, rebase-as-integration with agent-resolved conflicts, pushes from workers (yoke forbids worker push, PLAN 2.9; the compiler harness also used `--dangerously-skip-permissions` in containers, which yoke bans), and $20k-scale spend without a budget. Useful only as inspiration for verifiers (M17) and parallel-by-independent-failure decomposition (M20).

### 7.4 Auto-approved teammate plans
What it is: the official behaviour where the lead approves a teammate's plan without review [1]. Yoke's validator-reviewed, hash-bound approvals are the stronger design; copying this would downgrade it. Do not adopt. (An optional, *reviewed* per-ticket plan stage is listed under rejected ideas.)

### 7.5 Persistent long-lived teammate sessions
Teams keep teammates running between tasks. Yoke resumes coder sessions by ID but otherwise starts clean calls. Persistent sessions would raise token cost through context growth, complicate crash recovery (official limitation: no resume [1]) and make a stage result depend on hidden session history. Not recommended; the existing resume-by-ID retry is the right middle ground.

## 8. Rejected ideas

| Idea | Why rejected |
|---|---|
| Use real agent teams inside a yoke stage | Impossible: yoke calls `claude -p`, and teams do not spawn in non-interactive mode [1]; agy has no equivalent |
| tmux/iTerm split-pane display | Yoke runs detached with no TTY; the dashboard (M15) already covers visibility |
| Subagent definitions as role files | Role map plus `prompts/*.md` already cover this; extra format adds a second source of truth |
| Team size heuristics (3-5) | `--parallel N` exists; the real lever is ticket independence, covered by M18 |
| Per-ticket plan-then-implement stage | Possible, but the plan stage already reviews the whole plan and tickets carry `plan_excerpt` and exit criteria; adds a call per ticket for modest gain. Revisit if M20 shows many tickets failing for design reasons |
| Debate between agents via free messaging | Replaced by runner-mediated rounds in M20 |
| Per-teammate permission modes | Yoke already sets modes per stage and bans skip-permissions |
| Claude Tag / Dynamic Workflows mentions in community posts | Out of scope (other products) |

## 9. Draft milestone entries

Format follows PLAN section 3. Checkboxes are `[ ]` because none is built. Each proposal respects the standing constraints unless a note says otherwise. Per CLAUDE.md, any of these that changes behaviour would bump the version in `.claude-plugin/plugin.json` and `pyproject.toml` together; this report does not.

<a id="m16"></a>
### M16: Usage ledger and optional budget `[ ]`
Goal: make cost visible and, when the user opts in, bounded.
Files: `yoke/usage.py` (new), `yoke/calls.py`, `yoke/scheduler.py`, `yoke/runcmds.py`, `yoke/dashboard.py`, `yoke/dashboard.html`, `yoke/defaults.json`, `tests/test_usage.py`.
- `usage.summarize(run)` walks `meta.json` files (including `reask/`, `fallback/`, `.interrupted`) and sums `observed.usage` token fields and `observed.cost` by stage, ticket, agent and model; writes `usage.json` at each ticket close and at run end.
- `yoke status` and the dashboard show totals and the top 3 spenders. Missing cost (agy reports none that we verified) shows as "unreported", never as zero.
- Config `budget`: `{"max_cost_usd": null, "max_tokens": null}`. Default null keeps today's behaviour. When set and exceeded by recorded claude cost or tokens, the runner stops scheduling, lets in-flight calls finish and opens one budget escalation, mirroring `max_agent_calls_total` (PLAN 2.12).
- Exit criteria: summary matches hand-computed totals over a fixture run with retries and re-asks; null budget changes no existing test; a budget of 1 token escalates once with a clear message; resume after escalation does not re-escalate; dashboard shows totals; agy-only run shows "unreported" for cost.
- Notes: cost is the maintainer's third priority, so this is mainly visibility; it feeds M22. **Relaxes PLAN section 5 ("no token/cost caps")**, and the `max_agent_calls_total` overshoot caveat (M5 note b) applies equally. Only claude reports `total_cost_usd`; agy cost is not verified, so a dollar cap covers claude calls only. `claude -p` has a native `--max-budget-usd` flag (present in `claude --help` here and described in the costs doc) that an adapter could pass per call as a hard per-call stop; whether it fits a multi-call ticket budget is unverified. Needs a PLAN section 2.12 and section 5 update if adopted.

<a id="m17"></a>
### M17: Gate hooks `[ ]`
Goal: let the user add checks to the close gate without editing yoke, the way `TaskCompleted` and `TeammateIdle` hooks do for teams.
Files: `yoke/hooks.py` (new), `yoke/pipeline.py`, `yoke/config.py`, `yoke/defaults.json`, `yoke/schemas/` (none needed), `tests/test_gate_hooks.py`, `README.md`.
- Config `gate_hooks`: `{"after_stage": {"code": [cmd...], "qa": [...]}, "before_close": [cmd...]}`. Commands run by the runner in the ticket worktree with env `YOKE_TICKET`, `YOKE_STAGE`, `YOKE_ATTEMPT`, via the same runner-side mechanism as `_rerun`, but with process-group kill on timeout.
- Exit 0 passes. Exit 2 fails the attempt with the command's stdout/stderr tail as feedback (same path as a failing test rerun, so retries and escalation work unchanged). Any other non-zero logs `gate_hook_error` and is treated as a failure, never as a pass. Results are recorded as events and in the `close_gate` event under `gate`.
- Exit criteria: failing `before_close` hook prevents the commit and feeds the coder; hook timeout kills the group; passing hooks leave the existing close-gate tests unchanged; hook output appears in audit under the attempt dir; hooks defined only in repo `.yoke.json` are refused unless the user config or the run snapshot marks them trusted.
- Notes: a hook is arbitrary command execution. `.yoke.json` lives in the target repo, so an untrusted repo could ship one; the approval hashes cover only `plan.md` and `tickets.json`, not config. Recommended fix within the constraints: accept hooks only from `~/.yoke/config.json`, or record them in `run.json` at `init` and show them at `approve tickets`. This is a conflict to settle before implementation. Stdlib only; no agent involved, so nothing here touches `structured_output`.

<a id="m18"></a>
### M18: Upstream handoff notes and ownership-aware scheduling `[ ]`
Goal: give dependent tickets the information a teammate would have received by message, and stop tickets that touch the same files from running together, without peer messaging.
Files: `yoke/schemas/code.json`, `yoke/prompts/{code,qa,validate}.md`, `yoke/pipeline.py`, `yoke/scheduler.py`, `yoke/milestones.py`, `tests/test_handoff.py`, `tests/test_parallel.py`.
- `code` result gains optional `handoff` (strings: interface changes, new files, gotchas). On close the runner stores it in `tickets/T/handoff.json` (run dir, outside the repo, atomic write).
- Prompt variable `$upstream` inlines the handoff notes of the ticket's closed `depends_on` ancestors (capped) into code, qa and validate prompts. It is data, labelled as coming from another agent and not as instructions.
- Scheduler: when `parallel > 1`, a ready ticket is deferred if its `files_hint` overlaps (path or directory prefix) a currently running ticket's; empty hints never block. Deferral is logged as `ticket_deferred`.
- Exit criteria: a dependent ticket's prompt contains its upstream notes; notes absent when none; two overlapping tickets never run concurrently with `parallel=2` while disjoint ones do; empty `files_hint` behaves as today; schema stays backward compatible (optional field); `tickets.json` and approval hashes are unchanged.
- Notes: `files_hint` is advisory today (used for issue bodies and size warnings only), so the overlap rule is a heuristic, and it only reduces conflicts, the rebase path (M5) remains the backstop. Handoff text is agent-written and untrusted input to the next agent; keep it out of any decision logic. This is the mediated, audited version of the mailbox, see section 7.1. Folds in idea 9 (a progress note per ticket) if the maintainer wants it.

<a id="m19"></a>
### M19: Multi-lens parallel review `[ ]`
Goal: replace the single review call with independent lenses, for tickets where that is worth the cost.
Files: `yoke/prompts/review_<lens>.md`, `yoke/pipeline.py` (`_review_loop`), `yoke/config.py`, `yoke/defaults.json`, `tests/test_review.py`.
- Config `review_lenses`: default empty (today's single review). When set, e.g. `["security", "performance", "tests"]`, each round runs one read-only call per lens using the existing `review` schema; the runner merges findings, dedupes by file and line, and applies the existing severity rule (blocker/major block, minor/nit advisory) and `max_review_rounds`.
- Lens calls run sequentially at `parallel=1` and concurrently otherwise, with distinct audit labels (`review-security`, ...). Each counts against `max_agent_calls_total`.
- Exit criteria: empty config is byte-for-byte today's behaviour; two lenses producing the same finding yield one feedback item; a blocker from any lens triggers a coder revision; unresolved findings still flow to qa/validate/PR as in M11; call count equals rounds times lenses.
- Notes: cost scales with the number of lenses (and with ticket count), so default off and use the ticket type or a label to enable. Lens prompts must not see each other's output, which is the whole point (avoids anchoring); the runner merges. This matches the official parallel-review case [1] and uses no peer messaging.

<a id="m20"></a>
### M20: Competing-hypothesis triage for stuck tickets `[ ]`
Goal: when a ticket has failed twice with the same kind of failure, diagnose before spending the last attempt.
Files: `yoke/prompts/hypotheses.md`, `yoke/schemas/hypotheses.json`, `yoke/pipeline.py`, `yoke/router.py`, `yoke/config.py`, `tests/test_hypotheses.py`.
- Trigger: attempt number equals `max_attempts_per_ticket` and feedback from the last two attempts match (same failing test names or same validator issues, compared by the runner). Router `triage_failure` runs first; its hint (category, retryable) is appended to feedback, which also wires up the unused M4 note (g) hint.
- Optional fan-out (config `hypotheses: N`, default 0): N read-only calls each given one assigned hypothesis seed (different category from a fixed list: spec gap, environment, test bug, code bug) and the failure evidence; each returns a schema-validated `{hypothesis, evidence, fix_plan, confidence}`. A final call by the primary ranks them. The winning plan goes into the next attempt's feedback. No agent sees another's output until the ranking call, and all text is shown to the coder as feedback, not as instruction hierarchy.
- Exit criteria: no change when `hypotheses` is 0 except the triage hint; fan-out calls are audited under the attempt dir and counted against the call budget; runner never relies on `confidence` alone to decide pass or fail; a ticket that still fails escalates exactly as today.
- Notes: the official debate pattern has teammates challenge each other; here the challenge is done by the ranking call, keeping rounds deterministic. Router confidence is uncalibrated (M3 note c), so the hint is advisory. Cost spikes only on tickets that are already failing.

<a id="m21"></a>
### M21: `yoke nudge` `[ ]`
Goal: let the user steer a running or pending ticket without a full block-and-unblock cycle, the small safe analogue of messaging a teammate.
Files: `yoke/runcmds.py`, `yoke/pipeline.py`, `skills/status/SKILL.md`, `tests/test_nudge.py`.
- `yoke nudge T-xxx --note TEXT [--run R]` appends `User note: ...` to the ticket's next attempt feedback and emits a `nudge` event; it never interrupts a live call. It is read at the next attempt boundary (and between review rounds).
- Exit criteria: note appears in the next code prompt exactly once; works on pending and in-progress tickets and is refused on closed ones; no change to `tickets.json`, so approvals stay valid; event and note are in the audit trail.
- Notes: `unblock --note` already does this for blocked tickets (`scheduler.unblock`); this extends it to non-blocked ones. The note is user-authored, so it is trusted input, unlike M18 handoffs. A user can already change feedback files by hand; the command makes it audited.

<a id="m22"></a>
### M22: Quota and session-limit pause and resume `[ ]`
Goal: when an agent CLI stops because a session, usage or rate limit was reached, wait for the limit to refresh and continue, instead of failing the call, burning an attempt and escalating a healthy ticket.
Files: `yoke/limits.py` (new), `yoke/calls.py`, `yoke/pipeline.py`, `yoke/scheduler.py`, `yoke/adapters.py`, `yoke/config.py`, `yoke/defaults.json`, `yoke/runcmds.py`, `tests/test_limits.py`, `tests/fake_cli.py`.
- Detection: each adapter's `parse` classifies a limit stop (`CallResult.limit = {"reset_at": iso or None, "raw": text}`), recognised from the CLI's output and exit status. **The exact limit messages of `claude -p` and `agy -p` are not verified in this research and must be captured first in a spike (like M0), by forcing a limit or reading real logs; until then the patterns are config-driven (`limits.patterns`) so they can be fixed without a code release.** A call that matches a limit pattern is never counted as an attempt failure or a schema failure.
- Behaviour: the runner records event `limit_hit`, marks the run `waiting_limit` with `resume_at` in `run.json` (parsed reset time, else exponential backoff capped by `limits.max_wait_s`), stops scheduling new calls, lets in-flight calls finish, sleeps in short slices (so `yoke stop` and SIGTERM still work), then re-runs the *same call* with the same attempt number and the saved session id, audited under a `limit-retry` label. Budget `agent_calls` is not charged for the aborted call.
- `yoke status`, `yoke wait` and the dashboard show "waiting for limit until <time>". `yoke wait` does not return for this state (it is not an escalation). After `limits.max_waits` consecutive waits for one call, or when no reset time can be found and the cap is hit, the runner escalates once with the raw message.
- Crash recovery: a runner restart during a wait reads `resume_at` from `run.json` and continues waiting.
- Exit criteria: a fake-CLI limit response does not increment `attempt` or consume a stage retry; the same call is re-issued after the wait with the same session id; `waiting_limit` state is visible in status, wait and dashboard; killing and resuming the runner mid-wait keeps the wait; a fake CLI that always reports a limit escalates after `max_waits` with the raw text; non-limit failures behave exactly as today; no new network or non-stdlib dependency.
- Notes: reliability is the maintainer's first priority, and this is the gap their answer exposed: nothing in `yoke/*.py`, prompts, PLAN or README handles quotas. **Relaxes the implicit "agent calls never wait" behaviour and PLAN section 5 limits stance.** Cost interaction: waiting costs no tokens, and M16 totals can be shown while waiting. With `--parallel N`, all workers must pause together; a limit seen by one pauses the pool. A limit on one agent (claude) does not necessarily pause the other (agy); first version pauses the whole run for simplicity. Risk: misclassifying a real failure as a limit would loop; the `max_waits` cap and the config-driven patterns bound that.

## 10. Sources

Official (primary):
1. Claude Code docs, "Orchestrate teams of Claude Code sessions": https://code.claude.com/docs/en/agent-teams (fetched 2026-10-19)
2. Claude Code docs, "Hooks reference" (`TeammateIdle`, `TaskCreated`, `TaskCompleted` exit-code behaviour table): https://code.claude.com/docs/en/hooks (fetched 2026-10-19; I read the exit-code table only, not the full page)
3. Claude Code docs, "Manage costs effectively", section "Agent team token costs": https://code.claude.com/docs/en/costs (fetched 2026-10-19)
4. Anthropic Engineering, "Building a C compiler with a team of parallel Claudes" (N. Carlini, 2026-02-05): https://www.anthropic.com/engineering/building-c-compiler

9. Anthropic Engineering, "How we built our multi-agent research system": https://www.anthropic.com/engineering/multi-agent-research-system (official; a different product, source of the 4x/15x figures)

Community (claims not independently verified):
5. bestagent.dev, "Claude Code Agent Teams: Are They Worth the Tokens?" (2026-06-09): https://bestagent.dev/claude-code-agent-teams-worth-it-2026/
6. aibuilderclub.com, "Claude Code Agent Teams: Setup, Costs, Workflows (2026)" (2026-06-11): https://www.aibuilderclub.com/blog/claude-code-agent-teams-guide
7. magarcia.io, "Using Claude Code Agent Teams for Incident Investigation" (2026-03-02), a personal field report: https://magarcia.io/using-claude-code-agent-teams-for-incident-investigation/
8. Hacker News discussion of the official docs (item 46902368, 396 points) and other threads that mention agent teams, read via the HN Algolia API (comment search for "agent teams"): https://news.ycombinator.com/item?id=46902368 ; commenters cited include avereveard (quota in 3 minutes), GoatOfAplomb, ottah, ChadMoran (`/red-team`), drzaiusx11 (Opus/Sonnet split), aceelric (worktrees instead of file locks); Show HN "Real-time dashboard for Claude Code agent teams" (simple10), item 47602986. Comment authors are unverified individuals.

Search result listings I saw but did not read: 10xplaybooks.com "17 Lessons ... 144 Million Claude Code Tokens", aifreeapi.com agent teams guide, towardsai "I Spent a Week Learning Claude Code's Agent System". Limits of this research: community coverage is two articles, one field report and HN threads; no Reddit, X or Discord threads were read, and speedup and cost claims rest on single anecdotes.

Repo sources: `CLAUDE.md`, `docs/PLAN.md`, `docs/REQUIREMENTS.md`, `README.md` (headings only), `yoke/{scheduler,pipeline,calls,adapters,defaults.json}`, `hooks/hooks.json`, `yoke/schemas/ticket.json`, and grep checks of `cli.py`, `runcmds.py`, `dashboard.py`, `state.py`, `tickets.py`, `tracker.py`. I did not read `gitops.py`, `router.py`, `chart.py`, `mcp_server.py` or `tracker.py` in full; claims about them come from PLAN notes.
