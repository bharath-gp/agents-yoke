"""Per-run usage ledger and usage.json (PLAN M16).

Walks meta.json files (including reask/, fallback/, .interrupted) and sums
observed.usage token fields and observed.cost by stage, ticket, agent and model,
plus totals and top 3 spenders. Missing cost (agy) is recorded as unreported, never 0.
Writes usage.json atomically at ticket close and run end.
"""
import json
import os


def _aggregate_bucket(calls):
    """Aggregate a list of call dicts: sum token fields and compute cost.

    Missing costs are recorded as 'unreported', never 0.
    """
    token_sums = {}
    reported_costs = []
    unreported_count = 0

    for call in calls:
        for k, v in call.get("tokens", {}).items():
            token_sums[k] = token_sums.get(k, 0) + v

        c = call.get("cost")
        if isinstance(c, (int, float)) and not isinstance(c, bool):
            reported_costs.append(float(c))
        else:
            unreported_count += 1

    entry = {}
    if not calls:
        entry["cost"] = 0.0
    elif reported_costs and unreported_count == 0:
        entry["cost"] = round(sum(reported_costs), 6)
    elif not reported_costs:
        entry["cost"] = "unreported"
    else:
        entry["cost"] = round(sum(reported_costs), 6)
        entry["unreported_calls"] = unreported_count

    for k, v in sorted(token_sums.items()):
        entry[k] = v

    return entry


def summarize(run):
    """Walk every call's meta.json under run dir and sum usage & cost.

    Sums observed.usage token fields and observed.cost by stage, ticket, agent and model,
    plus totals and top 3 spenders. Missing cost is recorded as 'unreported', never 0.
    """
    run_dir = run.dir if hasattr(run, "dir") else str(run)

    meta_files = []
    if os.path.isdir(run_dir):
        for root, dirs, files in os.walk(run_dir):
            dirs[:] = [d for d in dirs if d != ".git" and d != "worktrees"]
            if "meta.json" in files:
                meta_files.append(os.path.join(root, "meta.json"))
    meta_files.sort()

    all_calls = []
    stage_groups = {}
    ticket_groups = {}
    agent_groups = {}
    model_groups = {}

    for mf in meta_files:
        try:
            with open(mf, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue

        stage = meta.get("stage") or "unknown"
        ticket = meta.get("ticket")
        agent = meta.get("agent") or "unknown"

        observed = meta.get("observed")
        if not isinstance(observed, dict) or not observed:
            continue

        requested = meta.get("requested")
        if not isinstance(requested, dict):
            requested = {}

        model = observed.get("model") or requested.get("model") or "unreported"

        tok = observed.get("usage")
        if not isinstance(tok, dict):
            tok = {}

        raw_cost = observed.get("cost")
        if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool):
            cost_val = float(raw_cost)
        else:
            cost_val = None

        call_tokens = {}
        for k, v in tok.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                call_tokens[k] = v

        call_info = {
            "stage": stage,
            "ticket": ticket,
            "agent": agent,
            "model": model,
            "cost": cost_val,
            "tokens": call_tokens,
        }

        all_calls.append(call_info)
        stage_groups.setdefault(stage, []).append(call_info)
        agent_groups.setdefault(agent, []).append(call_info)
        model_groups.setdefault(model, []).append(call_info)

        if ticket is not None:
            ticket_groups.setdefault(str(ticket), []).append(call_info)

    by_stage = {s: _aggregate_bucket(c_list) for s, c_list in sorted(stage_groups.items())}
    by_ticket = {t: _aggregate_bucket(c_list) for t, c_list in sorted(ticket_groups.items())}
    by_agent = {a: _aggregate_bucket(c_list) for a, c_list in sorted(agent_groups.items())}
    by_model = {m: _aggregate_bucket(c_list) for m, c_list in sorted(model_groups.items())}
    totals = _aggregate_bucket(all_calls)

    # Rank spenders by tokens when any ticket has unreported cost
    any_unreported = any(
        d.get("cost") == "unreported" or d.get("unreported_calls", 0) > 0
        for d in by_ticket.values()
    )

    def _spender_sort_key(item):
        tid, data = item
        tot_tokens = data.get("total_tokens")
        if not isinstance(tot_tokens, (int, float)) or isinstance(tot_tokens, bool):
            tot_tokens = sum(
                v
                for k, v in data.items()
                if "token" in k and isinstance(v, (int, float)) and not isinstance(v, bool)
            )
        if any_unreported:
            return (tot_tokens, tid)
        cost = data.get("cost", 0.0)
        cost_val = float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else 0.0
        return (cost_val, tot_tokens, tid)

    sorted_tickets = sorted(by_ticket.items(), key=_spender_sort_key, reverse=True)
    top_spenders = [dict(ticket=tid, **data) for tid, data in sorted_tickets[:3]]

    return {
        "by_stage": by_stage,
        "by_ticket": by_ticket,
        "by_agent": by_agent,
        "by_model": by_model,
        "totals": totals,
        "top_spenders": top_spenders,
    }


def empty_summary():
    """Return an empty usage summary dict."""
    return {
        "totals": {"cost": 0.0},
        "top_spenders": [],
        "by_stage": {},
        "by_ticket": {},
        "by_agent": {},
        "by_model": {},
    }


def format_cost(cost, unreported_calls=0):
    """Format cost value as a display string, noting unreported calls if mixed."""
    if cost == "unreported":
        res = "unreported"
    elif isinstance(cost, (int, float)) and not isinstance(cost, bool):
        res = "$%.4f" % float(cost)
    else:
        res = "$0.00"
    if unreported_calls:
        res += " (+%d unreported)" % unreported_calls
    return res


def write_usage(run):
    """Summarize and write usage.json atomically into run directory."""
    summary = summarize(run)
    run.write_json("usage.json", summary)
    return summary
