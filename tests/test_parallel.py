import json
import os
import time

from tests.test_gitops import git
from tests.test_pipeline import CODE, PipeBase, REV, resp, ticket, val
from yoke import gitops

os.environ.setdefault("YOKE_OLLAMA_URL", "http://127.0.0.1:9")


class TestParallel(PipeBase):
    def cfg(self, n):
        self.run_.update(config=dict(self.run_.cfg, parallel=n))

    def test_three_independent_tickets_linear_and_concurrent(self):
        self.cfg(3)
        self.setup_tickets(
            [ticket(1), ticket(2), ticket(3)],
            {"code": [resp(CODE, "c%d" % i, write={"f%d.txt" % i: "x"}, sleep=2) for i in (1, 2, 3)],
             "review": [resp(REV)] * 3,
             "validate": [resp(val())], "router": [{"exit": 1}]})
        t0 = time.time()
        self.assertEqual(self.go(), "done")
        self.assertLess(time.time() - t0, 5.0)  # sequential would be >= 6s of coder sleep alone
        branch = self.run_.read_json("run.json")["branch"]
        self.assertEqual(len(self.log()), 4)
        self.assertEqual(git(self.repo, "rev-list", "--merges", "--count", branch), "0")
        for n in (1, 2, 3):
            self.assertEqual(self.tstate(n)["status"], "closed")

    def int_wt(self):
        return self.run_.path("worktrees/_integration")

    def test_dependent_waits_for_dependency(self):
        self.cfg(3)
        self.setup_tickets(
            [ticket(1), ticket(2, deps=["T-001"])],
            {"code": [resp(CODE, "c1", write={"f1.txt": "x"}), resp(CODE, "c2", write={"f2.txt": "x"})],
             "review": [resp(REV)] * 2,
             "validate": [resp(val())], "router": [{"exit": 1}]})
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.log()[:2], ["T-002: ticket 2", "T-001: ticket 1"])

    def test_same_line_conflict_feedback_then_resolved(self):
        self.cfg(2)
        self.setup_tickets(
            [ticket(1), ticket(2)],
            {"code": [resp(CODE, "c1", write={"a.txt": "one\n"}), resp(CODE, "c2", write={"a.txt": "two\n"}),
                      resp(CODE, "c2", write={"a.txt": "merged\n"})],
             "review": [resp(REV)] * 3,
             "validate": [resp(val())], "router": [{"exit": 1}]})
        self.assertEqual(self.go(), "done")
        attempts = sorted(self.tstate(n)["attempt"] for n in (1, 2))
        self.assertEqual(attempts, [1, 2])
        loser = 1 if self.tstate(1)["attempt"] == 2 else 2
        with open(self.run_.path("tickets/T-%03d/attempt-2/feedback.md" % loser)) as f:
            fb = f.read()
        self.assertIn("Rebase conflict", fb)
        self.assertIn("a.txt", fb)
        self.assertEqual(len(self.log()), 3)
        branch = self.run_.read_json("run.json")["branch"]
        self.assertEqual(git(self.repo, "show", branch + ":a.txt"), "merged")
        self.assertEqual(git(self.repo, "rev-list", "--merges", "--count", branch), "0")

    def test_overlapping_tickets_never_run_concurrently_with_parallel_2(self):
        self.cfg(2)
        t1 = ticket(1)
        t1["files_hint"] = ["yoke/scheduler.py"]
        t2 = ticket(2)
        t2["files_hint"] = ["yoke/scheduler.py"]
        self.setup_tickets(
            [t1, t2],
            {"code": [resp(CODE, "c1", write={"f1.txt": "x"}, sleep=1),
                      resp(CODE, "c2", write={"f2.txt": "x"}, sleep=1)],
             "review": [resp(REV)] * 2,
             "validate": [resp(val())], "router": [{"exit": 1}]})
        t0 = time.time()
        self.assertEqual(self.go(), "done")
        self.assertGreaterEqual(time.time() - t0, 1.8)
        with open(self.run_.path("events.jsonl")) as f:
            events = [json.loads(l) for l in f if l.strip()]
        deferred = [e for e in events if e.get("kind") == "ticket_deferred"]
        self.assertEqual(len(deferred), 1)
        self.assertEqual(deferred[0]["ticket"], "T-002")
        self.assertEqual(deferred[0]["running"], "T-001")
        self.assertEqual(deferred[0]["overlaps"], "T-001")
        t1_close = [i for i, e in enumerate(events) if e.get("ticket") == "T-001" and e.get("kind") == "close_gate"]
        t2_start = [i for i, e in enumerate(events) if e.get("ticket") == "T-002" and e.get("kind") == "call_start"]
        self.assertTrue(t1_close and t2_start)
        self.assertLess(t1_close[0], t2_start[0])
        for n in (1, 2):
            self.assertEqual(self.tstate(n)["status"], "closed")

    def test_directory_prefix_overlapping_tickets_deferred(self):
        self.cfg(2)
        t1 = ticket(1)
        t1["files_hint"] = ["yoke"]
        t2 = ticket(2)
        t2["files_hint"] = ["yoke/scheduler.py"]
        self.setup_tickets(
            [t1, t2],
            {"code": [resp(CODE, "c1", write={"f1.txt": "x"}, sleep=1),
                      resp(CODE, "c2", write={"f2.txt": "x"}, sleep=1)],
             "review": [resp(REV)] * 2,
             "validate": [resp(val())], "router": [{"exit": 1}]})
        t0 = time.time()
        self.assertEqual(self.go(), "done")
        self.assertGreaterEqual(time.time() - t0, 1.8)
        with open(self.run_.path("events.jsonl")) as f:
            events = [json.loads(l) for l in f if l.strip()]
        deferred = [e for e in events if e.get("kind") == "ticket_deferred"]
        self.assertEqual(len(deferred), 1)
        self.assertEqual(deferred[0]["ticket"], "T-002")
        self.assertEqual(deferred[0]["running"], "T-001")
        t1_close = [i for i, e in enumerate(events) if e.get("ticket") == "T-001" and e.get("kind") == "close_gate"]
        t2_start = [i for i, e in enumerate(events) if e.get("ticket") == "T-002" and e.get("kind") == "call_start"]
        self.assertTrue(t1_close and t2_start)
        self.assertLess(t1_close[0], t2_start[0])
        for n in (1, 2):
            self.assertEqual(self.tstate(n)["status"], "closed")

    def test_disjoint_tickets_run_concurrently(self):
        self.cfg(2)
        t1 = ticket(1)
        t1["files_hint"] = ["yoke/scheduler.py"]
        t2 = ticket(2)
        t2["files_hint"] = ["yoke/pipeline.py"]
        self.setup_tickets(
            [t1, t2],
            {"code": [resp(CODE, "c1", write={"f1.txt": "x"}, sleep=1),
                      resp(CODE, "c2", write={"f2.txt": "x"}, sleep=1)],
             "review": [resp(REV)] * 2,
             "validate": [resp(val())], "router": [{"exit": 1}]})
        t0 = time.time()
        self.assertEqual(self.go(), "done")
        self.assertLess(time.time() - t0, 3.5)
        with open(self.run_.path("events.jsonl")) as f:
            events = [json.loads(l) for l in f if l.strip()]
        deferred = [e for e in events if e.get("kind") == "ticket_deferred"]
        self.assertEqual(len(deferred), 0)
        t1_start = [i for i, e in enumerate(events) if e.get("ticket") == "T-001" and e.get("kind") == "call_start"]
        t2_start = [i for i, e in enumerate(events) if e.get("ticket") == "T-002" and e.get("kind") == "call_start"]
        t1_end = [i for i, e in enumerate(events) if e.get("ticket") == "T-001" and e.get("kind") == "call_end"]
        self.assertTrue(t1_start and t2_start and t1_end)
        self.assertLess(t2_start[0], t1_end[0])
        for n in (1, 2):
            self.assertEqual(self.tstate(n)["status"], "closed")

    def test_empty_files_hint_never_blocks(self):
        self.cfg(2)
        t1 = ticket(1)
        t1["files_hint"] = []
        t2 = ticket(2)
        t2["files_hint"] = []
        self.setup_tickets(
            [t1, t2],
            {"code": [resp(CODE, "c1", write={"f1.txt": "x"}, sleep=1),
                      resp(CODE, "c2", write={"f2.txt": "x"}, sleep=1)],
             "review": [resp(REV)] * 2,
             "validate": [resp(val())], "router": [{"exit": 1}]})
        t0 = time.time()
        self.assertEqual(self.go(), "done")
        self.assertLess(time.time() - t0, 3.5)
        with open(self.run_.path("events.jsonl")) as f:
            events = [json.loads(l) for l in f if l.strip()]
        deferred = [e for e in events if e.get("kind") == "ticket_deferred"]
        self.assertEqual(len(deferred), 0)
        t1_start = [i for i, e in enumerate(events) if e.get("ticket") == "T-001" and e.get("kind") == "call_start"]
        t2_start = [i for i, e in enumerate(events) if e.get("ticket") == "T-002" and e.get("kind") == "call_start"]
        t1_end = [i for i, e in enumerate(events) if e.get("ticket") == "T-001" and e.get("kind") == "call_end"]
        self.assertTrue(t1_start and t2_start and t1_end)
        self.assertLess(t2_start[0], t1_end[0])
        for n in (1, 2):
            self.assertEqual(self.tstate(n)["status"], "closed")

    def test_parallel_1_never_defers_even_with_overlap(self):
        self.cfg(1)
        t1 = ticket(1)
        t1["files_hint"] = ["yoke/scheduler.py"]
        t2 = ticket(2)
        t2["files_hint"] = ["yoke/scheduler.py"]
        self.setup_tickets(
            [t1, t2],
            {"code": [resp(CODE, "c1", write={"f1.txt": "x"}),
                      resp(CODE, "c2", write={"f2.txt": "x"})],
             "review": [resp(REV)] * 2,
             "validate": [resp(val())], "router": [{"exit": 1}]})
        self.assertEqual(self.go(), "done")
        with open(self.run_.path("events.jsonl")) as f:
            events = [json.loads(l) for l in f if l.strip()]
        deferred = [e for e in events if e.get("kind") == "ticket_deferred"]
        self.assertEqual(len(deferred), 0)
        for n in (1, 2):
            self.assertEqual(self.tstate(n)["status"], "closed")


class TestHintsOverlap(PipeBase):
    def test_hints_overlap_logic(self):
        from yoke import milestones
        self.assertTrue(milestones.hints_overlap(["a.py"], ["a.py"]))
        self.assertTrue(milestones.hints_overlap(["yoke/scheduler.py"], ["yoke"]))
        self.assertTrue(milestones.hints_overlap(["yoke"], ["yoke/scheduler.py"]))
        self.assertTrue(milestones.hints_overlap(["yoke/"], ["yoke/scheduler.py"]))
        self.assertTrue(milestones.hints_overlap(["yoke/scheduler.py"], ["yoke/"]))
        self.assertTrue(milestones.hints_overlap(["yoke\\scheduler.py"], ["yoke/scheduler.py"]))
        self.assertFalse(milestones.hints_overlap(["yoke"], ["yoke_foo.py"]))
        self.assertFalse(milestones.hints_overlap(["yoke/scheduler.py"], ["yoke/pipeline.py"]))
        self.assertFalse(milestones.hints_overlap([], []))
        self.assertFalse(milestones.hints_overlap(["a.py"], []))
        self.assertFalse(milestones.hints_overlap([], ["a.py"]))
        self.assertFalse(milestones.hints_overlap([""], ["a.py"]))
        self.assertFalse(milestones.hints_overlap(["."], ["a.py"]))
        self.assertFalse(milestones.hints_overlap(None, ["a.py"]))
        self.assertFalse(milestones.hints_overlap(["a.py"], None))


class TestGitopsConflict(PipeBase):
    def test_reapply_leaves_markers_on_tip(self):
        wt = os.path.join(self.tmp, "wt", "T-001")
        other = os.path.join(self.tmp, "wt", "T-002")
        for w, n in ((wt, "T-001"), (other, "T-002")):
            gitops.add_worktree(self.repo, w, "x-" + n, "main")
            with open(os.path.join(w, "a.txt"), "w") as f:
                f.write("by %s\n" % n)
            gitops.commit_all(w, n, n)
        tip = gitops.head_sha(other)
        files = gitops.reapply_on(wt, tip)
        self.assertEqual(files, ["a.txt"])
        self.assertEqual(gitops.head_sha(wt), tip)
        with open(os.path.join(wt, "a.txt")) as f:
            self.assertIn("<<<<<<<", f.read())
