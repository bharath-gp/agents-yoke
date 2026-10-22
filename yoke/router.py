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
