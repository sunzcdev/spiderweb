"""Debug logging — enabled via SPIDERWEB_DEBUG=1 env var.

Logs to {data_dir}/debug.log — one JSON line per entry.
"""
import json
import os
import time
from datetime import datetime

_log_path: str | None = None


def init(log_dir: str):
    """Set up debug logging to {log_dir}/debug.log. Idempotent."""
    global _log_path
    if _log_path is not None:
        return  # already initialized
    try:
        os.makedirs(log_dir, exist_ok=True)
        _log_path = os.path.join(log_dir, "debug.log")
    except Exception:
        pass


def _enabled() -> bool:
    return bool(_log_path and os.environ.get("SPIDERWEB_DEBUG") == "1")


def _write(entry: dict):
    if not _enabled() or not _log_path:
        return
    entry["ts"] = datetime.now().isoformat()
    try:
        with open(_log_path, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def tool_call(name: str, arguments: dict):
    _write({"event": "tool_call", "tool": name, "args": arguments})


def tool_result(name: str, result: object, elapsed_ms: float):
    summary = str(result)[:500] if result else "empty"
    _write({"event": "tool_result", "tool": name, "elapsed_ms": round(elapsed_ms, 1), "summary": summary})


def tool_error(name: str, error: str, elapsed_ms: float):
    _write({"event": "tool_error", "tool": name, "elapsed_ms": round(elapsed_ms, 1), "error": error})


def llm_call(model: str, purpose: str, prompt_len: int):
    _write({"event": "llm_call", "model": model, "purpose": purpose, "prompt_len": prompt_len})


def llm_result(model: str, purpose: str, response_len: int, elapsed_ms: float):
    _write({"event": "llm_result", "model": model, "purpose": purpose, "response_len": response_len, "elapsed_ms": round(elapsed_ms, 1)})


def llm_error(model: str, purpose: str, error: str):
    _write({"event": "llm_error", "model": model, "purpose": purpose, "error": error})
