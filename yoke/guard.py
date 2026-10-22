"""Tiered safety checks for tool calls and runner commands (PLAN 2.11).

Fast-path allow -> deterministic denylist -> Ollama grey zone -> outcome.
Never raises: all public functions return an error envelope on exception.
"""
import fnmatch
import json
import os
import re

from . import config, router

FILE_TOOLS = ("Read", "Edit", "Write", "NotebookEdit")


def _match_path(path, globs, cwd=None):
    """Match path against glob patterns, expanding ~ and handling cwd relative forms."""
    if not path or not globs:
        return False
    p = os.path.expanduser(str(path))
    forms = [p]
    p_norm = os.path.normpath(p)
    if p_norm not in forms:
        forms.append(p_norm)

    if cwd:
        cwd_exp = os.path.expanduser(cwd)
        cwd_norm = os.path.normpath(cwd_exp)
        if not os.path.isabs(p):
            joined = os.path.join(cwd_exp, p)
            forms.extend([joined, os.path.normpath(joined)])
        else:
            try:
                rel = os.path.relpath(p, cwd_exp)
                if not rel.startswith("..") and rel != "..":
                    forms.extend([rel, os.path.normpath(rel)])
            except (ValueError, OSError):
                pass
            try:
                rel_norm = os.path.relpath(p_norm, cwd_norm)
                if not rel_norm.startswith("..") and rel_norm != "..":
                    forms.append(rel_norm)
            except (ValueError, OSError):
                pass

    for pat in globs:
        g = os.path.expanduser(pat)
        is_starstar = pat.startswith("**/")
        pat_suffix = pat[3:] if is_starstar else None
        no_slash = "/" not in pat
        for form in list(forms):
            form_slash = form.rstrip("/") + "/"
            for f in (form, form_slash):
                if fnmatch.fnmatch(f, g):
                    return True
                if no_slash and fnmatch.fnmatch(os.path.basename(f.rstrip("/")), g):
                    return True
                if is_starstar and pat_suffix is not None:
                    if fnmatch.fnmatch(f, pat_suffix):
                        return True
                    if fnmatch.fnmatch(os.path.basename(f.rstrip("/")), pat_suffix):
                        return True
            if not pat.startswith(("/", "~", "*")):
                if fnmatch.fnmatch(form, "*/" + g) or fnmatch.fnmatch(form_slash, "*/" + g):
                    return True
                if fnmatch.fnmatch(form, "**/" + g) or fnmatch.fnmatch(form_slash, "**/" + g):
                    return True
    return False


def _is_inside_cwd(path, cwd):
    if not cwd or not path:
        return False
    try:
        cwd_real = os.path.realpath(os.path.expanduser(cwd))
        p = os.path.expanduser(path)
        if not os.path.isabs(p):
            p = os.path.join(cwd_real, p)
        p_real = os.path.realpath(p)
        return os.path.commonpath([p_real, cwd_real]) == cwd_real
    except (ValueError, OSError):
        return False


def _extract_file_path(tool_input):
    if isinstance(tool_input, dict):
        for k in ("file_path", "path", "notebook_path", "target_file"):
            if k in tool_input and tool_input[k]:
                return str(tool_input[k])
        return ""
    if isinstance(tool_input, str):
        return tool_input
    return ""


def _extract_paths(tool_input):
    paths = []
    if isinstance(tool_input, dict):
        for k in ("file_path", "path", "notebook_path", "target_file", "pattern", "glob"):
            val = tool_input.get(k)
            if val and isinstance(val, str):
                paths.append(val)
    elif isinstance(tool_input, str):
        paths.append(tool_input)
    return paths


def _truncate_obj(obj, max_len=1500):
    if isinstance(obj, dict):
        return {k: _truncate_obj(v, max_len) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_truncate_obj(x, max_len) for x in obj]
    if isinstance(obj, str) and len(obj) > max_len:
        return obj[:max_len]
    return obj


def _extract_command(tool_input):
    if isinstance(tool_input, dict):
        for k in ("command", "cmd"):
            if k in tool_input and tool_input[k]:
                return str(tool_input[k])
        return ""
    if isinstance(tool_input, str):
        return tool_input
    return ""


def _model_flags(cfg, state, run=None):
    """Query model for reads_secrets, destructive, and exfiltrates flags via router.decide."""
    questions = {
        "reads_secrets": {
            "type": "noul",
            "instructions": "Does this action read or expose sensitive secrets or credentials?",
        },
        "destructive": {
            "type": "noul",
            "instructions": "Is this action destructive to files, system state, or git history?",
        },
        "exfiltrates": {
            "type": "noul",
            "instructions": "Does this action exfiltrate data or transmit code outside?",
        },
    }

    def parse(ans):
        flags = {}
        for k in ("reads_secrets", "destructive", "exfiltrates"):
            if k in ans:
                val = ans[k].get("noul") if isinstance(ans[k], dict) else ans[k]
                flags[k] = float(val)
            else:
                flags[k] = 0.0
        for k, v in ans.items():
            if k not in flags:
                val = v.get("noul") if isinstance(v, dict) else v
                try:
                    flags[k] = float(val)
                except (ValueError, TypeError):
                    pass
        return flags, 1.0

    timeout = cfg.get("guard", {}).get("ollama_timeout_s", 5) if isinstance(cfg, dict) else 5
    res = router.decide(cfg, "safety", state, questions, parse, run, timeout=timeout)
    if res.get("source") == "ollama" and res.get("result"):
        result = res["result"]
        max_conf = max(result.values()) if result else None
        return result, max_conf
    return None, None


def _grey(cfg, max_conf):
    """Return outcome category: 'deny' when >= safety threshold, 'none' when < ask_floor, else 'mid'."""
    if max_conf is None:
        return "none"
    thr = config.threshold(cfg, "safety")
    ask_floor = cfg.get("guard", {}).get("ask_floor", 0.4) if isinstance(cfg, dict) else 0.4
    if max_conf >= thr:
        return "deny"
    if max_conf < ask_floor:
        return "none"
    return "mid"


def _event(run, target, res, tool=None):
    if not run:
        return
    try:
        ticket = getattr(run, "ticket", None)
        run.event(
            "guard",
            target=target,
            decision=res.get("decision"),
            source=res.get("source"),
            reason=res.get("reason"),
            tool=tool,
            ticket=ticket,
        )
    except Exception:
        pass


def check_tool(cfg, tool_name, tool_input, cwd, mode, run=None):
    """Tiered safety check for a tool invocation.

    mode: 'chat' | 'agent'
    Returns dict: {decision: 'allow'|'deny'|'ask'|None, reason: str, source: str, confidence: float|None, flags: dict}
    """
    try:
        guard_cfg = cfg["guard"]
        if not guard_cfg.get("enabled", True):
            res = {
                "decision": None,
                "reason": "guard disabled",
                "source": "none",
                "confidence": None,
                "flags": {},
            }
            _event(run, "tool", res, tool=tool_name)
            return res

        deny_paths = guard_cfg.get("deny_paths", [])

        fast_allow = [t for t in guard_cfg.get("fast_allow_tools", []) if t not in FILE_TOOLS]
        if tool_name in fast_allow:
            for fpath in _extract_paths(tool_input):
                real_fpath = ""
                try:
                    p = os.path.expanduser(fpath)
                    if cwd and not os.path.isabs(p):
                        p = os.path.join(os.path.expanduser(cwd), p)
                    real_fpath = os.path.realpath(p)
                except Exception:
                    real_fpath = ""
                if _match_path(fpath, deny_paths, cwd=cwd) or (real_fpath and _match_path(real_fpath, deny_paths, cwd=cwd)):
                    res = {
                        "decision": "deny",
                        "reason": "matches deny path: %s" % fpath,
                        "source": "denylist",
                        "confidence": None,
                        "flags": {},
                    }
                    _event(run, "tool", res, tool=tool_name)
                    return res
            res = {
                "decision": "allow",
                "reason": "fast path tool",
                "source": "fast_path",
                "confidence": None,
                "flags": {},
            }
            _event(run, "tool", res, tool=tool_name)
            return res

        if tool_name in FILE_TOOLS:
            fpath = _extract_file_path(tool_input)
            real_fpath = ""
            if fpath:
                try:
                    p = os.path.expanduser(fpath)
                    if cwd and not os.path.isabs(p):
                        p = os.path.join(os.path.expanduser(cwd), p)
                    real_fpath = os.path.realpath(p)
                except Exception:
                    real_fpath = ""

            if _match_path(fpath, deny_paths, cwd=cwd) or (real_fpath and _match_path(real_fpath, deny_paths, cwd=cwd)):
                res = {
                    "decision": "deny",
                    "reason": "matches deny path: %s" % fpath,
                    "source": "denylist",
                    "confidence": None,
                    "flags": {},
                }
                _event(run, "tool", res, tool=tool_name)
                return res

            inside = _is_inside_cwd(fpath, cwd)
            sensitive_files = guard_cfg.get("sensitive_files", [])
            is_sensitive = _match_path(fpath, sensitive_files, cwd=cwd) or (real_fpath and _match_path(real_fpath, sensitive_files, cwd=cwd))

            if inside and not is_sensitive:
                res = {
                    "decision": "allow",
                    "reason": "file inside workspace",
                    "source": "fast_path",
                    "confidence": None,
                    "flags": {},
                }
                _event(run, "tool", res, tool=tool_name)
                return res

        if tool_name == "Bash" or tool_name.startswith("Bash("):
            cmd = _extract_command(tool_input)
            deny_commands = guard_cfg.get("deny_commands", [])
            for pat in deny_commands:
                try:
                    if re.search(pat, cmd):
                        res = {
                            "decision": "deny",
                            "reason": "matches deny command: %s" % pat,
                            "source": "denylist",
                            "confidence": None,
                            "flags": {},
                        }
                        _event(run, "tool", res, tool=tool_name)
                        return res
                except re.error:
                    pass

        is_bash = tool_name == "Bash" or tool_name.startswith("Bash(")
        is_web = tool_name == "WebFetch"
        is_file_grey = tool_name in FILE_TOOLS

        if not (is_bash or is_web or is_file_grey):
            res = {
                "decision": None,
                "reason": "no rule matched",
                "source": "none",
                "confidence": None,
                "flags": {},
            }
            _event(run, "tool", res, tool=tool_name)
            return res

        try:
            inp_obj = _truncate_obj(tool_input, 1500)
            inp_str = json.dumps(inp_obj)
        except Exception:
            inp_str = str(tool_input)
        state = "tool: %s\ninput: %s\ncwd: %s" % (tool_name, inp_str[:1500], cwd or "")

        flags, max_conf = _model_flags(cfg, state, run)
        if flags is None:
            res = {
                "decision": None,
                "reason": "model unavailable or no opinion",
                "source": "none",
                "confidence": None,
                "flags": {},
            }
            _event(run, "tool", res, tool=tool_name)
            return res

        verdict = _grey(cfg, max_conf)
        if verdict == "deny":
            decision = "deny"
            reason = "model flagged safety risk (%.2f)" % max_conf
        elif verdict == "mid":
            decision = "ask" if mode == "chat" else None
            reason = "model grey zone (%.2f)" % max_conf
        else:
            decision = None
            reason = "model flags below threshold (%.2f)" % max_conf

        res = {
            "decision": decision,
            "reason": reason,
            "source": "ollama",
            "confidence": max_conf,
            "flags": flags,
        }
        _event(run, "tool", res, tool=tool_name)
        return res

    except Exception as e:
        res = {
            "decision": None,
            "source": "error",
            "reason": str(e),
            "confidence": None,
            "flags": {},
        }
        _event(run, "tool", res, tool=tool_name)
        return res


def check_command(cfg, cmd, cwd, run=None):
    """Tiered safety check for runner command execution.

    Returns dict: {decision: 'allow'|'deny'|'escalate', reason: str, source: str, confidence: float|None, flags: dict}
    """
    try:
        guard_cfg = cfg["guard"]
        if not guard_cfg.get("enabled", True):
            res = {
                "decision": "allow",
                "reason": "guard disabled",
                "source": "none",
                "confidence": None,
                "flags": {},
            }
            _event(run, "command", res, tool=None)
            return res

        deny_commands = guard_cfg.get("deny_commands", [])
        for pat in deny_commands:
            try:
                if re.search(pat, cmd or ""):
                    res = {
                        "decision": "deny",
                        "reason": "matches deny command: %s" % pat,
                        "source": "denylist",
                        "confidence": None,
                        "flags": {},
                    }
                    _event(run, "command", res, tool=None)
                    return res
            except re.error:
                pass

        state = "command: %s\ncwd: %s" % ((cmd or "")[:1500], cwd or "")
        flags, max_conf = _model_flags(cfg, state, run)
        if flags is None:
            res = {
                "decision": "allow",
                "reason": "model unavailable, command allowed",
                "source": "none",
                "confidence": None,
                "flags": {},
            }
            _event(run, "command", res, tool=None)
            return res

        verdict = _grey(cfg, max_conf)
        if verdict == "deny":
            decision = "deny"
            reason = "model flagged safety risk (%.2f)" % max_conf
        elif verdict == "mid":
            decision = "escalate"
            reason = "model grey zone (%.2f)" % max_conf
        else:
            decision = "allow"
            reason = "model flags below threshold (%.2f)" % max_conf

        res = {
            "decision": decision,
            "reason": reason,
            "source": "ollama",
            "confidence": max_conf,
            "flags": flags,
        }
        _event(run, "command", res, tool=None)
        return res

    except Exception as e:
        res = {
            "decision": None,
            "source": "error",
            "reason": str(e),
            "confidence": None,
            "flags": {},
        }
        _event(run, "command", res, tool=None)
        return res


def hook_output(res, mode=None):
    """Format PreToolUse hook output string for Claude agent or chat-session hooks."""
    try:
        if not isinstance(res, dict):
            return ""
        d = res.get("decision")
        if d not in ("deny", "ask"):
            return ""
        reason = res.get("reason") or ""
        return json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": d,
                "permissionDecisionReason": "yoke guard: " + str(reason),
            }
        })
    except Exception:
        return ""
