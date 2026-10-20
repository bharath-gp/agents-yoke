import json
import os
import tempfile
import unittest

from yoke import config, state, usage


class UsageTestBase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = os.path.realpath(self._td.name)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo, exist_ok=True)
        cfg = config.load(home=self.tmp, overrides={"max_tickets": 10})
        self.run_ = state.Run.create(self.repo, cfg, home=self.tmp)

    def write_meta(self, rel_dir, meta):
        p = os.path.join(self.run_.dir, rel_dir, "meta.json")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(meta, f)
        return p


class TestUsageSummarize(UsageTestBase):
    def test_fixture_run_with_retries_reasks_fallback_and_interrupted(self):
        # 1. T-001 initial attempt-1 code call
        self.write_meta("tickets/T-001/attempt-1/code", {
            "stage": "code",
            "ticket": "T-001",
            "agent": "claude",
            "requested": {"model": "claude-sonnet-5-5"},
            "observed": {
                "model": "claude-sonnet-5-5",
                "usage": {"input_tokens": 100, "output_tokens": 50},
                "cost": 0.01,
            },
        })

        # 2. T-001 re-ask inside attempt-1 code
        self.write_meta("tickets/T-001/attempt-1/code/reask", {
            "stage": "code",
            "ticket": "T-001",
            "agent": "claude",
            "requested": {"model": "claude-sonnet-5-5"},
            "observed": {
                "model": "claude-sonnet-5-5",
                "usage": {"input_tokens": 80, "output_tokens": 20},
                "cost": 0.005,
            },
        })

        # 3. T-001 model fallback inside attempt-1 code
        self.write_meta("tickets/T-001/attempt-1/code/fallback", {
            "stage": "code",
            "ticket": "T-001",
            "agent": "claude",
            "requested": {"model": None},
            "observed": {
                "model": "claude-haiku-5-5",
                "usage": {"input_tokens": 120, "output_tokens": 40},
                "cost": 0.002,
            },
        })

        # 4. T-001 attempt-1 qa call (fails, triggering retry)
        self.write_meta("tickets/T-001/attempt-1/qa", {
            "stage": "qa",
            "ticket": "T-001",
            "agent": "claude",
            "requested": {"model": "claude-haiku-5-5"},
            "observed": {
                "model": "claude-haiku-5-5",
                "usage": {"input_tokens": 60, "output_tokens": 10},
                "cost": 0.001,
            },
        })

        # 5. T-001 attempt-2 qa retry call
        self.write_meta("tickets/T-001/attempt-2/qa", {
            "stage": "qa",
            "ticket": "T-001",
            "agent": "claude",
            "requested": {"model": "claude-haiku-5-5"},
            "observed": {
                "model": "claude-haiku-5-5",
                "usage": {"input_tokens": 70, "output_tokens": 15},
                "cost": 0.0015,
            },
        })

        # 6. Interrupted call from a previous attempt preserved by recover()
        self.write_meta("tickets/T-001/attempt-1.interrupted/code", {
            "stage": "code",
            "ticket": "T-001",
            "agent": "claude",
            "requested": {"model": "claude-sonnet-5-5"},
            # Interrupted before completion, so no ended_at or observed
        })

        # 7. T-002 call (spends more than T-001)
        self.write_meta("tickets/T-002/attempt-1/code", {
            "stage": "code",
            "ticket": "T-002",
            "agent": "claude",
            "requested": {"model": "claude-sonnet-5-5"},
            "observed": {
                "model": "claude-sonnet-5-5",
                "usage": {"input_tokens": 200, "output_tokens": 100},
                "cost": 0.02,
            },
        })

        # 8. T-003 call
        self.write_meta("tickets/T-003/attempt-1/code", {
            "stage": "code",
            "ticket": "T-003",
            "agent": "claude",
            "requested": {"model": "claude-sonnet-5-5"},
            "observed": {
                "model": "claude-sonnet-5-5",
                "usage": {"input_tokens": 50, "output_tokens": 25},
                "cost": 0.005,
            },
        })

        # 9. Non-ticket call (e.g. plan_review)
        self.write_meta("plan-review/round-1", {
            "stage": "plan_review",
            "ticket": None,
            "agent": "claude",
            "requested": {"model": "claude-sonnet-5-5"},
            "observed": {
                "model": "claude-sonnet-5-5",
                "usage": {"input_tokens": 30, "output_tokens": 10},
                "cost": 0.003,
            },
        })

        summary = usage.summarize(self.run_)

        # Hand-computed totals:
        # input_tokens = 100 + 80 + 120 + 60 + 70 + 0 + 200 + 50 + 30 = 710
        # output_tokens = 50 + 20 + 40 + 10 + 15 + 0 + 100 + 25 + 10 = 270
        # cost = 0.01 + 0.005 + 0.002 + 0.001 + 0.0015 + 0.02 + 0.005 + 0.003 = 0.0475
        expected_totals = {
            "cost": 0.0475,
            "input_tokens": 710,
            "output_tokens": 270,
        }
        self.assertEqual(summary["totals"], expected_totals)

        # By ticket hand-computed totals:
        # T-001: inputs = 430, outputs = 135, cost = 0.0195
        self.assertEqual(summary["by_ticket"]["T-001"]["input_tokens"], 430)
        self.assertEqual(summary["by_ticket"]["T-001"]["output_tokens"], 135)
        self.assertEqual(summary["by_ticket"]["T-001"]["cost"], 0.0195)

        # T-002: inputs = 200, outputs = 100, cost = 0.02
        self.assertEqual(summary["by_ticket"]["T-002"]["input_tokens"], 200)
        self.assertEqual(summary["by_ticket"]["T-002"]["output_tokens"], 100)
        self.assertEqual(summary["by_ticket"]["T-002"]["cost"], 0.02)

        # T-003: inputs = 50, outputs = 25, cost = 0.005
        self.assertEqual(summary["by_ticket"]["T-003"]["input_tokens"], 50)
        self.assertEqual(summary["by_ticket"]["T-003"]["output_tokens"], 25)
        self.assertEqual(summary["by_ticket"]["T-003"]["cost"], 0.005)

        # By stage:
        self.assertEqual(summary["by_stage"]["code"]["input_tokens"], 550)
        self.assertEqual(summary["by_stage"]["qa"]["input_tokens"], 130)
        self.assertEqual(summary["by_stage"]["plan_review"]["input_tokens"], 30)

        # By agent:
        self.assertEqual(summary["by_agent"]["claude"]["input_tokens"], 710)
        self.assertEqual(summary["by_agent"]["claude"]["output_tokens"], 270)
        self.assertEqual(summary["by_agent"]["claude"]["cost"], 0.0475)

        # By model:
        self.assertEqual(summary["by_model"]["claude-sonnet-5-5"]["input_tokens"], 460)
        self.assertEqual(summary["by_model"]["claude-sonnet-5-5"]["output_tokens"], 205)
        self.assertEqual(summary["by_model"]["claude-sonnet-5-5"]["cost"], 0.043)
        self.assertEqual(summary["by_model"]["claude-haiku-5-5"]["input_tokens"], 250)
        self.assertEqual(summary["by_model"]["claude-haiku-5-5"]["output_tokens"], 65)
        self.assertEqual(summary["by_model"]["claude-haiku-5-5"]["cost"], 0.0045)

        # Top 3 spenders: ranked by cost descending (T-002: 0.02, T-001: 0.0195, T-003: 0.005)
        spender_ids = [s["ticket"] for s in summary["top_spenders"]]
        self.assertEqual(spender_ids, ["T-002", "T-001", "T-003"])

    def test_agy_only_fixture_reports_cost_as_unreported_not_zero(self):
        # Agy calls have usage tokens but no cost (or cost is None)
        self.write_meta("tickets/T-001/attempt-1/code", {
            "stage": "code",
            "ticket": "T-001",
            "agent": "agy",
            "requested": {"model": "unreported"},
            "observed": {
                "model": "unreported",
                "usage": {"input_tokens": 12000, "output_tokens": 1500, "total_tokens": 13500},
                "cost": None,
            },
        })
        self.write_meta("tickets/T-001/attempt-1/qa", {
            "stage": "qa",
            "ticket": "T-001",
            "agent": "agy",
            "requested": {"model": "unreported"},
            "observed": {
                "model": "unreported",
                "usage": {"input_tokens": 8000, "output_tokens": 800, "total_tokens": 8800},
                "cost": None,
            },
        })
        # Second ticket with fewer tokens to verify top_spenders ranks by tokens when cost unreported
        self.write_meta("tickets/T-002/attempt-1/code", {
            "stage": "code",
            "ticket": "T-002",
            "agent": "agy",
            "requested": {"model": "unreported"},
            "observed": {
                "model": "unreported",
                "usage": {"input_tokens": 3000, "output_tokens": 300, "total_tokens": 3300},
                "cost": None,
            },
        })

        summary = usage.summarize(self.run_)

        # Cost must be the literal string "unreported", never 0 or 0.0
        self.assertEqual(summary["totals"]["cost"], "unreported")
        self.assertNotEqual(summary["totals"]["cost"], 0)
        self.assertNotEqual(summary["totals"]["cost"], 0.0)

        self.assertEqual(summary["by_stage"]["code"]["cost"], "unreported")
        self.assertEqual(summary["by_stage"]["qa"]["cost"], "unreported")
        self.assertEqual(summary["by_agent"]["agy"]["cost"], "unreported")
        self.assertEqual(summary["by_ticket"]["T-001"]["cost"], "unreported")
        self.assertEqual(summary["by_ticket"]["T-002"]["cost"], "unreported")
        self.assertEqual(summary["by_model"]["unreported"]["cost"], "unreported")

        # Tokens are correctly summed
        self.assertEqual(summary["totals"]["input_tokens"], 23000)
        self.assertEqual(summary["totals"]["output_tokens"], 2600)
        self.assertEqual(summary["totals"]["total_tokens"], 25600)

        # Spenders are ranked by tokens descending
        spenders = [s["ticket"] for s in summary["top_spenders"]]
        self.assertEqual(spenders, ["T-001", "T-002"])

    def test_mixed_fixture_with_reported_and_unreported_calls(self):
        # T-001: Claude call with reported cost
        self.write_meta("tickets/T-001/attempt-1/code", {
            "stage": "code",
            "ticket": "T-001",
            "agent": "claude",
            "requested": {"model": "m1"},
            "observed": {
                "model": "m1",
                "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
                "cost": 0.01,
            },
        })
        # T-002: Agy call with unreported cost but higher tokens
        self.write_meta("tickets/T-002/attempt-1/code", {
            "stage": "code",
            "ticket": "T-002",
            "agent": "agy",
            "requested": {"model": "unreported"},
            "observed": {
                "model": "unreported",
                "usage": {"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500},
                "cost": None,
            },
        })

        summary = usage.summarize(self.run_)

        # Totals has reported cost sum and records unreported_calls count
        self.assertEqual(summary["totals"]["cost"], 0.01)
        self.assertEqual(summary["totals"]["unreported_calls"], 1)
        self.assertEqual(summary["totals"]["input_tokens"], 1100)
        self.assertEqual(summary["totals"]["output_tokens"], 550)

        self.assertEqual(summary["by_ticket"]["T-001"]["cost"], 0.01)
        self.assertEqual(summary["by_ticket"]["T-002"]["cost"], "unreported")

        # Spenders ranked by tokens descending because an unreported ticket exists
        spenders = [s["ticket"] for s in summary["top_spenders"]]
        self.assertEqual(spenders, ["T-002", "T-001"])

    def test_empty_run(self):
        summary = usage.summarize(self.run_)
        self.assertEqual(summary["totals"], {"cost": 0.0})
        self.assertEqual(summary["by_stage"], {})
        self.assertEqual(summary["by_ticket"], {})
        self.assertEqual(summary["by_agent"], {})
        self.assertEqual(summary["by_model"], {})
        self.assertEqual(summary["top_spenders"], [])


class TestUsagePersistence(UsageTestBase):
    def test_write_usage_writes_atomically(self):
        self.write_meta("tickets/T-001/attempt-1/code", {
            "stage": "code",
            "ticket": "T-001",
            "agent": "claude",
            "requested": {"model": "m1"},
            "observed": {
                "model": "m1",
                "usage": {"input_tokens": 10},
                "cost": 0.01,
            },
        })

        usage_file = self.run_.path("usage.json")
        self.assertFalse(os.path.exists(usage_file))

        res = usage.write_usage(self.run_)
        self.assertTrue(os.path.exists(usage_file))

        with open(usage_file) as f:
            disk_data = json.load(f)

        self.assertEqual(disk_data["totals"]["cost"], 0.01)
        self.assertEqual(disk_data["totals"]["input_tokens"], 10)
        self.assertEqual(res, disk_data)


class TestCheckBudget(UsageTestBase):
    def test_null_budget_returns_false(self):
        self.run_.update(config=dict(self.run_.cfg, budget=None))
        self.assertEqual(config.check_budget(self.run_), (False, None))
        self.run_.update(config=dict(self.run_.cfg, budget={"max_cost_usd": None, "max_tokens": None}))
        self.assertEqual(config.check_budget(self.run_), (False, None))

    def test_max_tokens_cap_exceeded_message(self):
        self.write_meta("tickets/T-001/attempt-1/code", {
            "stage": "code",
            "ticket": "T-001",
            "agent": "claude",
            "requested": {"model": "m1"},
            "observed": {
                "model": "m1",
                "usage": {"input_tokens": 10},
            },
        })
        self.run_.update(config=dict(self.run_.cfg, budget={"max_cost_usd": None, "max_tokens": 1}))
        exceeded, reason = config.check_budget(self.run_)
        self.assertTrue(exceeded)
        self.assertIn("budget cap exhausted: max_tokens exceeded", reason)
        self.assertIn("10 >= 1", reason)

    def test_max_cost_usd_cap_exceeded_message(self):
        self.write_meta("tickets/T-001/attempt-1/code", {
            "stage": "code",
            "ticket": "T-001",
            "agent": "claude",
            "requested": {"model": "m1"},
            "observed": {
                "model": "m1",
                "cost": 0.05,
            },
        })
        self.run_.update(config=dict(self.run_.cfg, budget={"max_cost_usd": 0.01, "max_tokens": None}))
        exceeded, reason = config.check_budget(self.run_)
        self.assertTrue(exceeded)
        self.assertIn("budget cap exhausted: max_cost_usd exceeded", reason)

    def test_cap_does_not_retrigger_when_already_escalated(self):
        self.write_meta("tickets/T-001/attempt-1/code", {
            "stage": "code",
            "ticket": "T-001",
            "agent": "claude",
            "requested": {"model": "m1"},
            "observed": {
                "model": "m1",
                "usage": {"input_tokens": 10},
            },
        })
        self.run_.update(config=dict(self.run_.cfg, budget={"max_cost_usd": None, "max_tokens": 1}))
        self.run_.update(budget_escalated=True)
        self.assertEqual(config.check_budget(self.run_), (False, None))


from tests.test_core import FAKE_CLI
from tests.test_pipeline import PipeBase, ticket, resp, CODE, REV, val
from yoke import adapters, gitops, pipeline


class TestUsageIntegration(PipeBase):
    def test_usage_json_written_at_ticket_close_and_run_end(self):
        self.setup_tickets(
            [ticket(1)],
            {"code": [resp(CODE, "c1", write={"f1.txt": "x"})],
             "review": [resp(REV)],
             "validate": [resp(val())],
             "router": [{"exit": 1}]})

        usage_file = self.run_.path("usage.json")
        self.assertFalse(os.path.exists(usage_file))

        rc = self.go()
        self.assertEqual(rc, "done")
        self.assertTrue(os.path.exists(usage_file))
        # Ensure written under $YOKE_HOME run dir
        self.assertTrue(os.path.realpath(usage_file).startswith(os.path.realpath(os.environ["YOKE_HOME"])))

        with open(usage_file) as f:
            data = json.load(f)

        self.assertIn("totals", data)
        self.assertIn("by_ticket", data)
        self.assertIn("T-001", data["by_ticket"])

    def test_usage_written_at_ticket_close_specifically(self):
        self.setup_tickets(
            [ticket(1)],
            {"code": [resp(CODE, "c1", write={"f1.txt": "x"})],
             "review": [resp(REV)],
             "validate": [resp(val())],
             "router": [{"exit": 1}]})

        info = self.run_.read_json("run.json")
        gitops.ensure_feature_branch(info["repo"], info["branch"], info["base_sha"],
                                     self.run_.path("worktrees/_integration"))

        usage_file = self.run_.path("usage.json")
        self.assertFalse(os.path.exists(usage_file))

        # Directly run ticket to close without scheduler.run
        st = pipeline.run_ticket(
            self.run_,
            ticket(1),
            adapter_for=lambda role: adapters.FakeAdapter(FAKE_CLI),
            env=self.env,
        )
        self.assertEqual(st, "closed")
        self.assertTrue(os.path.exists(usage_file))
        with open(usage_file) as f:
            data = json.load(f)
        self.assertIn("T-001", data["by_ticket"])

    def test_usage_written_at_run_end_when_ticket_blocked(self):
        self.setup_tickets(
            [ticket(1)],
            {"code": [resp(CODE, "c1", write={"f1.txt": "x"})] * 3,
             "review": [resp(REV)] * 3,
             "validate": [resp(val(ok=False))] * 3,
             "router": [{"exit": 1}]})

        usage_file = self.run_.path("usage.json")
        self.assertFalse(os.path.exists(usage_file))

        rc = self.go()
        self.assertEqual(rc, "needs_user")
        self.assertEqual(self.tstate(1)["status"], "blocked")
        # Run ended without any ticket closing; usage.json must still be written at run end
        self.assertTrue(os.path.exists(usage_file))
        with open(usage_file) as f:
            data = json.load(f)
        self.assertIn("totals", data)

    def test_null_budget_leaves_behaviour_unchanged(self):
        self.setup_tickets(
            [ticket(1)],
            {"code": [resp(CODE, "c1", write={"f1.txt": "x"}, usage={"input_tokens": 100})],
             "review": [resp(REV)],
             "validate": [resp(val())],
             "router": [{"exit": 1}]})
        self.assertEqual(self.run_.cfg.get("budget"), {"max_cost_usd": None, "max_tokens": None})
        rc = self.go()
        self.assertEqual(rc, "done")
        self.assertEqual(self.run_.read_json("escalations.json", []), [])

    def test_budget_max_tokens_escalates_once_and_resume_does_not_reescalate(self):
        self.setup_tickets(
            [ticket(1), ticket(2)],
            {"code": [resp(CODE, "c1", write={"f1.txt": "x"}, usage={"input_tokens": 10}),
                      resp(CODE, "c2", write={"f2.txt": "y"}, usage={"input_tokens": 10})],
             "review": [resp(REV)] * 2,
             "validate": [resp(val())] * 2,
             "router": [{"exit": 1}]})
        self.run_.update(config=dict(self.run_.cfg, budget={"max_cost_usd": None, "max_tokens": 1}))

        # First run: ticket 1 runs, usage exceeds max_tokens=1, stops scheduling ticket 2, opens 1 escalation
        rc = self.go()
        self.assertEqual(rc, "needs_user")
        esc = self.run_.read_json("escalations.json")
        self.assertEqual(len(esc), 1)
        self.assertIsNone(esc[0]["ticket"])
        self.assertIn("budget", esc[0]["reason"].lower())
        self.assertIn("max_tokens", esc[0]["reason"])

        # Resume: must not re-escalate, allows remaining tickets to complete
        rc2 = self.go()
        self.assertEqual(rc2, "done")
        esc2 = self.run_.read_json("escalations.json")
        self.assertEqual(len(esc2), 1)
        self.assertEqual(self.tstate(1)["status"], "closed")
        self.assertEqual(self.tstate(2)["status"], "closed")

    def test_budget_max_cost_usd_escalates_once(self):
        self.setup_tickets(
            [ticket(1), ticket(2)],
            {"code": [resp(CODE, "c1", write={"f1.txt": "x"}, cost=0.05),
                      resp(CODE, "c2", write={"f2.txt": "y"}, cost=0.01)],
             "review": [resp(REV)] * 2,
             "validate": [resp(val())] * 2,
             "router": [{"exit": 1}]})
        self.run_.update(config=dict(self.run_.cfg, budget={"max_cost_usd": 0.01, "max_tokens": None}))

        rc = self.go()
        self.assertEqual(rc, "needs_user")
        esc = self.run_.read_json("escalations.json")
        self.assertEqual(len(esc), 1)
        self.assertIn("max_cost_usd", esc[0]["reason"])

        # Resume
        rc2 = self.go()
        self.assertEqual(rc2, "done")
        self.assertEqual(len(self.run_.read_json("escalations.json")), 1)


if __name__ == "__main__":
    unittest.main()
