"""Parse the single byte ranges supported by the artifact endpoint."""

from __future__ import annotations

import re

from ..contracts import ErrorCode
from .errors import ApiError


def byte_range(value: str, size: int) -> tuple[int, int] | None:
    """Return ``(offset, length)``; an unknown range unit is ignored.

    Empty/out-of-bounds ranges produce length zero for the route's 416
    response. Multiple ranges are deliberately unsupported in this API.
    """
    unit, separator, spec = value.strip().partition("=")
    if separator and unit.lower() != "bytes":
        return None
    match = re.fullmatch(r"([0-9]*)-([0-9]*)", spec.strip()) if separator else None
    if match is None or not any(match.groups()):
        raise ApiError(400, ErrorCode.INVALID_REQUEST, "Range must specify one byte range")
    first, last = match.groups()
    try:
        if not first:
            suffix = int(last)
            offset = max(0, size - suffix)
            return offset, size - offset
        offset = int(first)
        end = int(last) if last else size - 1
    except ValueError:
        raise ApiError(400, ErrorCode.INVALID_REQUEST, "invalid byte range") from None
    if last and end < offset:
        raise ApiError(400, ErrorCode.INVALID_REQUEST, "byte range end must not precede its start")
    return offset, max(0, min(end + 1, size) - offset)
