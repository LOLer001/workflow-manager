#!/usr/bin/env python3
"""Fail-open lifecycle hook for compact, privacy-safe Codex task continuity."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import sys
import tempfile
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator


SCHEMA_VERSION = 13
WRITER_VERSION = "1.0.26"
DOMAIN_CLASSIFIER_VERSION = "1"
DIFFICULTY_CLASSIFIER_VERSION = "1"
EXECUTION_PROFILE_VERSION = "1"
STABLE_SKILL_NAME = "workflow-manager"
STABLE_SKILL_SCHEMA = 1
STABLE_SKILL_MARKER = ".workflow-manager-managed.json"
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

MODEL_PROFILES = {"current", "work_assessment", "work_executor_low_latest"}
EXECUTOR_STATES = {
    "none",
    "spawn_required",
    "spawn_pending",
    "running",
    "succeeded",
    "recovery_required",
    "exhausted",
}
EXECUTOR_FAILURE_KINDS = {
    "model_unavailable",
    "invalid_spawn_config",
    "spawn_failed",
    "start_mismatch",
    "stale_contract",
    "executor_failed",
    "implementation_failed",
    "build_failed",
    "deploy_failed",
    "verification_failed",
}
MAX_EXECUTOR_ATTEMPTS = 2
CAUSAL_REVIEW_STATES = {"none", "triage_required", "triaging", "resolved"}
CAUSAL_REVIEW_OUTCOMES = {"introduced", "fix_ineffective", "unrelated", "uncertain"}
BASELINE_ACCEPTANCE_STATUSES = {"passed", "failed", "incomplete", "unknown"}
REFERENCE_ACCEPTANCE_STATES = {"disabled", "planned", "candidate", "accepted", "failed"}

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


def execution_contract_id(state: dict[str, Any]) -> str | None:
    """Bind one executor to the exact confirmed objective and plan without storing plan text."""
    objective = safe_fingerprint(state.get("objective", {}).get("fingerprint"))
    difficulty = safe_fingerprint(state.get("difficulty_decision_id"))
    plan = safe_fingerprint(state.get("plan_digest"))
    generation = max(safe_int(state.get("plan_generation")), 0)
    if not (objective and difficulty and plan and generation > 0):
        return None
    material = f"{EXECUTION_PROFILE_VERSION}\0{objective}\0{difficulty}\0{generation}\0{plan}"
    review = state.get("causal_review") if isinstance(state.get("causal_review"), dict) else {}
    review_id = safe_fingerprint(review.get("review_id"))
    if review.get("state") == "resolved" and review_id:
        material += f"\0{review_id}"
    reference = _safe_reference_acceptance(state.get("reference_acceptance"))
    if reference["enabled"] and reference["contract_digest"]:
        material += f"\0{reference['contract_digest']}"
    return stable_hash(material, 32)


def reference_requested(prompt: str) -> bool:
    """Opt in only for an explicit request to match a supplied reference."""
    lower = prompt.lower()
    return bool(
        re.search(r"\b(?:reference[- ]driven|match (?:the )?reference|visual fidelity|faithful(?:ly)? reproduce)\b", lower)
        or any(token in prompt for token in ("以参考为准", "对齐参考", "复刻参考", "与参考一致", "视觉保真", "行为保真"))
        or bool(re.search(r"(?:按|以)\s*[A-Za-z\u4e00-\u9fff][\w\u4e00-\u9fff -]{1,28}\s*(?:效果|界面|动画|视觉|行为|主题\d*)\s*为(?:准|参考)", prompt))
        or bool(re.search(r"(?:对齐|复刻|还原)\s*[A-Za-z\u4e00-\u9fff][\w\u4e00-\u9fff -]{1,28}\s*(?:效果|界面|动画|视觉|行为|主题\d*)", prompt))
        or bool(re.search(r"与\s*[A-Za-z\u4e00-\u9fff][\w\u4e00-\u9fff -]{1,28}\s*(?:效果|界面|动画|视觉|行为|主题\d*)\s*一致", prompt))
    )


def reference_contract_changed(prompt: str) -> bool:
    return bool(re.search(r"(?:版本|version|方向|orientation|视口|viewport|场景|scene|时相|phase|稳定态|过渡态)", prompt, re.I))


def _safe_reference_acceptance(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        item = {}
    enabled = bool(item.get("enabled"))
    state_value = item.get("state") if item.get("state") in REFERENCE_ACCEPTANCE_STATES else "disabled"
    return {
        "enabled": enabled,
        "contract_digest": safe_fingerprint(item.get("contract_digest")) or None,
        "reference_fingerprint": safe_fingerprint(item.get("reference_fingerprint")) or None,
        "version_fingerprint": safe_fingerprint(item.get("version_fingerprint")) or None,
        "phase": safe_label(item.get("phase"), 32) if item.get("phase") else "unknown",
        "state": state_value if enabled else "disabled",
        "engineering_health": item.get("engineering_health") in {"unknown", "passed", "failed"} and item.get("engineering_health") or "unknown",
        "functional_acceptance": item.get("functional_acceptance") in {"unknown", "passed", "failed"} and item.get("functional_acceptance") or "unknown",
        "fidelity_candidate": item.get("fidelity_candidate") in {"unknown", "candidate", "failed"} and item.get("fidelity_candidate") or "unknown",
        "user_final_acceptance": item.get("user_final_acceptance") in {"unknown", "accepted", "failed"} and item.get("user_final_acceptance") or "unknown",
        "evidence_digest": safe_fingerprint(item.get("evidence_digest")) or None,
    }


def _fingerprint_digest(values: list[str]) -> str | None:
    bounded = [value for value in values if re.fullmatch(r"[0-9a-f]{8,64}", value)]
    return stable_hash("\0".join(bounded), 32) if bounded else None


def build_execution_baseline(state: dict[str, Any]) -> dict[str, Any] | None:
    """Seal bounded facts for the last contract without treating agent completion as acceptance."""
    contract_id = safe_fingerprint(state.get("execution_contract_id"))
    objective = safe_fingerprint(state.get("objective", {}).get("fingerprint"))
    plan = safe_fingerprint(state.get("plan_digest"))
    if not (contract_id and objective and plan):
        return None
    operations = [
        item
        for item in state.get("operations", [])
        if isinstance(item, dict) and item.get("execution_contract_id") == contract_id
    ]
    change_categories = {"implementation", "build_package", "delivery_device"}
    verification_categories = {"verification", "evidence"}
    change_indexes = [
        index
        for index, item in enumerate(operations)
        if item.get("category") in change_categories and item.get("status") in SUCCESS_STATUSES
    ]
    last_change_index = max(change_indexes, default=-1)
    changes = [
        str(operations[index].get("fingerprint") or "") for index in change_indexes
    ]
    verification = [
        item
        for index, item in enumerate(operations)
        if index > last_change_index and item.get("category") in verification_categories
    ]
    verification_digest = _fingerprint_digest(
        [str(item.get("fingerprint") or "") for item in verification]
    )
    if not verification:
        acceptance = "incomplete"
    else:
        last_status = str(verification[-1].get("status") or "unknown")
        if last_status in SUCCESS_STATUSES:
            acceptance = "passed"
        elif last_status in ERROR_STATUSES or last_status.startswith("error"):
            acceptance = "failed"
        else:
            acceptance = "incomplete"
    change_digest = _fingerprint_digest(changes)
    baseline_id = stable_hash(
        f"{objective}\0{plan}\0{contract_id}\0{change_digest or ''}\0{verification_digest or ''}",
        32,
    )
    return {
        "baseline_id": baseline_id,
        "objective_fingerprint": objective,
        "plan_digest": plan,
        "execution_contract_id": contract_id,
        "change_set_digest": change_digest,
        "verification_digest": verification_digest,
        "acceptance_status": acceptance,
    }


def reset_executor_binding(state: dict[str, Any], *, preserve_failure: bool = False) -> None:
    failure = state.get("executor_failure_kind") if preserve_failure else None
    attempt = safe_int(state.get("executor_attempt")) if preserve_failure else 0
    state["execution_profile_version"] = EXECUTION_PROFILE_VERSION
    state["executor_state"] = "recovery_required" if failure else "none"
    state["execution_contract_id"] = None
    state["executor_agent_id"] = None
    state["executor_attempt"] = min(max(attempt, 0), MAX_EXECUTOR_ATTEMPTS)
    state["executor_failure_kind"] = failure if failure in EXECUTOR_FAILURE_KINDS else None
    state["executor_model"] = None
    state["executor_reasoning_effort"] = None
    state["executor_fork_turns"] = None


def initialize_confirmed_executor(state: dict[str, Any]) -> bool:
    contract_id = execution_contract_id(state)
    if not contract_id:
        return False
    if state.get("execution_contract_id") != contract_id:
        reset_executor_binding(state)
        state["execution_contract_id"] = contract_id
        state["executor_state"] = "spawn_required"
    elif state.get("executor_state") not in EXECUTOR_STATES - {"none"}:
        state["executor_state"] = "spawn_required"
    state["execution_profile_version"] = EXECUTION_PROFILE_VERSION
    state["model_profile"] = "work_executor_low_latest"
    return True


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
        "task_domain": value.get("task_domain")
        if value.get("task_domain") in {"daily", "work", "unknown"}
        else "unknown",
        "domain_confidence": value.get("domain_confidence")
        if value.get("domain_confidence") in {"low", "medium", "high"}
        else "low",
        "domain_rule_codes": [
            safe_label(item, 48)
            for item in as_list(value.get("domain_rule_codes"))
            if item
        ][:8],
        "model_profile": value.get("model_profile")
        if value.get("model_profile") in MODEL_PROFILES
        else "current",
        "domain_classifier_version": safe_label(
            value.get("domain_classifier_version") or DOMAIN_CLASSIFIER_VERSION, 16
        ),
        "domain_decision_id": safe_fingerprint(value.get("domain_decision_id")) or None,
        "work_difficulty": value.get("work_difficulty")
        if value.get("work_difficulty") in {"not_applicable", "simple", "hard", "unknown"}
        else "unknown",
        "difficulty_confidence": value.get("difficulty_confidence")
        if value.get("difficulty_confidence") in {"low", "medium", "high"}
        else "low",
        "difficulty_rule_codes": [
            safe_label(item, 48)
            for item in as_list(value.get("difficulty_rule_codes"))
            if item
        ][:8],
        "difficulty_classifier_version": safe_label(
            value.get("difficulty_classifier_version") or DIFFICULTY_CLASSIFIER_VERSION, 16
        ),
        "difficulty_decision_id": safe_fingerprint(value.get("difficulty_decision_id")) or None,
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


def _real_directory(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)


def _stable_skill_source(plugin_root: Path | None = None) -> Path:
    configured = plugin_root or os.environ.get("PLUGIN_ROOT")
    root = Path(configured) if configured else Path(__file__).resolve().parents[1]
    return root / "assets" / "stable-skill" / STABLE_SKILL_NAME


def _codex_home(configured: Path | None = None) -> Path:
    if configured is not None:
        return configured
    env_home = os.environ.get("CODEX_HOME")
    if env_home:
        return Path(env_home)
    return Path.home() / ".codex"


def _stable_source_files(source: Path) -> tuple[dict[str, bytes], str] | None:
    if not _real_directory(source):
        return None
    files: dict[str, bytes] = {}
    digest = hashlib.sha256()
    try:
        for path in sorted(source.rglob("*"), key=lambda item: item.as_posix()):
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                return None
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
                return None
            relative = path.relative_to(source).as_posix()
            payload = path.read_bytes()
            files[relative] = payload
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(payload)
            digest.update(b"\0")
    except OSError:
        return None
    if "SKILL.md" not in files:
        return None
    return files, digest.hexdigest()


def _managed_skill_marker(path: Path) -> dict[str, Any] | None:
    marker = path / STABLE_SKILL_MARKER
    try:
        info = marker.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_size > 64 * 1024
        ):
            return None
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(value, dict) or value.get("managed_by") != STABLE_SKILL_NAME:
        return None
    return value


def _ensure_real_parents(root: Path, relative: Path) -> Path:
    current = root
    for part in relative.parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o700)
        else:
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise OSError(f"unsafe managed Skill directory: {current}")
    return current


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise OSError(f"unsafe managed Skill file: {path}")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def sync_stable_skill(
    plugin_root: Path | None = None,
    codex_home: Path | None = None,
) -> dict[str, Any]:
    """Synchronize the bundled Skill into a stable, user-scoped, unversioned path."""
    source = _stable_skill_source(plugin_root)
    source_data = _stable_source_files(source)
    home = _codex_home(codex_home)
    skills_root = home / "skills"
    target = skills_root / STABLE_SKILL_NAME
    result: dict[str, Any] = {"path": str(target)}
    if source_data is None:
        return {**result, "status": "missing_or_unsafe_source"}
    files, digest = source_data
    marker_payload = {
        "schema": STABLE_SKILL_SCHEMA,
        "managed_by": STABLE_SKILL_NAME,
        "writer_version": WRITER_VERSION,
        "source_digest": digest,
        "files": sorted(files),
    }
    try:
        try:
            skills_root_info = skills_root.lstat()
        except FileNotFoundError:
            skills_root.mkdir(parents=True, mode=0o700)
        else:
            if (
                not stat.S_ISDIR(skills_root_info.st_mode)
                or stat.S_ISLNK(skills_root_info.st_mode)
            ):
                return {**result, "status": "unsafe_skills_root"}
        with state_lock(skills_root / f".{STABLE_SKILL_NAME}-sync", timeout=2.0):
            try:
                target_info = target.lstat()
            except FileNotFoundError:
                target_info = None
            if target_info is not None:
                if (
                    not stat.S_ISDIR(target_info.st_mode)
                    or stat.S_ISLNK(target_info.st_mode)
                ):
                    return {**result, "status": "unsafe_target"}
                current_marker = _managed_skill_marker(target)
                if current_marker is None:
                    return {**result, "status": "unmanaged_target"}
                if (
                    current_marker.get("source_digest") == digest
                    and current_marker.get("writer_version") == WRITER_VERSION
                ):
                    return {**result, "status": "current", "digest": digest}
                for relative, payload in files.items():
                    relative_path = Path(relative)
                    parent = _ensure_real_parents(target, relative_path.parent)
                    _atomic_write_bytes(parent / relative_path.name, payload)
                _atomic_write_bytes(
                    target / STABLE_SKILL_MARKER,
                    (json.dumps(marker_payload, ensure_ascii=False, sort_keys=True) + "\n").encode(
                        "utf-8"
                    ),
                )
                return {**result, "status": "updated", "digest": digest}

            staging = Path(
                tempfile.mkdtemp(prefix=f".{STABLE_SKILL_NAME}.", dir=str(skills_root))
            )
            try:
                for relative, payload in files.items():
                    relative_path = Path(relative)
                    parent = _ensure_real_parents(staging, relative_path.parent)
                    _atomic_write_bytes(parent / relative_path.name, payload)
                _atomic_write_bytes(
                    staging / STABLE_SKILL_MARKER,
                    (json.dumps(marker_payload, ensure_ascii=False, sort_keys=True) + "\n").encode(
                        "utf-8"
                    ),
                )
                os.replace(staging, target)
            finally:
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)
            return {**result, "status": "installed", "digest": digest}
    except TimeoutError:
        return {**result, "status": "lock_timeout"}
    except OSError as error:
        return {
            **result,
            "status": "write_error",
            "error": safe_label(type(error).__name__, 48),
        }


def new_state(payload: dict[str, Any]) -> dict[str, Any]:
    now = utc_now()
    return {
        "schema_version": SCHEMA_VERSION,
        "writer_version": WRITER_VERSION,
        "session_fingerprint": stable_hash(payload.get("session_id") or payload.get("hook_run_id")),
        "cwd_fingerprint": stable_hash(payload.get("cwd")),
        "model": safe_label(payload.get("model"), 80) if payload.get("model") else None,
        "task_domain": "unknown",
        "domain_confidence": "low",
        "domain_rule_codes": [],
        "model_profile": "current",
        "domain_classifier_version": DOMAIN_CLASSIFIER_VERSION,
        "domain_decision_id": None,
        "work_difficulty": "unknown",
        "difficulty_confidence": "low",
        "difficulty_rule_codes": [],
        "difficulty_classifier_version": DIFFICULTY_CLASSIFIER_VERSION,
        "difficulty_decision_id": None,
        "assessor_state": "none",
        "assessor_agent_id": None,
        "assessor_model": None,
        "assessor_reasoning_effort": None,
        "assessor_input_fingerprint": None,
        "plan_state": "none",
        "plan_generation": 0,
        "plan_digest": None,
        "plan_objective_fingerprint": None,
        "plan_difficulty_decision_id": None,
        "confirmed_plan_digest": None,
        "confirmed_at": None,
        "execution_profile_version": EXECUTION_PROFILE_VERSION,
        "executor_state": "none",
        "execution_contract_id": None,
        "executor_agent_id": None,
        "executor_attempt": 0,
        "executor_failure_kind": None,
        "executor_model": None,
        "executor_reasoning_effort": None,
        "executor_fork_turns": None,
        "reference_acceptance": _safe_reference_acceptance(None),
        "last_execution_baseline": {},
        "causal_review": {
            "state": "none",
            "review_id": None,
            "report_fingerprint": None,
            "baseline_id": None,
            "outcome": None,
            "evidence_digest": None,
        },
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
        "task_domain": item.get("task_domain")
        if item.get("task_domain") in {"daily", "work", "unknown"}
        else "unknown",
        "domain_confidence": item.get("domain_confidence")
        if item.get("domain_confidence") in {"low", "medium", "high"}
        else "low",
        "domain_rule_codes": [
            safe_label(value, 48)
            for value in as_list(item.get("domain_rule_codes"))
            if value
        ][:8],
        "model_profile": item.get("model_profile")
        if item.get("model_profile") in MODEL_PROFILES
        else "current",
        "domain_classifier_version": safe_label(
            item.get("domain_classifier_version") or DOMAIN_CLASSIFIER_VERSION, 16
        ),
        "domain_decision_id": safe_fingerprint(item.get("domain_decision_id")) or None,
        "work_difficulty": item.get("work_difficulty")
        if item.get("work_difficulty") in {"not_applicable", "simple", "hard", "unknown"}
        else "unknown",
        "difficulty_confidence": item.get("difficulty_confidence")
        if item.get("difficulty_confidence") in {"low", "medium", "high"}
        else "low",
        "difficulty_rule_codes": [
            safe_label(value, 48)
            for value in as_list(item.get("difficulty_rule_codes"))
            if value
        ][:8],
        "difficulty_classifier_version": safe_label(
            item.get("difficulty_classifier_version") or DIFFICULTY_CLASSIFIER_VERSION, 16
        ),
        "difficulty_decision_id": safe_fingerprint(item.get("difficulty_decision_id")) or None,
    })


def _safe_operation(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    fingerprint = str(item.get("fingerprint") or "")
    if not re.fullmatch(r"[0-9a-f]{8,64}", fingerprint):
        return None
    status_value = safe_label(item.get("status"), 32).lower()
    plan_digest = safe_fingerprint(item.get("plan_digest")) or None
    contract_id = safe_fingerprint(item.get("execution_contract_id")) or None
    return {
        "at": item.get("at"),
        "turn_id": safe_label(item.get("turn_id"), 120) if item.get("turn_id") else None,
        "tool": safe_label(item.get("tool"), 120),
        "fingerprint": fingerprint[:64],
        "status": status_value,
        "category": safe_label(item.get("category"), 32) if item.get("category") else "other",
        "plan_digest": plan_digest,
        "execution_contract_id": contract_id,
        "executor_agent_id": (
            safe_label(item.get("executor_agent_id"), 120)
            if item.get("executor_agent_id")
            else None
        ),
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
    contract_id = safe_fingerprint(item.get("contract_id")) or None
    role = (
        item.get("role")
        if item.get("role") in {"lane", "confirmed_executor"}
        else "lane"
    )
    fork_turns = str(item.get("fork_turns") or "")
    if fork_turns != "none" and not re.fullmatch(r"[1-9]\d*", fork_turns):
        fork_turns = ""
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
        "role": role,
        "contract_id": contract_id,
        "model": safe_label(item.get("model"), 80) if item.get("model") else None,
        "reasoning_effort": (
            safe_label(item.get("reasoning_effort"), 24)
            if item.get("reasoning_effort")
            else None
        ),
        "fork_turns": fork_turns or None,
        "attempt": min(max(safe_int(item.get("attempt")), 0), MAX_EXECUTOR_ATTEMPTS),
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


def _safe_execution_baseline(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    baseline_id = safe_fingerprint(item.get("baseline_id"))
    objective = safe_fingerprint(item.get("objective_fingerprint"))
    plan = safe_fingerprint(item.get("plan_digest"))
    contract = safe_fingerprint(item.get("execution_contract_id"))
    if not (baseline_id and objective and plan and contract):
        return {}
    acceptance = item.get("acceptance_status")
    return {
        "baseline_id": baseline_id,
        "objective_fingerprint": objective,
        "plan_digest": plan,
        "execution_contract_id": contract,
        "change_set_digest": safe_fingerprint(item.get("change_set_digest")) or None,
        "verification_digest": safe_fingerprint(item.get("verification_digest")) or None,
        "acceptance_status": (
            acceptance if acceptance in BASELINE_ACCEPTANCE_STATUSES else "unknown"
        ),
    }


def _safe_causal_review(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        item = {}
    state_value = item.get("state")
    outcome = item.get("outcome")
    return {
        "state": state_value if state_value in CAUSAL_REVIEW_STATES else "none",
        "review_id": safe_fingerprint(item.get("review_id")) or None,
        "report_fingerprint": safe_fingerprint(item.get("report_fingerprint")) or None,
        "baseline_id": safe_fingerprint(item.get("baseline_id")) or None,
        "outcome": outcome if outcome in CAUSAL_REVIEW_OUTCOMES else None,
        "evidence_digest": safe_fingerprint(item.get("evidence_digest")) or None,
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
    plan_digest = safe_fingerprint(item.get("plan_digest")) or None
    confirmed_plan_digest = safe_fingerprint(item.get("confirmed_plan_digest")) or None
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
        "work_difficulty": item.get("work_difficulty")
        if item.get("work_difficulty") in {"not_applicable", "simple", "hard", "unknown"}
        else "unknown",
        "difficulty_decision_id": safe_fingerprint(item.get("difficulty_decision_id")) or None,
        "plan_state": item.get("plan_state")
        if item.get("plan_state")
        in {"none", "analyzing", "plan_ready", "awaiting_confirmation", "confirmed", "invalidated"}
        else "none",
        "plan_generation": max(safe_int(item.get("plan_generation")), 0),
        "plan_digest": plan_digest,
        "confirmed_plan_digest": confirmed_plan_digest,
        "execution_profile_version": safe_label(
            item.get("execution_profile_version") or EXECUTION_PROFILE_VERSION, 16
        ),
        "executor_state": (
            item.get("executor_state")
            if item.get("executor_state") in EXECUTOR_STATES
            else "none"
        ),
        "execution_contract_id": safe_fingerprint(item.get("execution_contract_id")) or None,
        "executor_attempt": min(
            max(safe_int(item.get("executor_attempt")), 0), MAX_EXECUTOR_ATTEMPTS
        ),
        "executor_failure_kind": (
            item.get("executor_failure_kind")
            if item.get("executor_failure_kind") in EXECUTOR_FAILURE_KINDS
            else None
        ),
        "reference_acceptance": _safe_reference_acceptance(item.get("reference_acceptance")),
        "last_execution_baseline": _safe_execution_baseline(
            item.get("last_execution_baseline")
        ),
        "causal_review": _safe_causal_review(item.get("causal_review")),
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
    domain_source = value if value.get("task_domain") in {"daily", "work", "unknown"} else base["last_route"]
    base["task_domain"] = (
        domain_source.get("task_domain")
        if domain_source.get("task_domain") in {"daily", "work", "unknown"}
        else "unknown"
    )
    base["domain_confidence"] = (
        domain_source.get("domain_confidence")
        if domain_source.get("domain_confidence") in {"low", "medium", "high"}
        else "low"
    )
    base["domain_rule_codes"] = [
        safe_label(item, 48)
        for item in as_list(domain_source.get("domain_rule_codes"))
        if item
    ][:8]
    base["model_profile"] = (
        domain_source.get("model_profile")
        if domain_source.get("model_profile") in MODEL_PROFILES
        else "current"
    )
    base["domain_classifier_version"] = safe_label(
        domain_source.get("domain_classifier_version") or DOMAIN_CLASSIFIER_VERSION, 16
    )
    base["domain_decision_id"] = safe_fingerprint(domain_source.get("domain_decision_id")) or None
    difficulty_source = (
        value
        if value.get("work_difficulty") in {"not_applicable", "simple", "hard", "unknown"}
        else base["last_route"]
    )
    base["work_difficulty"] = (
        difficulty_source.get("work_difficulty")
        if difficulty_source.get("work_difficulty")
        in {"not_applicable", "simple", "hard", "unknown"}
        else "unknown"
    )
    base["difficulty_confidence"] = (
        difficulty_source.get("difficulty_confidence")
        if difficulty_source.get("difficulty_confidence") in {"low", "medium", "high"}
        else "low"
    )
    base["difficulty_rule_codes"] = [
        safe_label(item, 48)
        for item in as_list(difficulty_source.get("difficulty_rule_codes"))
        if item
    ][:8]
    base["difficulty_classifier_version"] = safe_label(
        difficulty_source.get("difficulty_classifier_version")
        or DIFFICULTY_CLASSIFIER_VERSION,
        16,
    )
    base["difficulty_decision_id"] = (
        safe_fingerprint(difficulty_source.get("difficulty_decision_id")) or None
    )
    if base["work_difficulty"] == "unknown":
        if base["task_domain"] == "daily":
            base["work_difficulty"] = "not_applicable"
            base["difficulty_confidence"] = "high"
            base["difficulty_rule_codes"] = ["migrated_daily"]
        elif base["task_domain"] == "work":
            legacy_route = base["last_route"]
            legacy_rules = set(base["domain_rule_codes"])
            legacy_hard = (
                legacy_route.get("label") in {"complex", "extensive"}
                or bool(
                    legacy_rules
                    & {"work_device_bug", "work_device_customization", "work_build_delivery"}
                )
            )
            base["work_difficulty"] = "hard" if legacy_hard else "simple"
            base["difficulty_confidence"] = "low"
            base["difficulty_rule_codes"] = [
                "migrated_hard_route" if legacy_hard else "migrated_simple_route"
            ]

    plan_state = value.get("plan_state")
    base["plan_state"] = (
        plan_state
        if plan_state
        in {"none", "analyzing", "plan_ready", "awaiting_confirmation", "confirmed", "invalidated"}
        else "none"
    )
    base["plan_generation"] = max(safe_int(value.get("plan_generation")), 0)
    base["plan_digest"] = safe_fingerprint(value.get("plan_digest")) or None
    base["plan_objective_fingerprint"] = (
        safe_fingerprint(value.get("plan_objective_fingerprint")) or None
    )
    base["plan_difficulty_decision_id"] = (
        safe_fingerprint(value.get("plan_difficulty_decision_id")) or None
    )
    base["confirmed_plan_digest"] = (
        safe_fingerprint(value.get("confirmed_plan_digest")) or None
    )
    base["confirmed_at"] = str(value.get("confirmed_at"))[:40] if value.get("confirmed_at") else None
    base["execution_profile_version"] = safe_label(
        value.get("execution_profile_version") or EXECUTION_PROFILE_VERSION, 16
    )
    base["executor_state"] = (
        value.get("executor_state")
        if value.get("executor_state") in EXECUTOR_STATES
        else "none"
    )
    base["execution_contract_id"] = (
        safe_fingerprint(value.get("execution_contract_id")) or None
    )
    base["executor_agent_id"] = (
        safe_label(value.get("executor_agent_id"), 120)
        if value.get("executor_agent_id")
        else None
    )
    base["executor_attempt"] = min(
        max(safe_int(value.get("executor_attempt")), 0), MAX_EXECUTOR_ATTEMPTS
    )
    base["executor_failure_kind"] = (
        value.get("executor_failure_kind")
        if value.get("executor_failure_kind") in EXECUTOR_FAILURE_KINDS
        else None
    )
    base["reference_acceptance"] = _safe_reference_acceptance(value.get("reference_acceptance"))
    base["executor_model"] = (
        safe_label(value.get("executor_model"), 80) if value.get("executor_model") else None
    )
    base["executor_reasoning_effort"] = (
        safe_label(value.get("executor_reasoning_effort"), 24)
        if value.get("executor_reasoning_effort")
        else None
    )
    fork_turns = str(value.get("executor_fork_turns") or "")
    base["executor_fork_turns"] = (
        fork_turns
        if fork_turns == "none" or re.fullmatch(r"[1-9]\d*", fork_turns)
        else None
    )
    base["last_execution_baseline"] = _safe_execution_baseline(
        value.get("last_execution_baseline")
    )
    base["causal_review"] = _safe_causal_review(value.get("causal_review"))

    base["objective"] = safe_metadata(value.get("objective"))
    if not base["objective"] and value.get("last_objective"):
        base["objective"] = text_metadata(value.get("last_objective"))
    base["last_assistant"] = safe_metadata(value.get("last_assistant"))

    valid_plan_binding = bool(
        base["plan_digest"]
        and base["plan_objective_fingerprint"]
        and base["plan_objective_fingerprint"] == base["objective"].get("fingerprint")
        and base["plan_difficulty_decision_id"]
        and base["plan_difficulty_decision_id"] == base["difficulty_decision_id"]
    )
    if base["plan_state"] in {"plan_ready", "awaiting_confirmation", "confirmed"} and not valid_plan_binding:
        base["plan_state"] = "analyzing" if base["work_difficulty"] == "hard" else "none"
        base["confirmed_plan_digest"] = None
        base["confirmed_at"] = None
    if base["plan_state"] == "confirmed" and base["confirmed_plan_digest"] != base["plan_digest"]:
        base["plan_state"] = "awaiting_confirmation"
        base["confirmed_plan_digest"] = None
        base["confirmed_at"] = None

    expected_contract = execution_contract_id(base) if base["plan_state"] == "confirmed" else None
    valid_execution_binding = bool(
        expected_contract
        and base["execution_contract_id"] == expected_contract
        and base["execution_profile_version"] == EXECUTION_PROFILE_VERSION
    )
    if base["plan_state"] == "confirmed" and not valid_execution_binding:
        # Schema 9 confirmations never proved an executor handoff. Recreate the contract, but never
        # infer that execution had started or succeeded.
        base["execution_profile_version"] = EXECUTION_PROFILE_VERSION
        base["execution_contract_id"] = expected_contract
        base["executor_state"] = "spawn_required"
        base["executor_agent_id"] = None
        base["executor_attempt"] = 0
        base["executor_failure_kind"] = None
        base["executor_model"] = None
        base["executor_reasoning_effort"] = None
        base["executor_fork_turns"] = None
        base["model_profile"] = "work_executor_low_latest"
    elif base["plan_state"] == "confirmed":
        base["model_profile"] = (
            "work_assessment"
            if base["executor_state"] in {"recovery_required", "exhausted"}
            else "work_executor_low_latest"
        )
    elif base["plan_state"] != "confirmed":
        base["execution_contract_id"] = None
        base["executor_state"] = "none"
        base["executor_agent_id"] = None
        base["executor_attempt"] = 0
        base["executor_failure_kind"] = None
        base["executor_model"] = None
        base["executor_reasoning_effort"] = None
        base["executor_fork_turns"] = None
    if base["causal_review"].get("state") in {"triage_required", "triaging"}:
        base["model_profile"] = "work_assessment"

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
    if (
        not base["last_execution_baseline"]
        and safe_int(value.get("schema_version")) <= 10
        and base.get("executor_state") == "succeeded"
    ):
        # Old schemas did not seal a baseline. Reconstruct only contract/change facts and
        # never infer user acceptance or a causal conclusion from executor completion.
        migrated_baseline = build_execution_baseline(base)
        if migrated_baseline:
            migrated_baseline["acceptance_status"] = "incomplete"
            base["last_execution_baseline"] = migrated_baseline
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


def cleanup_old_plugin_versions(
    plugin_root: Path | None = None, *, skill_paths_verified: bool = False
) -> int:
    """Remove older caches only after every retained task Skill path is verified."""
    if not skill_paths_verified:
        return 0
    configured_root = plugin_root or os.environ.get("PLUGIN_ROOT")
    root = Path(configured_root) if configured_root else Path(__file__).resolve().parents[1]
    semver_pattern = r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    try:
        root_info = root.lstat()
        if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
            return 0
        absolute_root = Path(os.path.abspath(root))
        resolved_root = root.resolve(strict=True)
        if os.path.normcase(str(absolute_root)) != os.path.normcase(str(resolved_root)):
            return 0
        root = resolved_root
        if (
            root.parent.name != "workflow-manager"
            or root.parent.parent.name != "workflow-manager"
        ):
            return 0
        manifest_dir = root / ".codex-plugin"
        manifest_dir_info = manifest_dir.lstat()
        if not stat.S_ISDIR(manifest_dir_info.st_mode) or stat.S_ISLNK(manifest_dir_info.st_mode):
            return 0
        manifest_path = manifest_dir / "plugin.json"
        manifest_info = manifest_path.lstat()
        if (
            not stat.S_ISREG(manifest_info.st_mode)
            or stat.S_ISLNK(manifest_info.st_mode)
            or manifest_info.st_size > 64 * 1024
            or manifest_path.resolve(strict=True) != manifest_path
        ):
            return 0
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            return 0
        current_label = str(manifest.get("version") or "")
        match = re.fullmatch(semver_pattern, current_label)
        if (
            not match
            or manifest.get("name") != "workflow-manager"
            or current_label != WRITER_VERSION
            or root.name != current_label
        ):
            return 0
        current = tuple(int(part) for part in match.groups())
        older: list[Path] = []
        for candidate in root.parent.iterdir():
            version_match = re.fullmatch(semver_pattern, candidate.name)
            if not version_match:
                continue
            info = candidate.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                continue
            version = tuple(int(part) for part in version_match.groups())
            if version > current:
                return 0
            if version < current:
                older.append(candidate)
        removed = 0
        for candidate in older:
            try:
                shutil.rmtree(candidate)
                removed += 1
            except OSError:
                continue
        return removed
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0


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

DAILY_EXACT_PATTERNS = (
    ("daily_weather", r"(?:天气|气温|下雨|空气质量|weather|forecast)"),
    ("daily_report", r"(?:生成|整理|写|帮我写|汇总).{0,10}(?:日报|周报|月报|daily report|weekly report)"),
    ("daily_cleanup", r"(?:清理|删除|整理).{0,16}(?:电脑|磁盘|缓存|垃圾文件|临时文件|重复文件|computer|disk|cache|junk|temporary files?)"),
    ("daily_chat", r"^(?:你好|您好|嗨|hello|hi|聊聊|陪我聊天|谢谢|早上好|晚上好)[!！。,.，\s]*$"),
)
WORK_STRONG_PATTERNS = (
    ("work_device_customization", r"(?:设备|产品|系统|固件|framework|android).{0,24}(?:定制|需求|适配|开发|修改|实现)"),
    ("work_device_bug", r"(?:设备|产品|系统|固件|framework|android).{0,24}(?:bug|问题|异常|故障|崩溃|重启|修复|排查|诊断)"),
    (
        "work_app_code",
        r"(?:写|编写|开发|实现|修改|修复|重构|调试|\b(?:review|write|create|implement|develop|debug|fix|refactor)\b)"
        r".{0,24}(?:应用|代码|源码|模块|脚本|函数|方法|接口|服务|插件|\b(?:app|java|kotlin|python|javascript|typescript|skill|hook|function|class|service|plugin|script|code)\b)",
    ),
    (
        "work_build_delivery",
        r"(?:编译|构建|合包|打包|烧录|刷机|部署|实机验证|\b(?:compile|deploy)\b|"
        r"\b(?:build|package)\b.{0,24}\b(?:project|repository|repo|app|apk|module|code|firmware|image)\b|"
        r"\bflash\b.{0,24}\b(?:device|firmware|image|rom)\b)",
    ),
    ("work_engineering_artifact", r"(?:仓库|代码库|项目|工程|模块|source tree|repository).{0,24}(?:修改|修复|实现|诊断|排查|测试|验证|发布|迁移)"),
    ("work_engineering_diagnosis", r"(?:排查|诊断|修复|调试|分析|审查|investigate|diagnose|debug|fix|analy[sz]e|review).{0,24}(?:日志|测试|bug|崩溃|重启|异常|故障|失败|log|test|crash|failure|error)"),
    ("work_engineering_operation", r"(?:测试|验证|优化|迁移|安装|test|verify|optimize|migrate|install).{0,24}(?:ci|api|数据库|服务器|服务|插件|skill|workflow|模块|代码|系统|设备|database|server|service|plugin|module|code|system|device)"),
)
WORK_CONTEXT_PATTERNS = (
    ("work_source_symbol", r"(?:\.java|\.kt|\.py|\.js|\.ts|\.cpp|\.c|\.h|方法|函数|类|源码文件)"),
    ("work_toolchain", r"(?:adb|gradle|maven|ninja|make|编译器|构建服务器|设备日志|logcat)"),
    ("work_product_delivery", r"(?:客户需求|交付|验收|版本发布|release|production|线上故障)"),
    (
        "work_repository_artifact",
        r"(?:(?:readme|changelog|仓库文档|代码文档).{0,24}(?:错字|链接|修改|修正|更新|typo|link|fix|update)|"
        r"(?:数据库|database).{0,30}(?:迁移|回滚|零停机|migration|rollback|zero[- ]downtime)|"
        r"(?:migration|迁移).{0,24}(?:数据库|database|回滚|rollback))",
    ),
)


def _english_hits(text: str, terms: tuple[str, ...]) -> int:
    return sum(1 for term in terms if re.search(rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])", text))


def classify_task_domain(prompt: str) -> dict[str, Any]:
    """Classify the user objective without storing its raw text.

    Domain controls only the model policy suggestion. It never relaxes safety,
    destructive-action confirmation, evidence, or verification requirements.
    """
    normalized = re.sub(r"\s+", " ", prompt.strip())
    lower = normalized.lower()
    daily_codes = [code for code, pattern in DAILY_EXACT_PATTERNS if re.search(pattern, lower, re.I)]
    work_codes = [code for code, pattern in WORK_STRONG_PATTERNS if re.search(pattern, lower, re.I)]
    context_codes = [code for code, pattern in WORK_CONTEXT_PATTERNS if re.search(pattern, lower, re.I)]
    report_with_separate_work = bool(
        re.search(
            r"(?:然后|同时|并且|此外|还要|再|then|also|and then).{0,40}"
            r"(?:修改|修复|实现|开发|编译|构建|部署|测试|验证|代码|app|设备|"
            r"modify|fix|implement|develop|build|compile|deploy|test|verify)",
            lower,
            re.I,
        )
    )

    # An explicit engineering deliverable outranks an incidental daily phrase in a mixed request.
    if "daily_report" in daily_codes and not report_with_separate_work and not work_codes:
        domain = "daily"
        confidence = "high"
        rule_codes = ["daily_report"]
    elif work_codes:
        domain = "work"
        confidence = "high"
        rule_codes = [*work_codes, *(context_codes[:2])]
    elif context_codes:
        domain = "work"
        confidence = "medium"
        rule_codes = context_codes
    elif daily_codes:
        domain = "daily"
        confidence = "high"
        rule_codes = daily_codes
    else:
        # General conversation, factual questions, and personal assistance stay on the
        # current model unless concrete engineering work is present.
        domain = "daily"
        confidence = "low"
        rule_codes = ["daily_general_default"]
    decision_seed = "\0".join(
        (DOMAIN_CLASSIFIER_VERSION, stable_hash(normalized, 32), domain, *sorted(set(rule_codes)))
    )
    return {
        "task_domain": domain,
        "domain_confidence": confidence,
        "domain_rule_codes": list(dict.fromkeys(rule_codes))[:8],
        "model_profile": "current" if domain == "daily" else "work_assessment",
        "domain_classifier_version": DOMAIN_CLASSIFIER_VERSION,
        "domain_decision_id": stable_hash(decision_seed, 24),
    }


HARD_WORK_PATTERNS = (
    ("hard_unknown_root_cause", r"(?:根因未知|未知根因|原因不明|反复|间歇|偶现|复现|root cause|intermittent|flaky|keeps|repeated)"),
    ("hard_cross_module", r"(?:跨模块|多个模块|多模块|跨组件|多个组件|framework.{0,28}systemui|settings.{0,28}framework|cross[- ]module|multiple modules?|several modules?)"),
    ("hard_architecture", r"(?:从零开发|完整开发|架构|离线同步|后台同步|认证系统|zero[- ]downtime|rollback|migration|迁移|生产发布|production)"),
    ("hard_external_chain", r"(?:编译|构建|compile|build).{0,60}(?:部署|安装|烧录|刷机|实机|deploy|install|flash|device)"),
    ("hard_shared_resource", r"(?:唯一|同一|共享|only|single|same|shared).{0,20}(?:设备|构建服务器|账号|资源|device|build server|account|resource)"),
)
SIMPLE_WORK_PATTERNS = (
    ("simple_explicit_small", r"(?:一个错字|单个错字|一处文案|小改动|单文件|one typo|single file|small change|tiny change)"),
    ("simple_bounded_contract", r"(?:给定输入输出|已有单测|现有单测|明确验收|provided input|existing tests?|clear acceptance)"),
    ("simple_explain_only", r"(?:查看|阅读|解释|说明|read|inspect|explain).{0,32}(?:当前实现|代码|源码|方法|函数|implementation|code|method|function)"),
)


def classify_work_difficulty(
    prompt: str, domain: dict[str, Any], route: dict[str, Any]
) -> dict[str, Any]:
    """Classify work difficulty independently from execution shape and agent count."""
    normalized = re.sub(r"\s+", " ", prompt.strip())
    lower = normalized.lower()
    if domain.get("task_domain") != "work":
        difficulty = "not_applicable"
        confidence = "high"
        rule_codes = ["daily_not_applicable"]
    else:
        hard_codes = [
            code for code, pattern in HARD_WORK_PATTERNS if re.search(pattern, lower, re.I)
        ]
        question_only = bool(
            re.search(r"^(?:what|why|how|when|where|who|is|are|can|could|would)\b", lower)
            or re.search(r"^(?:什么是|为什么|如何理解|怎么理解|请解释|解释一下)", normalized)
        )
        if question_only and not re.search(
            r"(?:修改|修复|实现|开发|编写|执行|部署|迁移|回滚|fix|implement|develop|write|execute|deploy|migrate|rollback)",
            lower,
            re.I,
        ):
            hard_codes = []
        domain_codes = set(as_list(domain.get("domain_rule_codes")))
        phases = set(as_list(route.get("phase_hints")))
        if domain_codes & {"work_device_bug", "work_device_customization"}:
            hard_codes.append("hard_device_change")
        if len(phases) >= 3:
            hard_codes.append("hard_three_phase_chain")
        if route.get("dependency_signal") in {"shared_resource", "ordered_shared"}:
            hard_codes.append("hard_shared_or_ordered")
        if question_only and not hard_codes:
            difficulty = "simple"
            confidence = "high"
            rule_codes = ["simple_explanation_request"]
        elif hard_codes:
            difficulty = "hard"
            confidence = "high"
            rule_codes = list(dict.fromkeys(hard_codes))[:8]
        else:
            simple_codes = [
                code for code, pattern in SIMPLE_WORK_PATTERNS if re.search(pattern, lower, re.I)
            ]
            route_label = str(route.get("label") or "direct")
            if route_label in {"direct", "focused"} and len(phases) <= 2:
                simple_codes.append("simple_bounded_route")
            if simple_codes:
                difficulty = "simple"
                confidence = "high" if route_label in {"direct", "focused"} else "medium"
                rule_codes = list(dict.fromkeys(simple_codes))[:8]
            else:
                difficulty = "hard"
                confidence = "medium"
                rule_codes = ["hard_ambiguous_work"]
    decision_seed = "\0".join(
        (
            DIFFICULTY_CLASSIFIER_VERSION,
            str(domain.get("domain_decision_id") or "unknown"),
            stable_hash(normalized, 32),
            difficulty,
            *sorted(set(rule_codes)),
        )
    )
    return {
        "work_difficulty": difficulty,
        "difficulty_confidence": confidence,
        "difficulty_rule_codes": rule_codes,
        "difficulty_classifier_version": DIFFICULTY_CLASSIFIER_VERSION,
        "difficulty_decision_id": stable_hash(decision_seed, 24),
    }


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
    result["task_domain"] = (
        result.get("task_domain")
        if result.get("task_domain") in {"daily", "work", "unknown"}
        else "unknown"
    )
    result["domain_confidence"] = (
        result.get("domain_confidence")
        if result.get("domain_confidence") in {"low", "medium", "high"}
        else "low"
    )
    result["domain_rule_codes"] = [
        safe_label(item, 48)
        for item in as_list(result.get("domain_rule_codes"))
        if item
    ][:8]
    result["model_profile"] = (
        result.get("model_profile")
        if result.get("model_profile") in MODEL_PROFILES
        else "current"
    )
    result["domain_classifier_version"] = safe_label(
        result.get("domain_classifier_version") or DOMAIN_CLASSIFIER_VERSION, 16
    )
    result["domain_decision_id"] = safe_fingerprint(result.get("domain_decision_id")) or None
    result["work_difficulty"] = (
        result.get("work_difficulty")
        if result.get("work_difficulty") in {"not_applicable", "simple", "hard", "unknown"}
        else "unknown"
    )
    result["difficulty_confidence"] = (
        result.get("difficulty_confidence")
        if result.get("difficulty_confidence") in {"low", "medium", "high"}
        else "low"
    )
    result["difficulty_rule_codes"] = [
        safe_label(item, 48)
        for item in as_list(result.get("difficulty_rule_codes"))
        if item
    ][:8]
    result["difficulty_classifier_version"] = safe_label(
        result.get("difficulty_classifier_version") or DIFFICULTY_CLASSIFIER_VERSION, 16
    )
    result["difficulty_decision_id"] = safe_fingerprint(result.get("difficulty_decision_id")) or None
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
    domain = classify_task_domain(normalized)
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
    route = {
        **domain,
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
    }
    route.update(classify_work_difficulty(normalized, domain, route))
    return decorate_route(route)
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


def subagent_request_candidates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Accept raw and host-normalized collaboration spawn shapes without inventing bindings."""
    candidates: list[dict[str, Any]] = []
    pending: list[Any] = [payload.get("tool_input"), payload]
    seen: set[int] = set()
    while pending:
        value = pending.pop(0)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                continue
        if not isinstance(value, dict) or id(value) in seen:
            continue
        seen.add(id(value))
        candidates.append(value)
        pending.extend(value.get(key) for key in ("args", "arguments", "input", "tool_input"))
    return candidates


def subagent_request_fields(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    candidates = subagent_request_candidates(payload)

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


def subagent_request_options(payload: dict[str, Any]) -> dict[str, str | None]:
    candidates = subagent_request_candidates(payload)
    result: dict[str, str | None] = {
        "model": None,
        "reasoning_effort": None,
        "fork_turns": None,
    }
    for candidate in candidates:
        for key in tuple(result):
            if result[key] is None and candidate.get(key) not in (None, ""):
                result[key] = str(candidate.get(key)).strip()
    return result


def confirmed_executor_request(payload: dict[str, Any], state: dict[str, Any]) -> tuple[bool, str | None]:
    contract_id = str(state.get("execution_contract_id") or "")
    request = subagent_request_text(payload)
    if not contract_id or not request:
        return False, "missing execution contract"
    options = subagent_request_options(payload)
    model = str(options.get("model") or "").strip()
    effort = str(options.get("reasoning_effort") or "").strip().lower()
    fork_turns = str(options.get("fork_turns") or "").strip().lower()
    contract_marker = bool(
        re.search(
            rf"(?:execution_contract_id|execution-contract-id|执行合同)\s*[:=：]\s*{re.escape(contract_id)}\b",
            request,
            re.I,
        )
    )
    plan_marker = str(state.get("plan_digest") or "") in request
    generation_marker = bool(
        re.search(
            rf"(?:plan_generation|plan-generation|计划代次)\s*[:=：]\s*{safe_int(state.get('plan_generation'))}\b",
            request,
            re.I,
        )
    )
    exclusive_scope = bool(
        re.search(r"\b(?:exclusive (?:owner|executor)|only executor)\b", request, re.I)
        or any(term in request for term in ("唯一执行者", "独占执行", "独占修改"))
    )
    acceptance = bool(
        re.search(r"\b(?:acceptance|verification|verify|test)\b", request, re.I)
        or any(term in request for term in ("验收", "验证", "测试"))
    )
    if state.get("executor_state") == "recovery_required":
        failure_kind = str(state.get("executor_failure_kind") or "")
        recovery_marker = bool(
            failure_kind
            and re.search(
                rf"(?:recovery_from|recovery-from|恢复自)\s*[:=：]\s*{re.escape(failure_kind)}\b",
                request,
                re.I,
            )
        )
        correction_marker = bool(
            re.search(
                r"(?:material_correction|material-correction|实质修正)\s*[:=：]\s*.{8,}",
                request,
                re.I,
            )
        )
        if not recovery_marker or not correction_marker:
            return False, "recovery request lacks the typed failure and a material correction"
    if not model:
        return False, "model_unavailable"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{1,79}", model):
        return False, "invalid executor model identifier"
    current_model = str(state.get("model") or "").strip()
    if current_model and safe_label(model, 80) == safe_label(current_model, 80):
        return False, "executor model is not a lower-tier override"
    if effort != "medium":
        return False, "reasoning_effort must be medium"
    if fork_turns != "none" and not re.fullmatch(r"[1-9]\d*", fork_turns):
        return False, "fork_turns must be none or a positive integer when overriding model"
    if not contract_marker or not plan_marker or not generation_marker:
        return False, "executor request is not bound to the exact confirmed plan"
    if not exclusive_scope or not acceptance:
        return False, "executor request must declare exclusive execution ownership and acceptance"
    return True, None


def subagent_request_text(payload: dict[str, Any]) -> str:
    for candidate in subagent_request_candidates(payload):
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


PLAN_MUTATING_GIT_COMMANDS = {
    "add",
    "am",
    "apply",
    "cherry-pick",
    "clean",
    "commit",
    "merge",
    "mv",
    "pull",
    "push",
    "rebase",
    "reset",
    "restore",
    "revert",
    "rm",
    "switch",
    "tag",
}


def command_mutates_device(command: str) -> bool:
    for candidate in command_views(command):
        visible = shell_syntax_view(candidate)
        if re.search(
            r"(?i)(?:^|[;&|]\s*)(?:adb(?:\.exe)?(?:\s+-s\s+\S+)?\s+)"
            r"(?:install|uninstall|push|sync|reboot|root|remount)\b",
            visible,
        ):
            return True
        if re.search(
            r"(?i)(?:^|[;&|]\s*)adb(?:\.exe)?(?:\s+-s\s+\S+)?\s+shell\s+"
            r"(?:(?:pm\s+(?:install|uninstall|clear|grant|revoke))|"
            r"(?:settings\s+(?:put|delete))|(?:setprop|svc|reboot)\b)",
            visible,
        ):
            return True
        if re.search(
            r"(?i)(?:^|[;&|]\s*)fastboot(?:\.exe)?(?:\s+-s\s+\S+)?\s+"
            r"(?:flash|erase|format|update|set_active|reboot)\b",
            visible,
        ):
            return True
    return False


def command_mutates_files(command: str) -> bool:
    for candidate in command_views(command):
        visible = shell_syntax_view(candidate)
        if re.search(r"(?<!\d)(?:>>?|&>)\s*(?!&)[^\s;&|]+", visible):
            return True
        if re.search(
            rf"(?i){_COMMAND_BOUNDARY_RE}{_COMMAND_PREFIX_RE}(?:"
            r"rm|mv|cp|install|truncate|tee|patch)\b",
            visible,
        ):
            return True
        if re.search(r"(?i)(?:^|[;&|]\s*)sed\s+(?:-[A-Za-z]*i[A-Za-z]*|--in-place)\b", visible):
            return True
    return False


def subagent_request_is_read_only(payload: dict[str, Any]) -> bool:
    request = subagent_request_text(payload)
    if not request:
        return False
    lower = request.lower()
    explicit_read_only = bool(
        re.search(r"\b(?:read[- ]only|no writes?|without modifying|do not modify)\b", lower)
        or any(term in request for term in ("只读", "不修改", "不要修改", "不写入"))
    )
    mutation = bool(
        re.search(
            r"\b(?:edit|write|modify|implement|fix|build|compile|deploy|install|flash|commit|push)\b",
            lower,
        )
        or any(
            term in request
            for term in ("修改", "写入", "实现", "修复", "编译", "构建", "部署", "安装", "烧录", "刷机", "提交", "推送")
        )
    )
    analysis_only = bool(
        re.search(r"\b(?:inspect|review|analyze|analyse|research|search|read|explain)\b", lower)
        or any(term in request for term in ("检查", "审查", "分析", "调研", "搜索", "阅读", "解释"))
    )
    return (explicit_read_only or analysis_only) and not mutation


def plan_confirmation_guard(payload: dict[str, Any], state: dict[str, Any]) -> str | None:
    if state.get("work_difficulty") != "hard":
        return None
    if state.get("plan_state") == "confirmed" and state.get("confirmed_plan_digest") == state.get("plan_digest"):
        return None
    tool_key = normalized_key(payload.get("tool_name"))
    if "updateplan" in tool_key or "requestuserinput" in tool_key:
        return None
    if is_subagent_spawn_tool(payload):
        return None if subagent_request_is_read_only(payload) else "subagent execution"
    if tool_key in {
        "applypatch",
        "edit",
        "write",
        "create",
        "replace",
        "createfile",
        "updatefile",
        "deletefile",
    } or any(marker in tool_key for marker in ("applypatch", "createfile", "updatefile", "deletefile")):
        return "file mutation"
    command = extract_command(payload)
    if not command:
        return None
    subcommand = git_subcommand(command)
    if subcommand in PLAN_MUTATING_GIT_COMMANDS:
        return "Git mutation"
    if any(BUILD_COMMAND_RE.search(shell_syntax_view(candidate)) for candidate in command_views(command)):
        return "build or package"
    if command_mutates_device(command):
        return "device mutation"
    if command_mutates_files(command):
        return "file mutation"
    return None


def executor_gate_guard(payload: dict[str, Any], state: dict[str, Any]) -> str | None:
    """Keep confirmed hard-work mutation with the one explicitly configured executor."""
    if state.get("work_difficulty") != "hard" or state.get("plan_state") != "confirmed":
        return None
    if state.get("confirmed_plan_digest") != state.get("plan_digest"):
        return "invalid confirmed-plan binding"
    tool_key = normalized_key(payload.get("tool_name"))
    if "updateplan" in tool_key or "requestuserinput" in tool_key:
        return None
    if is_subagent_spawn_tool(payload):
        if subagent_request_is_read_only(payload):
            return None
        valid, reason = confirmed_executor_request(payload, state)
        if valid and state.get("executor_state") in {"spawn_required", "recovery_required"}:
            return None
        if valid and state.get("executor_state") not in {"spawn_required", "recovery_required"}:
            return "duplicate confirmed executor"
        return reason or "non-contract execution subagent"
    mutating = plan_confirmation_guard(
        payload,
        {
            **state,
            "plan_state": "awaiting_confirmation",
            "confirmed_plan_digest": None,
        },
    )
    if not mutating:
        return None
    # Hooks are an orchestration guard, not an identity sandbox. Some hosts expose the active
    # subagent id on tool events; honor it when present, otherwise fail closed for the parent.
    caller_id = next(
        (
            safe_label(payload.get(key), 120)
            for key in ("agent_id", "subagent_id")
            if payload.get(key)
        ),
        None,
    )
    if (
        caller_id
        and state.get("executor_state") == "running"
        and caller_id == state.get("executor_agent_id")
        and state.get("execution_contract_id") == execution_contract_id(state)
    ):
        return None
    return f"parent or unbound {mutating}"


def causal_review_guard(payload: dict[str, Any], state: dict[str, Any]) -> str | None:
    review = _safe_causal_review(state.get("causal_review"))
    if review.get("state") not in {"triage_required", "triaging"}:
        return None
    tool_key = normalized_key(payload.get("tool_name"))
    if "updateplan" in tool_key or "requestuserinput" in tool_key:
        return None
    if is_subagent_spawn_tool(payload):
        return None if subagent_request_is_read_only(payload) else "causal review execution subagent"
    # Reuse the mature hard-plan mutation classifier, independent of the old plan's confirmed state.
    mutating = plan_confirmation_guard(
        payload,
        {
            **state,
            "work_difficulty": "hard",
            "plan_state": "awaiting_confirmation",
            "confirmed_plan_digest": None,
        },
    )
    return f"causal review {mutating}" if mutating else None


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
            ensure_ascii=True,
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
            ensure_ascii=True,
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
            ensure_ascii=True,
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
    stable_skill = sync_stable_skill()
    try:
        cleanup_old_plugin_versions()
    except Exception:
        pass
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
    if stable_skill.get("status") not in {"installed", "updated", "current"}:
        base += (
            " Stable Workflow Manager Skill activation is not verified "
            f"(sync={safe_label(stable_skill.get('status'), 48)}); do not claim the unversioned path is active."
        )
    if source in {"compact", "resume"}:
        successful = [op for op in state.get("operations", []) if op.get("status") in SUCCESS_STATUSES][-6:]
        agent_counts = agent_activity_counts(state)
        digest = {
            "schema": SCHEMA_VERSION,
            "objective_fingerprint": state.get("objective", {}).get("fingerprint"),
            "last_route": state.get("last_route", {}).get("label"),
            "task_domain": state.get("task_domain", "unknown"),
            "domain_decision_id": state.get("domain_decision_id"),
            "work_difficulty": state.get("work_difficulty", "unknown"),
            "difficulty_decision_id": state.get("difficulty_decision_id"),
            "model_profile": state.get("model_profile", "current"),
            "plan_state": state.get("plan_state", "none"),
            "plan_generation": safe_int(state.get("plan_generation")),
            "plan_digest": state.get("plan_digest"),
            "confirmed_plan_digest": state.get("confirmed_plan_digest"),
            "execution_profile_version": state.get("execution_profile_version"),
            "executor_state": state.get("executor_state", "none"),
            "execution_contract_id": state.get("execution_contract_id"),
            "executor_agent_id": state.get("executor_agent_id"),
            "executor_attempt": safe_int(state.get("executor_attempt")),
            "executor_failure_kind": state.get("executor_failure_kind"),
            "executor_model": state.get("executor_model"),
            "executor_reasoning_effort": state.get("executor_reasoning_effort"),
            "executor_fork_turns": state.get("executor_fork_turns"),
            "last_execution_baseline": _safe_execution_baseline(
                state.get("last_execution_baseline")
            ),
            "causal_review": _safe_causal_review(state.get("causal_review")),
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
        review = _safe_causal_review(state.get("causal_review"))
        if review.get("state") in {"triage_required", "triaging"}:
            base += (
                " Resume the active causal review read-only from the native summary and recorded baseline; "
                "do not reuse the old executor or mutate speculatively. Re-check only stale/missing evidence, "
                f"then bind the structured conclusion to baseline_id={review.get('baseline_id')} and "
                f"review_id={review.get('review_id')}."
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

REGRESSION_REPORT_MARKERS = (
    "验收",
    "仍然",
    "还是",
    "没修好",
    "未修好",
    "又出现",
    "新出现",
    "新增",
    "修复后",
    "改动后",
    "刚才改动",
    "回归",
    "导致",
    "是否相关",
    "是不是",
    "regression",
    "still fails",
    "not fixed",
    "after the fix",
    "introduced",
)
SUCCESS_FEEDBACK_MARKERS = (
    "验收通过",
    "验证通过",
    "已经解决",
    "问题已解决",
    "没有其他问题",
    "没问题了",
    "修好了",
    "acceptance passed",
    "verified fixed",
    "issue resolved",
)


def is_control_followup(prompt: str) -> bool:
    normalized = re.sub(r"[\s?!？！。,.，]+", " ", prompt.strip().lower()).strip()
    return normalized in FOLLOWUP_CONTROLS


PLAN_CONFIRM_PATTERNS = (
    r"确认执行",
    r"确认按(?:这个|上述|该|此|新)计划执行",
    r"同意按(?:这个|上述|该|此|新)计划执行",
    r"按(?:这个|上述|该|此|新)计划执行",
    r"开始执行(?:这个|上述|该|此|新)计划",
    r"confirm and execute (?:this|the) plan",
    r"execute (?:this|the) plan",
)
PLAN_CHANGE_MARKERS = (
    "但是",
    "但",
    "不过",
    "另外",
    "增加",
    "新增",
    "删除",
    "去掉",
    "改为",
    "改成",
    "先不要",
    "but",
    "except",
    "add",
    "remove",
    "change",
)
PLAN_REPLAN_PATTERNS = (
    r"重新规划",
    r"重做计划",
    r"修改计划",
    r"replan",
    r"revise (?:this|the) plan",
)


def pure_plan_confirmation(prompt: str) -> bool:
    normalized = re.sub(r"[?!？！。,.，]+", "", prompt.strip().lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized or any(marker in normalized for marker in PLAN_CHANGE_MARKERS):
        return False
    return any(re.fullmatch(pattern, normalized, re.I) for pattern in PLAN_CONFIRM_PATTERNS)


def plan_replan_request(prompt: str) -> bool:
    normalized = re.sub(r"\s+", " ", prompt.strip().lower())
    return any(re.fullmatch(pattern, normalized, re.I) for pattern in PLAN_REPLAN_PATTERNS)


def prompt_changes_pending_plan(prompt: str) -> bool:
    normalized = re.sub(r"\s+", " ", prompt.strip().lower())
    return any(marker in normalized for marker in PLAN_CHANGE_MARKERS)


def explicit_new_objective(prompt: str) -> bool:
    normalized = re.sub(r"\s+", " ", prompt.strip().lower())
    return bool(
        re.match(
            r"^(?:another task|new task|separately|换个问题|另一个任务|新任务|另外一个|顺便帮我|再帮我)",
            normalized,
        )
        or re.match(r"^现在帮我.{0,24}(?:写一个|创建|实现|解决|处理)(?:新的?|另一个)", normalized)
    )


def successful_acceptance_feedback(prompt: str) -> bool:
    normalized = re.sub(r"\s+", " ", prompt.strip().lower())
    regression_signal = any(marker in normalized for marker in REGRESSION_REPORT_MARKERS)
    contrast_signal = bool(
        re.search(r"(?:但是|但|不过|然而|却|同时|另外|可是|but|however|yet)", normalized)
    )
    fidelity_negative = any(marker in normalized for marker in ("不一致", "不对", "不像", "方向错误", "方向不对"))
    return any(marker in normalized for marker in SUCCESS_FEEDBACK_MARKERS) and not fidelity_negative and not (
        regression_signal and contrast_signal
    )


def regression_feedback(
    prompt: str, previous: dict[str, Any], *, new_objective: bool = False
) -> bool:
    baseline = _safe_execution_baseline(previous.get("last_execution_baseline"))
    review = _safe_causal_review(previous.get("causal_review"))
    normalized = re.sub(r"\s+", " ", prompt.strip().lower())
    return bool(
        baseline.get("change_set_digest")
        and previous.get("plan_state") == "confirmed"
        and previous.get("executor_state") == "succeeded"
        and baseline.get("execution_contract_id")
        == safe_fingerprint(previous.get("execution_contract_id"))
        and review.get("state") not in {"triage_required", "triaging"}
        and not new_objective
        and not is_control_followup(prompt)
        and not successful_acceptance_feedback(prompt)
        and any(marker in normalized for marker in REGRESSION_REPORT_MARKERS)
    )


def unmet_acceptance_without_recorded_change(
    prompt: str, previous: dict[str, Any], *, new_objective: bool = False
) -> bool:
    """Replan a failed outcome without inventing a change-caused regression."""
    baseline = _safe_execution_baseline(previous.get("last_execution_baseline"))
    review = _safe_causal_review(previous.get("causal_review"))
    normalized = re.sub(r"\s+", " ", prompt.strip().lower())
    return bool(
        baseline
        and not baseline.get("change_set_digest")
        and previous.get("plan_state") == "confirmed"
        and previous.get("executor_state") == "succeeded"
        and baseline.get("execution_contract_id")
        == safe_fingerprint(previous.get("execution_contract_id"))
        and review.get("state") == "none"
        and not new_objective
        and not is_control_followup(prompt)
        and not successful_acceptance_feedback(prompt)
        and any(marker in normalized for marker in REGRESSION_REPORT_MARKERS)
    )


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
    for key in (
        "task_domain",
        "domain_confidence",
        "domain_rule_codes",
        "model_profile",
        "domain_classifier_version",
        "domain_decision_id",
        "work_difficulty",
        "difficulty_confidence",
        "difficulty_rule_codes",
        "difficulty_classifier_version",
        "difficulty_decision_id",
    ):
        result[key] = prior.get(key)
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
    domain = str(classification.get("task_domain") or "unknown")
    difficulty = str(classification.get("work_difficulty") or "unknown")
    profile = str(classification.get("model_profile") or "current")
    profile_note = (
        "requested executor profile; proof requires accepted explicit child override"
        if profile == "work_executor_low_latest"
        else "advisory; no switch"
    )
    return (
        f"Domain: {domain}/{difficulty} | profile={profile} ({profile_note}). "
        f"Route: {label}/{shape} | pressure={pressure_summary} | budget={classification.get('future_token_range')}. "
        f"Order: {order}. {agents}{gate} Control: bounded. "
        "Update phase|done|next|blocker at kickoff/change/~60s wait. "
        "Preflight path/input/acceptance; diagnose once; retry after correction. "
        "Keep risk-based verification; reuse only unchanged evidence."
    )


def user_prompt_submit(payload: dict[str, Any]) -> None:
    prompt = str(payload.get("prompt") or "")
    previous = snapshot_state(payload)
    requested_reference = reference_requested(prompt)
    reference_changed = bool(_safe_reference_acceptance(previous.get("reference_acceptance"))["enabled"] and not requested_reference and reference_contract_changed(prompt))
    pending_plan = previous.get("plan_state") in {"plan_ready", "awaiting_confirmation"}
    active_plan = pending_plan or previous.get("plan_state") == "confirmed"
    explicit_new = explicit_new_objective(prompt)
    new_objective = bool(
        explicit_new
        and (
            active_plan
            or previous.get("last_execution_baseline")
            or previous.get("causal_review", {}).get("state") != "none"
        )
    )
    causal_active = (
        _safe_causal_review(previous.get("causal_review")).get("state")
        in {"triage_required", "triaging"}
    )
    acceptance_success = successful_acceptance_feedback(prompt)
    reference_rejection = bool(
        _safe_reference_acceptance(previous.get("reference_acceptance"))["enabled"]
        and not acceptance_success
        and any(marker in prompt.lower() for marker in REGRESSION_REPORT_MARKERS)
    )
    causal_report = regression_feedback(prompt, previous, new_objective=new_objective)
    acceptance_miss = unmet_acceptance_without_recorded_change(
        prompt, previous, new_objective=new_objective
    )
    reference_failure = acceptance_miss or (reference_rejection and not causal_report)
    already_confirmed = previous.get("plan_state") == "confirmed"
    confirmed_plan = pending_plan and pure_plan_confirmation(prompt)
    replan = active_plan and not causal_active and plan_replan_request(prompt)
    plan_changed = (
        active_plan
        and not causal_active
        and not causal_report
        and not acceptance_miss
        and not new_objective
        and prompt_changes_pending_plan(prompt)
        and not confirmed_plan
    )
    continuation = not new_objective and (
        is_control_followup(prompt) or is_progress_followup(prompt) or active_plan or reference_changed
    )
    classification = classify_prompt(prompt)
    if requested_reference:
        classification.update(
            {
                "task_domain": "work",
                "model_profile": "work_assessment",
                "work_difficulty": "hard",
                "difficulty_confidence": "high",
                "difficulty_rule_codes": ["explicit_reference_acceptance"],
                "difficulty_classifier_version": DIFFICULTY_CLASSIFIER_VERSION,
                "difficulty_decision_id": stable_hash(f"reference\0{stable_hash(prompt)}", 24),
            }
        )
    if continuation and not causal_report and previous.get("last_route"):
        classification = merge_followup_route(previous["last_route"], classification)
    if causal_active and not new_objective:
        classification["model_profile"] = "work_assessment"
    elif (
        already_confirmed
        and continuation
        and not causal_report
        and not acceptance_miss
        and not plan_changed
        and not new_objective
    ):
        classification["model_profile"] = previous.get(
            "model_profile", "work_executor_low_latest"
        )
    if causal_report:
        classification["model_profile"] = "work_assessment"
    if reference_failure:
        classification.update(
            {
                "task_domain": "work",
                "model_profile": "work_assessment",
                "work_difficulty": "hard",
                "difficulty_confidence": "high",
                "difficulty_rule_codes": ["acceptance_unmet_without_change"],
                "difficulty_classifier_version": DIFFICULTY_CLASSIFIER_VERSION,
                "difficulty_decision_id": stable_hash(
                    f"acceptance_miss\0{previous.get('difficulty_decision_id')}\0{stable_hash(prompt)}",
                    24,
                ),
            }
        )
    telemetry = latest_token_telemetry(payload)

    def update(state: dict[str, Any]) -> None:
        prompt_meta = text_metadata(prompt)
        if causal_report:
            baseline = _safe_execution_baseline(state.get("last_execution_baseline"))
            review_id = stable_hash(
                f"{baseline.get('baseline_id')}\0{prompt_meta.get('fingerprint')}", 32
            )
            state["causal_review"] = {
                "state": "triage_required",
                "review_id": review_id,
                "report_fingerprint": prompt_meta.get("fingerprint"),
                "baseline_id": baseline.get("baseline_id"),
                "outcome": None,
                "evidence_digest": None,
            }
            state["model_profile"] = "work_assessment"
        elif acceptance_miss and state.get("objective"):
            prior = state["objective"]
            state["objective"] = {
                "fingerprint": stable_hash(
                    f"{prior.get('fingerprint')}\0{prompt_meta.get('fingerprint')}", 16
                ),
                "length": max(safe_int(prior.get("length")), 0) + prompt_meta["length"],
                "updated_at": utc_now(),
            }
        elif plan_changed and state.get("objective"):
            prior = state["objective"]
            state["objective"] = {
                "fingerprint": stable_hash(
                    f"{prior.get('fingerprint')}\0{prompt_meta.get('fingerprint')}", 16
                ),
                "length": max(safe_int(prior.get("length")), 0) + prompt_meta["length"],
                "updated_at": utc_now(),
            }
        elif not continuation or not state.get("objective"):
            state["objective"] = {**prompt_meta, "updated_at": utc_now()}
        reference = _safe_reference_acceptance(state.get("reference_acceptance"))
        if requested_reference:
            # Keep only fingerprints/enumerations: never retain media, raw prompt, frames, or observations.
            reference_contract_digest = stable_hash(
                f"reference-v1\0{prompt_meta['fingerprint']}\0{state.get('objective', {}).get('fingerprint')}", 32
            )
            state["reference_acceptance"] = {
                "enabled": True,
                "contract_digest": reference_contract_digest,
                "reference_fingerprint": prompt_meta["fingerprint"],
                "version_fingerprint": stable_hash(WRITER_VERSION, 16),
                "phase": "planned", "state": "planned", "engineering_health": "unknown",
                "functional_acceptance": "unknown", "fidelity_candidate": "unknown",
                "user_final_acceptance": "unknown", "evidence_digest": None,
            }
            if reference.get("contract_digest") != reference_contract_digest:
                state["plan_state"] = "analyzing"
                state["plan_generation"] = max(safe_int(state.get("plan_generation")), 0) + 1
                state["plan_digest"] = None
                state["plan_objective_fingerprint"] = None
                state["plan_difficulty_decision_id"] = None
                state["confirmed_plan_digest"] = None
                state["confirmed_at"] = None
                reset_executor_binding(state)
        elif reference["enabled"] and reference_changed:
            reference.update({"contract_digest": stable_hash(f"{reference['contract_digest']}\0{prompt_meta['fingerprint']}", 32), "version_fingerprint": prompt_meta["fingerprint"], "phase": "planned", "state": "planned", "fidelity_candidate": "unknown", "user_final_acceptance": "unknown", "evidence_digest": None})
            state["reference_acceptance"] = reference
            state["plan_state"] = "analyzing"
            state["plan_generation"] = max(safe_int(state.get("plan_generation")), 0) + 1
            state["plan_digest"] = state["plan_objective_fingerprint"] = state["plan_difficulty_decision_id"] = None
            state["confirmed_plan_digest"] = state["confirmed_at"] = None
            reset_executor_binding(state)
        elif reference["enabled"] and (causal_report or reference_failure or reference_rejection):
            reference.update({"state": "failed", "fidelity_candidate": "failed", "user_final_acceptance": "failed"})
            state["reference_acceptance"] = reference
        elif reference["enabled"] and acceptance_success:
            # Only an explicit user acceptance may finalise reference fidelity.
            reference.update({"state": "accepted", "user_final_acceptance": "accepted"})
            state["reference_acceptance"] = reference
        state["last_route"] = {**classification, "at": utc_now()}
        for key in (
            "task_domain",
            "domain_confidence",
            "domain_rule_codes",
            "model_profile",
            "domain_classifier_version",
            "domain_decision_id",
            "work_difficulty",
            "difficulty_confidence",
            "difficulty_rule_codes",
            "difficulty_classifier_version",
            "difficulty_decision_id",
        ):
            state[key] = classification.get(key)
        if causal_report or reference_failure or (causal_active and not new_objective):
            state["model_profile"] = "work_assessment"
        elif already_confirmed and continuation and not plan_changed and not new_objective:
            state["model_profile"] = previous.get("model_profile", "work_executor_low_latest")
        objective_fingerprint = state.get("objective", {}).get("fingerprint")
        if causal_report:
            # Freeze the old plan/contract for comparison. The causal guard allows only
            # evidence collection until a structured, baseline-bound conclusion is recorded.
            if state.get("last_execution_baseline"):
                state["last_execution_baseline"]["acceptance_status"] = "failed"
        elif reference_failure:
            state["plan_state"] = "analyzing"
            state["plan_generation"] = max(safe_int(state.get("plan_generation")), 0) + 1
            state["plan_digest"] = None
            state["plan_objective_fingerprint"] = None
            state["plan_difficulty_decision_id"] = None
            state["confirmed_plan_digest"] = None
            state["confirmed_at"] = None
            reset_executor_binding(state)
            state["causal_review"] = _safe_causal_review(None)
            if state.get("last_execution_baseline"):
                state["last_execution_baseline"]["acceptance_status"] = "failed"
            state["model_profile"] = "work_assessment"
        elif confirmed_plan:
            binding_valid = bool(
                state.get("plan_digest")
                and state.get("plan_objective_fingerprint") == objective_fingerprint
                and state.get("plan_difficulty_decision_id")
                == state.get("difficulty_decision_id")
            )
            if binding_valid:
                state["plan_state"] = "confirmed"
                state["confirmed_plan_digest"] = state.get("plan_digest")
                state["confirmed_at"] = utc_now()
                if not initialize_confirmed_executor(state):
                    state["plan_state"] = "invalidated"
                    state["confirmed_plan_digest"] = None
                    state["confirmed_at"] = None
            else:
                state["plan_state"] = "invalidated"
                state["confirmed_plan_digest"] = None
                state["confirmed_at"] = None
                reset_executor_binding(state)
        elif replan or plan_changed:
            state["plan_state"] = "analyzing"
            state["plan_generation"] = max(safe_int(state.get("plan_generation")), 0) + 1
            state["plan_digest"] = None
            state["plan_objective_fingerprint"] = None
            state["plan_difficulty_decision_id"] = None
            state["confirmed_plan_digest"] = None
            state["confirmed_at"] = None
            reset_executor_binding(state)
            state["model_profile"] = "work_assessment"
        elif not continuation:
            state["plan_generation"] = 0
            state["plan_digest"] = None
            state["plan_objective_fingerprint"] = None
            state["plan_difficulty_decision_id"] = None
            state["confirmed_plan_digest"] = None
            state["confirmed_at"] = None
            reset_executor_binding(state)
            state["plan_state"] = (
                "analyzing" if classification.get("work_difficulty") == "hard" else "none"
            )
            state["causal_review"] = _safe_causal_review(None)
            state["last_execution_baseline"] = {}
        if acceptance_success and state.get("last_execution_baseline"):
            state["last_execution_baseline"]["acceptance_status"] = "passed"
            if causal_active:
                state["causal_review"] = _safe_causal_review(None)
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
    should_inject = causal_report or reference_failure or causal_active or classification["task_domain"] == "work" or classification["label"] in {"complex", "extensive"} or (
        isinstance(pressure, (int, float)) and pressure >= PRESSURE_TRIM_THRESHOLD
    )
    if not should_inject:
        return
    context = routing_context(classification, telemetry)
    if causal_report:
        refreshed = snapshot_state(payload)
        review = _safe_causal_review(refreshed.get("causal_review"))
        context += (
            " Causal review required before any corrective mutation. Compare the prior objective, confirmed plan, "
            "execution contract, bounded change/verification baseline, temporal order, changed inputs/environment, "
            "and the reported symptom. User wording is a trigger, never proof. Keep work read-only until evidence "
            f"supports a bound conclusion. baseline_id={review.get('baseline_id')} "
            f"review_id={review.get('review_id')}. End the assessment with exactly: CAUSAL_REVIEW "
            f"baseline_id={review.get('baseline_id')} review_id={review.get('review_id')} "
            "outcome=<introduced|fix_ineffective|unrelated|uncertain> evidence_digest=<32hex>."
        )
    elif reference_failure:
        context += (
            " Acceptance was not met, but the completed executor recorded no successful change set, so do not "
            "claim that a prior mutation introduced the symptom and do not open a causal-review contract. The old "
            "execution contract is invalidated. Use high-reasoning read-only analysis, then present one coherent "
            "replacement Hard plan covering the original acceptance, current symptom, verification, risk, and "
            "rollback; wait for strict confirmation before mutation."
        )
    elif causal_active and not new_objective and not acceptance_success:
        refreshed = snapshot_state(payload)
        review = _safe_causal_review(refreshed.get("causal_review"))
        context += (
            " Continue the active causal review read-only; do not reactivate or reuse the old confirmed executor. "
            "Relate any new facts to the original objective, plan, contract, change baseline, and verification, "
            f"then bind the conclusion to baseline_id={review.get('baseline_id')} and "
            f"review_id={review.get('review_id')}."
        )
    elif confirmed_plan:
        context += (
            " Confirmed plan binding is valid. The parent remains the high-reasoning coordinator; before any "
            "mutation, spawn exactly one exclusive confirmed-plan executor using the newest actually available "
            "lower-tier Codex model, reasoning_effort=medium, and fork_turns=none or a positive integer. Bind its "
            "request to execution_contract_id, plan_digest, plan_generation, full actionable plan, scope, and "
            "acceptance. Do not claim a model switch until the host accepts that explicit spawn override."
        )
    elif replan or plan_changed:
        context += " Pending plan invalidated by changed constraints; re-analyze and present a replacement plan before mutation."
    elif already_confirmed and not plan_changed and not new_objective:
        context += (
            " Confirmed plan binding is valid; the existing execution contract remains active. Continue from "
            "its recorded executor/recovery "
            "state instead of treating this follow-up as a new confirmation or redoing completed work."
        )
    elif classification.get("work_difficulty") == "hard" and not pending_plan:
        context += (
            " Hard work: use the highest available model/reasoning for analysis; present a detailed file/method, "
            "build/deploy, verification, risk, and rollback plan ending with '计划已就绪，等待确认后执行'. "
            "Do not mutate, build, or deploy before strict confirmation."
        )
    elif pending_plan and not confirmed_plan:
        context += " Awaiting strict plan confirmation; answer plan questions but do not mutate, build, or deploy."
    emit_context("UserPromptSubmit", context)


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

    executor_request, _ = confirmed_executor_request(payload, state)
    route_value = state.get("last_route")
    route_known = isinstance(route_value, dict) and bool(route_value)
    route = safe_route(route_value)
    gate = str(route.get("delegation_gate") or "closed")
    cap = safe_int(route.get("recommended_agent_cap"))
    if executor_request and state.get("executor_state") in {"spawn_required", "recovery_required"}:
        # This is a sequential model-profile handoff, not an extra parallel lane. It still has a
        # strict singleton contract, but must not be blocked by a shared-device route cap of zero.
        route_known = True
        gate = "open"
        cap = 1
    reaudited = (
        not executor_request
        and gate == "closed"
        and request_supports_delegation_reaudit(payload, route)
    )
    if reaudited:
        gate = "audit"
        cap = 1
    active = [] if executor_request else active_agent_records(state)
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
        options = subagent_request_options(payload)
        attempt = min(
            max(safe_int(current.get("executor_attempt")), 0) + 1,
            MAX_EXECUTOR_ATTEMPTS,
        ) if executor_request else 0
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
                "role": "confirmed_executor" if executor_request else "lane",
                "contract_id": current.get("execution_contract_id") if executor_request else None,
                "model": safe_label(options.get("model"), 80) if options.get("model") else None,
                "reasoning_effort": (
                    safe_label(options.get("reasoning_effort"), 24)
                    if options.get("reasoning_effort")
                    else None
                ),
                "fork_turns": options.get("fork_turns"),
                "attempt": attempt,
            }
        )
        if executor_request:
            current["executor_state"] = "spawn_pending"
            current["executor_attempt"] = attempt
            current["executor_failure_kind"] = None
            current["executor_model"] = safe_label(options.get("model"), 80)
            current["executor_reasoning_effort"] = safe_label(
                options.get("reasoning_effort"), 24
            )
            current["executor_fork_turns"] = str(options.get("fork_turns"))
            current["model_profile"] = "work_executor_low_latest"

    mutate_state(payload, record_request)
    if executor_request:
        emit_context(
            "PreToolUse",
            "Confirmed-plan executor request accepted: the explicit host model override, medium reasoning, "
            "non-full-history fork, exact execution contract, exclusive ownership, and acceptance markers match. "
            "This records a requested profile, not proof that the child started; wait for SubagentStart. The "
            "parent remains coordinator/reviewer and must not perform the executor's mutation.",
        )
    elif reaudited:
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
    state = snapshot_state(payload)
    causal_block = causal_review_guard(payload, state)
    if causal_block:
        def record_causal_guard(current: dict[str, Any]) -> None:
            current.setdefault("guards", []).append(
                {
                    "at": utc_now(),
                    "turn_id": safe_label(payload.get("turn_id"), 120)
                    if payload.get("turn_id")
                    else None,
                    "kind": "causal_review",
                    "action": "deny",
                    "fingerprint": fingerprint,
                }
            )
            review = _safe_causal_review(current.get("causal_review"))
            if review.get("state") == "triage_required":
                review["state"] = "triaging"
                current["causal_review"] = review

        mutate_state(payload, record_causal_guard)
        emit_pretool_deny(
            f"Workflow Manager blocked {causal_block}: causal review is read-only until a conclusion bound to "
            "the current baseline_id and review_id records introduced, fix_ineffective, unrelated, or uncertain. "
            "Compare prior plan/change/verification evidence before corrective mutation; do not reuse the old executor."
        )
        return
    executor_block = executor_gate_guard(payload, state)
    if executor_block:
        def record_executor_guard(current: dict[str, Any]) -> None:
            current.setdefault("guards", []).append(
                {
                    "at": utc_now(),
                    "turn_id": safe_label(payload.get("turn_id"), 120)
                    if payload.get("turn_id")
                    else None,
                    "kind": "executor_contract",
                    "action": "deny",
                    "fingerprint": fingerprint,
                }
            )
            if is_subagent_spawn_tool(payload):
                if current.get("executor_state") != "recovery_required":
                    current["executor_failure_kind"] = (
                        "model_unavailable"
                        if executor_block == "model_unavailable"
                        else "invalid_spawn_config"
                    )
                current["model_profile"] = "work_assessment"

        mutate_state(payload, record_executor_guard)
        emit_pretool_deny(
            f"Workflow Manager blocked {executor_block}: this confirmed hard-work plan requires exactly one "
            "contract-bound executor. Resolve the newest actually available lower-tier model, explicitly spawn "
            "it with reasoning_effort=medium and fork_turns=none or a positive integer, and include the exact "
            "execution_contract_id, plan_digest, plan_generation, exclusive scope, full actionable plan, and "
            "acceptance. The parent model was not switched and must not silently execute the mutation."
        )
        return
    if is_subagent_spawn_tool(payload):
        valid_executor, _ = confirmed_executor_request(payload, state)
        if valid_executor:
            if handle_subagent_pretool(payload, state, fingerprint):
                return
        route = safe_route(state.get("last_route"))
        gate = str(route.get("delegation_gate") or "closed")
        if gate == "closed":
            if handle_subagent_pretool(payload, state, fingerprint):
                return
        elif subagent_request_is_read_only(payload):
            if handle_subagent_pretool(payload, state, fingerprint):
                return
    blocked_action = plan_confirmation_guard(payload, state)
    if blocked_action:
        def record_plan_guard(current: dict[str, Any]) -> None:
            current.setdefault("guards", []).append(
                {
                    "at": utc_now(),
                    "turn_id": safe_label(payload.get("turn_id"), 120)
                    if payload.get("turn_id")
                    else None,
                    "kind": "plan_confirmation",
                    "action": "deny",
                    "fingerprint": fingerprint,
                }
            )

        mutate_state(payload, record_plan_guard)
        emit_pretool_deny(
            f"Workflow Manager blocked {blocked_action}: this hard work plan is not strictly confirmed. "
            "Continue read-only analysis or evidence collection, present the detailed plan ending with "
            "'计划已就绪，等待确认后执行', and wait for a pure confirmation bound to the current plan."
        )
        return
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
    caller_id = next(
        (
            safe_label(payload.get(key), 120)
            for key in ("agent_id", "subagent_id")
            if payload.get(key)
        ),
        None,
    )

    def update(state: dict[str, Any]) -> None:
        active_plan_digest = (
            state.get("plan_digest")
            if state.get("plan_state") == "confirmed"
            and state.get("confirmed_plan_digest") == state.get("plan_digest")
            else None
        )
        state.setdefault("operations", []).append(
            {
                "at": utc_now(),
                "turn_id": safe_label(payload.get("turn_id"), 120) if payload.get("turn_id") else None,
                "tool": tool,
                "fingerprint": fingerprint,
                "status": status_value,
                "category": category,
                "plan_digest": active_plan_digest,
                "execution_contract_id": (
                    state.get("execution_contract_id")
                    if active_plan_digest and caller_id == state.get("executor_agent_id")
                    else None
                ),
                "executor_agent_id": (
                    caller_id if caller_id == state.get("executor_agent_id") else None
                ),
                "risk_kind": risk_kind,
                **response_meta,
                "budgeted": budgeted,
                "oversized": oversized,
                "compacted": compacted,
            }
        )
        failed = status_value.startswith("error") or status_value in ERROR_STATUSES
        executor_operation = bool(
            caller_id
            and caller_id == state.get("executor_agent_id")
            and state.get("executor_state") == "running"
        )
        if executor_operation and failed:
            failure_by_category = {
                "implementation": "implementation_failed",
                "build_package": "build_failed",
                "delivery_device": "deploy_failed",
                "verification": "verification_failed",
                "evidence": "verification_failed",
            }
            state["executor_failure_kind"] = failure_by_category.get(
                category, "executor_failed"
            )
            state["executor_state"] = (
                "recovery_required"
                if safe_int(state.get("executor_attempt")) < MAX_EXECUTOR_ATTEMPTS
                else "exhausted"
            )
            state["model_profile"] = "work_assessment"
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
            "work_difficulty": state.get("work_difficulty", "unknown"),
            "difficulty_decision_id": state.get("difficulty_decision_id"),
            "plan_state": state.get("plan_state", "none"),
            "plan_generation": safe_int(state.get("plan_generation")),
            "plan_digest": state.get("plan_digest"),
                "confirmed_plan_digest": state.get("confirmed_plan_digest"),
                "execution_profile_version": state.get("execution_profile_version"),
                "executor_state": state.get("executor_state", "none"),
                "execution_contract_id": state.get("execution_contract_id"),
                "executor_attempt": safe_int(state.get("executor_attempt")),
                "executor_failure_kind": state.get("executor_failure_kind"),
                "reference_acceptance": _safe_reference_acceptance(state.get("reference_acceptance")),
                "last_execution_baseline": _safe_execution_baseline(
                    state.get("last_execution_baseline")
                ),
            "causal_review": _safe_causal_review(state.get("causal_review")),
            "reference_acceptance": _safe_reference_acceptance(state.get("reference_acceptance")),
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
            role = item.get("role") or "lane"
            if role == "confirmed_executor":
                for active_id, active_item in list(active.items()):
                    if active_item.get("role") == "confirmed_executor":
                        active.pop(active_id, None)
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
    used_counts: dict[str, int] = {}
    for item in state.get("subagents", []):
        if item.get("event") != "start" or not item.get("request_fingerprint"):
            continue
        fingerprint = str(item.get("request_fingerprint"))
        used_counts[fingerprint] = used_counts.get(fingerprint, 0) + 1
    seen_counts: dict[str, int] = {}
    candidates: list[dict[str, Any]] = []
    for item in state.get("subagents", []):
        if item.get("event") != "request" or not item.get("request_fingerprint"):
            continue
        fingerprint = str(item.get("request_fingerprint"))
        occurrence = seen_counts.get(fingerprint, 0)
        seen_counts[fingerprint] = occurrence + 1
        if occurrence >= used_counts.get(fingerprint, 0):
            candidates.append(item)
    if not candidates:
        return None
    turn_id = safe_label(payload.get("turn_id"), 120) if payload.get("turn_id") else None
    if turn_id:
        same_turn = [item for item in candidates if item.get("turn_id") == turn_id]
        if same_turn:
            executor_same_turn = [
                item
                for item in same_turn
                if item.get("role") == "confirmed_executor"
                and item.get("contract_id") == state.get("execution_contract_id")
                and safe_int(item.get("attempt")) == safe_int(state.get("executor_attempt"))
            ]
            return (executor_same_turn or same_turn)[-1]
    executor_pending = [
        item
        for item in candidates
        if item.get("role") == "confirmed_executor"
        and item.get("contract_id") == state.get("execution_contract_id")
        and safe_int(item.get("attempt")) == safe_int(state.get("executor_attempt"))
    ]
    return (executor_pending or candidates)[-1]


def subagent_start(payload: dict[str, Any]) -> None:
    previous = snapshot_state(payload)
    request = pending_subagent_request(previous, payload) or {}
    scope_value = payload.get("prompt") or payload.get("task") or payload.get("message")
    scope_fingerprint = (stable_hash(scope_value) if scope_value else None) or request.get("scope_fingerprint")
    task_name = task_name_from_payload(payload) or request.get("task_name")
    request_fingerprint = request.get("request_fingerprint")
    executor_request = request.get("role") == "confirmed_executor"
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
        agent_id = safe_label(payload.get("agent_id"), 120)
        request_contract = request.get("contract_id")
        contract_matches = bool(
            executor_request
            and request_contract
            and request_contract == state.get("execution_contract_id")
            and request_contract == execution_contract_id(state)
            and request.get("model") == state.get("executor_model")
            and request.get("reasoning_effort") == "medium"
            and request.get("fork_turns") == state.get("executor_fork_turns")
        )
        state.setdefault("subagents", []).append(
            {
                "at": utc_now(),
                "event": "start",
                "turn_id": safe_label(payload.get("turn_id"), 120) if payload.get("turn_id") else None,
                "agent_id": agent_id,
                "agent_type": safe_label(payload.get("agent_type"), 80),
                "task_name": task_name,
                "scope_fingerprint": scope_fingerprint,
                "request_fingerprint": request_fingerprint,
                "objective_fingerprint": objective_fingerprint,
                "stale": False,
                "status": "running",
                "role": request.get("role") or "lane",
                "contract_id": request_contract,
                "model": request.get("model"),
                "reasoning_effort": request.get("reasoning_effort"),
                "fork_turns": request.get("fork_turns"),
                "attempt": request.get("attempt"),
            }
        )
        if executor_request:
            if contract_matches:
                state["executor_state"] = "running"
                state["executor_agent_id"] = agent_id
                state["executor_failure_kind"] = None
            else:
                state["executor_state"] = "recovery_required"
                state["executor_agent_id"] = None
                state["executor_failure_kind"] = "start_mismatch"

    _, changed = mutate_state(payload, update)
    warnings: list[str] = []
    if executor_request:
        refreshed = snapshot_state(payload)
        if refreshed.get("executor_state") != "running":
            warnings.append(
                "Confirmed executor start did not match the persisted contract/config; do not mutate and return control for recovery."
            )
        else:
            warnings.append(
                "Confirmed executor is active. Execute only the bound plan and acceptance, preserve evidence, and stop on contract drift."
            )
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
    executor_agent = bool((started or {}).get("role") == "confirmed_executor")

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
                "role": (started or {}).get("role") or "lane",
                "contract_id": (started or {}).get("contract_id"),
                "model": (started or {}).get("model"),
                "reasoning_effort": (started or {}).get("reasoning_effort"),
                "fork_turns": (started or {}).get("fork_turns"),
                "attempt": (started or {}).get("attempt"),
            }
        )
        if executor_agent and state.get("executor_agent_id") == agent_id:
            contract_current = bool(
                not stale
                and (started or {}).get("contract_id") == state.get("execution_contract_id")
                and state.get("execution_contract_id") == execution_contract_id(state)
            )
            successful = status_value in {"completed", "ok"}
            if not contract_current:
                state["executor_state"] = "recovery_required"
                state["executor_failure_kind"] = "stale_contract"
                state["model_profile"] = "work_assessment"
            elif successful and state.get("executor_state") == "running":
                state["executor_state"] = "succeeded"
                state["executor_failure_kind"] = None
                baseline = build_execution_baseline(state)
                if baseline:
                    state["last_execution_baseline"] = baseline
                    state["causal_review"] = _safe_causal_review(None)
            elif not successful:
                state["executor_state"] = (
                    "recovery_required"
                    if safe_int(state.get("executor_attempt")) < MAX_EXECUTOR_ATTEMPTS
                    else "exhausted"
                )
                state["executor_failure_kind"] = "executor_failed"
                state["model_profile"] = "work_assessment"

    mutate_state(payload, update)
    if stale:
        emit_context(
            "SubagentStop",
            "Stale subagent result: the objective changed after this agent started. Use it only as verification; "
            "it must not drive mutation for the previous objective.",
        )
    elif executor_agent and status_value not in {"completed", "ok"}:
        emit_context(
            "SubagentStop",
            "Confirmed executor failed. Return to the high-reasoning parent for one diagnosis. Do not repeat "
            "unchanged actions: either make a material correction and use the one remaining bounded executor "
            "attempt, or invalidate/replan when scope or acceptance changes.",
        )
    else:
        emit_continue()


CAUSAL_REVIEW_RESULT_RE = re.compile(
    r"(?im)^\s*CAUSAL_REVIEW\s+"
    r"baseline_id=([0-9a-f]{32})\s+"
    r"review_id=([0-9a-f]{32})\s+"
    r"outcome=(introduced|fix_ineffective|unrelated|uncertain)\s+"
    r"evidence_digest=([0-9a-f]{32})\s*$"
)


def stop(payload: dict[str, Any]) -> None:
    assistant_message = str(payload.get("last_assistant_message") or "")
    causal_match = CAUSAL_REVIEW_RESULT_RE.search(assistant_message)
    plan_ready = bool(
        len(re.findall(r"(?m)^\s*(?:[-*]|\d+[.)、])\s+", assistant_message)) >= 2
        and re.search(r"(?:验收|验证|test|verify|acceptance)", assistant_message, re.I)
        and re.search(r"计划已就绪，等待确认后执行[。.!！\s]*$", assistant_message)
    )

    def update(state: dict[str, Any]) -> None:
        state["last_assistant"] = text_metadata(assistant_message)
        state["last_stop_at"] = utc_now()
        review = _safe_causal_review(state.get("causal_review"))
        if review.get("state") in {"triage_required", "triaging"}:
            if causal_match:
                baseline_id, review_id, outcome, evidence_digest = causal_match.groups()
                binding_valid = bool(
                    baseline_id == review.get("baseline_id")
                    and review_id == review.get("review_id")
                    and baseline_id
                    == _safe_execution_baseline(
                        state.get("last_execution_baseline")
                    ).get("baseline_id")
                )
                if binding_valid:
                    review["outcome"] = outcome
                    review["evidence_digest"] = evidence_digest
                    if outcome == "uncertain":
                        review["state"] = "triaging"
                        state["model_profile"] = "work_assessment"
                    else:
                        review["state"] = "resolved"
                        report_fingerprint = str(review.get("report_fingerprint") or "")
                        prior_objective = str(
                            state.get("objective", {}).get("fingerprint") or ""
                        )
                        state["objective"] = {
                            "fingerprint": stable_hash(
                                f"{prior_objective}\0{report_fingerprint}\0{review_id}", 16
                            ),
                            "length": max(
                                safe_int(state.get("objective", {}).get("length")), 0
                            ),
                            "updated_at": utc_now(),
                        }
                        state["plan_state"] = "analyzing"
                        state["plan_generation"] = max(
                            safe_int(state.get("plan_generation")), 0
                        ) + 1
                        state["plan_digest"] = None
                        state["plan_objective_fingerprint"] = None
                        state["plan_difficulty_decision_id"] = None
                        state["confirmed_plan_digest"] = None
                        state["confirmed_at"] = None
                        reset_executor_binding(state)
                        prior_followup_difficulty = state.get("work_difficulty")
                        prior_followup_confidence = state.get("difficulty_confidence")
                        prior_followup_rules = list(state.get("difficulty_rule_codes") or [])
                        prior_followup_decision = state.get("difficulty_decision_id")
                        state["task_domain"] = "work"
                        state["work_difficulty"] = (
                            "hard"
                            if outcome in {"introduced", "fix_ineffective"}
                            else (
                                prior_followup_difficulty
                                if prior_followup_difficulty in {"simple", "hard"}
                                else "unknown"
                            )
                        )
                        state["difficulty_confidence"] = (
                            "high"
                            if outcome in {"introduced", "fix_ineffective"}
                            else (
                                prior_followup_confidence
                                if prior_followup_confidence in {"low", "medium", "high"}
                                else "low"
                            )
                        )
                        state["difficulty_rule_codes"] = (
                            ["causal_regression_review"]
                            if outcome in {"introduced", "fix_ineffective"}
                            else prior_followup_rules or ["causal_unrelated_followup"]
                        )
                        state["difficulty_decision_id"] = (
                            stable_hash(f"causal\0{outcome}\0{review_id}", 24)
                            if outcome in {"introduced", "fix_ineffective"}
                            else safe_fingerprint(prior_followup_decision)
                            or stable_hash(f"causal\0{outcome}\0{review_id}", 24)
                        )
                        state["model_profile"] = "work_assessment"
                        route = dict(state.get("last_route") or {})
                        route.update(
                            {
                                "task_domain": state["task_domain"],
                                "model_profile": state["model_profile"],
                                "work_difficulty": state["work_difficulty"],
                                "difficulty_confidence": state["difficulty_confidence"],
                                "difficulty_rule_codes": state["difficulty_rule_codes"],
                                "difficulty_classifier_version": DIFFICULTY_CLASSIFIER_VERSION,
                                "difficulty_decision_id": state["difficulty_decision_id"],
                                "at": utc_now(),
                            }
                        )
                        state["last_route"] = route
                    state["causal_review"] = review
            return
        if state.get("work_difficulty") == "hard" and state.get("plan_state") in {
            "analyzing",
            "invalidated",
        }:
            if plan_ready:
                state["plan_generation"] = max(safe_int(state.get("plan_generation")), 0) + 1
                state["plan_digest"] = stable_hash(assistant_message, 32)
                state["plan_objective_fingerprint"] = state.get("objective", {}).get("fingerprint")
                state["plan_difficulty_decision_id"] = state.get("difficulty_decision_id")
                state["confirmed_plan_digest"] = None
                state["confirmed_at"] = None
                state["plan_state"] = "awaiting_confirmation"
            else:
                state["plan_state"] = "analyzing"

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
