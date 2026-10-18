"""Tracker: local (tickets.json only) or GitHub issues + the single push/PR path (PLAN 2.14, M8).
Issue numbers live in issues.json, not tickets.json, so creating them never invalidates the
tickets approval hash. The runner never pushes; only pr() does, after `approve final`."""
import glob
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


PR_FINDINGS_MAX = 50000


def pr_body(run):
    """Assemble PR body: prbody fill + blank line + Closes #N lines + Unresolved review findings section.

    Extracts run facts and fills PR template via prbody.fill, falling back to deterministic
    fill if errors occur (logging event 'pr_fill_fallback'). Appends 'Closes #N' lines from
    issues.json and an '## Unresolved review findings' section from the closed tickets' code review.
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

    closes = []
    try:
        for _, i in sorted(run.read_json("issues.json", {}).items()):
            closes.append("Closes #%d" % i["number"])
    except Exception:
        pass
    body = "\n\n".join(p for p in [(filled or "").strip(), "\n".join(closes)] if p)
    findings_by_ticket = {}
    for td in sorted(glob.glob(run.path("tickets/*/state.json"))):
        tid = os.path.basename(os.path.dirname(td))
        st = run.read_json("tickets/%s/state.json" % tid, {})
        unres = st.get("review", {}).get("unresolved", [])
        if unres and st.get("status") == "closed":  # failed or escalated attempts never merged
            findings_by_ticket[tid] = unres
    if findings_by_ticket:
        lines = ["## Unresolved review findings"]
        for tid, findings in sorted(findings_by_ticket.items()):
            lines.append("### %s" % tid)
            for f in findings:
                loc = ("%s:%s" % (f.get("file"), f.get("line"))) if f.get("file") else ""
                prefix = ("[%s] %s" % (f.get("severity", ""), loc)).strip()
                text = f.get("comment", "")
                if f.get("suggestion"):
                    text += " (suggestion: %s)" % f["suggestion"]
                lines.append("- %s: %s" % (prefix, text) if prefix else "- %s" % text)
        text = "\n".join(lines)
        if len(text) > PR_FINDINGS_MAX:  # GitHub rejects PR bodies over 65536 chars, after the push
            text = text[:PR_FINDINGS_MAX] + "\n... (truncated; see ticket issue comments)"
        body = body.rstrip() + "\n\n" + text
    return body + "\n"


def pr(run):
    """Push the feature branch and open the PR, or just push when one is already open. Needs github tracker and valid final approval."""
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
    existing = _gh(repo, "pr", "list", "--head", branch, "--state", "open", "--json", "url", "-q", ".[0].url")
    if existing:  # the push above updated it; keep its hand-edited title and body
        run.update(pr_url=existing)
        run.event("pr_updated", url=existing, at=state.now())
        return existing
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
