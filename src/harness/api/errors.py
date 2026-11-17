"""One error body for every failure (spec 5.1)::

    {"error_code": "...", "message": "...", "retryable": false, "request_id": "req_..."}

No stack traces or credentials reach the client; unexpected errors are
logged with the request ID and answered with ``internal`` only.
"""

from __future__ import annotations

import logging

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ..contracts import ErrorCode, HarnessError, new_id
from ..tasks import UnknownCheck

log = logging.getLogger(__name__)

# HarnessError.code -> HTTP status. NOT_FOUND is decided per call site: an
# unknown capability or profile in a request is 400, an unknown task is 404.
STATUS = {
    ErrorCode.INVALID_REQUEST: 400,
    ErrorCode.LIMITS_EXCEEDED: 400,
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.VERSION_CONFLICT: 409,
    ErrorCode.INVALID_TRANSITION: 409,
}


class ApiError(HarnessError):
    """An error with an explicit HTTP status, raised by the routes."""

    def __init__(self, status: int, code: ErrorCode, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable


def request_id(request: Request) -> str:
    rid = getattr(request.state, "request_id", None)
    if rid is None:
        rid = request.state.request_id = new_id("req")
    return rid


def body(request: Request, code: ErrorCode | str, message: str, retryable: bool = False) -> dict:
    return {"error_code": str(code), "message": message, "retryable": retryable, "request_id": request_id(request)}


def install(app) -> None:
    @app.exception_handler(ApiError)
    async def _api(request: Request, exc: ApiError):
        return JSONResponse(body(request, exc.code, exc.message, exc.retryable), status_code=exc.status)

    @app.exception_handler(UnknownCheck)
    async def _check(request: Request, exc: UnknownCheck):
        return JSONResponse(body(request, ErrorCode.INVALID_REQUEST, str(exc)), status_code=400)

    @app.exception_handler(HarnessError)
    async def _harness(request: Request, exc: HarnessError):
        status = STATUS.get(exc.code, 500)
        if status == 500:
            log.error("request %s: %s: %s", request_id(request), exc.code, exc.message)
            return JSONResponse(body(request, ErrorCode.INTERNAL, "internal error", exc.retryable), status_code=500)
        return JSONResponse(body(request, exc.code, exc.message, exc.retryable), status_code=status)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e.get('loc', ()) if p != 'body')}: {e.get('msg')}" for e in exc.errors()
        )
        return JSONResponse(body(request, ErrorCode.INVALID_REQUEST, problems or "invalid request"), status_code=400)

