"""Per-run usage ledger and usage.json (PLAN M16).

Walks meta.json files (including reask/, fallback/, .interrupted) and sums
observed.usage token fields and observed.cost by stage, ticket, agent and model,
plus totals and top 3 spenders. Missing cost (agy) is recorded as unreported, never 0.
Writes usage.json atomically at ticket close and run end.
"""
import datetime
import json
import os
import re


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
    if calls:
        # io_total is the headline figure; total_tokens (budget input) is left as the CLI reports it.
        entry["io_total"] = token_sums.get("input_tokens", 0) + token_sums.get("output_tokens", 0)
        entry["cache_total"] = (token_sums.get("cache_creation_input_tokens", 0)
                                 + token_sums.get("cache_read_input_tokens", 0))

    return entry


def merge(buckets):
    """Sum bucket dicts (from _aggregate_bucket) across runs; unreported cost is never counted as 0."""
    out = {}
    cost = 0.0
    reported = 0
    unreported = 0
    for b in buckets:
        for k, v in b.items():
            if k in ("cost", "unreported_calls"):
                continue
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out[k] = out.get(k, 0) + v
        c = b.get("cost")
        if isinstance(c, (int, float)) and not isinstance(c, bool):
            cost += c
            reported += 1
        elif c == "unreported":
            unreported += max(b.get("unreported_calls", 0), 1)
        unreported += b.get("unreported_calls", 0) if c != "unreported" else 0
    if reported == 0 and unreported:
        out["cost"] = "unreported"
    else:
        out["cost"] = round(cost, 6)
        if unreported:
            out["unreported_calls"] = unreported
    return out


def project_summary(runs):
    """Roll several runs' usage summaries up into totals and a per-agent split."""
    runs = list(runs)
    agents = {}
    for u in runs:
        for a, b in (u.get("by_agent") or {}).items():
            agents.setdefault(a, []).append(b)
    return {
        "totals": merge(u.get("totals") or {} for u in runs),
        "by_agent": {a: merge(bs) for a, bs in sorted(agents.items())},
    }


def summarize(run):
    """Walk every call's meta.json and screen.json under run dir and sum usage, cost & screen counts.

    Sums observed.usage token fields and observed.cost by stage, ticket, agent and model,
    plus totals and top 3 spenders. Missing cost is recorded as 'unreported', never 0.
    Also collects attempt screen.json files to aggregate factcheck_skipped, sources_discarded,
    and claims_screened under a 'screen' key when any counter is non-zero.
    """
    run_dir = run.dir if hasattr(run, "dir") else str(run)

    meta_files = []
    screen_files = []
    if os.path.isdir(run_dir):
        for root, dirs, files in os.walk(run_dir):
            dirs[:] = [d for d in dirs if d != ".git" and d != "worktrees"]
            if "meta.json" in files:
                meta_files.append(os.path.join(root, "meta.json"))
            if "screen.json" in files:
                screen_files.append(os.path.join(root, "screen.json"))
    meta_files.sort()
    screen_files.sort()

    all_calls = []
    stage_groups = {}
    ticket_groups = {}
    agent_groups = {}
    model_groups = {}
    ticket_agent_groups = {}
    session_ids = set()

    for mf in meta_files:
        try:
            with open(mf, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue

        sid = (meta.get("observed") or {}).get("session_id") if isinstance(meta.get("observed"), dict) else None
        if sid:
            session_ids.add(sid)
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
            ticket_agent_groups.setdefault(str(ticket), {}).setdefault(agent, []).append(call_info)

    by_stage = {s: _aggregate_bucket(c_list) for s, c_list in sorted(stage_groups.items())}
    by_ticket = {t: _aggregate_bucket(c_list) for t, c_list in sorted(ticket_groups.items())}
    by_ticket_agent = {
        t: {a: _aggregate_bucket(c) for a, c in sorted(ag.items())}
        for t, ag in sorted(ticket_agent_groups.items())
    }
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

    factcheck_skipped = 0
    sources_discarded = 0
    claims_screened = 0

    for sf in screen_files:
        try:
            with open(sf, "r", encoding="utf-8") as f:
                sc = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(sc, dict):
            continue
        if sc.get("factcheck_skipped"):
            factcheck_skipped += 1
        discarded = sc.get("discarded_sources")
        if isinstance(discarded, list):
            sources_discarded += len(discarded)
        elif isinstance(discarded, (int, float)) and not isinstance(discarded, bool):
            try:
                sources_discarded += int(discarded)
            except (OverflowError, ValueError):
                pass

        sup = sc.get("supported")
        unsup = sc.get("unsupported")
        esc = sc.get("escalated")

        def _count(val):
            if isinstance(val, list):
                return len(val)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                try:
                    return int(val)
                except (OverflowError, ValueError):
                    return 0
            return 0

        claims_screened += _count(sup) + _count(unsup) + _count(esc)

    ret = {
        "by_stage": by_stage,
        "by_ticket": by_ticket,
        "by_agent": by_agent,
        "by_ticket_agent": by_ticket_agent,
        "by_model": by_model,
        "totals": totals,
        "top_spenders": top_spenders,
        "session_ids": sorted(session_ids),
    }
    if factcheck_skipped > 0 or sources_discarded > 0 or claims_screened > 0:
        ret["screen"] = {
            "factcheck_skipped": factcheck_skipped,
            "sources_discarded": sources_discarded,
            "claims_screened": claims_screened,
        }
    return ret


def empty_summary():
    """Return an empty usage summary dict."""
    return {
        "totals": {"cost": 0.0},
        "top_spenders": [],
        "by_stage": {},
        "by_ticket": {},
        "by_agent": {},
        "by_ticket_agent": {},
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


_TRANSCRIPTS = {}  # path -> (mtime, size, ({message_id: (timestamp, session_id, usage)}, session cost or None))
_USAGE_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def _parse_ts(ts):
    try:
        return datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _read_transcript(path):
    st = os.stat(path)
    hit = _TRANSCRIPTS.get(path)
    if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]
    msgs = {}
    cost = None
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if not isinstance(o, dict):
                continue
            if o.get("type") == "cost-state" and _num(o.get("totalCostUSD")):
                cost = float(o["totalCostUSD"])  # running total: the last record wins
                continue
            m = o.get("message")
            if o.get("type") != "assistant" or not isinstance(m, dict) or not isinstance(m.get("usage"), dict):
                continue
            # streamed messages repeat under one id; the last record has the final output count
            msgs[m.get("id") or o.get("uuid")] = (o.get("timestamp"), o.get("sessionId"), m["usage"])
    _TRANSCRIPTS[path] = (st.st_mtime, st.st_size, (msgs, cost))
    return msgs, cost


def interactive_messages(repo, start, end, exclude_sessions=(), claude_home=None):
    """Assistant messages from interactive Claude Code sessions on repo within [start, end].

    Sessions yoke itself spawned (exclude_sessions, from the run's meta.json) are skipped so
    they are not counted twice. A session's recorded cost is per session, so each message
    carries an equal share of it and a run gets the share of the messages inside its window.
    Returns {message_id: {"sid", "usage", "cost" (share or None), "partial" (session cut by window)}}.
    """
    claude_home = claude_home or os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    d = os.path.join(claude_home, "projects", re.sub(r"[^A-Za-z0-9]", "-", repo or ""))
    lo, hi = _parse_ts(start), _parse_ts(end)
    out = {}
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for n in names:
        path = os.path.join(d, n)
        if not n.endswith(".jsonl"):
            continue
        try:
            if lo and os.stat(path).st_mtime < lo.timestamp():
                continue
            msgs, cost = _read_transcript(path)
        except OSError:
            continue
        inside = {}
        for mid, (ts, sid, u) in msgs.items():
            t = _parse_ts(ts)
            if sid in exclude_sessions or t is None or (lo and t < lo) or (hi and t > hi):
                continue
            inside[mid] = (sid, u)
        for mid, (sid, u) in inside.items():
            out[mid] = {"sid": sid, "usage": u, "partial": len(inside) < len(msgs),
                        "cost": cost / len(msgs) if cost is not None else None}
    return out


def interactive_bucket(msgs):
    """Bucket for interactive messages, one call per session.

    cost_partial counts sessions whose cost was pro-rated to the window (shown as ~).
    """
    sessions = {}
    for m in msgs.values():
        sessions.setdefault(m["sid"], []).append(m)
    calls = []
    partial = 0
    for ms in sessions.values():
        tokens = {}
        for m in ms:
            for k in _USAGE_KEYS:
                if _num(m["usage"].get(k)):
                    tokens[k] = tokens.get(k, 0) + m["usage"][k]
        costs = [m["cost"] for m in ms if m["cost"] is not None]
        cost = sum(costs) if costs else None
        if cost is not None and ms[0]["partial"]:
            partial += 1
        calls.append({"tokens": tokens, "cost": cost})
    b = _aggregate_bucket(calls)
    if partial:
        b["cost_partial"] = partial
    return b


def with_interactive(summary, msgs):
    """Fold interactive messages into a usage summary as agent/stage 'interactive'."""
    if not msgs:
        return summary
    b = interactive_bucket(msgs)
    out = dict(summary)
    out["totals"] = merge([summary.get("totals") or {}, b])
    for k in ("by_agent", "by_stage"):
        out[k] = dict(summary.get(k) or {}, interactive=b)
    return out
