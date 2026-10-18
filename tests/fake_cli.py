"""Scripted stand-in for an agent CLI (used by FakeAdapter).

FAKE_SCRIPT: JSON file {stage: [response, ...]}, responses consumed in order per stage
(position kept in FAKE_SCRIPT + ".pos"). Stage comes from YOKE_STAGE. A response is either
  {"structured_output": {...}, "session_id": "s1"}   printed as JSON
  {"sleep": 999, "child_pidfile": "path"}            optionally spawn a child, then sleep
  {"exit": 1}                                        exit with that code
  {"stderr": "text", "exit": 1}                      write text to stderr
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
    with open(script) as f:
        responses = json.load(f)[stage]
    pos_file = script + ".pos"
    with open(script + ".lock", "w") as lock:  # parallel tickets consume the script concurrently
        fcntl.flock(lock, fcntl.LOCK_EX)
        pos = {}
        if os.path.exists(pos_file):
            with open(pos_file) as f:
                pos = json.load(f)
        i = pos.get(stage, 0)
        pos[stage] = i + 1
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
    if "sleep" in resp:
        time.sleep(resp["sleep"])
    if "stderr" in resp:
        sys.stderr.write(resp["stderr"] + "\n")
        sys.stderr.flush()
    if "exit" in resp:
        sys.exit(resp["exit"])
    if "raw" in resp:
        print(resp["raw"])
        return
    print(json.dumps(resp))


if __name__ == "__main__":
    main()
