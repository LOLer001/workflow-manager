from __future__ import annotations

import json
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
SKILL_ROOT = PLUGIN_ROOT / "skills" / "task-planner"


class TaskPlannerPackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    def test_marketplace_target_and_metadata_are_compatible(self) -> None:
        self.assertEqual(PLUGIN_ROOT.name, "task-planner")
        self.assertEqual(PLUGIN_ROOT.parent.name, "plugins")
        self.assertEqual(self.manifest["name"], "task-planner")
        self.assertEqual(self.manifest["version"], "1.0.0")
        self.assertEqual(self.manifest["skills"], "./skills")
        self.assertEqual(self.manifest["interface"]["displayName"], "Task Planner")
        self.assertTrue((PLUGIN_ROOT / self.manifest["skills"]).is_dir())
        marketplace = json.loads(
            (PLUGIN_ROOT.parents[1] / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8")
        )
        entries = [entry for entry in marketplace["plugins"] if entry["name"] == self.manifest["name"]]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["source"], {"source": "local", "path": "./plugins/task-planner"})
        self.assertEqual(entries[0]["policy"]["installation"], "AVAILABLE")

    def test_skill_requires_explicit_invocation(self) -> None:
        skill_text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        agent_text = (SKILL_ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
        self.assertIn("name: task-planner", skill_text.split("---", 2)[1].splitlines())
        agent_lines = [line.strip() for line in agent_text.splitlines()]
        self.assertIn('display_name: "Task Planner"', agent_lines)
        self.assertIn('default_prompt: "Use $task-planner', agent_text)
        self.assertIn("allow_implicit_invocation: false", agent_lines)

    def test_package_has_no_runtime_or_hidden_control_surface(self) -> None:
        forbidden_manifest_keys = {"hooks", "mcpServers", "mcp", "apps", "settings", "installer"}

        def keys(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    yield key
                    yield from keys(child)
            elif isinstance(value, list):
                for child in value:
                    yield from keys(child)

        self.assertTrue(forbidden_manifest_keys.isdisjoint(keys(self.manifest)))

        forbidden_parts = {"hooks", "scripts", "state", "config", "settings", "installer"}
        binary_suffixes = {".bin", ".dll", ".exe", ".so", ".dylib", ".wasm"}
        runtime_files = {".mcp.json", "mcp.json", ".app.json", "settings.json", "hooks.json"}
        offenders: list[str] = []
        for path in PLUGIN_ROOT.rglob("*"):
            relative = path.relative_to(PLUGIN_ROOT)
            if path.is_symlink():
                offenders.append(str(relative))
                continue
            lowered_parts = {part.lower() for part in relative.parts}
            if lowered_parts & forbidden_parts or path.suffix.lower() in binary_suffixes or path.name in runtime_files:
                offenders.append(str(relative))
        self.assertEqual(offenders, [])

    def test_plan_template_is_a_real_discoverable_file(self) -> None:
        template = SKILL_ROOT / "references" / "plan-template.md"
        self.assertTrue(template.is_file())
        self.assertFalse(template.is_symlink())
        self.assertEqual(template.resolve().parent, (SKILL_ROOT / "references").resolve())
        skill_text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("references/plan-template.md", skill_text)
        self.assertGreater(len(template.read_text(encoding="utf-8").strip()), 200)


if __name__ == "__main__":
    unittest.main()
