"""Tests for yoke.screen decision-model screen module."""
import copy
import json
import unittest
from unittest import mock

from tests.test_branch_points import choice, patch_ollama
from tests.test_pipeline import PipeBase
from yoke import config, pipeline, router, screen


class TestScreen(PipeBase):
    def test_confident_keep_and_discard(self):
        sources = [
            {"url": "https://example.com/1", "title": "t1", "excerpt": "e1"},
            {"url": "https://example.com/2", "title": "t2", "excerpt": "e2"},
        ]
        with patch_ollama({"v": [choice("keep", 0.9), choice("discard", 0.9)]}):
            res = screen.screen_sources(self.run_, {"id": "T-001"}, {"sources": sources})
            self.assertEqual(len(res["kept"]), 1)
            self.assertEqual(res["kept"][0]["url"], "https://example.com/1")
            self.assertEqual(len(res["discarded"]), 1)
            self.assertEqual(res["discarded"][0]["url"], "https://example.com/2")
            self.assertEqual(res["discarded"][0]["by"], "model")
            self.assertEqual(res["discarded"][0]["reason"], "decision model: discard (conf 0.90)")

    def test_low_confidence_source_kept_unsure(self):
        sources = [{"url": "https://example.com/1", "title": "t1", "excerpt": "e1"}]
        with patch_ollama({"v": choice("keep", 0.5)}):
            res = screen.screen_sources(self.run_, {"id": "T-001"}, {"sources": sources})
            self.assertEqual(len(res["kept"]), 1)
            self.assertIn("https://example.com/1", res["unsure"])

    def test_heuristics_no_model_call(self):
        sources = [
            {"url": "ftp://example.com/1", "title": "ftp", "excerpt": "ftp text"},
            {"url": "https://example.com/dup", "title": "d1", "excerpt": "dup text 1"},
            {"url": "https://example.com/dup", "title": "d2", "excerpt": "dup text 2"},
            {"url": "https://example.com/empty", "title": "empty", "excerpt": "   "},
        ]
        with mock.patch("yoke.router._ollama", return_value={"v": choice("keep", 0.9)}) as mock_ollama:
            res = screen.screen_sources(self.run_, {"id": "T-001"}, {"sources": sources})
            # Exactly one candidate is valid ("https://example.com/dup" first appearance);
            # the other three must be discarded by heuristics with zero model calls.
            self.assertEqual(mock_ollama.call_count, 1)
            self.assertEqual(len(res["discarded"]), 3)
            for d in res["discarded"]:
                self.assertEqual(d["by"], "heuristic")
            reasons = [d["reason"] for d in res["discarded"]]
            self.assertIn("not an http(s) URL", reasons)
            self.assertIn("duplicate URL", reasons)
            self.assertIn("empty excerpt", reasons)

    def test_ollama_down_all_unsure_one_attempt(self):
        sources = [
            {"url": "https://example.com/1", "title": "t1", "excerpt": "e1"},
            {"url": "https://example.com/2", "title": "t2", "excerpt": "e2"},
            {"url": "https://example.com/3", "title": "t3", "excerpt": "e3"},
        ]
        t = {"id": "T-001", "title": "test", "description": "desc"}
        with mock.patch("yoke.router._ollama", side_effect=RuntimeError("ollama down")) as mock_ollama:
            res_s = screen.screen_sources(self.run_, t, {"sources": sources})
            self.assertEqual(mock_ollama.call_count, 1)
            self.assertEqual(len(res_s["kept"]), 3)
            self.assertEqual(res_s["unsure"], ["https://example.com/1", "https://example.com/2", "https://example.com/3"])

            claims = [
                {"id": "C1", "claim": "claim 1", "source_urls": ["https://example.com/1"]},
                {"id": "C2", "claim": "claim 2", "source_urls": ["https://example.com/2"]},
            ]
            res_c = screen.screen_claims(self.run_, t, {"claims": claims}, res_s["kept"])
            self.assertEqual(res_c["escalated"], ["C1", "C2"])
            self.assertEqual(mock_ollama.call_count, 2)

    def test_claim_supported_unsupported_escalated(self):
        kept = [
            {"url": "https://example.com/1", "excerpt": "e1"},
            {"url": "https://example.com/2", "excerpt": "e2"},
            {"url": "https://example.com/3", "excerpt": "e3"},
        ]
        claims = [
            {"id": "C1", "claim": "c1", "source_urls": ["https://example.com/1"]},
            {"id": "C2", "claim": "c2", "source_urls": ["https://example.com/2"]},
            {"id": "C3", "claim": "c3", "source_urls": ["https://example.com/3"]},
        ]
        with patch_ollama({"v": [choice("supported", 0.9), choice("unsupported", 0.9), choice("supported", 0.4)]}):
            res = screen.screen_claims(self.run_, {"id": "T-001"}, {"claims": claims}, kept)
            self.assertEqual(res["supported"], ["C1"])
            self.assertEqual([u["id"] for u in res["unsupported"]], ["C2"])
            self.assertEqual(res["unsupported"][0]["reason"], "decision model: unsupported (conf 0.90)")
            self.assertEqual(res["escalated"], ["C3"])
            self.assertEqual(res["conf_by_id"]["C1"], 0.9)

    def test_claim_without_kept_source_unsupported_no_model_call(self):
        kept = [{"url": "https://example.com/kept", "excerpt": "e_kept"}]
        claims = [
            {"id": "C1", "claim": "c1", "source_urls": ["https://example.com/not-kept"]},
            {"id": "C2", "claim": "c2", "source_urls": []},
            {"id": "C3", "claim": "c3"},
        ]
        with mock.patch("yoke.router._ollama") as mock_ollama:
            res = screen.screen_claims(self.run_, {"id": "T-001"}, {"claims": claims}, kept)
            mock_ollama.assert_not_called()
            unsupported_ids = [u["id"] for u in res["unsupported"]]
            self.assertEqual(unsupported_ids, ["C1", "C2", "C3"])
            for u in res["unsupported"]:
                self.assertEqual(u["reason"], "no cited source among kept sources")

        # Mixed citation claim: citations include both kept and not-kept URLs.
        # State passed to ollama should include the kept URL and not the dropped URL.
        captured_state = []

        def fake_ollama(cfg, state, questions, timeout=60):
            captured_state.append(state)
            return {"v": choice("supported", 0.9)}

        claims_mixed = [
            {"id": "C4", "claim": "c4", "source_urls": ["https://example.com/kept", "https://example.com/not-kept"]}
        ]
        with mock.patch("yoke.router._ollama", side_effect=fake_ollama):
            res_mixed = screen.screen_claims(self.run_, {"id": "T-001"}, {"claims": claims_mixed}, kept)
            self.assertEqual(res_mixed["supported"], ["C4"])
            self.assertEqual(len(captured_state), 1)
            self.assertIn("URL: https://example.com/kept", captured_state[0])
            self.assertNotIn("URL: https://example.com/not-kept", captured_state[0])

    def test_max_items_cap(self):
        cfg = config.deep_merge(self.run_.cfg, {"research": {"screen": {"max_items": 1}}})
        self.run_.update(config=cfg)
        sources = [
            {"url": "https://example.com/1", "excerpt": "e1"},
            {"url": "https://example.com/2", "excerpt": "e2"},
            {"url": "https://example.com/3", "excerpt": "e3"},
        ]
        with patch_ollama({"v": choice("keep", 0.9)}):
            with mock.patch("yoke.router._ollama", wraps=router._ollama) as mock_ollama:
                res = screen.screen_sources(self.run_, {"id": "T-001"}, {"sources": sources})
                self.assertEqual(mock_ollama.call_count, 1)
                self.assertEqual(len(res["kept"]), 3)
                self.assertEqual(res["unsure"], ["https://example.com/2", "https://example.com/3"])

        claims = [
            {"id": "C1", "claim": "c1", "source_urls": ["https://example.com/1"]},
            {"id": "C2", "claim": "c2", "source_urls": ["https://example.com/2"]},
        ]
        with patch_ollama({"v": choice("supported", 0.9)}):
            with mock.patch("yoke.router._ollama", wraps=router._ollama) as mock_ollama_claims:
                res_c = screen.screen_claims(self.run_, {"id": "T-001"}, {"claims": claims}, res["kept"])
                self.assertEqual(mock_ollama_claims.call_count, 1)
                self.assertEqual(res_c["supported"], ["C1"])
                self.assertEqual(res_c["escalated"], ["C2"])

    def test_synthetic_factcheck_passes_factcheck_failures(self):
        research_out = {
            "status": "done",
            "summary": "s",
            "claims": [
                {"id": "C1", "claim": "claim 1", "source_urls": ["https://example.com/1"]},
                {"id": "C2", "claim": "claim 2", "source_urls": ["https://example.com/2"]},
            ],
        }
        kept = [
            {"url": "https://example.com/1", "title": "t1", "excerpt": "e1", "relevance": "r1"},
            {"url": "https://example.com/2", "title": "t2", "excerpt": "e2", "relevance": "r2"},
        ]
        screen_state = {
            "supported": ["C1", "C2"],
            "unsupported": [],
            "escalated": [],
            "conf_by_id": {"C1": 0.91, "C2": 0.88},
        }
        res = screen.synthetic_factcheck(research_out, kept, screen_state)
        self.assertEqual(pipeline.factcheck_failures(res), [])
        self.assertTrue(res["screened"])
        self.assertEqual(res["verdict"], "pass")
        self.assertEqual(res["feedback"], "")
        self.assertEqual(res["claims"][0]["evidence"], "screened by decision model (conf 0.91)")
        self.assertEqual(res["claims"][0]["source_urls"], ["https://example.com/1"])
        self.assertIsNone(res["claims"][0]["correction"])
        self.assertEqual(res["sources"], [
            {"url": "https://example.com/1", "status": "ok", "note": "screened by decision model"},
            {"url": "https://example.com/2", "status": "ok", "note": "screened by decision model"},
        ])

    def test_should_skip_requires_supported_claim(self):
        cfg = self.run_.cfg
        self.assertFalse(screen.should_skip(cfg, {"supported": []}))
        self.assertFalse(screen.should_skip(cfg, {"supported": ["C1"], "unsure_sources": ["https://example.com/1"]}))
        self.assertFalse(screen.should_skip(cfg, {"supported": ["C1"], "escalated": ["C2"]}))
        self.assertTrue(screen.should_skip(cfg, {"supported": ["C1"]}))
        cfg_noskip = config.deep_merge(cfg, {"research": {"screen": {"skip_factcheck": False}}})
        self.assertFalse(screen.should_skip(cfg_noskip, {"supported": ["C1"]}))

    def test_merge_supported_fills_missing_claims(self):
        fc_out = {
            "verdict": "pass",
            "feedback": "",
            "summary": "s",
            "claims": [
                {"id": "C1", "claim": "claim 1", "verdict": "corrected", "evidence": "human", "source_urls": [], "correction": "c"}
            ],
            "sources": [{"url": "https://example.com/1", "status": "ok", "note": "ok"}],
        }
        research_out = {
            "claims": [
                {"id": "C1", "claim": "claim 1", "source_urls": ["https://example.com/1"]},
                {"id": "C2", "claim": "claim 2", "source_urls": ["https://example.com/2"]},
            ],
        }
        screen_state = {
            "supported": ["C1", "C2"],
            "conf_by_id": {"C1": 0.95, "C2": 0.92},
        }
        merged = screen.merge_supported(fc_out, research_out, screen_state)
        # C1 already existed in fc_out, so its original verdict is preserved and not duplicated
        self.assertEqual([c["id"] for c in merged["claims"]], ["C1", "C2"])
        c1_entry = [c for c in merged["claims"] if c["id"] == "C1"][0]
        self.assertEqual(c1_entry["verdict"], "corrected")
        c2_entry = [c for c in merged["claims"] if c["id"] == "C2"][0]
        self.assertEqual(c2_entry["verdict"], "supported")

        # Missing claims key or empty fc_out handled gracefully
        merged_empty = screen.merge_supported({}, research_out, screen_state)
        self.assertEqual([c["id"] for c in merged_empty["claims"]], ["C1", "C2"])
        self.assertEqual(pipeline.factcheck_failures(merged_empty), [])

    def test_norm_url_variants(self):
        self.assertEqual(
            screen.norm_url("  HTTPS://Example.COM:8080/path/to/doc/#heading  "),
            "https://example.com:8080/path/to/doc"
        )
        self.assertEqual(screen.norm_url("http://example.com/a/b/"), "http://example.com/a/b")
        self.assertEqual(screen.norm_url("http://example.com/"), "http://example.com")
        self.assertEqual(screen.norm_url(""), "")
        self.assertEqual(screen.norm_url(None), "")

    def test_screen_events(self):
        sources = [
            {"url": "https://example.com/1", "excerpt": "e1"},
            {"url": "https://example.com/2", "excerpt": "e2"},
        ]
        claims = [
            {"id": "C1", "claim": "c1", "source_urls": ["https://example.com/1"]},
            {"id": "C2", "claim": "c2", "source_urls": ["https://example.com/2"]},
        ]
        t = {"id": "T-042"}
        with patch_ollama({
            "v": [
                choice("keep", 0.9),
                choice("discard", 0.9),
                choice("supported", 0.9),
                choice("unsupported", 0.9),
            ]
        }):
            screen.screen_sources(self.run_, t, {"sources": sources}, attempt=2)
            screen.screen_claims(self.run_, t, {"claims": claims}, sources, attempt=2)

        with open(self.run_.path("events.jsonl")) as f:
            events = [json.loads(line) for line in f if '"screen"' in line]
        self.assertGreaterEqual(len(events), 2)
        ev_s, ev_c = events[-2], events[-1]
        self.assertEqual(ev_s["kind"], "screen")
        self.assertEqual(ev_s["ticket"], "T-042")
        self.assertEqual(ev_s["attempt"], 2)
        self.assertEqual(ev_s["stage"], "search")
        self.assertEqual(ev_s["kept"], 1)
        self.assertEqual(ev_s["discarded"], 1)
        self.assertEqual(ev_s["unsure"], 0)

        self.assertEqual(ev_c["kind"], "screen")
        self.assertEqual(ev_c["ticket"], "T-042")
        self.assertEqual(ev_c["attempt"], 2)
        self.assertEqual(ev_c["stage"], "research")
        self.assertEqual(ev_c["supported"], 1)
        self.assertEqual(ev_c["unsupported"], 1)
        self.assertEqual(ev_c["escalated"], 0)

    def test_cfg_not_mutated_and_source_threshold(self):
        cfg_plain = copy.deepcopy(self.run_.cfg)
        cfg_plain["research"]["screen"]["min_confidence"] = {"sources": 0.95}
        cfg_before = copy.deepcopy(cfg_plain)
        overridden = screen._override(cfg_plain, "source_screen", "sources")
        self.assertEqual(cfg_plain, cfg_before)
        self.assertEqual(cfg_plain["router"]["thresholds"]["source_screen"], 0.75)
        self.assertEqual(overridden["router"]["thresholds"]["source_screen"], 0.95)
        self.assertIsNot(overridden, cfg_plain)
        self.assertIsNot(overridden["router"], cfg_plain["router"])
        self.assertIsNot(overridden["router"]["thresholds"], cfg_plain["router"]["thresholds"])

        sources = [{"url": "https://example.com/1", "excerpt": "e1"}]
        cfg_high = config.deep_merge(self.run_.cfg, {
            "research": {"screen": {"min_confidence": {"sources": 0.95}}}
        })
        self.run_.update(config=cfg_high)
        with patch_ollama({"v": choice("keep", 0.90)}):
            res = screen.screen_sources(self.run_, {"id": "T-001"}, {"sources": sources})
            # 0.90 < 0.95 -> unsure
            self.assertEqual(res["unsure"], ["https://example.com/1"])
            self.assertEqual(len(res["kept"]), 1)
        # Check that run.cfg router thresholds were not mutated
        self.assertEqual(self.run_.cfg["router"]["thresholds"]["claim_screen"], 0.8)
        self.assertEqual(self.run_.cfg["router"]["thresholds"]["source_screen"], 0.75)

    def test_escalate_on_unsure_false_sources(self):
        cfg = config.deep_merge(self.run_.cfg, {
            "research": {"screen": {"escalate_on_unsure": False}}
        })
        self.run_.update(config=cfg)
        sources = [{"url": "https://example.com/1", "excerpt": "e1"}]
        with patch_ollama({"v": choice("keep", 0.5)}):
            res = screen.screen_sources(self.run_, {"id": "T-001"}, {"sources": sources})
            # When escalate_on_unsure is False, kept includes the unsure source, but unsure list is empty
            self.assertEqual(len(res["kept"]), 1)
            self.assertEqual(res["unsure"], [])

    def test_state_claim_fits_and_keeps_header(self):
        captured_state = []

        def fake_ollama(cfg, state, questions, timeout=60):
            captured_state.append(state)
            return {"v": choice("supported", 0.9)}

        with mock.patch("yoke.router._ollama", side_effect=fake_ollama):
            long_excerpt = "START_" + ("y" * 19990) + "_END"
            kept = [{"url": "https://example.com/doc", "excerpt": long_excerpt}]
            claims = [{"id": "C1", "claim": "Super Claim", "source_urls": ["https://example.com/doc"]}]
            screen.screen_claims(self.run_, {"id": "T-001"}, {"claims": claims}, kept)
            self.assertEqual(len(captured_state), 1)
            state = captured_state[0]
            self.assertLessEqual(len(state), router.STATE_CHARS)
            self.assertTrue(state.startswith("Claim: Super Claim"))
            self.assertTrue(state.endswith("_END"))

    def test_malformed_claim_handled(self):
        kept = [{"url": "https://example.com/1", "excerpt": "e1"}]
        claims = [
            "not-a-dict",
            None,
            {"id": "C1", "claim": "c1", "source_urls": ["https://example.com/1"]},
        ]
        with patch_ollama({"v": choice("supported", 0.9)}):
            res = screen.screen_claims(self.run_, {"id": "T-001"}, {"claims": claims}, kept)
            self.assertEqual(res["supported"], ["C1"])

    def test_url_normalization_matches_claim_sources(self):
        kept = [{"url": "https://a.example/doc/", "excerpt": "doc text"}]
        research_out = {
            "claims": [{"id": "C1", "claim": "c1", "source_urls": ["https://a.example/doc#s"]}],
        }
        with patch_ollama({"v": choice("supported", 0.9)}):
            res = screen.screen_claims(self.run_, {"id": "T-001"}, research_out, kept)
            self.assertNotIn("C1", [u["id"] for u in res["unsupported"]])
            self.assertIn("C1", res["supported"])

    def test_min_confidence_override_and_escalate_off(self):
        kept = [{"url": "https://example.com/1", "excerpt": "e1"}]
        research_out = {
            "claims": [{"id": "C1", "claim": "c1", "source_urls": ["https://example.com/1"]}],
        }
        cfg1 = config.deep_merge(self.run_.cfg, {
            "research": {"screen": {"min_confidence": {"claims": 0.95}, "escalate_on_unsure": True}}
        })
        self.run_.update(config=cfg1)
        with patch_ollama({"v": choice("supported", 0.9)}):
            res1 = screen.screen_claims(self.run_, {"id": "T-001"}, research_out, kept)
            self.assertIn("C1", res1["escalated"])
            self.assertNotIn("C1", res1["supported"])

        cfg2 = config.deep_merge(self.run_.cfg, {
            "research": {"screen": {"min_confidence": {"claims": 0.95}, "escalate_on_unsure": False}}
        })
        self.run_.update(config=cfg2)
        with patch_ollama({"v": choice("supported", 0.9)}):
            res2 = screen.screen_claims(self.run_, {"id": "T-001"}, research_out, kept)
            self.assertIn("C1", res2["supported"])
            self.assertNotIn("C1", res2["escalated"])
            self.assertEqual(res2["conf_by_id"]["C1"], 0.9)

        cfg3 = config.deep_merge(self.run_.cfg, {
            "research": {"screen": {"max_items": 0, "escalate_on_unsure": False}}
        })
        self.run_.update(config=cfg3)
        res3 = screen.screen_claims(self.run_, {"id": "T-001"}, research_out, kept)
        self.assertEqual(res3["supported"], ["C1"])
        self.assertEqual(res3["conf_by_id"], {"C1": 0.0})

    def test_state_fits_and_keeps_header(self):
        captured_state = []

        def fake_ollama(cfg, state, questions, timeout=60):
            captured_state.append(state)
            return {"v": choice("keep", 0.9)}

        with mock.patch("yoke.router._ollama", side_effect=fake_ollama):
            long_excerpt = "START_" + ("x" * 19990) + "_END"
            sources = [{"url": "https://example.com/doc", "title": "Big Doc", "excerpt": long_excerpt}]
            screen.screen_sources(self.run_, {"id": "T-001", "title": "Topic Title"}, {"sources": sources})
            self.assertEqual(len(captured_state), 1)
            state = captured_state[0]
            self.assertLessEqual(len(state), router.STATE_CHARS)
            self.assertTrue(state.startswith("Topic: Topic Title"))
            self.assertIn("URL: https://example.com/doc", state)
            self.assertIn("Title: Big Doc", state)
            self.assertTrue(state.endswith("_END"))

    def test_disabled(self):
        self.assertTrue(screen.enabled(self.run_.cfg))
        cfg_disabled = config.deep_merge(self.run_.cfg, {"research": {"screen": {"enabled": False}}})
        self.assertFalse(screen.enabled(cfg_disabled))
        self.assertFalse(screen.enabled({}))

    def test_heuristics_all_discarded_zero_model_calls(self):
        sources = [
            {"url": "ftp://example.com/1", "excerpt": "ftp text"},
            {"url": "https://example.com/empty", "excerpt": "   "},
        ]
        with mock.patch("yoke.router._ollama") as mock_ollama:
            res = screen.screen_sources(self.run_, {"id": "T-001"}, {"sources": sources})
            mock_ollama.assert_not_called()
            self.assertEqual(len(res["kept"]), 0)
            self.assertEqual(len(res["discarded"]), 2)

    def test_dedupe_and_filter_normalized_urls(self):
        sources = [
            {"url": "https://A.Example.COM/page/", "excerpt": "first"},
            {"url": "https://a.example.com/page#anchor", "excerpt": "second"},
        ]
        candidates, discarded = screen._dedupe_and_filter(sources)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(len(discarded), 1)
        self.assertEqual(discarded[0]["reason"], "duplicate URL")
        self.assertEqual(discarded[0]["by"], "heuristic")

    def test_ollama_failure_partway_preserves_earlier_decisions(self):
        sources = [
            {"url": "https://example.com/1", "excerpt": "e1"},
            {"url": "https://example.com/2", "excerpt": "e2"},
            {"url": "https://example.com/3", "excerpt": "e3"},
        ]
        t = {"id": "T-001", "title": "test", "description": "desc"}
        with mock.patch("yoke.router._ollama", side_effect=[{"v": choice("discard", 0.9)}, RuntimeError("down")]) as mock_ollama:
            res_s = screen.screen_sources(self.run_, t, {"sources": sources})
            self.assertEqual(mock_ollama.call_count, 2)
            self.assertEqual(len(res_s["discarded"]), 1)
            self.assertEqual(res_s["discarded"][0]["url"], "https://example.com/1")
            self.assertEqual(res_s["discarded"][0]["by"], "model")
            self.assertEqual(len(res_s["kept"]), 2)
            self.assertEqual(res_s["unsure"], ["https://example.com/2", "https://example.com/3"])

    def test_ollama_failure_partway_claims(self):
        kept = [
            {"url": "https://example.com/1", "excerpt": "e1"},
            {"url": "https://example.com/2", "excerpt": "e2"},
            {"url": "https://example.com/3", "excerpt": "e3"},
        ]
        claims = [
            {"id": "C1", "claim": "c1", "source_urls": ["https://example.com/1"]},
            {"id": "C2", "claim": "c2", "source_urls": ["https://example.com/2"]},
            {"id": "C3", "claim": "c3", "source_urls": ["https://example.com/3"]},
        ]
        t = {"id": "T-001"}
        with mock.patch("yoke.router._ollama", side_effect=[{"v": choice("supported", 0.9)}, RuntimeError("down")]) as mock_ollama:
            res_c = screen.screen_claims(self.run_, t, {"claims": claims}, kept)
            self.assertEqual(mock_ollama.call_count, 2)
            self.assertEqual(res_c["supported"], ["C1"])
            self.assertEqual(res_c["escalated"], ["C2", "C3"])

    def test_low_confidence_discard_kept_unsure(self):
        sources = [{"url": "https://example.com/1", "excerpt": "e1"}]
        with patch_ollama({"v": choice("discard", 0.5)}):
            res = screen.screen_sources(self.run_, {"id": "T-001"}, {"sources": sources})
            self.assertEqual(len(res["kept"]), 1)
            self.assertEqual(res["kept"][0]["url"], "https://example.com/1")
            self.assertEqual(res["unsure"], ["https://example.com/1"])
            self.assertEqual(len(res["discarded"]), 0)

    def test_ask_direct_and_default_threshold_fallback(self):
        # When min_confidence is deleted and router.thresholds.claim_screen is 0.6,
        # fallback threshold 0.6 accepts conf 0.78
        with patch_ollama({"v": choice("supported", 0.78)}):
            cfg_no_min = copy.deepcopy(self.run_.cfg)
            cfg_no_min.setdefault("research", {}).setdefault("screen", {}).pop("min_confidence", None)
            cfg_no_min.setdefault("router", {}).setdefault("thresholds", {})["claim_screen"] = 0.6
            self.run_.update(config=cfg_no_min)
            c, conf, reason = screen._ask(self.run_, "claim_screen", "Claim: test\nExcerpt: e")
            self.assertEqual(c, "supported")
            self.assertEqual(conf, 0.78)
            self.assertIsNone(reason)

        # When router.thresholds.claim_screen is 0.85, conf 0.78 is rejected
        with patch_ollama({"v": choice("supported", 0.78)}):
            cfg_high = copy.deepcopy(self.run_.cfg)
            cfg_high.setdefault("research", {}).setdefault("screen", {}).pop("min_confidence", None)
            cfg_high.setdefault("router", {}).setdefault("thresholds", {})["claim_screen"] = 0.85
            self.run_.update(config=cfg_high)
            c, conf, reason = screen._ask(self.run_, "claim_screen", "Claim: test\nExcerpt: e")
            self.assertIsNone(c)
            self.assertEqual(conf, 0.78)
            self.assertIn("ollama confidence 0.78 < 0.85", reason)

    def test_defaults_structure(self):
        cfg = self.run_.cfg
        screen_cfg = cfg["research"]["screen"]
        self.assertTrue(screen_cfg["enabled"])
        self.assertTrue(screen_cfg["skip_factcheck"])
        self.assertTrue(screen_cfg["escalate_on_unsure"])
        self.assertEqual(screen_cfg["max_items"], 25)
        self.assertEqual(screen_cfg["min_confidence"]["sources"], 0.75)
        self.assertEqual(screen_cfg["min_confidence"]["claims"], 0.8)
        self.assertEqual(cfg["router"]["thresholds"]["source_screen"], 0.75)
        self.assertEqual(cfg["router"]["thresholds"]["claim_screen"], 0.8)

    def test_should_skip_edge_cases(self):
        cfg = self.run_.cfg
        self.assertFalse(screen.should_skip(cfg, None))
        self.assertFalse(screen.should_skip(cfg, {}))
        self.assertFalse(screen.should_skip(None, {"supported": ["C1"]}))

    def test_claim_missing_id_unsupported(self):
        kept = [{"url": "https://example.com/1", "excerpt": "e1"}]
        claims = [
            {"claim": "no id", "source_urls": ["https://example.com/1"]},
            {"id": "", "claim": "empty id", "source_urls": ["https://example.com/1"]},
            {"id": ["unhashable_list"], "claim": "unhashable", "source_urls": ["https://example.com/1"]},
        ]
        with mock.patch("yoke.router._ollama") as mock_ollama:
            res = screen.screen_claims(self.run_, {"id": "T-001"}, {"claims": claims}, kept)
            mock_ollama.assert_not_called()
            self.assertEqual(len(res["supported"]), 0)
            self.assertEqual(len(res["unsupported"]), 3)
            for u in res["unsupported"]:
                self.assertEqual(u["reason"], "missing claim id")

        # Synthetic factcheck and merge_supported do not raise with unhashable or non-string IDs
        fc_state = {"supported": [["unhashable"], None, 123, "C1"], "conf_by_id": {"C1": 0.9}}
        synth = screen.synthetic_factcheck({"claims": [{"id": "C1", "claim": "ok"}]}, kept, fc_state)
        self.assertEqual(len(synth["claims"]), 1)
        self.assertEqual(synth["claims"][0]["id"], "C1")
        merged = screen.merge_supported({"claims": []}, {"claims": [{"id": "C1", "claim": "ok"}]}, fc_state)
        self.assertEqual(len(merged["claims"]), 1)
        self.assertEqual(merged["claims"][0]["id"], "C1")

    def test_long_header_capped_and_fits(self):
        long_topic = "T" * 10000
        src = {
            "url": "https://example.com/doc",
            "title": "Title",
            "excerpt": "START_" + ("x" * 2000) + "_END",
        }
        state = screen._state_source(long_topic, src)
        self.assertLessEqual(len(state), router.STATE_CHARS)
        self.assertTrue(state.startswith("Topic: "))
        self.assertIn("URL: https://example.com/doc", state)
        self.assertTrue(state.endswith("_END"))

    def test_malformed_url_discarded_heuristically(self):
        sources = [
            {"url": "http://[x", "title": "bad ipv6", "excerpt": "excerpt"},
            {"url": 12345, "title": "non string", "excerpt": "excerpt"},
        ]
        with mock.patch("yoke.router._ollama") as mock_ollama:
            res_s = screen.screen_sources(self.run_, {"id": "T-001"}, {"sources": sources})
            mock_ollama.assert_not_called()
            self.assertEqual(len(res_s["kept"]), 0)
            self.assertEqual(len(res_s["discarded"]), 2)
            self.assertEqual(res_s["discarded"][0]["reason"], "not an http(s) URL")
            self.assertEqual(res_s["discarded"][0]["by"], "heuristic")

        kept = [{"url": "https://example.com/1", "excerpt": "e1"}]
        claims = [
            {"id": "C1", "claim": "c1", "source_urls": ["http://[bad/"]},
        ]
        with mock.patch("yoke.router._ollama") as mock_ollama:
            res_c = screen.screen_claims(self.run_, {"id": "T-001"}, {"claims": claims}, kept)
            mock_ollama.assert_not_called()
            self.assertEqual(len(res_c["unsupported"]), 1)
            self.assertEqual(res_c["unsupported"][0]["reason"], "no cited source among kept sources")


if __name__ == "__main__":
    unittest.main()
