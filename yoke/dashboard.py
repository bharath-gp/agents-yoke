"""Dashboard data layer and HTTP server: list_runs, run_detail, and serve."""
import datetime
import http.server
import json
import os
import re
import sys
import urllib.parse

from . import config, milestones, runcmds, scheduler, state


def read_events_tail(path_or_run, limit=50):
    """Read last N entries from events.jsonl by seeking from the end, tolerating partial last line."""
    if hasattr(path_or_run, "path"):
        path = path_or_run.path("events.jsonl")
    else:
        path = str(path_or_run)

    if not os.path.isfile(path):
        return []

    try:
        size = os.path.getsize(path)
    except OSError:
        return []

    if size == 0:
        return []

    chunk_size = 4096
    buf = b""
    pos = size

    try:
        with open(path, "rb") as f:
            while pos > 0:
                read_bytes = min(chunk_size, pos)
                pos -= read_bytes
                f.seek(pos, os.SEEK_SET)
                chunk = f.read(read_bytes)
                buf = chunk + buf
                if buf.count(b"\n") >= limit + 2:
                    break
    except OSError:
        return []

    lines = buf.splitlines()
    entries = []
    for line in lines:
        line_str = line.decode("utf-8", errors="replace").strip()
        if not line_str:
            continue
        try:
            entry = json.loads(line_str)
            if isinstance(entry, dict):
                entries.append(entry)
        except Exception:
            continue

    return entries[-limit:]


def derive_stage(run):
    """Derive stage from state plus approvals.json: grill, plan, tickets, run, finish."""
    info = _read_json_safe(run, "run.json", {})
    st = info.get("state")
    if st == "done":
        return "finish"

    approvals = _read_json_safe(run, "approvals.json", {})
    if not isinstance(approvals, dict):
        approvals = {}

    if "tickets" in approvals:
        return "run"
    if "plan" in approvals:
        return "tickets"
    if _file_exists(run, "plan.md") or _file_exists(run, "requirements.md"):
        return "plan"
    return "grill"


def _file_exists(run, rel):
    try:
        return os.path.isfile(run.path(rel))
    except Exception:
        return False


def _read_json_safe(run, rel, default=None):
    try:
        path = run.path(rel)
        if not os.path.isfile(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _read_text_safe(run, rel):
    try:
        path = run.path(rel)
        if not os.path.isfile(path):
            return ""
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return ""


def _get_ticket_state(run, tid):
    return _read_json_safe(run, os.path.join("tickets", str(tid), "state.json"), {}) or {}


def _get_ticket_last_stage_and_updated(run, tid, recent_events=None):
    last_stage = None
    updated = None

    # 1. Look in tickets/<tid>/attempt-*/*/meta.json
    try:
        tickets_dir = run.path("tickets")
        tid_dir = os.path.join(tickets_dir, str(tid))
        if os.path.isdir(tid_dir):
            valid_metas = []
            for att in os.listdir(tid_dir):
                if not att.startswith("attempt-"):
                    continue
                att_dir = os.path.join(tid_dir, att)
                if not os.path.isdir(att_dir):
                    continue
                for stg in os.listdir(att_dir):
                    stg_dir = os.path.join(att_dir, stg)
                    if not os.path.isdir(stg_dir):
                        continue
                    meta_path = os.path.join(stg_dir, "meta.json")
                    if os.path.isfile(meta_path):
                        try:
                            with open(meta_path, "r", encoding="utf-8") as f:
                                meta_obj = json.load(f)
                            if isinstance(meta_obj, dict):
                                mtime = os.path.getmtime(meta_path)
                                ts = meta_obj.get("ended_at") or meta_obj.get("started_at") or ""
                                valid_metas.append((ts, mtime, meta_obj))
                        except Exception:
                            pass
            if valid_metas:
                valid_metas.sort(key=lambda item: (item[0], item[1]))
                best = valid_metas[-1]
                last_stage = best[2].get("stage")
                updated = best[2].get("ended_at") or best[2].get("started_at")
                if not updated and best[1]:
                    updated = datetime.datetime.fromtimestamp(best[1]).astimezone().isoformat(timespec="seconds")
    except Exception:
        pass

    # 2. If not both resolved, check recent events tail
    if not (last_stage and updated):
        try:
            if recent_events is None:
                recent_events = read_events_tail(run, limit=500)
            for ev in reversed(recent_events):
                if isinstance(ev, dict) and ev.get("ticket") == str(tid):
                    if not updated and ev.get("at"):
                        updated = ev.get("at")
                    if not last_stage and ev.get("stage"):
                        last_stage = ev.get("stage")
                    if last_stage and updated:
                        break
        except Exception:
            pass

    # 3. Fall back to state.json mtime
    if not updated:
        try:
            state_path = run.path(os.path.join("tickets", str(tid), "state.json"))
            if os.path.isfile(state_path):
                mtime = os.path.getmtime(state_path)
                updated = datetime.datetime.fromtimestamp(mtime).astimezone().isoformat(timespec="seconds")
        except Exception:
            pass

    return last_stage, updated


def list_runs(home=None):
    """List runs under home/runs/ with summary, progress (closed/total), last_event_at, newest first.

    Skips kind == research and unreadable ones. Never raises.
    """
    if home is None:
        home = config.yoke_home()
    runs_dir = os.path.join(home, "runs")
    if not os.path.isdir(runs_dir):
        return []

    try:
        entries = os.listdir(runs_dir)
    except OSError:
        return []

    results = []
    for name in entries:
        run_dir = os.path.join(runs_dir, name)
        if not os.path.isdir(run_dir):
            continue
        run_json_path = os.path.join(run_dir, "run.json")
        if not os.path.isfile(run_json_path):
            continue
        try:
            with open(run_json_path, "r", encoding="utf-8") as f:
                info = json.load(f)
        except Exception:
            continue
        if not isinstance(info, dict):
            continue
        if info.get("kind") == "research":
            continue

        try:
            r = state.Run(name, home=home)
            s = runcmds.summary(r)
            closed = s.get("tickets_closed", 0)
            total = s.get("tickets_total", 0)
            s["progress"] = "%d/%d" % (closed, total)
            evs = read_events_tail(r, limit=1)
            last_event_at = evs[-1].get("at") if (evs and isinstance(evs[-1], dict)) else None
            s["last_event_at"] = last_event_at
            results.append(s)
        except Exception:
            continue

    results.sort(key=lambda item: (item.get("created_at") or "", item.get("run_id") or ""), reverse=True)
    return results


def _run_window(run, info):
    """(start, end) timestamps of a run: creation to its last event, or now while it is active."""
    evs = read_events_tail(run, limit=1)
    last = evs[-1].get("at") if (evs and isinstance(evs[-1], dict)) else None
    end = last if info.get("state") == "done" and last else datetime.datetime.now(datetime.timezone.utc).isoformat()
    return info.get("created_at"), end


def _interactive(run, info, summary):
    from . import usage
    try:
        start, end = _run_window(run, info)
        return usage.interactive_messages(info.get("repo"), start, end, set(summary.get("session_ids") or []))
    except Exception:
        return {}


def list_projects(home=None):
    """Group runs by target repo and roll up usage (run.json 'repo'). Never raises."""
    from . import usage
    if home is None:
        home = config.yoke_home()
    runs_dir = os.path.join(home, "runs")
    groups = {}
    try:
        names = sorted(os.listdir(runs_dir))
    except OSError:
        return []
    for name in names:
        info = _read_json_safe(state.Run(name, home=home), "run.json", {})
        if not isinstance(info, dict) or info.get("kind") == "research" or not info.get("repo"):
            continue
        try:
            run = state.Run(name, home=home)
            u = _read_json_safe(run, "usage.json", None)
            if not isinstance(u, dict) or info.get("state") != "done":
                u = usage.summarize(run)
        except Exception:
            continue
        # messages merge by id so a chat spanning overlapping runs is counted once
        groups.setdefault(info["repo"], []).append((u, _interactive(run, info, u)))
    out = []
    for repo, pairs in sorted(groups.items()):
        msgs = {}
        for _, m in pairs:
            msgs.update(m)
        us = [u for u, _ in pairs]
        row = usage.project_summary(us)
        if msgs:
            b = usage.interactive_bucket(msgs)
            row["totals"] = usage.merge([row["totals"], b])
            row["by_agent"]["interactive"] = b
        row["repo"] = repo
        row["runs"] = len(us)
        out.append(row)
    return out


def decisions_summary(events):
    """Summarise router and guard events into decision points and guard counts."""
    points = {}
    guard = {"deny": 0, "ask": 0, "escalate": 0, "recent": []}
    recent_guard = []

    for e in (events or []):
        if not isinstance(e, dict):
            continue
        kind = e.get("kind")
        if kind == "router":
            p = e.get("point") or e.get("fn")
            if not isinstance(p, str):
                continue
            counts = points.setdefault(p, {"ollama": 0, "none": 0, "default": 0, "fallback": 0})
            src = e.get("source")
            if isinstance(src, str):
                counts[src] = counts.get(src, 0) + 1
        elif kind == "guard":
            dec = e.get("decision")
            if dec in ("deny", "ask", "escalate"):
                guard[dec] = guard.get(dec, 0) + 1
                recent_guard.append({
                    "at": e.get("at") or e.get("ts"),
                    "target": e.get("target"),
                    "decision": dec,
                    "reason": e.get("reason"),
                    "ticket": e.get("ticket"),
                })

    guard["recent"] = recent_guard[-10:]
    return {"points": points, "guard": guard}


def run_detail(run):
    """Return complete run detail dictionary from existing readers only.

    All reads are tolerant: missing or half-written files give empty values or null, never an exception.
    Includes status, runner liveness, stage, milestones, tickets, progress, escalations, events,
    decisions log, decision points and guard flags summary, usage totals with top spenders,
    and active waiting_limit state.
    """
    if isinstance(run, str):
        run = state.Run(run)

    # 1. Status and runner liveness
    try:
        status_info = run.status()
    except Exception:
        info = _read_json_safe(run, "run.json", {})
        status_info = {
            "run_id": getattr(run, "run_id", ""),
            "repo": info.get("repo"),
            "branch": info.get("branch"),
            "state": info.get("state"),
            "agent_calls": info.get("agent_calls", 0),
            "max_agent_calls": info.get("config", {}).get("max_agent_calls_total") if isinstance(info.get("config"), dict) else None,
            "tickets_total": 0,
            "tickets": {},
            "escalations": 0,
        }

    try:
        pid = scheduler.read_lock(run)
        alive = bool(pid and scheduler._alive(pid))
    except Exception:
        pid = None
        alive = False
    status_info["runner_alive"] = alive
    status_info["runner_pid"] = pid

    try:
        nxt = runcmds.next_step(run)
    except Exception:
        nxt = None
    status_info["next"] = nxt

    # 2. Stage
    stage = derive_stage(run)

    # 3. Destination and Out of scope
    try:
        dest, _, oos_list = milestones.destination_and_scope(run)
    except Exception:
        dest, oos_list = "(none)", []

    # 4. Milestones and Tickets
    try:
        all_milestones = milestones.milestones_of(run)
    except Exception:
        all_milestones = [{"id": "MS-00", "title": "implicit", "exit_check": None}]

    try:
        ms_states = milestones.load_states(run)
    except Exception:
        ms_states = {}

    try:
        all_tickets = milestones.merged_tickets(run)
    except Exception:
        all_tickets = []

    ms_idx = {m["id"]: i for i, m in enumerate(all_milestones)}

    sts = {}
    for t in all_tickets:
        tid = t.get("id")
        if tid:
            st_data = _get_ticket_state(run, tid)
            sts[tid] = st_data.get("status", "pending")

    milestone_entries = []
    for m in all_milestones:
        mid = m.get("id", "MS-00")
        m_title = m.get("title", "")
        gate_st = ms_states.get(mid, {}).get("status", "open")
        m_tickets = [t for t in all_tickets if t.get("milestone", "MS-00") == mid]
        m_ticket_ids = [t.get("id") for t in m_tickets if t.get("id")]
        m_closed = sum(1 for t in m_tickets if sts.get(t.get("id")) == "closed")
        m_total = len(m_tickets)
        milestone_entries.append({
            "id": mid,
            "title": m_title,
            "gate_status": gate_st,
            "exit_check": m.get("exit_check"),
            "ticket_ids": m_ticket_ids,
            "closed": m_closed,
            "total": m_total,
            "progress": "%d/%d" % (m_closed, m_total),
        })

    recent_events = read_events_tail(run, limit=500)

    ticket_entries = []
    for t in all_tickets:
        tid = t.get("id")
        st = sts.get(tid, "pending")
        reasons = milestones.ticket_waiting_for(t, all_milestones, ms_states, sts, ms_idx)
        waiting_reason = "; ".join(reasons) if reasons else None
        st_data = _get_ticket_state(run, tid) if tid else {}
        attempts = st_data.get("attempt", 0) if isinstance(st_data, dict) else 0

        last_stage, updated = _get_ticket_last_stage_and_updated(run, tid, recent_events=recent_events) if tid else (None, None)
        agent = None
        if last_stage:
            try:
                cfg = run.cfg if hasattr(run, "cfg") else _read_json_safe(run, "run.json", {}).get("config", {})
                agent = config.agent_for(cfg, last_stage)
            except Exception:
                agent = None

        ticket_entries.append({
            "id": tid,
            "title": t.get("title", ""),
            "type": t.get("type", "feature"),
            "milestone": t.get("milestone", "MS-00"),
            "depends_on": list(t.get("depends_on", [])),
            "status": st,
            "attempts": attempts,
            "last_stage": last_stage,
            "agent": agent,
            "updated": updated,
            "waiting_for": waiting_reason,
        })

    # 5. Progress counts
    closed_count = sum(1 for t in all_tickets if sts.get(t.get("id")) == "closed")
    skipped_count = sum(1 for t in all_tickets if sts.get(t.get("id")) == "skipped")
    in_progress_count = sum(1 for t in all_tickets if sts.get(t.get("id")) == "in_progress")
    awaiting_gate_count = sum(1 for t in all_tickets if sts.get(t.get("id")) == "awaiting_gate")
    blocked_count = 0
    pending_count = 0
    for t in all_tickets:
        tid = t.get("id")
        t_st = sts.get(tid, "pending")
        if t_st in ("closed", "skipped", "in_progress", "awaiting_gate"):
            continue
        reasons = milestones.ticket_waiting_for(t, all_milestones, ms_states, sts, ms_idx)
        if t_st == "blocked" or bool(reasons):
            blocked_count += 1
        else:
            pending_count += 1

    total_count = len(all_tickets)
    remaining_count = total_count - closed_count - skipped_count

    escalations = _read_json_safe(run, "escalations.json", [])
    if not isinstance(escalations, list):
        escalations = []

    progress_counts = {
        "total": total_count,
        "closed": closed_count,
        "skipped": skipped_count,
        "in_progress": in_progress_count,
        "awaiting_gate": awaiting_gate_count,
        "pending": pending_count,
        "blocked": blocked_count,
        "remaining": remaining_count,
        # escalations.json only retains unresolved/open escalations (unblock removes them)
        "escalations": len(escalations),
    }

    # 6. Events tail (last 50)
    events = recent_events[-50:]

    # 7. Decisions log (map.md text)
    decisions = _read_text_safe(run, "map.md")

    # 8. Usage summary
    try:
        from . import usage
        usage_data = usage.summarize(run)
        info = _read_json_safe(run, "run.json", {})
        usage_data = usage.with_interactive(usage_data, _interactive(run, info, usage_data))
    except Exception:
        from . import usage
        usage_data = usage.empty_summary()

    return {
        "run_id": getattr(run, "run_id", None),
        "status": status_info,
        "stage": stage,
        "destination": dest,
        "out_of_scope": oos_list,
        "milestones": milestone_entries,
        "tickets": ticket_entries,
        "progress": progress_counts,
        "escalations": escalations,
        "events": events,
        "decisions": decisions,
        "decision_points": decisions_summary(recent_events),
        "usage": usage_data,
        "waiting_limit": status_info.get("waiting_limit"),
    }


RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        self._dispatch(head=False)

    def do_HEAD(self):
        self._dispatch(head=True)

    def do_POST(self):
        self._send_405()

    def do_PUT(self):
        self._send_405()

    def do_DELETE(self):
        self._send_405()

    def do_PATCH(self):
        self._send_405()

    def do_OPTIONS(self):
        self._send_405()

    def __getattr__(self, name):
        if name.startswith("do_"):
            return self._send_405
        raise AttributeError(name)

    def _send_405(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > 0:
                self.rfile.read(min(length, 1048576))
        except Exception:
            pass
        self.close_connection = True
        self._send_json(405, {"error": "Method Not Allowed"}, head=False, headers={"Allow": "GET, HEAD"})

    def _send_json(self, status, obj, head=False, headers=None):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _serve_html(self, head=False):
        html_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
        body = None
        if os.path.isfile(html_path):
            try:
                with open(html_path, "rb") as f:
                    body = f.read()
            except OSError:
                pass
        if body is None:
            body = b"<!doctype html><html><head><title>yoke dashboard</title></head><body><h1>yoke dashboard</h1></body></html>\n"

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _serve_runs_list(self, head=False):
        home = getattr(self.server, "home", None) or config.yoke_home()
        runs = list_runs(home=home)
        self._send_json(200, runs, head=head)

    def _serve_projects(self, head=False):
        home = getattr(self.server, "home", None) or config.yoke_home()
        self._send_json(200, list_projects(home=home), head=head)

    def _serve_run_detail(self, raw_id, head=False):
        if not raw_id:
            self._send_json(404, {"error": "Run not found"}, head=head)
            return

        # Validate run id against ^[A-Za-z0-9._-]+$
        if not RUN_ID_RE.match(raw_id):
            self._send_json(400, {"error": "Invalid run id"}, head=head)
            return

        unquoted = urllib.parse.unquote(raw_id)
        if not RUN_ID_RE.match(unquoted):
            self._send_json(400, {"error": "Invalid run id"}, head=head)
            return

        # Pick the id from the directory listing, so no user-supplied string reaches a path
        home = getattr(self.server, "home", None) or config.yoke_home()
        runs_dir = os.path.join(home, "runs")
        try:
            name = next((n for n in os.listdir(runs_dir) if n == unquoted), None)
        except OSError:
            name = None
        if name is None:
            self._send_json(404, {"error": "Run not found"}, head=head)
            return
        target_dir = os.path.join(runs_dir, name)

        run_json_path = os.path.join(target_dir, "run.json")
        if not os.path.isfile(run_json_path):
            self._send_json(404, {"error": "Run not found"}, head=head)
            return

        try:
            with open(run_json_path, "r", encoding="utf-8") as f:
                info = json.load(f)
            if not isinstance(info, dict) or info.get("kind") == "research":
                self._send_json(404, {"error": "Run not found"}, head=head)
                return
        except Exception:
            self._send_json(404, {"error": "Run not found"}, head=head)
            return

        try:
            run_obj = state.Run(name, home=home)
            detail = run_detail(run_obj)
        except Exception:
            self._send_json(404, {"error": "Run not found"}, head=head)
            return

        self._send_json(200, detail, head=head)

    def _dispatch(self, head=False):
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path

        if path == "/" or path == "":
            self._serve_html(head=head)
            return

        if path == "/api/runs":
            self._serve_runs_list(head=head)
            return

        if path == "/api/projects":
            self._serve_projects(head=head)
            return

        if path.startswith("/api/runs/"):
            raw_id = path[len("/api/runs/"):]
            self._serve_run_detail(raw_id, head=head)
            return

        self._send_json(404, {"error": "Not Found"}, head=head)


class DashboardServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address, RequestHandlerClass, home=None):
        self.home = home or config.yoke_home()
        super().__init__(server_address, RequestHandlerClass)


def make_server(home=None, port=8765):
    """Create a DashboardServer bound to 127.0.0.1 on port."""
    if home is None:
        home = config.yoke_home()
    return DashboardServer(("127.0.0.1", int(port)), DashboardHandler, home=home)


def serve(home=None, port=8765, ready=None):
    """Serve local read-only web UI on 127.0.0.1 until Ctrl-C."""
    if home is None:
        home = config.yoke_home()
    try:
        httpd = make_server(home, port)
    except OSError as e:
        print("error: port %s: %s" % (port, e), file=sys.stderr)
        sys.exit(1)

    print("dashboard: http://127.0.0.1:%d" % httpd.server_address[1], flush=True)
    if callable(ready):
        ready(httpd)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0

