import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from yoke import cli, config, planreview, runcmds, state

os.environ.setdefault("YOKE_OLLAMA_URL", "http://127.0.0.1:9")


class TestRuns(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = os.path.realpath(self._td.name)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo, exist_ok=True)
        subprocess.run(["git", "-C", self.repo, "init", "-q"], check=True)
        self.other_repo = os.path.join(self.tmp, "other_repo")
        os.makedirs(self.other_repo, exist_ok=True)
        subprocess.run(["git", "-C", self.other_repo, "init", "-q"], check=True)

    def _mkrun(self, repo=None, created_at=None, kind=None, run_id=None):
        r = repo or self.repo
        cfg = config.load(home=self.tmp)
        run = state.Run.create(r, cfg, home=self.tmp)
        if run_id:
            # rename if needed, but easier to update run.json
            pass
        info = run.read_json("run.json")
        if created_at is not None:
            info["created_at"] = created_at
        if kind is not None:
            info["kind"] = kind
        run.write_json("run.json", info)
        return run

    def test_list_for_repo_filtering_and_order(self):
        # Create runs with different created_at timestamps and repos
        r1 = self._mkrun(self.repo, created_at="2026-01-01T10:00:00+00:00")
        r3 = self._mkrun(self.repo, created_at="2026-01-03T10:00:00+00:00")
        r2 = self._mkrun(self.repo, created_at="2026-01-02T10:00:00+00:00")
        # research run for same repo should be excluded
        r_research = self._mkrun(self.repo, created_at="2026-01-04T10:00:00+00:00", kind="research")
        # run for other repo should be excluded
        r_other = self._mkrun(self.other_repo, created_at="2026-01-05T10:00:00+00:00")

        runs = state.Run.list_for_repo(self.repo, home=self.tmp)
        self.assertEqual([r.run_id for r in runs], [r1.run_id, r2.run_id, r3.run_id])
        self.assertEqual(state.Run.latest_for_repo(self.repo, home=self.tmp).run_id, r3.run_id)

        # Corrupt run directory or non-run file in runs_dir is safely ignored
        corrupt_dir = os.path.join(self.tmp, "runs", "corrupt-run")
        os.makedirs(corrupt_dir, exist_ok=True)
        with open(os.path.join(corrupt_dir, "run.json"), "w") as f:
            f.write("not valid json")
        runs = state.Run.list_for_repo(self.repo, home=self.tmp)
        self.assertEqual([r.run_id for r in runs], [r1.run_id, r2.run_id, r3.run_id])

        # Empty repo
        empty_repo = os.path.join(self.tmp, "empty_repo")
        os.makedirs(empty_repo)
        self.assertEqual(state.Run.list_for_repo(empty_repo, home=self.tmp), [])
        self.assertIsNone(state.Run.latest_for_repo(empty_repo, home=self.tmp))

    def test_is_active(self):
        run = self._mkrun(self.repo)
        self.assertTrue(runcmds.is_active(run))

        run.update(state="running")
        self.assertTrue(runcmds.is_active(run))

        run.update(state="done")
        self.assertTrue(runcmds.is_active(run))

        # state == done and final approved
        run.write_json("approvals.json", {"final": {"sha256": "abc", "at": "now"}})
        self.assertFalse(runcmds.is_active(run))

        # state != done even with final in approvals.json
        run.update(state="running")
        self.assertTrue(runcmds.is_active(run))

    def test_goal_of(self):
        run = self._mkrun(self.repo)

        # 1. Missing requirements.md
        self.assertEqual(runcmds.goal_of(run), "(no requirements.md)")

        # 2. Under ## Goal (case-insensitive) with list item prefix and collapsing whitespace
        with open(run.path("requirements.md"), "w") as f:
            f.write("# Project Requirements\n\n## goal\n\n  - Build a   scalable \n system.  \n\n## Next")
        self.assertEqual(runcmds.goal_of(run), "Build a scalable system.")

        # With * bullet
        with open(run.path("requirements.md"), "w") as f:
            f.write("## GOAL\n* Ship the feature\n")
        self.assertEqual(runcmds.goal_of(run), "Ship the feature")

        # 3. Fallback: ## Goals (plural) should not match ## Goal section, falls back to first non-heading
        with open(run.path("requirements.md"), "w") as f:
            f.write("## Goals\nNot the goal\nFirst non-heading line")
        self.assertEqual(runcmds.goal_of(run), "Not the goal")

        # 4. Fallback: no ## Goal heading, first non-blank non-heading line
        with open(run.path("requirements.md"), "w") as f:
            f.write("# Overview\n\n### Details\n\n   This is the   fallback goal.  \nAnother line")
        self.assertEqual(runcmds.goal_of(run), "This is the fallback goal.")

        # 4. Fallback when file has only headings or whitespace
        with open(run.path("requirements.md"), "w") as f:
            f.write("# Title\n## Subtitle\n   \n")
        self.assertEqual(runcmds.goal_of(run), "(no requirements.md)")

        # 5. Truncation to 140 chars with ...
        long_goal = "A" * 150
        with open(run.path("requirements.md"), "w") as f:
            f.write("## Goal\n" + long_goal)
        truncated = runcmds.goal_of(run)
        self.assertEqual(len(truncated), 140)
        self.assertTrue(truncated.endswith("..."))
        self.assertEqual(truncated, ("A" * 137) + "...")

    def test_next_step_all_six_branches(self):
        run = self._mkrun(self.repo)

        # Set up valid plan and tickets for testing
        with open(run.path("plan.md"), "w") as f:
            f.write("Plan content")
        run.write_json("tickets.json", {"tickets": [{"id": "T-001"}]})

        # Branch 3: plan.md missing, or plan approval missing/invalid -> 'plan'
        # 3a: plan.md missing
        os.remove(run.path("plan.md"))
        self.assertEqual(runcmds.next_step(run), "plan")

        # 3b: plan.md exists, but not approved
        with open(run.path("plan.md"), "w") as f:
            f.write("Plan content")
        self.assertEqual(runcmds.next_step(run), "plan")

        # 3c: plan approved, but changed afterwards
        planreview.approve(run, "plan")
        with open(run.path("plan.md"), "w") as f:
            f.write("Modified plan")
        self.assertEqual(runcmds.next_step(run), "plan")

        # Re-approve plan
        planreview.approve(run, "plan")

        # Branch 4: tickets approval missing/invalid -> 'tickets'
        # 4a: tickets not approved
        self.assertEqual(runcmds.next_step(run), "tickets")

        # 4b: tickets approved, then changed
        planreview.approve(run, "tickets")
        run.write_json("tickets.json", {"tickets": [{"id": "T-002"}]})
        self.assertEqual(runcmds.next_step(run), "tickets")

        # Re-approve tickets
        planreview.approve(run, "tickets")

        # Branch 5: runner alive -> 'wait'
        with mock.patch("yoke.scheduler.read_lock", return_value=12345), \
             mock.patch("yoke.scheduler._alive", return_value=True):
            self.assertEqual(runcmds.next_step(run), "wait")

        # Branch 6: otherwise -> 'resume'
        with mock.patch("yoke.scheduler.read_lock", return_value=None):
            self.assertEqual(runcmds.next_step(run), "resume")

        # Branch 2: state == 'done', final not approved -> 'finish'
        run.update(state="done")
        self.assertEqual(runcmds.next_step(run), "finish")

        # Branch 1: state == 'done' and final approved -> 'done'
        planreview.approve(run, "final")
        self.assertEqual(runcmds.next_step(run), "done")

    def test_summary(self):
        run = self._mkrun(self.repo)
        with open(run.path("requirements.md"), "w") as f:
            f.write("## Goal\nTest summary helper")
        s = runcmds.summary(run)
        self.assertEqual(s["run_id"], run.run_id)
        self.assertEqual(s["state"], "init")
        self.assertEqual(s["goal"], "Test summary helper")
        self.assertEqual(s["tickets_total"], 0)
        self.assertEqual(s["tickets_closed"], 0)
        self.assertFalse(s["runner_alive"])
        self.assertEqual(s["escalations"], 0)
        self.assertEqual(s["next"], "plan")

    def test_yoke_runs_text_and_json(self):
        # Empty repo text
        empty_dir = os.path.join(self.tmp, "empty_dir")
        os.makedirs(empty_dir)
        subprocess.run(["git", "-C", empty_dir, "init", "-q"], check=True)

        with mock.patch("os.getcwd", return_value=empty_dir), \
             mock.patch.dict(os.environ, {"YOKE_HOME": self.tmp}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.main(["runs"])
            self.assertEqual(rc, 0)
            self.assertEqual(buf.getvalue().strip(), "no runs for this repo")

            # Empty repo json
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.main(["runs", "--json"])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(buf.getvalue().strip()), [])

        # Non-empty repo with 2 runs
        r1 = self._mkrun(self.repo, created_at="2026-01-01T10:00:00+00:00")
        with open(r1.path("requirements.md"), "w") as f:
            f.write("## Goal\nGoal 1")

        r2 = self._mkrun(self.repo, created_at="2026-01-02T10:00:00+00:00")
        with open(r2.path("requirements.md"), "w") as f:
            f.write("## Goal\nGoal 2")
        r2.write_json("escalations.json", [{"id": 1, "ticket": "T-001", "reason": "help"}])

        with mock.patch("os.getcwd", return_value=self.repo), \
             mock.patch.dict(os.environ, {"YOKE_HOME": self.tmp}):
            # Text output
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.main(["runs"])
            self.assertEqual(rc, 0)
            text = buf.getvalue().strip()
            self.assertIn("Goal 1", text)
            self.assertIn("Goal 2", text)
            self.assertIn("escalations: 1", text)
            # Newest is last: r1 appears before r2
            self.assertLess(text.index(r1.run_id), text.index(r2.run_id))

            # JSON output
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.main(["runs", "--json"])
            self.assertEqual(rc, 0)
            data = json.loads(buf.getvalue().strip())
            self.assertEqual(len(data), 2)
            self.assertEqual(data[0]["run_id"], r1.run_id)
            self.assertEqual(data[1]["run_id"], r2.run_id)
            self.assertEqual(data[1]["escalations"], 1)

            # Active filtering: mark r1 as done + final approved
            r1.update(state="done")
            r1.write_json("approvals.json", {"final": {"sha256": "x", "at": "now"}})

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.main(["runs", "--active"])
            self.assertEqual(rc, 0)
            text = buf.getvalue().strip()
            # Split into run entries or first words of blocks
            displayed_run_ids = [line.split()[0] for line in text.splitlines() if line and not line.startswith(" ")]
            self.assertNotIn(r1.run_id, displayed_run_ids)
            self.assertIn(r2.run_id, displayed_run_ids)

    def test_status_active_multi_run(self):
        # With none, prints nothing
        with mock.patch("os.getcwd", return_value=self.repo), \
             mock.patch.dict(os.environ, {"YOKE_HOME": self.tmp}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.main(["status", "--active"])
            self.assertEqual(rc, 0)
            self.assertEqual(buf.getvalue(), "")

        # 4 active runs
        runs = []
        for i in range(1, 5):
            r = self._mkrun(self.repo, created_at="2026-01-0%dT10:00:00+00:00" % i)
            runs.append(r)

        with mock.patch("os.getcwd", return_value=self.repo), \
             mock.patch.dict(os.environ, {"YOKE_HOME": self.tmp}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.main(["status", "--active"])
            self.assertEqual(rc, 0)
            out = buf.getvalue().strip()
            lines = out.splitlines()

            # At most 15 lines
            self.assertLessEqual(len(lines), 15)
            # Prints 3 briefs plus '+1 more'
            self.assertIn("+1 more: yoke runs --active", lines[-1])

            # Newest first: runs[3], runs[2], runs[1]
            self.assertIn(runs[3].run_id, out)
            self.assertIn(runs[2].run_id, out)
            # Oldest is omitted in top 3 briefs
            brief_run_ids = [line.split()[2] for line in lines[:-1] if line.startswith("yoke run ")]
            self.assertNotIn(runs[0].run_id, brief_run_ids)

            # Brief hints contain --run <id> or run id in hint
            for r in (runs[3], runs[2], runs[1]):
                self.assertIn(r.run_id, out)

            # Test escalation hint contains --run <id>
            runs[3].write_json("escalations.json", [{"id": 1, "ticket": "T-001", "reason": "help"}])
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cli.main(["status", "--active"])
            self.assertEqual(rc, 0)
            out = buf.getvalue().strip()
            self.assertIn("yoke escalations --run %s" % runs[3].run_id, out)


if __name__ == "__main__":
    unittest.main()
