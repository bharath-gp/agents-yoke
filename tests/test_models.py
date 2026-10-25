import contextlib
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from yoke import cli, config, models

os.environ.setdefault("YOKE_OLLAMA_URL", "http://127.0.0.1:9")


class TestListModels(unittest.TestCase):
    def test_claude_static_models(self):
        result, warn = models.list_models("claude")
        self.assertIsNone(warn)
        ids = [m["id"] for m in result]
        self.assertEqual(ids, ["opus", "sonnet", "haiku"])
        self.assertNotIn("fable", ids)

    def test_agy_stub_on_path(self):
        with tempfile.TemporaryDirectory() as td:
            stub = os.path.join(td, "agy")
            with open(stub, "w") as f:
                f.write("#!/bin/sh\n")
                f.write('echo "model-1\tModel One"\n')
                f.write('echo "model-2\tModel Two"\n')
            os.chmod(stub, os.stat(stub).st_mode | stat.S_IEXEC)

            with mock.patch.dict(os.environ, {"PATH": td + ":" + os.environ.get("PATH", "")}):
                result, warn = models.list_models("agy")
                self.assertIsNone(warn)
                ids = [m["id"] for m in result]
                self.assertEqual(ids, ["model-1", "model-2"])
                labels = [m.get("label", "") for m in result]
                self.assertEqual(labels, ["Model One", "Model Two"])

    def test_agy_missing_or_failing_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            stub = os.path.join(td, "agy")
            with open(stub, "w") as f:
                f.write("#!/bin/sh\nexit 1\n")
            os.chmod(stub, os.stat(stub).st_mode | stat.S_IEXEC)

            with mock.patch.dict(os.environ, {"PATH": td}):
                result, warn = models.list_models("agy")
                self.assertIsNotNone(warn)
                self.assertIn("warning", warn.lower())
                ids = [m["id"] for m in result]
                self.assertIn("gemini-3.8-flash-high", ids)
                self.assertIn("claude-opus-5-5-high", ids)

        # agy totally absent
        with mock.patch.dict(os.environ, {"PATH": ""}):
            result, warn = models.list_models("agy")
            self.assertIsNotNone(warn)
            ids = [m["id"] for m in result]
            self.assertIn("gemini-3.8-flash-high", ids)

    def test_unknown_agent_never_raises(self):
        result, warn = models.list_models("unknown-agent")
        self.assertEqual(result, [])
        self.assertIsNotNone(warn)


class TestModelsCLI(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = self._td.name
        self.home = os.path.join(self.tmp, "home")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.home, exist_ok=True)
        os.makedirs(self.repo, exist_ok=True)
        subprocess.run(["git", "init", self.repo], capture_output=True, check=True)
        self.orig_cwd = os.getcwd()
        os.chdir(self.repo)

    def tearDown(self):
        os.chdir(self.orig_cwd)
        self._td.cleanup()

    def run_cli(self, argv, env_overrides=None):
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        env = {"YOKE_HOME": self.home}
        if env_overrides:
            env.update(env_overrides)
        with mock.patch.dict(os.environ, env):
            with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                rc = cli.main(argv)
        return rc, out_buf.getvalue(), err_buf.getvalue()

    def test_list_with_stub_agy(self):
        stub_dir = os.path.join(self.tmp, "bin")
        os.makedirs(stub_dir, exist_ok=True)
        stub = os.path.join(stub_dir, "agy")
        with open(stub, "w") as f:
            f.write("#!/bin/sh\n")
            f.write('echo "stub-model-1\tStub Label 1"\n')
        os.chmod(stub, os.stat(stub).st_mode | stat.S_IEXEC)

        rc, out, err = self.run_cli(["models", "--list"], {"PATH": stub_dir + ":" + os.environ.get("PATH", "")})
        self.assertEqual(rc, 0)
        self.assertIn("claude:", out)
        self.assertIn("opus", out)
        self.assertIn("agy:", out)
        self.assertIn("stub-model-1", out)
        self.assertIn("Stub Label 1", out)

    def test_list_failing_agy_prints_warning_and_static_list_exit_0(self):
        rc, out, err = self.run_cli(["models", "--list"], {"PATH": ""})
        self.assertEqual(rc, 0)
        self.assertIn("warning", err.lower())
        self.assertIn("claude:", out)
        self.assertIn("opus", out)
        self.assertIn("agy:", out)
        self.assertIn("gemini-3.8-flash-high", out)

    def test_list_json_is_valid(self):
        rc, out, err = self.run_cli(["models", "--list", "--json"], {"PATH": ""})
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertIn("claude", data)
        self.assertIn("opus", data["claude"])
        self.assertIn("agy", data)
        self.assertIn("gemini-3.8-flash-high", data["agy"])

    def test_set_scopes(self):
        # repo scope
        rc, out, err = self.run_cli(["models", "--set", "claude.plan=opus", "--scope", "repo"])
        self.assertEqual(rc, 0)
        repo_cfg = os.path.join(self.repo, ".yoke.json")
        self.assertTrue(os.path.exists(repo_cfg))
        with open(repo_cfg) as f:
            data = json.load(f)
        self.assertEqual(data["models"]["claude"]["plan"]["model"], "opus")

        # user scope
        rc, out, err = self.run_cli(["models", "--set", "claude.cheap=haiku", "--scope", "user"])
        self.assertEqual(rc, 0)
        user_cfg = os.path.join(self.home, "config.json")
        self.assertTrue(os.path.exists(user_cfg))
        with open(user_cfg) as f:
            data = json.load(f)
        self.assertEqual(data["models"]["claude"]["cheap"]["model"], "haiku")

        # project scope is alias of repo
        rc, out, err = self.run_cli(["models", "--set", "agy.cheap=gemini-3.8-flash-low", "--scope", "project"])
        self.assertEqual(rc, 0)
        with open(repo_cfg) as f:
            data = json.load(f)
        self.assertEqual(data["models"]["agy"]["cheap"]["model"], "gemini-3.8-flash-low")

    def test_set_validation(self):
        # bad agent
        rc, out, err = self.run_cli(["models", "--set", "badagent.plan=opus"])
        self.assertNotEqual(rc, 0)
        self.assertIn("unknown agent", err)

        # bad tier
        rc, out, err = self.run_cli(["models", "--set", "claude.badtier=opus"])
        self.assertNotEqual(rc, 0)
        self.assertIn("unknown tier", err)

        # absent agy model warns but exits 0
        stub_dir = os.path.join(self.tmp, "bin")
        os.makedirs(stub_dir, exist_ok=True)
        stub = os.path.join(stub_dir, "agy")
        with open(stub, "w") as f:
            f.write("#!/bin/sh\n")
            f.write('echo "known-model\tKnown Model"\n')
        os.chmod(stub, os.stat(stub).st_mode | stat.S_IEXEC)

        rc, out, err = self.run_cli(["models", "--set", "agy.plan=not-in-agy", "--scope", "repo"],
                                    {"PATH": stub_dir + ":" + os.environ.get("PATH", "")})
        self.assertEqual(rc, 0)
        self.assertIn("warning", err.lower())
        self.assertIn("not-in-agy", err)

    def test_show_and_reset(self):
        self.run_cli(["models", "--set", "claude.plan=opus", "--effort", "high", "--scope", "repo"])
        self.run_cli(["models", "--set", "claude.cheap=haiku", "--scope", "repo"])

        rc, out, err = self.run_cli(["models", "--show"])
        self.assertEqual(rc, 0)
        self.assertIn("claude:", out)
        self.assertIn("plan: opus (effort: high)", out)
        self.assertIn("cheap: haiku", out)

        # Reset tier
        rc, out, err = self.run_cli(["models", "--reset", "claude.plan", "--scope", "repo"])
        self.assertEqual(rc, 0)
        rc, out, err = self.run_cli(["models", "--show"])
        self.assertIn("plan: (unset)", out)
        self.assertIn("cheap: haiku", out)

        # Reset agent
        rc, out, err = self.run_cli(["models", "--reset", "claude", "--scope", "repo"])
        self.assertEqual(rc, 0)
        rc, out, err = self.run_cli(["models", "--show"])
        self.assertIn("plan: (unset)", out)
        self.assertIn("cheap: (unset)", out)

    def test_repeatable_set(self):
        rc, out, err = self.run_cli([
            "models",
            "--set", "claude.plan=opus",
            "--set", "agy.cheap=gemini-3.8-flash-low",
            "--scope", "repo",
        ])
        self.assertEqual(rc, 0)
        repo_cfg = os.path.join(self.repo, ".yoke.json")
        with open(repo_cfg) as f:
            data = json.load(f)
        self.assertEqual(data["models"]["claude"]["plan"]["model"], "opus")
        self.assertEqual(data["models"]["agy"]["cheap"]["model"], "gemini-3.8-flash-low")

    def test_show_json(self):
        self.run_cli(["models", "--set", "claude.plan=opus", "--effort", "medium", "--scope", "repo"])
        rc, out, err = self.run_cli(["models", "--show", "--json"])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertIn("claude", data)
        self.assertEqual(data["claude"]["plan"]["model"], "opus")
        self.assertEqual(data["claude"]["plan"]["effort"], "medium")

    def test_reset_all_entries(self):
        self.run_cli(["models", "--set", "claude.plan=opus", "--scope", "repo"])
        self.run_cli(["models", "--set", "agy.cheap=gemini-3.8-flash-low", "--scope", "repo"])
        repo_cfg = os.path.join(self.repo, ".yoke.json")
        with open(repo_cfg) as f:
            data = json.load(f)
        self.assertTrue(data.get("models"))

        rc, out, err = self.run_cli(["models", "--reset", "--scope", "repo"])
        self.assertEqual(rc, 0)
        with open(repo_cfg) as f:
            data = json.load(f)
        self.assertEqual(data.get("models"), {})


class TestConfigReviewMode(unittest.TestCase):
    def test_config_review_mode_defaults_and_clamps(self):
        cfg = config.load()
        self.assertEqual(config.review_mode(cfg), "adversarial")
        self.assertEqual(config.debate_rounds(cfg), 1)
        self.assertEqual(config.review_mode({"review_mode": "independent"}), "independent")
        self.assertEqual(config.review_mode({"review_mode": "garbage"}), "adversarial")
        self.assertEqual(config.review_mode({}), "adversarial")
        self.assertEqual(config.debate_rounds({"debate_rounds": 0}), 1)
        self.assertEqual(config.debate_rounds({"debate_rounds": "x"}), 1)
        self.assertEqual(config.debate_rounds({"debate_rounds": None}), 1)
        self.assertEqual(config.debate_rounds({"debate_rounds": float("inf")}), 1)
        self.assertEqual(config.debate_rounds({"debate_rounds": 3}), 3)


def test_config_review_mode_defaults_and_clamps():
    TestConfigReviewMode().test_config_review_mode_defaults_and_clamps()


if __name__ == "__main__":
    unittest.main()

