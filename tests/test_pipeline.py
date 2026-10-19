import json
import os
import subprocess
import unittest

from tests.test_core import FAKE_CLI
from tests.test_gitops import GitBase, git
from yoke import adapters, pipeline, planreview, scheduler

CODE = {"status": "done", "summary": "s", "files_changed": [], "assumptions": [], "open_questions": [],
        "notes_for_qa": ""}
PASS_CMD = "test -f %s"


def qa(cmd, status="pass", failures=()):
    return {"status": status, "test_command": cmd, "tests_added": [], "commands_run": [],
            "failures": list(failures), "summary": "s"}


def val(ok=True, met=True):
    return {"verdict": "approve" if ok else "reject", "issues": [], "summary": "s",
            "feedback_for_coder": "" if ok else "fix the thing",
            "exit_criteria": [{"criterion": "c", "met": met, "evidence": "e"}]}


DOCS = {"status": "done", "files_changed": [], "summary": "s"}
REV = {"verdict": "approve", "findings": [], "summary": "s"}


def resp(obj, session=None, **extra):
    d = {"structured_output": obj}
    if session:
        d["session_id"] = session
    d.update(extra)
    return d


def ticket(n, type_="chore", deps=()):
    return {"id": "T-%03d" % n, "title": "ticket %d" % n, "type": type_, "description": "d",
            "exit_criteria": ["c"], "depends_on": list(deps), "files_hint": [], "plan_excerpt": "pe"}


class PipeBase(GitBase):
    def setUp(self):
        super().setUp()
        os.environ["YOKE_HOME"] = self.tmp
        self.addCleanup(os.environ.pop, "YOKE_HOME", None)
        self.run_ = planreview.init_run(self.repo, None, {"max_attempts_per_ticket": 2}, home=self.tmp)
        self.script = os.path.join(self.tmp, "script.json")
        self.env = {"FAKE_SCRIPT": self.script}
        with open(self.run_.path("plan.md"), "w") as f:
            f.write("plan")

    def setup_tickets(self, tickets, script):
        self.run_.write_json("tickets.json", {"tickets": tickets})
        with open(self.script, "w") as f:
            json.dump(script, f)
        for ext in (".pos", ".lock"):
            p = self.script + ext
            if os.path.exists(p):
                os.remove(p)
        planreview.approve(self.run_, "plan")
        planreview.approve(self.run_, "tickets")

    def go(self):
        return scheduler.run(self.run_, adapter_for=lambda role: adapters.FakeAdapter(FAKE_CLI), env=self.env)

    def tstate(self, n):
        return self.run_.read_json("tickets/T-%03d/state.json" % n)

    def log(self):
        return git(self.repo, "log", "--format=%s", "--first-parent", self.run_.read_json("run.json")["branch"]).splitlines()


class TestHappy(PipeBase):
    def test_three_tickets_three_linear_commits(self):
        ok = "test -f f%d.txt"
        self.setup_tickets(
            [ticket(1), ticket(2, deps=["T-001"]), ticket(3)],
            {"code": [resp(CODE, "c%d" % i, write={"f%d.txt" % i: "x"}) for i in (1, 2, 3)],
             "review": [resp(REV)] * 3,
             "validate": [resp(val())] * 3,
             "router": [{"exit": 1}]})
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.log(), ["T-003: ticket 3", "T-002: ticket 2", "T-001: ticket 1", "init"])
        branch = self.run_.read_json("run.json")["branch"]
        self.assertEqual(git(self.repo, "rev-list", "--merges", "--count", branch), "0")
        self.assertEqual(self.tstate(1)["status"], "closed")
        self.assertFalse(os.path.exists(self.run_.path("worktrees/T-001")))
        self.assertEqual(git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")  # user checkout untouched
        with open(self.run_.path("events.jsonl")) as f:
            gates = [json.loads(l) for l in f if '"close_gate"' in l]
        self.assertEqual(len(gates), 3)
        self.assertTrue(all(g["ok"] for g in gates))
        with open(self.run_.path("map.md")) as f:
            map_lines = f.read().splitlines()
        self.assertEqual(sorted(map_lines), [
            "- T-001 ticket 1: s",
            "- T-002 ticket 2: s",
            "- T-003 ticket 3: s",
        ])

    def test_refuses_without_approval(self):
        self.run_.write_json("tickets.json", {"tickets": [ticket(1)]})
        with self.assertRaises(planreview.ApprovalError):
            self.go()


class TestRetries(PipeBase):
    def test_qa_fail_then_pass_with_session_resume(self):
        self.setup_tickets(
            [ticket(1, "bugfix")],
            {"code": [resp(CODE, "cs", write={"f.txt": "1"}), resp(CODE, "cs", write={"g.txt": "1"})],
             "review": [resp(REV)] * 2,
             "qa": [resp(qa("true", "fail", ["boom"])), resp(qa("test -f g.txt"))],
             "validate": [resp(val())], "docs": [resp(DOCS)], "router": [{"exit": 1}]})
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["attempt"], 2)
        with open(self.run_.path("tickets/T-001/attempt-2/feedback.md")) as f:
            fb = f.read()
        self.assertIn("boom", fb)
        cmd = self.run_.read_json("tickets/T-001/attempt-2/code/command.json")
        self.assertIn("--resume", cmd)
        self.assertIn("cs", cmd)

    def test_failed_stage_call_retried_in_place_without_new_attempt(self):
        self.setup_tickets(
            [ticket(1, "bugfix")],
            {"code": [resp(CODE, "cs", write={"f.txt": "1"})],
             "review": [resp(REV)],
             "qa": [{"exit": 3}, resp(qa("test -f f.txt"))],
             "validate": [resp(val())], "docs": [resp(DOCS)], "router": [{"exit": 1}]})
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["attempt"], 1)
        self.assertTrue(os.path.isdir(self.run_.path("tickets/T-001/attempt-1/qa-retry")))

    def test_validate_reject_then_approve(self):
        self.setup_tickets(
            [ticket(1)],
            {"code": [resp(CODE, "cs", write={"f.txt": "1"}), resp(CODE, "cs")],
             "review": [resp(REV)] * 2,
             "validate": [resp(val(False)), resp(val())], "router": [{"exit": 1}]})
        self.assertEqual(self.go(), "done")
        with open(self.run_.path("tickets/T-001/attempt-2/feedback.md")) as f:
            self.assertIn("fix the thing", f.read())
        self.assertIn("--resume", self.run_.read_json("tickets/T-001/attempt-2/code/command.json"))

    def test_approve_with_unmet_criterion_rejected(self):
        self.setup_tickets(
            [ticket(1)],
            {"code": [resp(CODE, "cs", write={"f.txt": "1"})],
             "review": [resp(REV)] * 2,
             "validate": [resp(val(True, met=False)), resp(val())], "router": [{"exit": 1}]})
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["attempt"], 2)

    def test_runner_rerun_overrides_agent_pass(self):
        self.setup_tickets(
            [ticket(1, "test")],
            {"qa": [resp(qa("false"), write={"t.txt": "1"}), resp(qa("true"))],
             "validate": [resp(val())], "router": [{"exit": 1}]})
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["attempt"], 2)
        with open(self.run_.path("tickets/T-001/attempt-2/feedback.md")) as f:
            self.assertIn("exit", f.read())

    def test_max_attempts_blocks_and_independent_ticket_closes(self):
        self.setup_tickets(
            [ticket(1), ticket(2, deps=["T-001"]), ticket(3)],
            {"code": [resp(CODE, "a", write={"x": "1"}), resp(CODE, "a"), resp(CODE, "b", write={"y": "1"})],
             "review": [resp(REV)] * 3,
             "validate": [resp(val(False)), resp(val(False)), resp(val())], "router": [{"exit": 1}]})
        self.assertEqual(self.go(), "needs_user")
        self.assertEqual(self.tstate(1)["status"], "blocked")
        self.assertEqual(self.tstate(3)["status"], "closed")
        self.assertEqual(self.run_.read_json("tickets/T-002/state.json", {}).get("status", "pending"), "pending")
        esc = self.run_.read_json("escalations.json")
        self.assertEqual([e["ticket"] for e in esc], ["T-001"])
        self.assertEqual(self.log(), ["T-003: ticket 3", "init"])

    def test_coder_blocked_escalates_immediately(self):
        blocked = dict(CODE, status="blocked", blocked_reason="need creds")
        self.setup_tickets([ticket(1)], {"code": [resp(blocked)]})
        self.assertEqual(self.go(), "needs_user")
        self.assertEqual(self.tstate(1)["attempt"], 1)
        self.assertIn("need creds", self.run_.read_json("escalations.json")[0]["reason"])

    def test_agent_commit_detected(self):
        self.setup_tickets(
            [ticket(1)],
            {"code": [resp(CODE, "cs", write={"f.txt": "1"}, commit=True)],
             "review": [resp(REV)],
             "validate": [resp(val())], "router": [{"exit": 1}]})
        self.assertEqual(self.go(), "done")
        with open(self.run_.path("events.jsonl")) as f:
            self.assertIn('"agent_commit"', f.read())


class TestUnblockAndLock(PipeBase):
    def test_unblock_note_then_retry_closes(self):
        blocked = dict(CODE, status="blocked", blocked_reason="why")
        self.setup_tickets(
            [ticket(1)],
            {"code": [resp(blocked), resp(CODE, "s", write={"f": "1"})],
             "review": [resp(REV)],
             "validate": [resp(val())],
             "router": [{"exit": 1}]})
        self.assertEqual(self.go(), "needs_user")
        scheduler.unblock(self.run_, "T-001", note="use the stub")
        self.assertEqual(self.run_.read_json("escalations.json"), [])
        self.assertEqual(self.go(), "done")

    def test_skip_blocks_dependents(self):
        self.setup_tickets([ticket(1), ticket(2, deps=["T-001"])], {"code": [resp(CODE)]})
        scheduler.unblock(self.run_, "T-001", skip=True)
        self.assertEqual(self.go(), "needs_user")
        self.assertEqual(self.tstate(2)["status"], "blocked")
        self.assertIn("T-001", self.run_.read_json("escalations.json")[0]["reason"])

    def test_live_lock_refused_stale_lock_taken(self):
        self.setup_tickets([ticket(1)], {"code": [resp(CODE)]})
        proc = subprocess.Popen(["sleep", "30"])
        self.addCleanup(proc.kill)
        with open(self.run_.path("runner.lock"), "w") as f:
            f.write(str(proc.pid))
        with self.assertRaises(scheduler.LockError):
            self.go()
        proc.kill()
        proc.wait()
        self.assertEqual(self.go(), "needs_user")  # stale lock taken; ticket blocked after code w/o diff

    def test_budget_exhausted_stops(self):
        self.setup_tickets([ticket(1)], {"code": [resp(CODE, "s", write={"f": "1"})], "validate": [resp(val())]})
        self.run_.update(config=dict(self.run_.cfg, max_agent_calls_total=1))
        self.assertEqual(self.go(), "needs_user")
        self.assertIn("budget", self.run_.read_json("escalations.json")[0]["reason"])


if __name__ == "__main__":
    unittest.main()


class TestCli(PipeBase):
    def cli(self, *argv):
        import contextlib
        import io
        from yoke import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = cli.main(list(argv) + ["--run", self.run_.run_id] if argv[0] != "unblock" else
                          list(argv) + ["--run", self.run_.run_id])
        return rc, out.getvalue()

    def test_run_refused_unapproved_status_stop_unblock(self):
        self.run_.write_json("tickets.json", {"tickets": [ticket(1)]})
        rc, out = self.cli("run")
        self.assertEqual(rc, 1)
        self.assertIn("not approved", out)
        rc, out = self.cli("status")
        self.assertIn("tickets 1", out)
        self.assertEqual(self.cli("stop"), (0, "no runner active\n"))
        self.assertEqual(self.cli("unblock", "T-001")[0], 1)  # needs an action flag
        self.assertEqual(self.cli("unblock", "T-001", "--skip")[0], 0)
        self.assertEqual(self.tstate(1)["status"], "skipped")
