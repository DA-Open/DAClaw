"""Main's on-demand result read; the gateway records every read in its trace."""
from ...contracts import ToolResult, ToolSpec, ToolStatus, canonical_json
from .files import _Base


class BackgroundResultsConnector(_Base):
    spec = ToolSpec(name="background_results.read", description=(
        "Read a background data card by inbox ID. Default reserves the shared notice key before an incidental mention. "
        "Set explicit=true only when the user directly asks for this result; that permits old results, marked with their version and expiry."),
        input_schema={"type": "object", "properties": {"id": {"type": "string"}, "explicit": {"type": "boolean"}},
                      "required": ["id"], "additionalProperties": False}, read_only=True, side_effects=False, idempotent=True)

    def __init__(self, delivery):
        self.delivery = delivery

    async def execute(self, call):
        data = await self.delivery.read(call.scope_id, call.args["id"], explicit=call.args.get("explicit", False))
        return ToolResult(status=ToolStatus.SUCCEEDED, summary=canonical_json(data), data=data)
