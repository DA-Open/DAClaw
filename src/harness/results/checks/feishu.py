"""Verify a Feishu receipt against this task's successful send and delivered artifact."""

from ...contracts import ActionStatus, CheckResult, CheckSpec, VerificationStatus
from ...storage.repositories import Repositories
from ..verifier import CheckContext


class FeishuReceiptCheck:
    def __init__(self, repos: Repositories) -> None:
        self._repos = repos

    async def __call__(self, spec: CheckSpec, ctx: CheckContext) -> CheckResult:
        actions = await self._repos.actions.list_for_task(ctx.task.id)
        receipts = [a.receipt_ref for a in actions
                    if a.tool == "feishu.send_message" and a.status is ActionStatus.SUCCEEDED and a.receipt_ref]
        if not receipts:
            return CheckResult(id=spec.id, status=VerificationStatus.FAILED, detail="no successful Feishu send with a receipt")
        cited = set(ctx.candidate.evidence_refs) | set(ctx.candidate.required_action_receipts)
        matched = [ref for ref in receipts if ref in cited]
        if not matched:
            return CheckResult(id=spec.id, status=VerificationStatus.FAILED, detail="no cited receipt matches this task's send")
        text, problem = await ctx.read_text("receipt")
        if problem or text is None or text.strip() not in matched:
            return CheckResult(id=spec.id, status=VerificationStatus.FAILED, detail="receipt artifact must contain the cited receipt")
        return CheckResult(id=spec.id, status=VerificationStatus.PASSED, evidence_refs=matched)
