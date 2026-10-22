"""Sequential scheduler (PLAN 2.8, 2.11, 2.12): approvals, runner lock, next-ready pick, budgets,
unblock, and the parallel ticket pool (`parallel` workers, one shared merge lock)."""
import datetime
import glob
import json
import os
import signal
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from . import config, gitops, guard, milestones, pipeline, planreview, state, tracker, usage


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

    info = run.read_json("run.json", {})
    wl = info.get("waiting_limit")
    if wl and isinstance(wl, dict):
        resume_at = wl.get("resume_at")
        if resume_at:
            from . import limits
            target_dt = limits._parse_iso(resume_at)
            now_dt = datetime.datetime.now().astimezone()
            max_wait_s = float(run.cfg.get("limits", {}).get("max_wait_s", 7200))
            if target_dt is not None:
                if target_dt.tzinfo is not None and now_dt.tzinfo is None:
                    now_dt = now_dt.replace(tzinfo=target_dt.tzinfo)
                elif target_dt.tzinfo is None and now_dt.tzinfo is not None:
                    target_dt = target_dt.replace(tzinfo=now_dt.tzinfo)
                delta_s = (target_dt - now_dt).total_seconds()
                if delta_s > max_wait_s:
                    capped_dt = now_dt + datetime.timedelta(seconds=max_wait_s)
                    wl["resume_at"] = capped_dt.isoformat(timespec="seconds")
                    run.update(waiting_limit=wl)
            pause = pipeline.get_pause_state(run)
            with pause.lock:
                pause.stop_requested = False
                pause.stop_event.clear()
                pause.resume_at = wl.get("resume_at")
                pause.active_waiters = 1
                pause.unpaused.clear()


def stop(run):
    pipeline.get_pause_state(run).request_stop()
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
    pause = pipeline.reset_pause_state(run_)
    recover(run_)
    old_handler = None
    if threading.current_thread() is threading.main_thread():
        def _handle_term(*_):
            pause.request_stop()
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
        pause.request_stop()
        run_.update(state=final, runner_pid=None)
        run_.event("run_" + final)
        try:
            usage.write_usage(run_)
        except Exception as e:
            run_.event("usage_error", error=str(e))
        release_lock(run_)
        pipeline.clear_pause_state(run_)


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
                sts = {t["id"]: _status(run_, t["id"]) for t in tickets}
                for t in tickets:  # dependents of a skipped ticket cannot ever run
                    bad = [d for d in t.get("depends_on", []) if sts.get(d) == "skipped"]
                    if sts.get(t["id"]) == "pending" and bad:
                        st = pipeline.ticket_state(run_, t["id"])
                        st["status"] = "blocked"
                        pipeline.save_state(run_, t["id"], st)
                        pipeline.escalate(run_, t["id"], "depends on skipped %s" % ", ".join(bad))
                        sts[t["id"]] = "blocked"

                all_milestones = milestones.milestones_of(run_)
                ms_states = milestones.load_states(run_)
                ms_idx = {m["id"]: i for i, m in enumerate(all_milestones)}

                busy = set(running.values())
                ready = [
                    t for t in tickets
                    if t["id"] not in busy
                    and sts.get(t["id"]) in ("pending", "in_progress")
                    and all(sts.get(d) == "closed" for d in t.get("depends_on", []))
                    and all(
                        ms_states.get(em["id"], {}).get("status") in ("passed", "waived")
                        for em in all_milestones[:ms_idx.get(t.get("milestone", "MS-00"), 0)]
                    )
                ]
                ready.sort(key=lambda t: sts.get(t["id"]) != "in_progress")
                if not out_of_budget:
                    exceeded, reason = config.check_budget(run_)
                    if exceeded:
                        out_of_budget = reason

                pause = pipeline.get_pause_state(run_)
                if not pause.unpaused.is_set():
                    if not running:
                        from . import limits
                        normal_wake = limits.sleep_until(pause.resume_at, slice_s=0.2,
                                                         stop_check=lambda: pipeline._is_stopped(run_))
                        if normal_wake:
                            with pause.lock:
                                pause.active_waiters = 0
                                pause.resume_at = None
                                run_.update(waiting_limit=None)
                                pause.unpaused.set()
                        else:
                            return "stopped"

                deferred = []
                while ready and len(running) < parallel and not out_of_budget and pause.unpaused.is_set():
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

                if not running:
                    if out_of_budget:
                        is_cap = isinstance(out_of_budget, str) and out_of_budget.startswith(config.BUDGET_CAP_PREFIX)
                        reason = out_of_budget if isinstance(out_of_budget, str) else "budget exhausted: max_agent_calls_total reached"
                        if is_cap:
                            run_.update(budget_escalated=True)
                        pipeline.escalate(run_, None, reason)
                        return "needs_user"

                    unfinished_ms = None
                    for m in all_milestones:
                        m_st = ms_states.get(m["id"], {}).get("status", "open")
                        if m_st not in ("passed", "waived"):
                            unfinished_ms = m
                            break

                    if unfinished_ms:
                        m_id = unfinished_ms["id"]
                        curr_status = ms_states.get(m_id, {}).get("status", "open")
                        if curr_status == "failed":
                            return "needs_user"

                        m_tickets = [t for t in tickets if t.get("milestone", "MS-00") == m_id]
                        m_done = all(sts.get(t["id"]) in ("closed", "skipped") for t in m_tickets)

                        if m_done:
                            exit_check = unfinished_ms.get("exit_check")
                            if not exit_check:
                                ms_states.setdefault(m_id, {"id": m_id, "fix_count": 0})["status"] = "passed"
                                milestones.save_states(run_, ms_states)
                                tracker.close_milestone(run_, m_id)
                                continue

                            ms_states.setdefault(m_id, {"id": m_id, "fix_count": 0})["status"] = "checking"
                            milestones.save_states(run_, ms_states)

                            int_wt = run_.path("worktrees/_integration")
                            rc, tail = pipeline._rerun(run_, exit_check, int_wt)

                            if rc == 0:
                                ms_states[m_id]["status"] = "passed"
                                milestones.save_states(run_, ms_states)
                                run_.event("milestone_passed", milestone=m_id)
                                tracker.close_milestone(run_, m_id)
                                continue
                            else:
                                fix_count = ms_states[m_id].get("fix_count", 0)
                                max_fixes = run_.cfg.get("max_fix_tickets_per_milestone", 2)
                                if fix_count < max_fixes:
                                    fixes = run_.read_json("fixes.json", [])
                                    if not isinstance(fixes, list):
                                        fixes = []
                                    next_id = "F-%04d" % run_.alloc_number()
                                    note = ms_states[m_id].pop("note", None)
                                    desc = ("%s\n\n%s" % (note, tail)).strip() if note else tail
                                    fix = milestones.make_fix_ticket(
                                        id=next_id,
                                        milestone=unfinished_ms,
                                        failing_output=tail,
                                        description=desc,
                                        exit_check=exit_check,
                                    )
                                    fixes.append(fix)
                                    run_.write_json("fixes.json", fixes)
                                    ms_states[m_id]["fix_count"] = fix_count + 1
                                    ms_states[m_id]["status"] = "open"
                                    milestones.save_states(run_, ms_states)
                                    tracker.create_fix_issue(run_, fix)
                                    run_.event("fix_ticket_created", ticket=fix["id"], milestone=m_id)
                                    continue
                                else:
                                    ms_states[m_id]["status"] = "failed"
                                    milestones.save_states(run_, ms_states)
                                    pipeline.escalate(
                                        run_,
                                        m_id,
                                        ("milestone %s gate failed after %d fix tickets: %s" % (m_id, fix_count, tail)).strip(),
                                    )
                                    return "needs_user"

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
        pipeline.get_pause_state(run_).request_stop()
        raise


def unblock_milestone(run_, milestone, note=None, waive=None):
    return milestones.unblock_milestone(run_, milestone, note=note, waive=waive)


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
        info = run_.read_json("run.json", {})
        lw = dict(info.get("limit_waits", {}))
        new_lw = {k: v for k, v in lw.items() if not k.startswith(tid + ":")}
        if len(new_lw) != len(lw):
            run_.update(limit_waits=new_lw)
    run_.event("unblock", ticket=tid, skip=skip, reset_attempts=reset_attempts)
