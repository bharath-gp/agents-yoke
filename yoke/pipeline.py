"""Per-ticket state machine (PLAN 2.8): stages by ticket type, runner test rerun, feedback files,
close gate, commit and merge (2.9). Sequential unless the caller passes a shared merge_lock."""
import contextlib
import json
import os
import re
import string
import subprocess

from . import adapters, calls, config, gitops, milestones, planreview, router, schema, state, tracker, usage

PKG = os.path.dirname(os.path.abspath(__file__))


READ_STAGES = ("validate", "review", "search", "factcheck")
WEB_STAGES = ("search", "factcheck")


class Escalate(Exception):
    pass


class BudgetExhausted(Exception):
    def __init__(self, reason="budget exhausted: max_agent_calls_total reached"):
        super().__init__(reason)
        self.reason = reason


def default_adapter_for(cfg):
    def adapter_for(role):
        if role == "router":
            return adapters.get(cfg["router"]["fallback"]["agent"])
        return adapters.get(config.agent_for(cfg, role))
    return adapter_for


def ticket_state(run, tid):
    return run.read_json("tickets/%s/state.json" % tid,
                         {"status": "pending", "attempt": 1, "sessions": {}})


def save_state(run, tid, st):
    run.write_json("tickets/%s/state.json" % tid, st)
    run.event("ticket_state", ticket=tid, status=st["status"], attempt=st["attempt"])


def escalate(run, tid, reason):
    with state._LOCK:
        esc = run.read_json("escalations.json", [])
        esc.append({"id": len(esc) + 1, "ticket": tid, "reason": reason, "at": state.now()})
        run.write_json("escalations.json", esc)
    run.event("escalation", ticket=tid, reason=reason)
    if tid:
        tracker.comment(run, tid, "yoke escalation: " + reason)


def _feedback_path(run, tid, attempt):
    return run.path("tickets/%s/attempt-%d/feedback.md" % (tid, attempt))


def write_feedback(run, tid, attempt, text, append=False):
    p = _feedback_path(run, tid, attempt)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a" if append else "w") as f:
        f.write(text.rstrip() + "\n\n")


def _read_feedback(run, tid, attempt):
    try:
        with open(_feedback_path(run, tid, attempt)) as f:
            return f.read()
    except FileNotFoundError:
        return ""


def doc_path_for(cfg, ticket):
    """Repo-relative path of a research ticket's document (ticket["doc_path"] overrides)."""
    if ticket.get("doc_path"):
        return ticket["doc_path"]
    slug = re.sub(r"[^a-z0-9]+", "-", ("%s %s" % (ticket["id"], ticket["title"])).lower()).strip("-")[:60]
    return "%s/%s.md" % (cfg["research"]["doc_dir"], slug)


def _vars(cfg, ticket):
    depth = cfg["research"]["depths"][cfg["research"]["default_depth"]]
    return dict(topic="\n".join(x for x in (ticket["title"], ticket.get("description")) if x),
                doc_path=doc_path_for(cfg, ticket), search_tools=", ".join(cfg["search"]["tools"]),
                max_sources=depth["max_sources"], search_rounds=depth["search_rounds"])


def factcheck_failures(out):
    """Problems the runner itself finds in a factcheck result (it does not trust `verdict` alone)."""
    bad = ["%s %s: %s -> %s" % (c["id"], c["verdict"], c["claim"], c["correction"] or c["evidence"])
           for c in out["claims"] if c["verdict"] in ("refuted", "unsupported")]
    bad += ["source %s %s: %s" % (s["url"], s["status"], s["note"])
            for s in out["sources"] if s["status"] in ("dead", "mismatch")]
    if not out["claims"]:
        bad.append("the fact checker returned no claims, so nothing was checked")
    return bad


def _call(run, adapter_for, env, ticket, stage, attempt, wt, base, mode, results, feedback, resume=None, label=None):
    cfg = run.cfg
    if run.read_json("run.json")["agent_calls"] >= cfg["max_agent_calls_total"]:
        raise BudgetExhausted()
    exceeded, reason = config.check_budget(run)
    if exceeded:
        raise BudgetExhausted(reason)
    dest, oos, _ = milestones.destination_and_scope(run, ticket)
    with open(os.path.join(PKG, "prompts", stage + ".md")) as f:
        prompt = string.Template(f.read()).safe_substitute(
            ticket=json.dumps(ticket, indent=2), plan_excerpt=ticket["plan_excerpt"],
            destination=dest, out_of_scope=oos,
            prior_results=json.dumps(results, indent=2), feedback=feedback or "(none)",
            base_sha=base, worktree=wt, **_vars(cfg, ticket))
    over = next((s for s in router.route_ticket(cfg, ticket)["stages"] if s["stage"] == stage), {})
    hooks = os.path.join(PKG, "githooks")  # push protection for workers (PLAN 2.9)
    wenv = dict(env or {}, GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.hooksPath", GIT_CONFIG_VALUE_0=hooks)
    spec = adapters.CallSpec(
        prompt=prompt, schema_path=schema.schema_path(stage), cwd=wt, mode=mode,
        model=over.get("model"), effort=over.get("effort"), resume_session=resume,
        timeout_s=cfg["timeouts_s"][stage], env=wenv,
        allowed_tools=tuple(cfg["agents"]["claude"]["write_allowed_tools"]),
        web_tools=tuple(cfg["search"]["tools"]) if stage in WEB_STAGES else ())
    return calls.run_call(run, adapter_for(stage), spec, stage=stage, ticket=ticket["id"], attempt=attempt, label=label)


def _call_retry(run, adapter_for, env, ticket, stage, attempt, *args, **kw):
    """_call, plus one in-place retry when the call itself failed (denial, timeout, bad output).
    A failed call changed no code, so rerunning the stage beats restarting the whole attempt."""
    res = _call(run, adapter_for, env, ticket, stage, attempt, *args, **kw)
    if res.ok:
        return res
    run.event("stage_retry", ticket=ticket["id"], attempt=attempt, stage=stage, error=res.error)
    kw["label"] = (kw.get("label") or stage) + "-retry"
    return _call(run, adapter_for, env, ticket, stage, attempt, *args, **kw)


def _rerun(run, cmd, wt):
    """Runner's independent check: (exit_code, output tail)."""
    try:
        r = subprocess.run(cmd, shell=True, cwd=wt, capture_output=True, text=True,
                           timeout=run.cfg["timeouts_s"]["qa"])  # ponytail: no process-group kill
        return r.returncode, (r.stdout + r.stderr)[-2000:]
    except subprocess.TimeoutExpired:
        return -1, "timed out"


def _post_code(run, ticket, st, wt, since, res):
    """Save coder session id, revert unexpected agent commits, and check blocked status."""
    tid, attempt = ticket["id"], st["attempt"]
    if res.session_id:
        st["sessions"]["code"] = res.session_id
        save_state(run, tid, st)
    if gitops.agent_made_commits(wt, since):
        run.event("agent_commit", ticket=tid, attempt=attempt)
        gitops.undo_commits(wt, since)
    out = res.structured_output
    if out.get("status") == "blocked":
        raise Escalate("coder blocked: %s" % out.get("blocked_reason", "no reason given"))


def _format_findings(findings, header=None):
    """Format review findings with severities, file locations, and suggestions for feedback."""
    lines = []
    if header:
        lines.append(header)
    for f in findings:
        loc = ("%s:%s" % (f.get("file"), f.get("line"))) if f.get("file") else ""
        hdr = " ".join(x for x in [("[%s]" % f["severity"]) if f.get("severity") else "", loc] if x)
        detail = f.get("comment", "")
        if f.get("suggestion"):
            detail += "\nsuggestion: %s" % f["suggestion"]
        lines.append("%s: %s" % (hdr, detail) if hdr else detail)
    return "\n".join(lines)


def _review_loop(run, adapter_for, env, ticket, st, wt, base, results, feedback):
    """Run iterative review loop with resumed coder revisions up to max_review_rounds."""
    tid, attempt = ticket["id"], st["attempt"]
    cfg = run.cfg
    max_rounds = cfg.get("max_review_rounds", 2)
    n = 0
    while True:
        label = "review" if n == 0 else ("review-r%d" % n)
        rev_res = _call_retry(run, adapter_for, env, ticket, "review", attempt, wt, base, "read",
                              results, feedback, label=label)
        if not rev_res.ok:
            return "review call failed: %s" % rev_res.error
        out = results["review"] = rev_res.structured_output
        findings = out.get("findings", [])
        blocking = [f for f in findings if f.get("severity") in ("blocker", "major")]
        if not blocking:
            st["review"]["rounds"] = n
            st["review"]["unresolved"] = []
            save_state(run, tid, st)
            return None
        if n >= max_rounds:
            st["review"]["rounds"] = n
            st["review"]["unresolved"] = blocking
            save_state(run, tid, st)
            run.event("review_unresolved", ticket=tid, attempt=attempt, findings=blocking, rounds=n)
            tracker.comment(run, tid, "Unresolved review findings:\n" + _format_findings(blocking))
            return None
        n += 1
        st["review"]["rounds"] = n
        save_state(run, tid, st)
        fb_parts = [_format_findings(blocking, "Required review fixes:")]
        suggestions = [f for f in findings if f.get("severity") in ("minor", "nit")]
        if suggestions:
            fb_parts.append(_format_findings(suggestions, "Optional suggestions:"))
        # kept out of feedback.md: that file is the previous attempt's feedback and must survive a re-run
        coder_fb = "\n\n".join(p for p in [feedback] + fb_parts if p)
        since = gitops.head_sha(wt)
        coder_res = _call(run, adapter_for, env, ticket, "code", attempt, wt, base, "write",
                          results, coder_fb, resume=st["sessions"].get("code"), label="code-r%d" % n)
        if not coder_res.ok:
            return "code call failed: %s" % coder_res.error
        results["code"] = coder_res.structured_output
        _post_code(run, ticket, st, wt, since, coder_res)
        gitops.git(wt, "add", "-A")
        feedback = coder_fb  # the re-review sees what the coder was asked to fix


def _attempt(run, adapter_for, env, ticket, st, wt, stages):
    """Run the stages once. Returns (feedback or None, results, test_cmd, qa_rc)."""
    tid, attempt, base = ticket["id"], st["attempt"], st["base_sha"]
    if "review" in stages:
        st["review"] = {"rounds": 0, "unresolved": []}
        save_state(run, tid, st)
    feedback = _read_feedback(run, tid, attempt)
    since = gitops.head_sha(wt)
    results, test_cmd, rerun_rc = {}, None, None
    for stage in stages:
        if stage in ("review", "validate"):
            gitops.git(wt, "add", "-A")  # new files become visible to `git diff <base>`
        if stage == "review":
            fb = _review_loop(run, adapter_for, env, ticket, st, wt, base, results, feedback)
            if fb:
                return fb, results, test_cmd, rerun_rc
            continue
        mode = "read" if stage in READ_STAGES else "write"
        call = _call if stage == "code" else _call_retry  # a failed code call restarts the attempt anyway
        res = call(run, adapter_for, env, ticket, stage, attempt, wt, base, mode, results, feedback,
                   resume=st["sessions"].get("code") if stage == "code" and attempt > 1 else None)
        if not res.ok:
            return "%s call failed: %s" % (stage, res.error), results, test_cmd, rerun_rc
        out = results[stage] = res.structured_output
        if stage == "code":
            _post_code(run, ticket, st, wt, since, res)
        elif stage == "qa":
            if out["status"] == "blocked":
                raise Escalate("QA blocked: %s" % out["summary"])
            if out["status"] == "fail":
                return "QA failed:\n" + "\n".join(out["failures"] or [out["summary"]]), results, test_cmd, rerun_rc
            test_cmd = run.cfg.get("test_command") or out["test_command"]
            rerun_rc, tail = _rerun(run, test_cmd, wt)
            if rerun_rc != 0:
                return ("Runner re-ran `%s` and it exited %s (QA had reported pass):\n%s"
                        % (test_cmd, rerun_rc, tail)), results, test_cmd, rerun_rc
        elif stage == "search":
            if out["status"] == "none" or not out["sources"]:
                return "Search found no sources: " + out["summary"], results, test_cmd, rerun_rc
        elif stage == "research":
            if out["status"] == "blocked":
                raise Escalate("researcher blocked: %s" % out.get("blocked_reason", "no reason given"))
            doc = doc_path_for(run.cfg, ticket)
            if not os.path.isfile(os.path.join(wt, doc)):
                return "The research document was not written to %s." % doc, results, test_cmd, rerun_rc
        elif stage == "factcheck":
            bad = factcheck_failures(out)
            if bad:
                return "Fact check failed:\n" + "\n".join(bad), results, test_cmd, rerun_rc
        elif stage == "validate":
            unmet = [c["criterion"] for c in out["exit_criteria"] if not c["met"]]
            if out["verdict"] != "approve" or unmet:
                parts = [out["feedback_for_coder"]] + ["Unmet criterion: " + c for c in unmet]
                parts += ["%s %s: %s" % (i["severity"], i["file"], i["description"]) for i in out["issues"]]
                return "\n".join(p for p in parts if p), results, test_cmd, rerun_rc
    return None, results, test_cmd, rerun_rc


def _gate(stages, results, rerun_rc):
    g = {}
    if "search" in stages:
        g["search"] = results["search"]["status"] == "found" and bool(results["search"]["sources"])
    if "code" in stages:
        g["code"] = results["code"]["status"] == "done"
    if "qa" in stages:
        g["qa"] = results["qa"]["status"] == "pass" and rerun_rc == 0
    if "validate" in stages:
        v = results["validate"]
        g["validate"] = v["verdict"] == "approve" and all(c["met"] for c in v["exit_criteria"])
    if "research" in stages:
        g["research"] = results["research"]["status"] == "done"
    if "factcheck" in stages:
        g["factcheck"] = not factcheck_failures(results["factcheck"])
    if "docs" in stages:
        g["docs"] = results["docs"]["status"] in ("done", "not_needed")
    return g


def _close(run, adapter_for, ticket, st, wt, stages, results, test_cmd, rerun_rc, merge_lock):
    """Gate, commit, rebase, retest if moved, ff-merge. Returns feedback on a retryable failure."""
    tid, info = ticket["id"], run.read_json("run.json")
    gate = _gate(stages, results, rerun_rc)
    run.event("close_gate", ticket=tid, attempt=st["attempt"], gate=gate, ok=all(gate.values()))
    if not all(gate.values()):
        return "Close gate failed: " + ", ".join(k for k, v in gate.items() if not v)
    gitops.git(wt, "add", "-A")
    stat = gitops.git(wt, "diff", "--cached", "--stat").stdout
    msg = router.commit_message(run, ticket, stat, adapter_for("router"))["result"]
    subject = msg["subject"] + ("\n\n" + msg["body"] if msg.get("body") else "")
    gitops.commit_all(wt, subject, tid, amend=bool(st.get("commit")))
    with merge_lock:
        before = gitops.head_sha(wt)
        ok, files = gitops.rebase_onto(wt, info["branch"])
        if not ok:
            tip = gitops.head_sha(run.path("worktrees/_integration"))
            files = gitops.reapply_on(wt, tip) or files
            st["base_sha"] = tip  # validate diffs against the new base
            st["commit"] = None
            save_state(run, tid, st)
            return ("Rebase conflict on files %s. Your changes were re-applied on the latest %s and the "
                    "conflict markers are in the working tree; resolve them keeping both sides' intent."
                    % (", ".join(files), info["branch"]))
        if test_cmd and gitops.head_sha(wt) != before:
            rc, tail = _rerun(run, test_cmd, wt)
            if rc != 0:
                return "After rebase, `%s` exited %s:\n%s" % (test_cmd, rc, tail)
        gitops.ff_merge(run.path("worktrees/_integration"), info["branch"] + "-" + tid)
    st["commit"] = gitops.head_sha(wt)
    return None


def _append_decision(run, ticket, results):
    tid = ticket["id"]
    val = results.get("validate")
    val_summary = val.get("summary") if isinstance(val, dict) else None
    if val_summary and str(val_summary).strip():
        map_line = "- %s %s: %s\n" % (tid, ticket["title"], str(val_summary).strip())
    else:
        map_line = "- %s %s\n" % (tid, ticket["title"])
    map_path = run.path("map.md")
    with state._LOCK:
        if os.path.exists(map_path):
            try:
                with open(map_path) as f:
                    content = f.read()
                if any(line.startswith("- %s " % tid) or line == ("- %s" % tid) for line in content.splitlines()):
                    return
            except OSError:
                pass
        try:
            with open(map_path, "a") as f:
                f.write(map_line)
        except OSError:
            pass


def run_ticket(run, ticket, adapter_for=None, env=None, merge_lock=None):
    """Drive one ticket to closed or blocked. Returns the final status; BudgetExhausted propagates."""
    cfg, info, tid = run.cfg, run.read_json("run.json"), ticket["id"]
    adapter_for = adapter_for or default_adapter_for(cfg)
    merge_lock = merge_lock or contextlib.nullcontext()
    wt = run.path("worktrees/" + tid)
    gitops.add_worktree(info["repo"], wt, info["branch"] + "-" + tid, info["branch"])
    st = ticket_state(run, tid)
    st["base_sha"] = st.get("base_sha") or gitops.head_sha(wt)
    st["status"] = "in_progress"
    save_state(run, tid, st)
    stages = cfg["routing"][ticket["type"]]["stages"]
    try:
        while True:
            fb, results, test_cmd, rc = _attempt(run, adapter_for, env, ticket, st, wt, stages)
            if fb is None:
                fb = _close(run, adapter_for, ticket, st, wt, stages, results, test_cmd, rc, merge_lock)
            if fb is None:
                _append_decision(run, ticket, results)
                st["status"] = "closed"
                save_state(run, tid, st)
                gitops.remove_worktree(info["repo"], wt)
                try:
                    usage.write_usage(run)
                except Exception as e:
                    run.event("usage_error", ticket=tid, error=str(e))
                return "closed"
            if st["attempt"] + 1 > cfg["max_attempts_per_ticket"]:
                raise Escalate("max attempts (%d) reached; last feedback:\n%s" % (st["attempt"], fb))
            st["attempt"] += 1
            write_feedback(run, tid, st["attempt"], fb)
            save_state(run, tid, st)
    except Escalate as e:
        st["status"] = "blocked"
        save_state(run, tid, st)
        escalate(run, tid, str(e))
        return "blocked"
