"""Per-ticket state machine (PLAN 2.8): stages by ticket type, runner test rerun, feedback files,
close gate, commit and merge (2.9). Sequential unless the caller passes a shared merge_lock."""
import contextlib
import json
import os
import string
import subprocess

from . import adapters, calls, config, gitops, planreview, router, schema, state

PKG = os.path.dirname(os.path.abspath(__file__))


class Escalate(Exception):
    pass


class BudgetExhausted(Exception):
    pass


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


def _call(run, adapter_for, env, ticket, stage, attempt, wt, base, mode, results, feedback, resume=None):
    cfg = run.cfg
    if run.read_json("run.json")["agent_calls"] >= cfg["max_agent_calls_total"]:
        raise BudgetExhausted()
    with open(os.path.join(PKG, "prompts", stage + ".md")) as f:
        prompt = string.Template(f.read()).safe_substitute(
            ticket=json.dumps(ticket, indent=2), plan_excerpt=ticket["plan_excerpt"],
            prior_results=json.dumps(results, indent=2), feedback=feedback or "(none)",
            base_sha=base, worktree=wt)
    over = next((s for s in router.route_ticket(cfg, ticket)["stages"] if s["stage"] == stage), {})
    hooks = os.path.join(PKG, "githooks")  # push protection for workers (PLAN 2.9)
    wenv = dict(env or {}, GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.hooksPath", GIT_CONFIG_VALUE_0=hooks)
    spec = adapters.CallSpec(
        prompt=prompt, schema_path=schema.schema_path(stage), cwd=wt, mode=mode,
        model=over.get("model"), effort=over.get("effort"), resume_session=resume,
        timeout_s=cfg["timeouts_s"][stage], env=wenv,
        allowed_tools=tuple(cfg["agents"]["claude"]["write_allowed_tools"]))
    return calls.run_call(run, adapter_for(stage), spec, stage=stage, ticket=ticket["id"], attempt=attempt)


def _rerun(run, cmd, wt):
    """Runner's independent check: (exit_code, output tail)."""
    try:
        r = subprocess.run(cmd, shell=True, cwd=wt, capture_output=True, text=True,
                           timeout=run.cfg["timeouts_s"]["qa"])  # ponytail: no process-group kill
        return r.returncode, (r.stdout + r.stderr)[-2000:]
    except subprocess.TimeoutExpired:
        return -1, "timed out"


def _attempt(run, adapter_for, env, ticket, st, wt, stages):
    """Run the stages once. Returns (feedback or None, results, test_cmd, qa_rc)."""
    tid, attempt, base = ticket["id"], st["attempt"], st["base_sha"]
    feedback = _read_feedback(run, tid, attempt)
    since = gitops.head_sha(wt)
    results, test_cmd, rerun_rc = {}, None, None
    for stage in stages:
        mode = "read" if stage == "validate" else "write"
        if stage == "validate":
            gitops.git(wt, "add", "-A")  # new files become visible to `git diff <base>`
        res = _call(run, adapter_for, env, ticket, stage, attempt, wt, base, mode, results, feedback,
                    resume=st["sessions"].get("code") if stage == "code" and attempt > 1 else None)
        if not res.ok:
            return "%s call failed: %s" % (stage, res.error), results, test_cmd, rerun_rc
        out = results[stage] = res.structured_output
        if stage == "code":
            if res.session_id:
                st["sessions"]["code"] = res.session_id
                save_state(run, tid, st)
            if gitops.agent_made_commits(wt, since):
                run.event("agent_commit", ticket=tid, attempt=attempt)
                gitops.undo_commits(wt, since)
            if out["status"] == "blocked":
                raise Escalate("coder blocked: %s" % out.get("blocked_reason", "no reason given"))
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
        elif stage == "validate":
            unmet = [c["criterion"] for c in out["exit_criteria"] if not c["met"]]
            if out["verdict"] != "approve" or unmet:
                parts = [out["feedback_for_coder"]] + ["Unmet criterion: " + c for c in unmet]
                parts += ["%s %s: %s" % (i["severity"], i["file"], i["description"]) for i in out["issues"]]
                return "\n".join(p for p in parts if p), results, test_cmd, rerun_rc
    return None, results, test_cmd, rerun_rc


def _gate(stages, results, rerun_rc):
    g = {}
    if "code" in stages:
        g["code"] = results["code"]["status"] == "done"
    if "qa" in stages:
        g["qa"] = results["qa"]["status"] == "pass" and rerun_rc == 0
    if "validate" in stages:
        v = results["validate"]
        g["validate"] = v["verdict"] == "approve" and all(c["met"] for c in v["exit_criteria"])
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
            return "Rebase conflict on files %s; rebase onto %s and resolve." % (", ".join(files), info["branch"])
        if test_cmd and gitops.head_sha(wt) != before:
            rc, tail = _rerun(run, test_cmd, wt)
            if rc != 0:
                return "After rebase, `%s` exited %s:\n%s" % (test_cmd, rc, tail)
        gitops.ff_merge(run.path("worktrees/_integration"), info["branch"] + "-" + tid)
    st["commit"] = gitops.head_sha(wt)
    return None


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
                st["status"] = "closed"
                save_state(run, tid, st)
                gitops.remove_worktree(info["repo"], wt)
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
