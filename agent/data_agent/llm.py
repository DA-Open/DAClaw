"""LLM adapter for the DataAgent — talks to any OpenAI/Azure-compatible model.

The demo is wired to consume the *openclaw_evals* config format so the same
model config used for evaluation drives the interactive agent:

    {
      "client_type": "AzureOpenAI",
      "client_args": {"azure_endpoint": ..., "api_key": [k1, k2], "api_version": ...},
      "request_args": {"model": ..., "max_output_tokens": ..., "reasoning": {...}}
    }

Users can point at their own config via ``DACLAW_LLM_CONFIG`` (path to a JSON
file) or by passing a dict. When nothing is set we fall back to the repo's
default gpt-5.5 config so `debug`/`demo` works out of the box.

Design notes:
- ``api_key`` may be a string or a list (we rotate through the list on retry).
- Both ``AzureOpenAI`` and plain ``OpenAI`` client types are supported.
- ``max_output_tokens`` maps to the SDK's ``max_completion_tokens`` for newer
  reasoning models (``max_tokens`` is rejected by some of them).
- All heavy imports are lazy so importing this module never requires ``openai``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("daclaw.llm")

# Ensure our INFO lines surface even when the host (uvicorn) runs at
# log_level=warning — attach a dedicated stderr handler once. Records still
# also go to the JSONL file regardless of this handler.
if not any(getattr(h, "_daclaw_llm", False) for h in logger.handlers):
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s [daclaw.llm] %(message)s"))
    _h._daclaw_llm = True  # type: ignore[attr-defined]
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def _now_iso() -> str:
    """Local timestamp for log records (no external deps)."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _llm_log_path() -> Path:
    """Where per-request LLM logs are appended (JSONL, one record per line)."""
    home = os.environ.get("DACLAW_HOME") or os.path.join(
        os.path.expanduser("~"), ".daclaw_demo"
    )
    d = Path(home)
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d / "llm_requests.log"


def _log_llm_request(record: Dict[str, Any]) -> None:
    """Append one request record to the JSONL log AND emit a human line.

    Never raises — logging must not break the actual request.
    """
    # Human-readable one-liner to the app logger (shows up in api.log).
    status = record.get("status")
    ms = record.get("elapsed_ms")
    model = record.get("model")
    if status == "ok":
        logger.info(
            "LLM ok  model=%s %sms in=%s chars out=%s chars tokens=%s",
            model, ms, record.get("prompt_chars"),
            record.get("reply_chars"), record.get("total_tokens"),
        )
    else:
        logger.warning(
            "LLM ERR model=%s %sms err=%s",
            model, ms, str(record.get("error"))[:200],
        )
    # Structured JSONL for machine inspection / tailing.
    try:
        with _llm_log_path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass

# Default config shipped for local debugging / the demo. Users override via
# DACLAW_LLM_CONFIG or by passing their own path/dict.
_DEFAULT_CONFIG_CANDIDATES = [
    os.environ.get("DACLAW_LLM_CONFIG", "").strip(),
    "/Users/bytedance/workspace/openclaw_evals/scripts/batch_run/llm_config/gpt55.json",
]


class LLMConfigError(RuntimeError):
    pass


def _resolve_config_path(explicit: Optional[str] = None) -> Optional[Path]:
    candidates = [explicit] if explicit else []
    candidates += _DEFAULT_CONFIG_CANDIDATES
    for c in candidates:
        if c and Path(c).expanduser().is_file():
            return Path(c).expanduser()
    return None


def load_config(source: Optional[Any] = None) -> Dict[str, Any]:
    """Load an openclaw_evals-style LLM config from a dict, path, or default."""
    if isinstance(source, dict):
        return source
    path = _resolve_config_path(source if isinstance(source, str) else None)
    if not path:
        raise LLMConfigError(
            "No LLM config found. Set DACLAW_LLM_CONFIG to a JSON file path, or "
            "pass a config dict. Expected openclaw_evals format "
            "(client_type / client_args / request_args)."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _select_key(api_key: Any, index: int) -> Optional[str]:
    if api_key is None or isinstance(api_key, str):
        return api_key
    if isinstance(api_key, list) and api_key:
        item = api_key[index % len(api_key)]
        if isinstance(item, str):
            return item
        if isinstance(item, dict) and isinstance(item.get("api_key"), str):
            return item["api_key"]
    raise LLMConfigError("client_args.api_key must be a string or non-empty list")


class LLMClient:
    """Thin chat wrapper over an OpenAI/Azure-compatible endpoint."""

    def __init__(self, config: Optional[Any] = None):
        self.config = load_config(config)
        self.client_type = self.config.get("client_type", "AzureOpenAI")
        self.client_args = dict(self.config.get("client_args") or {})
        self.request_args = dict(self.config.get("request_args") or {})
        self.model = self.request_args.get("model", "gpt-5.5-2026-04-24")
        self._n_keys = self._count_keys()

    def _count_keys(self) -> int:
        k = self.client_args.get("api_key")
        return len(k) if isinstance(k, list) and k else 1

    def _build_client(self, key_index: int):
        try:
            from openai import AzureOpenAI, OpenAI
        except Exception as exc:  # pragma: no cover
            raise LLMConfigError("openai SDK not installed (pip install openai)") from exc

        key = _select_key(self.client_args.get("api_key"), key_index)
        if self.client_type == "AzureOpenAI":
            return AzureOpenAI(
                azure_endpoint=self.client_args.get("azure_endpoint", ""),
                api_key=key or "EMPTY",
                api_version=self.client_args.get("api_version", "2024-02-01"),
            )
        # plain OpenAI-compatible
        return OpenAI(
            base_url=self.client_args.get("base_url") or self.client_args.get("azure_endpoint"),
            api_key=key or "EMPTY",
        )

    def chat(
        self,
        messages: List[Dict[str, str]],
        *,
        max_output_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        json_mode: bool = False,
    ) -> str:
        """Send a chat completion and return the assistant text.

        Rotates through available API keys on transient failure. Every attempt
        is logged (model, endpoint, latency, token usage, ok/err) to
        ``$DACLAW_HOME/llm_requests.log`` and the app logger.
        """
        last_err: Optional[Exception] = None
        max_tokens = max_output_tokens or self.request_args.get("max_output_tokens", 4096)
        endpoint = (
            self.client_args.get("azure_endpoint")
            or self.client_args.get("base_url")
            or ""
        )
        prompt_chars = sum(len(str(m.get("content", ""))) for m in messages)
        started = time.time()

        for attempt in range(max(self._n_keys, 1)):
            try:
                client = self._build_client(attempt)
                payload: Dict[str, Any] = {
                    "model": self.model,
                    "messages": messages,
                    # newer reasoning models require max_completion_tokens
                    "max_completion_tokens": max_tokens,
                }
                if temperature is not None:
                    payload["temperature"] = temperature
                if json_mode:
                    payload["response_format"] = {"type": "json_object"}
                resp = client.chat.completions.create(**payload)
                reply = resp.choices[0].message.content or ""
                usage = getattr(resp, "usage", None)
                _log_llm_request({
                    "ts": _now_iso(),
                    "status": "ok",
                    "model": self.model,
                    "endpoint": endpoint,
                    "client_type": self.client_type,
                    "key_index": attempt,
                    "n_messages": len(messages),
                    "prompt_chars": prompt_chars,
                    "reply_chars": len(reply),
                    "max_tokens": max_tokens,
                    "json_mode": json_mode,
                    "elapsed_ms": int((time.time() - started) * 1000),
                    "prompt_tokens": getattr(usage, "prompt_tokens", None),
                    "completion_tokens": getattr(usage, "completion_tokens", None),
                    "total_tokens": getattr(usage, "total_tokens", None),
                })
                return reply
            except Exception as exc:  # rotate key and retry
                last_err = exc
                _log_llm_request({
                    "ts": _now_iso(),
                    "status": "error",
                    "model": self.model,
                    "endpoint": endpoint,
                    "client_type": self.client_type,
                    "key_index": attempt,
                    "n_messages": len(messages),
                    "prompt_chars": prompt_chars,
                    "elapsed_ms": int((time.time() - started) * 1000),
                    "error": f"{type(exc).__name__}: {exc}",
                })
                continue
        raise LLMConfigError(f"LLM chat failed after {self._n_keys} key(s): {last_err}")

    def chat_json(
        self, messages: List[Dict[str, str]], *, max_output_tokens: Optional[int] = None
    ) -> Dict[str, Any]:
        """Chat and parse a JSON object out of the response (robust to fences)."""
        text = self.chat(messages, max_output_tokens=max_output_tokens, json_mode=True)
        return _extract_json(text)


def _extract_json(text: str) -> Dict[str, Any]:
    """Best-effort extraction of a JSON object from model output."""
    text = (text or "").strip()
    if not text:
        raise LLMConfigError(
            "LLM returned an empty response (likely hit the output-token limit "
            "before producing JSON — try a smaller goal or raise max_output_tokens)"
        )
    if text.startswith("```"):
        # strip ```json ... ``` fences
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    try:
        return json.loads(text)
    except Exception:
        # find the first {...} balanced block
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except Exception:
                pass
    raise LLMConfigError(f"Could not parse JSON from LLM output: {text[:200]}")


_CLIENT: Optional[LLMClient] = None


def get_llm(config: Optional[Any] = None) -> LLMClient:
    """Return a process-wide LLM client (built from default config if unset)."""
    global _CLIENT
    if config is not None:
        return LLMClient(config)
    if _CLIENT is None:
        _CLIENT = LLMClient()
    return _CLIENT


def llm_available() -> bool:
    try:
        get_llm()
        return True
    except Exception:
        return False
