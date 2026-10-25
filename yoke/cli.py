"""yoke CLI: doctor, init, review-plan, approve, tickets, route; runner commands live in runcmds.py."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request

from . import config, milestones, models as models_mod, research, router, runcmds, state, tracker, tickets as tickets_mod

OLLAMA_URL = "http://127.0.0.1:11434"
DECISION_MODEL = "tev1:4b"  # /v1/systemone; M0 spike: 10/10 ticket types, ~0.75s
TOOLS = [("claude", True), ("agy", True), ("git", True), ("gh", False), ("ollama", False)]


def _version(exe):
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
        return (out.stdout or out.stderr).strip().splitlines()[0]
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return "version unknown"


AGY_SETTINGS = os.path.expanduser("~/.gemini/antigravity-cli/settings.json")


def _agy_reads_allowed():
    try:
        with open(AGY_SETTINGS) as f:
            allow = json.load(f).get("permissions", {}).get("allow", [])
    except (OSError, ValueError, AttributeError):
        return True  # unknown layout: do not nag
    return any(str(a).startswith("read_file") for a in allow)


def grant_agy_read(path=None):
    """Add `read_file` to agy's permissions.allow (idempotent). Returns True if the file changed."""
    path = path or AGY_SETTINGS
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    if not isinstance(data, dict) or not isinstance(data.get("permissions", {}), dict):
        raise ValueError("%s has an unexpected layout; add \"read_file\" to permissions.allow by hand" % path)
    allow = data.setdefault("permissions", {}).setdefault("allow", [])
    if any(str(a).startswith("read_file") for a in allow):
        return False
    allow.append("read_file")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    state._write_atomic(path, json.dumps(data, indent=2) + "\n")
    return True


def _ollama_models():
    try:
        with urllib.request.urlopen(OLLAMA_URL + "/api/tags", timeout=3) as r:
            return [m["name"] for m in json.load(r)["models"]]
    except Exception:
        return None


def _live(exe, args):
    try:
        r = subprocess.run([exe] + args, capture_output=True, text=True, timeout=120)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def doctor(live=False, out=print):
    """Return list of problems (required-tool failures only); print a report."""
    problems = []
    py = sys.version_info
    out("python %d.%d.%d %s" % (py[0], py[1], py[2], "ok" if py >= (3, 9) else "FAIL (need >= 3.9)"))
    if py < (3, 9):
        problems.append("python")
    for name, required in TOOLS:
        path = shutil.which(name)
        if path:
            out("%-7s ok   %s (%s)" % (name, _version(name), path))
        else:
            out("%-7s %s" % (name, "MISSING (required)" if required else "missing (optional)"))
            if required:
                problems.append(name)
    if shutil.which("agy") and not _agy_reads_allowed():
        out("agy read_file not in permissions.allow (headless reads get auto-denied, wasting calls): "
            "add \"read_file\" to permissions.allow in %s" % AGY_SETTINGS)
    models = _ollama_models()
    if models is None:
        out("ollama server not reachable at %s (router falls back to claude)" % OLLAMA_URL)
    else:
        for m in (DECISION_MODEL,):
            out("ollama model %s %s" % (m, "ok" if m in models else "not pulled: ollama pull " + m))
    if live:
        for name, args in (("claude", ["-p", "Reply ok", "--output-format", "json"]),
                           ("agy", ["-p", "Reply ok", "--output-format", "json"])):
            ok = shutil.which(name) and _live(name, args)
            out("live %-6s %s" % (name, "ok" if ok else "FAIL"))
            if not ok:
                problems.append("live " + name)
    return problems


def setup_markers():
    """(user marker, marker for the cwd repo); project setup is keyed by repo path, outside the repo."""
    home = config.yoke_home()
    key = hashlib.sha1(_git_toplevel(os.getcwd()).encode()).hexdigest()[:12]
    return os.path.join(home, "setup.json"), os.path.join(home, "projects", key + ".json")


def setup_cmd(args):
    """`--check`: exit 1 with a hint until setup passed for the user or this repo. Else run doctor, record it.

    Prints configured roles and resolved agents. Warns if an explicit review override
    matches the code agent.
    """
    user, project = setup_markers()
    if args.check:
        if os.path.exists(user) or os.path.exists(project):
            return 0
        print("yoke is not set up: run /yoke:setup")
        return 1
    bad = [m for m in args.map if "=" not in m]
    if bad:
        print("setup: --map expects ROLE=AGENT, got %s" % ", ".join(bad), file=sys.stderr)
        return 2
    problems = doctor(live=args.live)
    if problems:
        print("setup incomplete, fix: %s" % ", ".join(problems), file=sys.stderr)
        return 1
    if args.grant_agy_read:  # the /yoke:setup skill passes this only after the user said yes
        try:
            changed = grant_agy_read()
        except (OSError, ValueError) as e:
            print("setup: %s" % e, file=sys.stderr)
            return 2
        print("agy read_file %s in %s" % ("granted" if changed else "already allowed", AGY_SETTINGS))
    if args.primary or args.secondary or args.map:
        try:
            mapping = dict(m.split("=", 1) for m in args.map)
            path = (os.path.join(config.yoke_home(), "config.json") if args.scope == "user"
                    else os.path.join(_git_toplevel(os.getcwd()), ".yoke.json"))
            config.save_roles(path, args.primary, args.secondary, mapping)
        except ValueError as e:
            print("setup: %s" % e, file=sys.stderr)
            return 2
        print("agent roles saved to %s" % path)
    cfg = config.load(repo=_git_toplevel(os.getcwd()))
    print("primary=%s secondary=%s" % (cfg["roles"]["primary"], cfg["roles"]["secondary"]))
    for role, who in cfg["roles"]["by_role"].items():
        print("  %-13s %s" % (role, config.agent_for(cfg, role)))
    if cfg["roles"]["by_role"].get("review") != "auto" and config.agent_for(cfg, "review") == config.agent_for(cfg, "code"):
        print("warning: review agent equals code agent (%s)" % config.agent_for(cfg, "review"))
    rec = {"setup": True, "doctor_live": args.live}
    if args.scope == "project":
        rec["repo"] = _git_toplevel(os.getcwd())
    state._write_atomic(user if args.scope == "user" else project, json.dumps(rec))
    print("setup complete (%s)" % args.scope)
    return 0


def _git_toplevel(path):
    r = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"],
                       capture_output=True, text=True)
    return os.path.realpath(r.stdout.strip() if r.returncode == 0 else path)


def _run(args):
    """--run R, else the latest run for cwd's git toplevel (PLAN 2.16)."""
    if args.run:
        return state.Run.load(args.run)
    run = state.Run.latest_for_repo(_git_toplevel(os.getcwd()))
    if not run:
        raise SystemExit("yoke: no run for this repo; run `yoke init`")
    return run


def _plan_toc_errors(run):
    """TOC problems in plan.md, or [] (a missing plan.md is left to approve to report)."""
    from . import planreview
    try:
        with open(run.path("plan.md")) as f:
            return planreview.toc_errors(f.read())
    except FileNotFoundError:
        return []


def _front_half(args):
    from . import planreview
    if args.cmd == "init":
        roles = {k: v for k, v in (("primary", args.primary), ("secondary", args.secondary)) if v}
        run = planreview.init_run(args.repo or _git_toplevel(os.getcwd()), args.requirements,
                                  {"roles": roles} if roles else None)
        print(run.run_id)
        return 0
    run = _run(args)
    if args.cmd == "review-plan":
        for e in _plan_toc_errors(run):
            print("warning: plan.md %s (yoke approve plan will refuse)" % e, file=sys.stderr)
        try:
            n, res = planreview.review_round(run)
        except (RuntimeError, planreview.ApprovalError) as e:
            print("yoke: %s" % e, file=sys.stderr)
            return 1
        print("round %d: %s" % (n, run.path("plan-review/round-%d" % n)))
        if res.ok and res.schema_valid:
            out = res.structured_output
            print("verdict %s, %d findings: %s" % (out["verdict"], len(out["findings"]), out["summary"]))
            rel = "plan-review/round-%d/debate.json" % n
            try:
                debate = run.read_json(rel, None)
            except (FileNotFoundError, ValueError):
                debate = None
            if isinstance(debate, dict) and "upheld" in debate:
                upheld = debate.get("upheld") or []
                overruled = debate.get("overruled") or []
                print("debate: %d upheld, %d overruled (see %s)" % (len(upheld), len(overruled), run.path(rel)))
            return 0
        print("review failed: %s" % res.error, file=sys.stderr)
        return 1
    toc_errs = _plan_toc_errors(run) if args.kind == "plan" else []
    if toc_errs:
        for e in toc_errs:
            print("yoke: plan.md %s" % e, file=sys.stderr)
        print("yoke: add a `## Contents` index at the top of plan.md, then approve again", file=sys.stderr)
        return 1
    try:
        entry = planreview.approve(run, args.kind)
    except planreview.ApprovalError as e:
        print("yoke: %s" % e, file=sys.stderr)
        return 1
    print("approved %s sha256 %s" % (args.kind, entry["sha256"][:12]))
    if args.kind == "tickets" and tracker.detect(run.read_json("run.json")["repo"], run.cfg) == "github":
        try:
            print("github issues: %d" % len(tracker.create_issues(run)))
        except tracker.TrackerError as e:
            print("yoke: approved, but issue creation failed (re-run `yoke approve tickets` to resume): %s" % e,
                  file=sys.stderr)
            return 1
    return 0


def pr_cmd(args):
    try:
        print(tracker.pr(_run(args)))
    except tracker.TrackerError as e:
        print("yoke: %s" % e, file=sys.stderr)
        return 1
    return 0


def _print_tickets_table(doc):
    tickets = doc.get("tickets", [])
    ms_list = milestones.milestones_of(doc)
    for ms in ms_list:
        mid = ms["id"]
        m_title = ms.get("title", "")
        m_tickets = [t for t in tickets if t.get("milestone", "MS-00") == mid]
        if not m_tickets and mid == "MS-00" and len(ms_list) > 1:
            continue
        header = "%s: %s" % (mid, m_title) if m_title else mid
        print(header)
        for t in m_tickets:
            print("  %s [%s] %s%s" % (t["id"], t["type"], t["title"],
                                      "  (after %s)" % ",".join(t["depends_on"]) if t["depends_on"] else ""))


def chart_cmd(run, args, adapter=None):
    from . import chart as chart_mod, planreview
    try:
        n, res = chart_mod.chart_round(run, adapter=adapter, feedback=getattr(args, "feedback", None))
    except (chart_mod.ChartError, planreview.ApprovalError) as e:
        print("yoke: %s" % e, file=sys.stderr)
        return 1
    if not res.ok or not res.schema_valid:
        print("chart failed: %s" % res.error, file=sys.stderr)
        return 1
    import_errs = getattr(res, "import_errs", [])
    if import_errs:
        for err in import_errs:
            print("error: %s" % err, file=sys.stderr)
        return 1
    doc = run.read_json("tickets.json", {})
    for w in tickets_mod.warnings(doc):
        print("warning: " + w)
    _print_tickets_table(doc)
    return 0


def tickets_cmd(args, adapter=None):
    run = _run(args)
    if args.action == "chart":
        return chart_cmd(run, args, adapter=adapter)
    if args.action == "import":
        errs, ts = tickets_mod.import_tickets(run, args.file, adapter=adapter)
        for e in errs:
            print("error: " + e, file=sys.stderr)
        if not errs:
            doc = run.read_json("tickets.json", {})
            for w in tickets_mod.warnings(doc):
                print("warning: " + w)
            print("imported %d tickets" % len(ts))
        return 1 if errs else 0
    doc = run.read_json("tickets.json", {"tickets": []})
    _print_tickets_table(doc)
    return 0


def route_cmd(args):
    run = _run(args)
    t = next((t for t in milestones.merged_tickets(run) if t["id"] == args.ticket), None)
    if t is None:
        sys.exit("yoke: no such ticket " + args.ticket)
    print(json.dumps(router.route_ticket(run.cfg, t), indent=2))
    return 0


def _model_id(m):
    return m["id"] if isinstance(m, dict) else str(m)


def _model_label(m):
    return m.get("label", "") if isinstance(m, dict) else ""



def models_cmd(args):
    """Handle 'yoke models' subcommand: list, show, set, or reset model configurations."""
    if args.list:
        choices = {}
        for ag in config.AGENTS:
            models, warn = models_mod.list_models(ag)
            if warn:
                print(warn, file=sys.stderr)
            choices[ag] = models
        if args.json:
            out = {ag: [_model_id(m) for m in mlist] for ag, mlist in choices.items()}
            print(json.dumps(out, indent=2))
            return 0
        for ag in config.AGENTS:
            print("%s:" % ag)
            for m in choices[ag]:
                mid = _model_id(m)
                label = _model_label(m)
                if label:
                    print("  %s\t%s" % (mid, label))
                else:
                    print("  %s" % mid)
        return 0

    if not args.set and args.reset is None and not args.show:
        print("models: specify --list, --show, --set, or --reset", file=sys.stderr)
        return 2

    path = (os.path.join(config.yoke_home(), "config.json") if args.scope == "user"
            else os.path.join(_git_toplevel(os.getcwd()), ".yoke.json"))

    if args.set or args.reset is not None:
        reset_entries = []
        if args.reset is not None:
            for item in args.reset:
                item = (item or "").strip()
                if not item:
                    cur = config._read(path)
                    cur_models = cur.get("models", {})
                    reset_entries.extend(list(cur_models.keys()) or list(config.AGENTS))
                    continue
                if "." in item:
                    ag, ti = item.split(".", 1)
                    if ag not in config.AGENTS:
                        print("models: unknown agent %r (choose from %s)" % (ag, ", ".join(config.AGENTS)), file=sys.stderr)
                        return 2
                    if ti not in ("plan", "cheap"):
                        print("models: unknown tier %r (choose from plan, cheap)" % ti, file=sys.stderr)
                        return 2
                    reset_entries.append((ag, ti))
                else:
                    ag = item
                    if ag not in config.AGENTS:
                        print("models: unknown agent %r (choose from %s)" % (ag, ", ".join(config.AGENTS)), file=sys.stderr)
                        return 2
                    reset_entries.append(ag)

        sets_dict = {}
        if args.set:
            for item in args.set:
                if "=" not in item:
                    print("models: --set expects AGENT.TIER=MODEL, got %s" % item, file=sys.stderr)
                    return 2
                target, model = item.split("=", 1)
                if "." not in target:
                    print("models: --set target expects AGENT.TIER, got %s" % target, file=sys.stderr)
                    return 2
                ag, ti = target.split(".", 1)
                if ag not in config.AGENTS:
                    print("models: unknown agent %r (choose from %s)" % (ag, ", ".join(config.AGENTS)), file=sys.stderr)
                    return 2
                if ti not in ("plan", "cheap"):
                    print("models: unknown tier %r (choose from plan, cheap)" % ti, file=sys.stderr)
                    return 2
                if not model:
                    print("models: --set MODEL cannot be empty", file=sys.stderr)
                    return 2

                if ag == "agy":
                    available, warn = models_mod.list_models("agy")
                    if warn:
                        print(warn, file=sys.stderr)
                    else:
                        available_ids = [_model_id(m) for m in available]
                        if model not in available_ids:
                            print("warning: model %r not in 'agy models'" % model, file=sys.stderr)

                entry = {"model": model}
                if args.effort is not None:
                    entry["effort"] = args.effort
                sets_dict.setdefault(ag, {})[ti] = entry

        config.save_models(path, sets=sets_dict if sets_dict else None,
                           reset=reset_entries if reset_entries else None)
        if not args.show:
            action_desc = "models saved to %s" % path if args.set else "models reset in %s" % path
            print(action_desc)
            return 0

    if args.show:
        repo = _git_toplevel(os.getcwd())
        cfg = config.load(repo=repo)
        if args.json:
            out = {}
            for ag in config.AGENTS:
                plan_m, plan_e = config.model_for(cfg, ag, "planner")
                cheap_m, cheap_e = config.model_for(cfg, ag, "code")
                out[ag] = {
                    "plan": {"model": plan_m, "effort": plan_e} if plan_m else None,
                    "cheap": {"model": cheap_m, "effort": cheap_e} if cheap_m else None,
                }
            print(json.dumps(out, indent=2))
            return 0
        for ag in config.AGENTS:
            print("%s:" % ag)
            plan_m, plan_e = config.model_for(cfg, ag, "planner")
            cheap_m, cheap_e = config.model_for(cfg, ag, "code")
            for tier, m, eff in (("plan", plan_m, plan_e), ("cheap", cheap_m, cheap_e)):
                if m:
                    eff_str = " (effort: %s)" % eff if eff else ""
                    print("  %s: %s%s" % (tier, m, eff_str))
                else:
                    print("  %s: (unset)" % tier)
        return 0
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="yoke")
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init", help="create a run")
    i.add_argument("--repo")
    i.add_argument("--primary")
    i.add_argument("--secondary")
    i.add_argument("--requirements", help="file copied to requirements.md")
    r = sub.add_parser("review-plan", help="one secondary read-only review round on plan.md")
    r.add_argument("--run")
    a = sub.add_parser("approve", help="record hash-bound approval")
    a.add_argument("kind", choices=("plan", "tickets", "final"))
    a.add_argument("--run")
    d = sub.add_parser("doctor", help="check environment")
    d.add_argument("--live", action="store_true", help="one tiny -p call per agent CLI")
    su = sub.add_parser("setup", help="first-time setup check (skills require it)")
    su.add_argument("--grant-agy-read", action="store_true",
                    help="add read_file to agy's permissions.allow (ask the user first)")
    su.add_argument("--check", action="store_true", help="only report whether setup was completed")
    su.add_argument("--scope", choices=("user", "project"), default="user",
                    help="user: all repos; project: only the current git repo")
    su.add_argument("--live", action="store_true", help="also run the live agent calls")
    su.add_argument("--primary", choices=config.AGENTS, help="agent used for 'primary' roles")
    su.add_argument("--secondary", choices=config.AGENTS, help="agent used for 'secondary' roles")
    su.add_argument("--map", action="append", default=[], metavar="ROLE=AGENT",
                    help="map a role (code, review, qa, docs, ...) to primary, secondary, an agent, or auto (review only); repeatable")
    mo = sub.add_parser("models", help="list, show, configure, or reset model choices per agent")
    mo.add_argument("--list", action="store_true", help="list available model choices per agent")
    mo.add_argument("--json", action="store_true", help="output in JSON format")
    mo.add_argument("--show", action="store_true", help="print effective plan/cheap models per agent")
    mo.add_argument("--set", action="append", default=[], metavar="AGENT.TIER=MODEL",
                    help="set model for agent and tier (plan|cheap); repeatable")
    mo.add_argument("--effort", nargs="?", const="high", default=None,
                    help="reasoning effort for model (e.g. low, medium, high)")
    mo.add_argument("--scope", choices=("user", "project", "repo"), default="user",
                    help="user: ~/.yoke/config.json; project/repo: <repo>/.yoke.json (default: user)")
    mo.add_argument("--reset", nargs="?", const="", action="append", default=None, metavar="[AGENT[.TIER]]",
                    help="remove model configuration for AGENT or AGENT.TIER (or all if omitted)")
    tk = sub.add_parser("tickets", help="import, show, or chart tickets")
    tk.add_argument("action", choices=["import", "show", "chart"])
    tk.add_argument("file", nargs="?", help="tickets JSON (import)")
    tk.add_argument("--feedback", help="feedback text for chart")
    tk.add_argument("--run")
    rt = sub.add_parser("route", help="show the stage route for a ticket")
    rt.add_argument("ticket")
    rt.add_argument("--run")
    pr = sub.add_parser("pr", help="push the feature branch and open a PR (needs approve final)")
    pr.add_argument("--run")
    runcmds.add_parsers(sub)
    research.add_parsers(sub)
    sub.add_parser("mcp", help="stdio MCP server exposing the router")
    args = p.parse_args(argv)
    if args.cmd in runcmds.CMDS:
        return runcmds.dispatch(args)
    if args.cmd in research.CMDS:
        return research.dispatch(args)
    if args.cmd == "mcp":
        from . import mcp_server
        mcp_server.serve()
        return 0
    if args.cmd == "models":
        return models_cmd(args)
    if args.cmd == "tickets":
        if args.action == "import" and not args.file:
            p.error("tickets import needs a file")
        return tickets_cmd(args)
    if args.cmd == "route":
        return route_cmd(args)
    if args.cmd == "pr":
        return pr_cmd(args)
    if args.cmd == "setup":
        return setup_cmd(args)
    if args.cmd == "doctor":
        return 1 if doctor(live=args.live) else 0
    return _front_half(args)


if __name__ == "__main__":
    sys.exit(main())
