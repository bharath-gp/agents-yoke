import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

from tests.test_pipeline import FAKE_CLI, PipeBase, CODE, val, resp, ticket
from yoke import adapters, e2e, pipeline, planreview, router, schema

os.environ.setdefault("YOKE_OLLAMA_URL", "http://127.0.0.1:9")


class HarnessTest(unittest.TestCase):
    def test_is_local_url(self):
        self.assertTrue(e2e.is_local_url("http://localhost:3000"))
        self.assertTrue(e2e.is_local_url("http://127.0.0.1"))
        self.assertTrue(e2e.is_local_url("http://app.localhost:3000"))
        self.assertTrue(e2e.is_local_url("http://[::1]:8000"))

        self.assertFalse(e2e.is_local_url("https://example.com"))
        self.assertFalse(e2e.is_local_url("http://localhost.evil.com"))

    def test_playwright_missing_raises_importerror(self):
        with mock.patch.dict(sys.modules, {"playwright": None, "playwright.sync_api": None}):
            with self.assertRaises(ImportError):
                e2e.PlaywrightDriver()

    def test_start_server_ready(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        url = "http://127.0.0.1:%d" % port
        proc = e2e.start_server("python3 -m http.server %d" % port, tmp, url, 15)
        self.assertIsNotNone(proc)
        self.addCleanup(e2e.stop_server, proc)
        self.assertIsNone(proc.poll())

    def test_start_server_timeout_kills(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            dead_port = s.getsockname()[1]
        dead_url = "http://127.0.0.1:%d" % dead_port
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)

        real_popen = subprocess.Popen
        captured = []

        def spy_popen(*args, **kwargs):
            p = real_popen(*args, **kwargs)
            captured.append(p)
            return p

        with mock.patch("subprocess.Popen", side_effect=spy_popen):
            with self.assertRaises(RuntimeError):
                e2e.start_server("sleep 30", tmp, dead_url, 1)

        self.assertTrue(captured)
        proc = captured[0]
        with self.assertRaises(ProcessLookupError):
            os.killpg(proc.pid, 0)

    def test_e2e_schema_valid_example(self):
        s = schema.load_schema("e2e")
        example = {
            "status": "pass",
            "criteria": [
                {
                    "criterion": "Login button is visible",
                    "met": True,
                    "evidence": "Found button with text 'Login'",
                }
            ],
            "summary": "All criteria met.",
        }
        self.assertEqual(schema.validate(example, s), [])

        extra = dict(example, unexpected="extra_value")
        self.assertTrue(schema.validate(extra, s))

    def test_e2e_schema_validation_strictness(self):
        s = schema.load_schema("e2e")
        valid = {
            "status": "pass",
            "criteria": [
                {"criterion": "title visible", "met": True, "evidence": "found title"}
            ],
            "summary": "all good",
        }
        self.assertEqual(schema.validate(valid, s), [])

        # Invalid status enum
        bad_status = dict(valid, status="maybe")
        self.assertTrue(schema.validate(bad_status, s))

        # Missing required top-level fields
        for req in ("status", "criteria", "summary"):
            missing = dict(valid)
            del missing[req]
            self.assertTrue(schema.validate(missing, s))

        # Extra top-level field
        self.assertTrue(schema.validate(dict(valid, extra="val"), s))

        # Criteria item extra field
        extra_in_item = {
            "status": "pass",
            "criteria": [
                {"criterion": "c", "met": True, "evidence": "e", "extra": "no"}
            ],
            "summary": "s",
        }
        self.assertTrue(schema.validate(extra_in_item, s))

        # Criteria item missing required fields
        for req in ("criterion", "met", "evidence"):
            bad_item = {"criterion": "c", "met": True, "evidence": "e"}
            del bad_item[req]
            self.assertTrue(schema.validate({"status": "pass", "criteria": [bad_item], "summary": "s"}, s))

        # Criteria item invalid field types
        bad_type = {"criterion": "c", "met": "not_a_bool", "evidence": "e"}
        self.assertTrue(schema.validate({"status": "pass", "criteria": [bad_type], "summary": "s"}, s))

    def test_driver_base_class(self):
        driver = e2e.Driver()
        with self.assertRaises(NotImplementedError):
            driver.open("http://localhost:3000")
        with self.assertRaises(NotImplementedError):
            driver.candidates()
        with self.assertRaises(NotImplementedError):
            driver.click("e0")
        with self.assertRaises(NotImplementedError):
            driver.text()
        with self.assertRaises(NotImplementedError):
            driver.screenshot("test.png")
        with self.assertRaises(NotImplementedError):
            driver.close()

    def test_start_and_stop_server_edge_cases(self):
        self.assertIsNone(e2e.start_server("", "/tmp", "http://localhost:3000", 5))
        self.assertIsNone(e2e.start_server(None, "/tmp", "http://localhost:3000", 5))
        e2e.stop_server(None)

    def test_stop_server_terminates_process(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        proc = subprocess.Popen(
            "sleep 30",
            shell=True,
            cwd=tmp,
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(e2e.stop_server, proc)
        self.assertIsNone(proc.poll())
        e2e.stop_server(proc, grace=1)
        self.assertIsNotNone(proc.poll())
        with self.assertRaises(ProcessLookupError):
            os.killpg(proc.pid, 0)

    def test_start_server_early_exit_raises(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        cmd = "%s -c 'import sys; sys.exit(1)'" % sys.executable
        t0 = time.time()
        with self.assertRaises(RuntimeError) as ctx:
            e2e.start_server(cmd, tmp, "http://127.0.0.1:9", timeout_s=5)
        self.assertLess(time.time() - t0, 3.0)
        self.assertIn("server not ready", str(ctx.exception))

    def test_start_server_http_status_branches(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)

        mock_proc = mock.MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 12345

        # HTTP 200 response -> returns proc
        mock_resp = mock.MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = None

        with mock.patch("subprocess.Popen", return_value=mock_proc):
            with mock.patch("urllib.request.urlopen", return_value=mock_resp):
                proc = e2e.start_server("fake_cmd", tmp, "http://127.0.0.1:8000", timeout_s=2)
                self.assertEqual(proc, mock_proc)

        # HTTPError < 500 (e.g. 404) -> returns proc
        http_err_404 = urllib.error.HTTPError("http://127.0.0.1:8000", 404, "Not Found", {}, None)
        with mock.patch("subprocess.Popen", return_value=mock_proc):
            with mock.patch("urllib.request.urlopen", side_effect=http_err_404):
                proc = e2e.start_server("fake_cmd", tmp, "http://127.0.0.1:8000", timeout_s=2)
                self.assertEqual(proc, mock_proc)

        # HTTPError >= 500 (e.g. 500) -> keeps polling until timeout, raises RuntimeError
        http_err_500 = urllib.error.HTTPError("http://127.0.0.1:8000", 500, "Server Error", {}, None)
        with mock.patch("subprocess.Popen", return_value=mock_proc):
            with mock.patch("urllib.request.urlopen", side_effect=http_err_500):
                with mock.patch("os.killpg") as mock_killpg:
                    with self.assertRaises(RuntimeError):
                        e2e.start_server("fake_cmd", tmp, "http://127.0.0.1:8000", timeout_s=0.2)
                    mock_killpg.assert_called_with(mock_proc.pid, signal.SIGKILL)

    def test_playwright_driver_mocked(self):
        mock_pw_mod = mock.MagicMock()
        with mock.patch.dict(sys.modules, {"playwright": mock_pw_mod, "playwright.sync_api": mock_pw_mod}):
            driver = e2e.PlaywrightDriver()
            with self.assertRaises(ValueError):
                driver.open("https://example.com")

            driver.open("http://localhost:3000")
            driver.page.goto.assert_called_once_with("http://localhost:3000")

            # limit <= 0 returns empty list immediately
            self.assertEqual(driver.candidates(limit=0), [])
            self.assertEqual(driver.candidates(limit=-1), [])

            mock_loc = mock.MagicMock()
            driver.page.locator.return_value = mock_loc
            mock_loc.count.return_value = 3

            # el1: visible, role, aria-label, handle
            el1 = mock.MagicMock()
            el1.is_visible.return_value = True
            el1.get_attribute.side_effect = lambda attr: "button" if attr == "role" else "Submit"
            handle1 = mock.MagicMock()
            el1.element_handle.return_value = handle1

            # el2: hidden
            el2 = mock.MagicMock()
            el2.is_visible.return_value = False

            # el3: visible, tag fallback, inner_text fallback, evaluate fallback
            el3 = mock.MagicMock()
            el3.is_visible.return_value = True
            el3.element_handle.return_value = None
            el3.get_attribute.return_value = None
            el3.evaluate.side_effect = lambda expr, *args: "a" if "tagName" in expr else None
            el3.inner_text.return_value = "Click here link"

            mock_loc.nth.side_effect = [el1, el2, el3]

            candidates = driver.candidates(limit=10)
            self.assertEqual(len(candidates), 2)
            self.assertEqual(candidates[0], {"ref": "e0", "role": "button", "name": "Submit"})
            self.assertEqual(candidates[1], {"ref": "e1", "role": "a", "name": "Click here link"})

            handle1.evaluate.assert_called_once_with("(e, r) => e.setAttribute('data-yoke-ref', r)", "e0")
            el3.evaluate.assert_any_call("(e, r) => e.setAttribute('data-yoke-ref', r)", "e1")

            # limit cutoff test
            mock_loc.nth.side_effect = [el1, el2, el3]
            candidates_limited = driver.candidates(limit=1)
            self.assertEqual(len(candidates_limited), 1)
            self.assertEqual(candidates_limited[0]["ref"], "e0")

            driver.click("e0")
            driver.page.click.assert_called_once_with('[data-yoke-ref="e0"]')

            driver.page.inner_text.return_value = "hello body"
            self.assertEqual(driver.text(), "hello body")

            driver.screenshot("/tmp/screen.png")
            driver.page.screenshot.assert_called_once_with(path="/tmp/screen.png")

            mock_browser = driver.browser
            mock_pw = driver._pw
            driver.close()
            mock_browser.close.assert_called_once()
            mock_pw.stop.assert_called_once()
            self.assertIsNone(driver.browser)
            self.assertIsNone(driver._pw)


class FakeDriver(e2e.Driver):
    def __init__(self, states):
        self.states = states
        self.idx = 0
        self.clicks = []
        self.closed = False
        self.opened_urls = []
        self.screenshots = []

    def open(self, url: str) -> None:
        self.opened_urls.append(url)

    def candidates(self, limit: int = 20) -> list:
        cur = self.states[min(self.idx, len(self.states) - 1)]
        c = cur.get("candidates", [])
        return c[:limit] if limit is not None else c

    def click(self, ref: str) -> None:
        self.clicks.append(ref)
        if self.idx + 1 < len(self.states):
            self.idx += 1

    def text(self) -> str:
        cur = self.states[min(self.idx, len(self.states) - 1)]
        return cur.get("text", "")

    def screenshot(self, path: str) -> None:
        self.screenshots.append(path)

    def close(self) -> None:
        self.closed = True


class StageTest(PipeBase):
    def _read_events(self):
        with open(self.run_.path("events.jsonl")) as f:
            return [json.loads(line) for line in f if line.strip()]

    def test_picker_clicks_until_verdict_pass(self):
        cfg = dict(self.run_.cfg)
        cfg["e2e"] = {"url": "http://localhost:1", "candidates": 20, "max_steps": 12}
        self.run_.update(config=cfg)

        states = [
            {
                "candidates": [{"ref": "e0", "role": "button", "name": "Next"}],
                "text": "state 0 text",
            },
            {
                "candidates": [],
                "text": "state 1 text",
            },
        ]
        driver = FakeDriver(states)

        def fake_ollama(cfg, state, questions, timeout=60):
            res = {}
            if "met" in questions:
                res["met"] = {"noul": 0.1 if "state 0" in state else 0.9}
            if "action" in questions:
                res["action"] = {"type": "choice", "choice": "e0", "confidence": 0.9}
            return res

        t = dict(ticket(1), exit_criteria=["state 1 text visible"])
        adapter_for = lambda role: adapters.FakeAdapter(FAKE_CLI)
        with mock.patch("yoke.router._ollama", side_effect=fake_ollama):
            out = e2e.run_stage(self.run_, t, self.tmp, adapter_for, 1, driver=driver)

        self.assertEqual(out["status"], "pass")
        self.assertEqual(len(driver.clicks), 1)
        self.assertEqual(driver.clicks, ["e0"])
        self.assertTrue(driver.closed)

        events = self._read_events()
        step_events = [e for e in events if e.get("kind") == "e2e_step"]
        self.assertEqual(len(step_events), 1)
        self.assertEqual(step_events[0]["ticket"], "T-001")
        self.assertEqual(step_events[0]["criterion"], "state 1 text visible")
        self.assertEqual(step_events[0]["step"], 0)
        self.assertEqual(step_events[0]["action"], "e0")
        self.assertEqual(step_events[0]["source"], "ollama")
        self.assertEqual(step_events[0]["confidence"], 0.9)

    def test_low_confidence_falls_back_to_agent(self):
        cfg = dict(self.run_.cfg)
        cfg["e2e"] = {"url": "http://localhost:1", "candidates": 20, "max_steps": 12}
        self.run_.update(config=cfg)

        states = [
            {
                "candidates": [{"ref": "e0", "role": "button", "name": "Button"}],
                "text": "page text",
            }
        ]
        driver = FakeDriver(states)

        def fake_ollama(cfg, state, questions, timeout=60):
            res = {}
            if "met" in questions:
                res["met"] = {"noul": 0.1}
            if "action" in questions:
                res["action"] = {"type": "choice", "choice": "none", "confidence": 0.9}
            return res

        agent_result = {
            "status": "pass",
            "criteria": [
                {
                    "criterion": "submit succeeded",
                    "met": True,
                    "evidence": "agent verified the submission",
                }
            ],
            "summary": "verified by agent",
        }
        with open(self.script, "w") as f:
            json.dump({"e2e": [resp(agent_result)]}, f)

        t = dict(ticket(1), exit_criteria=["submit succeeded"])
        adapter_for = lambda role: adapters.FakeAdapter(FAKE_CLI)
        with mock.patch("yoke.router._ollama", side_effect=fake_ollama):
            out = e2e.run_stage(self.run_, t, self.tmp, adapter_for, 1, driver=driver, env=self.env)

        self.assertEqual(out["status"], "pass")
        self.assertEqual(len(out["criteria"]), 1)
        self.assertTrue(out["criteria"][0]["met"])
        self.assertEqual(out["criteria"][0]["evidence"], "agent verified the submission")
        self.assertTrue(driver.closed)

    def test_bounded_by_max_steps_falls_back_to_agent(self):
        cfg = dict(self.run_.cfg)
        cfg["e2e"] = {"url": "http://localhost:1", "candidates": 20, "max_steps": 3}
        self.run_.update(config=cfg)

        states = [
            {
                "candidates": [{"ref": "e0", "role": "button", "name": "Loop"}],
                "text": "loop text",
            }
        ]
        driver = FakeDriver(states)

        def fake_ollama(cfg, state, questions, timeout=60):
            res = {}
            if "met" in questions:
                res["met"] = {"noul": 0.1}
            if "action" in questions:
                res["action"] = {"type": "choice", "choice": "e0", "confidence": 0.9}
            return res

        agent_result = {
            "status": "pass",
            "criteria": [
                {
                    "criterion": "loop broken",
                    "met": True,
                    "evidence": "agent handled it after budget spent",
                }
            ],
            "summary": "agent pass",
        }
        with open(self.script, "w") as f:
            json.dump({"e2e": [resp(agent_result)]}, f)

        t = dict(ticket(1), exit_criteria=["loop broken"])
        adapter_for = lambda role: adapters.FakeAdapter(FAKE_CLI)
        with mock.patch("yoke.router._ollama", side_effect=fake_ollama):
            out = e2e.run_stage(self.run_, t, self.tmp, adapter_for, 1, driver=driver, env=self.env)

        self.assertEqual(out["status"], "pass")
        self.assertEqual(len(driver.clicks), 3)
        self.assertEqual(driver.clicks, ["e0", "e0", "e0"])
        self.assertTrue(out["criteria"][0]["met"])
        self.assertEqual(out["criteria"][0]["evidence"], "agent handled it after budget spent")
        self.assertTrue(driver.closed)

    def test_run_stage_fails_when_agent_returns_unmet(self):
        cfg = dict(self.run_.cfg)
        cfg["e2e"] = {"url": "http://localhost:1", "candidates": 20, "max_steps": 1}
        self.run_.update(config=cfg)

        driver = FakeDriver([{"candidates": [], "text": "bad page"}])

        def fake_ollama(cfg, state, questions, timeout=60):
            res = {}
            if "met" in questions:
                res["met"] = {"noul": 0.1}
            return res

        agent_result = {
            "status": "fail",
            "criteria": [
                {
                    "criterion": "criterion 1",
                    "met": False,
                    "evidence": "element not found on page",
                }
            ],
            "summary": "failed to find element",
        }
        with open(self.script, "w") as f:
            json.dump({"e2e": [resp(agent_result)]}, f)

        t = dict(ticket(1), exit_criteria=["criterion 1"])
        adapter_for = lambda role: adapters.FakeAdapter(FAKE_CLI)
        with mock.patch("yoke.router._ollama", side_effect=fake_ollama):
            out = e2e.run_stage(self.run_, t, self.tmp, adapter_for, 1, driver=driver, env=self.env)

        self.assertEqual(out["status"], "fail")
        self.assertFalse(out["criteria"][0]["met"])
        self.assertEqual(out["criteria"][0]["evidence"], "element not found on page")

    def test_text_input_candidates_excluded_from_picker(self):
        cfg = dict(self.run_.cfg)
        cfg["e2e"] = {"url": "http://localhost:1", "candidates": 20, "max_steps": 12}
        self.run_.update(config=cfg)

        states = [
            {
                "candidates": [
                    {"ref": "e0", "role": "textbox", "name": "Username"},
                    {"ref": "e1", "role": "textarea", "name": "Bio"},
                    {"ref": "e2", "role": "select", "name": "Country"},
                    {"ref": "e3", "role": "combobox", "name": "State"},
                    {"ref": "e4", "role": "input", "name": "Email"},
                    {"ref": "e5", "role": "button", "name": "Submit"},
                ],
                "text": "state 0 text",
            },
            {
                "candidates": [],
                "text": "state 1 text",
            },
        ]
        driver = FakeDriver(states)

        captured_questions = []

        def fake_ollama(cfg, state, questions, timeout=60):
            captured_questions.append(questions)
            res = {}
            if "met" in questions:
                res["met"] = {"noul": 0.1 if "state 0" in state else 0.9}
            if "action" in questions:
                res["action"] = {"type": "choice", "choice": "e5", "confidence": 0.9}
            return res

        t = dict(ticket(1), exit_criteria=["state 1 text visible"])
        adapter_for = lambda role: adapters.FakeAdapter(FAKE_CLI)
        with mock.patch("yoke.router._ollama", side_effect=fake_ollama):
            out = e2e.run_stage(self.run_, t, self.tmp, adapter_for, 1, driver=driver)

        self.assertEqual(out["status"], "pass")
        self.assertEqual(driver.clicks, ["e5"])
        action_q = next(q["action"] for q in captured_questions if "action" in q)
        criteria = action_q["criteria"]
        self.assertEqual(set(criteria.keys()), {"e5", "none"})
        self.assertEqual(criteria["e5"], "button Submit")
        self.assertEqual(criteria["none"], "no candidate helps")

    def test_cleanup_in_finally_on_exception(self):
        cfg = dict(self.run_.cfg)
        cfg["e2e"] = {
            "url": "http://localhost:1",
            "start_command": "echo start",
            "ready_timeout_s": 5,
        }
        self.run_.update(config=cfg)

        class ExplodingDriver(FakeDriver):
            def open(self, url: str) -> None:
                super().open(url)
                raise RuntimeError("open failed")

        driver = ExplodingDriver([{"candidates": [], "text": ""}])
        mock_proc = mock.MagicMock()

        t = dict(ticket(1), exit_criteria=["c"])
        adapter_for = lambda role: adapters.FakeAdapter(FAKE_CLI)
        with mock.patch("yoke.e2e.start_server", return_value=mock_proc) as mock_start:
            with mock.patch("yoke.e2e.stop_server") as mock_stop:
                with self.assertRaises(RuntimeError) as ctx:
                    e2e.run_stage(self.run_, t, self.tmp, adapter_for, 1, driver=driver)
                self.assertIn("open failed", str(ctx.exception))
                mock_start.assert_called_once_with("echo start", self.tmp, "http://localhost:1", 5)
                mock_stop.assert_called_once_with(mock_proc)
        self.assertTrue(driver.closed)

    def test_picker_unknown_ref_breaks_to_fallback(self):
        cfg = dict(self.run_.cfg)
        cfg["e2e"] = {"url": "http://localhost:1", "candidates": 20, "max_steps": 12}
        self.run_.update(config=cfg)

        states = [
            {
                "candidates": [{"ref": "e0", "role": "button", "name": "Button"}],
                "text": "page text",
            }
        ]
        driver = FakeDriver(states)

        def fake_ollama(cfg, state, questions, timeout=60):
            res = {}
            if "met" in questions:
                res["met"] = {"noul": 0.1}
            if "action" in questions:
                res["action"] = {"type": "choice", "choice": "e99", "confidence": 0.9}
            return res

        agent_result = {
            "status": "pass",
            "criteria": [
                {
                    "criterion": "fallback criterion",
                    "met": True,
                    "evidence": "recovered by fallback",
                }
            ],
            "summary": "pass",
        }
        with open(self.script, "w") as f:
            json.dump({"e2e": [resp(agent_result)]}, f)

        t = dict(ticket(1), exit_criteria=["fallback criterion"])
        adapter_for = lambda role: adapters.FakeAdapter(FAKE_CLI)
        with mock.patch("yoke.router._ollama", side_effect=fake_ollama):
            out = e2e.run_stage(self.run_, t, self.tmp, adapter_for, 1, driver=driver, env=self.env)

        self.assertEqual(out["status"], "pass")
        self.assertEqual(driver.clicks, [])
        self.assertTrue(out["criteria"][0]["met"])
        self.assertEqual(out["criteria"][0]["evidence"], "recovered by fallback")
        self.assertTrue(driver.closed)

    def test_agent_fallback_failure_sets_met_false(self):
        cfg = dict(self.run_.cfg)
        cfg["e2e"] = {"url": "http://localhost:1", "candidates": 20, "max_steps": 1}
        self.run_.update(config=cfg)

        driver = FakeDriver([{"candidates": [], "text": "error page"}])

        def fake_ollama(cfg, state, questions, timeout=60):
            res = {}
            if "met" in questions:
                res["met"] = {"noul": 0.1}
            return res

        t = dict(ticket(1), exit_criteria=["crit"])
        adapter_for = lambda role: adapters.FakeAdapter(FAKE_CLI)
        fail_res = adapters.CallResult(error="agent connection timed out")
        with mock.patch("yoke.router._ollama", side_effect=fake_ollama):
            with mock.patch("yoke.pipeline._call_retry", return_value=fail_res):
                out = e2e.run_stage(self.run_, t, self.tmp, adapter_for, 1, driver=driver, env=self.env)

        self.assertEqual(out["status"], "fail")
        self.assertFalse(out["criteria"][0]["met"])
        self.assertEqual(out["criteria"][0]["evidence"], "agent connection timed out")

    def test_run_stage_with_minimal_ticket_dict(self):
        cfg = dict(self.run_.cfg)
        cfg["e2e"] = {"url": "http://localhost:1", "candidates": 20, "max_steps": 1}
        self.run_.update(config=cfg)

        driver = FakeDriver([{"candidates": [], "text": "minimal"}])

        def fake_ollama(cfg, state, questions, timeout=60):
            return {"met": {"noul": 0.1}}

        agent_result = {
            "status": "pass",
            "criteria": [{"criterion": "c1", "met": True, "evidence": "ok"}],
            "summary": "pass",
        }
        with open(self.script, "w") as f:
            json.dump({"e2e": [resp(agent_result)]}, f)

        t = {"id": "T-999", "exit_criteria": ["c1"]}
        adapter_for = lambda role: adapters.FakeAdapter(FAKE_CLI)
        with mock.patch("yoke.router._ollama", side_effect=fake_ollama):
            out = e2e.run_stage(self.run_, t, self.tmp, adapter_for, 1, driver=driver, env=self.env)
        self.assertEqual(out["status"], "pass")
        self.assertTrue(out["criteria"][0]["met"])

    def test_non_local_url_blocked(self):
        cfg = dict(self.run_.cfg)
        cfg["e2e"] = {"url": "https://example.com"}
        self.run_.update(config=cfg)

        t = dict(ticket(1), exit_criteria=["c"])
        adapter_for = lambda role: adapters.FakeAdapter(FAKE_CLI)
        with mock.patch("yoke.e2e.PlaywrightDriver") as mock_pw:
            out = e2e.run_stage(self.run_, t, self.tmp, adapter_for, 1)
            mock_pw.assert_not_called()

        self.assertEqual(out["status"], "blocked")
        self.assertIn("non-local", out["summary"])

    def test_missing_playwright_skips_with_event(self):
        cfg = dict(self.run_.cfg)
        cfg["e2e"] = {"url": "http://localhost:3000"}
        self.run_.update(config=cfg)

        t = dict(ticket(1), exit_criteria=["c"])
        adapter_for = lambda role: adapters.FakeAdapter(FAKE_CLI)
        with mock.patch("yoke.e2e.PlaywrightDriver", side_effect=ImportError("no playwright")):
            out = e2e.run_stage(self.run_, t, self.tmp, adapter_for, 1)

        self.assertEqual(out["status"], "pass")
        self.assertTrue(out["summary"].startswith("skipped"))

        events = self._read_events()
        skip_events = [e for e in events if e.get("kind") == "e2e_skipped"]
        self.assertEqual(len(skip_events), 1)
        self.assertEqual(skip_events[0]["ticket"], "T-001")
        self.assertEqual(skip_events[0]["reason"], "playwright not installed")

    def test_pipeline_e2e_stage_gate(self):
        cfg = dict(self.run_.cfg)
        cfg.setdefault("routing", {})["chore"] = {"stages": ["code", "e2e", "validate"]}
        cfg["e2e"] = {"url": "http://localhost:1", "candidates": 20, "max_steps": 12}
        self.run_.update(config=cfg)

        fake_driver = FakeDriver([{"candidates": [], "text": "success page"}])

        def fake_ollama(cfg, state, questions, timeout=60):
            res = {}
            if "met" in questions:
                res["met"] = {"noul": 0.9}
            if "tier" in questions:
                res["tier"] = {"choice": "cheap", "confidence": 0.9}
            if "effort" in questions:
                res["effort"] = {"choice": "low", "confidence": 0.9}
            return res

        self.setup_tickets(
            [ticket(1)],
            {
                "code": [resp(CODE, "c1", write={"f1.txt": "x"})],
                "validate": [resp(val())],
                # commit_message falls back to default subject when router call exits non-zero
                "router": [{"exit": 1}],
            },
        )
        with mock.patch("yoke.e2e.PlaywrightDriver", return_value=fake_driver):
            with mock.patch("yoke.router._ollama", side_effect=fake_ollama):
                self.assertEqual(self.go(), "done")

        self.assertEqual(self.tstate(1)["status"], "closed")
        gates = [e for e in self._read_events() if e.get("kind") == "close_gate"]
        self.assertEqual(len(gates), 1)
        gate_dict = gates[0]["gate"]
        self.assertIn("e2e", gate_dict)
        self.assertTrue(gate_dict["e2e"])
        self.assertTrue(gates[0]["ok"])

    def test_pipeline_e2e_fail_returns_formatted_feedback(self):
        cfg = dict(self.run_.cfg)
        cfg.setdefault("routing", {})["chore"] = {"stages": ["code", "e2e", "validate"]}
        cfg["e2e"] = {"url": "http://localhost:1", "candidates": 20, "max_steps": 1}
        self.run_.update(config=cfg)

        fake_driver = FakeDriver([{"candidates": [], "text": "failure page"}])

        def fake_ollama(cfg, state, questions, timeout=60):
            res = {}
            if "met" in questions:
                res["met"] = {"noul": 0.1}
            if "tier" in questions:
                res["tier"] = {"choice": "cheap", "confidence": 0.9}
            if "effort" in questions:
                res["effort"] = {"choice": "low", "confidence": 0.9}
            return res

        agent_fail = {
            "status": "fail",
            "criteria": [
                {
                    "criterion": "check title",
                    "met": False,
                    "evidence": "title was missing",
                }
            ],
            "summary": "failed",
        }
        agent_pass = {
            "status": "pass",
            "criteria": [{"criterion": "check title", "met": True, "evidence": "title found"}],
            "summary": "pass",
        }
        self.setup_tickets(
            [dict(ticket(1), exit_criteria=["check title"])],
            {
                "code": [
                    resp(CODE, "c1", write={"f1.txt": "x"}),
                    resp(CODE, "c1", write={"f1.txt": "y"}),
                ],
                "e2e": [resp(agent_fail), resp(agent_pass)],
                "validate": [resp(val())],
                "router": [{"exit": 1}] * 4,
            },
        )
        with mock.patch("yoke.e2e.PlaywrightDriver", return_value=fake_driver):
            with mock.patch("yoke.router._ollama", side_effect=fake_ollama):
                self.assertEqual(self.go(), "done")

        self.assertEqual(self.tstate(1)["attempt"], 2)
        with open(self.run_.path("tickets/T-001/attempt-2/feedback.md")) as f:
            fb = f.read()
        self.assertIn("e2e failed:\n- check title: title was missing", fb)

    def test_pipeline_e2e_blocked_escalates(self):
        cfg = dict(self.run_.cfg)
        cfg.setdefault("routing", {})["chore"] = {"stages": ["code", "e2e", "validate"]}
        cfg["e2e"] = {"url": "https://example.com"}
        self.run_.update(config=cfg)

        self.setup_tickets(
            [ticket(1)],
            {
                "code": [resp(CODE, "c1", write={"f1.txt": "x"})],
                "validate": [resp(val())],
                "router": [{"exit": 1}],
            },
        )
        self.assertEqual(self.go(), "needs_user")
        self.assertEqual(self.tstate(1)["status"], "blocked")
        esc = self.run_.read_json("escalations.json")
        self.assertTrue(any("e2e blocked" in e["reason"] for e in esc))

    def test_gate_e2e_false_when_failed(self):
        g = pipeline._gate(["e2e"], {"e2e": {"status": "fail"}}, 0)
        self.assertIn("e2e", g)
        self.assertFalse(g["e2e"])

        g_pass = pipeline._gate(["e2e"], {"e2e": {"status": "pass"}}, 0)
        self.assertTrue(g_pass["e2e"])

        g_no_e2e = pipeline._gate(["code"], {"code": {"status": "done"}}, 0)
        self.assertNotIn("e2e", g_no_e2e)

    def test_run_stage_with_start_command_success_stops_server(self):
        cfg = dict(self.run_.cfg)
        cfg["e2e"] = {
            "url": "http://localhost:1",
            "start_command": "echo start",
            "ready_timeout_s": 5,
        }
        self.run_.update(config=cfg)

        driver = FakeDriver([{"candidates": [], "text": "ok"}])
        mock_proc = mock.MagicMock()

        def fake_ollama(cfg, state, questions, timeout=60):
            return {"met": {"noul": 0.9}}

        t = dict(ticket(1), exit_criteria=["c"])
        adapter_for = lambda role: adapters.FakeAdapter(FAKE_CLI)
        with mock.patch("yoke.e2e.start_server", return_value=mock_proc) as mock_start:
            with mock.patch("yoke.e2e.stop_server") as mock_stop:
                with mock.patch("yoke.router._ollama", side_effect=fake_ollama):
                    out = e2e.run_stage(self.run_, t, self.tmp, adapter_for, 1, driver=driver)
                self.assertEqual(out["status"], "pass")
                mock_start.assert_called_once_with("echo start", self.tmp, "http://localhost:1", 5)
                mock_stop.assert_called_once_with(mock_proc)
        self.assertTrue(driver.closed)

    def test_route_ticket_missing_and_unknown_type(self):
        cfg = self.run_.cfg
        route_empty = router.route_ticket(cfg, {})
        self.assertEqual(route_empty["type"], "chore")
        self.assertTrue(any(s["stage"] == "code" for s in route_empty["stages"]))

        route_unknown = router.route_ticket(cfg, {"type": "nonexistent_type"})
        self.assertEqual(route_unknown["type"], "nonexistent_type")
        self.assertTrue(any(s["stage"] == "code" for s in route_unknown["stages"]))

    def test_multiple_criteria_and_empty_criteria(self):
        cfg = dict(self.run_.cfg)
        cfg["e2e"] = {"url": "http://localhost:1", "candidates": 20, "max_steps": 1}
        self.run_.update(config=cfg)

        adapter_for = lambda role: adapters.FakeAdapter(FAKE_CLI)

        # Empty criteria
        driver_empty = FakeDriver([{"candidates": [], "text": ""}])
        t_empty = dict(ticket(1), exit_criteria=[])
        out_empty = e2e.run_stage(self.run_, t_empty, self.tmp, adapter_for, 1, driver=driver_empty)
        self.assertEqual(out_empty["status"], "pass")
        self.assertEqual(out_empty["criteria"], [])
        self.assertEqual(out_empty["summary"], "0/0 criteria met")

        # Two criteria: one met, one unmet -> fail
        driver_mixed = FakeDriver([{"candidates": [], "text": "crit1_ok crit2_fail"}])

        def fake_ollama(cfg, state, questions, timeout=60):
            res = {}
            if "met" in questions:
                res["met"] = {"noul": 0.9 if "Criterion: crit1" in state else 0.1}
            return res

        t_mixed = dict(ticket(1), exit_criteria=["crit1", "crit2"])
        fail_res = adapters.CallResult(error="crit2 agent fail")
        with mock.patch("yoke.router._ollama", side_effect=fake_ollama):
            with mock.patch("yoke.pipeline._call_retry", return_value=fail_res):
                out_mixed = e2e.run_stage(self.run_, t_mixed, self.tmp, adapter_for, 1, driver=driver_mixed, env=self.env)
        self.assertEqual(out_mixed["status"], "fail")
        self.assertEqual(len(out_mixed["criteria"]), 2)
        self.assertTrue(out_mixed["criteria"][0]["met"])
        self.assertFalse(out_mixed["criteria"][1]["met"])
        self.assertEqual(out_mixed["summary"], "1/2 criteria met")


if __name__ == "__main__":
    unittest.main()
