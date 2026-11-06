"""Git plumbing for the runner: feature branch, per-ticket worktrees, commit, rebase, ff merge (PLAN 2.9).
Everything shells out to `git -C`. The runner never pushes."""
import os
import re
import subprocess
import threading

_WT_LOCK = threading.Lock()  # concurrent `git worktree add/remove` race on shared .git files


class GitError(Exception):
    pass


def git(path, *args, check=True):
    r = subprocess.run(["git", "-C", path] + list(args), capture_output=True, text=True)
    if check and r.returncode != 0:
        raise GitError("git %s: %s" % (" ".join(args), (r.stderr or r.stdout).strip()))
    return r


def head_sha(path):
    return git(path, "rev-parse", "HEAD").stdout.strip()


def _branch_exists(repo, branch):
    return git(repo, "rev-parse", "--verify", "-q", "refs/heads/" + branch, check=False).returncode == 0


def add_worktree(repo, wt, branch, start):
    """Worktree at `wt` on new `branch` cut from `start`; reuses an existing worktree."""
    with _WT_LOCK:
        if os.path.isdir(wt):
            return
        os.makedirs(os.path.dirname(wt), exist_ok=True)
        if _branch_exists(repo, branch):
            git(repo, "worktree", "add", wt, branch)
        else:
            git(repo, "worktree", "add", "-b", branch, wt, start)


def ensure_feature_branch(repo, branch, base_sha, integration_wt):
    """Feature branch at base_sha plus the integration worktree on it (idempotent)."""
    if not _branch_exists(repo, branch):
        git(repo, "branch", branch, base_sha)
    if not os.path.isdir(integration_wt):
        os.makedirs(os.path.dirname(integration_wt), exist_ok=True)
        git(repo, "worktree", "add", integration_wt, branch)


def commit_all(wt, message, ticket_id, amend=False):
    """Stage everything and commit with the Yoke-Ticket trailer; sha, or None when nothing changed."""
    git(wt, "add", "-A")
    if git(wt, "status", "--porcelain").stdout.strip() == "":
        return None
    msg = "%s\n\nYoke-Ticket: %s\n" % (message, ticket_id)
    git(wt, "commit", "-q", *(["--amend"] if amend else []), "-m", msg)
    return head_sha(wt)


def rebase_onto(wt, onto):
    """(True, []) or, on conflict, abort the rebase and return (False, conflicted files)."""
    r = git(wt, "rebase", onto, check=False)
    if r.returncode == 0:
        return True, []
    files = git(wt, "diff", "--name-only", "--diff-filter=U", check=False).stdout.split()
    git(wt, "rebase", "--abort", check=False)
    return False, sorted(files)


def reapply_on(wt, tip):
    """After a rebase conflict: move the worktree to `tip` and re-apply the ticket commit uncommitted,
    so conflict markers sit in the working tree for the coder. Returns the conflicted files."""
    ticket_commit = head_sha(wt)
    git(wt, "reset", "-q", "--hard", tip)
    git(wt, "cherry-pick", "-n", ticket_commit, check=False)
    git(wt, "cherry-pick", "--quit", check=False)
    return sorted(git(wt, "diff", "--name-only", "--diff-filter=U", check=False).stdout.split())


def ff_merge(integration_wt, branch):
    git(integration_wt, "merge", "--ff-only", "-q", branch)


def remove_worktree(repo, wt):
    with _WT_LOCK:
        git(repo, "worktree", "remove", "--force", wt, check=False)


def agent_made_commits(wt, since_sha):
    return head_sha(wt) != since_sha


def undo_commits(wt, since_sha):
    """Drop commits made after since_sha but keep their changes staged (agents must not commit)."""
    git(wt, "reset", "--soft", since_sha)


def write_tree(wt):
    """Stage all changes with git add -A and return the tree SHA via git write-tree."""
    git(wt, "add", "-A")
    return git(wt, "write-tree").stdout.strip()


def changed_ranges(wt, tree_a, tree_b):
    """Return dict of {path: [[start, end], ...]} new-side line ranges between tree_a and tree_b."""
    # shortcut: ranges are as of that revision; later revisions can shift lines, map through later diffs if churn misses
    r = git(wt, "diff", "-U0", "--no-renames", tree_a, tree_b)
    ranges = {}
    current_file = None
    hunk_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
    for line in r.stdout.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[len("+++ b/"):]
        elif line.startswith("+++ /dev/null"):
            current_file = None
        elif current_file and line.startswith("@@ "):
            m = hunk_re.match(line)
            if m:
                c = int(m.group(1))
                d = int(m.group(2)) if m.group(2) is not None else 1
                span = [c, c] if d == 0 else [c, c + max(d, 1) - 1]
                ranges.setdefault(current_file, []).append(span)
    return ranges
