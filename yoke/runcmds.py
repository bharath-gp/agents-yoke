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

CMDS = ("run", "status", "escalations", "unblock", "stop", "wait", "clean")


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


def brief(run):
    """<=15 lines for the SessionStart hook: status plus the next command to run."""
    s = run.status()
    pid = scheduler.read_lock(run)
    alive = bool(pid and scheduler._alive(pid))
    if s["state"] == "done":
        hint = "all tickets closed; run /yoke:finish"
    elif alive:
        hint = "runner alive (pid %d); run /yoke:run to keep waiting" % pid
    else:
        hint = "runner not running; run /yoke:resume"
    if s["escalations"]:
        hint = "%d open escalation(s): `yoke escalations`; " % s["escalations"] + hint
    return "\n".join(["yoke run %s [%s]" % (s["run_id"], s["state"]),
                      "tickets %d, agent calls %d/%s" % (s["tickets_total"], s["agent_calls"], s["max_agent_calls"]),
                      hint])


def _active_run():
    from . import cli
    run = state.Run.latest_for_repo(cli._git_toplevel(os.getcwd()))
    if run and (run.read_json("run.json")["state"] != "done" or "final" not in run.read_json("approvals.json", {})):
        return run
    return None


def dispatch(args):
    from . import cli
    if args.cmd == "status" and args.active:  # hook path: never fail, never print when idle
        try:
            run = _active_run()
            if run:
                print(brief(run))
        except Exception:
            pass
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
