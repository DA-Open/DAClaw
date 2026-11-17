"""Capability packs (spec 2, 7): what a kind of task needs and how it is checked.

A pack is a YAML file next to this module, e.g. ``document_draft.yaml``::

    id: document_draft
    version: "1"
    instructions_ref: prompts/document_draft.md
    tools: [files.read, artifacts.write, artifacts.read]
    result_schema: schemas/document_result.json
    result_artifact: draft
    verifiers: [artifact_readable, required_sections]
    default_mode: draft_only

Rules from spec 7:

* A task fixes its pack by ``capability_ref`` (``document_draft@1``) when it
  is created; ``resolve`` returns exactly that version and never a newer one.
* ``tools`` is what the pack needs, not a grant. The tools a task may call
  are this list intersected with the role and user whitelists
  (``WhitelistPolicy.capabilities`` gets ``tool_map()``).
* ``verifiers`` are the checkers this pack offers; the task's acceptance
  picks which ones run (``results/verifier.py``).

Adding a scenario means adding a pack (and connectors if it needs new tools);
the scheduler, state machine and delivery do not change.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from ..contracts import NotFound, split_versioned_ref

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
_ID = re.compile(r"^[a-z][a-z0-9_.-]*$")
_KEYS = {
    "id",
    "version",
    "description",
    "instructions_ref",
    "tools",
    "result_schema",
    "result_artifact",
    "verifiers",
    "default_mode",
}
_REQUIRED = {"id", "version", "tools", "verifiers"}


class CapabilityError(ValueError):
    """A pack file is malformed."""


@dataclass(frozen=True)
class CapabilityPack:
    id: str
    version: str
    tools: frozenset[str]
    verifiers: tuple[str, ...]
    # Path of the instructions relative to the harness package (posix), so
    # the same InstructionLoader serves profiles and packs.
    instructions_ref: str | None = None
    result_schema: dict[str, Any] | None = None
    # Name of the artifact that is the deliverable (e.g. "draft"); checkers
    # that look at "the document" default to it.
    result_artifact: str | None = None
    default_mode: str = "draft_only"
    description: str = ""
    source: str = field(default="", compare=False)

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"


def _relative_to_package(path: Path) -> str:
    resolved = path.resolve()
    try:
        return PurePosixPath(resolved.relative_to(PACKAGE_ROOT)).as_posix()
    except ValueError:
        raise CapabilityError(f"{path} is outside the harness package") from None


def load_pack(path: Path) -> CapabilityPack:
    """Read and check one pack file. Paths inside it are relative to the file."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CapabilityError(f"{path.name}: invalid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise CapabilityError(f"{path.name}: expected a mapping at the top level")
    unknown = set(data) - _KEYS
    if unknown:
        raise CapabilityError(f"{path.name}: unknown keys {sorted(unknown)}")
    missing = _REQUIRED - set(data)
    if missing:
        raise CapabilityError(f"{path.name}: missing keys {sorted(missing)}")

    pack_id = str(data["id"])
    version = str(data["version"])
    if not _ID.match(pack_id):
        raise CapabilityError(f"{path.name}: id {pack_id!r} must match {_ID.pattern}")
    split_versioned_ref(f"{pack_id}@{version}")
    tools = data["tools"]
    verifiers = data["verifiers"]
    if not isinstance(tools, list) or not all(isinstance(t, str) and t for t in tools):
        raise CapabilityError(f"{path.name}: tools must be a list of tool names")
    if not isinstance(verifiers, list) or not all(isinstance(v, str) and v for v in verifiers):
        raise CapabilityError(f"{path.name}: verifiers must be a list of checker names")

    base = path.parent
    instructions_ref = None
    if data.get("instructions_ref"):
        target = base / str(data["instructions_ref"])
        if not target.is_file():
            raise CapabilityError(f"{path.name}: instructions file {data['instructions_ref']} not found")
        instructions_ref = _relative_to_package(target)

    schema = None
    if data.get("result_schema"):
        target = base / str(data["result_schema"])
        try:
            schema = json.loads(target.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise CapabilityError(f"{path.name}: result schema {data['result_schema']} not found") from None
        except json.JSONDecodeError as exc:
            raise CapabilityError(f"{path.name}: result schema is not valid JSON: {exc}") from None
        import jsonschema

        try:
            jsonschema.Draft202012Validator.check_schema(schema)
        except jsonschema.SchemaError as exc:
            raise CapabilityError(f"{path.name}: result schema is not a valid JSON Schema: {exc.message}") from None

    result_artifact = data.get("result_artifact")
    if result_artifact is not None and (not isinstance(result_artifact, str) or not result_artifact):
        raise CapabilityError(f"{path.name}: result_artifact must be an artifact name")

    return CapabilityPack(
        id=pack_id,
        version=version,
        tools=frozenset(tools),
        verifiers=tuple(verifiers),
        instructions_ref=instructions_ref,
        result_schema=schema,
        result_artifact=result_artifact,
        default_mode=str(data.get("default_mode") or "draft_only"),
        description=str(data.get("description") or ""),
        source=path.name,
    )


class CapabilityRegistry:
    """Packs by exact ``id@version``."""

    def __init__(self, packs: Iterable[CapabilityPack] = ()) -> None:
        self._packs: dict[str, CapabilityPack] = {}
        for pack in packs:
            self.register(pack)

    def register(self, pack: CapabilityPack) -> None:
        if pack.ref in self._packs:
            raise CapabilityError(f"capability {pack.ref} is registered twice")
        self._packs[pack.ref] = pack

    def resolve(self, ref: str) -> CapabilityPack:
        split_versioned_ref(ref)
        try:
            return self._packs[ref]
        except KeyError:
            raise NotFound(f"capability {ref} is not registered") from None

    def packs(self) -> list[CapabilityPack]:
        return list(self._packs.values())

    def tool_map(self) -> dict[str, frozenset[str]]:
        """For ``WhitelistPolicy(capabilities=...)``."""
        return {ref: pack.tools for ref, pack in self._packs.items()}

    @classmethod
    def from_directory(cls, directory: Path) -> CapabilityRegistry:
        return cls(load_pack(p) for p in sorted(Path(directory).glob("*.yaml")))

    @classmethod
    def builtin(cls) -> CapabilityRegistry:
        """The packs shipped with the harness (this directory)."""
        return cls.from_directory(Path(__file__).resolve().parent)
