import json
import os
import unittest

from tests.test_pipeline import CODE, DOCS, PipeBase, REV, qa, resp, ticket, val
from yoke import gitops, milestones, runcmds, scheduler

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

    def test_later_milestone_ticket_does_not_start_before_earlier_gate_passes(self):
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
        t2_started_idx = next(i for i, e in enumerate(events) if e.get("kind") == "ticket_state" and e.get("ticket") == "T-002" and e.get("status") == "in_progress")
        self.assertLess(m1_passed_idx, t2_started_idx)

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
