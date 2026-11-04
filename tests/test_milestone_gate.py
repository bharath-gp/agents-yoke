import json
import os
import unittest
from unittest import mock

from tests.test_planreview import Base
from yoke import milestones, router, scheduler, ticketstate, tracker

MS = [{"id": "MS-01", "title": "M1", "exit_check": "test -f ok.txt"}, {"id": "MS-02", "title": "M2"}]
OLLAMA_ESC = {"source": "ollama", "result": "escalate"}
FALLBACK = {"source": "default", "result": "fix_ticket"}


class TestGateRule(unittest.TestCase):
    def test_open_gates_before_and_current_gate(self):
        t2 = {"id": "T-002", "milestone": "MS-02"}
        self.assertEqual(milestones.open_gates_before(t2, MS, {}), ["MS-01"])
        self.assertEqual(milestones.open_gates_before(t2, MS, {"MS-01": {"status": "waived"}}), [])
        self.assertEqual(milestones.open_gates_before({"id": "T-001", "milestone": "MS-01"}, MS, {}), [])
        self.assertEqual(milestones.current_gate(MS, {"MS-01": {"status": "passed"}})["id"], "MS-02")
        self.assertIsNone(milestones.current_gate(MS, {"MS-01": {"status": "passed"}, "MS-02": {"status": "waived"}}))


class TestEvaluateGate(unittest.TestCase):
    def ev(self, rc, route=None, fix_count=0, exit_check="make it"):
        m = {"id": "MS-01", "exit_check": exit_check}
        return milestones.evaluate_gate(m, {"fix_count": fix_count}, rc, "boom", route, max_fixes=2)

    def test_verdicts(self):
        self.assertEqual(self.ev(None, exit_check=None), ("passed", None))
        self.assertEqual(self.ev(0), ("passed", None))
        self.assertEqual(self.ev(1, FALLBACK), ("fix", None))
        self.assertEqual(self.ev(1, OLLAMA_ESC),
                         ("escalate", "milestone MS-01 gate failed; decision model escalated: boom"))
        self.assertEqual(self.ev(1, FALLBACK, fix_count=2),
                         ("escalate", "milestone MS-01 gate failed after 2 fix tickets: boom"))


class TestGateStep(Base):
    def setUp(self):
        super().setUp()
        os.makedirs(self.run_.path("worktrees/_integration"))
        self.closed, self.fix_issues = [], []
        for name, sink in (("close_milestone", self.closed), ("create_fix_issue", self.fix_issues)):
            p = mock.patch.object(tracker, name, side_effect=lambda run, x, sink=sink: sink.append(x))
            p.start()
            self.addCleanup(p.stop)

    def kinds(self):
        with open(self.run_.path("events.jsonl")) as f:
            return [json.loads(line)["kind"] for line in f if line.strip()]

    def test_passing_exit_check(self):
        open(self.run_.path("worktrees/_integration/ok.txt"), "w").close()
        self.assertIsNone(scheduler._gate_step(self.run_, MS[0]))
        self.assertEqual(milestones.load_states(self.run_)["MS-01"]["status"], "passed")
        self.assertIn("milestone_passed", self.kinds())
        self.assertEqual(self.closed, ["MS-01"])

    def test_no_exit_check_passes_without_event(self):
        self.assertIsNone(scheduler._gate_step(self.run_, MS[1]))
        self.assertEqual(milestones.load_states(self.run_)["MS-02"]["status"], "passed")
        self.assertNotIn("milestone_passed", self.kinds())
        self.assertEqual(self.closed, ["MS-02"])

    def test_failing_exit_check_creates_fix_ticket_with_note(self):
        milestones.set_milestone_state(self.run_, "MS-01", note="try harder")
        with mock.patch.object(router, "gate_failure_route", return_value=FALLBACK):
            self.assertIsNone(scheduler._gate_step(self.run_, MS[0]))
        st = milestones.load_states(self.run_)["MS-01"]
        self.assertEqual((st["status"], st["fix_count"], "note" in st), ("open", 1, False))
        fixes = self.run_.read_json("fixes.json")
        self.assertEqual([f["id"] for f in fixes], [self.fix_issues[0]["id"]])
        self.assertEqual(fixes[0]["description"], "try harder")  # empty exit-check output
        self.assertEqual(self.kinds()[-1], "fix_ticket_created")

    def test_decision_model_escalates(self):
        with mock.patch.object(router, "gate_failure_route", return_value=OLLAMA_ESC):
            self.assertEqual(scheduler._gate_step(self.run_, MS[0]), "needs_user")
        self.assertEqual(milestones.load_states(self.run_)["MS-01"]["status"], "failed")
        self.assertEqual([e["ticket"] for e in ticketstate.escalations(self.run_)], ["MS-01"])
        self.assertFalse(os.path.exists(self.run_.path("fixes.json")))


if __name__ == "__main__":
    unittest.main()
