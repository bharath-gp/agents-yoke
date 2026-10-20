import io
import json
import os
import unittest
from unittest import mock

from tests.test_core import FAKE_CLI
from tests.test_pipeline import PipeBase, ticket, resp, CODE, REV, val
from yoke import adapters, cli, gitops, pipeline, planreview


class TestNudge(PipeBase):
    def test_nudge_cli_and_state_validation(self):
        self.setup_tickets(
            [ticket(1)],
            {"code": [resp(CODE, "c1", write={"f.txt": "1"})],
             "review": [resp(REV)],
             "validate": [resp(val())],
             "router": [{"exit": 1}]}
        )
        sha_before = planreview.current_hash(self.run_, "tickets")

        # Refuses unknown ticket
        with self.assertRaises(ValueError):
            pipeline.nudge(self.run_, "T-999", "unknown ticket note")
        buf = io.StringIO()
        with mock.patch("sys.stderr", buf):
            rc = cli.main(["nudge", "T-999", "--note", "unknown", "--run", self.run_.run_id])
        self.assertEqual(rc, 1)

        # Refuses empty note
        with self.assertRaises(ValueError):
            pipeline.nudge(self.run_, "T-001", "")

        # Works on pending ticket via CLI
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            rc = cli.main(["nudge", "T-001", "--note", "first pending note", "--run", self.run_.run_id])
        self.assertEqual(rc, 0)
        self.assertIn("nudged T-001", buf.getvalue())

        # Works on pending ticket via API
        pipeline.nudge(self.run_, "T-001", "second pending note")

        # tickets.json remains unchanged so approvals stay valid
        sha_after = planreview.current_hash(self.run_, "tickets")
        self.assertEqual(sha_before, sha_after)
        planreview.check_approval(self.run_, "tickets")

        # Events emitted in audit trail
        with open(self.run_.path("events.jsonl")) as f:
            lines = [json.loads(line) for line in f if '"nudge"' in line]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["ticket"], "T-001")
        self.assertEqual(lines[0]["note"], "first pending note")
        self.assertEqual(lines[1]["note"], "second pending note")

        # Works on in-progress ticket
        st = pipeline.ticket_state(self.run_, "T-001")
        st["status"] = "in_progress"
        pipeline.save_state(self.run_, "T-001", st)
        pipeline.nudge(self.run_, "T-001", "in progress note")
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            rc = cli.main(["nudge", "T-001", "--note", "in progress cli note", "--run", self.run_.run_id])
        self.assertEqual(rc, 0)
        self.assertIn("nudged T-001", buf.getvalue())

        # Refused on closed ticket
        st["status"] = "closed"
        pipeline.save_state(self.run_, "T-001", st)
        with self.assertRaises(ValueError):
            pipeline.nudge(self.run_, "T-001", "closed note")
        buf = io.StringIO()
        with mock.patch("sys.stderr", buf):
            rc = cli.main(["nudge", "T-001", "--note", "closed note", "--run", self.run_.run_id])
        self.assertEqual(rc, 1)

    def test_nudge_appears_in_next_code_prompt_exactly_once(self):
        self.setup_tickets(
            [ticket(1)],
            {"code": [resp(CODE, "c1", write={"f1.txt": "1"}), resp(CODE, "c1", write={"f2.txt": "2"})],
             "review": [resp(REV)] * 2,
             "validate": [resp(val(False)), resp(val(True))],
             "router": [{"exit": 1}]}
        )

        # Send nudge before run
        pipeline.nudge(self.run_, "T-001", "check edge cases")

        # Run pipeline: attempt 1 fails validation, attempt 2 passes
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["attempt"], 2)

        # Note appears in attempt 1 code prompt
        with open(self.run_.path("tickets/T-001/attempt-1/code/prompt.md")) as f:
            prompt1 = f.read()
        self.assertIn("User note: check edge cases", prompt1)

        # Note does NOT appear in attempt 2 code prompt (consumed exactly once)
        with open(self.run_.path("tickets/T-001/attempt-2/code/prompt.md")) as f:
            prompt2 = f.read()
        self.assertNotIn("User note: check edge cases", prompt2)

    def test_multiple_nudges_consumed_in_order(self):
        self.setup_tickets(
            [ticket(1)],
            {"code": [resp(CODE, "c1", write={"f.txt": "1"})],
             "review": [resp(REV)],
             "validate": [resp(val(True))],
             "router": [{"exit": 1}]}
        )

        pipeline.nudge(self.run_, "T-001", "first note alpha")
        pipeline.nudge(self.run_, "T-001", "second note beta")

        self.assertEqual(self.go(), "done")

        with open(self.run_.path("tickets/T-001/attempt-1/code/prompt.md")) as f:
            prompt = f.read()
        self.assertIn("User note: first note alpha", prompt)
        self.assertIn("User note: second note beta", prompt)
        self.assertLess(prompt.index("first note alpha"), prompt.index("second note beta"))

    def test_nudge_between_review_rounds_appears_in_code_r1(self):
        self.setup_tickets([ticket(1)], {})
        wt = self.run_.path("worktrees/T-001")
        info = self.run_.read_json("run.json")
        gitops.ensure_feature_branch(info["repo"], info["branch"], info["base_sha"], self.run_.path("worktrees/_integration"))
        gitops.add_worktree(info["repo"], wt, info["branch"] + "-T-001", info["branch"])
        st = pipeline.ticket_state(self.run_, "T-001")
        st["base_sha"] = gitops.head_sha(wt)
        st["review"] = {"rounds": 0, "unresolved": []}

        rev_findings = [{"file": "f.txt", "line": 1, "severity": "major", "comment": "fix performance", "suggestion": "use binary search"}]
        results = {"code": CODE}

        # Nudge before review round
        pipeline.nudge(self.run_, "T-001", "use binary search")

        adapter = adapters.FakeAdapter(FAKE_CLI)
        with open(self.script, "w") as f:
            json.dump({
                "review": [
                    resp({"verdict": "changes_requested", "findings": rev_findings, "summary": "fix"}),
                    resp({"verdict": "approve", "findings": [], "summary": "ok"})
                ],
                "code-r1": [resp(CODE, "c1", write={"f.txt": "binsearch"})]
            }, f)

        res = pipeline._review_loop(self.run_, lambda r: adapter, self.env, ticket(1), st, wt, st["base_sha"], results, "")
        self.assertIsNone(res)

        # Verify code-r1 prompt contains the nudge
        with open(self.run_.path("tickets/T-001/attempt-1/code-r1/prompt.md")) as f:
            prompt_r1 = f.read()
        self.assertIn("User note: use binary search", prompt_r1)
        self.assertEqual(prompt_r1.count("User note: use binary search"), 1)

    def test_nudge_multiline_collapsed_to_single_line(self):
        self.setup_tickets([ticket(1)], {})
        pipeline.nudge(self.run_, "T-001", "first line\nsecond line\n  third line")
        nudges = pipeline.consume_nudges(self.run_, "T-001")
        self.assertEqual(nudges, ["first line second line third line"])

    def test_status_skill_documents_nudge_and_waiting_limit(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "skills", "status", "SKILL.md")) as f:
            doc = f.read()
        self.assertIn("waiting_limit", doc)
        self.assertIn("`yoke nudge", doc)


if __name__ == "__main__":
    unittest.main()
