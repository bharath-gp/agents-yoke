"""Rate-limit pause (M22): the one writer of the in-memory RunPauseState and of the
run.json keys `waiting_limit` and `limit_waits`. Readers (state, runcmds, dashboard) only read."""
import dataclasses
import datetime
import os
import threading

from . import limits, state


class WorkerStopped(Exception):
    """Raised when a worker is stopped by runner shutdown/SIGTERM."""
    pass


class WaitsExhausted(Exception):
    """A call hit the rate limit max_waits times in a row; the message is the raw limit text."""


class RunPauseState:
    def __init__(self):
        self.lock = threading.Lock()
        self.unpaused = threading.Event()
        self.unpaused.set()
        self.active_waiters = 0
        self.resume_at = None
        self.stop_requested = False
        self.stop_event = threading.Event()

    def request_stop(self):
        self.stop_requested = True
        self.stop_event.set()
        self.unpaused.set()


_RUN_PAUSE_STATES = {}
_PAUSE_LOCK = threading.Lock()


def _run_key(run):
    d = getattr(run, "dir", None)
    if d:
        return os.path.realpath(d)
    return getattr(run, "run_id", str(run))


def get(run):
    rid = _run_key(run)
    with _PAUSE_LOCK:
        if rid not in _RUN_PAUSE_STATES:
            _RUN_PAUSE_STATES[rid] = RunPauseState()
        return _RUN_PAUSE_STATES[rid]


def clear(run):
    rid = _run_key(run)
    with _PAUSE_LOCK:
        _RUN_PAUSE_STATES.pop(rid, None)


def reset(run):
    pause = get(run)
    with pause.lock:
        pause.stop_requested = False
        pause.stop_event.clear()
        pause.active_waiters = 0
        pause.resume_at = None
        pause.unpaused.set()
    return pause


def is_stopped(run):
    pause = get(run)
    if pause.stop_requested or pause.stop_event.is_set():
        return True
    try:
        return run.read_json("run.json", {}).get("state") == "stopped"
    except Exception:
        return False


def wait_pool(run):
    pause = get(run)
    pause.unpaused.wait()
    if is_stopped(run):
        raise WorkerStopped("runner stopped")


def recover(run):
    """Resume an in-progress waiting_limit from run.json, capping resume_at at max_wait_s."""
    info = run.read_json("run.json", {})
    wl = info.get("waiting_limit")
    if not (wl and isinstance(wl, dict)):
        return
    resume_at = wl.get("resume_at")
    if not resume_at:
        return
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
    pause = get(run)
    with pause.lock:
        pause.stop_requested = False
        pause.stop_event.clear()
        pause.resume_at = wl.get("resume_at")
        pause.active_waiters = 1
        pause.unpaused.clear()


def is_paused(run):
    return not get(run).unpaused.is_set()


def idle_wait(run):
    """Scheduler with no running workers: sleep out the pause. False when stopped meanwhile."""
    pause = get(run)
    normal_wake = limits.sleep_until(pause.resume_at, slice_s=0.2, stop_check=lambda: is_stopped(run))
    if normal_wake:
        with pause.lock:
            pause.active_waiters = 0
            pause.resume_at = None
            run.update(waiting_limit=None)
            pause.unpaused.set()
    return normal_wake


def forget_ticket(run, tid):
    """Drop the consecutive-wait counters of ticket `tid` (user unblocked it)."""
    with state._LOCK:
        info = run.read_json("run.json", {})
        lw = dict(info.get("limit_waits", {}))
        new_lw = {k: v for k, v in lw.items() if not k.startswith(tid + ":")}
        if len(new_lw) != len(lw):
            run.update(limit_waits=new_lw)


def call_with_waits(run, invoke, spec, *, tid, stage, attempt, label=None):
    """invoke(spec, label, charge) -> CallResult. On CallResult.limit, pauses the pool, waits with
    exponential backoff or reset_at, and retries with the same attempt and session id under the
    limit-retry label. Raises WaitsExhausted after max_waits consecutive waits."""
    wait_pool(run)
    res = invoke(spec, label, True)
    key = "%s:%s" % (tid, label or stage)
    if res.limit is None:
        info = run.read_json("run.json", {})
        lw = dict(info.get("limit_waits", {}))
        if key in lw:
            lw.pop(key, None)
            run.update(limit_waits=lw)
        return res

    info = run.read_json("run.json", {})
    lw = dict(info.get("limit_waits", {}))
    consecutive_waits = lw.get(key, 0)
    if not consecutive_waits:
        wl = info.get("waiting_limit")
        if isinstance(wl, dict) and wl.get("ticket") == tid and wl.get("stage") == stage:
            consecutive_waits = wl.get("consecutive_waits", 0)

    max_waits = run.cfg.get("limits", {}).get("max_waits", 3)
    pause = get(run)

    while res.limit is not None:
        if consecutive_waits >= max_waits:
            raw_msg = res.limit.get("raw") or "rate limit exceeded"
            info = run.read_json("run.json", {})
            cur_lw = dict(info.get("limit_waits", {}))
            cur_lw.pop(key, None)
            run.update(limit_waits=cur_lw, waiting_limit=None)
            with pause.lock:
                pause.active_waiters = 0
                pause.resume_at = None
                pause.unpaused.set()
            raise WaitsExhausted(raw_msg)
        consecutive_waits += 1

        resume_at, wait_s = limits.calculate_resume_at(res.limit, consecutive_waits, cfg=run.cfg)
        run.event("limit_hit", ticket=tid, stage=stage, attempt=attempt,
                  resume_at=resume_at, wait_s=round(wait_s, 3), raw=res.limit.get("raw"))

        with pause.lock:
            pause.active_waiters += 1
            pause.unpaused.clear()
            if pause.resume_at:
                t1 = limits._parse_iso(resume_at)
                t2 = limits._parse_iso(pause.resume_at)
                if t1 and t2 and t2 > t1:
                    resume_at = pause.resume_at
            pause.resume_at = resume_at
            lw[key] = consecutive_waits
            run.update(waiting_limit={"resume_at": resume_at, "raw": res.limit.get("raw"),
                                      "ticket": tid, "stage": stage, "attempt": attempt,
                                      "consecutive_waits": consecutive_waits,
                                      "session_id": res.session_id or spec.resume_session},
                       limit_waits=lw)

        normal_wake = False
        try:
            normal_wake = limits.sleep_until(resume_at, slice_s=0.05,
                                             stop_check=lambda: is_stopped(run))
        finally:
            with pause.lock:
                pause.active_waiters -= 1
                if pause.active_waiters <= 0:
                    pause.active_waiters = 0
                    pause.resume_at = None
                    if normal_wake:
                        run.update(waiting_limit=None)
                    pause.unpaused.set()

        if not normal_wake:
            raise WorkerStopped("runner stopped during limit wait")

        wait_pool(run)
        retry_spec = dataclasses.replace(spec, resume_session=res.session_id or spec.resume_session)
        retry_label = "limit-retry" if consecutive_waits == 1 else ("limit-retry-%d" % consecutive_waits)
        res = invoke(retry_spec, retry_label, False)

    if res.ok:
        info = run.read_json("run.json", {})
        cur_lw = dict(info.get("limit_waits", {}))
        if key in cur_lw:
            cur_lw.pop(key, None)
            run.update(limit_waits=cur_lw)

    return res
