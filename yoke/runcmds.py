"""CLI handlers for the runner commands: run, status, escalations, unblock, nudge, stop (PLAN 2.16)."""
import glob
import json
import os
import shutil
import signal
import subprocess
import sys
import time

from . import config, gitops, milestones, pipeline, planreview, scheduler, state

CMDS = ("run", "status", "escalations", "unblock", "stop", "wait", "clean", "runs", "map", "dashboard", "nudge")


def add_parsers(sub):
    m = sub.add_parser("map", help="show implementation map, frontier, and decisions log")
    m.add_argument("--run")
    r = sub.add_parser("run", help="run the ticket pipeline")
    r.add_argument("--run")
    r.add_argument("--parallel", type=int, help="tickets in flight at once (saved in the run config)")
    r.add_argument("--detach", action="store_true", help="re-exec in the background (runner.log)")
    r.add_argument("--resume", action="store_true", help="accepted for symmetry; runs always resume from disk")
    s = sub.add_parser("status", help="run summary")
    s.add_argument("--run")
    s.add_argument("--json", action="store_true")
    s.add_argument("--brief", action="store_true", help="at most 15 lines")
    s.add_argument("--active", action="store_true",
                   help="latest unfinished run for this repo; print nothing and exit 0 if none")
    rn = sub.add_parser("runs", help="list runs for this repo")
    rn.add_argument("--active", action="store_true", help="only unfinished runs")
    rn.add_argument("--json", action="store_true", help="output JSON list of summaries")
    e = sub.add_parser("escalations", help="list open escalations")
    e.add_argument("--run")
    u = sub.add_parser("unblock", help="answer an escalation")
    u.add_argument("ticket", nargs="?")
    u.add_argument("--milestone")
    u.add_argument("--waive", metavar="REASON")
    u.add_argument("--run")
    u.add_argument("--note")
    u.add_argument("--skip", action="store_true")
    u.add_argument("--reset-attempts", action="store_true")
    w = sub.add_parser("wait", help="block until escalation, run end, runner death or timeout")
    w.add_argument("--run")
    w.add_argument("--timeout", type=int, default=3000)
    c = sub.add_parser("clean", help="remove the run's worktrees")
    c.add_argument("--run")
    st = sub.add_parser("stop", help="SIGTERM the runner and its workers")
    st.add_argument("--run")
    d = sub.add_parser("dashboard", help="serve local read-only web UI")
    d.add_argument("--port", type=int, default=8765, help="port to bind to (default: 8765)")
    nu = sub.add_parser("nudge", help="send a user note to a ticket for the next attempt or review round")
    nu.add_argument("ticket")
    nu.add_argument("--note", required=True, help="note text to append to feedback")
    nu.add_argument("--run")


def wait_for(run, timeout, poll=2):
    """Block until an escalation opens, the run ends, or the runner dies. -> (reason, message).

    Rate limit pauses (waiting_limit) do not cause wait_for to return; it keeps waiting until
    an escalation opens or the run reaches a terminal state.
    When the run completes, message contains rendered status including usage totals and top spenders.
    """
    end = time.time() + timeout
    while True:
        info = run.read_json("run.json")
        esc = run.read_json("escalations.json", [])
        pid = scheduler.read_lock(run)
        if esc:
            return "escalation", "; ".join("#%d %s: %s" % (e["id"], e["ticket"] or "-", e["reason"]) for e in esc)
        if info["state"] in ("done", "needs_user", "stopped"):
            return info["state"], run.render_status()
        if info["state"] == "running" and not (pid and scheduler._alive(pid)):
            return "died", "runner died; use /yoke:resume (yoke run --resume --detach)"
        if time.time() >= end:
            s = run.status()
            return "timeout", "still running: %d/%d closed" % (s["tickets"].get("closed", 0), s["tickets_total"])
        time.sleep(poll)


def clean(run):
    """Remove the run's worktrees (branches and audit records stay)."""
    pid = scheduler.read_lock(run)
    if pid and scheduler._alive(pid):
        raise scheduler.LockError("runner active (pid %d)" % pid)
    repo = run.read_json("run.json")["repo"]
    for wt in glob.glob(run.path("worktrees/*")):
        gitops.remove_worktree(repo, wt)
    shutil.rmtree(run.path("worktrees"), ignore_errors=True)
    gitops.git(repo, "worktree", "prune", check=False)


def is_active(run):
    """True unless state == 'done' and approvals.json has a 'final' entry."""
    info = run.read_json("run.json", {})
    if info.get("state") == "done" and "final" in run.read_json("approvals.json", {}):
        return False
    return True


def goal_of(run):
    """Read requirements.md; return first non-blank line under '## Goal' (case-insensitive);
    else first non-blank, non-heading line; else '(no requirements.md)'.
    Strip leading '- ' / '* ', collapse whitespace, truncate to 140 chars with '...'."""
    try:
        with open(run.path("requirements.md"), encoding="utf-8", errors="replace") as f:
            content = f.read()
    except (OSError, ValueError):
        return "(no requirements.md)"

    lines = content.splitlines()
    goal_line = None
    in_goal_section = False
    in_list_item = False
    list_lines = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("##") and not stripped.startswith("###") and stripped[2:].strip().lower() == "goal":
            in_goal_section = True
            continue
        if in_goal_section:
            if stripped.startswith("#"):
                in_goal_section = False
            elif stripped:
                if stripped.startswith(("- ", "* ")):
                    if list_lines:
                        break
                    in_list_item = True
                    list_lines.append(stripped[2:].strip())
                elif in_list_item:
                    # Continuation line of list item
                    list_lines.append(stripped)
                else:
                    goal_line = stripped
                    break
            else:
                if in_list_item:
                    # Blank line ends list item
                    break

    if list_lines:
        goal_line = " ".join(list_lines)

    if not goal_line:
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                goal_line = stripped
                break

    if not goal_line:
        return "(no requirements.md)"

    if goal_line.startswith(("- ", "* ")):
        goal_line = goal_line[2:].strip()

    collapsed = " ".join(goal_line.split())
    if len(collapsed) > 140:
        return collapsed[:137] + "..."
    return collapsed


def next_step(run):
    """Deterministic, first match wins:
    1. state done and final approved -> done
    2. state done -> finish
    3. plan.md missing, or plan approval missing/invalid -> plan
    4. tickets approval missing/invalid -> tickets
    5. runner alive -> wait (keep running yoke wait)
    6. otherwise -> resume (restart the runner)
    """
    info = run.read_json("run.json", {})
    st = info.get("state")
    if st == "done":
        try:
            planreview.check_approval(run, "final")
            return "done"
        except planreview.ApprovalError:
            return "finish"

    if not os.path.exists(run.path("plan.md")):
        return "plan"
    try:
        planreview.check_approval(run, "plan")
    except planreview.ApprovalError:
        return "plan"

    try:
        planreview.check_approval(run, "tickets")
    except planreview.ApprovalError:
        return "tickets"

    pid = scheduler.read_lock(run)
    if pid and scheduler._alive(pid):
        return "wait"

    return "resume"


def summary(run):
    """Returns a dict: run_id, state, created_at, goal, tickets_total, tickets_closed, runner_alive, escalations, next."""
    info = run.read_json("run.json", {})
    s = run.status()
    pid = scheduler.read_lock(run)
    alive = bool(pid and scheduler._alive(pid))
    res = {
        "run_id": run.run_id,
        "state": info.get("state", s.get("state")),
        "created_at": info.get("created_at"),
        "goal": goal_of(run),
        "tickets_total": s.get("tickets_total", 0),
        "tickets_closed": s.get("tickets", {}).get("closed", 0),
        "runner_alive": alive,
        "escalations": s.get("escalations", 0),
        "next": next_step(run),
    }
    if s.get("waiting_limit"):
        res["waiting_limit"] = s["waiting_limit"]
    return res


def brief(run):
    """<=15 lines for the SessionStart hook: status plus the next command to run."""
    s = run.status()
    pid = scheduler.read_lock(run)
    alive = bool(pid and scheduler._alive(pid))
    nxt = next_step(run)

    skill_map = {
        "done": "/yoke:finish",
        "finish": "/yoke:finish",
        "plan": "/yoke:plan",
        "tickets": "/yoke:tickets",
        "wait": "/yoke:run",
        "resume": "/yoke:resume",
    }
    skill_cmd = skill_map.get(nxt, "/yoke:%s" % nxt)

    if s["state"] == "done":
        hint = "all tickets closed; next: %s %s" % (skill_cmd, run.run_id)
    elif alive:
        hint = "runner alive (pid %d); next: %s %s" % (pid, skill_cmd, run.run_id)
    else:
        hint = "runner not running; next: %s %s" % (skill_cmd, run.run_id)

    if s["escalations"]:
        hint = "%d open escalation(s): `yoke escalations --run %s`; " % (s["escalations"], run.run_id) + hint

    lines = ["yoke run %s [%s]" % (s["run_id"], s["state"])]
    if s.get("waiting_limit"):
        wl = s["waiting_limit"]
        lines.append("waiting for limit until %s (%s)" % (wl.get("resume_at", "?"), wl.get("ticket") or "run"))
    lines.append("tickets %d, agent calls %d/%s" % (s["tickets_total"], s["agent_calls"], s["max_agent_calls"]))

    from . import usage
    u = s.get("usage") or {}
    totals = u.get("totals") or {}
    cost_disp = usage.format_cost(totals.get("cost"), totals.get("unreported_calls", 0))
    tot_tokens = totals.get("total_tokens")
    if not isinstance(tot_tokens, (int, float)) or isinstance(tot_tokens, bool):
        tot_tokens = sum(v for k, v in totals.items() if "token" in k and isinstance(v, (int, float)) and not isinstance(v, bool))
    in_tokens = totals.get("input_tokens", 0)
    out_tokens = totals.get("output_tokens", 0)
    lines.append("usage totals: cost %s, tokens %d (in %d, out %d)" % (cost_disp, tot_tokens, in_tokens, out_tokens))

    lines.append(hint)
    return "\n".join(lines)


def render_map(run):
    """Render implementation map: destination, out-of-scope, per-milestone gate status,
    exit_check, tickets with status and why pending wait, frontier, and decisions log."""
    dest, _, oos_list = milestones.destination_and_scope(run)

    lines = ["Destination: %s" % dest, "Out of scope:"]
    if oos_list:
        for item in oos_list:
            lines.append("  - %s" % item)
    else:
        lines.append("  (none)")

    all_milestones = milestones.milestones_of(run)
    ms_states = milestones.load_states(run)
    ms_idx = {m["id"]: i for i, m in enumerate(all_milestones)}
    tickets = milestones.merged_tickets(run)
    sts = {t["id"]: run.read_json("tickets/%s/state.json" % t["id"], {}).get("status", "pending") for t in tickets}

    lines.append("\nMilestones:")
    for m in all_milestones:
        mid = m["id"]
        m_title = m.get("title", "")
        gate = ms_states.get(mid, {}).get("status", "open")
        ec = m.get("exit_check")
        ec_str = ec if ec else "none"
        header = "%s: %s [gate: %s, exit_check: %s]" % (mid, m_title, gate, ec_str) if m_title else "%s [gate: %s, exit_check: %s]" % (mid, gate, ec_str)
        lines.append(header)
        m_tickets = [t for t in tickets if t.get("milestone", "MS-00") == mid]
        for t in m_tickets:
            tid = t["id"]
            st = sts.get(tid, "pending")
            dep_str = " (after %s)" % ", ".join(t.get("depends_on", [])) if t.get("depends_on") else ""
            reasons = milestones.ticket_waiting_for(t, all_milestones, ms_states, sts, ms_idx)
            wait_str = " (%s)" % "; ".join(reasons) if reasons else ""
            lines.append("  %s [%s] %s%s%s" % (tid, st, t.get("title", ""), dep_str, wait_str))

    frontier = [
        t for t in tickets
        if sts.get(t["id"]) in ("pending", "in_progress")
        and all(sts.get(d) == "closed" for d in t.get("depends_on", []))
        and all(
            ms_states.get(em["id"], {}).get("status") in ("passed", "waived")
            for em in all_milestones[:ms_idx.get(t.get("milestone", "MS-00"), 0)]
        )
    ]

    lines.append("\nFrontier:")
    if frontier:
        for t in frontier:
            lines.append("  %s: %s" % (t["id"], t.get("title", "")))
    else:
        lines.append("  (none)")

    lines.append("\nDecisions log:")
    map_path = run.path("map.md")
    decisions = ""
    if os.path.exists(map_path):
        try:
            with open(map_path) as f:
                decisions = f.read().strip()
        except OSError:
            pass
    if decisions:
        lines.append(decisions)
    else:
        lines.append("  (none)")

    return "\n".join(lines)


def _active_runs():
    from . import cli
    runs = state.Run.list_for_repo(cli._git_toplevel(os.getcwd()))
    return [r for r in runs if is_active(r)]


def dispatch(args):
    from . import cli
    if args.cmd == "status" and getattr(args, "active", False):  # hook path: never fail, never print when idle
        try:
            active = _active_runs()
            if active:
                # Up to 3 active runs, newest first
                rev = list(reversed(active))
                top = rev[:3]
                lines = []
                for r in top:
                    lines.append(brief(r))
                if len(rev) > 3:
                    lines.append("+%d more: yoke runs --active" % (len(rev) - 3))
                print("\n".join(lines))
        except Exception:
            pass
        return 0
    if args.cmd == "runs":
        repo = cli._git_toplevel(os.getcwd())
        runs = state.Run.list_for_repo(repo)
        if args.active:
            runs = [r for r in runs if is_active(r)]
        if args.json:
            out = []
            for r in runs:
                try:
                    out.append(summary(r))
                except Exception:
                    continue
            print(json.dumps(out, indent=2))
            return 0
        if not runs:
            print("no runs for this repo")
            return 0
        blocks = []
        for r in runs:
            try:
                summ = summary(r)
                runner_str = "runner alive" if summ["runner_alive"] else "runner stopped"
                line1 = "%s  [%s]  tickets %d/%d  %s  next: %s" % (
                    summ["run_id"],
                    summ["state"],
                    summ["tickets_closed"],
                    summ["tickets_total"],
                    runner_str,
                    summ["next"],
                )
                if summ["escalations"]:
                    line1 += "  escalations: %d" % summ["escalations"]
                line2 = "  goal: %s" % summ["goal"]
                blocks.append("%s\n%s" % (line1, line2))
            except Exception:
                continue
        if blocks:
            print("\n".join(blocks))
        else:
            print("no runs for this repo")
        return 0
    if args.cmd == "dashboard":
        from . import dashboard
        return dashboard.serve(config.yoke_home(), args.port)
    run = cli._run(args)
    if args.cmd == "run":
        try:
            planreview.check_approval(run, "tickets")
            if args.parallel:
                run.update(config=dict(run.cfg, parallel=max(1, args.parallel)))
            if args.detach:
                scheduler.acquire_lock(run)  # fail fast in the parent
                log = open(run.path("runner.log"), "a")
                proc = subprocess.Popen([sys.executable, "-m", "yoke.cli", "run", "--run", run.run_id],
                                        stdout=log, stderr=log, start_new_session=True)
                state._write_atomic(run.path("runner.lock"), str(proc.pid))
                print("runner started, pid %d, log %s" % (proc.pid, run.path("runner.log")))
                return 0
            final = scheduler.run(run)
        except (planreview.ApprovalError, scheduler.LockError) as e:
            print("yoke: %s" % e, file=sys.stderr)
            return 1
        print(run.render_status())
        return 0 if final == "done" else 2
    if args.cmd == "wait":
        reason, msg = wait_for(run, args.timeout)
        print(msg)
        return {"timeout": 0, "done": 0, "stopped": 0}.get(reason, 2)
    if args.cmd == "clean":
        try:
            clean(run)
        except scheduler.LockError as e:
            print("yoke: %s" % e, file=sys.stderr)
            return 1
        print("cleaned worktrees")
        return 0
    if args.cmd == "map":
        print(render_map(run))
        return 0
    if args.cmd == "status":
        print(json.dumps(run.status(), indent=2) if args.json else brief(run) if args.brief else run.render_status())
    elif args.cmd == "escalations":
        for e in run.read_json("escalations.json", []):
            print("#%d %s: %s" % (e["id"], e["ticket"] or "-", e["reason"]))
    elif args.cmd == "unblock":
        if not args.ticket and not args.milestone:
            print("yoke: give ticket or --milestone", file=sys.stderr)
            return 1
        if args.ticket and args.milestone:
            print("yoke: cannot pass both ticket and --milestone", file=sys.stderr)
            return 1
        if args.milestone:
            if args.skip or args.reset_attempts:
                print("yoke: --skip and --reset-attempts require ticket", file=sys.stderr)
                return 1
            scheduler.unblock_milestone(run, args.milestone, note=args.note, waive=args.waive)
            print("unblocked milestone %s" % args.milestone)
        else:
            if args.waive:
                print("yoke: --waive requires --milestone", file=sys.stderr)
                return 1
            if not (args.note or args.skip or args.reset_attempts):
                print("yoke: give --note, --skip or --reset-attempts", file=sys.stderr)
                return 1
            scheduler.unblock(run, args.ticket, args.note, args.skip, args.reset_attempts)
            print("unblocked %s" % args.ticket)
    elif args.cmd == "nudge":
        try:
            pipeline.nudge(run, args.ticket, args.note)
            print("nudged %s" % args.ticket)
            return 0
        except (ValueError, KeyError) as e:
            print("yoke: %s" % e, file=sys.stderr)
            return 1
    else:
        pid = scheduler.read_lock(run)
        if not pid or not scheduler._alive(pid):
            print("no runner active")
            return 0
        scheduler.stop(run)
        print("stopped runner %d" % pid)
    return 0
