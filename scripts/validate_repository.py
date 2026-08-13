#!/usr/bin/env python3
"""Validate the GitHub marketplace and bundled Workflow Manager identity."""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_NAME = "workflow-manager"
PLUGIN = ROOT / "plugins" / PLUGIN_NAME


def read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_command_generator():
    path = ROOT / "scripts" / "generate_hook_commands.py"
    spec = importlib.util.spec_from_file_location("workflow_manager_hook_commands", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def main() -> int:
    marketplace = read_json(ROOT / ".agents" / "plugins" / "marketplace.json")
    manifest = read_json(PLUGIN / ".codex-plugin" / "plugin.json")
    hooks_path = PLUGIN / "hooks" / "hooks.json"
    hooks_document = read_json(hooks_path)
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
    assert "skills" not in manifest
    prompts = manifest.get("interface", {}).get("defaultPrompt")
    assert isinstance(prompts, list) and 1 <= len(prompts) <= 3
    assert all(isinstance(prompt, str) and len(prompt) <= 128 for prompt in prompts)
    stable_asset = PLUGIN / "assets" / "stable-skill" / PLUGIN_NAME / "SKILL.md"
    assert stable_asset.is_file()
    assert not (PLUGIN / "skills").exists()
    assert (PLUGIN / "scripts" / "install_stable_skill.py").is_file()
    release_workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    assert 'tags:' in release_workflow and 'workflow_dispatch:' in release_workflow
    assert 'gh release create "$tag"' in release_workflow
    assert '--verify-tag' in release_workflow
    assert (ROOT / "scripts" / "extract_release_notes.py").is_file()
    generator = load_command_generator()
    assert (ROOT / "scripts" / "generate_hook_commands.py").is_file()
    declared = generator.command_hooks(hooks_document)
    expected_posix, expected_windows = generator.expected_commands()
    posix_commands = {hook["command"] for hook in declared}
    windows_commands = {hook["commandWindows"] for hook in declared}
    assert len(declared) == 9
    assert posix_commands == {expected_posix}
    assert windows_commands == {expected_windows}
    assert generator.canonical_text(generator.generated_document(hooks_document)) == hooks_path.read_text(
        encoding="utf-8"
    )
    windows_command = windows_commands.pop()
    assert len(windows_command) < 8191
    assert windows_command.isascii()
    assert " -EncodedCommand " in windows_command
    assert "if defined TOKEN_FRUGAL_DEBUG" in windows_command
    assert windows_command.endswith(' 2>NUL)"')
    encoded = windows_command.split(" -EncodedCommand ", 1)[1].split(" ", 1)[0]
    resolver = base64.b64decode(encoded).decode("utf-16le")
    expected_resolver = (
        PLUGIN / "scripts" / "resolve_orchestrator_hook.ps1"
    ).read_text(encoding="utf-8").replace("\r\n", "\n")
    assert resolver == expected_resolver
    for forbidden in (
        "EnumerateDirectories",
        "GetLastWriteTime",
        "selectedRoot",
        "Split-Path -Parent",
    ):
        assert forbidden not in resolver
    assert "runner_missing" in resolver
    assert "dirname" not in expected_posix
    assert "candidate" not in expected_posix

    generated = [
        path
        for path in ROOT.rglob("*")
        if path.name == "__pycache__"
        or (path.is_file() and path.suffix in {".pyc", ".orig", ".rej"})
    ]
    assert not generated, f"generated files present: {generated}"

    print(
        f"repository valid: marketplace={marketplace['name']} "
        f"plugin={manifest['name']} version={manifest['version']} "
        "skills=stable-user-path"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
