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

from yoke import e2e, schema

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


if __name__ == "__main__":
    unittest.main()
