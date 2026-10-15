import unittest

from agyloop import cli


class TestDoctor(unittest.TestCase):
    def test_runs_and_reports_python(self):
        lines = []
        problems = cli.doctor(out=lines.append)
        self.assertTrue(lines[0].startswith("python "))
        self.assertIsInstance(problems, list)
        self.assertEqual(cli.main(["doctor"]), 1 if problems else 0)


if __name__ == "__main__":
    unittest.main()
