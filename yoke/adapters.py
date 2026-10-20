"""Agent CLI adapters: build_cmd and parse are pure (PLAN 2.4). Nothing else knows CLI flags."""
import json
import sys
from dataclasses import dataclass, field

FORBIDDEN_FLAG = "--dangerously-skip-permissions"
CLAUDE_READ_TOOLS = "Read Grep Glob Bash(git diff:*) Bash(git log:*) Bash(git show:*)"
CLAUDE_NO_GIT_WRITE = ["Bash(git push:*)", "Bash(git commit:*)"]


@dataclass
class CallSpec:
    prompt: str
    schema_path: str
    cwd: str
    mode: str = "read"  # "read" | "write"
    model: str = None
    effort: str = None
    resume_session: str = None
    timeout_s: int = 600
    extra_dirs: tuple = ()
    env: dict = field(default_factory=dict)
    allowed_tools: tuple = ()  # claude write mode allowlist (config agents.claude.write_allowed_tools)
    web_tools: tuple = ()  # claude read mode extras for web stages (config search.tools)


@dataclass
class CallResult:
    structured_output: object = None
    session_id: str = None
    observed_model: str = None
    usage: dict = None
    cost: float = None
    error: str = None  # adapter/agent level problem; None means the CLI reported success
    # filled by calls.run_call
    exit_code: int = None
    timed_out: bool = False
    schema_valid: bool = None
    duration_s: float = None
    call_id: str = None
    audit_dir: str = None

    @property
    def ok(self):
        return self.error is None and self.structured_output is not None


def _guard(cmd):
    assert FORBIDDEN_FLAG not in cmd, "never pass " + FORBIDDEN_FLAG
    return cmd


def _load(stdout):
    """(obj, error) for one-JSON-object stdout."""
    try:
        obj = json.loads(stdout)
    except ValueError:
        return None, "stdout is not valid JSON"
    return (obj, None) if isinstance(obj, dict) else (None, "stdout JSON is not an object")


class Adapter:
    name = None

    def build_cmd(self, call):
        raise NotImplementedError

    def parse(self, stdout, exit_code):
        raise NotImplementedError


class ClaudeAdapter(Adapter):
    name = "claude"

    def build_cmd(self, call):
        with open(call.schema_path) as f:
            schema = json.dumps(json.load(f))
        cmd = ["claude", "-p", call.prompt, "--output-format", "json", "--json-schema", schema]
        if call.mode == "write":
            cmd += ["--permission-mode", "acceptEdits",
                    "--allowedTools", " ".join(call.allowed_tools),
                    "--disallowedTools"] + CLAUDE_NO_GIT_WRITE
        else:
            cmd += ["--permission-mode", "plan", "--allowedTools", " ".join((CLAUDE_READ_TOOLS,) + tuple(call.web_tools))]
        if call.resume_session:
            cmd += ["--resume", call.resume_session]
        if call.model:
            cmd += ["--model", call.model]
        if call.effort:
            cmd += ["--effort", call.effort]
        for d in call.extra_dirs:
            cmd += ["--add-dir", d]
        return _guard(cmd)

    def parse(self, stdout, exit_code):
        obj, err = _load(stdout)
        if err:
            return CallResult(error=err if exit_code == 0 else "exit %s; %s" % (exit_code, err))
        res = CallResult(structured_output=obj.get("structured_output"), session_id=obj.get("session_id"),
                         usage=obj.get("usage"), cost=obj.get("total_cost_usd"),
                         observed_model=",".join(sorted(obj.get("modelUsage") or {})) or None)
        if obj.get("is_error"):
            res.error = "claude error: %s" % (obj.get("result") or obj.get("subtype"))
        elif exit_code != 0:
            res.error = "exit %s" % exit_code
        elif res.structured_output is None:
            res.error = "no structured_output"
        return res


class AgyAdapter(Adapter):
    name = "agy"

    def build_cmd(self, call):
        mode = "accept-edits" if call.mode == "write" else "plan"  # M0: `auto` unavailable
        cmd = ["agy", "-p", call.prompt, "--output-format", "json", "--json-schema", call.schema_path,
               "--mode", mode, "--sandbox"]
        if call.resume_session:
            cmd += ["--conversation", call.resume_session]
        if call.model:
            cmd += ["--model", call.model]
        if call.effort:
            cmd += ["--effort", call.effort]
        for d in call.extra_dirs:
            cmd += ["--add-dir", d]
        return _guard(cmd)

    def parse(self, stdout, exit_code):
        obj, err = _load(stdout)
        if err:
            return CallResult(error=err if exit_code == 0 else "exit %s; %s" % (exit_code, err))
        res = CallResult(structured_output=obj.get("structured_output"), session_id=obj.get("conversation_id"),
                         usage=obj.get("usage"), observed_model="unreported")
        denied = obj.get("denied_actions")
        if exit_code != 0:
            res.error = "exit %s" % exit_code
        elif obj.get("status") != "SUCCESS":
            res.error = "agy status %s" % obj.get("status")
        elif denied:  # M0: headless denials look like success with no structured_output
            res.error = "permission denied: " + ", ".join(
                str(d.get("display_name") or d.get("action")) for d in denied)
        elif res.structured_output is None:
            res.error = "no structured_output"
        return res


class FakeAdapter(Adapter):
    """Replays tests/fake_cli.py; stage comes from YOKE_STAGE which run_call sets."""
    name = "fake"

    def __init__(self, cli):
        self.cli = cli

    def build_cmd(self, call):
        cmd = [sys.executable, self.cli, call.prompt]
        if call.resume_session:
            cmd += ["--resume", call.resume_session]
        if call.model:
            cmd += ["--model", call.model]
        if call.effort:
            cmd += ["--effort", call.effort]
        return _guard(cmd)

    def parse(self, stdout, exit_code):
        obj, err = _load(stdout)
        if err:
            return CallResult(error=err if exit_code == 0 else "exit %s; %s" % (exit_code, err))
        res = CallResult(structured_output=obj.get("structured_output"), session_id=obj.get("session_id"),
                         usage=obj.get("usage"), cost=obj.get("cost") or obj.get("total_cost_usd"),
                         observed_model="fake")
        if exit_code != 0:
            res.error = "exit %s" % exit_code
        elif obj.get("denied"):  # mimics agy's headless auto-denial
            res.error = "permission denied: " + obj["denied"]
        elif res.structured_output is None:
            res.error = "no structured_output"
        return res


def get(name):
    return {"claude": ClaudeAdapter, "agy": AgyAdapter}[name]()
