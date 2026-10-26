import datetime
import contextlib
import http.client
import io
import json
import os
import re
import socket
import tempfile
import threading
import unittest

from yoke import cli, config, dashboard, milestones, runcmds, state

os.environ.setdefault("YOKE_OLLAMA_URL", "http://127.0.0.1:9")


class TestDashboard(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = os.path.realpath(self._td.name)
        os.environ["YOKE_HOME"] = self.tmp
        self.addCleanup(os.environ.pop, "YOKE_HOME", None)
        self.repo_dir = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo_dir)
        self.cfg = config.load(home=self.tmp, overrides={
            "max_tickets": 40,
            "kill_grace_s": 1,
            "router": {"ollama_url": "http://127.0.0.1:1"}
        })

    def _create_run(self):
        return state.Run.create(self.repo_dir, self.cfg, home=self.tmp)

    def test_empty_run(self):
        run = self._create_run()
        runs = dashboard.list_runs(home=self.tmp)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["run_id"], run.run_id)
        self.assertEqual(runs[0]["progress"], "0/0")
        self.assertIsNotNone(runs[0]["last_event_at"])

        detail = dashboard.run_detail(run)
        self.assertIsInstance(detail, dict)
        self.assertEqual(detail["run_id"], run.run_id)
        self.assertEqual(detail["stage"], "grill")
        self.assertEqual(detail["destination"], "(none)")
        self.assertEqual(detail["out_of_scope"], [])
        self.assertEqual(detail["tickets"], [])
        self.assertEqual(detail["escalations"], [])
        self.assertEqual(detail["decisions"], "")
        self.assertEqual(detail["decision_points"], {
            "points": {},
            "guard": {"deny": 0, "ask": 0, "escalate": 0, "recent": []},
        })
        self.assertEqual(detail["progress"]["total"], 0)
        self.assertEqual(detail["progress"]["closed"], 0)
        self.assertEqual(detail["progress"]["remaining"], 0)
        self.assertFalse(detail["status"]["runner_alive"])
        # Must be JSON-serialisable
        serialized = json.dumps(detail)
        self.assertIsInstance(serialized, str)

    def test_mid_run_with_fix_ticket_and_gate(self):
        run = self._create_run()
        doc = {
            "destination": "build dashboard data layer",
            "out_of_scope": ["websockets", "auth"],
            "milestones": [
                {"id": "MS-01", "title": "Foundation", "exit_check": "check_a"},
                {"id": "MS-02", "title": "Execution", "exit_check": "check_b"},
            ],
            "tickets": [
                {
                    "id": "T-001",
                    "title": "Setup data model",
                    "type": "feature",
                    "depends_on": [],
                    "milestone": "MS-01",
                },
                {
                    "id": "T-002",
                    "title": "Add execution rules",
                    "type": "feature",
                    "depends_on": ["T-001"],
                    "milestone": "MS-02",
                },
                {
                    "id": "T-003",
                    "title": "Independent execution task",
                    "type": "feature",
                    "depends_on": [],
                    "milestone": "MS-02",
                },
            ],
        }
        run.write_json("tickets.json", doc)

        fix = milestones.make_fix_ticket(
            id="F-001",
            milestone="MS-01",
            failing_output="traceback",
            exit_check="check_a",
            title="Fix MS-01 failure",
        )
        run.write_json("fixes.json", [fix])

        milestones.save_states(run, {
            "MS-01": {"id": "MS-01", "status": "open", "fix_count": 0},
            "MS-02": {"id": "MS-02", "status": "open", "fix_count": 0},
        })

        run.write_json("approvals.json", {
            "plan": {"sha256": "sha-plan", "at": "2026-10-19T10:00:00+05:30"},
            "tickets": {"sha256": "sha-tickets", "at": "2026-10-19T10:05:00+05:30"},
        })

        # Ticket states
        run.write_json("tickets/T-001/state.json", {"status": "closed", "attempt": 1})
        run.write_json("tickets/F-001/state.json", {"status": "in_progress", "attempt": 1})
        run.write_json("tickets/T-002/state.json", {"status": "pending", "attempt": 0})
        run.write_json("tickets/T-003/state.json", {"status": "pending", "attempt": 0})

        # Add meta.json for T-001
        meta_dir = run.path("tickets/T-001/attempt-1/code")
        os.makedirs(meta_dir, exist_ok=True)
        meta_data = {
            "call_id": "c0001",
            "stage": "code",
            "ticket": "T-001",
            "started_at": "2026-10-19T10:00:00+05:30",
            "ended_at": "2026-10-19T10:04:00+05:30",
        }
        with open(os.path.join(meta_dir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta_data, f)

        detail = dashboard.run_detail(run)
        self.assertEqual(detail["stage"], "run")
        self.assertEqual(detail["destination"], "build dashboard data layer")
        self.assertEqual(detail["out_of_scope"], ["websockets", "auth"])

        # Check milestones: MS-01 has T-001 and F-001 (total: 2, closed: 1)
        ms_map = {m["id"]: m for m in detail["milestones"]}
        self.assertIn("MS-01", ms_map)
        self.assertIn("MS-02", ms_map)
        self.assertEqual(ms_map["MS-01"]["closed"], 1)
        self.assertEqual(ms_map["MS-01"]["total"], 2)
        self.assertEqual(ms_map["MS-01"]["progress"], "1/2")
        self.assertEqual(ms_map["MS-01"]["gate_status"], "open")
        self.assertIn("T-001", ms_map["MS-01"]["ticket_ids"])
        self.assertIn("F-001", ms_map["MS-01"]["ticket_ids"])
        self.assertNotIn("gate", ms_map["MS-01"])
        self.assertNotIn("tickets", ms_map["MS-01"])

        # Check tickets
        t_map = {t["id"]: t for t in detail["tickets"]}
        self.assertEqual(len(t_map), 4)
        self.assertEqual(t_map["T-001"]["status"], "closed")
        self.assertEqual(t_map["T-001"]["attempts"], 1)
        self.assertEqual(t_map["T-001"]["last_stage"], "code")
        self.assertEqual(t_map["T-001"]["updated"], "2026-10-19T10:04:00+05:30")
        self.assertIsNotNone(t_map["T-001"]["agent"])
        self.assertIsNone(t_map["T-001"]["waiting_for"])
        self.assertNotIn("attempt", t_map["T-001"])
        self.assertNotIn("waiting_reason", t_map["T-001"])

        # look-ahead: gates do not hold back pending tickets, only unclosed deps do
        self.assertIsNone(t_map["T-002"]["waiting_for"])
        self.assertIsNone(t_map["T-003"]["waiting_for"])

        # Check progress counts
        progress = detail["progress"]
        self.assertEqual(progress["total"], 4)
        self.assertEqual(progress["closed"], 1)
        self.assertEqual(progress["in_progress"], 1)
        self.assertEqual(progress["blocked"], 0)
        self.assertEqual(progress["pending"], 2)
        self.assertEqual(progress["remaining"], 3)

        # JSON serialisable
        json.dumps(detail)

    def test_done_run(self):
        run = self._create_run()
        run.update(state="done")
        detail = dashboard.run_detail(run)
        self.assertEqual(detail["stage"], "finish")
        self.assertEqual(detail["status"]["state"], "done")
        self.assertEqual(detail["status"]["next"], "finish")

        # When all approvals are valid, next step transitions to 'done'
        with open(run.path("plan.md"), "w", encoding="utf-8") as f:
            f.write("plan text")
        run.write_json("tickets.json", {"tickets": []})
        from yoke import planreview
        planreview.approve(run, "plan")
        planreview.approve(run, "tickets")
        planreview.approve(run, "final")
        detail_approved = dashboard.run_detail(run)
        self.assertEqual(detail_approved["stage"], "finish")
        self.assertEqual(detail_approved["status"]["next"], "done")



    def test_corrupt_run_json_skipped(self):
        runs_dir = os.path.join(self.tmp, "runs")
        corrupt_dir = os.path.join(runs_dir, "corrupt-run")
        os.makedirs(corrupt_dir, exist_ok=True)
        with open(os.path.join(corrupt_dir, "run.json"), "w", encoding="utf-8") as f:
            f.write("corrupted JSON content {")

        # list_runs should not raise, and should skip the corrupt run
        runs = dashboard.list_runs(home=self.tmp)
        self.assertEqual([r["run_id"] for r in runs if r["run_id"] == "corrupt-run"], [])

        # run_detail on a corrupt run should not raise
        corrupt_run = state.Run("corrupt-run", home=self.tmp)
        detail = dashboard.run_detail(corrupt_run)
        self.assertIsInstance(detail, dict)
        self.assertEqual(detail["run_id"], "corrupt-run")

    def test_partial_last_events_jsonl_line(self):
        run = self._create_run()
        ev_file = run.path("events.jsonl")
        with open(ev_file, "a", encoding="utf-8") as f:
            f.write('{"at": "2026-10-19T10:00:00+05:30", "kind": "test_event_1"}\n')
            f.write('{"at": "2026-10-19T10:01:00+05:30", "kind": "partial_eve')

        events = dashboard.read_events_tail(run, limit=50)
        self.assertTrue(len(events) >= 1)
        kinds = [e.get("kind") for e in events]
        self.assertIn("test_event_1", kinds)
        self.assertNotIn("partial_eve", kinds)

    def test_research_run_skipped(self):
        runs_dir = os.path.join(self.tmp, "runs")
        res_dir = os.path.join(runs_dir, "research-run-001")
        os.makedirs(res_dir, exist_ok=True)
        with open(os.path.join(res_dir, "run.json"), "w", encoding="utf-8") as f:
            json.dump({
                "run_id": "research-run-001",
                "kind": "research",
                "repo": self.repo_dir,
                "created_at": "2026-10-19T10:00:00+05:30",
            }, f)

        runs = dashboard.list_runs(home=self.tmp)
        run_ids = [r["run_id"] for r in runs]
        self.assertNotIn("research-run-001", run_ids)

    def test_events_tail_limit_50(self):
        run = self._create_run()
        ev_file = run.path("events.jsonl")
        # Overwrite with 70 entries
        with open(ev_file, "w", encoding="utf-8") as f:
            for i in range(70):
                f.write(json.dumps({"at": "2026-10-19T10:%02d:00+05:30" % (i % 60), "seq": i}) + "\n")

        events = dashboard.read_events_tail(run, limit=50)
        self.assertEqual(len(events), 50)
        self.assertEqual(events[0]["seq"], 20)
        self.assertEqual(events[-1]["seq"], 69)

    def test_dashboard_code_safety_grep(self):
        pkg_dir = os.path.dirname(dashboard.__file__)
        dash_path = os.path.join(pkg_dir, "dashboard.py")
        with open(dash_path, "r", encoding="utf-8") as f:
            code = f.read()

        # No subprocess import
        self.assertNotRegex(code, r"\bimport\s+subprocess\b")
        self.assertNotRegex(code, r"\bfrom\s+subprocess\b")

        # No write opens ('w', 'a', etc.)
        self.assertNotRegex(code, r"open\([^)]*['\"](?:w|a|w\+|a\+|wb|ab)['\"]")

        # No adapters or calls imports
        self.assertNotRegex(code, r"\bimport\s+.*(?:adapters|calls)\b")
        self.assertNotRegex(code, r"\bfrom\s+.*(?:adapters|calls)\b")

    def test_dashboard_html_safety_and_packaging(self):
        pkg_dir = os.path.dirname(dashboard.__file__)
        dash_html_path = os.path.join(pkg_dir, "dashboard.html")
        self.assertTrue(os.path.isfile(dash_html_path), "yoke/dashboard.html must exist")

        with open(dash_html_path, "r", encoding="utf-8") as f:
            html = f.read()

        # No external URLs or remote links/scripts
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        self.assertNotRegex(html, r"""<(?:script|link)[^>]*(?:src|href)=['"][^'"]*//""")

        # No innerHTML, outerHTML, or insertAdjacentHTML
        self.assertNotIn("innerHTML", html)
        self.assertNotIn("outerHTML", html)
        self.assertNotIn("insertAdjacentHTML", html)

        # pyproject.toml package-data check
        root_dir = os.path.dirname(pkg_dir)
        pyproject_path = os.path.join(root_dir, "pyproject.toml")
        with open(pyproject_path, "r", encoding="utf-8") as f:
            pyproject_text = f.read()
        self.assertRegex(pyproject_text, r'\[tool\.setuptools\.package-data\][\s\S]*?"dashboard\.html"')

    def test_shared_waiting_for_helper(self):
        all_milestones = [
            {"id": "MS-01", "title": "First", "exit_check": None},
            {"id": "MS-02", "title": "Second", "exit_check": None},
        ]
        ms_states = {
            "MS-01": {"id": "MS-01", "status": "open"},
            "MS-02": {"id": "MS-02", "status": "open"},
        }
        sts = {"T-001": "pending", "T-002": "pending"}
        t2 = {"id": "T-002", "title": "Second Task", "milestone": "MS-02", "depends_on": ["T-001"]}

        reasons = milestones.ticket_waiting_for(t2, all_milestones, ms_states, sts)
        self.assertEqual(reasons, ["waiting for T-001"])

        sts["T-002"] = "awaiting_gate"
        self.assertEqual(milestones.ticket_waiting_for(t2, all_milestones, ms_states, sts), ["awaiting gate MS-01"])
        ms_states["MS-01"]["status"] = "passed"
        self.assertEqual(milestones.ticket_waiting_for(t2, all_milestones, ms_states, sts), [])
        sts["T-002"] = "pending"
        ms_states["MS-01"]["status"] = "open"

        # Verify that render_map and run_detail match the helper's output
        run = self._create_run()
        run.write_json("tickets.json", {
            "milestones": all_milestones,
            "tickets": [
                {"id": "T-001", "title": "First Task", "milestone": "MS-01", "depends_on": []},
                t2,
            ],
        })
        milestones.save_states(run, ms_states)
        run.write_json("tickets/T-001/state.json", {"status": "pending"})
        run.write_json("tickets/T-002/state.json", {"status": "pending"})

        map_out = runcmds.render_map(run)
        expected_wait_str = "; ".join(reasons)
        self.assertIn("(%s)" % expected_wait_str, map_out)

        detail = dashboard.run_detail(run)
        detail_t2 = next(t for t in detail["tickets"] if t["id"] == "T-002")
        self.assertEqual(detail_t2["waiting_for"], expected_wait_str)

    def test_ticket_last_stage_and_updated_fallbacks(self):
        run = self._create_run()
        doc = {
            "milestones": [{"id": "MS-01", "title": "Base"}],
            "tickets": [
                {"id": "T-EVENT", "title": "From Event", "milestone": "MS-01", "depends_on": []},
                {"id": "T-MTIME", "title": "From Mtime", "milestone": "MS-01", "depends_on": []},
            ],
        }
        run.write_json("tickets.json", doc)
        run.write_json("tickets/T-EVENT/state.json", {"status": "in_progress", "attempt": 1})
        run.write_json("tickets/T-MTIME/state.json", {"status": "pending", "attempt": 0})

        # Ticket 1: no meta.json, fallback to events.jsonl
        run.event("stage_done", ticket="T-EVENT", stage="qa")

        # Ticket 2: no meta.json, no event, fallback to state.json mtime

        detail = dashboard.run_detail(run)
        t_map = {t["id"]: t for t in detail["tickets"]}

        self.assertEqual(t_map["T-EVENT"]["last_stage"], "qa")
        self.assertIsNotNone(t_map["T-EVENT"]["updated"])
        self.assertTrue(re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", t_map["T-EVENT"]["updated"]))

        self.assertIsNone(t_map["T-MTIME"]["last_stage"])
        self.assertIsNotNone(t_map["T-MTIME"]["updated"])
        self.assertTrue(re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", t_map["T-MTIME"]["updated"]))

    def test_run_detail_includes_usage_totals_and_spenders(self):
        run = self._create_run()
        meta_dir = run.path("tickets/T-001/attempt-1/code")
        os.makedirs(meta_dir, exist_ok=True)
        with open(os.path.join(meta_dir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump({
                "stage": "code",
                "ticket": "T-001",
                "agent": "claude",
                "observed": {
                    "model": "claude-sonnet-5-5",
                    "usage": {"input_tokens": 150, "output_tokens": 50, "total_tokens": 200},
                    "cost": 0.025,
                },
            }, f)

        detail = dashboard.run_detail(run)
        self.assertIn("usage", detail)
        self.assertEqual(detail["usage"]["totals"]["cost"], 0.025)
        self.assertEqual(detail["usage"]["totals"]["total_tokens"], 200)
        self.assertEqual(len(detail["usage"]["top_spenders"]), 1)
        self.assertEqual(detail["usage"]["top_spenders"][0]["ticket"], "T-001")
        self.assertEqual(detail["usage"]["top_spenders"][0]["cost"], 0.025)

    def test_usage_split_and_project_rollup(self):
        run = self._create_run()
        for agent, stage, usage, cost in (
            ("claude", "validate", {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 900}, 0.5),
            ("agy", "code", {"input_tokens": 30, "output_tokens": 10}, None),
        ):
            d = run.path("tickets/T-001/attempt-1/" + stage)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
                json.dump({"stage": stage, "ticket": "T-001", "agent": agent,
                           "observed": {"model": "m", "usage": usage, "cost": cost}}, f)
        u = dashboard.run_detail(run)["usage"]
        self.assertEqual(u["totals"]["io_total"], 160)
        self.assertEqual(u["totals"]["cache_total"], 900)
        self.assertEqual(u["by_ticket_agent"]["T-001"]["agy"]["io_total"], 40)
        proj = dashboard.list_projects(home=self.tmp)
        self.assertEqual(len(proj), 1)
        self.assertEqual(proj[0]["totals"]["io_total"], 160)
        self.assertEqual(proj[0]["totals"]["cost"], 0.5)
        self.assertEqual(proj[0]["totals"]["unreported_calls"], 1)
        self.assertEqual(proj[0]["by_agent"]["claude"]["io_total"], 120)

    def test_interactive_session_usage_included_once(self):
        run = self._create_run()
        info = json.load(open(run.path("run.json")))
        proj = os.path.join(self.tmp, "claude", "projects", re.sub(r"[^A-Za-z0-9]", "-", info["repo"]))
        os.makedirs(proj)
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()

        def rec(mid, sid, out, ts=None):
            return json.dumps({"type": "assistant", "timestamp": ts or now, "sessionId": sid, "message": {
                "id": mid, "usage": {"input_tokens": 10, "output_tokens": out}}})
        with open(os.path.join(proj, "chat.jsonl"), "w") as f:
            # m1 is streamed twice: only the final record counts
            f.write("\n".join([rec("m1", "chat", 1), rec("m1", "chat", 5), rec("m2", "chat", 7),
                               rec("m0", "chat", 100, "2000-01-01T00:00:00Z"),
                               json.dumps({"type": "cost-state", "sessionId": "chat", "totalCostUSD": 1.0}),
                               json.dumps({"type": "cost-state", "sessionId": "chat", "totalCostUSD": 3.0})]))
        with open(os.path.join(proj, "spawned.jsonl"), "w") as f:
            f.write(rec("m3", "yoke-spawned", 99))
        d = run.path("calls/router-1")
        os.makedirs(d)
        with open(os.path.join(d, "meta.json"), "w") as f:
            json.dump({"stage": "router", "agent": "claude", "observed": {
                "session_id": "yoke-spawned", "usage": {"input_tokens": 1, "output_tokens": 1}, "cost": 0.1}}, f)
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "claude")
        self.addCleanup(os.environ.pop, "CLAUDE_CONFIG_DIR", None)
        u = dashboard.run_detail(run)["usage"]
        self.assertEqual(u["by_agent"]["interactive"]["io_total"], 10 + 5 + 10 + 7)
        # session cost 3.0 over 3 messages, 2 inside the run window
        self.assertEqual(u["by_agent"]["interactive"]["cost"], 2.0)
        self.assertEqual(u["by_agent"]["interactive"]["cost_partial"], 1)
        self.assertEqual(u["totals"]["io_total"], 2 + 32)
        self.assertEqual(u["totals"]["cost"], 2.1)
        self.assertEqual(dashboard.list_projects(home=self.tmp)[0]["by_agent"]["interactive"]["io_total"], 32)

    def test_run_detail_agy_only_shows_unreported_cost(self):
        run = self._create_run()
        meta_dir = run.path("tickets/T-001/attempt-1/code")
        os.makedirs(meta_dir, exist_ok=True)
        with open(os.path.join(meta_dir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump({
                "stage": "code",
                "ticket": "T-001",
                "agent": "agy",
                "observed": {
                    "model": "unreported",
                    "usage": {"input_tokens": 500, "output_tokens": 100, "total_tokens": 600},
                    "cost": None,
                },
            }, f)

        detail = dashboard.run_detail(run)
        self.assertIn("usage", detail)
        self.assertEqual(detail["usage"]["totals"]["cost"], "unreported")
        self.assertEqual(detail["usage"]["totals"]["total_tokens"], 600)
        self.assertEqual(len(detail["usage"]["top_spenders"]), 1)
        self.assertEqual(detail["usage"]["top_spenders"][0]["cost"], "unreported")

    def test_dashboard_html_contains_usage_section(self):
        pkg_dir = os.path.dirname(dashboard.__file__)
        dash_html_path = os.path.join(pkg_dir, "dashboard.html")
        with open(dash_html_path, "r", encoding="utf-8") as f:
            html = f.read()

        self.assertIn("usage-cost-val", html)
        self.assertIn("usage-total-tokens-val", html)
        self.assertIn("usage-spenders-tbody", html)
        self.assertIn("unreported", html)
        self.assertIn("formatCost", html)

    def test_run_detail_fallback_uses_empty_summary_when_corrupt(self):
        run = self._create_run()
        meta_dir = run.path("tickets/T-001/attempt-1/code")
        os.makedirs(meta_dir, exist_ok=True)
        # Corrupt meta.json that fails or empty
        detail = dashboard.run_detail(run)
        self.assertIn("usage", detail)
        self.assertEqual(detail["usage"]["totals"]["cost"], 0.0)
        self.assertEqual(detail["usage"]["top_spenders"], [])

    def test_run_detail_includes_waiting_limit_and_resume_at(self):
        run = self._create_run()
        run.update(waiting_limit={"resume_at": "2026-10-20T18:00:00Z", "ticket": "T-001"})
        detail = dashboard.run_detail(run)
        self.assertIn("waiting_limit", detail)
        self.assertEqual(detail["waiting_limit"]["resume_at"], "2026-10-20T18:00:00Z")
        self.assertEqual(detail["status"]["waiting_limit"]["resume_at"], "2026-10-20T18:00:00Z")

        # Cleared waiting_limit is None
        run.update(waiting_limit=None)
        detail_cleared = dashboard.run_detail(run)
        self.assertIsNone(detail_cleared.get("waiting_limit"))

    def test_dashboard_html_contains_waiting_limit(self):
        pkg_dir = os.path.dirname(dashboard.__file__)
        dash_html_path = os.path.join(pkg_dir, "dashboard.html")
        with open(dash_html_path, "r", encoding="utf-8") as f:
            html = f.read()
        self.assertIn("detail-waiting-limit", html)
        self.assertIn("run-item-waiting-limit", html)
        self.assertIn("waiting for limit until", html)

    def test_list_runs_includes_waiting_limit(self):
        run = self._create_run()
        run.update(waiting_limit={"resume_at": "2026-10-20T18:00:00Z", "ticket": "T-001"})
        runs = dashboard.list_runs(home=self.tmp)
        self.assertEqual(len(runs), 1)
        self.assertIn("waiting_limit", runs[0])
        self.assertEqual(runs[0]["waiting_limit"]["resume_at"], "2026-10-20T18:00:00Z")

        # Cleared waiting_limit is not in summary
        run.update(waiting_limit=None)
        runs_cleared = dashboard.list_runs(home=self.tmp)
        self.assertNotIn("waiting_limit", runs_cleared[0])


class TestServe(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = os.path.realpath(self._td.name)
        os.environ["YOKE_HOME"] = self.tmp
        self.addCleanup(os.environ.pop, "YOKE_HOME", None)
        self.repo_dir = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo_dir)
        self.cfg = config.load(home=self.tmp, overrides={
            "max_tickets": 40,
            "kill_grace_s": 1,
            "router": {"ollama_url": "http://127.0.0.1:1"}
        })
        self.run = state.Run.create(self.repo_dir, self.cfg, home=self.tmp)
        self.server = dashboard.make_server(self.tmp, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def test_server_address_host_is_127_0_0_1(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_api_runs_list(self):
        self.run.update(waiting_limit={"resume_at": "2026-10-20T18:00:00Z", "ticket": "T-001"})
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("GET", "/api/runs")
        res = conn.getresponse()
        self.assertEqual(res.status, 200)
        self.assertEqual(res.getheader("Cache-Control"), "no-store")
        body = res.read().decode("utf-8")
        conn.close()
        data = json.loads(body)
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["run_id"], self.run.run_id)
        self.assertIn("waiting_limit", data[0])
        self.assertEqual(data[0]["waiting_limit"]["resume_at"], "2026-10-20T18:00:00Z")

    def test_api_run_detail_known(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("GET", "/api/runs/%s" % self.run.run_id)
        res = conn.getresponse()
        self.assertEqual(res.status, 200)
        self.assertEqual(res.getheader("Cache-Control"), "no-store")
        body = res.read().decode("utf-8")
        conn.close()
        data = json.loads(body)
        self.assertEqual(data["run_id"], self.run.run_id)

    def test_api_run_detail_includes_usage_and_unreported(self):
        meta_dir = self.run.path("tickets/T-001/attempt-1/code")
        os.makedirs(meta_dir, exist_ok=True)
        with open(os.path.join(meta_dir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump({
                "stage": "code",
                "ticket": "T-001",
                "agent": "agy",
                "observed": {
                    "model": "unreported",
                    "usage": {"input_tokens": 1200, "output_tokens": 300, "total_tokens": 1500},
                    "cost": None,
                },
            }, f)

        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("GET", "/api/runs/%s" % self.run.run_id)
        res = conn.getresponse()
        self.assertEqual(res.status, 200)
        data = json.loads(res.read().decode("utf-8"))
        conn.close()

        self.assertIn("usage", data)
        self.assertEqual(data["usage"]["totals"]["cost"], "unreported")
        self.assertEqual(data["usage"]["totals"]["total_tokens"], 1500)
        self.assertEqual(data["usage"]["top_spenders"][0]["ticket"], "T-001")
        self.assertEqual(data["usage"]["top_spenders"][0]["cost"], "unreported")

    def test_api_run_detail_includes_waiting_limit(self):
        self.run.update(waiting_limit={"resume_at": "2026-10-20T18:00:00Z", "ticket": "T-001"})
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("GET", "/api/runs/%s" % self.run.run_id)
        res = conn.getresponse()
        self.assertEqual(res.status, 200)
        data = json.loads(res.read().decode("utf-8"))
        conn.close()
        self.assertIn("waiting_limit", data)
        self.assertEqual(data["waiting_limit"]["resume_at"], "2026-10-20T18:00:00Z")
        self.assertEqual(data["status"]["waiting_limit"]["resume_at"], "2026-10-20T18:00:00Z")

    def test_api_run_detail_unknown(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("GET", "/api/runs/nonexistent-run")
        res = conn.getresponse()
        self.assertEqual(res.status, 404)
        self.assertEqual(res.getheader("Cache-Control"), "no-store")
        body = res.read().decode("utf-8")
        conn.close()
        data = json.loads(body)
        self.assertIn("error", data)

    def test_traversal_ids_rejected(self):
        for bad_id in ("..", "%2e%2e", "a/b"):
            conn = http.client.HTTPConnection("127.0.0.1", self.port)
            conn.request("GET", "/api/runs/" + bad_id)
            res = conn.getresponse()
            self.assertIn(res.status, (400, 404))
            self.assertEqual(res.getheader("Cache-Control"), "no-store")
            conn.close()

    def test_post_returns_405(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("POST", "/api/runs", body=b"{}")
        res = conn.getresponse()
        self.assertEqual(res.status, 405)
        self.assertEqual(res.getheader("Cache-Control"), "no-store")
        self.assertEqual(res.getheader("Allow"), "GET, HEAD")
        conn.close()

    def test_port_in_use_exits_one(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        in_use_port = s.getsockname()[1]
        self.addCleanup(s.close)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                dashboard.serve(self.tmp, in_use_port)
        self.assertEqual(cm.exception.code, 1)
        self.assertIn(str(in_use_port), err.getvalue())

    def test_serve_in_thread(self):
        server_holder = []
        ready = threading.Event()
        out = io.StringIO()

        def run_serve():
            with contextlib.redirect_stdout(out):
                dashboard.serve(self.tmp, 0, ready=lambda s: (server_holder.append(s), ready.set()))

        t = threading.Thread(target=run_serve, daemon=True)
        t.start()
        self.assertTrue(ready.wait(timeout=5))
        server = server_holder[0]
        port = server.server_address[1]
        try:
            self.assertEqual(server.server_address[0], "127.0.0.1")
            self.assertIn("dashboard: http://127.0.0.1:%d" % port, out.getvalue())
            conn = http.client.HTTPConnection("127.0.0.1", port)
            conn.request("GET", "/api/runs")
            res = conn.getresponse()
            self.assertEqual(res.status, 200)
            self.assertEqual(res.getheader("Cache-Control"), "no-store")
            data = json.loads(res.read().decode("utf-8"))
            self.assertEqual(data[0]["run_id"], self.run.run_id)
            conn.close()
        finally:
            server.shutdown()
            t.join(timeout=5)

    def test_root_serves_html_and_head(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("GET", "/")
        res = conn.getresponse()
        self.assertEqual(res.status, 200)
        self.assertEqual(res.getheader("Cache-Control"), "no-store")
        self.assertIn("text/html", res.getheader("Content-Type"))
        body = res.read().decode("utf-8")
        self.assertIn("yoke dashboard", body)
        self.assertNotIn("http://", body)
        self.assertNotIn("https://", body)
        self.assertNotRegex(body, r"""<(?:script|link)[^>]*(?:src|href)=['"][^'"]*//""")
        self.assertNotIn("innerHTML", body)
        conn.close()

        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("HEAD", "/")
        res = conn.getresponse()
        self.assertEqual(res.status, 200)
        self.assertEqual(res.getheader("Cache-Control"), "no-store")
        self.assertEqual(res.read(), b"")
        conn.close()

    def test_api_runs_head(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("HEAD", "/api/runs")
        res = conn.getresponse()
        self.assertEqual(res.status, 200)
        self.assertEqual(res.getheader("Cache-Control"), "no-store")
        self.assertEqual(res.read(), b"")
        conn.close()

    def test_unsupported_methods_return_405(self):
        for method in ("PUT", "DELETE", "PATCH"):
            conn = http.client.HTTPConnection("127.0.0.1", self.port)
            conn.request(method, "/api/runs")
            res = conn.getresponse()
            self.assertEqual(res.status, 405)
            self.assertEqual(res.getheader("Cache-Control"), "no-store")
            self.assertEqual(res.getheader("Allow"), "GET, HEAD")
            conn.close()

    def test_cli_dashboard_help(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as cm:
                cli.main(["dashboard", "--help"])
        self.assertEqual(cm.exception.code, 0)
        text = out.getvalue()
        self.assertIn("--port", text)
        self.assertIn("8765", text)
        self.assertNotIn("--host", text)
        self.assertNotIn("--run", text)


class DecisionsTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = os.path.realpath(self._td.name)
        os.environ["YOKE_HOME"] = self.tmp
        self.addCleanup(os.environ.pop, "YOKE_HOME", None)
        self.repo_dir = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo_dir)
        self.cfg = config.load(home=self.tmp, overrides={
            "max_tickets": 40,
            "kill_grace_s": 1,
            "router": {"ollama_url": "http://127.0.0.1:1"}
        })

    def _create_run(self):
        return state.Run.create(self.repo_dir, self.cfg, home=self.tmp)

    def test_summary_counts(self):
        events = [
            {"kind": "router", "point": "safety", "source": "ollama"},
            {"kind": "router", "fn": "classify_ticket", "source": "default"},
            {"kind": "guard", "decision": "deny", "target": "shell", "reason": "unsafe", "ticket": "T-1", "at": "2026-10-22T00:00:00Z"},
            {"kind": "guard", "decision": "allow", "target": "git", "ticket": "T-1"},
        ]
        res = dashboard.decisions_summary(events)
        self.assertEqual(res["points"]["safety"]["ollama"], 1)
        self.assertEqual(res["points"]["safety"]["none"], 0)
        self.assertEqual(res["points"]["safety"]["default"], 0)
        self.assertEqual(res["points"]["safety"]["fallback"], 0)
        self.assertEqual(res["points"]["classify_ticket"]["default"], 1)
        self.assertEqual(res["guard"]["deny"], 1)
        self.assertEqual(res["guard"]["ask"], 0)
        self.assertEqual(res["guard"]["escalate"], 0)
        self.assertEqual(len(res["guard"]["recent"]), 1)
        self.assertEqual(res["guard"]["recent"][0]["at"], "2026-10-22T00:00:00Z")
        self.assertEqual(res["guard"]["recent"][0]["target"], "shell")
        self.assertEqual(res["guard"]["recent"][0]["decision"], "deny")
        self.assertEqual(res["guard"]["recent"][0]["reason"], "unsafe")
        self.assertEqual(res["guard"]["recent"][0]["ticket"], "T-1")

        # Sources outside the four keys are added as their own key
        extra_events = [
            {"kind": "router", "point": "routing", "source": "custom_agent"}
        ]
        res_extra = dashboard.decisions_summary(extra_events)
        self.assertEqual(res_extra["points"]["routing"]["custom_agent"], 1)

        # Retain last 10 flagged guard events
        events_15 = [
            {"kind": "guard", "decision": "deny", "ticket": "T-%d" % i, "at": "2026-10-22T00:00:%02dZ" % i}
            for i in range(15)
        ]
        res_15 = dashboard.decisions_summary(events_15)
        self.assertEqual(res_15["guard"]["deny"], 15)
        self.assertEqual(len(res_15["guard"]["recent"]), 10)
        self.assertEqual(res_15["guard"]["recent"][0]["ticket"], "T-5")
        self.assertEqual(res_15["guard"]["recent"][-1]["ticket"], "T-14")

        # Defensive branches: non-dict events, missing point/fn, missing source, ts fallback, None/empty
        defensive_events = [
            None,
            "not-a-dict",
            {"kind": "router"},
            {"kind": "router", "point": ["not", "a", "str"], "source": "ollama"},
            {"kind": "router", "point": "routing", "source": ["not-a-str"]},
            {"kind": "guard", "decision": "ask", "ts": "2026-10-22T01:00:00Z"},
        ]
        res_defensive = dashboard.decisions_summary(defensive_events)
        self.assertEqual(res_defensive["points"]["routing"]["ollama"], 0)
        self.assertEqual(res_defensive["guard"]["ask"], 1)
        self.assertEqual(len(res_defensive["guard"]["recent"]), 1)
        self.assertEqual(res_defensive["guard"]["recent"][0]["at"], "2026-10-22T01:00:00Z")
        self.assertEqual(res_defensive["guard"]["recent"][0]["decision"], "ask")

        self.assertEqual(dashboard.decisions_summary([]), {
            "points": {},
            "guard": {"deny": 0, "ask": 0, "escalate": 0, "recent": []},
        })
        self.assertEqual(dashboard.decisions_summary(None), {
            "points": {},
            "guard": {"deny": 0, "ask": 0, "escalate": 0, "recent": []},
        })

    def test_run_detail_has_decisions(self):
        run = self._create_run()
        run.event("router", point="safety", source="ollama")
        run.event("guard", decision="deny", target="x", reason="r", ticket="T-1")
        detail = dashboard.run_detail(run)
        self.assertIn("decision_points", detail)
        self.assertIn("points", detail["decision_points"])
        self.assertIn("safety", detail["decision_points"]["points"])
        self.assertEqual(detail["decision_points"]["points"]["safety"]["ollama"], 1)
        self.assertEqual(detail["decision_points"]["guard"]["deny"], 1)
        self.assertEqual(len(detail["decision_points"]["guard"]["recent"]), 1)
        self.assertEqual(detail["decision_points"]["guard"]["recent"][0]["target"], "x")

    def test_html_has_decision_points_card(self):
        pkg_dir = os.path.dirname(dashboard.__file__)
        dash_html_path = os.path.join(pkg_dir, "dashboard.html")
        with open(dash_html_path, "r", encoding="utf-8") as f:
            html = f.read()

        self.assertIn("decision-points", html)
        self.assertIn("Decision models", html)
        self.assertIn("decisions-tbody", html)
        self.assertIn("guard-recent-list", html)
        self.assertIn("guard-counts", html)
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        self.assertNotIn("innerHTML", html)
        self.assertNotIn("outerHTML", html)
        self.assertNotIn("insertAdjacentHTML", html)


if __name__ == "__main__":
    unittest.main()

