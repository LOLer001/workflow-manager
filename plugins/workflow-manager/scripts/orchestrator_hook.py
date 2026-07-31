#!/usr/bin/env python3
"""Fail-open lifecycle hook for compact, privacy-safe Codex task continuity."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
import sys
import tempfile
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator


SCHEMA_VERSION = 7
WRITER_VERSION = "1.0.17"
MAX_EVENT_COUNT = 2**63 - 1
STATE_EVENTS = frozenset(
    {
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "PreCompact",
        "PostCompact",
        "SubagentStart",
        "SubagentStop",
        "Stop",
    }
)
MAX_PROMPTS = 8
MAX_OPERATIONS = 48
MAX_SUBAGENTS = 24
MAX_COMPACTIONS = 16
MAX_GUARDS = 32
MAX_PROCESSED_RUNS = 128
MAX_DUPLICATE_NOTICES = 64
MAX_STATE_BYTES = 1024 * 1024
TRANSCRIPT_TAIL_BYTES = 1024 * 1024
LOCK_TIMEOUT_SECONDS = 0.75
DUPLICATE_TTL_SECONDS = 15 * 60
DEFAULT_RETENTION_DAYS = 30
DEFAULT_MAX_SESSION_FILES = 200
DEFAULT_OUTPUT_CHAR_LIMIT = 16_000
DEFAULT_OUTPUT_LINE_LIMIT = 300
DEFAULT_VISUAL_ITEM_LIMIT = 3
PRESSURE_TRIM_THRESHOLD = 0.55
PRESSURE_CHECKPOINT_THRESHOLD = 0.70

SUCCESS_STATUSES = {"ok"}
ERROR_STATUSES = {
    "cancelled",
    "canceled",
    "error",
    "failed",
    "failure",
    "rejected",
    "timed_out",
    "timeout",
}
RUNNING_STATUSES = {"in_progress", "pending", "queued", "running", "started"}

SENSITIVE_KEYS = {
    "apikey",
    "authorization",
    "authtoken",
    "clientsecret",
    "cookie",
    "credential",
    "credentials",
    "idtoken",
    "password",
    "passwd",
    "privatekey",
    "refreshtoken",
    "secret",
    "secretaccesskey",
    "sessioncookie",
    "sessiontoken",
    "token",
    "accesstoken",
}

SENSITIVE_TEXT_KEY_PATTERN = (
    r"(?:api[_-]?key|(?:[a-z][a-z0-9]*[_-])*token|client[_-]?secret|secret|password|passwd|"
    r"authorization|cookie)"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stable_hash(value: Any, length: int = 16) -> str:
    if isinstance(value, bytes):
        raw = value
    else:
        raw = str(value or "").encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()[:length]


def normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def is_sensitive_key(value: Any) -> bool:
    key = normalized_key(value)
    if key in SENSITIVE_KEYS:
        return True
    if key.endswith("token"):
        return True
    return any(
        marker in key
        for marker in ("password", "passwd", "privatekey", "clientsecret", "refreshtoken", "secretaccesskey")
    )


def redact_structured(value: Any, depth: int = 0) -> Any:
    if depth > 12:
        return "<redacted-depth>"
    if isinstance(value, dict):
        return {
            str(key): "<redacted>" if is_sensitive_key(key) else redact_structured(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_structured(item, depth + 1) for item in value]
    return value


def redact_text(value: str) -> str:
    value = re.sub(
        r"-----BEGIN [^-]+ PRIVATE KEY-----.*?-----END [^-]+ PRIVATE KEY-----",
        "<redacted-private-key>",
        value,
        flags=re.I | re.S,
    )
    value = re.sub(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+\-/=:+]+", r"\1 <redacted>", value)
    value = re.sub(
        rf"(?i)([\"']?{SENSITIVE_TEXT_KEY_PATTERN}[\"']?\s*[:=]\s*)([\"'])(.*?)(\2)",
        r"\1\2<redacted>\2",
        value,
    )
    value = re.sub(
        rf"(?i)\b({SENSITIVE_TEXT_KEY_PATTERN})\b(\s*[:=]\s*)[^\s,;]+",
        r"\1\2<redacted>",
        value,
    )
    value = re.sub(
        rf"(?i)(--?{SENSITIVE_TEXT_KEY_PATTERN}(?:=|\s+))[^\s]+",
        r"\1<redacted>",
        value,
    )
    value = re.sub(r"(?i)(\b[a-z][a-z0-9+.-]*://[^/@\s:]+:)[^/@\s]+@", r"\1<redacted>@", value)
    value = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b", "<redacted-token>", value)
    return value


def compact_text(value: Any, limit: int = 480) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        try:
            value = json.dumps(redact_structured(value), ensure_ascii=False, sort_keys=True)
        except Exception:
            value = str(value)
    value = redact_text(value)
    value = re.sub(r"\s+", " ", value).strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def text_metadata(value: Any) -> dict[str, Any]:
    text = "" if value is None else str(value)
    return {"fingerprint": stable_hash(text), "length": len(text)}


def safe_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return text_metadata(value) if value not in (None, "") else {}
    fingerprint = str(value.get("fingerprint") or "")
    if not re.fullmatch(r"[0-9a-f]{8,64}", fingerprint):
        return {}
    result = {
        "fingerprint": fingerprint[:64],
        "length": max(safe_int(value.get("length")), 0),
    }
    if value.get("updated_at"):
        result["updated_at"] = str(value.get("updated_at"))[:40]
    return result


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_telemetry(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key in ("active_tokens", "cumulative_tokens", "context_window"):
        try:
            result[key] = max(int(value.get(key) or 0), 0)
        except (TypeError, ValueError):
            result[key] = 0
    pressure = value.get("pressure")
    if isinstance(pressure, (int, float)):
        result["pressure"] = max(float(pressure), 0.0)
    if value.get("measured_at"):
        result["measured_at"] = str(value.get("measured_at"))[:40]
    return result


def safe_event_counts(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, int] = {}
    for event in STATE_EVENTS:
        count = min(max(safe_int(value.get(event)), 0), MAX_EVENT_COUNT)
        if count:
            result[event] = count
    return result


def safe_persistence(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    source = value.get("state_path_source")
    outcome = value.get("outcome")
    return {
        "last_hook_at": safe_label(value.get("last_hook_at"), 40),
        "last_event": value.get("last_event") if value.get("last_event") in STATE_EVENTS else "unknown",
        "state_path_source": source
        if source in {"PLUGIN_DATA", "CLAUDE_PLUGIN_DATA", "home_fallback"}
        else "unknown",
        "session_id_present": bool(value.get("session_id_present")),
        "persist_attempted": bool(value.get("persist_attempted")),
        "persist_ok": bool(value.get("persist_ok")),
        "outcome": outcome
        if outcome in {"written", "duplicate", "disabled", "missing_session_id", "lock_timeout", "write_error"}
        else "unknown",
    }


def safe_migration(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    from_writer = safe_label(value.get("from_writer"), 64) if value.get("from_writer") else "unknown"
    to_writer = safe_label(value.get("to_writer"), 64) if value.get("to_writer") else "unknown"
    result = {
        "from_writer": from_writer,
        "to_writer": to_writer,
    }
    if value.get("at"):
        result["at"] = str(value.get("at"))[:40]
    return result


def safe_fingerprint(value: Any) -> str:
    fingerprint = str(value or "")
    return fingerprint[:64] if re.fullmatch(r"[0-9a-f]{8,64}", fingerprint) else ""


def safe_route(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    label = value.get("label") if value.get("label") in {"direct", "focused", "complex", "extensive"} else None
    if not label:
        return {}
    return decorate_route({
        "label": label,
        "score": max(safe_int(value.get("score")), 0),
        "future_token_range": safe_label(value.get("future_token_range"), 32),
        "recommended_agent_cap": min(max(safe_int(value.get("recommended_agent_cap")), 0), 3),
        "parallel_signal": bool(value.get("parallel_signal")),
        "delegation_gate": value.get("delegation_gate")
        if value.get("delegation_gate") in {"closed", "audit", "open"}
        else None,
        "readiness_signal": value.get("readiness_signal")
        if value.get("readiness_signal") in {"none", "possible", "ready_two", "ready_three_plus"}
        else "none",
        "dependency_signal": value.get("dependency_signal")
        if value.get("dependency_signal") in {"none", "ordered", "shared_resource", "ordered_shared"}
        else "none",
        "meta_delegation": bool(value.get("meta_delegation")),
        "delegation_opt_out": bool(value.get("delegation_opt_out")),
        "lane_signal": value.get("lane_signal")
        if value.get("lane_signal") in {"none", "possible", "explicit", "sequential"}
        else "none",
        "phase_hints": [
            safe_label(item, 32)
            for item in as_list(value.get("phase_hints"))
            if item
        ][:8],
        "dependency_hint": safe_label(value.get("dependency_hint"), 64)
        if value.get("dependency_hint")
        else None,
        "workflow_shape": safe_label(value.get("workflow_shape"), 32)
        if value.get("workflow_shape")
        else "direct",
        "execution_order": [
            safe_label(item, 24)
            for item in as_list(value.get("execution_order"))
            if item
        ][:8],
        "agent_mode": safe_label(value.get("agent_mode"), 32)
        if value.get("agent_mode")
        else "local",
        "route_source": safe_label(value.get("route_source"), 32)
        if value.get("route_source")
        else "prompt",
        "at": str(value.get("at"))[:40] if value.get("at") else None,
    })


def safe_label(value: Any, limit: int = 96) -> str:
    text = re.sub(r"[^A-Za-z0-9._:@/+-]+", "_", str(value or "unknown"))
    return (text[:limit] or "unknown").strip("_") or "unknown"


def safe_id(value: Any) -> str:
    raw = str(value or "")
    readable = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._-")[:56] or "session"
    return f"{readable}-{stable_hash(raw)}"


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return min(max(int(os.environ.get(name, default)), minimum), maximum)
    except (TypeError, ValueError):
        return default


def persistence_enabled() -> bool:
    return os.environ.get("TOKEN_FRUGAL_DISABLE_PERSISTENCE", "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }


def data_root_source() -> str:
    if os.environ.get("PLUGIN_DATA"):
        return "PLUGIN_DATA"
    if os.environ.get("CLAUDE_PLUGIN_DATA"):
        return "CLAUDE_PLUGIN_DATA"
    return "home_fallback"


def data_root() -> Path:
    configured = os.environ.get("PLUGIN_DATA") or os.environ.get("CLAUDE_PLUGIN_DATA")
    if configured:
        return Path(configured)
    return Path.home() / ".codex" / "workflow-manager"


def state_path(payload: dict[str, Any]) -> Path | None:
    if not persistence_enabled():
        return None
    raw_id = payload.get("session_id")
    if raw_id in (None, ""):
        return None
    return data_root() / "sessions" / f"{safe_id(raw_id)}.json"


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


@contextmanager
def state_lock(path: Path, timeout: float = LOCK_TIMEOUT_SECONDS) -> Iterator[None]:
    """Acquire a bounded cross-platform lock; never perform a read-modify-write unlocked."""
    lock_path = path.with_suffix(".lock")
    ensure_private_dir(lock_path.parent)
    handle = lock_path.open("a+b")
    try:
        lock_path.chmod(0o600)
    except OSError:
        pass
    acquired = False
    deadline = time.monotonic() + max(timeout, 0.0)
    try:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.025)
        if not acquired:
            raise TimeoutError("workflow-manager state lock timed out")
        yield
    finally:
        if acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
        handle.close()


def new_state(payload: dict[str, Any]) -> dict[str, Any]:
    now = utc_now()
    return {
        "schema_version": SCHEMA_VERSION,
        "writer_version": WRITER_VERSION,
        "session_fingerprint": stable_hash(payload.get("session_id") or payload.get("hook_run_id")),
        "cwd_fingerprint": stable_hash(payload.get("cwd")),
        "model": safe_label(payload.get("model"), 80) if payload.get("model") else None,
        "created_at": now,
        "updated_at": now,
        "objective": {},
        "last_assistant": {},
        "last_route": {},
        "telemetry": {},
        "event_counts": {},
        "persistence": {},
        "migration": {},
        "prompts": [],
        "operations": [],
        "subagents": [],
        "compactions": [],
        "guards": [],
        "processed_hook_runs": [],
        "duplicate_notices": [],
    }


def _safe_prompt(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    prompt_meta = item.get("prompt_meta")
    if not isinstance(prompt_meta, dict):
        prompt_meta = text_metadata(item.get("prompt"))
    return decorate_route({
        "at": item.get("at"),
        "turn_id": safe_label(item.get("turn_id"), 120) if item.get("turn_id") else None,
        "prompt_meta": {
            "fingerprint": safe_label(prompt_meta.get("fingerprint"), 64),
            "length": max(safe_int(prompt_meta.get("length")), 0),
        },
        "label": item.get("label") if item.get("label") in {"direct", "focused", "complex", "extensive"} else "direct",
        "score": max(safe_int(item.get("score")), 0),
        "future_token_range": safe_label(item.get("future_token_range"), 32),
        "recommended_agent_cap": min(max(safe_int(item.get("recommended_agent_cap")), 0), 3),
        "parallel_signal": bool(item.get("parallel_signal")),
        "delegation_gate": item.get("delegation_gate")
        if item.get("delegation_gate") in {"closed", "audit", "open"}
        else None,
        "readiness_signal": item.get("readiness_signal")
        if item.get("readiness_signal") in {"none", "possible", "ready_two", "ready_three_plus"}
        else "none",
        "dependency_signal": item.get("dependency_signal")
        if item.get("dependency_signal") in {"none", "ordered", "shared_resource", "ordered_shared"}
        else "none",
        "meta_delegation": bool(item.get("meta_delegation")),
        "delegation_opt_out": bool(item.get("delegation_opt_out")),
        "lane_signal": item.get("lane_signal")
        if item.get("lane_signal") in {"none", "possible", "explicit", "sequential"}
        else "none",
        "phase_hints": [safe_label(value, 32) for value in as_list(item.get("phase_hints")) if value][:8],
        "dependency_hint": safe_label(item.get("dependency_hint"), 64)
        if item.get("dependency_hint")
        else None,
        "workflow_shape": safe_label(item.get("workflow_shape"), 32)
        if item.get("workflow_shape")
        else "direct",
        "execution_order": [safe_label(value, 24) for value in as_list(item.get("execution_order")) if value][
            :8
        ],
        "agent_mode": safe_label(item.get("agent_mode"), 32) if item.get("agent_mode") else "local",
        "route_source": safe_label(item.get("route_source"), 32)
        if item.get("route_source")
        else "prompt",
    })


def _safe_operation(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    fingerprint = str(item.get("fingerprint") or "")
    if not re.fullmatch(r"[0-9a-f]{8,64}", fingerprint):
        return None
    status_value = safe_label(item.get("status"), 32).lower()
    return {
        "at": item.get("at"),
        "turn_id": safe_label(item.get("turn_id"), 120) if item.get("turn_id") else None,
        "tool": safe_label(item.get("tool"), 120),
        "fingerprint": fingerprint[:64],
        "status": status_value,
        "category": safe_label(item.get("category"), 32) if item.get("category") else "other",
        "risk_kind": safe_label(item.get("risk_kind"), 32) if item.get("risk_kind") else None,
        "output_chars": max(safe_int(item.get("output_chars")), 0),
        "output_lines": max(safe_int(item.get("output_lines")), 0),
        "visual_items": max(safe_int(item.get("visual_items")), 0),
        "truncated": bool(item.get("truncated")),
        "budgeted": bool(item.get("budgeted")),
        "oversized": bool(item.get("oversized")),
        "compacted": bool(item.get("compacted")),
    }


def _safe_guard(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    fingerprint = str(item.get("fingerprint") or "")
    if fingerprint and not re.fullmatch(r"[0-9a-f]{8,64}", fingerprint):
        return None
    return {
        "at": item.get("at"),
        "turn_id": safe_label(item.get("turn_id"), 120) if item.get("turn_id") else None,
        "kind": safe_label(item.get("kind"), 48),
        "action": safe_label(item.get("action"), 32),
        "fingerprint": fingerprint[:64] if fingerprint else None,
    }


def _safe_subagent(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    result_meta = item.get("result_meta")
    if not isinstance(result_meta, dict) and item.get("result") is not None:
        result_meta = text_metadata(item.get("result"))
    objective_fingerprint = str(item.get("objective_fingerprint") or "")
    if not re.fullmatch(r"[0-9a-f]{8,64}", objective_fingerprint):
        objective_fingerprint = ""
    request_fingerprint = str(item.get("request_fingerprint") or "")
    if not re.fullmatch(r"[0-9a-f]{8,64}", request_fingerprint):
        request_fingerprint = ""
    value = {
        "at": item.get("at"),
        "event": item.get("event") if item.get("event") in {"request", "start", "stop"} else "unknown",
        "turn_id": safe_label(item.get("turn_id"), 120) if item.get("turn_id") else None,
        "agent_id": safe_label(item.get("agent_id"), 120),
        "agent_type": safe_label(item.get("agent_type"), 80),
        "task_name": safe_label(item.get("task_name"), 120) if item.get("task_name") else None,
        "status": safe_label(item.get("status"), 32).lower() if item.get("status") else None,
        "scope_fingerprint": safe_label(item.get("scope_fingerprint"), 64) if item.get("scope_fingerprint") else None,
        "request_fingerprint": request_fingerprint or None,
        "objective_fingerprint": objective_fingerprint or None,
        "stale": bool(item.get("stale")),
        "request_gate": item.get("request_gate")
        if item.get("request_gate") in {"audit", "open"}
        else None,
        "request_cap": min(max(safe_int(item.get("request_cap")), 0), 3),
        "reaudited": bool(item.get("reaudited")),
    }
    if isinstance(result_meta, dict):
        value["result_meta"] = {
            "fingerprint": safe_label(result_meta.get("fingerprint"), 64),
            "length": max(safe_int(result_meta.get("length")), 0),
        }
    return value


def _safe_active_agent_scope(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    result: dict[str, Any] = {}
    for key in ("agent_fingerprint", "task_fingerprint", "scope_fingerprint", "objective_fingerprint"):
        fingerprint = str(item.get(key) or "")
        if fingerprint and re.fullmatch(r"[0-9a-f]{8,64}", fingerprint):
            result[key] = fingerprint[:64]
    return result or None


def _safe_continuity(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    change = str(item.get("change_fingerprint") or "")
    if change and not re.fullmatch(r"[0-9a-f]{8,64}", change):
        change = ""
    return {
        "current_stage": safe_label(item.get("current_stage"), 32)
        if item.get("current_stage")
        else "unknown",
        "acceptance_pending": bool(item.get("acceptance_pending")),
        "next_required_stage": safe_label(item.get("next_required_stage"), 32)
        if item.get("next_required_stage")
        else "unknown",
        "last_outcome_status": safe_label(item.get("last_outcome_status"), 32)
        if item.get("last_outcome_status")
        else "unknown",
        "evidence_available": bool(item.get("evidence_available")),
        "change_fingerprint": change[:64] if change else None,
    }


def _safe_compaction(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    objective_meta = item.get("objective_meta")
    if not isinstance(objective_meta, dict) and item.get("objective") is not None:
        objective_meta = text_metadata(item.get("objective"))
    successes = [
        str(value)[:64]
        for value in item.get("recent_successes", [])
        if re.fullmatch(r"[0-9a-f]{8,64}", str(value))
    ]
    return {
        "at": item.get("at"),
        "phase": item.get("phase") if item.get("phase") in {"pre", "post"} else "unknown",
        "trigger": safe_label(item.get("trigger"), 32) if item.get("trigger") else None,
        "turn_id": safe_label(item.get("turn_id"), 120) if item.get("turn_id") else None,
        "telemetry": safe_telemetry(item.get("telemetry")),
        "objective_meta": safe_metadata(objective_meta),
        "current_stage": safe_label(item.get("current_stage"), 32)
        if item.get("current_stage")
        else "unknown",
        "active_agent_scopes": [
            scope for raw in as_list(item.get("active_agent_scopes"))
            if (scope := _safe_active_agent_scope(raw)) is not None
        ][:8],
        "recent_successes": successes[-8:],
        "continuity": _safe_continuity(item.get("continuity")),
    }


def normalize_state(value: Any, payload: dict[str, Any]) -> dict[str, Any]:
    base = new_state(payload)
    if not isinstance(value, dict):
        return base
    base["created_at"] = value.get("created_at") or base["created_at"]
    for key in ("session_fingerprint", "cwd_fingerprint"):
        fingerprint = safe_fingerprint(value.get(key))
        if fingerprint:
            base[key] = fingerprint
    if value.get("model"):
        base["model"] = safe_label(value.get("model"), 80)
    base["telemetry"] = safe_telemetry(value.get("telemetry"))
    base["event_counts"] = safe_event_counts(value.get("event_counts"))
    base["persistence"] = safe_persistence(value.get("persistence"))
    base["migration"] = safe_migration(value.get("migration"))
    base["last_route"] = safe_route(value.get("last_route"))

    base["objective"] = safe_metadata(value.get("objective"))
    if not base["objective"] and value.get("last_objective"):
        base["objective"] = text_metadata(value.get("last_objective"))
    base["last_assistant"] = safe_metadata(value.get("last_assistant"))

    base["prompts"] = [item for raw in as_list(value.get("prompts")) if (item := _safe_prompt(raw)) is not None][-MAX_PROMPTS:]
    base["operations"] = [item for raw in as_list(value.get("operations")) if (item := _safe_operation(raw)) is not None][-MAX_OPERATIONS:]
    base["subagents"] = [item for raw in as_list(value.get("subagents")) if (item := _safe_subagent(raw)) is not None][-MAX_SUBAGENTS:]
    base["compactions"] = [item for raw in as_list(value.get("compactions")) if (item := _safe_compaction(raw)) is not None][-MAX_COMPACTIONS:]
    base["guards"] = [item for raw in as_list(value.get("guards")) if (item := _safe_guard(raw)) is not None][
        -MAX_GUARDS:
    ]
    base["processed_hook_runs"] = [safe_label(item, 64) for item in as_list(value.get("processed_hook_runs")) if item][-MAX_PROCESSED_RUNS:]
    base["duplicate_notices"] = [
        {
            "fingerprint": safe_label(item.get("fingerprint"), 64),
            "turn_id": safe_label(item.get("turn_id"), 120),
            "at": item.get("at"),
        }
        for item in as_list(value.get("duplicate_notices"))
        if isinstance(item, dict) and item.get("fingerprint")
    ][-MAX_DUPLICATE_NOTICES:]
    base["schema_version"] = SCHEMA_VERSION
    base["writer_version"] = WRITER_VERSION
    return base


def load_state(path: Path | None, payload: dict[str, Any]) -> dict[str, Any]:
    if path is None:
        return new_state(payload)
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_STATE_BYTES:
            return new_state(payload)
        with path.open("r", encoding="utf-8") as stream:
            raw = stream.read(MAX_STATE_BYTES + 1)
        if len(raw.encode("utf-8")) > MAX_STATE_BYTES:
            return new_state(payload)
        return normalize_state(json.loads(raw), payload)
    except Exception:
        return new_state(payload)


def atomic_write(path: Path, state: dict[str, Any]) -> None:
    ensure_private_dir(path.parent)
    state["updated_at"] = utc_now()
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        try:
            fchmod = getattr(os, "fchmod", None)
            if fchmod is not None:
                fchmod(fd, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            fd = -1
            json.dump(state, stream, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            stream.write("\n")
            stream.flush()
        os.replace(temporary, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def hook_run_key(payload: dict[str, Any]) -> str | None:
    run_id = payload.get("hook_run_id")
    if run_id in (None, ""):
        return None
    return stable_hash(f"{payload.get('hook_event_name')}\0{run_id}", 24)


def trim_state(state: dict[str, Any]) -> None:
    state["prompts"] = list(state.get("prompts", []))[-MAX_PROMPTS:]
    state["operations"] = list(state.get("operations", []))[-MAX_OPERATIONS:]
    state["subagents"] = list(state.get("subagents", []))[-MAX_SUBAGENTS:]
    state["compactions"] = list(state.get("compactions", []))[-MAX_COMPACTIONS:]
    state["guards"] = list(state.get("guards", []))[-MAX_GUARDS:]
    state["processed_hook_runs"] = list(state.get("processed_hook_runs", []))[-MAX_PROCESSED_RUNS:]
    state["duplicate_notices"] = list(state.get("duplicate_notices", []))[-MAX_DUPLICATE_NOTICES:]


def increment_event_count(state: dict[str, Any], payload: dict[str, Any]) -> None:
    event = str(payload.get("hook_event_name") or "")
    if event not in STATE_EVENTS:
        return
    counts = safe_event_counts(state.get("event_counts"))
    counts[event] = min(counts.get(event, 0) + 1, MAX_EVENT_COUNT)
    state["event_counts"] = counts


def set_persistence_metadata(
    state: dict[str, Any], payload: dict[str, Any], *, attempted: bool, ok: bool, outcome: str
) -> None:
    state["writer_version"] = WRITER_VERSION
    state["persistence"] = {
        "last_hook_at": utc_now(),
        "last_event": str(payload.get("hook_event_name") or "unknown")
        if str(payload.get("hook_event_name") or "") in STATE_EVENTS
        else "unknown",
        "state_path_source": data_root_source(),
        "session_id_present": payload.get("session_id") not in (None, ""),
        "persist_attempted": attempted,
        "persist_ok": ok,
        "outcome": outcome,
    }


def debug_persistence(
    payload: dict[str, Any], *, path_resolved: bool, outcome: str, error: Exception | None = None
) -> None:
    if os.environ.get("TOKEN_FRUGAL_DEBUG", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return
    event = str(payload.get("hook_event_name") or "")
    event = event if event in STATE_EVENTS else "unknown"
    error_type = type(error).__name__ if error is not None else "none"
    print(
        f"workflow-manager persistence writer={WRITER_VERSION} event={event} "
        f"session_id={'present' if payload.get('session_id') not in (None, '') else 'missing'} "
        f"state_path={'resolved' if path_resolved else 'none'} source={data_root_source()} "
        f"persist={outcome} error={safe_label(error_type, 48)}",
        file=sys.stderr,
    )


def snapshot_state(payload: dict[str, Any]) -> dict[str, Any]:
    """Read state under the writer lock so Windows can atomically replace the JSON file."""
    path = state_path(payload)
    if path is None:
        return new_state(payload)
    try:
        with state_lock(path):
            return load_state(path, payload)
    except TimeoutError as error:
        debug_persistence(payload, path_resolved=True, outcome="lock_timeout", error=error)
    except OSError as error:
        debug_persistence(payload, path_resolved=True, outcome="read_error", error=error)
    # Fail open without a second unlocked read, which could race another writer.
    return new_state(payload)


def mutate_state(
    payload: dict[str, Any], change: Callable[[dict[str, Any]], None]
) -> tuple[dict[str, Any], bool]:
    path = state_path(payload)
    if path is None:
        state = new_state(payload)
        increment_event_count(state, payload)
        change(state)
        outcome = "disabled" if not persistence_enabled() else "missing_session_id"
        set_persistence_metadata(state, payload, attempted=False, ok=False, outcome=outcome)
        trim_state(state)
        debug_persistence(payload, path_resolved=False, outcome=outcome)
        return state, True
    try:
        with state_lock(path):
            state = load_state(path, payload)
            if payload.get("cwd"):
                state["cwd_fingerprint"] = stable_hash(payload.get("cwd"))
            if payload.get("model"):
                state["model"] = safe_label(payload.get("model"), 80)
            run_key = hook_run_key(payload)
            if run_key and run_key in state.get("processed_hook_runs", []):
                debug_persistence(payload, path_resolved=True, outcome="duplicate")
                return state, False
            increment_event_count(state, payload)
            change(state)
            if run_key:
                state.setdefault("processed_hook_runs", []).append(run_key)
            trim_state(state)
            set_persistence_metadata(state, payload, attempted=True, ok=True, outcome="written")
            atomic_write(path, state)
            debug_persistence(payload, path_resolved=True, outcome="written")
            return state, True
    except TimeoutError as error:
        debug_persistence(payload, path_resolved=True, outcome="lock_timeout", error=error)
        return new_state(payload), False
    except OSError as error:
        debug_persistence(payload, path_resolved=True, outcome="write_error", error=error)
        return new_state(payload), False


def cleanup_old_sessions() -> None:
    if not persistence_enabled():
        return
    sessions = data_root() / "sessions"
    if not sessions.exists():
        return
    retention_days = env_int("TOKEN_FRUGAL_RETENTION_DAYS", DEFAULT_RETENTION_DAYS, 1, 3650)
    max_files = env_int("TOKEN_FRUGAL_MAX_SESSIONS", DEFAULT_MAX_SESSION_FILES, 10, 5000)
    cutoff = time.time() - retention_days * 86400
    candidates: list[tuple[float, Path]] = []

    def remove_if_unlocked(path: Path) -> bool:
        try:
            with state_lock(path, timeout=0.0):
                path.unlink(missing_ok=True)
            return True
        except (OSError, TimeoutError):
            return False

    try:
        for path in sessions.glob("*.json"):
            try:
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode):
                    continue
                if info.st_mtime < cutoff:
                    remove_if_unlocked(path)
                else:
                    candidates.append((info.st_mtime, path))
            except OSError:
                continue
        for _, path in sorted(candidates, reverse=True)[max_files:]:
            remove_if_unlocked(path)
        for path in sessions.glob("*.tmp"):
            try:
                if path.lstat().st_mtime < time.time() - 86400:
                    path.unlink(missing_ok=True)
            except OSError:
                pass
    except OSError:
        return


def refresh_related_states() -> None:
    """Migrate every retained valid state once per writer version."""
    if not persistence_enabled():
        return
    root = data_root()
    sessions = root / "sessions"
    marker = root / "migrations" / f"{safe_label(WRITER_VERSION, 64)}.json"
    try:
        if marker.is_file():
            return
        with state_lock(marker):
            if marker.is_file():
                return
            cleanup_old_sessions()
            ensure_private_dir(sessions)
            max_files = env_int(
                "TOKEN_FRUGAL_MAX_SESSIONS",
                DEFAULT_MAX_SESSION_FILES,
                10,
                5000,
            )
            candidates: list[tuple[float, Path]] = []
            for path in sessions.glob("*.json"):
                try:
                    info = path.lstat()
                    if stat.S_ISREG(info.st_mode) and info.st_size <= MAX_STATE_BYTES:
                        candidates.append((info.st_mtime, path))
                except OSError:
                    continue

            migrated = 0
            invalid = 0
            locked = 0
            for _, path in sorted(candidates, reverse=True)[:max_files]:
                try:
                    with state_lock(path, timeout=0.0):
                        info = path.lstat()
                        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_STATE_BYTES:
                            invalid += 1
                            continue
                        with path.open("r", encoding="utf-8") as stream:
                            raw = stream.read(MAX_STATE_BYTES + 1)
                        if len(raw.encode("utf-8")) > MAX_STATE_BYTES:
                            invalid += 1
                            continue
                        value = json.loads(raw)
                        if not isinstance(value, dict):
                            invalid += 1
                            continue
                        from_writer = (
                            safe_label(value.get("writer_version"), 64)
                            if value.get("writer_version")
                            else "unknown"
                        )
                        state = normalize_state(value, {"session_id": path.stem})
                        state["migration"] = {
                            "from_writer": from_writer,
                            "to_writer": WRITER_VERSION,
                            "at": utc_now(),
                        }
                        atomic_write(path, state)
                        migrated += 1
                except TimeoutError:
                    locked += 1
                except (OSError, ValueError, json.JSONDecodeError):
                    invalid += 1

            if locked:
                return
            atomic_write(
                marker,
                {
                    "schema_version": SCHEMA_VERSION,
                    "writer_version": WRITER_VERSION,
                    "migrated": migrated,
                    "invalid": invalid,
                },
            )
    except (OSError, TimeoutError):
        return


def read_transcript_tail(path_value: Any) -> list[bytes]:
    if not path_value:
        return []
    path = Path(str(path_value))
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            return []
        with path.open("rb") as stream:
            start = max(0, info.st_size - TRANSCRIPT_TAIL_BYTES)
            stream.seek(start)
            data = stream.read(TRANSCRIPT_TAIL_BYTES)
        if start:
            first_newline = data.find(b"\n")
            data = data[first_newline + 1 :] if first_newline >= 0 else b""
        return data.splitlines()
    except Exception:
        return []


def latest_token_telemetry(payload: dict[str, Any]) -> dict[str, Any]:
    for raw in reversed(read_transcript_tail(payload.get("transcript_path"))):
        try:
            item = json.loads(raw)
            event = item.get("payload", {})
            if item.get("type") != "event_msg" or event.get("type") != "token_count":
                continue
            info = event.get("info") or {}
            last = info.get("last_token_usage") or {}
            total = info.get("total_token_usage") or {}
            window = int(info.get("model_context_window") or 0)
            active = int(last.get("total_tokens") or last.get("input_tokens") or 0)
            cumulative = int(total.get("total_tokens") or 0)
            pressure = active / window if window > 0 else None
            return {
                "active_tokens": active,
                "cumulative_tokens": cumulative,
                "context_window": window,
                "pressure": pressure,
                "measured_at": utc_now(),
            }
        except Exception:
            continue
    return {}


EN_ACTIONS = (
    "build",
    "compile",
    "debug",
    "deploy",
    "diagnose",
    "fix",
    "implement",
    "integrate",
    "investigate",
    "install",
    "migrate",
    "optimize",
    "package",
    "record",
    "refactor",
    "reboot",
    "reproduce",
    "review",
    "simulate",
    "test",
    "verify",
)
ZH_ACTIONS = (
    "编译",
    "构建",
    "检查",
    "核对",
    "确认",
    "查看",
    "调试",
    "部署",
    "诊断",
    "修复",
    "修改",
    "实现",
    "集成",
    "安装",
    "排查",
    "迁移",
    "优化",
    "合包",
    "打包",
    "刷机",
    "重启",
    "复现",
    "录像",
    "录屏",
    "抓日志",
    "重构",
    "审查",
    "模拟",
    "测试",
    "验证",
)
EN_BREADTH = (
    "all files",
    "comprehensive",
    "complex",
    "end-to-end",
    "exhaustive",
    "multiple",
    "parallel",
    "systematic",
)
ZH_BREADTH = (
    "不要停止",
    "多个",
    "复杂",
    "完整流程",
    "全面",
    "全量",
    "并行",
    "彻底",
    "所有文件",
    "系统性",
    "端到端",
)

PHASE_TERMS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "analysis": (
        ("audit", "debug", "diagnose", "investigate", "log analysis", "reproduce", "review", "root cause"),
        ("审计", "调试", "诊断", "排查", "日志分析", "抓日志", "复现", "审查", "根因"),
    ),
    "implementation": (
        ("fix", "implement", "integrate", "migrate", "modify", "optimize", "patch", "refactor"),
        ("修复", "实现", "集成", "迁移", "修改", "优化", "补丁", "重构"),
    ),
    "build_package": (
        ("assemble", "build", "compile", "package"),
        ("编译", "构建", "合包", "打包"),
    ),
    "delivery_device": (
        ("adb", "deploy", "device", "flash", "install", "reboot"),
        ("部署", "设备", "实机", "上机", "安装", "刷机", "重启"),
    ),
    "verification": (
        ("acceptance", "regression", "test", "validate", "verification", "verify"),
        ("测试", "验证", "回归", "验收"),
    ),
    "evidence": (
        ("record", "screenrecord", "screenshot", "video"),
        ("录像", "录屏", "截图", "视频"),
    ),
    "research": (
        ("compare", "documentation", "historical", "research", "search"),
        ("对比", "文档", "历史", "调研", "搜索"),
    ),
}

EN_RECURRENCE = ("again", "crash loop", "flaky", "intermittent", "keeps", "repeated", "repeatedly", "still")
ZH_RECURRENCE = ("反复", "重复", "多次", "不断", "还是", "仍然", "仍旧", "又", "死循环")
EN_SMALL_SCOPE = ("one small", "single file", "small change", "tiny change")
ZH_SMALL_SCOPE = ("一个小", "单个文件", "仅修改", "小改动")
SEQUENTIAL_PHASES = {"build_package", "delivery_device", "verification", "evidence"}
ROUTE_RANK = {"direct": 0, "focused": 1, "complex": 2, "extensive": 3}


def _english_hits(text: str, terms: tuple[str, ...]) -> int:
    return sum(1 for term in terms if re.search(rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])", text))


def phase_hints(prompt: str) -> list[str]:
    lower = prompt.lower()
    result: list[str] = []
    for phase, (english, chinese) in PHASE_TERMS.items():
        if _english_hits(lower, english) or any(term in prompt for term in chinese):
            result.append(phase)
    return result


def meta_delegation_signal(prompt: str) -> bool:
    lower = prompt.lower()
    mentions_delegation = bool(
        re.search(r"\b(?:subagents?|child agents?|delegation|agent routing)\b", lower)
        or any(term in prompt for term in ("子智能体", "子代理", "代理路由", "委派"))
        or "workflow-manager" in lower
        or "workflow manager" in lower
    )
    if not mentions_delegation:
        return False
    return bool(
        re.search(
            r"\b(?:discuss|evaluate|judge|test|testing|decide|determine|consider|explain|"
            r"whether|when|how)\b.{0,48}\b(?:subagents?|delegation|agent routing|orchestrator)\b",
            lower,
        )
        or re.search(
            r"\b(?:subagents?|delegation|agent routing)\b.{0,48}\b(?:test|whether|when|how|needed)\b",
            lower,
        )
        or any(
            re.search(pattern, prompt)
            for pattern in (
                r"(?:讨论|测试|判断|评估|验证|决定|考虑|解释).{0,24}(?:子智能体|子代理|代理路由|编排器)",
                r"(?:是否|何时|什么时候|如何|怎么).{0,24}(?:使用|启用|需要|启动)?.{0,8}(?:子智能体|子代理)",
                r"(?:子智能体|子代理).{0,24}(?:是否|何时|如何|怎么|需要)",
            )
        )
    )


def explicit_spawn_signal(prompt: str, meta_delegation: bool = False) -> bool:
    lower = prompt.lower()
    if meta_delegation:
        return False
    if delegation_opt_out_signal(prompt):
        return False
    return bool(
        re.search(
            r"\b(?:spawn|launch|create|use|delegate to|assign to)\b.{0,32}"
            r"\b(?:subagents?|child agents?|workers?)\b",
            lower,
        )
        or re.search(r"(?:派出|启动|创建|使用|安排|委派给).{0,16}(?:子智能体|子代理|代理)", prompt)
    )


def delegation_opt_out_signal(prompt: str) -> bool:
    lower = prompt.lower()
    return bool(
        re.search(
        r"\b(?:do not|don't|never|without)\s+(?:use|spawn|launch|create|delegate to)\s+"
        r"(?:any\s+)?(?:subagents?|agents?|workers?)\b",
        lower,
        )
        or re.search(r"(?:不要|无需|禁止|不许).{0,12}(?:子智能体|子代理|委派)", prompt)
    )


def prompt_dependency_signal(prompt: str) -> str:
    lower = prompt.lower()
    ordered = bool(
        re.search(r"\bfirst\b.{0,100}\b(?:then|afterwards?|next)\b", lower)
        or re.search(
            r"\b(?:build|compile|package|deploy|install|reboot|flash)\b.{0,120}"
            r"\bthen\b.{0,120}\b(?:deploy|install|reboot|flash|verify|validate|test)\b",
            lower,
        )
        or re.search(r"\b(?:after|before|once)\b.{0,80}\b(?:build|compile|deploy|install|verify|test|record)", lower)
        or re.search(r"\b(?:depends? on|requires? (?:the )?(?:output|result|artifact)|using (?:the )?(?:output|artifact))\b", lower)
        or re.search(r"(?:先).{0,100}(?:再|然后|之后|后)", prompt)
        or re.search(r"(?:依赖|必须等|基于).{0,40}(?:产物|结果|完成|之后|以后)", prompt)
    )
    shared = bool(
        re.search(
            r"\b(?:only|single|same|shared)\s+(?:connected\s+)?"
            r"(?:device|build server|resource|workspace|file|artifact|account)\b",
            lower,
        )
        or re.search(r"\b(?:shared resource|shared device|one device)\b", lower)
        or re.search(r"(?:唯一|同一|共享).{0,12}(?:设备|资源|工作区|文件|产物|账号|构建服务器)", prompt)
    )
    if ordered and shared:
        return "ordered_shared"
    if ordered:
        return "ordered"
    if shared:
        return "shared_resource"
    return "none"


def ready_lane_evidence(prompt: str, list_items: int = 0) -> tuple[str, int]:
    lower = prompt.lower()
    ready_now = bool(
        re.search(
            r"\b(?:ready[- ]now|ready to start (?:now|immediately)|start immediately|"
            r"immediately (?:ready|available)|(?:can|may|able to) (?:all |both )?start (?:right )?now)\b",
            lower,
        )
        or re.search(r"(?:现在|立即|当前).{0,10}(?:可|可以|能|已经).{0,6}(?:开始|启动|开展|就绪)", prompt)
        or re.search(r"(?:可|可以|能)(?:立即|现在).{0,6}(?:开始|启动|开展)", prompt)
    )
    independent = bool(
        re.search(
            r"\b(?:independent|dependency[- ]free|no dependencies|disjoint|non-overlapping|"
            r"separate (?:files|modules|worktrees|lanes)|do not share|without shared)\b",
            lower,
        )
        or any(term in prompt for term in ("互不依赖", "无依赖", "各自独立", "独立工作线", "不重叠", "不同文件", "各自"))
    )
    count = 0
    number_map = {
        "two": 2,
        "three": 3,
        "four": 4,
        "2": 2,
        "3": 3,
        "4": 4,
    }
    for match in re.finditer(
        r"\b(two|three|four|[234])\b.{0,32}\b(?:lanes?|workstreams?|subtasks?|tasks?)\b",
        lower,
    ):
        count = max(count, number_map.get(match.group(1), 0))
    chinese_number_map = {"二": 2, "两": 2, "三": 3, "四": 4, "2": 2, "3": 3, "4": 4}
    for match in re.finditer(r"([二两三四234])\s*条?.{0,20}(?:工作线|子任务|任务|lane)", prompt, re.I):
        count = max(count, chinese_number_map.get(match.group(1), 0))
    named_lanes = {
        match.group(0).lower()
        for match in re.finditer(r"\b(?:lane|workstream|subtask)\s*(?:[a-d]|[1-4])\b", lower)
    }
    named_lanes.update(
        match.group(0) for match in re.finditer(r"(?:工作线|子任务)\s*[一二三四1-4]", prompt)
    )
    count = max(count, len(named_lanes))
    if list_items >= 2:
        count = max(count, min(list_items, 4))
    plural = bool(
        re.search(r"\b(?:both|each|several|multiple|parallel|lanes|workstreams|subtasks)\b", lower)
        or any(term in prompt for term in ("两条", "三条", "多个", "各自", "并行", "分别"))
    )
    if ready_now and independent and count < 2 and plural:
        count = 2
    if ready_now and independent and count >= 3:
        return "ready_three_plus", count
    if ready_now and independent and count >= 2:
        return "ready_two", count
    if ready_now or independent:
        return "possible", count
    return "none", count


def decorate_route(route: dict[str, Any]) -> dict[str, Any]:
    result = dict(route)
    label = str(result.get("label") or "direct")
    lane_signal = str(result.get("lane_signal") or "none")
    readiness = str(result.get("readiness_signal") or "none")
    if readiness not in {"none", "possible", "ready_two", "ready_three_plus"}:
        readiness = "none"
    dependency = str(result.get("dependency_signal") or "none")
    if dependency not in {"none", "ordered", "shared_resource", "ordered_shared"}:
        dependency = "none"
    meta_delegation = bool(result.get("meta_delegation"))
    delegation_opt_out = bool(result.get("delegation_opt_out"))
    gate = str(result.get("delegation_gate") or "")
    phases = list(dict.fromkeys(str(item) for item in as_list(result.get("phase_hints")) if item))[:8]
    result["phase_hints"] = phases

    if delegation_opt_out:
        gate = "closed"
    elif dependency != "none" or lane_signal == "sequential":
        lane_signal = "sequential"
        gate = "closed"
    elif label in {"direct", "focused"}:
        gate = "closed"
    elif meta_delegation and gate == "open":
        gate = "audit"
    elif gate not in {"closed", "audit", "open"}:
        gate = "open" if lane_signal == "explicit" and readiness in {"ready_two", "ready_three_plus"} else "audit"

    cap = min(max(safe_int(result.get("recommended_agent_cap")), 0), 3)
    if gate == "closed" or label in {"direct", "focused"}:
        cap = 0
    elif label == "complex":
        cap = 2
    elif label == "extensive":
        cap = 3
    else:
        cap = 0

    result["lane_signal"] = lane_signal
    result["delegation_gate"] = gate
    result["readiness_signal"] = readiness
    result["dependency_signal"] = dependency
    result["meta_delegation"] = meta_delegation
    result["delegation_opt_out"] = delegation_opt_out
    result["recommended_agent_cap"] = cap
    if dependency != "none" and not result.get("dependency_hint"):
        result["dependency_hint"] = (
            "shared_artifact_or_device"
            if dependency in {"shared_resource", "ordered_shared"}
            else "ordered_dependency"
        )

    if label == "direct":
        shape = "direct"
    elif label == "focused":
        shape = "single_chain"
    elif lane_signal == "sequential":
        shape = "sequential_pipeline"
    elif gate == "open":
        shape = "multi_lane"
    elif lane_signal in {"possible", "explicit"}:
        shape = "lane_audit"
    else:
        shape = "bounded_complex"

    if label == "direct":
        order = ["answer"]
    else:
        order = ["contract"]
        phase_set = set(phases)
        if phase_set & {"analysis", "research"}:
            order.append("evidence")
        if "implementation" in phase_set:
            order.append("change")
        if "build_package" in phase_set:
            order.append("build")
        if "delivery_device" in phase_set:
            order.append("deliver")
        if phase_set & {"verification", "evidence"}:
            order.append("verify")
        if label in {"complex", "extensive"} and "evidence" not in order:
            order.insert(1, "evidence")
        if label in {"complex", "extensive"} and "verify" not in order:
            order.append("verify")
        if label == "focused" and phase_set & {"implementation", "build_package", "delivery_device"} and "verify" not in order:
            order.append("verify")
        if label == "focused" and len(order) == 1:
            order.extend(("work", "verify"))
        order.append("report")

    if cap <= 0:
        agent_mode = "sequential_local" if lane_signal == "sequential" else "local"
    elif cap == 1:
        agent_mode = "parent_plus_one"
    else:
        agent_mode = "bounded_multi"
    result["workflow_shape"] = shape
    result["execution_order"] = list(dict.fromkeys(order))[:8]
    result["agent_mode"] = agent_mode
    return result
def classify_prompt(prompt: str) -> dict[str, Any]:
    normalized = prompt.strip()
    lower = normalized.lower()
    score = 0
    if len(normalized) > 280:
        score += 1
    if len(normalized) > 1200:
        score += 1
    list_items = len(re.findall(r"(?m)^\s*(?:[-*]|\d+[.)、])\s+", normalized))
    if list_items >= 3:
        score += 1
    if list_items >= 8:
        score += 1

    action_hits = _english_hits(lower, EN_ACTIONS) + sum(1 for term in ZH_ACTIONS if term in normalized)
    breadth_hits = _english_hits(lower, EN_BREADTH) + sum(1 for term in ZH_BREADTH if term in normalized)
    if action_hits >= 1:
        score += 1
    if action_hits >= 2:
        score += 1
    if action_hits >= 5:
        score += 1
    if breadth_hits >= 1:
        score += 1
    if breadth_hits >= 3:
        score += 1

    phases = phase_hints(normalized)
    recurrence = bool(_english_hits(lower, EN_RECURRENCE) or any(term in normalized for term in ZH_RECURRENCE))
    small_scope = any(term in lower for term in EN_SMALL_SCOPE) or any(term in normalized for term in ZH_SMALL_SCOPE)
    if small_scope and breadth_hits == 0 and score > 1:
        score -= 1
    if len(phases) >= 3:
        score = max(score, 2)
    if len(phases) >= 5:
        score = max(score, 5)
    if recurrence and ({"analysis", "delivery_device"} & set(phases)):
        score = max(score, 2)

    meta_delegation = meta_delegation_signal(normalized)
    delegation_opt_out = delegation_opt_out_signal(normalized)
    spawn_request = explicit_spawn_signal(normalized, meta_delegation)
    raw_parallel = any(
        term in lower
        for term in ("in parallel", "independent", "parallel", "separate lane", "two issues")
    ) or any(term in normalized for term in ("并行", "独立", "两条线", "两个问题", "分别", "同时"))
    parallel_signal = bool((raw_parallel or spawn_request) and not meta_delegation)
    if parallel_signal:
        score += 1

    readiness_signal, ready_lane_count = ready_lane_evidence(normalized, list_items)
    if readiness_signal == "ready_two":
        score = max(score, 2)
    elif readiness_signal == "ready_three_plus":
        score = max(score, 5)

    if score <= 0:
        label, future = "direct", "under 4k"
    elif score == 1:
        label, future = "focused", "4k-15k"
    elif score <= 4:
        label, future = "complex", "15k-50k"
    else:
        label, future = "extensive", "50k-120k+"

    phase_set = set(phases)
    dependency_signal = prompt_dependency_signal(normalized)
    if dependency_signal == "none" and len(phase_set) >= 2 and phase_set <= SEQUENTIAL_PHASES:
        dependency_signal = "ordered_shared"
    sequential = dependency_signal != "none"
    if label in {"direct", "focused"}:
        lane_signal, agents, gate = "none", 0, "closed"
    elif delegation_opt_out:
        lane_signal, agents, gate = "none", 0, "closed"
    elif sequential:
        lane_signal, agents, gate = "sequential", 0, "closed"
    elif (
        parallel_signal
        and readiness_signal in {"ready_two", "ready_three_plus"}
        and ready_lane_count >= 2
    ):
        lane_signal, gate = "explicit", "open"
        agents = 3 if label == "extensive" else 2
    else:
        lane_signal = "possible" if parallel_signal or len(phase_set) >= 2 or breadth_hits else "none"
        agents, gate = (3 if label == "extensive" else 2), "audit"

    dependency_hint = None
    if dependency_signal in {"shared_resource", "ordered_shared"}:
        dependency_hint = "shared_artifact_or_device"
    elif dependency_signal == "ordered":
        dependency_hint = "ordered_dependency"
    return decorate_route({
        "label": label,
        "score": score,
        "future_token_range": future,
        "recommended_agent_cap": agents,
        "parallel_signal": parallel_signal,
        "delegation_gate": gate,
        "readiness_signal": readiness_signal,
        "dependency_signal": dependency_signal,
        "meta_delegation": meta_delegation,
        "delegation_opt_out": delegation_opt_out,
        "lane_signal": lane_signal,
        "phase_hints": phases,
        "dependency_hint": dependency_hint,
        "route_source": "prompt",
    })
def extract_command(payload: dict[str, Any]) -> str:
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        for key in ("command", "cmd"):
            value = tool_input.get(key)
            if isinstance(value, str):
                return value
        for key in ("args", "arguments", "input"):
            nested = tool_input.get(key)
            if isinstance(nested, dict):
                for command_key in ("command", "cmd"):
                    value = nested.get(command_key)
                    if isinstance(value, str):
                        return value
    tool = str(payload.get("tool_name") or "").lower()
    if isinstance(tool_input, str) and tool in {"bash", "shell", "exec_command"}:
        return tool_input
    return ""


def is_subagent_spawn_tool(payload: dict[str, Any]) -> bool:
    name = normalized_key(payload.get("tool_name"))
    return name == "agent" or name.endswith("spawnagent")


def subagent_request_fields(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    tool_input = payload.get("tool_input")
    candidates: list[dict[str, Any]] = []
    if isinstance(tool_input, dict):
        candidates.append(tool_input)
        for key in ("args", "arguments", "input"):
            nested = tool_input.get(key)
            if isinstance(nested, dict):
                candidates.append(nested)

    task_name = None
    scope_value = None
    for candidate in candidates:
        if task_name is None:
            for key in ("task_name", "name", "description"):
                value = candidate.get(key)
                if value:
                    task_name = safe_label(value, 120)
                    break
        if scope_value is None:
            for key in ("message", "prompt", "task"):
                value = candidate.get(key)
                if value:
                    scope_value = value
                    break
    return task_name, stable_hash(scope_value) if scope_value else None


def subagent_request_text(payload: dict[str, Any]) -> str:
    tool_input = payload.get("tool_input")
    candidates: list[dict[str, Any]] = []
    if isinstance(tool_input, dict):
        candidates.append(tool_input)
        for key in ("args", "arguments", "input"):
            nested = tool_input.get(key)
            if isinstance(nested, dict):
                candidates.append(nested)
    for candidate in candidates:
        for key in ("message", "prompt", "task"):
            value = candidate.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return ""


def request_supports_delegation_reaudit(payload: dict[str, Any], route: dict[str, Any]) -> bool:
    if route.get("label") not in {"complex", "extensive"}:
        return False
    if route.get("delegation_opt_out"):
        return False
    request = subagent_request_text(payload)
    if not request:
        return False
    lower = request.lower()
    ready_now = bool(
        re.search(r"\b(?:ready[- ]now|start (?:right )?now|start immediately|can start now)\b", lower)
        or re.search(r"(?:现在|立即|当前).{0,10}(?:可|可以|能|已经).{0,6}(?:开始|启动|开展|就绪)", request)
        or re.search(r"(?:可|可以|能)(?:立即|现在).{0,6}(?:开始|启动|开展)", request)
    )
    independent = bool(
        re.search(r"\b(?:independent|disjoint|non-overlapping|no dependencies|separate (?:files|modules|paths))\b", lower)
        or any(term in request for term in ("互不依赖", "无依赖", "独立工作线", "不重叠", "不同文件", "独立于父"))
    )
    read_only = bool(
        re.search(r"\b(?:read[- ]only|no writes?|without modifying|do not modify)\b", lower)
        or any(term in request for term in ("只读", "不修改", "不要修改", "不写入"))
    )
    exclusive_write_owner = bool(
        re.search(
            r"\b(?:exclusive write owner|only (?:modify|edit|write)|owns? (?:the )?(?:files?|paths?|module))\b",
            lower,
        )
        or any(term in request for term in ("独占修改", "独占写入", "只修改", "仅修改", "负责修改", "写入所有权"))
    )
    dependency = str(route.get("dependency_signal") or "none")
    excludes_shared_resource = bool(
        re.search(
            r"\b(?:do not|does not|will not|without|won't)\b.{0,36}"
            r"\b(?:device|build server|build account|shared artifact|shared workspace)\b",
            lower,
        )
        or re.search(
            r"(?:不操作|不使用|不接触|不占用|无需).{0,16}"
            r"(?:设备|构建服务器|构建账号|共享产物|共享工作区)",
            request,
        )
    )
    conflicting_shared_action = bool(
        re.search(
            r"\b(?:build(?! (?:server|account))|compile|deploy|install|reboot|flash|run adb|"
            r"validate on (?:the )?device)\b",
            lower,
        )
        or re.search(r"(?:编译|构建(?!服务器|账号)|部署|安装|重启|刷机|设备验证|实机验证)", request)
    )
    if dependency in {"shared_resource", "ordered_shared"}:
        if not excludes_shared_resource or conflicting_shared_action:
            return False
    return ready_now and independent and (read_only or exclusive_write_owner)


def shell_syntax_view(command: str) -> str:
    """Mask quoted/escaped data while preserving shell operators and source offsets."""
    visible = list(command)
    quote: str | None = None
    escaped = False
    for index, char in enumerate(command):
        if escaped:
            visible[index] = " "
            escaped = False
            continue
        if quote is not None:
            visible[index] = " "
            if char == quote:
                quote = None
            elif char == "\\" and quote == '"':
                escaped = True
            continue
        if char in {"'", '"'}:
            visible[index] = " "
            quote = char
        elif char == "\\":
            visible[index] = " "
            escaped = True
    return "".join(visible)


_NESTED_POSIX_SHELL_RE = re.compile(
    r"(?i)(?:^|[;&|]\s*|\$\(|\(\s*)"
    r"(?:(?:env\s+(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*)|"
    r"(?:sudo(?:\s+-\S+)*\s+)|(?:timeout(?:\s+-\S+)*\s+\S+\s+))*"
    r"(?:sh|bash|zsh)(?:\.exe)?\s+(?:-(?:lc|c))[ \t]+?"
)
_NESTED_CMD_RE = re.compile(
    r"(?i)(?:^|[;&|]\s*|\$\(|\(\s*)cmd(?:\.exe)?\s+(?:/d\s+)?/c[ \t]+?"
)
_NESTED_POWERSHELL_RE = re.compile(
    r"(?i)(?:^|[;&|]\s*|\$\(|\(\s*)(?:powershell|pwsh)(?:\.exe)?\s+"
    r"(?:(?:-[A-Za-z][A-Za-z0-9-]*)\s+)*?-(?:command|c)[ \t]+?"
)


_NESTED_EVAL_RE = re.compile(
    r"(?i)(?:^|[;&|]\s*|\$\(|\(\s*)(?:(?:builtin|command)\s+)?eval[ \t]+?"
)


def _outer_shell_segment(command: str, start: int) -> str:
    masked = shell_syntax_view(command)
    boundary = re.search(r"(?:&&|\|\||[;|])", masked[start:])
    end = start + boundary.start() if boundary else len(command)
    return command[start:end].strip()


def _first_shell_argument(value: str) -> str:
    try:
        parts = shlex.split(value, posix=True)
    except (TypeError, ValueError):
        return ""
    return parts[0] if parts else ""


def _joined_shell_arguments(value: str) -> str:
    try:
        return " ".join(shlex.split(value, posix=True))
    except (TypeError, ValueError):
        return ""


def _nested_shell_payloads(command: str) -> list[str]:
    payloads: list[str] = []
    masked = shell_syntax_view(command)
    for match in _NESTED_POSIX_SHELL_RE.finditer(masked):
        payload = _first_shell_argument(_outer_shell_segment(command, match.end()))
        if payload:
            payloads.append(payload)
    for match in _NESTED_EVAL_RE.finditer(masked):
        payload = _joined_shell_arguments(_outer_shell_segment(command, match.end()))
        if payload:
            payloads.append(payload)
    for pattern in (_NESTED_CMD_RE, _NESTED_POWERSHELL_RE):
        for match in pattern.finditer(masked):
            tail = _outer_shell_segment(command, match.end())
            if not tail:
                continue
            payload = _first_shell_argument(tail) if tail[:1] in {"'", '"'} else tail
            if payload:
                payloads.append(payload)
    return payloads


def command_views(command: str) -> list[str]:
    """Return the outer command and bounded local shell command strings; fail open on ambiguity."""
    if not isinstance(command, str) or not command:
        return []
    views: list[str] = []
    pending: deque[tuple[str, int]] = deque([(command, 0)])
    while pending and len(views) < 8:
        value, depth = pending.popleft()
        if not value or value in views:
            continue
        views.append(value)
        if depth >= 3 or len(value) > 65_536:
            continue
        try:
            pending.extend((nested, depth + 1) for nested in _nested_shell_payloads(value))
        except Exception:
            continue
    return views


GIT_INVOCATION_RE = re.compile(
    r"(?i)(?:^|[;&|]\s*|\$\(|\(\s*)(?:env\s+(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*)?"
    r"git(?:\.exe)?\s+(?:(?:-[Cc]\s+\S+|--(?:git-dir|work-tree)(?:=|\s+)\S+)\s+)*([a-z][a-z0-9-]*)"
)
_COMMAND_BOUNDARY_RE = (
    r"(?:^|[;&|]\s*|\$\(|[({}]\s*|\)\s*|[\r\n]\s*|"
    r"\b(?:if|then|elif|else|while|until|do|coproc)\s+)"
)
_COMMAND_PREFIX_RE = (
    r"(?:!\s+)?(?:"
    r"(?:env(?:\s+(?:-\S+|[A-Za-z_][A-Za-z0-9_]*=\S+))*\s+)|"
    r"(?:sudo(?:(?:\s+-[ug]\s+\S+)|(?:\s+--(?:user|group)(?:=|\s+)\S+)|(?:\s+-\S+))*\s+)|"
    r"(?:timeout(?:\s+-\S+)*\s+\S+\s+)|(?:command(?:\s+-\S+)*\s+)|"
    r"(?:time(?:\s+-\S+)*\s+)|(?:nohup(?:\s+--)?\s+)|"
    r"(?:nice(?:\s+(?:-\S+|\d+))*\s+))*"
)
BUILD_COMMAND_RE = re.compile(
    rf"(?i){_COMMAND_BOUNDARY_RE}{_COMMAND_PREFIX_RE}(?:"
    rf"(?:\./)?(?:gradlew(?:\.bat)?|gradle|mvnw?|ninja|make|bazel|buck2?|cargo)\b|"
    rf"(?:m|mm|mmm)\s+(?:-|[A-Za-z0-9_./]))"
)
_DEVICE_COMMAND = "a" "db"
_LOG_COMMAND = "log" "cat"
_JOURNAL_COMMAND = "journal" "ctl"
_KERNEL_LOG_COMMAND = "d" "mesg"
_RECORD_COMMAND = "screen" "record"
_DEVICE_PREFIX_RE = rf"{_DEVICE_COMMAND}(?:\.exe)?(?:\s+-s\s+\S+)?"
LOG_COMMAND_RE = re.compile(
    rf"(?i){_COMMAND_BOUNDARY_RE}{_COMMAND_PREFIX_RE}(?:"
    rf"{_DEVICE_PREFIX_RE}\s+(?:shell\s+)?{_LOG_COMMAND}|"
    rf"{_LOG_COMMAND}|{_JOURNAL_COMMAND}|{_KERNEL_LOG_COMMAND})\b"
)
SCREENRECORD_RE = re.compile(
    rf"(?i){_COMMAND_BOUNDARY_RE}{_COMMAND_PREFIX_RE}{_DEVICE_PREFIX_RE}\b"
    rf"[^;&|\n]*\b{_RECORD_COMMAND}\b"
)


def _git_invocation(command: str) -> tuple[str, re.Match[str]] | None:
    for candidate in command_views(command):
        match = GIT_INVOCATION_RE.search(shell_syntax_view(candidate))
        if match:
            return candidate, match
    return None


def git_subcommand(command: str) -> str | None:
    invocation = _git_invocation(command)
    return invocation[1].group(1).lower() if invocation else None


def cwd_is_wsl_or_network_mount(cwd: Any) -> bool:
    value = str(cwd or "").strip()
    if not value:
        return False
    normalized = value.replace("\\", "/")
    if value.startswith("\\\\") or normalized.startswith("//"):
        return True
    if re.match(r"(?i)^/mnt/[a-z](?:/|$)", normalized):
        return True
    if os.name == "nt" or not normalized.startswith("/"):
        return False
    try:
        target = str(Path(normalized).resolve(strict=False))
        best_length = -1
        best_type = ""
        with Path("/proc/mounts").open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                fields = line.split()
                if len(fields) < 3:
                    continue
                mountpoint = fields[1].replace("\\040", " ").replace("\\134", "\\")
                if target == mountpoint or target.startswith(mountpoint.rstrip("/") + "/"):
                    if len(mountpoint) > best_length:
                        best_length = len(mountpoint)
                        best_type = fields[2].lower()
        return best_type in {"9p", "cifs", "drvfs", "fuse.sshfs", "smbfs"}
    except OSError:
        return False


def bounded_git_status(command: str) -> bool:
    invocation = _git_invocation(command)
    if not invocation:
        return True
    candidate, match = invocation
    if match.group(1).lower() != "status":
        return True
    segment = re.split(r"(?:&&|\|\||[;|])", candidate[match.start() :], maxsplit=1)[0]
    concise = bool(re.search(r"(?i)(?:^|\s)(?:-s|--short|--porcelain(?:=\S+)?)(?:\s|$)", segment))
    no_untracked = bool(re.search(r"(?i)(?:^|\s)(?:-uno|--untracked-files=no)(?:\s|$)", segment))
    path_match = re.search(r"\s--\s+([^\s]+)", segment)
    explicit_path = bool(path_match and path_match.group(1).strip("'\"") not in {".", "./", ":/"})
    return concise and no_untracked and explicit_path


def command_category(payload: dict[str, Any], command: str | None = None) -> str:
    tool = str(payload.get("tool_name") or "").lower()
    value = command if command is not None else extract_command(payload)
    detections = [shell_syntax_view(candidate) for candidate in command_views(value)]
    if "update_plan" in tool:
        return "planning"
    if tool in {"apply_patch", "edit", "write"}:
        return "implementation"
    if "subagent" in tool or "spawn_agent" in tool:
        return "subagent"
    if "view_image" in tool or "screenshot" in tool:
        return "evidence"
    if git_subcommand(value):
        return "git"
    if any(SCREENRECORD_RE.search(candidate) for candidate in detections):
        return "evidence"
    if any(LOG_COMMAND_RE.search(candidate) for candidate in detections):
        return "analysis"
    if any(BUILD_COMMAND_RE.search(candidate) for candidate in detections):
        return "build_package"
    if any(re.search(r"(?i)(?:^|[;&|]\s*)(?:adb(?:\.exe)?|fastboot|flash)\b", candidate) for candidate in detections):
        return "delivery_device"
    if any(re.search(r"(?i)\b(?:pytest|unittest|ctest|go\s+test|cargo\s+test|npm\s+test)\b", candidate) for candidate in detections):
        return "verification"
    if any(re.search(r"(?i)(?:^|[;&|]\s*)(?:rg|grep|sed|find|head|tail)\b", candidate) for candidate in detections):
        return "analysis"
    if "openaideveloperdocs" in tool or "search" in tool or "fetch" in tool:
        return "research"
    return "other"


def build_exit_status_preserved(command: str) -> bool:
    masked = shell_syntax_view(command)
    build = BUILD_COMMAND_RE.search(masked)
    if not build:
        return True
    prefix = masked[build.start():build.end()]
    if re.search(r"(?:^|\s)!\s", prefix):
        return False
    tail = re.sub(r";\s*}\s*$", "", masked[build.end():])
    status_changing_control = re.compile(
        r"\|\||&&|(?<!\|)\|(?!\|)|;(?=\s*\S)|[\r\n](?=\s*\S)|(?<![>&])&(?![>&])"
    )
    return status_changing_control.search(tail) is None


def command_output_budget(payload: dict[str, Any], command: str, risk_kind: str) -> bool:
    redirects_to_file = bool(re.search(r"(?<!\d)(?:>>?|&>)\s*(?!&)[^\s;&|]+", command))
    bounded_pipe = bool(re.search(r"(?i)\|\s*(?:head|tail)\b(?:\s+-n)?\s+\d+", command))
    if risk_kind == "build_output":
        # A UI/output cap, quiet flag, or head/tail pipe can hide the real build result.
        # Preserve the complete log and the command exit code before narrowing diagnostics.
        return redirects_to_file and build_exit_status_preserved(command)
    if risk_kind == "streaming_log":
        return redirects_to_file or bounded_pipe or bool(
            re.search(r"(?i)(?:^|\s)(?:-t\s*\d+|--lines(?:=|\s+)\d+|--max-count(?:=|\s+)\d+)(?:\s|$)", command)
        )
    if risk_kind == "screenrecord":
        match = re.search(r"(?i)--time-limit(?:=|\s+)(\d+)", command)
        return bool(match and 0 < int(match.group(1)) <= 180)
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        budget = safe_int(tool_input.get("max_output_tokens"))
        if 0 < budget <= 6000:
            return True
    return True


def command_guard(payload: dict[str, Any]) -> tuple[str, str] | None:
    command = extract_command(payload)
    if not command:
        return None
    subcommand = git_subcommand(command)
    if subcommand and cwd_is_wsl_or_network_mount(payload.get("cwd")):
        return (
            "mounted_local_git",
            "Workflow Manager guard blocked Git in a WSL/DrvFS/CIFS/UNC working tree. Use android-remote-git or the "
            "authoritative remote Linux source tree; do not retry another local Git variant.",
        )
    if subcommand == "status" and not bounded_git_status(command):
        return (
            "broad_git_status",
            "Workflow Manager guard blocked unbounded git status. On a safe authoritative Git tree, use "
            "git status --short --untracked-files=no -- <explicit-paths>.",
        )
    for candidate in command_views(command):
        detection_command = shell_syntax_view(candidate)
        if SCREENRECORD_RE.search(detection_command) and not command_output_budget(payload, candidate, "screenrecord"):
            return (
                "screenrecord",
                "Workflow Manager guard blocked unbounded screenrecord. Add --time-limit 180 or less, then inspect only "
                "1-3 representative frames unless more evidence is required.",
            )
        if LOG_COMMAND_RE.search(detection_command) and not command_output_budget(payload, candidate, "streaming_log"):
            return (
                "streaming_log",
                "Workflow Manager guard blocked an unbounded log stream. Capture a finite snapshot or redirect to a file, "
                "then query only the first direct error, Caused by, fatal exception, or relevant frames.",
            )
        if BUILD_COMMAND_RE.search(detection_command) and not command_output_budget(payload, candidate, "build_output"):
            return (
                "build_output",
                "Workflow Manager guard blocked a build/package command without a recoverable full log. Redirect complete "
                "output to a file with no trailing shell work so the real exit code remains observable; quiet flags, output caps, and head/tail pipes "
                "are not substitutes. Run follow-up commands separately, then inspect the exact diagnostics needed.",
            )
    return None
def command_risk_kind(payload: dict[str, Any], command: str) -> str | None:
    for candidate in command_views(command):
        detection_command = shell_syntax_view(candidate)
        if SCREENRECORD_RE.search(detection_command):
            return "screenrecord"
        if LOG_COMMAND_RE.search(detection_command):
            return "streaming_log"
        if BUILD_COMMAND_RE.search(detection_command):
            return "build_output"
    if git_subcommand(command) == "status":
        return "git_status"
    return None
def iter_response_strings(value: Any, depth: int = 0) -> Iterator[str]:
    if depth > 12:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if is_sensitive_key(key) or normalized_key(key) in {"base64", "data", "imageurl"}:
                continue
            yield from iter_response_strings(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from iter_response_strings(item, depth + 1)


def response_visual_items(value: Any, depth: int = 0) -> int:
    if depth > 12:
        return 0
    if isinstance(value, dict):
        own = 1 if str(value.get("type") or "").lower() in {"image", "image_url", "input_image"} else 0
        if "image_url" in value:
            own = max(own, 1)
        return own + sum(response_visual_items(item, depth + 1) for item in value.values())
    if isinstance(value, (list, tuple)):
        return sum(response_visual_items(item, depth + 1) for item in value)
    return 0


def response_truncated(value: Any, depth: int = 0) -> bool:
    if depth > 12:
        return False
    if isinstance(value, dict):
        for key, item in value.items():
            if normalized_key(key) in {"truncated", "outputtruncated", "istruncated"} and bool(item):
                return True
            if response_truncated(item, depth + 1):
                return True
    elif isinstance(value, (list, tuple)):
        return any(response_truncated(item, depth + 1) for item in value)
    return False


DIAGNOSTIC_LINE_RE = re.compile(
    r"(?i)^\s*(?:"
    r"\[(?:error|fatal|failure)\]\s*|"
    r"caused by(?:\s*:|\b)|fatal(?:\s+error)?(?:\s*:|\b)|"
    r"error(?:\[[^\]]+\])?(?:\s*:|\b)|exception(?:\s*:|\b)|"
    r"traceback(?:\s+\(most recent call last\))?(?:\s*:|\b)|failure(?:\s*:|\b)|"
    r"(?:build|script|command|task|test)\s+failed(?:\s*:|\b)|"
    r"[A-Za-z_][A-Za-z0-9_.]*(?:error|exception)\s*:"
    r")"
)


def analyze_tool_response(response: Any) -> tuple[dict[str, Any], str]:
    total_chars = 0
    total_lines = 0
    diagnostic_lines: list[str] = []
    tail: deque[str] = deque(maxlen=4)
    for chunk in iter_response_strings(response):
        total_chars += len(chunk)
        total_lines += chunk.count("\n") + (1 if chunk else 0)
        for raw_line in chunk.splitlines():
            line = compact_text(raw_line, 240)
            if not line:
                continue
            tail.append(line)
            if len(diagnostic_lines) < 4 and DIAGNOSTIC_LINE_RE.search(line):
                diagnostic_lines.append(line)
    excerpt_lines = list(dict.fromkeys([*diagnostic_lines, *tail]))[:8]
    meta = {
        "output_chars": total_chars,
        "output_lines": total_lines,
        "visual_items": response_visual_items(response),
        "truncated": response_truncated(response),
    }
    return meta, "\n".join(excerpt_lines)


def output_compaction_limits(telemetry: dict[str, Any] | None = None) -> dict[str, int]:
    pressure = (telemetry or {}).get("pressure")
    factor = 0.5 if isinstance(pressure, (int, float)) and pressure >= PRESSURE_CHECKPOINT_THRESHOLD else (
        0.75 if isinstance(pressure, (int, float)) and pressure >= PRESSURE_TRIM_THRESHOLD else 1.0
    )
    configured = {
        "output_chars": env_int("TOKEN_FRUGAL_OUTPUT_CHAR_LIMIT", DEFAULT_OUTPUT_CHAR_LIMIT, 1000, 500_000),
        "output_lines": env_int("TOKEN_FRUGAL_OUTPUT_LINE_LIMIT", DEFAULT_OUTPUT_LINE_LIMIT, 50, 10_000),
        "visual_items": env_int("TOKEN_FRUGAL_VISUAL_ITEM_LIMIT", DEFAULT_VISUAL_ITEM_LIMIT, 1, 50),
    }
    dynamic = {
        "output_chars": max(int(DEFAULT_OUTPUT_CHAR_LIMIT * factor), 1000),
        "output_lines": max(int(DEFAULT_OUTPUT_LINE_LIMIT * factor), 50),
        "visual_items": max(int(DEFAULT_VISUAL_ITEM_LIMIT * factor), 1),
    }
    return {key: min(configured[key], dynamic[key]) for key in configured}


def output_needs_compaction(
    meta: dict[str, Any], telemetry: dict[str, Any] | None = None
) -> bool:
    limits = output_compaction_limits(telemetry)
    return any(safe_int(meta.get(key)) > limit for key, limit in limits.items())
def emit_pretool_deny(reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            },
            ensure_ascii=False,
        )
    )


def emit_posttool_advisory(status_value: str, meta: dict[str, Any]) -> None:
    summary = (
        "Workflow Manager noticed an oversized tool result and preserved the original for normal model reasoning: "
        f'status={status_value}, chars={safe_int(meta.get("output_chars"))}, '
        f'lines={safe_int(meta.get("output_lines"))}, visuals={safe_int(meta.get("visual_items"))}, '
        f'truncated={bool(meta.get("truncated"))}. '
        "Correctness and evidence completeness take priority over context savings. Use the current result; "
        "bound only future reads or queries, and obtain more exact evidence whenever the current result is insufficient."
    )
    print(
        json.dumps(
            {
                "continue": True,
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": summary,
                },
            },
            ensure_ascii=False,
        )
    )


def tool_fingerprint(payload: dict[str, Any]) -> tuple[str, str]:
    tool = safe_label(payload.get("tool_name"), 120)
    canonical_payload = {
        "cwd": str(payload.get("cwd") or ""),
        "session_id": str(payload.get("session_id") or ""),
        "tool": str(payload.get("tool_name") or "unknown"),
        "tool_input": payload.get("tool_input"),
    }
    try:
        canonical = json.dumps(canonical_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:
        canonical = str(canonical_payload)
    return stable_hash(canonical), tool


def response_status(response: Any) -> str:
    if not isinstance(response, dict):
        return "unknown"
    if response.get("error") or response.get("isError") is True or response.get("is_error") is True:
        return "error"
    if response.get("success") is False or response.get("ok") is False:
        return "error"
    code = response.get("exit_code")
    if isinstance(code, int):
        return "ok" if code == 0 else f"error:{code}"
    status_value = str(response.get("status") or response.get("state") or "").strip().lower()
    if status_value in ERROR_STATUSES:
        return "error"
    if status_value in RUNNING_STATUSES:
        return "running"
    if status_value in {"complete", "completed", "done", "ok", "success", "succeeded"}:
        return "ok"
    if response.get("session_id") and code is None:
        return "running"
    if isinstance(response.get("content"), list) and response.get("isError") is False:
        return "ok"
    return "unknown"


def emit_context(event: str, text: str) -> None:
    print(
        json.dumps(
            {
                "continue": True,
                "hookSpecificOutput": {
                    "hookEventName": event,
                    "additionalContext": text,
                },
            },
            ensure_ascii=False,
        )
    )


def emit_continue() -> None:
    print(json.dumps({"continue": True}))


def pressure_text(telemetry: dict[str, Any]) -> str:
    pressure = telemetry.get("pressure")
    active = telemetry.get("active_tokens")
    window = telemetry.get("context_window")
    if isinstance(pressure, (int, float)) and active is not None and window:
        return f"active context about {active:,}/{window:,} tokens ({pressure:.1%})"
    return "live token telemetry unavailable"


def active_agent_count(state: dict[str, Any]) -> int:
    active: set[str] = set()
    for item in state.get("subagents", []):
        agent_id = str(item.get("agent_id") or "")
        if not agent_id:
            continue
        if item.get("event") == "start":
            active.add(agent_id)
        elif item.get("event") == "stop":
            active.discard(agent_id)
    return len(active)


def agent_activity_counts(state: dict[str, Any]) -> dict[str, int]:
    started = {str(item.get("agent_id")) for item in state.get("subagents", []) if item.get("event") == "start"}
    completed = {str(item.get("agent_id")) for item in state.get("subagents", []) if item.get("event") == "stop"}
    return {"started": len(started), "completed": len(completed), "active": active_agent_count(state)}


def current_execution_stage(state: dict[str, Any]) -> str:
    stage_by_category = {
        "planning": "contract",
        "analysis": "evidence",
        "research": "evidence",
        "git": "evidence",
        "implementation": "change",
        "build_package": "build",
        "delivery_device": "deliver",
        "verification": "verify",
        "evidence": "verify",
    }
    for operation in reversed(state.get("operations", [])):
        stage = stage_by_category.get(str(operation.get("category") or ""))
        if stage:
            return stage
    order = as_list(state.get("last_route", {}).get("execution_order"))
    return str(order[0]) if order else "unknown"


def quality_continuity(state: dict[str, Any]) -> dict[str, Any]:
    operations = [item for item in state.get("operations", []) if isinstance(item, dict)]
    if not operations:
        compactions = [item for item in state.get("compactions", []) if isinstance(item, dict)]
        if compactions:
            prior = _safe_continuity(compactions[-1].get("continuity"))
            if prior:
                return prior
    order = [str(item) for item in as_list(state.get("last_route", {}).get("execution_order")) if item]
    current_stage = current_execution_stage(state)
    last_status = str(operations[-1].get("status") or "unknown") if operations else "unknown"
    last_change_index = -1
    last_verification_success = -1
    evidence_available = False
    change_fingerprint: str | None = None
    for index, operation in enumerate(operations):
        category = str(operation.get("category") or "")
        status_value = str(operation.get("status") or "")
        if category in {"implementation", "build_package", "delivery_device"}:
            last_change_index = index
            fingerprint = str(operation.get("fingerprint") or "")
            if re.fullmatch(r"[0-9a-f]{8,64}", fingerprint):
                change_fingerprint = fingerprint[:64]
        if status_value in SUCCESS_STATUSES and category in {"analysis", "research", "evidence", "verification"}:
            evidence_available = True
        if status_value in SUCCESS_STATUSES and category in {"evidence", "verification"}:
            last_verification_success = index
    acceptance_pending = "verify" in order and (
        last_verification_success < last_change_index or last_verification_success < 0
    )
    if not operations:
        next_required_stage = order[0] if order else "unknown"
    elif last_status not in SUCCESS_STATUSES:
        next_required_stage = current_stage
    elif current_stage in order:
        current_index = order.index(current_stage)
        next_required_stage = order[current_index + 1] if current_index + 1 < len(order) else "complete"
    else:
        next_required_stage = "verify" if acceptance_pending else "report"
    return {
        "current_stage": current_stage,
        "acceptance_pending": acceptance_pending,
        "next_required_stage": next_required_stage,
        "last_outcome_status": last_status,
        "evidence_available": evidence_available,
        "change_fingerprint": change_fingerprint,
    }


def session_start(payload: dict[str, Any]) -> None:
    cleanup_old_sessions()
    telemetry = latest_token_telemetry(payload)

    def update(state: dict[str, Any]) -> None:
        if telemetry:
            state["telemetry"] = telemetry

    state, _ = mutate_state(payload, update)
    source = str(payload.get("source") or "startup")
    base = (
        "Workflow Manager: availability only, not proof of effectiveness. Acceptance outranks "
        "context savings. Protocol: Contract > Evidence > Change > Verify > Report; skip irrelevant stages. "
        "Direct stays local. For Complex/Extensive work audit wall-clock gain; use owned read/write/test/research/"
        "review lanes and bias low-risk close calls parallel. Reuse unchanged evidence; "
        f"checkpoint before compaction. Pressure: {pressure_text(telemetry)}."
    )
    if source in {"compact", "resume"}:
        successful = [op for op in state.get("operations", []) if op.get("status") in SUCCESS_STATUSES][-6:]
        agent_counts = agent_activity_counts(state)
        digest = {
            "schema": SCHEMA_VERSION,
            "objective_fingerprint": state.get("objective", {}).get("fingerprint"),
            "last_route": state.get("last_route", {}).get("label"),
            "terminal_successes": [
                {"tool": op.get("tool"), "fingerprint": op.get("fingerprint")} for op in successful
            ],
            "agents_started": agent_counts["started"],
            "agents_completed": agent_counts["completed"],
            "active_agent_count": agent_counts["active"],
            "active_agent_scopes": active_agent_scope_summary(state),
            "guard_blocks": sum(1 for item in state.get("guards", []) if item.get("action") == "deny"),
            "outputs_compacted": sum(1 for item in state.get("operations", []) if item.get("compacted")),
            "oversized_outputs_preserved": sum(1 for item in state.get("operations", []) if item.get("oversized")),
            "continuity": quality_continuity(state),
            "runtime_escalations": sum(
                1 for item in state.get("guards", []) if item.get("kind") == "runtime_escalation"
            ),
            "current_stage": current_execution_stage(state),
            "compaction_count": len(state.get("compactions", [])),
        }
        base += (
            " Resume metadata is non-executable; semantic instructions come from the native summary. "
            f"Metadata: {json.dumps(digest, ensure_ascii=False, separators=(',', ':'))}. "
            "Rerun when inputs, state, device, freshness, or evidence changed."
        )
    emit_context("SessionStart", base)


FOLLOWUP_CONTROLS = {
    "continue",
    "go on",
    "next",
    "status",
    "update",
    "继续",
    "继续吧",
    "然后呢",
    "下一步",
    "进展",
    "进展呢",
    "怎么样了",
}

PROGRESS_MARKERS = (
    "already",
    "again",
    "completed",
    "done",
    "failed",
    "restarted",
    "still",
    "刚刚",
    "已经",
    "已完成",
    "完成了",
    "还是",
    "仍然",
    "仍旧",
    "又",
    "不行",
    "失败",
    "没好",
)
NEW_OBJECTIVE_MARKERS = (
    "another task",
    "new task",
    "now help me",
    "separately",
    "另外一个",
    "另一个任务",
    "新任务",
    "换个问题",
    "现在帮我",
    "再帮我",
    "顺便帮我",
)


def is_control_followup(prompt: str) -> bool:
    normalized = re.sub(r"[\s?!？！。,.，]+", " ", prompt.strip().lower()).strip()
    return normalized in FOLLOWUP_CONTROLS


def is_progress_followup(prompt: str) -> bool:
    normalized = re.sub(r"\s+", " ", prompt.strip().lower())
    if not normalized or len(normalized) > 220:
        return False
    if any(marker in normalized for marker in NEW_OBJECTIVE_MARKERS):
        return False
    if re.search(
        r"^(?:(?:i|it|this|that|build|test|device|app|the [a-z0-9_.-]+)\s+)?"
        r"(?:already|still|again|completed|done|failed|restarted)\b",
        normalized,
    ):
        return True
    chinese_prefix = normalized[:40]
    if any(marker in chinese_prefix for marker in PROGRESS_MARKERS if any("\u4e00" <= char <= "\u9fff" for char in marker)):
        directive = re.search(r"(?:帮我|请|新增|实现|修复|写一个|创建)", chinese_prefix)
        return directive is None or directive.start() > 20
    return False


def merge_followup_route(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    prior = safe_route(previous)
    if not prior:
        return current
    base = prior if ROUTE_RANK[prior["label"]] > ROUTE_RANK[current["label"]] else current
    result = dict(base)
    result["score"] = max(safe_int(prior.get("score")), safe_int(current.get("score")))
    result["phase_hints"] = list(
        dict.fromkeys([*as_list(prior.get("phase_hints")), *as_list(current.get("phase_hints"))])
    )[:8]
    lane_rank = {"none": 0, "sequential": 1, "possible": 2, "explicit": 3}
    result["lane_signal"] = max(
        (prior.get("lane_signal", "none"), current.get("lane_signal", "none")),
        key=lambda value: lane_rank.get(str(value), 0),
    )
    result["recommended_agent_cap"] = max(
        safe_int(prior.get("recommended_agent_cap")), safe_int(current.get("recommended_agent_cap"))
    )
    result["parallel_signal"] = bool(prior.get("parallel_signal") or current.get("parallel_signal"))
    result["delegation_opt_out"] = bool(
        prior.get("delegation_opt_out") or current.get("delegation_opt_out")
    )
    result["dependency_hint"] = current.get("dependency_hint") or prior.get("dependency_hint")
    result["route_source"] = "continued"
    result.pop("at", None)
    return decorate_route(result)


def routing_context(classification: dict[str, Any], telemetry: dict[str, Any]) -> str:
    label = str(classification.get("label") or "direct")
    shape = str(classification.get("workflow_shape") or "direct")
    order = ">".join(str(item) for item in as_list(classification.get("execution_order"))) or "work"
    cap = min(max(safe_int(classification.get("recommended_agent_cap")), 0), 3)
    mode = str(classification.get("agent_mode") or "local")
    if cap:
        agents = f"Agents: {mode}; subagent_cap={cap} ceiling; efficiency audit; positive-net owned lanes."
    elif label in {"complex", "extensive"}:
        agents = f"Agents: 0 ({mode}); serialize conflicts; re-audit side lanes."
    else:
        agents = "Agents: 0; do not delegate for pressure alone."
    pressure = telemetry.get("pressure")
    pressure_summary = f"{pressure:.1%}" if isinstance(pressure, (int, float)) else "unknown"
    gate = (
        " gate=checkpoint+stop-broad; required reasoning/evidence continue."
        if isinstance(pressure, (int, float)) and pressure >= PRESSURE_CHECKPOINT_THRESHOLD
        else ""
    )
    return (
        f"Route: {label}/{shape} | pressure={pressure_summary} | budget={classification.get('future_token_range')}. "
        f"Order: {order}. {agents}{gate} Control: bounded. "
        "Update: phase|done|next|blocker at kickoff/material change/~60s wait only; never per tool. "
        "Preflight path/input/acceptance; diagnose once; retry after material correction only. "
        "Acceptance in contract; verify to risk; never trim it; reuse only unchanged state/evidence."
    )


def user_prompt_submit(payload: dict[str, Any]) -> None:
    prompt = str(payload.get("prompt") or "")
    classification = classify_prompt(prompt)
    previous = snapshot_state(payload)
    continuation = is_control_followup(prompt) or is_progress_followup(prompt)
    if continuation and previous.get("last_route"):
        classification = merge_followup_route(previous["last_route"], classification)
    telemetry = latest_token_telemetry(payload)

    def update(state: dict[str, Any]) -> None:
        prompt_meta = text_metadata(prompt)
        if not continuation or not state.get("objective"):
            state["objective"] = {**prompt_meta, "updated_at": utc_now()}
        state["last_route"] = {**classification, "at": utc_now()}
        state.setdefault("prompts", []).append(
            {
                "at": utc_now(),
                "turn_id": safe_label(payload.get("turn_id"), 120) if payload.get("turn_id") else None,
                "prompt_meta": prompt_meta,
                **classification,
            }
        )
        if telemetry:
            state["telemetry"] = telemetry

    mutate_state(payload, update)
    pressure = telemetry.get("pressure")
    should_inject = classification["label"] in {"complex", "extensive"} or (
        isinstance(pressure, (int, float)) and pressure >= PRESSURE_TRIM_THRESHOLD
    )
    if not should_inject:
        return
    emit_context("UserPromptSubmit", routing_context(classification, telemetry))


def operation_is_recent(operation: dict[str, Any]) -> bool:
    try:
        timestamp = datetime.fromisoformat(str(operation.get("at"))).timestamp()
        return 0 <= time.time() - timestamp <= DUPLICATE_TTL_SECONDS
    except Exception:
        return False



def operation_failed(operation: dict[str, Any]) -> bool:
    status_value = str(operation.get("status") or "").lower()
    return status_value in ERROR_STATUSES or status_value.startswith("error")


def same_stage_action_count(state: dict[str, Any], turn_id: str, category: str) -> int:
    if turn_id == "unknown" or not category:
        return 0
    return sum(
        1
        for item in state.get("operations", [])
        if item.get("turn_id") == turn_id and item.get("category") == category
    )

def runtime_route_escalation(state: dict[str, Any], current_category: str) -> dict[str, Any] | None:
    current_label = str(state.get("last_route", {}).get("label") or "direct")
    if ROUTE_RANK.get(current_label, 0) >= ROUTE_RANK["complex"]:
        return None
    relevant = {
        "analysis",
        "build_package",
        "delivery_device",
        "evidence",
        "git",
        "implementation",
        "research",
        "verification",
    }
    categories = {
        str(item.get("category"))
        for item in state.get("operations", [])[-16:]
        if str(item.get("category")) in relevant
    }
    if current_category in relevant:
        categories.add(current_category)
    recent_errors = sum(
        1
        for item in state.get("operations", [])[-10:]
        if str(item.get("status") or "").startswith("error")
    )
    if len(categories) < 3 and recent_errors < 2:
        return None
    sequential = categories and categories <= {"build_package", "delivery_device", "evidence", "verification"}
    return decorate_route({
        "label": "complex",
        "score": max(safe_int(state.get("last_route", {}).get("score")), 2),
        "future_token_range": "15k-50k",
        "recommended_agent_cap": 0 if sequential else 2,
        "parallel_signal": False,
        "lane_signal": "sequential" if sequential else "possible",
        "phase_hints": sorted(categories)[:8],
        "dependency_hint": "shared_artifact_or_device" if sequential else None,
        "route_source": "runtime",
        "at": utc_now(),
    })


def handle_subagent_pretool(payload: dict[str, Any], state: dict[str, Any], fingerprint: str) -> bool:
    if not is_subagent_spawn_tool(payload):
        return False

    route_value = state.get("last_route")
    route_known = isinstance(route_value, dict) and bool(route_value)
    route = safe_route(route_value)
    gate = str(route.get("delegation_gate") or "closed")
    cap = safe_int(route.get("recommended_agent_cap"))
    reaudited = gate == "closed" and request_supports_delegation_reaudit(payload, route)
    if reaudited:
        gate = "audit"
        cap = 1
    active = active_agent_records(state)
    task_name, scope_fingerprint = subagent_request_fields(payload)
    duplicate_scope = any(
        (task_name and item.get("task_name") == task_name)
        or (scope_fingerprint and item.get("scope_fingerprint") == scope_fingerprint)
        for item in active
    )

    deny_kind = None
    deny_reason = None
    if not route_known:
        deny_kind = "subagent_route_missing"
        deny_reason = (
            "Subagent spawn denied before start: no persisted route is available. Restore or resubmit the "
            "current objective before delegating."
        )
    elif gate == "closed":
        deny_kind = "subagent_gate"
        deny_reason = (
            "Subagent spawn denied before start: the delegation gate is closed by the current route "
            "(for example, focused work, dependency ordering, or a shared resource). Keep this work serialized."
        )
    elif duplicate_scope:
        deny_kind = "subagent_duplicate"
        deny_reason = "Subagent spawn denied before start: an active agent already owns the same task name or scope."
    elif len(active) >= cap:
        deny_kind = "subagent_cap"
        deny_reason = f"Subagent spawn denied before start: active={len(active)}, cap={cap}; reuse or stop a lane first."

    if deny_reason:
        def record_guard(current: dict[str, Any]) -> None:
            current.setdefault("guards", []).append(
                {
                    "at": utc_now(),
                    "turn_id": safe_label(payload.get("turn_id"), 120) if payload.get("turn_id") else None,
                    "kind": deny_kind,
                    "action": "deny",
                    "fingerprint": fingerprint,
                }
            )

        mutate_state(payload, record_guard)
        emit_pretool_deny(deny_reason)
        return True

    def record_request(current: dict[str, Any]) -> None:
        current.setdefault("subagents", []).append(
            {
                "at": utc_now(),
                "event": "request",
                "turn_id": safe_label(payload.get("turn_id"), 120) if payload.get("turn_id") else None,
                "agent_id": None,
                "agent_type": None,
                "task_name": task_name,
                "scope_fingerprint": scope_fingerprint,
                "request_fingerprint": fingerprint,
                "objective_fingerprint": current.get("objective", {}).get("fingerprint"),
                "stale": False,
                "status": "pending",
                "request_gate": gate,
                "request_cap": cap,
                "reaudited": reaudited,
            }
        )

    mutate_state(payload, record_request)
    if reaudited:
        emit_context(
            "PreToolUse",
            "Delegation re-audit accepted one positive-utility side lane: its prompt proves that the child is "
            "independent, ready now, and either read-only or the exclusive write owner. Shared build/device work "
            "remains with the parent. Keep the canonical task_name schema-safe; describe the "
            "child's purpose concisely in Chinese in user-facing updates.",
        )
    elif route_known and gate == "audit":
        emit_context(
            "PreToolUse",
            "Delegation gate is audit: this spawn is within the ceiling; proceed when expected wall-clock benefit "
            "exceeds coordination/collision cost. The lane may read, write, test, research, or review when it is "
            "ready and has non-overlapping ownership. Keep the canonical task_name schema-safe and use a concise "
            "Chinese purpose summary in user-facing updates.",
        )
    return True


def pre_tool_use(payload: dict[str, Any]) -> None:
    fingerprint, tool = tool_fingerprint(payload)
    guard = command_guard(payload)
    if guard:
        kind, reason = guard

        def record_guard(state: dict[str, Any]) -> None:
            state.setdefault("guards", []).append(
                {
                    "at": utc_now(),
                    "turn_id": safe_label(payload.get("turn_id"), 120) if payload.get("turn_id") else None,
                    "kind": kind,
                    "action": "deny",
                    "fingerprint": fingerprint,
                }
            )

        mutate_state(payload, record_guard)
        emit_pretool_deny(reason)
        return

    state = snapshot_state(payload)
    if handle_subagent_pretool(payload, state, fingerprint):
        return
    duplicate = next(
        (
            op
            for op in reversed(state.get("operations", []))
            if op.get("fingerprint") == fingerprint
            and op.get("status") in SUCCESS_STATUSES
            and operation_is_recent(op)
        ),
        None,
    )
    turn_id = safe_label(payload.get("turn_id"), 120) if payload.get("turn_id") else "unknown"
    current_category = command_category(payload)
    failed_duplicate = next(
        (
            op
            for op in reversed(state.get("operations", []))
            if op.get("fingerprint") == fingerprint
            and operation_failed(op)
            and operation_is_recent(op)
        ),
        None,
    )
    failure_already_noticed = any(
        item.get("kind") == "unchanged_failure"
        and item.get("fingerprint") == fingerprint
        and item.get("turn_id") == turn_id
        for item in state.get("guards", [])
    )
    stage_count = same_stage_action_count(state, turn_id, current_category)
    stage_already_noticed = any(
        item.get("kind") == "stage_budget" and item.get("turn_id") == turn_id
        for item in state.get("guards", [])
    )
    duplicate_already_noticed = any(
        item.get("fingerprint") == fingerprint and item.get("turn_id") == turn_id
        for item in state.get("duplicate_notices", [])
    )
    escalation = runtime_route_escalation(state, current_category)
    escalation_already_noticed = any(
        item.get("kind") == "runtime_escalation" and item.get("turn_id") == turn_id
        for item in state.get("guards", [])
    )
    telemetry = latest_token_telemetry(payload) or safe_telemetry(state.get("telemetry"))
    pressure = telemetry.get("pressure")
    pressure_notices = [
        kind
        for threshold, kind in (
            (PRESSURE_TRIM_THRESHOLD, "pressure_55"),
            (PRESSURE_CHECKPOINT_THRESHOLD, "pressure_70"),
        )
        if isinstance(pressure, (int, float))
        and pressure >= threshold
        and not any(item.get("kind") == kind for item in state.get("guards", []))
    ]
    notify_duplicate = bool(duplicate and not duplicate_already_noticed)
    notify_escalation = bool(escalation and not escalation_already_noticed)
    notify_failure = bool(failed_duplicate and not failure_already_noticed)
    notify_stage = bool(stage_count >= 25 and not stage_already_noticed)
    if not any((notify_duplicate, notify_escalation, notify_failure, notify_stage, pressure_notices)):
        return

    def update(current: dict[str, Any]) -> None:
        if notify_duplicate:
            current.setdefault("duplicate_notices", []).append(
                {"fingerprint": fingerprint, "turn_id": turn_id, "at": utc_now()}
            )
        if notify_escalation and escalation:
            current["last_route"] = escalation
            current.setdefault("guards", []).append(
                {
                    "at": utc_now(),
                    "turn_id": turn_id,
                    "kind": "runtime_escalation",
                    "action": "advise",
                    "fingerprint": fingerprint,
                }
            )
        for kind, enabled in (
            ("unchanged_failure", notify_failure),
            ("stage_budget", notify_stage),
        ):
            if enabled:
                current.setdefault("guards", []).append(
                    {
                        "at": utc_now(),
                        "turn_id": turn_id,
                        "kind": kind,
                        "action": "advise",
                        "fingerprint": fingerprint if kind == "unchanged_failure" else None,
                    }
                )
        for kind in pressure_notices:
            current.setdefault("guards", []).append(
                {
                    "at": utc_now(),
                    "turn_id": turn_id,
                    "kind": kind,
                    "action": "advise",
                    "fingerprint": None,
                }
            )
        if telemetry:
            current["telemetry"] = telemetry

    _, changed = mutate_state(payload, update)
    if not changed:
        return
    notices: list[str] = []
    if notify_duplicate:
        notices.append(
            f"Duplicate-success hint: tool={tool}, fingerprint={fingerprint} (no command/result text). Reuse only if "
            "input, cwd, files, device/external state, freshness, and native evidence are unchanged; otherwise rerun "
            "the narrow check."
        )
    if notify_failure:
        notices.append(
            "Unchanged failure already exists for this tool/input: keep the first error, diagnose once, and retry "
            "only after a material correction or one bounded alternate route."
        )
    if notify_stage:
        notices.append("Stage action budget reached (~25): checkpoint and reclassify before more broad/equivalent work, then continue any reasoning or verification still required.")
    if "pressure_55" in pressure_notices:
        notices.append("Context pressure crossed 55%: trim redundant presentation only; preserve all reasoning and evidence needed for correctness. This does not raise the subagent cap.")
    if "pressure_70" in pressure_notices:
        notices.append("Context pressure crossed 70%: checkpoint before unfocused work, then continue required reasoning, evidence, and verification narrowly or after native compaction. This does not raise the subagent cap.")
    if notify_escalation and escalation:
        phases = ",".join(escalation.get("phase_hints") or [])
        order = " > ".join(escalation.get("execution_order") or [])
        cap = safe_int(escalation.get("recommended_agent_cap"))
        notices.append(
            f"Runtime re-route: complex | observed={phases} | order={order} | agent_cap={cap}. "
            "Audit for positive-net independent read/write/test/research/review lanes and continue useful parent "
            "work. Serialize only conflicting build/deploy/device stages. Update: phase | done | next | blocker."
        )
    emit_context("PreToolUse", " ".join(notices))
def post_tool_use(payload: dict[str, Any]) -> None:
    fingerprint, tool = tool_fingerprint(payload)
    response = payload.get("tool_response")
    status_value = response_status(response)
    command = extract_command(payload)
    category = command_category(payload, command)
    risk_kind = command_risk_kind(payload, command)
    response_meta, _ = analyze_tool_response(response)
    previous = snapshot_state(payload)
    telemetry = latest_token_telemetry(payload) or safe_telemetry(previous.get("telemetry"))
    oversized = output_needs_compaction(response_meta, telemetry)
    compacted = False
    budgeted = command_output_budget(payload, command, risk_kind) if command and risk_kind else False

    def update(state: dict[str, Any]) -> None:
        state.setdefault("operations", []).append(
            {
                "at": utc_now(),
                "turn_id": safe_label(payload.get("turn_id"), 120) if payload.get("turn_id") else None,
                "tool": tool,
                "fingerprint": fingerprint,
                "status": status_value,
                "category": category,
                "risk_kind": risk_kind,
                **response_meta,
                "budgeted": budgeted,
                "oversized": oversized,
                "compacted": compacted,
            }
        )
        if telemetry:
            state["telemetry"] = telemetry

    mutate_state(payload, update)
    if oversized:
        emit_posttool_advisory(status_value, response_meta)
def compact_event(payload: dict[str, Any], phase: str) -> None:
    telemetry = latest_token_telemetry(payload)

    def update(state: dict[str, Any]) -> None:
        if phase == "post":
            state["guards"] = [
                item
                for item in state.get("guards", [])
                if item.get("kind") not in {"pressure_55", "pressure_70", "pressure_75"}
            ]
        state.setdefault("compactions", []).append(
            {
                "at": utc_now(),
                "phase": phase,
                "trigger": safe_label(payload.get("trigger"), 32) if payload.get("trigger") else None,
                "turn_id": safe_label(payload.get("turn_id"), 120) if payload.get("turn_id") else None,
                "telemetry": telemetry,
                "objective_meta": state.get("objective", {}),
                "current_stage": current_execution_stage(state),
                "continuity": quality_continuity(state),
                "active_agent_scopes": active_agent_scope_summary(state),
                "recent_successes": [
                    op.get("fingerprint")
                    for op in state.get("operations", [])
                    if op.get("status") in SUCCESS_STATUSES
                ][-8:],
            }
        )
        if telemetry:
            state["telemetry"] = telemetry

    mutate_state(payload, update)
    emit_continue()


def task_name_from_payload(payload: dict[str, Any]) -> str | None:
    for key in ("task_name", "agent_name", "name"):
        if payload.get(key):
            return safe_label(payload.get(key), 120)
    return None


def active_agent_records(state: dict[str, Any]) -> list[dict[str, Any]]:
    active: dict[str, dict[str, Any]] = {}
    for item in state.get("subagents", []):
        agent_id = str(item.get("agent_id") or "")
        if not agent_id:
            continue
        if item.get("event") == "start":
            active[agent_id] = item
        elif item.get("event") == "stop":
            active.pop(agent_id, None)
    return list(active.values())


def active_agent_scope_summary(state: dict[str, Any]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for item in active_agent_records(state):
        summary = {
            "agent_fingerprint": stable_hash(item.get("agent_id")),
            "task_fingerprint": stable_hash(item.get("task_name")) if item.get("task_name") else None,
            "scope_fingerprint": item.get("scope_fingerprint"),
            "objective_fingerprint": item.get("objective_fingerprint"),
        }
        safe_summary = _safe_active_agent_scope(summary)
        if safe_summary:
            summaries.append(safe_summary)
    return summaries[:8]


def pending_subagent_request(state: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any] | None:
    used = {
        str(item.get("request_fingerprint"))
        for item in state.get("subagents", [])
        if item.get("event") == "start" and item.get("request_fingerprint")
    }
    candidates = [
        item
        for item in state.get("subagents", [])
        if item.get("event") == "request"
        and item.get("request_fingerprint")
        and str(item.get("request_fingerprint")) not in used
    ]
    if not candidates:
        return None
    turn_id = safe_label(payload.get("turn_id"), 120) if payload.get("turn_id") else None
    if turn_id:
        same_turn = [item for item in candidates if item.get("turn_id") == turn_id]
        if same_turn:
            return same_turn[-1]
    return candidates[-1]


def subagent_start(payload: dict[str, Any]) -> None:
    previous = snapshot_state(payload)
    request = pending_subagent_request(previous, payload) or {}
    scope_value = payload.get("prompt") or payload.get("task") or payload.get("message")
    scope_fingerprint = (stable_hash(scope_value) if scope_value else None) or request.get("scope_fingerprint")
    task_name = task_name_from_payload(payload) or request.get("task_name")
    request_fingerprint = request.get("request_fingerprint")
    active = active_agent_records(previous)
    objective_fingerprint = request.get("objective_fingerprint") or previous.get("objective", {}).get("fingerprint")
    route = safe_route(previous.get("last_route"))
    gate = str(request.get("request_gate") or route.get("delegation_gate") or "closed")
    cap = safe_int(request.get("request_cap")) or safe_int(route.get("recommended_agent_cap"))
    duplicate_scope = any(
        (task_name and item.get("task_name") == task_name)
        or (scope_fingerprint and item.get("scope_fingerprint") == scope_fingerprint)
        for item in active
    )
    over_cap = len(active) >= cap

    def update(state: dict[str, Any]) -> None:
        state.setdefault("subagents", []).append(
            {
                "at": utc_now(),
                "event": "start",
                "turn_id": safe_label(payload.get("turn_id"), 120) if payload.get("turn_id") else None,
                "agent_id": safe_label(payload.get("agent_id"), 120),
                "agent_type": safe_label(payload.get("agent_type"), 80),
                "task_name": task_name,
                "scope_fingerprint": scope_fingerprint,
                "request_fingerprint": request_fingerprint,
                "objective_fingerprint": objective_fingerprint,
                "stale": False,
                "status": "running",
            }
        )

    _, changed = mutate_state(payload, update)
    warnings: list[str] = []
    if duplicate_scope and changed:
        warnings.append("Existing active subagent already has the same task name or scope; reuse it or send one bounded follow-up.")
    if over_cap and changed:
        warnings.append(f"Subagent cap exceeded: active={len(active)}, cap={cap}; stop or reuse a lane before adding more.")
    if gate == "closed" and changed:
        warnings.append("Delegation gate is closed by dependency/shared-resource policy; keep the dependent work serialized.")
    elif gate == "audit" and changed:
        warnings.append(
            "Delegation gate is audit: the cap is only a ceiling; use it for ready lanes with positive net "
            "wall-clock benefit and clear non-overlapping ownership."
        )
    warning_text = (" " + " ".join(warnings)) if warnings else ""
    emit_context(
        "SubagentStart",
        "Workflow Manager subagent contract: stay inside the assigned scope, do not redo parent or sibling work, keep "
        "raw logs out of the result, and return only decisive evidence, exact paths/identifiers, uncertainty, "
        f"verification, and the next action. Use a concise Chinese purpose summary in user-facing updates.{warning_text}",
    )
def subagent_stop(payload: dict[str, Any]) -> None:
    result = payload.get("last_assistant_message")
    declared_status = str(payload.get("status") or "").lower()
    status_value = declared_status if declared_status in ERROR_STATUSES | {"completed", "ok"} else "unknown"
    agent_id = safe_label(payload.get("agent_id"), 120)
    previous = snapshot_state(payload)
    started = next(
        (item for item in reversed(active_agent_records(previous)) if item.get("agent_id") == agent_id),
        None,
    )
    started_objective = str((started or {}).get("objective_fingerprint") or "")
    current_objective = str(previous.get("objective", {}).get("fingerprint") or "")
    stale = bool(started_objective and current_objective and started_objective != current_objective)

    def update(state: dict[str, Any]) -> None:
        state.setdefault("subagents", []).append(
            {
                "at": utc_now(),
                "event": "stop",
                "agent_id": agent_id,
                "agent_type": safe_label(payload.get("agent_type"), 80),
                "task_name": task_name_from_payload(payload) or (started or {}).get("task_name"),
                "scope_fingerprint": (started or {}).get("scope_fingerprint"),
                "objective_fingerprint": started_objective or None,
                "stale": stale,
                "status": status_value,
                "result_meta": text_metadata(result),
            }
        )

    mutate_state(payload, update)
    if stale:
        emit_context(
            "SubagentStop",
            "Stale subagent result: the objective changed after this agent started. Use it only as verification; "
            "it must not drive mutation for the previous objective.",
        )
    else:
        emit_continue()
def stop(payload: dict[str, Any]) -> None:
    def update(state: dict[str, Any]) -> None:
        state["last_assistant"] = text_metadata(payload.get("last_assistant_message"))
        state["last_stop_at"] = utc_now()

    mutate_state(payload, update)
    emit_continue()


HANDLERS: dict[str, Callable[[dict[str, Any]], None]] = {
    "SessionStart": session_start,
    "UserPromptSubmit": user_prompt_submit,
    "PreToolUse": pre_tool_use,
    "PostToolUse": post_tool_use,
    "PreCompact": lambda payload: compact_event(payload, "pre"),
    "PostCompact": lambda payload: compact_event(payload, "post"),
    "SubagentStart": subagent_start,
    "SubagentStop": subagent_stop,
    "Stop": stop,
}


def fail_open(event: str) -> None:
    if event in {"PreCompact", "PostCompact", "SubagentStop", "Stop"}:
        emit_continue()


def report_error(event: str, error: Exception) -> None:
    if os.environ.get("TOKEN_FRUGAL_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}:
        print(f"workflow-manager hook {safe_label(event)} failed: {type(error).__name__}", file=sys.stderr)


def main() -> int:
    event = ""
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        event = str(payload.get("hook_event_name") or "")
        refresh_related_states()
        handler = HANDLERS.get(event)
        if handler:
            handler(payload)
    except Exception as error:
        report_error(event, error)
        fail_open(event)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
