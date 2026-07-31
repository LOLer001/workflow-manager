#!/usr/bin/env python3
"""Extract one version section from CHANGELOG.md for a GitHub Release."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


def extract_release_notes(text: str, version: str) -> str:
    heading = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
    matches = list(heading.finditer(text))
    for index, match in enumerate(matches):
        if match.group(1) != version:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        notes = text[match.end() : end].strip()
        if not notes:
            raise ValueError(f"CHANGELOG section {version} is empty")
        return notes + "\n"
    raise ValueError(f"CHANGELOG section {version} was not found")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--changelog", type=Path, default=Path("CHANGELOG.md"))
    args = parser.parse_args()
    print(
        extract_release_notes(args.changelog.read_text(encoding="utf-8"), args.version),
        end="",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
