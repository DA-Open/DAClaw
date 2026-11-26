"""The examples packaged for the model are valid decisions for the stated time window."""
import json
import re
from datetime import datetime, timezone

import pytest

from harness.agents import PackageInstructions
from harness.capabilities import CapabilityRegistry


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", ["personal_watch@1", "analysis_watch@1"])
async def test_pack_includes_two_usable_decision_examples(capability):
    pack = CapabilityRegistry.builtin().resolve(capability)
    prompt = await PackageInstructions()(pack.instructions_ref)
    examples = [json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", prompt, flags=re.S)]
    assert [example["decision"] for example in examples] == ["propose_notify", "silent"]
    from harness.personal.decision import ProactiveDecision

    start = datetime(2026, 11, 25, 2, tzinfo=timezone.utc)
    end = datetime(2026, 11, 26, 2, tzinfo=timezone.utc)
    notify, silent = [ProactiveDecision.model_validate(example) for example in examples]
    assert notify.draft and start < notify.valid_until < end
    assert any(ref.startswith("event:") for ref in notify.evidence_refs)
    assert silent.draft is None and silent.valid_until is None
    if capability == "personal_watch@1":
        assert all("claims" not in example for example in examples)
    else:
        assert notify.claims and notify.claims[0].query_ref in notify.evidence_refs
    for name in ("memory.read", "memory.search", "artifacts.write", "submit_result", "dedup_key"):
        assert name in prompt
