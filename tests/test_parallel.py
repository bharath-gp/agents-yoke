import json
import os
import time

from tests.test_gitops import git
from tests.test_pipeline import CODE, PipeBase, resp, ticket, val
from yoke import gitops


class TestParallel(PipeBase):
    def cfg(self, n):
        self.run_.update(config=dict(self.run_.cfg, parallel=n))

    def test_three_independent_tickets_linear_and_concurrent(self):
        self.cfg(3)
        self.setup_tickets(
            [ticket(1), ticket(2), ticket(3)],
            {"code": [resp(CODE, "c%d" % i, write={"f%d.txt" % i: "x"}, sleep=2) for i in (1, 2, 3)],
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
             "validate": [resp(val())], "router": [{"exit": 1}]})
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.log()[:2], ["T-002: ticket 2", "T-001: ticket 1"])

    def test_same_line_conflict_feedback_then_resolved(self):
        self.cfg(2)
        self.setup_tickets(
            [ticket(1), ticket(2)],
            {"code": [resp(CODE, "c1", write={"a.txt": "one\n"}), resp(CODE, "c2", write={"a.txt": "two\n"}),
                      resp(CODE, "c2", write={"a.txt": "merged\n"})],
             "validate": [resp(val())], "router": [{"exit": 1}]})
        self.assertEqual(self.go(), "done")
        attempts = sorted(self.tstate(n)["attempt"] for n in (1, 2))
        self.assertEqual(attempts, [1, 2])
        loser = 1 if self.tstate(1)["attempt"] == 2 else 2
        fb = open(self.run_.path("tickets/T-%03d/attempt-2/feedback.md" % loser)).read()
        self.assertIn("Rebase conflict", fb)
        self.assertIn("a.txt", fb)
        self.assertEqual(len(self.log()), 3)
        branch = self.run_.read_json("run.json")["branch"]
        self.assertEqual(git(self.repo, "show", branch + ":a.txt"), "merged")
        self.assertEqual(git(self.repo, "rev-list", "--merges", "--count", branch), "0")


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
        self.assertIn("<<<<<<<", open(os.path.join(wt, "a.txt")).read())
