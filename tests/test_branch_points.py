import json
import os
import unittest
from unittest import mock

from tests.test_pipeline import CODE, DOCS, PipeBase, REV, qa, resp, ticket, val
from yoke import pipeline, router

os.environ.setdefault("YOKE_OLLAMA_URL", "http://127.0.0.1:9")


def choice(c, conf):
    return {"type": "choice", "choice": c, "confidence": conf, "probabilities": {c: conf}}


class _OllamaPatcher:
    def __init__(self, answers_by_key):
        self.answers_by_key = answers_by_key

        def fake_ollama(cfg, state, questions, timeout=60):
            return {k: self.answers_by_key[k] for k in questions if k in self.answers_by_key}

        self._patcher = mock.patch("yoke.router._ollama", side_effect=fake_ollama)
        self._active = False

    def __enter__(self):
        if not self._active:
            self.start()
        return self

    def __exit__(self, *args):
        self.stop()

    def start(self):
        if not self._active:
            self._patcher.start()
            self._active = True
        return self

    def stop(self):
        if self._active:
            self._patcher.stop()
            self._active = False


def patch_ollama(answers_by_key):
    return _OllamaPatcher(answers_by_key)


class BranchTest(PipeBase):
    def patch_ollama(self, answers_by_key):
        patcher = patch_ollama(answers_by_key)
        patcher.start()
        self.addCleanup(patcher.stop)
        return patcher

    def test_testware_restarts_at_qa_not_code(self):
        self.patch_ollama({"category": choice("testware", 0.9)})
        self.setup_tickets(
            [ticket(1, type_="refactor")],
            {
                "code": [resp(CODE, "c1", write={"f1.txt": "x"})],
                "review": [resp(REV)] * 4,
                "qa": [
                    resp(qa("false", status="pass")),
                    resp(qa("true", status="pass")),
                ],
                "validate": [resp(val())] * 4,
                "router": [{"exit": 1}],
            },
        )
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["status"], "closed")
        self.assertNotIn("start_stage", self.tstate(1))
        with open(self.script + ".pos") as f:
            pos = json.load(f)
        self.assertEqual(pos.get("code", 0), 1)
        self.assertEqual(pos.get("qa", 0), 2)
        fb = pipeline._read_feedback(self.run_, "T-001", 2)
        self.assertTrue(fb.startswith("Decision: testware failure, QA reruns.\n"))
        with open(self.run_.path("events.jsonl")) as f:
            events = [json.loads(line) for line in f if line.strip()]
        stage_skip_events = [e for e in events if e.get("kind") == "stage_skip"]
        self.assertEqual(len(stage_skip_events), 1)
        self.assertEqual(stage_skip_events[0]["start_stage"], "qa")
        self.assertEqual(stage_skip_events[0]["ticket"], "T-001")

    def test_environment_reruns_once_then_escalates(self):
        self.patch_ollama({"category": choice("environment", 0.9)})
        self.setup_tickets(
            [ticket(1, type_="refactor")],
            {
                "code": [resp(CODE, "c1", write={"f1.txt": "x"})],
                "review": [resp(REV)] * 4,
                "qa": [resp(qa("false", status="pass"))],
                "validate": [resp(val())] * 4,
                "router": [{"exit": 1}],
            },
        )
        self.assertEqual(self.go(), "needs_user")
        self.assertEqual(self.tstate(1)["status"], "blocked")
        self.assertEqual(self.tstate(1)["attempt"], 1)
        self.assertTrue(self.tstate(1).get("env_rerun"))
        with open(self.script + ".pos") as f:
            pos = json.load(f)
        self.assertEqual(pos.get("code", 0), 1)
        self.assertEqual(pos.get("qa", 0), 1)
        esc = self.run_.read_json("escalations.json")
        self.assertTrue(any("environment failure" in e["reason"] for e in esc))
        with open(self.run_.path("events.jsonl")) as f:
            events = [json.loads(line) for line in f if line.strip()]
        esc_events = [e for e in events if e.get("kind") == "escalation"]
        self.assertTrue(any("environment failure" in e["reason"] for e in esc_events))

    def test_environment_rerun_passes(self):
        self.patch_ollama({"category": choice("environment", 0.9)})
        self.setup_tickets(
            [ticket(1, type_="refactor")],
            {
                "code": [resp(CODE, "c1", write={"f1.txt": "x"})],
                "review": [resp(REV)] * 4,
                "qa": [resp(qa("test -f m || (touch m; false)", status="pass"))],
                "validate": [resp(val())] * 4,
                "router": [{"exit": 1}],
            },
        )
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["status"], "closed")
        self.assertEqual(self.tstate(1)["attempt"], 1)
        with open(self.script + ".pos") as f:
            pos = json.load(f)
        self.assertEqual(pos.get("code", 0), 1)
        self.assertEqual(pos.get("qa", 0), 1)
        self.assertTrue(self.tstate(1).get("env_rerun"))
        self.assertEqual(pipeline._read_feedback(self.run_, "T-001", 2), "")

    def test_below_threshold_keeps_old_flow(self):
        self.patch_ollama({"category": choice("testware", 0.5)})
        self.setup_tickets(
            [ticket(1, type_="refactor")],
            {
                "code": [
                    resp(CODE, "c1", write={"f1.txt": "x"}),
                    resp(CODE, "c1", write={"f1.txt": "y"}),
                ],
                "review": [resp(REV)] * 4,
                "qa": [
                    resp(qa("false", status="pass")),
                    resp(qa("true", status="pass")),
                ],
                "validate": [resp(val())] * 4,
                "router": [{"exit": 1}],
            },
        )
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["status"], "closed")
        self.assertNotIn("start_stage", self.tstate(1))
        with open(self.script + ".pos") as f:
            pos = json.load(f)
        self.assertEqual(pos.get("code", 0), 2)

    def test_product_failure_keeps_old_flow(self):
        self.patch_ollama({"category": choice("product", 0.9)})
        self.setup_tickets(
            [ticket(1, type_="refactor")],
            {
                "code": [
                    resp(CODE, "c1", write={"f1.txt": "x"}),
                    resp(CODE, "c1", write={"f1.txt": "y"}),
                ],
                "review": [resp(REV)] * 4,
                "qa": [
                    resp(qa("false", status="pass")),
                    resp(qa("true", status="pass")),
                ],
                "validate": [resp(val())] * 4,
                "router": [{"exit": 1}],
            },
        )
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["status"], "closed")
        self.assertNotIn("start_stage", self.tstate(1))
        with open(self.script + ".pos") as f:
            pos = json.load(f)
        self.assertEqual(pos.get("code", 0), 2)

    def test_start_stage_ignored_when_results_missing(self):
        self.setup_tickets(
            [ticket(1, type_="refactor")],
            {
                "code": [resp(CODE, "c1", write={"f1.txt": "x"})],
                "review": [resp(REV)] * 4,
                "qa": [resp(qa("true", status="pass"))],
                "validate": [resp(val())] * 4,
                "router": [{"exit": 1}],
            },
        )
        st = pipeline.ticket_state(self.run_, "T-001")
        st["start_stage"] = "qa"
        st["last_results"] = {}
        pipeline.save_state(self.run_, "T-001", st)

        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["status"], "closed")
        self.assertNotIn("start_stage", self.tstate(1))
        with open(self.script + ".pos") as f:
            pos = json.load(f)
        self.assertEqual(pos.get("code", 0), 1)
        with open(self.run_.path("events.jsonl")) as f:
            events = [json.loads(line) for line in f if line.strip()]
        stage_skip_events = [e for e in events if e.get("kind") == "stage_skip"]
        self.assertEqual(len(stage_skip_events), 0)

    def test_docs_start_keeps_qa_gate(self):
        self.setup_tickets(
            [ticket(1, type_="feature")],
            {
                "docs": [resp(DOCS)],
                "router": [{"exit": 1}],
            },
        )
        st = pipeline.ticket_state(self.run_, "T-001")
        st["start_stage"] = "docs"
        st["last_results"] = {
            "code": CODE,
            "review": REV,
            "qa": qa("true", status="pass"),
            "validate": val(ok=True),
        }
        st["last_test_cmd"] = "true"
        st["last_rerun_rc"] = 0
        pipeline.save_state(self.run_, "T-001", st)

        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["status"], "closed")
        self.assertNotIn("start_stage", self.tstate(1))
        self.assertEqual(self.tstate(1).get("last_test_cmd"), "true")
        self.assertEqual(self.tstate(1).get("last_rerun_rc"), 0)
        with open(self.script + ".pos") as f:
            pos = json.load(f)
        self.assertEqual(pos.get("code", 0), 0)
        self.assertEqual(pos.get("qa", 0), 0)
        self.assertEqual(pos.get("docs", 0), 1)
        with open(self.run_.path("events.jsonl")) as f:
            events = [json.loads(line) for line in f if line.strip()]
        stage_skip_events = [e for e in events if e.get("kind") == "stage_skip"]
        self.assertEqual(len(stage_skip_events), 1)
        self.assertEqual(stage_skip_events[0]["start_stage"], "docs")
        self.assertEqual(stage_skip_events[0]["ticket"], "T-001")
        gates = [e for e in events if e.get("kind") == "close_gate"]
        self.assertTrue(len(gates) >= 1)
        self.assertTrue(gates[-1]["gate"]["qa"])

    def test_router_test_failure_category_structure(self):
        recorded = {}

        def fake_decide(cfg, point, state, q, parse, run):
            recorded["point"] = point
            recorded["state"] = state
            recorded["q"] = q
            res, conf = parse({"category": {"choice": "product", "confidence": 0.85}})
            return {"point": point, "result": res, "confidence": conf, "source": "ollama"}

        with mock.patch("yoke.router.decide", side_effect=fake_decide):
            out = router.test_failure_category(self.run_, "Ticket string", "pytest", "test failed")
            self.assertEqual(recorded["point"], "test_failure")
            self.assertIn("category", recorded["q"])
            self.assertEqual(recorded["q"]["category"]["type"], "choice")
            self.assertEqual(
                set(recorded["q"]["category"]["criteria"]),
                {"product", "testware", "environment"},
            )
            self.assertEqual(out["result"], "product")
            self.assertEqual(out["confidence"], 0.85)

            out_dict = router.test_failure_category(
                self.run_, {"title": "Test Title"}, "pytest", "test failed"
            )
            self.assertIn("Test Title", recorded["state"])
            self.assertEqual(out_dict["result"], "product")

            long_tail = "x" * (router.STATE_CHARS + 200)
            router.test_failure_category(self.run_, "Ticket string", "pytest", long_tail)
            self.assertLessEqual(len(recorded["state"]), router.STATE_CHARS)

    def test_router_test_failure_category_with_ollama(self):
        self.patch_ollama({"category": choice("testware", 0.95)})
        res = router.test_failure_category(self.run_, {"title": "T"}, "false", "tail output")
        self.assertEqual(res["point"], "test_failure")
        self.assertEqual(res["source"], "ollama")
        self.assertEqual(res["result"], "testware")
        self.assertEqual(res["confidence"], 0.95)


if __name__ == "__main__":
    unittest.main()
