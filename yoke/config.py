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


AGENTS = ("claude", "agy")


def agent_for(cfg, role):
    """Concrete agent name for a role; by_role values may be primary, secondary, auto (review only), or a name.

    When role is 'review' and mapped to 'auto', resolves to the opposite of the
    'code' agent within AGENTS (claude <-> agy). For custom code agent names outside
    AGENTS, falls back to 'primary' when different from the code agent, else 'secondary'.
    """
    roles = cfg["roles"]
    who = roles["by_role"][role]
    if who == "auto" and role != "review":
        raise ValueError("role %s cannot be auto (review only)" % role)
    if role == "review" and who == "auto":
        code_agent = agent_for(cfg, "code")
        if code_agent in AGENTS:
            return "claude" if code_agent == "agy" else "agy"
        pri = roles["primary"]
        return pri if pri != code_agent else roles["secondary"]
    return roles[who] if who in ("primary", "secondary") else who


def _write_atomic(path, text):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    try:
        with open(tmp, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def save_roles(path, primary=None, secondary=None, mapping=None):
    """Merge a roles override into the JSON config at path; validates agent and role names.

    'auto' is accepted as an agent target only for the 'review' role.
    """
    roles = _read(DEFAULTS_PATH, required=True)["roles"]
    mapping = mapping or {}
    for name in (primary, secondary):
        if name is not None and name not in AGENTS:
            raise ValueError("unknown agent %r (choose from %s)" % (name, ", ".join(AGENTS)))
    for role, who in mapping.items():
        if role not in roles["by_role"]:
            raise ValueError("unknown role %r (choose from %s)" % (role, ", ".join(roles["by_role"])))
        allowed = AGENTS + ("primary", "secondary") + (("auto",) if role == "review" else ())
        if who not in allowed:
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
    _write_atomic(path, json.dumps(out, indent=2) + "\n")
    return out["roles"]


PLAN_ROLES = ("requirements", "planner", "plan_review")


def model_for(cfg, agent, role):
    """Concrete (model, effort) tuple for (agent, role); either may be None.

    PLAN_ROLES use the 'plan' tier; all other roles (present in cfg roles.by_role
    or defaults) use the 'cheap' tier. Returns (None, None) for stages that are
    not roles (e.g. 'router') or when models config is empty/unset.
    """
    if isinstance(cfg, dict) and "roles" in cfg:
        by_roles = cfg.get("roles", {}).get("by_role", {})
    else:
        try:
            defaults = _read(DEFAULTS_PATH, required=False) or {}
            by_roles = defaults.get("roles", {}).get("by_role", {})
        except Exception:
            by_roles = {}
    if role not in PLAN_ROLES and role not in by_roles:
        return None, None
    tier = "plan" if role in PLAN_ROLES else "cheap"
    agent_name = getattr(agent, "name", agent)
    models = cfg.get("models") if isinstance(cfg, dict) else None
    if not isinstance(models, dict):
        return None, None
    agent_cfg = models.get(agent_name)
    if not isinstance(agent_cfg, dict):
        return None, None
    tier_cfg = agent_cfg.get(tier)
    if not isinstance(tier_cfg, dict):
        return None, None
    return tier_cfg.get("model"), tier_cfg.get("effort")


def save_models(path, sets=None, reset=None):
    """Merge model overrides into the JSON config at path atomically; returns out['models'].

    sets: dict of {<agent>: {<tier>: {"model": str, "effort": str}}} merged into models.
    reset: list of (agent, tier) tuples or agent strings to remove.
    """
    cur = _read(path)
    out = copy.deepcopy(cur)
    models = out.setdefault("models", {})

    if reset:
        for entry in reset:
            if isinstance(entry, (tuple, list)) and len(entry) >= 2:
                ag, ti = entry[0], entry[1]
                if ag in models:
                    models[ag].pop(ti, None)
                    if not models[ag]:
                        models.pop(ag, None)
            elif isinstance(entry, str):
                models.pop(entry, None)

    if sets:
        for agent, tiers in sets.items():
            ag_dict = models.setdefault(agent, {})
            if isinstance(tiers, dict):
                for tier, val in tiers.items():
                    if isinstance(val, dict):
                        ag_dict.setdefault(tier, {}).update(val)

    _write_atomic(path, json.dumps(out, indent=2) + "\n")
    return out["models"]
