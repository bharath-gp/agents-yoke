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

from yoke import config, hooks


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


if __name__ == "__main__":
    unittest.main()
