"""Helpers for custom endpoint client options."""

from __future__ import annotations

import json
from typing import Any, Dict


DEFAULT_ENDPOINT_API_VERSION = "2024-02-01"
_AZURE_COMPAT_MARKER_ATTR = "_daclaw_azure_compatible_endpoint"


def normalize_endpoint_client_options(raw: Any) -> Dict[str, Any]:
    """Return sanitized client options from config/runtime input."""
    if not isinstance(raw, dict):
        return {}

    normalized: Dict[str, Any] = {}

    api_version = raw.get("api_version")
    if isinstance(api_version, str) and api_version.strip():
        normalized["api_version"] = api_version.strip()

    headers = raw.get("default_headers")
    if isinstance(headers, str):
        try:
            headers = json.loads(headers)
        except Exception:
            headers = None
    if isinstance(headers, dict):
        clean_headers = {
            str(key).strip(): str(value)
            for key, value in headers.items()
            if str(key).strip() and value is not None
        }
        if clean_headers:
            normalized["default_headers"] = clean_headers

    return normalized


def attach_azure_compatible_client_marker(
    client: Any,
    *,
    endpoint: str,
    client_options: Dict[str, Any],
) -> Any:
    """Attach metadata so Azure-compatible clients can be rebuilt later."""
    marker = {
        "endpoint": str(endpoint or "").strip().rstrip("/"),
        "api_version": str(
            (client_options or {}).get("api_version") or DEFAULT_ENDPOINT_API_VERSION
        ).strip(),
        "default_headers": dict((client_options or {}).get("default_headers") or {}),
    }
    try:
        setattr(client, _AZURE_COMPAT_MARKER_ATTR, marker)
    except Exception:
        pass
    return client


def get_azure_compatible_client_marker(client: Any) -> Dict[str, Any]:
    """Return Azure-compatible endpoint metadata attached to a client."""
    marker = getattr(client, _AZURE_COMPAT_MARKER_ATTR, None)
    return marker if isinstance(marker, dict) else {}
