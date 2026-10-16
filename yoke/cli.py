"""yoke CLI. M0 skeleton: only `doctor` exists; later milestones add subcommands."""
import argparse
import json
import shutil
import subprocess
import sys
import urllib.request

from . import router, state, tickets as tickets_mod

OLLAMA_URL = "http://127.0.0.1:11434"
DECISION_MODEL = "tev1:4b"  # /v1/systemone; M0 spike: 10/10 ticket types, ~0.75s
TOOLS = [("claude", True), ("agy", True), ("git", True), ("gh", False), ("ollama", False)]


def _version(exe):
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
        return (out.stdout or out.stderr).strip().splitlines()[0]
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return "version unknown"


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


def _run(args):
    import os
    run = state.Run.load(args.run) if args.run else state.Run.latest_for_repo(os.getcwd())
    if run is None:
        sys.exit("yoke: no run for this repo (use --run)")
    return run


def tickets_cmd(args):
    run = _run(args)
    if args.action == "import":
        errs, ts = tickets_mod.import_tickets(run, args.file)
        for e in errs:
            print("error: " + e, file=sys.stderr)
        if not errs:
            print("imported %d tickets" % len(ts))
        return 1 if errs else 0
    for t in run.read_json("tickets.json", {"tickets": []})["tickets"]:
        print("%s [%s] %s%s" % (t["id"], t["type"], t["title"],
                                "  (after %s)" % ",".join(t["depends_on"]) if t["depends_on"] else ""))
    return 0


def route_cmd(args):
    run = _run(args)
    t = next((t for t in run.read_json("tickets.json")["tickets"] if t["id"] == args.ticket), None)
    if t is None:
        sys.exit("yoke: no such ticket " + args.ticket)
    print(json.dumps(router.route_ticket(run.cfg, t), indent=2))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="yoke")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("doctor", help="check environment")
    d.add_argument("--live", action="store_true", help="one tiny -p call per agent CLI")
    tk = sub.add_parser("tickets", help="import or show tickets")
    tk.add_argument("action", choices=["import", "show"])
    tk.add_argument("file", nargs="?", help="tickets JSON (import)")
    tk.add_argument("--run")
    rt = sub.add_parser("route", help="show the stage route for a ticket")
    rt.add_argument("ticket")
    rt.add_argument("--run")
    args = p.parse_args(argv)
    if args.cmd == "tickets":
        if args.action == "import" and not args.file:
            p.error("tickets import needs a file")
        return tickets_cmd(args)
    if args.cmd == "route":
        return route_cmd(args)
    if args.cmd == "doctor":
        return 1 if doctor(live=args.live) else 0


if __name__ == "__main__":
    sys.exit(main())
