"""tickets import/show: validate a ticket list, classify untyped tickets, write tickets.json (PLAN 2.7, M3)."""
import json

from . import router, schema


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


def check(tickets, max_tickets):
    """Runner-level checks beyond the schema; returns error strings."""
    errs = []
    ids = [t["id"] for t in tickets]
    if len(tickets) > max_tickets:
        errs.append("%d tickets exceeds max_tickets %d" % (len(tickets), max_tickets))
    for i in sorted({i for i in ids if ids.count(i) > 1}):
        errs.append("duplicate id %s" % i)
    for t in tickets:
        for d in t["depends_on"]:
            if d not in ids:
                errs.append("%s depends on unknown %s" % (t["id"], d))
    if not errs:
        cyc = find_cycle(tickets)
        if cyc:
            errs.append("dependency cycle: " + " -> ".join(cyc))
    return errs


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
        errs = check(obj["tickets"], run.cfg["max_tickets"])
    if errs:
        return errs, None
    run.write_json("tickets.json", obj)
    approvals = run.read_json("approvals.json", {})
    if approvals.pop("tickets", None) is not None:  # new tickets need a new approval
        run.write_json("approvals.json", approvals)
    run.event("tickets_imported", count=len(raw))
    return [], obj["tickets"]
