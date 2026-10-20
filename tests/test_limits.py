import datetime
import json
import os
import tempfile
import unittest

from tests.test_core import FAKE_CLI, SCHEMA
from tests.test_pipeline import PipeBase, ticket, resp, CODE, REV, val
from yoke import adapters, calls, config, limits, pipeline, scheduler, state


class LimitsTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = os.path.realpath(self._td.name)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        self.cfg = config.load(home=self.tmp)
        self.run_ = state.Run.create(self.repo, self.cfg, home=self.tmp)
        self.addCleanup(pipeline.clear_pause_state, self.run_)
        self.script = os.path.join(self.tmp, "script.json")
        self.schema_path = os.path.join(self.tmp, "schema.json")
        with open(self.schema_path, "w") as f:
            json.dump(SCHEMA, f)
        self.adapter = adapters.FakeAdapter(FAKE_CLI, cfg=self.cfg)

    def call(self, script, adapter=None, stage="code", timeout_s=30, **kw):
        with open(self.script, "w") as f:
            json.dump(script, f)
        pos = self.script + ".pos"
        if os.path.exists(pos):
            os.remove(pos)
        ad = adapter or self.adapter
        spec = adapters.CallSpec(prompt="do task", schema_path=self.schema_path,
                                 cwd=self.tmp, timeout_s=timeout_s, env={"FAKE_SCRIPT": self.script})
        return calls.run_call(self.run_, ad, spec, stage=stage, **kw)

    def test_fake_cli_limit_with_reset_at(self):
        res = self.call({"code": [{"limit": "Rate limit reached. Resets at 2026-10-20T15:30:00Z"}]})
        self.assertFalse(res.ok)
        self.assertIsNotNone(res.limit)
        self.assertEqual(res.limit["reset_at"], "2026-10-20T15:30:00Z")
        self.assertIn("Rate limit reached", res.limit["raw"])

    def test_fake_cli_limit_without_reset_at(self):
        res = self.call({"code": [{"limit": "Rate limit exceeded. Please wait."}]})
        self.assertFalse(res.ok)
        self.assertIsNotNone(res.limit)
        self.assertIsNone(res.limit["reset_at"])
        self.assertEqual(res.limit["raw"], "Rate limit exceeded. Please wait.")

    def test_fake_cli_limit_relative_reset_at(self):
        res = self.call({"code": [{"limit": "Quota exceeded. Resets in 120s."}]})
        self.assertFalse(res.ok)
        self.assertIsNotNone(res.limit)
        self.assertIsNotNone(res.limit["reset_at"])
        self.assertIn("T", res.limit["reset_at"])

    def test_limit_call_does_not_trigger_schema_reask(self):
        # Even when a valid response follows in the script, limit stop returns immediately
        res = self.call({
            "code": [
                {"limit": "Rate limit reached. Resets at 2026-10-20T15:30:00Z"},
                {"structured_output": {"status": "done"}},
            ]
        }, ticket="T-001", attempt=1)
        self.assertFalse(res.ok)
        self.assertIsNotNone(res.limit)
        self.assertEqual(self.run_.read_json("run.json")["agent_calls"], 1)
        reask_dir = self.run_.path("tickets/T-001/attempt-1/code/reask")
        self.assertFalse(os.path.exists(reask_dir))

    def test_patterns_come_from_config_defaults(self):
        cfg = config.load(home=self.tmp)
        self.assertIn("limits", cfg)
        lim = cfg["limits"]
        self.assertIn("patterns", lim)
        self.assertIn("claude", lim["patterns"])
        self.assertIn("agy", lim["patterns"])
        self.assertEqual(lim["max_waits"], 3)
        self.assertIn("max_wait_s", lim)

    def test_patterns_can_be_overridden(self):
        over = {
            "limits": {
                "patterns": {
                    "fake": [r"CUSTOM_LIMIT_TRIGGER"],
                    "claude": [r"MY_CUSTOM_CLAUDE_LIMIT"],
                }
            }
        }
        cfg_custom = config.load(home=self.tmp, overrides=over)
        self.run_.update(config=cfg_custom)
        custom_adapter = adapters.FakeAdapter(FAKE_CLI, cfg=cfg_custom)

        try:
            # Output with custom pattern matches limit
            res1 = self.call({"code": [{"limit": "CUSTOM_LIMIT_TRIGGER: Resets at 2026-10-20T20:00:00Z"}]},
                             adapter=custom_adapter)
            self.assertIsNotNone(res1.limit)
            self.assertEqual(res1.limit["reset_at"], "2026-10-20T20:00:00Z")

            # Output with standard pattern does not match because fake patterns were overridden
            res2 = self.call({"code": [{"limit": "Rate limit reached"}]}, adapter=custom_adapter)
            self.assertIsNone(res2.limit)
            self.assertFalse(res2.ok)
        finally:
            self.run_.update(config=self.cfg)

    def test_patterns_partial_override_falls_back_to_defaults(self):
        over = {
            "limits": {
                "patterns": {
                    "claude": [r"ONLY_CLAUDE_LIMIT"],
                }
            }
        }
        cfg_partial = config.load(home=self.tmp, overrides=over)
        claude_pats = limits.get_patterns(cfg=cfg_partial, agent="claude")
        self.assertEqual(claude_pats, [r"ONLY_CLAUDE_LIMIT"])
        agy_pats = limits.get_patterns(cfg=cfg_partial, agent="agy")
        self.assertIn("(?i)resource_exhausted", agy_pats)

    def test_fake_cli_limit_json_format(self):
        res = self.call({
            "code": [{"limit": {"is_error": True, "result": "Rate limit exceeded. Resets at 2026-10-20T16:00:00Z"}}]
        })
        self.assertFalse(res.ok)
        self.assertIsNotNone(res.limit)
        self.assertEqual(res.limit["reset_at"], "2026-10-20T16:00:00Z")
        self.assertIn("Rate limit exceeded", res.limit["raw"])

    def test_timeout_clears_limit(self):
        # A timed out call must not carry a limit stop even if stdout has rate limit text
        res = self.call({
            "code": [{"print": "Rate limit reached. Resets at 2026-10-20T15:30:00Z", "sleep": 2}]
        }, timeout_s=1)
        self.assertTrue(res.timed_out)
        self.assertIsNone(res.limit)
        self.assertIn("timeout", res.error)

    def test_claude_adapter_limit_classification(self):
        a = adapters.ClaudeAdapter(cfg=self.cfg)
        json_limit = json.dumps({
            "is_error": True,
            "result": "You have exceeded your usage limit. Resets at 2026-10-20T18:00:00Z",
            "session_id": "sess-limit",
        })
        res = a.parse(json_limit, 0)
        self.assertIsNotNone(res.limit)
        self.assertEqual(res.limit["reset_at"], "2026-10-20T18:00:00Z")
        self.assertEqual(res.session_id, "sess-limit")

        # Exit code 1 with limit text in stdout
        res2 = a.parse("Rate limit reached. Try again later.", 1)
        self.assertIsNotNone(res2.limit)
        self.assertIsNone(res2.limit["reset_at"])

    def test_agy_adapter_limit_classification(self):
        a = adapters.AgyAdapter(cfg=self.cfg)
        json_limit = json.dumps({
            "status": "RESOURCE_EXHAUSTED",
            "response": "Resource exhausted: quota exceeded. Resets at 2026-10-20T19:00:00Z",
            "conversation_id": "conv-limit",
        })
        res = a.parse(json_limit, 0)
        self.assertIsNotNone(res.limit)
        self.assertEqual(res.limit["reset_at"], "2026-10-20T19:00:00Z")
        self.assertEqual(res.session_id, "conv-limit")

    def test_non_limit_failures_classify_exactly_as_today(self):
        res_exit = self.call({"code": [{"exit": 3}]})
        self.assertFalse(res_exit.ok)
        self.assertIsNone(res_exit.limit)
        self.assertEqual(res_exit.exit_code, 3)

        res_schema = self.call({
            "code": [
                {"structured_output": {"status": "bogus"}, "session_id": "s1"},
                {"structured_output": {"status": "done"}, "session_id": "s1"},
            ]
        }, ticket="T-002", attempt=1)
        self.assertTrue(res_schema.ok)
        self.assertIsNone(res_schema.limit)
        self.assertTrue(res_schema.audit_dir.endswith("code/reask"))

        res_denied = self.call({
            "code": [
                {"denied": "git status", "session_id": "s2"},
                {"structured_output": {"status": "done"}, "session_id": "s2"},
            ]
        }, ticket="T-003", attempt=1)
        self.assertTrue(res_denied.ok)
        self.assertIsNone(res_denied.limit)
        self.assertTrue(res_denied.audit_dir.endswith("code/reask"))

    def test_prose_mention_of_rate_limit_does_not_trigger_limit(self):
        # Claude with no structured_output mentioning rate limit in prose
        claude_adapter = adapters.ClaudeAdapter(cfg=self.cfg)
        no_so_claude = json.dumps({
            "session_id": "s1",
            "is_error": False,
            "result": "I considered rate limit handling but did not output structured JSON",
        })
        res_claude = claude_adapter.parse(no_so_claude, 0)
        self.assertIsNone(res_claude.limit)
        self.assertEqual(res_claude.error, "no structured_output")

        # Agy with status FAILED mentioning rate limit in response
        agy_adapter = adapters.AgyAdapter(cfg=self.cfg)
        agy_failed = json.dumps({
            "conversation_id": "c1",
            "status": "FAILED",
            "response": "Encountered rate limit during tool execution",
        })
        res_agy = agy_adapter.parse(agy_failed, 0)
        self.assertIsNone(res_agy.limit)
        self.assertEqual(res_agy.error, "agy status FAILED")

        # Agy with permission denial mentioning rate limit
        agy_denied = json.dumps({
            "conversation_id": "c2",
            "status": "SUCCESS",
            "denied_actions": [{"action": "shell", "display_name": "check rate limit"}],
        })
        res_denied = agy_adapter.parse(agy_denied, 0)
        self.assertIsNone(res_denied.limit)
        self.assertIn("permission denied: check rate limit", res_denied.error)

    def test_raw_is_truncated_when_too_long(self):
        long_message = "Rate limit reached: " + ("x" * 600) + " Resets at 2026-10-20T15:30:00Z"
        res = self.call({"code": [{"limit": long_message}]})
        self.assertIsNotNone(res.limit)
        self.assertLessEqual(len(res.limit["raw"]), 500)
        self.assertTrue(res.limit["raw"].endswith("..."))


    def test_uncharged_call_id_formatting_with_complex_label(self):
        res = self.call(
            {"code": [{"structured_output": {"status": "done"}}]},
            label="retry%s%d",
            charge=False,
        )
        self.assertTrue(res.ok)
        meta_path = os.path.join(self.run_.path(res.audit_dir), "meta.json")
        with open(meta_path) as f:
            meta = json.load(f)
        self.assertIn("retry%s%d", meta["call_id"])


class TestRunnerLimits(PipeBase):
    def test_fake_cli_limit_does_not_increment_attempt_or_consume_stage_retry(self):
        self.setup_tickets(
            [ticket(1)],
            {"code": [
                {"limit": "Rate limit reached. Resets in 0s.", "session_id": "sess-init"},
                resp(CODE, "sess-init", write={"f.txt": "1"}),
             ],
             "review": [resp(REV)],
             "validate": [resp(val())],
             "router": [{"exit": 1}]}
        )
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["attempt"], 1)
        self.assertEqual(self.tstate(1)["status"], "closed")

        with open(self.run_.path("events.jsonl")) as f:
            events = [json.loads(line) for line in f]
        stage_retries = [e for e in events if e.get("kind") == "stage_retry"]
        self.assertEqual(len(stage_retries), 0)
        limit_hits = [e for e in events if e.get("kind") == "limit_hit"]
        self.assertEqual(len(limit_hits), 1)

        limit_retry_meta = self.run_.path("tickets/T-001/attempt-1/limit-retry/meta.json")
        self.assertTrue(os.path.exists(limit_retry_meta))
        with open(limit_retry_meta) as f:
            meta = json.load(f)
        self.assertEqual(meta["label"], "limit-retry")
        self.assertEqual(meta["attempt"], 1)

        self.assertEqual(meta["call_id"], "c0001-limit-retry")

        # 4 charged calls: code (attempt 1), review, validate, router. limit-retry is uncharged.
        self.assertEqual(self.run_.read_json("run.json")["agent_calls"], 4)

    def test_same_call_reissued_after_wait_with_same_session_id(self):
        self.setup_tickets(
            [ticket(1)],
            {"code": [
                {"limit": {"is_error": True, "result": "Quota exceeded. Resets in 0s.", "session_id": "sess-limit-test"}},
                resp(CODE, "sess-limit-test", write={"f.txt": "1"}),
             ],
             "review": [resp(REV)],
             "validate": [resp(val())],
             "router": [{"exit": 1}]}
        )
        self.assertEqual(self.go(), "done")
        limit_retry_cmd = self.run_.path("tickets/T-001/attempt-1/limit-retry/command.json")
        self.assertTrue(os.path.exists(limit_retry_cmd))
        with open(limit_retry_cmd) as f:
            cmd = json.load(f)
        self.assertIn("--resume", cmd)
        self.assertEqual(cmd[cmd.index("--resume") + 1], "sess-limit-test")

    def test_kill_and_resume_runner_mid_wait_keeps_wait(self):
        self.setup_tickets(
            [ticket(1)],
            {"code": [
                {"limit": "Rate limit reached. Resets in 10s."},
                resp(CODE, write={"f.txt": "1"}),
             ],
             "review": [resp(REV)],
             "validate": [resp(val())],
             "router": [{"exit": 1}]}
        )
        import threading
        import time
        runner_thread = threading.Thread(target=self.go)
        runner_thread.daemon = True
        runner_thread.start()

        for _ in range(100):
            info = self.run_.read_json("run.json")
            if info.get("waiting_limit"):
                break
            time.sleep(0.01)

        self.assertIsNotNone(self.run_.read_json("run.json").get("waiting_limit"))
        scheduler.stop(self.run_)
        runner_thread.join(timeout=5)
        self.assertFalse(runner_thread.is_alive())

        info = self.run_.read_json("run.json")
        self.assertEqual(info.get("state"), "stopped")
        self.assertIsNotNone(info.get("waiting_limit"))
        self.assertEqual(info["waiting_limit"]["consecutive_waits"], 1)

        # recover preserves wait
        scheduler.recover(self.run_)
        info_rec = self.run_.read_json("run.json")
        self.assertIsNotNone(info_rec.get("waiting_limit"))

        # Advance time: expire wait
        past_iso = (datetime.datetime.now().astimezone() - datetime.timedelta(seconds=5)).isoformat(timespec="seconds")
        self.run_.update(waiting_limit={"resume_at": past_iso, "raw": "limit wait done", "consecutive_waits": 1, "ticket": "T-001"})

        self.assertEqual(self.go(), "done")
        self.assertIsNone(self.run_.read_json("run.json").get("waiting_limit"))
        self.assertEqual(self.tstate(1)["status"], "closed")

    def test_fake_cli_always_reports_limit_escalates_once_after_max_waits_with_raw_text(self):
        cfg = config.deep_merge(self.run_.cfg, {
            "limits": {"max_waits": 2, "backoff_s": 0.01, "max_wait_s": 0.05}
        })
        self.run_.update(config=cfg)
        self.setup_tickets(
            [ticket(1)],
            {"code": [{"limit": "Raw rate limit: please wait 1 hour"}]}
        )
        self.assertEqual(self.go(), "needs_user")
        self.assertEqual(self.tstate(1)["status"], "blocked")
        self.assertEqual(self.tstate(1)["attempt"], 1)

        esc = self.run_.read_json("escalations.json")
        self.assertEqual(len(esc), 1)
        self.assertEqual(esc[0]["ticket"], "T-001")
        self.assertIn("Raw rate limit: please wait 1 hour", esc[0]["reason"])

    def test_yoke_stop_during_wait_works(self):
        self.setup_tickets(
            [ticket(1)],
            {"code": [{"limit": "Rate limit reached. Resets in 10s."}]}
        )
        import threading
        import time
        runner_thread = threading.Thread(target=self.go)
        runner_thread.daemon = True
        runner_thread.start()

        for _ in range(100):
            info = self.run_.read_json("run.json")
            if info.get("waiting_limit"):
                break
            time.sleep(0.01)

        self.assertIsNotNone(self.run_.read_json("run.json").get("waiting_limit"))

        scheduler.stop(self.run_)
        runner_thread.join(timeout=5)
        self.assertFalse(runner_thread.is_alive(), "runner thread did not stop within timeout")

        info = self.run_.read_json("run.json")
        self.assertEqual(info.get("state"), "stopped")
        self.assertIsNotNone(info.get("waiting_limit"))

    def test_unblock_after_max_waits_escalation_allows_new_wait_cycle(self):
        cfg = config.deep_merge(self.run_.cfg, {
            "limits": {"max_waits": 1, "backoff_s": 0.01, "max_wait_s": 0.05}
        })
        self.run_.update(config=cfg)
        self.setup_tickets(
            [ticket(1)],
            {"code": [
                {"limit": "Rate limit round 1"},
                {"limit": "Rate limit round 1 again"},
                {"limit": "Rate limit round 2"},
                resp(CODE, write={"f.txt": "1"}),
             ],
             "review": [resp(REV)],
             "validate": [resp(val())],
             "router": [{"exit": 1}]}
        )
        # Round 1: first limit waits, second reaches max_waits (1) and escalates
        self.assertEqual(self.go(), "needs_user")
        self.assertEqual(self.tstate(1)["status"], "blocked")

        # Unblock ticket: limit_waits must be cleared
        scheduler.unblock(self.run_, "T-001")
        self.assertEqual(self.tstate(1)["status"], "pending")

        # Round 2: next limit hit must enter a fresh wait cycle and then succeed
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["status"], "closed")

        with open(self.run_.path("events.jsonl")) as f:
            events = [json.loads(line) for line in f]
        limit_hits = [e for e in events if e.get("kind") == "limit_hit"]
        self.assertEqual(len(limit_hits), 2)

    def test_render_status_shows_waiting_limit(self):
        self.run_.update(waiting_limit={"resume_at": "2026-10-20T18:00:00Z", "ticket": "T-001"})
        rendered = self.run_.render_status()
        self.assertIn("waiting for rate limit until 2026-10-20T18:00:00Z (T-001)", rendered)

    def test_recover_resumes_in_progress_wait_and_caps_max_wait(self):
        # Set waiting_limit far in future exceeding max_wait_s (default 7200s)
        self.addCleanup(pipeline.clear_pause_state, self.run_)
        far_future = (datetime.datetime.now().astimezone() + datetime.timedelta(seconds=100000)).isoformat(timespec="seconds")
        self.run_.update(waiting_limit={"resume_at": far_future, "raw": "limit hit", "consecutive_waits": 1, "ticket": "T-001"})
        scheduler.recover(self.run_)
        wl = self.run_.read_json("run.json")["waiting_limit"]
        self.assertIsNotNone(wl)
        pause = pipeline.get_pause_state(self.run_)
        self.assertFalse(pause.unpaused.is_set())
        self.assertEqual(pause.active_waiters, 1)

        rec_dt = limits._parse_iso(wl["resume_at"])
        now_dt = datetime.datetime.now().astimezone()
        delta = (rec_dt - now_dt).total_seconds()
        self.assertLessEqual(delta, 7205)


if __name__ == "__main__":
    unittest.main()


