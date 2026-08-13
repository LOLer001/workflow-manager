#!/usr/bin/env python3
"""Generate the nine Workflow Manager hook commands from one trusted source."""

from __future__ import annotations

import argparse
import base64
import copy
import json
import sys
from pathlib import Path


PLUGIN = Path(__file__).resolve().parents[1]
HOOKS_PATH = PLUGIN / "hooks" / "hooks.json"
WINDOWS_RESOLVER = PLUGIN / "scripts" / "resolve_orchestrator_hook.ps1"
EXPECTED_HOOK_COUNT = 9
DRIFT_MESSAGE = "workflow_manager_hooks: generated commands are out of date"

POSIX_COMMAND = (
    'root="${PLUGIN_ROOT-}"; runner="$root/scripts/run_orchestrator_hook.sh"; '
    'if [ -n "$root" ] && [ -f "$runner" ]; then sh "$runner"; '
    'elif [ "${TOKEN_FRUGAL_DEBUG-}" = "1" ]; then '
    "printf '%s\\n' 'workflow_manager_hook: runner_missing' >&2; "
    "fi; exit 0"
)


def resolver_text() -> str:
    return WINDOWS_RESOLVER.read_text(encoding="utf-8").replace("\r\n", "\n")


def expected_commands() -> tuple[str, str]:
    encoded = base64.b64encode(resolver_text().encode("utf-16le")).decode("ascii")
    powershell = (
        "powershell.exe -NoLogo -NoProfile -NonInteractive "
        f"-ExecutionPolicy Bypass -EncodedCommand {encoded}"
    )
    windows = (
        'cmd.exe /d /c "if defined TOKEN_FRUGAL_DEBUG ('
        + powershell
        + ") else ("
        + powershell
        + ' 2>NUL)"'
    )
    return POSIX_COMMAND, windows


def command_hooks(document: dict) -> list[dict]:
    hooks = document.get("hooks")
    if not isinstance(hooks, dict):
        raise ValueError("hooks document must contain an object named 'hooks'")
    declared: list[dict] = []
    for matchers in hooks.values():
        if not isinstance(matchers, list):
            raise ValueError("hook matchers must be lists")
        for matcher in matchers:
            entries = matcher.get("hooks") if isinstance(matcher, dict) else None
            if not isinstance(entries, list):
                raise ValueError("each matcher must contain a hooks list")
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("type") != "command":
                    raise ValueError("Workflow Manager declares command hooks only")
                declared.append(entry)
    if len(declared) != EXPECTED_HOOK_COUNT:
        raise ValueError(
            f"expected {EXPECTED_HOOK_COUNT} command hooks, found {len(declared)}"
        )
    return declared


def generated_document(source: dict) -> dict:
    generated = copy.deepcopy(source)
    posix, windows = expected_commands()
    for entry in command_hooks(generated):
        entry["command"] = posix
        entry["commandWindows"] = windows
    return generated


def canonical_text(document: dict) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def generated_text(path: Path = HOOKS_PATH) -> str:
    source = json.loads(path.read_text(encoding="utf-8"))
    return canonical_text(generated_document(source))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify hooks.json without modifying it",
    )
    args = parser.parse_args(argv)
    expected = generated_text()
    current = HOOKS_PATH.read_text(encoding="utf-8")
    if args.check:
        if current != expected:
            print(DRIFT_MESSAGE, file=sys.stderr)
            return 1
        return 0
    if current != expected:
        HOOKS_PATH.write_text(expected, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
