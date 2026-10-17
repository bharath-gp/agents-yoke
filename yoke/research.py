"""Standalone `yoke search|research|factcheck` (PLAN M10): the same stages as the `research` ticket
type, run directly in the repo checkout. The doc is left uncommitted; the runner never commits here."""
import os
import re
import sys

from . import gitops, pipeline, planreview

CMDS = ("search", "research", "factcheck")


def add_parsers(sub):
    depths = ("quick", "normal", "deep")
    s = sub.add_parser("search", help="web search agent: ranked sources for a query")
    s.add_argument("query")
    s.add_argument("--depth", choices=depths)
    r = sub.add_parser("research", help="search, research, fact-check loop; writes docs/research/<topic>.md")
    r.add_argument("topic")
    r.add_argument("--depth", choices=depths)
    f = sub.add_parser("factcheck", help="fact-check a markdown file in this repo")
    f.add_argument("file")


def new_run(repo, depth=None):
    """A run only for audit records; kind=research keeps it out of `yoke status` and the hook."""
    run = planreview.init_run(repo, None, {"research": {"default_depth": depth}} if depth else None)
    run.update(kind="research", state="done")
    return run


def _ticket(title, doc_path=None):
    return {"id": "T-000", "title": title, "type": "research", "description": "", "exit_criteria": ["done"],
            "depends_on": [], "files_hint": [], "plan_excerpt": "", "doc_path": doc_path}


def drive(run, ticket, stages, attempts=None, adapter_for=None, env=None):
    """Attempt loop over `stages` in the repo. Returns (ok, results, feedback)."""
    cfg, repo = run.cfg, run.read_json("run.json")["repo"]
    adapter_for = adapter_for or pipeline.default_adapter_for(cfg)
    limit = attempts or cfg["max_attempts_per_ticket"]
    st = {"status": "in_progress", "attempt": 1, "sessions": {}, "base_sha": gitops.head_sha(repo)}
    while True:
        results = {}
        try:
            fb, results, _, _ = pipeline._attempt(run, adapter_for, env, ticket, st, repo, stages)
        except pipeline.Escalate as e:
            return False, results, str(e)
        if fb is None:
            return True, results, ""
        if st["attempt"] + 1 > limit:
            return False, results, fb
        st["attempt"] += 1
        pipeline.write_feedback(run, ticket["id"], st["attempt"], fb)


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "topic"


def dispatch(args):
    from . import cli
    repo = cli._git_toplevel(os.getcwd())
    run = new_run(repo, getattr(args, "depth", None))
    try:
        if args.cmd == "search":
            ok, res, fb = drive(run, _ticket(args.query), ["search"])
            for s in res.get("search", {}).get("sources", []) if ok else []:
                print("%s  %s\n  %s" % (s["url"], s["title"], s["relevance"]))
        elif args.cmd == "research":
            doc = "%s/%s.md" % (run.cfg["research"]["doc_dir"], slug(args.topic))
            os.makedirs(os.path.join(repo, run.cfg["research"]["doc_dir"]), exist_ok=True)
            ok, res, fb = drive(run, _ticket(args.topic, doc), ["search", "research", "factcheck"])
            if ok:
                cl = res["factcheck"]["claims"]
                print("%s: %d of %d claims verified" % (doc, sum(c["verdict"] == "supported" for c in cl), len(cl)))
        else:
            rel = os.path.relpath(os.path.realpath(args.file), repo)
            if rel.startswith("..") or not os.path.isfile(os.path.join(repo, rel)):
                print("yoke: %s is not a file inside %s" % (args.file, repo), file=sys.stderr)
                return 1
            ok, res, fb = drive(run, _ticket(rel, rel), ["factcheck"], attempts=1)
            if ok:  # on failure the stderr report below already lists every bad claim
                for c in res["factcheck"]["claims"]:
                    if c["verdict"] == "unverifiable":
                        print("%s unverifiable: %s" % (c["id"], c["claim"]))
                print("fact check passed")
    except pipeline.BudgetExhausted:
        print("yoke: max_agent_calls_total reached", file=sys.stderr)
        return 1
    if not ok:
        print("yoke: %s failed:\n%s" % (args.cmd, fb), file=sys.stderr)
        return 2
    return 0
