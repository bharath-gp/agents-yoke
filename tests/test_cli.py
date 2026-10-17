import os
import tempfile
import unittest

from yoke import cli, config


class TestDoctor(unittest.TestCase):
    def test_runs_and_reports_python(self):
        lines = []
        problems = cli.doctor(out=lines.append)
        self.assertTrue(lines[0].startswith("python "))
        self.assertIsInstance(problems, list)
        self.assertEqual(cli.main(["doctor"]), 1 if problems else 0)


class TestSaveRoles(unittest.TestCase):
    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as d:
            config.save_roles(os.path.join(d, "config.json"), "agy", "claude", {"code": "primary"})
            cfg = config.load(home=d)
            self.assertEqual(config.agent_for(cfg, "code"), "agy")
            self.assertEqual(config.agent_for(cfg, "qa"), "claude")
            self.assertEqual(config.agent_for(cfg, "planner"), "agy")

    def test_rejects_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                config.save_roles(os.path.join(d, "c.json"), mapping={"nope": "agy"})
            with self.assertRaises(ValueError):
                config.save_roles(os.path.join(d, "c.json"), primary="gpt")


if __name__ == "__main__":
    unittest.main()
