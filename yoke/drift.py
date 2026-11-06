"""yoke drift: plan-vs-diff drift check."""
import os
import re
import string

from . import adapters, calls, config, gitops, planreview, schema, state

DRIFT_DIFF_MAX = 200000

PKG = os.path.dirname(os.path.abspath(__file__))


def work_items_text(plan):
    """Extract text from '^## Work items' (re.M) up to the next '^## ' heading; whole plan when absent."""
    m = re.search(r"^## Work items", plan, re.M)
    if not m:
        return plan
    rest = plan[m.end():]
    next_m = re.search(r"^## ", rest, re.M)
    if next_m:
        return plan[m.start():m.end() + next_m.start()]
    return plan[m.start():]


def _record(base_sha, branch, summary="", drift=None, error=None):
    return {
        "base_sha": base_sha,
        "branch": branch,
        "created_at": state.now(),
        "summary": summary,
        "drift": drift if drift is not None else [],
        "error": error,
    }


def check_drift(run, adapter=None, env=None):
    """Run one read-only agent call over plan work items and git diff, persisting drift.json."""
    plan = planreview._read(run, "plan.md")
    info = run.read_json("run.json")
    repo = info["repo"]
    branch = info["branch"]
    base_sha = info["base_sha"]

    try:
        diff = gitops.git(repo, "diff", "%s..%s" % (base_sha, branch)).stdout
    except gitops.GitError as e:
        rec = _record(base_sha, branch, summary="", drift=[], error=str(e))
        run.write_json("drift.json", rec)
        run.event("drift", entries=0, ok=False)
        return rec

    cap = DRIFT_DIFF_MAX
    if len(diff) > cap:
        diff = diff[:cap] + ("\n... (diff truncated at %d chars)" % cap)

    prompt_path = os.path.join(PKG, "prompts", "drift.md")
    with open(prompt_path) as f:
        template = string.Template(f.read())
    prompt = template.safe_substitute(
        work_items=work_items_text(plan),
        diff=diff,
        base_sha=base_sha,
        branch=branch,
    )

    cfg = run.cfg
    role = "drift"
    adapter = adapter or adapters.get(config.agent_for(cfg, role), cfg=cfg)
    spec = adapters.CallSpec(
        prompt=prompt,
        schema_path=schema.schema_path("drift"),
        cwd=repo,
        mode="read",
        timeout_s=cfg["timeouts_s"][role],
        env=env or {},
        setting_sources=((cfg.get("agents") or {}).get("claude") or {}).get("setting_sources", "project,local"),
    )

    try:
        res = calls.run_call(run, adapter, spec, stage=role, attempt=1, label="drift", budget=True)
    except calls.BudgetExhausted:
        rec = _record(base_sha, branch, summary="", drift=[], error="budget exhausted")
        run.write_json("drift.json", rec)
        run.event("drift", entries=0, ok=False)
        return rec

    is_ok = res.ok and res.schema_valid is not False and isinstance(res.structured_output, dict)
    if not is_ok:
        err = "schema invalid" if (res.schema_valid is False or (res.error and res.error.startswith("schema:"))) else (res.error or "schema invalid")
        rec = _record(base_sha, branch, summary="", drift=[], error=err)
        run.write_json("drift.json", rec)
        run.event("drift", entries=0, ok=False)
        return rec

    out = res.structured_output
    drift_items = out.get("drift") or []
    summary = out.get("summary") or ""
    rec = _record(base_sha, branch, summary=summary, drift=drift_items, error=None)
    run.write_json("drift.json", rec)

    if drift_items:
        lines = ["- [%s] %s: %s" % (item.get("kind"), item.get("work_item"), item.get("detail")) for item in drift_items]
        section = "\n## Plan drift (%s)\n" % state.now() + "\n".join(lines) + "\n"
    else:
        section = "\n## Plan drift (%s)\nNo drift found.\n" % state.now()

    try:
        with open(run.path("map.md"), "a") as f:
            f.write(section)
    except OSError:
        pass

    run.event("drift", entries=len(drift_items), ok=True)
    return rec


def pr_section(run, limit=None):
    """Return markdown section for the PR body from drift.json, or '' when missing/errored."""
    try:
        rec = run.read_json("drift.json")
    except (FileNotFoundError, OSError, ValueError):
        return ""
    if not isinstance(rec, dict) or rec.get("error"):
        return ""
    drift_items = rec.get("drift") or []
    if drift_items:
        lines = ["- [%s] %s: %s" % (item.get("kind"), item.get("work_item"), item.get("detail")) for item in drift_items]
        text = "## Plan drift\n\n" + "\n".join(lines)
    else:
        text = "## Plan drift\n\nNo drift found."
    if limit is not None and len(text) > limit:
        text = text[:limit] + "\n... (truncated; see drift.json)"
    return text
