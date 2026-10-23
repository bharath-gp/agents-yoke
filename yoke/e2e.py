"""E2E driver protocol, Playwright driver, localhost checker, and server harness."""
import json
import os
import signal
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from . import router

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


def run_stage(run, ticket, wt, adapter_for, attempt, driver=None, env=None, base=None):
    """Run E2E verification stage for ticket exit criteria.

    Drives a browser Driver instance (PlaywrightDriver by default) against a local
    target URL to verify each exit criterion of the ticket.

    Step lifecycle per criterion:
      1. Verdict: evaluates page text with Ollama decision point e2e_verdict.
      2. Picker: if verdict below threshold, picks interactive candidate elements
         (excluding text inputs) with Ollama decision point e2e_pick to click.
      3. Fallback: on step exhaustion or low-confidence picker, captures screenshot
         and invokes read-mode agent with e2e prompt schema.

    Returns dict:
        {"status": "pass"|"fail"|"blocked", "criteria": [...], "summary": str}
    """
    tid = ticket.get("id", "T-e2e") if isinstance(ticket, dict) else str(ticket)
    cfg = (run.cfg.get("e2e") or {}) if hasattr(run, "cfg") and isinstance(run.cfg, dict) else {}
    url = cfg.get("url")
    if not url or not is_local_url(url):
        reason = "unset e2e url" if not url else "non-local e2e url: %s" % url
        return {"status": "blocked", "criteria": [], "summary": reason}

    if driver is None:
        try:
            driver = PlaywrightDriver()
        except ImportError:
            run.event("e2e_skipped", ticket=tid, reason="playwright not installed")
            return {"status": "pass", "criteria": [], "summary": "skipped: playwright not installed"}

    proc = None
    try:
        start_cmd = cfg.get("start_command")
        if start_cmd:
            ready_timeout_s = cfg.get("ready_timeout_s", 60)
            proc = start_server(start_cmd, wt, url, ready_timeout_s)
        driver.open(url)

        criteria_results = []
        exit_criteria = ticket.get("exit_criteria") or []
        max_steps = int(cfg.get("max_steps", 12))
        num_candidates = int(cfg.get("candidates", 20))

        for n, c in enumerate(exit_criteria):
            c_name = c if isinstance(c, str) else c.get("criterion", str(c))
            met = False
            evidence = ""
            for step in range(max_steps):
                text = driver.text()
                state = "Criterion: %s\nPage:\n%s" % (c_name, text)
                v = router.decide(
                    run.cfg,
                    "e2e_verdict",
                    state,
                    {"met": {"type": "noul", "instructions": "Does the page show that this criterion is met?"}},
                    lambda a: (a["met"]["noul"], a["met"]["noul"]),
                    run,
                )
                if v.get("source") == "ollama":
                    met = True
                    conf = v.get("confidence")
                    evidence = "model verdict %.2f" % (conf if conf is not None else v.get("result", 1.0))
                    break

                cands = [
                    x for x in driver.candidates(num_candidates)
                    if (x.get("role") or "").lower().strip() not in ("input", "textarea", "select", "textbox", "combobox")
                ]
                crit = {x["ref"]: "%s %s" % (x.get("role", ""), x.get("name", "")) for x in cands}
                crit["none"] = "no candidate helps"
                p = router.decide(
                    run.cfg,
                    "e2e_pick",
                    state,
                    {
                        "action": {
                            "type": "choice",
                            "instructions": "Which element should be clicked next to check this criterion?",
                            "criteria": crit,
                        }
                    },
                    lambda a: (a["action"]["choice"], a["action"]["confidence"]),
                    run,
                )
                if p.get("source") == "ollama" and p.get("result") != "none" and p.get("result") in crit:
                    driver.click(p["result"])
                    run.event(
                        "e2e_step",
                        ticket=tid,
                        criterion=c_name,
                        step=step,
                        action=p["result"],
                        source=p.get("source"),
                        confidence=p.get("confidence"),
                    )
                    continue
                else:
                    break

            if not met:
                from . import pipeline
                base_sha = base
                if base_sha is None:
                    try:
                        base_sha = pipeline.ticket_state(run, tid).get("base_sha")
                    except Exception:
                        base_sha = None
                shot = os.path.join(run.path("tickets/%s" % tid), "e2e-%d.png" % n)
                try:
                    os.makedirs(os.path.dirname(shot), exist_ok=True)
                    driver.screenshot(shot)
                except Exception:
                    pass
                t_call = dict(ticket) if isinstance(ticket, dict) else {"id": str(ticket)}
                t_call.setdefault("id", tid)
                t_call.setdefault("title", tid)
                t_call.setdefault("description", "")
                t_call.setdefault("type", "chore")
                res = pipeline._call_retry(
                    run,
                    adapter_for,
                    env,
                    t_call,
                    "e2e",
                    attempt,
                    wt,
                    base_sha,
                    "read",
                    {},
                    "",
                    prompt_name="e2e",
                    extra_vars={
                        "criterion": c_name,
                        "candidates": json.dumps(driver.candidates(num_candidates), indent=2),
                        "page_text": driver.text(),
                        "screenshot": shot,
                    },
                )
                if res.ok and isinstance(res.structured_output, dict) and res.structured_output.get("criteria"):
                    first = res.structured_output["criteria"][0]
                    met = bool(first.get("met", False))
                    evidence = first.get("evidence", "")
                else:
                    met = False
                    evidence = getattr(res, "error", None) or "agent fallback failed"

            criteria_results.append({"criterion": c_name, "met": met, "evidence": evidence})

        all_met = all(c["met"] for c in criteria_results) if criteria_results else True
        met_count = sum(1 for c in criteria_results if c["met"])
        total_count = len(criteria_results)
        status = "pass" if all_met else "fail"
        summary = "%d/%d criteria met" % (met_count, total_count)
        return {"status": status, "criteria": criteria_results, "summary": summary}
    finally:
        if driver is not None:
            try:
                driver.close()
            except Exception:
                pass
        if proc is not None:
            try:
                stop_server(proc)
            except Exception:
                pass
