"""Unit tests for yoke.prbody."""
import json
import os
import subprocess
import tempfile
import unittest

from yoke import prbody


class TestFindTemplate(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.repo = os.path.realpath(self._td.name)
        for k, v in (
            ("GIT_CONFIG_GLOBAL", "/dev/null"),
            ("GIT_CONFIG_SYSTEM", "/dev/null"),
            ("GIT_CONFIG_NOSYSTEM", "1"),
            ("GIT_CONFIG_COUNT", "0"),
        ):
            old = os.environ.get(k)
            os.environ[k] = v
            self.addCleanup(lambda k=k, old=old: os.environ.__setitem__(k, old) if old is not None else os.environ.pop(k, None))

    def _write(self, rel_path, content="template content"):
        full_path = os.path.join(self.repo, rel_path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content)
        return full_path

    def test_template_found_github_dir_single(self):
        self._write(".github/pull_request_template.md", "content gh")
        res = prbody.find_template(self.repo)
        self.assertEqual(res, "content gh")

    def test_template_found_case_insensitive(self):
        self._write(".github/PULL_REQUEST_TEMPLATE.md", "content uppercase")
        res = prbody.find_template(self.repo)
        self.assertEqual(res, "content uppercase")

    def test_template_found_root(self):
        self._write("pull_request_template.md", "content root")
        res = prbody.find_template(self.repo)
        self.assertEqual(res, "content root")

    def test_template_found_docs(self):
        self._write("docs/pull_request_template.md", "content docs")
        res = prbody.find_template(self.repo)
        self.assertEqual(res, "content docs")

    def test_template_missing_returns_none(self):
        res = prbody.find_template(self.repo)
        self.assertIsNone(res)

    def test_several_templates_type_priority_pick(self):
        self._write(".github/PULL_REQUEST_TEMPLATE/feature.md", "# Feature PR")
        self._write(".github/PULL_REQUEST_TEMPLATE/bugfix.md", "# Bugfix PR")
        self._write(".github/PULL_REQUEST_TEMPLATE/refactor.md", "# Refactor PR")
        self._write(".github/PULL_REQUEST_TEMPLATE/test.md", "# Test PR")
        self._write(".github/PULL_REQUEST_TEMPLATE/docs.md", "# Docs PR")
        self._write(".github/PULL_REQUEST_TEMPLATE/chore.md", "# Chore PR")
        self._write(".github/PULL_REQUEST_TEMPLATE/spike.md", "# Spike PR")
        self._write(".github/PULL_REQUEST_TEMPLATE/research.md", "# Research PR")

        # Pick bugfix when bugfix is the ticket type
        res = prbody.find_template(self.repo, ticket_types=["bugfix"])
        self.assertEqual(res, "# Bugfix PR")

        # Priority chain: feature > bugfix > refactor > test > docs > chore > spike > research
        self.assertEqual(prbody.find_template(self.repo, ticket_types=["bugfix", "feature"]), "# Feature PR")
        self.assertEqual(prbody.find_template(self.repo, ticket_types=["refactor", "bugfix"]), "# Bugfix PR")
        self.assertEqual(prbody.find_template(self.repo, ticket_types=["test", "refactor"]), "# Refactor PR")
        self.assertEqual(prbody.find_template(self.repo, ticket_types=["docs", "test"]), "# Test PR")
        self.assertEqual(prbody.find_template(self.repo, ticket_types=["chore", "docs"]), "# Docs PR")
        self.assertEqual(prbody.find_template(self.repo, ticket_types=["spike", "chore"]), "# Chore PR")
        self.assertEqual(prbody.find_template(self.repo, ticket_types=["research", "spike"]), "# Spike PR")

        # Pick based on tickets.json in repo when ticket_types is not passed
        self._write("tickets.json", json.dumps({"tickets": [{"type": "refactor"}]}))
        self.assertEqual(prbody.find_template(self.repo), "# Refactor PR")

    def test_several_templates_first_sorted_fallback(self):
        self._write(".github/PULL_REQUEST_TEMPLATE/beta.md", "# Beta")
        self._write(".github/PULL_REQUEST_TEMPLATE/alpha.md", "# Alpha")

        # No ticket types provided
        res = prbody.find_template(self.repo, ticket_types=[])
        self.assertEqual(res, "# Alpha")

        # Ticket type does not match any template stem
        res = prbody.find_template(self.repo, ticket_types=["nonexistent"])
        self.assertEqual(res, "# Alpha")

    def test_dir_without_md_falls_through(self):
        # Template directory exists but contains only a .txt file
        self._write(".github/PULL_REQUEST_TEMPLATE/notes.txt", "not markdown")
        self._write("pull_request_template.md", "# Fallback root template")

        res = prbody.find_template(self.repo)
        self.assertEqual(res, "# Fallback root template")

        # Empty template directory falls through to single-file paths
        empty_dir = os.path.join(self.repo, "empty_repo")
        os.makedirs(os.path.join(empty_dir, ".github", "PULL_REQUEST_TEMPLATE"), exist_ok=True)
        with open(os.path.join(empty_dir, "pull_request_template.md"), "w", encoding="utf-8") as f:
            f.write("# Empty dir fallback")
        self.assertEqual(prbody.find_template(empty_dir), "# Empty dir fallback")

    def test_git_branch_lookup_and_fallback(self):
        # Initialize a real git repo to test git show/ls-tree lookup
        subprocess.run(["git", "-C", self.repo, "init", "-q"], check=True)
        subprocess.run(["git", "-C", self.repo, "config", "user.name", "Tester"], check=True)
        subprocess.run(["git", "-C", self.repo, "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", self.repo, "config", "core.hooksPath", ""], check=True)

        self._write(".github/pull_request_template.md", "# Git branch template")
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "add template"], check=True)
        subprocess.run(["git", "-C", self.repo, "branch", "feature-branch"], check=True)

        # Remove file from working tree to prove it reads via git show on branch
        os.remove(os.path.join(self.repo, ".github", "pull_request_template.md"))

        res = prbody.find_template(self.repo, branch="feature-branch")
        self.assertEqual(res, "# Git branch template")

        # When branch does not exist, falls back to working tree
        self._write("pull_request_template.md", "# Working tree fallback")
        res = prbody.find_template(self.repo, branch="nonexistent-branch")
        self.assertEqual(res, "# Working tree fallback")


class TestBuiltinStructure(unittest.TestCase):
    def test_template_missing_returns_builtin_structure(self):
        facts = {
            "title": "Add prbody support",
            "summary": "Add prbody support",
            "why": "Need deterministic PR templates",
            "changes": "- T-001: implement prbody\n\n```\n diffstat \n```",
            "verification": "python3 -m unittest tests.test_prbody passed",
            "verified": {"tests_pass"},
        }
        res = prbody.fill_template(None, facts)

        self.assertIn("## Summary\nAdd prbody support", res)
        self.assertIn("## Why\nNeed deterministic PR templates", res)
        self.assertIn("## What changed\n- T-001: implement prbody", res)
        self.assertIn("## Verification\npython3 -m unittest tests.test_prbody passed", res)
        self.assertIn("## Review focus\nN/A - not recorded by yoke", res)

    def test_empty_template_returns_builtin_structure(self):
        facts = {
            "title": "Title 1",
            "why": "Why 1",
            "changes": "Changes 1",
            "verification": "Verif 1",
            "verified": set(),
        }
        res_empty = prbody.fill_template("", facts)
        res_ws = prbody.fill_template("   \n\t  ", facts)

        for heading in ("## Summary", "## Why", "## What changed", "## Verification", "## Review focus"):
            self.assertIn(heading, res_empty)
            self.assertIn(heading, res_ws)

    def test_template_missing_builtin_structure(self):
        """Explicitly verify template missing produces built-in structure with all headings."""
        facts = {
            "title": "Built-in title",
            "why": "Built-in why",
            "changes": "Built-in changes",
            "verification": "Built-in verification",
            "review_focus": "Built-in review focus",
        }
        res = prbody.fill_template(None, facts)
        self.assertIn("## Summary", res)
        self.assertIn("## Why", res)
        self.assertIn("## What changed", res)
        self.assertIn("## Verification", res)
        self.assertIn("## Review focus", res)


class TestChecklistVerification(unittest.TestCase):
    def test_checklist_item_not_ticked_without_verification(self):
        template = """## Checklist
- [ ] Tests pass
- [ ] Documentation updated
"""
        facts = {
            "verified": set(),
            "verification": "N/A - not recorded by yoke",
        }
        res = prbody.fill_template(template, facts)

        self.assertIn("- [ ] Tests pass", res)
        self.assertIn("- [ ] Documentation updated", res)
        self.assertNotIn("- [x]", res)

    def test_checklist_item_ticked_with_passing_close_gate(self):
        template = """## Checklist
- [ ] Tests pass
- [ ] Documentation updated
"""
        facts = {
            "verified": {"tests_pass"},
            "verification": "- T-001: `pytest` passed",
        }
        res = prbody.fill_template(template, facts)

        self.assertIn("- [x] Tests pass", res)
        self.assertIn("- [ ] Documentation updated", res)

    def test_checklist_item_ticked_with_passing_close_gate_end_to_end(self):
        """End-to-end: collect facts from run directory with close_gate event and fill template."""
        with tempfile.TemporaryDirectory() as td:
            run_dir = td
            # Write passing close_gate event
            with open(os.path.join(run_dir, "events.jsonl"), "w", encoding="utf-8") as f:
                f.write(json.dumps({
                    "kind": "close_gate",
                    "ticket": "T-001",
                    "attempt": 1,
                    "ok": True,
                }) + "\n")
            with open(os.path.join(run_dir, "tickets.json"), "w", encoding="utf-8") as f:
                json.dump({"tickets": [{"id": "T-001", "title": "Implement feature"}]}, f)
            os.makedirs(os.path.join(run_dir, "tickets", "T-001", "attempt-1", "qa"), exist_ok=True)
            with open(os.path.join(run_dir, "tickets", "T-001", "attempt-1", "qa", "result.json"), "w", encoding="utf-8") as f:
                json.dump({"test_command": "python3 -m unittest discover -s tests"}, f)

            facts = prbody.collect(run_dir)
            self.assertIn("tests_pass", facts["verified"])

            template = """## Checklist
- [ ] Tests pass
- [ ] Documentation updated
"""
            res = prbody.fill_template(template, facts)
            self.assertIn("- [x] Tests pass", res)
            self.assertIn("- [ ] Documentation updated", res)

            # Now test when close_gate failed
            with open(os.path.join(run_dir, "events.jsonl"), "w", encoding="utf-8") as f:
                f.write(json.dumps({
                    "kind": "close_gate",
                    "ticket": "T-001",
                    "attempt": 1,
                    "ok": False,
                }) + "\n")

            facts_failed = prbody.collect(run_dir)
            self.assertNotIn("tests_pass", facts_failed["verified"])
            res_failed = prbody.fill_template(template, facts_failed)
            self.assertIn("- [ ] Tests pass", res_failed)
            self.assertNotIn("- [x]", res_failed)


class TestCollect(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.run_dir = os.path.realpath(self._td.name)

    def _write_file(self, rel_path, content):
        p = os.path.join(self.run_dir, rel_path)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return p

    def _write_json(self, rel_path, obj):
        p = os.path.join(self.run_dir, rel_path)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        return p

    def _mock_run(self):
        class MockRun:
            def __init__(self, d):
                self.run_id = "test-run-123"
                self.dir = d

            def path(self, rel):
                return os.path.join(self.dir, rel)

            def read_json(self, rel, default=None):
                p = self.path(rel)
                if os.path.exists(p):
                    with open(p, encoding="utf-8") as f:
                        return json.load(f)
                return default if default is not None else {}

        return MockRun(self.run_dir)

    def test_collect_title_from_plan_md_regex(self):
        run = self._mock_run()
        self._write_file("plan.md", "# Plan: Implement PR Body Generator\n## Approach\nDetails")
        facts = prbody.collect(run)
        self.assertEqual(facts["title"], "Implement PR Body Generator")

        # With extra prefix words
        self._write_file("plan.md", "# Detailed Technical Plan: Generate PR Descriptions\n")
        facts2 = prbody.collect(run)
        self.assertEqual(facts2["title"], "Generate PR Descriptions")

        # Fallback when plan has no # heading
        self._write_file("plan.md", "Just plain text\n")
        facts3 = prbody.collect(run)
        self.assertEqual(facts3["title"], "yoke: test-run-123")

    def test_collect_why_from_requirements_and_plan(self):
        run = self._mock_run()
        self._write_file("requirements.md", "# Requirements\n## Goal\nAutomate pull request summaries.\n")
        self._write_file("plan.md", "# Plan: Work\n## Approach\nRead git trees and format facts.\n")
        facts = prbody.collect(run)
        self.assertIn("Automate pull request summaries.", facts["why"])
        self.assertIn("Read git trees and format facts.", facts["why"])

    def test_collect_changes_from_tickets(self):
        run = self._mock_run()
        self._write_json("tickets.json", {
            "tickets": [
                {"id": "T-001", "title": "Template lookup"},
                {"id": "T-002", "title": "Deterministic fill"},
            ]
        })
        facts = prbody.collect(run)
        self.assertIn("- T-001: Template lookup", facts["changes"])
        self.assertIn("- T-002: Deterministic fill", facts["changes"])

    def test_collect_verification_from_qa_result_and_passing_close_gate(self):
        run = self._mock_run()
        self._write_json("tickets.json", {
            "tickets": [{"id": "T-001", "title": "First ticket"}]
        })
        self._write_json("tickets/T-001/state.json", {"status": "closed", "attempt": 1})
        self._write_json("tickets/T-001/attempt-1/qa/result.json", {
            "test_command": "python3 -m unittest discover -s tests"
        })
        self._write_file("events.jsonl", json.dumps({
            "kind": "close_gate",
            "ticket": "T-001",
            "attempt": 1,
            "ok": True,
        }) + "\n")

        facts = prbody.collect(run)
        self.assertIn("tests_pass", facts["verified"])
        self.assertIn("python3 -m unittest discover -s tests", facts["verification"])

    def test_collect_verification_not_verified_when_close_gate_fails(self):
        run = self._mock_run()
        self._write_json("tickets.json", {
            "tickets": [{"id": "T-001", "title": "First ticket"}]
        })
        self._write_json("tickets/T-001/state.json", {"status": "closed", "attempt": 1})
        self._write_file("events.jsonl", json.dumps({
            "kind": "close_gate",
            "ticket": "T-001",
            "attempt": 1,
            "ok": False,
        }) + "\n")

        facts = prbody.collect(run)
        self.assertNotIn("tests_pass", facts["verified"])


class TestFillTemplate(unittest.TestCase):
    def test_headings_and_order_preserved(self):
        template = """## Why
Please explain why.

## What changed
Please explain changes.

## Other notes
Any additional info.
"""
        facts = {
            "why": "This is why.",
            "changes": "- Did something",
            "verified": set(),
        }
        res = prbody.fill_template(template, facts)

        # Check headings order
        why_idx = res.index("## Why")
        what_idx = res.index("## What changed")
        notes_idx = res.index("## Other notes")
        self.assertTrue(why_idx < what_idx < notes_idx)

        self.assertIn("This is why.", res)
        self.assertIn("- Did something", res)
        self.assertIn("N/A - not recorded by yoke", res)


if __name__ == "__main__":
    unittest.main()
