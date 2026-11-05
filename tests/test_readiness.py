import contextlib
import io
import json
import os
import unittest
from unittest import mock

from tests.test_core import FAKE_CLI, slurp
from tests.test_planreview import Base, GOOD_PLAN
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


class TestApproveGate(Base):
    def setUp(self):
        super().setUp()
        self.run_.update(config=dict(self.run_.cfg, ticket_review=True))
        self.plan(GOOD_PLAN)
        planreview.approve(self.run_, "plan")
        self.tickets([
            {
                "id": "T-001",
                "title": "first ticket",
                "plan_sections": ["t1"],
                "files": [{"action": "create", "path": "file1.py"}],
            },
            {
                "id": "T-002",
                "title": "second ticket",
                "plan_sections": ["t1"],
                "files": [{"action": "create", "path": "file2.py"}],
            },
        ])

    def test_refuses_without_review_at_hash(self):
        with self.assertRaises(planreview.ApprovalError) as ctx:
            planreview.approve(self.run_, "tickets")
        self.assertIn("review-tickets", str(ctx.exception))

    def test_refuses_on_not_ready(self):
        h = planreview.current_hash(self.run_, "tickets")
        self.run_.write_json("ticket-review/round-1/summary.json", {
            "round": 1,
            "tickets_sha256": h,
            "ready": ["T-001"],
            "not_ready": ["T-002"],
            "skipped": [],
        })
        with self.assertRaises(planreview.ApprovalError) as ctx:
            planreview.approve(self.run_, "tickets")
        self.assertIn("T-002", str(ctx.exception))

    def test_accepts_when_all_ready(self):
        h = planreview.current_hash(self.run_, "tickets")
        self.run_.write_json("ticket-review/round-1/summary.json", {
            "round": 1,
            "tickets_sha256": h,
            "ready": ["T-001", "T-002"],
            "not_ready": [],
            "skipped": [],
        })
        entry = planreview.approve(self.run_, "tickets")
        self.assertTrue(entry and "sha256" in entry)
        self.assertFalse(os.path.exists(self.run_.path("plan-review/arbitration.md")))

    def test_edit_after_review_refuses(self):
        h = planreview.current_hash(self.run_, "tickets")
        self.run_.write_json("ticket-review/round-1/summary.json", {
            "round": 1,
            "tickets_sha256": h,
            "ready": ["T-001", "T-002"],
            "not_ready": [],
            "skipped": [],
        })
        self.tickets([
            {
                "id": "T-001",
                "title": "first ticket edited",
                "plan_sections": ["t1"],
                "files": [{"action": "create", "path": "file1.py"}],
            },
            {
                "id": "T-002",
                "title": "second ticket",
                "plan_sections": ["t1"],
                "files": [{"action": "create", "path": "file2.py"}],
            },
        ])
        with self.assertRaises(planreview.ApprovalError) as ctx:
            planreview.approve(self.run_, "tickets")
        self.assertIn("no readiness review at the current tickets hash", str(ctx.exception))

    def test_accept_unready_records_reason(self):
        h = planreview.current_hash(self.run_, "tickets")
        self.run_.write_json("ticket-review/round-1/summary.json", {
            "round": 1,
            "tickets_sha256": h,
            "ready": ["T-001"],
            "not_ready": ["T-002"],
            "skipped": [],
        })
        entry = planreview.approve(self.run_, "tickets", accept_unready="coder can infer")
        self.assertTrue(entry and "sha256" in entry)
        arb_path = self.run_.path("plan-review/arbitration.md")
        self.assertTrue(os.path.exists(arb_path))
        with open(arb_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("T-002 not_ready", content)
        self.assertIn("coder can infer", content)
        events = [json.loads(l) for l in slurp(self.run_.path("events.jsonl")).splitlines()]
        accepted_events = [e for e in events if e.get("kind") == "ticket_review_accepted"]
        self.assertEqual(len(accepted_events), 1)
        self.assertEqual(accepted_events[0].get("reason"), "coder can infer")

    def test_cli_accept_unready_flag(self):
        h = planreview.current_hash(self.run_, "tickets")
        self.run_.write_json("ticket-review/round-1/summary.json", {
            "round": 1,
            "tickets_sha256": h,
            "ready": ["T-001"],
            "not_ready": ["T-002"],
            "skipped": [],
        })
        buf_out = io.StringIO()
        buf_err = io.StringIO()
        with mock.patch.dict(os.environ, {"YOKE_HOME": self.tmp}), \
             mock.patch("yoke.cli.tracker.detect", return_value=None), \
             contextlib.redirect_stdout(buf_out), \
             contextlib.redirect_stderr(buf_err):
            ret = cli.main(["approve", "tickets", "--run", self.run_.run_id, "--accept-unready", "why"])
        self.assertEqual(ret, 0)
        self.assertIn("approved tickets", buf_out.getvalue())
        arb_path = self.run_.path("plan-review/arbitration.md")
        self.assertTrue(os.path.exists(arb_path))
        with open(arb_path, encoding="utf-8") as f:
            self.assertIn("why", f.read())

        buf_err2 = io.StringIO()
        with mock.patch.dict(os.environ, {"YOKE_HOME": self.tmp}), \
             mock.patch("yoke.cli.tracker.detect", return_value=None), \
             contextlib.redirect_stderr(buf_err2):
            ret2 = cli.main(["approve", "tickets", "--run", self.run_.run_id, "--accept-unready", " "])
        self.assertEqual(ret2, 1)
        self.assertIn("yoke: --accept-unready needs a reason", buf_err2.getvalue())

    def test_approve_blank_reason_raises(self):
        h = planreview.current_hash(self.run_, "tickets")
        self.run_.write_json("ticket-review/round-1/summary.json", {
            "round": 1,
            "tickets_sha256": h,
            "ready": ["T-001"],
            "not_ready": ["T-002"],
        })
        for blank in ("", "   "):
            with self.assertRaises(planreview.ApprovalError):
                planreview.approve(self.run_, "tickets", accept_unready=blank)
            self.assertFalse(os.path.exists(self.run_.path("plan-review/arbitration.md")))
            self.assertNotIn("tickets", self.run_.read_json("approvals.json", {}))

    def test_plan_changed_refuses_before_recording_accept(self):
        h = planreview.current_hash(self.run_, "tickets")
        self.run_.write_json("ticket-review/round-1/summary.json", {
            "round": 1,
            "tickets_sha256": h,
            "ready": ["T-001"],
            "not_ready": ["T-002"],
        })
        self.plan("changed plan")
        with self.assertRaises(planreview.ApprovalError):
            planreview.approve(self.run_, "tickets", accept_unready="coder can infer")
        self.assertFalse(os.path.exists(self.run_.path("plan-review/arbitration.md")))

    def test_gate_problems_edge_cases(self):
        # Missing ticket-review directory returns problem
        clean_run = planreview.init_run(self.repo, os.path.join(self.tmp, "req.md"), home=self.tmp)
        clean_run.write_json("tickets.json", {"tickets": []})
        self.assertEqual(readiness.gate_problems(clean_run), ["no readiness review at the current tickets hash"])

        # round-1 matches, round-2 stale hash, round-invalid directory name, round-3 corrupt json
        h = planreview.current_hash(self.run_, "tickets")
        self.run_.write_json("ticket-review/round-1/summary.json", {
            "round": 1,
            "tickets_sha256": h,
            "not_ready": ["T-001"],
        })
        self.run_.write_json("ticket-review/round-2/summary.json", {
            "round": 2,
            "tickets_sha256": "stalehash",
            "not_ready": ["T-002"],
        })
        os.makedirs(self.run_.path("ticket-review/round-invalid"), exist_ok=True)
        os.makedirs(self.run_.path("ticket-review/round-3"), exist_ok=True)
        with open(self.run_.path("ticket-review/round-3/summary.json"), "w") as f:
            f.write("corrupted json")

        # Latest matching is still round-1
        self.assertEqual(readiness.gate_problems(self.run_), ["T-001 not_ready"])

    def test_gate_off_by_default(self):
        self.run_.update(config=dict(self.run_.cfg, ticket_review=False))
        entry = planreview.approve(self.run_, "tickets")
        self.assertTrue(entry and "sha256" in entry)
        self.assertFalse(os.path.exists(self.run_.path("plan-review/arbitration.md")))

    def test_round_ordering_picks_latest_numeric_round(self):
        h = planreview.current_hash(self.run_, "tickets")
        self.run_.write_json("ticket-review/round-2/summary.json", {
            "round": 2,
            "tickets_sha256": h,
            "not_ready": ["T-002"],
        })
        self.run_.write_json("ticket-review/round-10/summary.json", {
            "round": 10,
            "tickets_sha256": h,
            "not_ready": ["T-010"],
        })
        problems = readiness.gate_problems(self.run_)
        self.assertEqual(problems, ["T-010 not_ready"])


if __name__ == "__main__":
    unittest.main()
