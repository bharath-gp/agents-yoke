import json
import os
import unittest
from unittest import mock

from tests.test_gitops import git
from tests.test_pipeline import CODE, DOCS, PipeBase, REV, qa, resp, ticket, val
from tests.test_router import choice
from yoke import gitops, milestones, router, runcmds, scheduler

os.environ.setdefault("YOKE_OLLAMA_URL", "http://127.0.0.1:9")


def ms_ticket(n, milestone="MS-01", type_="chore", deps=()):
    t = ticket(n, type_=type_, deps=deps)
    t["milestone"] = milestone
    return t


class TestGates(PipeBase):
    def setup_doc(self, doc, script):
        self.run_.write_json("tickets.json", doc)
        with open(self.script, "w") as f:
            json.dump(script, f)
        for ext in (".pos", ".lock"):
            p = self.script + ext
            if os.path.exists(p):
                os.remove(p)
        from yoke import planreview
        planreview.approve(self.run_, "plan")
        planreview.approve(self.run_, "tickets")

    def test_later_milestone_ticket_does_not_close_before_earlier_gate_passes(self):
        doc = {
            "milestones": [
                {"id": "MS-01", "title": "M1", "exit_check": "test -f m1.txt"},
                {"id": "MS-02", "title": "M2", "exit_check": "test -f m2.txt"},
            ],
            "tickets": [
                ms_ticket(1, milestone="MS-01"),
                ms_ticket(2, milestone="MS-02"),
            ],
        }
        script = {
            "code": [
                resp(CODE, "c1", write={"m1.txt": "1\n"}),
                resp(CODE, "c2", write={"m2.txt": "2\n"}),
            ],
            "review": [resp(REV)] * 2,
            "validate": [resp(val())] * 2,
            "router": [{"exit": 1}],
        }
        self.setup_doc(doc, script)
        self.assertEqual(self.go(), "done")

        with open(self.run_.path("events.jsonl")) as f:
            events = [json.loads(line) for line in f]

        m1_passed_idx = next(i for i, e in enumerate(events) if e.get("kind") == "milestone_passed" and e.get("milestone") == "MS-01")
        t2_closed_idx = next(i for i, e in enumerate(events) if e.get("kind") == "ticket_state" and e.get("ticket") == "T-002" and e.get("status") == "closed")
        self.assertLess(m1_passed_idx, t2_closed_idx)
        self.assertEqual(self.log()[:2], ["T-002: ticket 2", "T-001: ticket 1"])

    def test_failing_exit_check_creates_fix_ticket_which_closes_and_gate_passes(self):
        doc = {
            "milestones": [
                {"id": "MS-01", "title": "M1", "exit_check": "test -f fix.txt"},
            ],
            "tickets": [
                ms_ticket(1, milestone="MS-01"),
            ],
        }
        script = {
            "code": [
                resp(CODE, "c1", write={"other.txt": "x\n"}),
                resp(CODE, "c2", write={"fix.txt": "fixed\n"}),
            ],
            "review": [resp(REV)] * 2,
            "qa": [resp(qa("test -f fix.txt"))],
            "validate": [resp(val())] * 2,
            "docs": [resp(DOCS)],
            "router": [{"exit": 1}],
        }
        self.setup_doc(doc, script)
        self.assertEqual(self.go(), "done")

        fixes = self.run_.read_json("fixes.json", [])
        self.assertEqual(len(fixes), 1)
        self.assertEqual(fixes[0]["id"], "F-0002")
        self.assertEqual(fixes[0]["milestone"], "MS-01")
        self.assertEqual(self.tstate(1)["status"], "closed")
        f_state = self.run_.read_json("tickets/F-0002/state.json")
        self.assertEqual(f_state["status"], "closed")
        ms_st = milestones.load_states(self.run_)
        self.assertEqual(ms_st["MS-01"]["status"], "passed")

    def test_gate_decision_escalates(self):
        doc = {
            "milestones": [
                {"id": "MS-01", "title": "M1", "exit_check": "test -f fix.txt"},
            ],
            "tickets": [
                ms_ticket(1, milestone="MS-01"),
            ],
        }
        script = {
            "code": [
                resp(CODE, "c1", write={"other.txt": "x\n"}),
            ],
            "review": [resp(REV)],
            "validate": [resp(val())],
            "router": [{"exit": 1}],
        }
        self.setup_doc(doc, script)
        with mock.patch.object(router, "_ollama", return_value={"gate": choice("escalate", 0.95)}):
            res = self.go()
            self.assertEqual(res, "needs_user")

        fixes = self.run_.read_json("fixes.json", [])
        self.assertEqual(fixes, [])
        ms_st = milestones.load_states(self.run_)
        self.assertEqual(ms_st["MS-01"]["status"], "failed")

        with open(self.run_.path("events.jsonl")) as f:
            events = [json.loads(line) for line in f]
        self.assertTrue(any(e.get("kind") == "escalation" and e.get("ticket") == "MS-01" for e in events))

    def test_gate_decision_none_creates_fix_ticket(self):
        doc = {
            "milestones": [
                {"id": "MS-01", "title": "M1", "exit_check": "test -f fix.txt"},
            ],
            "tickets": [
                ms_ticket(1, milestone="MS-01"),
            ],
        }
        script = {
            "code": [
                resp(CODE, "c1", write={"other.txt": "x\n"}),
                resp(CODE, "c2", write={"fix.txt": "fixed\n"}),
            ],
            "review": [resp(REV)] * 2,
            "qa": [resp(qa("test -f fix.txt"))],
            "validate": [resp(val())] * 2,
            "docs": [resp(DOCS)],
            "router": [{"exit": 1}],
        }
        self.setup_doc(doc, script)
        with mock.patch.object(router, "_ollama", side_effect=RuntimeError("offline")):
            self.assertEqual(self.go(), "done")

        fixes = self.run_.read_json("fixes.json", [])
        self.assertEqual(len(fixes), 1)
        self.assertEqual(fixes[0]["id"], "F-0002")
        self.assertEqual(fixes[0]["milestone"], "MS-01")
        self.assertEqual(self.tstate(1)["status"], "closed")
        f_state = self.run_.read_json("tickets/F-0002/state.json")
        self.assertEqual(f_state["status"], "closed")
        ms_st = milestones.load_states(self.run_)
        self.assertEqual(ms_st["MS-01"]["status"], "passed")

    def test_gate_decision_fix_ticket_creates_fix_ticket(self):
        doc = {
            "milestones": [
                {"id": "MS-01", "title": "M1", "exit_check": "test -f fix.txt"},
            ],
            "tickets": [
                ms_ticket(1, milestone="MS-01"),
            ],
        }
        script = {
            "code": [
                resp(CODE, "c1", write={"other.txt": "x\n"}),
                resp(CODE, "c2", write={"fix.txt": "fixed\n"}),
            ],
            "review": [resp(REV)] * 2,
            "qa": [resp(qa("test -f fix.txt"))],
            "validate": [resp(val())] * 2,
            "docs": [resp(DOCS)],
            "router": [{"exit": 1}],
        }
        self.setup_doc(doc, script)
        with mock.patch.object(router, "_ollama", return_value={"gate": choice("fix_ticket", 0.95)}):
            self.assertEqual(self.go(), "done")

        fixes = self.run_.read_json("fixes.json", [])
        self.assertEqual(len(fixes), 1)
        self.assertEqual(fixes[0]["id"], "F-0002")
        self.assertEqual(fixes[0]["milestone"], "MS-01")
        self.assertEqual(self.tstate(1)["status"], "closed")
        f_state = self.run_.read_json("tickets/F-0002/state.json")
        self.assertEqual(f_state["status"], "closed")
        ms_st = milestones.load_states(self.run_)
        self.assertEqual(ms_st["MS-01"]["status"], "passed")

    def test_gate_decision_below_threshold_creates_fix_ticket(self):
        doc = {
            "milestones": [
                {"id": "MS-01", "title": "M1", "exit_check": "test -f fix.txt"},
            ],
            "tickets": [
                ms_ticket(1, milestone="MS-01"),
            ],
        }
        script = {
            "code": [
                resp(CODE, "c1", write={"other.txt": "x\n"}),
                resp(CODE, "c2", write={"fix.txt": "fixed\n"}),
            ],
            "review": [resp(REV)] * 2,
            "qa": [resp(qa("test -f fix.txt"))],
            "validate": [resp(val())] * 2,
            "docs": [resp(DOCS)],
            "router": [{"exit": 1}],
        }
        self.setup_doc(doc, script)
        with mock.patch.object(router, "_ollama", return_value={"gate": choice("escalate", 0.50)}):
            self.assertEqual(self.go(), "done")

        fixes = self.run_.read_json("fixes.json", [])
        self.assertEqual(len(fixes), 1)
        self.assertEqual(fixes[0]["id"], "F-0002")
        self.assertEqual(fixes[0]["milestone"], "MS-01")
        self.assertEqual(self.tstate(1)["status"], "closed")
        f_state = self.run_.read_json("tickets/F-0002/state.json")
        self.assertEqual(f_state["status"], "closed")
        ms_st = milestones.load_states(self.run_)
        self.assertEqual(ms_st["MS-01"]["status"], "passed")

    def test_reaching_max_fix_tickets_per_milestone_escalates_and_needs_user(self):
        self.run_.update(config=dict(self.run_.cfg, max_fix_tickets_per_milestone=1))
        doc = {
            "milestones": [
                {"id": "MS-01", "title": "M1", "exit_check": "false"},
            ],
            "tickets": [
                ms_ticket(1, milestone="MS-01"),
            ],
        }
        script = {
            "code": [
                resp(CODE, "c1", write={"a.txt": "1\n"}),
                resp(CODE, "c2", write={"b.txt": "2\n"}),
            ],
            "review": [resp(REV)] * 2,
            "qa": [resp(qa("true"))],
            "validate": [resp(val())] * 2,
            "docs": [resp(DOCS)],
            "router": [{"exit": 1}],
        }
        self.setup_doc(doc, script)
        res = self.go()
        self.assertEqual(res, "needs_user")

        escs = self.run_.read_json("escalations.json", [])
        self.assertTrue(any(e.get("ticket") == "MS-01" for e in escs))
        ms_st = milestones.load_states(self.run_)
        self.assertEqual(ms_st["MS-01"]["status"], "failed")

    def test_unblock_milestone_resets_and_clears_escalation(self):
        self.run_.update(config=dict(self.run_.cfg, max_fix_tickets_per_milestone=1))
        doc = {
            "milestones": [
                {"id": "MS-01", "title": "M1", "exit_check": "false"},
            ],
            "tickets": [
                ms_ticket(1, milestone="MS-01"),
            ],
        }
        script = {
            "code": [resp(CODE, "c1", write={"a.txt": "1\n"}), resp(CODE, "c2", write={"b.txt": "2\n"})],
            "review": [resp(REV)] * 2,
            "qa": [resp(qa("true"))],
            "validate": [resp(val())] * 2,
            "docs": [resp(DOCS)],
            "router": [{"exit": 1}],
        }
        self.setup_doc(doc, script)
        self.assertEqual(self.go(), "needs_user")

        escs = self.run_.read_json("escalations.json", [])
        self.assertEqual(len(escs), 1)

        scheduler.unblock_milestone(self.run_, "MS-01", note="please retry")
        escs_after = self.run_.read_json("escalations.json", [])
        self.assertEqual(escs_after, [])
        ms_st = milestones.load_states(self.run_)
        self.assertEqual(ms_st["MS-01"]["fix_count"], 0)
        self.assertEqual(ms_st["MS-01"]["status"], "open")
        self.assertEqual(ms_st["MS-01"]["note"], "please retry")

    def test_waive_marks_waived_and_later_tickets_start(self):
        self.run_.update(config=dict(self.run_.cfg, max_fix_tickets_per_milestone=0))
        doc = {
            "milestones": [
                {"id": "MS-01", "title": "M1", "exit_check": "false"},
                {"id": "MS-02", "title": "M2", "exit_check": "test -f m2.txt"},
            ],
            "tickets": [
                ms_ticket(1, milestone="MS-01"),
                ms_ticket(2, milestone="MS-02"),
            ],
        }
        script = {
            "code": [
                resp(CODE, "c1", write={"m1.txt": "1\n"}),
                resp(CODE, "c2", write={"m2.txt": "2\n"}),
            ],
            "review": [resp(REV)] * 2,
            "validate": [resp(val())] * 2,
            "router": [{"exit": 1}],
        }
        self.setup_doc(doc, script)
        self.assertEqual(self.go(), "needs_user")

        scheduler.unblock_milestone(self.run_, "MS-01", waive="skip flakiness")
        ms_st = milestones.load_states(self.run_)
        self.assertEqual(ms_st["MS-01"]["status"], "waived")
        self.assertEqual(self.run_.read_json("escalations.json", []), [])

        with open(self.run_.path("map.md")) as f:
            map_content = f.read()
        self.assertIn("MS-01 waived", map_content)
        self.assertIn("skip flakiness", map_content)

        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(2)["status"], "closed")

    def test_crash_while_status_is_checking_resumes_and_reruns_check(self):
        doc = {
            "milestones": [
                {"id": "MS-01", "title": "M1", "exit_check": "test -f pass.txt"},
            ],
            "tickets": [
                ms_ticket(1, milestone="MS-01"),
            ],
        }
        script = {
            "code": [resp(CODE, "c1", write={"pass.txt": "ok\n"})],
            "review": [resp(REV)],
            "validate": [resp(val())],
            "router": [{"exit": 1}],
        }
        self.setup_doc(doc, script)
        int_wt = self.run_.path("worktrees/_integration")
        gitops.ensure_feature_branch(self.repo, self.run_.read_json("run.json")["branch"],
                                     self.run_.read_json("run.json")["base_sha"], int_wt)
        with open(os.path.join(int_wt, "pass.txt"), "w") as f:
            f.write("ok\n")
        self.run_.write_json("tickets/T-001/state.json", {"status": "closed", "attempt": 1, "sessions": {}})
        self.run_.write_json("milestones.json", {"MS-01": {"id": "MS-01", "status": "checking", "fix_count": 0}})

        self.assertEqual(self.go(), "done")
        ms_st = milestones.load_states(self.run_)
        self.assertEqual(ms_st["MS-01"]["status"], "passed")

    def test_milestone_without_exit_check_auto_passes(self):
        doc = {
            "milestones": [
                {"id": "MS-01", "title": "M1"},
            ],
            "tickets": [
                ms_ticket(1, milestone="MS-01"),
            ],
        }
        script = {
            "code": [resp(CODE, "c1", write={"a.txt": "1\n"})],
            "review": [resp(REV)],
            "validate": [resp(val())],
            "router": [{"exit": 1}],
        }
        self.setup_doc(doc, script)
        self.assertEqual(self.go(), "done")
        ms_st = milestones.load_states(self.run_)
        self.assertEqual(ms_st["MS-01"]["status"], "passed")


if __name__ == "__main__":
    unittest.main()


class TestLookAhead(PipeBase):
    """PLAN M35: a k+1 ticket starts once its deps are closed but merges only after gate k."""

    setup_doc = TestGates.setup_doc

    def events(self):
        with open(self.run_.path("events.jsonl")) as f:
            return [json.loads(line) for line in f if line.strip()]

    def idx(self, events, kind, **kw):
        return next(i for i, e in enumerate(events)
                    if e.get("kind") == kind and all(e.get(k) == v for k, v in kw.items()))

    def two_ms(self, check="true"):
        return [{"id": "MS-01", "title": "M1", "exit_check": check},
                {"id": "MS-02", "title": "M2", "exit_check": "true"}]

    def test_k_plus_1_ticket_starts_early_parks_and_merges_after_gate(self):
        self.run_.update(config=dict(self.run_.cfg, parallel=2))
        t1 = ms_ticket(1, milestone="MS-01", type_="feature")  # docs stage sleeps: stays running
        doc = {"milestones": self.two_ms(), "tickets": [t1, ms_ticket(2, milestone="MS-02")]}
        script = {
            "code": [resp(CODE, "c1", write={"a.txt": "1\n"}), resp(CODE, "c2", write={"b.txt": "2\n"})],
            "review": [resp(REV)] * 2,
            "qa": [resp(qa("true"))],
            "validate": [resp(val())] * 2,
            "docs": [resp(DOCS, sleep=2)],
            "router": [{"exit": 1}],
        }
        self.setup_doc(doc, script)
        self.assertEqual(self.go(), "done")
        ev = self.events()
        parked = self.idx(ev, "ticket_awaiting_gate", ticket="T-002")
        t1_closed = self.idx(ev, "ticket_state", ticket="T-001", status="closed")
        passed = self.idx(ev, "milestone_passed", milestone="MS-01")
        t2_closed = self.idx(ev, "ticket_state", ticket="T-002", status="closed")
        self.assertLess(parked, t1_closed)  # started and finished while T-001 was still running
        self.assertLess(t1_closed, passed)
        self.assertLess(passed, t2_closed)
        self.assertEqual(self.log()[:2], ["T-002: ticket 2", "T-001: ticket 1"])

    def test_parked_ticket_merges_after_fix_ticket_and_gate_pass(self):
        self.run_.update(config=dict(self.run_.cfg, parallel=2))
        doc = {"milestones": self.two_ms("test -f fix.txt"),
               "tickets": [ms_ticket(1, milestone="MS-01"), ms_ticket(2, milestone="MS-02")]}
        script = {
            "code": [resp(CODE, "c1", write={"a.txt": "1\n"}), resp(CODE, "c2", write={"b.txt": "2\n"}),
                     resp(CODE, "c3", write={"fix.txt": "fixed\n"})],
            "review": [resp(REV)] * 3,
            "qa": [resp(qa("test -f fix.txt"))],
            "validate": [resp(val())] * 3,
            "docs": [resp(DOCS)],
            "router": [{"exit": 1}],
        }
        self.setup_doc(doc, script)
        self.assertEqual(self.go(), "done")
        fixes = self.run_.read_json("fixes.json", [])
        self.assertEqual(len(fixes), 1)
        ev = self.events()
        self.assertLess(self.idx(ev, "fix_ticket_created"), self.idx(ev, "ticket_state", ticket="T-002", status="closed"))
        subjects = self.log()
        self.assertEqual(subjects[0], "T-002: ticket 2")  # merged last, on top of the fix
        self.assertTrue(subjects[1].startswith("F-"))
        branch = self.run_.read_json("run.json")["branch"]
        self.assertEqual(git(self.repo, "rev-list", "--merges", "--count", branch), "0")
        self.assertEqual(git(self.repo, "show", branch + ":fix.txt"), "fixed")

    def test_rebase_conflict_at_deferred_close_reenters_coder_retry(self):
        self.run_.update(config=dict(self.run_.cfg, parallel=2))
        doc = {"milestones": self.two_ms(),
               "tickets": [ms_ticket(1, milestone="MS-01"), ms_ticket(2, milestone="MS-02")]}
        script = {
            "code": [resp(CODE, "c1", write={"a.txt": "one\n"}), resp(CODE, "c2", write={"a.txt": "two\n"}),
                     resp(CODE, "c2", write={"a.txt": "merged\n"})],
            "review": [resp(REV)] * 3,
            "validate": [resp(val())],
            "router": [{"exit": 1}],
        }
        self.setup_doc(doc, script)
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["attempt"], 1)
        self.assertEqual(self.tstate(2)["attempt"], 2)
        with open(self.run_.path("tickets/T-002/attempt-2/feedback.md")) as f:
            self.assertIn("Rebase conflict", f.read())
        branch = self.run_.read_json("run.json")["branch"]
        self.assertEqual(git(self.repo, "show", branch + ":a.txt"), "merged")

    def test_restart_resumes_parked_ticket_without_double_merge(self):
        self.run_.update(config=dict(self.run_.cfg, parallel=2))
        doc = {"milestones": self.two_ms(),
               "tickets": [ms_ticket(1, milestone="MS-01", type_="feature"), ms_ticket(2, milestone="MS-02")]}
        script = {
            "code": [resp(CODE, "c%d" % i, write={"f%d.txt" % i: "x\n"}) for i in (1, 2, 3)],
            "review": [resp(REV)] * 3,
            "qa": [resp(qa("true", status="fail", failures=["boom"]))],
            "validate": [resp(val())],
            "router": [{"exit": 1}],
        }
        self.setup_doc(doc, script)
        self.assertEqual(self.go(), "needs_user")  # T-001 exhausted its attempts; T-002 parked
        self.assertEqual(self.tstate(1)["status"], "blocked")
        self.assertEqual(self.tstate(2)["status"], "awaiting_gate")
        self.assertTrue(os.path.isdir(self.run_.path("worktrees/T-002")))
        self.assertEqual(self.log(), ["init"])
        scheduler.recover(self.run_)
        self.assertEqual(self.tstate(2)["status"], "awaiting_gate")
        scheduler.unblock(self.run_, "T-001", skip=True)
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(2)["status"], "closed")
        self.assertEqual(self.log(), ["T-002: ticket 2", "init"])
        self.assertFalse(os.path.exists(self.run_.path("worktrees/T-002")))

    def test_status_and_map_show_awaiting_gate(self):
        doc = {"milestones": self.two_ms(),
               "tickets": [ms_ticket(1, milestone="MS-01"), ms_ticket(2, milestone="MS-02")]}
        self.setup_doc(doc, {"router": [{"exit": 1}]})
        self.run_.write_json("tickets/T-002/state.json", {"status": "awaiting_gate", "attempt": 1})
        self.assertEqual(self.run_.status()["tickets"].get("awaiting_gate"), 1)
        out = runcmds.render_map(self.run_)
        self.assertIn("T-002 [awaiting_gate]", out)
        self.assertIn("awaiting gate MS-01", out)
        from yoke import dashboard
        prog = dashboard.run_detail(self.run_)["progress"]
        self.assertEqual(prog["awaiting_gate"], 1)
        self.assertEqual(prog["blocked"], 0)
