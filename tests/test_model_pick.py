import copy
import json
import os
import tempfile
import unittest
from unittest import mock

from tests.test_core import FAKE_CLI
from tests.test_pipeline import CODE, PipeBase, REV, resp, ticket, val
from yoke import adapters, config, pipeline, router, state

os.environ.setdefault("YOKE_OLLAMA_URL", "http://127.0.0.1:9")


class TestBump(unittest.TestCase):
    def test_bump_sequence(self):
        # Step 1: bump('cheap','low',1) == ('cheap','low'); 2 -> medium; 3 -> high; 4 -> ('plan','high')
        self.assertEqual(router.bump("cheap", "low", 1), ("cheap", "low"))
        self.assertEqual(router.bump("cheap", "low", 2), ("cheap", "medium"))
        self.assertEqual(router.bump("cheap", "low", 3), ("cheap", "high"))
        self.assertEqual(router.bump("cheap", "low", 4), ("plan", "high"))
        self.assertEqual(router.bump("cheap", "low", 5), ("plan", "high"))

        # From medium
        self.assertEqual(router.bump("cheap", "medium", 1), ("cheap", "medium"))
        self.assertEqual(router.bump("cheap", "medium", 2), ("cheap", "high"))
        self.assertEqual(router.bump("cheap", "medium", 3), ("plan", "high"))

        # From high
        self.assertEqual(router.bump("cheap", "high", 1), ("cheap", "high"))
        self.assertEqual(router.bump("cheap", "high", 2), ("plan", "high"))

        # Already plan
        self.assertEqual(router.bump("plan", "low", 1), ("plan", "low"))
        self.assertEqual(router.bump("plan", "low", 4), ("plan", "high"))

        # None stays None
        self.assertEqual(router.bump(None, None, 2), (None, None))
        self.assertEqual(router.bump("cheap", None, 2), ("cheap", None))
        self.assertEqual(router.bump(None, "low", 1), (None, "low"))
        self.assertEqual(router.bump(None, "low", 4), (None, "high"))

        # Invalid effort unchanged
        self.assertEqual(router.bump("cheap", "ultra", 2), ("cheap", "ultra"))


class TestPickTierEffort(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.cfg = config.load(home=self._td.name)
        self.run_ = mock.MagicMock()
        self.run_.cfg = self.cfg
        self.ticket = {
            "id": "T-001",
            "title": "ticket title",
            "type": "feature",
            "description": "desc",
            "design": "des",
            "steps": ["step 1"],
            "risks": ["risk 1"],
            "files_hint": ["yoke/router.py"],
        }

    def test_pick_both_accepted_and_payloads(self):
        answers = {
            "tier": {"choice": "plan", "confidence": 0.95},
            "effort": {"choice": "high", "confidence": 0.9},
        }
        with mock.patch("yoke.router._ollama", return_value=answers):
            res = router.pick_tier_effort(self.run_, self.ticket, "code", 1)
        self.assertEqual(res, {"tier": "plan", "effort": "high", "source": "ollama"})
        self.assertEqual(self.run_.event.call_count, 2)

        calls = self.run_.event.call_args_list
        tier_call = calls[0].kwargs
        effort_call = calls[1].kwargs

        self.assertEqual(tier_call["fn"], "model_tier")
        self.assertEqual(tier_call["point"], "model_tier")
        self.assertEqual(tier_call["source"], "ollama")
        self.assertEqual(tier_call["choice"], "plan")
        self.assertEqual(tier_call["confidence"], 0.95)
        self.assertEqual(tier_call["threshold"], config.threshold(self.cfg, "model_tier"))
        self.assertIsNone(tier_call["fallback_reason"])

        self.assertEqual(effort_call["fn"], "effort")
        self.assertEqual(effort_call["point"], "effort")
        self.assertEqual(effort_call["source"], "ollama")
        self.assertEqual(effort_call["choice"], "high")
        self.assertEqual(effort_call["confidence"], 0.9)
        self.assertEqual(effort_call["threshold"], config.threshold(self.cfg, "effort"))
        self.assertIsNone(effort_call["fallback_reason"])

    def test_pick_tier_accepted_effort_rejected(self):
        answers = {
            "tier": {"choice": "plan", "confidence": 0.95},
            "effort": {"choice": "high", "confidence": 0.4},  # below threshold 0.7
        }
        with mock.patch("yoke.router._ollama", return_value=answers):
            res = router.pick_tier_effort(self.run_, self.ticket, "code", 1)
        self.assertEqual(res, {"tier": "plan", "effort": None, "source": "ollama"})

        calls = self.run_.event.call_args_list
        effort_call = calls[1].kwargs
        self.assertEqual(effort_call["source"], "none")
        self.assertIsNone(effort_call["choice"])
        self.assertEqual(effort_call["confidence"], 0.4)
        self.assertIn("confidence 0.40 < 0.70", effort_call["fallback_reason"])

    def test_pick_neither_accepted(self):
        answers = {
            "tier": {"choice": "plan", "confidence": 0.5},
            "effort": {"choice": "high", "confidence": 0.5},
        }
        with mock.patch("yoke.router._ollama", return_value=answers):
            res = router.pick_tier_effort(self.run_, self.ticket, "code", 1)
        self.assertEqual(res, {"tier": None, "effort": None, "source": "none"})

    def test_pick_ollama_fails(self):
        with mock.patch("yoke.router._ollama", side_effect=RuntimeError("connection refused")):
            res = router.pick_tier_effort(self.run_, self.ticket, "code", 1)
        self.assertEqual(res, {"tier": None, "effort": None, "source": "none"})
        self.assertEqual(self.run_.event.call_count, 2)
        for c in self.run_.event.call_args_list:
            self.assertEqual(c.kwargs["source"], "none")
            self.assertIsNone(c.kwargs["choice"])
            self.assertIn("ollama failed", c.kwargs["fallback_reason"])

    def test_pick_non_numeric_confidence_treated_as_missing(self):
        answers = {
            "tier": {"choice": "plan", "confidence": "high"},
            "effort": {"choice": "low", "confidence": None},
        }
        with mock.patch("yoke.router._ollama", return_value=answers):
            res = router.pick_tier_effort(self.run_, self.ticket, "code", 1)
        self.assertEqual(res, {"tier": None, "effort": None, "source": "none"})

    def test_pick_invalid_choice_rejected(self):
        answers = {
            "tier": {"choice": "huge", "confidence": 0.99},
            "effort": {"choice": "maximum", "confidence": 0.99},
        }
        with mock.patch("yoke.router._ollama", return_value=answers):
            res = router.pick_tier_effort(self.run_, self.ticket, "code", 1)
        self.assertEqual(res, {"tier": None, "effort": None, "source": "none"})
        for c in self.run_.event.call_args_list:
            self.assertEqual(c.kwargs["source"], "none")
            self.assertIn("not in", c.kwargs["fallback_reason"])

    def test_pick_missing_key_in_answers(self):
        answers = {
            "tier": {"choice": "plan", "confidence": 0.95},
        }
        with mock.patch("yoke.router._ollama", return_value=answers):
            res = router.pick_tier_effort(self.run_, self.ticket, "code", 1)
        self.assertEqual(res, {"tier": "plan", "effort": None, "source": "ollama"})

    def test_pick_bool_and_nan_confidence_rejected(self):
        answers = {
            "tier": {"choice": "plan", "confidence": True},
            "effort": {"choice": "high", "confidence": float("nan")},
        }
        with mock.patch("yoke.router._ollama", return_value=answers):
            res = router.pick_tier_effort(self.run_, self.ticket, "code", 1)
        self.assertEqual(res, {"tier": None, "effort": None, "source": "none"})


class TestModelPickPipeline(PipeBase):
    def test_pick_applies_plan_tier_model(self):
        # run config models.fake = {cheap: {model: 'c'}, plan: {model: 'p'}};
        # _ollama returns tier plan 0.9, effort medium 0.9;
        # after a one-ticket chore run, the code call's meta.json requested.model == 'p'.
        cfg = dict(self.run_.cfg)
        cfg["models"] = {"fake": {"cheap": {"model": "c"}, "plan": {"model": "p"}}}
        self.run_.update(config=cfg)

        def fake_ollama(c, s, q, timeout=60):
            if "tier" in q and "effort" in q:
                return {
                    "tier": {"choice": "plan", "confidence": 0.9},
                    "effort": {"choice": "medium", "confidence": 0.9},
                }
            return {}

        with mock.patch("yoke.router._ollama", side_effect=fake_ollama):
            self.setup_tickets(
                [ticket(1, "chore")],
                {
                    "code": [resp(CODE, "c1", write={"f1.txt": "x"})],
                    "review": [resp(REV)],
                    "validate": [resp(val())],
                    "router": [{"exit": 1}],
                },
            )
            self.assertEqual(self.go(), "done")

        meta = self.run_.read_json("tickets/T-001/attempt-1/code/meta.json")
        self.assertEqual(meta["requested"]["model"], "p")
        self.assertEqual(meta["requested"]["effort"], "medium")

    def test_override_wins(self):
        # routing.chore.overrides.code.model = 'o';
        # Explicit overrides win: pick is not run, requested.model == 'o'.
        cfg = dict(self.run_.cfg)
        cfg["models"] = {"fake": {"cheap": {"model": "c"}, "plan": {"model": "p"}}}
        routing = dict(cfg.get("routing", {}))
        chore = dict(routing.get("chore", {}))
        overrides = dict(chore.get("overrides", {}))
        overrides["code"] = {"model": "o"}
        chore["overrides"] = overrides
        routing["chore"] = chore
        cfg["routing"] = routing
        self.run_.update(config=cfg)

        orig_pick = router.pick_tier_effort
        code_pick_calls = []

        def spy_pick(run, t, stage, attempt):
            if stage == "code":
                code_pick_calls.append((stage, attempt))
                # If pick were mistakenly called, returning plan would alter the model to 'p'
                return {"tier": "plan", "effort": "high", "source": "ollama"}
            return orig_pick(run, t, stage, attempt)

        with mock.patch("yoke.router.pick_tier_effort", side_effect=spy_pick):
            self.setup_tickets(
                [ticket(1, "chore")],
                {
                    "code": [resp(CODE, "c1", write={"f1.txt": "x"})],
                    "review": [resp(REV)],
                    "validate": [resp(val())],
                    "router": [{"exit": 1}],
                },
            )
            self.assertEqual(self.go(), "done")

        self.assertEqual(code_pick_calls, [])
        meta = self.run_.read_json("tickets/T-001/attempt-1/code/meta.json")
        self.assertEqual(meta["requested"]["model"], "o")

    def test_effort_override_wins(self):
        # Effort-only override also skips pick
        cfg = dict(self.run_.cfg)
        cfg["models"] = {"fake": {"cheap": {"model": "c", "effort": "low"}, "plan": {"model": "p"}}}
        routing = dict(cfg.get("routing", {}))
        chore = dict(routing.get("chore", {}))
        overrides = dict(chore.get("overrides", {}))
        overrides["code"] = {"effort": "high"}
        chore["overrides"] = overrides
        routing["chore"] = chore
        cfg["routing"] = routing
        self.run_.update(config=cfg)

        code_pick_calls = []

        def spy_pick(run, t, stage, attempt):
            if stage == "code":
                code_pick_calls.append((stage, attempt))
                return {"tier": "plan", "effort": "low", "source": "ollama"}
            return {"tier": None, "effort": None, "source": "none"}

        with mock.patch("yoke.router.pick_tier_effort", side_effect=spy_pick):
            self.setup_tickets(
                [ticket(1, "chore")],
                {
                    "code": [resp(CODE, "c1", write={"f1.txt": "x"})],
                    "review": [resp(REV)],
                    "validate": [resp(val())],
                    "router": [{"exit": 1}],
                },
            )
            self.assertEqual(self.go(), "done")

        self.assertEqual(code_pick_calls, [])
        meta = self.run_.read_json("tickets/T-001/attempt-1/code/meta.json")
        self.assertEqual(meta["requested"]["effort"], "high")

    def test_offline_unchanged(self):
        # Ollama down; requested model/effort for code equal config.model_for(cfg, 'fake', 'code').
        # With models unset:
        self.setup_tickets(
            [ticket(1, "chore")],
            {
                "code": [resp(CODE, "c1", write={"f1.txt": "x"})],
                "review": [resp(REV)],
                "validate": [resp(val())],
                "router": [{"exit": 1}],
            },
        )
        self.assertEqual(self.go(), "done")

        meta = self.run_.read_json("tickets/T-001/attempt-1/code/meta.json")
        expected_model, expected_effort = config.model_for(self.run_.cfg, "fake", "code")
        self.assertEqual(meta["requested"]["model"], expected_model)
        self.assertEqual(meta["requested"]["effort"], expected_effort)

    def test_offline_unchanged_with_cheap_model(self):
        cfg = dict(self.run_.cfg)
        cfg["models"] = {"fake": {"cheap": {"model": "m-cheap", "effort": "low"}}}
        self.run_.update(config=cfg)

        self.setup_tickets(
            [ticket(1, "chore")],
            {
                "code": [resp(CODE, "c1", write={"f1.txt": "x"})],
                "review": [resp(REV)],
                "validate": [resp(val())],
                "router": [{"exit": 1}],
            },
        )
        self.assertEqual(self.go(), "done")

        meta = self.run_.read_json("tickets/T-001/attempt-1/code/meta.json")
        expected_model, expected_effort = config.model_for(self.run_.cfg, "fake", "code")
        self.assertEqual(meta["requested"]["model"], expected_model)
        self.assertEqual(meta["requested"]["effort"], expected_effort)
        self.assertEqual(meta["requested"]["model"], "m-cheap")
        self.assertEqual(meta["requested"]["effort"], "low")

    def test_retry_bump_pipeline(self):
        # On attempt 2, retry_bump bumps effort from starting low to medium
        cfg = dict(self.run_.cfg)
        cfg["models"] = {"fake": {"cheap": {"model": "m-cheap", "effort": "low"}, "plan": {"model": "m-plan"}}}
        self.run_.update(config=cfg)

        self.setup_tickets(
            [ticket(1, "chore")],
            {
                "code": [
                    resp(CODE, "c1", write={"f1.txt": "x"}),
                    resp(CODE, "c2", write={"f2.txt": "x"}),
                ],
                "review": [resp(REV)] * 2,
                "validate": [resp(val(ok=False)), resp(val(ok=True))],
                "router": [{"exit": 1}],
            },
        )
        self.assertEqual(self.go(), "done")

        meta1 = self.run_.read_json("tickets/T-001/attempt-1/code/meta.json")
        self.assertEqual(meta1["requested"]["model"], "m-cheap")
        self.assertEqual(meta1["requested"]["effort"], "low")

        meta2 = self.run_.read_json("tickets/T-001/attempt-2/code/meta.json")
        self.assertEqual(meta2["requested"]["model"], "m-cheap")
        self.assertEqual(meta2["requested"]["effort"], "medium")

    def test_retry_bump_disabled(self):
        # When cfg['router']['retry_bump'] is False, attempt 2 does not bump effort
        cfg = dict(self.run_.cfg)
        cfg["models"] = {"fake": {"cheap": {"model": "m-cheap", "effort": "low"}}}
        cfg["router"] = dict(cfg.get("router", {}))
        cfg["router"]["retry_bump"] = False
        self.run_.update(config=cfg)

        self.setup_tickets(
            [ticket(1, "chore")],
            {
                "code": [
                    resp(CODE, "c1", write={"f1.txt": "x"}),
                    resp(CODE, "c2", write={"f2.txt": "x"}),
                ],
                "review": [resp(REV)] * 2,
                "validate": [resp(val(ok=False)), resp(val(ok=True))],
                "router": [{"exit": 1}],
            },
        )
        self.assertEqual(self.go(), "done")

        meta2 = self.run_.read_json("tickets/T-001/attempt-2/code/meta.json")
        self.assertEqual(meta2["requested"]["model"], "m-cheap")
        self.assertEqual(meta2["requested"]["effort"], "low")

    def test_picked_effort_combined_with_retry_bump(self):
        # When pick returns effort='low' on attempt 2, retry bump bumps it to 'medium'
        cfg = dict(self.run_.cfg)
        cfg["models"] = {"fake": {"cheap": {"model": "m-cheap"}, "plan": {"model": "m-plan"}}}
        self.run_.update(config=cfg)

        def fake_ollama(c, s, q, timeout=60):
            if "effort" in q:
                return {
                    "tier": {"choice": "cheap", "confidence": 0.9},
                    "effort": {"choice": "low", "confidence": 0.9},
                }
            return {}

        with mock.patch("yoke.router._ollama", side_effect=fake_ollama):
            self.setup_tickets(
                [ticket(1, "chore")],
                {
                    "code": [
                        resp(CODE, "c1", write={"f1.txt": "x"}),
                        resp(CODE, "c2", write={"f2.txt": "x"}),
                    ],
                    "review": [resp(REV)] * 2,
                    "validate": [resp(val(ok=False)), resp(val(ok=True))],
                    "router": [{"exit": 1}],
                },
            )
            self.assertEqual(self.go(), "done")

        meta1 = self.run_.read_json("tickets/T-001/attempt-1/code/meta.json")
        self.assertEqual(meta1["requested"]["effort"], "low")

        meta2 = self.run_.read_json("tickets/T-001/attempt-2/code/meta.json")
        self.assertEqual(meta2["requested"]["effort"], "medium")

    def test_retry_bump_escalates_to_plan_tier(self):
        # On attempt 4, starting at low effort bumps past index 2 to plan tier
        cfg = dict(self.run_.cfg)
        cfg["max_attempts_per_ticket"] = 4
        cfg["models"] = {"fake": {"cheap": {"model": "m-cheap", "effort": "low"}, "plan": {"model": "m-plan", "effort": "high"}}}
        self.run_.update(config=cfg)

        self.setup_tickets(
            [ticket(1, "chore")],
            {
                "code": [
                    resp(CODE, "c1", write={"f1.txt": "x"}),
                    resp(CODE, "c2", write={"f2.txt": "x"}),
                    resp(CODE, "c3", write={"f3.txt": "x"}),
                    resp(CODE, "c4", write={"f4.txt": "x"}),
                ],
                "review": [resp(REV)] * 4,
                "validate": [
                    resp(val(ok=False)),
                    resp(val(ok=False)),
                    resp(val(ok=False)),
                    resp(val(ok=True)),
                ],
                "router": [{"exit": 1}],
            },
        )
        self.assertEqual(self.go(), "done")

        meta4 = self.run_.read_json("tickets/T-001/attempt-4/code/meta.json")
        self.assertEqual(meta4["requested"]["model"], "m-plan")
        self.assertEqual(meta4["requested"]["effort"], "high")

    def test_stage_tier_sets_plan_model_for_judge(self):
        cfg = dict(self.run_.cfg)
        cfg["models"] = {"agy": {"plan": {"model": "P", "effort": "high"}, "cheap": {"model": "C", "effort": "low"}}}
        self.run_.update(config=cfg)
        captured = []
        with mock.patch("yoke.calls.run_call", side_effect=lambda run, ad, spec, **kw: (captured.append(spec), mock.MagicMock(limit=None, ok=True))[1]):
            with mock.patch("yoke.router.pick_tier_effort") as mock_pick:
                t = ticket(1)
                adapter = adapters.FakeAdapter(FAKE_CLI)
                adapter.name = "agy"
                pipeline._call(
                    self.run_,
                    lambda role: adapter,
                    self.env,
                    t,
                    "judge",
                    1,
                    self.repo,
                    "HEAD",
                    "read",
                    {},
                    "",
                    adapter=adapter,
                )
                self.assertEqual(len(captured), 1)
                self.assertEqual(captured[0].model, "P")
                self.assertEqual(captured[0].effort, "high")
                mock_pick.assert_not_called()

    def test_stage_tier_cheap_for_rebut_and_unset_when_unconfigured(self):
        cfg = dict(self.run_.cfg)
        cfg["models"] = {"agy": {"plan": {"model": "P", "effort": "high"}, "cheap": {"model": "C", "effort": "low"}}}
        self.run_.update(config=cfg)
        t = ticket(1)
        adapter = adapters.FakeAdapter(FAKE_CLI)
        adapter.name = "agy"
        for stage in ("rebut", "reply"):
            captured = []
            with mock.patch("yoke.calls.run_call", side_effect=lambda run, ad, spec, **kw: (captured.append(spec), mock.MagicMock(limit=None, ok=True))[1]):
                with mock.patch("yoke.router.pick_tier_effort") as mock_pick:
                    pipeline._call(
                        self.run_,
                        lambda role: adapter,
                        self.env,
                        t,
                        stage,
                        1,
                        self.repo,
                        "HEAD",
                        "read",
                        {},
                        "",
                        adapter=adapter,
                    )
                    self.assertEqual(len(captured), 1)
                    self.assertEqual(captured[0].model, "C")
                    self.assertEqual(captured[0].effort, "low")
                    mock_pick.assert_not_called()

        # With models {} the spec model and effort are None
        cfg["models"] = {}
        self.run_.update(config=cfg)
        for stage in ("rebut", "reply"):
            captured = []
            with mock.patch("yoke.calls.run_call", side_effect=lambda run, ad, spec, **kw: (captured.append(spec), mock.MagicMock(limit=None, ok=True))[1]):
                with mock.patch("yoke.router.pick_tier_effort") as mock_pick:
                    pipeline._call(
                        self.run_,
                        lambda role: adapter,
                        self.env,
                        t,
                        stage,
                        1,
                        self.repo,
                        "HEAD",
                        "read",
                        {},
                        "",
                        adapter=adapter,
                    )
                    self.assertEqual(len(captured), 1)
                    self.assertIsNone(captured[0].model)
                    self.assertIsNone(captured[0].effort)
                    mock_pick.assert_not_called()

    def test_stage_tier_override_precedence_over_tier(self):
        cfg = dict(self.run_.cfg)
        cfg["roles"] = copy.deepcopy(cfg["roles"])
        cfg["roles"]["by_role"]["judge"] = "secondary"
        cfg["models"] = {"agy": {"plan": {"model": "P", "effort": "high"}, "cheap": {"model": "C", "effort": "low"}}}
        cfg["routing"] = dict(cfg.get("routing", {}))
        cfg["routing"]["chore"] = {
            "stages": ["judge"],
            "overrides": {"judge": {"model": "custom-judge", "effort": "low"}},
        }
        self.run_.update(config=cfg)
        captured = []
        with mock.patch("yoke.calls.run_call", side_effect=lambda run, ad, spec, **kw: (captured.append(spec), mock.MagicMock(limit=None, ok=True))[1]):
            t = ticket(1, "chore")
            adapter = adapters.FakeAdapter(FAKE_CLI)
            adapter.name = "agy"
            pipeline._call(
                self.run_,
                lambda role: adapter,
                self.env,
                t,
                "judge",
                1,
                self.repo,
                "HEAD",
                "read",
                {},
                "",
                adapter=adapter,
            )
            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0].model, "custom-judge")
            self.assertEqual(captured[0].effort, "low")

    def test_stage_tier_timeout_fallback_when_missing_from_config(self):
        cfg = dict(self.run_.cfg)
        cfg["timeouts_s"] = dict(cfg.get("timeouts_s", {}))
        cfg["timeouts_s"].pop("judge", None)
        self.run_.update(config=cfg)
        captured = []
        with mock.patch("yoke.calls.run_call", side_effect=lambda run, ad, spec, **kw: (captured.append(spec), mock.MagicMock(limit=None, ok=True))[1]):
            t = ticket(1)
            adapter = adapters.FakeAdapter(FAKE_CLI)
            adapter.name = "agy"
            pipeline._call(
                self.run_,
                lambda role: adapter,
                self.env,
                t,
                "judge",
                1,
                self.repo,
                "HEAD",
                "read",
                {},
                "",
                adapter=adapter,
            )
            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0].timeout_s, 900)

        # Configured timeout is honoured
        cfg["timeouts_s"]["judge"] = 1234
        self.run_.update(config=cfg)
        captured.clear()
        with mock.patch("yoke.calls.run_call", side_effect=lambda run, ad, spec, **kw: (captured.append(spec), mock.MagicMock(limit=None, ok=True))[1]):
            pipeline._call(
                self.run_,
                lambda role: adapter,
                self.env,
                t,
                "judge",
                1,
                self.repo,
                "HEAD",
                "read",
                {},
                "",
                adapter=adapter,
            )
            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0].timeout_s, 1234)


if __name__ == "__main__":
    unittest.main()
