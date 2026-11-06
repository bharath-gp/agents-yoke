import contextlib
import io
import json
import os
import unittest
from unittest import mock

from tests.test_core import FAKE_CLI
from tests.test_planreview import Base
from yoke import adapters, cli, config, planreview, schema, shootdown


class TestShootdown(Base):
    def test_schema_file(self):
        s = schema.load_schema("shootdown")
        valid = {
            "verdict": "proceed",
            "findings": [
                {
                    "severity": "minor",
                    "requirement": "req 1",
                    "issue": "minor issue",
                    "suggestion": "fix it",
                }
            ],
            "summary": "Looks good.",
        }
        self.assertEqual(schema.validate(valid, s), [])

        valid_no_req = {
            "verdict": "revise",
            "findings": [
                {
                    "severity": "blocker",
                    "issue": "blocker issue",
                    "suggestion": "fix blocker",
                }
            ],
            "summary": "Need changes.",
        }
        self.assertEqual(schema.validate(valid_no_req, s), [])

        bad_severity = {
            "verdict": "revise",
            "findings": [{"severity": "critical", "issue": "bad", "suggestion": "fix"}],
            "summary": "bad",
        }
        self.assertTrue(schema.validate(bad_severity, s))

        extra_top = dict(valid, extra="bad")
        self.assertTrue(schema.validate(extra_top, s))

        extra_finding = {
            "verdict": "proceed",
            "findings": [{"severity": "minor", "issue": "x", "suggestion": "y", "extra": "bad"}],
            "summary": "x",
        }
        self.assertTrue(schema.validate(extra_finding, s))

    def test_empty_lenses_skipped(self):
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"YOKE_HOME": self.tmp}), \
             contextlib.redirect_stdout(buf):
            ret = cli.main(["shootdown", "--run", self.run_.run_id])
        self.assertEqual(ret, 0)
        self.assertIn("shootdown_lenses is empty; skipped", buf.getvalue())
        self.assertFalse(os.path.exists(self.run_.path("shootdown")))

    def test_one_result_per_lens(self):
        self.run_.update(config=dict(
            self.run_.cfg,
            shootdown_lenses=["zoom_out", "feasibility", "ergonomics"],
            parallel=1,
        ))
        ok_out = {
            "verdict": "proceed",
            "findings": [],
            "summary": "all clear",
        }
        with open(self.script, "w") as f:
            json.dump({
                "zoom_out": [{"structured_output": ok_out, "session_id": "s1"}],
                "feasibility": [{"structured_output": ok_out, "session_id": "s2"}],
                "ergonomics": [{"structured_output": ok_out, "session_id": "s3"}],
            }, f)
        calls_before = self.run_.read_json("run.json")["agent_calls"]
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"YOKE_HOME": self.tmp, "FAKE_SCRIPT": self.script}), \
             mock.patch("yoke.shootdown.adapters.get", return_value=adapters.FakeAdapter(FAKE_CLI)), \
             contextlib.redirect_stdout(buf):
            ret = cli.main(["shootdown", "--run", self.run_.run_id])
        self.assertEqual(ret, 0)
        for lens in ("zoom_out", "feasibility", "ergonomics"):
            p = self.run_.path("shootdown/round-1/%s.json" % lens)
            self.assertTrue(os.path.exists(p), "missing record %s" % p)
            rec = self.run_.read_json("shootdown/round-1/%s.json" % lens)
            self.assertEqual(rec["lens"], lens)
            self.assertEqual(rec["verdict"], "proceed")
            self.assertEqual(rec["findings"], [])
            self.assertEqual(rec["summary"], "all clear")
            self.assertIsNone(rec["error"])
        calls_after = self.run_.read_json("run.json")["agent_calls"]
        self.assertEqual(calls_after - calls_before, 3)

    def test_blockers_printed(self):
        self.run_.update(config=dict(self.run_.cfg, shootdown_lenses=["feasibility"], parallel=1))
        blocker_out = {
            "verdict": "revise",
            "findings": [
                {
                    "severity": "blocker",
                    "requirement": "Section 1",
                    "issue": "Missing dependency libxyz",
                    "suggestion": "Add dependency",
                }
            ],
            "summary": "Blocker found",
        }
        with open(self.script, "w") as f:
            json.dump({
                "feasibility": [{"structured_output": blocker_out, "session_id": "s1"}],
            }, f)
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"YOKE_HOME": self.tmp, "FAKE_SCRIPT": self.script}), \
             mock.patch("yoke.shootdown.adapters.get", return_value=adapters.FakeAdapter(FAKE_CLI)), \
             contextlib.redirect_stdout(buf):
            ret = cli.main(["shootdown", "--run", self.run_.run_id])
        self.assertEqual(ret, 0)
        out = buf.getvalue()
        self.assertIn("feasibility: revise, 1 blocker(s), 0 major(s)", out)
        self.assertIn("feasibility: [blocker] Missing dependency libxyz", out)

    def test_lens_failure_recorded_others_ok(self):
        self.run_.update(config=dict(self.run_.cfg, shootdown_lenses=["zoom_out", "feasibility"], parallel=1))
        bad_out = {"invalid": True}
        ok_out = {
            "verdict": "proceed",
            "findings": [],
            "summary": "all good",
        }
        with open(self.script, "w") as f:
            json.dump({
                "zoom_out": [
                    {"structured_output": bad_out, "session_id": "s1"},
                    {"structured_output": bad_out, "session_id": "s1"},
                ],
                "feasibility": [{"structured_output": ok_out, "session_id": "s2"}],
            }, f)
        buf_out = io.StringIO()
        buf_err = io.StringIO()
        with mock.patch.dict(os.environ, {"YOKE_HOME": self.tmp, "FAKE_SCRIPT": self.script}), \
             mock.patch("yoke.shootdown.adapters.get", return_value=adapters.FakeAdapter(FAKE_CLI)), \
             contextlib.redirect_stdout(buf_out), \
             contextlib.redirect_stderr(buf_err):
            ret = cli.main(["shootdown", "--run", self.run_.run_id])
        self.assertEqual(ret, 0)
        self.assertIn("zoom_out: failed: ", buf_err.getvalue())
        rec_bad = self.run_.read_json("shootdown/round-1/zoom_out.json")
        self.assertIsNone(rec_bad["verdict"])
        self.assertEqual(rec_bad["findings"], [])
        self.assertEqual(rec_bad["summary"], "")
        self.assertIsNotNone(rec_bad["error"])
        rec_ok = self.run_.read_json("shootdown/round-1/feasibility.json")
        self.assertEqual(rec_ok["verdict"], "proceed")
        self.assertIsNone(rec_ok["error"])

        # All lenses invalid -> exit 1
        with open(self.script, "w") as f:
            json.dump({
                "zoom_out": [
                    {"structured_output": bad_out, "session_id": "s1"},
                    {"structured_output": bad_out, "session_id": "s1"},
                ],
                "feasibility": [
                    {"structured_output": bad_out, "session_id": "s2"},
                    {"structured_output": bad_out, "session_id": "s2"},
                ],
            }, f)
        buf_out = io.StringIO()
        buf_err = io.StringIO()
        with mock.patch.dict(os.environ, {"YOKE_HOME": self.tmp, "FAKE_SCRIPT": self.script}), \
             mock.patch("yoke.shootdown.adapters.get", return_value=adapters.FakeAdapter(FAKE_CLI)), \
             contextlib.redirect_stdout(buf_out), \
             contextlib.redirect_stderr(buf_err):
            ret = cli.main(["shootdown", "--run", self.run_.run_id])
        self.assertEqual(ret, 1)
        err = buf_err.getvalue()
        self.assertIn("zoom_out: failed:", err)
        self.assertIn("feasibility: failed:", err)

    def test_missing_requirements_exits_1(self):
        self.run_.update(config=dict(self.run_.cfg, shootdown_lenses=["feasibility"]))
        os.remove(self.run_.path("requirements.md"))
        buf_err = io.StringIO()
        with mock.patch.dict(os.environ, {"YOKE_HOME": self.tmp}), \
             contextlib.redirect_stderr(buf_err):
            ret = cli.main(["shootdown", "--run", self.run_.run_id])
        self.assertEqual(ret, 1)
        err = buf_err.getvalue()
        self.assertTrue(err.startswith("yoke: "))
        self.assertNotIn("Traceback", err)

    def test_unknown_lens_value_exits_1(self):
        self.run_.update(config=dict(self.run_.cfg, shootdown_lenses=["nope"]))
        buf_err = io.StringIO()
        with mock.patch.dict(os.environ, {"YOKE_HOME": self.tmp}), \
             contextlib.redirect_stderr(buf_err):
            ret = cli.main(["shootdown", "--run", self.run_.run_id])
        self.assertEqual(ret, 1)
        err = buf_err.getvalue()
        self.assertIn("yoke: unknown shootdown_lenses entry", err)
        self.assertNotIn("Traceback", err)

    def test_budget_counts_calls(self):
        cur_calls = self.run_.read_json("run.json")["agent_calls"]
        self.run_.update(config=dict(
            self.run_.cfg,
            shootdown_lenses=["zoom_out", "feasibility", "ergonomics"],
            parallel=1,
            max_agent_calls_total=cur_calls + 1,
        ))
        ok_out = {
            "verdict": "proceed",
            "findings": [],
            "summary": "ok",
        }
        with open(self.script, "w") as f:
            json.dump({
                "zoom_out": [{"structured_output": ok_out, "session_id": "s1"}],
                "feasibility": [{"structured_output": ok_out, "session_id": "s2"}],
                "ergonomics": [{"structured_output": ok_out, "session_id": "s3"}],
            }, f)
        res = shootdown.shoot(
            self.run_,
            adapters.FakeAdapter(FAKE_CLI),
            env={"FAKE_SCRIPT": self.script},
        )
        self.assertIsNone(res["zoom_out"]["error"])
        self.assertEqual(res["feasibility"]["error"], "budget exhausted")
        self.assertEqual(res["ergonomics"]["error"], "budget exhausted")
        r_feas = self.run_.read_json("shootdown/round-1/feasibility.json")
        self.assertEqual(r_feas["error"], "budget exhausted")
        r_ergo = self.run_.read_json("shootdown/round-1/ergonomics.json")
        self.assertEqual(r_ergo["error"], "budget exhausted")
        calls_after = self.run_.read_json("run.json")["agent_calls"]
        self.assertEqual(calls_after - cur_calls, 1)

    def test_audit_dir(self):
        self.run_.update(config=dict(self.run_.cfg, shootdown_lenses=["zoom_out"], parallel=1))
        ok_out = {
            "verdict": "proceed",
            "findings": [],
            "summary": "ok",
        }
        with open(self.script, "w") as f:
            json.dump({
                "zoom_out": [{"structured_output": ok_out, "session_id": "s1"}],
            }, f)
        shootdown.shoot(
            self.run_,
            adapters.FakeAdapter(FAKE_CLI),
            env={"FAKE_SCRIPT": self.script},
        )
        audit = self.run_.path("shootdown/round-1/zoom_out")
        self.assertTrue(os.path.isdir(audit), "expected audit dir %s" % audit)

    def test_event_emitted(self):
        self.run_.update(config=dict(self.run_.cfg, shootdown_lenses=["zoom_out", "feasibility"], parallel=1))
        blocker_out = {
            "verdict": "revise",
            "findings": [
                {
                    "severity": "blocker",
                    "requirement": "R1",
                    "issue": "issue 1",
                    "suggestion": "suggestion 1",
                },
                {
                    "severity": "blocker",
                    "requirement": "R2",
                    "issue": "issue 2",
                    "suggestion": "suggestion 2",
                },
                {
                    "severity": "minor",
                    "issue": "issue 3",
                    "suggestion": "suggestion 3",
                },
            ],
            "summary": "blockers",
        }
        ok_out = {
            "verdict": "proceed",
            "findings": [],
            "summary": "clean",
        }
        with open(self.script, "w") as f:
            json.dump({
                "zoom_out": [{"structured_output": blocker_out, "session_id": "s1"}],
                "feasibility": [{"structured_output": ok_out, "session_id": "s2"}],
            }, f)
        shootdown.shoot(
            self.run_,
            adapters.FakeAdapter(FAKE_CLI),
            env={"FAKE_SCRIPT": self.script},
        )
        events_path = self.run_.path("events.jsonl")
        self.assertTrue(os.path.exists(events_path))
        with open(events_path) as f:
            events = [json.loads(line) for line in f if line.strip()]
        sh_events = [e for e in events if e.get("kind") == "shootdown"]
        self.assertEqual(len(sh_events), 1)
        ev = sh_events[0]
        self.assertEqual(ev["lenses"], ["zoom_out", "feasibility"])
        self.assertEqual(ev["ok"], 2)
        self.assertEqual(ev["blockers"], 2)

    def test_parallel_execution(self):
        self.run_.update(config=dict(
            self.run_.cfg,
            shootdown_lenses=["zoom_out", "feasibility", "ergonomics"],
            parallel=2,
        ))
        ok_out = {
            "verdict": "proceed",
            "findings": [],
            "summary": "clean",
        }
        with open(self.script, "w") as f:
            json.dump({
                "zoom_out": [{"structured_output": ok_out, "session_id": "s1"}],
                "feasibility": [{"structured_output": ok_out, "session_id": "s2"}],
                "ergonomics": [{"structured_output": ok_out, "session_id": "s3"}],
            }, f)
        res = shootdown.shoot(
            self.run_,
            adapters.FakeAdapter(FAKE_CLI),
            env={"FAKE_SCRIPT": self.script},
        )
        self.assertEqual(list(res.keys()), ["zoom_out", "feasibility", "ergonomics"])
        for lens in ("zoom_out", "feasibility", "ergonomics"):
            self.assertTrue(os.path.exists(self.run_.path("shootdown/round-1/%s.json" % lens)))
            self.assertIsNone(res[lens]["error"])
        events_path = self.run_.path("events.jsonl")
        with open(events_path) as f:
            events = [json.loads(line) for line in f if line.strip()]
        sh_events = [e for e in events if e.get("kind") == "shootdown"]
        self.assertEqual(sh_events[-1]["ok"], 3)


if __name__ == "__main__":
    unittest.main()
