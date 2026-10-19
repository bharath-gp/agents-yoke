import contextlib
import io
import json
import os
import subprocess
import unittest
from unittest.mock import patch

from tests.test_core import FAKE_CLI, Tmp
from yoke import adapters, chart, cli, milestones, planreview, schema, state

VALID_CHART = {
    "destination": "complete project build",
    "out_of_scope": ["unrelated features"],
    "milestones": [
        {"id": "MS-01", "title": "m1", "exit_check": "echo ok 1"},
        {"id": "MS-02", "title": "m2", "exit_check": "echo ok 2"},
    ],
    "tickets": [
        {
            "id": "T-001",
            "title": "ticket 1",
            "type": "feature",
            "description": "desc 1",
            "exit_criteria": ["check 1"],
            "depends_on": [],
            "files_hint": ["a.py"],
            "plan_excerpt": "step 1",
            "milestone": "MS-01",
        },
        {
            "id": "T-002",
            "title": "ticket 2",
            "type": "feature",
            "description": "desc 2",
            "exit_criteria": ["check 2"],
            "depends_on": ["T-001"],
            "files_hint": ["b.py"],
            "plan_excerpt": "step 2",
            "milestone": "MS-02",
        },
    ],
}


class BaseChartTest(Tmp):
    def setUp(self):
        super().setUp()
        old_home = os.environ.get("YOKE_HOME")
        os.environ["YOKE_HOME"] = self.tmp
        if old_home is None:
            self.addCleanup(os.environ.pop, "YOKE_HOME", None)
        else:
            self.addCleanup(os.environ.__setitem__, "YOKE_HOME", old_home)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        subprocess.run(["git", "-C", self.repo, "init", "-q"], check=True)
        req = os.path.join(self.tmp, "req.md")
        with open(req, "w") as f:
            f.write("build something great")
        self.run_ = planreview.init_run(
            self.repo,
            req,
            {"max_chart_rounds": 2, "max_tickets": 10},
            home=self.tmp,
        )
        self.script = os.path.join(self.tmp, "script.json")
        old_script = os.environ.get("FAKE_SCRIPT")
        os.environ["FAKE_SCRIPT"] = self.script
        if old_script is None:
            self.addCleanup(os.environ.pop, "FAKE_SCRIPT", None)
        else:
            self.addCleanup(os.environ.__setitem__, "FAKE_SCRIPT", old_script)

    def plan(self, text="the detailed plan"):
        with open(self.run_.path("plan.md"), "w") as f:
            f.write(text)

    def chart(self, *responses, feedback=None):
        with open(self.script, "w") as f:
            json.dump({"wayfinder": [{"structured_output": r, "session_id": "s"} for r in responses]}, f)
        return chart.chart_round(
            self.run_,
            adapters.FakeAdapter(FAKE_CLI),
            feedback=feedback,
            env={"FAKE_SCRIPT": self.script},
        )


class TestChart(BaseChartTest):
    def test_fake_adapter_chart_result_imports_into_tickets_json_with_milestones_and_audit_dir(self):
        self.plan()
        planreview.approve(self.run_, "plan")
        n, res = self.chart(VALID_CHART)
        self.assertEqual(n, 1)
        self.assertTrue(res.ok and res.schema_valid)

        # Audit dir is chart/round-1
        expected_dir = self.run_.path("chart/round-1")
        self.assertEqual(res.audit_dir, expected_dir)
        self.assertTrue(os.path.exists(os.path.join(expected_dir, "prompt.md")))
        self.assertTrue(os.path.exists(os.path.join(expected_dir, "result.json")))
        self.assertTrue(os.path.exists(os.path.join(expected_dir, "meta.json")))

        # Chart intermediate tickets file
        self.assertTrue(os.path.exists(self.run_.path("chart/tickets.json")))

        # Imported into tickets.json with milestones
        self.assertTrue(os.path.exists(self.run_.path("tickets.json")))
        doc = self.run_.read_json("tickets.json")
        self.assertEqual(len(doc["tickets"]), 2)
        self.assertEqual(len(doc["milestones"]), 2)
        ms = milestones.milestones_of(self.run_)
        self.assertEqual([m["id"] for m in ms], ["MS-01", "MS-02"])

    def test_chart_assigns_ids_if_absent_and_router_classifies_type(self):
        self.plan()
        planreview.approve(self.run_, "plan")
        unassigned_chart = {
            "destination": "dest",
            "milestones": [
                {"id": "MS-01", "title": "m1", "exit_check": "check"}
            ],
            "tickets": [
                {
                    "title": "ticket without id",
                    "description": "desc",
                    "exit_criteria": ["ok"],
                    "depends_on": [],
                    "files_hint": [],
                    "plan_excerpt": "step",
                    "milestone": "MS-01",
                }
            ],
        }
        with open(self.script, "w") as f:
            json.dump({
                "wayfinder": [{"structured_output": unassigned_chart, "session_id": "s"}],
                "router": [{"structured_output": {"type": "feature", "confidence": 0.9, "reason": "r"}}],
            }, f)
        n, res = chart.chart_round(
            self.run_,
            adapters.FakeAdapter(FAKE_CLI),
            env={"FAKE_SCRIPT": self.script},
        )
        self.assertEqual(n, 1)
        self.assertTrue(res.ok and res.schema_valid)
        doc = self.run_.read_json("tickets.json")
        self.assertEqual(doc["tickets"][0]["id"], "T-001")
        self.assertEqual(doc["tickets"][0]["type"], "feature")

    def test_schema_invalid_result_triggers_single_reask(self):
        self.plan()
        planreview.approve(self.run_, "plan")
        invalid_chart = {"invalid": "payload without tickets"}
        n, res = self.chart(invalid_chart, VALID_CHART)
        self.assertEqual(n, 1)
        self.assertTrue(res.ok and res.schema_valid)

        # Single re-ask executed in chart/round-1/reask
        reask_dir = self.run_.path("chart/round-1/reask")
        self.assertTrue(os.path.isdir(reask_dir))
        self.assertTrue(os.path.exists(os.path.join(reask_dir, "result.json")))

        # Final result imported into tickets.json
        self.assertTrue(os.path.exists(self.run_.path("tickets.json")))

    def test_round_after_max_chart_rounds_is_refused(self):
        self.plan()
        planreview.approve(self.run_, "plan")
        self.chart(VALID_CHART)
        self.chart(VALID_CHART)
        with self.assertRaisesRegex(RuntimeError, "max_chart_rounds"):
            self.chart(VALID_CHART)
        self.assertEqual(chart.next_round(self.run_), 3)

    def test_chart_refused_when_plan_is_not_approved(self):
        self.plan()
        with self.assertRaises(planreview.ApprovalError):
            self.chart(VALID_CHART)

        # Approved but then changed
        planreview.approve(self.run_, "plan")
        self.plan("altered plan")
        with self.assertRaises(planreview.ApprovalError):
            self.chart(VALID_CHART)

    def test_cli_tickets_chart_and_feedback(self):
        self.plan()
        planreview.approve(self.run_, "plan")
        with open(self.script, "w") as f:
            json.dump({"wayfinder": [{"structured_output": VALID_CHART, "session_id": "s"}]}, f)

        buf = io.StringIO()
        adapter = adapters.FakeAdapter(FAKE_CLI)
        with patch.dict(os.environ, {"FAKE_SCRIPT": self.script, "YOKE_HOME": self.tmp}), \
             contextlib.redirect_stdout(buf):
            ret = cli.tickets_cmd(
                cli.argparse.Namespace(
                    action="chart",
                    feedback="keep milestone exit checks short",
                    run=self.run_.run_id,
                ),
                adapter=adapter,
            )
        self.assertEqual(ret, 0)
        out = buf.getvalue()
        self.assertIn("MS-01: m1", out)
        self.assertIn("T-001 [feature] ticket 1", out)
        self.assertIn("MS-02: m2", out)
        self.assertIn("T-002 [feature] ticket 2", out)

        # Check prompt has feedback
        prompt_path = self.run_.path("chart/round-1/prompt.md")
        with open(prompt_path) as f:
            prompt_content = f.read()
        self.assertIn("keep milestone exit checks short", prompt_content)

    def test_cli_tickets_chart_import_error_prints_error_and_exits_1(self):
        self.plan()
        planreview.approve(self.run_, "plan")
        bad_deps_chart = {
            "destination": "dest",
            "milestones": [
                {"id": "MS-01", "title": "m1", "exit_check": "check"}
            ],
            "tickets": [
                {
                    "id": "T-001",
                    "title": "t1",
                    "type": "feature",
                    "description": "d",
                    "exit_criteria": ["ok"],
                    "depends_on": ["T-999"],
                    "files_hint": [],
                    "plan_excerpt": "p",
                    "milestone": "MS-01",
                }
            ],
        }
        with open(self.script, "w") as f:
            json.dump({"wayfinder": [{"structured_output": bad_deps_chart, "session_id": "s"}]}, f)

        buf_err = io.StringIO()
        adapter = adapters.FakeAdapter(FAKE_CLI)
        with patch.dict(os.environ, {"FAKE_SCRIPT": self.script, "YOKE_HOME": self.tmp}), \
             contextlib.redirect_stderr(buf_err):
            ret = cli.tickets_cmd(
                cli.argparse.Namespace(
                    action="chart",
                    feedback=None,
                    run=self.run_.run_id,
                ),
                adapter=adapter,
            )
        self.assertEqual(ret, 1)
        err = buf_err.getvalue()
        self.assertIn("unknown T-999", err)

    def test_cli_main_tickets_chart(self):
        self.plan()
        planreview.approve(self.run_, "plan")
        with open(self.script, "w") as f:
            json.dump({"wayfinder": [{"structured_output": VALID_CHART, "session_id": "s"}]}, f)

        buf = io.StringIO()
        fake_adapter = adapters.FakeAdapter(FAKE_CLI)
        with patch.dict(os.environ, {"FAKE_SCRIPT": self.script, "YOKE_HOME": self.tmp}), \
             patch("yoke.adapters.get", return_value=fake_adapter), \
             contextlib.redirect_stdout(buf):
            ret = cli.main(["tickets", "chart", "--run", self.run_.run_id])
        self.assertEqual(ret, 0)
        out = buf.getvalue()
        self.assertIn("MS-01: m1", out)
        self.assertIn("T-001 [feature] ticket 1", out)


if __name__ == "__main__":
    unittest.main()
