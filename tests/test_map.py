import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from yoke import cli, config, milestones, pipeline, runcmds, state, tickets

os.environ.setdefault("YOKE_OLLAMA_URL", "http://127.0.0.1:9")


class TestMap(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = os.path.realpath(self._td.name)
        os.environ["YOKE_HOME"] = self.tmp
        self.addCleanup(os.environ.pop, "YOKE_HOME", None)
        os.makedirs(os.path.join(self.tmp, "repo"))
        cfg = config.load(home=self.tmp, overrides={
            "max_tickets": 40,
            "kill_grace_s": 1,
            "router": {"ollama_url": "http://127.0.0.1:1"}
        })
        self.run_ = state.Run.create(os.path.join(self.tmp, "repo"), cfg, home=self.tmp)

    def test_render_map_milestones_frontier_and_blocked_by_gate(self):
        doc = {
            "destination": "build milestone map and status system",
            "out_of_scope": ["unrelated features", "cloud deployment"],
            "milestones": [
                {"id": "MS-01", "title": "Foundation", "exit_check": "python3 -m unittest tests.test_a"},
                {"id": "MS-02", "title": "Execution", "exit_check": "python3 -m unittest tests.test_b"},
            ],
            "tickets": [
                {
                    "id": "T-001",
                    "title": "Setup data model",
                    "type": "feature",
                    "description": "desc",
                    "exit_criteria": ["ok"],
                    "depends_on": [],
                    "files_hint": [],
                    "plan_excerpt": "excerpt",
                    "milestone": "MS-01",
                },
                {
                    "id": "T-002",
                    "title": "Add execution rules",
                    "type": "feature",
                    "description": "desc",
                    "exit_criteria": ["ok"],
                    "depends_on": ["T-001"],
                    "files_hint": [],
                    "plan_excerpt": "excerpt",
                    "milestone": "MS-02",
                },
                {
                    "id": "T-003",
                    "title": "Independent execution task",
                    "type": "feature",
                    "description": "desc",
                    "exit_criteria": ["ok"],
                    "depends_on": [],
                    "files_hint": [],
                    "plan_excerpt": "excerpt",
                    "milestone": "MS-02",
                },
            ],
        }
        self.run_.write_json("tickets.json", doc)
        milestones.save_states(self.run_, {
            "MS-01": {"id": "MS-01", "status": "open", "fix_count": 0},
            "MS-02": {"id": "MS-02", "status": "open", "fix_count": 0},
        })

        out = runcmds.render_map(self.run_)
        self.assertIn("Destination: build milestone map and status system", out)
        self.assertIn("Out of scope:", out)
        self.assertIn("- unrelated features", out)
        self.assertIn("- cloud deployment", out)

        self.assertIn("MS-01: Foundation", out)
        self.assertIn("gate: open", out)
        self.assertIn("exit_check: python3 -m unittest tests.test_a", out)
        self.assertIn("MS-02: Execution", out)
        self.assertIn("exit_check: python3 -m unittest tests.test_b", out)

        # Frontier lists T-001 which is ready right now
        self.assertIn("Frontier:", out)
        self.assertIn("T-001: Setup data model", out)

        # T-003 is in MS-02 and blocked by gate MS-01 even though it has no depends_on
        self.assertIn("blocked by gate MS-01", out)
        # T-002 has unclosed dependency T-001
        self.assertIn("waiting for T-001", out)

        # CLI command prints output
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["map", "--run", self.run_.run_id])
        self.assertEqual(rc, 0)
        cli_out = buf.getvalue()
        self.assertIn("Frontier:", cli_out)
        self.assertIn("blocked by gate MS-01", cli_out)

    def test_status_includes_fix_tickets_from_fixes_json(self):
        doc = {
            "milestones": [
                {"id": "MS-01", "title": "Foundation", "exit_check": "check1"},
            ],
            "tickets": [
                {
                    "id": "T-001",
                    "title": "Initial ticket",
                    "type": "feature",
                    "description": "d",
                    "exit_criteria": ["c"],
                    "depends_on": [],
                    "files_hint": [],
                    "plan_excerpt": "p",
                    "milestone": "MS-01",
                }
            ],
        }
        self.run_.write_json("tickets.json", doc)
        fix = milestones.make_fix_ticket(
            id="F-001",
            milestone="MS-01",
            failing_output="traceback",
            exit_check="check1",
            title="Fix MS-01 failure",
        )
        self.run_.write_json("fixes.json", [fix])

        st = self.run_.status()
        self.assertEqual(st["tickets_total"], 2)
        self.assertEqual(st["tickets"]["pending"], 2)

        rendered = self.run_.render_status()
        self.assertIn("tickets 2 (pending 2)", rendered)
        self.assertIn("milestones 1 (MS-01 open)", rendered)

        # CLI status test
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["status", "--run", self.run_.run_id])
        self.assertEqual(rc, 0)
        self.assertIn("tickets 2", buf.getvalue())

        # CLI route test for fix ticket from fixes.json
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["route", "F-001", "--run", self.run_.run_id])
        self.assertEqual(rc, 0)
        self.assertIn("stages", buf.getvalue())
        self.assertIn("bugfix", buf.getvalue())

    def test_map_md_append_on_close_and_no_validate_fallback(self):
        t1 = {
            "id": "T-001",
            "title": "Ticket with validate",
            "type": "feature",
        }
        results_with_val = {
            "validate": {"summary": "All checks passed successfully", "verdict": "approve"}
        }
        pipeline._append_decision(self.run_, t1, results_with_val)

        with open(self.run_.path("map.md")) as f:
            lines = f.read().splitlines()
        self.assertEqual(lines, ["- T-001 Ticket with validate: All checks passed successfully"])

        # Idempotent: appending same ticket again does not duplicate
        pipeline._append_decision(self.run_, t1, results_with_val)
        with open(self.run_.path("map.md")) as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 1)

        # Ticket without validate stage falls back to title
        t2 = {
            "id": "T-002",
            "title": "Ticket without validate",
            "type": "research",
        }
        pipeline._append_decision(self.run_, t2, {})
        with open(self.run_.path("map.md")) as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[1], "- T-002 Ticket without validate")

        # render_map includes decisions log
        out = runcmds.render_map(self.run_)
        self.assertIn("Decisions log:", out)
        self.assertIn("- T-001 Ticket with validate: All checks passed successfully", out)
        self.assertIn("- T-002 Ticket without validate", out)


if __name__ == "__main__":
    unittest.main()
