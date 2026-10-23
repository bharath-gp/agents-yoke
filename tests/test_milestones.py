import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from yoke import cli, config, milestones, schema, state, tickets

os.environ.setdefault("YOKE_OLLAMA_URL", "http://127.0.0.1:9")


class TestMilestones(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = os.path.realpath(self._td.name)
        os.makedirs(os.path.join(self.tmp, "repo"))
        cfg = config.load(home=self.tmp, overrides={
            "max_tickets": 40,
            "kill_grace_s": 1,
            "router": {"ollama_url": "http://127.0.0.1:1"}
        })
        self.run_ = state.Run.create(os.path.join(self.tmp, "repo"), cfg, home=self.tmp)

    def imp_doc(self, doc):
        p = os.path.join(self.tmp, "in.json")
        with open(p, "w") as f:
            json.dump(doc, f)
        return tickets.import_tickets(self.run_, p)

    def test_valid_map_imports(self):
        doc = {
            "destination": "build milestone system",
            "out_of_scope": ["unrelated features"],
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
            ],
        }
        errs, ts = self.imp_doc(doc)
        self.assertEqual(errs, [])
        self.assertEqual(len(ts), 2)
        ms = milestones.milestones_of(self.run_)
        self.assertEqual(len(ms), 2)
        self.assertEqual(ms[0]["id"], "MS-01")
        self.assertEqual(ms[1]["id"], "MS-02")

    def test_missing_exit_check_rejected(self):
        for bad_ec in [None, "", "   "]:
            ms = [{"id": "MS-01", "title": "M1"}]
            if bad_ec is not None:
                ms[0]["exit_check"] = bad_ec
            doc = {
                "milestones": ms,
                "tickets": [
                    {
                        "id": "T-001",
                        "title": "t1",
                        "type": "feature",
                        "description": "d",
                        "exit_criteria": ["ok"],
                        "depends_on": [],
                        "files_hint": [],
                        "plan_excerpt": "p",
                        "milestone": "MS-01",
                    }
                ],
            }
            errs, out = self.imp_doc(doc)
            self.assertIsNone(out)
            self.assertTrue(any("missing exit_check" in e for e in errs), errs)

    def test_backward_dependency_violation_rejected(self):
        doc = {
            "milestones": [
                {"id": "MS-01", "title": "M1", "exit_check": "check1"},
                {"id": "MS-02", "title": "M2", "exit_check": "check2"},
            ],
            "tickets": [
                {
                    "id": "T-001",
                    "title": "t1",
                    "type": "feature",
                    "description": "d",
                    "exit_criteria": ["ok"],
                    "depends_on": ["T-002"],
                    "files_hint": [],
                    "plan_excerpt": "p",
                    "milestone": "MS-01",
                },
                {
                    "id": "T-002",
                    "title": "t2",
                    "type": "feature",
                    "description": "d",
                    "exit_criteria": ["ok"],
                    "depends_on": [],
                    "files_hint": [],
                    "plan_excerpt": "p",
                    "milestone": "MS-02",
                },
            ],
        }
        errs, out = self.imp_doc(doc)
        self.assertIsNone(out)
        self.assertTrue(any("backward-dependency violation" in e for e in errs), errs)

    def test_orphan_milestone_rejected(self):
        doc = {
            "milestones": [
                {"id": "MS-01", "title": "M1", "exit_check": "check1"},
                {"id": "MS-02", "title": "M2", "exit_check": "check2"},
            ],
            "tickets": [
                {
                    "id": "T-001",
                    "title": "t1",
                    "type": "feature",
                    "description": "d",
                    "exit_criteria": ["ok"],
                    "depends_on": [],
                    "files_hint": [],
                    "plan_excerpt": "p",
                    "milestone": "MS-01",
                }
            ],
        }
        errs, out = self.imp_doc(doc)
        self.assertIsNone(out)
        self.assertTrue(any("orphan milestone" in e for e in errs), errs)

    def test_unknown_milestone_rejected(self):
        doc = {
            "milestones": [
                {"id": "MS-01", "title": "M1", "exit_check": "check1"},
            ],
            "tickets": [
                {
                    "id": "T-001",
                    "title": "t1",
                    "type": "feature",
                    "description": "d",
                    "exit_criteria": ["ok"],
                    "depends_on": [],
                    "files_hint": [],
                    "plan_excerpt": "p",
                    "milestone": "MS-99",
                }
            ],
        }
        errs, out = self.imp_doc(doc)
        self.assertIsNone(out)
        self.assertTrue(any("unknown milestone" in e for e in errs), errs)

    def test_duplicate_milestone_id_rejected(self):
        doc = {
            "milestones": [
                {"id": "MS-01", "title": "M1", "exit_check": "check1"},
                {"id": "MS-01", "title": "M1 duplicate", "exit_check": "check2"},
            ],
            "tickets": [
                {
                    "id": "T-001",
                    "title": "t1",
                    "type": "feature",
                    "description": "d",
                    "exit_criteria": ["ok"],
                    "depends_on": [],
                    "files_hint": [],
                    "plan_excerpt": "p",
                    "milestone": "MS-01",
                }
            ],
        }
        errs, out = self.imp_doc(doc)
        self.assertIsNone(out)
        self.assertTrue(any("duplicate milestone id" in e for e in errs), errs)

    def test_no_milestones_yields_implicit_ms00(self):
        doc = {
            "tickets": [
                {
                    "id": "T-001",
                    "title": "t1",
                    "type": "feature",
                    "description": "d",
                    "exit_criteria": ["ok"],
                    "depends_on": [],
                    "files_hint": [],
                    "plan_excerpt": "p",
                }
            ]
        }
        errs, out = self.imp_doc(doc)
        self.assertEqual(errs, [])
        self.assertIsNotNone(out)
        ms = milestones.milestones_of(self.run_)
        self.assertEqual(len(ms), 1)
        self.assertEqual(ms[0]["id"], "MS-00")
        self.assertIsNone(ms[0]["exit_check"])
        self.assertEqual(milestones.milestones_of(doc), [{"id": "MS-00", "title": "implicit", "exit_check": None}])

    def test_wayfinder_model_tier(self):
        cfg = {
            "roles": {"by_role": {"wayfinder": "primary"}},
            "models": {
                "claude": {
                    "plan": {"model": "claude-sonnet-5-5", "effort": "high"},
                    "cheap": {"model": "claude-haiku-5-5", "effort": "low"},
                }
            },
        }
        self.assertEqual(config.model_for(cfg, "claude", "wayfinder"), ("claude-sonnet-5-5", "high"))
        def_cfg = config.load(home=self.tmp)
        self.assertIn("wayfinder", config.PLAN_ROLES)
        self.assertEqual(def_cfg["timeouts_s"]["wayfinder"], 1200)
        self.assertEqual(def_cfg["max_fix_tickets_per_milestone"], 2)
        self.assertEqual(def_cfg["max_chart_rounds"], 3)

    def test_make_fix_ticket_required_fields_and_validation(self):
        s = schema.load_schema("ticket")
        expected_required = [
            "id",
            "title",
            "type",
            "description",
            "exit_criteria",
            "depends_on",
            "files_hint",
            "plan_excerpt",
            "milestone",
        ]
        self.assertEqual(s.get("required"), expected_required)

        fix = milestones.make_fix_ticket(
            id="F-001",
            milestone="MS-01",
            failing_output="AssertionError: 1 != 2",
            exit_check="python3 -m unittest tests.test_core",
            title="Fix MS-01 failure",
        )
        for req in expected_required:
            self.assertIn(req, fix)
        errs = schema.validate(fix, s)
        self.assertEqual(errs, [])

        fix2 = milestones.make_fix_ticket(
            2,
            {"id": "MS-02", "title": "Execution", "exit_check": "python3 -m test_b"},
            "stack trace",
        )
        self.assertEqual(fix2["id"], "F-002")
        self.assertEqual(fix2["milestone"], "MS-02")
        self.assertEqual(fix2["exit_criteria"], ["python3 -m test_b exits 0"])
        self.assertEqual(schema.validate(fix2, s), [])

        with self.assertRaises(ValueError):
            milestones.make_fix_ticket(1, "MS-01", "trace")

    def test_merged_tickets(self):
        self.run_.write_json("tickets.json", {
            "tickets": [{"id": "T-001", "milestone": "MS-01"}]
        })
        self.run_.write_json("fixes.json", [
            {"id": "F-001", "milestone": "MS-01"}
        ])
        merged = milestones.merged_tickets(self.run_)
        self.assertEqual([t["id"] for t in merged], ["T-001", "F-001"])
        self.assertEqual(
            [t["id"] for t in milestones.merged_tickets([{"id": "T-001"}], [{"id": "F-001"}])],
            ["T-001", "F-001"],
        )

    def test_warnings(self):
        doc = {
            "milestones": [{"id": "MS-01", "title": "M1", "exit_check": "check"}],
            "tickets": [
                {
                    "id": "T-001",
                    "milestone": "MS-01",
                    "files_hint": ["f1", "f2", "f3", "f4", "f5", "f6", "f7"],
                    "exit_criteria": ["e1", "e2", "e3", "e4", "e5", "e6", "e7"],
                }
            ],
        }
        warns = tickets.warnings(doc)
        self.assertEqual(len(warns), 2)
        self.assertTrue(any("files_hint" in w for w in warns))
        self.assertTrue(any("exit_criteria" in w for w in warns))

        many_tickets = [{"id": "T-%03d" % i, "milestone": "MS-01"} for i in range(1, 14)]
        doc_many = {
            "milestones": [{"id": "MS-01", "title": "M1", "exit_check": "check"}],
            "tickets": many_tickets,
        }
        warns_many = tickets.warnings(doc_many)
        self.assertTrue(any("exceeds 12" in w or "more than 12" in w for w in warns_many))

    def test_warnings_detail_fields(self):
        base = {"id": "T-001", "milestone": "MS-01", "type": "feature"}
        warns = tickets.warnings({"tickets": [dict(base, steps=["edit a.py:f"])]})
        self.assertEqual(warns, ["ticket T-001 (feature) has no test_plan; the coder will have to plan it"])
        full = dict(base, steps=["edit a.py:f"], test_plan=["tests/test_a.py::test_f"])
        self.assertEqual(tickets.warnings({"tickets": [full, dict(base, id="T-002", type="docs")]}), [])

    def test_detail_fields_validate(self):
        t = {"id": "T-001", "title": "t", "type": "feature", "description": "d", "exit_criteria": ["e"],
             "depends_on": [], "files_hint": [], "plan_excerpt": "p", "design": "x",
             "interfaces": ["f(a) -> b"], "steps": ["s"], "test_plan": ["c"], "impacted_tests": ["none found"],
             "risks": ["r"], "plan_sections": ["w1"]}
        self.assertEqual(schema.validate({"tickets": [t]}, schema.load_schema("ticket-list")), [])
        self.assertEqual(schema.validate({"tickets": [t]}, schema.load_schema("chart")), [])
        self.assertTrue(schema.validate({"tickets": [dict(t, steps="s")]}, schema.load_schema("ticket-list")))

    def test_cli_show_grouped_by_milestone(self):
        doc = {
            "milestones": [
                {"id": "MS-01", "title": "First", "exit_check": "check1"},
                {"id": "MS-02", "title": "Second", "exit_check": "check2"},
            ],
            "tickets": [
                {
                    "id": "T-001",
                    "title": "t1",
                    "type": "feature",
                    "description": "d",
                    "exit_criteria": ["ok"],
                    "depends_on": [],
                    "files_hint": [],
                    "plan_excerpt": "p",
                    "milestone": "MS-01",
                },
                {
                    "id": "T-002",
                    "title": "t2",
                    "type": "test",
                    "description": "d",
                    "exit_criteria": ["ok"],
                    "depends_on": ["T-001"],
                    "files_hint": [],
                    "plan_excerpt": "p",
                    "milestone": "MS-02",
                },
            ],
        }
        p = os.path.join(self.tmp, "in.json")
        with open(p, "w") as f:
            json.dump(doc, f)
        run = ["--run", self.run_.run_id]
        os.environ["YOKE_HOME"] = self.tmp
        self.addCleanup(os.environ.pop, "YOKE_HOME", None)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main(["tickets", "import", p] + run), 0)
            self.assertEqual(cli.main(["tickets", "show"] + run), 0)
        out = buf.getvalue()
        self.assertIn("MS-01: First", out)
        self.assertIn("MS-02: Second", out)
        self.assertIn("T-001 [feature] t1", out)
        self.assertIn("T-002 [test] t2  (after T-001)", out)


if __name__ == "__main__":
    unittest.main()
