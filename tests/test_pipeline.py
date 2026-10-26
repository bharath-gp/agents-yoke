import json
import os
import subprocess
import tempfile
import time
import types
import unittest
from unittest import mock

from tests.test_core import FAKE_CLI
from tests.test_gitops import GitBase, git
from yoke import adapters, config, pipeline, planreview, scheduler

os.environ.setdefault("YOKE_OLLAMA_URL", "http://127.0.0.1:9")

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
        self.run_ = planreview.init_run(self.repo, None, {"max_attempts_per_ticket": 2, "review_lenses": [], "review_mode": "independent"}, home=self.tmp)
        self.addCleanup(pipeline.clear_pause_state, self.run_)
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

    def test_close_commit_message_default_mode(self):
        self.run_.update(config=config.deep_merge(
            self.run_.cfg, {"router": {"commit_message": "default"}}))
        val_res = val()
        val_res["summary"] = "validation succeeded smoothly"
        self.setup_tickets(
            [ticket(1)],
            {"code": [resp(CODE, "c1", write={"f1.txt": "x"})],
             "review": [resp(REV)],
             "validate": [resp(val_res)]})
        self.assertEqual(self.go(), "done")
        branch = self.run_.read_json("run.json")["branch"]
        msg = git(self.repo, "log", "-1", "--format=%B", branch).strip()
        self.assertEqual(msg, "T-001: ticket 1\n\nvalidation succeeded smoothly\n\nYoke-Ticket: T-001")

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


class TestValidateEvidence(PipeBase):
    def test_validate_prompt_has_rerun_evidence(self):
        self.assertEqual(pipeline._test_evidence(None, None), "No test command ran for this ticket type.")
        self.assertEqual(pipeline._test_evidence("x", None), "No test command ran for this ticket type.")
        self.assertEqual(pipeline._test_evidence(None, 0), "No test command ran for this ticket type.")
        self.assertEqual(
            pipeline._test_evidence("x", 1),
            "The runner ran `x` in this worktree and it exited 1.",
        )
        self.setup_tickets(
            [ticket(1, "test")],
            {"qa": [resp(qa(PASS_CMD % "f"), write={"f": "1"})],
             "validate": [resp(val())],
             "router": [{"exit": 1}]})
        self.assertEqual(self.go(), "done")
        with open(self.run_.path("tickets/T-001/attempt-1/validate/prompt.md")) as f:
            prompt = f.read()
        self.assertIn("test -f f", prompt)
        self.assertIn("exited 0", prompt)


class TestDebateStages(unittest.TestCase):
    def test_read_stages_include_debate(self):
        for stage in ("rebut", "reply", "judge"):
            self.assertIn(stage, pipeline.READ_STAGES)
        self.assertEqual(pipeline.STAGE_TIER, {"rebut": "cheap", "reply": "cheap", "judge": "plan"})


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


class TestSettingSources(PipeBase):
    def test_cfg_value_reaches_callspec(self):
        cfg = self.run_.cfg
        cfg.setdefault("agents", {}).setdefault("claude", {})["setting_sources"] = "project"
        self.run_.update(config=cfg)
        captured = []

        def fake_run_call(run, adapter, spec, **kwargs):
            captured.append(spec)
            return adapters.CallResult(structured_output={"status": "done"}, exit_code=0)

        with mock.patch("yoke.calls.run_call", side_effect=fake_run_call):
            pipeline._call(
                self.run_,
                adapter_for=lambda role: adapters.FakeAdapter(FAKE_CLI),
                env=self.env,
                ticket=ticket(1),
                stage="code",
                attempt=1,
                wt=self.repo,
                base="HEAD",
                mode="write",
                results={},
                feedback="",
            )
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].setting_sources, "project")

    def test_cfg_default_setting_sources_when_missing(self):
        cfg = self.run_.cfg
        if "agents" in cfg and "claude" in cfg["agents"]:
            cfg["agents"]["claude"].pop("setting_sources", None)
        self.run_.update(config=cfg)
        captured = []

        def fake_run_call(run, adapter, spec, **kwargs):
            captured.append(spec)
            return adapters.CallResult(structured_output={"status": "done"}, exit_code=0)

        with mock.patch("yoke.calls.run_call", side_effect=fake_run_call):
            pipeline._call(
                self.run_,
                adapter_for=lambda role: adapters.FakeAdapter(FAKE_CLI),
                env=self.env,
                ticket=ticket(1),
                stage="code",
                attempt=1,
                wt=self.repo,
                base="HEAD",
                mode="write",
                results={},
                feedback="",
            )
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].setting_sources, "project,local")


class TestRerun(unittest.TestCase):
    def test_timeout_returns_minus_one_fast(self):
        fake_run = types.SimpleNamespace(cfg={"timeouts_s": {"test": 1}, "kill_grace_s": 0.5})
        t0 = time.time()
        rc, text = pipeline._rerun(fake_run, "python3 -c 'import time; time.sleep(30)'", None)
        elapsed = time.time() - t0
        self.assertLess(elapsed, 5.0)
        self.assertEqual(rc, -1)
        self.assertTrue(text.startswith("timed out after"))

    def test_timeout_kills_children(self):
        fake_run = types.SimpleNamespace(cfg={"timeouts_s": {"test": 1}, "kill_grace_s": 0.5})
        with tempfile.TemporaryDirectory() as td:
            pidfile = os.path.join(td, "pid")
            cmd = "sh -c 'sleep 30 & echo $! > %s; wait'" % pidfile
            rc, text = pipeline._rerun(fake_run, cmd, td)
            self.assertEqual(rc, -1)
            self.assertTrue(text.startswith("timed out after"))
            self.assertTrue(os.path.exists(pidfile))
            with open(pidfile) as f:
                pid = int(f.read().strip())
            for _ in range(40):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.1)
            else:
                self.fail("child process %d survived timeout kill" % pid)

    def test_failure_text_unittest(self):
        noise = ["noise line %d" % i for i in range(50)]
        lines = (
            noise
            + ["=" * 70]
            + ["ERROR: test_x (tests.test_a.T)"]
            + ["-" * 70]
            + ["Traceback (most recent call last):", "  File 'test_a.py', line 10, in test_x", "KeyError: 'judge'"]
            + ["-" * 70]
            + ["Ran 3 tests in 0.1s"]
            + ["FAILED (errors=1)"]
        )
        output = "\n".join(lines)
        res = pipeline._failure_text(output)
        self.assertIn("KeyError: 'judge'", res)
        self.assertIn("Ran 3 tests", res)
        self.assertIn("FAILED (errors=1)", res)
        self.assertFalse(res.startswith("noise"))

    def test_failure_text_pytest(self):
        lines = [
            "noise line 1",
            "rootdir: /tmp",
            "FAILED tests/test_a.py::test_x - AssertionError",
            "E   assert 1 == 2",
            "noise line 2",
            "==== 1 failed, 2 passed in 0.1s ====",
        ]
        output = "\n".join(lines)
        res = pipeline._failure_text(output)
        expected = "FAILED tests/test_a.py::test_x - AssertionError\nE   assert 1 == 2\n==== 1 failed, 2 passed in 0.1s ===="
        self.assertEqual(res, expected)

    def test_failure_text_pytest_limit(self):
        lines = [
            "FAILED test_a.py::test_1 - AssertionError",
            "E   assert 1 == 2",
            "FAILED test_b.py::test_2 - AssertionError",
            "E   assert 3 == 4",
            "==== 2 failed in 0.1s ====",
        ]
        res = pipeline._failure_text("\n".join(lines), limit=50)
        self.assertLessEqual(len(res), 50)
        self.assertIn("2 failed", res)

    def test_failure_text_fallback(self):
        output = "a" * 5000
        res = pipeline._failure_text(output, limit=4000)
        self.assertEqual(res, output[-4000:])

    def test_rerun_non_timeout_and_defaults(self):
        fake_run = types.SimpleNamespace(cfg={})
        rc, text = pipeline._rerun(fake_run, "sh -c 'echo hi; exit 3'", None)
        self.assertEqual(rc, 3)
        self.assertIn("hi", text)

    def test_failure_text_unittest_multiple_blocks_and_limit(self):
        lines = (
            ["noise 1", "noise 2", "=" * 70]
            + ["FAIL: test_f (tests.test_a.T)"]
            + ["-" * 70]
            + ["Traceback (most recent call last):", "AssertionError: 1 != 2"]
            + ["=" * 70]
            + ["ERROR: test_e (tests.test_a.T)"]
            + ["-" * 70]
            + ["Traceback (most recent call last):", "ValueError: bad"]
            + ["-" * 70]
            + ["Ran 5 tests in 0.2s"]
            + ["FAILED (failures=1, errors=1)"]
        )
        res = pipeline._failure_text("\n".join(lines), limit=200)
        self.assertIn("ValueError: bad", res)
        self.assertIn("FAILED (failures=1, errors=1)", res)
        self.assertNotIn("noise", res)
        self.assertLessEqual(len(res), 200)

    def test_failure_text_edge_cases(self):
        self.assertEqual(pipeline._failure_text(""), "")
        passed_output = "==== 5 passed in 0.1s ===="
        self.assertEqual(pipeline._failure_text(passed_output), passed_output)


class TestTestFixLoop(PipeBase):
    def test_fix_events(self):
        with open(self.run_.path("events.jsonl")) as f:
            events = [json.loads(line) for line in f if line.strip()]
        return [e for e in events if e.get("kind") == "test_fix"]

    def test_fail_then_pass_closes_attempt_one(self):
        self.run_.update(config=dict(self.run_.cfg, test_command="test -f ok"))
        self.setup_tickets(
            [ticket(1)],
            {
                "code": [resp(CODE, "cs")],
                "code-t1": [resp(CODE, "cs", write={"ok": "1"})],
                "review": [resp(REV)],
                "validate": [resp(val())],
                "router": [{"exit": 1}],
            },
        )
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["status"], "closed")
        self.assertFalse(os.path.exists(self.run_.path("tickets/T-001/attempt-2")))
        with open(self.run_.path("tickets/T-001/attempt-1/code-t1/prompt.md")) as f:
            prompt_t1 = f.read()
        self.assertIn("Runner re-ran `test -f ok` after your change and it exited", prompt_t1)
        with open(self.run_.path("tickets/T-001/attempt-1/code-t1/command.json")) as f:
            cmd_t1 = json.load(f)
        self.assertIn("--resume", cmd_t1)
        self.assertIn("cs", cmd_t1)
        events = self.test_fix_events()
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["ticket"], "T-001")
        self.assertEqual(events[0]["attempt"], 1)
        self.assertEqual(events[0]["round"], 0)
        self.assertNotEqual(events[0]["rc"], 0)
        self.assertEqual(events[1]["ticket"], "T-001")
        self.assertEqual(events[1]["attempt"], 1)
        self.assertEqual(events[1]["round"], 1)
        self.assertEqual(events[1]["rc"], 0)

    def test_both_rounds_fail_starts_attempt_two(self):
        self.run_.update(config=dict(self.run_.cfg, test_command="test -f ok"))
        self.setup_tickets(
            [ticket(1)],
            {
                "code": [
                    resp(CODE, "cs"),
                    resp(CODE, "cs", write={"ok": "1"}),
                ],
                "code-t1": [resp(CODE, "cs")],
                "code-t2": [resp(CODE, "cs")],
                "review": [resp(REV)],
                "validate": [resp(val())],
                "router": [{"exit": 1}],
            },
        )
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["status"], "closed")
        self.assertEqual(self.tstate(1)["attempt"], 2)
        attempt1_fixes = [e for e in self.test_fix_events() if e.get("attempt") == 1]
        self.assertEqual(len(attempt1_fixes), 3)
        self.assertEqual([e["round"] for e in attempt1_fixes], [0, 1, 2])
        self.assertTrue(all(e["rc"] != 0 for e in attempt1_fixes))
        fb2 = pipeline._read_feedback(self.run_, "T-001", 2)
        self.assertIn("after the coder's fixes", fb2)

    def test_no_test_command_no_loop(self):
        self.setup_tickets(
            [ticket(1)],
            {
                "code": [resp(CODE, "cs", write={"f1.txt": "x"})],
                "review": [resp(REV)],
                "validate": [resp(val())],
                "router": [{"exit": 1}],
            },
        )
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["status"], "closed")
        self.assertEqual(len(self.test_fix_events()), 0)

    def test_zero_rounds_disables(self):
        self.run_.update(config=dict(self.run_.cfg, test_command="test -f ok", max_test_fix_rounds=0))
        self.setup_tickets(
            [ticket(1)],
            {
                "code": [resp(CODE, "cs", write={"f1.txt": "x"})],
                "review": [resp(REV)],
                "validate": [resp(val())],
                "router": [{"exit": 1}],
            },
        )
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["status"], "closed")
        self.assertEqual(len(self.test_fix_events()), 0)

    def test_guard_deny_returns_feedback(self):
        self.run_.update(config=dict(self.run_.cfg, test_command="test -f ok"))
        self.setup_tickets(
            [ticket(1)],
            {
                "code": [resp(CODE, "cs", write={"f1.txt": "x"})] * 2,
                "review": [resp(REV)],
                "validate": [resp(val())],
                "router": [{"exit": 1}],
            },
        )
        with mock.patch("yoke.guard.check_command", return_value={"decision": "deny", "reason": "danger"}):
            self.assertEqual(self.go(), "needs_user")
        self.assertEqual(self.tstate(1)["status"], "blocked")
        fb2 = pipeline._read_feedback(self.run_, "T-001", 2)
        self.assertTrue(fb2.startswith("Safety guard refused test command"))
        self.assertIn("danger", fb2)
        self.assertEqual(len(self.test_fix_events()), 0)

    def test_guard_ask_escalates(self):
        self.run_.update(config=dict(self.run_.cfg, test_command="test -f ok"))
        self.setup_tickets(
            [ticket(1)],
            {
                "code": [resp(CODE, "cs", write={"f1.txt": "x"})],
                "review": [resp(REV)],
                "validate": [resp(val())],
                "router": [{"exit": 1}],
            },
        )
        with mock.patch("yoke.guard.check_command", return_value={"decision": "ask", "reason": "operator required"}):
            self.assertEqual(self.go(), "needs_user")
        self.assertEqual(self.tstate(1)["status"], "blocked")
        esc = self.run_.read_json("escalations.json")
        self.assertTrue(any("safety guard: operator required" in e["reason"] for e in esc))
        self.assertEqual(len(self.test_fix_events()), 0)

    def test_code_call_failed_returns_feedback(self):
        self.run_.update(config=dict(self.run_.cfg, test_command="test -f ok"))
        self.setup_tickets(
            [ticket(1)],
            {
                "code": [
                    resp(CODE, "cs"),
                    resp(CODE, "cs", write={"ok": "1"}),
                ],
                "code-t1": [{"exit": 1}],
                "review": [resp(REV)],
                "validate": [resp(val())],
                "router": [{"exit": 1}],
            },
        )
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["status"], "closed")
        self.assertEqual(self.tstate(1)["attempt"], 2)
        fb2 = pipeline._read_feedback(self.run_, "T-001", 2)
        self.assertIn("code call failed: exit 1", fb2)

    def test_last_test_cmd_fallback_enters_loop(self):
        self.setup_tickets(
            [ticket(1, type_="refactor")],
            {
                "code": [
                    resp(CODE, "c1", write={"f1.txt": "x"}),
                    resp(CODE, "c1"),
                ],
                "code-t1": [resp(CODE, "c1", write={"ok": "1"})],
                "review": [resp(REV)] * 4,
                "qa": [
                    resp(qa("test -f ok", status="pass")),
                    resp(qa("test -f ok", status="pass")),
                ],
                "validate": [resp(val())] * 4,
                "router": [{"exit": 1}],
            },
        )
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["status"], "closed")
        self.assertEqual(self.tstate(1)["attempt"], 2)
        attempt1_fixes = [e for e in self.test_fix_events() if e.get("attempt") == 1]
        self.assertEqual(len(attempt1_fixes), 0)
        attempt2_fixes = [e for e in self.test_fix_events() if e.get("attempt") == 2]
        self.assertEqual(len(attempt2_fixes), 2)
        self.assertEqual(attempt2_fixes[0]["round"], 0)
        self.assertNotEqual(attempt2_fixes[0]["rc"], 0)
        self.assertEqual(attempt2_fixes[1]["round"], 1)
        self.assertEqual(attempt2_fixes[1]["rc"], 0)


if __name__ == "__main__":
    unittest.main()
