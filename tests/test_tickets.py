import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

from yoke import adapters, cli, config, state, tickets
try:
    from tests.test_core import FAKE_CLI
except ImportError:
    from test_router import FAKE_CLI


def tk(i, deps=(), **kw):
    t = {"id": "T-%03d" % i, "title": "t%d" % i, "type": "feature", "description": "d",
         "exit_criteria": ["ok"], "depends_on": list(deps), "files_hint": [], "plan_excerpt": ""}
    t.update(kw)
    return t


class TestTickets(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "repo"))
        cfg = config.load(home=self.tmp, overrides={"max_tickets": 3, "kill_grace_s": 1,
                                                    "router": {"ollama_url": "http://127.0.0.1:1"}})
        self.run_ = state.Run.create(os.path.join(self.tmp, "repo"), cfg, home=self.tmp)

    def imp(self, ts, adapter=None):
        p = os.path.join(self.tmp, "in.json")
        with open(p, "w") as f:
            json.dump({"tickets": ts}, f)
        return tickets.import_tickets(self.run_, p, adapter)

    def test_ok_writes_and_invalidates_approval(self):
        self.run_.write_json("approvals.json", {"plan": {"sha256": "a"}, "tickets": {"sha256": "b"}})
        errs, ts = self.imp([tk(1), tk(2, ["T-001"])])
        self.assertEqual(errs, [])
        self.assertEqual(len(self.run_.read_json("tickets.json")["tickets"]), 2)
        self.assertEqual(list(self.run_.read_json("approvals.json")), ["plan"])

    def test_rejects(self):
        cases = {
            "duplicate": [tk(1), tk(1)],
            "unknown": [tk(1, ["T-009"])],
            "cycle": [tk(1, ["T-002"]), tk(2, ["T-001"])],
            "max_tickets": [tk(i) for i in range(1, 5)],
            "exit_criteria": [tk(1, exit_criteria=[])],
            "does not match": [tk(1, id="X1")],
        }
        for needle, ts in cases.items():
            errs, out = self.imp(ts)
            self.assertIsNone(out, needle)
            self.assertTrue(any(needle in e for e in errs), (needle, errs))
        self.assertFalse(os.path.exists(self.run_.path("tickets.json")))

    def test_cycle_message_and_diamond_ok(self):
        self.assertEqual(tickets.find_cycle([tk(1, ["T-002"]), tk(2, ["T-003"]), tk(3, ["T-001"])]),
                         ["T-001", "T-002", "T-003", "T-001"])
        self.assertIsNone(tickets.find_cycle([tk(1), tk(2, ["T-001"]), tk(3, ["T-001"]), tk(4, ["T-002", "T-003"])]))

    def test_missing_type_is_classified_via_router(self):
        script = os.path.join(self.tmp, "s.json")
        with open(script, "w") as f:
            json.dump({"router": [{"structured_output": {"type": "docs", "confidence": 0.9, "reason": "x"}}]}, f)
        os.environ["FAKE_SCRIPT"] = script
        self.addCleanup(os.environ.pop, "FAKE_SCRIPT", None)
        t = tk(1)
        del t["type"]
        errs, ts = self.imp([t, tk(2, type="bogus")], adapters.FakeAdapter(FAKE_CLI))
        self.assertEqual(errs, [])
        self.assertEqual([x["type"] for x in ts], ["docs", "docs"])

    def test_cli_import_show_route(self):
        p = os.path.join(self.tmp, "in.json")
        with open(p, "w") as f:
            json.dump({"tickets": [tk(1), tk(2, ["T-001"], type="test")]}, f)
        run = ["--run", self.run_.run_id]
        os.environ["YOKE_HOME"] = self.tmp
        self.addCleanup(os.environ.pop, "YOKE_HOME", None)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main(["tickets", "import", p] + run), 0)
            self.assertEqual(cli.main(["tickets", "show"] + run), 0)
            self.assertEqual(cli.main(["route", "T-002"] + run), 0)
        out = buf.getvalue()
        self.assertIn("T-002 [test] t2  (after T-001)", out)
        self.assertIn('"stage": "qa"', out)


if __name__ == "__main__":
    unittest.main()
