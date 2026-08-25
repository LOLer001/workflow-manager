from __future__ import annotations

import json
import re
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PLUGIN_ROOT.parents[1]
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
        self.work_routing = (self.skill_dir / "references" / "work-routing.md").read_text(
            encoding="utf-8"
        )
        self.confirmed_execution = (
            self.skill_dir / "references" / "confirmed-execution.md"
        ).read_text(encoding="utf-8")

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
        self.assertIn("authorization and evidence layer", self.skill_text)
        self.assertIn("Parent review must bind structured host evidence", self.skill_text)
        self.assertIn("Hard-work authorization", self.manifest["description"])
        prompts = " ".join(self.manifest["interface"]["defaultPrompt"])
        self.assertIn("runtime truth", prompts)
        self.assertIn("acceptance evidence and compaction continuity", self.agent_text)

    def test_default_prompt_is_a_thin_layer_not_generic_model_guidance(self) -> None:
        prompts = " ".join(self.manifest["interface"]["defaultPrompt"])
        self.assertIn("Hard authorization", prompts)
        self.assertIn("Leave ordinary planning", prompts)
        self.assertNotIn("expected wall-clock time", prompts)
        self.assertNotIn("proactively delegating", self.agent_text)
        self.assertNotIn("Caps are live", self.skill_text)
        self.assertNotIn("At 70%", self.skill_text)

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

    def test_plugin_upgrade_preserves_protocol_continuity_without_generic_restatement(self) -> None:
        self.assertIn("Native summaries own ordinary compaction continuity", self.skill_text)
        self.assertIn("Schema 27/writer 1.0.46", self.confirmed_execution)
        self.assertIn("preserve its real profile/contract", self.confirmed_execution)
        self.assertNotIn("progress update every", self.skill_text.lower())
        self.assertFalse((self.skill_dir / "references" / "agent-lifecycle.md").exists())
        self.assertFalse((self.skill_dir / "references" / "live-coordination.md").exists())

    def test_only_unversioned_user_skill_is_discoverable(self) -> None:
        self.assertFalse((PLUGIN_ROOT / "skills").exists())
        self.assertTrue((PLUGIN_ROOT / "scripts" / "install_stable_skill.py").is_file())
        self.assertIn(
            "sync_stable_skill",
            (PLUGIN_ROOT / "scripts" / "orchestrator_hook.py").read_text(encoding="utf-8"),
        )

    def test_canonical_hard_plan_contract_is_documented(self) -> None:
        self.assertIn("private canonical journal", self.skill_text)
        self.assertIn("plans/<session-token>/hard-plan.md", self.work_routing)
        self.assertIn("Before `plan_state` may become `awaiting_confirmation`", self.work_routing)
        self.assertIn("current trusted revision is the plan-content authority", self.work_routing)
        self.assertIn(
            "projection_only canonical_revision_digest=<digest>", self.work_routing
        )
        for value in ("983040", "10485760", "revision_too_large", "journal_full"):
            self.assertIn(value, self.confirmed_execution)
        self.assertIn("marker → journal → state → cleanup", self.confirmed_execution)
        self.assertIn("old journal/old state or new journal/new state", self.confirmed_execution)
        self.assertIn("Schema 27/writer 1.0.46", self.confirmed_execution)
        self.assertIn("execution profile v10", self.confirmed_execution)
        self.assertIn("workflow-manager-execution-slices", self.confirmed_execution)
        self.assertIn("1..3", self.confirmed_execution)
        self.assertIn("hard upper bound is 6", self.confirmed_execution)
        self.assertIn("highest-available assessor at `max`", self.skill_text)
        self.assertIn("non-Hard engineering work use native Codex directly", self.skill_text)
        self.assertIn("PreTool records the request", self.skill_text)
        self.assertIn("PostTool records host acceptance", self.skill_text)
        self.assertIn("Start must fully observe official model", self.skill_text)
        self.assertIn("same-turn host effort", self.skill_text)
        self.assertIn("reasoning_effort=max", self.work_routing)
        self.assertIn("host-generated", self.confirmed_execution)
        self.assertIn("slice_id=sNN", self.confirmed_execution)
        result_contract = re.search(
            r"`EXECUTION_RESULT execution_contract_id=<32hex>[^`]+`",
            self.confirmed_execution,
        )
        review_contract = re.search(
            r"`EXECUTION_REVIEW execution_contract_id=<32hex>[^`]+`",
            self.confirmed_execution,
        )
        self.assertIsNotNone(result_contract)
        self.assertIsNotNone(review_contract)
        self.assertNotIn("evidence_digest", result_contract.group(0))
        self.assertNotIn("evidence_digest", review_contract.group(0))
        self.assertIn("plugin-data-root", self.confirmed_execution)
        self.assertIn("privately inject that exact body", self.confirmed_execution)
        self.assertIn("_<failure_kind>_v2", self.confirmed_execution)
        self.assertIn("verification_required", self.confirmed_execution)
        self.assertIn("EXECUTION_REVIEW", self.confirmed_execution)
        self.assertIn("at most six", self.confirmed_execution)
        self.assertIn("journal alone never grants authority", self.confirmed_execution)

    def test_release_docs_target_current_version_without_fixed_suite_counts(self) -> None:
        readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
        changelog = (REPOSITORY_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        contributing = (REPOSITORY_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        self.assertEqual(self.manifest["version"], "1.0.46")
        self.assertIn("/1.0.46/", readme)
        self.assertNotIn("/1.0.42/", readme)
        self.assertNotIn("/1.0.41/", readme)
        self.assertNotIn("/1.0.37/", readme)
        self.assertRegex(changelog, r"\A# 更新记录\n\n## 1\.0\.46\n")
        self.assertNotRegex(readme + contributing, r"\b30\s*项计划")


if __name__ == "__main__":
    unittest.main()
