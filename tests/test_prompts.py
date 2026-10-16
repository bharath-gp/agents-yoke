import os
import unittest

PROMPTS = os.path.join(os.path.dirname(__file__), "..", "yoke", "prompts")
NO_SHELL = "You have no shell in this environment. Do not run any command; edit files only."


def _read(name):
    with open(os.path.join(PROMPTS, name + ".md")) as f:
        return f.read()


class TestNoShellPrompts(unittest.TestCase):
    def test_agy_stages_say_no_shell(self):
        for stage in ("code", "qa", "docs"):
            self.assertIn(NO_SHELL, _read(stage), stage)

    def test_qa_does_not_run_tests(self):
        qa = _read("qa")
        self.assertNotIn(", run them", qa)
        self.assertIn("do not run them", qa)
        self.assertIn("runner", qa)


if __name__ == "__main__":
    unittest.main()
