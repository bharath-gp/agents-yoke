import contextlib
import io
import json
import os
import subprocess
import unittest
from unittest import mock

from tests.test_core import FAKE_CLI, Tmp, slurp
from yoke import adapters, cli, planreview, schema, state

os.environ.setdefault("YOKE_OLLAMA_URL", "http://127.0.0.1:9")

APPROVE = {"verdict": "approve", "findings": [], "summary": "ok"}
CHANGES = {"verdict": "changes_requested", "summary": "fix",
           "findings": [{"severity": "major", "section": "2", "issue": "gap", "suggestion": "fill"}]}


class Base(Tmp):
    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        subprocess.run(["git", "-C", self.repo, "init", "-q"], check=True)
        req = os.path.join(self.tmp, "req.md")
        with open(req, "w") as f:
            f.write("build a thing")
        self.run_ = planreview.init_run(self.repo, req, {"max_plan_review_rounds": 2, "review_mode": "independent"}, home=self.tmp)
        self.script = os.path.join(self.tmp, "script.json")

    def plan(self, text="the plan"):
        with open(self.run_.path("plan.md"), "w") as f:
            f.write(text)

    def tickets(self, tickets):
        self.run_.write_json("tickets.json", {"tickets": tickets})

    def review(self, *responses):
        with open(self.script, "w") as f:
            json.dump({"plan_review": [{"structured_output": r, "session_id": "s"} for r in responses]}, f)
        return planreview.review_round(self.run_, adapters.FakeAdapter(FAKE_CLI),
                                       env={"FAKE_SCRIPT": self.script})


class TestInit(Base):
    def test_init_copies_requirements_and_snapshots_config(self):
        with open(self.run_.path("requirements.md")) as f:
            self.assertEqual(f.read(), "build a thing")
        self.assertEqual(self.run_.cfg["max_plan_review_rounds"], 2)

    def test_cli_init_prints_run_id_and_overrides_roles(self):
        os.environ["YOKE_HOME"] = self.tmp
        self.addCleanup(os.environ.pop, "YOKE_HOME", None)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(["init", "--repo", self.repo, "--secondary", "claude"]), 0)
        run = state.Run.load(buf.getvalue().strip(), home=self.tmp)
        self.assertEqual(run.cfg["roles"]["secondary"], "claude")


class TestReviewPlan(Base):
    def test_round_saved_and_schema_valid(self):
        self.plan()
        n, res = self.review(CHANGES)
        self.assertEqual(n, 1)
        self.assertTrue(res.ok and res.schema_valid)
        d = self.run_.path("plan-review/round-1")
        for f in ("prompt.md", "result.json", "meta.json"):
            self.assertTrue(os.path.exists(os.path.join(d, f)), f)
        with open(os.path.join(d, "prompt.md")) as f:
            prompt = f.read()
        self.assertIn("build a thing", prompt)
        self.assertIn("the plan", prompt)
        self.assertEqual(self.run_.read_json("plan-review/round-1/command.json")[0:1] and self.run_.read_json("plan-review/round-1/meta.json")["cwd"], self.repo)

    def test_rounds_bounded(self):
        self.plan()
        self.review(CHANGES)
        self.review(CHANGES)
        with self.assertRaisesRegex(RuntimeError, "arbitrate"):
            self.review(CHANGES)
        self.assertEqual(planreview.next_round(self.run_), 3)

    def test_missing_plan_refused(self):
        with self.assertRaises(planreview.ApprovalError):
            self.review(APPROVE)

    def test_invalid_review_fails_call(self):
        self.plan()
        _, res = self.review({"verdict": "maybe"}, {"verdict": "maybe"})  # re-ask also invalid
        self.assertFalse(res.schema_valid)

    def test_schema_file(self):
        self.assertEqual(schema.validate(CHANGES, schema.load_schema("plan-review")), [])
        self.assertTrue(schema.validate({"verdict": "maybe"}, schema.load_schema("plan-review")))


class TestApprovals(Base):
    def test_hash_binding(self):
        self.plan()
        planreview.approve(self.run_, "plan")
        planreview.check_approval(self.run_, "plan")
        self.plan("changed")
        with self.assertRaisesRegex(planreview.ApprovalError, "plan changed"):
            planreview.check_approval(self.run_, "plan")
        planreview.approve(self.run_, "plan")  # re-approve works
        planreview.check_approval(self.run_, "plan")

    def test_tickets_need_plan_approved_and_unchanged(self):
        self.plan()
        self.tickets([{"id": "T-001"}])
        with self.assertRaisesRegex(planreview.ApprovalError, "plan not approved"):
            planreview.approve(self.run_, "tickets")
        planreview.approve(self.run_, "plan")
        self.plan("changed")
        with self.assertRaisesRegex(planreview.ApprovalError, "plan changed"):
            planreview.approve(self.run_, "tickets")

    def test_ticket_change_invalidates_tickets_and_final(self):
        self.plan()
        self.tickets([{"id": "T-001", "title": "a"}])
        planreview.approve(self.run_, "plan")
        planreview.approve(self.run_, "tickets")
        planreview.approve(self.run_, "final")
        planreview.check_approval(self.run_, "final")
        self.tickets([{"id": "T-001", "title": "b"}])
        for kind in ("tickets", "final"):
            with self.assertRaisesRegex(planreview.ApprovalError, "tickets changed"):
                planreview.check_approval(self.run_, kind)

    def test_ticket_hash_ignores_formatting(self):
        self.plan()
        self.tickets([{"id": "T-001", "title": "a"}])
        h = planreview.current_hash(self.run_, "tickets")
        with open(self.run_.path("tickets.json"), "w") as f:
            f.write('{"tickets":[{"title":"a","id":"T-001"}]}')
        self.assertEqual(planreview.current_hash(self.run_, "tickets"), h)

    def test_final_needs_tickets(self):
        self.plan()
        planreview.approve(self.run_, "plan")
        self.tickets([])
        with self.assertRaisesRegex(planreview.ApprovalError, "tickets not approved"):
            planreview.approve(self.run_, "final")


GOOD_PLAN = """# Plan

## Contents
- [Context](#context)
- [Work items](#work-items)
  - [T1 add helper](#t1)

## Context
text, and a [link](#context) outside the index

## Work items
<a id="t1"></a>
### T1 add helper
```
## not a heading
```
"""


class TestToc(Base):
    def test_good_plan_and_repo_plan(self):
        self.assertEqual(planreview.toc_errors(GOOD_PLAN), [])
        with open(os.path.join(os.path.dirname(__file__), "..", "docs", "PLAN.md")) as f:
            self.assertEqual(planreview.toc_errors(f.read()), [])

    def test_missing_or_late_toc(self):
        self.assertIn("no table of contents", planreview.toc_errors("# P\n## A\n")[0])
        self.assertIn("no table of contents", planreview.toc_errors("# P\n## A\n## Contents\n- [A](#a)\n")[0])
        self.assertEqual(planreview.toc_errors("# P\n## Contents\nnone\n## A\n")[0],
                         "table of contents has no [title](#anchor) links")

    def test_dangling_link_and_unlisted_section(self):
        errs = planreview.toc_errors(GOOD_PLAN.replace("(#t1)", "(#t9)") + "## Risks\n")
        self.assertEqual(errs, ["table of contents links to missing anchor #t9",
                                "table of contents does not link section 'Risks'"])

    def test_duplicate_headings_get_numbered_slugs(self):
        doc = "## Contents\n- [A](#a)\n- [A again](#a-1)\n## A\n## A\n"
        self.assertEqual(planreview.toc_errors(doc), [])

    def test_cli_approve_plan_refuses_without_toc(self):
        patch = mock.patch.dict(os.environ, {"YOKE_HOME": self.tmp})
        patch.start()
        self.addCleanup(patch.stop)
        self.plan("# Plan\n## Approach\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["approve", "plan", "--run", self.run_.run_id]), 1)
        self.assertIn("no table of contents", err.getvalue())
        self.assertNotIn("plan", self.run_.read_json("approvals.json", {}))
        self.plan(GOOD_PLAN)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["approve", "plan", "--run", self.run_.run_id]), 0)


class TestPlanDebate(Base):
    def setUp(self):
        super().setUp()
        self.run_.update(config=dict(self.run_.cfg, review_mode="adversarial"))

    def debate(self, plan_review, plan_rebut=None, plan_judge=None):
        script = {"plan_review": [{"structured_output": plan_review, "session_id": "s"}]}
        if plan_rebut is not None:
            script["plan_rebut"] = [{"structured_output": plan_rebut, "session_id": "s"}]
        if plan_judge is not None:
            script["plan_judge"] = [{"structured_output": plan_judge, "session_id": "s"}]
        with open(self.script, "w") as f:
            json.dump(script, f)
        return planreview.review_round(self.run_, adapters.FakeAdapter(FAKE_CLI),
                                       env={"FAKE_SCRIPT": self.script})

    def test_debate_writes_transcript_and_upheld_only(self):
        self.plan()
        changes = {
            "verdict": "changes_requested",
            "summary": "fix",
            "findings": [
                {"severity": "major", "section": "1", "issue": "first issue", "suggestion": "fix 1"},
                {"severity": "major", "section": "2", "issue": "second issue", "suggestion": "fix 2"},
            ],
        }
        rebut = {
            "responses": [
                {"id": "p1", "stance": "concede", "argument": "good catch", "evidence": "spec"},
                {"id": "p2", "stance": "dispute", "argument": "invalid finding", "evidence": "section 2.1"},
            ],
            "summary": "conceded p1, disputed p2",
        }
        judge = {
            "rulings": [
                {"id": "p2", "ruling": "overruled", "reason": "addressed in 2.1"},
            ],
            "summary": "p2 overruled",
        }
        n, res = self.debate(changes, plan_rebut=rebut, plan_judge=judge)
        self.assertEqual(n, 1)
        self.assertTrue(res.ok and res.schema_valid)

        debate = self.run_.read_json("plan-review/round-1/debate.json")
        self.assertEqual(len(debate["upheld"]), 1)
        self.assertEqual(debate["upheld"][0]["issue"], "first issue")
        self.assertEqual(len(debate["overruled"]), 1)
        self.assertEqual(debate["overruled"][0]["finding"]["issue"], "second issue")
        self.assertEqual(debate["overruled"][0]["reason"], "addressed in 2.1")
        self.assertEqual(debate["rebuttals"], rebut["responses"])
        self.assertEqual(debate["rulings"], judge["rulings"])

        events = [json.loads(l) for l in slurp(self.run_.path("events.jsonl")).splitlines()]
        debate_events = [e for e in events if e.get("kind") == "plan_debate"]
        self.assertEqual(len(debate_events), 1)
        self.assertEqual(debate_events[0]["round"], 1)
        self.assertEqual(debate_events[0]["upheld"], 1)
        self.assertEqual(debate_events[0]["overruled"], 1)

        self.assertTrue(os.path.isdir(self.run_.path("plan-review/round-1/rebut")))
        self.assertTrue(os.path.isdir(self.run_.path("plan-review/round-1/judge")))

    def test_all_conceded_skips_judge(self):
        self.plan()
        changes = {
            "verdict": "changes_requested",
            "summary": "fix",
            "findings": [
                {"severity": "major", "section": "1", "issue": "first issue", "suggestion": "fix 1"},
                {"severity": "major", "section": "2", "issue": "second issue", "suggestion": "fix 2"},
            ],
        }
        rebut = {
            "responses": [
                {"id": "p1", "stance": "concede", "argument": "conceded 1", "evidence": "spec"},
                {"id": "p2", "stance": "concede", "argument": "conceded 2", "evidence": "spec"},
            ],
            "summary": "conceded all",
        }
        n, res = self.debate(changes, plan_rebut=rebut)
        self.assertEqual(n, 1)
        self.assertFalse(os.path.exists(self.run_.path("plan-review/round-1/judge")))

        debate = self.run_.read_json("plan-review/round-1/debate.json")
        self.assertEqual(len(debate["upheld"]), 2)
        self.assertEqual(len(debate["overruled"]), 0)
        self.assertEqual([f["issue"] for f in debate["upheld"]], ["first issue", "second issue"])
        self.assertEqual(debate["rulings"], [])

    def test_next_round_unaffected_by_debate_files(self):
        self.plan()
        changes = {
            "verdict": "changes_requested",
            "summary": "fix",
            "findings": [
                {"severity": "major", "section": "1", "issue": "first issue", "suggestion": "fix 1"},
                {"severity": "major", "section": "2", "issue": "second issue", "suggestion": "fix 2"},
            ],
        }
        rebut = {
            "responses": [
                {"id": "p1", "stance": "concede", "argument": "conceded", "evidence": "spec"},
                {"id": "p2", "stance": "dispute", "argument": "disputed", "evidence": "code"},
            ],
            "summary": "summary",
        }
        judge = {
            "rulings": [
                {"id": "p2", "ruling": "overruled", "reason": "reason"},
            ],
            "summary": "summary",
        }
        self.debate(changes, plan_rebut=rebut, plan_judge=judge)
        self.assertEqual(planreview.next_round(self.run_), 2)

    def test_approve_verdict_skips_debate(self):
        self.plan()
        n, res = self.debate(APPROVE)
        self.assertEqual(n, 1)
        self.assertFalse(os.path.exists(self.run_.path("plan-review/round-1/debate.json")))
        self.assertFalse(os.path.exists(self.run_.path("plan-review/round-1/rebut")))

    def test_independent_mode_skips_debate(self):
        self.run_.update(config=dict(self.run_.cfg, review_mode="independent"))
        self.plan()
        n, res = self.debate(CHANGES)
        self.assertEqual(n, 1)
        self.assertFalse(os.path.exists(self.run_.path("plan-review/round-1/debate.json")))
        self.assertFalse(os.path.exists(self.run_.path("plan-review/round-1/rebut")))

    def test_rebut_failure_treats_every_blocking_finding_as_upheld(self):
        self.plan()
        changes = {
            "verdict": "changes_requested",
            "summary": "fix",
            "findings": [
                {"severity": "major", "section": "1", "issue": "first issue", "suggestion": "fix 1"},
                {"severity": "blocker", "section": "2", "issue": "second issue", "suggestion": "fix 2"},
            ],
        }
        script = {
            "plan_review": [{"structured_output": changes, "session_id": "s"}],
            "plan_rebut": [{"exit": 1, "session_id": "s"}],
        }
        with open(self.script, "w") as f:
            json.dump(script, f)
        n, res = planreview.review_round(self.run_, adapters.FakeAdapter(FAKE_CLI),
                                         env={"FAKE_SCRIPT": self.script})
        self.assertEqual(n, 1)
        self.assertFalse(os.path.exists(self.run_.path("plan-review/round-1/judge")))
        debate = self.run_.read_json("plan-review/round-1/debate.json")
        self.assertEqual(len(debate["upheld"]), 2)
        self.assertEqual(len(debate["overruled"]), 0)
        self.assertEqual(debate["rebuttals"], [])
        self.assertEqual(debate["rulings"], [])
        self.assertIn("error", debate)

        events = [json.loads(l) for l in slurp(self.run_.path("events.jsonl")).splitlines()]
        debate_events = [e for e in events if e.get("kind") == "plan_debate"]
        self.assertEqual(len(debate_events), 1)
        self.assertEqual(debate_events[0]["round"], 1)
        self.assertEqual(debate_events[0]["upheld"], 2)
        self.assertEqual(debate_events[0]["overruled"], 0)

    def test_missing_ids_count_as_disputed_and_go_to_judge(self):
        self.plan()
        changes = {
            "verdict": "changes_requested",
            "summary": "fix",
            "findings": [
                {"severity": "major", "section": "1", "issue": "first issue", "suggestion": "fix 1"},
                {"severity": "major", "section": "2", "issue": "second issue", "suggestion": "fix 2"},
            ],
        }
        rebut = {
            "responses": [
                {"id": "p1", "stance": "concede", "argument": "conceded", "evidence": "spec"},
            ],
            "summary": "only answered p1",
        }
        judge = {
            "rulings": [
                {"id": "p2", "ruling": "overruled", "reason": "dismissed"},
            ],
            "summary": "overruled",
        }
        self.debate(changes, plan_rebut=rebut, plan_judge=judge)
        debate = self.run_.read_json("plan-review/round-1/debate.json")
        self.assertEqual(len(debate["upheld"]), 1)
        self.assertEqual(debate["upheld"][0]["issue"], "first issue")
        self.assertEqual(len(debate["overruled"]), 1)
        self.assertEqual(debate["overruled"][0]["finding"]["issue"], "second issue")

        events = [json.loads(l) for l in slurp(self.run_.path("events.jsonl")).splitlines()]
        debate_events = [e for e in events if e.get("kind") == "plan_debate"]
        self.assertEqual(len(debate_events), 1)
        self.assertEqual(debate_events[0]["upheld"], 1)
        self.assertEqual(debate_events[0]["overruled"], 1)

    def test_judge_explicit_upheld_ruling(self):
        self.plan()
        changes = {
            "verdict": "changes_requested",
            "summary": "fix",
            "findings": [
                {"severity": "major", "section": "1", "issue": "first issue", "suggestion": "fix 1"},
                {"severity": "major", "section": "2", "issue": "second issue", "suggestion": "fix 2"},
            ],
        }
        rebut = {
            "responses": [
                {"id": "p1", "stance": "concede", "argument": "conceded", "evidence": "spec"},
                {"id": "p2", "stance": "dispute", "argument": "disputed", "evidence": "code"},
            ],
            "summary": "conceded 1, disputed 1",
        }
        judge = {
            "rulings": [
                {"id": "p2", "ruling": "upheld", "reason": "reviewer is right"},
            ],
            "summary": "upheld p2",
        }
        self.debate(changes, plan_rebut=rebut, plan_judge=judge)
        debate = self.run_.read_json("plan-review/round-1/debate.json")
        self.assertEqual(len(debate["upheld"]), 2)
        self.assertEqual(len(debate["overruled"]), 0)
        self.assertEqual([f["issue"] for f in debate["upheld"]], ["first issue", "second issue"])
        self.assertEqual(len(debate["rulings"]), 1)

        events = [json.loads(l) for l in slurp(self.run_.path("events.jsonl")).splitlines()]
        debate_events = [e for e in events if e.get("kind") == "plan_debate"]
        self.assertEqual(len(debate_events), 1)
        self.assertEqual(debate_events[0]["upheld"], 2)
        self.assertEqual(debate_events[0]["overruled"], 0)

    def test_judge_missing_ruling_counts_as_upheld(self):
        self.plan()
        changes = {
            "verdict": "changes_requested",
            "summary": "fix",
            "findings": [
                {"severity": "major", "section": "1", "issue": "first issue", "suggestion": "fix 1"},
                {"severity": "major", "section": "2", "issue": "second issue", "suggestion": "fix 2"},
            ],
        }
        rebut = {
            "responses": [
                {"id": "p1", "stance": "concede", "argument": "conceded", "evidence": "spec"},
                {"id": "p2", "stance": "dispute", "argument": "disputed", "evidence": "code"},
            ],
            "summary": "conceded 1, disputed 1",
        }
        judge = {
            "rulings": [],
            "summary": "empty rulings",
        }
        self.debate(changes, plan_rebut=rebut, plan_judge=judge)
        debate = self.run_.read_json("plan-review/round-1/debate.json")
        self.assertEqual(len(debate["upheld"]), 2)
        self.assertEqual(len(debate["overruled"]), 0)
        self.assertEqual([f["issue"] for f in debate["upheld"]], ["first issue", "second issue"])

        events = [json.loads(l) for l in slurp(self.run_.path("events.jsonl")).splitlines()]
        debate_events = [e for e in events if e.get("kind") == "plan_debate"]
        self.assertEqual(len(debate_events), 1)
        self.assertEqual(debate_events[0]["upheld"], 2)
        self.assertEqual(debate_events[0]["overruled"], 0)

    def test_judge_call_failure_treats_disputed_as_upheld(self):
        self.plan()
        changes = {
            "verdict": "changes_requested",
            "summary": "fix",
            "findings": [
                {"severity": "major", "section": "1", "issue": "first issue", "suggestion": "fix 1"},
                {"severity": "major", "section": "2", "issue": "second issue", "suggestion": "fix 2"},
            ],
        }
        rebut = {
            "responses": [
                {"id": "p1", "stance": "concede", "argument": "conceded", "evidence": "spec"},
                {"id": "p2", "stance": "dispute", "argument": "disputed", "evidence": "code"},
            ],
            "summary": "conceded 1, disputed 1",
        }
        script = {
            "plan_review": [{"structured_output": changes, "session_id": "s"}],
            "plan_rebut": [{"structured_output": rebut, "session_id": "s"}],
            "plan_judge": [{"exit": 1, "session_id": "s"}],
        }
        with open(self.script, "w") as f:
            json.dump(script, f)
        n, res = planreview.review_round(self.run_, adapters.FakeAdapter(FAKE_CLI),
                                         env={"FAKE_SCRIPT": self.script})
        self.assertEqual(n, 1)
        self.assertTrue(os.path.isdir(self.run_.path("plan-review/round-1/judge")))
        debate = self.run_.read_json("plan-review/round-1/debate.json")
        self.assertEqual(len(debate["upheld"]), 2)
        self.assertEqual(len(debate["overruled"]), 0)
        self.assertEqual(debate["rulings"], [])

        events = [json.loads(l) for l in slurp(self.run_.path("events.jsonl")).splitlines()]
        debate_events = [e for e in events if e.get("kind") == "plan_debate"]
        self.assertEqual(len(debate_events), 1)
        self.assertEqual(debate_events[0]["upheld"], 2)
        self.assertEqual(debate_events[0]["overruled"], 0)

    def test_only_minor_findings_skips_debate(self):
        self.plan()
        changes = {
            "verdict": "changes_requested",
            "summary": "minor nits",
            "findings": [
                {"severity": "minor", "section": "1", "issue": "minor nit", "suggestion": "cleanup"},
            ],
        }
        n, res = self.debate(changes)
        self.assertEqual(n, 1)
        self.assertFalse(os.path.exists(self.run_.path("plan-review/round-1/debate.json")))
        self.assertFalse(os.path.exists(self.run_.path("plan-review/round-1/rebut")))

    def test_mixed_blocker_and_minor_ids_over_blocking_only(self):
        self.plan()
        changes = {
            "verdict": "changes_requested",
            "summary": "mixed",
            "findings": [
                {"severity": "minor", "section": "1", "issue": "minor nit", "suggestion": "cleanup"},
                {"severity": "major", "section": "2", "issue": "real issue", "suggestion": "fix real"},
            ],
        }
        rebut = {
            "responses": [
                {"id": "p1", "stance": "concede", "argument": "conceded real", "evidence": "spec"},
            ],
            "summary": "conceded p1",
        }
        self.debate(changes, plan_rebut=rebut)
        debate = self.run_.read_json("plan-review/round-1/debate.json")
        self.assertEqual(len(debate["upheld"]), 1)
        self.assertEqual(debate["upheld"][0]["issue"], "real issue")
        rebut_prompt = slurp(self.run_.path("plan-review/round-1/rebut/prompt.md"))
        self.assertIn("real issue", rebut_prompt)
        self.assertNotIn("minor nit", rebut_prompt)

    def test_cli_prints_debate_summary(self):
        self.plan()
        changes = {
            "verdict": "changes_requested",
            "summary": "fix",
            "findings": [
                {"severity": "major", "section": "1", "issue": "first issue", "suggestion": "fix 1"},
            ],
        }
        rebut = {
            "responses": [
                {"id": "p1", "stance": "dispute", "argument": "disputed", "evidence": "spec"},
            ],
            "summary": "disputed p1",
        }
        judge = {
            "rulings": [
                {"id": "p1", "ruling": "overruled", "reason": "dismissed"},
            ],
            "summary": "overruled p1",
        }
        script = {
            "plan_review": [{"structured_output": changes, "session_id": "s"}],
            "plan_rebut": [{"structured_output": rebut, "session_id": "s"}],
            "plan_judge": [{"structured_output": judge, "session_id": "s"}],
        }
        with open(self.script, "w") as f:
            json.dump(script, f)

        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"YOKE_HOME": self.tmp, "FAKE_SCRIPT": self.script}), \
             mock.patch("yoke.planreview.adapters.get", return_value=adapters.FakeAdapter(FAKE_CLI)), \
             contextlib.redirect_stdout(buf):
            rc = cli.main(["review-plan", "--run", self.run_.run_id])

        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("debate: 0 upheld, 1 overruled", out)
        self.assertIn("debate.json", out)
        expected = "debate: 0 upheld, 1 overruled (see %s)" % self.run_.path("plan-review/round-1/debate.json")
        self.assertIn(expected, out)

    def test_cli_prints_debate_summary_with_upheld_and_overruled(self):
        self.plan()
        changes = {
            "verdict": "changes_requested",
            "summary": "fix",
            "findings": [
                {"severity": "major", "section": "1", "issue": "first issue", "suggestion": "fix 1"},
                {"severity": "major", "section": "2", "issue": "second issue", "suggestion": "fix 2"},
            ],
        }
        rebut = {
            "responses": [
                {"id": "p1", "stance": "concede", "argument": "conceded", "evidence": "spec"},
                {"id": "p2", "stance": "dispute", "argument": "disputed", "evidence": "spec"},
            ],
            "summary": "conceded p1, disputed p2",
        }
        judge = {
            "rulings": [
                {"id": "p2", "ruling": "overruled", "reason": "dismissed"},
            ],
            "summary": "overruled p2",
        }
        script = {
            "plan_review": [{"structured_output": changes, "session_id": "s"}],
            "plan_rebut": [{"structured_output": rebut, "session_id": "s"}],
            "plan_judge": [{"structured_output": judge, "session_id": "s"}],
        }
        with open(self.script, "w") as f:
            json.dump(script, f)

        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"YOKE_HOME": self.tmp, "FAKE_SCRIPT": self.script}), \
             mock.patch("yoke.planreview.adapters.get", return_value=adapters.FakeAdapter(FAKE_CLI)), \
             contextlib.redirect_stdout(buf):
            rc = cli.main(["review-plan", "--run", self.run_.run_id])

        self.assertEqual(rc, 0)
        out = buf.getvalue()
        expected = "debate: 1 upheld, 1 overruled (see %s)" % self.run_.path("plan-review/round-1/debate.json")
        self.assertIn(expected, out)

    def test_cli_malformed_or_non_dict_debate_json_prints_no_debate_summary(self):
        self.plan()
        changes = {
            "verdict": "changes_requested",
            "summary": "fix",
            "findings": [
                {"severity": "major", "section": "1", "issue": "first issue", "suggestion": "fix 1"},
            ],
        }
        rebut = {
            "responses": [
                {"id": "p1", "stance": "dispute", "argument": "disputed", "evidence": "spec"},
            ],
            "summary": "disputed p1",
        }
        judge = {
            "rulings": [
                {"id": "p1", "ruling": "overruled", "reason": "dismissed"},
            ],
            "summary": "overruled p1",
        }
        script = {
            "plan_review": [{"structured_output": changes, "session_id": "s"}],
            "plan_rebut": [{"structured_output": rebut, "session_id": "s"}],
            "plan_judge": [{"structured_output": judge, "session_id": "s"}],
        }
        with open(self.script, "w") as f:
            json.dump(script, f)

        orig_read_json = state.Run.read_json

        # Non-dict debate.json
        def fake_non_dict(run_self, rel, default=None):
            if "debate.json" in rel:
                return ["not a dict"]
            return orig_read_json(run_self, rel, default)

        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"YOKE_HOME": self.tmp, "FAKE_SCRIPT": self.script}), \
             mock.patch("yoke.planreview.adapters.get", return_value=adapters.FakeAdapter(FAKE_CLI)), \
             mock.patch.object(state.Run, "read_json", fake_non_dict), \
             contextlib.redirect_stdout(buf):
            rc = cli.main(["review-plan", "--run", self.run_.run_id])

        self.assertEqual(rc, 0)
        self.assertNotIn("debate:", buf.getvalue())

        # Corrupt / invalid json raising ValueError
        def fake_corrupt(run_self, rel, default=None):
            if "debate.json" in rel:
                raise ValueError("corrupt json")
            return orig_read_json(run_self, rel, default)

        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"YOKE_HOME": self.tmp, "FAKE_SCRIPT": self.script}), \
             mock.patch("yoke.planreview.adapters.get", return_value=adapters.FakeAdapter(FAKE_CLI)), \
             mock.patch.object(state.Run, "read_json", fake_corrupt), \
             contextlib.redirect_stdout(buf):
            rc = cli.main(["review-plan", "--run", self.run_.run_id])

        self.assertEqual(rc, 0)
        self.assertNotIn("debate:", buf.getvalue())

    def test_cli_independent_mode_prints_no_debate_summary(self):
        self.run_.update(config=dict(self.run_.cfg, review_mode="independent"))
        self.plan()
        changes = {
            "verdict": "changes_requested",
            "summary": "fix",
            "findings": [
                {"severity": "major", "section": "1", "issue": "first issue", "suggestion": "fix 1"},
            ],
        }
        script = {
            "plan_review": [{"structured_output": changes, "session_id": "s"}],
        }
        with open(self.script, "w") as f:
            json.dump(script, f)

        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"YOKE_HOME": self.tmp, "FAKE_SCRIPT": self.script}), \
             mock.patch("yoke.planreview.adapters.get", return_value=adapters.FakeAdapter(FAKE_CLI)), \
             contextlib.redirect_stdout(buf):
            rc = cli.main(["review-plan", "--run", self.run_.run_id])

        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertNotIn("debate:", out)


if __name__ == "__main__":
    unittest.main()
