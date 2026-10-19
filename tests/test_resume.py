import os
import signal
import subprocess
import sys
import time

from tests.test_core import FAKE_CLI
from tests.test_pipeline import CODE, PipeBase, REV, resp, ticket, val
from yoke import runcmds, scheduler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = ("import sys; from yoke import adapters, scheduler, state; from tests.test_core import FAKE_CLI; "
          "scheduler.run(state.Run.load(sys.argv[1]), adapter_for=lambda r: adapters.FakeAdapter(FAKE_CLI))")


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class TestResume(PipeBase):
    def test_kill_runner_mid_stage_then_resume(self):
        pidfile = os.path.join(self.tmp, "child.pid")
        self.setup_tickets([ticket(1)], {
            "code": [resp(CODE, "s", sleep=999, child_pidfile=pidfile), resp(CODE, "s", write={"f.txt": "x"})],
            "review": [resp(REV)],
            "validate": [resp(val())], "router": [{"exit": 1}]})
        env = dict(os.environ, **self.env, PYTHONPATH=ROOT)
        runner = subprocess.Popen([sys.executable, "-c", RUNNER, self.run_.run_id], cwd=ROOT, env=env)
        self.addCleanup(runner.kill)
        for _ in range(100):
            if os.path.exists(pidfile) and os.path.getsize(pidfile):
                break
            time.sleep(0.1)
        with open(pidfile) as f:
            child = int(f.read())
        runner.kill()  # crash: the worker group survives
        runner.wait()
        self.assertTrue(alive(child))
        self.assertEqual(self.go(), "done")
        for _ in range(30):
            if not alive(child):
                break
            time.sleep(0.1)
        self.assertFalse(alive(child))
        attempt = self.run_.path("tickets/T-001/attempt-1")
        self.assertTrue(os.path.isdir(attempt + "/code.interrupted"))
        self.assertTrue(os.path.exists(attempt + "/code/result.json"))
        self.assertEqual(self.tstate(1)["attempt"], 1)
        self.assertEqual(self.tstate(1)["status"], "closed")

    def test_recover_checking_treated_as_open(self):
        self.run_.write_json("milestones.json", {"MS-01": {"id": "MS-01", "status": "checking", "fix_count": 0}})
        scheduler.recover(self.run_)
        st = self.run_.read_json("milestones.json")
        self.assertEqual(st["MS-01"]["status"], "open")


class TestWait(PipeBase):
    def w(self, timeout=0.3):
        return runcmds.wait_for(self.run_, timeout, poll=0.05)

    def test_returns_on_escalation(self):
        self.run_.update(state="running", runner_pid=os.getpid())
        with open(self.run_.path("runner.lock"), "w") as f:
            f.write(str(os.getpid()))
        self.run_.write_json("escalations.json", [{"id": 1, "ticket": "T-001", "reason": "stuck", "at": "x"}])
        reason, msg = self.w()
        self.assertEqual(reason, "escalation")
        self.assertIn("stuck", msg)

    def test_returns_on_done(self):
        self.run_.update(state="done")
        self.assertEqual(self.w()[0], "done")

    def test_returns_when_runner_died(self):
        self.run_.update(state="running")
        with open(self.run_.path("runner.lock"), "w") as f:
            f.write("999999")
        self.assertEqual(self.w()[0], "died")

    def test_timeout_reports_progress(self):
        self.run_.write_json("tickets.json", {"tickets": [ticket(1), ticket(2)]})
        self.run_.update(state="running")
        with open(self.run_.path("runner.lock"), "w") as f:
            f.write(str(os.getpid()))
        reason, msg = self.w(0.2)
        self.assertEqual(reason, "timeout")
        self.assertIn("still running: 0/2 closed", msg)


class TestClean(PipeBase):
    def test_clean_removes_worktrees_keeps_branch(self):
        self.setup_tickets([ticket(1)], {"code": [resp(CODE, "s", write={"f": "1"})],
                                         "review": [resp(REV)],
                                         "validate": [resp(val())], "router": [{"exit": 1}]})
        self.assertEqual(self.go(), "done")
        self.assertTrue(os.path.isdir(self.run_.path("worktrees/_integration")))
        runcmds.clean(self.run_)
        self.assertFalse(os.path.exists(self.run_.path("worktrees")))
        branch = self.run_.read_json("run.json")["branch"]
        self.assertIn(branch, subprocess.run(["git", "-C", self.repo, "branch"], capture_output=True, text=True).stdout)

    def test_clean_refuses_live_runner(self):
        with open(self.run_.path("runner.lock"), "w") as f:
            f.write(str(os.getpid()))
        with self.assertRaises(scheduler.LockError):
            runcmds.clean(self.run_)
