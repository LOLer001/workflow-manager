from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
SKILLS_ROOT = PLUGIN_ROOT / "assets" / "stable-skill"
EXPECTED_ID = "workflow-manager"
EXPECTED_DISPLAY = "Workflow Manager"


def frontmatter_name(text: str) -> str:
    match = re.search(r"(?m)^name:\s*([a-z0-9-]+)\s*$", text)
    if not match:
        raise AssertionError("SKILL.md has no valid name field")
    return match.group(1)


class SkillIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.skill_dirs = sorted(path.parent for path in SKILLS_ROOT.glob("*/SKILL.md"))
        self.assertEqual(len(self.skill_dirs), 1, self.skill_dirs)
        self.skill_dir = self.skill_dirs[0]
        self.skill_text = (self.skill_dir / "SKILL.md").read_text(encoding="utf-8")
        self.agent_text = (self.skill_dir / "agents" / "openai.yaml").read_text(encoding="utf-8")

    def test_plugin_and_skill_share_one_internal_identity(self) -> None:
        self.assertEqual(self.manifest["name"], EXPECTED_ID)
        self.assertNotIn("skills", self.manifest)
        self.assertEqual(self.skill_dir.name, EXPECTED_ID)
        self.assertEqual(frontmatter_name(self.skill_text), EXPECTED_ID)

    def test_plugin_and_skill_share_one_display_identity(self) -> None:
        self.assertEqual(self.manifest["interface"]["displayName"], EXPECTED_DISPLAY)
        self.assertIn(f'display_name: "{EXPECTED_DISPLAY}"', self.agent_text)
        self.assertIn(f"# {EXPECTED_DISPLAY}", self.skill_text)

    def test_only_bundled_skill_is_implicitly_invocable(self) -> None:
        self.assertIn(f"default_prompt: \"Use ${EXPECTED_ID}", self.agent_text)
        self.assertIn("allow_implicit_invocation: true", self.agent_text)
        prompts = json.dumps(self.manifest["interface"]["defaultPrompt"], ensure_ascii=False)
        self.assertIn(f"${EXPECTED_ID}", prompts)

    def test_quality_first_context_savings_contract_is_consistent(self) -> None:
        self.assertIn("Correctness and required reasoning", self.skill_text)
        self.assertIn("never the depth needed to solve the task", self.skill_text)
        self.assertIn("Quality-first, context-efficient", self.manifest["description"])
        prompts = " ".join(self.manifest["interface"]["defaultPrompt"])
        self.assertIn("Correctness and acceptance evidence outrank context savings", prompts)
        self.assertIn("preserving all reasoning, evidence, correction, and verification", self.agent_text)

    def test_delegation_policy_is_efficiency_biased_not_read_only_quota(self) -> None:
        prompts = " ".join(self.manifest["interface"]["defaultPrompt"])
        self.assertIn("expected wall-clock time", prompts)
        self.assertIn("positive-utility", self.skill_text)
        self.assertIn("Read-only investigation is only one option", self.skill_text)
        self.assertIn("Complex up to 2 subagents; Extensive up to 3", self.skill_text)
        self.assertIn("caps as ceilings, never quotas", self.skill_text)
        self.assertIn("read, write, test, research, or review", self.agent_text)

    def test_legacy_skill_identity_is_absent_from_plugin(self) -> None:
        legacy_ids = ("token-frugal-" + "orchestrator", "token-frugal-" + "workflow")
        readable_suffixes = {".json", ".md", ".ps1", ".py", ".sh", ".yaml", ".yml"}
        offenders = []
        for path in PLUGIN_ROOT.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in readable_suffixes:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if any(legacy_id in text for legacy_id in legacy_ids):
                offenders.append(str(path.relative_to(PLUGIN_ROOT)))
        self.assertEqual(offenders, [])

    def test_child_agent_purpose_naming_matches_host_constraint(self) -> None:
        self.assertIn("concise Chinese purpose summary", self.skill_text)
        self.assertIn("ASCII `task_name`", self.skill_text)

    def test_plugin_upgrade_preserves_skill_path_continuity(self) -> None:
        self.assertIn("supported host API", self.skill_text)
        self.assertIn("$CODEX_HOME/skills/workflow-manager", self.skill_text)
        self.assertIn("never edit rollout JSONL or live databases/indexes/tasks", self.skill_text)
        self.assertIn("Keep old caches until either route covers all tasks", self.skill_text)
        self.assertIn("new/resumed tasks", self.skill_text)

    def test_only_unversioned_user_skill_is_discoverable(self) -> None:
        self.assertFalse((PLUGIN_ROOT / "skills").exists())
        self.assertTrue((PLUGIN_ROOT / "scripts" / "install_stable_skill.py").is_file())
        self.assertIn(
            "sync_stable_skill",
            (PLUGIN_ROOT / "scripts" / "orchestrator_hook.py").read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
