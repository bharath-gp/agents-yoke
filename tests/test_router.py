import json
import os
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from yoke import adapters, config, router, state

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
        want = {"feature": "code qa validate docs", "bugfix": "code qa validate docs",
                "refactor": "code qa validate", "test": "qa validate", "docs": "docs validate",
                "chore": "code validate", "spike": "code validate"}
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


if __name__ == "__main__":
    unittest.main()
