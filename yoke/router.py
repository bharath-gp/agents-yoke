"""Decision router (PLAN 2.10): Ollama /v1/systemone -> cheap agent call -> deterministic default.
Never blocks: every function returns {result, source, model, confidence, fallback_reason}."""
import json
import urllib.request

from . import adapters, calls, config, schema

TICKET_TYPES = ["feature", "bugfix", "refactor", "test", "docs", "chore", "spike", "research"]
STATE_CHARS = 6000  # ~1500 tokens; tev1:4b runs with num_ctx 2048
# noul is uncalibrated (failures read 0.28-0.44 in the M0 spike); ponytail: tune on real data
RETRYABLE_MIN = 0.2
TRIAGE_CATEGORIES = ["test_failure", "env_problem", "spec_gap", "agent_error", "other"]
EFFORTS = ["low", "medium", "high"]


def _out(result, source, model, confidence, reason=None):
    return {"result": result, "source": source, "model": model, "confidence": confidence,
            "fallback_reason": reason}


def _ollama(cfg, state, questions, timeout=60):
    """POST /v1/systemone; returns the `answers` dict. Raises on any failure."""
    r = cfg["router"]
    body = json.dumps({"model": r["model"], "state": state, "questions": questions}).encode()
    req = urllib.request.Request(r["ollama_url"].rstrip("/") + "/v1/systemone", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)["answers"]


def _agent(run, name, prompt, adapter=None):
    """Cheap agent call; returns structured output or None on any failure."""
    cfg = run.cfg
    fb = cfg["router"]["fallback"]
    spec = adapters.CallSpec(
        prompt=prompt, schema_path=schema.schema_path("router-" + name), cwd=run.read_json("run.json")["repo"],
        model=cfg["router"].get("llm_model") or fb.get("model"), effort=fb.get("effort"),
        timeout_s=cfg["timeouts_s"]["router_fallback"])
    try:
        res = calls.run_call(run, adapter or adapters.get(fb["agent"]), spec, stage="router")
    except Exception as e:  # never block the loop on a router problem
        run.event("router_agent_error", fn=name, error=str(e))
        return None
    return res.structured_output if res.ok and res.schema_valid is not False else None


def _decide(run, fn, state, questions, from_ollama, prompt, default, adapter):
    """Fallback chain for decision functions. from_ollama(answers) -> (result, confidence)."""
    cfg = run.cfg
    model, reason, sname = cfg["router"]["model"], None, schema_name_of(fn)
    try:
        result, conf = from_ollama(_ollama(cfg, state, questions))
        errs = schema.validate(result, schema.load_schema("router-" + sname))
        if errs:
            reason = "ollama result invalid: " + "; ".join(errs)
        elif conf < config.threshold(cfg, fn):
            reason = "ollama confidence %.2f < %.2f" % (conf, config.threshold(cfg, fn))
        else:
            return _log(run, fn, _out(result, "ollama", model, conf))
    except Exception as e:
        reason = "ollama failed: %s" % e
    out = _agent(run, sname, prompt, adapter)
    if out is not None:
        return _log(run, fn, _out(out, "fallback", cfg["router"]["llm_model"], out.get("confidence"), reason))
    return _log(run, fn, _out(default, "default", None, None, reason + "; fallback agent failed"))


def decide(cfg, point, state, questions, parse, run=None, timeout=60):
    """Ollama-only decision path that never raises and never calls an agent."""
    thr = config.threshold(cfg, point)
    result = None
    conf = None
    source = "none"
    reason = None
    try:
        raw = _ollama(cfg, (state or "")[:STATE_CHARS], questions, timeout=timeout)
        result, conf = parse(raw)
        if conf is None or conf < thr:
            source = "none"
            result = None
            if conf is None:
                reason = "ollama confidence none < %.2f" % thr
            else:
                reason = "ollama confidence %.2f < %.2f" % (conf, thr)
        else:
            source = "ollama"
            reason = None
    except Exception as e:
        source = "none"
        result = None
        reason = "ollama failed: %s" % e

    out = {
        "point": point,
        "result": result,
        "source": source,
        "confidence": conf,
        "threshold": thr,
        "fallback_reason": reason,
    }

    if run is not None:
        try:
            choice = str(result)[:200] if result is not None else None
            model = cfg["router"]["model"]
            run.event(
                "router",
                fn=point,
                point=point,
                source=source,
                choice=choice,
                confidence=conf,
                threshold=thr,
                fallback_reason=reason,
                model=model,
            )
        except Exception:
            pass

    return out


def schema_name_of(fn):
    return {"classify_ticket": "classify", "triage_failure": "triage", "assert_check": "assert"}[fn]


def _log(run, fn, out):
    run.event("router", fn=fn, source=out["source"], model=out["model"], confidence=out["confidence"],
              fallback_reason=out["fallback_reason"])
    return out


def _ticket_text(t):
    return "Title: %s\nDescription: %s\nExit criteria: %s" % (
        t.get("title", ""), t.get("description", ""), "; ".join(t.get("exit_criteria") or []))


def classify_ticket(run, ticket, adapter=None):
    crit = dict.fromkeys(TICKET_TYPES + ["other"])
    crit.update(feature="new capability", bugfix="fix a defect", refactor="restructure, same behaviour",
                test="add or fix tests only", docs="documentation only", chore="build, deps, tooling",
                spike="exploration, throwaway",
                research="web research written up as a document")
    q = {"type": {"type": "choice", "instructions": "What kind of work is this ticket?", "criteria": crit}}

    def parse(ans):
        a = ans["type"]
        return {"type": a["choice"], "confidence": a["confidence"], "reason": "ollama"}, a["confidence"]

    prompt = ("Classify this ticket as exactly one of %s.\n\n%s" % (", ".join(TICKET_TYPES), _ticket_text(ticket)))
    return _decide(run, "classify_ticket", _ticket_text(ticket)[:STATE_CHARS], q, parse, prompt,
                   {"type": "feature", "confidence": 0.0, "reason": "default"}, adapter)


def triage_failure(run, log, adapter=None):
    q = {"category": {"type": "choice", "instructions": "Why did this agent step fail?",
                      "criteria": {"test_failure": "tests failed", "env_problem": "environment, tooling, network",
                                   "spec_gap": "ticket unclear or incomplete", "agent_error": "agent crashed or misbehaved",
                                   "other": None}},
         "retryable": {"type": "noul", "instructions": "Would retrying with feedback likely fix this?"}}

    def parse(ans):
        c = ans["category"]
        return {"category": c["choice"], "retryable": ans["retryable"]["noul"] >= RETRYABLE_MIN,
                "confidence": c["confidence"], "hint": "ollama"}, c["confidence"]

    prompt = "Triage this failure. Category is one of %s.\n\n%s" % (", ".join(TRIAGE_CATEGORIES), log[-STATE_CHARS:])
    return _decide(run, "triage_failure", log[-STATE_CHARS:], q, parse, prompt,
                   {"category": "other", "retryable": True, "confidence": 0.0, "hint": "default"}, adapter)


def format_triage_hint(triage_res):
    """Format category and retryable as an advisory hint string."""
    res = triage_res.get("result", {}) if isinstance(triage_res, dict) and "result" in triage_res else triage_res
    if not isinstance(res, dict):
        return ""
    cat = res.get("category") or "other"
    ret = res.get("retryable") if res.get("retryable") is not None else True
    return "Triage hint (advisory only): category=%s, retryable=%s" % (cat, ret)


def test_failure_category(run, ticket, cmd, tail):
    """Classify test command failure category via Ollama decision point 'test_failure'.

    Returns a decision dict with result set to 'product', 'testware', or 'environment'
    when Ollama confidence meets router.thresholds.test_failure, or source 'none'.
    """
    q = {
        "category": {
            "type": "choice",
            "instructions": "Why did this test command fail?",
            "criteria": {
                "product": "the code under test is wrong",
                "testware": "the tests or fixtures themselves are wrong",
                "environment": "missing tool, network, port, flaky timing",
            },
        }
    }
    title = ticket.get("title", "") if isinstance(ticket, dict) else str(ticket)
    state = ("Ticket: %s\nCommand: %s\nOutput tail:\n%s" % (title, cmd, tail))[-STATE_CHARS:]
    parse = lambda a: (a["category"]["choice"], a["category"]["confidence"])
    return decide(run.cfg, "test_failure", state, q, parse, run)


def validator_route(run, ticket, out):
    """Route a validator rejection to coder, qa, docs, or escalate."""
    q = {
        "route": {
            "type": "choice",
            "instructions": "Where should this validator rejection be routed?",
            "criteria": {
                "coder": "code needs changes",
                "qa": "tests are weak or missing",
                "docs": "only docs are lacking",
                "escalate": "the spec is ambiguous or contradictory",
            },
        }
    }
    title = ticket.get("title", "") if isinstance(ticket, dict) else str(ticket)
    out = out or {}
    summary = out.get("summary") or ""
    fb_coder = out.get("feedback_for_coder") or ""
    unmet = [
        c.get("criterion", "") if isinstance(c, dict) else str(c)
        for c in (out.get("exit_criteria") or [])
        if isinstance(c, dict) and not c.get("met")
    ]
    issues = [
        ("%s %s: %s" % (i.get("severity", ""), i.get("file", ""), i.get("description", ""))).strip()
        if isinstance(i, dict) else str(i)
        for i in (out.get("issues") or [])
    ]
    parts = ["Ticket: %s" % title]
    if summary:
        parts.append("Summary: %s" % summary)
    if fb_coder:
        parts.append("Feedback for coder:\n%s" % fb_coder)
    if unmet:
        parts.append("Unmet criteria:\n" + "\n".join(unmet))
    if issues:
        parts.append("Issues:\n" + "\n".join(issues))
    state = "\n\n".join(parts)[-STATE_CHARS:]
    parse = lambda a: (a["route"]["choice"], a["route"]["confidence"])
    return decide(run.cfg, "validator_reject", state, q, parse, run)


def after_failure(run, ticket, attempt, feedback):
    """Ask Ollama decision point 'after_failure' whether to retry, split, replan, or escalate."""
    q = {
        "next": {
            "type": "choice",
            "instructions": "What should be done after this failed attempt?",
            "criteria": {
                "retry": "retry the attempt",
                "split": "the ticket is too big",
                "replan": "the plan is wrong for this",
                "escalate": "needs a human",
            },
        }
    }
    title = ticket.get("title", "") if isinstance(ticket, dict) else str(ticket)
    state = ("Ticket: %s\nAttempt: %d\nFeedback:\n%s" % (title, attempt, feedback or ""))[-STATE_CHARS:]
    parse = lambda a: (a["next"]["choice"], a["next"]["confidence"])
    return decide(run.cfg, "after_failure", state, q, parse, run)


def gate_failure_route(run, milestone_id, exit_check, tail):
    """Ask Ollama decision point 'gate_failure' whether to create a fix ticket or escalate."""
    q = {
        "gate": {
            "type": "choice",
            "instructions": "Should this milestone gate failure create a fix ticket or escalate?",
            "criteria": {
                "fix_ticket": "a local defect a single fix can address",
                "escalate": "spec gap or cross-ticket design problem",
            },
        }
    }
    state = ("Milestone: %s\nExit check: %s\nOutput tail:\n%s" % (milestone_id, exit_check, tail))[-STATE_CHARS:]
    parse = lambda a: (a["gate"]["choice"], a["gate"]["confidence"])
    return decide(run.cfg, "gate_failure", state, q, parse, run)


def bump(tier, effort, attempt):
    """Deterministically bump effort (and tier past index 2) on retries.

    Effort advances by (attempt - 1) positions along EFFORTS (low, medium, high).
    When the advanced index exceeds high (index 2), tier escalates to 'plan' (if tier
    is not None) and effort remains clamped at 'high'. Returns (tier, effort).
    """
    if effort is None or effort not in EFFORTS:
        return tier, effort
    try:
        att = int(attempt) if attempt is not None else 1
    except (ValueError, TypeError):
        att = 1
    if att < 1:
        att = 1
    start = EFFORTS.index(effort)
    idx = start + (att - 1)
    if idx > 2:
        if tier is not None:
            tier = "plan"
        idx = min(idx, 2)
    return tier, EFFORTS[idx]


def pick_tier_effort(run, ticket, stage, attempt):
    """Ask Ollama for model tier (cheap/plan) and effort (low/medium/high) for a stage call.

    Evaluates ticket metadata and attempt count against model_tier and effort confidence
    thresholds. Returns {'tier': str|None, 'effort': str|None, 'source': 'ollama'|'none'}.
    """
    cfg = getattr(run, "cfg", None) or {}
    t = ticket if isinstance(ticket, dict) else {}
    title = t.get("title", "") if isinstance(ticket, dict) else str(ticket or "")
    type_ = t.get("type", "")
    desc = t.get("description", "")
    design = t.get("design", "")
    steps = t.get("steps") or []
    n_steps = len(steps) if isinstance(steps, list) else 0
    risks = t.get("risks") or []
    risks_str = "; ".join(str(r) for r in risks) if isinstance(risks, list) else str(risks)
    files_hint = t.get("files_hint") or []
    n_files = len(files_hint) if isinstance(files_hint, list) else 0

    state_parts = [
        "Title: %s" % title,
        "Type: %s" % type_,
        "Stage: %s, attempt %s" % (stage, attempt if attempt is not None else 1),
    ]
    if desc:
        state_parts.append("Description: %s" % desc)
    if design:
        state_parts.append("Design: %s" % design)
    state_parts.append("Steps count: %d" % n_steps)
    if risks_str:
        state_parts.append("Risks: %s" % risks_str)
    state_parts.append("Files hint count: %d" % n_files)
    state = "\n".join(state_parts)[:STATE_CHARS]

    q = {
        "tier": {
            "type": "choice",
            "instructions": "What model tier is appropriate?",
            "criteria": {
                "cheap": "routine or straightforward work",
                "plan": "complex reasoning or planning work",
            },
        },
        "effort": {
            "type": "choice",
            "instructions": "What reasoning effort is appropriate?",
            "criteria": {
                "low": "minimal effort",
                "medium": "moderate effort",
                "high": "thorough reasoning",
            },
        },
    }

    key_points = [
        ("tier", "model_tier", ("cheap", "plan")),
        ("effort", "effort", tuple(EFFORTS)),
    ]
    model = cfg.get("router", {}).get("model")

    try:
        answers = _ollama(cfg, state, q, timeout=10)
    except Exception as e:
        reason = "ollama failed: %s" % e
        for _, point, _ in key_points:
            thr = config.threshold(cfg, point)
            if run is not None:
                try:
                    run.event(
                        "router",
                        fn=point,
                        point=point,
                        source="none",
                        choice=None,
                        confidence=None,
                        threshold=thr,
                        fallback_reason=reason,
                        model=model,
                    )
                except Exception:
                    pass
        return {"tier": None, "effort": None, "source": "none"}

    picked = {"tier": None, "effort": None}
    for key, point, allowed in key_points:
        thr = config.threshold(cfg, point)
        ans = answers.get(key) if isinstance(answers, dict) else None
        choice = ans.get("choice") if isinstance(ans, dict) else None
        conf = ans.get("confidence") if isinstance(ans, dict) else None
        reason = None
        pt_source = "none"
        if not isinstance(ans, dict) or choice is None:
            reason = "ollama result missing choice"
        elif choice not in allowed:
            reason = "ollama choice %r not in %s" % (choice, allowed)
        elif isinstance(conf, bool) or not isinstance(conf, (int, float)) or not (conf >= thr):
            if isinstance(conf, bool) or not isinstance(conf, (int, float)):
                reason = "ollama confidence none < %.2f" % thr
            else:
                reason = "ollama confidence %.2f < %.2f" % (conf, thr)
        else:
            pt_source = "ollama"
            picked[key] = choice

        if run is not None:
            try:
                run.event(
                    "router",
                    fn=point,
                    point=point,
                    source=pt_source,
                    choice=str(picked[key])[:200] if picked[key] is not None else None,
                    confidence=conf,
                    threshold=thr,
                    fallback_reason=reason,
                    model=model,
                )
            except Exception:
                pass

    source = "ollama" if (picked["tier"] is not None or picked["effort"] is not None) else "none"
    return {"tier": picked["tier"], "effort": picked["effort"], "source": source}


def assert_check(run, claim, evidence, adapter=None):
    state = ("Evidence:\n%s" % evidence)[-STATE_CHARS:]

    def parse(ans):
        p = ans["a"]["noul"]
        return {"answer": p >= 0.5, "confidence": max(p, 1 - p), "reason": "ollama"}, max(p, 1 - p)

    q = {"a": {"type": "noul", "instructions": "Is this true? " + claim}}
    prompt = "Is this claim true given the evidence?\nClaim: %s\n\n%s" % (claim, evidence[-STATE_CHARS:])
    # unknown -> escalate: default has answer False with confidence 0 and reason "unknown"
    return _decide(run, "assert_check", state, q, parse, prompt,
                   {"answer": False, "confidence": 0.0, "reason": "unknown"}, adapter)


def summarize(run, text, adapter=None):
    out = _agent(run, "summary", "Summarize in at most 5 lines:\n\n" + text[-STATE_CHARS:], adapter)
    if out:
        return _log(run, "summarize", _out(out["text"], "fallback", run.cfg["router"]["llm_model"], None))
    return _log(run, "summarize", _out(text[:500], "default", None, None, "agent failed"))


def commit_message(run, ticket, diffstat="", adapter=None):
    out = _agent(run, "commit_message", "Write a git commit message (subject <= 72 chars) for ticket:\n%s\n\n%s"
                 % (_ticket_text(ticket), diffstat[-STATE_CHARS:]), adapter)
    if out:
        return _log(run, "commit_message", _out(out, "fallback", run.cfg["router"]["llm_model"], None))
    return _log(run, "commit_message", _out({"subject": "%s: %s" % (ticket["id"], ticket["title"]), "body": ""},
                                            "default", None, None, "agent failed"))


def route_ticket(cfg, ticket):
    """Deterministic: stages for the ticket type, each with agent + optional model/effort overrides."""
    route = cfg["routing"][ticket["type"]]
    over = route.get("overrides", {})
    stages = []
    for st in route["stages"]:
        s = {"stage": st, "agent": config.agent_for(cfg, st)}
        s.update(over.get(st, {}))
        stages.append(s)
    return {"type": ticket["type"], "stages": stages}


def health(cfg):
    """{ollama: reachable, model: pulled} without raising."""
    r = cfg["router"]
    try:
        with urllib.request.urlopen(r["ollama_url"].rstrip("/") + "/api/tags", timeout=3) as resp:
            names = [m["name"] for m in json.load(resp)["models"]]
        return {"ollama": True, "model": r["model"] in names}
    except Exception:
        return {"ollama": False, "model": False}
