"""Sequential scheduler (PLAN 2.8, 2.11, 2.12): approvals, runner lock, next-ready pick, budgets,
unblock, and the parallel ticket pool (`parallel` workers, one shared merge lock)."""
import glob
import json
import os
import signal
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from . import config, gitops, guard, hooks, milestones, pause, pipeline, planreview, router, state, ticketstate, tracker, usage


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


def kill_workers(run):
    """Workers run in their own process group; meta.json without ended_at is a live call.
    Returns the audit dirs of those calls."""
    live = []
    for p in glob.glob(run.path("tickets/*/attempt-*/**/meta.json"), recursive=True):
        with open(p) as f:
            meta = json.load(f)
        pid = meta.get("pid")
        if pid and "ended_at" not in meta:
            live.append(os.path.dirname(p))
            try:
                if os.getpgid(pid) == pid:  # guard against a recycled pid
                    os.killpg(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
    return live


def recover(run):
    """Crash recovery (PLAN 2.11): kill orphaned workers, set aside their half-done stage dirs.
    Tickets stay in_progress and restart their attempt; worktree changes are kept.
    Milestones recovery: treat checking as open.
    Limits recovery: resumes in-progress waiting_limit pause states from run.json."""
    root = run.path("tickets")
    for d in {os.sep.join(os.path.relpath(d, root).split(os.sep)[:3]) for d in kill_workers(run)}:
        src, dst, n = os.path.join(root, d), os.path.join(root, d) + ".interrupted", 1
        while os.path.exists(dst):
            n += 1
            dst = "%s.interrupted%d" % (src, n)
        os.rename(src, dst)
        run.event("interrupted", dir=os.path.relpath(src, run.dir))

    states = milestones.load_states(run)
    changed = False
    for mid, st in states.items():
        if isinstance(st, dict) and st.get("status") == "checking":
            st["status"] = "open"
            changed = True
    if changed:
        milestones.save_states(run, states)

    pause.recover(run)


def stop(run):
    pause.get(run).request_stop()
    pid = read_lock(run)
    if pid and pid != os.getpid() and _alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    kill_workers(run)


def run(run_, adapter_for=None, env=None):
    """Walk the tickets until done or nothing more can progress. Returns the final run state."""
    planreview.check_approval(run_, "tickets")  # includes plan
    acquire_lock(run_)
    pause_ = pause.reset(run_)
    recover(run_)
    old_handler = None
    if threading.current_thread() is threading.main_thread():
        def _handle_term(*_):
            pause_.request_stop()
            (_ for _ in ()).throw(SystemExit(143))
        try:
            old_handler = signal.signal(signal.SIGTERM, _handle_term)
        except (ValueError, OSError):
            pass
    final = "stopped"
    try:
        info = run_.read_json("run.json")
        gitops.ensure_feature_branch(info["repo"], info["branch"], info["base_sha"],
                                     run_.path("worktrees/_integration"))
        if (run_.cfg.get("guard") or {}).get("enabled", True):
            run_.update(agent_settings=guard.write_agent_settings(run_))
        else:
            run_.update(agent_settings=None)
        run_.update(state="running", runner_pid=os.getpid())
        final = _loop(run_, adapter_for, env)
        return final
    except SystemExit:
        final = "stopped"
        raise
    finally:
        if old_handler is not None and threading.current_thread() is threading.main_thread():
            try:
                signal.signal(signal.SIGTERM, old_handler)
            except (ValueError, OSError):
                pass
        pause_.request_stop()
        run_.update(state=final, runner_pid=None)
        run_.event("run_" + final)
        try:
            if final in ("done", "needs_user"):
                if final == "done":
                    msg = "run %s done" % run_.run_id
                else:
                    esc = ticketstate.escalations(run_)
                    last_esc = esc[-1] if (isinstance(esc, list) and esc) else None
                    if isinstance(last_esc, dict):
                        reason = (last_esc.get("reason") or "")[:200]
                    else:
                        reason = "needs user"
                    if not reason:
                        reason = "needs user"
                    msg = "run %s needs you: %s" % (run_.run_id, reason)
                hooks.notify(run_, "run_" + final, msg)
        except Exception:
            pass
        try:
            usage.write_usage(run_)
        except Exception as e:
            run_.event("usage_error", error=str(e))
        release_lock(run_)
        pause.clear(run_)


def _loop(run_, adapter_for, env):
    """Main scheduler pool loop. Dispatches ready tickets up to parallel capacity.
    When parallel > 1, defers ready tickets whose files_hint overlaps running tickets
    (exact path or directory prefix match), emitting a ticket_deferred event."""
    parallel = max(1, run_.cfg.get("parallel", 1))
    merge_lock = threading.Lock()
    running, out_of_budget = {}, False  # future -> ticket id
    running_tickets = {}  # ticket id -> ticket dict
    deferred_reported = set()  # set of (ticket_id, running_id) already logged
    try:
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            while True:
                tickets = milestones.merged_tickets(run_)
                sts = {t["id"]: ticketstate.status(run_, t["id"]) for t in tickets}
                for t in tickets:  # dependents of a skipped ticket cannot ever run
                    bad = [d for d in t.get("depends_on", []) if sts.get(d) == "skipped"]
                    if sts.get(t["id"]) == "pending" and bad:
                        ticketstate.block(run_, t["id"], ticketstate.load(run_, t["id"]),
                                          "depends on skipped %s" % ", ".join(bad))
                        sts[t["id"]] = "blocked"

                all_milestones = milestones.milestones_of(run_)
                ms_states = milestones.load_states(run_)
                ms_idx = {m["id"]: i for i, m in enumerate(all_milestones)}

                busy = set(running.values())

                # Look-ahead (PLAN M36): a ticket may start once its deps are closed, whatever the
                # gates say; only a parked ticket (awaiting_gate) needs the earlier gates passed to resume.
                ready = [
                    t for t in tickets
                    if t["id"] not in busy
                    and (
                        (sts.get(t["id"]) in ("pending", "in_progress")
                         and all(sts.get(d) == "closed" for d in t.get("depends_on", [])))
                        or (sts.get(t["id"]) == "awaiting_gate"
                            and not milestones.open_gates_before(t, all_milestones, ms_states, ms_idx))
                    )
                ]
                # resumed work first, then earliest milestone so look-ahead never starves the current one
                ready.sort(key=lambda t: (sts.get(t["id"]) == "pending",
                                          ms_idx.get(t.get("milestone", "MS-00"), 0)))
                if not out_of_budget:
                    exceeded, reason = config.check_budget(run_)
                    if exceeded:
                        out_of_budget = reason

                if pause.is_paused(run_) and not running and not pause.idle_wait(run_):
                    return "stopped"

                deferred = []
                while ready and len(running) < parallel and not out_of_budget and not pause.is_paused(run_):
                    t = ready.pop(0)
                    if parallel > 1 and running_tickets:
                        overlapping = [
                            rt for rt in running_tickets.values()
                            if milestones.hints_overlap(t.get("files_hint"), rt.get("files_hint"))
                        ]
                        if overlapping:
                            pair = (t["id"], overlapping[0]["id"])
                            if pair not in deferred_reported:
                                deferred_reported.add(pair)
                                run_.event(
                                    "ticket_deferred",
                                    ticket=t["id"],
                                    running=overlapping[0]["id"],
                                    overlaps=overlapping[0]["id"],
                                    reason="files_hint overlaps running %s" % overlapping[0]["id"],
                                )
                            deferred.append(t)
                            continue
                    running[pool.submit(pipeline.run_ticket, run_, t, adapter_for, env, merge_lock)] = t["id"]
                    running_tickets[t["id"]] = t

                unfinished_ms = milestones.current_gate(all_milestones, ms_states)
                gate_due = unfinished_ms is not None and all(
                    sts.get(t["id"]) in ("closed", "skipped")
                    for t in tickets if t.get("milestone", "MS-00") == unfinished_ms["id"]
                )

                if not running or gate_due:
                    if out_of_budget and not running:
                        is_cap = isinstance(out_of_budget, str) and out_of_budget.startswith(config.BUDGET_CAP_PREFIX)
                        reason = out_of_budget if isinstance(out_of_budget, str) else "budget exhausted: max_agent_calls_total reached"
                        if is_cap:
                            run_.update(budget_escalated=True)
                        ticketstate.escalate(run_, None, reason)
                        return "needs_user"

                    if unfinished_ms:
                        if ms_states.get(unfinished_ms["id"], {}).get("status", "open") == "failed":
                            return "needs_user"
                        if gate_due:
                            if _gate_step(run_, unfinished_ms) == "needs_user":
                                return "needs_user"
                            continue

                    if not running:
                        return "done" if all(s in ("closed", "skipped") for s in sts.values()) else "needs_user"

                done, _ = wait(running, return_when=FIRST_COMPLETED)
                for f in done:
                    tid = running.pop(f, None)
                    if tid:
                        running_tickets.pop(tid, None)
                        deferred_reported = {p for p in deferred_reported if p[1] != tid}
                    try:
                        f.result()
                    except pipeline.BudgetExhausted as e:
                        out_of_budget = str(e) or "budget exhausted: max_agent_calls_total reached"  # let in-flight tickets stop at their next call
                    except (pipeline.WorkerStopped, SystemExit):
                        return "stopped"
    except BaseException:
        pause.get(run_).request_stop()
        raise


def _gate_step(run_, m):
    """Run milestone m's due gate and apply milestones.evaluate_gate's verdict.
    Returns "needs_user" on escalation, else None (the loop goes round again)."""
    m_id, exit_check = m["id"], m.get("exit_check")
    rc = tail = route = None
    if exit_check:
        milestones.set_milestone_state(run_, m_id, status="checking")
        rc, tail = pipeline._rerun(run_, exit_check, run_.path("worktrees/_integration"))
        if rc != 0:
            route = router.gate_failure_route(run_, m_id, exit_check, tail)
    ms_state = milestones.load_states(run_).get(m_id, {})
    verdict, reason = milestones.evaluate_gate(m, ms_state, rc, tail, route,
                                               run_.cfg.get("max_fix_tickets_per_milestone", 2))
    if verdict == "passed":
        milestones.set_milestone_state(run_, m_id, status="passed")
        if exit_check:
            run_.event("milestone_passed", milestone=m_id)
        tracker.close_milestone(run_, m_id)
        return None
    if verdict == "escalate":
        milestones.set_milestone_state(run_, m_id, status="failed")
        ticketstate.escalate(run_, m_id, reason)
        return "needs_user"
    fixes = run_.read_json("fixes.json", [])
    if not isinstance(fixes, list):
        fixes = []
    next_id = "F-%04d" % run_.alloc_number()
    note = ms_state.get("note")
    desc = ("%s\n\n%s" % (note, tail)).strip() if note else tail
    fix = milestones.make_fix_ticket(id=next_id, milestone=m, failing_output=tail,
                                     description=desc, exit_check=exit_check)
    fixes.append(fix)
    run_.write_json("fixes.json", fixes)
    milestones.set_milestone_state(run_, m_id, drop=("note",), fix_count=ms_state.get("fix_count", 0) + 1,
                                   status="open")
    tracker.create_fix_issue(run_, fix)
    run_.event("fix_ticket_created", ticket=fix["id"], milestone=m_id)
    return None


def unblock_milestone(run_, milestone, note=None, waive=None):
    return milestones.unblock_milestone(run_, milestone, note=note, waive=waive)


def unblock(run_, tid, note=None, skip=False, reset_attempts=False):
    """User's answer to an escalation. Attempts are kept (the blocked attempt re-runs) unless reset."""
    if note and not skip:  # written before the status flips, so a live runner sees it
        attempt = 1 if reset_attempts else ticketstate.load(run_, tid)["attempt"]
        pipeline.write_feedback(run_, tid, attempt, "User note: " + note, append=True)
    ticketstate.unblock(run_, tid, skip=skip, reset_attempts=reset_attempts)
    pause.forget_ticket(run_, tid)
    run_.event("unblock", ticket=tid, skip=skip, reset_attempts=reset_attempts)
