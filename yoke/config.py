"""Config: defaults <- ~/.yoke/config.json <- <repo>/.yoke.json <- overrides (PLAN 2.6)."""
import copy
import json
import os

DEFAULTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "defaults.json")


def yoke_home():
    return os.environ.get("YOKE_HOME") or os.path.join(os.path.expanduser("~"), ".yoke")


def deep_merge(base, over):
    """Return base overlaid with over; dicts merge recursively, everything else replaces."""
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _read(path, required=False):
    if not os.path.exists(path):
        if required:
            raise FileNotFoundError(path)
        return {}
    try:
        with open(path) as f:
            obj = json.load(f)
    except ValueError as e:
        raise ValueError("invalid JSON in %s: %s" % (path, e))
    if not isinstance(obj, dict):
        raise ValueError("%s must contain a JSON object" % path)
    return obj


def load(repo=None, home=None, overrides=None):
    cfg = _read(DEFAULTS_PATH, required=True)
    cfg = deep_merge(cfg, _read(os.path.join(home or yoke_home(), "config.json")))
    if repo:
        cfg = deep_merge(cfg, _read(os.path.join(repo, ".yoke.json")))
    return deep_merge(cfg, overrides or {})


def agent_for(cfg, role):
    """Concrete agent name for a role; by_role values may be primary, secondary or a name."""
    roles = cfg["roles"]
    who = roles["by_role"][role]
    return roles[who] if who in ("primary", "secondary") else who


AGENTS = ("claude", "agy")


def save_roles(path, primary=None, secondary=None, mapping=None):
    """Merge a roles override into the JSON config at path; validates agent and role names."""
    roles = _read(DEFAULTS_PATH, required=True)["roles"]
    mapping = mapping or {}
    for name in (primary, secondary):
        if name is not None and name not in AGENTS:
            raise ValueError("unknown agent %r (choose from %s)" % (name, ", ".join(AGENTS)))
    for role, who in mapping.items():
        if role not in roles["by_role"]:
            raise ValueError("unknown role %r (choose from %s)" % (role, ", ".join(roles["by_role"])))
        if who not in AGENTS + ("primary", "secondary"):
            raise ValueError("unknown agent %r for role %s" % (who, role))
    over = {}
    if primary:
        over["primary"] = primary
    if secondary:
        over["secondary"] = secondary
    if mapping:
        over["by_role"] = mapping
    cur = _read(path)
    out = deep_merge(cur, {"roles": over})
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(out, f, indent=2)
        f.write("\n")
    return out["roles"]
