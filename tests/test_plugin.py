import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from yoke import cli, config, state

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS = ("setup", "grill", "plan", "tickets", "run", "resume", "status", "finish", "go", "models", "dashboard")


def load(rel):
    with open(os.path.join(ROOT, rel)) as f:
        return json.load(f)


class TestPackaging(unittest.TestCase):
    def test_manifests(self):
        p = load(".claude-plugin/plugin.json")
        self.assertEqual(p["name"], "yoke")
        self.assertEqual(p["version"], "0.7.2")
        with open(os.path.join(ROOT, "pyproject.toml")) as f:
            pyproject_ver = re.search(r'version\s*=\s*"([^"]+)"', f.read()).group(1)
        self.assertEqual(pyproject_ver, "0.7.2")
        self.assertEqual(p["version"], pyproject_ver)
        m = load(".claude-plugin/marketplace.json")
        self.assertEqual(m["name"], "yoke")
        self.assertEqual(m["plugins"][0]["name"], "yoke")
        self.assertEqual(m["plugins"][0]["source"], "./")

    def test_hook(self):
        h = load("hooks/hooks.json")["hooks"]["SessionStart"][0]
        self.assertEqual(h["matcher"], "startup|resume|compact")
        self.assertIn("yoke status --brief --active", h["hooks"][0]["command"])

    def test_skills(self):
        for name in SKILLS:
            with open(os.path.join(ROOT, "skills", name, "SKILL.md")) as f:
                text = f.read()
            fm = re.match(r"---\nname: (\S+)\ndescription: (.+)\n---\n", text)
            self.assertTrue(fm, name)
            self.assertEqual(fm.group(1), name)
            self.assertIn("yoke ", text)
            self.assertNotIn("dangerously-skip-permissions", text)

    def test_skill_commands_exist(self):
        """Every `yoke <sub>` named in a skill is a real subcommand."""
        subs = set()
        for name in SKILLS:
            with open(os.path.join(ROOT, "skills", name, "SKILL.md")) as f:
                subs.update(re.findall(r"`yoke ([a-z-]+)", f.read()))
        known = {"setup", "init", "review-plan", "approve", "tickets", "run", "wait", "status", "escalations",
                 "unblock", "stop", "pr", "route", "doctor", "models", "runs", "map", "dashboard"}
        self.assertFalse(subs - known, subs - known)


    def test_skills_gate_on_setup(self):
        for name in SKILLS:
            if name == "setup":
                continue
            with open(os.path.join(ROOT, "skills", name, "SKILL.md")) as f:
                self.assertIn("`yoke setup --check`", f.read(), name)

    def test_go_skill(self):
        with open(os.path.join(ROOT, "skills", "go", "SKILL.md")) as f:
            text = f.read()
        self.assertIn("`yoke setup --check`", text)
        self.assertIn("auto-approved tickets", text)
        self.assertIn("GitHub issues", text)
        run_count = sum(1 for line in text.splitlines() if "--run" in line)
        self.assertGreaterEqual(run_count, 8)

    def test_resume_skill(self):
        with open(os.path.join(ROOT, "skills", "resume", "SKILL.md")) as f:
            text = f.read()
        self.assertIn("yoke runs --active", text)
        for target in ("plan", "tickets", "resume", "finish"):
            self.assertTrue(re.search(rf"- `{target}`:.*--run R", text), f"Missing branch for {target}")

    def test_skills_run_flag(self):
        # 'dashboard' is exempt because it is not run-scoped.
        self.assertNotIn("dashboard", ("plan", "tickets", "run", "finish", "status"))
        for name in ("plan", "tickets", "run", "finish", "status"):
            with open(os.path.join(ROOT, "skills", name, "SKILL.md")) as f:
                self.assertIn("--run", f.read(), name)

    def test_dashboard_skill(self):
        path = os.path.join(ROOT, "skills", "dashboard", "SKILL.md")
        self.assertTrue(os.path.exists(path))
        with open(path) as f:
            content = f.read()
        self.assertIn("`yoke setup --check`", content)
        self.assertIn("`yoke dashboard", content)
        self.assertIn("/api/runs", content)

    def test_grill_mentions_go(self):
        with open(os.path.join(ROOT, "skills", "grill", "SKILL.md")) as f:
            self.assertIn("/yoke:go", f.read())

    def test_models_skill(self):
        path = os.path.join(ROOT, "skills", "models", "SKILL.md")
        self.assertTrue(os.path.exists(path))
        with open(path) as f:
            content = f.read()
        self.assertIn("`yoke setup --check`", content)
        self.assertIn("`yoke models --list`", content)
        self.assertIn("`yoke models --show`", content)
        self.assertIn("`yoke models --set", content)
        self.assertIn("`yoke models --reset", content)

    def test_model_pick_step_in_go_and_grill(self):
        for name in ("go", "grill"):
            with open(os.path.join(ROOT, "skills", name, "SKILL.md")) as f:
                content = f.read()
            self.assertIn("`yoke models --list`", content, name)
            self.assertIn("`yoke models --set", content, name)
            list_pos = content.index("`yoke models --list`")
            init_pos = content.index("`yoke init")
            self.assertLess(list_pos, init_pos, f"model pick should precede init in {name}")

    def test_setup_skill_points_to_models(self):
        with open(os.path.join(ROOT, "skills", "setup", "SKILL.md")) as f:
            content = f.read()
        self.assertIn("/yoke:models", content)


class TestInstaller(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_dry_run_scope(self):
        out = subprocess.run(
            ["node", os.path.join(ROOT, "bin", "install.js"), "--dry-run", "--only", "claude,agy", "--scope", "project"],
            capture_output=True, text=True, check=True).stdout
        self.assertIn("claude plugin marketplace add bharath-gp/agents-yoke --scope project", out)
        self.assertIn("claude plugin install yoke@yoke --scope project", out)
        self.assertIn("agy plugin install", out)


class TestSetup(unittest.TestCase):
    def test_check_until_setup_done(self):
        home = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"YOKE_HOME": home}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(cli.main(["setup", "--check"]), 1)
            self.assertIn("/yoke:setup", buf.getvalue())
            with mock.patch.object(cli, "doctor", return_value=[]), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["setup"]), 0)
            self.assertEqual(cli.main(["setup", "--check"]), 0)

    def test_project_scope_is_per_repo(self):
        home = tempfile.mkdtemp()
        repo1, repo2 = (os.path.realpath(tempfile.mkdtemp()) for _ in range(2))
        quiet = contextlib.redirect_stdout(io.StringIO())
        with mock.patch.dict(os.environ, {"YOKE_HOME": home}), \
                mock.patch.object(cli, "doctor", return_value=[]), quiet:
            cwd = os.getcwd()
            try:
                os.chdir(repo1)
                self.assertEqual(cli.main(["setup", "--scope", "project"]), 0)
                self.assertEqual(cli.main(["setup", "--check"]), 0)
                os.chdir(repo2)
                self.assertEqual(cli.main(["setup", "--check"]), 1)
            finally:
                os.chdir(cwd)

    def test_setup_fails_on_problems(self):
        home = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"YOKE_HOME": home}), \
                mock.patch.object(cli, "doctor", return_value=["agy"]), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["setup"]), 1)
            self.assertFalse(os.path.exists(cli.setup_markers()[0]))


class TestBriefStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = os.path.realpath(os.path.join(self.tmp, "repo"))
        os.makedirs(self.repo)
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        os.environ["YOKE_HOME"] = self.tmp
        self.addCleanup(os.environ.pop, "YOKE_HOME", None)
        self.cwd = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, self.cwd)

    def status(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(["status", "--brief", "--active"])
        return rc, buf.getvalue()

    def test_silent_without_runs(self):
        self.assertEqual(self.status(), (0, ""))

    def test_silent_when_done_and_finalized(self):
        run = state.Run.create(self.repo, config.load(home=self.tmp), home=self.tmp)
        run.update(state="done")
        run.write_json("approvals.json", {"final": {"sha256": "x", "at": "t"}})
        self.assertEqual(self.status(), (0, ""))

    def test_active_run_summary(self):
        run = state.Run.create(self.repo, config.load(home=self.tmp), home=self.tmp)
        run.update(state="needs_user")
        rc, out = self.status()
        self.assertEqual(rc, 0)
        self.assertIn(run.run_id, out)
        self.assertIn("needs_user", out)
        self.assertLessEqual(len(out.splitlines()), 15)

    def test_done_not_finalized_is_active(self):
        run = state.Run.create(self.repo, config.load(home=self.tmp), home=self.tmp)
        run.update(state="done")
        self.assertIn("finish", self.status()[1])


if __name__ == "__main__":
    unittest.main()
