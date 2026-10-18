import json
import os
import unittest
from unittest import mock

from tests.test_gitops import git
from tests.test_pipeline import CODE, DOCS, PipeBase, resp, ticket, val
from yoke import tracker

REV_APPROVE = {"verdict": "approve", "findings": [], "summary": "looks good"}


def finding(severity="blocker", file="f.py", line=10, comment="bad code", suggestion="fix code"):
    return {"severity": severity, "file": file, "line": line, "comment": comment, "suggestion": suggestion}


def rev_changes(findings, summary="changes needed"):
    return {"verdict": "changes_requested", "findings": list(findings), "summary": summary}


class TestReviewStage(PipeBase):
    def test_approve_path_makes_exactly_one_review_call_and_ticket_closes(self):
        self.setup_tickets(
            [ticket(1)],
            {"code": [resp(CODE, "c1", write={"f1.txt": "x"})],
             "review": [resp(REV_APPROVE)],
             "validate": [resp(val())],
             "router": [{"exit": 1}]})
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["status"], "closed")
        attempt_dir = self.run_.path("tickets/T-001/attempt-1")
        self.assertTrue(os.path.isdir(os.path.join(attempt_dir, "review")))
        self.assertFalse(os.path.exists(os.path.join(attempt_dir, "review-r1")))
        self.assertFalse(os.path.exists(os.path.join(attempt_dir, "code-r1")))
        st = self.tstate(1)
        self.assertEqual(st["review"]["rounds"], 0)
        self.assertEqual(st["review"]["unresolved"], [])
        with open(self.run_.path("events.jsonl")) as f:
            rev_calls = [json.loads(l) for l in f
                         if json.loads(l).get("kind") == "call_start" and json.loads(l).get("stage") == "review"]
        self.assertEqual(len(rev_calls), 1)

    def test_changes_requested_then_approve_resumes_coder_and_second_review(self):
        self.setup_tickets(
            [ticket(1)],
            {"code": [resp(CODE, "sess-code-42", write={"f1.txt": "v1"}),
                      resp(CODE, "sess-code-42", write={"f1.txt": "v2"})],
             "review": [resp(rev_changes([finding(severity="blocker", comment="need v2")])),
                        resp(REV_APPROVE)],
             "validate": [resp(val())],
             "router": [{"exit": 1}]})
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["status"], "closed")
        attempt_dir = self.run_.path("tickets/T-001/attempt-1")
        self.assertTrue(os.path.isdir(os.path.join(attempt_dir, "review")))
        self.assertTrue(os.path.isdir(os.path.join(attempt_dir, "code-r1")))
        self.assertTrue(os.path.isdir(os.path.join(attempt_dir, "review-r1")))
        cmd = self.run_.read_json("tickets/T-001/attempt-1/code-r1/command.json")
        self.assertIn("--resume", cmd)
        self.assertEqual(cmd[cmd.index("--resume") + 1], "sess-code-42")
        self.assertEqual(cmd[cmd.index("--resume") + 1], self.tstate(1)["sessions"]["code"])
        self.assertEqual(self.tstate(1)["review"]["rounds"], 1)
        self.assertEqual(self.tstate(1)["review"]["unresolved"], [])
        with open(self.run_.path("events.jsonl")) as f:
            rev_calls = [json.loads(l) for l in f
                         if json.loads(l).get("kind") == "call_start" and json.loads(l).get("stage") == "review"]
        self.assertEqual(len(rev_calls), 2)

    def test_cap_reached_with_unresolved_closes_records_event_comments_and_surfaces(self):
        comments = []
        findings_cap = [finding(severity="blocker", comment="blocking issue remains"),
                        finding(severity="major", comment="major issue remains")]
        with mock.patch("yoke.tracker.comment", side_effect=lambda r, tid, text: comments.append((tid, text))):
            self.setup_tickets(
                [ticket(1)],
                {"code": [resp(CODE, "c1", write={"f1.txt": "v1"}),
                          resp(CODE, "c1", write={"f1.txt": "v2"}),
                          resp(CODE, "c1", write={"f1.txt": "v3"})],
                 "review": [resp(rev_changes(findings_cap))] * 3,
                 "validate": [resp(val())],
                 "router": [{"exit": 1}]})
            self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["status"], "closed")
        st = self.tstate(1)
        self.assertTrue(len(st["review"]["unresolved"]) > 0)
        self.assertEqual(len(st["review"]["unresolved"]), 2)
        self.assertEqual(st["review"]["unresolved"][0]["comment"], "blocking issue remains")
        self.assertEqual(st["review"]["unresolved"][1]["comment"], "major issue remains")
        self.assertEqual(st["review"]["rounds"], 2)
        with open(self.run_.path("events.jsonl")) as f:
            events = [json.loads(l) for l in f if '"review_unresolved"' in l]
        self.assertTrue(len(events) > 0)
        self.assertEqual(events[0]["ticket"], "T-001")
        self.assertTrue(any(c[0] == "T-001" and "blocking issue remains" in c[1] for c in comments))
        self.assertTrue(any(c[0] == "T-001" and "major issue remains" in c[1] for c in comments))
        with open(self.run_.path("tickets/T-001/attempt-1/validate/prompt.md")) as f:
            val_prompt = f.read()
        self.assertIn("blocking issue remains", val_prompt)
        self.assertIn("major issue remains", val_prompt)

    def test_minor_nit_only_triggers_no_coder_round_and_suggestions_reach_validate(self):
        self.setup_tickets(
            [ticket(1)],
            {"code": [resp(CODE, "c1", write={"f1.txt": "1"})],
             "review": [resp(rev_changes([finding(severity="minor", comment="tidy spacing", suggestion="remove trailing space")]))],
             "validate": [resp(val())],
             "router": [{"exit": 1}]})
        self.assertEqual(self.go(), "done")
        attempt_dir = self.run_.path("tickets/T-001/attempt-1")
        self.assertFalse(os.path.exists(os.path.join(attempt_dir, "code-r1")))
        self.assertFalse(os.path.exists(self.run_.path("tickets/T-001/attempt-1/feedback.md")))
        with open(self.run_.path("tickets/T-001/attempt-1/validate/prompt.md")) as f:
            val_prompt = f.read()
        self.assertIn("remove trailing space", val_prompt)
        self.assertIn("tidy spacing", val_prompt)

    def test_verdict_approve_with_blocker_finding_treated_as_blocking(self):
        self.setup_tickets(
            [ticket(1)],
            {"code": [resp(CODE, "c1", write={"f1.txt": "1"}), resp(CODE, "c1", write={"f1.txt": "2"})],
             "review": [resp({"verdict": "approve", "findings": [finding(severity="blocker", comment="hidden blocker")], "summary": "s"}),
                        resp(REV_APPROVE)],
             "validate": [resp(val())],
             "router": [{"exit": 1}]})
        self.assertEqual(self.go(), "done")
        attempt_dir = self.run_.path("tickets/T-001/attempt-1")
        self.assertTrue(os.path.isdir(os.path.join(attempt_dir, "code-r1")))
        self.assertTrue(os.path.isdir(os.path.join(attempt_dir, "review-r1")))

    def test_every_review_and_revision_call_has_distinct_audit_dir(self):
        self.setup_tickets(
            [ticket(1)],
            {"code": [resp(CODE, "c1", write={"f1.txt": "v1"}),
                      resp(CODE, "c1", write={"f1.txt": "v2"}),
                      resp(CODE, "c1", write={"f1.txt": "v3"})],
             "review": [resp(rev_changes([finding(severity="blocker", comment="round 0")])),
                        resp(rev_changes([finding(severity="blocker", comment="round 1")])),
                        resp(REV_APPROVE)],
             "validate": [resp(val())],
             "router": [{"exit": 1}]})
        self.assertEqual(self.go(), "done")
        attempt_dir = self.run_.path("tickets/T-001/attempt-1")
        for expected in ("review", "code-r1", "review-r1", "code-r2", "review-r2"):
            p = os.path.join(attempt_dir, expected)
            self.assertTrue(os.path.isdir(p), "%s should be a directory" % p)
            self.assertTrue(os.path.isfile(os.path.join(p, "command.json")))
            self.assertTrue(os.path.isfile(os.path.join(p, "meta.json")))

    def test_failed_resumed_coder_call_fails_attempt_and_resets_round_counter_and_clears_st(self):
        self.setup_tickets(
            [ticket(1)],
            {"code": [resp(CODE, "c1", write={"f1.txt": "1"}),
                      {"exit": 1},
                      resp(CODE, "c1", write={"f1.txt": "2"})],
             "review": [resp(rev_changes([finding(severity="blocker", comment="b1")])),
                        resp(REV_APPROVE)],
             "validate": [resp(val())],
             "router": [{"exit": 1}]})
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["attempt"], 2)
        with open(self.run_.path("tickets/T-001/attempt-2/feedback.md")) as f:
            fb2 = f.read()
        self.assertIn("code call failed", fb2)
        st = self.tstate(1)
        self.assertEqual(st["review"]["rounds"], 0)
        self.assertEqual(st["review"]["unresolved"], [])

    def test_review_calls_increment_agent_calls_and_respect_max_agent_calls(self):
        self.setup_tickets(
            [ticket(1)],
            {"code": [resp(CODE, "c1", write={"f1.txt": "1"})],
             "review": [resp(REV_APPROVE)],
             "validate": [resp(val())],
             "router": [{"exit": 1}]})
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.run_.read_json("run.json")["agent_calls"], 4)
        with open(self.run_.path("events.jsonl")) as f:
            rev_calls = [json.loads(l) for l in f
                         if json.loads(l).get("kind") == "call_start" and json.loads(l).get("stage") == "review"]
        self.assertEqual(len(rev_calls), 1)

    def test_review_calls_respect_max_agent_calls_total_and_exhaustion_propagates(self):
        self.run_.update(config=dict(self.run_.cfg, max_agent_calls_total=1))
        self.setup_tickets(
            [ticket(1)],
            {"code": [resp(CODE, "c1", write={"f1.txt": "1"})],
             "review": [resp(REV_APPROVE)],
             "validate": [resp(val())],
             "router": [{"exit": 1}]})
        self.assertEqual(self.go(), "needs_user")
        esc = self.run_.read_json("escalations.json")
        self.assertIn("budget", esc[0]["reason"])

    def test_test_docs_and_research_tickets_never_call_review(self):
        from tests.test_research import SEARCH, fc
        from yoke import pipeline
        res_ticket = ticket(3, "research")
        doc_path = pipeline.doc_path_for(self.run_.cfg, res_ticket)
        res_out = {"status": "done", "doc_path": doc_path, "summary": "s",
                   "claims": [{"id": "C1", "claim": "sky is blue", "source_urls": ["https://a.example"]}]}
        self.setup_tickets([ticket(1, "test"), ticket(2, "docs"), res_ticket], {
            "qa": [resp({"status": "pass", "test_command": "true", "tests_added": [], "commands_run": [],
                         "failures": [], "summary": "s"})],
            "docs": [resp(DOCS)],
            "search": [resp(SEARCH)],
            "research": [resp(res_out, "r1", write={doc_path: "research doc"})],
            "factcheck": [resp(fc())],
            "validate": [resp(val())] * 3,
            "router": [{"exit": 1}] * 3,
        })
        self.assertEqual(self.go(), "done")
        for i, tid in enumerate(("T-001", "T-002", "T-003"), 1):
            self.assertEqual(self.tstate(i)["status"], "closed")
            self.assertFalse(os.path.exists(self.run_.path("tickets/%s/attempt-1/review" % tid)))

    def test_test_ticket_never_calls_review(self):
        self.setup_tickets([ticket(1, "test")], {
            "qa": [resp({"status": "pass", "test_command": "true", "tests_added": [], "commands_run": [],
                         "failures": [], "summary": "s"})],
            "validate": [resp(val())],
            "router": [{"exit": 1}]
        })
        self.assertEqual(self.go(), "done")
        self.assertFalse(os.path.exists(self.run_.path("tickets/T-001/attempt-1/review")))

    def test_docs_ticket_never_calls_review(self):
        self.setup_tickets([ticket(1, "docs")], {
            "docs": [resp(DOCS)],
            "validate": [resp(val())],
            "router": [{"exit": 1}]
        })
        self.assertEqual(self.go(), "done")
        self.assertFalse(os.path.exists(self.run_.path("tickets/T-001/attempt-1/review")))

    def test_research_ticket_never_calls_review(self):
        from tests.test_research import SEARCH, fc
        from yoke import pipeline
        res_ticket = ticket(1, "research")
        doc_path = pipeline.doc_path_for(self.run_.cfg, res_ticket)
        res_out = {"status": "done", "doc_path": doc_path, "summary": "s",
                   "claims": [{"id": "C1", "claim": "sky is blue", "source_urls": ["https://a.example"]}]}
        self.setup_tickets([res_ticket], {
            "search": [resp(SEARCH)],
            "research": [resp(res_out, "r1", write={doc_path: "research doc"})],
            "factcheck": [resp(fc())],
            "router": [{"exit": 1}]
        })
        self.assertEqual(self.go(), "done")
        self.assertFalse(os.path.exists(self.run_.path("tickets/T-001/attempt-1/review")))

    def test_pr_body_unresolved_findings_surfaced_or_omitted(self):
        self.run_.write_json("tickets/T-001/state.json", {
            "status": "closed",
            "review": {"rounds": 0, "unresolved": []}
        })
        body_clean = tracker.pr_body(self.run_)
        self.assertNotIn("Unresolved review findings", body_clean)

        self.run_.write_json("tickets/T-001/state.json", {
            "status": "closed",
            "review": {
                "rounds": 2,
                "unresolved": [
                    finding(severity="blocker", file="yoke/core.py", line=15,
                            comment="unhandled null pointer", suggestion="check None before access")
                ]
            }
        })
        body_unresolved = tracker.pr_body(self.run_)
        self.assertIn("Unresolved review findings", body_unresolved)
        self.assertIn("unhandled null pointer", body_unresolved)
        self.assertIn("check None before access", body_unresolved)


if __name__ == "__main__":
    unittest.main()
