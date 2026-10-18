"""CLI handlers for the runner commands: run, status, escalations, unblock, stop (PLAN 2.16)."""
import glob
import json
import os
import shutil
import signal
import subprocess
import sys
import time

from . import gitops, planreview, scheduler, state

CMDS = ("run", "status", "escalations", "unblock", "stop", "wait", "clean", "runs")


def add_parsers(sub):
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
    u.add_argument("ticket")
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


def wait_for(run, timeout, poll=2):
    """Block until an escalation opens, the run ends, or the runner dies. -> (reason, message)."""
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
    return {
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

    return "\n".join(["yoke run %s [%s]" % (s["run_id"], s["state"]),
                      "tickets %d, agent calls %d/%s" % (s["tickets_total"], s["agent_calls"], s["max_agent_calls"]),
                      hint])


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
    if args.cmd == "status":
        print(json.dumps(run.status(), indent=2) if args.json else brief(run) if args.brief else run.render_status())
    elif args.cmd == "escalations":
        for e in run.read_json("escalations.json", []):
            print("#%d %s: %s" % (e["id"], e["ticket"] or "-", e["reason"]))
    elif args.cmd == "unblock":
        if not (args.note or args.skip or args.reset_attempts):
            print("yoke: give --note, --skip or --reset-attempts", file=sys.stderr)
            return 1
        scheduler.unblock(run, args.ticket, args.note, args.skip, args.reset_attempts)
        print("unblocked %s" % args.ticket)
    else:
        pid = scheduler.read_lock(run)
        if not pid or not scheduler._alive(pid):
            print("no runner active")
            return 0
        os.kill(pid, signal.SIGTERM)
        scheduler.kill_workers(run)
        print("stopped runner %d" % pid)
    return 0
