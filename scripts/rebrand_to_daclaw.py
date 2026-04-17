#!/usr/bin/env python3
"""
Safe repo rebranding helper: DAClaw -> DAClaw.

This script intentionally focuses on user-facing text and command names.
It avoids touching internal Python module identifiers such as:
- hermes_cli
- hermes_constants
- HERMES_HOME
- ~/.hermes paths
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

SKIP_DIR_NAMES = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    "node_modules",
    "dist",
    "build",
    ".mypy_cache",
    ".ruff_cache",
}

TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".txt",
    ".toml",
    ".json",
    ".yaml",
    ".yml",
    ".ini",
    ".cfg",
    ".sh",
    ".cmd",
    ".ps1",
    ".in",
    ".rst",
}

ALLOWED_BASENAMES = {
    "MANIFEST.in",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
}


def _is_text_candidate(path: Path) -> bool:
    if path.name in ALLOWED_BASENAMES:
        return True
    return path.suffix.lower() in TEXT_SUFFIXES


def _should_skip(path: Path) -> bool:
    return any(part in SKIP_DIR_NAMES for part in path.parts)


def transform_text(text: str) -> str:
    original = text

    # High-signal phrase replacements first.
    phrase_rules = [
        ("DAClaw Agent", "DAClaw Agent"),
        ("DAClaw CLI", "DAClaw CLI"),
        ("DAClaw-Agent", "DAClaw-Agent"),
        ("daclaw-agent", "daclaw-agent"),
        ("DAClaw", "DAClaw"),
        ("DAClaw", "DAClaw"),
    ]
    for old, new in phrase_rules:
        text = text.replace(old, new)

    # Command replacement: "daclaw xxx" -> "daclaw xxx"
    # Avoid module identifiers like "hermes_cli" and paths like "~/.hermes".
    text = re.sub(r"(?<![\w./-])hermes(?=\s)", "daclaw", text)

    # Replace quoted single-token command forms: `daclaw` / "daclaw" / 'daclaw'
    text = re.sub(r"(?<![\w./-])`daclaw`", "`daclaw`", text)
    text = re.sub(r'(?<![\w./-])"daclaw"', '"daclaw"', text)
    text = re.sub(r"(?<![\w./-])'daclaw'", "'daclaw'", text)

    return text if text != original else original


def process_repo(apply: bool) -> tuple[int, int]:
    scanned = 0
    changed = 0

    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if _should_skip(path):
            continue
        if not _is_text_candidate(path):
            continue

        scanned += 1

        try:
            src = path.read_text(encoding="utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            try:
                src = path.read_text(encoding="utf-8-sig")
                encoding = "utf-8-sig"
            except UnicodeDecodeError:
                continue

        dst = transform_text(src)
        if dst == src:
            continue

        changed += 1
        rel = path.relative_to(ROOT)
        print(f"[change] {rel}")
        if apply:
            # Normalize to utf-8 without BOM for consistency.
            path.write_text(dst, encoding="utf-8")
        else:
            # Keep behavior explicit in dry-run mode.
            _ = encoding

    return scanned, changed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Safely replace DAClaw branding with DAClaw branding across text files."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write changes. Without this flag, runs in dry-run mode.",
    )
    args = parser.parse_args()

    scanned, changed = process_repo(apply=args.apply)
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] scanned={scanned} changed={changed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

