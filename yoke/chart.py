"""Charting step: wayfinder maps plan to milestones and tickets (PLAN 1.3)."""
import copy
import os
import string
import sys

from . import adapters, calls, config, planreview, schema, tickets, tracker

PKG = os.path.dirname(os.path.abspath(__file__))


class ChartError(RuntimeError):
    """Raised when charting cannot proceed (e.g. max_chart_rounds reached)."""
    pass


def next_round(run):
    """Next chart round number (existing chart/round-* dirs + 1)."""
    d = run.path("chart")
    done = [n for n in os.listdir(d) if n.startswith("round-")] if os.path.isdir(d) else []
    return len(done) + 1


def ticket_offset(run):
    """Starting offset for T-ids: highest GitHub issue/PR number, frozen in run.json on first use.
    Falls back to 0 (T-0001) with a warning if gh is unusable; local tracker mode just uses 0."""
    info = run.read_json("run.json")
    if "ticket_offset" in info:
        return info["ticket_offset"]
    offset, source = 0, "local"
    if tracker.detect(info["repo"], run.cfg) == "github":
        try:
            offset, source = tracker.highest_number(info["repo"]), "github"
        except tracker.TrackerError as e:
            source = "fallback"
            print("warning: gh unavailable (%s); ticket ids start at T-0001" % e, file=sys.stderr)
    elif run.cfg.get("tracker", "auto") != "local":
        print("warning: no usable GitHub tracker; ticket ids start at T-0001", file=sys.stderr)
    run.update(ticket_offset=offset)
    run.event("ticket_offset", offset=offset, source=source)
    return offset


def renumber(run, tickets_list):
    """Give tickets contiguous T-ids after the run's offset, remapping depends_on. Skipped when ids clash,
    so tickets.check still reports the duplicates."""
    olds = [t.get("id") for t in tickets_list if isinstance(t, dict)]
    if len(set(olds)) != len(olds):
        return
    offset = ticket_offset(run)
    mapping = {}
    for i, t in enumerate(tickets_list):
        if isinstance(t, dict):
            mapping[t.get("id")] = "T-%04d" % (offset + i + 1)
    for t in tickets_list:
        if isinstance(t, dict):
            t["id"] = mapping[t.get("id")]
            t["depends_on"] = [mapping.get(d, d) for d in t.get("depends_on", [])]
    run.update(id_counter=offset + len(tickets_list))


def chart_prompt(cfg, plan, requirements="(none provided)", feedback=None):
    """Build the prompt for wayfinder from prompts/chart.md."""
    with open(os.path.join(PKG, "prompts", "chart.md")) as f:
        tmpl = string.Template(f.read())
    vars_dict = {
        "requirements": requirements,
        "plan": plan,
        "feedback": feedback or "(none)",
        "max_tickets": cfg.get("max_tickets", 40),
    }
    return tmpl.substitute(vars_dict), vars_dict


def chart_round(run, adapter=None, feedback=None, env=None):
    """One charting round with wayfinder.

    1. Requires plan approved (planreview.check_approval(run, "plan")).
    2. Refuses past max_chart_rounds.
    3. Builds prompt from prompts/chart.md, calls calls.run_call with wayfinder role.
    4. Writes chart/tickets.json and runs tickets.import_tickets on it.
    Returns (round, CallResult).
    """
    planreview.check_approval(run, "plan")
    cfg = run.cfg
    n = next_round(run)
    max_rounds = cfg.get("max_chart_rounds", 3)
    if n > max_rounds:
        raise ChartError("max_chart_rounds (%d) reached: ask the user" % max_rounds)

    with open(run.path("plan.md")) as f:
        plan = f.read()
    try:
        with open(run.path("requirements.md")) as f:
            requirements = f.read()
    except OSError:
        requirements = "(none provided)"

    prompt, _ = chart_prompt(cfg, plan, requirements=requirements, feedback=feedback)

    role = "wayfinder"
    spec = adapters.CallSpec(
        prompt=prompt,
        schema_path=os.path.join(schema.SCHEMA_DIR, "chart.json"),
        cwd=run.read_json("run.json")["repo"],
        mode="read",
        timeout_s=cfg["timeouts_s"].get(role, 1200),
        env=env or {},
    )
    adapter = adapter or adapters.get(config.agent_for(cfg, role))
    res = calls.run_call(run, adapter, spec, stage=role, attempt=n)

    import_errs = []
    if res.ok and res.schema_valid:
        raw_out = copy.deepcopy(res.structured_output) if isinstance(res.structured_output, dict) else {}
        tickets_list = raw_out.get("tickets", [])
        for idx, t in enumerate(tickets_list):
            if isinstance(t, dict) and not t.get("id"):
                t["id"] = "tmp-%d" % (idx + 1)
        renumber(run, tickets_list)
        run.write_json("chart/tickets.json", raw_out)
        import_errs, _ = tickets.import_tickets(run, run.path("chart/tickets.json"), adapter=adapter)

    res.import_errs = import_errs
    run.event("chart", round=n, ok=bool(res.ok and res.schema_valid),
              imported=not bool(import_errs) if (res.ok and res.schema_valid) else False)
    return n, res
