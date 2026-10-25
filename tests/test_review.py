import json
import os
import unittest
from unittest import mock

from tests.test_gitops import git
from tests.test_pipeline import CODE, DOCS, PipeBase, qa, resp, ticket, val
from yoke import pipeline, tracker

os.environ.setdefault("YOKE_OLLAMA_URL", "http://127.0.0.1:9")

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
        self.assertEqual(st["review"]["overruled"], [])
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
        self.assertEqual(self.tstate(1)["review"]["overruled"], [])
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
        self.assertEqual(st["review"]["overruled"], [])

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

    def test_review_state_resumed_from_disk_initialises_overruled(self):
        st = {"attempt": 1, "status": "in_progress", "review": {"rounds": 0, "unresolved": []}}
        with mock.patch("yoke.pipeline._call_retry") as mock_call:
            mock_call.return_value = mock.MagicMock(ok=True, structured_output={"verdict": "approve", "findings": []})
            pipeline._review_loop(self.run_, lambda r: None, {}, ticket(1), st, self.repo, "HEAD", {}, "")
        self.assertIn("overruled", st["review"])
        self.assertEqual(st["review"]["overruled"], [])


class TestMultiLensReview(PipeBase):
    def test_two_lenses_producing_same_finding_yield_one_feedback_item(self):
        self.run_.update(config=dict(self.run_.cfg, review_lenses=["security", "performance"]))
        dup_finding = finding(severity="blocker", file="f.py", line=10, comment="dup issue", suggestion="fix dup")
        self.setup_tickets(
            [ticket(1)],
            {"code": [resp(CODE, "c1", write={"f1.txt": "1"}), resp(CODE, "c1", write={"f1.txt": "2"})],
             "review-security": [resp(rev_changes([dup_finding]))],
             "review-security-r1": [resp(REV_APPROVE)],
             "review-performance": [resp(rev_changes([dup_finding]))],
             "review-performance-r1": [resp(REV_APPROVE)],
             "validate": [resp(val())],
             "router": [{"exit": 1}]})
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["status"], "closed")
        cmd_r1 = self.run_.path("tickets/T-001/attempt-1/code-r1/prompt.md")
        with open(cmd_r1) as f:
            prompt_r1 = f.read()
        self.assertEqual(prompt_r1.count("[blocker] f.py:10: dup issue"), 1)

    def test_blocker_from_any_lens_triggers_coder_revision(self):
        self.run_.update(config=dict(self.run_.cfg, review_lenses=["security", "performance"]))
        sec_blocker = finding(severity="blocker", file="f.py", line=10, comment="security hole")
        perf_ok = REV_APPROVE
        self.setup_tickets(
            [ticket(1)],
            {"code": [resp(CODE, "c1", write={"f1.txt": "1"}), resp(CODE, "c1", write={"f1.txt": "2"})],
             "review-security": [resp(rev_changes([sec_blocker]))],
             "review-security-r1": [resp(REV_APPROVE)],
             "review-performance": [resp(perf_ok)],
             "review-performance-r1": [resp(REV_APPROVE)],
             "validate": [resp(val())],
             "router": [{"exit": 1}]})
        self.assertEqual(self.go(), "done")
        attempt_dir = self.run_.path("tickets/T-001/attempt-1")
        self.assertTrue(os.path.isdir(os.path.join(attempt_dir, "code-r1")))
        self.assertTrue(os.path.isdir(os.path.join(attempt_dir, "review-security")))
        self.assertTrue(os.path.isfile(os.path.join(attempt_dir, "review-security/meta.json")))
        self.assertTrue(os.path.isdir(os.path.join(attempt_dir, "review-security-r1")))
        self.assertTrue(os.path.isfile(os.path.join(attempt_dir, "review-security-r1/meta.json")))
        self.assertTrue(os.path.isdir(os.path.join(attempt_dir, "review-performance")))
        self.assertTrue(os.path.isfile(os.path.join(attempt_dir, "review-performance/meta.json")))
        self.assertTrue(os.path.isdir(os.path.join(attempt_dir, "review-performance-r1")))
        self.assertTrue(os.path.isfile(os.path.join(attempt_dir, "review-performance-r1/meta.json")))
        self.assertEqual(self.tstate(1)["review"]["rounds"], 1)

    def test_unresolved_findings_flow_to_validate_and_pr(self):
        comments = []
        rem_finding = finding(severity="blocker", file="f.py", line=10, comment="unresolved flaw")
        with mock.patch("yoke.tracker.comment", side_effect=lambda r, tid, text: comments.append((tid, text))):
            self.run_.update(config=dict(self.run_.cfg, review_lenses=["security", "tests"], max_review_rounds=1))
            self.setup_tickets(
                [ticket(1, "feature")],
                {"code": [resp(CODE, "c1", write={"f1.txt": "1"}), resp(CODE, "c1", write={"f1.txt": "2"})],
                 "review-security": [resp(rev_changes([rem_finding]))],
                 "review-security-r1": [resp(rev_changes([rem_finding]))],
                 "review-tests": [resp(REV_APPROVE)],
                 "review-tests-r1": [resp(REV_APPROVE)],
                 "qa": [resp(qa("true"))],
                 "validate": [resp(val())],
                 "docs": [resp(DOCS)],
                 "router": [{"exit": 1}]})
            self.assertEqual(self.go(), "done")
        st = self.tstate(1)
        self.assertEqual(len(st["review"]["unresolved"]), 1)
        self.assertEqual(st["review"]["unresolved"][0]["comment"], "unresolved flaw")
        with open(self.run_.path("tickets/T-001/attempt-1/qa/prompt.md")) as f:
            qa_prompt = f.read()
        self.assertIn("unresolved flaw", qa_prompt)
        with open(self.run_.path("tickets/T-001/attempt-1/validate/prompt.md")) as f:
            val_prompt = f.read()
        self.assertIn("unresolved flaw", val_prompt)
        pr_text = tracker.pr_body(self.run_)
        self.assertIn("unresolved flaw", pr_text)
        self.assertTrue(any("unresolved flaw" in c[1] for c in comments))

    def test_call_count_equals_rounds_times_lenses_and_counts_against_max_agent_calls_total(self):
        self.run_.update(config=dict(self.run_.cfg, review_lenses=["security", "performance", "tests"]))
        b = finding(severity="blocker", file="f.py", line=10, comment="round 0 blocker")
        self.setup_tickets(
            [ticket(1)],
            {"code": [resp(CODE, "c1", write={"f1.txt": "1"}), resp(CODE, "c1", write={"f1.txt": "2"})],
             "review-security": [resp(rev_changes([b]))],
             "review-security-r1": [resp(REV_APPROVE)],
             "review-performance": [resp(REV_APPROVE)],
             "review-performance-r1": [resp(REV_APPROVE)],
             "review-tests": [resp(REV_APPROVE)],
             "review-tests-r1": [resp(REV_APPROVE)],
             "validate": [resp(val())],
             "router": [{"exit": 1}]})
        self.assertEqual(self.go(), "done")
        with open(self.run_.path("events.jsonl")) as f:
            rev_calls = [json.loads(l) for l in f
                         if json.loads(l).get("kind") == "call_start" and json.loads(l).get("stage") == "review"]
        # 2 rounds * 3 lenses = 6 review calls
        self.assertEqual(len(rev_calls), 6)

    def test_multi_lens_respects_max_agent_calls_total_exhaustion(self):
        self.run_.update(config=dict(self.run_.cfg, review_lenses=["security", "performance", "tests"], max_agent_calls_total=2))
        self.setup_tickets(
            [ticket(1)],
            {"code": [resp(CODE, "c1", write={"f1.txt": "1"})],
             "review-security": [resp(REV_APPROVE)],
             "review-performance": [resp(REV_APPROVE)],
             "review-tests": [resp(REV_APPROVE)],
             "validate": [resp(val())],
             "router": [{"exit": 1}]})
        self.assertEqual(self.go(), "needs_user")
        esc = self.run_.read_json("escalations.json")
        self.assertIn("budget", esc[0]["reason"])

    def test_multi_lens_parallel_respects_max_agent_calls_total_exhaustion(self):
        self.run_.update(config=dict(self.run_.cfg, review_lenses=["security", "performance", "tests"], parallel=3, max_agent_calls_total=2))
        self.setup_tickets(
            [ticket(1)],
            {"code": [resp(CODE, "c1", write={"f1.txt": "1"})],
             "review-security": [resp(REV_APPROVE)],
             "review-performance": [resp(REV_APPROVE)],
             "review-tests": [resp(REV_APPROVE)],
             "validate": [resp(val())],
             "router": [{"exit": 1}]})
        self.assertEqual(self.go(), "needs_user")
        esc = self.run_.read_json("escalations.json")
        self.assertIn("budget", esc[0]["reason"])

    def test_parallel_execution_when_parallel_greater_than_one(self):
        self.run_.update(config=dict(self.run_.cfg, review_lenses=["security", "performance"], parallel=2))
        self.setup_tickets(
            [ticket(1)],
            {"code": [resp(CODE, "c1", write={"f1.txt": "1"})],
             "review-security": [resp(REV_APPROVE)],
             "review-performance": [resp(REV_APPROVE)],
             "validate": [resp(val())],
             "router": [{"exit": 1}]})
        self.assertEqual(self.go(), "done")
        attempt_dir = self.run_.path("tickets/T-001/attempt-1")
        self.assertTrue(os.path.isdir(os.path.join(attempt_dir, "review-security")))
        self.assertTrue(os.path.isdir(os.path.join(attempt_dir, "review-performance")))

    def test_unknown_lens_falls_back_to_generic_review_prompt(self):
        self.run_.update(config=dict(self.run_.cfg, review_lenses=["customlens"]))
        self.setup_tickets(
            [ticket(1)],
            {"code": [resp(CODE, "c1", write={"f1.txt": "1"})],
             "review-customlens": [resp(REV_APPROVE)],
             "validate": [resp(val())],
             "router": [{"exit": 1}]})
        self.assertEqual(self.go(), "done")
        prompt_path = self.run_.path("tickets/T-001/attempt-1/review-customlens/prompt.md")
        with open(prompt_path) as f:
            content = f.read()
        self.assertIn("You are the reviewer, read-only.", content)

    def test_empty_review_lenses_restores_single_review_byte_for_byte(self):
        self.run_.update(config=dict(self.run_.cfg, review_lenses=[]))
        self.setup_tickets(
            [ticket(1)],
            {"code": [resp(CODE, "c1", write={"f1.txt": "1"})],
             "review": [resp(REV_APPROVE)],
             "validate": [resp(val())],
             "router": [{"exit": 1}]})
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["status"], "closed")
        attempt_dir = self.run_.path("tickets/T-001/attempt-1")
        self.assertTrue(os.path.isdir(os.path.join(attempt_dir, "review")))
        self.assertFalse(os.path.exists(os.path.join(attempt_dir, "review-security")))
        self.assertFalse(os.path.exists(os.path.join(attempt_dir, "review-r1")))
        with open(self.run_.path("events.jsonl")) as f:
            rev_calls = [json.loads(l) for l in f
                         if json.loads(l).get("kind") == "call_start" and json.loads(l).get("stage") == "review"]
        self.assertEqual(len(rev_calls), 1)

    def test_dedupe_keeps_highest_severity_when_lenses_report_different_severities(self):
        self.run_.update(config=dict(self.run_.cfg, review_lenses=["security", "performance"]))
        sec_blocker = finding(severity="blocker", file="f.py", line=10, comment="security issue")
        perf_minor = finding(severity="minor", file="f.py", line=10, comment="minor issue")
        self.setup_tickets(
            [ticket(1)],
            {"code": [resp(CODE, "c1", write={"f1.txt": "1"}), resp(CODE, "c1", write={"f1.txt": "2"})],
             "review-security": [resp(rev_changes([sec_blocker]))],
             "review-security-r1": [resp(REV_APPROVE)],
             "review-performance": [resp(rev_changes([perf_minor]))],
             "review-performance-r1": [resp(REV_APPROVE)],
             "validate": [resp(val())],
             "router": [{"exit": 1}]})
        self.assertEqual(self.go(), "done")
        cmd_r1 = self.run_.path("tickets/T-001/attempt-1/code-r1/prompt.md")
        with open(cmd_r1) as f:
            prompt_r1 = f.read()
        self.assertEqual(prompt_r1.count("f.py:10"), 1)
        self.assertIn("[blocker] f.py:10", prompt_r1)

    def test_lenses_never_see_each_others_output(self):
        self.run_.update(config=dict(self.run_.cfg, review_lenses=["security", "performance"]))
        sec_finding = finding(severity="blocker", file="sec.py", line=1, comment="secret_token_leaked")
        self.setup_tickets(
            [ticket(1)],
            {"code": [resp(CODE, "c1", write={"f1.txt": "1"}), resp(CODE, "c1", write={"f1.txt": "2"})],
             "review-security": [resp(rev_changes([sec_finding]))],
             "review-security-r1": [resp(REV_APPROVE)],
             "review-performance": [resp(REV_APPROVE)],
             "review-performance-r1": [resp(REV_APPROVE)],
             "validate": [resp(val())],
             "router": [{"exit": 1}]})
        self.assertEqual(self.go(), "done")
        with open(self.run_.path("tickets/T-001/attempt-1/review-performance/prompt.md")) as f:
            perf_prompt_r0 = f.read()
        self.assertNotIn("secret_token_leaked", perf_prompt_r0)
        self.assertNotIn("review-security", perf_prompt_r0)
        with open(self.run_.path("tickets/T-001/attempt-1/review-performance-r1/prompt.md")) as f:
            perf_prompt_r1 = f.read()
        self.assertNotIn("review-security", perf_prompt_r1)


class TestAdversarialReview(PipeBase):
    def setUp(self):
        super().setUp()
        self.run_.update(config=dict(self.run_.cfg, review_mode="adversarial"))

    def _calls(self):
        with open(self.run_.path("events.jsonl")) as f:
            return [json.loads(l) for l in f if json.loads(l).get("kind") == "call_start"]

    def script_dict(self, **overrides):
        f = finding(severity="blocker", file="a.py", line=1, comment="blocker finding", suggestion="fix a.py")
        d = {
            "code": [resp(CODE, "c1", write={"f1.txt": "1"}), resp(CODE, "c1", write={"f1.txt": "2"})],
            "review": [resp(rev_changes([f])), resp(REV_APPROVE)],
            "rebut": [resp({"responses": [{"id": "f1", "stance": "concede", "argument": "ok", "evidence": "a.py:1"}], "summary": "conceding"})],
            "reply": [resp({"replies": [{"id": "f1", "stance": "maintain", "argument": "still broken"}], "summary": "maintaining"})],
            "judge": [resp({"rulings": [{"id": "f1", "ruling": "upheld", "reason": "finding upheld"}], "summary": "ruling"})],
            "qa": [resp(qa("true"))],
            "validate": [resp(val())],
            "router": [{"exit": 1}],
        }
        d.update(overrides)
        return d

    def test_all_conceded_skips_judge_and_coder_fixes(self):
        script = self.script_dict(
            rebut=[resp({"responses": [{"id": "f1", "stance": "concede", "argument": "ok", "evidence": "a.py:1"}], "summary": "conceding"})],
        )
        self.setup_tickets([ticket(1)], script)
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["status"], "closed")
        calls = self._calls()
        self.assertFalse(any(c.get("stage") == "judge" for c in calls))
        self.assertTrue(os.path.isdir(self.run_.path("tickets/T-001/attempt-1/code-r1")))
        with open(self.run_.path("tickets/T-001/attempt-1/code-r1/prompt.md")) as pfile:
            code_prompt = pfile.read()
        self.assertIn("blocker finding", code_prompt)

    def test_disputed_overruled_ticket_closes_without_coder_round(self):
        f = finding(severity="blocker", file="a.py", line=1, comment="overruled comment", suggestion="fix a.py")
        script = self.script_dict(
            review=[resp(rev_changes([f]))],
            rebut=[resp({"responses": [{"id": "f1", "stance": "dispute", "argument": "not a bug", "evidence": "a.py:1"}], "summary": "disputing"})],
            judge=[resp({"rulings": [{"id": "f1", "ruling": "overruled", "reason": "not a bug indeed"}], "summary": "ruling"})],
        )
        self.setup_tickets([ticket(1)], script)
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["status"], "closed")
        self.assertFalse(os.path.exists(self.run_.path("tickets/T-001/attempt-1/code-r1")))
        st = self.tstate(1)
        self.assertEqual(len(st["review"]["overruled"]), 1)
        self.assertEqual(st["review"]["overruled"][0]["finding"]["comment"], "overruled comment")
        self.assertEqual(st["review"]["overruled"][0]["reason"], "not a bug indeed")
        self.assertTrue(os.path.isfile(self.run_.path("tickets/T-001/attempt-1/debate-r0.json")))
        debate = self.run_.read_json("tickets/T-001/attempt-1/debate-r0.json")
        self.assertEqual(set(debate.keys()), {"round", "findings", "transcript", "rulings", "upheld", "overruled"})
        self.assertEqual(debate["round"], 0)
        self.assertEqual(debate["upheld"], [])
        self.assertEqual(len(debate["overruled"]), 1)
        self.assertEqual(debate["overruled"][0]["finding"]["comment"], "overruled comment")
        self.assertEqual(debate["overruled"][0]["reason"], "not a bug indeed")
        with open(self.run_.path("events.jsonl")) as f:
            deb_events = [json.loads(l) for l in f if json.loads(l).get("kind") == "review_debate"]
        self.assertEqual(len(deb_events), 1)
        self.assertEqual(deb_events[0]["ticket"], "T-001")
        self.assertEqual(deb_events[0]["attempt"], 1)
        self.assertEqual(deb_events[0]["round"], 0)
        self.assertEqual(deb_events[0]["upheld"], 0)
        self.assertEqual(deb_events[0]["overruled"], 1)

    def test_disputed_upheld_triggers_coder_round(self):
        f = finding(severity="blocker", file="a.py", line=1, comment="upheld bug comment", suggestion="fix a.py")
        script = self.script_dict(
            review=[resp(rev_changes([f])), resp(REV_APPROVE)],
            rebut=[resp({"responses": [{"id": "f1", "stance": "dispute", "argument": "not a bug", "evidence": "a.py:1"}], "summary": "disputing"})],
            judge=[resp({"rulings": [{"id": "f1", "ruling": "upheld", "reason": "it is a bug"}], "summary": "ruling"})],
        )
        self.setup_tickets([ticket(1)], script)
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["status"], "closed")
        self.assertTrue(os.path.isdir(self.run_.path("tickets/T-001/attempt-1/code-r1")))
        with open(self.run_.path("tickets/T-001/attempt-1/code-r1/prompt.md")) as pfile:
            code_prompt = pfile.read()
        self.assertIn("upheld bug comment", code_prompt)
        debate = self.run_.read_json("tickets/T-001/attempt-1/debate-r0.json")
        self.assertEqual(set(debate.keys()), {"round", "findings", "transcript", "rulings", "upheld", "overruled"})
        self.assertEqual(len(debate["upheld"]), 1)
        self.assertNotIn("id", debate["upheld"][0])
        self.assertEqual(debate["upheld"][0]["comment"], "upheld bug comment")
        self.assertEqual(debate["overruled"], [])
        with open(self.run_.path("events.jsonl")) as f:
            deb_events = [json.loads(l) for l in f if json.loads(l).get("kind") == "review_debate"]
        self.assertEqual(len(deb_events), 1)
        self.assertEqual(deb_events[0]["ticket"], "T-001")
        self.assertEqual(deb_events[0]["attempt"], 1)
        self.assertEqual(deb_events[0]["round"], 0)
        self.assertEqual(deb_events[0]["upheld"], 1)
        self.assertEqual(deb_events[0]["overruled"], 0)

    def test_debate_rounds_two_adds_reply_call(self):
        self.run_.update(config=dict(self.run_.cfg, debate_rounds=2))
        f = finding(severity="blocker", file="a.py", line=1, comment="issue", suggestion="fix a.py")
        script = self.script_dict(
            review=[resp(rev_changes([f])), resp(REV_APPROVE)],
            rebut=[resp({"responses": [{"id": "f1", "stance": "dispute", "argument": "arg1", "evidence": "a.py:1"}], "summary": "d1"}),
                   resp({"responses": [{"id": "f1", "stance": "dispute", "argument": "arg2", "evidence": "a.py:1"}], "summary": "d2"})],
            reply=[resp({"replies": [{"id": "f1", "stance": "maintain", "argument": "still an issue"}], "summary": "m1"})],
            judge=[resp({"rulings": [{"id": "f1", "ruling": "upheld", "reason": "upheld"}], "summary": "ruling"})],
        )
        self.setup_tickets([ticket(1)], script)
        self.assertEqual(self.go(), "done")
        stages = [c["stage"] for c in self._calls() if c["stage"] in ("rebut", "reply", "judge")]
        self.assertEqual(stages, ["rebut", "reply", "rebut", "judge"])
        labels = [c["dir"].split("/")[-1] for c in self._calls() if c["stage"] in ("rebut", "reply", "judge")]
        self.assertEqual(labels, ["rebut-r0-1", "reply-r0-1", "rebut-r0-2", "judge-r0"])
        self.assertTrue(os.path.isdir(self.run_.path("tickets/T-001/attempt-1/rebut-r0-1")))
        self.assertTrue(os.path.isdir(self.run_.path("tickets/T-001/attempt-1/reply-r0-1")))
        self.assertTrue(os.path.isdir(self.run_.path("tickets/T-001/attempt-1/rebut-r0-2")))
        self.assertTrue(os.path.isdir(self.run_.path("tickets/T-001/attempt-1/judge-r0")))
        debate = self.run_.read_json("tickets/T-001/attempt-1/debate-r0.json")
        self.assertEqual(len(debate["transcript"]), 2)

    def test_reply_withdraw_drops_finding_without_judge(self):
        self.run_.update(config=dict(self.run_.cfg, debate_rounds=2))
        f = finding(severity="blocker", file="a.py", line=1, comment="withdrawn finding", suggestion="fix a.py")
        script = self.script_dict(
            review=[resp(rev_changes([f]))],
            rebut=[resp({"responses": [{"id": "f1", "stance": "dispute", "argument": "explanation", "evidence": "a.py:1"}], "summary": "dispute"})],
            reply=[resp({"replies": [{"id": "f1", "stance": "withdraw", "argument": "explanation accepted"}], "summary": "withdraw"})],
        )
        self.setup_tickets([ticket(1)], script)
        self.assertEqual(self.go(), "done")
        calls = self._calls()
        self.assertFalse(any(c.get("stage") == "judge" for c in calls))
        self.assertFalse(os.path.exists(self.run_.path("tickets/T-001/attempt-1/code-r1")))
        st = self.tstate(1)
        self.assertEqual(len(st["review"]["overruled"]), 1)
        self.assertEqual(st["review"]["overruled"][0]["finding"]["comment"], "withdrawn finding")
        self.assertEqual(st["review"]["overruled"][0]["reason"], "withdrawn by reviewer")
        debate = self.run_.read_json("tickets/T-001/attempt-1/debate-r0.json")
        self.assertEqual(debate["round"], 0)
        self.assertEqual(debate["upheld"], [])
        self.assertEqual(len(debate["overruled"]), 1)
        self.assertEqual(debate["overruled"][0]["finding"]["comment"], "withdrawn finding")
        self.assertEqual(debate["overruled"][0]["reason"], "withdrawn by reviewer")
        with open(self.run_.path("events.jsonl")) as f:
            deb_events = [json.loads(l) for l in f if json.loads(l).get("kind") == "review_debate"]
        self.assertEqual(len(deb_events), 1)
        self.assertEqual(deb_events[0]["upheld"], 0)
        self.assertEqual(deb_events[0]["overruled"], 1)

    def test_reply_mixed_withdraw_and_maintain_sent_to_judge(self):
        self.run_.update(config=dict(self.run_.cfg, debate_rounds=2))
        f1 = finding(severity="blocker", file="a.py", line=1, comment="finding to withdraw", suggestion="fix a.py")
        f2 = finding(severity="major", file="b.py", line=2, comment="finding to maintain", suggestion="fix b.py")
        script = self.script_dict(
            review=[resp(rev_changes([f1, f2])), resp(REV_APPROVE)],
            rebut=[
                resp({"responses": [
                    {"id": "f1", "stance": "dispute", "argument": "arg a", "evidence": "a.py:1"},
                    {"id": "f2", "stance": "dispute", "argument": "arg b", "evidence": "b.py:2"},
                ], "summary": "disputing both"}),
                resp({"responses": [
                    {"id": "f2", "stance": "dispute", "argument": "still dispute b", "evidence": "b.py:2"},
                ], "summary": "disputing f2"}),
            ],
            reply=[
                resp({"replies": [
                    {"id": "f1", "stance": "withdraw", "argument": "withdrawing a"},
                    {"id": "f2", "stance": "maintain", "argument": "maintaining b"},
                ], "summary": "mixed reply"}),
            ],
            judge=[
                resp({"rulings": [
                    {"id": "f2", "ruling": "upheld", "reason": "b is a valid bug"},
                ], "summary": "judge ruling"}),
            ],
        )
        self.setup_tickets([ticket(1)], script)
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["status"], "closed")

        debate = self.run_.read_json("tickets/T-001/attempt-1/debate-r0.json")
        self.assertEqual(len(debate["upheld"]), 1)
        self.assertEqual(debate["upheld"][0]["comment"], "finding to maintain")
        self.assertEqual(len(debate["overruled"]), 1)
        self.assertEqual(debate["overruled"][0]["finding"]["comment"], "finding to withdraw")
        self.assertEqual(debate["overruled"][0]["reason"], "withdrawn by reviewer")

        with open(self.run_.path("events.jsonl")) as f:
            deb_events = [json.loads(l) for l in f if json.loads(l).get("kind") == "review_debate"]
        self.assertEqual(len(deb_events), 1)
        self.assertEqual(deb_events[0]["upheld"], 1)
        self.assertEqual(deb_events[0]["overruled"], 1)

        st = self.tstate(1)
        self.assertEqual(len(st["review"]["overruled"]), 1)
        self.assertEqual(st["review"]["overruled"][0]["finding"]["comment"], "finding to withdraw")
        self.assertEqual(st["review"]["overruled"][0]["reason"], "withdrawn by reviewer")

        self.assertTrue(os.path.isdir(self.run_.path("tickets/T-001/attempt-1/code-r1")))
        with open(self.run_.path("tickets/T-001/attempt-1/code-r1/prompt.md")) as pfile:
            code_prompt = pfile.read()
        self.assertIn("finding to maintain", code_prompt)
        self.assertNotIn("finding to withdraw", code_prompt)

    def test_independent_mode_never_calls_rebut_or_judge(self):
        self.run_.update(config=dict(self.run_.cfg, review_mode="independent"))
        f = finding(severity="blocker", file="a.py", line=1, comment="blocker", suggestion="fix a.py")
        script = self.script_dict(
            review=[resp(rev_changes([f])), resp(REV_APPROVE)],
        )
        self.setup_tickets([ticket(1)], script)
        self.assertEqual(self.go(), "done")
        calls = self._calls()
        self.assertFalse(any(c.get("stage") in ("rebut", "reply", "judge") for c in calls))
        self.assertTrue(os.path.isdir(self.run_.path("tickets/T-001/attempt-1/code-r1")))

    def test_minor_findings_skip_debate(self):
        f = finding(severity="minor", file="a.py", line=1, comment="minor suggestion", suggestion="tidy a.py")
        script = self.script_dict(
            review=[resp(rev_changes([f]))],
        )
        self.setup_tickets([ticket(1)], script)
        self.assertEqual(self.go(), "done")
        calls = self._calls()
        self.assertFalse(any(c.get("stage") in ("rebut", "reply", "judge") for c in calls))
        self.assertFalse(os.path.exists(self.run_.path("tickets/T-001/attempt-1/code-r1")))

    def test_rebut_falls_back_to_fresh_session(self):
        f = finding(severity="blocker", file="a.py", line=1, comment="b", suggestion="fix a.py")
        script = self.script_dict(
            review=[resp(rev_changes([f])), resp(REV_APPROVE)],
            **{
                "rebut-r0-1": [{"exit": 1}],
                "rebut": [resp({"responses": [{"id": "f1", "stance": "concede", "argument": "ok", "evidence": "a.py:1"}], "summary": "c"})],
            }
        )
        self.setup_tickets([ticket(1)], script)
        self.assertEqual(self.go(), "done")
        self.assertTrue(os.path.isdir(self.run_.path("tickets/T-001/attempt-1/rebut-r0-1-fresh")))
        self.assertEqual(self.tstate(1)["sessions"]["code"], "c1")
        cmd_resumed = self.run_.read_json("tickets/T-001/attempt-1/rebut-r0-1/command.json")
        self.assertIn("--resume", cmd_resumed)
        self.assertIn("c1", cmd_resumed)
        cmd_fresh = self.run_.read_json("tickets/T-001/attempt-1/rebut-r0-1-fresh/command.json")
        self.assertNotIn("--resume", cmd_fresh)

    def test_reply_call_failure_fails_attempt(self):
        self.run_.update(config=dict(self.run_.cfg, debate_rounds=2, max_attempts_per_ticket=2))
        f = finding(severity="blocker", file="a.py", line=1, comment="b", suggestion="fix a.py")
        script = self.script_dict(
            code=[resp(CODE, "c1", write={"f1.txt": "1"}),
                  resp(CODE, "c1", write={"f1.txt": "2"})],
            review=[resp(rev_changes([f])), resp(REV_APPROVE)],
            rebut=[resp({"responses": [{"id": "f1", "stance": "dispute", "argument": "no", "evidence": "a.py:1"}], "summary": "d"})],
            reply=[{"exit": 1}],
        )
        self.setup_tickets([ticket(1)], script)
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["attempt"], 2)
        with open(self.run_.path("tickets/T-001/attempt-2/feedback.md")) as f:
            fb = f.read()
        self.assertTrue(fb.startswith("reply call failed"))

    def test_judge_call_failure_fails_attempt(self):
        self.run_.update(config=dict(self.run_.cfg, max_attempts_per_ticket=2))
        f = finding(severity="blocker", file="a.py", line=1, comment="b", suggestion="fix a.py")
        script = self.script_dict(
            code=[resp(CODE, "c1", write={"f1.txt": "1"}),
                  resp(CODE, "c1", write={"f1.txt": "2"})],
            review=[resp(rev_changes([f])), resp(REV_APPROVE)],
            rebut=[resp({"responses": [{"id": "f1", "stance": "dispute", "argument": "no", "evidence": "a.py:1"}], "summary": "d"})],
            judge=[{"exit": 1}],
        )
        self.setup_tickets([ticket(1)], script)
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["attempt"], 2)
        with open(self.run_.path("tickets/T-001/attempt-2/feedback.md")) as f:
            fb = f.read()
        self.assertTrue(fb.startswith("judge call failed"))

    def test_missing_id_in_judge_treated_as_upheld(self):
        f = finding(severity="blocker", file="a.py", line=1, comment="missing ruling finding", suggestion="fix a.py")
        script = self.script_dict(
            review=[resp(rev_changes([f])), resp(REV_APPROVE)],
            rebut=[resp({"responses": [{"id": "f1", "stance": "dispute", "argument": "not a bug", "evidence": "a.py:1"}], "summary": "disputing"})],
            judge=[resp({"rulings": [], "summary": "empty rulings"})],
        )
        self.setup_tickets([ticket(1)], script)
        self.assertEqual(self.go(), "done")
        calls = self._calls()
        self.assertTrue(any(c.get("stage") == "judge" for c in calls))
        self.assertTrue(os.path.isdir(self.run_.path("tickets/T-001/attempt-1/code-r1")))
        debate = self.run_.read_json("tickets/T-001/attempt-1/debate-r0.json")
        self.assertEqual(len(debate["upheld"]), 1)
        self.assertEqual(debate["upheld"][0]["comment"], "missing ruling finding")

    def test_mixed_findings_with_identity_filtering_and_minor_preservation(self):
        f1 = finding(severity="blocker", file="dup.py", line=10, comment="duplicate finding", suggestion="fix dup")
        f2 = finding(severity="major", file="b.py", line=20, comment="major finding", suggestion="fix b")
        f3 = finding(severity="blocker", file="dup.py", line=10, comment="duplicate finding", suggestion="fix dup")
        f_minor = finding(severity="minor", file="m.py", line=30, comment="minor suggestion", suggestion="clean")

        self.assertEqual(f1, f3)
        self.assertIsNot(f1, f3)

        script = self.script_dict(
            review=[resp({"verdict": "changes_requested", "findings": [f1, f2, f3, f_minor], "summary": "mixed"}),
                    resp(REV_APPROVE)],
            rebut=[resp({"responses": [
                {"id": "f1", "stance": "concede", "argument": "agree", "evidence": "dup.py:10"},
                {"id": "f2", "stance": "dispute", "argument": "disagree", "evidence": "b.py:20"},
                {"id": "f3", "stance": "dispute", "argument": "disagree", "evidence": "dup.py:10"},
            ], "summary": "rebuttal"})],
            judge=[resp({"rulings": [
                {"id": "f2", "ruling": "upheld", "reason": "judge upholds f2"},
                {"id": "f3", "ruling": "overruled", "reason": "judge overrules f3"},
            ], "summary": "rulings"})],
        )
        self.setup_tickets([ticket(1)], script)
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["status"], "closed")

        debate = self.run_.read_json("tickets/T-001/attempt-1/debate-r0.json")
        self.assertEqual(set(debate.keys()), {"round", "findings", "transcript", "rulings", "upheld", "overruled"})
        self.assertEqual(debate["round"], 0)
        self.assertEqual(len(debate["upheld"]), 2)
        for u in debate["upheld"]:
            self.assertNotIn("id", u)
        self.assertEqual(debate["upheld"][0]["file"], "dup.py")
        self.assertEqual(debate["upheld"][0]["comment"], "duplicate finding")
        self.assertEqual(debate["upheld"][1]["file"], "b.py")
        self.assertEqual(debate["upheld"][1]["comment"], "major finding")

        self.assertEqual(len(debate["overruled"]), 1)
        self.assertEqual(debate["overruled"][0]["finding"]["file"], "dup.py")
        self.assertEqual(debate["overruled"][0]["finding"]["comment"], "duplicate finding")
        self.assertEqual(debate["overruled"][0]["reason"], "judge overrules f3")

        with open(self.run_.path("events.jsonl")) as f:
            deb_events = [json.loads(l) for l in f if json.loads(l).get("kind") == "review_debate"]
        self.assertEqual(len(deb_events), 1)
        self.assertEqual(deb_events[0]["ticket"], "T-001")
        self.assertEqual(deb_events[0]["attempt"], 1)
        self.assertEqual(deb_events[0]["round"], 0)
        self.assertEqual(deb_events[0]["upheld"], 2)
        self.assertEqual(deb_events[0]["overruled"], 1)

        with open(self.run_.path("tickets/T-001/attempt-1/code-r1/prompt.md")) as pfile:
            prompt_r1 = pfile.read()
        self.assertIn("dup.py:10", prompt_r1)
        self.assertIn("b.py:20", prompt_r1)
        self.assertIn("m.py:30", prompt_r1)
        self.assertIn("clean", prompt_r1)
        self.assertEqual(prompt_r1.count("dup.py:10"), 1)

        prior_section = prompt_r1.split("Results of earlier stages (JSON):\n")[1].split("\n\nFeedback from the previous attempt")[0]
        prior_results = json.loads(prior_section)
        review_findings = prior_results["review"]["findings"]
        self.assertEqual(len(review_findings), 3)
        self.assertEqual(review_findings, [f1, f2, f_minor])

        st = self.tstate(1)
        self.assertEqual(len(st["review"]["overruled"]), 1)
        self.assertEqual(st["review"]["overruled"][0]["finding"]["file"], "dup.py")
        self.assertEqual(st["review"]["overruled"][0]["reason"], "judge overrules f3")

    def test_rebut_call_failure_fails_attempt(self):
        self.run_.update(config=dict(self.run_.cfg, max_attempts_per_ticket=2))
        f = finding(severity="blocker", file="a.py", line=1, comment="b", suggestion="fix a.py")
        script = self.script_dict(
            code=[resp(CODE, "c1", write={"f1.txt": "1"}),
                  resp(CODE, "c1", write={"f1.txt": "2"})],
            review=[resp(rev_changes([f])), resp(REV_APPROVE)],
            rebut=[{"exit": 1}],
        )
        self.setup_tickets([ticket(1)], script)
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["attempt"], 2)
        with open(self.run_.path("tickets/T-001/attempt-2/feedback.md")) as f:
            fb = f.read()
        self.assertTrue(fb.startswith("rebut call failed"))

    def test_judge_call_counts_against_max_agent_calls_total(self):
        self.run_.update(config=dict(self.run_.cfg, max_agent_calls_total=3))
        f = finding(severity="blocker", file="a.py", line=1, comment="b", suggestion="fix a.py")
        script = self.script_dict(
            review=[resp(rev_changes([f]))],
            rebut=[resp({"responses": [{"id": "f1", "stance": "dispute", "argument": "no", "evidence": "a.py:1"}], "summary": "d"})],
            judge=[resp({"rulings": [{"id": "f1", "ruling": "upheld", "reason": "upheld"}], "summary": "r"})],
        )
        self.setup_tickets([ticket(1)], script)
        self.assertEqual(self.go(), "needs_user")
        esc = self.run_.read_json("escalations.json")
        self.assertIn("budget", esc[0]["reason"])
        self.assertIn("max_agent_calls_total", esc[0]["reason"])
        self.assertEqual(self.run_.read_json("run.json")["agent_calls"], 3)
        calls = self._calls()
        stages = [c["stage"] for c in calls]
        self.assertIn("rebut", stages)
        self.assertNotIn("judge", stages)

    def test_missing_id_in_rebut_treated_as_disputed(self):
        f = finding(severity="blocker", file="a.py", line=1, comment="b", suggestion="fix a.py")
        script = self.script_dict(
            review=[resp(rev_changes([f])), resp(REV_APPROVE)],
            rebut=[resp({"responses": [], "summary": "empty responses"})],
            judge=[resp({"rulings": [{"id": "f1", "ruling": "upheld", "reason": "valid"}], "summary": "upheld"})],
        )
        self.setup_tickets([ticket(1)], script)
        self.assertEqual(self.go(), "done")
        calls = self._calls()
        self.assertTrue(any(c.get("stage") == "judge" for c in calls))
        self.assertTrue(os.path.isdir(self.run_.path("tickets/T-001/attempt-1/code-r1")))

    def test_multiple_review_passes_unique_debate_files_and_labels(self):
        f1 = finding(severity="blocker", file="a.py", line=1, comment="blocker pass 0", suggestion="fix a")
        f2 = finding(severity="blocker", file="b.py", line=2, comment="blocker pass 1", suggestion="fix b")
        script = self.script_dict(
            code=[
                resp(CODE, "c1", write={"f1.txt": "1"}),
                resp(CODE, "c1", write={"f1.txt": "2"}),
            ],
            review=[resp(rev_changes([f1])), resp(rev_changes([f2]))],
            rebut=[
                resp({"responses": [{"id": "f1", "stance": "dispute", "argument": "arg 0", "evidence": "a.py:1"}], "summary": "d0"}),
                resp({"responses": [{"id": "f1", "stance": "dispute", "argument": "arg 1", "evidence": "b.py:2"}], "summary": "d1"}),
            ],
            judge=[
                resp({"rulings": [{"id": "f1", "ruling": "upheld", "reason": "pass 0 upheld"}], "summary": "j0"}),
                resp({"rulings": [{"id": "f1", "ruling": "overruled", "reason": "pass 1 overruled"}], "summary": "j1"}),
            ],
        )
        self.setup_tickets([ticket(1)], script)
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["status"], "closed")

        self.assertTrue(os.path.isfile(self.run_.path("tickets/T-001/attempt-1/debate-r0.json")))
        self.assertTrue(os.path.isfile(self.run_.path("tickets/T-001/attempt-1/debate-r1.json")))
        self.assertTrue(os.path.isdir(self.run_.path("tickets/T-001/attempt-1/rebut-r0-1")))
        self.assertTrue(os.path.isdir(self.run_.path("tickets/T-001/attempt-1/judge-r0")))
        self.assertTrue(os.path.isdir(self.run_.path("tickets/T-001/attempt-1/code-r1")))
        self.assertTrue(os.path.isdir(self.run_.path("tickets/T-001/attempt-1/rebut-r1-1")))
        self.assertTrue(os.path.isdir(self.run_.path("tickets/T-001/attempt-1/judge-r1")))

        d0 = self.run_.read_json("tickets/T-001/attempt-1/debate-r0.json")
        d1 = self.run_.read_json("tickets/T-001/attempt-1/debate-r1.json")
        self.assertEqual(d0["round"], 0)
        self.assertEqual(len(d0["upheld"]), 1)
        self.assertEqual(d0["upheld"][0]["comment"], "blocker pass 0")
        self.assertEqual(d1["round"], 1)
        self.assertEqual(d1["upheld"], [])
        self.assertEqual(len(d1["overruled"]), 1)
        self.assertEqual(d1["overruled"][0]["finding"]["comment"], "blocker pass 1")

        with open(self.run_.path("events.jsonl")) as f:
            deb_events = [json.loads(l) for l in f if json.loads(l).get("kind") == "review_debate"]
        self.assertEqual(len(deb_events), 2)
        self.assertEqual([e["round"] for e in deb_events], [0, 1])


if __name__ == "__main__":
    unittest.main()
