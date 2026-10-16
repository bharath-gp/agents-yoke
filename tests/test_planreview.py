import contextlib
import io
import json
import os
import subprocess
import unittest

from tests.test_core import FAKE_CLI, Tmp
from yoke import adapters, cli, planreview, schema, state

APPROVE = {"verdict": "approve", "findings": [], "summary": "ok"}
CHANGES = {"verdict": "changes_requested", "summary": "fix",
           "findings": [{"severity": "major", "section": "2", "issue": "gap", "suggestion": "fill"}]}


class Base(Tmp):
    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        subprocess.run(["git", "-C", self.repo, "init", "-q"], check=True)
        req = os.path.join(self.tmp, "req.md")
        with open(req, "w") as f:
            f.write("build a thing")
        self.run_ = planreview.init_run(self.repo, req, {"max_plan_review_rounds": 2}, home=self.tmp)
        self.script = os.path.join(self.tmp, "script.json")

    def plan(self, text="the plan"):
        with open(self.run_.path("plan.md"), "w") as f:
            f.write(text)

    def tickets(self, tickets):
        self.run_.write_json("tickets.json", {"tickets": tickets})

    def review(self, *responses):
        with open(self.script, "w") as f:
            json.dump({"plan_review": [{"structured_output": r, "session_id": "s"} for r in responses]}, f)
        return planreview.review_round(self.run_, adapters.FakeAdapter(FAKE_CLI),
                                       env={"FAKE_SCRIPT": self.script})


class TestInit(Base):
    def test_init_copies_requirements_and_snapshots_config(self):
        with open(self.run_.path("requirements.md")) as f:
            self.assertEqual(f.read(), "build a thing")
        self.assertEqual(self.run_.cfg["max_plan_review_rounds"], 2)

    def test_cli_init_prints_run_id_and_overrides_roles(self):
        os.environ["YOKE_HOME"] = self.tmp
        self.addCleanup(os.environ.pop, "YOKE_HOME")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(["init", "--repo", self.repo, "--secondary", "claude"]), 0)
        run = state.Run.load(buf.getvalue().strip(), home=self.tmp)
        self.assertEqual(run.cfg["roles"]["secondary"], "claude")


class TestReviewPlan(Base):
    def test_round_saved_and_schema_valid(self):
        self.plan()
        n, res = self.review(CHANGES)
        self.assertEqual(n, 1)
        self.assertTrue(res.ok and res.schema_valid)
        d = self.run_.path("plan-review/round-1")
        for f in ("prompt.md", "result.json", "meta.json"):
            self.assertTrue(os.path.exists(os.path.join(d, f)), f)
        with open(os.path.join(d, "prompt.md")) as f:
            prompt = f.read()
        self.assertIn("build a thing", prompt)
        self.assertIn("the plan", prompt)
        self.assertEqual(self.run_.read_json("plan-review/round-1/command.json")[0:1] and self.run_.read_json("plan-review/round-1/meta.json")["cwd"], self.repo)

    def test_rounds_bounded(self):
        self.plan()
        self.review(CHANGES)
        self.review(CHANGES)
        with self.assertRaisesRegex(RuntimeError, "arbitrate"):
            self.review(CHANGES)
        self.assertEqual(planreview.next_round(self.run_), 3)

    def test_missing_plan_refused(self):
        with self.assertRaises(planreview.ApprovalError):
            self.review(APPROVE)

    def test_invalid_review_fails_call(self):
        self.plan()
        _, res = self.review({"verdict": "maybe"}, {"verdict": "maybe"})  # re-ask also invalid
        self.assertFalse(res.schema_valid)

    def test_schema_file(self):
        self.assertEqual(schema.validate(CHANGES, schema.load_schema("plan-review")), [])
        self.assertTrue(schema.validate({"verdict": "maybe"}, schema.load_schema("plan-review")))


class TestApprovals(Base):
    def test_hash_binding(self):
        self.plan()
        planreview.approve(self.run_, "plan")
        planreview.check_approval(self.run_, "plan")
        self.plan("changed")
        with self.assertRaisesRegex(planreview.ApprovalError, "plan changed"):
            planreview.check_approval(self.run_, "plan")
        planreview.approve(self.run_, "plan")  # re-approve works
        planreview.check_approval(self.run_, "plan")

    def test_tickets_need_plan_approved_and_unchanged(self):
        self.plan()
        self.tickets([{"id": "T-001"}])
        with self.assertRaisesRegex(planreview.ApprovalError, "plan not approved"):
            planreview.approve(self.run_, "tickets")
        planreview.approve(self.run_, "plan")
        self.plan("changed")
        with self.assertRaisesRegex(planreview.ApprovalError, "plan changed"):
            planreview.approve(self.run_, "tickets")

    def test_ticket_change_invalidates_tickets_and_final(self):
        self.plan()
        self.tickets([{"id": "T-001", "title": "a"}])
        planreview.approve(self.run_, "plan")
        planreview.approve(self.run_, "tickets")
        planreview.approve(self.run_, "final")
        planreview.check_approval(self.run_, "final")
        self.tickets([{"id": "T-001", "title": "b"}])
        for kind in ("tickets", "final"):
            with self.assertRaisesRegex(planreview.ApprovalError, "tickets changed"):
                planreview.check_approval(self.run_, kind)

    def test_ticket_hash_ignores_formatting(self):
        self.plan()
        self.tickets([{"id": "T-001", "title": "a"}])
        h = planreview.current_hash(self.run_, "tickets")
        with open(self.run_.path("tickets.json"), "w") as f:
            f.write('{"tickets":[{"title":"a","id":"T-001"}]}')
        self.assertEqual(planreview.current_hash(self.run_, "tickets"), h)

    def test_final_needs_tickets(self):
        self.plan()
        planreview.approve(self.run_, "plan")
        self.tickets([])
        with self.assertRaisesRegex(planreview.ApprovalError, "tickets not approved"):
            planreview.approve(self.run_, "final")


if __name__ == "__main__":
    unittest.main()
