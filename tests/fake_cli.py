"""Scripted stand-in for an agent CLI (used by FakeAdapter).

FAKE_SCRIPT: JSON file {stage_or_label: [response, ...]}; responses looked up by YOKE_LABEL
first, then YOKE_STAGE, and consumed in order per key (position kept in FAKE_SCRIPT + ".pos").
A response is either
  {"structured_output": {...}, "session_id": "s1"}   printed as JSON
  {"sleep": 999, "child_pidfile": "path"}            optionally spawn a child, then sleep
  {"exit": 1}                                        exit with that code
  {"stderr": "text", "exit": 1}                      write text to stderr
  {"limit": "text"}                                  print limit text and exit 1 (or exit: N)
  {"print": "text"}                                  print text before sleep or exit
  {"raw": "text"}                                    print text verbatim
Any response may add "write": {path: text} (files created in cwd) and "commit": true (git add -A
and commit in cwd, simulating a misbehaving agent) before printing.
"""
import fcntl
import json
import os
import subprocess
import sys
import time


def main():
    script = os.environ["FAKE_SCRIPT"]
    stage = os.environ["YOKE_STAGE"]
    label = os.environ.get("YOKE_LABEL")
    with open(script) as f:
        data = json.load(f)
    key = label if (label and label in data) else stage
    responses = data[key]
    pos_file = script + ".pos"
    with open(script + ".lock", "w") as lock:  # parallel tickets consume the script concurrently
        fcntl.flock(lock, fcntl.LOCK_EX)
        pos = {}
        if os.path.exists(pos_file):
            with open(pos_file) as f:
                pos = json.load(f)
        i = pos.get(key, 0)
        pos[key] = i + 1
        with open(pos_file, "w") as f:
            json.dump(pos, f)
    resp = responses[min(i, len(responses) - 1)]
    if "child_pidfile" in resp:
        child = subprocess.Popen(["sleep", "999"])
        with open(resp["child_pidfile"], "w") as f:
            f.write(str(child.pid))
    for rel, text in resp.get("write", {}).items():
        os.makedirs(os.path.dirname(os.path.abspath(rel)), exist_ok=True)
        with open(rel, "w") as f:
            f.write(text)
    if resp.get("commit"):
        subprocess.run(["git", "add", "-A"], check=True)
        subprocess.run(["git", "commit", "-qm", "agent commit"], check=True)
    if "print" in resp:
        sys.stdout.write(resp["print"] + "\n")
        sys.stdout.flush()
    if "sleep" in resp:
        time.sleep(resp["sleep"])
    if "stderr" in resp:
        sys.stderr.write(resp["stderr"] + "\n")
        sys.stderr.flush()
    if "limit" in resp:
        lim = resp["limit"]
        if isinstance(lim, dict):
            print(json.dumps(lim))
            sys.exit(resp.get("exit", 0 if lim.get("is_error") else 1))
        text = lim if isinstance(lim, str) else "rate limit exceeded"
        print(text)
        sys.exit(resp.get("exit", 1))
    if "exit" in resp:
        sys.exit(resp["exit"])
    if "raw" in resp:
        print(resp["raw"])
        return
    print(json.dumps(resp))


if __name__ == "__main__":
    main()
