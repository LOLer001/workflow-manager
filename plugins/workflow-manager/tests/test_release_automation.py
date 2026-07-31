from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "extract_release_notes.py"
SPEC = importlib.util.spec_from_file_location("extract_release_notes", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ReleaseAutomationTests(unittest.TestCase):
    def test_every_unpublished_version_has_release_notes(self) -> None:
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        for version in ("1.0.15", "1.0.16", "1.0.17", "1.0.18", "1.0.19", "1.0.20"):
            with self.subTest(version=version):
                notes = MODULE.extract_release_notes(changelog, version)
                self.assertTrue(notes.startswith("- "))
                self.assertNotIn("## ", notes)

    def test_missing_or_empty_section_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            MODULE.extract_release_notes("# log\n", "1.0.20")
        with self.assertRaises(ValueError):
            MODULE.extract_release_notes("## 1.0.20\n\n## 1.0.19\n- old\n", "1.0.20")


if __name__ == "__main__":
    unittest.main()
