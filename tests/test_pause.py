import datetime
import glob
import os
import re
import tempfile
import unittest

from tests.test_core import FAKE_CLI
from yoke import adapters, calls, config, pause, pipeline, state

os.environ.setdefault("YOKE_OLLAMA_URL", "http://127.0.0.1:9")
PKG = os.path.dirname(os.path.abspath(pause.__file__))


class TestPause(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = os.path.realpath(self._td.name)
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(repo)
        self.run_ = state.Run.create(repo, config.load(home=self.tmp), home=self.tmp)
        self.addCleanup(pause.clear, self.run_)

    def test_single_writer_of_pause_state(self):
        writes = re.compile(r"\b(waiting_limit|limit_waits)=|\.(active_waiters|resume_at|stop_requested)\s*[-+]?=[^=]"
                            r"|\.(unpaused|stop_event)\.(set|clear)\(")
        for path in glob.glob(os.path.join(PKG, "*.py")):
            if os.path.basename(path) == "pause.py":
                continue
            with open(path) as f:
                self.assertIsNone(writes.search(f.read()), path)

    def test_recover_pauses_pool_until_resume_at(self):
        soon = (datetime.datetime.now().astimezone() + datetime.timedelta(seconds=60)).isoformat(timespec="seconds")
        self.run_.update(waiting_limit={"resume_at": soon, "ticket": "T-001", "stage": "code"})
        pause.recover(self.run_)
        p = pause.get(self.run_)
        self.assertFalse(p.unpaused.is_set())
        self.assertEqual((p.active_waiters, p.resume_at), (1, soon))

    def test_forget_ticket_drops_only_its_wait_counters(self):
        self.run_.update(limit_waits={"T-001:code": 2, "T-001:qa": 1, "T-002:code": 1})
        pause.forget_ticket(self.run_, "T-001")
        self.assertEqual(self.run_.read_json("run.json")["limit_waits"], {"T-002:code": 1})

    def test_run_call_budget_check_raises_before_any_audit(self):
        self.assertIs(pipeline.BudgetExhausted, calls.BudgetExhausted)
        self.run_.update(agent_calls=self.run_.cfg["max_agent_calls_total"])
        spec = adapters.CallSpec(prompt="p", schema_path="unused", cwd=self.tmp)
        with self.assertRaises(calls.BudgetExhausted) as cm:
            calls.run_call(self.run_, adapters.FakeAdapter(FAKE_CLI), spec, stage="code",
                           ticket="T-001", attempt=1, budget=True)
        self.assertEqual(str(cm.exception), "budget exhausted: max_agent_calls_total reached")
        self.assertFalse(os.path.exists(self.run_.path("tickets/T-001")))


if __name__ == "__main__":
    unittest.main()
