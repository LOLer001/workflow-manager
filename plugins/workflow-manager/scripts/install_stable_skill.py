#!/usr/bin/env python3
"""Provision Workflow Manager into Codex's stable user Skill directory."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
HOOK_SCRIPT = SCRIPT_DIR / "orchestrator_hook.py"


def load_hook():
    spec = importlib.util.spec_from_file_location("workflow_manager_orchestrator_hook", HOOK_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load Workflow Manager hook")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install/update Workflow Manager at an unversioned user Skill path."
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        help="Codex home directory; defaults to CODEX_HOME or ~/.codex.",
    )
    parser.add_argument(
        "--plugin-root",
        type=Path,
        default=SCRIPT_DIR.parent,
        help="Workflow Manager plugin root.",
    )
    args = parser.parse_args()
    result = load_hook().sync_stable_skill(
        plugin_root=args.plugin_root,
        codex_home=args.codex_home,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") in {"installed", "updated", "current"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
