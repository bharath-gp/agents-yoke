"""Stdio MCP server (PLAN 2.10): newline-delimited JSON-RPC 2.0 exposing the router to agent sessions."""
import json
import os
import sys

from . import config, router, state

PROTOCOL_DEFAULT = "2025-03-26"
_TICKET = {"type": "object", "required": ["title"], "properties": {
    "id": {"type": "string"}, "title": {"type": "string"}, "description": {"type": "string"},
    "exit_criteria": {"type": "array", "items": {"type": "string"}}}}


def _obj(required, **props):
    return {"type": "object", "required": required, "properties": props}


S = {"type": "string"}
# name -> (description, inputSchema, handler(run, args))
TOOLS = {
    "yoke_classify_ticket": ("Classify a ticket into feature/bugfix/refactor/test/docs/chore/spike.", _TICKET,
                             lambda run, a: router.classify_ticket(run, a)),
    "yoke_triage_failure": ("Triage a failure log: category and whether a retry is likely to help.",
                            _obj(["log"], log=S), lambda run, a: router.triage_failure(run, a["log"])),
    "yoke_summarize": ("Summarize text in at most 5 lines.", _obj(["text"], text=S),
                       lambda run, a: router.summarize(run, a["text"])),
    "yoke_commit_message": ("Write a commit message for a ticket.", dict(
        _TICKET, properties=dict(_TICKET["properties"], diffstat=S)),
        lambda run, a: router.commit_message(run, a, a.get("diffstat", ""))),
    "yoke_assert": ("Check a claim against evidence; unknown escalates (answer=false, reason=unknown).",
                    _obj(["claim", "evidence"], claim=S, evidence=S),
                    lambda run, a: router.assert_check(run, a["claim"], a["evidence"])),
    "yoke_health": ("Is Ollama reachable and the decision model pulled?", _obj([]), None),
    "yoke_status": ("Read-only summary of the current yoke run.", _obj([]), None),
}


def _run():
    """YOKE_RUN, else the latest run for cwd's git toplevel; None if there is none."""
    from .cli import _git_toplevel
    try:
        if os.environ.get("YOKE_RUN"):
            return state.Run.load(os.environ["YOKE_RUN"])
        return state.Run.latest_for_repo(_git_toplevel(os.getcwd()))
    except (OSError, ValueError):
        return None


def _missing(schema, args):
    return [k for k in schema.get("required", []) if k not in args]


def call_tool(name, args):
    """Returns an MCP tools/call result; tool failures are isError results, not protocol errors."""
    if name not in TOOLS:
        return _text("unknown tool: %s" % name, True)
    _, schema, handler = TOOLS[name]
    args = args or {}
    miss = _missing(schema, args)
    if miss:
        return _text("missing arguments: %s" % ", ".join(miss), True)
    run = _run()
    try:
        if name == "yoke_health":
            return _text(json.dumps(router.health(run.cfg if run else config.load())))
        if run is None:
            return _text("no yoke run for this repo (set YOKE_RUN or run `yoke init`)", True)
        out = run.status() if name == "yoke_status" else handler(run, args)
        return _text(json.dumps(out))
    except Exception as e:  # a tool must never kill the server
        return _text("%s: %s" % (type(e).__name__, e), True)


def _text(s, is_error=False):
    r = {"content": [{"type": "text", "text": s}]}
    if is_error:
        r["isError"] = True
    return r


def handle(msg):
    """One JSON-RPC message -> response dict, or None for notifications."""
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None
    params = msg.get("params") or {}
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion") or PROTOCOL_DEFAULT,
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "yoke", "version": "0.1.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [{"name": n, "description": d, "inputSchema": s} for n, (d, s, _) in TOOLS.items()]}
    elif method == "tools/call":
        result = call_tool(params.get("name"), params.get("arguments"))
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found: %s" % method}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve(stdin=None, stdout=None):
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    for line in stdin:
        if not line.strip():
            continue
        try:
            resp = handle(json.loads(line))
        except ValueError:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        if resp is not None:
            stdout.write(json.dumps(resp) + "\n")
            stdout.flush()
