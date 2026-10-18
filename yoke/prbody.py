"""PR body generation: template lookup, run facts, deterministic fill (PLAN 2.14, M8)."""
import json
import os
import re
import string
import subprocess

from . import adapters, calls, config, schema, state

TICKET_TYPE_PRIORITY = (
    "feature",
    "bugfix",
    "refactor",
    "test",
    "docs",
    "chore",
    "spike",
    "research",
)

BUILTIN_HEADINGS = (
    "Summary",
    "Why",
    "What changed",
    "Verification",
    "Review focus",
)


def _find_entry(parent, name):
    """Case-insensitive search for an entry name in parent directory."""
    if not os.path.isdir(parent):
        return None
    target = name.lower()
    try:
        for entry in os.listdir(parent):
            if entry.lower() == target:
                return os.path.join(parent, entry)
    except OSError:
        pass
    return None


def _git_show(repo, branch, path):
    try:
        r = subprocess.run(
            ["git", "-C", repo, "show", f"{branch}:{path}"],
            capture_output=True,
            text=True,
        )
        if r.returncode == 0:
            return r.stdout
    except Exception:
        pass
    return None


def _select_template_file(items, ticket_types, key=lambda x: x):
    """Pick template whose stem matches highest-priority ticket type present, else first sorted."""
    if not items:
        return None
    if ticket_types:
        for p in TICKET_TYPE_PRIORITY:
            if p in ticket_types:
                for item in items:
                    stem = os.path.splitext(os.path.basename(key(item)))[0]
                    if stem.lower() == p.lower():
                        return item
    return sorted(items, key=lambda x: (os.path.basename(key(x)).lower(), os.path.basename(key(x))))[0]


def _find_template_git(repo, branch, ticket_types):
    try:
        r = subprocess.run(
            ["git", "-C", repo, "ls-tree", "-r", "--name-only", branch],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            return None
        lines = [line.strip() for line in r.stdout.splitlines() if line.strip()]
        if not lines:
            return None

        # 1. .github/PULL_REQUEST_TEMPLATE/ directory
        md_in_dir = []
        for path in lines:
            norm = path.replace("\\", "/")
            parts = norm.split("/")
            if len(parts) >= 3:
                if parts[0].lower() == ".github" and parts[1].lower() == "pull_request_template":
                    if parts[-1].lower().endswith(".md"):
                        md_in_dir.append(norm)

        if md_in_dir:
            chosen = _select_template_file(md_in_dir, ticket_types)
            return _git_show(repo, branch, chosen)

        # 2. .github/pull_request_template.md
        for path in lines:
            parts = path.replace("\\", "/").split("/")
            if len(parts) == 2 and parts[0].lower() == ".github" and parts[1] == "pull_request_template.md":
                return _git_show(repo, branch, path)

        # 3. .github/PULL_REQUEST_TEMPLATE.md
        for path in lines:
            parts = path.replace("\\", "/").split("/")
            if len(parts) == 2 and parts[0].lower() == ".github" and parts[1] == "PULL_REQUEST_TEMPLATE.md":
                return _git_show(repo, branch, path)

        for path in lines:
            parts = path.replace("\\", "/").split("/")
            if len(parts) == 2 and parts[0].lower() == ".github" and parts[1].lower() == "pull_request_template.md":
                return _git_show(repo, branch, path)

        for path in lines:
            parts = path.replace("\\", "/").split("/")
            if len(parts) == 1 and parts[0].lower() == "pull_request_template.md":
                return _git_show(repo, branch, path)

        for path in lines:
            parts = path.replace("\\", "/").split("/")
            if len(parts) == 2 and parts[0].lower() == "docs" and parts[1].lower() == "pull_request_template.md":
                return _git_show(repo, branch, path)

        return None
    except Exception:
        return None


def _find_template_fs(repo, ticket_types):
    if not os.path.isdir(repo):
        return None

    # 1. .github/PULL_REQUEST_TEMPLATE/ directory
    github_dir = _find_entry(repo, ".github")
    if github_dir and os.path.isdir(github_dir):
        tmpl_dir = _find_entry(github_dir, "pull_request_template")
        if tmpl_dir and os.path.isdir(tmpl_dir):
            try:
                entries = os.listdir(tmpl_dir)
            except OSError:
                entries = []
            md_files = [
                e for e in entries
                if e.lower().endswith(".md") and os.path.isfile(os.path.join(tmpl_dir, e))
            ]
            if md_files:
                chosen = _select_template_file(md_files, ticket_types)
                with open(os.path.join(tmpl_dir, chosen), encoding="utf-8") as f:
                    return f.read()

    # 2. .github/pull_request_template.md
    # 3. .github/PULL_REQUEST_TEMPLATE.md
    if github_dir and os.path.isdir(github_dir):
        for candidate in ("pull_request_template.md", "PULL_REQUEST_TEMPLATE.md"):
            exact = os.path.join(github_dir, candidate)
            if os.path.isfile(exact):
                with open(exact, encoding="utf-8") as f:
                    return f.read()
        pr_md = _find_entry(github_dir, "pull_request_template.md")
        if pr_md and os.path.isfile(pr_md):
            with open(pr_md, encoding="utf-8") as f:
                return f.read()

    # 3. pull_request_template.md in repo root
    root_md = _find_entry(repo, "pull_request_template.md")
    if root_md and os.path.isfile(root_md):
        with open(root_md, encoding="utf-8") as f:
            return f.read()

    # 4. docs/pull_request_template.md
    docs_dir = _find_entry(repo, "docs")
    if docs_dir and os.path.isdir(docs_dir):
        docs_md = _find_entry(docs_dir, "pull_request_template.md")
        if docs_md and os.path.isfile(docs_md):
            with open(docs_md, encoding="utf-8") as f:
                return f.read()

    return None


def find_template(repo, branch=None, ticket_types=None):
    """Find a PR template in repo for branch (or working tree fallback).

    Lookup order (case-insensitive):
    1. .github/PULL_REQUEST_TEMPLATE/ (dir: pick stem matching highest-priority ticket type,
       priority feature>bugfix>refactor>test>docs>chore>spike>research; else first sorted .md;
       empty or no .md falls through).
    2. .github/pull_request_template.md
    3. .github/PULL_REQUEST_TEMPLATE.md
    4. pull_request_template.md
    5. docs/pull_request_template.md
    Returns template text or None.
    """
    if ticket_types is None:
        tickets_path = os.path.join(repo, "tickets.json")
        if os.path.isfile(tickets_path):
            try:
                with open(tickets_path, encoding="utf-8") as f:
                    tickets = json.load(f).get("tickets", [])
                    ticket_types = [t.get("type") for t in tickets if t.get("type")]
            except Exception:
                ticket_types = []
        else:
            ticket_types = []

    if branch:
        template = _find_template_git(repo, branch, ticket_types)
        if template is not None:
            return template

    return _find_template_fs(repo, ticket_types)


def _extract_section(text, names):
    """Extract content under any markdown heading matching one of names."""
    lines = text.splitlines()
    in_section = False
    section_lines = []
    names_lower = [n.lower() for n in names]

    for line in lines:
        stripped = line.strip()
        m = re.match(r"^#{1,6}\s+(.*)$", stripped)
        if m:
            heading = m.group(1).strip().lower()
            if in_section:
                break
            if any(n in heading for n in names_lower):
                in_section = True
                continue
        elif in_section:
            section_lines.append(line)

    return "\n".join(section_lines).strip()


def collect(run):
    """Collect facts from a run directory:
    - title: from plan.md first '# ' heading with ^(?:\\w+\\s+)*Plan:\\s* stripped; fallback 'yoke: <run_id>'
    - why: from requirements Goal + plan approach
    - changes: from ticket titles + git diff --stat <base>...<branch>
    - verification: test_command + close_gate events ok=true for closed tickets
    - verified: set of verified flags (e.g. {'tests_pass'})
    """
    if isinstance(run, str):
        run_dir = run
        run_id = os.path.basename(run_dir)
        def _path(rel):
            return os.path.join(run_dir, rel)
        def _read_json(rel, default=None):
            p = _path(rel)
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    return json.load(f)
            return default if default is not None else {}
    else:
        run_id = getattr(run, "run_id", "unknown")
        _path = run.path
        _read_json = run.read_json

    # 1. Title from plan.md
    title = None
    plan_path = _path("plan.md")
    plan_text = ""
    if os.path.isfile(plan_path):
        try:
            with open(plan_path, encoding="utf-8") as f:
                plan_text = f.read()
            for line in plan_text.splitlines():
                s = line.strip()
                if s.startswith("# "):
                    heading = s[2:].strip()
                    cleaned = re.sub(r"^(?:\w+\s+)*Plan:\s*", "", heading, flags=re.IGNORECASE).strip()
                    if cleaned:
                        title = cleaned
                    break
        except Exception:
            pass

    if not title:
        title = f"yoke: {run_id}"

    # 2. Why from requirements Goal + plan approach
    req_text = ""
    req_path = _path("requirements.md")
    if os.path.isfile(req_path):
        try:
            with open(req_path, encoding="utf-8") as f:
                req_text = f.read()
        except Exception:
            pass

    goal = _extract_section(req_text, ["goal", "motivation", "summary", "problem"])
    if not goal and req_text.strip():
        non_headings = [l for l in req_text.splitlines() if not l.strip().startswith("#")]
        goal = "\n".join(non_headings).strip() or req_text.strip()

    approach = _extract_section(plan_text, ["approach", "overview", "solution", "design"])
    if not approach and plan_text.strip():
        non_headings = [l for l in plan_text.splitlines() if not l.strip().startswith("#")]
        approach = "\n".join(non_headings).strip()

    why_parts = [p for p in (goal, approach) if p]
    why = "\n\n".join(why_parts) if why_parts else "N/A - not recorded by yoke"

    # 3. Changes from ticket titles + git diff --stat
    tickets = _read_json("tickets.json", {}).get("tickets", [])
    ticket_lines = []
    for t in tickets:
        tid = t.get("id")
        ttitle = t.get("title")
        if tid and ttitle:
            ticket_lines.append(f"- {tid}: {ttitle}")
        elif ttitle:
            ticket_lines.append(f"- {ttitle}")

    run_info = _read_json("run.json", {})
    repo = run_info.get("repo")
    base = run_info.get("base_sha")
    branch = run_info.get("branch")
    stat_text = ""
    if repo and branch:
        try:
            cmd = ["git", "-C", repo, "diff", "--stat"]
            if base:
                r = subprocess.run(cmd + [f"{base}...{branch}"], capture_output=True, text=True)
                if r.returncode != 0:
                    r = subprocess.run(cmd + [base, branch], capture_output=True, text=True)
            else:
                r = subprocess.run(cmd + [branch], capture_output=True, text=True)
            if r.returncode == 0 and r.stdout.strip():
                stat_text = r.stdout.strip()
        except Exception:
            pass

    changes_parts = []
    if ticket_lines:
        changes_parts.append("\n".join(ticket_lines))
    if stat_text:
        changes_parts.append(f"```\n{stat_text}\n```")
    changes = "\n\n".join(changes_parts) if changes_parts else "N/A - not recorded by yoke"

    # 4. Verification and verified set
    events = []
    events_path = _path("events.jsonl")
    if os.path.isfile(events_path):
        try:
            with open(events_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        events.append(json.loads(line))
        except Exception:
            pass

    close_gates = {}
    for ev in events:
        if ev.get("kind") == "close_gate":
            tid = ev.get("ticket")
            close_gates[tid] = ev

    verified = set()
    verif_lines = []

    passing_count = 0
    total_closed = 0
    for t in tickets:
        tid = t.get("id")
        st = _read_json(f"tickets/{tid}/state.json", {})
        is_closed = st.get("status") == "closed" or (tid in close_gates and close_gates[tid].get("ok") is True)
        if is_closed:
            total_closed += 1
            ev = close_gates.get(tid)
            if ev and ev.get("ok") is True:
                passing_count += 1
                attempt = ev.get("attempt", 1)
                qa_res = _read_json(f"tickets/{tid}/attempt-{attempt}/qa/result.json", {})
                test_cmd = qa_res.get("test_command") or run_info.get("config", {}).get("test_command")
                if test_cmd:
                    verif_lines.append(f"- {tid}: `{test_cmd}` passed")
                else:
                    verif_lines.append(f"- {tid}: passed close gate")

    if not tickets and close_gates:
        for tid, ev in sorted(close_gates.items()):
            if ev.get("ok") is True:
                passing_count += 1
                total_closed += 1
                attempt = ev.get("attempt", 1)
                qa_res = _read_json(f"tickets/{tid}/attempt-{attempt}/qa/result.json", {})
                test_cmd = qa_res.get("test_command")
                if test_cmd:
                    verif_lines.append(f"- {tid}: `{test_cmd}` passed")
                else:
                    verif_lines.append(f"- {tid}: passed close gate")

    if total_closed > 0 and passing_count == total_closed:
        verified.add("tests_pass")

    verification = "\n".join(verif_lines) if verif_lines else "N/A - not recorded by yoke"

    return {
        "title": title,
        "summary": title,
        "why": why,
        "changes": changes,
        "verification": verification,
        "verified": verified,
    }


def _should_tick(text, verified):
    if not verified:
        return False
    if "tests_pass" in verified or "test" in verified or "tests" in verified:
        if re.search(r"\btests?\b", text, re.IGNORECASE):
            return True
    for v in verified:
        if v not in ("tests_pass", "test", "tests"):
            if re.search(r"\b" + re.escape(v) + r"\b", text, re.IGNORECASE):
                return True
    return False


def _map_fact(heading_text, facts):
    h = heading_text.lower()
    if any(k in h for k in ("test", "verif")):
        return facts.get("verification") or "N/A - not recorded by yoke"
    if any(k in h for k in ("what", "change")):
        return facts.get("changes") or "N/A - not recorded by yoke"
    if any(k in h for k in ("why", "summary", "motivation")):
        if "summary" in h and facts.get("summary") and facts.get("summary") != facts.get("why"):
            return facts["summary"]
        return facts.get("why") or facts.get("summary") or facts.get("title") or "N/A - not recorded by yoke"
    return "N/A - not recorded by yoke"


def _built_in_structure(facts):
    summary = facts.get("summary") or facts.get("title") or "N/A - not recorded by yoke"
    why = facts.get("why") or "N/A - not recorded by yoke"
    changes = facts.get("changes") or "N/A - not recorded by yoke"
    verification = facts.get("verification") or "N/A - not recorded by yoke"
    review_focus = facts.get("review_focus") or "N/A - not recorded by yoke"

    return "\n\n".join([
        f"## Summary\n{summary}",
        f"## Why\n{why}",
        f"## What changed\n{changes}",
        f"## Verification\n{verification}",
        f"## Review focus\n{review_focus}",
    ]) + "\n"


def fill_template(template, facts):
    """Fill template with facts deterministically:
    - Keep headings, order, and checklist wording
    - Put mapped facts under headings:
      why/summary/motivation -> why
      what/change -> changes
      test/verif -> verification
      else N/A - not recorded by yoke
    - Tick '- [ ]' only when item text matches verified set (test/tests pass)
    - If template is None or empty, return built-in structure:
      Summary, Why, What changed, Verification, Review focus.
    """
    if not template or not template.strip():
        return _built_in_structure(facts)

    lines = template.splitlines()
    verified = facts.get("verified", set())
    if isinstance(verified, (list, tuple)):
        verified = set(verified)

    preamble = []
    sections = []
    current_heading = None
    current_level = 0
    current_title = ""
    current_checklists = []

    checklist_pattern = re.compile(r"^(\s*(?:[-*+]|\d+\.)\s*\[)([ xX])(\]\s*)(.*)$")

    for line in lines:
        m = re.match(r"^(#{1,6})\s+(.*)$", line.strip())
        if m:
            if current_heading is not None:
                sections.append((current_heading, current_level, current_title, current_checklists))
            current_heading = line
            current_level = len(m.group(1))
            current_title = m.group(2).strip()
            current_checklists = []
        else:
            chk = checklist_pattern.match(line)
            if chk:
                prefix = chk.group(1)
                suffix = chk.group(3)
                item_text = chk.group(4)
                tick = "x" if _should_tick(item_text, verified) else " "
                current_checklists.append(f"{prefix}{tick}{suffix}{item_text}")
            elif current_heading is None:
                if not (line.strip().startswith("<!--") and line.strip().endswith("-->")):
                    preamble.append(line)

    if current_heading is not None:
        sections.append((current_heading, current_level, current_title, current_checklists))

    if not sections:
        processed = []
        for line in lines:
            chk = checklist_pattern.match(line)
            if chk:
                prefix = chk.group(1)
                suffix = chk.group(3)
                item_text = chk.group(4)
                tick = "x" if _should_tick(item_text, verified) else " "
                processed.append(f"{prefix}{tick}{suffix}{item_text}")
            else:
                processed.append(line)
        return "\n".join(processed) + "\n"

    out_sections = []
    if preamble and any(p.strip() for p in preamble):
        out_sections.append("\n".join(preamble).strip())

    for i, (h_line, level, title, chk_lines) in enumerate(sections):
        mapped = _map_fact(title, facts)
        is_container = False
        if i + 1 < len(sections):
            next_level = sections[i + 1][1]
            if next_level > level and not chk_lines and mapped == "N/A - not recorded by yoke":
                is_container = True

        if is_container:
            out_sections.append(h_line)
            continue

        body_parts = []
        if mapped != "N/A - not recorded by yoke":
            body_parts.append(mapped)
            if chk_lines:
                body_parts.append("\n".join(chk_lines))
        elif chk_lines:
            body_parts.append("\n".join(chk_lines))
        else:
            body_parts.append("N/A - not recorded by yoke")

        section_text = h_line + "\n" + "\n\n".join(body_parts)
        out_sections.append(section_text)

    return "\n\n".join(out_sections).strip() + "\n"


def untick_unverified(body, verified):
    """Post-validate checklist items by un-ticking any checkbox not in verified set."""
    if isinstance(verified, (list, tuple)):
        verified = set(verified)
    elif not isinstance(verified, set):
        verified = set(verified) if verified else set()

    checklist_pattern = re.compile(r"^(\s*(?:[-*+]|\d+\.)\s*\[)([ xX])(\]\s*)(.*)$")
    out_lines = []
    for line in body.splitlines():
        chk = checklist_pattern.match(line)
        if chk:
            prefix = chk.group(1)
            tick = chk.group(2)
            suffix = chk.group(3)
            item_text = chk.group(4)
            if tick in ("x", "X") and not _should_tick(item_text, verified):
                out_lines.append(f"{prefix} {suffix}{item_text}")
            else:
                out_lines.append(line)
        else:
            out_lines.append(line)
    res = "\n".join(out_lines)
    if body.endswith("\n"):
        res += "\n"
    return res


def fill(run, template, facts, adapter=None, env=None):
    """Fill PR template using agent via calls.run_call with fallback to deterministic fill (PLAN 2.14).

    Calls agent with mode='read' using role 'pr_body'.
    Reads body from structured_output['body'].
    On any failure or invalid output: returns deterministic fill and logs event 'pr_fill_fallback'.
    Post-validates by un-ticking any checkbox not in verified set.
    """
    role = "pr_body"

    def _fallback():
        if hasattr(run, "event"):
            try:
                run.event("pr_fill_fallback")
            except Exception:
                pass
        elif isinstance(run, str):
            try:
                events_p = os.path.join(run, "events.jsonl")
                with open(events_p, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"kind": "pr_fill_fallback", "time": state.now()}) + "\n")
            except Exception:
                pass
        return fill_template(template, facts)

    try:
        cfg = getattr(run, "cfg", None)
        if cfg is None:
            if hasattr(run, "read_json"):
                cfg = run.read_json("run.json", {}).get("config")
        if cfg is None:
            cfg = config.load()

        timeout_s = cfg.get("timeouts_s", {}).get(role, 300)
        repo = None
        if hasattr(run, "read_json"):
            try:
                repo = run.read_json("run.json", {}).get("repo")
            except Exception:
                pass
        if not repo:
            repo = getattr(run, "dir", os.getcwd())

        verified = facts.get("verified", set())
        if isinstance(verified, (list, tuple)):
            verified_str = ", ".join(sorted(verified))
        elif isinstance(verified, set):
            verified_str = ", ".join(sorted(verified))
        else:
            verified_str = str(verified)

        prompts_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")
        with open(os.path.join(prompts_dir, "pr_body.md"), encoding="utf-8") as f:
            prompt_tmpl = string.Template(f.read())

        prompt = prompt_tmpl.safe_substitute(
            template=template or "(none provided)",
            title=facts.get("title", ""),
            summary=facts.get("summary", ""),
            why=facts.get("why", ""),
            changes=facts.get("changes", ""),
            verification=facts.get("verification", ""),
            verified=verified_str,
        )

        spec = adapters.CallSpec(
            prompt=prompt,
            schema_path=schema.schema_path("pr_body"),
            cwd=repo,
            mode="read",
            timeout_s=timeout_s,
            env=env or {},
        )

        if adapter is None:
            agent_name = config.agent_for(cfg, role)
            adapter = adapters.get(agent_name)

        res = calls.run_call(run, adapter, spec, stage=role)

        if (
            res is None
            or not res.ok
            or res.schema_valid is False
            or not isinstance(res.structured_output, dict)
            or not isinstance(res.structured_output.get("body"), str)
            or not res.structured_output.get("body", "").strip()
        ):
            return _fallback()

        body = res.structured_output["body"]
        return untick_unverified(body, verified)

    except Exception:
        return _fallback()

