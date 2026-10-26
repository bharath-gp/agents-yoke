"""Config: defaults <- ~/.yoke/config.json <- <repo>/.yoke.json <- overrides (PLAN 2.6)."""
import copy
import json
import logging
import os

DEFAULTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "defaults.json")
logger = logging.getLogger(__name__)


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
    """Load configuration merged across defaults, user config, repo config, and overrides.

    gate_hooks, guard, notify_command, and router.thresholds.safety are accepted only from user config
    (~/.yoke/config.json). If defined in repo config, they are refused and logged with a warning.
    """
    cfg = _read(DEFAULTS_PATH, required=True)
    cfg = deep_merge(cfg, _read(os.path.join(home or yoke_home(), "config.json")))
    url = os.environ.get("YOKE_OLLAMA_URL")
    if url:
        cfg["router"]["ollama_url"] = url
    if repo:
        repo_path = os.path.join(repo, ".yoke.json")
        repo_cfg = _read(repo_path)
        user_safety = cfg.get("router", {}).get("thresholds", {}).get("safety", 0.7)
        has_gate_hooks = "gate_hooks" in repo_cfg
        has_guard = "guard" in repo_cfg
        repo_router = repo_cfg.get("router")
        has_safety = (
            isinstance(repo_router, dict)
            and "thresholds" in repo_router
            and (not isinstance(repo_router["thresholds"], dict) or "safety" in repo_router["thresholds"])
        )
        has_notify = "notify_command" in repo_cfg
        if has_gate_hooks or has_guard or has_safety or has_notify:
            repo_cfg = copy.deepcopy(repo_cfg)
            if has_notify:
                logger.warning(
                    "refusing notify_command from repo config %s: notify_command may only be configured in user config",
                    repo_path,
                )
                del repo_cfg["notify_command"]
            if has_gate_hooks:
                logger.warning(
                    "refusing gate_hooks from repo config %s: gate_hooks may only be configured in user config",
                    repo_path,
                )
                del repo_cfg["gate_hooks"]
            if has_guard:
                logger.warning(
                    "refusing guard from repo config %s: guard may only be configured in user config",
                    repo_path,
                )
                del repo_cfg["guard"]
            if has_safety:
                logger.warning(
                    "refusing router.thresholds.safety from repo config %s: router.thresholds.safety may only be configured in user config",
                    repo_path,
                )
                if not isinstance(repo_cfg["router"]["thresholds"], dict):
                    repo_cfg["router"].pop("thresholds", None)
                else:
                    repo_cfg["router"]["thresholds"].pop("safety", None)
        cfg = deep_merge(cfg, repo_cfg)
        if "router" in cfg:
            if not isinstance(cfg["router"], dict):
                cfg["router"] = {}
            if not isinstance(cfg["router"].get("thresholds"), dict):
                cfg["router"]["thresholds"] = {}
            cfg["router"]["thresholds"]["safety"] = user_safety
    return deep_merge(cfg, overrides or {})


AGENTS = ("claude", "agy")
DEFAULT_REVIEW_LENSES = ["security", "performance", "tests"]


def review_lenses(cfg=None):
    """Return configured review lenses, defaulting to ['security', 'performance', 'tests']."""
    if cfg is None:
        cfg = load()
    lenses = cfg.get("review_lenses")
    if lenses is not None:
        return list(lenses)
    return list(DEFAULT_REVIEW_LENSES)


def review_mode(cfg=None):
    """Return configured review mode, 'adversarial' (default) or 'independent'."""
    if cfg is None:
        cfg = load()
    return "independent" if cfg.get("review_mode") == "independent" else "adversarial"


def debate_rounds(cfg=None):
    """Return configured debate rounds count, defaulting to 1."""
    if cfg is None:
        cfg = load()
    try:
        return max(1, int(cfg.get("debate_rounds", 1)))
    except (TypeError, ValueError, OverflowError):
        return 1


def hypotheses_count(cfg=None):
    """Return configured hypotheses fan-out count, defaulting to 0."""
    if cfg is None:
        cfg = load()
    try:
        return max(0, int(cfg.get("hypotheses", 0)))
    except (TypeError, ValueError):
        return 0


def agent_for(cfg, role):
    """Concrete agent name for a role; by_role values may be primary, secondary, auto (review only), or a name.

    When role is 'review' and mapped to 'auto', resolves to the opposite of the
    'code' agent within AGENTS (claude <-> agy). For custom code agent names outside
    AGENTS, falls back to 'primary' when different from the code agent, else 'secondary'.
    """
    roles = cfg["roles"]
    if role in ("primary", "secondary"):
        return roles[role]
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


PLAN_ROLES = ("requirements", "planner", "plan_review", "wayfinder")


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


def threshold(cfg, point):
    """Return threshold for decision point, defaulting to min_confidence."""
    router = cfg.get("router", {}) if isinstance(cfg, dict) else {}
    thresholds = router.get("thresholds") or {}
    if point == "safety":
        return thresholds.get("safety", 0.7)
    return thresholds.get(point, router.get("min_confidence", 0.7))


def model_for_tier(cfg, agent, tier):
    """Concrete (model, effort) tuple for (agent, tier); either may be None.

    Returns (None, None) when absent or malformed.
    """
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


BUDGET_CAP_PREFIX = "budget cap exhausted:"


def check_budget(run):
    """Check if config budget (max_cost_usd, max_tokens) is exceeded by recorded usage.

    Agents counted:
    - max_tokens counts total tokens across all agents (totals.total_tokens).
    - max_cost_usd checks recorded Claude cost (by_agent.claude.cost), falling back to totals.cost if claude entry is absent.

    Returns (exceeded: bool, reason: str or None).
    The budget cap is one-shot: once escalated, resume does not re-escalate for the rest of the run.
    """
    cfg = run.cfg
    budget = cfg.get("budget")
    if not isinstance(budget, dict):
        return False, None

    max_cost = budget.get("max_cost_usd")
    max_tokens = budget.get("max_tokens")
    if max_cost is None and max_tokens is None:
        return False, None

    try:
        max_cost_val = float(max_cost) if max_cost is not None else None
    except (ValueError, TypeError):
        max_cost_val = None

    try:
        max_tokens_val = int(max_tokens) if max_tokens is not None else None
    except (ValueError, TypeError):
        max_tokens_val = None

    if max_cost_val is None and max_tokens_val is None:
        return False, None

    # Cap is one-shot: check run.json flag and past escalations
    info = run.read_json("run.json", {})
    if info.get("budget_escalated"):
        return False, None
    for e in run.read_json("escalations.json", []):
        if e.get("ticket") is None and str(e.get("reason", "")).startswith(BUDGET_CAP_PREFIX):
            return False, None

    from . import usage
    summary = usage.summarize(run)
    totals = summary.get("totals", {})

    if max_tokens_val is not None:
        tot_tokens = totals.get("total_tokens")
        if not isinstance(tot_tokens, (int, float)) or isinstance(tot_tokens, bool):
            tot_tokens = sum(
                v for k, v in totals.items()
                if "token" in k and k != "total_tokens" and isinstance(v, (int, float)) and not isinstance(v, bool)
            )
        if tot_tokens > 0 and tot_tokens >= max_tokens_val:
            return True, "%s max_tokens exceeded (%d >= %d)" % (BUDGET_CAP_PREFIX, tot_tokens, max_tokens_val)

    if max_cost_val is not None:
        claude_cost = summary.get("by_agent", {}).get("claude", {}).get("cost")
        if isinstance(claude_cost, (int, float)) and not isinstance(claude_cost, bool):
            cost_val = float(claude_cost)
        elif isinstance(totals.get("cost"), (int, float)) and not isinstance(totals.get("cost"), bool):
            cost_val = float(totals["cost"])
        else:
            cost_val = 0.0

        if cost_val > 0 and cost_val >= max_cost_val:
            return True, "%s max_cost_usd exceeded (%.6f >= %.6f)" % (BUDGET_CAP_PREFIX, cost_val, max_cost_val)

    return False, None
