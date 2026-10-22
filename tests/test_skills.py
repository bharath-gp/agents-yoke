import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from tests.test_pipeline import PipeBase, ticket, resp, CODE, REV, val
from yoke import router, skills

os.environ.setdefault("YOKE_OLLAMA_URL", "http://127.0.0.1:9")


class SkillsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _write_skill(self, dir_path, name, content):
        skill_dir = os.path.join(dir_path, name)
        os.makedirs(skill_dir, exist_ok=True)
        path = os.path.join(skill_dir, "SKILL.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def test_discover_reads_frontmatter(self):
        root = os.path.join(self.tmp, "user_skills")
        self._write_skill(
            root,
            "skill1",
            "---\nname: \"skill-one\"\ndescription: 'first skill description'\n---\nBody for skill one\n",
        )
        self._write_skill(
            root,
            "skill2",
            "---\nname: skill-two\n---\nBody for skill two without description\n",
        )
        self._write_skill(
            root,
            "skill3",
            "---\ndescription: \"third skill description\"\n---\nBody for skill three\n",
        )

        res = skills.discover([root])
        self.assertEqual(len(res), 3)

        # skill1: quotes stripped
        self.assertEqual(res[0]["name"], "skill-one")
        self.assertEqual(res[0]["description"], "first skill description")
        self.assertTrue(res[0]["path"].endswith(os.path.join("skill1", "SKILL.md")))

        # skill2: missing description gives ""
        self.assertEqual(res[1]["name"], "skill-two")
        self.assertEqual(res[1]["description"], "")
        self.assertTrue(res[1]["path"].endswith(os.path.join("skill2", "SKILL.md")))

        # skill3: fallback to dir name
        self.assertEqual(res[2]["name"], "skill3")
        self.assertEqual(res[2]["description"], "third skill description")
        self.assertTrue(res[2]["path"].endswith(os.path.join("skill3", "SKILL.md")))

    def test_plugin_skills_prefixed_and_newest_version(self):
        fake_home = os.path.join(self.tmp, "home")
        plugin_cache = os.path.join(fake_home, ".claude", "plugins", "cache", "m", "mattpocock-skills")

        v1_dir = os.path.join(plugin_cache, "1.0")
        v1_skills = os.path.join(v1_dir, "skills")
        self._write_skill(
            v1_skills,
            "tdd",
            "---\nname: tdd\ndescription: version 1.0\n---\nVersion 1 body\n",
        )

        v2_dir = os.path.join(plugin_cache, "2.0")
        v2_skills = os.path.join(v2_dir, "skills")
        p2 = self._write_skill(
            v2_skills,
            "tdd",
            "---\nname: tdd\ndescription: version 2.0\n---\nVersion 2 body\n",
        )

        # Set mtimes so 2.0 is newer
        os.utime(v1_dir, (1000, 1000))
        os.utime(v1_skills, (1000, 1000))
        os.utime(v2_dir, (2000, 2000))
        os.utime(v2_skills, (2000, 2000))

        roots = skills.default_roots(repo=None, home=fake_home)
        discovered = skills.discover(roots)

        plugin_skills = [s for s in discovered if s["name"] == "mattpocock-skills:tdd"]
        self.assertEqual(len(plugin_skills), 1)
        self.assertEqual(plugin_skills[0]["path"], p2)
        self.assertIn(os.path.join("2.0", "skills", "tdd", "SKILL.md"), plugin_skills[0]["path"])
        self.assertEqual(plugin_skills[0]["description"], "version 2.0")

    def test_catalog_empty_allowlist_returns_none(self):
        cfg = {"skills": {"allowlist": []}}
        self.assertEqual(skills.catalog(cfg, repo=self.tmp), [])

        cfg_none = {"skills": {"allowlist": None}}
        self.assertEqual(skills.catalog(cfg_none, repo=self.tmp), [])

        cfg_missing = {"skills": {}}
        self.assertEqual(skills.catalog(cfg_missing, repo=self.tmp), [])

    def test_catalog_allowlist_glob(self):
        fake_home = os.path.join(self.tmp, "home")
        user_skills = os.path.join(fake_home, ".claude", "skills")
        self._write_skill(
            user_skills,
            "local-skill",
            "---\nname: local-skill\ndescription: local\n---\nBody\n",
        )

        plugin_cache = os.path.join(fake_home, ".claude", "plugins", "cache", "m", "mattpocock-skills", "1.0", "skills")
        self._write_skill(
            plugin_cache,
            "tdd",
            "---\nname: tdd\ndescription: tdd plugin\n---\nBody\n",
        )

        cfg = {
            "skills": {
                "allowlist": ["mattpocock-skills:*"],
            }
        }
        res = skills.catalog(cfg, repo=None, home=fake_home)
        names = [s["name"] for s in res]
        self.assertEqual(names, ["mattpocock-skills:tdd"])

    def test_render_claude_lists_names(self):
        picked = [
            {"name": "tdd", "description": "test-driven development", "path": "/fake/tdd/SKILL.md"},
            {"name": "refactor", "description": "safe code refactoring", "path": "/fake/refactor/SKILL.md"},
        ]
        cfg = {"skills": {}}
        out = skills.render(picked, agent="claude", cfg=cfg)
        self.assertIn("Relevant skills (invoke with the Skill tool):", out)
        self.assertIn("- tdd: test-driven development", out)
        self.assertIn("- refactor: safe code refactoring", out)

    def test_render_agy_inline_caps_chars(self):
        skill_path = self._write_skill(
            self.tmp,
            "verbose-skill",
            "---\nname: verbose-skill\ndescription: verbose\n---\n" + ("Long body line. " * 30) + "\n",
        )
        picked = [{"name": "verbose-skill", "description": "verbose", "path": skill_path}]
        cfg = {"skills": {"agy_mode": "inline", "inline_chars": 50}}
        out = skills.render(picked, agent="agy", cfg=cfg)
        self.assertLessEqual(len(out), 50)
        self.assertTrue(out.startswith("### verbose-skill\n"))

    def test_render_none(self):
        self.assertEqual(skills.render([], agent="claude", cfg={}), "(none)")
        self.assertEqual(skills.render([], agent="agy", cfg={}), "(none)")
        self.assertEqual(skills.render(None, agent="claude", cfg={}), "(none)")

    def test_render_agy_name_mode(self):
        picked = [{"name": "tdd", "description": "test-driven development", "path": "/fake/tdd/SKILL.md"}]
        cfg = {"skills": {"agy_mode": "name"}}
        out = skills.render(picked, agent="agy", cfg=cfg)
        self.assertIn("Relevant skills (invoke with the Skill tool):", out)
        self.assertIn("- tdd: test-driven development", out)

    def test_frontmatter_edge_cases(self):
        # No frontmatter
        meta, body = skills._frontmatter("Just plain text\nSecond line\n")
        self.assertEqual(meta, {})
        self.assertEqual(body, "Just plain text\nSecond line\n")

        # Unterminated frontmatter
        meta, body = skills._frontmatter("---\nname: broken\nmissing closing dashes\n")
        self.assertEqual(meta, {})
        self.assertEqual(body, "---\nname: broken\nmissing closing dashes\n")

        # Colon inside value
        meta, body = skills._frontmatter("---\nname: my-skill\ndescription: step 1: do this: then that\n---\nBody here\n")
        self.assertEqual(meta["name"], "my-skill")
        self.assertEqual(meta["description"], "step 1: do this: then that")
        self.assertEqual(body, "Body here\n")

        # Empty string
        meta, body = skills._frontmatter("")
        self.assertEqual(meta, {})
        self.assertEqual(body, "")

    def test_render_agy_inline_strips_frontmatter_and_joins_multiple(self):
        p1 = self._write_skill(
            self.tmp,
            "skill-a",
            "---\nname: skill-a\ndescription: desc a\n---\nFirst skill body\n",
        )
        p2 = self._write_skill(
            self.tmp,
            "skill-b",
            "---\nname: skill-b\ndescription: desc b\n---\nSecond skill body\n",
        )
        picked = [
            {"name": "skill-a", "description": "desc a", "path": p1},
            {"name": "skill-b", "description": "desc b", "path": p2},
        ]
        cfg = {"skills": {"agy_mode": "inline", "inline_chars": 4000}}
        out = skills.render(picked, agent="agy", cfg=cfg)
        expected = "### skill-a\nFirst skill body\n\n### skill-b\nSecond skill body"
        self.assertEqual(out, expected)
        self.assertNotIn("description:", out)
        self.assertNotIn("desc a", out)

    def test_catalog_roots_override(self):
        custom_root = os.path.join(self.tmp, "custom_root")
        self._write_skill(custom_root, "custom1", "---\nname: custom1\n---\nBody\n")

        fake_home = os.path.join(self.tmp, "home")
        default_skills = os.path.join(fake_home, ".claude", "skills")
        self._write_skill(default_skills, "default1", "---\nname: default1\n---\nBody\n")

        cfg = {
            "skills": {
                "allowlist": ["*"],
                "roots": [custom_root],
            }
        }
        res = skills.catalog(cfg, repo=self.tmp, home=fake_home)
        names = [s["name"] for s in res]
        self.assertEqual(names, ["custom1"])

    def test_default_roots_repo_and_plugin_filtering(self):
        fake_home = os.path.join(self.tmp, "home")
        fake_repo = os.path.join(self.tmp, "repo")
        repo_skills = os.path.join(fake_repo, ".claude", "skills")
        self._write_skill(repo_skills, "repo-skill", "---\nname: repo-skill\n---\nRepo body\n")

        p1_dir = os.path.join(fake_home, ".claude", "plugins", "cache", "m", "plugin-one", "1.0", "skills")
        self._write_skill(p1_dir, "s1", "---\nname: s1\n---\nBody\n")

        p2_dir = os.path.join(fake_home, ".claude", "plugins", "cache", "m", "plugin-two", "1.0", "skills")
        self._write_skill(p2_dir, "s2", "---\nname: s2\n---\nBody\n")

        roots = skills.default_roots(repo=fake_repo, home=fake_home)
        self.assertIn(os.path.join(fake_repo, ".claude", "skills"), roots)

        # Allowlist matching plugin-one only
        cfg = {"skills": {"allowlist": ["plugin-one:*"]}}
        res = skills.catalog(cfg, repo=fake_repo, home=fake_home)
        self.assertEqual([s["name"] for s in res], ["plugin-one:s1"])

        # Allowlist matching repo skill
        cfg_repo = {"skills": {"allowlist": ["repo-skill"]}}
        res_repo = skills.catalog(cfg_repo, repo=fake_repo, home=fake_home)
        self.assertEqual([s["name"] for s in res_repo], ["repo-skill"])

    def test_discover_multiple_roots_ordering(self):
        root1 = os.path.join(self.tmp, "root1")
        root2 = os.path.join(self.tmp, "root2")
        self._write_skill(root1, "alpha", "---\nname: alpha\n---\nBody\n")
        self._write_skill(root2, "beta", "---\nname: beta\n---\nBody\n")

        res = skills.discover([root1, root2])
        self.assertEqual([s["name"] for s in res], ["alpha", "beta"])

    def test_pick_skills_empty_catalog(self):
        run = mock.MagicMock()
        run.cfg = {"router": {"model": "tev1:4b", "thresholds": {"skill": 0.6}}, "skills": {"max": 3}}
        ticket_data = {"title": "Test ticket"}
        with mock.patch("yoke.router._ollama") as mock_ollama:
            picked = router.pick_skills(run, ticket_data, "code", [])
            self.assertEqual(picked, [])
            mock_ollama.assert_not_called()

    def test_pick_skills_bare_number_and_missing_key(self):
        catalog = [
            {"name": "n0", "description": "d0", "path": "/p0"},
            {"name": "n1", "description": "d1", "path": "/p1"},
            {"name": "n2", "description": "d2", "path": "/p2"},
        ]
        cfg = {"router": {"model": "tev1:4b", "thresholds": {"skill": 0.6}}, "skills": {"max": 3}}
        run = mock.MagicMock()
        run.cfg = cfg
        ticket_data = {"title": "Test ticket"}
        answers = {
            "s0": 0.85,  # bare float
            # s1 is missing
            "s2": {"noul": 0.7},
        }
        with mock.patch("yoke.router._ollama", return_value=answers):
            picked = router.pick_skills(run, ticket_data, "code", catalog)
            self.assertEqual([s["name"] for s in picked], ["n0", "n2"])

    def test_pick_skills_threshold_and_cap(self):
        catalog = [
            {"name": "n0", "description": "d0", "path": "/p0"},
            {"name": "n1", "description": "d1", "path": "/p1"},
            {"name": "n2", "description": "d2", "path": "/p2"},
            {"name": "n3", "description": "d3", "path": "/p3"},
            {"name": "n4", "description": "d4", "path": "/p4"},
        ]
        cfg = {
            "router": {
                "model": "tev1:4b",
                "thresholds": {"skill": 0.6},
            },
            "skills": {"max": 3},
        }
        run = mock.MagicMock()
        run.cfg = cfg
        ticket_data = {"title": "Test ticket", "description": "desc", "exit_criteria": []}

        ollama_answers = {
            "s0": {"noul": 0.9},
            "s1": {"noul": 0.2},
            "s2": {"noul": 0.8},
            "s3": {"noul": 0.7},
            "s4": {"noul": 0.95},
        }
        with mock.patch("yoke.router._ollama", return_value=ollama_answers) as mock_ollama:
            picked = router.pick_skills(run, ticket_data, "code", catalog)
            mock_ollama.assert_called_once()
            names = [s["name"] for s in picked]
            self.assertEqual(names, ["n4", "n0", "n2"])
            self.assertEqual([s["path"] for s in picked], ["/p4", "/p0", "/p2"])
            run.event.assert_called_once()
            kwargs = run.event.call_args[1]
            self.assertEqual(kwargs.get("point"), "skill")
            self.assertEqual(kwargs.get("fn"), "skill")
            self.assertEqual(kwargs.get("source"), "ollama")
            self.assertEqual(kwargs.get("choice"), "n4,n0,n2")

    def test_pick_skills_down_returns_empty(self):
        catalog = [{"name": "n0", "description": "d0", "path": "/p0"}]
        cfg = {"router": {"model": "tev1:4b", "thresholds": {"skill": 0.6}}, "skills": {"max": 3}}
        run = mock.MagicMock()
        run.cfg = cfg
        ticket_data = {"title": "Test ticket"}
        with mock.patch("yoke.router._ollama", side_effect=OSError("connection refused")):
            picked = router.pick_skills(run, ticket_data, "code", catalog)
            self.assertEqual(picked, [])
            run.event.assert_called_once()
            kwargs = run.event.call_args[1]
            self.assertEqual(kwargs.get("source"), "none")
            self.assertEqual(kwargs.get("point"), "skill")
            self.assertIn("ollama failed", kwargs.get("fallback_reason", ""))

    def test_pick_skills_boundary_and_caps(self):
        catalog = [
            {"name": "n_exact", "description": "exact threshold", "path": "/pe"},
            {"name": "n_below", "description": "below threshold", "path": "/pb"},
            {"name": "n_above", "description": "above threshold", "path": "/pa"},
        ]
        cfg = {
            "router": {
                "model": "tev1:4b",
                "thresholds": {"skill": 0.6},
            },
            "skills": {"max": 3},
        }
        run = mock.MagicMock()
        run.cfg = cfg
        ticket_data = {"title": "Test ticket"}
        answers = {
            "s0": {"noul": 0.6},
            "s1": {"noul": 0.599},
            "s2": {"noul": 0.75},
        }
        with mock.patch("yoke.router._ollama", return_value=answers):
            picked = router.pick_skills(run, ticket_data, "code", catalog)
            self.assertEqual([s["name"] for s in picked], ["n_above", "n_exact"])

    def test_pick_skills_caps_at_64_candidates_and_truncates_description(self):
        long_desc = "x" * 300
        catalog = [{"name": "skill_%d" % i, "description": long_desc, "path": "/p%d" % i} for i in range(70)]
        cfg = {
            "router": {"model": "tev1:4b", "thresholds": {"skill": 0.6}},
            "skills": {"max": 5},
        }
        run = mock.MagicMock()
        run.cfg = cfg
        with mock.patch("yoke.router._ollama", return_value={"s0": {"noul": 0.8}}) as mock_ollama:
            picked = router.pick_skills(run, {"title": "T"}, "code", catalog)
            mock_ollama.assert_called_once()
            called_q = mock_ollama.call_args[0][2]
            self.assertEqual(len(called_q), 64)
            self.assertIn("s0", called_q)
            self.assertIn("s63", called_q)
            self.assertNotIn("s64", called_q)
            self.assertIn("x" * 200, called_q["s0"]["instructions"])
            self.assertNotIn("x" * 201, called_q["s0"]["instructions"])
            self.assertEqual([s["name"] for s in picked], ["skill_0"])

    def test_pick_skills_down_truncates_fallback_reason(self):
        catalog = [{"name": "n0", "description": "d0", "path": "/p0"}]
        cfg = {"router": {"model": "tev1:4b", "thresholds": {"skill": 0.6}}, "skills": {"max": 3}}
        run = mock.MagicMock()
        run.cfg = cfg
        ticket_data = {"title": "Test ticket"}
        long_err = "error_" + ("z" * 500)
        with mock.patch("yoke.router._ollama", side_effect=OSError(long_err)):
            picked = router.pick_skills(run, ticket_data, "code", catalog)
            self.assertEqual(picked, [])
            kwargs = run.event.call_args[1]
            self.assertLessEqual(len(kwargs.get("fallback_reason", "")), 200)


class TestSkillPipelineInjection(PipeBase):
    def test_call_injects_skills_into_prompt(self):
        fixture_dir = os.path.join(self.tmp, "fixture_skills")
        skill_dir = os.path.join(fixture_dir, "demo")
        os.makedirs(skill_dir, exist_ok=True)
        with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: demo\ndescription: demo skill description\n---\nDemo skill body\n")

        with open(os.path.join(self.tmp, "config.json"), "w") as f:
            json.dump({"skills": {"allowlist": ["demo"], "roots": [fixture_dir]}}, f)

        cfg = dict(self.run_.cfg)
        cfg["skills"] = {"allowlist": ["demo"], "roots": [fixture_dir], "max": 3, "agy_mode": "inline"}
        self.run_.update(config=cfg)

        self.setup_tickets(
            [ticket(1, type_="chore")],
            {
                "code": [resp(CODE, "c1", write={"f1.txt": "x"})],
                "review": [resp(REV)],
                "validate": [resp(val())],
                "router": [{"exit": 1}],
            },
        )

        with mock.patch("yoke.router._ollama", return_value={"s0": {"noul": 0.9}}):
            status = self.go()

        self.assertEqual(status, "done")
        prompt_path = self.run_.path("tickets/T-001/attempt-1/code/prompt.md")
        self.assertTrue(os.path.exists(prompt_path), "Expected %s to exist" % prompt_path)
        with open(prompt_path, "r", encoding="utf-8") as f:
            prompt_content = f.read()
        self.assertIn("demo", prompt_content)
        self.assertIn("demo skill description", prompt_content)
        self.assertIn("## Skills", prompt_content)
        self.assertNotIn("## Skills\n\n(none)", prompt_content)

    def test_call_allowed_tools_appends_skill_for_claude_when_picked(self):
        from yoke import adapters, pipeline
        fixture_dir = os.path.join(self.tmp, "fixture_skills")
        skill_dir = os.path.join(fixture_dir, "demo")
        os.makedirs(skill_dir, exist_ok=True)
        with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: demo\ndescription: demo skill description\n---\nDemo skill body\n")

        cfg = dict(self.run_.cfg)
        cfg["skills"] = {"allowlist": ["demo"], "roots": [fixture_dir], "max": 3, "agy_mode": "inline"}
        self.run_.update(config=cfg)

        t = ticket(1, type_="feature")

        class DummyClaudeAdapter(adapters.Adapter):
            name = "claude"

        dummy_adapter = DummyClaudeAdapter()

        with mock.patch("yoke.router._ollama", return_value={"s0": {"noul": 0.9}}):
            with mock.patch("yoke.calls.run_call") as mock_rc:
                mock_rc.return_value = adapters.CallResult(structured_output={"status": "done"}, session_id="s1")
                pipeline._call(
                    self.run_,
                    adapter_for=lambda role: dummy_adapter,
                    env={},
                    ticket=t,
                    stage="code",
                    attempt=1,
                    wt=self.repo,
                    base="HEAD",
                    mode="write",
                    results={},
                    feedback="",
                    adapter=dummy_adapter,
                )
                self.assertTrue(mock_rc.called)
                spec = mock_rc.call_args[0][2]
                self.assertIn("Skill", spec.allowed_tools)

    def test_call_allowed_tools_no_skill_for_non_claude_adapter(self):
        from yoke import adapters, pipeline
        fixture_dir = os.path.join(self.tmp, "fixture_skills")
        skill_dir = os.path.join(fixture_dir, "demo")
        os.makedirs(skill_dir, exist_ok=True)
        with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: demo\ndescription: demo skill description\n---\nDemo skill body\n")

        cfg = dict(self.run_.cfg)
        cfg["skills"] = {"allowlist": ["demo"], "roots": [fixture_dir], "max": 3, "agy_mode": "inline"}
        self.run_.update(config=cfg)

        t = ticket(1, type_="feature")

        class DummyAgyAdapter(adapters.Adapter):
            name = "agy"

        dummy_adapter = DummyAgyAdapter()

        with mock.patch("yoke.router._ollama", return_value={"s0": {"noul": 0.9}}):
            with mock.patch("yoke.calls.run_call") as mock_rc:
                mock_rc.return_value = adapters.CallResult(structured_output={"status": "done"}, session_id="s1")
                pipeline._call(
                    self.run_,
                    adapter_for=lambda role: dummy_adapter,
                    env={},
                    ticket=t,
                    stage="code",
                    attempt=1,
                    wt=self.repo,
                    base="HEAD",
                    mode="write",
                    results={},
                    feedback="",
                    adapter=dummy_adapter,
                )
                self.assertTrue(mock_rc.called)
                spec = mock_rc.call_args[0][2]
                self.assertNotIn("Skill", spec.allowed_tools)
                self.assertIn("### demo", spec.prompt)
                self.assertIn("Demo skill body", spec.prompt)

    def test_call_allowed_tools_no_skill_when_empty_pick(self):
        from yoke import adapters, pipeline
        t = ticket(1, type_="feature")

        class DummyClaudeAdapter(adapters.Adapter):
            name = "claude"

        dummy_adapter = DummyClaudeAdapter()
        with mock.patch("yoke.router._ollama", return_value={}), mock.patch(
            "yoke.router.pick_skills"
        ) as mock_pick_skills:
            with mock.patch("yoke.calls.run_call") as mock_rc:
                mock_rc.return_value = adapters.CallResult(structured_output={"status": "done"}, session_id="s1")
                pipeline._call(
                    self.run_,
                    adapter_for=lambda role: dummy_adapter,
                    env={},
                    ticket=t,
                    stage="code",
                    attempt=1,
                    wt=self.repo,
                    base="HEAD",
                    mode="write",
                    results={},
                    feedback="",
                    adapter=dummy_adapter,
                )
                self.assertTrue(mock_rc.called)
                mock_pick_skills.assert_not_called()
                spec = mock_rc.call_args[0][2]
                self.assertNotIn("Skill", spec.allowed_tools)
                self.assertIn("## Skills\n\n(none)", spec.prompt)

    def test_call_injects_skills_into_qa_and_docs_prompts(self):
        from yoke import adapters, pipeline
        fixture_dir = os.path.join(self.tmp, "fixture_skills")
        skill_dir = os.path.join(fixture_dir, "demo")
        os.makedirs(skill_dir, exist_ok=True)
        with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: demo\ndescription: demo skill description\n---\nDemo skill body\n")

        cfg = dict(self.run_.cfg)
        cfg["skills"] = {"allowlist": ["demo"], "roots": [fixture_dir], "max": 3, "agy_mode": "inline"}
        self.run_.update(config=cfg)

        t = ticket(1, type_="feature")

        class DummyClaudeAdapter(adapters.Adapter):
            name = "claude"

        dummy_adapter = DummyClaudeAdapter()

        for stg in ("qa", "docs"):
            with mock.patch("yoke.router._ollama", return_value={"s0": {"noul": 0.9}}):
                with mock.patch("yoke.calls.run_call") as mock_rc:
                    mock_rc.return_value = adapters.CallResult(structured_output={"status": "done"}, session_id="s1")
                    pipeline._call(
                        self.run_,
                        adapter_for=lambda role: dummy_adapter,
                        env={},
                        ticket=t,
                        stage=stg,
                        attempt=1,
                        wt=self.repo,
                        base="HEAD",
                        mode="write",
                        results={},
                        feedback="",
                        adapter=dummy_adapter,
                    )
                    self.assertTrue(mock_rc.called)
                    spec = mock_rc.call_args[0][2]
                    self.assertIn("Skill", spec.allowed_tools)
                    self.assertIn("Relevant skills (invoke with the Skill tool):", spec.prompt)
                    self.assertIn("- demo: demo skill description", spec.prompt)

    def test_call_non_empty_catalog_all_low_scores_gives_no_skill_and_none_in_prompt(self):
        from yoke import adapters, pipeline
        fixture_dir = os.path.join(self.tmp, "fixture_skills")
        skill_dir = os.path.join(fixture_dir, "demo")
        os.makedirs(skill_dir, exist_ok=True)
        with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: demo\ndescription: demo skill description\n---\nDemo skill body\n")

        cfg = dict(self.run_.cfg)
        cfg["skills"] = {"allowlist": ["demo"], "roots": [fixture_dir], "max": 3, "agy_mode": "inline"}
        self.run_.update(config=cfg)

        t = ticket(1, type_="feature")

        class DummyClaudeAdapter(adapters.Adapter):
            name = "claude"

        dummy_adapter = DummyClaudeAdapter()

        with mock.patch("yoke.router._ollama", return_value={"s0": {"noul": 0.1}}):
            with mock.patch("yoke.calls.run_call") as mock_rc:
                mock_rc.return_value = adapters.CallResult(structured_output={"status": "done"}, session_id="s1")
                pipeline._call(
                    self.run_,
                    adapter_for=lambda role: dummy_adapter,
                    env={},
                    ticket=t,
                    stage="code",
                    attempt=1,
                    wt=self.repo,
                    base="HEAD",
                    mode="write",
                    results={},
                    feedback="",
                    adapter=dummy_adapter,
                )
                self.assertTrue(mock_rc.called)
                spec = mock_rc.call_args[0][2]
                self.assertNotIn("Skill", spec.allowed_tools)
                self.assertIn("## Skills\n\n(none)", spec.prompt)

    def test_call_read_only_stages_skip_skills(self):
        from yoke import adapters, pipeline
        fixture_dir = os.path.join(self.tmp, "fixture_skills")
        skill_dir = os.path.join(fixture_dir, "demo")
        os.makedirs(skill_dir, exist_ok=True)
        with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: demo\ndescription: demo skill description\n---\nDemo skill body\n")

        cfg = dict(self.run_.cfg)
        cfg["skills"] = {"allowlist": ["demo"], "roots": [fixture_dir], "max": 3, "agy_mode": "inline"}
        self.run_.update(config=cfg)

        t = ticket(1, type_="feature")

        class DummyClaudeAdapter(adapters.Adapter):
            name = "claude"

        dummy_adapter = DummyClaudeAdapter()

        for ro_stage in ("review", "validate"):
            with mock.patch("yoke.router.pick_skills") as mock_pick_skills, mock.patch(
                "yoke.router._ollama", return_value={}
            ), mock.patch(
                "yoke.router.pick_tier_effort", return_value={"tier": None, "effort": None, "source": "none"}
            ):
                with mock.patch("yoke.calls.run_call") as mock_rc:
                    mock_rc.return_value = adapters.CallResult(structured_output={"verdict": "approve"}, session_id="s1")
                    pipeline._call(
                        self.run_,
                        adapter_for=lambda role: dummy_adapter,
                        env={},
                        ticket=t,
                        stage=ro_stage,
                        attempt=1,
                        wt=self.repo,
                        base="HEAD",
                        mode="read",
                        results={},
                        feedback="",
                        adapter=dummy_adapter,
                    )
                    mock_pick_skills.assert_not_called()
                    spec = mock_rc.call_args[0][2]
                    self.assertNotIn("Skill", spec.allowed_tools)
                    self.assertNotIn("demo", spec.prompt)


if __name__ == "__main__":
    unittest.main()
