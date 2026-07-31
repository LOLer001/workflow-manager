#!/usr/bin/env python3
"""Validate the GitHub marketplace and bundled Workflow Manager identity."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_NAME = "workflow-manager"
PLUGIN = ROOT / "plugins" / PLUGIN_NAME


def read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def main() -> int:
    marketplace = read_json(ROOT / ".agents" / "plugins" / "marketplace.json")
    manifest = read_json(PLUGIN / ".codex-plugin" / "plugin.json")
    entries = marketplace.get("plugins")

    assert marketplace.get("name") == PLUGIN_NAME
    assert isinstance(entries, list) and len(entries) == 1
    assert entries[0].get("name") == PLUGIN_NAME
    assert entries[0].get("source") == {
        "source": "local",
        "path": f"./plugins/{PLUGIN_NAME}",
    }
    assert entries[0].get("policy") == {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL",
    }
    assert manifest.get("name") == PLUGIN_NAME
    assert manifest.get("interface", {}).get("displayName") == "Workflow Manager"
    prompts = manifest.get("interface", {}).get("defaultPrompt")
    assert isinstance(prompts, list) and 1 <= len(prompts) <= 3
    assert all(isinstance(prompt, str) and len(prompt) <= 128 for prompt in prompts)
    assert (PLUGIN / "skills" / PLUGIN_NAME / "SKILL.md").is_file()

    generated = [
        path
        for path in ROOT.rglob("*")
        if path.name == "__pycache__"
        or (path.is_file() and path.suffix in {".pyc", ".orig", ".rej"})
    ]
    assert not generated, f"generated files present: {generated}"

    print(
        f"repository valid: marketplace={marketplace['name']} "
        f"plugin={manifest['name']} version={manifest['version']} skills=1"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
