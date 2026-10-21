#!/usr/bin/env bash
# Manual end-to-end: real claude + agy (+ ollama) on a disposable repo; spends real quota.
# Usage: tests/e2e.sh [normal|fallback]   (fallback = router's Ollama URL pointed at a dead port,
# so the router falls back to the cheap agent; the Ollama server itself is left alone)
# Checks: run done, 3 commits on the feature branch, nothing pushed, every audit dir complete.
set -euo pipefail
MODE=${1:-normal}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
W=$(mktemp -d "${TMPDIR:-/tmp}/yoke-e2e.XXXXXX")
export YOKE_HOME=$W/home PYTHONPATH=$ROOT
export GIT_AUTHOR_NAME=e2e GIT_AUTHOR_EMAIL=e2e@example.com GIT_COMMITTER_NAME=e2e GIT_COMMITTER_EMAIL=e2e@example.com
y() { python3 -m yoke.cli "$@"; }
fail() { echo "E2E FAIL: $*" >&2; echo "workdir: $W" >&2; exit 1; }

git init -q --bare "$W/origin.git"
REPO=$W/repo; mkdir -p "$REPO/tinylib" "$REPO/tests"; cd "$REPO"
git init -q -b main; git remote add origin "$W/origin.git"
touch tinylib/__init__.py
cat > tests/test_smoke.py <<'PY'
import unittest

class T(unittest.TestCase):
    def test_import(self):
        import tinylib
PY
git add -A; git commit -qm init; BASE=$(git rev-parse HEAD)

echo "Build a tiny python library in tinylib/: stats helpers, a slugify helper, and a package export." > "$W/req.md"
RUN=$(y init --repo "$REPO" --requirements "$W/req.md")
python3 - "$YOKE_HOME/runs/$RUN" "$MODE" <<'PY'
import json, sys
d, mode = sys.argv[1:]
p = d + "/run.json"; info = json.load(open(p))
info["config"]["parallel"] = 2
info["config"]["test_command"] = "python3 -m unittest discover -s tests"
if mode == "fallback":
    info["config"]["router"]["ollama_url"] = "http://127.0.0.1:1"
json.dump(info, open(p, "w"), indent=2)
open(d + "/plan.md", "w").write("# Plan\n\n## Contents\n- [Work items](#work-items)\n\n## Work items\nT-001 stats, T-002 export (after T-001), T-003 slugify.\n")
def t(i, title, type_, desc, crit, deps=()):
    d_ = {"id": "T-%03d" % i, "title": title, "description": desc, "exit_criteria": crit,
          "depends_on": list(deps), "files_hint": [], "plan_excerpt": "see plan"}
    if type_: d_["type"] = type_
    return d_
json.dump({"tickets": [
  t(1, "Add stats helpers", "feature", "Create tinylib/stats.py with mean(xs) and median(xs); both raise ValueError on an empty list. Add tests in tests/test_stats.py.",
    ["mean([1,2,3]) == 2", "median([3,1,2]) == 2", "empty input raises ValueError"]),
  t(2, "Export stats from the package", "chore", "In tinylib/__init__.py add `from .stats import mean, median`.",
    ["`from tinylib import mean, median` works"], ["T-001"]),
  t(3, "Add slugify", None, "Create tinylib/strings.py with slugify(s): lowercase, non-alphanumerics collapsed to single hyphens, no leading/trailing hyphen. Add tests in tests/test_strings.py.",
    ["slugify('Hello, World!') == 'hello-world'"]),
]}, open(d + "/in.json", "w"))
PY
y tickets import "$YOKE_HOME/runs/$RUN/in.json" --run "$RUN"
y approve plan --run "$RUN"; y approve tickets --run "$RUN"
set +e; y run --run "$RUN"; RC=$?; set -e
[ $RC -eq 0 ] || fail "yoke run exited $RC"
R=$YOKE_HOME/runs/$RUN
BRANCH=$(python3 -c "import json;print(json.load(open('$R/run.json'))['branch'])")
[ "$(git -C "$REPO" rev-list --count "$BASE..$BRANCH")" = 3 ] || fail "expected 3 commits on $BRANCH"
[ -z "$(git -C "$W/origin.git" for-each-ref)" ] || fail "something was pushed to origin"
[ -z "$(find "$R/tickets" -name '*.interrupted*')" ] || fail "interrupted dirs on a clean run"
python3 - "$R" <<'PY' || fail "incomplete audit dirs"
import glob, json, os, sys
r = sys.argv[1]; n = 0
for m in glob.glob(r + "/tickets/*/attempt-*/*/meta.json"):
    meta = json.load(open(m))
    assert "ended_at" in meta, m  # a transient agy denial may leave a failed attempt; it must still be audited
    if meta["schema_valid"] is not True:
        continue
    n += 1
    assert meta["exit_code"] == 0, m
    d = os.path.dirname(m)
    for f in ("prompt.md", "command.json", "stdout.log", "stderr.log", "result.json"):
        assert os.path.exists(d + "/" + f), d + "/" + f
assert n >= 7, n
PY
git -C "$REPO" show "$BRANCH:tinylib/__init__.py" | grep -q mean || fail "T-002 change missing"
WANT=ollama; [ "$MODE" = fallback ] && WANT=fallback
grep -q "\"fn\": \"classify_ticket\".*\"source\": \"$WANT\"" "$R/events.jsonl" || fail "classify_ticket source was not $WANT"
echo "E2E PASS ($MODE) workdir $W run $RUN"
