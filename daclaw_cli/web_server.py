"""
DAClaw Agent — Web UI server.

Provides a FastAPI backend serving the Vite/React frontend and REST API
endpoints for managing configuration, environment variables, and sessions.

Usage:
    python -m daclaw_cli.main web          # Start on http://127.0.0.1:9119
    python -m daclaw_cli.main web --port 8080
"""

import asyncio
import base64
import csv
import hmac
import io
import json
import logging
import mimetypes
import os
import queue
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path, PureWindowsPath
from typing import Any, Dict, List, Optional

import yaml

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from daclaw_cli import __version__, __release_date__
from daclaw_cli.config import (
    DEFAULT_CONFIG,
    OPTIONAL_ENV_VARS,
    get_config_path,
    get_env_path,
    get_daclaw_home,
    load_config,
    load_env,
    save_config,
    save_env_value,
    remove_env_value,
    check_config_version,
    redact_key,
)
from gateway.status import get_running_pid, read_runtime_status

try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel, Field
except ImportError:
    raise SystemExit(
        "Web UI requires fastapi and uvicorn.\n"
        "Run 'daclaw web' to auto-install, or: pip install daclaw-agent[web]"
    )

WEB_DIST = Path(__file__).parent / "web_dist"
_log = logging.getLogger(__name__)

app = FastAPI(title="DAClaw Agent", version=__version__)

# ---------------------------------------------------------------------------
# Session token for protecting sensitive endpoints (reveal).
# Generated fresh on every server start — dies when the process exits.
# Injected into the SPA HTML so only the legitimate web UI can use it.
# In development you can pin a fixed token via DACLAW_WEB_DEV_TOKEN so the Vite
# dev server (npm run dev) can attach the same token in its /api proxy. In
# production leave it unset — a fresh random token is generated per process.
# ---------------------------------------------------------------------------
_SESSION_TOKEN = os.environ.get("DACLAW_WEB_DEV_TOKEN", "").strip() or secrets.token_urlsafe(32)


# Load ~/.daclaw/.env into the process environment at startup so the agent's
# inference provider (e.g. ARK_API_KEY for custom:volcengine-ark) is available
# when the web server is launched directly (uvicorn / start.sh) rather than
# through the CLI. Existing real environment variables always win.
#
# Note: the demo launcher sets DACLAW_HOME=~/.daclaw_demo, but the installer
# writes API keys to the canonical ~/.daclaw/.env. We therefore load BOTH the
# DACLAW_HOME-scoped .env (via load_env) and the canonical ~/.daclaw/.env so the
# key is found regardless of which home the process is scoped to.
def _load_daclaw_env_into_environ() -> None:
    merged: Dict[str, str] = {}
    # 1) DACLAW_HOME-scoped .env (whatever load_env resolves to)
    try:
        merged.update(load_env() or {})
    except Exception:
        pass
    # 2) canonical ~/.daclaw/.env (parsed directly, independent of DACLAW_HOME)
    try:
        canonical = Path.home() / ".daclaw" / ".env"
        if canonical.is_file():
            for raw in canonical.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k:
                    merged.setdefault(k, v)
    except Exception:
        pass
    for _k, _v in merged.items():
        if _k and _k not in os.environ:
            os.environ[_k] = _v


_load_daclaw_env_into_environ()

# Simple rate limiter for the reveal endpoint
_reveal_timestamps: List[float] = []
_REVEAL_MAX_PER_WINDOW = 5
_REVEAL_WINDOW_SECONDS = 30

# CORS: restrict to localhost origins only.  The web UI is intended to run
# locally; binding to 0.0.0.0 with allow_origins=["*"] would let any website
# read/modify config and secrets.

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Endpoints that do NOT require the session token.  Everything else under
# /api/ is gated by the auth middleware below.  Keep this list minimal —
# only truly non-sensitive, read-only endpoints belong here.
# ---------------------------------------------------------------------------
_PUBLIC_API_PATHS: frozenset = frozenset({
    "/api/status",
    "/api/config/defaults",
    "/api/config/schema",
    "/api/model/info",
})


def _require_token(request: Request) -> None:
    """Validate the ephemeral session token.  Raises 401 on mismatch.

    Uses ``hmac.compare_digest`` to prevent timing side-channels.
    """
    auth = request.headers.get("authorization", "")
    expected = f"Bearer {_SESSION_TOKEN}"
    if not hmac.compare_digest(auth.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Require the session token on all /api/ routes except the public list."""
    path = request.url.path
    # DataAgent artifact downloads are opened via plain browser links (no auth
    # header), and only serve files from a sandboxed output dir — allow them.
    _is_da_download = path.startswith("/api/da/download/")
    if path.startswith("/api/") and path not in _PUBLIC_API_PATHS and not _is_da_download:
        auth = request.headers.get("authorization", "")
        expected = f"Bearer {_SESSION_TOKEN}"
        if not hmac.compare_digest(auth.encode(), expected.encode()):
            return JSONResponse(
                status_code=401,
                content={"detail": "Unauthorized"},
            )
    return await call_next(request)


# ---------------------------------------------------------------------------
# Config schema — auto-generated from DEFAULT_CONFIG
# ---------------------------------------------------------------------------

# Manual overrides for fields that need select options or custom types
_SCHEMA_OVERRIDES: Dict[str, Dict[str, Any]] = {
    "model": {
        "type": "string",
        "description": "Default model (e.g. anthropic/claude-sonnet-4.6)",
        "category": "general",
    },
    "model_context_length": {
        "type": "number",
        "description": "Context window override (0 = auto-detect from model metadata)",
        "category": "general",
    },
    "terminal.backend": {
        "type": "select",
        "description": "Terminal execution backend",
        "options": ["local", "docker", "ssh", "modal", "daytona", "singularity"],
    },
    "terminal.modal_mode": {
        "type": "select",
        "description": "Modal sandbox mode",
        "options": ["sandbox", "function"],
    },
    "tts.provider": {
        "type": "select",
        "description": "Text-to-speech provider",
        "options": ["edge", "elevenlabs", "openai", "neutts"],
    },
    "stt.provider": {
        "type": "select",
        "description": "Speech-to-text provider",
        "options": ["local", "openai", "mistral"],
    },
    "display.skin": {
        "type": "select",
        "description": "CLI visual theme",
        "options": ["default", "ares", "mono", "slate"],
    },
    "display.resume_display": {
        "type": "select",
        "description": "How resumed sessions display history",
        "options": ["minimal", "full", "off"],
    },
    "display.busy_input_mode": {
        "type": "select",
        "description": "Input behavior while agent is running",
        "options": ["queue", "interrupt", "block"],
    },
    "memory.provider": {
        "type": "select",
        "description": "Memory provider plugin",
        "options": ["builtin", "honcho"],
    },
    "approvals.mode": {
        "type": "select",
        "description": "Dangerous command approval mode",
        "options": ["ask", "yolo", "deny"],
    },
    "context.engine": {
        "type": "select",
        "description": "Context management engine",
        "options": ["default", "custom"],
    },
    "human_delay.mode": {
        "type": "select",
        "description": "Simulated typing delay mode",
        "options": ["off", "typing", "fixed"],
    },
    "logging.level": {
        "type": "select",
        "description": "Log level for agent.log",
        "options": ["DEBUG", "INFO", "WARNING", "ERROR"],
    },
    "agent.service_tier": {
        "type": "select",
        "description": "API service tier (OpenAI/Anthropic)",
        "options": ["", "auto", "default", "flex"],
    },
    "delegation.reasoning_effort": {
        "type": "select",
        "description": "Reasoning effort for delegated subagents",
        "options": ["", "low", "medium", "high"],
    },
}

# Categories with fewer fields get merged into "general" to avoid tab sprawl.
_CATEGORY_MERGE: Dict[str, str] = {
    "privacy": "security",
    "context": "agent",
    "skills": "agent",
    "cron": "agent",
    "network": "agent",
    "checkpoints": "agent",
    "approvals": "security",
    "human_delay": "display",
    "smart_model_routing": "agent",
}

# Display order for tabs — unlisted categories sort alphabetically after these.
_CATEGORY_ORDER = [
    "general", "agent", "terminal", "display", "delegation",
    "memory", "compression", "security", "browser", "voice",
    "tts", "stt", "logging", "discord", "auxiliary",
]


def _infer_type(value: Any) -> str:
    """Infer a UI field type from a Python value."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "number"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return "string"


def _build_schema_from_config(
    config: Dict[str, Any],
    prefix: str = "",
) -> Dict[str, Dict[str, Any]]:
    """Walk DEFAULT_CONFIG and produce a flat dot-path → field schema dict."""
    schema: Dict[str, Dict[str, Any]] = {}
    for key, value in config.items():
        full_key = f"{prefix}.{key}" if prefix else key

        # Skip internal / version keys
        if full_key in ("_config_version",):
            continue

        # Category is the first path component for nested keys, or "general"
        # for top-level scalar fields (model, toolsets, timezone, etc.).
        if prefix:
            category = prefix.split(".")[0]
        elif isinstance(value, dict):
            category = key
        else:
            category = "general"

        if isinstance(value, dict):
            # Recurse into nested dicts
            schema.update(_build_schema_from_config(value, full_key))
        else:
            entry: Dict[str, Any] = {
                "type": _infer_type(value),
                "description": full_key.replace(".", " → ").replace("_", " ").title(),
                "category": category,
            }
            # Apply manual overrides
            if full_key in _SCHEMA_OVERRIDES:
                entry.update(_SCHEMA_OVERRIDES[full_key])
            # Merge small categories
            entry["category"] = _CATEGORY_MERGE.get(entry["category"], entry["category"])
            schema[full_key] = entry
    return schema


CONFIG_SCHEMA = _build_schema_from_config(DEFAULT_CONFIG)

# Inject virtual fields that don't live in DEFAULT_CONFIG but are surfaced
# by the normalize/denormalize cycle.  Insert model_context_length right after
# the "model" key so it renders adjacent in the frontend.
_mcl_entry = _SCHEMA_OVERRIDES["model_context_length"]
_ordered_schema: Dict[str, Dict[str, Any]] = {}
for _k, _v in CONFIG_SCHEMA.items():
    _ordered_schema[_k] = _v
    if _k == "model":
        _ordered_schema["model_context_length"] = _mcl_entry
CONFIG_SCHEMA = _ordered_schema


class ConfigUpdate(BaseModel):
    config: dict


class EnvVarUpdate(BaseModel):
    key: str
    value: str


class EnvVarDelete(BaseModel):
    key: str


class EnvVarReveal(BaseModel):
    key: str


_GATEWAY_HEALTH_URL = os.getenv("GATEWAY_HEALTH_URL")
_GATEWAY_HEALTH_TIMEOUT = float(os.getenv("GATEWAY_HEALTH_TIMEOUT", "3"))


def _probe_gateway_health() -> tuple[bool, dict | None]:
    """Probe the gateway via its HTTP health endpoint (cross-container).

    Uses ``/health/detailed`` first (returns full state), falling back to
    the simpler ``/health`` endpoint.  Returns ``(is_alive, body_dict)``.

    Accepts any of these as ``GATEWAY_HEALTH_URL``:
    - ``http://gateway:8642``                (base URL — recommended)
    - ``http://gateway:8642/health``         (explicit health path)
    - ``http://gateway:8642/health/detailed`` (explicit detailed path)

    This is a **blocking** call — run via ``run_in_executor`` from async code.
    """
    if not _GATEWAY_HEALTH_URL:
        return False, None

    # Normalise to base URL so we always probe the right paths regardless of
    # whether the user included /health or /health/detailed in the env var.
    base = _GATEWAY_HEALTH_URL.rstrip("/")
    if base.endswith("/health/detailed"):
        base = base[: -len("/health/detailed")]
    elif base.endswith("/health"):
        base = base[: -len("/health")]

    for path in (f"{base}/health/detailed", f"{base}/health"):
        try:
            req = urllib.request.Request(path, method="GET")
            with urllib.request.urlopen(req, timeout=_GATEWAY_HEALTH_TIMEOUT) as resp:
                if resp.status == 200:
                    body = json.loads(resp.read())
                    return True, body
        except Exception:
            continue
    return False, None


@app.get("/api/status")
async def get_status():
    current_ver, latest_ver = check_config_version()

    # --- Gateway liveness detection ---
    # Try local PID check first (same-host).  If that fails and a remote
    # GATEWAY_HEALTH_URL is configured, probe the gateway over HTTP so the
    # dashboard works when the gateway runs in a separate container.
    gateway_pid = get_running_pid()
    gateway_running = gateway_pid is not None
    remote_health_body: dict | None = None

    if not gateway_running and _GATEWAY_HEALTH_URL:
        loop = asyncio.get_event_loop()
        alive, remote_health_body = await loop.run_in_executor(
            None, _probe_gateway_health
        )
        if alive:
            gateway_running = True
            # PID from the remote container (display only — not locally valid)
            if remote_health_body:
                gateway_pid = remote_health_body.get("pid")

    gateway_state = None
    gateway_platforms: dict = {}
    gateway_exit_reason = None
    gateway_updated_at = None
    configured_gateway_platforms: set[str] | None = None
    try:
        from gateway.config import load_gateway_config

        gateway_config = load_gateway_config()
        configured_gateway_platforms = {
            platform.value for platform in gateway_config.get_connected_platforms()
        }
    except Exception:
        configured_gateway_platforms = None

    # Prefer the detailed health endpoint response (has full state) when the
    # local runtime status file is absent or stale (cross-container).
    runtime = read_runtime_status()
    if runtime is None and remote_health_body and remote_health_body.get("gateway_state"):
        runtime = remote_health_body

    if runtime:
        gateway_state = runtime.get("gateway_state")
        gateway_platforms = runtime.get("platforms") or {}
        if configured_gateway_platforms is not None:
            gateway_platforms = {
                key: value
                for key, value in gateway_platforms.items()
                if key in configured_gateway_platforms
            }
        gateway_exit_reason = runtime.get("exit_reason")
        gateway_updated_at = runtime.get("updated_at")
        if not gateway_running:
            gateway_state = gateway_state if gateway_state in ("stopped", "startup_failed") else "stopped"
            gateway_platforms = {}
        elif gateway_running and remote_health_body is not None:
            # The health probe confirmed the gateway is alive, but the local
            # runtime status file may be stale (cross-container).  Override
            # stopped/None state so the dashboard shows the correct badge.
            if gateway_state in (None, "stopped"):
                gateway_state = "running"

    # If there was no runtime info at all but the health probe confirmed alive,
    # ensure we still report the gateway as running (no shared volume scenario).
    if gateway_running and gateway_state is None and remote_health_body is not None:
        gateway_state = "running"

    active_sessions = 0
    try:
        from daclaw_state import SessionDB
        db = SessionDB()
        try:
            sessions = db.list_sessions_rich(limit=50)
            now = time.time()
            active_sessions = sum(
                1 for s in sessions
                if s.get("ended_at") is None
                and (now - s.get("last_active", s.get("started_at", 0))) < 300
            )
        finally:
            db.close()
    except Exception:
        pass

    workspace_root = ""
    try:
        workspace_root = str(_workspace_upload_root())
    except Exception:
        _log.debug("Unable to resolve workspace root for status response", exc_info=True)

    return {
        "version": __version__,
        "release_date": __release_date__,
        "daclaw_home": str(get_daclaw_home()),
        "workspace_root": workspace_root,
        "cwd": str(Path.cwd()),
        "platform": sys.platform,
        "config_path": str(get_config_path()),
        "env_path": str(get_env_path()),
        "config_version": current_ver,
        "latest_config_version": latest_ver,
        "gateway_running": gateway_running,
        "gateway_pid": gateway_pid,
        "gateway_state": gateway_state,
        "gateway_platforms": gateway_platforms,
        "gateway_exit_reason": gateway_exit_reason,
        "gateway_updated_at": gateway_updated_at,
        "active_sessions": active_sessions,
    }


@app.get("/api/sessions")
async def get_sessions(limit: int = 20, offset: int = 0):
    try:
        from daclaw_state import SessionDB
        db = SessionDB()
        try:
            sessions = db.list_sessions_rich(limit=limit, offset=offset)
            total = db.session_count()
            now = time.time()
            for s in sessions:
                s["is_active"] = (
                    s.get("ended_at") is None
                    and (now - s.get("last_active", s.get("started_at", 0))) < 300
                )
            return {"sessions": sessions, "total": total, "limit": limit, "offset": offset}
        finally:
            db.close()
    except Exception as e:
        _log.exception("GET /api/sessions failed")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/sessions/search")
async def search_sessions(q: str = "", limit: int = 20):
    """Full-text search across session message content using FTS5."""
    if not q or not q.strip():
        return {"results": []}
    try:
        from daclaw_state import SessionDB
        db = SessionDB()
        try:
            # Auto-add prefix wildcards so partial words match
            # e.g. "nimb" → "nimb*" matches "nimby"
            # Preserve quoted phrases and existing wildcards as-is
            import re
            terms = []
            for token in re.findall(r'"[^"]*"|\S+', q.strip()):
                if token.startswith('"') or token.endswith("*"):
                    terms.append(token)
                else:
                    terms.append(token + "*")
            prefix_query = " ".join(terms)
            matches = db.search_messages(query=prefix_query, limit=limit)
            # Group by session_id — return unique sessions with their best snippet
            seen: dict = {}
            for m in matches:
                sid = m["session_id"]
                if sid not in seen:
                    seen[sid] = {
                        "session_id": sid,
                        "snippet": m.get("snippet", ""),
                        "role": m.get("role"),
                        "source": m.get("source"),
                        "model": m.get("model"),
                        "session_started": m.get("session_started"),
                    }
            return {"results": list(seen.values())}
        finally:
            db.close()
    except Exception:
        _log.exception("GET /api/sessions/search failed")
        raise HTTPException(status_code=500, detail="Search failed")


def _normalize_config_for_web(config: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize config for the web UI.

    DAClaw supports ``model`` as either a bare string (``"anthropic/claude-sonnet-4"``)
    or a dict (``{default: ..., provider: ..., base_url: ...}``).  The schema is built
    from DEFAULT_CONFIG where ``model`` is a string, but user configs often have the
    dict form.  Normalize to the string form so the frontend schema matches.

    Also surfaces ``model_context_length`` as a top-level field so the web UI can
    display and edit it.  A value of 0 means "auto-detect".
    """
    config = dict(config)  # shallow copy
    model_val = config.get("model")
    if isinstance(model_val, dict):
        # Extract context_length before flattening the dict
        ctx_len = model_val.get("context_length", 0)
        config["model"] = model_val.get("default", model_val.get("name", ""))
        config["model_context_length"] = ctx_len if isinstance(ctx_len, int) else 0
    else:
        config["model_context_length"] = 0
    return config


@app.get("/api/config")
async def get_config():
    config = _normalize_config_for_web(load_config())
    # Strip internal keys that the frontend shouldn't see or send back
    return {k: v for k, v in config.items() if not k.startswith("_")}


@app.get("/api/config/defaults")
async def get_defaults():
    return DEFAULT_CONFIG


@app.get("/api/config/schema")
async def get_schema():
    return {"fields": CONFIG_SCHEMA, "category_order": _CATEGORY_ORDER}


_EMPTY_MODEL_INFO: dict = {
    "model": "",
    "provider": "",
    "auto_context_length": 0,
    "config_context_length": 0,
    "effective_context_length": 0,
    "capabilities": {},
}


@app.get("/api/model/info")
def get_model_info():
    """Return resolved model metadata for the currently configured model.

    Calls the same context-length resolution chain the agent uses, so the
    frontend can display "Auto-detected: 200K" alongside the override field.
    Also returns model capabilities (vision, reasoning, tools) when available.
    """
    try:
        cfg = load_config()
        model_cfg = cfg.get("model", "")

        # Extract model name and provider from the config
        if isinstance(model_cfg, dict):
            model_name = model_cfg.get("default", model_cfg.get("name", ""))
            provider = model_cfg.get("provider", "")
            base_url = model_cfg.get("base_url", "")
            config_ctx = model_cfg.get("context_length")
        else:
            model_name = str(model_cfg) if model_cfg else ""
            provider = ""
            base_url = ""
            config_ctx = None

        if not model_name:
            return dict(_EMPTY_MODEL_INFO, provider=provider)

        # Resolve auto-detected context length (pass config_ctx=None to get
        # purely auto-detected value, then separately report the override)
        try:
            from agent.model_metadata import get_model_context_length
            auto_ctx = get_model_context_length(
                model=model_name,
                base_url=base_url,
                provider=provider,
                config_context_length=None,  # ignore override — we want auto value
            )
        except Exception:
            auto_ctx = 0

        config_ctx_int = 0
        if isinstance(config_ctx, int) and config_ctx > 0:
            config_ctx_int = config_ctx

        # Effective is what the agent actually uses
        effective_ctx = config_ctx_int if config_ctx_int > 0 else auto_ctx

        # Try to get model capabilities from models.dev
        caps = {}
        try:
            from agent.models_dev import get_model_capabilities
            mc = get_model_capabilities(provider=provider, model=model_name)
            if mc is not None:
                caps = {
                    "supports_tools": mc.supports_tools,
                    "supports_vision": mc.supports_vision,
                    "supports_reasoning": mc.supports_reasoning,
                    "context_window": mc.context_window,
                    "max_output_tokens": mc.max_output_tokens,
                    "model_family": mc.model_family,
                }
        except Exception:
            pass

        return {
            "model": model_name,
            "provider": provider,
            "auto_context_length": auto_ctx,
            "config_context_length": config_ctx_int,
            "effective_context_length": effective_ctx,
            "capabilities": caps,
        }
    except Exception:
        _log.exception("GET /api/model/info failed")
        return dict(_EMPTY_MODEL_INFO)


def _denormalize_config_from_web(config: Dict[str, Any]) -> Dict[str, Any]:
    """Reverse _normalize_config_for_web before saving.

    Reconstructs ``model`` as a dict by reading the current on-disk config
    to recover model subkeys (provider, base_url, api_mode, etc.) that were
    stripped from the GET response.  The frontend only sees model as a flat
    string; the rest is preserved transparently.

    Also handles ``model_context_length`` — writes it back into the model dict
    as ``context_length``.  A value of 0 or absent means "auto-detect" (omitted
    from the dict so get_model_context_length() uses its normal resolution).
    """
    config = dict(config)
    # Remove any _model_meta that might have leaked in (shouldn't happen
    # with the stripped GET response, but be defensive)
    config.pop("_model_meta", None)

    # Extract and remove model_context_length before processing model
    ctx_override = config.pop("model_context_length", 0)
    if not isinstance(ctx_override, int):
        try:
            ctx_override = int(ctx_override)
        except (TypeError, ValueError):
            ctx_override = 0

    model_val = config.get("model")
    if isinstance(model_val, str) and model_val:
        # Read the current disk config to recover model subkeys
        try:
            disk_config = load_config()
            disk_model = disk_config.get("model")
            if isinstance(disk_model, dict):
                # Preserve all subkeys, update default with the new value
                disk_model["default"] = model_val
                # Write context_length into the model dict (0 = remove/auto)
                if ctx_override > 0:
                    disk_model["context_length"] = ctx_override
                else:
                    disk_model.pop("context_length", None)
                config["model"] = disk_model
            else:
                # Model was previously a bare string — upgrade to dict if
                # user is setting a context_length override
                if ctx_override > 0:
                    config["model"] = {
                        "default": model_val,
                        "context_length": ctx_override,
                    }
        except Exception:
            pass  # can't read disk config — just use the string form
    return config


@app.put("/api/config")
async def update_config(body: ConfigUpdate):
    try:
        save_config(_denormalize_config_from_web(body.config))
        return {"ok": True}
    except Exception as e:
        _log.exception("PUT /api/config failed")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/env")
async def get_env_vars():
    env_on_disk = load_env()
    result = {}
    for var_name, info in OPTIONAL_ENV_VARS.items():
        value = env_on_disk.get(var_name)
        result[var_name] = {
            "is_set": bool(value),
            "redacted_value": redact_key(value) if value else None,
            "description": info.get("description", ""),
            "url": info.get("url"),
            "category": info.get("category", ""),
            "is_password": info.get("password", False),
            "tools": info.get("tools", []),
            "advanced": info.get("advanced", False),
        }
    return result


@app.put("/api/env")
async def set_env_var(body: EnvVarUpdate):
    try:
        save_env_value(body.key, body.value)
        return {"ok": True, "key": body.key}
    except Exception as e:
        _log.exception("PUT /api/env failed")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.delete("/api/env")
async def remove_env_var(body: EnvVarDelete):
    try:
        removed = remove_env_value(body.key)
        if not removed:
            raise HTTPException(status_code=404, detail=f"{body.key} not found in .env")
        return {"ok": True, "key": body.key}
    except HTTPException:
        raise
    except Exception as e:
        _log.exception("DELETE /api/env failed")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/api/env/reveal")
async def reveal_env_var(body: EnvVarReveal, request: Request):
    """Return the real (unredacted) value of a single env var.

    Protected by:
    - Ephemeral session token (generated per server start, injected into SPA)
    - Rate limiting (max 5 reveals per 30s window)
    - Audit logging
    """
    # --- Token check ---
    _require_token(request)

    # --- Rate limit ---
    now = time.time()
    cutoff = now - _REVEAL_WINDOW_SECONDS
    _reveal_timestamps[:] = [t for t in _reveal_timestamps if t > cutoff]
    if len(_reveal_timestamps) >= _REVEAL_MAX_PER_WINDOW:
        raise HTTPException(status_code=429, detail="Too many reveal requests. Try again shortly.")
    _reveal_timestamps.append(now)

    # --- Reveal ---
    env_on_disk = load_env()
    value = env_on_disk.get(body.key)
    if value is None:
        raise HTTPException(status_code=404, detail=f"{body.key} not found in .env")

    _log.info("env/reveal: %s", body.key)
    return {"key": body.key, "value": value}


# ---------------------------------------------------------------------------
# OAuth provider endpoints — status + disconnect (Phase 1)
# ---------------------------------------------------------------------------
#
# Phase 1 surfaces *which OAuth providers exist* and whether each is
# connected, plus a disconnect button. The actual login flow (PKCE for
# Anthropic, device-code for Nous/Codex) still runs in the CLI for now;
# Phase 2 will add in-browser flows. For unconnected providers we return
# the canonical ``daclaw auth add <provider>`` command so the dashboard
# can surface a one-click copy.


def _truncate_token(value: Optional[str], visible: int = 6) -> str:
    """Return ``...XXXXXX`` (last N chars) for safe display in the UI.

    We never expose more than the trailing ``visible`` characters of an
    OAuth access token. JWT prefixes (the part before the first dot) are
    stripped first when present so the visible suffix is always part of
    the signing region rather than a meaningless header chunk.
    """
    if not value:
        return ""
    s = str(value)
    if "." in s and s.count(".") >= 2:
        # Looks like a JWT — show the trailing piece of the signature only.
        s = s.rsplit(".", 1)[-1]
    if len(s) <= visible:
        return s
    return f"…{s[-visible:]}"


def _anthropic_oauth_status() -> Dict[str, Any]:
    """Combined status across the three Anthropic credential sources we read.

    DAClaw resolves Anthropic creds in this order at runtime:
    1. ``~/.daclaw/.anthropic_oauth.json`` — DAClaw-managed PKCE flow
    2. ``~/.claude/.credentials.json`` — Claude Code CLI credentials (auto)
    3. ``ANTHROPIC_TOKEN`` / ``ANTHROPIC_API_KEY`` env vars
    The dashboard reports the highest-priority source that's actually present.
    """
    try:
        from agent.anthropic_adapter import (
            read_daclaw_oauth_credentials,
            read_claude_code_credentials,
            _DACLAW_OAUTH_FILE,
        )
    except ImportError:
        read_claude_code_credentials = None  # type: ignore
        read_daclaw_oauth_credentials = None  # type: ignore
        _DACLAW_OAUTH_FILE = None  # type: ignore

    daclaw_creds = None
    if read_daclaw_oauth_credentials:
        try:
            daclaw_creds = read_daclaw_oauth_credentials()
        except Exception:
            daclaw_creds = None
    if daclaw_creds and daclaw_creds.get("accessToken"):
        return {
            "logged_in": True,
            "source": "daclaw_pkce",
            "source_label": f"DAClaw PKCE ({_DACLAW_OAUTH_FILE})",
            "token_preview": _truncate_token(daclaw_creds.get("accessToken")),
            "expires_at": daclaw_creds.get("expiresAt"),
            "has_refresh_token": bool(daclaw_creds.get("refreshToken")),
        }

    cc_creds = None
    if read_claude_code_credentials:
        try:
            cc_creds = read_claude_code_credentials()
        except Exception:
            cc_creds = None
    if cc_creds and cc_creds.get("accessToken"):
        return {
            "logged_in": True,
            "source": "claude_code",
            "source_label": "Claude Code (~/.claude/.credentials.json)",
            "token_preview": _truncate_token(cc_creds.get("accessToken")),
            "expires_at": cc_creds.get("expiresAt"),
            "has_refresh_token": bool(cc_creds.get("refreshToken")),
        }

    env_token = os.getenv("ANTHROPIC_TOKEN") or os.getenv("CLAUDE_CODE_OAUTH_TOKEN")
    if env_token:
        return {
            "logged_in": True,
            "source": "env_var",
            "source_label": "ANTHROPIC_TOKEN environment variable",
            "token_preview": _truncate_token(env_token),
            "expires_at": None,
            "has_refresh_token": False,
        }
    return {"logged_in": False, "source": None}


def _claude_code_only_status() -> Dict[str, Any]:
    """Surface Claude Code CLI credentials as their own provider entry.

    Independent of the Anthropic entry above so users can see whether their
    Claude Code subscription tokens are actively flowing into DAClaw even
    when they also have a separate DAClaw-managed PKCE login.
    """
    try:
        from agent.anthropic_adapter import read_claude_code_credentials
        creds = read_claude_code_credentials()
    except Exception:
        creds = None
    if creds and creds.get("accessToken"):
        return {
            "logged_in": True,
            "source": "claude_code_cli",
            "source_label": "~/.claude/.credentials.json",
            "token_preview": _truncate_token(creds.get("accessToken")),
            "expires_at": creds.get("expiresAt"),
            "has_refresh_token": bool(creds.get("refreshToken")),
        }
    return {"logged_in": False, "source": None}


# Provider catalog. The order matters — it's how we render the UI list.
# ``cli_command`` is what the dashboard surfaces as the copy-to-clipboard
# fallback while Phase 2 (in-browser flows) isn't built yet.
# ``flow`` describes the OAuth shape so the future modal can pick the
# right UI: ``pkce`` = open URL + paste callback code, ``device_code`` =
# show code + verification URL + poll, ``external`` = read-only (delegated
# to a third-party CLI like Claude Code or Qwen).
_OAUTH_PROVIDER_CATALOG: tuple[Dict[str, Any], ...] = (
    {
        "id": "anthropic",
        "name": "Anthropic (Claude API)",
        "flow": "pkce",
        "cli_command": "daclaw auth add anthropic",
        "docs_url": "https://docs.claude.com/en/api/getting-started",
        "status_fn": _anthropic_oauth_status,
    },
    {
        "id": "claude-code",
        "name": "Claude Code (subscription)",
        "flow": "external",
        "cli_command": "claude setup-token",
        "docs_url": "https://docs.claude.com/en/docs/claude-code",
        "status_fn": _claude_code_only_status,
    },
    {
        "id": "nous",
        "name": "Nous Portal",
        "flow": "device_code",
        "cli_command": "daclaw auth add nous",
        "docs_url": "https://portal.DA-Open.com",
        "status_fn": None,  # dispatched via auth.get_nous_auth_status
    },
    {
        "id": "openai-codex",
        "name": "OpenAI Codex (ChatGPT)",
        "flow": "device_code",
        "cli_command": "daclaw auth add openai-codex",
        "docs_url": "https://platform.openai.com/docs",
        "status_fn": None,  # dispatched via auth.get_codex_auth_status
    },
    {
        "id": "qwen-oauth",
        "name": "Qwen (via Qwen CLI)",
        "flow": "external",
        "cli_command": "daclaw auth add qwen-oauth",
        "docs_url": "https://github.com/QwenLM/qwen-code",
        "status_fn": None,  # dispatched via auth.get_qwen_auth_status
    },
)


def _resolve_provider_status(provider_id: str, status_fn) -> Dict[str, Any]:
    """Dispatch to the right status helper for an OAuth provider entry."""
    if status_fn is not None:
        try:
            return status_fn()
        except Exception as e:
            return {"logged_in": False, "error": str(e)}
    try:
        from daclaw_cli import auth as hauth
        if provider_id == "nous":
            raw = hauth.get_nous_auth_status()
            return {
                "logged_in": bool(raw.get("logged_in")),
                "source": "nous_portal",
                "source_label": raw.get("portal_base_url") or "Nous Portal",
                "token_preview": _truncate_token(raw.get("access_token")),
                "expires_at": raw.get("access_expires_at"),
                "has_refresh_token": bool(raw.get("has_refresh_token")),
            }
        if provider_id == "openai-codex":
            raw = hauth.get_codex_auth_status()
            return {
                "logged_in": bool(raw.get("logged_in")),
                "source": raw.get("source") or "openai_codex",
                "source_label": raw.get("auth_mode") or "OpenAI Codex",
                "token_preview": _truncate_token(raw.get("api_key")),
                "expires_at": None,
                "has_refresh_token": False,
                "last_refresh": raw.get("last_refresh"),
            }
        if provider_id == "qwen-oauth":
            raw = hauth.get_qwen_auth_status()
            return {
                "logged_in": bool(raw.get("logged_in")),
                "source": "qwen_cli",
                "source_label": raw.get("auth_store_path") or "Qwen CLI",
                "token_preview": _truncate_token(raw.get("access_token")),
                "expires_at": raw.get("expires_at"),
                "has_refresh_token": bool(raw.get("has_refresh_token")),
            }
    except Exception as e:
        return {"logged_in": False, "error": str(e)}
    return {"logged_in": False}


@app.get("/api/providers/oauth")
async def list_oauth_providers():
    """Enumerate every OAuth-capable LLM provider with current status.

    Response shape (per provider):
        id              stable identifier (used in DELETE path)
        name            human label
        flow            "pkce" | "device_code" | "external"
        cli_command     fallback CLI command for users to run manually
        docs_url        external docs/portal link for the "Learn more" link
        status:
          logged_in        bool — currently has usable creds
          source           short slug ("daclaw_pkce", "claude_code", ...)
          source_label     human-readable origin (file path, env var name)
          token_preview    last N chars of the token, never the full token
          expires_at       ISO timestamp string or null
          has_refresh_token bool
    """
    providers = []
    for p in _OAUTH_PROVIDER_CATALOG:
        status = _resolve_provider_status(p["id"], p.get("status_fn"))
        providers.append({
            "id": p["id"],
            "name": p["name"],
            "flow": p["flow"],
            "cli_command": p["cli_command"],
            "docs_url": p["docs_url"],
            "status": status,
        })
    return {"providers": providers}


@app.delete("/api/providers/oauth/{provider_id}")
async def disconnect_oauth_provider(provider_id: str, request: Request):
    """Disconnect an OAuth provider. Token-protected (matches /env/reveal)."""
    _require_token(request)

    valid_ids = {p["id"] for p in _OAUTH_PROVIDER_CATALOG}
    if provider_id not in valid_ids:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown provider: {provider_id}. "
                   f"Available: {', '.join(sorted(valid_ids))}",
        )

    # Anthropic and claude-code clear the same DAClaw-managed PKCE file
    # AND forget the Claude Code import. We don't touch ~/.claude/* directly
    # — that's owned by the Claude Code CLI; users can re-auth there if they
    # want to undo a disconnect.
    if provider_id in ("anthropic", "claude-code"):
        try:
            from agent.anthropic_adapter import _DACLAW_OAUTH_FILE
            if _DACLAW_OAUTH_FILE.exists():
                _DACLAW_OAUTH_FILE.unlink()
        except Exception:
            pass
        # Also clear the credential pool entry if present.
        try:
            from daclaw_cli.auth import clear_provider_auth
            clear_provider_auth("anthropic")
        except Exception:
            pass
        _log.info("oauth/disconnect: %s", provider_id)
        return {"ok": True, "provider": provider_id}

    try:
        from daclaw_cli.auth import clear_provider_auth
        cleared = clear_provider_auth(provider_id)
        _log.info("oauth/disconnect: %s (cleared=%s)", provider_id, cleared)
        return {"ok": bool(cleared), "provider": provider_id}
    except Exception as e:
        _log.exception("disconnect %s failed", provider_id)
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# OAuth Phase 2 — in-browser PKCE & device-code flows
# ---------------------------------------------------------------------------
#
# Two flow shapes are supported:
#
#   PKCE (Anthropic):
#     1. POST /api/providers/oauth/anthropic/start
#          → server generates code_verifier + challenge, builds claude.ai
#            authorize URL, stashes verifier in _oauth_sessions[session_id]
#          → returns { session_id, flow: "pkce", auth_url }
#     2. UI opens auth_url in a new tab. User authorizes, copies code.
#     3. POST /api/providers/oauth/anthropic/submit { session_id, code }
#          → server exchanges (code + verifier) → tokens at console.anthropic.com
#          → persists to ~/.daclaw/.anthropic_oauth.json AND credential pool
#          → returns { ok: true, status: "approved" }
#
#   Device code (Nous, OpenAI Codex):
#     1. POST /api/providers/oauth/{nous|openai-codex}/start
#          → server hits provider's device-auth endpoint
#          → gets { user_code, verification_url, device_code, interval, expires_in }
#          → spawns background poller thread that polls the token endpoint
#            every `interval` seconds until approved/expired
#          → stores poll status in _oauth_sessions[session_id]
#          → returns { session_id, flow: "device_code", user_code,
#                      verification_url, expires_in, poll_interval }
#     2. UI opens verification_url in a new tab and shows user_code.
#     3. UI polls GET /api/providers/oauth/{provider}/poll/{session_id}
#          every 2s until status != "pending".
#     4. On "approved" the background thread has already saved creds; UI
#        refreshes the providers list.
#
# Sessions are kept in-memory only (single-process FastAPI) and time out
# after 15 minutes. A periodic cleanup runs on each /start call to GC
# expired sessions so the dict doesn't grow without bound.

_OAUTH_SESSION_TTL_SECONDS = 15 * 60
_oauth_sessions: Dict[str, Dict[str, Any]] = {}
_oauth_sessions_lock = threading.Lock()

# Import OAuth constants from canonical source instead of duplicating.
# Guarded so daclaw web still starts if anthropic_adapter is unavailable;
# Phase 2 endpoints will return 501 in that case.
try:
    from agent.anthropic_adapter import (
        _OAUTH_CLIENT_ID as _ANTHROPIC_OAUTH_CLIENT_ID,
        _OAUTH_TOKEN_URL as _ANTHROPIC_OAUTH_TOKEN_URL,
        _OAUTH_REDIRECT_URI as _ANTHROPIC_OAUTH_REDIRECT_URI,
        _OAUTH_SCOPES as _ANTHROPIC_OAUTH_SCOPES,
        _generate_pkce as _generate_pkce_pair,
    )
    _ANTHROPIC_OAUTH_AVAILABLE = True
except ImportError:
    _ANTHROPIC_OAUTH_AVAILABLE = False
_ANTHROPIC_OAUTH_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"


def _gc_oauth_sessions() -> None:
    """Drop expired sessions. Called opportunistically on /start."""
    cutoff = time.time() - _OAUTH_SESSION_TTL_SECONDS
    with _oauth_sessions_lock:
        stale = [sid for sid, sess in _oauth_sessions.items() if sess["created_at"] < cutoff]
        for sid in stale:
            _oauth_sessions.pop(sid, None)


def _new_oauth_session(provider_id: str, flow: str) -> tuple[str, Dict[str, Any]]:
    """Create + register a new OAuth session, return (session_id, session_dict)."""
    sid = secrets.token_urlsafe(16)
    sess = {
        "session_id": sid,
        "provider": provider_id,
        "flow": flow,
        "created_at": time.time(),
        "status": "pending",  # pending | approved | denied | expired | error
        "error_message": None,
    }
    with _oauth_sessions_lock:
        _oauth_sessions[sid] = sess
    return sid, sess


def _save_anthropic_oauth_creds(access_token: str, refresh_token: str, expires_at_ms: int) -> None:
    """Persist Anthropic PKCE creds to both DAClaw file AND credential pool.

    Mirrors what auth_commands.add_command does so the dashboard flow leaves
    the system in the same state as ``daclaw auth add anthropic``.
    """
    from agent.anthropic_adapter import _DACLAW_OAUTH_FILE
    payload = {
        "accessToken": access_token,
        "refreshToken": refresh_token,
        "expiresAt": expires_at_ms,
    }
    _DACLAW_OAUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    _DACLAW_OAUTH_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    # Best-effort credential-pool insert. Failure here doesn't invalidate
    # the file write — pool registration only matters for the rotation
    # strategy, not for runtime credential resolution.
    try:
        from agent.credential_pool import (
            PooledCredential,
            load_pool,
            AUTH_TYPE_OAUTH,
            SOURCE_MANUAL,
        )
        import uuid
        pool = load_pool("anthropic")
        # Avoid duplicate entries: delete any prior dashboard-issued OAuth entry
        existing = [e for e in pool.entries() if getattr(e, "source", "").startswith(f"{SOURCE_MANUAL}:dashboard_pkce")]
        for e in existing:
            try:
                pool.remove_entry(getattr(e, "id", ""))
            except Exception:
                pass
        entry = PooledCredential(
            provider="anthropic",
            id=uuid.uuid4().hex[:6],
            label="dashboard PKCE",
            auth_type=AUTH_TYPE_OAUTH,
            priority=0,
            source=f"{SOURCE_MANUAL}:dashboard_pkce",
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at_ms=expires_at_ms,
        )
        pool.add_entry(entry)
    except Exception as e:
        _log.warning("anthropic pool add (dashboard) failed: %s", e)


def _start_anthropic_pkce() -> Dict[str, Any]:
    """Begin PKCE flow. Returns the auth URL the UI should open."""
    if not _ANTHROPIC_OAUTH_AVAILABLE:
        raise HTTPException(status_code=501, detail="Anthropic OAuth not available (missing adapter)")
    verifier, challenge = _generate_pkce_pair()
    sid, sess = _new_oauth_session("anthropic", "pkce")
    sess["verifier"] = verifier
    sess["state"] = verifier  # Anthropic round-trips verifier as state
    params = {
        "code": "true",
        "client_id": _ANTHROPIC_OAUTH_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": _ANTHROPIC_OAUTH_REDIRECT_URI,
        "scope": _ANTHROPIC_OAUTH_SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": verifier,
    }
    auth_url = f"{_ANTHROPIC_OAUTH_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"
    return {
        "session_id": sid,
        "flow": "pkce",
        "auth_url": auth_url,
        "expires_in": _OAUTH_SESSION_TTL_SECONDS,
    }


def _submit_anthropic_pkce(session_id: str, code_input: str) -> Dict[str, Any]:
    """Exchange authorization code for tokens. Persists on success."""
    with _oauth_sessions_lock:
        sess = _oauth_sessions.get(session_id)
    if not sess or sess["provider"] != "anthropic" or sess["flow"] != "pkce":
        raise HTTPException(status_code=404, detail="Unknown or expired session")
    if sess["status"] != "pending":
        return {"ok": False, "status": sess["status"], "message": sess.get("error_message")}

    # Anthropic's redirect callback page formats the code as `<code>#<state>`.
    # Strip the state suffix if present (we already have the verifier server-side).
    parts = code_input.strip().split("#", 1)
    code = parts[0].strip()
    if not code:
        return {"ok": False, "status": "error", "message": "No code provided"}
    state_from_callback = parts[1] if len(parts) > 1 else ""

    exchange_data = json.dumps({
        "grant_type": "authorization_code",
        "client_id": _ANTHROPIC_OAUTH_CLIENT_ID,
        "code": code,
        "state": state_from_callback or sess["state"],
        "redirect_uri": _ANTHROPIC_OAUTH_REDIRECT_URI,
        "code_verifier": sess["verifier"],
    }).encode()
    req = urllib.request.Request(
        _ANTHROPIC_OAUTH_TOKEN_URL,
        data=exchange_data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "daclaw-dashboard/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read().decode())
    except Exception as e:
        sess["status"] = "error"
        sess["error_message"] = f"Token exchange failed: {e}"
        return {"ok": False, "status": "error", "message": sess["error_message"]}

    access_token = result.get("access_token", "")
    refresh_token = result.get("refresh_token", "")
    expires_in = int(result.get("expires_in") or 3600)
    if not access_token:
        sess["status"] = "error"
        sess["error_message"] = "No access token returned"
        return {"ok": False, "status": "error", "message": sess["error_message"]}

    expires_at_ms = int(time.time() * 1000) + (expires_in * 1000)
    try:
        _save_anthropic_oauth_creds(access_token, refresh_token, expires_at_ms)
    except Exception as e:
        sess["status"] = "error"
        sess["error_message"] = f"Save failed: {e}"
        return {"ok": False, "status": "error", "message": sess["error_message"]}
    sess["status"] = "approved"
    _log.info("oauth/pkce: anthropic login completed (session=%s)", session_id)
    return {"ok": True, "status": "approved"}


async def _start_device_code_flow(provider_id: str) -> Dict[str, Any]:
    """Initiate a device-code flow (Nous or OpenAI Codex).

    Calls the provider's device-auth endpoint via the existing CLI helpers,
    then spawns a background poller. Returns the user-facing display fields
    so the UI can render the verification page link + user code.
    """
    from daclaw_cli import auth as hauth
    if provider_id == "nous":
        from daclaw_cli.auth import _request_device_code, PROVIDER_REGISTRY
        import httpx
        pconfig = PROVIDER_REGISTRY["nous"]
        portal_base_url = (
            os.getenv("DACLAW_PORTAL_BASE_URL")
            or os.getenv("NOUS_PORTAL_BASE_URL")
            or pconfig.portal_base_url
        ).rstrip("/")
        client_id = pconfig.client_id
        scope = pconfig.scope
        def _do_nous_device_request():
            with httpx.Client(timeout=httpx.Timeout(15.0), headers={"Accept": "application/json"}) as client:
                return _request_device_code(
                    client=client,
                    portal_base_url=portal_base_url,
                    client_id=client_id,
                    scope=scope,
                )
        device_data = await asyncio.get_event_loop().run_in_executor(None, _do_nous_device_request)
        sid, sess = _new_oauth_session("nous", "device_code")
        sess["device_code"] = str(device_data["device_code"])
        sess["interval"] = int(device_data["interval"])
        sess["expires_at"] = time.time() + int(device_data["expires_in"])
        sess["portal_base_url"] = portal_base_url
        sess["client_id"] = client_id
        threading.Thread(
            target=_nous_poller, args=(sid,), daemon=True, name=f"oauth-poll-{sid[:6]}"
        ).start()
        return {
            "session_id": sid,
            "flow": "device_code",
            "user_code": str(device_data["user_code"]),
            "verification_url": str(device_data["verification_uri_complete"]),
            "expires_in": int(device_data["expires_in"]),
            "poll_interval": int(device_data["interval"]),
        }

    if provider_id == "openai-codex":
        # Codex uses fixed OpenAI device-auth endpoints; reuse the helper.
        sid, _ = _new_oauth_session("openai-codex", "device_code")
        # Use the helper but in a thread because it polls inline.
        # We can't extract just the start step without refactoring auth.py,
        # so we run the full helper in a worker and proxy the user_code +
        # verification_url back via the session dict. The helper prints
        # to stdout — we capture nothing here, just status.
        threading.Thread(
            target=_codex_full_login_worker, args=(sid,), daemon=True,
            name=f"oauth-codex-{sid[:6]}",
        ).start()
        # Block briefly until the worker has populated the user_code, OR error.
        deadline = time.time() + 10
        while time.time() < deadline:
            with _oauth_sessions_lock:
                s = _oauth_sessions.get(sid)
            if s and (s.get("user_code") or s["status"] != "pending"):
                break
            await asyncio.sleep(0.1)
        with _oauth_sessions_lock:
            s = _oauth_sessions.get(sid, {})
        if s.get("status") == "error":
            raise HTTPException(status_code=500, detail=s.get("error_message") or "device-auth failed")
        if not s.get("user_code"):
            raise HTTPException(status_code=504, detail="device-auth timed out before returning a user code")
        return {
            "session_id": sid,
            "flow": "device_code",
            "user_code": s["user_code"],
            "verification_url": s["verification_url"],
            "expires_in": int(s.get("expires_in") or 900),
            "poll_interval": int(s.get("interval") or 5),
        }

    raise HTTPException(status_code=400, detail=f"Provider {provider_id} does not support device-code flow")


def _nous_poller(session_id: str) -> None:
    """Background poller that drives a Nous device-code flow to completion."""
    from daclaw_cli.auth import _poll_for_token, refresh_nous_oauth_from_state
    from datetime import datetime, timezone
    import httpx
    with _oauth_sessions_lock:
        sess = _oauth_sessions.get(session_id)
    if not sess:
        return
    portal_base_url = sess["portal_base_url"]
    client_id = sess["client_id"]
    device_code = sess["device_code"]
    interval = sess["interval"]
    expires_in = max(60, int(sess["expires_at"] - time.time()))
    try:
        with httpx.Client(timeout=httpx.Timeout(15.0), headers={"Accept": "application/json"}) as client:
            token_data = _poll_for_token(
                client=client,
                portal_base_url=portal_base_url,
                client_id=client_id,
                device_code=device_code,
                expires_in=expires_in,
                poll_interval=interval,
            )
        # Same post-processing as _nous_device_code_login (mint agent key)
        now = datetime.now(timezone.utc)
        token_ttl = int(token_data.get("expires_in") or 0)
        auth_state = {
            "portal_base_url": portal_base_url,
            "inference_base_url": token_data.get("inference_base_url"),
            "client_id": client_id,
            "scope": token_data.get("scope"),
            "token_type": token_data.get("token_type", "Bearer"),
            "access_token": token_data["access_token"],
            "refresh_token": token_data.get("refresh_token"),
            "obtained_at": now.isoformat(),
            "expires_at": (
                datetime.fromtimestamp(now.timestamp() + token_ttl, tz=timezone.utc).isoformat()
                if token_ttl else None
            ),
            "expires_in": token_ttl,
        }
        full_state = refresh_nous_oauth_from_state(
            auth_state, min_key_ttl_seconds=300, timeout_seconds=15.0,
            force_refresh=False, force_mint=True,
        )
        # Save into credential pool same as auth_commands.py does
        from agent.credential_pool import (
            PooledCredential,
            load_pool,
            AUTH_TYPE_OAUTH,
            SOURCE_MANUAL,
        )
        pool = load_pool("nous")
        entry = PooledCredential.from_dict("nous", {
            **full_state,
            "label": "dashboard device_code",
            "auth_type": AUTH_TYPE_OAUTH,
            "source": f"{SOURCE_MANUAL}:dashboard_device_code",
            "base_url": full_state.get("inference_base_url"),
        })
        pool.add_entry(entry)
        # Also persist to auth store so get_nous_auth_status() sees it
        # (matches what _login_nous in auth.py does for the CLI flow).
        try:
            from daclaw_cli.auth import (
                _load_auth_store, _save_provider_state, _save_auth_store,
                _auth_store_lock,
            )
            with _auth_store_lock():
                auth_store = _load_auth_store()
                _save_provider_state(auth_store, "nous", full_state)
                _save_auth_store(auth_store)
        except Exception as store_exc:
            _log.warning(
                "oauth/device: credential pool saved but auth store write failed "
                "(session=%s): %s", session_id, store_exc,
            )
        with _oauth_sessions_lock:
            sess["status"] = "approved"
        _log.info("oauth/device: nous login completed (session=%s)", session_id)
    except Exception as e:
        _log.warning("nous device-code poll failed (session=%s): %s", session_id, e)
        with _oauth_sessions_lock:
            sess["status"] = "error"
            sess["error_message"] = str(e)


def _codex_full_login_worker(session_id: str) -> None:
    """Run the complete OpenAI Codex device-code flow.

    Codex doesn't use the standard OAuth device-code endpoints; it has its
    own ``/api/accounts/deviceauth/usercode`` (JSON body, returns
    ``device_auth_id``) and ``/api/accounts/deviceauth/token`` (JSON body
    polled until 200). On success the response carries an
    ``authorization_code`` + ``code_verifier`` that get exchanged at
    CODEX_OAUTH_TOKEN_URL with grant_type=authorization_code.

    The flow is replicated inline (rather than calling
    _codex_device_code_login) because that helper prints/blocks/polls in a
    single function — we need to surface the user_code to the dashboard the
    moment we receive it, well before polling completes.
    """
    try:
        import httpx
        from daclaw_cli.auth import (
            CODEX_OAUTH_CLIENT_ID,
            CODEX_OAUTH_TOKEN_URL,
            DEFAULT_CODEX_BASE_URL,
        )
        issuer = "https://auth.openai.com"

        # Step 1: request device code
        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            resp = client.post(
                f"{issuer}/api/accounts/deviceauth/usercode",
                json={"client_id": CODEX_OAUTH_CLIENT_ID},
                headers={"Content-Type": "application/json"},
            )
        if resp.status_code != 200:
            raise RuntimeError(f"deviceauth/usercode returned {resp.status_code}")
        device_data = resp.json()
        user_code = device_data.get("user_code", "")
        device_auth_id = device_data.get("device_auth_id", "")
        poll_interval = max(3, int(device_data.get("interval", "5")))
        if not user_code or not device_auth_id:
            raise RuntimeError("device-code response missing user_code or device_auth_id")
        verification_url = f"{issuer}/codex/device"
        with _oauth_sessions_lock:
            sess = _oauth_sessions.get(session_id)
            if not sess:
                return
            sess["user_code"] = user_code
            sess["verification_url"] = verification_url
            sess["device_auth_id"] = device_auth_id
            sess["interval"] = poll_interval
            sess["expires_in"] = 15 * 60  # OpenAI's effective limit
            sess["expires_at"] = time.time() + sess["expires_in"]

        # Step 2: poll until authorized
        deadline = time.time() + sess["expires_in"]
        code_resp = None
        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            while time.time() < deadline:
                time.sleep(poll_interval)
                poll = client.post(
                    f"{issuer}/api/accounts/deviceauth/token",
                    json={"device_auth_id": device_auth_id, "user_code": user_code},
                    headers={"Content-Type": "application/json"},
                )
                if poll.status_code == 200:
                    code_resp = poll.json()
                    break
                if poll.status_code in (403, 404):
                    continue  # user hasn't authorized yet
                raise RuntimeError(f"deviceauth/token poll returned {poll.status_code}")

        if code_resp is None:
            with _oauth_sessions_lock:
                sess["status"] = "expired"
                sess["error_message"] = "Device code expired before approval"
            return

        # Step 3: exchange authorization_code for tokens
        authorization_code = code_resp.get("authorization_code", "")
        code_verifier = code_resp.get("code_verifier", "")
        if not authorization_code or not code_verifier:
            raise RuntimeError("device-auth response missing authorization_code/code_verifier")
        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            token_resp = client.post(
                CODEX_OAUTH_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": authorization_code,
                    "redirect_uri": f"{issuer}/deviceauth/callback",
                    "client_id": CODEX_OAUTH_CLIENT_ID,
                    "code_verifier": code_verifier,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if token_resp.status_code != 200:
            raise RuntimeError(f"token exchange returned {token_resp.status_code}")
        tokens = token_resp.json()
        access_token = tokens.get("access_token", "")
        refresh_token = tokens.get("refresh_token", "")
        if not access_token:
            raise RuntimeError("token exchange did not return access_token")

        # Persist via credential pool — same shape as auth_commands.add_command
        from agent.credential_pool import (
            PooledCredential,
            load_pool,
            AUTH_TYPE_OAUTH,
            SOURCE_MANUAL,
        )
        import uuid as _uuid
        pool = load_pool("openai-codex")
        base_url = (
            os.getenv("DACLAW_CODEX_BASE_URL", "").strip().rstrip("/")
            or DEFAULT_CODEX_BASE_URL
        )
        entry = PooledCredential(
            provider="openai-codex",
            id=_uuid.uuid4().hex[:6],
            label="dashboard device_code",
            auth_type=AUTH_TYPE_OAUTH,
            priority=0,
            source=f"{SOURCE_MANUAL}:dashboard_device_code",
            access_token=access_token,
            refresh_token=refresh_token,
            base_url=base_url,
        )
        pool.add_entry(entry)
        with _oauth_sessions_lock:
            sess["status"] = "approved"
        _log.info("oauth/device: openai-codex login completed (session=%s)", session_id)
    except Exception as e:
        _log.warning("codex device-code worker failed (session=%s): %s", session_id, e)
        with _oauth_sessions_lock:
            s = _oauth_sessions.get(session_id)
            if s:
                s["status"] = "error"
                s["error_message"] = str(e)


@app.post("/api/providers/oauth/{provider_id}/start")
async def start_oauth_login(provider_id: str, request: Request):
    """Initiate an OAuth login flow. Token-protected."""
    _require_token(request)
    _gc_oauth_sessions()
    valid = {p["id"] for p in _OAUTH_PROVIDER_CATALOG}
    if provider_id not in valid:
        raise HTTPException(status_code=400, detail=f"Unknown provider {provider_id}")
    catalog_entry = next(p for p in _OAUTH_PROVIDER_CATALOG if p["id"] == provider_id)
    if catalog_entry["flow"] == "external":
        raise HTTPException(
            status_code=400,
            detail=f"{provider_id} uses an external CLI; run `{catalog_entry['cli_command']}` manually",
        )
    try:
        if catalog_entry["flow"] == "pkce":
            return _start_anthropic_pkce()
        if catalog_entry["flow"] == "device_code":
            return await _start_device_code_flow(provider_id)
    except HTTPException:
        raise
    except Exception as e:
        _log.exception("oauth/start %s failed", provider_id)
        raise HTTPException(status_code=500, detail=str(e))
    raise HTTPException(status_code=400, detail="Unsupported flow")


class OAuthSubmitBody(BaseModel):
    session_id: str
    code: str


@app.post("/api/providers/oauth/{provider_id}/submit")
async def submit_oauth_code(provider_id: str, body: OAuthSubmitBody, request: Request):
    """Submit the auth code for PKCE flows. Token-protected."""
    _require_token(request)
    if provider_id == "anthropic":
        return await asyncio.get_event_loop().run_in_executor(
            None, _submit_anthropic_pkce, body.session_id, body.code,
        )
    raise HTTPException(status_code=400, detail=f"submit not supported for {provider_id}")


@app.get("/api/providers/oauth/{provider_id}/poll/{session_id}")
async def poll_oauth_session(provider_id: str, session_id: str):
    """Poll a device-code session's status (no auth — read-only state)."""
    with _oauth_sessions_lock:
        sess = _oauth_sessions.get(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found or expired")
    if sess["provider"] != provider_id:
        raise HTTPException(status_code=400, detail="Provider mismatch for session")
    return {
        "session_id": session_id,
        "status": sess["status"],
        "error_message": sess.get("error_message"),
        "expires_at": sess.get("expires_at"),
    }


@app.delete("/api/providers/oauth/sessions/{session_id}")
async def cancel_oauth_session(session_id: str, request: Request):
    """Cancel a pending OAuth session. Token-protected."""
    _require_token(request)
    with _oauth_sessions_lock:
        sess = _oauth_sessions.pop(session_id, None)
    if sess is None:
        return {"ok": False, "message": "session not found"}
    return {"ok": True, "session_id": session_id}


# ---------------------------------------------------------------------------
# Session detail endpoints
# ---------------------------------------------------------------------------


@app.get("/api/sessions/{session_id}")
async def get_session_detail(session_id: str):
    from daclaw_state import SessionDB
    db = SessionDB()
    try:
        sid = db.resolve_session_id(session_id)
        session = db.get_session(sid) if sid else None
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        return session
    finally:
        db.close()


@app.get("/api/sessions/{session_id}/messages")
async def get_session_messages(session_id: str):
    from daclaw_state import SessionDB
    db = SessionDB()
    try:
        sid = db.resolve_session_id(session_id)
        if not sid:
            raise HTTPException(status_code=404, detail="Session not found")
        messages = db.get_messages(sid)
        return {"session_id": sid, "messages": messages}
    finally:
        db.close()


@app.delete("/api/sessions/{session_id}")
async def delete_session_endpoint(session_id: str):
    from daclaw_state import SessionDB
    db = SessionDB()
    try:
        if not db.delete_session(session_id):
            raise HTTPException(status_code=404, detail="Session not found")
        return {"ok": True}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Web chat endpoints
# ---------------------------------------------------------------------------


_CHAT_UPLOAD_ROOT = get_daclaw_home() / "web_uploads"
_CHAT_UPLOAD_DEFAULT_MAX_MB = 1024
_CHAT_EVENT_RESULT_LIMIT = 1200
_CHAT_EVENT_ARGS_LIMIT = 500
_CHAT_COMMAND_PREVIEW_LIMIT = 180
_CHAT_TRACE_LIMIT = 40
_CHAT_EVENT_QUEUE_LIMIT = 200
_CHAT_HEARTBEAT_SECONDS = 10
_CHAT_TOTAL_TIMEOUT_SECONDS = 1800
_CHAT_RUN_EVENT_LIMIT = 1000
_CHAT_RUN_RETENTION_SECONDS = 2 * 60 * 60
_CHAT_RUN_LIMIT = 50
_CHAT_RUNS: Dict[str, Dict[str, Any]] = {}
_CHAT_RUN_LOCK = threading.RLock()
_WEB_AGENT_DEFAULT_TOOLSETS = [
    "data",
    "file",
    "terminal",
    "code_execution",
    "skills",
    "memory",
    "session_search",
    "web",
    "vision",
    "image_gen",
    "todo",
    "delegation",
]
_SQLITE_SUFFIXES = {".sqlite", ".sqlite3", ".db", ".db3"}
_WEB_WORKSPACE_STATE = get_daclaw_home() / "web_workspace.json"


class ChatAttachment(BaseModel):
    id: str
    name: str
    path: str
    size: int = 0
    content_type: str = ""


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    message: str
    history: List[ChatMessage] = Field(default_factory=list)
    attachments: List[ChatAttachment] = Field(default_factory=list)
    session_id: Optional[str] = None


class DaAgentRequest(BaseModel):
    """Request for the scoped Data Agent chat (/api/da/agent)."""
    message: str
    history: List[ChatMessage] = Field(default_factory=list)
    session_id: Optional[str] = None
    workflow: Optional[Dict[str, Any]] = None


class WorkspaceRootRequest(BaseModel):
    path: str


def _safe_upload_name(filename: str) -> str:
    """Return a filesystem-safe filename while keeping it readable."""
    raw = Path(PureWindowsPath(filename or "upload").name).name.strip() or "upload"
    safe = unicodedata.normalize("NFC", raw)
    safe = re.sub(r"[\x00-\x1f\x7f/\\:]+", "_", safe).strip(" .")
    return safe[:160] or "upload"


def _unique_workspace_upload_target(root: Path, filename: str) -> Path:
    """Return a readable, non-colliding target path in the workspace root."""
    target = root / filename
    if not target.exists():
        return target

    stem = Path(filename).stem or "upload"
    suffix = Path(filename).suffix
    for index in range(2, 10_000):
        candidate = root / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
    raise HTTPException(status_code=409, detail="Could not allocate a unique upload filename")


def _chat_upload_max_bytes() -> int:
    raw = os.getenv("DACLAW_WEB_UPLOAD_MAX_MB", str(_CHAT_UPLOAD_DEFAULT_MAX_MB))
    try:
        max_mb = int(raw)
    except ValueError:
        max_mb = _CHAT_UPLOAD_DEFAULT_MAX_MB
    return max(1, max_mb) * 1024 * 1024


def _chat_heartbeat_seconds() -> float:
    raw = os.getenv("DACLAW_WEB_CHAT_HEARTBEAT_SECONDS", str(_CHAT_HEARTBEAT_SECONDS))
    try:
        return max(2.0, float(raw))
    except ValueError:
        return float(_CHAT_HEARTBEAT_SECONDS)


def _chat_total_timeout_seconds() -> float:
    raw = os.getenv("DACLAW_WEB_CHAT_TIMEOUT_SECONDS", str(_CHAT_TOTAL_TIMEOUT_SECONDS))
    try:
        value = float(raw)
    except ValueError:
        value = float(_CHAT_TOTAL_TIMEOUT_SECONDS)
    return max(0.0, value)


def _compact_value(value: Any, limit: int = _CHAT_EVENT_RESULT_LIMIT) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, indent=2)
    else:
        text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars]"


def _compact_args(args: Any, limit: int = _CHAT_EVENT_ARGS_LIMIT) -> Dict[str, Any]:
    if not args:
        return {}
    try:
        text = json.dumps(args, ensure_ascii=False)
    except Exception:
        text = str(args)
    if len(text) <= limit and isinstance(args, dict):
        return args
    if isinstance(args, dict):
        compact: Dict[str, Any] = {}
        if "command" in args:
            compact["command"] = _compact_value(args.get("command"), limit)
        if "path" in args:
            compact["path"] = args.get("path")
        if "target" in args:
            compact["target"] = args.get("target")
        if "pattern" in args:
            compact["pattern"] = args.get("pattern")
        if "timeout" in args:
            compact["timeout"] = args.get("timeout")
        compact["_truncated"] = f"Arguments truncated from {len(text)} chars."
        return compact
    return {"_truncated": _compact_value(text, limit)}


def _single_line_preview(value: Any, limit: int = _CHAT_COMMAND_PREVIEW_LIMIT) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [truncated {len(text) - limit} chars]"


def _tool_summary(name: str, args: Any = None) -> Dict[str, Any]:
    """Return a tiny, browser-safe tool summary for the chat process panel.

    This deliberately avoids shipping full terminal commands or stdout/stderr
    to the frontend. Large command payloads were the main reason the page could
    freeze during SQLite analysis runs.
    """
    if not isinstance(args, dict):
        args = {}
    lower_name = (name or "").lower()
    summary: Dict[str, Any] = {}

    if lower_name == "terminal":
        command = str(args.get("command") or "")
        lowered = command.lower()
        if ".sqlite" in lowered or ".sqlite3" in lowered or "sqlite3" in lowered:
            label = "SQLite 检查"
        elif "to_csv" in lowered or ".csv" in lowered or ".html" in lowered:
            label = "报告文件生成"
        elif "pandas" in lowered or "read_sql" in lowered or "dataframe" in lowered:
            label = "数据分析"
        elif "python" in lowered or "python3" in lowered:
            label = "Python 分析"
        elif re.search(r"(^|\s)(rg|find)\s", lowered):
            label = "文件搜索"
        elif re.search(r"(^|\s)(head|tail|cat|sed)\s", lowered):
            label = "文件预览"
        else:
            label = "终端命令"
        summary["summary"] = label
        preview = _single_line_preview(command)
        if preview:
            summary["preview"] = preview
        if args.get("timeout") is not None:
            summary["args"] = {"timeout": args.get("timeout")}
        return summary

    friendly = {
        "write_file": "文件写入",
        "read_file": "文件读取",
        "search_files": "文件搜索",
        "patch": "文件修改",
        "todo": "任务规划",
        "skill_view": "技能查看",
    }.get(lower_name)
    if friendly:
        summary["summary"] = friendly
    return summary


def _detect_lightweight_result(result: Any, limit: int = 800) -> bool:
    if result is None:
        return False
    try:
        text = json.dumps(result, ensure_ascii=False)
    except Exception:
        text = str(result)
    if len(text) > limit:
        return False
    lowered = text.lower()
    # Terminal/file tools often wrap stdout or full file contents. Even when
    # short, those details are rarely worth rendering in the always-visible
    # process panel for large data-analysis runs.
    noisy_markers = ('"output"', '"stdout"', '"stderr"', '"content"', '"command"')
    return not any(marker in lowered for marker in noisy_markers)


def _should_record_chat_event(event: Dict[str, Any]) -> bool:
    event_type = event.get("type")
    if event_type in {"assistant_delta", "done", "heartbeat", "reasoning"}:
        return False
    if event_type == "tool_progress":
        return bool(event.get("is_error"))
    if event_type == "status":
        message = str(event.get("message") or "").lower()
        return bool(event.get("is_error")) or any(
            marker in message for marker in ("error", "failed", "exception", "timeout")
        )
    return True


def _chat_sse(event: Dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _chat_run_payload(run: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "run_id": run["id"],
        "status": run.get("status", "queued"),
        "created_at": run.get("created_at"),
        "updated_at": run.get("updated_at"),
        "next_seq": run.get("next_seq", 0),
        "session_id": run.get("session_id"),
        "final_response": run.get("final_response"),
        "error": run.get("error"),
    }


def _chat_run_cleanup_locked() -> None:
    now = time.time()
    stale_ids = [
        run_id
        for run_id, run in _CHAT_RUNS.items()
        if now - float(run.get("updated_at") or run.get("created_at") or now) > _CHAT_RUN_RETENTION_SECONDS
    ]
    for run_id in stale_ids:
        _CHAT_RUNS.pop(run_id, None)

    if len(_CHAT_RUNS) <= _CHAT_RUN_LIMIT:
        return
    ordered = sorted(
        _CHAT_RUNS.items(),
        key=lambda item: float(item[1].get("updated_at") or item[1].get("created_at") or 0),
    )
    for run_id, run in ordered:
        if len(_CHAT_RUNS) <= _CHAT_RUN_LIMIT:
            break
        if run.get("status") not in {"running", "queued"}:
            _CHAT_RUNS.pop(run_id, None)


def _chat_run_get(run_id: str) -> Dict[str, Any]:
    with _CHAT_RUN_LOCK:
        run = _CHAT_RUNS.get(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Chat run not found")
        return run


def _chat_run_append(run_id: str, event: Dict[str, Any], *, record: bool = True) -> Dict[str, Any]:
    with _CHAT_RUN_LOCK:
        run = _CHAT_RUNS.get(run_id)
        if not run:
            raise RuntimeError(f"Chat run not found: {run_id}")

        item = dict(event)
        item.setdefault("ts", time.time())
        item["run_id"] = run_id
        item["seq"] = int(run.get("next_seq") or 0)
        run["next_seq"] = item["seq"] + 1
        run["updated_at"] = item["ts"]

        event_type = item.get("type")
        if event_type == "run_started":
            run["status"] = "running"
        elif event_type == "done":
            run["status"] = "completed"
            run["session_id"] = item.get("session_id")
            run["final_response"] = item.get("final_response") or ""
        elif event_type == "error":
            run["status"] = "failed"
            run["error"] = item.get("message") or "Unknown error"

        if record and _should_record_chat_event(item):
            trace_item = {key: value for key, value in item.items() if key not in {"seq", "run_id"}}
            run["trace"].append(trace_item)
            if len(run["trace"]) > _CHAT_TRACE_LIMIT:
                del run["trace"][: len(run["trace"]) - _CHAT_TRACE_LIMIT]

        run["events"].append(item)
        if len(run["events"]) > _CHAT_RUN_EVENT_LIMIT:
            del run["events"][: len(run["events"]) - _CHAT_RUN_EVENT_LIMIT]
        return dict(item)


def _chat_run_events(run_id: str, after: int = -1) -> Dict[str, Any]:
    with _CHAT_RUN_LOCK:
        run = _CHAT_RUNS.get(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Chat run not found")
        events = [dict(event) for event in run["events"] if int(event.get("seq", -1)) > after]
        return {
            "run": _chat_run_payload(run),
            "events": events,
            "next_seq": int(run.get("next_seq") or 0),
        }


def _attachment_analysis_hint(item: ChatAttachment) -> Optional[str]:
    suffix = Path(item.name or item.path).suffix.lower()
    if suffix in _SQLITE_SUFFIXES:
        return (
            "This upload is a SQLite database. Analyze it with Python's sqlite3 module "
            "or the sqlite CLI in read-only mode when possible. Inspect sqlite_master, "
            "PRAGMA table_info, row counts, and small sample rows instead of reading the "
            "database file as plain text or loading the entire database into memory."
        )
    return None


def _wsl_path_for_windows_path(path: str) -> Optional[str]:
    try:
        win_path = PureWindowsPath(path)
    except Exception:
        return None
    if not win_path.drive:
        return None
    drive = win_path.drive.rstrip(":").lower()
    parts = [part for part in win_path.parts[1:] if part not in {"\\", "/"}]
    if not drive or not parts:
        return None
    return f"/mnt/{drive}/" + "/".join(parts)


def _normalize_workspace_path(path_text: str) -> Path:
    raw = (path_text or "").strip().strip('"').strip("'")
    if not raw:
        raise HTTPException(status_code=400, detail="Workspace path is required")

    terminal_path = _wsl_path_for_windows_path(raw)
    if terminal_path:
        raw = terminal_path

    expanded = os.path.expandvars(os.path.expanduser(raw))
    path = Path(expanded)
    if not path.is_absolute():
        raise HTTPException(status_code=400, detail="Workspace path must be absolute")
    resolved = path.resolve()
    if not resolved.exists():
        raise HTTPException(status_code=404, detail=f"Workspace path does not exist: {resolved}")
    if not resolved.is_dir():
        raise HTTPException(status_code=400, detail=f"Workspace path is not a directory: {resolved}")
    return resolved


def _default_workspace_root() -> Path:
    return _CHAT_UPLOAD_ROOT.resolve()


def _load_workspace_root() -> Path:
    env_root = os.getenv("DACLAW_WEB_WORKSPACE_ROOT", "").strip()
    if env_root:
        try:
            return _normalize_workspace_path(env_root)
        except HTTPException:
            _log.warning("Ignoring invalid DACLAW_WEB_WORKSPACE_ROOT=%r", env_root)

    try:
        if _WEB_WORKSPACE_STATE.exists():
            data = json.loads(_WEB_WORKSPACE_STATE.read_text(encoding="utf-8"))
            root = str(data.get("root") or "").strip()
            if root:
                return _normalize_workspace_path(root)
    except Exception as exc:
        _log.warning("Failed to load web workspace root: %s", exc)

    root = _default_workspace_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _save_workspace_root(path: Path) -> None:
    _WEB_WORKSPACE_STATE.parent.mkdir(parents=True, exist_ok=True)
    _WEB_WORKSPACE_STATE.write_text(
        json.dumps({"root": str(path), "updated_at": time.time()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _pick_workspace_root_native() -> Optional[Path]:
    script = r"""
Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = 'Select DAClaw workspace folder'
$dialog.ShowNewFolderButton = $true
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
  Write-Output $dialog.SelectedPath
}
"""
    errors: List[str] = []
    for executable in ("powershell.exe", "pwsh.exe"):
        try:
            result = subprocess.run(
                [executable, "-NoProfile", "-STA", "-Command", script],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=600,
            )
        except FileNotFoundError:
            errors.append(f"{executable} not found")
            continue
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(status_code=504, detail="Folder picker timed out") from exc

        if result.returncode == 0:
            selected = result.stdout.strip().splitlines()[-1].strip() if result.stdout.strip() else ""
            return _normalize_workspace_path(selected) if selected else None
        errors.append((result.stderr or result.stdout or f"{executable} exited with {result.returncode}").strip())

    raise HTTPException(
        status_code=501,
        detail="Native folder picker is unavailable. Use the path input as a fallback. " + " | ".join(errors),
    )


def _workspace_root_payload(root: Optional[Path] = None) -> Dict[str, Any]:
    active = (root or _workspace_upload_root()).resolve()
    return {
        "root": str(active),
        "default_root": str(_default_workspace_root()),
        "terminal_cwd": str(active),
        "exists": active.exists() and active.is_dir(),
    }


def _build_chat_user_message(body: ChatRequest) -> str:
    message = body.message.strip()
    workspace_root = _workspace_upload_root()
    workspace_context = [
        f"Current DAClaw web workspace root: {workspace_root}",
        "Treat this workspace root as the active analysis scope for all local file, dataset, report, and dashboard work.",
        "Before saying a file is missing, inspect or list files under this workspace root. Do not fall back to the process cwd or home directory unless the user explicitly asks for another path.",
        "When writing outputs, save them inside this workspace root unless the user gives a different target path.",
        "For SQLite databases, prefer Python's built-in sqlite3 module in a single analysis script. Do not install system packages or rely on the sqlite3 command-line program unless the user explicitly asks.",
        "For CSV/TSV files, prefer data_profile for quick profiling, then use Python scripts for deeper analysis. Reserve terminal for builds, scripts, package management, or tasks that truly need a shell.",
    ]
    if not body.attachments:
        return "\n".join([message, "", *workspace_context]).strip()

    lines = [
        message,
        "",
        *workspace_context,
        "",
        "Uploaded files available in the local workspace for this web chat:",
    ]
    for item in body.attachments:
        terminal_path = _wsl_path_for_windows_path(item.path)
        lines.append(
            f"- {item.name} ({item.size} bytes, {item.content_type or 'unknown type'}): {item.path}"
        )
        if terminal_path:
            lines.append(f"  Terminal path if the shell is Linux/WSL: {terminal_path}")
        hint = _attachment_analysis_hint(item)
        if hint:
            lines.append(f"  Analysis hint: {hint}")
    lines.append(
        "Use the exact provided upload path when inspecting files. If the terminal runs in Linux/WSL, "
        "use the Terminal path above. Do not search the filesystem root for uploaded files."
    )
    lines.append(
        "For data-analysis tasks in web chat, you have a dedicated DataAgent toolchain — "
        "PREFER IT over hand-writing HTML/SVG:\n"
        "  1. data_connect(source=<the upload path>) to load a CSV/Excel/Parquet/SQLite/DuckDB file.\n"
        "  2. When a table is first uploaded, call data_dashboard(title=..., table=<table>) to "
        "instantly produce a beautiful overview dashboard (KPIs + trend + category charts).\n"
        "  3. To answer a specific question WITH a chart, call data_visualize(sql=..., title=...) — "
        "chart type auto-selects; the result is a self-contained interactive ECharts HTML that "
        "previews directly in the workspace.\n"
        "  4. To produce a full report, call data_document(title=..., sections=[...]) — it returns a "
        "polished self-contained HTML report (and optional PDF/DOCX via format=).\n"
        "Every number MUST come from a real data_query/data_visualize result — never estimate. "
        "These tools write their HTML into the workspace automatically, so tell the user the "
        "generated file name and summarize findings in chat. Only fall back to hand-written "
        "HTML/CSS/SVG if the DataAgent tools are unavailable."
    )
    return "\n".join(lines).strip()


_WORKSPACE_TEXT_SUFFIXES = {
    ".csv",
    ".tsv",
    ".txt",
    ".md",
    ".json",
    ".jsonl",
    ".log",
    ".sql",
    ".py",
    ".r",
    ".yaml",
    ".yml",
}
_WORKSPACE_HTML_SUFFIXES = {".html", ".htm"}
_WORKSPACE_TABLE_SUFFIXES = {".csv", ".tsv"}
_WORKSPACE_DOCUMENT_SUFFIXES = {".pdf", ".doc", ".docx", ".ppt", ".pptx"}
_WORKSPACE_MAX_TEXT_PREVIEW = 80_000
_WORKSPACE_MAX_HTML_PREVIEW = 2_000_000
_WORKSPACE_FILE_LIMIT = 500
_WORKSPACE_TEXT_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "gbk", "big5")
_WORKSPACE_LEGACY_UPLOAD_DIR = "uploads"
_WORKSPACE_LEGACY_UPLOAD_ID_RE = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)
_WORKSPACE_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}


def _workspace_decode_bytes(raw: bytes, limit: Optional[int] = None) -> Dict[str, Any]:
    """Decode text previews with Chinese-friendly fallbacks."""
    sample = raw if limit is None else raw[:limit]
    for encoding in _WORKSPACE_TEXT_ENCODINGS:
        try:
            return {"text": sample.decode(encoding), "encoding": encoding, "lossy": False}
        except UnicodeDecodeError:
            continue
    return {"text": sample.decode("utf-8", errors="replace"), "encoding": "utf-8", "lossy": True}


def _workspace_is_legacy_upload_hash_dir(path: Path, root: Path) -> bool:
    try:
        rel = path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return (
        len(rel.parts) == 2
        and rel.parts[0] == _WORKSPACE_LEGACY_UPLOAD_DIR
        and bool(_WORKSPACE_LEGACY_UPLOAD_ID_RE.fullmatch(rel.parts[1]))
        and path.is_dir()
    )


def _workspace_legacy_upload_files(path: Path, root: Path) -> List[Path]:
    if not _workspace_is_legacy_upload_hash_dir(path, root):
        return []
    try:
        return [child for child in path.iterdir() if child.is_file() and not child.name.startswith(".")]
    except OSError:
        return []


def _workspace_upload_root() -> Path:
    return _load_workspace_root()


def _workspace_encode_file_id(path: Path) -> str:
    rel = path.resolve().relative_to(_workspace_upload_root())
    rel_text = rel.as_posix()
    encoded = base64.urlsafe_b64encode(rel_text.encode("utf-8")).decode("ascii")
    return encoded.rstrip("=")


def _workspace_decode_file_id(file_id: str) -> Path:
    try:
        padded = file_id + ("=" * (-len(file_id) % 4))
        rel_text = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Workspace file not found") from exc

    rel_path = Path(rel_text)
    if rel_path.is_absolute() or ".." in rel_path.parts:
        raise HTTPException(status_code=404, detail="Workspace file not found")

    root = _workspace_upload_root()
    path = (root / rel_path).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Workspace file not found") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Workspace file not found")
    return path


def _workspace_file_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    name = path.name.lower()
    if suffix in _SQLITE_SUFFIXES:
        return "database"
    if suffix in _WORKSPACE_TABLE_SUFFIXES or suffix in {".xlsx", ".xls", ".parquet"}:
        return "dataset"
    if suffix in _WORKSPACE_HTML_SUFFIXES:
        return "dashboard" if "dashboard" in name else "report"
    if suffix in _WORKSPACE_DOCUMENT_SUFFIXES:
        return "document"
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}:
        return "visual"
    return "artifact"


def _workspace_previewable(path: Path) -> bool:
    suffix = path.suffix.lower()
    return (
        suffix in _WORKSPACE_TEXT_SUFFIXES
        or suffix in _WORKSPACE_HTML_SUFFIXES
        or suffix in _SQLITE_SUFFIXES
    )


def _workspace_file_record(path: Path) -> Dict[str, Any]:
    stat = path.stat()
    rel = path.resolve().relative_to(_workspace_upload_root())
    upload_id = rel.parts[0] if rel.parts else ""
    content_type = mimetypes.guess_type(path.name)[0] or ""
    return {
        "id": _workspace_encode_file_id(path),
        "upload_id": upload_id,
        "name": path.name,
        "relative_path": rel.as_posix(),
        "path": str(path),
        "size": stat.st_size,
        "modified_at": stat.st_mtime,
        "created_at": stat.st_ctime,
        "suffix": path.suffix.lower(),
        "kind": _workspace_file_kind(path),
        "content_type": content_type,
        "previewable": _workspace_previewable(path),
    }


def _workspace_normalize_folder(folder: str = "") -> Path:
    root = _workspace_upload_root()
    folder_text = (folder or "").strip().strip("/\\")
    rel = Path(folder_text) if folder_text else Path()
    if rel.is_absolute() or ".." in rel.parts:
        raise HTTPException(status_code=400, detail="Invalid workspace folder")
    target = (root / rel).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid workspace folder") from exc
    if not target.exists() or not target.is_dir():
        raise HTTPException(status_code=404, detail="Workspace folder not found")
    return target.relative_to(root)


def _workspace_directory_record(path: Path) -> Dict[str, Any]:
    root = _workspace_upload_root()
    stat = path.stat()
    rel = path.resolve().relative_to(root)
    child_files = 0
    child_dirs = 0
    try:
        for child in path.iterdir():
            if child.name.startswith("."):
                continue
            if child.is_dir() and child.name not in _WORKSPACE_SKIP_DIRS:
                child_dirs += 1
            elif child.is_file():
                child_files += 1
    except OSError:
        pass
    return {
        "name": path.name,
        "relative_path": rel.as_posix(),
        "path": str(path),
        "modified_at": stat.st_mtime,
        "file_count": child_files,
        "directory_count": child_dirs,
    }


def _workspace_browse_directory_record(path: Path) -> Dict[str, Any]:
    stat = path.stat()
    child_files = 0
    child_dirs = 0
    try:
        for child in path.iterdir():
            if child.name.startswith("."):
                continue
            if child.is_dir() and child.name not in _WORKSPACE_SKIP_DIRS:
                child_dirs += 1
            elif child.is_file():
                child_files += 1
    except OSError:
        pass
    return {
        "name": path.name or str(path),
        "path": str(path),
        "modified_at": stat.st_mtime,
        "file_count": child_files,
        "directory_count": child_dirs,
    }


def _workspace_browse_root_candidates() -> List[Dict[str, str]]:
    candidates: List[Path] = [
        _workspace_upload_root(),
        Path.cwd(),
        Path.home(),
        _default_workspace_root(),
    ]
    for raw in ("/mnt/c/D/Projects", "/mnt/c/D", "/mnt/c/Users", "/mnt/c", "/"):
        candidates.append(Path(raw))
    mnt = Path("/mnt")
    try:
        candidates.extend(path for path in mnt.iterdir() if path.is_dir())
    except OSError:
        pass

    seen: set[str] = set()
    roots: List[Dict[str, str]] = []
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        key = str(resolved)
        if key in seen or not resolved.exists() or not resolved.is_dir():
            continue
        seen.add(key)
        roots.append({"name": resolved.name or key, "path": key})
    return roots


def _workspace_browse_directories(path_text: str = "") -> Dict[str, Any]:
    current = _normalize_workspace_path(path_text) if (path_text or "").strip() else _workspace_upload_root()
    directories: List[Path] = []
    try:
        for child in current.iterdir():
            if child.name.startswith(".") or child.name in _WORKSPACE_SKIP_DIRS:
                continue
            if child.is_dir():
                directories.append(child)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Cannot list folder: {exc}") from exc

    directories.sort(key=lambda item: item.name.lower())
    parent = None if current.parent == current else str(current.parent)
    return {
        "path": str(current),
        "parent": parent,
        "roots": _workspace_browse_root_candidates(),
        "directories": [_workspace_browse_directory_record(path) for path in directories],
    }


def _workspace_list_files(folder: str = "") -> Dict[str, Any]:
    root = _workspace_upload_root()
    if not root.exists():
        return {
            "folder": "",
            "current_path": str(root),
            "parent": None,
            "directories": [],
            "files": [],
            "summary": {},
            "truncated": False,
        }

    rel_folder = _workspace_normalize_folder(folder)
    current_dir = (root / rel_folder).resolve()
    directories: List[Path] = []
    files: List[Path] = []
    truncated = False

    try:
        for child in current_dir.iterdir():
            if child.name.startswith("."):
                continue
            if child.is_dir():
                if child.name in _WORKSPACE_SKIP_DIRS:
                    continue
                legacy_files = _workspace_legacy_upload_files(child, root)
                if legacy_files:
                    for legacy_file in legacy_files:
                        if len(files) >= _WORKSPACE_FILE_LIMIT:
                            truncated = True
                            continue
                        files.append(legacy_file)
                    continue
                directories.append(child)
            elif child.is_file():
                if len(files) >= _WORKSPACE_FILE_LIMIT:
                    truncated = True
                    continue
                files.append(child)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Cannot list workspace folder: {exc}") from exc

    directories.sort(key=lambda item: item.name.lower())
    files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    records = [_workspace_file_record(path) for path in files[:_WORKSPACE_FILE_LIMIT]]
    summary: Dict[str, int] = {}
    for item in records:
        kind = str(item.get("kind") or "artifact")
        summary[kind] = summary.get(kind, 0) + 1

    parent = None
    if rel_folder.parts:
        parent = Path(*rel_folder.parts[:-1]).as_posix() if len(rel_folder.parts) > 1 else ""

    rel_folder_text = rel_folder.as_posix() if rel_folder.parts else ""

    return {
        "folder": rel_folder_text,
        "current_path": str(current_dir),
        "parent": parent,
        "directories": [_workspace_directory_record(path) for path in directories],
        "files": records,
        "summary": summary,
        "truncated": truncated,
    }


def _workspace_quote_sqlite_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _workspace_preview_text(path: Path, limit: int = _WORKSPACE_MAX_TEXT_PREVIEW) -> Dict[str, Any]:
    raw = path.read_bytes()
    truncated = len(raw) > limit
    decoded = _workspace_decode_bytes(raw, limit)
    text = str(decoded["text"])
    return {
        "type": "text",
        "text": text,
        "truncated": truncated,
        "line_count": text.count("\n") + (1 if text else 0),
        "encoding": decoded["encoding"],
        "lossy": decoded["lossy"],
    }


def _workspace_preview_delimited(path: Path) -> Dict[str, Any]:
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    raw = path.read_bytes()
    decoded = _workspace_decode_bytes(raw, _WORKSPACE_MAX_TEXT_PREVIEW)
    reader = csv.reader(io.StringIO(str(decoded["text"])), delimiter=delimiter)
    try:
        columns = next(reader)
    except StopIteration:
        return {
            "type": "table",
            "columns": [],
            "rows": [],
            "truncated": False,
            "encoding": decoded["encoding"],
            "lossy": decoded["lossy"],
        }
    rows: List[List[str]] = []
    for index, row in enumerate(reader):
        if index >= 80:
            break
        rows.append(row)
    return {
        "type": "table",
        "columns": columns,
        "rows": rows,
        "truncated": len(rows) >= 80 or len(raw) > _WORKSPACE_MAX_TEXT_PREVIEW,
        "encoding": decoded["encoding"],
        "lossy": decoded["lossy"],
    }


def _workspace_preview_json(path: Path) -> Dict[str, Any]:
    text_preview = _workspace_preview_text(path)
    try:
        parsed = json.loads(text_preview["text"])
        pretty = json.dumps(parsed, ensure_ascii=False, indent=2)
        if len(pretty) <= _WORKSPACE_MAX_TEXT_PREVIEW:
            text_preview["text"] = pretty
            text_preview["truncated"] = False
    except Exception:
        pass
    text_preview["type"] = "json"
    return text_preview


def _workspace_preview_html(path: Path) -> Dict[str, Any]:
    raw = path.read_bytes()
    truncated = len(raw) > _WORKSPACE_MAX_HTML_PREVIEW
    decoded = _workspace_decode_bytes(raw, _WORKSPACE_MAX_HTML_PREVIEW)
    return {
        "type": "html",
        "html": decoded["text"],
        "truncated": truncated,
        "encoding": decoded["encoding"],
        "lossy": decoded["lossy"],
    }


def _workspace_preview_sqlite(path: Path) -> Dict[str, Any]:
    tables: List[Dict[str, Any]] = []
    db_uri = f"file:{path.as_posix()}?mode=ro"
    with sqlite3.connect(db_uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        table_rows = conn.execute(
            """
            SELECT name, type
            FROM sqlite_master
            WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            LIMIT 40
            """
        ).fetchall()
        for row in table_rows:
            table_name = str(row["name"])
            quoted = _workspace_quote_sqlite_identifier(table_name)
            columns = [
                {"name": str(col["name"]), "type": str(col["type"] or "")}
                for col in conn.execute(f"PRAGMA table_info({quoted})").fetchall()
            ]
            count: Optional[int] = None
            sample: List[Dict[str, Any]] = []
            try:
                count = int(conn.execute(f"SELECT COUNT(*) AS c FROM {quoted}").fetchone()["c"])
                sample = [
                    dict(item)
                    for item in conn.execute(f"SELECT * FROM {quoted} LIMIT 5").fetchall()
                ]
            except Exception:
                pass
            tables.append(
                {
                    "name": table_name,
                    "type": str(row["type"]),
                    "columns": columns,
                    "row_count": count,
                    "sample": sample,
                }
            )
    return {"type": "database", "tables": tables}


def _workspace_preview_xlsx(path: Path) -> Dict[str, Any]:
    """Preview an .xlsx as a multi-sheet 'database': each sheet → a table with
    its header row + a few sample rows. Reuses the engine's header detection so
    title banners don't get mistaken for headers."""
    try:
        import pandas as pd
    except Exception:
        return {"type": "unsupported", "message": "Excel preview needs pandas + openpyxl."}
    try:
        from agent.data_agent.engine import _reheader_excel_sheet  # header detection
    except Exception:
        _reheader_excel_sheet = None

    tables: List[Dict[str, Any]] = []
    try:
        raw_sheets = pd.read_excel(path, sheet_name=None, header=None)
    except Exception as exc:
        return {"type": "unsupported", "message": f"Could not read workbook: {exc}"}

    for sheet_name, raw in list(raw_sheets.items())[:20]:
        df = _reheader_excel_sheet(raw) if _reheader_excel_sheet else raw
        if df is None or df.empty:
            tables.append({"name": str(sheet_name), "type": "sheet",
                           "columns": [], "row_count": 0, "sample": []})
            continue
        columns = [{"name": str(c), "type": ""} for c in df.columns]
        sample: List[Dict[str, Any]] = []
        for _, row in df.head(8).iterrows():
            rec = {}
            for c in df.columns:
                v = row[c]
                if pd.isna(v):
                    v = None
                elif hasattr(v, "item"):
                    try:
                        v = v.item()
                    except Exception:
                        v = str(v)
                rec[str(c)] = v
            sample.append(rec)
        tables.append({
            "name": str(sheet_name),
            "type": "sheet",
            "columns": columns,
            "row_count": int(len(df)),
            "sample": sample,
        })
    return {"type": "database", "tables": tables}


def _workspace_preview_file(path: Path) -> Dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix in _WORKSPACE_TABLE_SUFFIXES:
        return _workspace_preview_delimited(path)
    if suffix in {".xlsx", ".xls", ".xlsm"}:
        return _workspace_preview_xlsx(path)
    if suffix in _SQLITE_SUFFIXES:
        return _workspace_preview_sqlite(path)
    if suffix in _WORKSPACE_HTML_SUFFIXES:
        return _workspace_preview_html(path)
    if suffix == ".json":
        return _workspace_preview_json(path)
    if suffix in _WORKSPACE_TEXT_SUFFIXES:
        return _workspace_preview_text(path)
    return {
        "type": "unsupported",
        "message": "Preview is not available for this file type yet.",
    }


@app.get("/api/workspace/files")
async def list_workspace_files(folder: str = ""):
    listing = _workspace_list_files(folder)
    return {
        "root": str(_workspace_upload_root()),
        "folder": listing["folder"],
        "current_path": listing["current_path"],
        "parent": listing["parent"],
        "directories": listing["directories"],
        "files": listing["files"],
        "summary": listing["summary"],
        "truncated": listing["truncated"],
        "limit": _WORKSPACE_FILE_LIMIT,
    }


@app.get("/api/workspace/root")
async def get_workspace_root():
    return _workspace_root_payload()


@app.put("/api/workspace/root")
async def set_workspace_root(body: WorkspaceRootRequest):
    root = _normalize_workspace_path(body.path)
    _save_workspace_root(root)
    os.environ["TERMINAL_CWD"] = str(root)
    os.environ["MESSAGING_CWD"] = str(root)
    return _workspace_root_payload(root)


@app.get("/api/workspace/directories")
async def browse_workspace_directories(path: str = ""):
    return _workspace_browse_directories(path)


@app.post("/api/workspace/root/pick")
async def pick_workspace_root():
    # Native Windows folder dialogs are unreliable when the dashboard is served
    # from a hidden WSL/background process. The frontend now uses the
    # non-blocking /api/workspace/directories browser instead; keep this endpoint
    # fast so older cached clients do not spin forever.
    payload = _workspace_root_payload()
    payload["cancelled"] = True
    payload["detail"] = "Use /api/workspace/directories to choose a workspace folder."
    return payload


@app.get("/api/workspace/files/{file_id}/preview")
async def preview_workspace_file(file_id: str):
    path = _workspace_decode_file_id(file_id)
    return {
        "file": _workspace_file_record(path),
        "preview": _workspace_preview_file(path),
    }


@app.get("/api/workspace/files/{file_id}/download")
async def download_workspace_file(file_id: str):
    path = _workspace_decode_file_id(file_id)
    return FileResponse(path, filename=path.name)


@app.delete("/api/workspace/files/{file_id}")
async def delete_workspace_file(file_id: str):
    path = _workspace_decode_file_id(file_id)
    try:
        path.unlink()
        return {"ok": True}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Workspace file not found") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to delete workspace file: {exc}") from exc


def _history_for_agent(history: List[ChatMessage]) -> List[Dict[str, str]]:
    allowed = {"user", "assistant", "system", "tool"}
    result: List[Dict[str, str]] = []
    for msg in history[-40:]:
        role = msg.role if msg.role in allowed else "user"
        content = (msg.content or "").strip()
        if content:
            result.append({"role": role, "content": content})
    return result


def _create_web_chat_agent(
    *,
    session_id: Optional[str],
    stream_delta_callback=None,
    tool_progress_callback=None,
    tool_start_callback=None,
    tool_complete_callback=None,
    reasoning_callback=None,
    status_callback=None,
    step_callback=None,
    enabled_toolsets_override: Optional[List[str]] = None,
):
    """Create an AIAgent for the dashboard chat without changing core logic.

    ``enabled_toolsets_override`` pins a specific toolset (e.g. the Data Agent
    chat uses ``["data-agent-chat"]``) instead of the configured/default web
    toolsets — used by the scoped /api/da/agent surface.
    """
    from run_agent import AIAgent
    from gateway.run import (
        GatewayRunner,
        _resolve_gateway_model,
        _resolve_runtime_agent_kwargs,
    )

    workspace_root = _workspace_upload_root()
    os.environ["TERMINAL_CWD"] = str(workspace_root)
    os.environ["MESSAGING_CWD"] = str(workspace_root)
    config = load_config()
    runtime_kwargs = _resolve_runtime_agent_kwargs()
    model = _resolve_gateway_model(config)
    if not model:
        try:
            from daclaw_cli.models import get_default_model_for_provider
            model = get_default_model_for_provider(runtime_kwargs.get("provider", ""))
        except Exception:
            model = ""
    if enabled_toolsets_override:
        enabled_toolsets = list(enabled_toolsets_override)
    else:
        web_config = config.get("web") if isinstance(config.get("web"), dict) else {}
        configured_toolsets = web_config.get("agent_toolsets") if isinstance(web_config, dict) else None
        if isinstance(configured_toolsets, list) and configured_toolsets:
            enabled_toolsets = sorted(str(item) for item in configured_toolsets if str(item).strip())
        else:
            enabled_toolsets = list(_WEB_AGENT_DEFAULT_TOOLSETS)
    max_iterations = int(
        (config.get("agent") or {}).get("max_turns")
        or os.getenv("DACLAW_MAX_ITERATIONS", "90")
        or 90
    )

    try:
        from daclaw_state import SessionDB
        session_db = SessionDB()
    except Exception:
        session_db = None

    agent = AIAgent(
        model=model,
        **runtime_kwargs,
        max_iterations=max_iterations,
        quiet_mode=True,
        verbose_logging=False,
        enabled_toolsets=enabled_toolsets,
        session_id=session_id,
        platform="web",
        stream_delta_callback=stream_delta_callback,
        tool_progress_callback=tool_progress_callback,
        tool_start_callback=tool_start_callback,
        tool_complete_callback=tool_complete_callback,
        reasoning_callback=reasoning_callback,
        status_callback=status_callback,
        step_callback=step_callback,
        session_db=session_db,
        fallback_model=GatewayRunner._load_fallback_model(),
    )
    # Web chat cannot currently resume the internal repair loop if the browser
    # refreshes or the SSE connection drops.  Keep the core analysis path intact
    # but skip the optional final audit/repair pass for this surface so a
    # completed report is delivered promptly instead of hanging in a second LLM
    # call after the data work has already succeeded.
    agent._disable_analysis_audit = True
    return agent


def _da_agent_system_prompt() -> str:
    """Build the Data Agent system prompt with the live connected schema.

    Injected as ``system_message`` on the scoped /api/da/agent run so the model
    knows exactly which tables exist and is instructed to ground every number in
    a real query.
    """
    try:
        from agent.data_agent.engine import get_engine
        eng = get_engine()
        lines = []
        for t in eng.list_tables():
            try:
                cols = ", ".join(f'{c["name"]} {c["type"]}' for c in eng.table_schema(t))
                lines.append(f"- {t}({cols})")
            except Exception:
                lines.append(f"- {t}")
        schema = "\n".join(lines)
    except Exception:
        schema = ""
    if not schema:
        schema = "(no data connected yet — ask the user to upload a file first)"
    try:
        from agent.data_agent.knowledge import list_sources

        sources = list_sources()
        knowledge = "\n".join(f"- {item['name']} ({item['kind']}, id={item['source_id']})" for item in sources)
    except Exception:
        knowledge = ""
    if not knowledge:
        knowledge = "(no knowledge documents connected)"
    workspace_root = _workspace_upload_root()
    return (
        "You are DAClaw's Data Agent — a tightly data-focused analyst. You act "
        "ONLY through your data tools: data_workflow_plan, data_connect, "
        "data_knowledge_connect, data_knowledge_search, data_query, data_visualize, "
        "data_dashboard, data_document, data_story, data_excel, data_excel_edit, "
        "data_excel_formula, data_excel_fix, data_dbt (and data_profile). You never write "
        "arbitrary files or run shell/Python.\n\n"
        f"Currently connected tables:\n{schema}\n\n"
        f"Connected knowledge documents:\n{knowledge}\n\n"
        "Rules:\n"
        "- Answer questions by running SQL via data_query (or data_visualize for "
        "a chart). Every number MUST come from a real query result — never "
        "estimate or invent values.\n"
        "- When the user wants a chart/plot, call data_visualize; for a "
        "multi-panel overview call data_dashboard. When they want a written "
        "analysis or report, call data_document. When they want a NEW Excel / "
        "spreadsheet file (.xlsx), call data_excel. When they want to EDIT the "
        "workbook they uploaded (add a summary sheet, restyle/repair), call "
        "data_excel_edit — it preserves the original sheets.\n"
        "- When the request relies on policy documents, research, metric definitions, "
        "or market context, call data_knowledge_search and cite the returned source "
        "name alongside the structured-data evidence.\n"
        "- When the user asks for a presentation, narrated walkthrough, executive "
        "story, or data video, call data_story. It creates an animated, interactive "
        "HTML story from real query results.\n"
        "- When the user wants a FORMULA-DRIVEN, computable model (DCF, LBO, "
        "M&A accretion/dilution, sensitivity tables, any 'build a model with "
        "real formulas'), call data_excel_formula — it writes real Excel "
        "formulas and recalculates offline to PROVE the numbers compute. "
        "CRITICAL: the model must be fully live — ONLY input assumptions may be "
        "literal numbers; EVERY calculated cell (including sensitivity/scenario "
        "grids and every period of a cash-flow projection) MUST be a formula "
        "that references assumption cells. Never paste a pre-computed number "
        "into a calculated cell, and never bury a constant inside a formula — "
        "put it in a labeled assumption cell and reference it. "
        "When the user wants to FIX formula errors in an uploaded workbook "
        "(#REF!, #DIV/0!, #NUM!, broken cross-sheet refs, hardcoded numbers, "
        "circular refs), call data_excel_fix — first with no 'fixes' to AUDIT, "
        "then again with 'fixes' to repair and verify the errors are gone.\n"
        "- Use correct DuckDB SQL dialect. Quote identifiers with double quotes "
        "when they contain non-ASCII or spaces. Keep answers concise and "
        "reference the SQL you ran.\n"
        f"- Generated artifacts are saved under {workspace_root}/data_agent/.\n"
    )


def _da_artifact_from_result(name: str, result: Any) -> Optional[Dict[str, Any]]:
    """Turn a data_visualize/dashboard/document tool result into a structured
    artifact descriptor the frontend right-panel can render directly.

    Returns None for other tools, or when the artifact file cannot be resolved
    under the workspace root (in which case the trace still shows, but no panel
    opens — a graceful degradation that keeps the demo alive).
    """
    if name not in {"data_visualize", "data_dashboard", "data_document", "data_story", "data_excel", "data_excel_edit", "data_excel_formula", "data_excel_fix"}:
        return None
    # Tool handlers return a JSON *string* (tools.registry.tool_result), which is
    # what reaches this callback. Parse it; also tolerate a dict or a
    # {"result": ...} envelope in case the caller changes.
    payload: Any = result
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            return None
    if isinstance(payload, dict) and "result" in payload and isinstance(payload["result"], (str, dict)):
        inner = payload["result"]
        if isinstance(inner, str):
            try:
                inner = json.loads(inner)
            except Exception:
                inner = None
        if isinstance(inner, dict):
            payload = inner
    if not isinstance(payload, dict):
        return None
    # data_excel returns excel_path; the html-producing tools return html_path.
    file_path = payload.get("html_path") or payload.get("excel_path")
    workspace_file = payload.get("workspace_file")
    if not file_path or not workspace_file:
        return None
    try:
        file_id = _workspace_encode_file_id(Path(file_path))
    except Exception:
        # file is not under the workspace root — skip the panel.
        return None
    kind = {
        "data_visualize": "chart",
        "data_dashboard": "dashboard",
        "data_document": "document",
        "data_story": "story",
        "data_excel": "excel",
        "data_excel_edit": "excel",
        "data_excel_formula": "excel",
        "data_excel_fix": "excel",
    }[name]
    return {
        "kind": kind,
        "workspace_file": workspace_file,
        "file_id": file_id,
        # .xlsx cannot render in an iframe; the frontend shows a download card
        # for kind == "excel" and ignores preview_url.
        "preview_url": f"/api/workspace/files/{file_id}/preview",
        "download_url": f"/api/workspace/files/{file_id}/download",
        "artifact_id": payload.get("artifact_id"),
        "title": payload.get("title") or workspace_file,
        "row_count": payload.get("row_count"),
    }


@app.post("/api/chat/upload")
async def upload_chat_file(request: Request):
    """Store a user-uploaded file for DAClaw to inspect during web chat."""
    try:
        form = await request.form()
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=(
                "File upload parsing failed. Install the web extra with "
                "`pip install daclaw-agent[web]`."
            ),
        ) from exc

    upload = form.get("file")
    if upload is None or not hasattr(upload, "filename"):
        raise HTTPException(status_code=400, detail="Missing file")

    target_dir = _workspace_upload_root()
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = _safe_upload_name(getattr(upload, "filename", "") or "upload")
    target = _unique_workspace_upload_target(target_dir, filename)

    size = 0
    max_bytes = _chat_upload_max_bytes()
    with target.open("wb") as fh:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                try:
                    target.unlink(missing_ok=True)
                except Exception:
                    pass
                max_mb = max_bytes // (1024 * 1024)
                raise HTTPException(status_code=413, detail=f"File is larger than {max_mb} MB")
            fh.write(chunk)

    return {
        "id": _workspace_encode_file_id(target),
        "name": target.name,
        "path": str(target),
        "size": size,
        "content_type": getattr(upload, "content_type", "") or "",
    }


def _chat_run_worker(run_id: str) -> None:
    run = _chat_run_get(run_id)
    body: ChatRequest = run["body"]
    started = float(run["created_at"])
    delta_buffer = ""
    last_delta_push = started

    def push(event: Dict[str, Any], *, record: bool = True) -> Dict[str, Any]:
        return _chat_run_append(run_id, event, record=record)

    def flush_delta() -> None:
        nonlocal delta_buffer, last_delta_push
        if not delta_buffer:
            return
        push({"type": "assistant_delta", "delta": delta_buffer}, record=False)
        delta_buffer = ""
        last_delta_push = time.time()

    def on_delta(delta: Optional[str]) -> None:
        nonlocal delta_buffer
        if not delta:
            return None
        delta_buffer += str(delta)
        now = time.time()
        # Do not send every token. Coalescing keeps the browser responsive
        # while still making the report visibly stream in.
        if len(delta_buffer) >= 320 or now - last_delta_push >= 0.12:
            flush_delta()
        return None

    def on_reasoning(text: str) -> None:
        # Do not expose raw model reasoning in the web process panel.
        # Tool starts/completions below are the user-visible execution trace.
        return None

    def on_status(event_type: str, message: str) -> None:
        push({"type": "status", "event": event_type, "message": message})

    def on_step(iteration: int, prev_tools: list) -> None:
        push({"type": "step", "iteration": iteration, "previous_tools": prev_tools or []})

    def on_tool_progress(event_type: str, name: str, preview=None, args=None, **kwargs) -> None:
        is_error = bool(kwargs.get("is_error", False))
        if not is_error:
            return
        event: Dict[str, Any] = {
            "type": "tool_progress",
            "event": event_type,
            "name": name,
            "preview": _compact_value(preview, 1000) if preview is not None else "",
            "args": _compact_args(args),
            "duration": kwargs.get("duration"),
            "is_error": is_error,
        }
        event.update({k: v for k, v in _tool_summary(name, args).items() if k not in event or not event[k]})
        push(event)

    def on_tool_start(tool_id: str, name: str, args: dict) -> None:
        event: Dict[str, Any] = {"type": "tool_start", "tool_id": tool_id, "name": name}
        event.update(_tool_summary(name, args))
        if name not in {"terminal", "write_file", "patch"}:
            event["args"] = _compact_args(args)
        push(event)

    def on_tool_complete(tool_id: str, name: str, args: dict, result: Any) -> None:
        event: Dict[str, Any] = {
            "type": "tool_complete",
            "tool_id": tool_id,
            "name": name,
            "result": _compact_value(result, 800) if _detect_lightweight_result(result) else "",
        }
        event.update(_tool_summary(name, args))
        art = _da_artifact_from_result(name, result)
        if art:
            event["artifact"] = art
        push(event)

    try:
        push({"type": "run_started", "message": "DAClaw is working"})
        agent = _create_web_chat_agent(
            session_id=body.session_id,
            stream_delta_callback=on_delta,
            tool_progress_callback=on_tool_progress,
            tool_start_callback=on_tool_start,
            tool_complete_callback=on_tool_complete,
            reasoning_callback=on_reasoning,
            status_callback=on_status,
            step_callback=on_step,
            enabled_toolsets_override=run.get("enabled_toolsets"),
        )
        result = agent.run_conversation(
            _build_chat_user_message(body),
            conversation_history=_history_for_agent(body.history),
            system_message=run.get("system_message"),
        )
        flush_delta()
        with _CHAT_RUN_LOCK:
            trace = list(_CHAT_RUNS.get(run_id, {}).get("trace") or [])
        push({
            "type": "done",
            "final_response": result.get("final_response") or "",
            "session_id": getattr(agent, "session_id", None),
            "completed": bool(result.get("completed")),
            "api_calls": result.get("api_calls", 0),
            "elapsed": time.time() - started,
            "trace": trace,
        }, record=False)
    except Exception as exc:
        _log.exception("Chat run %s failed", run_id)
        push({
            "type": "error",
            "message": str(exc),
            "elapsed": time.time() - started,
        }, record=False)


def _start_chat_run(
    body: ChatRequest,
    *,
    enabled_toolsets: Optional[List[str]] = None,
    system_message: Optional[str] = None,
) -> Dict[str, Any]:
    if not body.message.strip() and not body.attachments:
        raise HTTPException(status_code=400, detail="Message or attachment is required")

    now = time.time()
    run_id = secrets.token_urlsafe(16)
    run = {
        "id": run_id,
        "body": body,
        "status": "queued",
        "created_at": now,
        "updated_at": now,
        "next_seq": 0,
        "session_id": body.session_id,
        "final_response": "",
        "error": None,
        "trace": [],
        "events": [],
        "enabled_toolsets": enabled_toolsets,
        "system_message": system_message,
    }
    with _CHAT_RUN_LOCK:
        _CHAT_RUNS[run_id] = run
        _chat_run_cleanup_locked()
    threading.Thread(target=_chat_run_worker, args=(run_id,), daemon=True).start()
    return run


@app.post("/api/chat/runs")
async def start_chat_run(body: ChatRequest):
    """Start a web-chat turn that can be resumed if the page disconnects."""
    run = _start_chat_run(body)
    return _chat_run_payload(run)


@app.post("/api/da/agent")
async def start_da_agent_run(body: DaAgentRequest):
    """Start a scoped Data Agent turn: agentic chat pinned to the
    ``data-agent-chat`` toolset, grounded in the connected-table schema.

    Reuses the same run store + polling endpoint as /api/chat/runs; read
    events via GET /api/chat/runs/{run_id}/events.
    """
    chat_body = ChatRequest(
        message=body.message,
        history=body.history,
        attachments=[],
        session_id=body.session_id,
    )
    system_message = _da_agent_system_prompt()
    if body.workflow:
        try:
            from agent.data_agent.workflow import workflow_prompt

            system_message += "\n\n" + workflow_prompt(body.workflow)
        except Exception:
            pass
    run = _start_chat_run(
        chat_body,
        enabled_toolsets=["data-agent-chat"],
        system_message=system_message,
    )
    return _chat_run_payload(run)


@app.get("/api/chat/runs/{run_id}/events")
async def get_chat_run_events(run_id: str, after: int = -1):
    """Return chat run events newer than the provided sequence number."""
    return _chat_run_events(run_id, after)


@app.post("/api/chat/stream")
async def stream_chat(body: ChatRequest):
    """Run one DAClaw web-chat turn and stream UI events as SSE frames."""
    run = _start_chat_run(body)
    run_id = run["id"]

    async def event_stream():
        heartbeat_seconds = _chat_heartbeat_seconds()
        total_timeout_seconds = _chat_total_timeout_seconds()
        started = float(run["created_at"])
        after = -1
        last_emit = time.time()
        while True:
            payload = _chat_run_events(run_id, after)
            events = payload["events"]
            if events:
                for event in events:
                    after = int(event.get("seq", after))
                    last_emit = time.time()
                    yield _chat_sse(event)
                if payload["run"].get("status") in {"completed", "failed"}:
                    break
                continue

            await asyncio.sleep(0.35)
            if time.time() - last_emit >= heartbeat_seconds:
                now = time.time()
                elapsed = now - started
                if total_timeout_seconds and elapsed > total_timeout_seconds:
                    yield _chat_sse({
                        "type": "error",
                        "message": (
                            "DAClaw web chat timed out while waiting for a long-running "
                            "model/tool turn. The generated files remain in the upload "
                            "directory; start a new turn to continue from them."
                        ),
                        "elapsed": elapsed,
                    })
                    break
                yield _chat_sse({
                    "type": "heartbeat",
                    "run_id": run_id,
                    "seq": after,
                    "message": "DAClaw is still working",
                    "elapsed": elapsed,
                    "idle": now - float(payload["run"].get("updated_at") or started),
                })
                last_emit = now

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Log viewer endpoint
# ---------------------------------------------------------------------------


@app.get("/api/logs")
async def get_logs(
    file: str = "agent",
    lines: int = 100,
    level: Optional[str] = None,
    component: Optional[str] = None,
    search: Optional[str] = None,
):
    from daclaw_cli.logs import _read_tail, LOG_FILES

    log_name = LOG_FILES.get(file)
    if not log_name:
        raise HTTPException(status_code=400, detail=f"Unknown log file: {file}")
    log_path = get_daclaw_home() / "logs" / log_name
    if not log_path.exists():
        return {"file": file, "lines": []}

    try:
        from daclaw_logging import COMPONENT_PREFIXES
    except ImportError:
        COMPONENT_PREFIXES = {}

    # Normalize "ALL" / "all" / empty → no filter. _matches_filters treats an
    # empty tuple as "must match a prefix" (startswith(()) is always False),
    # so passing () instead of None silently drops every line.
    min_level = level if level and level.upper() != "ALL" else None
    if component and component.lower() != "all":
        comp_prefixes = COMPONENT_PREFIXES.get(component)
        if comp_prefixes is None:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown component: {component}. "
                       f"Available: {', '.join(sorted(COMPONENT_PREFIXES))}",
            )
    else:
        comp_prefixes = None

    has_filters = bool(min_level or comp_prefixes or search)
    result = _read_tail(
        log_path, min(lines, 500) if not search else 2000,
        has_filters=has_filters,
        min_level=min_level,
        component_prefixes=comp_prefixes,
    )
    # Post-filter by search term (case-insensitive substring match).
    # _read_tail doesn't support free-text search, so we filter here and
    # trim to the requested line count afterward.
    if search:
        needle = search.lower()
        result = [l for l in result if needle in l.lower()][-min(lines, 500):]
    return {"file": file, "lines": result}


# ---------------------------------------------------------------------------
# Cron job management endpoints
# ---------------------------------------------------------------------------


class CronJobCreate(BaseModel):
    prompt: str
    schedule: str
    name: str = ""
    deliver: str = "local"


class CronJobUpdate(BaseModel):
    updates: dict


@app.get("/api/cron/jobs")
async def list_cron_jobs():
    from cron.jobs import list_jobs
    return list_jobs(include_disabled=True)


@app.get("/api/cron/jobs/{job_id}")
async def get_cron_job(job_id: str):
    from cron.jobs import get_job
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/api/cron/jobs")
async def create_cron_job(body: CronJobCreate):
    from cron.jobs import create_job
    try:
        job = create_job(prompt=body.prompt, schedule=body.schedule,
                         name=body.name, deliver=body.deliver)
        return job
    except Exception as e:
        _log.exception("POST /api/cron/jobs failed")
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/api/cron/jobs/{job_id}")
async def update_cron_job(job_id: str, body: CronJobUpdate):
    from cron.jobs import update_job
    job = update_job(job_id, body.updates)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/api/cron/jobs/{job_id}/pause")
async def pause_cron_job(job_id: str):
    from cron.jobs import pause_job
    job = pause_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/api/cron/jobs/{job_id}/resume")
async def resume_cron_job(job_id: str):
    from cron.jobs import resume_job
    job = resume_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/api/cron/jobs/{job_id}/trigger")
async def trigger_cron_job(job_id: str):
    from cron.jobs import trigger_job
    job = trigger_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.delete("/api/cron/jobs/{job_id}")
async def delete_cron_job(job_id: str):
    from cron.jobs import remove_job
    if not remove_job(job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Skills & Tools endpoints
# ---------------------------------------------------------------------------


class SkillToggle(BaseModel):
    name: str
    enabled: bool


@app.get("/api/skills")
async def get_skills():
    from tools.skills_tool import _find_all_skills
    from daclaw_cli.skills_config import get_disabled_skills
    config = load_config()
    disabled = get_disabled_skills(config)
    skills = _find_all_skills(skip_disabled=True)
    for s in skills:
        s["enabled"] = s["name"] not in disabled
    return skills


@app.put("/api/skills/toggle")
async def toggle_skill(body: SkillToggle):
    from daclaw_cli.skills_config import get_disabled_skills, save_disabled_skills
    config = load_config()
    disabled = get_disabled_skills(config)
    if body.enabled:
        disabled.discard(body.name)
    else:
        disabled.add(body.name)
    save_disabled_skills(config, disabled)
    return {"ok": True, "name": body.name, "enabled": body.enabled}


@app.get("/api/tools/toolsets")
async def get_toolsets():
    from daclaw_cli.tools_config import (
        _get_effective_configurable_toolsets,
        _get_platform_tools,
        _toolset_has_keys,
    )
    from toolsets import resolve_toolset

    config = load_config()
    enabled_toolsets = _get_platform_tools(
        config,
        "cli",
        include_default_mcp_servers=False,
    )
    result = []
    for name, label, desc in _get_effective_configurable_toolsets():
        try:
            tools = sorted(set(resolve_toolset(name)))
        except Exception:
            tools = []
        is_enabled = name in enabled_toolsets
        result.append({
            "name": name, "label": label, "description": desc,
            "enabled": is_enabled,
            "available": is_enabled,
            "configured": _toolset_has_keys(name, config),
            "tools": tools,
        })
    return result


# ---------------------------------------------------------------------------
# Raw YAML config endpoint
# ---------------------------------------------------------------------------


class RawConfigUpdate(BaseModel):
    yaml_text: str


@app.get("/api/config/raw")
async def get_config_raw():
    path = get_config_path()
    if not path.exists():
        return {"yaml": ""}
    return {"yaml": path.read_text(encoding="utf-8")}


@app.put("/api/config/raw")
async def update_config_raw(body: RawConfigUpdate):
    try:
        parsed = yaml.safe_load(body.yaml_text)
        if not isinstance(parsed, dict):
            raise HTTPException(status_code=400, detail="YAML must be a mapping")
        save_config(parsed)
        return {"ok": True}
    except yaml.YAMLError as e:
        raise HTTPException(status_code=400, detail=f"Invalid YAML: {e}")


# ---------------------------------------------------------------------------
# Token / cost analytics endpoint
# ---------------------------------------------------------------------------


@app.get("/api/analytics/usage")
async def get_usage_analytics(days: int = 30):
    from daclaw_state import SessionDB
    db = SessionDB()
    try:
        cutoff = time.time() - (days * 86400)
        cur = db._conn.execute("""
            SELECT date(started_at, 'unixepoch') as day,
                   SUM(input_tokens) as input_tokens,
                   SUM(output_tokens) as output_tokens,
                   SUM(cache_read_tokens) as cache_read_tokens,
                   SUM(reasoning_tokens) as reasoning_tokens,
                   COALESCE(SUM(estimated_cost_usd), 0) as estimated_cost,
                   COALESCE(SUM(actual_cost_usd), 0) as actual_cost,
                   COUNT(*) as sessions
            FROM sessions WHERE started_at > ?
            GROUP BY day ORDER BY day
        """, (cutoff,))
        daily = [dict(r) for r in cur.fetchall()]

        cur2 = db._conn.execute("""
            SELECT model,
                   SUM(input_tokens) as input_tokens,
                   SUM(output_tokens) as output_tokens,
                   COALESCE(SUM(estimated_cost_usd), 0) as estimated_cost,
                   COUNT(*) as sessions
            FROM sessions WHERE started_at > ? AND model IS NOT NULL
            GROUP BY model ORDER BY SUM(input_tokens) + SUM(output_tokens) DESC
        """, (cutoff,))
        by_model = [dict(r) for r in cur2.fetchall()]

        cur3 = db._conn.execute("""
            SELECT SUM(input_tokens) as total_input,
                   SUM(output_tokens) as total_output,
                   SUM(cache_read_tokens) as total_cache_read,
                   SUM(reasoning_tokens) as total_reasoning,
                   COALESCE(SUM(estimated_cost_usd), 0) as total_estimated_cost,
                   COALESCE(SUM(actual_cost_usd), 0) as total_actual_cost,
                   COUNT(*) as total_sessions
            FROM sessions WHERE started_at > ?
        """, (cutoff,))
        totals = dict(cur3.fetchone())

        return {"daily": daily, "by_model": by_model, "totals": totals, "period_days": days}
    finally:
        db.close()


def mount_spa(application: FastAPI):
    """Mount the built SPA. Falls back to index.html for client-side routing.

    The session token is injected into index.html via a ``<script>`` tag so
    the SPA can authenticate against protected API endpoints without a
    separate (unauthenticated) token-dispensing endpoint.
    """
    if not WEB_DIST.exists():
        @application.get("/{full_path:path}")
        async def no_frontend(full_path: str):
            return JSONResponse(
                {"error": "Frontend not built. Run: cd web && npm run build"},
                status_code=404,
            )
        return

    _index_path = WEB_DIST / "index.html"

    def _serve_index():
        """Return index.html with the session token injected."""
        html = _index_path.read_text()
        token_script = (
            f'<script>window.__DACLAW_SESSION_TOKEN__="{_SESSION_TOKEN}";</script>'
        )
        html = html.replace("</head>", f"{token_script}</head>", 1)
        return HTMLResponse(
            html,
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
        )

    application.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

    @application.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        if full_path == "api" or full_path.startswith("api/"):
            return JSONResponse(
                {"detail": f"API endpoint not found: /{full_path}"},
                status_code=404,
            )

        file_path = WEB_DIST / full_path
        # Prevent path traversal via url-encoded sequences (%2e%2e/)
        if (
            full_path
            and file_path.resolve().is_relative_to(WEB_DIST.resolve())
            and file_path.exists()
            and file_path.is_file()
        ):
            return FileResponse(file_path)
        return _serve_index()


# Register DataAgent demo routes (Part 1 workbench + Part 2 leaderboard)
# BEFORE mounting the SPA catch-all, so /api/da/* and /api/lb/* resolve.
try:
    from daclaw_cli.data_agent_web import register as _register_data_agent_routes

    _register_data_agent_routes(app)
except Exception as _da_exc:  # pragma: no cover - demo routes are optional
    _log.warning("DataAgent demo routes not registered: %s", _da_exc)

mount_spa(app)


def start_server(
    host: str = "127.0.0.1",
    port: int = 9119,
    open_browser: bool = True,
    allow_public: bool = False,
):
    """Start the web UI server."""
    import uvicorn

    _LOCALHOST = ("127.0.0.1", "localhost", "::1")
    if host not in _LOCALHOST and not allow_public:
        raise SystemExit(
            f"Refusing to bind to {host} — the dashboard exposes API keys "
            f"and config without robust authentication.\n"
            f"Use --insecure to override (NOT recommended on untrusted networks)."
        )
    if host not in _LOCALHOST:
        _log.warning(
            "Binding to %s with --insecure — the dashboard has no robust "
            "authentication. Only use on trusted networks.", host,
        )

    if open_browser:
        import threading
        import webbrowser

        def _open():
            import time as _t
            _t.sleep(1.0)
            webbrowser.open(f"http://{host}:{port}")

        threading.Thread(target=_open, daemon=True).start()

    print(f"  DAClaw Web UI → http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")
