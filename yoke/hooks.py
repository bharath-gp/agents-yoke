"""Hook runner: execute gate hooks in worktrees with process-group kill on timeout (PLAN M17)."""
import logging
import os
import signal
import subprocess
from typing import NamedTuple, Optional

logger = logging.getLogger(__name__)

TAIL_LIMIT = 1000


class HookResult(NamedTuple):
    """Result of a hook command execution."""

    status: str
    tail: str = ""
    exit_code: Optional[int] = None
    timed_out: bool = False
    output: str = ""


def _kill_group(proc: subprocess.Popen, grace: float = 1.0) -> None:
    """Kill the process group of proc with SIGTERM, wait grace, then SIGKILL."""
    pgid = proc.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass

    if grace:
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass

    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass

    try:
        proc.wait(timeout=1.0)
    except Exception:
        pass


def run_hook(
    cmd,
    cwd,
    ticket=None,
    stage=None,
    attempt=None,
    timeout_s: float = 300,
    kill_grace_s: float = 1.0,
    env: Optional[dict] = None,
) -> HookResult:
    """Run a hook command in a worktree.

    Environment variables set:
    - YOKE_TICKET: ticket id string
    - YOKE_STAGE: stage name string
    - YOKE_ATTEMPT: attempt number string

    On timeout, the entire process group is killed.

    Returns HookResult with status:
    - 'pass' on exit code 0
    - 'fail' on exit code 2 (with stdout/stderr tail)
    - 'gate_hook_error' on other non-zero exit code or timeout
    """
    hook_env = dict(os.environ)
    if env:
        hook_env.update(env)
    if ticket is not None:
        hook_env["YOKE_TICKET"] = str(ticket)
    if stage is not None:
        hook_env["YOKE_STAGE"] = str(stage)
    if attempt is not None:
        hook_env["YOKE_ATTEMPT"] = str(attempt)

    shell = isinstance(cmd, str)
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=hook_env,
            shell=shell,
            start_new_session=True,
            text=True,
            errors="replace",
        )
    except OSError as e:
        return HookResult(status="gate_hook_error", tail=str(e), exit_code=127, output=str(e))

    timed_out = False
    try:
        stdout, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc, grace=kill_grace_s)
        try:
            stdout, _ = proc.communicate(timeout=1.0)
        except Exception:
            stdout = ""

    stdout = stdout or ""
    tail = stdout[-TAIL_LIMIT:] if len(stdout) > TAIL_LIMIT else stdout

    if timed_out:
        status = "gate_hook_error"
        if not tail:
            tail = f"timed out after {timeout_s}s"
    elif proc.returncode == 0:
        status = "pass"
    elif proc.returncode == 2:
        status = "fail"
    else:
        status = "gate_hook_error"

    return HookResult(
        status=status,
        tail=tail,
        exit_code=proc.returncode,
        timed_out=timed_out,
        output=stdout or tail,
    )


def run_hooks(
    hooks,
    cwd,
    ticket=None,
    stage=None,
    attempt=None,
    timeout_s: float = 300,
    **kw,
) -> HookResult:
    """Run a sequence of hook commands. Returns first non-pass result or final pass result."""
    if not hooks:
        return HookResult(status="pass", tail="", exit_code=0)
    if isinstance(hooks, str):
        hooks = [hooks]
    last_res = HookResult(status="pass", tail="", exit_code=0)
    for cmd in hooks:
        res = run_hook(
            cmd,
            cwd=cwd,
            ticket=ticket,
            stage=stage,
            attempt=attempt,
            timeout_s=timeout_s,
            **kw,
        )
        if res.status != "pass":
            return res
        last_res = res
    return last_res


def notify(run, event: str, message: str) -> None:
    """Run cfg notify_command with YOKE_EVENT, YOKE_RUN, and YOKE_MESSAGE in env.

    Logs event 'notify_failed' (event=, error=) on non-pass or exception.
    Never raises.
    """
    try:
        cfg = getattr(run, "cfg", None)
        cmd = cfg.get("notify_command") if hasattr(cfg, "get") else None
        if not cmd:
            return

        run_id = getattr(run, "run_id", "")
        cwd = getattr(run, "dir", None) or os.getcwd()
        env = {
            "YOKE_EVENT": str(event),
            "YOKE_RUN": str(run_id),
            "YOKE_MESSAGE": str(message),
        }
        res = run_hook(cmd, cwd=cwd, timeout_s=10, kill_grace_s=1.0, env=env)
        if res.status != "pass":
            err = res.tail or res.output or (f"exit code {res.exit_code}" if res.exit_code is not None else res.status)
            try:
                run.event("notify_failed", event=event, error=str(err)[:500])
            except Exception:
                pass
    except Exception as e:
        try:
            run.event("notify_failed", event=event, error=str(e)[:500])
        except Exception:
            pass

