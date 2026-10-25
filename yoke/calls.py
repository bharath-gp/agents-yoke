"""run_call: the one place agent CLIs are executed. Owns audit dir, process-group timeout kill,
schema validation and one re-ask (PLAN 2.12, 2.13)."""
import dataclasses
import json
import os
import signal
import subprocess
import time

from . import config, schema, state
from .adapters import CallResult


def audit_rel(stage, ticket, attempt, call_id, label=None):
    tag = label or stage
    if ticket:
        return "tickets/%s/attempt-%s/%s" % (ticket, attempt or 1, tag)
    if stage == "plan_review":
        return "plan-review/round-%s" % (attempt or 1)
    if stage == "plan_rebut":
        return "plan-review/round-%s/rebut" % (attempt or 1)
    if stage == "plan_judge":
        return "plan-review/round-%s/judge" % (attempt or 1)
    if stage == "wayfinder":
        return "chart/round-%s" % (attempt or 1)
    return "calls/%s-%s" % (tag, call_id)


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


def _exec(run, adapter, spec, rel, stage, ticket, attempt, label=None, charge=True):
    """One CLI invocation into audit dir `rel`; returns CallResult (not yet schema-checked)."""
    cfg = run.cfg
    if charge:
        call_id = "c%04d" % run.incr_calls()
    else:
        n = run.read_json("run.json")["agent_calls"]
        call_id = "c%04d-%s" % (n, label or "retry")
    d = run.path(rel)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "prompt.md"), "w") as f:
        f.write(spec.prompt)
    argv = adapter.build_cmd(spec)
    shown = [os.path.join(d, "prompt.md") if a == spec.prompt else a for a in argv]
    run.write_json(rel + "/command.json", shown)
    env = dict(os.environ, **spec.env)
    env["YOKE_STAGE"] = stage
    env["YOKE_LABEL"] = label or stage
    meta = {"call_id": call_id, "stage": stage, "label": label or stage, "ticket": ticket, "attempt": attempt,
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
        import inspect
        sig = inspect.signature(adapter.parse)
        if "cfg" in sig.parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
            res = adapter.parse(stdout, proc.returncode, cfg=cfg)
        else:
            res = adapter.parse(stdout, proc.returncode)
        if getattr(res, "limit", None) is None:
            from . import limits
            lim = limits.classify(getattr(adapter, "name", "unknown"), stdout, proc.returncode, cfg=getattr(adapter, "cfg", None) or cfg)
            if lim is not None:
                res.limit = lim
        if meta["timed_out"]:
            res.error = "timeout after %ss" % spec.timeout_s
            res.limit = None
        meta["exit_code"] = proc.returncode
    res.exit_code = meta.get("exit_code")
    res.timed_out = meta["timed_out"]
    res.call_id, res.audit_dir = call_id, d
    res.duration_s = round(time.time() - t0, 3)
    meta.update(ended_at=state.now(), duration_s=res.duration_s, exit_code=res.exit_code,
                schema_valid=None, error=res.error, limit=res.limit,
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


def run_call(run, adapter, spec, *, stage, ticket=None, attempt=None, label=None, charge=True):
    """Run one agent call.

    Resolves unset spec.model and spec.effort via config.model_for(cfg, adapter, stage).
    If a call with spec.model fails with a model error, retries once without model or effort
    in audit subdir fallback/ and emits a model_fallback event.
    On schema failure re-asks once in the same session (audit subdir reask/).
    """
    cfg = getattr(run, "cfg", None) or {}
    model, effort = config.model_for(cfg, getattr(adapter, "name", adapter), stage)
    if spec.model is None or spec.effort is None:
        spec = dataclasses.replace(
            spec,
            model=model if spec.model is None else spec.model,
            effort=effort if spec.effort is None else spec.effort,
        )

    calls_cnt = run.read_json("run.json")["agent_calls"] + (1 if charge else 0)
    rel = audit_rel(stage, ticket, attempt, "%04d" % calls_cnt, label=label)
    res, meta = _exec(run, adapter, spec, rel, stage, ticket, attempt, label=label, charge=charge)
    cli_failed = not res.ok and not res.timed_out
    errs = _check(run, res, meta, spec, rel)
    if res.limit is not None:
        return res

    if spec.model and cli_failed:
        stderr = ""
        stderr_path = os.path.join(run.path(rel), "stderr.log")
        try:
            with open(stderr_path) as f:
                stderr = f.read()
        except OSError:
            pass
        if "model" in (res.error or "").lower() or "model" in stderr.lower():
            run.event("model_fallback", stage=stage, ticket=ticket, attempt=attempt, model=spec.model)
            # Drop model and effort on fallback
            fallback_spec = dataclasses.replace(spec, model=None, effort=None)
            fallback_rel = rel + "/fallback"
            res, meta = _exec(run, adapter, fallback_spec, fallback_rel, stage, ticket, attempt, label=label, charge=charge)
            errs = _check(run, res, meta, fallback_spec, fallback_rel)
            rel = fallback_rel
            spec = fallback_spec

    denied = (res.error or "").startswith("permission denied")  # agy headless: a shell command was auto-denied
    if not errs and not denied:
        return res
    if denied:
        ask = ("Your previous attempt was blocked: %s. It cannot be approved and there is no shell. Do not "
               "run any command or retry that action. Keep all work already done (files edited, files already read); do not "
               "redo or re-read it. Finish only what remains using other file tools, then return only the "
               "JSON object matching the schema." % res.error)
    else:
        ask = "Your previous output failed schema validation:\n" + "\n".join("- " + e for e in errs) + \
              "\nReturn only a corrected JSON object matching the schema."
    if not res.session_id:  # cannot resume: restate the task
        ask = spec.prompt + "\n\n" + ask
    spec2 = dataclasses.replace(spec, prompt=ask, resume_session=res.session_id or spec.resume_session)
    res2, meta2 = _exec(run, adapter, spec2, rel + "/reask", stage, ticket, attempt, label=label, charge=charge)
    _check(run, res2, meta2, spec2, rel + "/reask")
    return res2
