"""agyloop CLI. M0 skeleton: only `doctor` exists; later milestones add subcommands."""
import argparse
import json
import shutil
import subprocess
import sys
import urllib.request

OLLAMA_URL = "http://127.0.0.1:11434"
DECISION_MODEL = "tev1:4b"  # /v1/systemone; M0 spike: 10/10 ticket types, ~0.75s
CHAT_MODEL = "qwen3.5:9b-mlx"  # /api/chat with think=false, for summaries and commit messages
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
        for m in (DECISION_MODEL, CHAT_MODEL):
            out("ollama model %s %s" % (m, "ok" if m in models else "not pulled: ollama pull " + m))
    if live:
        for name, args in (("claude", ["-p", "Reply ok", "--output-format", "json"]),
                           ("agy", ["-p", "Reply ok", "--output-format", "json"])):
            ok = shutil.which(name) and _live(name, args)
            out("live %-6s %s" % (name, "ok" if ok else "FAIL"))
            if not ok:
                problems.append("live " + name)
    return problems


def main(argv=None):
    p = argparse.ArgumentParser(prog="agyloop")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("doctor", help="check environment")
    d.add_argument("--live", action="store_true", help="one tiny -p call per agent CLI")
    args = p.parse_args(argv)
    if args.cmd == "doctor":
        return 1 if doctor(live=args.live) else 0


if __name__ == "__main__":
    sys.exit(main())
