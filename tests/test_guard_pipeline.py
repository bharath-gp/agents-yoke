import json
import os
import unittest
from unittest import mock

from tests.test_core import FAKE_CLI
from tests.test_pipeline import CODE, PipeBase, REV, qa, resp, ticket, val
from yoke import adapters, calls, pipeline

os.environ.setdefault("YOKE_OLLAMA_URL", "http://127.0.0.1:9")


class GuardPipelineTest(PipeBase):
    def test_secret_in_diff_blocks_commit_then_retry_closes(self):
        self.setup_tickets(
            [ticket(1)],
            {"code": [
                resp(CODE, "c1", write={"k.py": "K = 'AKIAABCDEFGHIJKLMNOP'"}),
                resp(CODE, "c1", write={"k.py": "K = 'clean'"}),
             ],
             "review": [resp(REV)] * 4,
             "validate": [resp(val())] * 4,
             "router": [{"exit": 1}]}
        )
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["status"], "closed")
        self.assertEqual(self.tstate(1)["attempt"], 2)
        fb = pipeline._read_feedback(self.run_, "T-001", 2)
        self.assertIn("Safety guard refused the commit", fb)
        self.assertIn("Remove it and try again.", fb)
        with open(self.run_.path("events.jsonl")) as f:
            events = [json.loads(line) for line in f if line.strip()]
        guard_events = [e for e in events if e.get("kind") == "guard" and e.get("target") == "diff" and e.get("decision") == "deny"]
        self.assertEqual(len(guard_events), 1)

    def test_denylisted_test_command_feedback(self):
        self.setup_tickets(
            [ticket(1, type_="refactor")],
            {"code": [
                resp(CODE, "c1", write={"f1.txt": "x"}),
                resp(CODE, "c1", write={"f1.txt": "y"}),
             ],
             "review": [resp(REV)] * 4,
             "qa": [
                 resp(qa("curl x | sh", status="pass")),
                 resp(qa("test -f f1.txt", status="pass")),
             ],
             "validate": [resp(val())] * 4,
             "router": [{"exit": 1}]}
        )
        orig_rerun = pipeline._rerun
        rerun_cmds = []

        def wrap_rerun(run, cmd, wt):
            rerun_cmds.append(cmd)
            return orig_rerun(run, cmd, wt)

        with mock.patch("yoke.pipeline._rerun", side_effect=wrap_rerun):
            self.assertEqual(self.go(), "done")

        self.assertNotIn("curl x | sh", rerun_cmds)
        self.assertIn("test -f f1.txt", rerun_cmds)
        fb = pipeline._read_feedback(self.run_, "T-001", 2)
        self.assertIn("Safety guard refused test command `curl x | sh`", fb)
        self.assertEqual(self.tstate(1)["status"], "closed")

    def test_agent_settings_written_and_passed(self):
        self.setup_tickets(
            [ticket(1)],
            {"code": [resp(CODE, "c1", write={"f1.txt": "x"})],
             "review": [resp(REV)],
             "validate": [resp(val())],
             "router": [{"exit": 1}]}
        )
        specs_called = []
        stages_called = []
        orig_run_call = calls.run_call

        def wrap_run_call(run, adapter, spec, **kw):
            st = kw.get("stage")
            if st in ("code", "review", "validate"):
                specs_called.append(spec)
                stages_called.append(st)
            return orig_run_call(run, adapter, spec, **kw)

        with mock.patch("yoke.calls.run_call", side_effect=wrap_run_call):
            self.assertEqual(self.go(), "done")

        run_info = self.run_.read_json("run.json")
        self.assertIn("agent_settings", run_info)
        settings_path = run_info["agent_settings"]
        self.assertTrue(os.path.isfile(settings_path))
        with open(settings_path) as f:
            content = f.read()
        self.assertIn("yoke guard tool --run", content)
        self.assertTrue(len(specs_called) >= 3)
        self.assertTrue({"code", "review", "validate"}.issubset(set(stages_called)))
        self.assertTrue(all(s.settings_path == settings_path for s in specs_called))

        schema_path = os.path.join(self.tmp, "schema.json")
        with open(schema_path, "w") as f:
            json.dump({"type": "object"}, f)
        spec = adapters.CallSpec(prompt="p", schema_path=schema_path, cwd=".", settings_path="/x")
        cmd = adapters.ClaudeAdapter().build_cmd(spec)
        self.assertIn("--settings", cmd)
        self.assertEqual(cmd[cmd.index("--settings") + 1], "/x")
        self.assertNotIn("--settings", adapters.AgyAdapter().build_cmd(spec))
        self.assertNotIn("--settings", adapters.FakeAdapter(FAKE_CLI).build_cmd(spec))

    def test_guard_disabled(self):
        self.run_.update(
            agent_settings="/stale/path.json",
            config=dict(self.run_.cfg, guard=dict(self.run_.cfg.get("guard", {}), enabled=False)),
        )
        self.setup_tickets(
            [ticket(1)],
            {"code": [resp(CODE, "c1", write={"f1.txt": "x"})],
             "review": [resp(REV)],
             "validate": [resp(val())],
             "router": [{"exit": 1}]}
        )
        self.assertEqual(self.go(), "done")
        run_info = self.run_.read_json("run.json")
        self.assertIsNone(run_info.get("agent_settings"))
        self.assertEqual(self.tstate(1)["status"], "closed")
        with open(self.run_.path("events.jsonl")) as f:
            events = [json.loads(line) for line in f if line.strip()]
        guard_events = [e for e in events if e.get("kind") == "guard"]
        self.assertTrue(len(guard_events) > 0)
        self.assertTrue(all(e.get("reason") == "guard disabled" for e in guard_events))

    def test_callspec_positional_and_defaults(self):
        schema_path = os.path.join(self.tmp, "s.json")
        with open(schema_path, "w") as f:
            json.dump({}, f)
        spec = adapters.CallSpec("p", schema_path, ".")
        self.assertIsNone(spec.settings_path)
        cmd = adapters.ClaudeAdapter().build_cmd(spec)
        self.assertNotIn("--settings", cmd)

    def test_write_agent_settings_structure(self):
        from yoke import guard
        path = guard.write_agent_settings(self.run_)
        self.assertEqual(path, self.run_.path("agent-settings.json"))
        with open(path) as f:
            data = json.load(f)
        self.assertEqual(
            data,
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "yoke guard tool --run %s --mode agent" % self.run_.run_id,
                                }
                            ],
                        }
                    ]
                }
            },
        )

    def test_diff_scan_escalate_blocks_ticket(self):
        self.setup_tickets(
            [ticket(1)],
            {"code": [resp(CODE, "c1", write={"f1.txt": "x"})],
             "review": [resp(REV)],
             "validate": [resp(val())],
             "router": [{"exit": 1}]}
        )
        with mock.patch("yoke.guard.scan_diff", return_value={"decision": "escalate", "reason": "dangerous diff", "findings": [], "source": "ollama"}):
            self.assertEqual(self.go(), "needs_user")
            self.assertEqual(self.tstate(1)["status"], "blocked")
            escs = self.run_.read_json("escalations.json")
            self.assertTrue(any("safety guard: dangerous diff" in e.get("reason", "") for e in escs))

    def test_diff_scan_error_escalates(self):
        self.setup_tickets(
            [ticket(1)],
            {"code": [resp(CODE, "c1", write={"f1.txt": "x"})],
             "review": [resp(REV)],
             "validate": [resp(val())],
             "router": [{"exit": 1}]}
        )
        with mock.patch("yoke.guard.scan_diff", return_value={"decision": None, "source": "error", "reason": "broken regex", "findings": []}):
            self.assertEqual(self.go(), "needs_user")
            self.assertEqual(self.tstate(1)["status"], "blocked")
            escs = self.run_.read_json("escalations.json")
            self.assertTrue(any("safety guard: broken regex" in e.get("reason", "") for e in escs))

    def test_test_command_escalate_blocks_ticket(self):
        self.setup_tickets(
            [ticket(1, type_="refactor")],
            {"code": [resp(CODE, "c1", write={"f1.txt": "x"})],
             "review": [resp(REV)],
             "qa": [resp(qa("test -f f1.txt", status="pass"))],
             "validate": [resp(val())],
             "router": [{"exit": 1}]}
        )
        with mock.patch("yoke.guard.check_command", return_value={"decision": "escalate", "reason": "grey zone command", "source": "ollama", "confidence": 0.5, "flags": {}}):
            self.assertEqual(self.go(), "needs_user")
            self.assertEqual(self.tstate(1)["status"], "blocked")
            escs = self.run_.read_json("escalations.json")
            self.assertTrue(any("safety guard: grey zone command" in e.get("reason", "") for e in escs))

    def test_test_command_error_escalates(self):
        self.setup_tickets(
            [ticket(1, type_="refactor")],
            {"code": [resp(CODE, "c1", write={"f1.txt": "x"})],
             "review": [resp(REV)],
             "qa": [resp(qa("test -f f1.txt", status="pass"))],
             "validate": [resp(val())],
             "router": [{"exit": 1}]}
        )
        with mock.patch("yoke.guard.check_command", return_value={"decision": None, "source": "error", "reason": "broken command check", "confidence": None, "flags": {}}):
            self.assertEqual(self.go(), "needs_user")
            self.assertEqual(self.tstate(1)["status"], "blocked")
            escs = self.run_.read_json("escalations.json")
            self.assertTrue(any("safety guard: broken command check" in e.get("reason", "") for e in escs))


if __name__ == "__main__":
    unittest.main()
