import json
import os
import unittest

from tests.test_planreview import Base
from yoke import ticketstate


class TestTicketState(Base):
    def events(self, *kinds):
        with open(self.run_.path("events.jsonl")) as f:
            evs = [json.loads(line) for line in f if line.strip()]
        return [e for e in evs if e["kind"] in kinds]

    def test_load_defaults_and_path(self):
        self.assertEqual(ticketstate.rel("T-001"), "tickets/T-001/state.json")
        self.assertEqual(ticketstate.load(self.run_, "T-001"), {"status": "pending", "attempt": 1, "sessions": {}})
        self.assertEqual(ticketstate.status(self.run_, "T-001"), "pending")
        self.assertEqual(ticketstate.ids(self.run_), [])

    def test_transitions_write_state_and_events(self):
        st = ticketstate.load(self.run_, "T-001")
        ticketstate.start(self.run_, "T-001", st)
        self.assertEqual(ticketstate.status(self.run_, "T-001"), "in_progress")
        ticketstate.await_gate(self.run_, "T-001", st, "MS-01")
        self.assertEqual(ticketstate.status(self.run_, "T-001"), "awaiting_gate")
        ticketstate.close(self.run_, "T-001", st)
        self.assertEqual(self.run_.read_json("tickets/T-001/state.json")["status"], "closed")
        self.assertEqual(ticketstate.ids(self.run_), ["T-001"])
        self.assertEqual([(e["kind"], e.get("status")) for e in self.events("ticket_state", "ticket_awaiting_gate")],
                         [("ticket_state", "in_progress"), ("ticket_state", "awaiting_gate"),
                          ("ticket_awaiting_gate", None), ("ticket_state", "closed")])

    def test_block_writes_status_escalation_and_events(self):
        st = ticketstate.load(self.run_, "T-002")
        ticketstate.block(self.run_, "T-002", st, "stuck")
        self.assertEqual(ticketstate.status(self.run_, "T-002"), "blocked")
        esc = ticketstate.escalations(self.run_)
        self.assertEqual([(e["id"], e["ticket"], e["reason"]) for e in esc], [(1, "T-002", "stuck")])
        self.assertEqual([e["kind"] for e in self.events("ticket_state", "escalation")], ["ticket_state", "escalation"])

    def test_unblock_and_skip_clear_only_own_escalations(self):
        for tid in ("T-001", "T-002"):
            ticketstate.block(self.run_, tid, ticketstate.load(self.run_, tid), "x")
        ticketstate.escalate(self.run_, None, "budget")
        st = ticketstate.load(self.run_, "T-001")
        st["attempt"] = 3
        ticketstate.save(self.run_, "T-001", st)

        ticketstate.unblock(self.run_, "T-001")
        st = ticketstate.load(self.run_, "T-001")
        self.assertEqual((st["status"], st["attempt"]), ("pending", 3))
        ticketstate.unblock(self.run_, "T-002", skip=True, reset_attempts=True)
        st = ticketstate.load(self.run_, "T-002")
        self.assertEqual((st["status"], st["attempt"]), ("skipped", 1))
        self.assertEqual([e["ticket"] for e in ticketstate.escalations(self.run_)], [None])

    def test_clear_escalations_by_milestone_key(self):
        ticketstate.escalate(self.run_, "MS-01", "gate failed")
        ticketstate.escalate(self.run_, "T-001", "x")
        ticketstate.clear_escalations(self.run_, "MS-01")
        self.assertEqual([e["ticket"] for e in ticketstate.escalations(self.run_)], ["T-001"])
        self.assertTrue(os.path.isfile(self.run_.path("escalations.json")))


if __name__ == "__main__":
    unittest.main()
