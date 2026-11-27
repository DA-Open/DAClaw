"""Input supplements in a restored, provider-neutral model transcript."""

import json

import pytest

from harness.app import build_system
from harness.config import HarnessConfig
from harness.contracts import ArtifactInput, Checkpoint, ToolCall
from harness.runtime import messages as msg
from harness.runtime.models import ScriptedModelAdapter

from ..fakes import claimed_task
from .test_repositories import repos

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize(("pending", "new_refs"), [
    (True, ["artifact:notes"]), (False, []),
    (False, ["artifact:notes", "file:quarterly report", "artifact:in-system", "artifact:in-tool"]),
], ids=["pending-call", "already-shown", "new-user-refs"])
async def test_checkpoint_input_messages_preserve_tool_order_and_history(repos, tmp_path, pending, new_refs):
    task, execution = await claimed_task(repos)
    system = build_system(HarnessConfig(artifact_root=tmp_path), ScriptedModelAdapter(), repos=repos)
    async with system.lifespan():
        profile = system.agents.resolve_ref(task.agent_profile_ref)
        initial = await system.builder.build(task, profile, execution.lease)
        call = ToolCall(tool="files.read", native_call_id="call-1", logical_key="step-1.0")
        checkpoint = Checkpoint(stage="tools_pending" if pending else "tools_completed",
                                step_id=f"{task.id}.step-1", pending_calls=[call] if pending else [])
        history = [
            msg.system("old instructions\n- artifact:in-system"),
            msg.user("## Inputs\n- artifact:notes-long\n- file:quarterly report revised\n- artifact:multi\nline\n- "),
            msg.user("Please read artifact:source before revising the draft."),
            msg.user("Additional inputs provided on resume:\n- artifact:previous"),
            msg.Message(role="assistant", tool_calls=[msg.ToolCallPart(id="call-1", name="files.read")]).to_dict(),
        ]
        if not pending:
            history.append(msg.tool_result("call-1", "files.read", "read:\n- artifact:in-tool"))
        saved = await system.artifacts.put(ArtifactInput(
            scope_id=task.scope_id, media_type="application/json",
            content=json.dumps({"checkpoint": checkpoint.model_dump(mode="json"), "messages": history}).encode(),
        ))
        previous = ["artifact:notes-long", "file:quarterly report revised", "artifact:multi\nline", "",
                    "artifact:source", "artifact:previous"]
        task = task.model_copy(update={"input_refs": previous + new_refs, "checkpoint_ref": saved.id})
        restored = await system.builder.build(task, profile, execution.lease)
        expected = [initial.messages[0], *history[1:]]
        if new_refs and not pending:
            expected.append(msg.user("Additional inputs provided on resume:\n" + "\n".join(f"- {r}" for r in new_refs)))
        assert restored.messages == expected
        assert restored.checkpoint == checkpoint
