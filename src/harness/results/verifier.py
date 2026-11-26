"""Verifier: decides whether a candidate result is done (spec 4.1 step 7, 6.3).

The model saying "done" only produces a ``CandidateResult``. The Verifier runs
the checks fixed in the task's acceptance at creation time and returns
``passed``, ``failed`` or ``inconclusive``:

* ``passed`` only when every check passed; then ``result_ref`` is the
  deliverable artifact and the task may become ``succeeded``.
* ``failed`` when any check failed (the result is wrong or incomplete).
* ``inconclusive`` when nothing failed but something could not be decided
  (an unknown checker, a check that cannot run). It is never treated as
  passed: the Runner turns it into ``waiting`` for the user (spec 6.3).

The Verifier runs the task's acceptance ``checks`` (each with its parameters)
and any missing ``required_checks`` from the capability pack. The task's
``required_artifacts`` and the pack's ``result_schema`` also apply. A checker named in
the acceptance but not offered by the pack is reported as unverified rather
than run, so a request cannot pick checkers the pack never promised.

Every verification is stored through ``VerificationRepository`` and a
``verification_recorded`` event is written, so the commit step can confirm
the passed verification belongs to the same ``result_ref``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from ..capabilities.registry import CapabilityPack, CapabilityRegistry
from ..contracts import (
    Action,
    Actor,
    CandidateResult,
    CheckResult,
    CheckSpec,
    Event,
    EventType,
    HarnessError,
    NotFound,
    Task,
    VerificationResult,
    VerificationStatus,
    utcnow,
)
from ..interfaces import ArtifactStore
from ..storage.repositories import Repositories
from .checkers import query_evidence

CHECKER_VERSION = "verifier@1"
PASSED, FAILED, INCONCLUSIVE = VerificationStatus.PASSED, VerificationStatus.FAILED, VerificationStatus.INCONCLUSIVE
# Text types the document checkers can read.
TEXT_TYPES = ("text/", "application/json", "application/markdown")
MAX_CHECK_BYTES = 5 * 1024 * 1024


@dataclass
class CheckContext:
    """What one checker may look at."""

    task: Task
    candidate: CandidateResult
    pack: CapabilityPack
    actor: Actor
    artifacts: ArtifactStore
    actions: Sequence[Action] = ()

    def target_names(self, spec: CheckSpec) -> list[str]:
        """Artifacts a document check applies to: ``artifact`` / ``artifacts``
        in the check, else the pack's deliverable, else all named outputs."""
        params = spec.model_dump(exclude={"id"})
        if isinstance(params.get("artifacts"), list):
            return [str(a) for a in params["artifacts"]]
        if params.get("artifact"):
            return [str(params["artifact"])]
        if self.pack.result_artifact:
            return [self.pack.result_artifact]
        return list(self.candidate.artifact_refs)

    async def read_text(self, name: str) -> tuple[str | None, str]:
        """(text, problem). ``problem`` says why there is no text."""
        ref = self.candidate.artifact_refs.get(name)
        if not ref:
            return None, f"no artifact named {name!r} in the result"
        try:
            content = await self.artifacts.read(ref, self.actor)
        except NotFound:
            return None, f"artifact {name!r} ({ref}) cannot be read"
        except HarnessError as exc:
            return None, f"artifact {name!r} ({ref}) cannot be read: {exc.message}"
        if content.ref.size_bytes > MAX_CHECK_BYTES:
            return None, f"artifact {name!r} is {content.ref.size_bytes} bytes, over the {MAX_CHECK_BYTES} byte check limit"
        if not content.ref.media_type.startswith(TEXT_TYPES):
            return None, f"artifact {name!r} is {content.ref.media_type}, not text"
        try:
            return content.content.decode("utf-8"), ""
        except UnicodeDecodeError:
            return None, f"artifact {name!r} is not valid UTF-8 text"


class Checker(Protocol):
    async def __call__(self, spec: CheckSpec, ctx: CheckContext) -> CheckResult: ...


def _result(spec_id: str, status: VerificationStatus, detail: str = "", evidence: list[str] | None = None) -> CheckResult:
    return CheckResult(id=spec_id, status=status, detail=detail, evidence_refs=evidence or [])


# -- checkers -------------------------------------------------------------------


async def artifact_readable(spec: CheckSpec, ctx: CheckContext) -> CheckResult:
    """Each target artifact exists in this scope, is text, and is not empty."""
    names = ctx.target_names(spec)
    if not names:
        return _result(spec.id, FAILED, "the result names no artifacts")
    problems, evidence = [], []
    for name in names:
        text, problem = await ctx.read_text(name)
        if text is None:
            problems.append(problem)
        elif not text.strip():
            problems.append(f"artifact {name!r} is empty")
        else:
            evidence.append(ctx.candidate.artifact_refs[name])
    if problems:
        return _result(spec.id, FAILED, "; ".join(problems), evidence)
    return _result(spec.id, PASSED, f"readable: {', '.join(names)}", evidence)


_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_NUMBERING = re.compile(r"^(?:\d+(?:\.\d+)*[.、)]?|[一二三四五六七八九十]+[、.．])\s*")


def _norm(title: str) -> str:
    title = _NUMBERING.sub("", title.strip())
    title = re.sub(r"[*_`]", "", title)
    return re.sub(r"\s+", "", title).casefold()


def markdown_headings(text: str) -> list[str]:
    """ATX headings outside fenced code blocks."""
    out, fenced = [], False
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced:
            continue
        m = _HEADING.match(line)
        if m:
            out.append(m.group(2))
    return out


async def required_sections(spec: CheckSpec, ctx: CheckContext) -> CheckResult:
    """Every section named in ``sections`` is a Markdown heading of the target.

    Matching ignores leading numbering ("1.", "一、"), Markdown emphasis,
    whitespace and case, so "## 1. 目标" satisfies "目标". A heading must be
    the section name, not merely contain it.
    """
    params = spec.model_dump(exclude={"id"})
    sections = params.get("sections")
    if not isinstance(sections, list) or not sections or not all(isinstance(s, str) and s.strip() for s in sections):
        return _result(spec.id, INCONCLUSIVE, "check has no 'sections' list to look for")
    names = ctx.target_names(spec)
    if not names:
        return _result(spec.id, FAILED, "the result names no artifacts")
    missing_by_doc, evidence = [], []
    for name in names:
        text, problem = await ctx.read_text(name)
        if text is None:
            return _result(spec.id, FAILED, problem)
        have = {_norm(h) for h in markdown_headings(text)}
        missing = [s for s in sections if _norm(s) not in have]
        if missing:
            missing_by_doc.append(f"{name!r} is missing: {', '.join(missing)}")
        evidence.append(ctx.candidate.artifact_refs[name])
    if missing_by_doc:
        return _result(spec.id, FAILED, "; ".join(missing_by_doc), evidence)
    return _result(spec.id, PASSED, f"all {len(sections)} sections present", evidence)


DEFAULT_CHECKERS: dict[str, Checker] = {
    "artifact_readable": artifact_readable,
    "required_sections": required_sections,
    "query_evidence": query_evidence,
}


# -- built-in checks that always apply -----------------------------------------------


async def _required_artifacts(task: Task, candidate: CandidateResult, artifacts: ArtifactStore) -> CheckResult | None:
    wanted = task.acceptance.required_artifacts
    if not wanted:
        return None
    missing = [n for n in wanted if n not in candidate.artifact_refs]
    if missing:
        return _result("required_artifacts", FAILED, f"missing artifacts: {', '.join(missing)}")
    actor = Actor(actor_id=task.actor_id, scope_id=task.scope_id)
    for name in wanted:
        ref = candidate.artifact_refs[name]
        try:
            await artifacts.read(ref, actor, length=0)
        except HarnessError as exc:
            return _result("required_artifacts", FAILED, f"artifact {name!r} ({ref}) cannot be read: {exc.message}")
    return _result("required_artifacts", PASSED, f"present: {', '.join(wanted)}",
                   [candidate.artifact_refs[n] for n in wanted])


def _result_schema(pack: CapabilityPack, candidate: CandidateResult) -> CheckResult | None:
    """The result must match the pack's schema. Like any checker, a check
    that cannot run (jsonschema missing, a broken schema) is inconclusive,
    never an exception out of verify() (review of #17)."""
    if not pack.result_schema:
        return None
    try:
        import jsonschema
    except ImportError:
        return _result("result_schema", INCONCLUSIVE, "jsonschema is not installed")
    try:
        jsonschema.validate(candidate.model_dump(mode="json"), pack.result_schema)
    except jsonschema.ValidationError as exc:
        where = "/".join(str(p) for p in exc.absolute_path)
        return _result("result_schema", FAILED, f"result does not match the pack schema{' at ' + where if where else ''}: {exc.message}")
    except Exception as exc:  # noqa: BLE001 - e.g. SchemaError, a bad $ref
        return _result("result_schema", INCONCLUSIVE, f"schema check raised {type(exc).__name__}: {exc}")
    return _result("result_schema", PASSED, f"matches {pack.ref} result schema")


async def _evidence(task: Task, candidate: CandidateResult, repos: Repositories, task_actions: Sequence[Action]) -> CheckResult | None:
    """Every evidence reference the result cites must be real and reachable.

    Spec 5.2: CandidateResult carries "事实依据引用". A reference counts only if
    it is an artifact this task's scope can read, or an action of this task
    that succeeded. Checking that the cited source actually supports each
    number (review comment 7, replaying read-only actions) needs the data
    connectors and comes after P0; this check stops made-up references.
    """
    refs = list(candidate.evidence_refs) + list(candidate.required_action_receipts)
    if not refs:
        return None
    actions = {a.id: a for a in task_actions}
    receipts = {a.receipt_ref: a for a in actions.values() if a.receipt_ref}
    bad = []
    for ref in refs:
        action = actions.get(ref) or receipts.get(ref)
        if action is not None:
            if str(action.status) != "succeeded":
                bad.append(f"{ref} (action {action.status})")
            continue
        try:
            await repos.artifacts.get(task.scope_id, ref)
        except NotFound:
            bad.append(f"{ref} (not found)")
    if bad:
        return _result("evidence_refs", FAILED, "unknown or unusable evidence: " + ", ".join(bad))
    return _result("evidence_refs", PASSED, f"{len(refs)} reference(s) found", refs)


def _required_actions(task: Task, candidate: CandidateResult) -> CheckResult | None:
    """P0 packs have no outside actions; a task asking for any cannot be
    confirmed until receipts can be checked (spec 6.3: 发送或修改远端对象检查实际回执)."""
    if not task.acceptance.required_actions:
        return None
    return _result(
        "required_actions",
        INCONCLUSIVE,
        "receipts for outside actions cannot be checked yet: " + ", ".join(task.acceptance.required_actions),
    )


def _overall(checks: list[CheckResult]) -> VerificationStatus:
    statuses = {c.status for c in checks}
    if FAILED in statuses:
        return FAILED
    if INCONCLUSIVE in statuses or not checks:
        return INCONCLUSIVE
    return PASSED


class CapabilityVerifier:
    """``interfaces.Verifier`` driven by capability packs."""

    def __init__(
        self,
        repos: Repositories,
        artifacts: ArtifactStore,
        capabilities: CapabilityRegistry,
        *,
        checkers: Mapping[str, Checker] | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._repos = repos
        self._artifacts = artifacts
        self._capabilities = capabilities
        self._checkers = dict(DEFAULT_CHECKERS if checkers is None else checkers)
        self._clock = clock

    async def verify(self, result: CandidateResult, task: Task) -> VerificationResult:
        checks: list[CheckResult] = []
        unverified: list[str] = []
        try:
            pack = self._capabilities.resolve(task.capability_ref)
        except (NotFound, ValueError) as exc:
            pack = None
            unverified.append(f"capability {task.capability_ref}: {exc}")

        task_actions = (
            await self._repos.actions.list_for_task(task.id)
            if result.evidence_refs or result.required_action_receipts else []
        )
        for built_in in (
            await _required_artifacts(task, result, self._artifacts),
            _result_schema(pack, result) if pack else None,
            await _evidence(task, result, self._repos, task_actions),
            _required_actions(task, result),
        ):
            if built_in is not None:
                checks.append(built_in)

        if pack is not None:
            ctx = CheckContext(
                task=task,
                candidate=result,
                pack=pack,
                actor=Actor(actor_id=task.actor_id, scope_id=task.scope_id),
                artifacts=self._artifacts,
                actions=task_actions,
            )
            for spec in task.acceptance.checks:
                checks.append(await self._run(spec, ctx, pack, unverified))
            scheduled = {spec.id for spec in task.acceptance.checks}
            for required in pack.required_checks:
                if required not in scheduled:
                    checks.append(await self._run(CheckSpec(id=required), ctx, pack, unverified))

        # The pack's result schema only checks the shape of the claim. A task
        # whose acceptance asks for nothing (no required artifacts, no checks,
        # no actions) has nothing that can confirm it is done, so it must not
        # pass on the schema alone.
        acceptance = task.acceptance
        if not (acceptance.required_artifacts or acceptance.checks or acceptance.required_actions):
            unverified.append("the task has no acceptance conditions to check")
        status = _overall(checks)
        if unverified and status is PASSED:
            status = INCONCLUSIVE
        verification = VerificationResult(
            task_id=task.id,
            result_ref=self._result_ref(result, pack),
            checker_version=f"{CHECKER_VERSION}+{pack.ref}" if pack else CHECKER_VERSION,
            status=status,
            checks=checks,
            unverified_items=unverified,
            created_at=self._clock(),
        )
        stored = await self._repos.verifications.add(verification)
        now = self._clock()
        await self._repos.events.append(
            Event(
                scope_id=task.scope_id,
                task_id=task.id,
                attempt_id=task.current_attempt_id,
                type=EventType.VERIFICATION_RECORDED,
                payload={
                    "verification_id": stored.id,
                    "status": str(stored.status),
                    "result_ref": stored.result_ref,
                    "checks": {c.id: str(c.status) for c in stored.checks},
                    "unverified": list(stored.unverified_items),
                },
                occurred_at=now,
                received_at=now,
            )
        )
        return stored

    async def _run(self, spec: CheckSpec, ctx: CheckContext, pack: CapabilityPack, unverified: list[str]) -> CheckResult:
        if spec.id not in pack.verifiers:
            unverified.append(f"check {spec.id!r} is not offered by {pack.ref}")
            return _result(spec.id, INCONCLUSIVE, f"not offered by {pack.ref}")
        checker = self._checkers.get(spec.id)
        if checker is None:
            unverified.append(f"no checker implements {spec.id!r}")
            return _result(spec.id, INCONCLUSIVE, "no checker implements this check")
        try:
            return await checker(spec, ctx)
        except Exception as exc:  # noqa: BLE001 - a broken checker must not pass a result
            return _result(spec.id, INCONCLUSIVE, f"checker raised {type(exc).__name__}: {exc}")

    @staticmethod
    def _result_ref(candidate: CandidateResult, pack: CapabilityPack | None) -> str:
        """The deliverable: the pack's named artifact, else the first one."""
        if pack and pack.result_artifact and pack.result_artifact in candidate.artifact_refs:
            return candidate.artifact_refs[pack.result_artifact]
        return next(iter(candidate.artifact_refs.values()), "none")


__all__: list[Any] = ["CapabilityVerifier", "CheckContext", "Checker", "DEFAULT_CHECKERS", "markdown_headings"]
