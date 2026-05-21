#!/usr/bin/env python3
"""Apply installer-provided DAClaw defaults on first run.

This script intentionally treats installer defaults as seed data:

- A missing config.yaml is created from packaging/defaults/config.yaml.
- An existing config.yaml only receives missing/empty keys.
- .env values are appended only when the user has not already set that key.

Secrets must not be committed to the repository. Release builders may generate
packaging/defaults/.env inside the private build payload.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - this runs after pip install
    raise SystemExit(f"PyYAML is required before applying installer defaults: {exc}")

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from daclaw_constants import get_daclaw_home


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data if isinstance(data, dict) else {}


def _merge_missing(target: dict[str, Any], defaults: dict[str, Any]) -> bool:
    changed = False
    for key, value in defaults.items():
        if key not in target or target[key] in (None, ""):
            target[key] = value
            changed = True
            continue
        if isinstance(target.get(key), dict) and isinstance(value, dict):
            changed = _merge_missing(target[key], value) or changed
    return changed


def _env_keys(path: Path) -> set[str]:
    keys: set[str] = set()
    if not path.exists():
        return keys
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _sep, _value = line.partition("=")
        key = key.strip()
        if key:
            keys.add(key)
    return keys


def _append_missing_env(target: Path, defaults: Path) -> bool:
    if not defaults.exists():
        return False

    existing = _env_keys(target)
    lines_to_append: list[str] = []
    for raw_line in defaults.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _sep, _value = line.partition("=")
        key = key.strip()
        if key and key not in existing:
            lines_to_append.append(raw_line)
            existing.add(key)

    if not lines_to_append:
        return False

    prefix = ""
    if target.exists() and target.read_text(encoding="utf-8").strip():
        prefix = "\n"
    with target.open("a", encoding="utf-8") as f:
        f.write(prefix)
        f.write("# Added by DAClaw installer defaults\n")
        for line in lines_to_append:
            f.write(line.rstrip() + "\n")
    return True


def _chmod_private(path: Path) -> None:
    if os.name == "nt" or not path.exists():
        return
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def main() -> int:
    defaults_dir_raw = os.environ.get("DACLAW_INSTALLER_DEFAULTS_DIR", "").strip()
    if defaults_dir_raw:
        defaults_dir = Path(defaults_dir_raw).expanduser()
    else:
        defaults_dir = REPO_ROOT / "packaging" / "defaults"

    config_default = defaults_dir / "config.yaml"
    env_default = defaults_dir / ".env"
    if not config_default.exists() and not env_default.exists():
        return 0

    daclaw_home = get_daclaw_home()
    daclaw_home.mkdir(parents=True, exist_ok=True)

    config_path = daclaw_home / "config.yaml"
    default_config = _load_yaml(config_default)
    if default_config:
        current_config = _load_yaml(config_path)
        if _merge_missing(current_config, default_config) or not config_path.exists():
            with config_path.open("w", encoding="utf-8") as f:
                yaml.safe_dump(current_config, f, sort_keys=False, allow_unicode=True)
            _chmod_private(config_path)

    env_path = daclaw_home / ".env"
    if _append_missing_env(env_path, env_default):
        _chmod_private(env_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
