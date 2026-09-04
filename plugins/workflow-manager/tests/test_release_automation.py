from __future__ import annotations

import importlib.util
import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "extract_release_notes.py"
SPEC = importlib.util.spec_from_file_location("extract_release_notes", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ReleaseAutomationTests(unittest.TestCase):
    def test_current_release_version_matrix_is_consistent(self) -> None:
        manifest = json.loads(
            (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        hook = (PLUGIN_ROOT / "scripts" / "orchestrator_hook.py").read_text(
            encoding="utf-8"
        )
        metadata = json.loads((PLUGIN_ROOT / "release_metadata.json").read_text(encoding="utf-8"))
        self.assertIn("RELEASE_METADATA = _release_metadata()", hook)
        constants = {
            "SCHEMA_VERSION": str(metadata["schema"]),
            "WRITER_VERSION": metadata["version"],
            "EXECUTION_PROFILE_VERSION": metadata["execution_profile"],
            "STABLE_SKILL_SCHEMA": str(metadata["stable_skill_schema"]),
        }
        self.assertEqual(
            constants,
            {
                "SCHEMA_VERSION": "34",
                "WRITER_VERSION": "1.0.70",
                "EXECUTION_PROFILE_VERSION": "13",
                "STABLE_SKILL_SCHEMA": "10",
            },
        )
        self.assertEqual(manifest["version"], constants["WRITER_VERSION"])
        doctor = (
            PLUGIN_ROOT / "scripts" / "hook_trust_doctor.py"
        ).read_text(encoding="utf-8")
        self.assertEqual(
            re.search(
                r'^DISPATCH_WRITER_VERSION\s*=\s*"([^"]+)"$',
                doctor,
                re.MULTILINE,
            ).group(1),
            constants["WRITER_VERSION"],
        )
        self.assertEqual(
            re.search(
                r"^DISPATCH_STATE_SCHEMA\s*=\s*([0-9]+)$",
                doctor,
                re.MULTILINE,
            ).group(1),
            constants["SCHEMA_VERSION"],
        )
        self.assertEqual(
            re.search(r'^DISPATCH_EXECUTION_PROFILE\s*=\s*"([^\"]+)"$', doctor, re.MULTILINE).group(1),
            constants["EXECUTION_PROFILE_VERSION"],
        )

    def test_release_launchers_disable_bytecode(self) -> None:
        installer = (PLUGIN_ROOT / "scripts" / "install_stable_skill.py").read_text(
            encoding="utf-8"
        )
        posix = (PLUGIN_ROOT / "scripts" / "run_orchestrator_hook.sh").read_text(
            encoding="utf-8"
        )
        windows = (PLUGIN_ROOT / "scripts" / "run_orchestrator_hook.ps1").read_text(
            encoding="utf-8"
        )
        validator = (ROOT / "scripts" / "validate_repository.py").read_text(
            encoding="utf-8"
        )
        self.assertLess(
            installer.index("sys.dont_write_bytecode = True"),
            installer.index("import importlib.util"),
        )
        self.assertLess(
            validator.index("sys.dont_write_bytecode = True"),
            validator.index("import importlib.util"),
        )
        self.assertIn("PYTHONDONTWRITEBYTECODE=1", posix)
        self.assertIn("python3 -B", posix)
        self.assertIn('$env:PYTHONDONTWRITEBYTECODE = "1"', windows)
        self.assertIn("-3 -B $hookScript", windows)
        self.assertIn("-B $hookScript", windows)

    def test_every_unpublished_version_has_release_notes(self) -> None:
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        for version in (
            "1.0.15",
            "1.0.16",
            "1.0.17",
            "1.0.18",
            "1.0.19",
            "1.0.20",
            "1.0.44",
            "1.0.46",
            "1.0.47",
            "1.0.49",
            "1.0.50",
            "1.0.51",
            "1.0.52",
            "1.0.53",
            "1.0.54",
            "1.0.55",
        ):
            with self.subTest(version=version):
                notes = MODULE.extract_release_notes(changelog, version)
                self.assertTrue(notes.startswith("- "))
                self.assertNotIn("## ", notes)

    def test_release_workflow_does_not_special_case_a_missing_version(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn('[[ "$tag" == "v1.0.46" ]]', workflow)
        self.assertNotIn("Release v1.0.46 is forbidden", workflow)
        self.assertIn('previous_tag="v1.0.$((patch - 1))"', workflow)
        self.assertIn("Release sequence gap:", workflow)
        self.assertIn('latest_flag="--latest=false"', workflow)
        self.assertIn('repos/$GITHUB_REPOSITORY/releases/latest', workflow)

    def test_missing_or_empty_section_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            MODULE.extract_release_notes("# log\n", "1.0.20")
        with self.assertRaises(ValueError):
            MODULE.extract_release_notes("## 1.0.20\n\n## 1.0.19\n- old\n", "1.0.20")


if __name__ == "__main__":
    unittest.main()
