"""Limit detection: regex patterns, reset_at timestamp parsing, classification (PLAN M22)."""
import copy
import datetime
import json
import re

_ISO_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)\b")
_REL_RE = re.compile(r"\b(?:in|wait)\s+(\d+)\s*(s(?:ec(?:onds?)?)?|m(?:in(?:utes?)?)?|h(?:(?:ou)?rs?)?)\b", re.IGNORECASE)


def parse_reset_at(text):
    """Extract and normalize a reset timestamp from text into an ISO 8601 string or None."""
    if not text or not isinstance(text, str):
        return None

    m = _ISO_RE.search(text)
    if m:
        return m.group(1).replace(" ", "T")

    m_rel = _REL_RE.search(text)
    if m_rel:
        qty = int(m_rel.group(1))
        unit = m_rel.group(2).lower()
        if unit.startswith("s"):
            sec = qty
        elif unit.startswith("m"):
            sec = qty * 60
        elif unit.startswith("h"):
            sec = qty * 3600
        else:
            sec = qty
        target = datetime.datetime.now().astimezone() + datetime.timedelta(seconds=sec)
        return target.isoformat(timespec="seconds")

    return None


def _load_default_patterns():
    try:
        from . import config
        cfg = config._read(config.DEFAULTS_PATH, required=False)
        p = cfg.get("limits", {}).get("patterns")
        if isinstance(p, dict):
            return p
    except Exception:
        pass
    return {}


def get_patterns(cfg=None, agent=None):
    """Return list of limit regex patterns for the given agent from config or defaults."""
    patterns = None
    if cfg and isinstance(cfg, dict):
        lim_cfg = cfg.get("limits")
        if isinstance(lim_cfg, dict):
            patterns = lim_cfg.get("patterns")

    defaults = _load_default_patterns()
    if not isinstance(patterns, dict):
        patterns = defaults

    if agent:
        if agent in patterns:
            return list(patterns[agent])
        if agent in defaults:
            return list(defaults[agent])
        combined = []
        for p_list in patterns.values():
            if isinstance(p_list, (list, tuple)):
                combined.extend(p_list)
        return combined

    return copy.deepcopy(patterns)


def classify(agent, stdout, exit_code=0, cfg=None, patterns=None, obj=None):
    """Classify output and exit_code into a limit stop dict: {reset_at: iso or None, raw: text} or None."""
    if obj is None and stdout:
        s = stdout.strip()
        if s.startswith("{") and s.endswith("}"):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, dict):
                    obj = parsed
            except ValueError:
                pass

    candidates = []
    if isinstance(obj, dict):
        is_error = bool(obj.get("is_error"))
        status = str(obj.get("status", "")).upper()
        if is_error or status == "RESOURCE_EXHAUSTED":
            for k in ("result", "error", "message"):
                v = obj.get(k)
                if isinstance(v, str) and v:
                    candidates.append(v)
                elif isinstance(v, dict):
                    msg = v.get("message") or v.get("error")
                    if isinstance(msg, str) and msg:
                        candidates.append(msg)
            if status == "RESOURCE_EXHAUSTED":
                resp = obj.get("response")
                if isinstance(resp, str) and resp:
                    candidates.append(resp)
    elif exit_code != 0 and stdout and stdout.strip():
        for line in stdout.strip().splitlines():
            s_line = line.strip()
            if s_line and s_line not in candidates:
                candidates.append(s_line)
        if stdout.strip() not in candidates:
            candidates.append(stdout.strip())

    if not candidates:
        return None

    if patterns is None:
        patterns = get_patterns(cfg=cfg, agent=agent)

    for text in candidates:
        for pat in patterns:
            try:
                if re.search(pat, text):
                    raw = text
                    if len(raw) > 500:
                        raw = raw[:497] + "..."
                    reset_at = parse_reset_at(raw)
                    if reset_at is None and stdout and stdout != raw:
                        reset_at = parse_reset_at(stdout)
                    return {"reset_at": reset_at, "raw": raw}
            except re.error:
                continue

    return None
