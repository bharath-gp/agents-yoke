"""Tests for hypotheses / advisory triage hints on repeated last-attempt failures (MS-06 / T-0072)."""
import json
import os
import unittest
from unittest import mock

from tests.test_core import FAKE_CLI
from tests.test_pipeline import CODE, PipeBase, REV, qa, resp, ticket, val
from yoke import adapters, pipeline, router, scheduler

os.environ.setdefault("YOKE_OLLAMA_URL", "http://127.0.0.1:9")


def slurp(path):
    with open(path) as f:
        return f.read()


class HypothesesBase(PipeBase):
    def setUp(self):
        super().setUp()
        os.environ["FAKE_SCRIPT"] = self.script
        self.addCleanup(os.environ.pop, "FAKE_SCRIPT", None)
        router_cfg = dict(self.run_.cfg.get("router") or {})
        router_cfg.setdefault("model", "tev1:4b")
        router_cfg.setdefault("llm_model", "claude-haiku-5-5")
        router_cfg.setdefault("min_confidence", 0.7)
        router_cfg.setdefault("fallback", {"agent": "claude", "model": "claude-haiku-5-5", "effort": "low"})
        router_cfg["ollama_url"] = "http://127.0.0.1:1"
        self.run_.update(config=dict(
            self.run_.cfg,
            max_attempts_per_ticket=3,
            router=router_cfg,
        ))

    def mock_triage(self, category="test_failure", retryable=True):
        ret = {"result": {"category": category, "retryable": retryable},
               "category": category, "retryable": retryable}
        return mock.patch("yoke.pipeline.router.triage_failure", return_value=ret)


class TestHypotheses(HypothesesBase):
    def test_matching_failures_on_last_attempt_append_triage_hint(self):
        """Matching QA failures on the last attempt trigger triage_failure and append advisory hint."""
        self.setup_tickets(
            [ticket(1, "test")],
            {
                "qa": [
                    resp(qa("false", status="fail", failures=["tests.test_a: test_foo failed"])),
                    resp(qa("false", status="fail", failures=["tests.test_a: test_foo failed"])),
                    resp(qa("true", status="pass")),
                ],
                "validate": [resp(val())],
            },
        )
        with self.mock_triage("test_failure", True) as mock_triage:
            self.assertEqual(self.go(), "done")
            mock_triage.assert_called_once()
        self.assertEqual(self.tstate(1)["attempt"], 3)
        self.assertEqual(self.tstate(1)["status"], "closed")

        fb2 = slurp(self.run_.path("tickets/T-001/attempt-2/feedback.md"))
        self.assertIn("test_foo failed", fb2)
        self.assertNotIn("Triage hint", fb2)

        fb3 = slurp(self.run_.path("tickets/T-001/attempt-3/feedback.md"))
        self.assertIn("test_foo failed", fb3)
        self.assertIn("Triage hint (advisory only): category=test_failure, retryable=True", fb3)

    def test_matching_validator_issues_on_last_attempt_append_triage_hint(self):
        """Matching validator unmet criteria/issues on last attempt append triage hint."""
        val_fail = {
            "verdict": "reject",
            "summary": "reject",
            "feedback_for_coder": "fix issues",
            "issues": [{"severity": "major", "file": "app.py", "description": "missing error check"}],
            "exit_criteria": [{"criterion": "criterion_alpha", "met": False, "evidence": "none"}],
        }
        self.setup_tickets(
            [ticket(1, "chore")],
            {
                "code": [
                    resp(CODE, "c1", write={"a.txt": "1"}),
                    resp(CODE, "c1", write={"a.txt": "2"}),
                    resp(CODE, "c1", write={"a.txt": "3"}),
                ],
                "review": [resp(REV)] * 3,
                "validate": [
                    resp(val_fail),
                    resp(val_fail),
                    resp(val()),
                ],
            },
        )
        with self.mock_triage("spec_gap", False) as mock_triage:
            self.assertEqual(self.go(), "done")
            mock_triage.assert_called_once()
        self.assertEqual(self.tstate(1)["attempt"], 3)
        self.assertEqual(self.tstate(1)["status"], "closed")

        fb2 = slurp(self.run_.path("tickets/T-001/attempt-2/feedback.md"))
        self.assertIn("criterion_alpha", fb2)
        self.assertNotIn("Triage hint", fb2)

        fb3 = slurp(self.run_.path("tickets/T-001/attempt-3/feedback.md"))
        self.assertIn("criterion_alpha", fb3)
        self.assertIn("missing error check", fb3)
        self.assertIn("Triage hint (advisory only): category=spec_gap, retryable=False", fb3)

    def test_non_matching_failures_add_no_hint(self):
        """Non-matching failures across attempts do not add a triage hint."""
        self.setup_tickets(
            [ticket(1, "test")],
            {
                "qa": [
                    resp(qa("false", status="fail", failures=["tests.test_a: test_alpha failed"])),
                    resp(qa("false", status="fail", failures=["tests.test_b: test_beta failed"])),
                    resp(qa("true", status="pass")),
                ],
                "validate": [resp(val())],
            },
        )
        with mock.patch("yoke.pipeline.router.triage_failure") as mock_triage:
            self.assertEqual(self.go(), "done")
            mock_triage.assert_not_called()
        self.assertEqual(self.tstate(1)["attempt"], 3)
        self.assertEqual(self.tstate(1)["status"], "closed")

        fb3 = slurp(self.run_.path("tickets/T-001/attempt-3/feedback.md"))
        self.assertIn("test_beta failed", fb3)
        self.assertNotIn("Triage hint", fb3)

    def test_ticket_that_still_fails_escalates_exactly_as_today(self):
        """A ticket that still fails on the last attempt escalates to blocked exactly as before."""
        self.setup_tickets(
            [ticket(1, "test")],
            {
                "qa": [
                    resp(qa("false", status="fail", failures=["tests.test_a: persistent_failure"])),
                    resp(qa("false", status="fail", failures=["tests.test_a: persistent_failure"])),
                    resp(qa("false", status="fail", failures=["tests.test_a: persistent_failure"])),
                ],
            },
        )
        with self.mock_triage("agent_error", True) as mock_triage:
            self.assertEqual(self.go(), "needs_user")
            mock_triage.assert_called_once()
        self.assertEqual(self.tstate(1)["attempt"], 3)
        self.assertEqual(self.tstate(1)["status"], "blocked")

        escalations = self.run_.read_json("escalations.json")
        self.assertEqual([e["ticket"] for e in escalations], ["T-001"])
        self.assertIn("max attempts (3) reached", escalations[0]["reason"])
        self.assertIn("persistent_failure", escalations[0]["reason"])
        self.assertNotIn("Triage hint", escalations[0]["reason"])

        fb3 = slurp(self.run_.path("tickets/T-001/attempt-3/feedback.md"))
        self.assertIn("Triage hint (advisory only): category=agent_error, retryable=True", fb3)

    def test_not_last_attempt_does_not_append_hint(self):
        """Intermediate attempts (attempt < max_attempts_per_ticket) do not append triage hint."""
        self.run_.update(config=dict(self.run_.cfg, max_attempts_per_ticket=4))
        self.setup_tickets(
            [ticket(1, "test")],
            {
                "qa": [
                    resp(qa("false", status="fail", failures=["persistent_failure"])),
                    resp(qa("false", status="fail", failures=["persistent_failure"])),
                    resp(qa("false", status="fail", failures=["persistent_failure"])),
                    resp(qa("true", status="pass")),
                ],
                "validate": [resp(val())],
            },
        )
        with self.mock_triage("test_failure", True) as mock_triage:
            self.assertEqual(self.go(), "done")
            mock_triage.assert_called_once()
        self.assertEqual(self.tstate(1)["attempt"], 4)

        fb2 = slurp(self.run_.path("tickets/T-001/attempt-2/feedback.md"))
        self.assertNotIn("Triage hint", fb2)

        fb3 = slurp(self.run_.path("tickets/T-001/attempt-3/feedback.md"))
        self.assertNotIn("Triage hint", fb3)

        fb4 = slurp(self.run_.path("tickets/T-001/attempt-4/feedback.md"))
        self.assertIn("Triage hint (advisory only): category=test_failure, retryable=True", fb4)


class TestFeedbackMatchingLogic(HypothesesBase):
    def test_clean_feedback_strips_existing_triage_hint(self):
        raw = "QA failed:\ntest_one\n\nTriage hint (advisory only): category=test_failure, retryable=True"
        cleaned = pipeline._clean_feedback(raw)
        self.assertEqual(cleaned, "QA failed:\ntest_one")

    def test_clean_feedback_strips_existing_hypothesis_fix_plan(self):
        raw = "QA failed:\ntest_one\n\nHypothesis fix plan (advisory only):\nFix the thing"
        cleaned = pipeline._clean_feedback(raw)
        self.assertEqual(cleaned, "QA failed:\ntest_one")

    def test_feedback_matches_identical(self):
        fb = "QA failed:\ntest_x failed"
        self.assertTrue(pipeline.feedback_matches(fb, fb))

    def test_feedback_matches_empty_or_none(self):
        self.assertFalse(pipeline.feedback_matches("", ""))
        self.assertFalse(pipeline.feedback_matches(None, "QA failed:\nx"))
        self.assertFalse(pipeline.feedback_matches("QA failed:\nx", ""))

    def test_feedback_matches_qa_test_names(self):
        fb1 = "QA failed:\ntest_one\ntest_two"
        fb2 = "QA failed:\ntest_two\ntest_one"
        self.assertTrue(pipeline.feedback_matches(fb1, fb2))

        fb3 = "QA failed:\ntest_one\ntest_three"
        self.assertFalse(pipeline.feedback_matches(fb1, fb3))

    def test_feedback_matches_runner_rerun_ignoring_unittest_noise(self):
        fb1 = "Runner re-ran `pytest` and it exited 1:\nFAIL: test_a (pkg.Test)\nFAILED (failures=1)"
        fb2 = "Runner re-ran `pytest` and it exited 1:\nFAIL: test_a (pkg.Test)\nFAILED (failures=2)"
        self.assertTrue(pipeline.feedback_matches(fb1, fb2))

    def test_feedback_matches_validator_unmet_and_issues(self):
        fb1 = "fix this\nUnmet criterion: crit1\nmajor file.py: bug"
        fb2 = "different intro\nmajor file.py: bug\nUnmet criterion: crit1"
        self.assertTrue(pipeline.feedback_matches(fb1, fb2))

        fb3 = "fix this\nUnmet criterion: crit2\nmajor file.py: bug"
        self.assertFalse(pipeline.feedback_matches(fb1, fb3))

    def test_maybe_triage_hint_resumes_without_duplicate(self):
        """_maybe_triage_hint appends hint when missing on last attempt and does not duplicate."""
        adapter = adapters.FakeAdapter(FAKE_CLI)
        adapter_for = lambda role: adapter
        pipeline.write_feedback(self.run_, "T-001", 1, "QA failed:\ntest_fail")
        pipeline.write_feedback(self.run_, "T-001", 2, "QA failed:\ntest_fail")

        fb = "QA failed:\ntest_fail"
        with self.mock_triage("test_failure", True) as mock_triage:
            hinted_fb = pipeline._maybe_triage_hint(self.run_, adapter_for, "T-001", 3, fb)
            mock_triage.assert_called_once()
        self.assertIn("Triage hint (advisory only): category=test_failure, retryable=True", hinted_fb)

        again = pipeline._maybe_triage_hint(self.run_, adapter_for, "T-001", 3, hinted_fb)
        self.assertEqual(hinted_fb, again)
        self.assertEqual(again.count("Triage hint"), 1)


class TestRouterFormatHint(unittest.TestCase):
    def test_format_triage_hint_structured_result(self):
        res = {"result": {"category": "test_failure", "retryable": False}}
        hint = router.format_triage_hint(res)
        self.assertEqual(hint, "Triage hint (advisory only): category=test_failure, retryable=False")

    def test_format_triage_hint_flat_dict(self):
        res = {"category": "spec_gap", "retryable": True}
        hint = router.format_triage_hint(res)
        self.assertEqual(hint, "Triage hint (advisory only): category=spec_gap, retryable=True")

    def test_format_triage_hint_defaults(self):
        hint = router.format_triage_hint({})
        self.assertEqual(hint, "Triage hint (advisory only): category=other, retryable=True")

    def test_format_triage_hint_invalid(self):
        self.assertEqual(router.format_triage_hint(None), "")
        self.assertEqual(router.format_triage_hint("non-dict"), "")


class TestHypothesesFanOutAndRanking(HypothesesBase):
    def test_hypotheses_fan_out_and_ranking_audited_and_charged(self):
        """When hypotheses > 0 at trigger, runs N fan-out calls plus 1 ranking call audited under attempt dir."""
        self.run_.update(config=dict(
            self.run_.cfg,
            hypotheses=2,
            max_attempts_per_ticket=3,
        ))

        h1 = {"hypothesis": "spec gap identified", "evidence": "spec docs", "fix_plan": "fix spec edge case", "confidence": 0.75}
        h2 = {"hypothesis": "environment mismatch", "evidence": "env flags", "fix_plan": "export ENV_VAR=1", "confidence": 0.60}
        winning = {"hypothesis": "spec gap confirmed", "evidence": "spec docs and logs", "fix_plan": "winning plan: apply spec patch", "confidence": 0.90}

        self.setup_tickets(
            [ticket(1, "test")],
            {
                "qa": [
                    resp(qa("false", status="fail", failures=["tests.test_a: persistent_test failed"])),
                    resp(qa("false", status="fail", failures=["tests.test_a: persistent_test failed"])),
                    resp(qa("true", status="pass")),
                ],
                "validate": [resp(val())],
                "hypotheses": [
                    resp(h1),
                    resp(h2),
                    resp(winning),
                ],
            },
        )

        initial_calls = self.run_.read_json("run.json")["agent_calls"]
        status = self.go()
        self.assertEqual(status, "done")
        self.assertEqual(self.tstate(1)["attempt"], 3)
        self.assertEqual(self.tstate(1)["status"], "closed")

        # Feedback verification
        fb2 = slurp(self.run_.path("tickets/T-001/attempt-2/feedback.md"))
        self.assertNotIn("Hypothesis fix plan", fb2)

        fb3 = slurp(self.run_.path("tickets/T-001/attempt-3/feedback.md"))
        self.assertIn("Hypothesis fix plan (advisory only):", fb3)
        self.assertIn("winning plan: apply spec patch", fb3)

        # Audit directory verification
        h1_dir = self.run_.path("tickets/T-001/attempt-3/hypotheses-1")
        h2_dir = self.run_.path("tickets/T-001/attempt-3/hypotheses-2")
        rank_dir = self.run_.path("tickets/T-001/attempt-3/hypotheses-rank")

        self.assertTrue(os.path.isdir(h1_dir), "hypotheses-1 directory missing")
        self.assertTrue(os.path.isdir(h2_dir), "hypotheses-2 directory missing")
        self.assertTrue(os.path.isdir(rank_dir), "hypotheses-rank directory missing")

        # Verify audit contents
        for d in (h1_dir, h2_dir, rank_dir):
            self.assertTrue(os.path.isfile(os.path.join(d, "meta.json")))
            self.assertTrue(os.path.isfile(os.path.join(d, "result.json")))

        # Check that 2 fan-out calls + 1 ranking call were charged
        final_calls = self.run_.read_json("run.json")["agent_calls"]
        self.assertGreaterEqual(final_calls - initial_calls, 3)

    def test_hypotheses_zero_changes_nothing_except_triage_hint(self):
        """When hypotheses is 0, no fan-out calls occur and triage hint is added instead."""
        self.run_.update(config=dict(
            self.run_.cfg,
            hypotheses=0,
            max_attempts_per_ticket=3,
        ))

        self.setup_tickets(
            [ticket(1, "test")],
            {
                "qa": [
                    resp(qa("false", status="fail", failures=["tests.test_a: fail_zero"])),
                    resp(qa("false", status="fail", failures=["tests.test_a: fail_zero"])),
                    resp(qa("true", status="pass")),
                ],
                "validate": [resp(val())],
            },
        )

        with self.mock_triage("test_failure", True) as mock_triage:
            self.assertEqual(self.go(), "done")
            mock_triage.assert_called_once()

        fb3 = slurp(self.run_.path("tickets/T-001/attempt-3/feedback.md"))
        self.assertIn("Triage hint (advisory only): category=test_failure, retryable=True", fb3)
        self.assertNotIn("Hypothesis fix plan", fb3)

        # Audit dir check: no hypotheses directories should exist
        h1_dir = self.run_.path("tickets/T-001/attempt-3/hypotheses-1")
        self.assertFalse(os.path.exists(h1_dir))

    def test_pass_fail_never_decided_by_high_confidence(self):
        """High confidence (1.0) does not pass ticket if QA fails; ticket blocks as expected."""
        self.run_.update(config=dict(
            self.run_.cfg,
            hypotheses=1,
            max_attempts_per_ticket=3,
        ))
        high_conf = {"hypothesis": "sure thing", "evidence": "solid", "fix_plan": "confident fix", "confidence": 1.0}

        self.setup_tickets(
            [ticket(1, "test")],
            {
                "qa": [
                    resp(qa("false", status="fail", failures=["tests.test_a: fail_forever"])),
                    resp(qa("false", status="fail", failures=["tests.test_a: fail_forever"])),
                    resp(qa("false", status="fail", failures=["tests.test_a: fail_forever"])),
                ],
                "hypotheses": [
                    resp(high_conf),
                    resp(high_conf),
                ],
            },
        )

        status = self.go()
        self.assertEqual(status, "needs_user")
        self.assertEqual(self.tstate(1)["status"], "blocked")

    def test_pass_fail_never_decided_by_low_confidence(self):
        """Low confidence (0.0) does not block ticket if QA passes; ticket closes as expected."""
        self.run_.update(config=dict(
            self.run_.cfg,
            hypotheses=1,
            max_attempts_per_ticket=3,
        ))
        low_conf = {"hypothesis": "no idea", "evidence": "none", "fix_plan": "wild guess", "confidence": 0.0}

        self.setup_tickets(
            [ticket(1, "test")],
            {
                "qa": [
                    resp(qa("false", status="fail", failures=["tests.test_a: fail_once"])),
                    resp(qa("false", status="fail", failures=["tests.test_a: fail_once"])),
                    resp(qa("true", status="pass")),
                ],
                "validate": [resp(val())],
                "hypotheses": [
                    resp(low_conf),
                    resp(low_conf),
                ],
            },
        )

        status = self.go()
        self.assertEqual(status, "done")
        self.assertEqual(self.tstate(1)["status"], "closed")

    def test_ranking_fallback_picks_highest_confidence_when_ranking_call_fails(self):
        """When ranking call fails, runner falls back to candidate with highest confidence."""
        self.run_.update(config=dict(
            self.run_.cfg,
            hypotheses=2,
            max_attempts_per_ticket=3,
        ))

        cand_low = {"hypothesis": "h_low", "evidence": "e", "fix_plan": "low confidence plan", "confidence": 0.3}
        cand_high = {"hypothesis": "h_high", "evidence": "e", "fix_plan": "high confidence plan", "confidence": 0.85}

        self.setup_tickets(
            [ticket(1, "test")],
            {
                "qa": [
                    resp(qa("false", status="fail", failures=["tests.test_a: fail_rank"])),
                    resp(qa("false", status="fail", failures=["tests.test_a: fail_rank"])),
                    resp(qa("true", status="pass")),
                ],
                "validate": [resp(val())],
                "hypotheses": [
                    resp(cand_low),
                    resp(cand_high),
                    {"exit": 1},  # ranking call exits with error
                ],
            },
        )

        status = self.go()
        self.assertEqual(status, "done")

        fb3 = slurp(self.run_.path("tickets/T-001/attempt-3/feedback.md"))
        self.assertIn("high confidence plan", fb3)
        self.assertNotIn("low confidence plan", fb3)

    def test_budget_exhaustion_during_fan_out(self):
        """When max_agent_calls_total is reached during fan-out, runner escalates budget exhausted."""
        self.run_.update(config=dict(
            self.run_.cfg,
            hypotheses=2,
            max_attempts_per_ticket=3,
            max_agent_calls_total=3,
        ))

        h1 = {"hypothesis": "h1", "evidence": "e", "fix_plan": "p1", "confidence": 0.5}

        self.setup_tickets(
            [ticket(1, "test")],
            {
                "qa": [
                    resp(qa("false", status="fail", failures=["tests.test_a: fail_budget"])),
                    resp(qa("false", status="fail", failures=["tests.test_a: fail_budget"])),
                ],
                "hypotheses": [
                    resp(h1),
                ],
            },
        )

        status = self.go()
        self.assertEqual(status, "needs_user")
        escalations = self.run_.read_json("escalations.json")
        self.assertTrue(any("max_agent_calls_total reached" in e.get("reason", "") for e in escalations))

    def test_fan_out_failure_falls_back_to_triage_hint(self):
        """When hypotheses > 0 but fan-out returns no candidates, runner falls back to triage hint."""
        self.run_.update(config=dict(
            self.run_.cfg,
            hypotheses=1,
            max_attempts_per_ticket=3,
        ))

        self.setup_tickets(
            [ticket(1, "test")],
            {
                "qa": [
                    resp(qa("false", status="fail", failures=["tests.test_a: fail_both"])),
                    resp(qa("false", status="fail", failures=["tests.test_a: fail_both"])),
                    resp(qa("true", status="pass")),
                ],
                "validate": [resp(val())],
                "hypotheses": [
                    {"exit": 1},  # fan-out call fails
                ],
            },
        )

        with self.mock_triage("test_failure", True) as mock_triage:
            status = self.go()
            self.assertEqual(status, "done")
            mock_triage.assert_called_once()

        fb3 = slurp(self.run_.path("tickets/T-001/attempt-3/feedback.md"))
        self.assertIn("Triage hint (advisory only): category=test_failure, retryable=True", fb3)
        self.assertNotIn("Hypothesis fix plan", fb3)

    def test_missing_hypotheses_role_falls_back_to_secondary(self):
        """When by_role lacks 'hypotheses', runner falls back to secondary agent adapter."""
        cfg = dict(self.run_.cfg)
        cfg["roles"] = dict(cfg["roles"])
        cfg["roles"]["by_role"] = dict(cfg["roles"]["by_role"])
        cfg["roles"]["by_role"].pop("hypotheses", None)
        cfg["roles"]["by_role"].pop("hypotheses_rank", None)
        cfg["hypotheses"] = 1
        cfg["max_attempts_per_ticket"] = 3
        self.run_.update(config=cfg)

        h1 = {"hypothesis": "h1", "evidence": "e", "fix_plan": "fallback role fix", "confidence": 0.8}
        self.setup_tickets(
            [ticket(1, "test")],
            {
                "qa": [
                    resp(qa("false", status="fail", failures=["tests.test_a: fail_missing_role"])),
                    resp(qa("false", status="fail", failures=["tests.test_a: fail_missing_role"])),
                    resp(qa("true", status="pass")),
                ],
                "validate": [resp(val())],
                "hypotheses": [
                    resp(h1),
                    resp(h1),
                ],
            },
        )

        status = self.go()
        self.assertEqual(status, "done")
        fb3 = slurp(self.run_.path("tickets/T-001/attempt-3/feedback.md"))
        self.assertIn("Hypothesis fix plan (advisory only):", fb3)
        self.assertIn("fallback role fix", fb3)


if __name__ == "__main__":
    unittest.main()
