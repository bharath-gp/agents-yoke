import glob
import json
import os
import unittest

from yoke import config, schema


class TestDebateSchemas(unittest.TestCase):
    def test_schema_paths_exist(self):
        for name in ("rebut", "reply", "judge"):
            path = schema.schema_path(name)
            self.assertTrue(os.path.exists(path), "Schema path does not exist: %s" % path)

    def test_debate_timeouts_and_defaults(self):
        cfg = config.load()
        self.assertEqual(cfg.get("review_mode"), "adversarial")
        self.assertEqual(cfg.get("debate_rounds"), 1)
        timeouts = cfg.get("timeouts_s", {})
        for key in ("rebut", "reply", "judge", "plan_rebut", "plan_judge"):
            self.assertEqual(timeouts.get(key), 900, "Timeout for %s should be 900" % key)

    def test_rebut_valid_and_invalid(self):
        s = schema.load_schema("rebut")
        valid = {
            "responses": [
                {
                    "id": "item-1",
                    "stance": "concede",
                    "argument": "Conceding finding.",
                    "evidence": "Fixed in patch.",
                },
                {
                    "id": "item-2",
                    "stance": "dispute",
                    "argument": "Disputing finding.",
                    "evidence": "Tested and working.",
                },
            ],
            "summary": "Summary of rebuttal.",
        }
        self.assertEqual(schema.validate(valid, s), [])

        # empty array is valid
        self.assertEqual(schema.validate({"responses": [], "summary": "None."}, s), [])

        # missing responses array
        missing_responses = {"summary": "Summary."}
        errs = schema.validate(missing_responses, s)
        self.assertTrue(errs)
        self.assertTrue(any("responses" in e for e in errs))

        # stance 'maybe' is invalid
        bad_stance = {
            "responses": [
                {
                    "id": "item-1",
                    "stance": "maybe",
                    "argument": "Uncertain.",
                    "evidence": "None.",
                }
            ],
            "summary": "Summary.",
        }
        errs = schema.validate(bad_stance, s)
        self.assertTrue(errs)
        self.assertTrue(any("stance" in e for e in errs))

        # missing evidence field
        missing_evidence = {
            "responses": [
                {
                    "id": "item-1",
                    "stance": "concede",
                    "argument": "Missing evidence.",
                }
            ],
            "summary": "Summary.",
        }
        errs = schema.validate(missing_evidence, s)
        self.assertTrue(errs)
        self.assertTrue(any("evidence" in e for e in errs))

        # extra top-level key
        extra_key = dict(valid, extra="unexpected")
        errs = schema.validate(extra_key, s)
        self.assertTrue(errs)
        self.assertTrue(any("extra" in e for e in errs))

        # extra nested item key
        extra_item_key = {
            "responses": [
                {
                    "id": "item-1",
                    "stance": "concede",
                    "argument": "Conceding.",
                    "evidence": "Patch.",
                    "extra": "bad",
                }
            ],
            "summary": "Summary.",
        }
        errs = schema.validate(extra_item_key, s)
        self.assertTrue(errs)
        self.assertTrue(any("extra" in e for e in errs))

    def test_reply_valid_and_invalid(self):
        s = schema.load_schema("reply")
        valid = {
            "replies": [
                {
                    "id": "item-1",
                    "stance": "maintain",
                    "argument": "Still an issue.",
                },
                {
                    "id": "item-2",
                    "stance": "withdraw",
                    "argument": "Explanation accepted.",
                },
            ],
            "summary": "Summary of reply.",
        }
        self.assertEqual(schema.validate(valid, s), [])

        # empty array is valid
        self.assertEqual(schema.validate({"replies": [], "summary": "None."}, s), [])

        # missing replies array
        missing_replies = {"summary": "Summary."}
        errs = schema.validate(missing_replies, s)
        self.assertTrue(errs)
        self.assertTrue(any("replies" in e for e in errs))

        # stance 'concede' is invalid for reply
        bad_stance = {
            "replies": [
                {
                    "id": "item-1",
                    "stance": "concede",
                    "argument": "Invalid stance.",
                }
            ],
            "summary": "Summary.",
        }
        errs = schema.validate(bad_stance, s)
        self.assertTrue(errs)
        self.assertTrue(any("stance" in e for e in errs))

        # missing argument
        missing_arg = {
            "replies": [
                {
                    "id": "item-1",
                    "stance": "maintain",
                }
            ],
            "summary": "Summary.",
        }
        errs = schema.validate(missing_arg, s)
        self.assertTrue(errs)
        self.assertTrue(any("argument" in e for e in errs))

        # extra key
        extra_key = dict(valid, extra="bad")
        errs = schema.validate(extra_key, s)
        self.assertTrue(errs)
        self.assertTrue(any("extra" in e for e in errs))

    def test_judge_valid_and_invalid(self):
        s = schema.load_schema("judge")
        valid = {
            "rulings": [
                {
                    "id": "item-1",
                    "ruling": "upheld",
                    "reason": "Finding is valid.",
                },
                {
                    "id": "item-2",
                    "ruling": "overruled",
                    "reason": "Finding is incorrect.",
                },
            ],
            "summary": "Summary of rulings.",
        }
        self.assertEqual(schema.validate(valid, s), [])

        # empty array is valid
        self.assertEqual(schema.validate({"rulings": [], "summary": "None."}, s), [])

        # missing rulings array
        missing_rulings = {"summary": "Summary."}
        errs = schema.validate(missing_rulings, s)
        self.assertTrue(errs)
        self.assertTrue(any("rulings" in e for e in errs))

        # ruling 'dismissed' is invalid
        bad_ruling = {
            "rulings": [
                {
                    "id": "item-1",
                    "ruling": "dismissed",
                    "reason": "Dismissed finding.",
                }
            ],
            "summary": "Summary.",
        }
        errs = schema.validate(bad_ruling, s)
        self.assertTrue(errs)
        self.assertTrue(any("ruling" in e for e in errs))

        # missing summary
        missing_summary = {
            "rulings": [
                {
                    "id": "item-1",
                    "ruling": "upheld",
                    "reason": "Reason.",
                }
            ]
        }
        errs = schema.validate(missing_summary, s)
        self.assertTrue(errs)
        self.assertTrue(any("summary" in e for e in errs))

        # extra key
        extra_key = dict(valid, extra="bad")
        errs = schema.validate(extra_key, s)
        self.assertTrue(errs)
        self.assertTrue(any("extra" in e for e in errs))

    def test_all_schemas_load(self):
        schema_files = glob.glob(os.path.join(schema.SCHEMA_DIR, "*.json"))
        self.assertTrue(len(schema_files) > 0)
        for path in schema_files:
            with open(path) as f:
                data = json.load(f)
            self.assertIsInstance(data, dict)


def test_all_schemas_load():
    TestDebateSchemas().test_all_schemas_load()


if __name__ == "__main__":
    unittest.main()
