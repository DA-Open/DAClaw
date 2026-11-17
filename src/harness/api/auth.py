"""Who is calling: bearer token -> (scope_id, actor_id).

Spec 5.1: "身份与 scope_id 从认证取得" -- the request body never names the
scope. P0 has no user system; the permissions file lists API callers by the
sha256 of their token (the token itself is never stored)::

    tokens:
      - sha256: <64 hex chars>     # printf %s "$TOKEN" | sha256sum
        scope_id: s1
        actor_id: u1

A missing, malformed or unknown token is 401. Comparison is on the digest,
in constant time.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable, Mapping
from typing import Any

from ..contracts import Actor, ErrorCode, HarnessError


class Unauthenticated(HarnessError):
    code = ErrorCode.UNAUTHENTICATED


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class TokenAuth:
    def __init__(self, entries: Iterable[Mapping[str, Any]]) -> None:
        self._actors = {
            str(e["sha256"]).lower(): Actor(actor_id=e["actor_id"], scope_id=e["scope_id"]) for e in entries
        }

    def __len__(self) -> int:
        return len(self._actors)

    def actor(self, authorization: str | None) -> Actor:
        if not authorization:
            raise Unauthenticated("missing Authorization header")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise Unauthenticated("expected 'Authorization: Bearer <token>'")
        digest = token_digest(token.strip())
        found = None
        for known, actor in self._actors.items():
            if hmac.compare_digest(known, digest):
                found = actor
        if found is None:
            raise Unauthenticated("unknown token")
        return found
