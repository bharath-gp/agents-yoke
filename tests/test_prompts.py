import json
import os
import string
import unittest

from yoke import config, schema

PROMPTS = os.path.join(os.path.dirname(__file__), "..", "yoke", "prompts")
NO_SHELL = "You have no shell in this environment. Do not run any command; edit files only."


def _read(name):
    with open(os.path.join(PROMPTS, name + ".md")) as f:
        return f.read()


class TestNoShellPrompts(unittest.TestCase):
    def test_agy_stages_say_no_shell(self):
        for stage in ("code", "qa", "docs"):
            self.assertIn(NO_SHELL, _read(stage), stage)

    def test_plan_review_has_no_shell_beyond_allowlist(self):
        text = _read("plan_review")
        self.assertIn("no general shell", text)
        self.assertNotIn("git write commands", text)

    def test_qa_does_not_run_tests(self):
        qa = _read("qa")
        self.assertNotIn(", run them", qa)
        self.assertIn("do not run them", qa)
        self.assertIn("runner", qa)


class TestPRBodyPromptSchemaRole(unittest.TestCase):
    def test_pr_body_prompt_consistent(self):
        text = _read("pr_body")
        self.assertIn("no shell", text.lower())
        self.assertIn("keep template headings", text.lower())
        self.assertIn("order", text.lower())
        self.assertIn("checklist wording", text.lower())
        self.assertIn("n/a", text.lower())
        self.assertIn("verified list", text.lower())
        self.assertIn("toolbox:create-pr advisory only", text)
        self.assertIn("{\"body\":", text)

        # Ensure valid string.Template with all expected placeholders
        tmpl = string.Template(text)
        rendered = tmpl.substitute(
            template="## Summary",
            title="Title",
            summary="Summary",
            why="Why",
            changes="Changes",
            verification="Verification",
            verified="tests_pass",
        )
        self.assertIn("## Summary", rendered)
        self.assertIn("Title", rendered)
        self.assertIn("tests_pass", rendered)

    def test_pr_body_schema_consistent(self):
        s = schema.load_schema("pr_body")
        self.assertEqual(s.get("type"), "object")
        self.assertEqual(s.get("required"), ["body"])
        self.assertFalse(s.get("additionalProperties", True))
        self.assertEqual(s.get("properties", {}).get("body", {}).get("type"), "string")

        # Valid payload
        self.assertEqual(schema.validate({"body": "PR description"}, s), [])
        # Invalid payloads
        self.assertTrue(schema.validate({}, s))
        self.assertTrue(schema.validate({"body": 123}, s))
        self.assertTrue(schema.validate({"body": "text", "extra": "field"}, s))

    def test_pr_body_role_and_defaults_consistent(self):
        cfg = config.load()
        self.assertEqual(cfg["roles"]["by_role"].get("pr_body"), "primary")
        self.assertEqual(cfg["timeouts_s"].get("pr_body"), 300)
        self.assertEqual(config.agent_for(cfg, "pr_body"), "claude")


if __name__ == "__main__":
    unittest.main()

