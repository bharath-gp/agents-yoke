"""tickets import/show: validate a ticket list, classify untyped tickets, write tickets.json (PLAN 2.7, M3)."""
import json

from . import router, schema

DETAILED_TYPES = ("feature", "bugfix", "refactor")  # types expected to carry steps and test_plan


def find_cycle(tickets):
    """Return a list of ids forming a dependency cycle, or None (DFS, deps assumed to exist)."""
    deps = {t["id"]: t["depends_on"] for t in tickets}
    state, stack = {}, []

    def visit(i):
        state[i] = 1
        stack.append(i)
        for d in deps[i]:
            if state.get(d) == 1:
                return stack[stack.index(d):] + [d]
            if d not in state:
                cyc = visit(d)
                if cyc:
                    return cyc
        state[i] = 2
        stack.pop()
        return None

    for i in deps:
        if i not in state:
            cyc = visit(i)
            if cyc:
                return cyc
    return None


def check(tickets, max_tickets, milestones=None):
    """Runner-level checks beyond the schema; returns error strings.

    Validates max ticket limits, duplicate IDs, unknown dependencies, cycles,
    and milestone rules (exit checks, valid and non-orphan milestones, and
    dependency order across milestones).
    """
    if isinstance(tickets, dict):
        doc = tickets
        milestones = milestones if milestones is not None else doc.get("milestones")
        tickets = doc.get("tickets", [])

    errs = []
    ids = [t["id"] for t in tickets if isinstance(t, dict) and "id" in t]
    if len(tickets) > max_tickets:
        errs.append("%d tickets exceeds max_tickets %d" % (len(tickets), max_tickets))
    for i in sorted({i for i in ids if ids.count(i) > 1}):
        errs.append("duplicate id %s" % i)
    for t in tickets:
        for d in t.get("depends_on", []):
            if d not in ids:
                errs.append("%s depends on unknown %s" % (t["id"], d))

    if milestones is not None:
        ms_ids = [m["id"] for m in milestones if isinstance(m, dict) and "id" in m]
        for mid in sorted({i for i in ms_ids if ms_ids.count(i) > 1}):
            errs.append("duplicate milestone id %s" % mid)
        for m in milestones:
            if isinstance(m, dict):
                ec = m.get("exit_check")
                if not ec or not str(ec).strip():
                    errs.append("milestone %s missing exit_check" % m.get("id", "?"))
        valid_mids = set(ms_ids)
        for t in tickets:
            t_ms = t.get("milestone")
            if not t_ms:
                errs.append("ticket %s missing milestone" % t.get("id"))
            elif t_ms not in valid_mids:
                errs.append("ticket %s has unknown milestone %s" % (t.get("id"), t_ms))
        ticket_mids = {t.get("milestone") for t in tickets if isinstance(t, dict)}
        for mid in ms_ids:
            if mid not in ticket_mids:
                errs.append("orphan milestone %s has no tickets" % mid)

        ms_order = {}
        for idx, mid in enumerate(ms_ids):
            if mid not in ms_order:
                ms_order[mid] = idx
        ticket_map = {t["id"]: t for t in tickets if isinstance(t, dict) and "id" in t}
        for t in tickets:
            t_ms = t.get("milestone")
            t_order = ms_order.get(t_ms)
            for d in t.get("depends_on", []):
                dep_ticket = ticket_map.get(d)
                if dep_ticket:
                    d_ms = dep_ticket.get("milestone")
                    d_order = ms_order.get(d_ms)
                    if t_order is not None and d_order is not None and d_order > t_order:
                        errs.append("%s (in %s) depends on %s in later milestone %s (backward-dependency violation)" %
                                    (t.get("id"), t_ms, d, d_ms))

    if not errs:
        cyc = find_cycle(tickets)
        if cyc:
            errs.append("dependency cycle: " + " -> ".join(cyc))
    return errs


def _longest_chain(group):
    """Return the longest in-group dependency path as a list of ticket ids from root to leaf."""
    group_ids = {
        t["id"]
        for t in group
        if isinstance(t, dict) and isinstance(t.get("id"), str)
    }
    deps = {
        t["id"]: [
            d
            for d in (t.get("depends_on") if isinstance(t.get("depends_on"), list) else [])
            if isinstance(d, str) and d in group_ids
        ]
        for t in group
        if isinstance(t, dict) and isinstance(t.get("id"), str)
    }
    memo = {}
    visiting = set()

    # shortcut: recursion depth bounded by ticket count and max_tickets config
    def visit(tid):
        if tid in memo:
            return memo[tid]
        if tid in visiting:
            return []
        visiting.add(tid)
        best = []
        for d in deps.get(tid, []):
            p = visit(d)
            if len(p) > len(best):
                best = p
        visiting.remove(tid)
        res = best + [tid]
        memo[tid] = res
        return res

    longest = []
    for tid in deps:
        p = visit(tid)
        if len(p) > len(longest):
            longest = p
    return longest


def warnings(tickets, milestones=None):
    """Runner-level warnings (non-fatal); returns warning strings.

    Flags tickets with >6 files_hint or >6 exit_criteria, feature/bugfix/refactor tickets
    without steps or test_plan, milestones with >12 tickets, and milestones with
    dependency chains of 4 or more tickets or full chains of 3 or more tickets.
    """
    if isinstance(tickets, dict):
        doc = tickets
        milestones = milestones if milestones is not None else doc.get("milestones")
        tickets = doc.get("tickets", [])

    warns = []
    for t in tickets:
        if not isinstance(t, dict):
            continue
        hints = t.get("files_hint", [])
        if len(hints) > 6:
            warns.append("ticket %s has %d files_hint (more than 6)" % (t.get("id"), len(hints)))
        ec = t.get("exit_criteria", [])
        if len(ec) > 6:
            warns.append("ticket %s has %d exit_criteria (more than 6)" % (t.get("id"), len(ec)))
        if t.get("type") in DETAILED_TYPES:
            missing = [k for k in ("steps", "test_plan") if not t.get(k)]
            if missing:
                warns.append("ticket %s (%s) has no %s; the coder will have to plan it" %
                             (t.get("id"), t["type"], " or ".join(missing)))

    if milestones:
        counts = {}
        for t in tickets:
            if isinstance(t, dict):
                mid = t.get("milestone")
                if isinstance(mid, str):
                    counts[mid] = counts.get(mid, 0) + 1
        for m in milestones:
            mid = m.get("id") if isinstance(m, dict) else m
            if isinstance(mid, str) and counts.get(mid, 0) > 12:
                warns.append("milestone %s has %d tickets (more than 12)" % (mid, counts[mid]))

    groups = {}
    for t in tickets:
        if isinstance(t, dict):
            mid = t.get("milestone")
            if isinstance(mid, str):
                groups.setdefault(mid, []).append(t)

    if milestones:
        ms_order = []
        seen = set()
        for m in milestones:
            mid = m.get("id") if isinstance(m, dict) else m
            if isinstance(mid, str) and mid in groups and mid not in seen:
                seen.add(mid)
                ms_order.append(mid)
        for mid in groups:
            if mid not in seen:
                ms_order.append(mid)
    else:
        ms_order = list(groups.keys())

    for mid in ms_order:
        group = groups[mid]
        path = _longest_chain(group)
        n = len(path)
        m_count = len(group)
        if n >= 4 or (n == m_count and m_count >= 3):
            warns.append(
                "milestone %s has a dependency chain of %d tickets (%s); only true code dependencies belong in depends_on"
                % (mid, n, " -> ".join(path))
            )

    return warns


def import_tickets(run, path, adapter=None):
    """Validate and store tickets; returns (errors, tickets). Nothing is written on error."""
    with open(path) as f:
        obj = json.load(f)
    raw = obj.get("tickets") if isinstance(obj, dict) else None
    if not isinstance(raw, list):
        return ["expected {\"tickets\": [...]}"], None
    valid_types = schema.load_schema("ticket")["properties"]["type"]["enum"]
    for t in raw:
        if isinstance(t, dict) and t.get("type") not in valid_types:
            t["type"] = router.classify_ticket(run, t, adapter)["result"]["type"]
    errs = schema.validate(obj, schema.load_schema("ticket-list"))
    if not errs:
        errs = check(obj["tickets"], run.cfg["max_tickets"], milestones=obj.get("milestones"))
    if errs:
        return errs, None
    run.write_json("tickets.json", obj)
    approvals = run.read_json("approvals.json", {})
    if approvals.pop("tickets", None) is not None:  # new tickets need a new approval
        run.write_json("approvals.json", approvals)
    run.event("tickets_imported", count=len(raw))
    return [], obj["tickets"]
