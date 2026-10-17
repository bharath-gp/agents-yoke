"""run_call: the one place agent CLIs are executed. Owns audit dir, process-group timeout kill,
schema validation and one re-ask (PLAN 2.12, 2.13)."""
import dataclasses
import json
import os
import signal
import subprocess
import time

from . import schema, state
from .adapters import CallResult


def audit_rel(stage, ticket, attempt, call_id):
    if ticket:
        return "tickets/%s/attempt-%s/%s" % (ticket, attempt or 1, stage)
    if stage == "plan_review":
        return "plan-review/round-%s" % (attempt or 1)
    return "calls/%s-%s" % (stage, call_id)


def _kill_group(proc, grace):
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, None)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            break
        if wait is not None:
            try:
                proc.wait(timeout=wait)
            except subprocess.TimeoutExpired:
                pass
    proc.wait()


def _exec(run, adapter, spec, rel, stage, ticket, attempt):
    """One CLI invocation into audit dir `rel`; returns CallResult (not yet schema-checked)."""
    cfg = run.cfg
    call_id = "c%04d" % run.incr_calls()
    d = run.path(rel)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "prompt.md"), "w") as f:
        f.write(spec.prompt)
    argv = adapter.build_cmd(spec)
    shown = [os.path.join(d, "prompt.md") if a == spec.prompt else a for a in argv]
    run.write_json(rel + "/command.json", shown)
    env = dict(os.environ, **spec.env)
    env["YOKE_STAGE"] = stage
    meta = {"call_id": call_id, "stage": stage, "ticket": ticket, "attempt": attempt,
            "agent": adapter.name, "adapter": type(adapter).__name__, "cmd": shown, "cwd": spec.cwd,
            "requested": {"model": spec.model, "effort": spec.effort, "mode": spec.mode},
            "started_at": state.now(), "timed_out": False, "pid": None}
    run.event("call_start", call_id=call_id, stage=stage, ticket=ticket, attempt=attempt, dir=rel)
    t0 = time.time()
    with open(os.path.join(d, "stdout.log"), "w") as out, open(os.path.join(d, "stderr.log"), "w") as err:
        try:
            proc = subprocess.Popen(argv, cwd=spec.cwd, stdout=out, stderr=err, env=env,
                                    start_new_session=True)
        except OSError as e:
            res = CallResult(error="cannot start %s: %s" % (argv[0], e))
            proc = None
        if proc:
            meta["pid"] = proc.pid
            run.write_json(rel + "/meta.json", meta)  # pid visible while running
            try:
                proc.wait(timeout=spec.timeout_s)
            except subprocess.TimeoutExpired:
                meta["timed_out"] = True
                _kill_group(proc, cfg.get("kill_grace_s", 15))
    if proc:
        with open(os.path.join(d, "stdout.log")) as f:
            stdout = f.read()
        res = adapter.parse(stdout, proc.returncode)
        if meta["timed_out"]:
            res.error = "timeout after %ss" % spec.timeout_s
        meta["exit_code"] = proc.returncode
    res.exit_code = meta.get("exit_code")
    res.timed_out = meta["timed_out"]
    res.call_id, res.audit_dir = call_id, d
    res.duration_s = round(time.time() - t0, 3)
    meta.update(ended_at=state.now(), duration_s=res.duration_s, exit_code=res.exit_code,
                schema_valid=None, error=res.error,
                observed={"model": res.observed_model, "session_id": res.session_id,
                          "usage": res.usage, "cost": res.cost})
    run.write_json(rel + "/meta.json", meta)
    return res, meta


def _check(run, res, meta, spec, rel):
    """Validate against the schema; returns the error list. Persists result.json and meta."""
    errs = []
    if res.ok:
        with open(spec.schema_path) as f:
            errs = schema.validate(res.structured_output, json.load(f))
        res.schema_valid = not errs
        meta["schema_valid"] = res.schema_valid
        if errs:
            res.error = "schema: " + "; ".join(errs)
            meta["error"] = res.error
        else:
            run.write_json(rel + "/result.json", res.structured_output)
        run.write_json(rel + "/meta.json", meta)
    run.event("call_end", call_id=res.call_id, stage=meta["stage"], ticket=meta["ticket"],
              ok=res.ok and res.schema_valid is not False, error=res.error)
    return errs


def run_call(run, adapter, spec, *, stage, ticket=None, attempt=None):
    """Run one agent call. On schema failure re-ask once in the same session (audit subdir reask/)."""
    rel = audit_rel(stage, ticket, attempt, "%04d" % (run.read_json("run.json")["agent_calls"] + 1))
    res, meta = _exec(run, adapter, spec, rel, stage, ticket, attempt)
    errs = _check(run, res, meta, spec, rel)
    denied = (res.error or "").startswith("permission denied")  # agy headless: a shell command was auto-denied
    if not errs and not denied:
        return res
    if denied:
        ask = ("Your previous attempt was blocked: %s. There is no shell in this environment and it cannot be "
               "approved. Do not run any command; use only file reading and editing tools. Complete the task "
               "and return only the JSON object matching the schema." % res.error)
    else:
        ask = "Your previous output failed schema validation:\n" + "\n".join("- " + e for e in errs) + \
              "\nReturn only a corrected JSON object matching the schema."
    if not res.session_id:  # cannot resume: restate the task
        ask = spec.prompt + "\n\n" + ask
    spec2 = dataclasses.replace(spec, prompt=ask, resume_session=res.session_id or spec.resume_session)
    res2, meta2 = _exec(run, adapter, spec2, rel + "/reask", stage, ticket, attempt)
    _check(run, res2, meta2, spec2, rel + "/reask")
    return res2
