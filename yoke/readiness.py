"""Ticket readiness review: isolated read-only calls per ticket."""
import json
import os
import re
import string

from . import adapters, calls, config, planreview, schema

PKG = os.path.dirname(os.path.abspath(__file__))


def plan_section(plan_text, anchor):
    """Extract a plan section matching anchor (by <a id> or heading slug).

    Leading '#' in anchor is stripped. Matches stopping at the next heading of
    the same or higher level, keeping deeper sub-headings inside. Returns ''
    when not found.
    """
    if not plan_text or not anchor:
        return ""
    anchor = str(anchor).lstrip("#").strip().lower()
    if not anchor:
        return ""

    lines = plan_text.splitlines()
    fence = False
    pending_anchors = []
    slugs_seen = {}
    headings = []

    for idx, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            fence = not fence
            continue
        if fence:
            continue
        found = planreview._ANCHOR.findall(line)
        if found:
            for a in found:
                pending_anchors.append(a.strip().lower())
        m = planreview._HEADING.match(line)
        if not m:
            if line.strip() and not planreview._ANCHOR.search(line):
                pending_anchors = []
            continue
        level = len(m.group(1))
        title = m.group(2)
        slug = planreview._slug(title)
        n = slugs_seen.get(slug, 0)
        slugs_seen[slug] = n + 1
        slug_with_num = slug if n == 0 else "%s-%d" % (slug, n)
        all_anchors = set(pending_anchors)
        all_anchors.add(slug)
        all_anchors.add(slug_with_num)
        headings.append({
            "level": level,
            "title": title,
            "line_idx": idx,
            "anchors": all_anchors,
        })
        pending_anchors = []

    match_h = None
    match_pos = -1
    for i, h in enumerate(headings):
        if anchor in h["anchors"]:
            match_h = h
            match_pos = i
            break

    if match_h is None:
        return ""

    start_line = match_h["line_idx"]
    match_level = match_h["level"]

    end_line = len(lines)
    for next_h in headings[match_pos + 1:]:
        if next_h["level"] <= match_level:
            stop_idx = next_h["line_idx"]
            k = stop_idx - 1
            while k > start_line:
                prev_line = lines[k]
                if prev_line.strip() == "" or planreview._ANCHOR.search(prev_line):
                    stop_idx = k
                    k -= 1
                else:
                    break
            end_line = stop_idx
            break

    section_lines = lines[start_line:end_line]
    return "\n".join(section_lines).strip()


def next_round(run):
    """Count of ticket-review/round-* dirs + 1."""
    d = run.path("ticket-review")
    if not os.path.isdir(d):
        return 1
    done = [n for n in os.listdir(d) if n.startswith("round-") and os.path.isdir(os.path.join(d, n))]
    return len(done) + 1


def review_tickets(run, adapter=None, env=None):
    """Run one read-only readiness call per ticket (up to ticket_review_max).

    Writes per-ticket records and summary. Returns (round_num, summary_dict).
    Raises RuntimeError when round cap exceeded, or ApprovalError when plan.md
    or tickets.json is missing.
    """
    cfg = run.cfg
    plan = planreview._read(run, "plan.md")
    tickets_text = planreview._read(run, "tickets.json")
    try:
        h = planreview.current_hash(run, "tickets")
        tickets_doc = json.loads(tickets_text)
    except (ValueError, TypeError) as e:
        raise planreview.ApprovalError("malformed tickets.json: %s" % e)

    n = next_round(run)
    max_rounds = cfg.get("max_ticket_review_rounds", 2)
    if n > max_rounds:
        raise RuntimeError(
            "max_ticket_review_rounds (%d) reached: arbitrate tickets and ask the user" % max_rounds
        )

    standard = planreview.load_standard("readiness")
    with open(os.path.join(PKG, "prompts", "readiness.md")) as f:
        prompt_tmpl = string.Template(f.read())

    tickets = tickets_doc.get("tickets", []) if isinstance(tickets_doc, dict) else []

    max_tickets = cfg.get("ticket_review_max", 30)
    to_review = tickets[:max_tickets]
    to_skip = tickets[max_tickets:]

    skipped = [t.get("id", "T-unknown") for t in to_skip if isinstance(t, dict)]
    ready = []
    not_ready = []

    repo = run.read_json("run.json")["repo"]
    setting_sources = ((cfg.get("agents") or {}).get("claude") or {}).get("setting_sources", "project,local")
    timeout_s = (cfg.get("timeouts_s") or {}).get("readiness", 900)
    act_adapter = adapter or adapters.get(config.agent_for(cfg, "readiness"))

    budget_exhausted = False
    for i, t in enumerate(to_review):
        tid = t.get("id", "T-unknown") if isinstance(t, dict) else "T-unknown"
        if not isinstance(tid, str) or not re.match(r"^[A-Za-z0-9_.-]+$", tid):
            tid = re.sub(r"[^A-Za-z0-9_.-]", "_", str(tid))
        if budget_exhausted:
            rec = {
                "ticket": tid,
                "tickets_sha256": h,
                "verdict": "not_ready",
                "paraphrase": "",
                "questions": [],
                "missing_refs": [],
                "error": "budget exhausted",
            }
            not_ready.append(tid)
            run.write_json("ticket-review/round-%d/%s.json" % (n, tid), rec)
            continue

        cites = t.get("plan_sections") or []
        sections_str = "\n\n".join(plan_section(plan, a) for a in cites).strip()
        sections_block = sections_str if sections_str else "(no plan sections cited)"

        prompt = prompt_tmpl.safe_substitute(
            standard=standard,
            ticket=json.dumps(t, indent=2),
            plan_sections=sections_block,
        )

        spec = adapters.CallSpec(
            mode="read",
            cwd=repo,
            prompt=prompt,
            schema_path=schema.schema_path("readiness"),
            timeout_s=timeout_s,
            env=env or {},
            setting_sources=setting_sources,
        )

        try:
            res = calls.run_call(run, act_adapter, spec, stage="readiness", attempt=n, label=tid, budget=True)
        except calls.BudgetExhausted:
            budget_exhausted = True
            rec = {
                "ticket": tid,
                "tickets_sha256": h,
                "verdict": "not_ready",
                "paraphrase": "",
                "questions": [],
                "missing_refs": [],
                "error": "budget exhausted",
            }
            not_ready.append(tid)
            run.write_json("ticket-review/round-%d/%s.json" % (n, tid), rec)
            continue

        is_ok = res.ok and res.schema_valid is not False and isinstance(res.structured_output, dict)
        if is_ok:
            out = res.structured_output
            verdict = out.get("verdict")
            paraphrase = out.get("paraphrase") or ""
            questions = out.get("questions") or []
            missing_refs = out.get("missing_refs") or []
            rec = {
                "ticket": tid,
                "tickets_sha256": h,
                "verdict": verdict,
                "paraphrase": paraphrase,
                "questions": questions,
                "missing_refs": missing_refs,
                "error": None,
            }
            if verdict == "ready":
                ready.append(tid)
            else:
                not_ready.append(tid)
        else:
            out = res.structured_output if isinstance(res.structured_output, dict) else {}
            rec = {
                "ticket": tid,
                "tickets_sha256": h,
                "verdict": "not_ready",
                "paraphrase": out.get("paraphrase") or "",
                "questions": out.get("questions") or [],
                "missing_refs": out.get("missing_refs") or [],
                "error": res.error or "schema invalid",
            }
            not_ready.append(tid)

        run.write_json("ticket-review/round-%d/%s.json" % (n, tid), rec)

    summary = {
        "round": n,
        "tickets_sha256": h,
        "ready": ready,
        "not_ready": not_ready,
        "skipped": skipped,
    }
    run.write_json("ticket-review/round-%d/summary.json" % n, summary)
    run.event("ticket_review", round=n, ready=len(ready), not_ready=len(not_ready), skipped=len(skipped))

    return n, summary
