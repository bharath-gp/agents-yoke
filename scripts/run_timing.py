"""Per-run timing breakdown from yoke audit records (see docs/perf-plan.md).

Usage: python3 scripts/run_timing.py ~/.yoke/runs/<run_id> [...]
"""
import json, os, sys, glob, collections, datetime as D

def P(s): return D.datetime.fromisoformat(s)
def h(x): return "%.2fh" % (x / 3600)
def m(x): return "%.1fm" % (x / 60)

def union(iv):
    iv = sorted(iv); tot = 0; cur = None
    for a, b in iv:
        if cur is None or a > cur[1]:
            if cur: tot += cur[1] - cur[0]
            cur = [a, b]
        else: cur[1] = max(cur[1], b)
    if cur: tot += cur[1] - cur[0]
    return tot

for r in sys.argv[1:]:
    print("=" * 30, os.path.basename(r))
    ev = [json.loads(l) for l in open(r + "/events.jsonl")]
    first = lambda k: next((P(e["at"]) for e in ev if e["kind"] == k), None)
    t0 = P(ev[0]["at"]); tk_ok = [P(e["at"]) for e in ev if e["kind"] == "approved" and e["approval"] == "tickets"][0]
    done = first("run_done"); pr = first("pr_opened")
    nu = [P(e["at"]) for e in ev if e["kind"] == "run_needs_user"]; ub = [P(e["at"]) for e in ev if e["kind"] == "unblock"]
    human_block = sum((b - a).total_seconds() for a, b in zip(nu, ub))
    runner = (done - tk_ok).total_seconds()
    print("total", h((pr - t0).total_seconds()), "| pre-runner (grill/plan/chart/approve)", h((tk_ok - t0).total_seconds()),
          "| runner", h(runner), "of which blocked on user", h(human_block), "| done->PR", h((pr - done).total_seconds()))
    metas = []
    for f in glob.glob(r + "/**/meta.json", recursive=True):
        try: d = json.load(open(f))
        except Exception: continue
        if "duration_s" not in d: continue
        end = P(d["ended_at"]); st = end - D.timedelta(seconds=d["duration_s"])
        d["_s"], d["_e"], d["_rel"] = st.timestamp(), end.timestamp(), os.path.relpath(f, r)
        metas.append(d)
    rs, re_ = tk_ok.timestamp(), done.timestamp()
    rm = [d for d in metas if rs <= d["_s"] <= re_]
    print("calls total", len(metas), "in runner", len(rm), "| runner wall with >=1 agent call active", h(union([(d["_s"], d["_e"]) for d in rm])))
    by = collections.defaultdict(lambda: [0, 0.0, 0.0])
    for d in metas:
        lab = d.get("label") or "?"
        base = lab.split("-r")[0].replace("-retry", "")
        k = (d.get("stage") or base, d.get("agent"))
        by[k][0] += 1; by[k][1] += d["duration_s"]; by[k][2] = max(by[k][2], d["duration_s"])
    print("stage/agent: n, sum, mean, max")
    for k, v in sorted(by.items(), key=lambda x: -x[1][1]):
        print("  %-22s %-7s n=%-3d sum=%-7s mean=%-6s max=%s" % (k[0], k[1], v[0], m(v[1]), m(v[1] / v[0]), m(v[2])))
    print("timed_out", sum(1 for d in metas if d.get("timed_out")), "| schema invalid", sum(1 for d in metas if d.get("schema_valid") is False),
          "| reask dirs", len(glob.glob(r + "/**/reask*", recursive=True)), "| -retry labels", sum(1 for d in metas if "retry" in (d.get("label") or "")),
          "| review rounds>0", sum(1 for d in metas if "-r" in (d.get("label") or "") and d.get("stage") in ("review", "code")))
    # per ticket
    tick = collections.defaultdict(list)
    for d in rm:
        if d.get("ticket"): tick[d["ticket"]].append(d)
    closes = {e["ticket"]: P(e["at"]).timestamp() for e in ev if e["kind"] == "ticket_state" and e["status"] == "closed"}
    starts = {}
    for e in ev:
        if e["kind"] == "ticket_state" and e["status"] == "in_progress": starts.setdefault(e["ticket"], P(e["at"]).timestamp())
    tot_gap = 0; tot_span = 0; tot_call = 0
    print("ticket: attempts span agent-union non-agent  (calls)")
    for t in sorted(tick):
        ds = tick[t]; s = starts.get(t, min(d["_s"] for d in ds)); e = closes.get(t, max(d["_e"] for d in ds))
        cu = union([(d["_s"], d["_e"]) for d in ds]); span = e - s
        if t in [x for x in ub]: pass
        att = max(d.get("attempt") or 1 for d in ds)
        tot_gap += span - cu; tot_span += span; tot_call += cu
        print("  %-7s att=%d span=%-7s agent=%-7s other=%-7s n=%d" % (t, att, m(span), m(cu), m(span - cu), len(ds)))
    print("  SUM span", h(tot_span), "agent", h(tot_call), "non-agent(tests/git/router/merge-lock/blocked)", h(tot_gap))
    # concurrency: wall where >=1 ticket in flight
    ivs = [(starts.get(t, 0), closes.get(t, 0)) for t in tick if t in starts and t in closes]
    print("  runner wall with >=1 ticket in flight", h(union(ivs)), "| idle (milestone checks/scheduling)", h(runner - union(ivs)))
