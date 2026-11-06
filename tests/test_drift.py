import contextlib
import glob
import io
import json
import os
import subprocess
import unittest
from unittest import mock

from tests.test_core import FAKE_CLI
from tests.test_gitops import ENV
from tests.test_planreview import Base
from yoke import adapters, cli, config, drift, gitops, schema

os.environ.setdefault("YOKE_OLLAMA_URL", "http://127.0.0.1:9")


class TestDrift(Base):
    def setUp(self):
        super().setUp()
        old_env = {k: os.environ.get(k) for k in ENV}
        for k, v in ENV.items():
            os.environ[k] = v
        self.addCleanup(self._restore_env, old_env)

        # Setup git repo with main branch and initial commit
        subprocess.run(["git", "-C", self.repo, "checkout", "-q", "-b", "main"], check=True)
        with open(os.path.join(self.repo, "base.txt"), "w") as f:
            f.write("base commit\n")
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "base commit"], check=True)
        self.base_sha = gitops.head_sha(self.repo)

        # Create feature branch yoke/<run_id> with added file/line
        self.branch = "yoke/" + self.run_.run_id
        subprocess.run(["git", "-C", self.repo, "checkout", "-q", "-b", self.branch, self.base_sha], check=True)
        with open(os.path.join(self.repo, "feature.txt"), "w") as f:
            f.write("feature branch line\n")
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "feature commit"], check=True)

        # Switch back to main and create a commit after branching
        subprocess.run(["git", "-C", self.repo, "checkout", "-q", "main"], check=True)
        with open(os.path.join(self.repo, "main_only.txt"), "w") as f:
            f.write("main only line\n")
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "main only commit"], check=True)

        # Update run.json with repo, branch, base_sha
        info = self.run_.read_json("run.json")
        info["repo"] = self.repo
        info["branch"] = self.branch
        info["base_sha"] = self.base_sha
        self.run_.write_json("run.json", info)

        self.plan("## Work items\n- item 1\n\n## Next\nsomething\n")

    def _restore_env(self, old_env):
        for k, old in old_env.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old

    def test_off_by_default_skipped(self):
        if os.path.exists(self.run_.path("drift.json")):
            os.remove(self.run_.path("drift.json"))
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"YOKE_HOME": self.tmp}), \
             contextlib.redirect_stdout(buf):
            rc = cli.main(["drift", "--run", self.run_.run_id])
        self.assertEqual(rc, 0)
        self.assertIn("drift_check is off; skipped", buf.getvalue())
        self.assertFalse(os.path.exists(self.run_.path("drift.json")))

    def test_work_items_text_extracts_section(self):
        plan_with_sections = (
            "# Plan\n\n"
            "## Context\nSome context\n\n"
            "## Work items\n- W1: work item 1\n- W2: work item 2\n\n"
            "## Decisions\nSome decisions\n"
        )
        txt = drift.work_items_text(plan_with_sections)
        self.assertIn("- W1: work item 1", txt)
        self.assertIn("- W2: work item 2", txt)
        self.assertNotIn("Some context", txt)
        self.assertNotIn("Some decisions", txt)

    def test_work_items_text_fallback_whole_plan(self):
        plan_without_section = "# Plan\nJust plain text\n"
        self.assertEqual(drift.work_items_text(plan_without_section), plan_without_section)

    def test_work_items_text_last_section(self):
        plan_last = "# Plan\n\n## Context\nSome context\n\n## Work items\n- W1: only work item\n"
        txt = drift.work_items_text(plan_last)
        self.assertIn("## Work items\n- W1: only work item", txt)
        self.assertNotIn("Some context", txt)

    def test_writes_drift_json_and_map_section(self):
        self.run_.update(config=dict(self.run_.cfg, drift_check=True))
        resp = {
            "summary": "found 2 drifts",
            "drift": [
                {"work_item": "W1", "kind": "missing", "detail": "W1 missing"},
                {"work_item": "W2", "kind": "divergent", "detail": "W2 divergent"},
            ],
        }
        with open(self.script, "w") as f:
            json.dump({"drift": [{"structured_output": resp, "session_id": "s1"}]}, f)

        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"YOKE_HOME": self.tmp, "FAKE_SCRIPT": self.script}), \
             mock.patch("yoke.drift.adapters.get", return_value=adapters.FakeAdapter(FAKE_CLI)), \
             contextlib.redirect_stdout(buf):
            rc = cli.main(["drift", "--run", self.run_.run_id])
        self.assertEqual(rc, 0)

        data = self.run_.read_json("drift.json")
        self.assertEqual(len(data["drift"]), 2)
        self.assertIsNone(data["error"])

        with open(self.run_.path("map.md")) as f:
            map_text = f.read()
        self.assertIn("## Plan drift (", map_text)
        self.assertIn("- [missing] W1: W1 missing", map_text)
        self.assertIn("- [divergent] W2: W2 divergent", map_text)

        out = buf.getvalue()
        self.assertIn("drift: " + self.run_.path("drift.json"), out)
        self.assertIn("[missing] W1: W1 missing", out)
        self.assertIn("[divergent] W2: W2 divergent", out)

        with open(self.run_.path("events.jsonl")) as f:
            events = [json.loads(line) for line in f]
        drift_events = [e for e in events if e.get("kind") == "drift"]
        self.assertTrue(drift_events)
        self.assertTrue(drift_events[-1]["ok"])
        self.assertEqual(drift_events[-1]["entries"], 2)

    def test_no_drift_message(self):
        self.run_.update(config=dict(self.run_.cfg, drift_check=True))
        resp = {"summary": "all good", "drift": []}
        with open(self.script, "w") as f:
            json.dump({"drift": [{"structured_output": resp, "session_id": "s1"}]}, f)

        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"YOKE_HOME": self.tmp, "FAKE_SCRIPT": self.script}), \
             mock.patch("yoke.drift.adapters.get", return_value=adapters.FakeAdapter(FAKE_CLI)), \
             contextlib.redirect_stdout(buf):
            rc = cli.main(["drift", "--run", self.run_.run_id])
        self.assertEqual(rc, 0)

        out = buf.getvalue()
        self.assertIn("drift: " + self.run_.path("drift.json"), out)
        self.assertIn("drift: none", out)

        with open(self.run_.path("map.md")) as f:
            map_text = f.read()
        self.assertIn("No drift found.", map_text)

    def test_diff_is_base_to_branch(self):
        self.run_.update(config=dict(self.run_.cfg, drift_check=True))
        resp = {"summary": "ok", "drift": []}
        with open(self.script, "w") as f:
            json.dump({"drift": [{"structured_output": resp, "session_id": "s1"}]}, f)

        with mock.patch.dict(os.environ, {"YOKE_HOME": self.tmp, "FAKE_SCRIPT": self.script}), \
             mock.patch("yoke.drift.adapters.get", return_value=adapters.FakeAdapter(FAKE_CLI)):
            rc = cli.main(["drift", "--run", self.run_.run_id])
        self.assertEqual(rc, 0)

        audit_prompts = glob.glob(self.run_.path("calls/drift-*/prompt.md"))
        self.assertTrue(audit_prompts)
        with open(audit_prompts[-1]) as f:
            prompt_text = f.read()
        self.assertIn("feature branch line", prompt_text)
        self.assertNotIn("main only line", prompt_text)
        self.assertNotIn("diff truncated", prompt_text)
        self.assertIn("Diff: git diff %s..%s" % (self.base_sha, self.branch), prompt_text)
        self.assertIn("## Work items", prompt_text)
        self.assertIn("- item 1", prompt_text)

    def test_diff_truncated_over_cap(self):
        self.run_.update(config=dict(self.run_.cfg, drift_check=True))
        resp = {"summary": "ok", "drift": []}
        with open(self.script, "w") as f:
            json.dump({"drift": [{"structured_output": resp, "session_id": "s1"}]}, f)

        with mock.patch.dict(os.environ, {"YOKE_HOME": self.tmp, "FAKE_SCRIPT": self.script}), \
             mock.patch("yoke.drift.adapters.get", return_value=adapters.FakeAdapter(FAKE_CLI)), \
             mock.patch("yoke.drift.DRIFT_DIFF_MAX", 10):
            rc = cli.main(["drift", "--run", self.run_.run_id])
        self.assertEqual(rc, 0)

        audit_prompts = glob.glob(self.run_.path("calls/drift-*/prompt.md"))
        self.assertTrue(audit_prompts)
        with open(audit_prompts[-1]) as f:
            prompt_text = f.read()
        self.assertIn("... (diff truncated at 10 chars)", prompt_text)

    def test_call_failure_is_advisory(self):
        self.run_.update(config=dict(self.run_.cfg, drift_check=True))
        with open(self.run_.path("map.md"), "w") as f:
            f.write("original map\n")

        with open(self.script, "w") as f:
            json.dump({"drift": [{"exit": 1, "stderr": "agent failed"}]}, f)

        buf_out = io.StringIO()
        buf_err = io.StringIO()
        with mock.patch.dict(os.environ, {"YOKE_HOME": self.tmp, "FAKE_SCRIPT": self.script}), \
             mock.patch("yoke.drift.adapters.get", return_value=adapters.FakeAdapter(FAKE_CLI)), \
             contextlib.redirect_stdout(buf_out), \
             contextlib.redirect_stderr(buf_err):
            rc = cli.main(["drift", "--run", self.run_.run_id])
        self.assertEqual(rc, 0)
        self.assertIn("drift failed: ", buf_err.getvalue())
        self.assertNotIn("drift: none", buf_out.getvalue())

        data = self.run_.read_json("drift.json")
        self.assertIsNotNone(data["error"])
        self.assertEqual(data["drift"], [])

        with open(self.run_.path("map.md")) as f:
            self.assertEqual(f.read(), "original map\n")

        with open(self.run_.path("events.jsonl")) as f:
            events = [json.loads(line) for line in f]
        drift_events = [e for e in events if e.get("kind") == "drift"]
        self.assertTrue(drift_events)
        self.assertFalse(drift_events[-1]["ok"])
        self.assertEqual(drift_events[-1]["entries"], 0)

    def test_git_error_is_advisory(self):
        self.run_.update(config=dict(self.run_.cfg, drift_check=True))
        with open(self.run_.path("map.md"), "w") as f:
            f.write("original map\n")
        info = self.run_.read_json("run.json")
        info["base_sha"] = "bogus_ref_nonexistent"
        self.run_.write_json("run.json", info)

        buf_err = io.StringIO()
        with mock.patch.dict(os.environ, {"YOKE_HOME": self.tmp}), \
             contextlib.redirect_stderr(buf_err):
            rc = cli.main(["drift", "--run", self.run_.run_id])
        self.assertEqual(rc, 0)
        self.assertIn("drift failed: git diff ", buf_err.getvalue())
        self.assertIn("fatal: ambiguous argument", buf_err.getvalue())

        data = self.run_.read_json("drift.json")
        self.assertIsNotNone(data["error"])
        self.assertEqual(data["drift"], [])
        with open(self.run_.path("map.md")) as f:
            self.assertEqual(f.read(), "original map\n")

        with open(self.run_.path("events.jsonl")) as f:
            events = [json.loads(line) for line in f]
        drift_events = [e for e in events if e.get("kind") == "drift"]
        self.assertTrue(drift_events)
        self.assertFalse(drift_events[-1]["ok"])
        self.assertEqual(drift_events[-1]["entries"], 0)

    def test_schema_invalid_fallback(self):
        self.run_.update(config=dict(self.run_.cfg, drift_check=True))
        with open(self.script, "w") as f:
            json.dump({"drift": [{"structured_output": "not-a-dict", "session_id": "s1"}]}, f)

        with mock.patch.dict(os.environ, {"YOKE_HOME": self.tmp, "FAKE_SCRIPT": self.script}), \
             mock.patch("yoke.drift.adapters.get", return_value=adapters.FakeAdapter(FAKE_CLI)):
            rec = drift.check_drift(self.run_)
        self.assertEqual(rec["error"], "schema invalid")
        self.assertEqual(rec["drift"], [])

        with open(self.run_.path("events.jsonl")) as f:
            events = [json.loads(line) for line in f]
        drift_events = [e for e in events if e.get("kind") == "drift"]
        self.assertTrue(drift_events)
        self.assertFalse(drift_events[-1]["ok"])
        self.assertEqual(drift_events[-1]["entries"], 0)

    def test_missing_run_info_exits_1(self):
        self.run_.update(config=dict(self.run_.cfg, drift_check=True))
        info = self.run_.read_json("run.json")
        del info["base_sha"]
        self.run_.write_json("run.json", info)

        buf_err = io.StringIO()
        with mock.patch.dict(os.environ, {"YOKE_HOME": self.tmp}), \
             contextlib.redirect_stderr(buf_err):
            rc = cli.main(["drift", "--run", self.run_.run_id])
        self.assertEqual(rc, 1)
        err = buf_err.getvalue()
        self.assertIn("yoke: ", err)
        self.assertNotIn("Traceback", err)

    def test_missing_plan_exits_1(self):
        self.run_.update(config=dict(self.run_.cfg, drift_check=True))
        if os.path.exists(self.run_.path("plan.md")):
            os.remove(self.run_.path("plan.md"))

        buf_err = io.StringIO()
        with mock.patch.dict(os.environ, {"YOKE_HOME": self.tmp}), \
             contextlib.redirect_stderr(buf_err):
            rc = cli.main(["drift", "--run", self.run_.run_id])
        self.assertEqual(rc, 1)
        err = buf_err.getvalue()
        self.assertIn("yoke: ", err)
        self.assertNotIn("Traceback", err)

    def test_schema_validates_kinds(self):
        s = schema.load_schema("drift")
        bad_kind = {
            "summary": "ok",
            "drift": [{"work_item": "W1", "kind": "other", "detail": "something"}],
        }
        errs = schema.validate(bad_kind, s)
        self.assertTrue(errs)
        self.assertTrue(any("kind" in e for e in errs))

        extra_prop = {
            "summary": "ok",
            "drift": [{"work_item": "W1", "kind": "missing", "detail": "something", "extra": "prop"}],
        }
        errs = schema.validate(extra_prop, s)
        self.assertTrue(errs)
        self.assertTrue(any("extra" in e for e in errs))

        valid = {
            "summary": "ok",
            "drift": [{"work_item": "W1", "kind": "missing", "detail": "something"}],
        }
        self.assertEqual(schema.validate(valid, s), [])

    def test_audit_and_budget(self):
        self.run_.update(config=dict(self.run_.cfg, drift_check=True))
        resp = {"summary": "ok", "drift": []}
        with open(self.script, "w") as f:
            json.dump({"drift": [{"structured_output": resp, "session_id": "s1"}]}, f)

        with open(self.run_.path("map.md"), "w") as f:
            f.write("original map\n")

        c0 = self.run_.read_json("run.json")["agent_calls"]
        with mock.patch.dict(os.environ, {"YOKE_HOME": self.tmp, "FAKE_SCRIPT": self.script}), \
             mock.patch("yoke.drift.adapters.get", return_value=adapters.FakeAdapter(FAKE_CLI)):
            drift.check_drift(self.run_)
        c1 = self.run_.read_json("run.json")["agent_calls"]
        self.assertEqual(c1, c0 + 1)

        # Exhaust budget
        self.run_.update(config=dict(self.run_.cfg, max_agent_calls_total=c1))
        with open(self.run_.path("map.md"), "w") as f:
            f.write("original map\n")
        with mock.patch.dict(os.environ, {"YOKE_HOME": self.tmp, "FAKE_SCRIPT": self.script}), \
             mock.patch("yoke.drift.adapters.get", return_value=adapters.FakeAdapter(FAKE_CLI)):
            rec = drift.check_drift(self.run_)
        self.assertEqual(rec["error"], "budget exhausted")
        self.assertEqual(self.run_.read_json("drift.json")["error"], "budget exhausted")
        self.assertEqual(self.run_.read_json("run.json")["agent_calls"], c1)
        with open(self.run_.path("map.md")) as f:
            self.assertEqual(f.read(), "original map\n")

        with open(self.run_.path("events.jsonl")) as f:
            events = [json.loads(line) for line in f]
        drift_events = [e for e in events if e.get("kind") == "drift"]
        self.assertTrue(drift_events)
        self.assertFalse(drift_events[-1]["ok"])
        self.assertEqual(drift_events[-1]["entries"], 0)

    def test_pr_section(self):
        # 1. missing drift.json -> ''
        if os.path.exists(self.run_.path("drift.json")):
            os.remove(self.run_.path("drift.json"))
        self.assertEqual(drift.pr_section(self.run_), "")

        # 2. error set -> ''
        self.run_.write_json("drift.json", {
            "base_sha": self.base_sha,
            "branch": self.branch,
            "created_at": "now",
            "summary": "",
            "drift": [],
            "error": "some error",
        })
        self.assertEqual(drift.pr_section(self.run_), "")

        # 3. two entries -> starts '## Plan drift' with both lines
        self.run_.write_json("drift.json", {
            "base_sha": self.base_sha,
            "branch": self.branch,
            "created_at": "now",
            "summary": "summary",
            "drift": [
                {"work_item": "W1", "kind": "missing", "detail": "W1 detail"},
                {"work_item": "W2", "kind": "divergent", "detail": "W2 detail"},
            ],
            "error": None,
        })
        sec = drift.pr_section(self.run_)
        self.assertTrue(sec.startswith("## Plan drift\n\n"))
        self.assertIn("- [missing] W1: W1 detail", sec)
        self.assertIn("- [divergent] W2: W2 detail", sec)

        # 4. empty drift list -> '## Plan drift\n\nNo drift found.'
        self.run_.write_json("drift.json", {
            "base_sha": self.base_sha,
            "branch": self.branch,
            "created_at": "now",
            "summary": "",
            "drift": [],
            "error": None,
        })
        self.assertEqual(drift.pr_section(self.run_), "## Plan drift\n\nNo drift found.")

        # 5. limit small -> ends with '... (truncated; see drift.json)'
        truncated = drift.pr_section(self.run_, limit=25)
        self.assertTrue(truncated.endswith("\n... (truncated; see drift.json)"))

        # 6. limit larger than text -> not truncated
        not_truncated = drift.pr_section(self.run_, limit=1000)
        self.assertEqual(not_truncated, "## Plan drift\n\nNo drift found.")
        self.assertNotIn("truncated", not_truncated)


if __name__ == "__main__":
    unittest.main()
