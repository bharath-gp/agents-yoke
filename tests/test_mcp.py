import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest

from yoke import config, state

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def dead_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class McpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = os.path.realpath(os.path.join(self.tmp, "repo"))
        os.makedirs(self.repo)
        bindir = os.path.join(self.tmp, "bin")  # git only: no real claude, so the fallback agent fails
        os.makedirs(bindir)
        os.symlink(shutil.which("git"), os.path.join(bindir, "git"))
        cfg = config.load(home=self.tmp, overrides={
            "router": {"ollama_url": "http://127.0.0.1:%d" % dead_port()}})
        self.run_ = state.Run.create(self.repo, cfg, home=self.tmp)
        env = dict(os.environ, YOKE_HOME=self.tmp, PATH=bindir, PYTHONPATH=ROOT)
        env.pop("YOKE_RUN", None)
        self.proc = subprocess.Popen([sys.executable, "-m", "yoke.cli", "mcp"], cwd=self.repo, env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(self.close)
        self.n = 0

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=10)
        self.proc.stdout.close()

    def rpc(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self.n += 1
            msg["id"] = self.n
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        return None if notify else json.loads(self.proc.stdout.readline())

    def call(self, name, args=None):
        r = self.rpc("tools/call", {"name": name, "arguments": args or {}})
        return r["result"]

    def test_initialize_echoes_protocol_version(self):
        r = self.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                    "clientInfo": {"name": "t", "version": "0"}})["result"]
        self.assertEqual(r["protocolVersion"], "2025-03-26")
        self.assertIn("tools", r["capabilities"])
        self.rpc("notifications/initialized", notify=True)
        self.assertEqual(self.rpc("ping")["result"], {})

    def test_tools_list(self):
        tools = self.rpc("tools/list")["result"]["tools"]
        self.assertEqual(sorted(t["name"] for t in tools), [
            "yoke_assert", "yoke_classify_ticket", "yoke_commit_message", "yoke_health",
            "yoke_status", "yoke_summarize", "yoke_triage_failure"])
        for t in tools:
            self.assertEqual(t["inputSchema"]["type"], "object")

    def test_health_ollama_down(self):
        res = self.call("yoke_health")
        self.assertFalse(res.get("isError"))
        self.assertEqual(json.loads(res["content"][0]["text"]), {"ollama": False, "model": False})

    def test_classify_falls_back_to_default(self):
        res = self.call("yoke_classify_ticket", {"title": "add login", "description": "x"})
        out = json.loads(res["content"][0]["text"])
        self.assertEqual(out["source"], "default")
        self.assertEqual(out["result"]["type"], "feature")

    def test_assert_default_escalates(self):
        out = json.loads(self.call("yoke_assert", {"claim": "c", "evidence": "e"})["content"][0]["text"])
        self.assertEqual(out["result"]["reason"], "unknown")

    def test_status_reports_run(self):
        out = json.loads(self.call("yoke_status")["content"][0]["text"])
        self.assertEqual(out["run_id"], self.run_.run_id)

    def test_errors(self):
        self.assertEqual(self.rpc("nope")["error"]["code"], -32601)
        res = self.call("yoke_nope")
        self.assertTrue(res["isError"])
        res = self.call("yoke_assert", {"claim": "c"})  # missing evidence
        self.assertTrue(res["isError"])


class NoRunCase(unittest.TestCase):
    def test_status_without_run_is_tool_error(self):
        tmp = tempfile.mkdtemp()
        env = dict(os.environ, YOKE_HOME=tmp, PYTHONPATH=ROOT)
        env.pop("YOKE_RUN", None)
        req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "yoke_status"}}
        out = subprocess.run([sys.executable, "-m", "yoke.cli", "mcp"], cwd=tmp, env=env, text=True,
                             input=json.dumps(req) + "\n", capture_output=True, timeout=30).stdout
        self.assertTrue(json.loads(out)["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
