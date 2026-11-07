"""Requirements shootdown review: one read-only call per configured lens."""
import concurrent.futures
import os
import string
import sys

from . import adapters, calls, config, planreview, schema

PKG = os.path.dirname(os.path.abspath(__file__))

LENS_FOCUS = {
    "zoom_out": "Is it needed at all? Is it already in the repo? Is there a smaller version that solves the core problem?",
    "feasibility": "Can it be built with the stated stack/constraints? Are there missing dependencies or contradictory requirements?",
    "ergonomics": "Usage from the user's side: commands, defaults, error cases, naming, surprise.",
}


def shoot(run, adapter=None, env=None):
    """Run one read-only shootdown call per configured lens.

    Writes per-lens records under shootdown/round-1/<lens>.json.
    Emits a shootdown event.
    Returns dict mapping lens name to record.
    """
    cfg = run.cfg
    lenses = config.shootdown_lenses(cfg)
    if not lenses:
        return {}

    requirements = planreview._read(run, "requirements.md")
    repo = run.read_json("run.json")["repo"]

    prompt_path = os.path.join(PKG, "prompts", "shootdown.md")
    with open(prompt_path, encoding="utf-8") as f:
        prompt_tmpl = string.Template(f.read())

    setting_sources = ((cfg.get("agents") or {}).get("claude") or {}).get("setting_sources", "project,local")
    timeout_s = (cfg.get("timeouts_s") or {}).get("shootdown", 900)
    act_adapter = adapter or adapters.get(config.agent_for(cfg, "shootdown"))

    def _execute_lens(lens):
        prompt = prompt_tmpl.safe_substitute(
            lens=lens,
            focus=LENS_FOCUS.get(lens, ""),
            requirements=requirements,
        )
        spec = adapters.CallSpec(
            prompt=prompt,
            schema_path=schema.schema_path("shootdown"),
            cwd=repo,
            mode="read",
            timeout_s=timeout_s,
            env=env or {},
            setting_sources=setting_sources,
        )
        try:
            res = calls.run_call(run, act_adapter, spec, stage="shootdown", attempt=1, label=lens, budget=True)
        except calls.BudgetExhausted:
            rec = {
                "lens": lens,
                "verdict": None,
                "findings": [],
                "summary": "",
                "error": "budget exhausted",
            }
            run.write_json("shootdown/round-1/%s.json" % lens, rec)
            return rec
        except Exception as e:
            rec = {
                "lens": lens,
                "verdict": None,
                "findings": [],
                "summary": "",
                "error": str(e) or "call failed",
            }
            run.write_json("shootdown/round-1/%s.json" % lens, rec)
            return rec

        is_ok = res.ok and res.schema_valid is not False and isinstance(res.structured_output, dict)
        if is_ok:
            out = res.structured_output
            rec = {
                "lens": lens,
                "verdict": out.get("verdict"),
                "findings": out.get("findings") if isinstance(out.get("findings"), list) else [],
                "summary": out.get("summary") or "",
                "error": None,
            }
        else:
            rec = {
                "lens": lens,
                "verdict": None,
                "findings": [],
                "summary": "",
                "error": res.error or "schema invalid",
            }

        run.write_json("shootdown/round-1/%s.json" % lens, rec)
        return rec

    try:
        parallel = int(cfg.get("parallel", 1))
    except (TypeError, ValueError):
        parallel = 1

    if parallel > 1 and len(lenses) > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(lenses), parallel)) as pool:
            futures = [pool.submit(_execute_lens, lens) for lens in lenses]
            lens_records = [f.result() for f in futures]
    else:
        lens_records = [_execute_lens(lens) for lens in lenses]

    records = {rec["lens"]: rec for rec in lens_records}
    ok_count = sum(1 for rec in records.values() if rec.get("error") is None)
    blockers_count = sum(
        1
        for rec in records.values()
        for f in (rec.get("findings") or [])
        if isinstance(f, dict) and f.get("severity") == "blocker"
    )
    run.event("shootdown", lenses=lenses, ok=ok_count, blockers=blockers_count)
    return records
