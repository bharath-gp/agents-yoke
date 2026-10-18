"""Tracker: local (tickets.json only) or GitHub issues + the single push/PR path (PLAN 2.14, M8).
Issue numbers live in issues.json, not tickets.json, so creating them never invalidates the
tickets approval hash. The runner never pushes; only pr() does, after `approve final`."""
import json
import os
import re
import subprocess

from . import gitops, planreview, prbody, state

LABEL = "yoke"


class TrackerError(Exception):
    pass


def _gh(repo, *args):
    try:
        r = subprocess.run(["gh"] + list(args), cwd=repo, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise TrackerError("gh %s: %s" % (" ".join(args[:2]), e))
    if r.returncode != 0:
        raise TrackerError("gh %s: %s" % (" ".join(args[:2]), (r.stderr or r.stdout).strip()))
    return r.stdout.strip()


def detect(repo, cfg):
    """'local' or 'github'. Config 'local'/'github' wins; 'auto' needs a github.com origin and a working gh."""
    mode = cfg.get("tracker", "auto")
    if mode in ("local", "github"):
        return mode
    url = gitops.git(repo, "remote", "get-url", "origin", check=False).stdout.strip()
    if "github.com" not in url:
        return "local"
    try:
        _gh(repo, "repo", "view", "--json", "name")
    except TrackerError:
        return "local"
    return "github"


def _body(t):
    lines = [t["description"], "", "**Exit criteria**"] + ["- [ ] " + c for c in t["exit_criteria"]]
    if t["depends_on"]:
        lines += ["", "Depends on: " + ", ".join(t["depends_on"])]
    if t["files_hint"]:
        lines += ["", "Files: " + ", ".join(t["files_hint"])]
    lines += ["", "<details><summary>Plan excerpt</summary>", "", t["plan_excerpt"], "", "</details>"]
    return "\n".join(lines)


def create_issues(run):
    """One issue per ticket, skipping tickets that already have one (safe to retry). Returns issues.json."""
    repo = run.read_json("run.json")["repo"]
    issues = run.read_json("issues.json", {})
    todo = [t for t in run.read_json("tickets.json")["tickets"] if t["id"] not in issues]
    if todo:
        _gh(repo, "label", "create", LABEL, "--force", "--description", "created by yoke")
    for t in todo:
        url = _gh(repo, "issue", "create", "--title", "%s: %s" % (t["id"], t["title"]),
                  "--body", _body(t), "--label", LABEL).splitlines()[-1]
        m = re.search(r"/issues/(\d+)$", url)
        if not m:
            raise TrackerError("could not read issue number from: %s" % url)
        issues[t["id"]] = {"number": int(m.group(1)), "url": url}
        run.write_json("issues.json", issues)  # after each issue, so a failure keeps progress
        run.event("issue_created", ticket=t["id"], number=issues[t["id"]]["number"])
    return issues


def comment(run, tid, text):
    """Comment on the ticket's issue. Never raises: a down tracker must not break the runner."""
    try:
        issue = run.read_json("issues.json", {}).get(tid)
        if issue:
            _gh(run.read_json("run.json")["repo"], "issue", "comment", str(issue["number"]), "--body", text)
    except Exception as e:
        run.event("tracker_error", ticket=tid, error=str(e))


def _get_review_rounds(run):
    d = run.path("plan-review")
    if not os.path.isdir(d):
        return []
    rounds = []
    try:
        entries = os.listdir(d)
    except OSError:
        return []
    for name in entries:
        m = re.match(r"^round-(\d+)$", name)
        if m:
            round_num = int(m.group(1))
            res_path = os.path.join(d, name, "result.json")
            if os.path.isfile(res_path):
                try:
                    with open(res_path, encoding="utf-8") as f:
                        data = json.load(f)
                    rounds.append((round_num, data))
                except Exception:
                    pass
    rounds.sort(key=lambda x: x[0])
    return [r[1] for r in rounds]


def _format_finding(f):
    if isinstance(f, str):
        return f"- {f}"
    sev = f.get("severity")
    sec = f.get("section")
    issue = f.get("issue", "")
    sug = f.get("suggestion")

    parts = []
    if sev:
        parts.append(f"[{sev}]")
    if sec:
        sec_str = str(sec).strip()
        if not sec_str.lower().startswith("section"):
            sec_str = f"section {sec_str}:"
        else:
            sec_str = f"{sec_str}:"
        parts.append(sec_str)
    if issue:
        parts.append(issue)
    if sug:
        parts.append(f"({sug})")
    line = " ".join(parts) if parts else "unspecified finding"
    return f"- {line}"


def _finding_matches_accept(finding, accept_line):
    issue = str(finding.get("issue", "")).strip().lower()
    section = str(finding.get("section", "")).strip().lower()
    suggestion = str(finding.get("suggestion", "")).strip().lower()
    line_lower = accept_line.lower()

    if issue and issue in line_lower:
        return True
    if suggestion and suggestion in line_lower:
        return True
    m = re.search(r"\bACCEPT\b[\s:-]*(.*)$", accept_line, re.IGNORECASE)
    if m:
        content = m.group(1).strip().lower()
        if content and (content in issue or issue in content):
            return True
    if section:
        sec_num = re.sub(r"^section\s*", "", section).strip()
        if sec_num and (f"section {sec_num}" in line_lower or f"sec {sec_num}" in line_lower):
            return True
    return False


def _unresolved_review_findings(run):
    rounds = _get_review_rounds(run)
    if not rounds:
        return []

    non_approve_rounds = [r for r in rounds if r.get("verdict") != "approve"]
    last_non_approve_findings = non_approve_rounds[-1].get("findings", []) if non_approve_rounds else []

    arb_text = ""
    for rel in ("plan-review/arbitration.md", "arbitration.md"):
        p = run.path(rel)
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    arb_text = f.read()
                break
            except Exception:
                pass

    if not arb_text or not arb_text.strip():
        return last_non_approve_findings[:20]

    accept_lines = [
        line.strip()
        for line in arb_text.splitlines()
        if re.search(r"\bACCEPT\b", line, re.IGNORECASE)
    ]
    if not accept_lines:
        return last_non_approve_findings[:20]

    all_findings = []
    seen = set()
    for r in rounds:
        for f in r.get("findings", []):
            key = (f.get("severity"), str(f.get("section")), f.get("issue"))
            if key not in seen:
                seen.add(key)
                all_findings.append(f)

    unmatched = []
    any_matched = False
    for f in all_findings:
        matched = False
        for acc in accept_lines:
            if _finding_matches_accept(f, acc):
                matched = True
                any_matched = True
                break
        if not matched:
            unmatched.append(f)

    if not any_matched and all_findings:
        return last_non_approve_findings[:20]

    return unmatched[:20]


def pr_body(run):
    """Assemble PR body: prbody fill + blank line + Closes #N lines + Unresolved review findings section.

    Extracts run facts and fills PR template via prbody.fill, falling back to deterministic
    fill if errors occur (logging event 'pr_fill_fallback'). Appends 'Closes #N' lines from
    issues.json and an '## Unresolved review findings' section listing up to 20 unresolved
    findings from plan review rounds.
    """
    template = None
    facts = {}
    try:
        info = run.read_json("run.json", {})
        repo = info.get("repo")
        branch = info.get("branch")
        tickets = run.read_json("tickets.json", {}).get("tickets", [])
        ticket_types = [t.get("type") for t in tickets if t.get("type")]
        template = prbody.find_template(repo, branch=branch, ticket_types=ticket_types)
        facts = prbody.collect(run)
        filled = prbody.fill(run, template, facts)
    except Exception:
        try:
            run.event("pr_fill_fallback")
        except Exception:
            pass
        try:
            filled = prbody.fill_template(template, facts)
        except Exception:
            filled = prbody.fill_template(None, {})

    filled_text = (filled or "").strip()
    parts = [filled_text] if filled_text else []

    try:
        issues = run.read_json("issues.json", {})
        closes = []
        for _, i in sorted(issues.items()):
            num = i.get("number") if isinstance(i, dict) else i
            if num is not None:
                closes.append("Closes #%d" % num)
        if closes:
            parts.append("\n".join(closes))
    except Exception:
        pass

    try:
        unresolved = _unresolved_review_findings(run)
        if unresolved:
            lines = ["## Unresolved review findings"]
            for f in unresolved:
                lines.append(_format_finding(f))
            parts.append("\n".join(lines))
    except Exception:
        pass

    return "\n\n".join(parts) + "\n"


def pr(run):
    """Push the feature branch and open the PR. Needs github tracker and valid final approval."""
    info = run.read_json("run.json")
    repo, branch = info["repo"], info["branch"]
    if detect(repo, info["config"]) != "github":
        raise TrackerError("tracker is not github; nothing to open a PR on")
    try:
        planreview.check_approval(run, "final")
    except planreview.ApprovalError as e:
        raise TrackerError(str(e))
    base = _gh(repo, "repo", "view", "--json", "defaultBranchRef", "-q", ".defaultBranchRef.name")
    try:
        gitops.git(repo, "-c", "core.hooksPath=", "push", "-u", "origin", branch)
    except gitops.GitError as e:
        raise TrackerError(str(e))
    body = pr_body(run)
    try:
        title = prbody.title(run)
    except Exception:
        title = "yoke: %s" % run.run_id
    url = _gh(repo, "pr", "create", "--base", base, "--head", branch,
              "--title", title, "--body", body).splitlines()[-1]
    run.update(pr_url=url)
    run.event("pr_opened", url=url, at=state.now())
    return url
