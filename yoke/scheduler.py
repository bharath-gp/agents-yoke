"""Sequential scheduler (PLAN 2.8, 2.11, 2.12): approvals, runner lock, next-ready pick, budgets,
unblock, and the parallel ticket pool (`parallel` workers, one shared merge lock)."""
import os
import signal
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from . import gitops, pipeline, planreview, state


class LockError(Exception):
    pass


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def read_lock(run):
    try:
        with open(run.path("runner.lock")) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def acquire_lock(run):
    pid = read_lock(run)
    if pid and pid != os.getpid() and _alive(pid):
        raise LockError("runner already active (pid %d)" % pid)
    state._write_atomic(run.path("runner.lock"), str(os.getpid()))


def release_lock(run):
    if read_lock(run) == os.getpid():
        os.remove(run.path("runner.lock"))


def _status(run, tid):
    return pipeline.ticket_state(run, tid)["status"]


def run(run_, adapter_for=None, env=None):
    """Walk the tickets until done or nothing more can progress. Returns the final run state."""
    planreview.check_approval(run_, "tickets")  # includes plan
    acquire_lock(run_)
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(SystemExit(143)))
    final = "stopped"
    try:
        info = run_.read_json("run.json")
        gitops.ensure_feature_branch(info["repo"], info["branch"], info["base_sha"],
                                     run_.path("worktrees/_integration"))
        run_.update(state="running", runner_pid=os.getpid())
        final = _loop(run_, adapter_for, env)
        return final
    finally:
        run_.update(state=final, runner_pid=None)
        run_.event("run_" + final)
        release_lock(run_)


def _loop(run_, adapter_for, env):
    parallel = max(1, run_.cfg.get("parallel", 1))
    merge_lock = threading.Lock()
    running, out_of_budget = {}, False  # future -> ticket id
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        while True:
            tickets = run_.read_json("tickets.json")["tickets"]
            sts = {t["id"]: _status(run_, t["id"]) for t in tickets}
            for t in tickets:  # dependents of a skipped ticket cannot ever run
                bad = [d for d in t["depends_on"] if sts[d] == "skipped"]
                if sts[t["id"]] == "pending" and bad:
                    st = pipeline.ticket_state(run_, t["id"])
                    st["status"] = "blocked"
                    pipeline.save_state(run_, t["id"], st)
                    pipeline.escalate(run_, t["id"], "depends on skipped %s" % ", ".join(bad))
                    sts[t["id"]] = "blocked"
            busy = set(running.values())
            ready = [t for t in tickets if t["id"] not in busy and sts[t["id"]] in ("pending", "in_progress")
                     and all(sts[d] == "closed" for d in t["depends_on"])]
            ready.sort(key=lambda t: sts[t["id"]] != "in_progress")
            while ready and len(running) < parallel and not out_of_budget:
                t = ready.pop(0)
                running[pool.submit(pipeline.run_ticket, run_, t, adapter_for, env, merge_lock)] = t["id"]
            if not running:
                if out_of_budget:
                    pipeline.escalate(run_, None, "budget exhausted: max_agent_calls_total reached")
                    return "needs_user"
                return "done" if all(s in ("closed", "skipped") for s in sts.values()) else "needs_user"
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            for f in done:
                del running[f]
                try:
                    f.result()
                except pipeline.BudgetExhausted:
                    out_of_budget = True  # let in-flight tickets stop at their next call


def unblock(run_, tid, note=None, skip=False, reset_attempts=False):
    """User's answer to an escalation. Attempts are kept (the blocked attempt re-runs) unless reset."""
    st = pipeline.ticket_state(run_, tid)
    st["status"] = "skipped" if skip else "pending"
    if reset_attempts:
        st["attempt"] = 1
    if note and not skip:
        pipeline.write_feedback(run_, tid, st["attempt"], "User note: " + note, append=True)
    pipeline.save_state(run_, tid, st)
    with state._LOCK:
        esc = [e for e in run_.read_json("escalations.json", []) if e["ticket"] != tid]
        run_.write_json("escalations.json", esc)
    run_.event("unblock", ticket=tid, skip=skip, reset_attempts=reset_attempts)
