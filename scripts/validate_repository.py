#!/usr/bin/env python3
"""Validate the GitHub marketplace and bundled Workflow Manager identity."""

from __future__ import annotations

import sys

# Validation imports release tooling. It must not leave bytecode that can be
# mistaken for an installed or source release surface.
sys.dont_write_bytecode = True

import base64
import importlib.util
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_NAME = "workflow-manager"
PLUGIN = ROOT / "plugins" / PLUGIN_NAME
EXPECTED_VERSION_MATRIX = {
    "1.0.47": {
        "schema": 27,
        "execution_profile": "10",
        "stable_skill_schema": 8,
    }
}


def read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_command_generator():
    path = PLUGIN / "scripts" / "generate_hook_commands.py"
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
    release_version = manifest.get("version")
    assert isinstance(release_version, str) and re.fullmatch(
        r"[0-9]+\.[0-9]+\.[0-9]+", release_version
    )
    orchestrator_source = (
        PLUGIN / "scripts" / "orchestrator_hook.py"
    ).read_text(encoding="utf-8")
    writer_version = re.search(
        r'^WRITER_VERSION\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"\s*$',
        orchestrator_source,
        re.MULTILINE,
    )
    assert writer_version and writer_version.group(1) == release_version
    matrix = EXPECTED_VERSION_MATRIX.get(release_version)
    assert matrix is not None, f"release version missing from matrix: {release_version}"
    schema_version = re.search(
        r"^SCHEMA_VERSION\s*=\s*([0-9]+)\s*$", orchestrator_source, re.MULTILINE
    )
    execution_profile = re.search(
        r'^EXECUTION_PROFILE_VERSION\s*=\s*"([0-9]+)"\s*$',
        orchestrator_source,
        re.MULTILINE,
    )
    stable_skill_schema = re.search(
        r"^STABLE_SKILL_SCHEMA\s*=\s*([0-9]+)\s*$",
        orchestrator_source,
        re.MULTILINE,
    )
    assert schema_version and int(schema_version.group(1)) == matrix["schema"]
    assert execution_profile and execution_profile.group(1) == matrix["execution_profile"]
    assert stable_skill_schema and int(stable_skill_schema.group(1)) == matrix["stable_skill_schema"]
    prompts = manifest.get("interface", {}).get("defaultPrompt")
    assert isinstance(prompts, list) and 1 <= len(prompts) <= 3
    assert all(isinstance(prompt, str) and len(prompt) <= 128 for prompt in prompts)
    stable_asset = PLUGIN / "assets" / "stable-skill" / PLUGIN_NAME / "SKILL.md"
    assert stable_asset.is_file()
    confirmed_execution = (
        PLUGIN
        / "assets"
        / "stable-skill"
        / PLUGIN_NAME
        / "references"
        / "confirmed-execution.md"
    ).read_text(encoding="utf-8")
    assert f"Schema {matrix['schema']}/writer {release_version}" in confirmed_execution
    assert f"execution profile v{matrix['execution_profile']}" in confirmed_execution
    result_marker = re.search(r"`EXECUTION_RESULT [^`]+`", confirmed_execution)
    review_marker = re.search(r"`EXECUTION_REVIEW [^`]+`", confirmed_execution)
    assert result_marker and "evidence_digest" not in result_marker.group(0)
    assert review_marker and "evidence_digest" not in review_marker.group(0)
    assert "workflow-manager-execution-slices" in confirmed_execution
    assert not (PLUGIN / "skills").exists()
    installer_path = PLUGIN / "scripts" / "install_stable_skill.py"
    assert installer_path.is_file()
    installer_source = installer_path.read_text(encoding="utf-8")
    assert installer_source.index("sys.dont_write_bytecode = True") < installer_source.index(
        "import importlib.util"
    )
    posix_runner = (PLUGIN / "scripts" / "run_orchestrator_hook.sh").read_text(
        encoding="utf-8"
    )
    windows_runner = (PLUGIN / "scripts" / "run_orchestrator_hook.ps1").read_text(
        encoding="utf-8"
    )
    assert "PYTHONDONTWRITEBYTECODE=1" in posix_runner and "python3 -B" in posix_runner
    assert '$env:PYTHONDONTWRITEBYTECODE = "1"' in windows_runner
    assert "-3 -B $hookScript" in windows_runner and "-B $hookScript" in windows_runner
    doctor_path = PLUGIN / "scripts" / "hook_trust_doctor.py"
    assert doctor_path.is_file()
    doctor_source = doctor_path.read_text(encoding="utf-8")
    doctor_source_lower = doctor_source.lower()
    assert '"hooks/list"' in doctor_source
    for forbidden in ("config/" + "batchwrite", "by" + "pass"):
        assert forbidden not in doctor_source_lower
    production_sources = [
        path
        for path in PLUGIN.rglob("*")
        if path.is_file()
        and "tests" not in path.parts
        and path.suffix.lower() in {".json", ".ps1", ".py", ".sh"}
    ]
    assert all(
        "trusted_" + "hash" not in path.read_text(
            encoding="utf-8", errors="replace"
        ).lower()
        for path in production_sources
    )

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert fr"\{release_version}\scripts\install_stable_skill.py" in readme
    assert f"workflow-manager/{release_version}/scripts/install_stable_skill.py" in readme
    assert f"--ref v{release_version} --json" in readme
    latest_changelog = re.search(r"(?m)^## ([0-9]+\.[0-9]+\.[0-9]+)\s*$", changelog)
    assert latest_changelog and latest_changelog.group(1) == release_version
    absolute_hash = re.compile(r"(?i)\b(?:sha256:)?[0-9a-f]{64}\b")
    assert absolute_hash.search(readme) is None
    assert absolute_hash.search(changelog) is None
    release_workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    assert 'tags:' in release_workflow and 'workflow_dispatch:' in release_workflow
    assert 'gh release create "$tag"' in release_workflow
    assert '--verify-tag' in release_workflow
    assert 'v1.0.46' in release_workflow and 'forbidden' in release_workflow
    assert (ROOT / "scripts" / "extract_release_notes.py").is_file()
    generator = load_command_generator()
    assert (PLUGIN / "scripts" / "generate_hook_commands.py").is_file()
    assert not (ROOT / "scripts" / "generate_hook_commands.py").exists()
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
