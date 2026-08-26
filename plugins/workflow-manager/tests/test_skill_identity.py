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
        self.work_routing = (self.skill_dir / "references" / "work-routing.md").read_text(encoding="utf-8")
        self.confirmed_execution = (self.skill_dir / "references" / "confirmed-execution.md").read_text(encoding="utf-8")
        self.assessment_liveness = (self.skill_dir / "references" / "assessment-liveness.md").read_text(encoding="utf-8")
        self.stall_recovery = (self.skill_dir / "references" / "stall-recovery.md").read_text(encoding="utf-8")
        self.regression_continuity = (self.skill_dir / "references" / "regression-continuity.md").read_text(encoding="utf-8")

    def test_plugin_and_skill_share_identity(self) -> None:
        self.assertEqual(self.manifest["name"], EXPECTED_ID)
        self.assertNotIn("skills", self.manifest)
        self.assertEqual(self.skill_dir.name, EXPECTED_ID)
        self.assertEqual(frontmatter_name(self.skill_text), EXPECTED_ID)
        self.assertEqual(self.manifest["interface"]["displayName"], EXPECTED_DISPLAY)
        self.assertIn(f'display_name: "{EXPECTED_DISPLAY}"', self.agent_text)
        self.assertIn(f"# {EXPECTED_DISPLAY}", self.skill_text)

    def test_only_bundled_skill_is_implicitly_invocable(self) -> None:
        self.assertIn(f"default_prompt: \"Use ${EXPECTED_ID}", self.agent_text)
        self.assertIn("allow_implicit_invocation: true", self.agent_text)
        prompts = json.dumps(self.manifest["interface"]["defaultPrompt"], ensure_ascii=False)
        self.assertIn(f"${EXPECTED_ID}", prompts)
        self.assertFalse((PLUGIN_ROOT / "skills").exists())
        self.assertTrue((PLUGIN_ROOT / "scripts" / "install_stable_skill.py").is_file())

    def test_skill_is_lean_and_leaves_native_judgment_to_codex(self) -> None:
        self.assertLessEqual(len(self.skill_text.encode("utf-8")), 7000)
        for phrase in (
            "narrow authorization and evidence layer",
            "Current Codex owns planning",
            "Everything else is advisory",
            "task_name` is an opaque host label",
            "No `WORK_ASSESSMENT`, JSON fence, fixed keywords, closing sentence, or minimum prose length",
            "`EXECUTION_RESULT` is optional",
            "`EXECUTION_REVIEW` is optional",
            "Start=0",
        ):
            self.assertIn(phrase, self.skill_text)
        for retired in (
            "ends with exactly",
            "standalone exact final",
            "task name binds contract/slice",
            "failed attempt-two review exhausts",
            "hard upper bound is 6",
        ):
            self.assertNotIn(retired, self.skill_text.lower())

    def test_only_irreducible_gates_are_documented(self) -> None:
        contract = "\n".join((self.skill_text, self.work_routing, self.confirmed_execution))
        for phrase in (
            "Host truth",
            "Authorization scope",
            "Mutation ownership",
            "External safety",
            "host_accepted=true",
            "one unique full Start",
            "Missing, unknown, or rejected host acceptance maps to `model_unavailable`",
            "every other request/Start/state conflict maps to `start_mismatch`",
            "Any concise safe ASCII `task_name` is valid",
            "one live writer",
            "no child nesting",
        ):
            self.assertIn(phrase, contract)

    def test_native_plan_and_confirmation_continuity_are_documented(self) -> None:
        contract = "\n".join((self.skill_text, self.work_routing, self.confirmed_execution))
        for phrase in (
            "plans/<session-token>/hard-plan.md",
            "Before `plan_state` may become `awaiting_confirmation`",
            "current trusted revision is the plan-content authority",
            "projection_only canonical_revision_digest=<digest>",
            "workflow-manager-execution-slices` block is optional",
            "one logical slice",
            "196608-byte / 1024-node",
            "no separate slice or list-count ceiling",
            "host-bound confirmation-receipt digest",
            "automatically bind",
            "plan prose, slice layout, or manifest digest",
        ):
            self.assertIn(phrase, contract)
        for value in ("983040", "10485760", "revision_too_large", "journal_full"):
            self.assertIn(value, self.confirmed_execution)
        self.assertIn("marker → journal → state → cleanup", self.confirmed_execution)

    def test_recovery_and_liveness_are_evidence_budget_driven(self) -> None:
        contract = "\n".join((self.skill_text, self.confirmed_execution, self.assessment_liveness))
        for phrase in (
            "`gpt-5.6-sol` at `max`",
            "positive, monotonic",
            "bounded state byte/node budget",
            "Three or more distinct failure fingerprints",
            "same failure fingerprint",
            "600 seconds is an activity observation",
            "exactly 1200 seconds remains live",
            "diagnose the cause, or split the step",
        ):
            self.assertIn(phrase, contract)
        self.assertIn("current highest available `gpt-5.6-sol`", self.stall_recovery)
        self.assertIn("inherits the existing strict confirmation", self.regression_continuity)

    def test_protocol_continuity_and_privacy_are_preserved(self) -> None:
        self.assertIn("Schema 28/writer 1.0.48", self.confirmed_execution)
        self.assertIn("execution profile v10", self.confirmed_execution)
        self.assertIn("canonical journal v2", self.confirmed_execution)
        self.assertIn("preserves its real profile/contract", self.confirmed_execution)
        self.assertIn("Persist only digests", self.confirmed_execution)
        self.assertIn("Native summaries own ordinary continuity", self.confirmed_execution)
        self.assertFalse((self.skill_dir / "references" / "agent-lifecycle.md").exists())
        self.assertFalse((self.skill_dir / "references" / "live-coordination.md").exists())

    def test_legacy_skill_identity_is_absent(self) -> None:
        legacy_ids = ("token-frugal-" + "orchestrator", "token-frugal-" + "workflow")
        readable = {".json", ".md", ".ps1", ".py", ".sh", ".yaml", ".yml"}
        offenders = []
        for path in PLUGIN_ROOT.rglob("*"):
            if path.is_file() and path.suffix.lower() in readable:
                text = path.read_text(encoding="utf-8", errors="replace")
                if any(identity in text for identity in legacy_ids):
                    offenders.append(str(path.relative_to(PLUGIN_ROOT)))
        self.assertEqual(offenders, [])

    def test_release_docs_target_current_version_without_fixed_suite_counts(self) -> None:
        readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
        changelog = (REPOSITORY_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        contributing = (REPOSITORY_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        self.assertEqual(self.manifest["version"], "1.0.48")
        self.assertIn("/1.0.48/", readme)
        self.assertRegex(changelog, r"\A# 更新记录\n\n## 1\.0\.48\n")
        self.assertNotRegex(readme + contributing, r"\b30\s*项计划")

    def test_ci_runs_python_without_bytecode_on_linux_and_windows(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
        self.assertIn('PYTHONDONTWRITEBYTECODE: "1"', workflow)
        self.assertIn("python -B scripts/validate_repository.py", workflow)
        self.assertIn("python -B -m unittest discover", workflow)
        self.assertIn("py -3 -B scripts/validate_repository.py", workflow)
        self.assertIn("py -3 -B -m unittest discover", workflow)


if __name__ == "__main__":
    unittest.main()
