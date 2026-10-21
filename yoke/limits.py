"""Limit detection: regex patterns, reset_at timestamp parsing, classification (PLAN M22)."""
import copy
import datetime
import json
import re
import time

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


def _parse_iso(s):
    if not s or not isinstance(s, str):
        return None
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.datetime.now().astimezone().tzinfo)
        return dt
    except Exception:
        return None


def calculate_resume_at(limit_dict, consecutive_waits=1, cfg=None, now_dt=None):
    """Compute (resume_at_iso, wait_seconds) for a limit stop.
    Uses parsed reset_at if present; else exponential backoff capped by limits.max_wait_s.
    """
    limits_cfg = cfg.get("limits", {}) if isinstance(cfg, dict) else {}
    max_wait_s = limits_cfg.get("max_wait_s", 7200)
    try:
        max_wait_s = float(max_wait_s)
    except (ValueError, TypeError):
        max_wait_s = 7200.0

    base_s = limits_cfg.get("backoff_s", 30)
    try:
        base_s = float(base_s)
    except (ValueError, TypeError):
        base_s = 30.0

    now_dt = now_dt or datetime.datetime.now().astimezone()

    reset_at = limit_dict.get("reset_at") if isinstance(limit_dict, dict) else None
    target_dt = _parse_iso(reset_at) if reset_at else None

    if target_dt is not None:
        if target_dt.tzinfo is not None and now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=target_dt.tzinfo)
        elif target_dt.tzinfo is None and now_dt.tzinfo is not None:
            target_dt = target_dt.replace(tzinfo=now_dt.tzinfo)
        delta_s = max(0.0, (target_dt - now_dt).total_seconds())
        if delta_s > max_wait_s:
            capped_dt = now_dt + datetime.timedelta(seconds=max_wait_s)
            return capped_dt.isoformat(timespec="seconds"), max_wait_s
        return reset_at, delta_s
    else:
        exp_wait = base_s * (2 ** max(0, consecutive_waits - 1))
        wait_s = min(max_wait_s, exp_wait)
        target_dt = now_dt + datetime.timedelta(seconds=wait_s)
        return target_dt.isoformat(timespec="seconds"), wait_s


def sleep_until(resume_at, slice_s=0.2, stop_check=None):
    """Sleep in short slices until resume_at, so SIGTERM / yoke stop can interrupt cleanly.
    Returns True if target time was reached normally, False if stopped/interrupted.
    """
    target_dt = _parse_iso(resume_at)
    if not target_dt:
        return True
    now_dt = datetime.datetime.now().astimezone()
    if target_dt.tzinfo is not None and now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=target_dt.tzinfo)
    elif target_dt.tzinfo is None and now_dt.tzinfo is not None:
        target_dt = target_dt.replace(tzinfo=now_dt.tzinfo)
    wait_s = (target_dt - now_dt).total_seconds()
    if wait_s <= 0:
        return True
    end_time = time.time() + wait_s
    while time.time() < end_time:
        if stop_check and stop_check():
            return False
        rem = end_time - time.time()
        if rem <= 0:
            break
        time.sleep(min(slice_s, rem))
    return not (stop_check and stop_check())


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
