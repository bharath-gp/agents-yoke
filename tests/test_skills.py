import os
import shutil
import tempfile
import unittest

from yoke import skills

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


if __name__ == "__main__":
    unittest.main()
