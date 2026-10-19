"""Milestone handling: milestone extraction, ticket merging, and fix ticket generation."""
import copy

from . import state


def milestones_of(run_or_doc):
    """Return milestone list for run or tickets doc; falls back to implicit MS-00."""
    if hasattr(run_or_doc, "read_json"):
        doc = run_or_doc.read_json("tickets.json", {})
    elif isinstance(run_or_doc, dict):
        doc = run_or_doc
    else:
        doc = {}
    ms = doc.get("milestones") if isinstance(doc, dict) else None
    if ms:
        return copy.deepcopy(list(ms))
    return [{"id": "MS-00", "title": "implicit", "exit_check": None}]


def destination_and_scope(run_or_doc, ticket=None):
    """Return (destination_str, out_of_scope_str, out_of_scope_list) tuple."""
    if hasattr(run_or_doc, "read_json"):
        doc = run_or_doc.read_json("tickets.json", {})
    elif isinstance(run_or_doc, dict):
        doc = run_or_doc
    else:
        doc = {}
    dest = (ticket.get("destination") if ticket else None) or doc.get("destination") or "(none)"
    raw_oos = (ticket.get("out_of_scope") if ticket else None) or doc.get("out_of_scope") or []
    if isinstance(raw_oos, list):
        oos_list = list(raw_oos)
        oos_str = "\n".join("- %s" % x for x in oos_list) if oos_list else "(none)"
    elif isinstance(raw_oos, str) and raw_oos.strip():
        oos_list = [raw_oos.strip()]
        oos_str = raw_oos.strip()
    else:
        oos_list = []
        oos_str = "(none)"
    return dest, oos_str, oos_list


def merged_tickets(run_or_tickets, fixes=None):
    """Return tickets from tickets.json followed by fix tickets from fixes.json."""
    if fixes is not None:
        tickets = run_or_tickets
        fixes_list = fixes
    elif hasattr(run_or_tickets, "read_json"):
        t_doc = run_or_tickets.read_json("tickets.json", {})
        tickets = t_doc.get("tickets", []) if isinstance(t_doc, dict) else []
        fixes_list = run_or_tickets.read_json("fixes.json", [])
        if not isinstance(fixes_list, list):
            fixes_list = []
    elif isinstance(run_or_tickets, dict):
        tickets = run_or_tickets.get("tickets", [])
        fixes_list = []
    elif isinstance(run_or_tickets, list):
        tickets = run_or_tickets
        fixes_list = []
    else:
        tickets = []
        fixes_list = []

    out = []
    for t in tickets:
        item = dict(t)
        if "milestone" not in item:
            item["milestone"] = "MS-00"
        out.append(item)
    for f in fixes_list:
        out.append(dict(f))
    return out


def make_fix_ticket(
    id="F-001",
    milestone="MS-01",
    failing_output="",
    exit_check=None,
    title=None,
    description=None,
    files_hint=None,
    depends_on=None,
    plan_excerpt=None,
    **kw
):
    """Create a fix ticket dictionary carrying all required ticket fields."""
    if isinstance(id, int):
        id_str = "F-%03d" % id
    elif isinstance(id, str) and id.isdigit():
        id_str = "F-%03d" % int(id)
    else:
        id_str = str(id or "F-001")

    if isinstance(milestone, dict):
        ms_id = milestone.get("id", "MS-01")
        ms_title = milestone.get("title", "")
        ms_exit_check = exit_check or milestone.get("exit_check")
    elif isinstance(milestone, str):
        ms_id = milestone
        ms_title = kw.get("milestone_title", "")
        ms_exit_check = exit_check
    else:
        ms_id = "MS-01"
        ms_title = ""
        ms_exit_check = exit_check

    if not ms_exit_check:
        raise ValueError("exit_check is required to create a fix ticket")

    desc = description if description is not None else (failing_output or "")
    t_title = title or ("Fix %s: %s" % (ms_id, ms_title) if ms_title else "Fix %s" % ms_id)
    t_exit_criteria = kw.get("exit_criteria") or ["%s exits 0" % ms_exit_check]
    if plan_excerpt is not None:
        t_plan_excerpt = plan_excerpt
    else:
        t_plan_excerpt = ("%s\n%s" % (ms_title, ms_exit_check)).strip() if ms_title else ms_exit_check

    return {
        "id": id_str,
        "title": t_title,
        "type": "bugfix",
        "description": desc,
        "exit_criteria": list(t_exit_criteria),
        "depends_on": list(depends_on) if depends_on is not None else [],
        "files_hint": list(files_hint) if files_hint is not None else [],
        "plan_excerpt": t_plan_excerpt,
        "milestone": ms_id,
    }


def milestones_path(run):
    return run.path("milestones.json")


def load_states(run):
    """Return dict of milestone_id -> gate state dict from milestones.json."""
    if not hasattr(run, "read_json"):
        return {}
    data = run.read_json("milestones.json", {})
    if isinstance(data, list):
        return {m["id"]: m for m in data if isinstance(m, dict) and "id" in m}
    if isinstance(data, dict):
        if "milestones" in data and isinstance(data["milestones"], list):
            return {m["id"]: m for m in data["milestones"] if isinstance(m, dict) and "id" in m}
        return dict(data)
    return {}


def save_states(run, states):
    """Persist gate state dict to milestones.json."""
    if hasattr(run, "write_json"):
        run.write_json("milestones.json", states)


def get_milestone_state(run, mid):
    states = load_states(run)
    return states.get(mid, {"id": mid, "status": "open", "fix_count": 0})


def set_milestone_state(run, mid, **updates):
    states = load_states(run)
    st = states.setdefault(mid, {"id": mid, "status": "open", "fix_count": 0})
    st.update(updates)
    save_states(run, states)
    return st


def unblock_milestone(run, milestone, note=None, waive=None):
    """Reset fix_count to 0, clear escalations, and either record waiver or set status to open."""
    if not milestone:
        raise ValueError("milestone id is required")
    states = load_states(run)
    st = states.setdefault(milestone, {"id": milestone, "status": "open", "fix_count": 0})
    st["fix_count"] = 0
    if waive:
        st["status"] = "waived"
        st["waive_reason"] = waive
        save_states(run, states)
        run.event("milestone_waived", milestone=milestone, reason=waive)
        map_path = run.path("map.md")
        try:
            with open(map_path, "a") as f:
                f.write("\n## Milestone %s waived\nReason: %s\n" % (milestone, waive))
        except OSError:
            pass
    else:
        st["status"] = "open"
        if note:
            st["note"] = note
        save_states(run, states)

    with state._LOCK:
        esc = [e for e in run.read_json("escalations.json", []) if e.get("ticket") != milestone]
        run.write_json("escalations.json", esc)
    run.event("unblock", milestone=milestone, note=note, waive=waive)
