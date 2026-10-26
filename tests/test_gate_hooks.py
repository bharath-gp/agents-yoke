import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from tests.test_pipeline import PipeBase, ticket, resp, CODE, REV, val, qa
from yoke import config, hooks

os.environ.setdefault("YOKE_OLLAMA_URL", "http://127.0.0.1:9")


class TestHookRunner(unittest.TestCase):
    def test_exit_0_pass(self):
        with tempfile.TemporaryDirectory() as td:
            res = hooks.run_hook(
                [sys.executable, "-c", "import sys; print('all good'); sys.exit(0)"],
                cwd=td,
                ticket="T-0065",
                stage="code",
                attempt=1,
            )
            self.assertEqual(res.status, "pass")
            self.assertEqual(res.exit_code, 0)
            self.assertIn("all good", res.tail)
            self.assertFalse(res.timed_out)

    def test_exit_2_fail_with_tail(self):
        with tempfile.TemporaryDirectory() as td:
            res = hooks.run_hook(
                [sys.executable, "-c", "import sys; sys.stdout.write('gate check failed\\n'); sys.exit(2)"],
                cwd=td,
                ticket="T-0065",
                stage="qa",
                attempt=1,
            )
            self.assertEqual(res.status, "fail")
            self.assertEqual(res.exit_code, 2)
            self.assertIn("gate check failed", res.tail)
            self.assertFalse(res.timed_out)

    def test_other_nonzero_is_gate_hook_error(self):
        with tempfile.TemporaryDirectory() as td:
            for code in (1, 3, 42):
                res = hooks.run_hook(
                    [sys.executable, "-c", f"import sys; sys.stderr.write('err {code}\\n'); sys.exit({code})"],
                    cwd=td,
                    ticket="T-0065",
                    stage="qa",
                    attempt=1,
                )
                self.assertEqual(res.status, "gate_hook_error")
                self.assertEqual(res.exit_code, code)
                self.assertIn(f"err {code}", res.tail)
                self.assertFalse(res.timed_out)

    def test_env_vars_injected(self):
        with tempfile.TemporaryDirectory() as td:
            script = (
                "import os\n"
                "print('TICKET=' + os.environ.get('YOKE_TICKET', ''))\n"
                "print('STAGE=' + os.environ.get('YOKE_STAGE', ''))\n"
                "print('ATTEMPT=' + os.environ.get('YOKE_ATTEMPT', ''))\n"
            )
            res = hooks.run_hook(
                [sys.executable, "-c", script],
                cwd=td,
                ticket="T-9999",
                stage="validate",
                attempt=2,
            )
            self.assertEqual(res.status, "pass")
            self.assertIn("TICKET=T-9999", res.tail)
            self.assertIn("STAGE=validate", res.tail)
            self.assertIn("ATTEMPT=2", res.tail)

    def test_stdout_and_stderr_merged_in_tail(self):
        with tempfile.TemporaryDirectory() as td:
            script = (
                "import sys\n"
                "sys.stdout.write('out line\\n')\n"
                "sys.stdout.flush()\n"
                "sys.stderr.write('err line\\n')\n"
                "sys.stderr.flush()\n"
                "sys.exit(2)\n"
            )
            res = hooks.run_hook(
                [sys.executable, "-c", script],
                cwd=td,
            )
            self.assertEqual(res.status, "fail")
            self.assertIn("out line", res.tail)
            self.assertIn("err line", res.tail)

    def test_tail_limit_truncation(self):
        with tempfile.TemporaryDirectory() as td:
            res = hooks.run_hook(
                [sys.executable, "-c", "import sys; sys.stdout.write('a' * 1500 + 'b' * 1500); sys.exit(2)"],
                cwd=td,
            )
            self.assertEqual(res.status, "fail")
            self.assertEqual(len(res.tail), hooks.TAIL_LIMIT)
            self.assertEqual(res.tail, "b" * hooks.TAIL_LIMIT)

    def test_non_utf8_output_handled(self):
        with tempfile.TemporaryDirectory() as td:
            res = hooks.run_hook(
                [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\xff\\xfe invalid'); sys.exit(0)"],
                cwd=td,
            )
            self.assertEqual(res.status, "pass")
            self.assertIn("invalid", res.tail)

    def test_invalid_cwd_fails_with_gate_hook_error(self):
        nonexistent = os.path.join(tempfile.gettempdir(), "nonexistent_dir_998877")
        res = hooks.run_hook(
            [sys.executable, "-c", "print('hello')"],
            cwd=nonexistent,
        )
        self.assertEqual(res.status, "gate_hook_error")
        self.assertEqual(res.exit_code, 127)
        self.assertTrue(len(res.tail) > 0)

    def test_pure_timeout_sets_gate_hook_error_and_tail(self):
        with tempfile.TemporaryDirectory() as td:
            res = hooks.run_hook(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                cwd=td,
                timeout_s=0.2,
                kill_grace_s=0.1,
            )
            self.assertEqual(res.status, "gate_hook_error")
            self.assertTrue(res.timed_out)
            self.assertIn("timed out after 0.2s", res.tail)

    def test_hook_timeout_kills_whole_process_group(self):
        with tempfile.TemporaryDirectory() as td:
            child_pid_file = os.path.join(td, "child.pid")
            script = (
                "import subprocess, sys, time\n"
                "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                f"with open({repr(child_pid_file)}, 'w') as f:\n"
                "    f.write(str(p.pid))\n"
                "time.sleep(60)\n"
            )
            res = hooks.run_hook(
                [sys.executable, "-c", script],
                cwd=td,
                timeout_s=0.4,
                kill_grace_s=0.1,
            )
            self.assertEqual(res.status, "gate_hook_error")
            self.assertTrue(res.timed_out)
            self.assertTrue(os.path.exists(child_pid_file))
            with open(child_pid_file) as f:
                child_pid = int(f.read().strip())

            alive = True
            for _ in range(40):
                try:
                    os.kill(child_pid, 0)
                    time.sleep(0.05)
                except ProcessLookupError:
                    alive = False
                    break
                except OSError:
                    alive = False
                    break
            self.assertFalse(alive, f"Descendant process {child_pid} was not killed on hook timeout")

    def test_shell_command_as_string(self):
        with tempfile.TemporaryDirectory() as td:
            res = hooks.run_hook(
                "echo hook_string_cmd",
                cwd=td,
            )
            self.assertEqual(res.status, "pass")
            self.assertEqual(res.exit_code, 0)
            self.assertIn("hook_string_cmd", res.tail)

    def test_run_hooks_sequence(self):
        with tempfile.TemporaryDirectory() as td:
            res = hooks.run_hooks([], cwd=td)
            self.assertEqual(res.status, "pass")

            res = hooks.run_hooks(
                [
                    [sys.executable, "-c", "import sys; print('step1'); sys.exit(0)"],
                    [sys.executable, "-c", "import sys; print('step2'); sys.exit(0)"],
                ],
                cwd=td,
            )
            self.assertEqual(res.status, "pass")
            self.assertIn("step2", res.tail)

            fail_flag = os.path.join(td, "ran_after_fail.txt")
            res = hooks.run_hooks(
                [
                    [sys.executable, "-c", "import sys; print('fail step'); sys.exit(2)"],
                    [sys.executable, "-c", f"import sys; open({repr(fail_flag)}, 'w').write('ran'); sys.exit(0)"],
                ],
                cwd=td,
            )
            self.assertEqual(res.status, "fail")
            self.assertEqual(res.exit_code, 2)
            self.assertIn("fail step", res.tail)
            self.assertFalse(os.path.exists(fail_flag))

            res = hooks.run_hooks(
                [
                    [sys.executable, "-c", "import sys; print('error step'); sys.exit(1)"],
                    [sys.executable, "-c", f"import sys; open({repr(fail_flag)}, 'w').write('ran'); sys.exit(0)"],
                ],
                cwd=td,
            )
            self.assertEqual(res.status, "gate_hook_error")
            self.assertEqual(res.exit_code, 1)
            self.assertIn("error step", res.tail)
            self.assertFalse(os.path.exists(fail_flag))


class TestGateHooksConfig(unittest.TestCase):
    def test_defaults_include_gate_hooks(self):
        cfg = config.load()
        self.assertIn("gate_hooks", cfg)
        self.assertEqual(
            cfg["gate_hooks"],
            {
                "after_stage": {
                    "code": [],
                    "qa": [],
                },
                "before_close": [],
            },
        )

    def test_user_config_hooks_load(self):
        with tempfile.TemporaryDirectory() as home_dir:
            user_config = {
                "gate_hooks": {
                    "after_stage": {
                        "code": ["./run_lint.sh"],
                    },
                    "before_close": ["./run_audit.sh"],
                }
            }
            with open(os.path.join(home_dir, "config.json"), "w") as f:
                json.dump(user_config, f)

            cfg = config.load(home=home_dir)
            self.assertEqual(
                cfg["gate_hooks"]["after_stage"]["code"],
                ["./run_lint.sh"],
            )
            self.assertEqual(
                cfg["gate_hooks"]["before_close"],
                ["./run_audit.sh"],
            )
            self.assertEqual(cfg["gate_hooks"]["after_stage"]["qa"], [])

    def test_repo_gate_hooks_refused_and_logged(self):
        with tempfile.TemporaryDirectory() as home_dir, tempfile.TemporaryDirectory() as repo_dir:
            user_config = {
                "gate_hooks": {
                    "after_stage": {
                        "code": ["echo user_code_hook"],
                        "qa": [],
                    },
                    "before_close": ["echo user_close_hook"],
                }
            }
            with open(os.path.join(home_dir, "config.json"), "w") as f:
                json.dump(user_config, f)

            repo_config = {
                "parallel": 8,
                "gate_hooks": {
                    "after_stage": {
                        "code": ["echo repo_malicious_hook"],
                    },
                    "before_close": ["echo repo_malicious_close"],
                },
            }
            with open(os.path.join(repo_dir, ".yoke.json"), "w") as f:
                json.dump(repo_config, f)

            with self.assertLogs(config.logger, level="WARNING") as log_cm:
                cfg = config.load(repo=repo_dir, home=home_dir)

            self.assertTrue(
                any("refusing gate_hooks from repo config" in msg for msg in log_cm.output),
                f"Expected warning log not found in {log_cm.output}",
            )
            self.assertEqual(
                cfg["gate_hooks"]["after_stage"]["code"],
                ["echo user_code_hook"],
            )
            self.assertEqual(
                cfg["gate_hooks"]["before_close"],
                ["echo user_close_hook"],
            )
            self.assertEqual(cfg["parallel"], 8)

    def test_repo_without_gate_hooks_does_not_warn(self):
        with tempfile.TemporaryDirectory() as home_dir, tempfile.TemporaryDirectory() as repo_dir:
            repo_config = {"parallel": 4}
            with open(os.path.join(repo_dir, ".yoke.json"), "w") as f:
                json.dump(repo_config, f)

            with mock.patch.object(config.logger, "warning") as mock_warn:
                cfg = config.load(repo=repo_dir, home=home_dir)
                mock_warn.assert_not_called()

            self.assertEqual(cfg["parallel"], 4)


class TestPipelineGateHooks(PipeBase):
    def test_no_hooks_existing_close_gate_unchanged(self):
        self.setup_tickets(
            [ticket(1)],
            {"code": [resp(CODE, "c1", write={"f1.txt": "x"})],
             "review": [resp(REV)],
             "validate": [resp(val())],
             "router": [{"exit": 1}]})
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["status"], "closed")
        with open(self.run_.path("events.jsonl")) as f:
            gates = [json.loads(l) for l in f if '"close_gate"' in l]
        self.assertEqual(len(gates), 1)
        self.assertTrue(gates[0]["ok"])
        self.assertNotIn("gate_hooks", gates[0]["gate"])

    def test_failing_before_close_hook_prevents_commit_and_feeds_coder(self):
        flag_file = os.path.join(self.tmp, "fixed.txt")
        check_script = (
            "import os, sys\n"
            f"if not os.path.exists({repr(flag_file)}):\n"
            "    sys.stdout.write('gate hook failed: missing fixed.txt\\n')\n"
            "    sys.exit(2)\n"
            "sys.stdout.write('gate hook passed\\n')\n"
        )
        self.run_.update(config=dict(self.run_.cfg, gate_hooks={
            "after_stage": {"code": [], "qa": []},
            "before_close": [[sys.executable, "-c", check_script]],
        }))
        self.setup_tickets(
            [ticket(1)],
            {
                "code": [
                    resp(CODE, "c1", write={"f1.txt": "1"}),
                    resp(CODE, "c1", write={"f1.txt": "2", flag_file: "ok"}),
                ],
                "review": [resp(REV), resp(REV)],
                "validate": [resp(val()), resp(val())],
                "router": [{"exit": 1}],
            },
        )
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["attempt"], 2)
        self.assertEqual(self.tstate(1)["status"], "closed")

        with open(self.run_.path("tickets/T-001/attempt-2/feedback.md")) as f:
            fb = f.read()
        self.assertIn("missing fixed.txt", fb)

        audit_log = self.run_.path("tickets/T-001/attempt-1/hooks/before_close.log")
        self.assertTrue(os.path.exists(audit_log))
        with open(audit_log) as f:
            self.assertIn("missing fixed.txt", f.read())

        with open(self.run_.path("events.jsonl")) as f:
            events = [json.loads(l) for l in f if '"close_gate"' in l]
        self.assertEqual(len(events), 2)
        self.assertFalse(events[0]["ok"])
        self.assertIn("gate_hooks", events[0]["gate"])
        self.assertIn("missing fixed.txt", events[0]["gate"]["gate_hooks"]["before_close"]["tail"])

        self.assertTrue(events[1]["ok"])
        self.assertIn("gate hook passed", events[1]["gate"]["gate_hooks"]["before_close"]["tail"])

    def test_after_stage_code_hook_failure_feeds_coder(self):
        hook_cmd = [sys.executable, "-c", "import sys; sys.stdout.write('lint error\\n'); sys.exit(2)"]
        self.run_.update(config=dict(self.run_.cfg, gate_hooks={
            "after_stage": {"code": [hook_cmd], "qa": []},
            "before_close": [],
        }))
        self.setup_tickets(
            [ticket(1)],
            {
                "code": [
                    resp(CODE, "c1", write={"f1.txt": "1"}),
                    resp(CODE, "c1", write={"f1.txt": "2"}),
                ],
                "review": [resp(REV)],
                "validate": [resp(val())],
                "router": [{"exit": 1}],
            },
        )
        self.assertEqual(self.go(), "needs_user")
        self.assertEqual(self.tstate(1)["status"], "blocked")
        with open(self.run_.path("tickets/T-001/attempt-2/feedback.md")) as f:
            fb = f.read()
        self.assertIn("lint error", fb)

        audit_log = self.run_.path("tickets/T-001/attempt-1/hooks/code.log")
        self.assertTrue(os.path.exists(audit_log))
        with open(audit_log) as f:
            self.assertIn("lint error", f.read())

    def test_after_stage_qa_hook_failure(self):
        hook_cmd = [sys.executable, "-c", "import sys; sys.stdout.write('qa hook error\\n'); sys.exit(2)"]
        self.run_.update(config=dict(self.run_.cfg, gate_hooks={
            "after_stage": {"code": [], "qa": [hook_cmd]},
            "before_close": [],
        }))
        self.setup_tickets(
            [ticket(1, "feature")],
            {
                "code": [resp(CODE, "c1", write={"f1.txt": "1"})] * 2,
                "review": [resp(REV)] * 2,
                "qa": [resp(qa("true")), resp(qa("true"))],
                "validate": [resp(val())],
                "docs": [resp({"status": "done", "files_changed": [], "summary": "s"})],
                "router": [{"exit": 1}],
            },
        )
        self.assertEqual(self.go(), "needs_user")
        self.assertEqual(self.tstate(1)["status"], "blocked")
        with open(self.run_.path("tickets/T-001/attempt-2/feedback.md")) as f:
            fb = f.read()
        self.assertIn("qa hook error", fb)

        audit_log = self.run_.path("tickets/T-001/attempt-1/hooks/qa.log")
        self.assertTrue(os.path.exists(audit_log))
        with open(audit_log) as f:
            self.assertIn("qa hook error", f.read())

    def test_hook_timeout_uses_gate_hook_config(self):
        self.run_.update(config=dict(
            self.run_.cfg,
            timeouts_s=dict(self.run_.cfg.get("timeouts_s", {}), gate_hook=42, code=2400, qa=1800),
            gate_hooks={
                "after_stage": {"code": ["echo code_ok"], "qa": ["echo qa_ok"]},
                "before_close": ["echo close_ok"],
            },
        ))
        self.setup_tickets(
            [ticket(1, "feature")],
            {
                "code": [resp(CODE, "c1", write={"f1.txt": "1"})],
                "review": [resp(REV)],
                "qa": [resp(qa("true"))],
                "validate": [resp(val())],
                "docs": [resp({"status": "done", "files_changed": [], "summary": "s"})],
                "router": [{"exit": 1}],
            },
        )
        real_run_hook = hooks.run_hook
        timeouts_passed = []
        def spy_run_hook(*args, **kw):
            if kw.get("stage") is not None:
                timeouts_passed.append((kw.get("stage"), kw.get("timeout_s")))
            return real_run_hook(*args, **kw)

        with mock.patch("yoke.pipeline.hooks.run_hook", side_effect=spy_run_hook):
            self.assertEqual(self.go(), "done")

        self.assertEqual(len(timeouts_passed), 3)
        for stage, timeout in timeouts_passed:
            self.assertEqual(timeout, 42, f"Stage {stage} used timeout {timeout} instead of gate_hook timeout 42")

    def test_passing_gate_hooks_recorded_in_close_gate_and_audit(self):
        self.run_.update(config=dict(
            self.run_.cfg,
            gate_hooks={
                "after_stage": {
                    "code": ["echo code_ok"],
                    "qa": ["echo qa_ok"],
                },
                "before_close": ["echo close_ok"],
            },
        ))
        self.setup_tickets(
            [ticket(1, "feature")],
            {
                "code": [resp(CODE, "c1", write={"f1.txt": "1"})],
                "review": [resp(REV)],
                "qa": [resp(qa("true"))],
                "validate": [resp(val())],
                "docs": [resp({"status": "done", "files_changed": [], "summary": "s"})],
                "router": [{"exit": 1}],
            },
        )
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["status"], "closed")

        for stage_name, expected_text in [("code", "code_ok"), ("qa", "qa_ok"), ("before_close", "close_ok")]:
            audit_log = self.run_.path(f"tickets/T-001/attempt-1/hooks/{stage_name}.log")
            self.assertTrue(os.path.exists(audit_log), f"Missing {stage_name}.log")
            with open(audit_log) as f:
                self.assertIn(expected_text, f.read())

            meta_file = self.run_.path(f"tickets/T-001/attempt-1/hooks/{stage_name}.meta.json")
            self.assertTrue(os.path.exists(meta_file), f"Missing {stage_name}.meta.json")
            meta = self.run_.read_json(f"tickets/T-001/attempt-1/hooks/{stage_name}.meta.json")
            self.assertTrue(meta["ok"])
            self.assertEqual(meta["status"], "pass")
            self.assertEqual(meta["exit_code"], 0)

        with open(self.run_.path("events.jsonl")) as f:
            gates = [json.loads(l) for l in f if '"close_gate"' in l]
        self.assertEqual(len(gates), 1)
        self.assertTrue(gates[0]["ok"])
        hook_records = gates[0]["gate"]["gate_hooks"]
        self.assertIn("code", hook_records)
        self.assertIn("qa", hook_records)
        self.assertIn("before_close", hook_records)
        for stage in ("code", "qa", "before_close"):
            self.assertTrue(hook_records[stage]["ok"])
            self.assertEqual(hook_records[stage]["status"], "pass")
            self.assertEqual(hook_records[stage]["exit_code"], 0)

    def test_gate_hooks_reset_between_attempts(self):
        attempt_file = os.path.join(self.tmp, "attempt_flag.txt")
        qa_hook_script = (
            "import os, sys\n"
            f"if not os.path.exists({repr(attempt_file)}):\n"
            f"    open({repr(attempt_file)}, 'w').write('1')\n"
            "    sys.stdout.write('qa hook attempt 1 failed\\n')\n"
            "    sys.exit(2)\n"
            "sys.stdout.write('qa hook attempt 2 passed\\n')\n"
        )
        self.run_.update(config=dict(
            self.run_.cfg,
            gate_hooks={
                "after_stage": {
                    "code": ["echo code_ok"],
                    "qa": [[sys.executable, "-c", qa_hook_script]],
                },
                "before_close": ["echo close_ok"],
            },
        ))
        self.setup_tickets(
            [ticket(1, "feature")],
            {
                "code": [
                    resp(CODE, "c1", write={"f1.txt": "1"}),
                    resp(CODE, "c1", write={"f1.txt": "2"}),
                ],
                "review": [resp(REV), resp(REV)],
                "qa": [resp(qa("true")), resp(qa("true"))],
                "validate": [resp(val())],
                "docs": [resp({"status": "done", "files_changed": [], "summary": "s"})],
                "router": [{"exit": 1}],
            },
        )
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["attempt"], 2)
        self.assertEqual(self.tstate(1)["status"], "closed")

        with open(self.run_.path("events.jsonl")) as f:
            gates = [json.loads(l) for l in f if '"close_gate"' in l]
        self.assertEqual(len(gates), 1)
        self.assertEqual(gates[0]["attempt"], 2)
        self.assertTrue(gates[0]["ok"])
        hook_records = gates[0]["gate"]["gate_hooks"]
        self.assertIn("qa hook attempt 2 passed", hook_records["qa"]["tail"])

    def test_multiple_hooks_in_stage_appended_to_audit_log(self):
        self.run_.update(config=dict(
            self.run_.cfg,
            gate_hooks={
                "after_stage": {"code": [], "qa": []},
                "before_close": ["echo hook_one", "echo hook_two"],
            },
        ))
        self.setup_tickets(
            [ticket(1)],
            {
                "code": [resp(CODE, "c1", write={"f1.txt": "1"})],
                "review": [resp(REV)],
                "validate": [resp(val())],
                "router": [{"exit": 1}],
            },
        )
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["status"], "closed")

        audit_log = self.run_.path("tickets/T-001/attempt-1/hooks/before_close.log")
        self.assertTrue(os.path.exists(audit_log))
        with open(audit_log) as f:
            content = f.read()
        self.assertIn("hook_one", content)
        self.assertIn("hook_two", content)

        meta = self.run_.read_json("tickets/T-001/attempt-1/hooks/before_close.meta.json")
        self.assertEqual(meta["cmd"], "echo hook_two")
        self.assertTrue(meta["ok"])


if __name__ == "__main__":
    unittest.main()
