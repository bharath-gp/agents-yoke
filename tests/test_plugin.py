import contextlib
import io
import json
import os
import re
import subprocess
import tempfile
import unittest

from yoke import cli, config, state

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS = ("grill", "plan", "tickets", "run", "resume", "status", "finish")


def load(rel):
    with open(os.path.join(ROOT, rel)) as f:
        return json.load(f)


class TestPackaging(unittest.TestCase):
    def test_manifests(self):
        p = load(".claude-plugin/plugin.json")
        self.assertEqual(p["name"], "yoke")
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
        known = {"init", "review-plan", "approve", "tickets", "run", "wait", "status", "escalations",
                 "unblock", "stop", "pr", "route", "doctor"}
        self.assertFalse(subs - known, subs - known)


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
