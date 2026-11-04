"""The ticket record (PLAN 2.2): tickets/<tid>/state.json, its status transitions, and escalations.json.
The only module that writes ticket status or escalations; every write goes through state.Run.write_json."""
import glob
import os

from . import state


def rel(tid):
    """Run-relative path of the ticket's state.json."""
    return "tickets/%s/state.json" % tid


def load(run, tid):
    return run.read_json(rel(tid), {"status": "pending", "attempt": 1, "sessions": {}})


def status(run, tid):
    return load(run, tid).get("status", "pending")


def ids(run):
    """Ids of tickets that have a state.json, sorted."""
    return [os.path.basename(os.path.dirname(p)) for p in sorted(glob.glob(run.path(rel("*"))))]


def save(run, tid, st):
    run.write_json(rel(tid), st)
    run.event("ticket_state", ticket=tid, status=st["status"], attempt=st["attempt"])


def _set(run, tid, st, status_):
    st["status"] = status_
    save(run, tid, st)


def start(run, tid, st):
    _set(run, tid, st, "in_progress")


def close(run, tid, st):
    _set(run, tid, st, "closed")


def await_gate(run, tid, st, gates):
    _set(run, tid, st, "awaiting_gate")
    run.event("ticket_awaiting_gate", ticket=tid, gates=gates)


def block(run, tid, st, reason):
    """Blocked is status, escalation and events together."""
    _set(run, tid, st, "blocked")
    escalate(run, tid, reason)


def unblock(run, tid, skip=False, reset_attempts=False):
    """Back to pending (or skipped), dropping the ticket's open escalations. Returns the saved state."""
    st = load(run, tid)
    st["status"] = "skipped" if skip else "pending"
    if reset_attempts:
        st["attempt"] = 1
    save(run, tid, st)
    clear_escalations(run, tid)
    return st


def escalations(run):
    return run.read_json("escalations.json", [])


def escalate(run, tid, reason):
    """Open an escalation; tid is a ticket id, a milestone id, or None for run-level."""
    from . import tracker  # lazy: tracker -> prbody imports this module
    with state._LOCK:
        esc = escalations(run)
        esc.append({"id": len(esc) + 1, "ticket": tid, "reason": reason, "at": state.now()})
        run.write_json("escalations.json", esc)
    run.event("escalation", ticket=tid, reason=reason)
    if tid:
        tracker.comment(run, tid, "yoke escalation: " + reason)


def clear_escalations(run, key):
    """Drop every escalation whose ticket field is key (a ticket or milestone id)."""
    with state._LOCK:
        run.write_json("escalations.json", [e for e in escalations(run) if e.get("ticket") != key])
