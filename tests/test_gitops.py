import os
import subprocess
import unittest

from tests.test_core import Tmp
from yoke import gitops

ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_COUNT": "0",
}


def git(repo, *args):
    return subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=True, check=True).stdout.strip()


class GitBase(Tmp):
    def setUp(self):
        super().setUp()
        cleanups = []
        for k in list(os.environ):
            if k.startswith("GIT_CONFIG_KEY_") or k.startswith("GIT_CONFIG_VALUE_"):
                old = os.environ.pop(k)
                cleanups.append((k, old))
        for k, v in ENV.items():
            old = os.environ.get(k)
            os.environ[k] = v
            cleanups.append((k, old))
        for k, old in reversed(cleanups):
            self.addCleanup(lambda k=k, old=old: os.environ.__setitem__(k, old) if old is not None else os.environ.pop(k, None))
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        git(self.repo, "init", "-q", "-b", "main")
        self.put(self.repo, "a.txt", "a\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "init")
        self.base = gitops.head_sha(self.repo)
        self.int = os.path.join(self.tmp, "wt", "_integration")
        gitops.ensure_feature_branch(self.repo, "yoke/r1", self.base, self.int)

    def put(self, d, name, text):
        with open(os.path.join(d, name), "w") as f:
            f.write(text)


class TestGitops(GitBase):
    def test_feature_branch_idempotent(self):
        gitops.ensure_feature_branch(self.repo, "yoke/r1", self.base, self.int)
        self.assertEqual(git(self.int, "rev-parse", "--abbrev-ref", "HEAD"), "yoke/r1")

    def test_commit_merge_linear(self):
        wts = []
        for n in ("T-001", "T-002"):
            wt = os.path.join(self.tmp, "wt", n)
            gitops.add_worktree(self.repo, wt, "yoke/r1-" + n, "yoke/r1")
            self.put(wt, n + ".txt", n)
            self.assertTrue(gitops.commit_all(wt, n + ": x", n))
            wts.append((wt, "yoke/r1-" + n))
        for wt, br in wts:
            self.assertEqual(gitops.rebase_onto(wt, "yoke/r1"), (True, []))
            gitops.ff_merge(self.int, br)
        log = git(self.repo, "log", "--format=%s", "--first-parent", "yoke/r1").splitlines()
        self.assertEqual(log, ["T-002: x", "T-001: x", "init"])
        self.assertEqual(git(self.repo, "rev-list", "--merges", "--count", "yoke/r1"), "0")
        self.assertIn("Yoke-Ticket: T-001", git(self.repo, "log", "-1", "--format=%B", "yoke/r1-T-001"))

    def test_commit_nothing_returns_none_and_amend(self):
        wt = os.path.join(self.tmp, "wt", "T-001")
        gitops.add_worktree(self.repo, wt, "yoke/r1-T-001", "yoke/r1")
        self.assertIsNone(gitops.commit_all(wt, "m", "T-001"))
        self.put(wt, "x", "1")
        first = gitops.commit_all(wt, "m", "T-001")
        self.put(wt, "x", "2")
        second = gitops.commit_all(wt, "m", "T-001", amend=True)
        self.assertNotEqual(first, second)
        self.assertEqual(git(wt, "rev-list", "--count", "yoke/r1..HEAD"), "1")

    def test_rebase_conflict_aborts(self):
        wts = []
        for n in ("T-001", "T-002"):
            wt = os.path.join(self.tmp, "wt", n)
            gitops.add_worktree(self.repo, wt, "yoke/r1-" + n, "yoke/r1")
            self.put(wt, "a.txt", "changed by %s\n" % n)
            gitops.commit_all(wt, n, n)
            wts.append((wt, "yoke/r1-" + n))
        gitops.ff_merge(self.int, wts[0][1])
        ok, files = gitops.rebase_onto(wts[1][0], "yoke/r1")
        self.assertFalse(ok)
        self.assertEqual(files, ["a.txt"])
        self.assertEqual(git(wts[1][0], "status", "--porcelain"), "")  # aborted cleanly

    def test_agent_made_commits_and_remove(self):
        wt = os.path.join(self.tmp, "wt", "T-001")
        gitops.add_worktree(self.repo, wt, "yoke/r1-T-001", "yoke/r1")
        since = gitops.head_sha(wt)
        self.assertFalse(gitops.agent_made_commits(wt, since))
        self.put(wt, "x", "1")
        git(wt, "add", "-A")
        git(wt, "commit", "-qm", "sneaky")
        self.assertTrue(gitops.agent_made_commits(wt, since))
        gitops.remove_worktree(self.repo, wt)
        self.assertFalse(os.path.exists(wt))


if __name__ == "__main__":
    unittest.main()
