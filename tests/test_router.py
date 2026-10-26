import json
import os
import shutil
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from yoke import adapters, config, router, state

os.environ.setdefault("YOKE_OLLAMA_URL", "http://127.0.0.1:9")

FAKE_CLI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_cli.py")


class Stub(BaseHTTPRequestHandler):
    answers = {}

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        body = json.dumps({"answers": Stub.answers}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def dead_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def choice(c, conf):
    return {"type": "choice", "choice": c, "confidence": conf, "probabilities": {c: conf}}


class RouterCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(repo)
        self.server = HTTPServer(("127.0.0.1", 0), Stub)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.cfg = config.load(home=self.tmp, overrides={
            "kill_grace_s": 1, "router": {"ollama_url": "http://127.0.0.1:%d" % self.server.server_port}})
        self.run_ = state.Run.create(repo, self.cfg, home=self.tmp)
        self.script = os.path.join(self.tmp, "script.json")
        self.adapter = adapters.FakeAdapter(FAKE_CLI)
        os.environ["FAKE_SCRIPT"] = self.script
        self.addCleanup(os.environ.pop, "FAKE_SCRIPT", None)

    def point_at_dead_port(self):
        self.run_.update(config=config.deep_merge(
            self.cfg, {"router": {"ollama_url": "http://127.0.0.1:%d" % dead_port()}}))

    def script_router(self, *responses):
        with open(self.script, "w") as f:
            json.dump({"router": list(responses)}, f)

    def events(self):
        with open(self.run_.path("events.jsonl")) as f:
            return [json.loads(l) for l in f if '"router"' in l]

    T = {"id": "T-001", "title": "Fix crash", "description": "crash on missing config", "exit_criteria": ["no crash"]}


class TestDecisions(RouterCase):
    def test_ollama_used_when_confident(self):
        Stub.answers = {"type": choice("bugfix", 0.97)}
        out = router.classify_ticket(self.run_, self.T, self.adapter)
        self.assertEqual((out["source"], out["result"]["type"]), ("ollama", "bugfix"))
        self.assertIsNone(out["fallback_reason"])
        self.assertEqual(self.events()[-1]["source"], "ollama")

    def test_ollama_down_uses_fallback_and_logs(self):
        self.point_at_dead_port()
        self.script_router({"structured_output": {"type": "refactor", "confidence": 0.9, "reason": "x"}})
        out = router.classify_ticket(self.run_, self.T, self.adapter)
        self.assertEqual((out["source"], out["result"]["type"]), ("fallback", "refactor"))
        self.assertIn("ollama failed", out["fallback_reason"])
        self.assertEqual(self.events()[-1]["source"], "fallback")

    def test_low_confidence_uses_fallback(self):
        Stub.answers = {"type": choice("bugfix", 0.4)}
        self.script_router({"structured_output": {"type": "chore", "confidence": 0.8, "reason": "x"}})
        out = router.classify_ticket(self.run_, self.T, self.adapter)
        self.assertEqual((out["source"], out["result"]["type"]), ("fallback", "chore"))
        self.assertIn("confidence", out["fallback_reason"])

    def test_other_choice_is_schema_invalid_so_fallback(self):
        Stub.answers = {"type": choice("other", 0.99)}
        self.script_router({"structured_output": {"type": "docs", "confidence": 0.8, "reason": "x"}})
        self.assertEqual(router.classify_ticket(self.run_, self.T, self.adapter)["source"], "fallback")

    def test_fallback_down_uses_default(self):
        self.point_at_dead_port()
        self.script_router({"exit": 1})
        out = router.classify_ticket(self.run_, self.T, self.adapter)
        self.assertEqual((out["source"], out["result"]["type"]), ("default", "feature"))
        self.assertIn("fallback agent failed", out["fallback_reason"])
        out = router.triage_failure(self.run_, "boom", self.adapter)
        self.assertEqual((out["result"]["category"], out["result"]["retryable"]), ("other", True))
        out = router.assert_check(self.run_, "x is true", "evidence", self.adapter)
        self.assertEqual((out["source"], out["result"]["reason"]), ("default", "unknown"))

    def test_triage_and_assert_from_ollama(self):
        Stub.answers = {"category": choice("test_failure", 0.95), "retryable": {"type": "noul", "noul": 0.36}}
        out = router.triage_failure(self.run_, "3 tests failed", self.adapter)
        self.assertEqual((out["source"], out["result"]["category"], out["result"]["retryable"]),
                         ("ollama", "test_failure", True))
        Stub.answers = {"a": {"type": "noul", "noul": 0.95}}
        out = router.assert_check(self.run_, "tests pass", "ok", self.adapter)
        self.assertEqual((out["source"], out["result"]["answer"]), ("ollama", True))

    def test_generative_defaults(self):
        self.script_router({"exit": 1})
        self.assertEqual(router.commit_message(self.run_, self.T, "", self.adapter)["result"]["subject"],
                         "T-001: Fix crash")
        self.assertEqual(router.summarize(self.run_, "a" * 900, self.adapter)["result"], "a" * 500)
        self.script_router({"structured_output": {"text": "short"}})
        self.assertEqual(router.summarize(self.run_, "long", self.adapter)["result"], "short")

    def test_health(self):
        self.point_at_dead_port()
        self.assertEqual(router.health(self.run_.cfg), {"ollama": False, "model": False})


class TestRoute(unittest.TestCase):
    def test_route_table_per_type(self):
        cfg = config.load(home=tempfile.mkdtemp())
        want = {"feature": "code review qa validate docs", "bugfix": "code review qa validate docs",
                "refactor": "code review qa validate", "test": "qa validate", "docs": "docs validate",
                "chore": "code review validate", "spike": "code review validate"}
        for typ, stages in want.items():
            r = router.route_ticket(cfg, {"type": typ})
            self.assertEqual(" ".join(s["stage"] for s in r["stages"]), stages)
        r = router.route_ticket(cfg, {"type": "test"})
        self.assertEqual([(s["stage"], s["agent"]) for s in r["stages"]], [("qa", "agy"), ("validate", "claude")])

    def test_per_stage_override(self):
        cfg = config.load(home=tempfile.mkdtemp(), overrides={
            "routing": {"chore": {"overrides": {"code": {"model": "m1", "effort": "low"}}}}})
        s = router.route_ticket(cfg, {"type": "chore"})["stages"][0]
        self.assertEqual((s["stage"], s["model"], s["effort"]), ("code", "m1", "low"))


class DecideTest(RouterCase):
    def test_decide_acts_above_threshold(self):
        Stub.answers = {"q": choice("a", 0.9)}

        def parse(ans):
            return ans["q"]["choice"], ans["q"]["confidence"]

        out = router.decide(self.run_.cfg, "classify_ticket", "state", {"q": "inst"}, parse, run=self.run_)
        self.assertEqual(out["result"], "a")
        self.assertEqual(out["source"], "ollama")
        self.assertEqual(out["confidence"], 0.9)
        self.assertIsNone(out["fallback_reason"])
        ev = self.events()[-1]
        self.assertEqual(ev.get("point"), "classify_ticket")
        self.assertEqual(ev.get("source"), "ollama")

    def test_decide_below_threshold_is_none(self):
        Stub.answers = {"q": choice("a", 0.5)}

        def parse(ans):
            return ans["q"]["choice"], ans["q"]["confidence"]

        out = router.decide(self.run_.cfg, "classify_ticket", "state", {"q": "inst"}, parse, run=self.run_)
        self.assertIsNone(out["result"])
        self.assertEqual(out["source"], "none")
        self.assertEqual(out["confidence"], 0.5)
        self.assertIn("confidence", out["fallback_reason"])

    def test_decide_per_point_threshold(self):
        cfg = config.deep_merge(self.run_.cfg, {"router": {"thresholds": {"x": 0.95}}})
        Stub.answers = {"q": choice("a", 0.9)}

        def parse(ans):
            return ans["q"]["choice"], ans["q"]["confidence"]

        out = router.decide(cfg, "x", "state", {"q": "inst"}, parse)
        self.assertIsNone(out["result"])
        self.assertEqual(out["source"], "none")
        self.assertEqual(out["confidence"], 0.9)

    def test_decide_ollama_down_is_none(self):
        cfg = config.deep_merge(self.run_.cfg, {"router": {"ollama_url": "http://127.0.0.1:%d" % dead_port()}})

        def parse(ans):
            return "a", 0.9

        out = router.decide(cfg, "x", "state", {}, parse)
        self.assertEqual(out["source"], "none")
        self.assertIsNone(out["result"])
        self.assertIn("ollama failed", out["fallback_reason"])

    def test_decide_parse_error_is_none(self):
        Stub.answers = {}

        def parse(ans):
            raise KeyError("missing")

        out = router.decide(self.run_.cfg, "x", "state", {}, parse)
        self.assertEqual(out["source"], "none")
        self.assertIsNone(out["result"])
        self.assertIn("ollama failed", out["fallback_reason"])

    def test_decide_exact_threshold_acts(self):
        Stub.answers = {"q": choice("exact", 0.7)}

        def parse(ans):
            return ans["q"]["choice"], ans["q"]["confidence"]

        out = router.decide(self.run_.cfg, "unknown_point", "state", {"q": "inst"}, parse, run=self.run_)
        self.assertEqual(out["result"], "exact")
        self.assertEqual(out["source"], "ollama")
        self.assertEqual(out["confidence"], 0.7)

    def test_decide_conf_none_is_none(self):
        Stub.answers = {"q": {"choice": "a"}}

        def parse(ans):
            return ans["q"]["choice"], None

        out = router.decide(self.run_.cfg, "x", "state", {}, parse)
        self.assertEqual(out["source"], "none")
        self.assertIsNone(out["result"])
        self.assertIn("confidence", out["fallback_reason"])

    def test_decide_no_run_no_event(self):
        Stub.answers = {"q": choice("a", 0.9)}

        def parse(ans):
            return ans["q"]["choice"], ans["q"]["confidence"]

        events_before = len(self.events())
        out = router.decide(self.run_.cfg, "x", "state", {}, parse, run=None)
        self.assertEqual(out["source"], "ollama")
        self.assertEqual(len(self.events()), events_before)

    def test_ollama_timeout_passed(self):
        from unittest import mock
        with mock.patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value.__enter__.return_value.read.return_value = b'{"answers": {}}'
            try:
                router._ollama(self.run_.cfg, "s", {}, timeout=42)
            except Exception:
                pass
            self.assertEqual(mock_open.call_args[1].get("timeout"), 42)

    def test_decide_timeout_passed(self):
        from unittest import mock
        with mock.patch("yoke.router._ollama") as mock_ollama:
            mock_ollama.return_value = {"q": choice("a", 0.9)}
            router.decide(self.run_.cfg, "x", "state", {}, lambda a: ("a", 0.9), timeout=15)
            self.assertEqual(mock_ollama.call_args[1].get("timeout"), 15)


def _exploding_adapter():
    adapter = mock.MagicMock()
    adapter.side_effect = AssertionError("adapter called in default mode")
    adapter.build_cmd.side_effect = AssertionError("adapter called in default mode")
    return adapter


class TestCommitMessage(RouterCase):
    def test_default_mode_no_agent(self):
        self.run_.update(config=config.deep_merge(
            self.cfg, {"router": {"commit_message": "default"}}))
        adapter = _exploding_adapter()

        ticket = {"id": "T-001", "title": "title"}
        out = router.commit_message(self.run_, ticket, adapter=adapter, summary="summary")
        self.assertEqual(out["source"], "default")
        self.assertEqual(out["result"]["subject"], "T-001: title")
        self.assertEqual(out["result"]["body"], "summary")
        self.assertIsNone(out["fallback_reason"])
        self.assertEqual(len(adapter.mock_calls), 0)

        long_ticket = {"id": "T-001", "title": "a" * 100}
        out_long = router.commit_message(self.run_, long_ticket, adapter=adapter, summary="summary")
        self.assertEqual(len(out_long["result"]["subject"]), 72)
        self.assertEqual(out_long["result"]["subject"], ("T-001: " + "a" * 100)[:72])

        # 72-char boundary tests
        ticket_72 = {"id": "T-001", "title": "b" * 65}
        out_72 = router.commit_message(self.run_, ticket_72, adapter=adapter, summary="s")
        self.assertEqual(len(out_72["result"]["subject"]), 72)
        self.assertEqual(out_72["result"]["subject"], "T-001: " + "b" * 65)

        ticket_73 = {"id": "T-001", "title": "b" * 66}
        out_73 = router.commit_message(self.run_, ticket_73, adapter=adapter, summary="s")
        self.assertEqual(len(out_73["result"]["subject"]), 72)
        self.assertEqual(out_73["result"]["subject"], ("T-001: " + "b" * 66)[:72])

        # Missing id or title
        out_empty = router.commit_message(self.run_, {}, adapter=adapter, summary="s")
        self.assertEqual(out_empty["result"]["subject"], ": ")

    def test_default_mode_diffstat_body(self):
        self.run_.update(config=config.deep_merge(
            self.cfg, {"router": {"commit_message": "default"}}))
        adapter = _exploding_adapter()

        ticket = {"id": "T-001", "title": "title"}
        out = router.commit_message(self.run_, ticket, diffstat="1 file changed", adapter=adapter, summary=None)
        self.assertEqual(out["source"], "default")
        self.assertEqual(out["result"]["subject"], "T-001: title")
        self.assertEqual(out["result"]["body"], "1 file changed")
        self.assertIsNone(out["fallback_reason"])

        # Empty string summary falls back to diffstat
        out_empty_summary = router.commit_message(self.run_, ticket, diffstat="2 files changed", adapter=adapter, summary="")
        self.assertEqual(out_empty_summary["source"], "default")
        self.assertEqual(out_empty_summary["result"]["subject"], "T-001: title")
        self.assertEqual(out_empty_summary["result"]["body"], "2 files changed")

    def test_agent_mode_unchanged(self):
        self.run_.update(config=config.deep_merge(
            self.cfg, {"router": {"commit_message": "agent"}}))
        self.script_router({"structured_output": {"subject": "agent subject", "body": "agent body"}})
        out = router.commit_message(self.run_, self.T, "", self.adapter)
        self.assertEqual(out["source"], "fallback")
        self.assertEqual(out["result"]["subject"], "agent subject")
        self.assertEqual(out["result"]["body"], "agent body")

    def test_repo_config_commit_message_and_deep_merge(self):
        repo_root = os.path.join(self.tmp, "repo_cfg")
        os.makedirs(repo_root)
        with open(os.path.join(repo_root, ".yoke.json"), "w") as f:
            json.dump({"roles": {"primary": "claude"}, "models": {},
                       "test_command": "python3 tests/run_parallel.py",
                       "router": {"commit_message": "default"}}, f)
        cfg = config.load(repo=repo_root, home=self.tmp)
        self.assertEqual(cfg["router"]["commit_message"], "default")
        self.assertEqual(cfg["router"]["llm_model"], "claude-haiku-5-5")
        self.assertEqual(cfg["test_command"], "python3 tests/run_parallel.py")
        self.assertIn("roles", cfg)
        self.assertIn("models", cfg)


if __name__ == "__main__":
    unittest.main()
