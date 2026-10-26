import json
import os
import types
import unittest
from unittest import mock

from tests.test_pipeline import CODE, PipeBase, REV, resp, ticket, val
from yoke import hooks, scheduler

os.environ.setdefault("YOKE_OLLAMA_URL", "http://127.0.0.1:9")


class TestNotify(PipeBase):
    def read_events(self):
        events_path = self.run_.path("events.jsonl")
        self.assertTrue(os.path.exists(events_path), "events.jsonl must exist")
        with open(events_path) as f:
            return [json.loads(line) for line in f if line.strip()]

    def test_notify_env_only(self):
        out_json = os.path.join(self.tmp, "out.json")
        notify_cmd = (
            'python3 -c "import os,json; '
            'json.dump({k: os.environ[k] for k in (\'YOKE_EVENT\',\'YOKE_RUN\',\'YOKE_MESSAGE\')}, '
            'open(\'%s\',\'w\'))"' % out_json
        )
        self.run_.update(config=dict(self.run_.cfg, notify_command=notify_cmd))
        msg = 'msg with $(touch pwned) and "quotes"'
        hooks.notify(self.run_, "run_needs_user", msg)
        self.assertTrue(os.path.exists(out_json))
        with open(out_json) as f:
            data = json.load(f)
        self.assertEqual(data["YOKE_EVENT"], "run_needs_user")
        self.assertEqual(data["YOKE_RUN"], self.run_.run_id)
        self.assertEqual(data["YOKE_MESSAGE"], msg)
        self.assertFalse(os.path.exists(os.path.join(self.run_.dir, "pwned")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "pwned")))

    def test_notify_env_only_simple_namespace(self):
        out_json = os.path.join(self.tmp, "out_ns.json")
        notify_cmd = (
            'python3 -c "import os,json; '
            'json.dump({k: os.environ[k] for k in (\'YOKE_EVENT\',\'YOKE_RUN\',\'YOKE_MESSAGE\')}, '
            'open(\'%s\',\'w\'))"' % out_json
        )
        events = []
        ns = types.SimpleNamespace(
            cfg={"notify_command": notify_cmd},
            dir=self.tmp,
            run_id="ns-run-123",
            event=lambda kind, **data: events.append(dict(data, kind=kind)),
        )
        msg = 'msg with $(touch pwned) and "quotes"'
        hooks.notify(ns, "run_needs_user", msg)
        self.assertTrue(os.path.exists(out_json))
        with open(out_json) as f:
            data = json.load(f)
        self.assertEqual(data["YOKE_EVENT"], "run_needs_user")
        self.assertEqual(data["YOKE_RUN"], "ns-run-123")
        self.assertEqual(data["YOKE_MESSAGE"], msg)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "pwned")))
        self.assertEqual(events, [])

    def test_notify_failure_logs_event(self):
        self.run_.update(config=dict(self.run_.cfg, notify_command="exit 3"))
        hooks.notify(self.run_, "run_done", "all done")
        events = self.read_events()
        failed_events = [e for e in events if e.get("kind") == "notify_failed"]
        self.assertEqual(len(failed_events), 1)
        self.assertEqual(failed_events[0]["event"], "run_done")
        self.assertIn("error", failed_events[0])
        self.assertTrue(bool(failed_events[0]["error"]))

    def test_notify_exception_logs_event(self):
        self.run_.update(config=dict(self.run_.cfg, notify_command="exit 0"))
        with mock.patch("yoke.hooks.run_hook", side_effect=RuntimeError("unexpected crash")):
            hooks.notify(self.run_, "run_done", "all done")
        events = self.read_events()
        failed_events = [e for e in events if e.get("kind") == "notify_failed"]
        self.assertEqual(len(failed_events), 1)
        self.assertEqual(failed_events[0]["event"], "run_done")
        self.assertIn("unexpected crash", failed_events[0]["error"])

    def test_notify_timeout(self):
        with mock.patch("yoke.hooks.run_hook") as mock_rh:
            mock_rh.return_value = hooks.HookResult(
                status="gate_hook_error",
                tail="timed out after 10s",
                exit_code=None,
                timed_out=True,
                output="timed out after 10s",
            )
            self.run_.update(config=dict(self.run_.cfg, notify_command="sleep 100"))
            hooks.notify(self.run_, "run_done", "msg")
        events = self.read_events()
        failed_events = [e for e in events if e.get("kind") == "notify_failed"]
        self.assertEqual(len(failed_events), 1)
        self.assertEqual(failed_events[0]["event"], "run_done")
        self.assertIn("timed out", failed_events[0]["error"])

    def test_notify_null_no_call(self):
        self.run_.update(config=dict(self.run_.cfg, notify_command=None))
        with mock.patch("yoke.hooks.run_hook") as mock_run_hook:
            hooks.notify(self.run_, "run_done", "all done")
            mock_run_hook.assert_not_called()
        events = self.read_events()
        failed_events = [e for e in events if e.get("kind") == "notify_failed"]
        self.assertEqual(len(failed_events), 0)

    def test_run_end_notifies(self):
        out_json = os.path.join(self.tmp, "out_run_end.json")
        notify_cmd = (
            'python3 -c "import os,json; '
            'json.dump({k: os.environ[k] for k in (\'YOKE_EVENT\',\'YOKE_RUN\',\'YOKE_MESSAGE\')}, '
            'open(\'%s\',\'w\'))"' % out_json
        )
        self.run_.update(config=dict(self.run_.cfg, notify_command=notify_cmd))
        blocked = dict(CODE, status="blocked", blocked_reason="need creds")
        self.setup_tickets([ticket(1)], {"code": [resp(blocked)]})
        self.assertEqual(self.go(), "needs_user")
        self.assertTrue(os.path.exists(out_json))
        with open(out_json) as f:
            data = json.load(f)
        self.assertEqual(data["YOKE_EVENT"], "run_needs_user")
        self.assertEqual(data["YOKE_RUN"], self.run_.run_id)
        self.assertIn("need creds", data["YOKE_MESSAGE"])

    def test_run_end_notifies_on_done(self):
        out_json = os.path.join(self.tmp, "out_done.json")
        notify_cmd = (
            'python3 -c "import os,json; '
            'json.dump({k: os.environ[k] for k in (\'YOKE_EVENT\',\'YOKE_RUN\',\'YOKE_MESSAGE\')}, '
            'open(\'%s\',\'w\'))"' % out_json
        )
        self.run_.update(config=dict(self.run_.cfg, notify_command=notify_cmd))
        self.setup_tickets(
            [ticket(1)],
            {"code": [resp(CODE, "c1", write={"f.txt": "x"})],
             "review": [resp(REV)],
             "validate": [resp(val())],
             "router": [{"exit": 1}]}
        )
        self.assertEqual(self.go(), "done")
        self.assertTrue(os.path.exists(out_json))
        with open(out_json) as f:
            data = json.load(f)
        self.assertEqual(data["YOKE_EVENT"], "run_done")
        self.assertEqual(data["YOKE_RUN"], self.run_.run_id)
        self.assertEqual(data["YOKE_MESSAGE"], "run %s done" % self.run_.run_id)

    def test_run_end_needs_user_empty_escalations(self):
        out_json = os.path.join(self.tmp, "out_empty_esc.json")
        notify_cmd = (
            'python3 -c "import os,json; '
            'json.dump({k: os.environ[k] for k in (\'YOKE_EVENT\',\'YOKE_RUN\',\'YOKE_MESSAGE\')}, '
            'open(\'%s\',\'w\'))"' % out_json
        )
        self.run_.update(config=dict(self.run_.cfg, notify_command=notify_cmd))
        self.setup_tickets(
            [ticket(1)],
            {"code": [resp(CODE, "c1", write={"f.txt": "x"})],
             "review": [resp(REV)],
             "validate": [resp(val())],
             "router": [{"exit": 1}]}
        )
        with mock.patch("yoke.scheduler._loop", return_value="needs_user"):
            self.assertEqual(self.go(), "needs_user")
        self.assertTrue(os.path.exists(out_json))
        with open(out_json) as f:
            data = json.load(f)
        self.assertEqual(data["YOKE_EVENT"], "run_needs_user")
        self.assertEqual(data["YOKE_RUN"], self.run_.run_id)
        self.assertEqual(data["YOKE_MESSAGE"], "run %s needs you: needs user" % self.run_.run_id)

    def test_scheduler_swallows_notify_exception(self):
        self.run_.update(config=dict(self.run_.cfg, notify_command="exit 0"))
        self.setup_tickets(
            [ticket(1)],
            {"code": [resp(CODE, "c1", write={"f.txt": "x"})],
             "review": [resp(REV)],
             "validate": [resp(val())],
             "router": [{"exit": 1}]}
        )
        with mock.patch("yoke.hooks.notify", side_effect=RuntimeError("notify explosion")):
            state = self.go()
        self.assertEqual(state, "done")


if __name__ == "__main__":
    unittest.main()
