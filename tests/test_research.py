import argparse
import contextlib
import io
import json
import os
import unittest
from unittest import mock

from tests.test_core import FAKE_CLI
from tests.test_gitops import git
from tests.test_pipeline import PipeBase, resp, ticket
from yoke import adapters, pipeline, research, schema, state, tickets

os.environ.setdefault("YOKE_OLLAMA_URL", "http://127.0.0.1:9")

DOC = "docs/research/t-001-ticket-1.md"
SEARCH = {"status": "found", "queries": ["q"], "tools_used": ["WebSearch"], "summary": "s",
          "sources": [{"url": "https://a.example", "title": "A", "excerpt": "e", "relevance": "r"}]}
RESEARCH = {"status": "done", "doc_path": DOC, "summary": "s",
            "claims": [{"id": "C1", "claim": "sky is blue", "source_urls": ["https://a.example"]}]}


def fc(claim="supported", source="ok", verdict="pass"):
    return {"verdict": verdict, "feedback": "fix C1", "summary": "s",
            "claims": [{"id": "C1", "claim": "sky is blue", "verdict": claim, "evidence": "e",
                        "source_urls": [], "correction": "it is grey"}],
            "sources": [{"url": "https://a.example", "status": source, "note": "n"}]}


def doc_resp(text="doc"):
    return resp(RESEARCH, "r1", write={DOC: text})


class TestResearchTicket(PipeBase):
    def scripted(self, **kw):
        base = {"search": [resp(SEARCH)], "research": [doc_resp()], "factcheck": [resp(fc())],
                "router": [{"exit": 1}]}
        base.update(kw)
        self.setup_tickets([ticket(1, "research")], base)

    def test_closes_and_commits_doc(self):
        self.scripted()
        self.assertEqual(self.go(), "done")
        branch = self.run_.read_json("run.json")["branch"]
        self.assertEqual(git(self.repo, "show", "%s:%s" % (branch, DOC)).strip(), "doc")

    def test_refuted_claim_fails_even_if_verdict_pass(self):
        self.scripted(factcheck=[resp(fc(claim="refuted")), resp(fc())], research=[doc_resp("v1"), doc_resp("v2")])
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["attempt"], 2)
        with open(self.run_.path("tickets/T-001/attempt-2/feedback.md")) as f:
            self.assertIn("C1 refuted", f.read())

    def test_dead_source_fails(self):
        self.scripted(factcheck=[resp(fc(source="dead"))])
        self.assertEqual(self.go(), "needs_user")
        self.assertEqual(self.tstate(1)["status"], "blocked")

    def test_empty_claims_fails(self):
        self.scripted(factcheck=[resp(dict(fc(), claims=[]))])
        self.assertEqual(self.go(), "needs_user")

    def test_null_correction_valid(self):
        o = fc()
        o["claims"][0]["correction"] = None
        self.assertEqual(schema.validate(o, schema.load_schema("factcheck")), [])

    def test_blocked_research_needs_no_doc(self):
        self.assertEqual(schema.validate({"status": "blocked", "summary": "s"}, schema.load_schema("research")), [])

    def test_unverifiable_passes(self):
        self.scripted(factcheck=[resp(fc(claim="unverifiable"))])
        self.assertEqual(self.go(), "done")

    def test_missing_doc_retries(self):
        self.scripted(research=[resp(RESEARCH), doc_resp()])
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["attempt"], 2)

    def test_no_sources_retries(self):
        none = dict(SEARCH, status="none", sources=[])
        self.scripted(search=[resp(none), resp(SEARCH)])
        self.assertEqual(self.go(), "done")
        self.assertEqual(self.tstate(1)["attempt"], 2)

    def test_researcher_blocked_escalates(self):
        self.scripted(research=[resp(dict(RESEARCH, status="blocked", blocked_reason="no web"))])
        self.assertEqual(self.go(), "needs_user")


class TestStandalone(PipeBase):
    def drive(self, script, stages, **kw):
        with open(self.script, "w") as f:
            json.dump(script, f)
        run = research.new_run(self.repo)
        t = research._ticket("sky", DOC)
        return run, research.drive(run, t, stages, adapter_for=lambda r: adapters.FakeAdapter(FAKE_CLI),
                                   env=self.env, **kw)

    def test_full_loop_leaves_doc_uncommitted(self):
        _, (ok, res, _) = self.drive({"search": [resp(SEARCH)], "research": [doc_resp()],
                                      "factcheck": [resp(fc())]}, ["search", "research", "factcheck"])
        self.assertTrue(ok)
        self.assertTrue(os.path.isfile(os.path.join(self.repo, DOC)))
        self.assertIn("?? docs/", git(self.repo, "status", "--short"))

    def test_factcheck_only_single_attempt(self):
        run, (ok, res, fb) = self.drive({"factcheck": [resp(fc(claim="refuted"))]}, ["factcheck"], attempts=1)
        self.assertFalse(ok)
        self.assertIn("C1 refuted", fb)
        self.assertEqual(run.read_json("run.json")["agent_calls"], 1)

    def test_research_run_hidden_from_latest(self):
        run, _ = self.drive({"factcheck": [resp(fc())]}, ["factcheck"], attempts=1)
        latest = state.Run.latest_for_repo(self.repo)
        self.assertNotEqual(latest.run_id, run.run_id)


class TestDispatch(PipeBase):
    def call(self, script, **ns):
        with open(self.script, "w") as f:
            json.dump(script, f)
        out, err = io.StringIO(), io.StringIO()
        fake = lambda cfg: (lambda role: adapters.FakeAdapter(FAKE_CLI))
        old = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, old)
        with mock.patch.dict(os.environ, self.env), mock.patch.object(pipeline, "default_adapter_for", fake), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = research.dispatch(argparse.Namespace(depth=None, **ns))
        return rc, out.getvalue(), err.getvalue()

    def test_search_prints_sources(self):
        rc, out, _ = self.call({"search": [resp(SEARCH)]}, cmd="search", query="sky")
        self.assertEqual(rc, 0)
        self.assertIn("https://a.example  A\n  r", out)

    def test_research_reports_doc_and_counts(self):
        doc = "docs/research/sky.md"
        rs = resp(dict(RESEARCH, doc_path=doc), "r1", write={doc: "doc"})
        rc, out, _ = self.call({"search": [resp(SEARCH)], "research": [rs], "factcheck": [resp(fc())]},
                               cmd="research", topic="sky")
        self.assertEqual((rc, out.strip()), (0, "%s: 1 of 1 claims verified" % doc))
        self.assertTrue(os.path.isfile(os.path.join(self.repo, doc)))

    def test_factcheck_failure_reports_once_on_stderr(self):
        with open(os.path.join(self.repo, "a.md"), "w") as f:
            f.write("sky is blue")
        rc, out, err = self.call({"factcheck": [resp(fc(claim="refuted"))]}, cmd="factcheck", file="a.md")
        self.assertEqual(rc, 2)
        self.assertEqual(out, "")
        self.assertEqual(err.count("C1 refuted"), 1)

    def test_factcheck_pass_flags_unverifiable(self):
        with open(os.path.join(self.repo, "a.md"), "w") as f:
            f.write("sky is blue")
        rc, out, _ = self.call({"factcheck": [resp(fc(claim="unverifiable", verdict="fail"))]},
                               cmd="factcheck", file="a.md")
        self.assertEqual(rc, 0)
        self.assertIn("C1 unverifiable", out)
        self.assertIn("fact check passed", out)

    def test_factcheck_rejects_file_outside_repo(self):
        rc, _, err = self.call({}, cmd="factcheck", file="/etc/hosts")
        self.assertEqual(rc, 1)
        self.assertIn("not a file inside", err)

    def test_tickets_import_accepts_doc_path(self):
        t = dict(ticket(1, "research"), doc_path="docs/research/x.md")
        path = os.path.join(self.tmp, "t.json")
        with open(path, "w") as f:
            json.dump({"tickets": [t]}, f)
        errs, ts = tickets.import_tickets(self.run_, path)
        self.assertEqual(errs, [])
        self.assertEqual(ts[0]["doc_path"], "docs/research/x.md")


class TestWiring(unittest.TestCase):
    def test_schemas_valid_json(self):
        for n in ("search", "research", "factcheck"):
            schema.load_schema(n)
        self.assertEqual(schema.validate(fc(), schema.load_schema("factcheck")), [])
        self.assertEqual(schema.validate(SEARCH, schema.load_schema("search")), [])

    def test_claude_web_tools_only_when_given(self):
        base = dict(prompt="p", schema_path=schema.schema_path("search"), cwd=".")
        web = adapters.ClaudeAdapter().build_cmd(adapters.CallSpec(web_tools=("WebSearch", "mcp__firecrawl"), **base))
        plain = adapters.ClaudeAdapter().build_cmd(adapters.CallSpec(**base))
        self.assertIn("WebSearch mcp__firecrawl", web[web.index("--allowedTools") + 1])
        self.assertNotIn("WebSearch", plain[plain.index("--allowedTools") + 1])

    def test_researcher_prompt_says_no_shell(self):
        with open(os.path.join(os.path.dirname(schema.SCHEMA_DIR), "prompts", "research.md")) as f:
            self.assertIn("You have no shell in this environment. Do not run any command; edit files only.", f.read())


if __name__ == "__main__":
    unittest.main()
