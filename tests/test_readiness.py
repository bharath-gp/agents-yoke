import contextlib
import io
import json
import os
import unittest
from unittest import mock

from tests.test_core import FAKE_CLI, slurp
from tests.test_planreview import Base
from yoke import adapters, calls, cli, config, planreview, readiness, schema

READY_OUT = {
    "verdict": "ready",
    "paraphrase": "Ready ticket paraphrase.",
    "questions": [],
    "missing_refs": [],
}

NOT_READY_OUT = {
    "verdict": "not_ready",
    "paraphrase": "Not ready ticket paraphrase.",
    "questions": ["What is the answer?"],
    "missing_refs": [],
}


class TestReadiness(Base):
    def setUp(self):
        super().setUp()
        self.run_.update(config=dict(self.run_.cfg, ticket_review=True))
        self.plan_text = (
            '<a id="w1"></a>\n'
            '### W1: one\n'
            'w1 section text\n'
            '#### sub\n'
            'sub section text\n'
            '\n'
            '<a id="w2"></a>\n'
            '### W2: two\n'
            'w2 section text\n'
        )
        self.plan(self.plan_text)
        self.tickets([
            {
                "id": "T-001",
                "title": "first ticket",
                "plan_sections": ["w1"],
                "files": [{"action": "create", "path": "file1.py"}],
            },
            {
                "id": "T-002",
                "title": "second ticket",
                "plan_sections": ["#w2"],
                "files": [{"action": "create", "path": "file2.py"}],
            },
        ])

    def test_schema_file(self):
        s = schema.load_schema("readiness")
        self.assertEqual(schema.validate(READY_OUT, s), [])
        self.assertEqual(schema.validate(NOT_READY_OUT, s), [])
        self.assertTrue(schema.validate({"verdict": "maybe"}, s))

    def test_plan_section_by_anchor_and_slug(self):
        sec_w1 = readiness.plan_section(self.plan_text, "w1")
        self.assertIn("### W1: one", sec_w1)
        self.assertIn("w1 section text", sec_w1)
        self.assertIn("#### sub", sec_w1)
        self.assertIn("sub section text", sec_w1)
        self.assertNotIn("W2", sec_w1)
        self.assertNotIn("w2 section text", sec_w1)

        sec_hash_w1 = readiness.plan_section(self.plan_text, "#w1")
        self.assertEqual(sec_w1, sec_hash_w1)

        sec_slug = readiness.plan_section(self.plan_text, "w1-one")
        self.assertEqual(sec_w1, sec_slug)

        self.assertEqual(readiness.plan_section(self.plan_text, "unknown"), "")

    def test_one_call_per_ticket_and_records(self):
        calls_before = self.run_.read_json("run.json")["agent_calls"]
        with open(self.script, "w") as f:
            json.dump({
                "T-001": [{"structured_output": READY_OUT, "session_id": "s1"}],
                "T-002": [{"structured_output": NOT_READY_OUT, "session_id": "s2"}],
            }, f)
        n, summary = readiness.review_tickets(self.run_, adapters.FakeAdapter(FAKE_CLI),
                                              env={"FAKE_SCRIPT": self.script})
        self.assertEqual(n, 1)
        h = planreview.current_hash(self.run_, "tickets")
        r1 = self.run_.read_json("ticket-review/round-1/T-001.json")
        r2 = self.run_.read_json("ticket-review/round-1/T-002.json")
        self.assertEqual(r1["tickets_sha256"], h)
        self.assertEqual(r2["tickets_sha256"], h)
        self.assertEqual(r1["verdict"], "ready")
        self.assertEqual(r2["verdict"], "not_ready")
        self.assertEqual(summary["ready"], ["T-001"])
        self.assertEqual(summary["not_ready"], ["T-002"])
        calls_after = self.run_.read_json("run.json")["agent_calls"]
        self.assertEqual(calls_after - calls_before, 2)

    def test_prompt_has_only_own_ticket_and_sections(self):
        with open(self.script, "w") as f:
            json.dump({
                "T-001": [{"structured_output": READY_OUT, "session_id": "s1"}],
                "T-002": [{"structured_output": NOT_READY_OUT, "session_id": "s2"}],
            }, f)
        readiness.review_tickets(self.run_, adapters.FakeAdapter(FAKE_CLI),
                                 env={"FAKE_SCRIPT": self.script})
        prompt1 = slurp(self.run_.path("ticket-review/round-1/T-001/prompt.md"))
        self.assertIn("first ticket", prompt1)
        self.assertIn("w1 section text", prompt1)
        self.assertNotIn("second ticket", prompt1)
        self.assertNotIn("w2 section text", prompt1)
        self.assertNotIn("build a thing", prompt1)

    def test_cap_skips_extra_tickets(self):
        self.run_.update(config=dict(self.run_.cfg, ticket_review_max=1))
        calls_before = self.run_.read_json("run.json")["agent_calls"]
        with open(self.script, "w") as f:
            json.dump({
                "T-001": [{"structured_output": READY_OUT, "session_id": "s1"}],
            }, f)
        n, summary = readiness.review_tickets(self.run_, adapters.FakeAdapter(FAKE_CLI),
                                              env={"FAKE_SCRIPT": self.script})
        self.assertEqual(summary["skipped"], ["T-002"])
        calls_after = self.run_.read_json("run.json")["agent_calls"]
        self.assertEqual(calls_after - calls_before, 1)

        buf_out = io.StringIO()
        buf_err = io.StringIO()
        with mock.patch.dict(os.environ, {"YOKE_HOME": self.tmp, "FAKE_SCRIPT": self.script}), \
             mock.patch("yoke.readiness.adapters.get", return_value=adapters.FakeAdapter(FAKE_CLI)), \
             contextlib.redirect_stdout(buf_out), \
             contextlib.redirect_stderr(buf_err):
            cli.main(["review-tickets", "--run", self.run_.run_id])
        self.assertIn("warning: 1 tickets not reviewed (ticket_review_max=1)", buf_err.getvalue())

    def test_budget_respected(self):
        # 3 tickets: T-001 succeeds, T-002 hits BudgetExhausted, T-003 skips call via if budget_exhausted
        self.tickets([
            {
                "id": "T-001",
                "title": "first ticket",
                "plan_sections": ["w1"],
                "files": [{"action": "create", "path": "file1.py"}],
            },
            {
                "id": "T-002",
                "title": "second ticket",
                "plan_sections": ["#w2"],
                "files": [{"action": "create", "path": "file2.py"}],
            },
            {
                "id": "T-003",
                "title": "third ticket",
                "plan_sections": ["w1"],
                "files": [{"action": "create", "path": "file3.py"}],
            },
        ])
        cur_calls = self.run_.read_json("run.json")["agent_calls"]
        self.run_.update(config=dict(self.run_.cfg, max_agent_calls_total=cur_calls + 1))
        with open(self.script, "w") as f:
            json.dump({
                "T-001": [{"structured_output": READY_OUT, "session_id": "s1"}],
                "T-002": [{"structured_output": NOT_READY_OUT, "session_id": "s2"}],
                "T-003": [{"structured_output": READY_OUT, "session_id": "s3"}],
            }, f)
        n, summary = readiness.review_tickets(self.run_, adapters.FakeAdapter(FAKE_CLI),
                                              env={"FAKE_SCRIPT": self.script})
        self.assertEqual(summary["ready"], ["T-001"])
        self.assertEqual(summary["not_ready"], ["T-002", "T-003"])
        r2 = self.run_.read_json("ticket-review/round-1/T-002.json")
        self.assertEqual(r2["verdict"], "not_ready")
        self.assertEqual(r2["error"], "budget exhausted")
        r3 = self.run_.read_json("ticket-review/round-1/T-003.json")
        self.assertEqual(r3["verdict"], "not_ready")
        self.assertEqual(r3["error"], "budget exhausted")
        calls_after = self.run_.read_json("run.json")["agent_calls"]
        self.assertEqual(calls_after - cur_calls, 1)

    def test_failed_call_is_not_ready(self):
        bad_out = {"paraphrase": "no verdict"}
        with open(self.script, "w") as f:
            json.dump({
                "T-001": [
                    {"structured_output": bad_out, "session_id": "s1"},
                    {"structured_output": bad_out, "session_id": "s1"},
                ],
                "T-002": [{"structured_output": READY_OUT, "session_id": "s2"}],
            }, f)
        n, summary = readiness.review_tickets(self.run_, adapters.FakeAdapter(FAKE_CLI),
                                              env={"FAKE_SCRIPT": self.script})
        self.assertEqual(summary["ready"], ["T-002"])
        self.assertEqual(summary["not_ready"], ["T-001"])
        r1 = self.run_.read_json("ticket-review/round-1/T-001.json")
        self.assertEqual(r1["verdict"], "not_ready")
        self.assertTrue(r1["error"] and ("schema invalid" in r1["error"] or "required" in r1["error"]))
        r2 = self.run_.read_json("ticket-review/round-1/T-002.json")
        self.assertEqual(r2["verdict"], "ready")
        self.assertIsNone(r2["error"])

    def test_round_cap(self):
        self.run_.update(config=dict(self.run_.cfg, max_ticket_review_rounds=1))
        with open(self.script, "w") as f:
            json.dump({
                "T-001": [{"structured_output": READY_OUT, "session_id": "s1"}],
                "T-002": [{"structured_output": READY_OUT, "session_id": "s2"}],
            }, f)
        readiness.review_tickets(self.run_, adapters.FakeAdapter(FAKE_CLI),
                                 env={"FAKE_SCRIPT": self.script})
        with self.assertRaises(RuntimeError):
            readiness.review_tickets(self.run_, adapters.FakeAdapter(FAKE_CLI),
                                     env={"FAKE_SCRIPT": self.script})

    def test_cli_off_skips(self):
        self.run_.update(config=dict(self.run_.cfg, ticket_review=False))
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"YOKE_HOME": self.tmp}), \
             contextlib.redirect_stdout(buf):
            ret = cli.main(["review-tickets", "--run", self.run_.run_id])
        self.assertEqual(ret, 0)
        self.assertIn("ticket_review is off; skipped", buf.getvalue())
        self.assertFalse(os.path.exists(self.run_.path("ticket-review")))

    def test_cli_prints_counts(self):
        with open(self.script, "w") as f:
            json.dump({
                "T-001": [{"structured_output": READY_OUT, "session_id": "s1"}],
                "T-002": [{"structured_output": NOT_READY_OUT, "session_id": "s2"}],
            }, f)
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"YOKE_HOME": self.tmp, "FAKE_SCRIPT": self.script}), \
             mock.patch("yoke.readiness.adapters.get", return_value=adapters.FakeAdapter(FAKE_CLI)), \
             contextlib.redirect_stdout(buf):
            ret = cli.main(["review-tickets", "--run", self.run_.run_id])
        self.assertEqual(ret, 0)
        out = buf.getvalue()
        self.assertIn("round 1:", out)
        self.assertIn("ready 1, not_ready 1, skipped 0", out)
        self.assertIn("T-002:", out)
        self.assertIn("What is the answer?", out)

    def test_cli_error_contract(self):
        # 1. Missing tickets.json raises ApprovalError and CLI outputs 'yoke: ...' to stderr, exit 1
        os.remove(self.run_.path("tickets.json"))
        buf_err = io.StringIO()
        with mock.patch.dict(os.environ, {"YOKE_HOME": self.tmp}), \
             contextlib.redirect_stderr(buf_err):
            ret = cli.main(["review-tickets", "--run", self.run_.run_id])
        self.assertEqual(ret, 1)
        self.assertTrue(buf_err.getvalue().startswith("yoke: "))

        # 2. Exceeding max_ticket_review_rounds raises RuntimeError and CLI outputs 'yoke: ...' to stderr, exit 1
        self.tickets([{"id": "T-001", "plan_sections": []}])
        self.run_.update(config=dict(self.run_.cfg, max_ticket_review_rounds=1))
        with open(self.script, "w") as f:
            json.dump({"T-001": [{"structured_output": READY_OUT, "session_id": "s1"}]}, f)
        readiness.review_tickets(self.run_, adapters.FakeAdapter(FAKE_CLI), env={"FAKE_SCRIPT": self.script})

        buf_err2 = io.StringIO()
        with mock.patch.dict(os.environ, {"YOKE_HOME": self.tmp, "FAKE_SCRIPT": self.script}), \
             mock.patch("yoke.readiness.adapters.get", return_value=adapters.FakeAdapter(FAKE_CLI)), \
             contextlib.redirect_stderr(buf_err2):
            ret2 = cli.main(["review-tickets", "--run", self.run_.run_id])
        self.assertEqual(ret2, 1)
        self.assertTrue(buf_err2.getvalue().startswith("yoke: "))
        self.assertIn("max_ticket_review_rounds", buf_err2.getvalue())

    def test_audit_rel(self):
        self.assertEqual(
            calls.audit_rel("readiness", None, 1, "c0001", label="T-001"),
            "ticket-review/round-1/T-001"
        )
        self.assertEqual(
            calls.audit_rel("readiness", None, 2, "c0002", label=None),
            "ticket-review/round-2/ticket"
        )
        self.assertEqual(
            calls.audit_rel("readiness", "T-001", 1, "c0001", label="custom"),
            "tickets/T-001/attempt-1/custom"
        )

    def test_missing_plan_and_tickets(self):
        os.remove(self.run_.path("plan.md"))
        with self.assertRaises(planreview.ApprovalError):
            readiness.review_tickets(self.run_)
        self.plan("a plan")
        os.remove(self.run_.path("tickets.json"))
        with self.assertRaises(planreview.ApprovalError):
            readiness.review_tickets(self.run_)

    def test_malformed_tickets_raises_approval_error(self):
        with open(self.run_.path("tickets.json"), "w") as f:
            f.write("{not-valid-json}")
        with self.assertRaises(planreview.ApprovalError):
            readiness.review_tickets(self.run_)


if __name__ == "__main__":
    unittest.main()
