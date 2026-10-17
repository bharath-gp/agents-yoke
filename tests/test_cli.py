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
            with self.assertRaises(ValueError):
                config.save_roles(os.path.join(d, "c.json"), mapping={"code": "auto"})
            res = config.save_roles(os.path.join(d, "c.json"), mapping={"review": "auto"})
            self.assertEqual(res["by_role"]["review"], "auto")


class TestSetup(unittest.TestCase):
    def test_setup_prints_review_and_warns_on_override(self):
        import contextlib
        import io
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, {"YOKE_HOME": d}), \
                 mock.patch("yoke.cli.doctor", return_value=[]):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    rc = cli.main(["setup"])
                self.assertEqual(rc, 0)
                text = out.getvalue()
                self.assertRegex(text, r"review\s+claude")
                self.assertNotIn("warning", text.lower())

                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    rc = cli.main(["setup", "--map", "review=agy"])
                self.assertEqual(rc, 0)
                text = out.getvalue()
                self.assertRegex(text, r"review\s+agy")
                self.assertIn("warning", text.lower())


if __name__ == "__main__":
    unittest.main()
