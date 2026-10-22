"""Tiered safety checks for tool calls and runner commands (PLAN 2.11).

Fast-path allow -> deterministic denylist -> Ollama grey zone -> outcome.
Never raises: all public functions return an error envelope on exception.
"""
import fnmatch
import json
import os
import re

from . import config, gitops, router

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


def diff_inputs(wt):
    """Collect staged diff text, affected files, and total deleted lines for diff scan.

    Args:
        wt: Path to the git worktree.

    Returns:
        tuple: (diff_text, files, deleted_lines) where diff_text is truncated
        to 200,000 characters, files is a list of affected relative file paths,
        and deleted_lines is the integer sum of deleted lines from numstat.
    """
    diff_text = gitops.git(wt, "diff", "--cached").stdout[:200000]
    numstat = gitops.git(wt, "diff", "--cached", "--numstat").stdout
    files = []
    deleted_lines = 0
    for line in numstat.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) >= 3:
            added_str, deleted_str = parts[0], parts[1]
            path = "\t".join(parts[2:])
            files.append(path)
            if deleted_str.isdigit():
                deleted_lines += int(deleted_str)
    return diff_text, files, deleted_lines


def scan_diff(cfg, diff_text, files, deleted_lines, run=None):
    """Pre-commit diff scan: deterministic deny on secrets or sensitive files,
    grey zone evaluation via model, or allow.

    Args:
        cfg: Configuration dictionary containing 'guard' settings.
        diff_text: Staged diff string.
        files: List of file paths affected in the diff.
        deleted_lines: Total number of deleted lines.
        run: Optional active Run instance for emitting 'guard' audit events.

    Returns:
        dict: {
            "decision": "allow" | "deny" | "escalate",
            "reason": str,
            "findings": [{"kind": str, "detail": str}],
            "source": "denylist" | "ollama" | "none" | "error"
        }
        Finding kinds include 'secret', 'sensitive_file', 'mass_delete',
        'ci_or_hook', and 'network'. Secret findings cite pattern index and file,
        never leaking secret text.
    """
    try:
        guard_cfg = cfg["guard"]
        if not guard_cfg.get("enabled", True):
            res = {
                "decision": "allow",
                "reason": "guard disabled",
                "findings": [],
                "source": "none",
            }
            _event(run, "diff", res)
            return res

        deny_findings = []
        grey_findings = []

        secret_patterns = guard_cfg.get("secret_patterns", [])
        network_patterns = guard_cfg.get("network_patterns", [])
        sensitive_files = guard_cfg.get("sensitive_files", [])
        grey_files = guard_cfg.get("grey_files", [])

        compiled_secrets = []
        for i, pat in enumerate(secret_patterns):
            try:
                compiled_secrets.append((i, re.compile(pat)))
            except re.error:
                pass

        compiled_network = []
        for i, pat in enumerate(network_patterns):
            try:
                compiled_network.append((i, re.compile(pat)))
            except re.error:
                pass

        current_file = ""
        in_hunk = False
        diff_str = diff_text or ""
        for line in diff_str.splitlines():
            if line.startswith("diff --git "):
                in_hunk = False
                continue
            if line.startswith("@@ "):
                in_hunk = True
                continue
            if not in_hunk:
                if line.startswith("+++ b/"):
                    current_file = line[6:].strip()
                    continue
                if line.startswith("+++ /dev/null"):
                    continue
                if line.startswith("--- "):
                    continue
            if line.startswith("+"):
                # Added line
                attr_file = current_file or ((files[0] if files else "") or "")
                content = line[1:]
                # Check secrets
                for i, rx in compiled_secrets:
                    if rx.search(content):
                        f = {"kind": "secret", "detail": "secret pattern %d in %s" % (i, attr_file)}
                        if f not in deny_findings:
                            deny_findings.append(f)
                # Check network
                for i, rx in compiled_network:
                    if rx.search(content):
                        f = {"kind": "network", "detail": "network pattern %d in %s" % (i, attr_file)}
                        if f not in grey_findings:
                            grey_findings.append(f)

        # Check sensitive files
        all_files = list(files or [])
        if current_file and current_file != "/dev/null" and current_file not in all_files:
            all_files.append(current_file)

        for fpath in all_files:
            if _match_path(fpath, sensitive_files, cwd=None):
                f = {"kind": "sensitive_file", "detail": "sensitive file %s" % fpath}
                if f not in deny_findings:
                    deny_findings.append(f)

        # Deterministic deny
        if deny_findings:
            res = {
                "decision": "deny",
                "reason": "; ".join(f["detail"] for f in deny_findings),
                "findings": deny_findings,
                "source": "denylist",
            }
            _event(run, "diff", res)
            return res

        # Grey findings from files and deleted lines
        max_deleted = guard_cfg.get("max_deleted_lines", 500)
        if deleted_lines and deleted_lines > max_deleted:
            f = {"kind": "mass_delete", "detail": "mass delete: %d lines" % deleted_lines}
            if f not in grey_findings:
                grey_findings.append(f)

        for fpath in all_files:
            if _match_path(fpath, grey_files, cwd=None):
                f = {"kind": "ci_or_hook", "detail": "ci or hook file %s" % fpath}
                if f not in grey_findings:
                    grey_findings.append(f)

        # Clean diff -> allow without model call
        if not grey_findings:
            res = {
                "decision": "allow",
                "reason": "clean",
                "findings": [],
                "source": "none",
            }
            _event(run, "diff", res)
            return res

        # Grey findings present -> model call
        details = "\n".join(f["detail"] for f in grey_findings)
        state = "findings:\n" + details + "\ndiff:\n" + diff_str[:1500]
        flags, mx = _model_flags(cfg, state, run)

        if flags is None or mx is None:
            res = {
                "decision": "escalate",
                "reason": "model unavailable",
                "findings": grey_findings,
                "source": "none",
            }
            _event(run, "diff", res)
            return res

        thr = config.threshold(cfg, "safety")
        ask_floor = guard_cfg.get("ask_floor", 0.4)

        if mx >= thr:
            decision = "deny"
            source = "ollama"
            reason = "model flagged safety risk (%.2f)" % mx
        elif mx < ask_floor:
            decision = "allow"
            source = "ollama"
            reason = "model flags below threshold (%.2f)" % mx
        else:
            decision = "escalate"
            source = "ollama"
            reason = "model grey zone (%.2f)" % mx

        res = {
            "decision": decision,
            "reason": reason,
            "findings": grey_findings,
            "source": source,
        }
        _event(run, "diff", res)
        return res

    except Exception as e:
        res = {
            "decision": None,
            "source": "error",
            "reason": str(e),
            "findings": [],
        }
        _event(run, "diff", res)
        return res
