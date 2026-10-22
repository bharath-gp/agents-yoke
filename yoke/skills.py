"""Skill catalog discovery, filtering, and rendering (PLAN W11)."""
import fnmatch
import glob
import os


def _frontmatter(text):
    """Parse YAML-like frontmatter between the first two '---' lines.

    Returns (meta dict, body str). If no frontmatter is found, returns ({}, text).
    """
    meta = {}
    if not text.startswith("---"):
        return meta, text
    lines = text.splitlines(True)
    if not lines or lines[0].strip() != "---":
        return meta, text

    end_idx = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_idx = idx
            break

    if end_idx is None:
        return meta, text

    for line in lines[1:end_idx]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" in stripped:
            k, v = stripped.split(":", 1)
            meta[k.strip()] = v.strip().strip("'\"")

    body = "".join(lines[end_idx + 1:])
    return meta, body


def discover(roots):
    """Discover SKILL.md files from given roots.

    roots entries are a path str or (path, prefix) tuple.
    Returns list of dicts: {"name", "description", "path"}.
    """
    results = []
    if not roots:
        return results

    for entry in roots:
        if isinstance(entry, (tuple, list)):
            root_path, prefix = entry[0], entry[1]
        else:
            root_path, prefix = entry, None

        expanded = os.path.expanduser(root_path)
        pattern = os.path.join(glob.escape(expanded), "*", "SKILL.md")
        for skill_file in sorted(glob.glob(pattern)):
            try:
                with open(skill_file, "r", encoding="utf-8") as f:
                    content = f.read()
            except OSError:
                content = ""

            meta, _body = _frontmatter(content)
            dir_name = os.path.basename(os.path.dirname(skill_file))
            name = meta.get("name") or dir_name
            full_name = f"{prefix}:{name}" if prefix else name
            description = meta.get("description", "")

            results.append({
                "name": full_name,
                "description": description,
                "path": skill_file,
            })

    return results


def default_roots(repo=None, home=None):
    """Return default skill search roots.

    Includes home/.claude/skills, repo/.claude/skills (if repo given),
    and newest version dirs under home/.claude/plugins/cache/*/*/*/skills.
    """
    home = home or os.path.expanduser("~")
    roots = [os.path.join(home, ".claude", "skills")]
    if repo:
        roots.append(os.path.join(repo, ".claude", "skills"))

    cache_pattern = os.path.join(glob.escape(home), ".claude", "plugins", "cache", "*", "*", "*", "skills")
    plugins = {}
    for skills_dir in glob.glob(cache_pattern):
        version_dir = os.path.dirname(skills_dir)
        plugin_dir = os.path.dirname(version_dir)
        plugin_name = os.path.basename(plugin_dir)
        try:
            mtime = os.path.getmtime(version_dir)
        except OSError:
            try:
                mtime = os.path.getmtime(skills_dir)
            except OSError:
                mtime = 0

        if plugin_dir not in plugins or mtime > plugins[plugin_dir][0]:
            plugins[plugin_dir] = (mtime, skills_dir, plugin_name)

    for plugin_dir in sorted(plugins.keys()):
        _mtime, skills_dir, plugin_name = plugins[plugin_dir]
        roots.append((skills_dir, plugin_name))

    return roots


def catalog(cfg, repo=None, home=None):
    """Discover skills filtered by cfg['skills']['allowlist'].

    Empty allowlist returns [].
    When cfg['skills']['roots'] is non-empty, those replace default roots.
    """
    skills_cfg = (cfg or {}).get("skills", {})
    allow = skills_cfg.get("allowlist") or []
    if not allow:
        return []

    roots = skills_cfg.get("roots") or default_roots(repo, home=home)
    discovered = discover(roots)
    return [s for s in discovered if any(fnmatch.fnmatch(s["name"], p) for p in allow)]


def render(picked, agent, cfg=None):
    """Render picked skills for the specified agent.

    Nothing picked -> "(none)".
    agy inline mode -> "### name\n<body>" capped at skills.inline_chars.
    claude and agy name mode -> "Relevant skills (invoke with the Skill tool):\n- name: description".
    """
    if not picked:
        return "(none)"

    skills_cfg = (cfg or {}).get("skills", {})
    if agent == "agy" and skills_cfg.get("agy_mode") == "inline":
        chunks = []
        inline_chars = skills_cfg.get("inline_chars", 4000)
        current_len = 0
        for s in picked:
            body = ""
            path = s.get("path")
            if path and os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        _meta, body = _frontmatter(f.read())
                except OSError:
                    body = ""
            chunk = f"### {s['name']}\n{body.strip()}"
            chunks.append(chunk)
            current_len += len(chunk) + 2
            if inline_chars is not None and current_len >= inline_chars:
                break
        out = "\n\n".join(chunks)
        if inline_chars is not None and len(out) > inline_chars:
            out = out[:inline_chars]
        return out
    else:
        lines = ["Relevant skills (invoke with the Skill tool):"]
        for s in picked:
            name = s.get("name", "")
            desc = s.get("description", "")
            lines.append(f"- {name}: {desc}")
        return "\n".join(lines)
