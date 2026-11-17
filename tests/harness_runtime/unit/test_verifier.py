"""Verifier, capability packs and the Main profile (results/, capabilities/, agents/)."""

import dataclasses
import json

import pytest

from harness.agents import MAIN_PROFILE, AgentRegistry, PackageInstructions, main_role_whitelist
from harness.capabilities import CapabilityError, CapabilityPack, CapabilityRegistry, load_pack
from harness.contracts import (
    Acceptance,
    AgentRole,
    ArtifactInput,
    CandidateResult,
    CheckSpec,
    EventType,
    NotFound,
    RunLane,
    TaskOutcome,
    TaskStatus,
    VerificationStatus,
)
from harness.results import CapabilityVerifier, FileArtifactStore, markdown_headings
from harness.storage.memory import InMemoryRepositories

from ..fakes import ACTOR, T0, claimed_task


P, F, I = VerificationStatus.PASSED, VerificationStatus.FAILED, VerificationStatus.INCONCLUSIVE
SECTIONS = ["目标", "方案", "计划"]
GOOD = "# 项目说明草稿\n\n## 1. 目标\n交付 P0。\n\n## 方案\n新 harness。\n\n## 三、计划\n两周。\n"


def acceptance(**kw):
    data = dict(
        required_artifacts=["draft"],
        checks=[{"id": "artifact_readable"}, {"id": "required_sections", "sections": SECTIONS}],
    )
    data.update(kw)
    return Acceptance(**data)


class World:
    def __init__(self, tmp_path, *, packs=None):
        self.repos = InMemoryRepositories()
        self.store = FileArtifactStore(tmp_path / "artifacts", self.repos.artifacts)
        self.packs = packs or CapabilityRegistry.builtin()
        self.verifier = CapabilityVerifier(self.repos, self.store, self.packs)

    async def task(self, **kw):
        self.t, self.execution = await claimed_task(self.repos, acceptance=kw.pop("acceptance", acceptance()), **kw)
        return self.t

    async def put(self, text, *, name="draft", media_type="text/markdown", scope=None):
        ref = await self.store.put(
            ArtifactInput(scope_id=scope or ACTOR.scope_id, task_id=self.t.id, name=name, media_type=media_type,
                          content=text.encode("utf-8") if isinstance(text, str) else text)
        )
        return ref.id

    async def verify(self, refs, summary="done"):
        return await self.verifier.verify(CandidateResult(summary=summary, artifact_refs=refs), self.t)

    def by_id(self, v):
        return {c.id: c for c in v.checks}


# -- the pass path -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_draft_passes(tmp_path):
    w = World(tmp_path)
    await w.task()
    ref = await w.put(GOOD)
    v = await w.verify({"draft": ref})
    assert v.status is P
    assert v.result_ref == ref
    assert set(w.by_id(v)) == {"required_artifacts", "result_schema", "artifact_readable", "required_sections"}
    assert v.checker_version == "verifier@1+document_draft@1"
    assert (await w.repos.verifications.get(v.id)).status is P


@pytest.mark.asyncio
async def test_passed_verification_lets_the_task_succeed(tmp_path):
    w = World(tmp_path)
    await w.task()
    ref = await w.put(GOOD)
    v = await w.verify({"draft": ref})
    outcome = TaskOutcome(status=TaskStatus.SUCCEEDED, result_ref=v.result_ref, verification_ref=v.id)
    assert (await w.repos.tasks.commit(w.execution.lease, outcome, T0)).status is TaskStatus.SUCCEEDED


@pytest.mark.asyncio
@pytest.mark.parametrize("artifact_state", ["missing", "other_scope", "missing_content"])
async def test_required_artifact_must_be_readable_without_explicit_checks(tmp_path, artifact_state):
    from harness.contracts import VerificationRequired

    w = World(tmp_path)
    await w.task(acceptance=acceptance(checks=[]))
    if artifact_state == "missing":
        ref = "art_does_not_exist"
    else:
        ref = await w.put(GOOD, scope="other" if artifact_state == "other_scope" else None)
        if artifact_state == "missing_content":
            artifact = await w.repos.artifacts.get(ACTOR.scope_id, ref)
            w.store._file_of(artifact).unlink()
    verification = await w.verify({"draft": ref})
    assert verification.status is F
    assert w.by_id(verification)["required_artifacts"].status is F
    outcome = TaskOutcome(status=TaskStatus.SUCCEEDED, result_ref=ref, verification_ref=verification.id)
    with pytest.raises(VerificationRequired):
        await w.repos.tasks.commit(w.execution.lease, outcome, T0)


@pytest.mark.asyncio
async def test_required_artifact_without_explicit_checks_allows_binary_content(tmp_path):
    w = World(tmp_path)
    await w.task(acceptance=acceptance(checks=[]))
    ref = await w.put(b"\x00\xff", media_type="application/octet-stream")
    assert (await w.verify({"draft": ref})).status is P


@pytest.mark.asyncio
async def test_verification_event_is_recorded(tmp_path):
    w = World(tmp_path)
    await w.task()
    v = await w.verify({"draft": await w.put(GOOD)})
    events = [e for e in await w.repos.events.list("s1", task_id=w.t.id) if e.type is EventType.VERIFICATION_RECORDED]
    assert len(events) == 1
    assert events[0].payload["verification_id"] == v.id and events[0].payload["status"] == "passed"


# -- P0.5 done criteria: 缺章节 / 产物不可读时核验失败 -------------------------------------


@pytest.mark.asyncio
async def test_missing_section_fails(tmp_path):
    w = World(tmp_path)
    await w.task()
    v = await w.verify({"draft": await w.put("# 草稿\n## 目标\nx\n## 方案\ny\n")})
    assert v.status is F
    assert "计划" in w.by_id(v)["required_sections"].detail


@pytest.mark.asyncio
async def test_section_name_only_in_body_text_does_not_count(tmp_path):
    w = World(tmp_path)
    await w.task()
    text = "# 草稿\n## 目标\nx\n## 方案\n后面再写计划。\n```\n## 计划\n```\n"
    assert (await w.verify({"draft": await w.put(text)})).status is F


@pytest.mark.asyncio
async def test_heading_that_only_contains_the_name_does_not_count(tmp_path):
    w = World(tmp_path)
    await w.task()
    text = "## 目标\nx\n## 方案\ny\n## 计划之外的事\nz\n"
    assert (await w.verify({"draft": await w.put(text)})).status is F


@pytest.mark.asyncio
async def test_missing_draft_fails(tmp_path):
    w = World(tmp_path)
    await w.task()
    other = await w.put(GOOD, name="notes")
    v = await w.verify({"notes": other})
    assert v.status is F
    assert w.by_id(v)["required_artifacts"].status is F
    assert w.by_id(v)["result_schema"].status is F


@pytest.mark.asyncio
async def test_artifact_from_another_scope_is_unreadable(tmp_path):
    w = World(tmp_path)
    await w.task()
    foreign = await w.put(GOOD, scope="someone-else")
    v = await w.verify({"draft": foreign})
    assert v.status is F and "cannot be read" in w.by_id(v)["artifact_readable"].detail


@pytest.mark.asyncio
async def test_made_up_artifact_id_fails(tmp_path):
    w = World(tmp_path)
    await w.task()
    assert (await w.verify({"draft": "art_does_not_exist"})).status is F


@pytest.mark.asyncio
async def test_empty_or_binary_draft_fails(tmp_path):
    w = World(tmp_path)
    await w.task()
    assert (await w.verify({"draft": await w.put("   \n")})).status is F
    binary = await w.put(b"\x89PNG\x00\x01", media_type="image/png")
    assert (await w.verify({"draft": binary})).status is F


@pytest.mark.asyncio
async def test_changed_file_on_disk_fails(tmp_path):
    w = World(tmp_path)
    await w.task()
    ref = await w.put(GOOD)
    next(p for p in (tmp_path / "artifacts").rglob("*") if p.is_file()).write_bytes(b"tampered")
    assert (await w.verify({"draft": ref})).status is F


# -- P0.5 done criteria: inconclusive 不会被当成功 ---------------------------------------


@pytest.mark.asyncio
async def test_check_the_pack_does_not_offer_is_inconclusive(tmp_path):
    w = World(tmp_path)
    await w.task(acceptance=acceptance(checks=[{"id": "artifact_readable"}, {"id": "facts_match_sources"}]))
    v = await w.verify({"draft": await w.put(GOOD)})
    assert v.status is I and any("facts_match_sources" in u for u in v.unverified_items)


@pytest.mark.asyncio
async def test_required_outside_action_is_inconclusive(tmp_path):
    w = World(tmp_path)
    await w.task(acceptance=acceptance(required_actions=["send_to_reviewer"]))
    assert (await w.verify({"draft": await w.put(GOOD)})).status is I


@pytest.mark.asyncio
async def test_failure_wins_over_inconclusive(tmp_path):
    w = World(tmp_path)
    await w.task(acceptance=acceptance(required_actions=["send"]))
    assert (await w.verify({"draft": await w.put("## 目标\n")})).status is F


@pytest.mark.asyncio
async def test_no_checks_at_all_is_inconclusive(tmp_path):
    w = World(tmp_path)
    await w.task(acceptance=Acceptance())
    assert (await w.verify({"draft": await w.put(GOOD)})).status is I


@pytest.mark.asyncio
async def test_unknown_capability_is_inconclusive(tmp_path):
    w = World(tmp_path, packs=CapabilityRegistry())
    await w.task()
    v = await w.verify({"draft": await w.put(GOOD)})
    # Only required_artifacts can run without the pack; it passes, but the
    # pack's checks were never run, so the result is not accepted.
    assert v.status is I
    assert any("document_draft@1" in u for u in v.unverified_items)


@pytest.mark.asyncio
async def test_crashing_checker_is_inconclusive_not_passed(tmp_path):
    async def boom(spec, ctx):
        raise RuntimeError("bug")

    w = World(tmp_path)
    w.verifier = CapabilityVerifier(w.repos, w.store, w.packs, checkers={"artifact_readable": boom})
    await w.task(acceptance=acceptance(checks=[{"id": "artifact_readable"}]))
    v = await w.verify({"draft": await w.put(GOOD)})
    assert v.status is I and "RuntimeError" in w.by_id(v)["artifact_readable"].detail


@pytest.mark.asyncio
async def test_broken_result_schema_is_inconclusive_not_an_exception(tmp_path):
    # Review of #17: the schema check must degrade like the other checkers.
    good = CapabilityRegistry.builtin().resolve("document_draft@1")
    broken = dataclasses.replace(good, result_schema={"type": "object", "properties": {"summary": {"type": 12}}})
    packs = CapabilityRegistry()
    packs.register(broken)
    w = World(tmp_path, packs=packs)
    await w.task()
    v = await w.verify({"draft": await w.put(GOOD)})
    assert v.status is I
    assert "SchemaError" in w.by_id(v)["result_schema"].detail


@pytest.mark.asyncio
async def test_missing_jsonschema_is_inconclusive(tmp_path, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_jsonschema(name, *args, **kwargs):
        if name == "jsonschema":
            raise ImportError("no jsonschema")
        return real_import(name, *args, **kwargs)

    w = World(tmp_path)
    await w.task()
    ref = await w.put(GOOD)
    monkeypatch.setattr(builtins, "__import__", no_jsonschema)
    v = await w.verify({"draft": ref})
    monkeypatch.setattr(builtins, "__import__", real_import)
    assert v.status is I and "not installed" in w.by_id(v)["result_schema"].detail


@pytest.mark.asyncio
async def test_inconclusive_cannot_be_committed_as_succeeded(tmp_path):
    from harness.contracts import VerificationRequired

    w = World(tmp_path)
    await w.task(acceptance=acceptance(required_actions=["send"]))
    ref = await w.put(GOOD)
    v = await w.verify({"draft": ref})
    assert v.status is I
    with pytest.raises(VerificationRequired):
        await w.repos.tasks.commit(
            w.execution.lease,
            TaskOutcome(status=TaskStatus.SUCCEEDED, result_ref=ref, verification_ref=v.id),
            T0,
        )


@pytest.mark.asyncio
async def test_sections_check_without_sections_is_inconclusive(tmp_path):
    w = World(tmp_path)
    await w.task(acceptance=acceptance(checks=[{"id": "required_sections"}]))
    assert (await w.verify({"draft": await w.put(GOOD)})).status is I


# -- section matching ---------------------------------------------------------------


def test_heading_parsing_skips_code_fences_and_setext():
    text = "# A\n```\n# not a heading\n```\n  ## B ##\nnot\n===\n####### too deep\n"
    assert markdown_headings(text) == ["A", "B"]


@pytest.mark.asyncio
async def test_numbering_case_and_emphasis_are_ignored(tmp_path):
    w = World(tmp_path)
    await w.task(acceptance=acceptance(checks=[{"id": "required_sections", "sections": ["Goals", "目标"]}]))
    text = "## 2.1 **goals**\nx\n### 一、目标\ny\n"
    assert (await w.verify({"draft": await w.put(text)})).status is P


@pytest.mark.asyncio
async def test_check_can_name_its_artifact(tmp_path):
    w = World(tmp_path)
    await w.task(acceptance=acceptance(
        required_artifacts=["draft", "appendix"],
        checks=[{"id": "required_sections", "sections": ["附录"], "artifact": "appendix"}],
    ))
    draft = await w.put("## 目标\n", name="draft")
    appendix = await w.put("## 附录\n", name="appendix")
    assert (await w.verify({"draft": draft, "appendix": appendix})).status is P


# -- capability packs -----------------------------------------------------------------


def test_builtin_document_draft_pack():
    pack = CapabilityRegistry.builtin().resolve("document_draft@1")
    assert pack.tools == {"files.read", "artifacts.read", "artifacts.write"}
    assert pack.verifiers == ("artifact_readable", "required_sections")
    assert pack.result_artifact == "draft" and pack.default_mode == "draft_only"
    assert pack.instructions_ref == "prompts/document_draft.md"
    assert pack.result_schema["required"] == ["summary", "artifact_refs"]


def test_resolve_is_exact_version():
    reg = CapabilityRegistry.builtin()
    with pytest.raises(NotFound):
        reg.resolve("document_draft@999")
    assert reg.resolve("document_draft@2").tools - reg.resolve("document_draft@1").tools == {"background_results.read"}
    with pytest.raises(ValueError):
        reg.resolve("document_draft")


def test_tool_map_feeds_the_policy():
    assert CapabilityRegistry.builtin().tool_map()["document_draft@1"] == {"files.read", "artifacts.read", "artifacts.write"}


def test_duplicate_pack_is_refused():
    pack = CapabilityPack(id="x", version="1", tools=frozenset(), verifiers=())
    with pytest.raises(CapabilityError):
        CapabilityRegistry([pack, pack])


@pytest.mark.parametrize(
    "body,match",
    [
        ("id: x\nversion: '1'\ntools: [a]\n", "verifiers"),
        ("id: x\nversion: '1'\ntools: [a]\nverifiers: []\nsurprise: 1\n", "unknown"),
        ("id: X Bad\nversion: '1'\ntools: [a]\nverifiers: []\n", "id"),
        ("id: x\nversion: '1'\ntools: a\nverifiers: []\n", "tools"),
        ("id: x\nversion: '1'\ntools: [a]\nverifiers: []\nresult_schema: nope.json\n", "not found"),
        ("- just\n- a list\n", "mapping"),
        ("id: [unclosed\n", "YAML"),
    ],
)
def test_bad_pack_files_are_refused(tmp_path, body, match):
    path = tmp_path / "pack.yaml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(CapabilityError, match=match):
        load_pack(path)


def test_invalid_result_schema_is_refused(tmp_path):
    (tmp_path / "s.json").write_text(json.dumps({"type": 12}), encoding="utf-8")
    path = tmp_path / "pack.yaml"
    path.write_text("id: x\nversion: '1'\ntools: []\nverifiers: []\nresult_schema: s.json\n", encoding="utf-8")
    with pytest.raises(CapabilityError, match="JSON Schema"):
        load_pack(path)


# -- agent profiles and instructions ----------------------------------------------------------


def test_main_profile_registry():
    reg = AgentRegistry.builtin()
    assert reg.resolve(AgentRole.MAIN, "1") is MAIN_PROFILE
    assert reg.resolve_ref("main@1") is MAIN_PROFILE
    assert reg.tool_policies() == {"main@1": "main_tools@1", "proactive@1": "proactive_tools@1"}
    assert reg.resolve(AgentRole.PROACTIVE, "1").default_lane == RunLane.BACKGROUND
    assert main_role_whitelist() == {
        "main_tools@1": {"files.read", "artifacts.read", "artifacts.write", "background_results.read"},
        "proactive_tools@1": {"data.profile", "data.query_readonly", "artifacts.read", "artifacts.write"},
    }


@pytest.mark.asyncio
async def test_package_instructions_load_and_refuse_escapes():
    load = PackageInstructions()
    assert "Main agent" in await load("prompts/main.md")
    assert "document draft" in (await load("prompts/document_draft.md")).lower()
    with pytest.raises(KeyError):
        await load("../../pyproject.toml")
    with pytest.raises(KeyError):
        await load("prompts/missing.md")


# -- evidence references (spec 5.2 CandidateResult: 事实依据引用) -------------------------


@pytest.mark.asyncio
async def test_real_evidence_passes_and_made_up_evidence_fails(tmp_path):
    w = World(tmp_path)
    await w.task()
    draft = await w.put(GOOD)
    source = await w.put("原始材料", name="source", media_type="text/plain")
    ok = await w.verifier.verify(CandidateResult(summary="s", artifact_refs={"draft": draft}, evidence_refs=[source]), w.t)
    assert ok.status is P and w.by_id(ok)["evidence_refs"].status is P
    bad = await w.verifier.verify(
        CandidateResult(summary="s", artifact_refs={"draft": draft}, evidence_refs=["art_made_up"]), w.t
    )
    assert bad.status is F and "art_made_up" in w.by_id(bad)["evidence_refs"].detail


@pytest.mark.asyncio
async def test_evidence_from_another_scope_fails(tmp_path):
    w = World(tmp_path)
    await w.task()
    draft = await w.put(GOOD)
    foreign = await w.put("别人的材料", name="x", scope="someone-else")
    v = await w.verifier.verify(CandidateResult(summary="s", artifact_refs={"draft": draft}, evidence_refs=[foreign]), w.t)
    assert v.status is F


@pytest.mark.asyncio
async def test_evidence_can_cite_a_succeeded_action_but_not_a_failed_one(tmp_path):
    from harness.contracts import Action, ActionStatus

    w = World(tmp_path)
    await w.task()
    draft = await w.put(GOOD)
    good = (await w.repos.actions.register(Action(task_id=w.t.id, attempt_id=w.execution.lease.attempt_id,
             logical_key="step-1.0", tool="files.read", request_hash="h1", read_only=True))).action
    await w.repos.actions.update(good.id, ActionStatus.SUCCEEDED, now=T0)
    bad = (await w.repos.actions.register(Action(task_id=w.t.id, attempt_id=w.execution.lease.attempt_id,
            logical_key="step-2.0", tool="files.read", request_hash="h2", read_only=True))).action
    await w.repos.actions.update(bad.id, ActionStatus.FAILED, now=T0)
    ok = await w.verifier.verify(CandidateResult(summary="s", artifact_refs={"draft": draft}, evidence_refs=[good.id]), w.t)
    assert ok.status is P
    no = await w.verifier.verify(CandidateResult(summary="s", artifact_refs={"draft": draft}, evidence_refs=[bad.id]), w.t)
    assert no.status is F and "failed" in w.by_id(no)["evidence_refs"].detail
