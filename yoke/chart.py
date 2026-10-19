"""Charting step: wayfinder maps plan to milestones and tickets (PLAN 1.3)."""
import copy
import os
import string

from . import adapters, calls, config, planreview, schema, tickets

PKG = os.path.dirname(os.path.abspath(__file__))


class ChartError(RuntimeError):
    """Raised when charting cannot proceed (e.g. max_chart_rounds reached)."""
    pass


def next_round(run):
    """Next chart round number (existing chart/round-* dirs + 1)."""
    d = run.path("chart")
    done = [n for n in os.listdir(d) if n.startswith("round-")] if os.path.isdir(d) else []
    return len(done) + 1


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
                t["id"] = "T-%03d" % (idx + 1)
        run.write_json("chart/tickets.json", raw_out)
        import_errs, _ = tickets.import_tickets(run, run.path("chart/tickets.json"), adapter=adapter)

    res.import_errs = import_errs
    run.event("chart", round=n, ok=bool(res.ok and res.schema_valid),
              imported=not bool(import_errs) if (res.ok and res.schema_valid) else False)
    return n, res
