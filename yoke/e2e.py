"""E2E driver protocol, Playwright driver, localhost checker, and server harness."""
import os
import signal
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

SELECTOR = "a,button,[role=button],[role=link],input,select,textarea,[role=tab],[role=menuitem]"
STATE_CHARS = 6000
STATE_SIZE = STATE_CHARS


def is_local_url(url: str) -> bool:
    """Return True if url targets localhost / loopback."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
    except Exception:
        return False
    return host in ("localhost", "127.0.0.1", "::1", "[::1]") or host.endswith(".localhost")


class Driver:
    """Abstract base class for E2E drivers."""

    def open(self, url: str) -> None:
        raise NotImplementedError

    def candidates(self, limit: int = 20) -> list:
        raise NotImplementedError

    def click(self, ref: str) -> None:
        raise NotImplementedError

    def text(self) -> str:
        raise NotImplementedError

    def screenshot(self, path: str) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class PlaywrightDriver(Driver):
    """Playwright-based E2E driver for localhost targets."""

    def __init__(self, headless: bool = True):
        try:
            from playwright.sync_api import sync_playwright
            if sync_playwright is None:
                raise ImportError("playwright is not available")
        except (ImportError, Exception) as e:
            raise ImportError(
                "playwright is required for PlaywrightDriver; install yoke[e2e] or playwright"
            ) from e
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(headless=headless)
        self._browser = self.browser
        self.page = self.browser.new_page()

    def open(self, url: str) -> None:
        if not is_local_url(url):
            raise ValueError("Refusing to open non-local URL: %s" % url)
        self.page.goto(url)

    def candidates(self, limit: int = 20) -> list:
        if limit is not None and limit <= 0:
            return []
        loc = self.page.locator(SELECTOR)
        total = loc.count()
        results = []
        for i in range(total):
            el = loc.nth(i)
            if not el.is_visible():
                continue
            ref = "e%d" % len(results)
            try:
                handle = el.element_handle()
                if handle:
                    handle.evaluate("(e, r) => e.setAttribute('data-yoke-ref', r)", ref)
                else:
                    el.evaluate("(e, r) => e.setAttribute('data-yoke-ref', r)", ref)
            except Exception:
                try:
                    el.evaluate("(e, r) => e.setAttribute('data-yoke-ref', r)", ref)
                except Exception:
                    pass

            role = el.get_attribute("role")
            if not role:
                try:
                    role = el.evaluate("e => e.tagName.toLowerCase()")
                except Exception:
                    role = ""
            role = (role or "").strip()

            aria = el.get_attribute("aria-label")
            if aria and aria.strip():
                name = aria.strip()[:80]
            else:
                try:
                    name = (el.inner_text() or "").strip()[:80]
                except Exception:
                    name = ""

            results.append({"ref": ref, "role": role, "name": name})
            if limit is not None and len(results) >= limit:
                break
        return results

    def click(self, ref: str) -> None:
        self.page.click('[data-yoke-ref="%s"]' % ref)

    def text(self) -> str:
        try:
            return (self.page.inner_text("body") or "")[:STATE_SIZE]
        except Exception:
            return ""

    def screenshot(self, path: str) -> None:
        self.page.screenshot(path=path)

    def close(self) -> None:
        if getattr(self, "browser", None):
            try:
                self.browser.close()
            except Exception:
                pass
            self.browser = None
            self._browser = None
        if getattr(self, "_pw", None):
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None


def start_server(cmd, cwd, url, timeout_s=60):
    """Start server process and poll url until ready (HTTP < 500).

    Returns Popen object, or None if cmd is falsy.
    Raises RuntimeError on timeout after killing the process group.
    """
    if not cmd:
        return None

    proc = subprocess.Popen(
        cmd,
        shell=True,
        cwd=cwd,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    deadline = time.time() + timeout_s
    while True:
        if proc.poll() is not None:
            break
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "yoke-e2e"})
            with urllib.request.urlopen(req, timeout=min(2.0, max(0.2, float(timeout_s)))) as resp:
                status = getattr(resp, "status", getattr(resp, "code", 200))
                if status < 500:
                    return proc
        except urllib.error.HTTPError as e:
            if e.code < 500:
                return proc
        except Exception:
            pass

        if time.time() >= deadline:
            break
        time.sleep(0.5)

    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        pass

    try:
        proc.wait(timeout=5)
    except Exception:
        pass

    raise RuntimeError("server not ready at %s within %ss" % (url, timeout_s))


def stop_server(proc, grace=5) -> None:
    """Stop server process and kill its process group."""
    if not proc:
        return

    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        try:
            proc.poll()
        except Exception:
            pass
        return
    except OSError:
        pass

    try:
        proc.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass
    except Exception:
        pass

    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        pass

    try:
        proc.wait(timeout=5)
    except Exception:
        pass
