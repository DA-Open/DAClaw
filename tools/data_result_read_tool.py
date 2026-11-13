"""data_result_read -- read a persisted tool result or analysis artifact.

Large tool outputs are spilled to disk by tools/tool_result_storage.py and the
model is told to read them back. The data-agent toolset deliberately has no
generic file tools, so it had no way to do that: the hint pointed at read_file,
which is not in the toolset, and the model either failed or guessed.

This tool is the missing counterpart. It only reads from the two places the
harness itself writes to:

* persisted tool results (<temp>/daclaw-results/*.txt)
* analysis run artifacts ($DACLAW_HOME/analysis_runs/**)

Any other path is refused, so it cannot be turned into a general file reader
through a crafted argument.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from tools.file_tools import read_file_tool
from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)

# Line-based pagination, same semantics as read_file.
DEFAULT_LIMIT = 500
MAX_LIMIT = 2000


def _candidate_roots() -> List[Path]:
    """Directories this tool is allowed to read from."""
    roots: List[Path] = [Path("/tmp/daclaw-results")]
    for env_name in ("TMPDIR", "TMP"):
        value = os.environ.get(env_name)
        if value:
            roots.append(Path(value) / "daclaw-results")
    try:
        from daclaw_constants import get_daclaw_home

        roots.append(get_daclaw_home() / "analysis_runs")
    except Exception:  # pragma: no cover - defensive
        pass
    return roots


def _analysis_runs_root() -> Optional[Path]:
    try:
        from daclaw_constants import get_daclaw_home

        return get_daclaw_home() / "analysis_runs"
    except Exception:  # pragma: no cover - defensive
        return None


def _candidate_paths(path_text: str) -> List[Path]:
    """Where a given path might be, in the order worth trying.

    Analysis artifacts hand the model full_result_path relative to the run
    directory (e.g. results/<artifact_id>.json), so a bare relative path has to
    be anchored somewhere before it can be opened.
    """
    candidate = Path(path_text).expanduser()
    if candidate.is_absolute():
        return [candidate]

    tries = [Path.cwd() / candidate]
    root = _analysis_runs_root()
    if root is not None:
        tries.append(root / candidate)
        try:
            for run_dir in sorted(root.iterdir()):
                if run_dir.is_dir():
                    tries.append(run_dir / candidate)
        except OSError:
            pass
    return tries


def _resolve_allowed(path_text: str) -> Optional[Path]:
    """Resolve path_text and return it only if it sits inside a known root."""
    if not path_text or not isinstance(path_text, str):
        return None

    roots = []
    for root in _candidate_roots():
        try:
            roots.append(root.resolve())
        except OSError:
            continue

    contained = []
    for candidate in _candidate_paths(path_text):
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        for root_resolved in roots:
            if resolved == root_resolved or root_resolved in resolved.parents:
                if resolved.is_file():
                    return resolved
                contained.append(resolved)
                break
    # Nothing existed, but the path was inside a root: report it as missing
    # rather than as a permission problem.
    return contained[0] if contained else None


def data_result_read_tool(
    path: str,
    offset: int = 1,
    limit: int = DEFAULT_LIMIT,
    task_id: str = "default",
) -> str:
    """Read a persisted result / artifact file with line pagination."""
    resolved = _resolve_allowed(path)
    if resolved is None:
        return tool_error(
            "Access denied: only persisted tool results and analysis artifacts"
            " can be read (expected a path under daclaw-results/ or analysis_runs/)",
            path=path,
        )
    if not resolved.is_file():
        return tool_error(f"No such result file: {path}", path=path)

    try:
        offset = max(1, int(offset))
        limit = max(1, min(int(limit), MAX_LIMIT))
    except (TypeError, ValueError):
        return tool_error("offset and limit must be integers", path=path)

    logger.debug("data_result_read %s offset=%s limit=%s", resolved, offset, limit)
    return read_file_tool(str(resolved), offset=offset, limit=limit, task_id=task_id)


DATA_RESULT_READ_SCHEMA: Dict[str, Any] = {
    "name": "data_result_read",
    "description": (
        "Read the full content of a persisted tool result or analysis artifact,"
        " with line pagination. Use this when a previous result was too large and"
        " was saved to a path instead of being shown in full."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "The path shown in the persisted-output message.",
            },
            "offset": {
                "type": "integer",
                "description": "First line to return (1-based, default: 1).",
                "default": 1,
            },
            "limit": {
                "type": "integer",
                "description": (
                    f"Number of lines to return (default: {DEFAULT_LIMIT},"
                    f" max: {MAX_LIMIT})."
                ),
                "default": DEFAULT_LIMIT,
            },
        },
        "required": ["path"],
    },
}


def _handle_data_result_read(args, **_kwargs) -> str:
    return data_result_read_tool(
        path=args.get("path", ""),
        offset=args.get("offset", 1),
        limit=args.get("limit", DEFAULT_LIMIT),
    )


registry.register(
    name="data_result_read",
    toolset="data",
    schema=DATA_RESULT_READ_SCHEMA,
    handler=_handle_data_result_read,
    emoji="DATA",
    max_result_size_chars=200_000,
)
