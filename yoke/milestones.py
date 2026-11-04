"""Milestone handling: milestone extraction, ticket merging, and fix ticket generation."""
import copy
import os

from . import state, ticketstate


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


def set_milestone_state(run, mid, drop=(), **updates):
    """The one writer of a milestone's gate state: drops the keys in drop, applies updates, saves."""
    states = load_states(run)
    st = states.setdefault(mid, {"id": mid, "status": "open", "fix_count": 0})
    for k in drop:
        st.pop(k, None)
    st.update(updates)
    save_states(run, states)
    return st


def gate_cleared(ms_states, mid):
    """The gate rule: a milestone is out of the way once it is passed or waived."""
    return ms_states.get(mid, {}).get("status") in ("passed", "waived")


def open_gates_before(ticket, all_milestones, ms_states, ms_idx=None):
    """Ids of the milestones before the ticket's own whose gates are not cleared."""
    if ms_idx is None:
        ms_idx = {m["id"]: i for i, m in enumerate(all_milestones)}
    return [m["id"] for m in all_milestones[:ms_idx.get(ticket.get("milestone", "MS-00"), 0)]
            if not gate_cleared(ms_states, m["id"])]


def current_gate(all_milestones, ms_states):
    """The first milestone whose gate is not cleared, or None."""
    return next((m for m in all_milestones if not gate_cleared(ms_states, m["id"])), None)


def evaluate_gate(milestone, ms_state, rc, tail, route, max_fixes):
    """Decide a due gate from its exit-check result. rc is None when the milestone has no exit_check;
    route is router.gate_failure_route's decision when rc != 0.
    Returns ("passed", None), ("fix", None) or ("escalate", reason)."""
    mid = milestone["id"]
    if not milestone.get("exit_check") or rc == 0:
        return "passed", None
    if route and route.get("source") == "ollama" and route.get("result") == "escalate":
        return "escalate", "milestone %s gate failed; decision model escalated: %s" % (mid, tail)
    fix_count = ms_state.get("fix_count", 0)
    if fix_count < max_fixes:
        return "fix", None
    return "escalate", ("milestone %s gate failed after %d fix tickets: %s" % (mid, fix_count, tail)).strip()


def unblock_milestone(run, milestone, note=None, waive=None):
    """Reset fix_count to 0, clear escalations, and either record waiver or set status to open."""
    if not milestone:
        raise ValueError("milestone id is required")
    if waive:
        set_milestone_state(run, milestone, fix_count=0, status="waived", waive_reason=waive)
        run.event("milestone_waived", milestone=milestone, reason=waive)
        map_path = run.path("map.md")
        try:
            with open(map_path, "a") as f:
                f.write("\n## Milestone %s waived\nReason: %s\n" % (milestone, waive))
        except OSError:
            pass
    else:
        set_milestone_state(run, milestone, fix_count=0, status="open", **({"note": note} if note else {}))

    ticketstate.clear_escalations(run, milestone)
    run.event("unblock", milestone=milestone, note=note, waive=waive)


def normalize_hint(path_or_dir):
    """Normalize a files_hint entry: forward slashes, stripped, no leading/trailing slash, '.' -> ''."""
    if not path_or_dir or not isinstance(path_or_dir, str):
        return ""
    s = path_or_dir.strip().replace("\\", "/")
    if not s:
        return ""
    norm = os.path.normpath(s).replace("\\", "/").strip("/")
    if norm in (".", ""):
        return ""
    return norm


def hints_overlap(hints_a, hints_b):
    """True if any non-empty hint in hints_a overlaps (exact path or directory prefix) any in hints_b.
    Empty hints never overlap or block."""
    if not hints_a or not hints_b:
        return False
    norm_a = [normalize_hint(h) for h in hints_a]
    norm_b = [normalize_hint(h) for h in hints_b]
    norm_a = [h for h in norm_a if h]
    norm_b = [h for h in norm_b if h]
    if not norm_a or not norm_b:
        return False
    for a in norm_a:
        for b in norm_b:
            if a == b or a.startswith(b + "/") or b.startswith(a + "/"):
                return True
    return False


def overlap_paths(ticket):
    """Return files_hint when non-empty, else paths from files [{path, action}]."""
    if not isinstance(ticket, dict):
        return []
    hints = ticket.get("files_hint")
    if hints:
        return list(hints)
    raw_files = ticket.get("files")
    files_list = raw_files if isinstance(raw_files, list) else []
    return [f["path"] for f in files_list if isinstance(f, dict) and f.get("path")]


def ticket_waiting_for(ticket, all_milestones, ms_states, sts, ms_idx=None):
    """Return list of reasons why ticket is waiting/blocked, or empty list."""
    if ms_idx is None:
        ms_idx = {m["id"]: i for i, m in enumerate(all_milestones)}
    tid = ticket.get("id")
    st = sts.get(tid, "pending")
    reasons = []
    if st == "awaiting_gate":
        blocked_gates = open_gates_before(ticket, all_milestones, ms_states, ms_idx)
        if blocked_gates:
            reasons.append("awaiting gate %s" % ", ".join(blocked_gates))
    elif st == "pending":  # look-ahead: only unclosed deps hold a pending ticket back
        unclosed = [d for d in ticket.get("depends_on", []) if sts.get(d) != "closed"]
        if unclosed:
            reasons.append("waiting for %s" % ", ".join(unclosed))
    return reasons
