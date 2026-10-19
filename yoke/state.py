"""Run directory under $YOKE_HOME/runs/<run_id>/: atomic JSON, event log, status (PLAN 2.2)."""
import datetime
import json
import os
import re
import subprocess
import threading

from . import config

_LOCK = threading.RLock()  # one lock for all state mutations (PLAN 2.2)


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _write_atomic(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.tmp.%d.%d" % (path, os.getpid(), threading.get_ident())
    try:
        with open(tmp, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


class Run:
    def __init__(self, run_id, home=None):
        self.run_id = run_id
        self.home = home or config.yoke_home()
        self.dir = os.path.join(self.home, "runs", run_id)

    @classmethod
    def create(cls, repo, cfg, home=None):
        repo = os.path.realpath(repo)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        base_id = "%s-%s" % (os.path.basename(repo), stamp)
        run, n = cls(base_id, home), 1
        while os.path.exists(run.dir):  # two inits in the same second
            n += 1
            run = cls("%s-%d" % (base_id, n), home)
        os.makedirs(run.dir)
        sha = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                             capture_output=True, text=True).stdout.strip() or None
        run.write_json("run.json", {
            "run_id": run.run_id, "repo": repo, "base_sha": sha,
            "branch": "yoke/" + run.run_id, "config": cfg, "runner_pid": None,
            "state": "init", "agent_calls": 0, "created_at": now(),
        })
        run.event("run_created", repo=repo)
        return run

    @classmethod
    def load(cls, run_id, home=None):
        run = cls(run_id, home)
        if not os.path.exists(run.path("run.json")):
            raise FileNotFoundError("no such run: %s" % run_id)
        return run

    @classmethod
    def list_for_repo(cls, path, home=None):
        """All runs whose run.json repo == realpath(path) and kind != 'research', sorted by created_at ascending."""
        path = os.path.realpath(path)
        runs_dir = os.path.join(home or config.yoke_home(), "runs")
        matching = []
        for rid in sorted(os.listdir(runs_dir)) if os.path.isdir(runs_dir) else []:
            try:
                run = cls(rid, home)
                info = run.read_json("run.json")
            except (OSError, ValueError):
                continue
            if isinstance(info, dict) and info.get("kind") != "research":
                repo = info.get("repo")
                if repo and (repo == path or os.path.realpath(repo) == path):
                    matching.append((info.get("created_at") or "", run))
        matching.sort(key=lambda item: item[0])
        return [item[1] for item in matching]

    @classmethod
    def latest_for_repo(cls, path, home=None):
        """Most recent run whose repo == path, or None."""
        runs = cls.list_for_repo(path, home=home)
        return runs[-1] if runs else None

    def path(self, rel):
        return os.path.join(self.dir, rel)

    def write_json(self, rel, obj):
        text = json.dumps(obj, indent=2, sort_keys=True) + "\n"  # serialize before touching disk
        with _LOCK:
            _write_atomic(self.path(rel), text)

    def read_json(self, rel, default=None):
        try:
            with open(self.path(rel)) as f:
                return json.load(f)
        except FileNotFoundError:
            if default is not None:
                return default
            raise

    @property
    def cfg(self):
        return self.read_json("run.json")["config"]

    def update(self, **kv):
        """Patch top-level fields of run.json."""
        with _LOCK:
            info = self.read_json("run.json")
            info.update(kv)
            self.write_json("run.json", info)
            return info

    def alloc_number(self):
        """Next number from the T/F counter shared by tickets and fix tickets, persisted in run.json.
        Also clears anything already in tickets.json/fixes.json, so legacy runs and recovery never duplicate."""
        with _LOCK:
            info = self.read_json("run.json")
            used = [info.get("id_counter", 0)]
            docs = (self.read_json("tickets.json", {}).get("tickets", []), self.read_json("fixes.json", []))
            for t in docs[0] + docs[1]:
                m = re.match(r"^[TF]-(\d+)$", str(t.get("id", ""))) if isinstance(t, dict) else None
                if m:
                    used.append(int(m.group(1)))
            info["id_counter"] = max(used) + 1
            self.write_json("run.json", info)
            return info["id_counter"]

    def incr_calls(self):
        """Bump the agent-call budget counter; returns the new total."""
        with _LOCK:
            info = self.read_json("run.json")
            info["agent_calls"] += 1
            self.write_json("run.json", info)
            return info["agent_calls"]

    def event(self, kind, **data):
        line = json.dumps(dict(data, kind=kind, at=now()), sort_keys=True)
        with _LOCK:
            os.makedirs(self.dir, exist_ok=True)
            with open(self.path("events.jsonl"), "a") as f:
                f.write(line + "\n")
        self.render_status()

    def status(self):
        info = self.read_json("run.json")
        counts = {}
        from . import milestones
        tickets = milestones.merged_tickets(self)
        for t in tickets:
            st = self.read_json("tickets/%s/state.json" % t["id"], {}).get("status", "pending")
            counts[st] = counts.get(st, 0) + 1
        return {
            "run_id": self.run_id, "repo": info["repo"], "branch": info["branch"],
            "state": info["state"], "agent_calls": info["agent_calls"],
            "max_agent_calls": info["config"].get("max_agent_calls_total"),
            "tickets_total": len(tickets), "tickets": counts,
            "escalations": len(self.read_json("escalations.json", [])),
        }

    def render_status(self):
        """Derive status.json and return a short text summary."""
        s = self.status()
        with _LOCK:
            self.write_json("status.json", s)
        by = ", ".join("%s %d" % kv for kv in sorted(s["tickets"].items())) or "none"
        from . import milestones
        ms_list = milestones.milestones_of(self)
        ms_states = milestones.load_states(self)
        ms_str = ", ".join("%s %s" % (m["id"], ms_states.get(m["id"], {}).get("status", "open")) for m in ms_list)
        return "\n".join([
            "run %s [%s]" % (s["run_id"], s["state"]),
            "repo %s  branch %s" % (s["repo"], s["branch"]),
            "agent calls %d/%s" % (s["agent_calls"], s["max_agent_calls"]),
            "milestones %d (%s)" % (len(ms_list), ms_str),
            "tickets %d (%s)" % (s["tickets_total"], by),
            "open escalations %d" % s["escalations"],
        ])
