import json
import os
import tempfile
import time
import unittest

from yoke import adapters, calls, config, schema, state

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE_CLI = os.path.join(HERE, "fake_cli.py")

SCHEMA = {"type": "object", "required": ["status"], "additionalProperties": False,
          "properties": {"status": {"type": "string", "enum": ["done", "blocked"]}}}

# Real outputs recorded in M0 (PLAN section 1), trimmed.
AGY_OK = {"conversation_id": "f2ddb28b-1", "status": "SUCCESS", "response": "noise",
          "duration_seconds": 24.37, "num_turns": 2, "structured_output": {"ok": True},
          "usage": {"input_tokens": 23414, "output_tokens": 1926, "total_tokens": 25340}}
AGY_DENIED = {"conversation_id": "c2", "status": "SUCCESS", "response": "",
              "denied_actions": [{"action": "shell", "display_name": "echo hi"}]}
CLAUDE_OK = {"type": "result", "subtype": "success", "is_error": False, "result": "text",
             "session_id": "sess-1", "structured_output": {"ok": True}, "total_cost_usd": 0.01,
             "usage": {"input_tokens": 3}, "modelUsage": {"claude-sonnet-5-5": {}}}


def slurp(path):
    with open(path) as f:
        return f.read()


class Tmp(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = os.path.realpath(self._td.name)

    def write(self, rel, obj):
        p = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            json.dump(obj, f)
        return p


class TestConfig(Tmp):
    def test_merge_order(self):
        home, repo = os.path.join(self.tmp, "home"), os.path.join(self.tmp, "repo")
        self.write("home/config.json", {"max_tickets": 10, "timeouts_s": {"code": 1}})
        self.write("repo/.yoke.json", {"max_tickets": 20, "parallel": 4})
        cfg = config.load(repo, home=home, overrides={"parallel": 2})
        self.assertEqual(cfg["max_tickets"], 20)  # repo beats home
        self.assertEqual(cfg["parallel"], 2)  # overrides beat repo
        self.assertEqual(cfg["timeouts_s"]["code"], 1)  # deep merge
        self.assertEqual(cfg["timeouts_s"]["qa"], 1800)  # untouched default
        self.assertEqual(cfg["max_attempts_per_ticket"], 3)

    def test_invalid_json_names_file(self):
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(repo)
        with open(os.path.join(repo, ".yoke.json"), "w") as f:
            f.write("{nope")
        with self.assertRaisesRegex(ValueError, r"\.yoke\.json"):
            config.load(repo, home=self.tmp)

    def test_agent_for(self):
        cfg = config.load(home=self.tmp)
        self.assertEqual(config.agent_for(cfg, "code"), "agy")
        self.assertEqual(config.agent_for(cfg, "validate"), "claude")
        cfg = config.deep_merge(cfg, {"roles": {"by_role": {"docs": "gemini"}}})
        self.assertEqual(config.agent_for(cfg, "docs"), "gemini")

    def test_agent_for_review(self):
        cfg = config.load(home=self.tmp)
        self.assertEqual(config.agent_for(cfg, "code"), "agy")
        self.assertEqual(config.agent_for(cfg, "review"), "claude")

        cfg_claude = config.deep_merge(cfg, {"roles": {"by_role": {"code": "claude"}}})
        self.assertEqual(config.agent_for(cfg_claude, "code"), "claude")
        self.assertEqual(config.agent_for(cfg_claude, "review"), "agy")

        cfg_explicit = config.deep_merge(cfg, {"roles": {"by_role": {"review": "secondary"}}})
        self.assertEqual(config.agent_for(cfg_explicit, "review"), "agy")
        cfg_explicit2 = config.deep_merge(cfg, {"roles": {"by_role": {"review": "claude"}}})
        self.assertEqual(config.agent_for(cfg_explicit2, "review"), "claude")

        cfg_custom = config.deep_merge(cfg, {"roles": {"by_role": {"code": "custom"}}})
        self.assertEqual(config.agent_for(cfg_custom, "review"), "claude")
        cfg_custom_pri = config.deep_merge(cfg, {"roles": {"primary": "custom", "secondary": "agy",
                                                           "by_role": {"code": "custom"}}})
        self.assertEqual(config.agent_for(cfg_custom_pri, "review"), "agy")

    def test_save_roles_review_auto(self):
        p = os.path.join(self.tmp, "c.json")
        with self.assertRaises(ValueError):
            config.save_roles(p, mapping={"code": "auto"})
        res = config.save_roles(p, mapping={"review": "auto"})
        self.assertEqual(res["by_role"]["review"], "auto")

    def test_model_for_tiers(self):
        cfg = {
            "roles": {"by_role": {"plan_review": "secondary", "code": "secondary", "qa": "secondary", "docs": "secondary"}},
            "models": {
                "claude": {
                    "plan": {"model": "claude-sonnet-5-5", "effort": "high"},
                    "cheap": {"model": "claude-haiku-5-5", "effort": "low"},
                }
            }
        }
        self.assertEqual(config.model_for(cfg, "claude", "plan_review"), ("claude-sonnet-5-5", "high"))
        self.assertEqual(config.model_for(cfg, "claude", "requirements"), ("claude-sonnet-5-5", "high"))
        self.assertEqual(config.model_for(cfg, "claude", "planner"), ("claude-sonnet-5-5", "high"))
        self.assertEqual(config.model_for(cfg, "claude", "wayfinder"), ("claude-sonnet-5-5", "high"))
        self.assertEqual(config.model_for(cfg, "claude", "code"), ("claude-haiku-5-5", "low"))
        self.assertEqual(config.model_for(cfg, "claude", "qa"), ("claude-haiku-5-5", "low"))
        self.assertEqual(config.model_for(cfg, "claude", "docs"), ("claude-haiku-5-5", "low"))
        self.assertEqual(config.model_for(cfg, "claude", "router"), (None, None))
        self.assertEqual(config.model_for(cfg, "claude", "unknown_role"), (None, None))
        self.assertEqual(config.model_for(cfg, "unknown_agent", "code"), (None, None))
        self.assertEqual(config.model_for(cfg, adapters.FakeAdapter(FAKE_CLI), "code"), (None, None))
        self.assertEqual(config.model_for({"models": {}}, "claude", "code"), (None, None))
        self.assertEqual(config.model_for({}, "claude", "code"), (None, None))
        self.assertEqual(config.model_for(None, "claude", "code"), (None, None))
        cfg_fake = {"models": {"fake": {"cheap": {"model": "m-fake", "effort": "low"}}}}
        self.assertEqual(config.model_for(cfg_fake, adapters.FakeAdapter(FAKE_CLI), "code"), ("m-fake", "low"))
        cfg_partial = {"models": {"claude": {"cheap": {"model": "m-only"}}}}
        self.assertEqual(config.model_for(cfg_partial, "claude", "code"), ("m-only", None))
        self.assertEqual(config.model_for({"models": {"claude": None}}, "claude", "code"), (None, None))
        self.assertEqual(config.model_for({"models": {"claude": {"cheap": "invalid"}}}, "claude", "code"), (None, None))


    def test_save_models_set_and_reset(self):
        p = os.path.join(self.tmp, "cfg.json")
        res = config.save_models(p, sets={"claude": {"plan": {"model": "m-plan", "effort": "high"},
                                                      "cheap": {"model": "m-cheap", "effort": "low"}}})
        self.assertEqual(res["claude"]["plan"]["model"], "m-plan")
        self.assertEqual(res["claude"]["cheap"]["effort"], "low")

        # Reset specific tier
        res2 = config.save_models(p, reset=[("claude", "plan")])
        self.assertNotIn("plan", res2.get("claude", {}))
        self.assertIn("cheap", res2.get("claude", {}))

        # Reset entire agent
        res3 = config.save_models(p, reset=["claude"])
        self.assertNotIn("claude", res3)
        disk_data = json.loads(slurp(p))
        self.assertEqual(disk_data.get("models"), res3)

    def test_routing_review(self):
        cfg = config.load(home=self.tmp)
        routing = cfg["routing"]
        for typ in ("feature", "bugfix", "refactor", "chore", "spike"):
            stages = routing[typ]["stages"]
            self.assertIn("review", stages)
            self.assertEqual(stages.index("review"), stages.index("code") + 1)
        for typ in ("test", "docs", "research"):
            self.assertNotIn("review", routing[typ]["stages"])

    def test_defaults_review(self):
        cfg = config.load(home=self.tmp)
        self.assertEqual(cfg["timeouts_s"]["review"], 1200)
        self.assertEqual(cfg["max_review_rounds"], 2)

    def test_setup_review_output_and_warning(self):
        import contextlib
        import io
        from unittest import mock
        from yoke import cli
        with mock.patch.dict(os.environ, {"YOKE_HOME": self.tmp}), \
             mock.patch("yoke.cli.doctor", return_value=[]):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = cli.main(["setup"])
            self.assertEqual(rc, 0)
            text = out.getvalue()
            self.assertRegex(text, r"review\s+claude")
            self.assertNotIn("warning", text.lower())

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = cli.main(["setup", "--map", "review=agy"])
            self.assertEqual(rc, 0)
            text = out.getvalue()
            self.assertRegex(text, r"review\s+agy")
            self.assertIn("warning", text.lower())


class TestSchema(unittest.TestCase):
    def test_valid_and_invalid(self):
        self.assertEqual(schema.validate({"status": "done"}, SCHEMA), [])
        self.assertTrue(schema.validate({"status": "nope"}, SCHEMA))
        self.assertTrue(schema.validate({}, SCHEMA))
        self.assertTrue(schema.validate({"status": "done", "x": 1}, SCHEMA))
        self.assertTrue(schema.validate([], SCHEMA))

    def test_array_string_rules(self):
        s = {"type": "object", "properties": {
            "ids": {"type": "array", "minItems": 1, "items": {"type": "string", "pattern": r"^T-\d{3}$"}},
            "t": {"type": "string", "minLength": 1}}}
        self.assertEqual(schema.validate({"ids": ["T-001"], "t": "x"}, s), [])
        self.assertTrue(schema.validate({"ids": []}, s))
        self.assertTrue(schema.validate({"ids": ["T-1"]}, s))
        self.assertTrue(schema.validate({"t": ""}, s))

    def test_bool_is_not_number(self):
        self.assertTrue(schema.validate(True, {"type": "integer"}))
        self.assertEqual(schema.validate(3, {"type": ["integer", "null"]}), [])
        self.assertEqual(schema.validate(None, {"type": ["integer", "null"]}), [])


class TestState(Tmp):
    def mkrun(self, repo="repo"):
        r = os.path.join(self.tmp, repo)
        os.makedirs(r, exist_ok=True)
        return state.Run.create(r, config.load(home=self.tmp), home=self.tmp)

    def test_create_load_and_latest(self):
        a = self.mkrun()
        b = self.mkrun()
        self.assertNotEqual(a.run_id, b.run_id)
        info = state.Run.load(a.run_id, home=self.tmp).read_json("run.json")
        self.assertEqual(info["state"], "init")
        self.assertEqual(info["config"]["max_tickets"], 40)
        self.assertEqual(state.Run.latest_for_repo(os.path.join(self.tmp, "repo"), home=self.tmp).run_id,
                         b.run_id)
        self.assertIsNone(state.Run.latest_for_repo(os.path.join(self.tmp, "other"), home=self.tmp))
        with self.assertRaises(FileNotFoundError):
            state.Run.load("nope", home=self.tmp)

    def test_atomic_write_keeps_old_on_failure(self):
        run = self.mkrun()
        run.write_json("x/a.json", {"v": 1})
        with self.assertRaises(TypeError):
            run.write_json("x/a.json", {"v": object()})
        self.assertEqual(run.read_json("x/a.json"), {"v": 1})
        self.assertEqual(os.listdir(run.path("x")), ["a.json"])  # no tmp left behind

    def test_events_and_status(self):
        run = self.mkrun()
        run.event("hello", n=1)
        lines = slurp(run.path("events.jsonl")).splitlines()
        self.assertEqual(json.loads(lines[-1])["kind"], "hello")
        run.write_json("tickets.json", {"tickets": [{"id": "T-001"}, {"id": "T-002"}]})
        run.write_json("tickets/T-001/state.json", {"status": "closed"})
        text = run.render_status()
        self.assertIn("tickets 2 (closed 1, pending 1)", text)
        self.assertEqual(run.read_json("status.json")["tickets_total"], 2)


class TestAdapterCmds(Tmp):
    def spec(self, **kw):
        p = self.write("schema.json", SCHEMA)
        return adapters.CallSpec(prompt="do it", schema_path=p, cwd=self.tmp, **kw)

    def test_claude_read(self):
        cmd = adapters.ClaudeAdapter().build_cmd(self.spec())
        self.assertEqual(cmd[:3], ["claude", "-p", "do it"])  # prompt right after -p
        self.assertEqual(json.loads(cmd[cmd.index("--json-schema") + 1]), SCHEMA)
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "plan")
        self.assertNotIn("acceptEdits", cmd)
        self.assertNotIn("--disallowedTools", cmd)

    def test_claude_write_resume_model(self):
        cmd = adapters.ClaudeAdapter().build_cmd(self.spec(
            mode="write", resume_session="s1", model="m", effort="low",
            allowed_tools=("Read", "Edit"), extra_dirs=("/d",)))
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(cmd[cmd.index("--allowedTools") + 1], "Read Edit")
        i = cmd.index("--disallowedTools")
        self.assertEqual(cmd[i + 1:i + 3], ["Bash(git push:*)", "Bash(git commit:*)"])
        for pair in (["--resume", "s1"], ["--model", "m"], ["--effort", "low"], ["--add-dir", "/d"]):
            self.assertIn(pair[0], cmd)
            self.assertEqual(cmd[cmd.index(pair[0]) + 1], pair[1])

    def test_agy_modes(self):
        a = adapters.AgyAdapter()
        read = a.build_cmd(self.spec())
        self.assertEqual(read[:3], ["agy", "-p", "do it"])
        self.assertEqual(read[read.index("--mode") + 1], "plan")
        self.assertIn("--sandbox", read)
        self.assertEqual(read[read.index("--json-schema") + 1], self.tmp + "/schema.json")
        write = a.build_cmd(self.spec(mode="write", resume_session="c1"))
        self.assertEqual(write[write.index("--mode") + 1], "accept-edits")
        self.assertIn("--sandbox", write)
        self.assertEqual(write[write.index("--conversation") + 1], "c1")
        self.assertNotIn("auto", write)

    def test_never_skip_permissions(self):
        for a in (adapters.ClaudeAdapter(), adapters.AgyAdapter(), adapters.FakeAdapter(FAKE_CLI)):
            for mode in ("read", "write"):
                self.assertNotIn("--dangerously-skip-permissions", a.build_cmd(self.spec(mode=mode)))
        with self.assertRaises(AssertionError):
            adapters._guard(["x", "--dangerously-skip-permissions"])


class TestAdapterParse(unittest.TestCase):
    def test_claude_ok(self):
        r = adapters.ClaudeAdapter().parse(json.dumps(CLAUDE_OK), 0)
        self.assertTrue(r.ok)
        self.assertEqual((r.session_id, r.observed_model, r.cost), ("sess-1", "claude-sonnet-5-5", 0.01))
        self.assertEqual(r.structured_output, {"ok": True})

    def test_claude_errors(self):
        a = adapters.ClaudeAdapter()
        self.assertIn("claude error", a.parse(json.dumps(dict(CLAUDE_OK, is_error=True)), 0).error)
        no_so = {k: v for k, v in CLAUDE_OK.items() if k != "structured_output"}
        self.assertEqual(a.parse(json.dumps(no_so), 0).error, "no structured_output")
        self.assertIn("not valid JSON", a.parse("garbage", 0).error)
        self.assertIn("exit 1", a.parse("", 1).error)

    def test_agy_ok(self):
        r = adapters.AgyAdapter().parse(json.dumps(AGY_OK), 0)
        self.assertTrue(r.ok)
        self.assertEqual((r.session_id, r.observed_model), ("f2ddb28b-1", "unreported"))

    def test_agy_denial_is_error(self):
        r = adapters.AgyAdapter().parse(json.dumps(AGY_DENIED), 0)
        self.assertFalse(r.ok)
        self.assertIn("permission denied: echo hi", r.error)

    def test_agy_missing_structured_output_is_error(self):
        obj = {k: v for k, v in AGY_OK.items() if k != "structured_output"}
        self.assertEqual(adapters.AgyAdapter().parse(json.dumps(obj), 0).error, "no structured_output")
        self.assertIn("status FAILED", adapters.AgyAdapter().parse(json.dumps(dict(AGY_OK, status="FAILED")), 0).error)


class TestRunCall(Tmp):
    def setUp(self):
        super().setUp()
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(repo)
        cfg = config.load(home=self.tmp, overrides={"kill_grace_s": 1})
        self.run_ = state.Run.create(repo, cfg, home=self.tmp)
        self.script = self.write("script.json", {})
        self.schema_path = self.write("schema.json", SCHEMA)
        self.adapter = adapters.FakeAdapter(FAKE_CLI)

    def call(self, script, stage="code", timeout=30, **kw):
        with open(self.script, "w") as f:
            json.dump(script, f)
        spec = adapters.CallSpec(prompt="task", schema_path=self.schema_path, cwd=self.tmp,
                                 timeout_s=timeout, env={"FAKE_SCRIPT": self.script})
        return calls.run_call(self.run_, self.adapter, spec, stage=stage, **kw)

    def test_success_writes_audit(self):
        res = self.call({"code": [{"structured_output": {"status": "done"}, "session_id": "s1"}]},
                        ticket="T-001", attempt=2)
        self.assertTrue(res.ok)
        self.assertTrue(res.schema_valid)
        d = self.run_.path("tickets/T-001/attempt-2/code")
        self.assertEqual(res.audit_dir, d)
        for f in ("prompt.md", "command.json", "stdout.log", "stderr.log", "result.json", "meta.json"):
            self.assertTrue(os.path.exists(os.path.join(d, f)), f)
        self.assertEqual(slurp(os.path.join(d, "prompt.md")), "task")
        cmd = self.run_.read_json("tickets/T-001/attempt-2/code/command.json")
        self.assertIn(os.path.join(d, "prompt.md"), cmd)  # prompt replaced by its path
        self.assertNotIn("task", cmd)
        meta = self.run_.read_json("tickets/T-001/attempt-2/code/meta.json")
        self.assertEqual((meta["stage"], meta["ticket"], meta["attempt"], meta["exit_code"]),
                         ("code", "T-001", 2, 0))
        self.assertEqual((meta["schema_valid"], meta["timed_out"], meta["observed"]["session_id"]),
                         (True, False, "s1"))
        self.assertEqual(self.run_.read_json("run.json")["agent_calls"], 1)
        kinds = [json.loads(l)["kind"] for l in slurp(self.run_.path("events.jsonl")).splitlines()]
        self.assertEqual(kinds[-2:], ["call_start", "call_end"])

    def test_plan_review_dir(self):
        res = self.call({"plan_review": [{"structured_output": {"status": "done"}}]},
                        stage="plan_review", attempt=3)
        self.assertEqual(res.audit_dir, self.run_.path("plan-review/round-3"))

    def test_wayfinder_dir(self):
        res = self.call({"wayfinder": [{"structured_output": {"status": "done"}}]},
                        stage="wayfinder", attempt=2)
        self.assertEqual(res.audit_dir, self.run_.path("chart/round-2"))

    def test_schema_reask_succeeds(self):
        res = self.call({"code": [{"structured_output": {"status": "bogus"}, "session_id": "s1"},
                                  {"structured_output": {"status": "done"}, "session_id": "s1"}]},
                        ticket="T-001", attempt=1)
        self.assertTrue(res.ok)
        self.assertTrue(res.audit_dir.endswith("code/reask"))
        self.assertEqual(self.run_.read_json("run.json")["agent_calls"], 2)
        ask = slurp(os.path.join(res.audit_dir, "prompt.md"))
        self.assertIn("bogus", ask)
        cmd = self.run_.read_json("tickets/T-001/attempt-1/code/reask/command.json")
        self.assertEqual(cmd[cmd.index("--resume") + 1], "s1")  # same session
        first = self.run_.read_json("tickets/T-001/attempt-1/code/meta.json")
        self.assertFalse(first["schema_valid"])

    def test_schema_reask_only_once(self):
        bad = {"structured_output": {"status": "bogus"}, "session_id": "s1"}
        res = self.call({"code": [bad, bad, {"structured_output": {"status": "done"}}]})
        self.assertFalse(res.ok)
        self.assertIn("schema:", res.error)
        self.assertEqual(self.run_.read_json("run.json")["agent_calls"], 2)

    def test_permission_denial_retried_once_without_shell(self):
        res = self.call({"code": [{"denied": "ls -la", "session_id": "s1"},
                                  {"structured_output": {"status": "done"}, "session_id": "s1"}]},
                        ticket="T-001", attempt=1)
        self.assertTrue(res.ok)
        self.assertTrue(res.audit_dir.endswith("code/reask"))
        ask = slurp(os.path.join(res.audit_dir, "prompt.md"))
        self.assertIn("permission denied: ls -la", ask)
        self.assertIn("Do not run any command", ask)
        cmd = self.run_.read_json("tickets/T-001/attempt-1/code/reask/command.json")
        self.assertEqual(cmd[cmd.index("--resume") + 1], "s1")

    def test_permission_denial_twice_fails(self):
        d = {"denied": "ls", "session_id": "s1"}
        res = self.call({"code": [d, d, {"structured_output": {"status": "done"}}]})
        self.assertFalse(res.ok)
        self.assertIn("permission denied", res.error)
        self.assertEqual(self.run_.read_json("run.json")["agent_calls"], 2)

    def test_nonzero_exit_no_reask(self):
        res = self.call({"code": [{"exit": 3}]})
        self.assertFalse(res.ok)
        self.assertEqual(res.exit_code, 3)
        self.assertEqual(self.run_.read_json("run.json")["agent_calls"], 1)

    def test_timeout_kills_process_group(self):
        pidfile = os.path.join(self.tmp, "child.pid")
        t0 = time.time()
        res = self.call({"code": [{"sleep": 999, "child_pidfile": pidfile}]}, timeout=2)
        self.assertLess(time.time() - t0, 20)
        self.assertTrue(res.timed_out)
        self.assertIn("timeout", res.error)
        self.assertTrue(self.run_.read_json(os.path.relpath(res.audit_dir, self.run_.dir) + "/meta.json")
                        ["timed_out"])
        child = int(slurp(pidfile))
        for _ in range(40):
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            self.fail("grandchild survived timeout kill")

    def test_missing_executable(self):
        spec = adapters.CallSpec(prompt="t", schema_path=self.schema_path, cwd=self.tmp)

        class Missing(adapters.Adapter):
            name = "missing"

            def build_cmd(self, call):
                return ["definitely-not-a-binary-xyz"]

        res = calls.run_call(self.run_, Missing(), spec, stage="code")
        self.assertFalse(res.ok)
        self.assertIn("cannot start", res.error)

    def test_run_call_model_fill_and_override(self):
        cfg = config.deep_merge(self.run_.cfg, {
            "models": {"fake": {"cheap": {"model": "m-cfg", "effort": "low"}}}
        })
        self.run_.update(config=cfg)

        res = self.call({"code": [{"structured_output": {"status": "done"}}]}, ticket="T-001", attempt=1)
        self.assertTrue(res.ok)
        cmd = self.run_.read_json("tickets/T-001/attempt-1/code/command.json")
        self.assertIn("--model", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "m-cfg")
        self.assertIn("--effort", cmd)
        self.assertEqual(cmd[cmd.index("--effort") + 1], "low")

        with open(self.script, "w") as f:
            json.dump({"code": [{"structured_output": {"status": "done"}}]}, f)
        if os.path.exists(self.script + ".pos"):
            os.remove(self.script + ".pos")
        spec = adapters.CallSpec(prompt="task", schema_path=self.schema_path, cwd=self.tmp,
                                 timeout_s=30, model="m-override", effort="high",
                                 env={"FAKE_SCRIPT": self.script})
        res2 = calls.run_call(self.run_, self.adapter, spec, stage="code", ticket="T-001", attempt=2)
        self.assertTrue(res2.ok)
        cmd2 = self.run_.read_json("tickets/T-001/attempt-2/code/command.json")
        self.assertEqual(cmd2[cmd2.index("--model") + 1], "m-override")
        self.assertEqual(cmd2[cmd2.index("--effort") + 1], "high")

        # Partial override: model specified, effort filled from config
        with open(self.script, "w") as f:
            json.dump({"code": [{"structured_output": {"status": "done"}}]}, f)
        if os.path.exists(self.script + ".pos"):
            os.remove(self.script + ".pos")
        spec_partial = adapters.CallSpec(prompt="task", schema_path=self.schema_path, cwd=self.tmp,
                                         timeout_s=30, model="m-partial",
                                         env={"FAKE_SCRIPT": self.script})
        res3 = calls.run_call(self.run_, self.adapter, spec_partial, stage="code", ticket="T-001", attempt=3)
        self.assertTrue(res3.ok)
        cmd3 = self.run_.read_json("tickets/T-001/attempt-3/code/command.json")
        self.assertEqual(cmd3[cmd3.index("--model") + 1], "m-partial")
        self.assertEqual(cmd3[cmd3.index("--effort") + 1], "low")

    def test_run_call_model_error_fallback(self):
        cfg = config.deep_merge(self.run_.cfg, {
            "models": {"fake": {"cheap": {"model": "bad-model"}}}
        })
        self.run_.update(config=cfg)

        script = {
            "code": [
                {"stderr": "Error: model bad-model not found", "exit": 1},
                {"structured_output": {"status": "done"}}
            ]
        }
        res = self.call(script, ticket="T-001", attempt=1)
        self.assertTrue(res.ok)
        self.assertTrue(res.audit_dir.endswith("code/fallback"))

        base_dir = self.run_.path("tickets/T-001/attempt-1/code")
        self.assertTrue(os.path.exists(os.path.join(base_dir, "stdout.log")))
        self.assertTrue(os.path.exists(os.path.join(base_dir, "stderr.log")))
        self.assertIn("bad-model", slurp(os.path.join(base_dir, "stderr.log")))

        fallback_cmd = self.run_.read_json("tickets/T-001/attempt-1/code/fallback/command.json")
        self.assertNotIn("--model", fallback_cmd)

        events = [json.loads(l) for l in slurp(self.run_.path("events.jsonl")).splitlines()]
        fb_events = [e for e in events if e.get("kind") == "model_fallback"]
        self.assertEqual(len(fb_events), 1)
        self.assertEqual(fb_events[0]["model"], "bad-model")
        self.assertEqual(fb_events[0]["ticket"], "T-001")

    def test_run_call_model_error_in_res_error_fallback(self):
        cfg = config.deep_merge(self.run_.cfg, {
            "models": {"fake": {"cheap": {"model": "bad-model"}}}
        })
        self.run_.update(config=cfg)

        class ModelErrorAdapter(adapters.FakeAdapter):
            def parse(self, stdout, exit_code):
                res = super().parse(stdout, exit_code)
                if exit_code != 0:
                    res.error = "provider error: model not found"
                return res

        script = {
            "code": [
                {"exit": 1},
                {"structured_output": {"status": "done"}}
            ]
        }
        with open(self.script, "w") as f:
            json.dump(script, f)
        if os.path.exists(self.script + ".pos"):
            os.remove(self.script + ".pos")
        spec = adapters.CallSpec(prompt="task", schema_path=self.schema_path, cwd=self.tmp,
                                 timeout_s=30, env={"FAKE_SCRIPT": self.script})
        adapter = ModelErrorAdapter(FAKE_CLI)
        res = calls.run_call(self.run_, adapter, spec, stage="code", ticket="T-001", attempt=1)
        self.assertTrue(res.ok)
        self.assertTrue(res.audit_dir.endswith("code/fallback"))
        events = [json.loads(l) for l in slurp(self.run_.path("events.jsonl")).splitlines()]
        fb_events = [e for e in events if e.get("kind") == "model_fallback"]
        self.assertTrue(any(e.get("ticket") == "T-001" and e.get("stage") == "code" and e.get("model") == "bad-model" for e in fb_events))


    def test_run_call_no_fallback_on_non_model_error(self):
        cfg = config.deep_merge(self.run_.cfg, {
            "models": {"fake": {"cheap": {"model": "m-fail"}}}
        })
        self.run_.update(config=cfg)

        script = {
            "code": [
                {"stderr": "Syntax error in script", "exit": 1},
                {"structured_output": {"status": "done"}}
            ]
        }
        res = self.call(script, ticket="T-001", attempt=1)
        self.assertFalse(res.ok)
        self.assertFalse(res.audit_dir.endswith("fallback"))
        self.assertFalse(os.path.exists(self.run_.path("tickets/T-001/attempt-1/code/fallback")))

    def test_run_call_no_fallback_when_model_unset(self):
        script = {
            "code": [
                {"stderr": "Error: model bad-model not found", "exit": 1},
                {"structured_output": {"status": "done"}}
            ]
        }
        res = self.call(script, ticket="T-001", attempt=1)
        self.assertFalse(res.ok)
        self.assertFalse(os.path.exists(self.run_.path("tickets/T-001/attempt-1/code/fallback")))

    def test_run_call_no_fallback_on_timeout(self):
        cfg = config.deep_merge(self.run_.cfg, {
            "models": {"fake": {"cheap": {"model": "m-timeout"}}}
        })
        self.run_.update(config=cfg)

        script = {
            "code": [
                {"sleep": 999},
                {"structured_output": {"status": "done"}}
            ]
        }
        res = self.call(script, ticket="T-001", attempt=1, timeout=1)
        self.assertTrue(res.timed_out)
        self.assertFalse(os.path.exists(self.run_.path("tickets/T-001/attempt-1/code/fallback")))


if __name__ == "__main__":
    unittest.main()
