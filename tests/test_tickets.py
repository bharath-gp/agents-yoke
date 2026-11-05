import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout

from yoke import adapters, cli, config, milestones, planreview, schema, state, tickets
try:
    from tests.test_core import FAKE_CLI
except ImportError:
    from test_router import FAKE_CLI

os.environ.setdefault("YOKE_OLLAMA_URL", "http://127.0.0.1:9")


def tk(i, deps=(), **kw):
    t = {"id": "T-%03d" % i, "title": "t%d" % i, "type": "feature", "description": "d",
         "exit_criteria": ["ok"], "depends_on": list(deps), "files_hint": [], "plan_excerpt": ""}
    t.update(kw)
    return t


class TestTickets(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "repo"))
        cfg = config.load(home=self.tmp, overrides={"max_tickets": 3, "kill_grace_s": 1,
                                                    "router": {"ollama_url": "http://127.0.0.1:1"}})
        self.run_ = state.Run.create(os.path.join(self.tmp, "repo"), cfg, home=self.tmp)

    def imp(self, ts, adapter=None):
        p = os.path.join(self.tmp, "in.json")
        with open(p, "w") as f:
            json.dump({"tickets": ts}, f)
        return tickets.import_tickets(self.run_, p, adapter)

    def test_ok_writes_and_invalidates_approval(self):
        self.run_.write_json("approvals.json", {"plan": {"sha256": "a"}, "tickets": {"sha256": "b"}})
        errs, ts = self.imp([tk(1), tk(2, ["T-001"])])
        self.assertEqual(errs, [])
        self.assertEqual(len(self.run_.read_json("tickets.json")["tickets"]), 2)
        self.assertEqual(list(self.run_.read_json("approvals.json")), ["plan"])

    def test_rejects(self):
        cases = {
            "duplicate": [tk(1), tk(1)],
            "unknown": [tk(1, ["T-009"])],
            "cycle": [tk(1, ["T-002"]), tk(2, ["T-001"])],
            "max_tickets": [tk(i) for i in range(1, 5)],
            "exit_criteria": [tk(1, exit_criteria=[])],
            "does not match": [tk(1, id="X1")],
        }
        for needle, ts in cases.items():
            errs, out = self.imp(ts)
            self.assertIsNone(out, needle)
            self.assertTrue(any(needle in e for e in errs), (needle, errs))
        self.assertFalse(os.path.exists(self.run_.path("tickets.json")))

    def test_cycle_message_and_diamond_ok(self):
        self.assertEqual(tickets.find_cycle([tk(1, ["T-002"]), tk(2, ["T-003"]), tk(3, ["T-001"])]),
                         ["T-001", "T-002", "T-003", "T-001"])
        self.assertIsNone(tickets.find_cycle([tk(1), tk(2, ["T-001"]), tk(3, ["T-001"]), tk(4, ["T-002", "T-003"])]))

    def test_missing_type_is_classified_via_router(self):
        script = os.path.join(self.tmp, "s.json")
        with open(script, "w") as f:
            json.dump({"router": [{"structured_output": {"type": "docs", "confidence": 0.9, "reason": "x"}}]}, f)
        os.environ["FAKE_SCRIPT"] = script
        self.addCleanup(os.environ.pop, "FAKE_SCRIPT", None)
        t = tk(1)
        del t["type"]
        errs, ts = self.imp([t, tk(2, type="bogus")], adapters.FakeAdapter(FAKE_CLI))
        self.assertEqual(errs, [])
        self.assertEqual([x["type"] for x in ts], ["docs", "docs"])

    def test_cli_import_show_route(self):
        p = os.path.join(self.tmp, "in.json")
        with open(p, "w") as f:
            json.dump({"tickets": [tk(1), tk(2, ["T-001"], type="test")]}, f)
        run = ["--run", self.run_.run_id]
        os.environ["YOKE_HOME"] = self.tmp
        self.addCleanup(os.environ.pop, "YOKE_HOME", None)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main(["tickets", "import", p] + run), 0)
            self.assertEqual(cli.main(["tickets", "show"] + run), 0)
            self.assertEqual(cli.main(["route", "T-002"] + run), 0)
        out = buf.getvalue()
        self.assertIn("T-002 [test] t2  (after T-001)", out)
        self.assertIn('"stage": "qa"', out)

    def test_milestone_imports_and_rejections(self):
        # Valid map import
        p = os.path.join(self.tmp, "map.json")
        doc = {
            "destination": "dest",
            "out_of_scope": ["scope"],
            "milestones": [
                {"id": "MS-01", "title": "m1", "exit_check": "check1"},
                {"id": "MS-02", "title": "m2", "exit_check": "check2"},
            ],
            "tickets": [
                tk(1, milestone="MS-01"),
                tk(2, ["T-001"], milestone="MS-02"),
            ],
        }
        with open(p, "w") as f:
            json.dump(doc, f)
        errs, ts = tickets.import_tickets(self.run_, p)
        self.assertEqual(errs, [])
        self.assertEqual(len(ts), 2)
        ms = milestones.milestones_of(self.run_)
        self.assertEqual([m["id"] for m in ms], ["MS-01", "MS-02"])

        # Rejection cases
        cases = {
            "missing exit_check": {
                "milestones": [{"id": "MS-01", "title": "m1"}],
                "tickets": [tk(1, milestone="MS-01")],
            },
            "backward-dependency violation": {
                "milestones": [
                    {"id": "MS-01", "title": "m1", "exit_check": "c1"},
                    {"id": "MS-02", "title": "m2", "exit_check": "c2"},
                ],
                "tickets": [
                    tk(1, ["T-002"], milestone="MS-01"),
                    tk(2, milestone="MS-02"),
                ],
            },
            "orphan milestone": {
                "milestones": [
                    {"id": "MS-01", "title": "m1", "exit_check": "c1"},
                    {"id": "MS-02", "title": "m2", "exit_check": "c2"},
                ],
                "tickets": [tk(1, milestone="MS-01")],
            },
            "unknown milestone": {
                "milestones": [{"id": "MS-01", "title": "m1", "exit_check": "c1"}],
                "tickets": [tk(1, milestone="MS-99")],
            },
            "duplicate milestone id": {
                "milestones": [
                    {"id": "MS-01", "title": "m1", "exit_check": "c1"},
                    {"id": "MS-01", "title": "m1 dup", "exit_check": "c2"},
                ],
                "tickets": [tk(1, milestone="MS-01")],
            },
        }
        for needle, bad_doc in cases.items():
            with open(p, "w") as f:
                json.dump(bad_doc, f)
            errs, out = tickets.import_tickets(self.run_, p)
            self.assertIsNone(out, needle)
            self.assertTrue(any(needle in e for e in errs), (needle, errs))

        # File with no milestones yields one implicit MS-00
        p_no_ms = os.path.join(self.tmp, "no_ms.json")
        with open(p_no_ms, "w") as f:
            json.dump({"tickets": [tk(1)]}, f)
        errs, out = tickets.import_tickets(self.run_, p_no_ms)
        self.assertEqual(errs, [])
        self.assertEqual(milestones.milestones_of(self.run_), [{"id": "MS-00", "title": "implicit", "exit_check": None}])


class TestChainWarning(unittest.TestCase):
    def test_chain_flagged(self):
        ts = [
            {"id": "T-0081", "title": "t81", "type": "chore", "milestone": "MS-01", "depends_on": []},
            {"id": "T-0082", "title": "t82", "type": "chore", "milestone": "MS-01", "depends_on": ["T-0081"]},
            {"id": "T-0083", "title": "t83", "type": "chore", "milestone": "MS-01", "depends_on": ["T-0082"]},
            {"id": "T-0084", "title": "t84", "type": "chore", "milestone": "MS-01", "depends_on": ["T-0083"]},
            {"id": "T-0085", "title": "t85", "type": "chore", "milestone": "MS-01", "depends_on": ["T-0084"]},
        ]
        warns = tickets.warnings(ts)
        chain_warns = [w for w in warns if "dependency chain" in w]
        self.assertEqual(len(chain_warns), 1)
        w = chain_warns[0]
        self.assertIn("MS-01", w)
        self.assertIn("5 tickets", w)
        self.assertIn("T-0081 -> T-0082", w)
        self.assertEqual(
            w,
            "milestone MS-01 has a dependency chain of 5 tickets (T-0081 -> T-0082 -> T-0083 -> T-0084 -> T-0085); only true code dependencies belong in depends_on",
        )

    def test_three_ticket_full_chain_flagged(self):
        ts = [
            {"id": "T-001", "title": "t1", "type": "chore", "milestone": "MS-01", "depends_on": []},
            {"id": "T-002", "title": "t2", "type": "chore", "milestone": "MS-01", "depends_on": ["T-001"]},
            {"id": "T-003", "title": "t3", "type": "chore", "milestone": "MS-01", "depends_on": ["T-002"]},
        ]
        warns = tickets.warnings(ts)
        chain_warns = [w for w in warns if "dependency chain" in w]
        self.assertEqual(len(chain_warns), 1)
        self.assertEqual(
            chain_warns[0],
            "milestone MS-01 has a dependency chain of 3 tickets (T-001 -> T-002 -> T-003); only true code dependencies belong in depends_on",
        )

    def test_wide_milestone_no_warning(self):
        ts = [
            {"id": "T-001", "title": "t1", "type": "chore", "milestone": "MS-01", "depends_on": []},
            {"id": "T-002", "title": "t2", "type": "chore", "milestone": "MS-01", "depends_on": ["T-001"]},
            {"id": "T-003", "title": "t3", "type": "chore", "milestone": "MS-01", "depends_on": []},
            {"id": "T-004", "title": "t4", "type": "chore", "milestone": "MS-01", "depends_on": ["T-003"]},
            {"id": "T-005", "title": "t5", "type": "chore", "milestone": "MS-01", "depends_on": []},
        ]
        warns = tickets.warnings(ts)
        chain_warns = [w for w in warns if "dependency chain" in w]
        self.assertEqual(chain_warns, [])

    def test_cross_milestone_deps_ignored(self):
        ts = [
            {"id": "T-001", "title": "t1", "type": "chore", "milestone": "MS-01", "depends_on": []},
            {"id": "T-002", "title": "t2", "type": "chore", "milestone": "MS-01", "depends_on": ["T-001"]},
            {"id": "T-003", "title": "t3", "type": "chore", "milestone": "MS-02", "depends_on": ["T-002"]},
            {"id": "T-004", "title": "t4", "type": "chore", "milestone": "MS-02", "depends_on": ["T-003"]},
            {"id": "T-005", "title": "t5", "type": "chore", "milestone": "MS-02", "depends_on": ["T-001"]},
        ]
        warns = tickets.warnings(ts)
        chain_warns = [w for w in warns if "dependency chain" in w]
        self.assertEqual(chain_warns, [])

    def test_cycle_does_not_recurse(self):
        ts = [
            {"id": "T-001", "title": "t1", "type": "chore", "milestone": "MS-01", "depends_on": ["T-002"]},
            {"id": "T-002", "title": "t2", "type": "chore", "milestone": "MS-01", "depends_on": ["T-001"]},
            {"id": "T-003", "title": "t3", "type": "chore", "milestone": "MS-01", "depends_on": ["T-003"]},
        ]
        warns = tickets.warnings(ts)
        self.assertIsInstance(warns, list)
        chain_warns = [w for w in warns if "dependency chain" in w]
        self.assertEqual(chain_warns, [])
        self.assertEqual(len(tickets._longest_chain(ts)), 2)

    def test_malformed_input_does_not_crash(self):
        ts = [
            {"id": 123, "title": "bad id", "milestone": "MS-01", "depends_on": "not a list"},
            {"id": "T-001", "title": "t1", "milestone": "MS-01", "depends_on": [{}, ["nested"], None, "T-002"]},
            {"id": "T-002", "title": "t2", "milestone": ["unhashable"], "depends_on": []},
            {"id": "T-003", "title": "t3", "milestone": {"unhashable": "dict"}, "depends_on": []},
            "not a dict",
        ]
        warns = tickets.warnings(ts, milestones=[["unhashable_mid"], {"id": ["unhashable"]}, "MS-01"])
        self.assertIsInstance(warns, list)
        chain_warns = [w for w in warns if "dependency chain" in w]
        self.assertEqual(chain_warns, [])

    def test_two_ticket_chain_no_warning(self):
        ts = [
            {"id": "T-001", "title": "t1", "type": "chore", "milestone": "MS-01", "depends_on": []},
            {"id": "T-002", "title": "t2", "type": "chore", "milestone": "MS-01", "depends_on": ["T-001"]},
        ]
        warns = tickets.warnings(ts)
        chain_warns = [w for w in warns if "dependency chain" in w]
        self.assertEqual(chain_warns, [])

    def test_four_ticket_milestone_three_chain_no_warning(self):
        ts = [
            {"id": "T-001", "title": "t1", "type": "chore", "milestone": "MS-01", "depends_on": []},
            {"id": "T-002", "title": "t2", "type": "chore", "milestone": "MS-01", "depends_on": ["T-001"]},
            {"id": "T-003", "title": "t3", "type": "chore", "milestone": "MS-01", "depends_on": ["T-002"]},
            {"id": "T-004", "title": "t4", "type": "chore", "milestone": "MS-01", "depends_on": []},
        ]
        warns = tickets.warnings(ts)
        chain_warns = [w for w in warns if "dependency chain" in w]
        self.assertEqual(chain_warns, [])

    def test_four_ticket_milestone_four_chain_warns(self):
        ts = [
            {"id": "T-001", "title": "t1", "type": "chore", "milestone": "MS-01", "depends_on": []},
            {"id": "T-002", "title": "t2", "type": "chore", "milestone": "MS-01", "depends_on": ["T-001"]},
            {"id": "T-003", "title": "t3", "type": "chore", "milestone": "MS-01", "depends_on": ["T-002"]},
            {"id": "T-004", "title": "t4", "type": "chore", "milestone": "MS-01", "depends_on": ["T-003"]},
        ]
        warns = tickets.warnings(ts)
        chain_warns = [w for w in warns if "dependency chain" in w]
        self.assertEqual(len(chain_warns), 1)
        self.assertEqual(
            chain_warns[0],
            "milestone MS-01 has a dependency chain of 4 tickets (T-001 -> T-002 -> T-003 -> T-004); only true code dependencies belong in depends_on",
        )

    def test_milestone_ordering_respected(self):
        ts = [
            {"id": "T-001", "title": "t1", "type": "chore", "milestone": "MS-01", "depends_on": []},
            {"id": "T-002", "title": "t2", "type": "chore", "milestone": "MS-01", "depends_on": ["T-001"]},
            {"id": "T-003", "title": "t3", "type": "chore", "milestone": "MS-01", "depends_on": ["T-002"]},
            {"id": "T-011", "title": "t11", "type": "chore", "milestone": "MS-02", "depends_on": []},
            {"id": "T-012", "title": "t12", "type": "chore", "milestone": "MS-02", "depends_on": ["T-011"]},
            {"id": "T-013", "title": "t13", "type": "chore", "milestone": "MS-02", "depends_on": ["T-012"]},
        ]
        warns = tickets.warnings(ts, milestones=[{"id": "MS-02"}, {"id": "MS-01"}])
        chain_warns = [w for w in warns if "dependency chain" in w]
        self.assertEqual(len(chain_warns), 2)
        self.assertIn("MS-02", chain_warns[0])
        self.assertIn("MS-01", chain_warns[1])


class TestFilesField(unittest.TestCase):
    def test_schema_accepts_files_and_rejects_bad_action(self):
        s_list = schema.load_schema("ticket-list")
        doc_list = {
            "tickets": [
                {
                    "id": "T-001",
                    "title": "t1",
                    "type": "feature",
                    "description": "d",
                    "exit_criteria": ["ok"],
                    "depends_on": [],
                    "files_hint": [],
                    "plan_excerpt": "",
                    "files": [{"path": "a.py", "action": "modify"}],
                }
            ]
        }
        self.assertEqual(schema.validate(doc_list, s_list), [])

        doc_bad_action = {
            "tickets": [
                {
                    "id": "T-001",
                    "title": "t1",
                    "type": "feature",
                    "description": "d",
                    "exit_criteria": ["ok"],
                    "depends_on": [],
                    "files_hint": [],
                    "plan_excerpt": "",
                    "files": [{"path": "a.py", "action": "rename"}],
                }
            ]
        }
        errs = schema.validate(doc_bad_action, s_list)
        self.assertTrue(errs)
        self.assertTrue(any("action" in e or "rename" in e for e in errs))

        doc_missing_path = {
            "tickets": [
                {
                    "id": "T-001",
                    "title": "t1",
                    "type": "feature",
                    "description": "d",
                    "exit_criteria": ["ok"],
                    "depends_on": [],
                    "files_hint": [],
                    "plan_excerpt": "",
                    "files": [{"action": "modify"}],
                }
            ]
        }
        errs = schema.validate(doc_missing_path, s_list)
        self.assertTrue(errs)
        self.assertTrue(any("path" in e for e in errs))

        s_ticket = schema.load_schema("ticket")
        ticket_doc = {
            "id": "T-001",
            "title": "t1",
            "type": "feature",
            "description": "d",
            "exit_criteria": ["ok"],
            "depends_on": [],
            "files_hint": [],
            "plan_excerpt": "",
            "milestone": "MS-01",
            "files": [{"path": "a.py", "action": "modify"}],
        }
        self.assertEqual(schema.validate(ticket_doc, s_ticket), [])
        bad_ticket = dict(ticket_doc, files=[{"path": "a.py", "action": "rename"}])
        self.assertTrue(schema.validate(bad_ticket, s_ticket))
        missing_path_ticket = dict(ticket_doc, files=[{"action": "modify"}])
        self.assertTrue(schema.validate(missing_path_ticket, s_ticket))

        s_chart = schema.load_schema("chart")
        chart_doc = {
            "tickets": [
                {
                    "id": "T-001",
                    "title": "t1",
                    "type": "feature",
                    "description": "d",
                    "exit_criteria": ["ok"],
                    "depends_on": [],
                    "files_hint": [],
                    "plan_excerpt": "",
                    "files": [{"path": "a.py", "action": "modify"}],
                }
            ]
        }
        self.assertEqual(schema.validate(chart_doc, s_chart), [])
        bad_chart = {
            "tickets": [
                {
                    "id": "T-001",
                    "title": "t1",
                    "type": "feature",
                    "description": "d",
                    "exit_criteria": ["ok"],
                    "depends_on": [],
                    "files_hint": [],
                    "plan_excerpt": "",
                    "files": [{"path": "a.py", "action": "rename"}],
                }
            ]
        }
        self.assertTrue(schema.validate(bad_chart, s_chart))

    def test_old_tickets_import_hash_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            os.makedirs(repo)
            run = planreview.init_run(repo, home=tmp)
            doc = {
                "tickets": [
                    {
                        "id": "T-001",
                        "title": "t1",
                        "type": "feature",
                        "description": "d",
                        "exit_criteria": ["ok"],
                        "depends_on": [],
                        "files_hint": ["a.py"],
                        "plan_excerpt": "",
                    }
                ]
            }
            p = os.path.join(tmp, "doc.json")
            with open(p, "w") as f:
                json.dump(doc, f)
            errs, ts = tickets.import_tickets(run, p)
            self.assertEqual(errs, [])
            saved = run.read_json("tickets.json")
            for t in saved["tickets"]:
                self.assertNotIn("files", t)
            h1 = planreview.current_hash(run, "tickets")
            errs2, ts2 = tickets.import_tickets(run, p)
            self.assertEqual(errs2, [])
            h2 = planreview.current_hash(run, "tickets")
            self.assertEqual(h1, h2)

    def test_warns_detailed_ticket_without_files(self):
        for typ in ("feature", "bugfix", "refactor"):
            with self.subTest(type=typ):
                t_detailed = {
                    "id": "T-001",
                    "title": "t1",
                    "type": typ,
                    "description": "d",
                    "exit_criteria": ["ok"],
                    "depends_on": [],
                    "files_hint": [],
                    "plan_excerpt": "",
                    "steps": ["step1"],
                    "test_plan": ["test1"],
                }
                warns = tickets.warnings([t_detailed])
                self.assertTrue(
                    any(
                        f"ticket T-001 ({typ}) has no files; its write boundary will not be checked" in w
                        for w in warns
                    )
                )

                t_empty_files = dict(t_detailed, files=[])
                warns_empty = tickets.warnings([t_empty_files])
                self.assertTrue(
                    any(
                        f"ticket T-001 ({typ}) has no files; its write boundary will not be checked" in w
                        for w in warns_empty
                    )
                )

                t_with_files = dict(t_detailed, files=[{"path": "a.py", "action": "modify"}])
                warns_files = tickets.warnings([t_with_files])
                self.assertFalse(any("has no files" in w for w in warns_files))

        t_docs = {
            "id": "T-002",
            "title": "t2",
            "type": "docs",
            "description": "d",
            "exit_criteria": ["ok"],
            "depends_on": [],
            "files_hint": [],
            "plan_excerpt": "",
        }
        warns3 = tickets.warnings([t_docs])
        self.assertFalse(any("has no files" in w for w in warns3))


class TestMissingRefs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, "yoke"))
        os.makedirs(os.path.join(self.repo, "tests"))
        with open(os.path.join(self.repo, "yoke", "a.py"), "w") as f:
            f.write("# a\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reports_missing_repo_path(self):
        self.assertEqual(
            tickets.missing_refs(
                ["see `yoke/a.py` and `yoke/b.py:fn` and `tests/test_x.py::T::t`"],
                self.repo,
            ),
            ["tests/test_x.py", "yoke/b.py"],
        )

    def test_skips_non_paths(self):
        inputs = [
            "`plan.md`",
            "`~/.yoke/x`",
            "`$YOKE_HOME/x`",
            "`yoke/*.py`",
            "`yoke/schemas/{a,b}.json`",
            "`https://x/y`",
            "`plan-review/round-1/debate.json`",
            "`./yoke/x.py`",
            "`-x/y`",
            "`yoke/a b.py`",
            "`foo(yoke/x.py)`",
            "`yoke/a.py#L10`",
        ]
        self.assertEqual(tickets.missing_refs(inputs, self.repo), [])
        self.assertEqual(tickets.missing_refs(["see `yoke/b.py`"], None), [])
        self.assertEqual(tickets.missing_refs([None, 5], self.repo), [])

    def test_create_declared_not_reported(self):
        self.assertEqual(
            tickets.missing_refs(
                ["`yoke/new.py`"],
                self.repo,
                created=["yoke/new.py"],
            ),
            [],
        )

    def test_warnings_with_repo_flags_missing_modify_file(self):
        t_modify = {
            "id": "T-001",
            "title": "t1",
            "type": "feature",
            "description": "d",
            "exit_criteria": ["ok"],
            "depends_on": [],
            "files_hint": [],
            "plan_excerpt": "",
            "steps": ["step1"],
            "test_plan": ["test1"],
            "files": [{"path": "a.py", "action": "modify"}],
        }
        t_create = {
            "id": "T-002",
            "title": "t2",
            "type": "feature",
            "description": "d",
            "exit_criteria": ["ok"],
            "depends_on": [],
            "files_hint": [],
            "plan_excerpt": "",
            "steps": ["step1"],
            "test_plan": ["test1"],
            "files": [{"path": "b.py", "action": "create"}],
        }
        doc = {"tickets": [t_modify, t_create]}

        warns = tickets.warnings(doc, repo=self.repo)
        self.assertIn("ticket T-001 references missing path a.py", warns)
        self.assertFalse(any("T-002 references missing path" in w for w in warns))

        warns_no_repo = tickets.warnings(doc)
        self.assertFalse(any("references missing path" in w for w in warns_no_repo))

    def test_warnings_with_repo_scans_ticket_text_fields(self):
        t1 = {
            "id": "T-001",
            "title": "t1",
            "type": "feature",
            "description": "references `yoke/missing1.py`",
            "exit_criteria": ["`yoke/created_in_t2.py` exists"],
            "depends_on": [],
            "files_hint": [],
            "plan_excerpt": "see `tests/test_missing.py`",
            "steps": ["modify `yoke/missing2.py`"],
            "test_plan": ["test1"],
            "files": [],
        }
        t2 = {
            "id": "T-002",
            "title": "t2",
            "type": "feature",
            "description": "d",
            "exit_criteria": ["ok"],
            "depends_on": [],
            "files_hint": [],
            "plan_excerpt": "",
            "steps": ["step1"],
            "test_plan": ["test1"],
            "files": [{"path": "yoke/created_in_t2.py", "action": "create"}],
        }
        doc = {"tickets": [t1, t2]}
        warns = tickets.warnings(doc, repo=self.repo)
        self.assertIn("ticket T-001 references missing path tests/test_missing.py", warns)
        self.assertIn("ticket T-001 references missing path yoke/missing1.py", warns)
        self.assertIn("ticket T-001 references missing path yoke/missing2.py", warns)
        self.assertFalse(any("created_in_t2.py" in w for w in warns))


if __name__ == "__main__":
    unittest.main()
