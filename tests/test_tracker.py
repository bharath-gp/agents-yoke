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
fail = os.environ.get("FAKE_GH_FAIL")
if fail and (fail == key or fail == a[0]):
    sys.stderr.write("boom\\n")
    sys.exit(1)
if key == "issue create":
    with open(os.environ["FAKE_GH_LOG"]) as _f:
        n = sum(1 for l in _f if l.startswith('["issue", "create"')) + 39
    print("https://github.com/o/r/issues/%%d" %% n)
elif a[0] == "api":
    if os.environ.get("FAKE_GH_422") and "-f" in a and any("title=" in x for x in a):
        sys.stderr.write("HTTP 422: Validation Failed (already_exists)\\n")
        sys.exit(1)
    if "--paginate" in a:
        print(os.environ.get("FAKE_GH_MILESTONES", '[{"title": "Foundation", "number": 1}, {"title": "Execution", "number": 2}]'))
    elif "-X" in a and "PATCH" in a:
        print('{"state": "closed"}')
    elif "-q" in a and ".number" in a:
        with open(os.environ["FAKE_GH_LOG"]) as _f:
            cnt = sum(1 for l in _f if '"api"' in l and '"-q"' in l and '".number"' in l)
        print(str(cnt))
    else:
        print("1")
elif key == "repo view":
    print("main")
elif key == "pr list":
    print(os.environ.get("FAKE_GH_PR", ""))
elif key == "pr create":
    print("https://github.com/o/r/pull/7")
"""


class TrackerBase(Base):
    def setUp(self):
        super().setUp()
        old_env = {k: os.environ.get(k) for k in ENV}
        for k, v in ENV.items():
            os.environ[k] = v
        self.addCleanup(self._restore_env, old_env)
        git_config_keys = [k for k in os.environ if k.startswith("GIT_CONFIG_")]
        old_git_config = {k: os.environ[k] for k in git_config_keys}
        for k in git_config_keys:
            os.environ.pop(k, None)
        self.addCleanup(self._restore_git_config, old_git_config)
        git(self.repo, "checkout", "-q", "-b", "main")
        with open(os.path.join(self.repo, "a.txt"), "w") as f:
            f.write("a\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "init")
        self.remote = os.path.join(self.tmp, "remote.git")
        subprocess.run(["git", "init", "-q", "--bare", self.remote], check=True)
        git(self.repo, "remote", "add", "origin", self.remote)
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        gh = os.path.join(self.bin, "gh")
        with open(gh, "w") as f:
            f.write(SHIM % sys.executable)
        os.chmod(gh, os.stat(gh).st_mode | stat.S_IXUSR)
        for agent in ("claude", "agy"):  # PR body fill must fall back offline, never spend real agent quota
            p = os.path.join(self.bin, agent)
            with open(p, "w") as f:
                f.write("#!%s\nimport sys\nsys.exit(1)\n" % sys.executable)
            os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)
        self.log = os.path.join(self.tmp, "gh.log")
        for k, v in (("YOKE_HOME", self.tmp), ("PATH", self.bin + os.pathsep + os.environ["PATH"]), ("FAKE_GH_LOG", self.log)):
            old = os.environ.get(k)
            os.environ[k] = v
            self.addCleanup(lambda k=k, old=old: os.environ.__setitem__(k, old) if old else os.environ.pop(k, None))
        self.run_.update(config=dict(self.run_.cfg, tracker="github"))
        self.plan()
        self.tickets([
            {"id": "T-001", "title": "one", "type": "feature", "description": "d", "exit_criteria": ["works"],
             "depends_on": [], "files_hint": ["a.py"], "plan_excerpt": "x"},
            {"id": "T-002", "title": "two", "type": "chore", "description": "d", "exit_criteria": ["ok"],
             "depends_on": ["T-001"], "files_hint": [], "plan_excerpt": "y"}])

    def _restore_env(self, old_env):
        for k, old in old_env.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old

    def _restore_git_config(self, old_git_config):
        for k in list(os.environ):
            if k.startswith("GIT_CONFIG_") and k not in old_git_config:
                os.environ.pop(k, None)
        os.environ.update(old_git_config)

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
        self.addCleanup(os.environ.pop, "FAKE_GH_FAIL", None)
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
        self.addCleanup(os.environ.pop, "FAKE_GH_FAIL", None)
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
        planreview.approve(self.run_, "final")
        rc, _ = self.pr()
        self.assertEqual(rc, 0)
        self.assertIn(self.branch, git(self.remote, "branch", "--list"))
        pr = [c for c in self.calls() if c[:2] == ["pr", "create"]][0]
        self.assertEqual(pr[pr.index("--base") + 1], "main")
        self.assertEqual(pr[pr.index("--head") + 1], self.branch)
        body = pr[pr.index("--body") + 1]
        self.assertIn("Closes #40", body)
        self.assertIn("Closes #41", body)
        self.assertNotIn("Unresolved review findings", body)
        self.assertEqual(self.run_.read_json("run.json")["pr_url"], "https://github.com/o/r/pull/7")

    def test_existing_pr_is_pushed_not_recreated(self):
        planreview.approve(self.run_, "final")
        os.environ["FAKE_GH_PR"] = "https://github.com/o/r/pull/9"
        self.addCleanup(os.environ.pop, "FAKE_GH_PR", None)
        rc, _ = self.pr()
        self.assertEqual(rc, 0)
        self.assertEqual(self.run_.read_json("run.json")["pr_url"], "https://github.com/o/r/pull/9")
        self.assertEqual([c for c in self.calls() if c[:2] in (["pr", "create"], ["pr", "edit"])], [])
        self.assertIn(self.branch, git(self.remote, "branch", "--list"))

    def test_local_tracker_refuses(self):
        planreview.approve(self.run_, "final")
        self.run_.update(config=dict(self.run_.cfg, tracker="local"))
        rc, err = self.pr()
        self.assertEqual(rc, 1)
        self.assertIn("github", err)

    def test_pr_body_without_template_uses_builtin_structure(self):
        body = tracker.pr_body(self.run_)
        for h in ("## Summary", "## Why", "## What changed", "## Verification", "## Review focus"):
            self.assertIn(h, body)
        self.assertIn("Closes #40", body)

    def test_pr_body_uses_repo_template(self):
        os.makedirs(os.path.join(self.repo, ".github"))
        with open(os.path.join(self.repo, ".github", "pull_request_template.md"), "w") as f:
            f.write("## What / why\n\n## Checklist\n- [ ] Tests/checks pass locally\n")
        body = tracker.pr_body(self.run_)
        self.assertIn("## What / why", body)
        self.assertIn("- [ ] Tests/checks pass locally", body)
        self.assertIn("Closes #40", body)

    def test_pr_body_unresolved_findings(self):
        planreview.approve(self.run_, "final")
        self.run_.write_json("tickets/T-001/state.json", {
            "status": "closed",
            "review": {
                "rounds": 2,
                "unresolved": [
                    {"file": "yoke/foo.py", "line": 42, "severity": "major",
                     "comment": "critical bug in parsing", "suggestion": "handle empty input"}
                ]
            }
        })
        rc, err = self.pr()
        self.assertEqual(rc, 0, err)
        body = [c for c in self.calls() if c[:2] == ["pr", "create"]][-1]
        body_text = body[body.index("--body") + 1]
        self.assertIn("Unresolved review findings", body_text)
        self.assertIn("critical bug in parsing", body_text)
        self.assertIn("handle empty input", body_text)

    def test_pr_body_skips_unmerged_ticket_findings(self):
        self.run_.write_json("tickets/T-001/state.json", {
            "status": "in_progress",
            "review": {"rounds": 2, "unresolved": [
                {"file": "a.py", "line": 1, "severity": "major", "comment": "stale finding", "suggestion": ""}]}})
        self.assertNotIn("stale finding", tracker.pr_body(self.run_))


class TestMilestones(TrackerBase):
    def setUp(self):
        super().setUp()
        git(self.repo, "remote", "set-url", "origin", "git@github.com:o/r.git")
        self.ms_doc = {
            "milestones": [
                {"id": "MS-01", "title": "Foundation", "exit_check": "true"},
                {"id": "MS-02", "title": "Execution", "exit_check": "true"}
            ],
            "tickets": [
                {"id": "T-001", "title": "one", "type": "feature", "description": "d",
                 "exit_criteria": ["works"], "depends_on": [], "files_hint": ["a.py"],
                 "plan_excerpt": "x", "milestone": "MS-01"},
                {"id": "T-002", "title": "two", "type": "chore", "description": "d",
                 "exit_criteria": ["ok"], "depends_on": ["T-001"], "files_hint": [],
                 "plan_excerpt": "y", "milestone": "MS-02"}
            ]
        }

    def test_milestones_created_once_and_issues_assigned(self):
        self.run_.write_json("tickets.json", self.ms_doc)
        self.assertEqual(self.approve_all(), 0)
        ms = self.run_.read_json("gh_milestones.json")
        self.assertEqual(ms, {"MS-01": 1, "MS-02": 2})
        api_calls = [c for c in self.calls() if c[0] == "api" and "-q" in c]
        self.assertEqual(len(api_calls), 2)
        self.assertIn("title=Foundation", api_calls[0])
        self.assertIn("title=Execution", api_calls[1])
        creates = [c for c in self.calls() if c[:2] == ["issue", "create"]]
        self.assertEqual(len(creates), 2)
        self.assertIn("--milestone", creates[0])
        self.assertEqual(creates[0][creates[0].index("--milestone") + 1], "Foundation")
        self.assertIn("--milestone", creates[1])
        self.assertEqual(creates[1][creates[1].index("--milestone") + 1], "Execution")

    def test_rerun_does_not_duplicate(self):
        self.run_.write_json("tickets.json", self.ms_doc)
        self.assertEqual(self.approve_all(), 0)
        self.assertEqual(self.approve_all(), 0)
        api_calls = [c for c in self.calls() if c[0] == "api" and "-q" in c]
        self.assertEqual(len(api_calls), 2)
        creates = [c for c in self.calls() if c[:2] == ["issue", "create"]]
        self.assertEqual(len(creates), 2)

    def test_422_looks_up_existing_milestone_by_title(self):
        self.run_.write_json("tickets.json", self.ms_doc)
        os.environ["FAKE_GH_422"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_GH_422", None)
        self.assertEqual(self.approve_all(), 0)
        ms = self.run_.read_json("gh_milestones.json")
        self.assertEqual(ms.get("MS-01"), 1)
        paginates = [c for c in self.calls() if c[0] == "api" and "--paginate" in c]
        self.assertTrue(len(paginates) >= 1)

    def test_milestone_closed_on_gate_pass(self):
        self.run_.write_json("tickets.json", self.ms_doc)
        self.approve_all()
        tracker.close_milestone(self.run_, "MS-01")
        patches = [c for c in self.calls() if c[:3] == ["api", "-X", "PATCH"]]
        self.assertEqual(len(patches), 1)
        self.assertIn("repos/o/r/milestones/1", patches[0])
        self.assertIn("state=closed", patches[0])
        events = [json.loads(line) for line in open(self.run_.path("events.jsonl"))]
        self.assertTrue(any(e.get("kind") == "milestone_closed" and e.get("milestone") == "MS-01" for e in events))

    def test_fix_ticket_issue_created_in_same_milestone(self):
        from yoke import milestones
        self.run_.write_json("tickets.json", self.ms_doc)
        self.approve_all()
        fix = milestones.make_fix_ticket(id="F-001", milestone={"id": "MS-01", "title": "Foundation", "exit_check": "true"})
        tracker.create_fix_issue(self.run_, fix)
        issues = self.run_.read_json("issues.json")
        self.assertIn("F-001", issues)
        fix_creates = [c for c in self.calls() if c[:2] == ["issue", "create"] and any("F-001" in x for x in c)]
        self.assertEqual(len(fix_creates), 1)
        self.assertEqual(fix_creates[0][fix_creates[0].index("--milestone") + 1], "Foundation")

    def test_gh_failure_at_gate_time_logs_tracker_error_and_run_continues(self):
        from yoke import milestones
        self.run_.write_json("tickets.json", self.ms_doc)
        self.approve_all()
        os.environ["FAKE_GH_FAIL"] = "api"
        self.addCleanup(os.environ.pop, "FAKE_GH_FAIL", None)
        tracker.close_milestone(self.run_, "MS-01")
        events = [json.loads(line) for line in open(self.run_.path("events.jsonl"))]
        self.assertTrue(any(e.get("kind") == "tracker_error" and e.get("milestone") == "MS-01" for e in events))

        os.environ["FAKE_GH_FAIL"] = "issue create"
        fix = milestones.make_fix_ticket(id="F-002", milestone={"id": "MS-01", "title": "Foundation", "exit_check": "true"})
        tracker.create_fix_issue(self.run_, fix)
        os.environ.pop("FAKE_GH_FAIL", None)
        events = [json.loads(line) for line in open(self.run_.path("events.jsonl"))]
        self.assertTrue(any(e.get("kind") == "tracker_error" and e.get("ticket") == "F-002" for e in events))

    def test_scheduler_gate_gh_failure_logs_tracker_error_and_run_continues(self):
        from yoke import scheduler
        self.run_.write_json("tickets.json", self.ms_doc)
        self.approve_all()
        self.run_.write_json("tickets/T-001/state.json", {"status": "closed", "attempt": 1, "sessions": {}})
        self.run_.write_json("tickets/T-002/state.json", {"status": "closed", "attempt": 1, "sessions": {}})
        os.environ["FAKE_GH_FAIL"] = "api"
        self.addCleanup(os.environ.pop, "FAKE_GH_FAIL", None)
        self.assertEqual(scheduler.run(self.run_), "done")
        events = [json.loads(line) for line in open(self.run_.path("events.jsonl"))]
        self.assertTrue(any(e.get("kind") == "tracker_error" and e.get("milestone") == "MS-01" for e in events))
        self.assertTrue(any(e.get("kind") == "milestone_passed" and e.get("milestone") == "MS-01" for e in events))

    def test_no_milestone_tickets_json_behaves_as_before(self):
        self.run_.write_json("tickets.json", {
            "tickets": [
                {"id": "T-001", "title": "t1", "type": "feature", "description": "d",
                 "exit_criteria": ["ok"], "depends_on": [], "files_hint": [], "plan_excerpt": "p"}
            ]
        })
        self.assertEqual(self.approve_all(), 0)
        self.assertFalse(os.path.exists(self.run_.path("gh_milestones.json")))
        creates = [c for c in self.calls() if c[:2] == ["issue", "create"]]
        self.assertNotIn("--milestone", creates[-1])

    def test_pr_body_produces_closes_only_for_ticket_issues(self):
        self.run_.write_json("gh_milestones.json", {"MS-01": 1, "MS-02": 2})
        self.run_.write_json("issues.json", {"T-001": {"number": 40, "url": "https://github.com/o/r/issues/40"}})
        body = tracker.pr_body(self.run_)
        self.assertIn("Closes #40", body)
        self.assertNotIn("Closes #1", body)
        self.assertNotIn("Closes #2", body)


if __name__ == "__main__":
    unittest.main()
