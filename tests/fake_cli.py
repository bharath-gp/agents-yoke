"""Scripted stand-in for an agent CLI (used by FakeAdapter).

FAKE_SCRIPT: JSON file {stage: [response, ...]}, responses consumed in order per stage
(position kept in FAKE_SCRIPT + ".pos"). Stage comes from YOKE_STAGE. A response is either
  {"structured_output": {...}, "session_id": "s1"}   printed as JSON
  {"sleep": 999, "child_pidfile": "path"}            optionally spawn a child, then sleep
  {"exit": 1}                                        exit with that code
  {"raw": "text"}                                    print text verbatim
"""
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
    if "sleep" in resp:
        time.sleep(resp["sleep"])
    if "exit" in resp:
        sys.exit(resp["exit"])
    if "raw" in resp:
        print(resp["raw"])
        return
    print(json.dumps(resp))


if __name__ == "__main__":
    main()
