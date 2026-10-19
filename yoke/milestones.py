"""Milestone handling: milestone extraction, ticket merging, and fix ticket generation."""
import copy


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
