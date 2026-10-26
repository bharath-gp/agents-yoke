"""Front half: init, bounded plan-review rounds, hash-bound approvals (PLAN 2.3, M2)."""
import hashlib
import json
import os
import re
import shutil
import string

from . import adapters, calls, config, schema, state

PKG = os.path.dirname(os.path.abspath(__file__))
KINDS = ("plan", "tickets", "final")


class ApprovalError(Exception):
    pass


def init_run(repo, requirements=None, overrides=None, home=None):
    """Create a run for `repo`, snapshot config, copy requirements. Returns the Run."""
    repo = os.path.realpath(repo)
    run = state.Run.create(repo, config.load(repo, home=home, overrides=overrides), home=home)
    if requirements:
        shutil.copyfile(requirements, run.path("requirements.md"))
    return run


def _read(run, rel):
    try:
        with open(run.path(rel)) as f:
            return f.read()
    except FileNotFoundError:
        raise ApprovalError("%s missing in run %s" % (rel, run.run_id))


def current_hash(run, kind):
    if kind == "plan":
        return hashlib.sha256(_read(run, "plan.md").encode()).hexdigest()
    canon = json.dumps(json.loads(_read(run, "tickets.json")), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()


def check_approval(run, kind):
    """Raise ApprovalError unless `kind` (and everything it depends on) is approved at its
    current hash. Later commands (run, pr) call this."""
    deps = {"plan": None, "tickets": "plan", "final": "tickets"}
    if deps[kind]:
        check_approval(run, deps[kind])
    entry = run.read_json("approvals.json", {}).get(kind)
    if not entry:
        raise ApprovalError("%s not approved; run: yoke approve %s" % (kind, kind))
    what = "plan" if kind == "plan" else "tickets"  # final is bound to the tickets hash
    if entry["sha256"] != current_hash(run, what):
        raise ApprovalError("%s changed after approval; re-approve" % what)
    return entry


def approve(run, kind):
    """Record sha256 for `kind`. tickets needs plan approved+unchanged; final needs tickets."""
    if kind == "final":
        check_approval(run, "tickets")
    elif kind == "tickets":
        check_approval(run, "plan")
    entry = {"sha256": current_hash(run, "plan" if kind == "plan" else "tickets"), "at": state.now()}
    with state._LOCK:
        approvals = run.read_json("approvals.json", {})
        approvals[kind] = entry
        run.write_json("approvals.json", approvals)
    run.event("approved", approval=kind, sha256=entry["sha256"])
    return entry


_TOC_TITLE = re.compile(r"^(contents|table of contents)$", re.I)
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_ANCHOR = re.compile(r"<a\s+(?:id|name)=\"([^\"]+)\"")
_LINK = re.compile(r"\]\(#([^)\s]+)\)")


def _slug(title):
    """GitHub-style heading anchor."""
    return re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-")


def toc_errors(text):
    """Problems with the document's table of contents; [] when it is fine.

    The first `##` section must be titled Contents (or Table of contents), list at least one
    `[...](#anchor)` link, link every other `##` section, and every link must resolve to a
    heading slug or an `<a id="...">` anchor."""
    anchors, slugs_seen, sections = set(), {}, []  # sections: (title, its anchors)
    toc, in_toc, fence, pending = None, False, False, []
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
            continue
        if fence:
            continue
        pending += _ANCHOR.findall(line)
        anchors.update(_ANCHOR.findall(line))
        m = _HEADING.match(line)
        if not m:
            if in_toc:
                toc += _LINK.findall(line)
            if line.strip() and not _ANCHOR.search(line):  # an <a id> applies to the heading right below it
                pending = []
            continue
        level, title = len(m.group(1)), m.group(2)
        slug = _slug(title)
        n = slugs_seen.get(slug, 0)
        slugs_seen[slug] = n + 1
        slug = slug if n == 0 else "%s-%d" % (slug, n)
        anchors.add(slug)
        here, pending = pending + [slug], []
        if level <= 2 and in_toc:
            in_toc = False
        if level == 2 or (toc is None and level <= 3 and _TOC_TITLE.match(title)):
            if toc is None and not sections and _TOC_TITLE.match(title):
                toc, in_toc = [], True
            elif not _TOC_TITLE.match(title) and level == 2:
                sections.append((title, here))
    if toc is None:
        return ["no table of contents: the first ## section must be `## Contents` with links to every ## section"]
    errs = []
    if not toc:
        errs.append("table of contents has no [title](#anchor) links")
    errs += ["table of contents links to missing anchor #%s" % a for a in toc if a not in anchors]
    errs += ["table of contents does not link section '%s'" % t for t, ids in sections
             if not set(ids) & set(toc)]
    return errs


def next_round(run):
    d = run.path("plan-review")
    done = [n for n in os.listdir(d) if n.startswith("round-")] if os.path.isdir(d) else []
    return len(done) + 1


def review_round(run, adapter=None, env=None):
    """One secondary read-only review of plan.md. Returns (round, CallResult). Raises
    RuntimeError when max_plan_review_rounds is used up."""
    cfg = run.cfg
    n = next_round(run)
    if n > cfg["max_plan_review_rounds"]:
        raise RuntimeError("max_plan_review_rounds (%d) reached: arbitrate the findings in "
                           "plan-review/arbitration.md and ask the user" % cfg["max_plan_review_rounds"])
    plan = _read(run, "plan.md")
    try:
        requirements = _read(run, "requirements.md")
    except ApprovalError:
        requirements = "(none provided)"
    prior = ""
    if n > 1:
        prior = ("\nPrevious round results and the primary's arbitration are in the run "
                 "directory; the plan below is the current revision.\n")
    with open(os.path.join(PKG, "prompts", "plan_review.md")) as f:
        prompt = string.Template(f.read()).safe_substitute(
            requirements=requirements, plan=plan, round=n, prior=prior)
    role = "plan_review"
    spec = adapters.CallSpec(
        prompt=prompt, schema_path=os.path.join(schema.SCHEMA_DIR, "plan-review.json"),
        cwd=run.read_json("run.json")["repo"], mode="read",
        timeout_s=cfg["timeouts_s"][role], env=env or {},
        setting_sources=((cfg.get("agents") or {}).get("claude") or {}).get("setting_sources", "project,local"))
    adapter = adapter or adapters.get(config.agent_for(cfg, role))
    res = calls.run_call(run, adapter, spec, stage=role, attempt=n)
    run.event("plan_review", round=n, ok=res.ok and res.schema_valid is not False,
              plan_sha256=current_hash(run, "plan"))
    return n, res
