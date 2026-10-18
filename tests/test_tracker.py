import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import unittest

from tests.test_gitops import ENV, git
from tests.test_planreview import Base
from yoke import cli, pipeline, planreview, tracker

# fake gh: logs argv as a JSON line to $FAKE_GH_LOG; FAKE_GH_FAIL=<subcmd> makes that subcommand exit 1
SHIM = """#!%s
import json, os, sys
a = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a") as f:
    f.write(json.dumps(a) + "\\n")
key = " ".join(a[:2])
if os.environ.get("FAKE_GH_FAIL") == key:
    sys.stderr.write("boom\\n")
    sys.exit(1)
if key == "issue create":
    n = sum(1 for l in open(os.environ["FAKE_GH_LOG"]) if l.startswith('["issue", "create"')) + 39
    print("https://github.com/o/r/issues/%%d" %% n)
elif key == "repo view":
    print("main")
elif key == "pr create":
    print("https://github.com/o/r/pull/7")
"""


class TrackerBase(Base):
    def setUp(self):
        super().setUp()
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        gh = os.path.join(self.bin, "gh")
        with open(gh, "w") as f:
            f.write(SHIM % sys.executable)
        os.chmod(gh, 0o755)
        agent_shim = """#!%s
import os, sys
if "FAKE_SCRIPT" in os.environ:
    from tests import fake_cli
    fake_cli.main()
else:
    sys.exit(1)
""" % sys.executable
        for agent in ("claude", "agy"):
            p = os.path.join(self.bin, agent)
            with open(p, "w") as f:
                f.write(agent_shim)
            os.chmod(p, 0o755)
        self.log = os.path.join(self.tmp, "gh.log")

        cleanups = []
        for k in list(os.environ):
            if k.startswith("GIT_CONFIG_KEY_") or k.startswith("GIT_CONFIG_VALUE_") or k in ("GIT_DIR", "GIT_WORK_TREE"):
                old = os.environ.pop(k)
                cleanups.append((k, old))

        overrides = dict(ENV)
        overrides.update({
            "YOKE_HOME": self.tmp,
            "PATH": self.bin + os.pathsep + os.environ.get("PATH", ""),
            "FAKE_GH_LOG": self.log,
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_COUNT": "0",
        })
        for k, v in overrides.items():
            old = os.environ.get(k)
            os.environ[k] = v
            cleanups.append((k, old))

        for k, old in reversed(cleanups):
            self.addCleanup(lambda k=k, old=old: os.environ.__setitem__(k, old) if old is not None else os.environ.pop(k, None))

        git(self.repo, "checkout", "-q", "-b", "main")
        with open(os.path.join(self.repo, "a.txt"), "w") as f:
            f.write("a\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "init")
        self.remote = os.path.join(self.tmp, "remote.git")
        subprocess.run(["git", "init", "-q", "--bare", self.remote], check=True)
        git(self.repo, "remote", "add", "origin", self.remote)
        git(self.repo, "config", "core.hooksPath", "")
        subprocess.run(["git", "-C", self.remote, "config", "core.hooksPath", ""], check=True)
        self.run_.update(config=dict(self.run_.cfg, tracker="github"))
        self.plan("# Plan: Awesome Tracker Feature\n\nDetails...")
        self.tickets([
            {"id": "T-001", "title": "one", "type": "feature", "description": "d", "exit_criteria": ["works"],
             "depends_on": [], "files_hint": ["a.py"], "plan_excerpt": "x"},
            {"id": "T-002", "title": "two", "type": "chore", "description": "d", "exit_criteria": ["ok"],
             "depends_on": ["T-001"], "files_hint": [], "plan_excerpt": "y"}])

    def calls(self):
        try:
            with open(self.log) as f:
                return [json.loads(l) for l in f]
        except FileNotFoundError:
            return []

    def approve_all(self):
        planreview.approve(self.run_, "plan")
        with contextlib.redirect_stdout(io.StringIO()):
            return cli.main(["approve", "tickets", "--run", self.run_.run_id])


class TestDetect(TrackerBase):
    def test_config_wins_and_auto_needs_github_remote(self):
        self.assertEqual(tracker.detect(self.repo, {"tracker": "local"}), "local")
        self.assertEqual(tracker.detect(self.repo, {"tracker": "github"}), "github")
        self.assertEqual(tracker.detect(self.repo, {"tracker": "auto"}), "local")  # origin is a local path
        git(self.repo, "remote", "set-url", "origin", "git@github.com:o/r.git")
        self.assertEqual(tracker.detect(self.repo, {"tracker": "auto"}), "github")
        os.environ["FAKE_GH_FAIL"] = "repo view"
        self.addCleanup(os.environ.pop, "FAKE_GH_FAIL")
        self.assertEqual(tracker.detect(self.repo, {"tracker": "auto"}), "local")

    def test_auto_local_without_gh(self):
        git(self.repo, "remote", "set-url", "origin", "git@github.com:o/r.git")
        old = os.environ["PATH"]
        os.environ["PATH"] = os.path.dirname(subprocess.check_output(["which", "git"]).decode().strip())
        self.addCleanup(os.environ.__setitem__, "PATH", old)
        self.assertEqual(tracker.detect(self.repo, {"tracker": "auto"}), "local")


class TestIssues(TrackerBase):
    def test_approve_tickets_creates_issues_once(self):
        self.assertEqual(self.approve_all(), 0)
        creates = [c for c in self.calls() if c[:2] == ["issue", "create"]]
        self.assertEqual(len(creates), 2)
        self.assertIn("T-001: one", creates[0])
        self.assertIn("yoke", creates[0])
        self.assertEqual(self.run_.read_json("issues.json")["T-002"]["number"], 41)
        self.assertEqual(self.approve_all(), 0)  # re-approve is idempotent
        self.assertEqual(len([c for c in self.calls() if c[:2] == ["issue", "create"]]), 2)

    def test_tickets_json_untouched_so_approval_stays_valid(self):
        self.approve_all()
        planreview.check_approval(self.run_, "tickets")

    def test_local_tracker_makes_no_gh_calls(self):
        self.run_.update(config=dict(self.run_.cfg, tracker="local"))
        self.assertEqual(self.approve_all(), 0)
        self.assertEqual(self.calls(), [])
        self.assertFalse(os.path.exists(self.run_.path("issues.json")))

    def test_gh_failure_keeps_approval_and_reports_then_retry_resumes(self):
        os.environ["FAKE_GH_FAIL"] = "issue create"
        self.assertEqual(self.approve_all(), 1)
        del os.environ["FAKE_GH_FAIL"]
        self.assertEqual(self.approve_all(), 0)
        self.assertEqual(sorted(self.run_.read_json("issues.json")), ["T-001", "T-002"])


class TestComment(TrackerBase):
    def test_escalation_comments_on_issue_and_never_raises(self):
        self.approve_all()
        pipeline.escalate(self.run_, "T-001", "max attempts")
        c = [c for c in self.calls() if c[:2] == ["issue", "comment"]]
        self.assertEqual(c[0][2], "40")
        self.assertIn("max attempts", c[0][-1])
        os.environ["FAKE_GH_FAIL"] = "issue comment"
        self.addCleanup(os.environ.pop, "FAKE_GH_FAIL")
        pipeline.escalate(self.run_, "T-001", "again")  # gh down: no exception
        self.assertEqual(len(self.run_.read_json("escalations.json")), 2)

    def test_no_issue_no_comment(self):
        pipeline.escalate(self.run_, "T-001", "x")
        pipeline.escalate(self.run_, None, "budget")
        self.assertEqual(self.calls(), [])


class TestPr(TrackerBase):
    def setUp(self):
        super().setUp()
        self.approve_all()
        self.branch = self.run_.read_json("run.json")["branch"]
        git(self.repo, "branch", self.branch)

    def pr(self):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = cli.main(["pr", "--run", self.run_.run_id])
        return rc, err.getvalue()

    def test_refuses_without_final_approval_and_does_not_push(self):
        rc, err = self.pr()
        self.assertEqual(rc, 1)
        self.assertIn("final not approved", err)
        self.assertEqual(git(self.remote, "branch", "--list"), "")
        self.assertEqual([c for c in self.calls() if c[0] == "pr"], [])

    def test_refuses_when_tickets_change_after_final(self):
        planreview.approve(self.run_, "final")
        self.tickets([])
        self.assertEqual(self.pr()[0], 1)
        self.assertEqual(git(self.remote, "branch", "--list"), "")

    def test_pushes_branch_and_opens_pr_with_closes_lines(self):
        tmpl_dir = os.path.join(self.repo, ".github")
        os.makedirs(tmpl_dir, exist_ok=True)
        with open(os.path.join(tmpl_dir, "pull_request_template.md"), "w") as f:
            f.write("<!-- PR Template Preamble -->\nTemplate content for PR.\n\n## Custom Template\n- [ ] Tests pass\n")

        round_dir = self.run_.path("plan-review/round-1")
        os.makedirs(round_dir, exist_ok=True)
        with open(os.path.join(round_dir, "result.json"), "w") as f:
            json.dump({
                "verdict": "changes_requested",
                "findings": [
                    {"severity": "major", "section": "2", "issue": "Missing edge case handling", "suggestion": "Add check"},
                    {"severity": "minor", "section": "3", "issue": "Typo in comment", "suggestion": "Fix spelling"},
                ],
                "summary": "Fix issues",
            }, f)
        with open(self.run_.path("plan-review/arbitration.md"), "w") as f:
            f.write("ACCEPT: Typo in comment\nREJECT: Missing edge case handling\n")

        planreview.approve(self.run_, "final")
        rc, err = self.pr()
        self.assertEqual(rc, 0, err)
        self.assertIn(self.branch, git(self.remote, "branch", "--list"))
        pr = [c for c in self.calls() if c[:2] == ["pr", "create"]][0]
        self.assertEqual(pr[pr.index("--base") + 1], "main")
        self.assertEqual(pr[pr.index("--head") + 1], self.branch)
        title = pr[pr.index("--title") + 1]
        self.assertEqual(title, "Awesome Tracker Feature")
        self.assertNotEqual(title, "yoke: %s" % self.run_.run_id)
        body = pr[pr.index("--body") + 1]
        self.assertIn("Template content for PR.", body)
        self.assertIn("## Custom Template", body)
        self.assertIn("Closes #40", body)
        self.assertIn("Closes #41", body)
        self.assertIn("## Unresolved review findings", body)
        self.assertIn("Missing edge case handling", body)
        self.assertNotIn("Typo in comment", body)
        self.assertEqual(self.run_.read_json("run.json")["pr_url"], "https://github.com/o/r/pull/7")

    def test_pr_body_fill_fallback_never_blocks(self):
        # Force a failure during fill; verify pr_body completes and logs event
        body = tracker.pr_body(self.run_)
        self.assertIn("Closes #40", body)
        self.assertIn("Closes #41", body)
        events = []
        with open(self.run_.path("events.jsonl")) as f:
            for l in f:
                if l.strip():
                    events.append(json.loads(l))
        self.assertTrue(any(e.get("kind") == "pr_fill_fallback" for e in events))

    def test_pr_body_unresolved_findings_ambiguous_arbitration_fallback(self):
        round_dir = self.run_.path("plan-review/round-1")
        os.makedirs(round_dir, exist_ok=True)
        with open(os.path.join(round_dir, "result.json"), "w") as f:
            json.dump({
                "verdict": "changes_requested",
                "findings": [{"severity": "major", "section": "1", "issue": "ambiguous finding", "suggestion": "s"}],
                "summary": "s",
            }, f)
        body = tracker.pr_body(self.run_)
        self.assertIn("## Unresolved review findings", body)
        self.assertIn("ambiguous finding", body)

    def test_pr_body_unresolved_findings_with_arbitration(self):
        round_dir = self.run_.path("plan-review/round-1")
        os.makedirs(round_dir, exist_ok=True)
        with open(os.path.join(round_dir, "result.json"), "w") as f:
            json.dump({
                "verdict": "changes_requested",
                "findings": [
                    {"severity": "major", "section": "1", "issue": "accepted issue", "suggestion": "s1"},
                    {"severity": "minor", "section": "2", "issue": "rejected issue", "suggestion": "s2"},
                ],
                "summary": "s",
            }, f)
        with open(self.run_.path("plan-review/arbitration.md"), "w") as f:
            f.write("ACCEPT: accepted issue\nREJECT: rejected issue\n")
        body = tracker.pr_body(self.run_)
        self.assertIn("## Unresolved review findings", body)
        self.assertIn("rejected issue", body)
        self.assertNotIn("accepted issue", body)

    def test_pr_body_unresolved_findings_max_20(self):
        round_dir = self.run_.path("plan-review/round-1")
        os.makedirs(round_dir, exist_ok=True)
        findings = [
            {"severity": "minor", "section": "1", "issue": f"finding {i}", "suggestion": "s"}
            for i in range(25)
        ]
        with open(os.path.join(round_dir, "result.json"), "w") as f:
            json.dump({
                "verdict": "changes_requested",
                "findings": findings,
                "summary": "s",
            }, f)
        body = tracker.pr_body(self.run_)
        self.assertIn("## Unresolved review findings", body)
        for i in range(20):
            self.assertIn(f"finding {i}", body)
        for i in range(20, 25):
            self.assertNotIn(f"finding {i}", body)

    def test_pr_body_without_template_uses_builtin_structure(self):
        body = tracker.pr_body(self.run_)
        self.assertIn("## Summary", body)
        self.assertIn("## Why", body)
        self.assertIn("## What changed", body)
        self.assertIn("## Verification", body)
        self.assertIn("## Review focus", body)
        self.assertIn("Closes #40", body)
        self.assertIn("Closes #41", body)

    def test_pr_body_no_unresolved_findings_when_none_exist(self):
        body = tracker.pr_body(self.run_)
        self.assertNotIn("## Unresolved review findings", body)

    def test_pr_body_structure_and_blank_lines(self):
        tmpl_dir = os.path.join(self.repo, ".github")
        os.makedirs(tmpl_dir, exist_ok=True)
        with open(os.path.join(tmpl_dir, "pull_request_template.md"), "w") as f:
            f.write("Template text\n")

        round_dir = self.run_.path("plan-review/round-1")
        os.makedirs(round_dir, exist_ok=True)
        with open(os.path.join(round_dir, "result.json"), "w") as f:
            json.dump({
                "verdict": "changes_requested",
                "findings": [{"severity": "major", "section": "1", "issue": "some issue", "suggestion": "s"}],
                "summary": "s",
            }, f)

        body = tracker.pr_body(self.run_)
        expected_substring = "Template text\n\nCloses #40\nCloses #41\n\n## Unresolved review findings\n- [major] section 1: some issue (s)\n"
        self.assertIn(expected_substring, body)

    def test_local_tracker_refuses(self):
        planreview.approve(self.run_, "final")
        self.run_.update(config=dict(self.run_.cfg, tracker="local"))
        rc, err = self.pr()
        self.assertEqual(rc, 1)
        self.assertIn("github", err)


if __name__ == "__main__":
    unittest.main()
