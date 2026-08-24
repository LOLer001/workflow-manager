#!/usr/bin/env python3
"""Fail-open lifecycle hook for compact, privacy-safe Codex task continuity."""

from __future__ import annotations

import hashlib
import errno
import json
import os
import re
import secrets
import shlex
import shutil
import stat
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator


SCHEMA_VERSION = 25
WRITER_VERSION = "1.0.44"
DOMAIN_CLASSIFIER_VERSION = "1"
DIFFICULTY_CLASSIFIER_VERSION = "1"
EXECUTION_PROFILE_VERSION = "8"
# The assessor is the hard-work safety gate: use the highest generally exposed
# effort (max); ultra is reserved for the explicit whole-session policy.
DEFAULT_PLAN_REASONING_EFFORT = "max"
HIGHEST_SESSION_REASONING_EFFORT = "ultra"
STABLE_SKILL_NAME = "workflow-manager"
STABLE_SKILL_SCHEMA = 6
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
MAX_TERMINAL_SUBAGENT_LIFECYCLES = 10
MAX_COMPACTIONS = 16
MAX_GUARDS = 32
MAX_PROCESSED_RUNS = 128
MAX_DUPLICATE_NOTICES = 64
MAX_COORDINATION_ACTIVITY = 32
MAX_COORDINATION_NOTICES = 32
MAX_COORDINATION_INBOUND = 32
MAX_CHANGE_EPOCH_LEDGER = 16
COORDINATION_SNAPSHOT_TTL_SECONDS = 60
COORDINATION_ID_MAX_BYTES = 4096
MAX_STATE_BYTES = 1024 * 1024
MAX_PLAN_REVISION_BYTES = 960 * 1024
MAX_PLAN_JOURNAL_BYTES = 10 * 1024 * 1024
# Compatibility alias for callers that inspected the v1 mirror limit.  Since Schema 20
# it is the hard per-revision limit and content is rejected instead of truncated.
MAX_PLAN_ARTIFACT_BODY_BYTES = MAX_PLAN_REVISION_BYTES
MAX_LEGACY_PLAN_ARTIFACTS = 6
MAX_OLD_PLAN_ARTIFACTS = 5
MAX_RETENTION_TRANSACTION_ITEMS = 16
TRANSCRIPT_TAIL_BYTES = 1024 * 1024
# A host rollout is the only supported bridge for Codex Desktop compactions
# that do not dispatch the declared Pre/PostCompact hooks.  Keep this bounded:
# an oversized or non-regular file is not evidence.
HOST_ROLLOUT_RECONCILE_BYTES = 4 * 1024 * 1024
LOCK_TIMEOUT_SECONDS = 0.75
DUPLICATE_TTL_SECONDS = 15 * 60
DEFAULT_RETENTION_DAYS = 30
DEFAULT_MAX_SESSION_FILES = 200
DEFAULT_OUTPUT_CHAR_LIMIT = 16_000
DEFAULT_OUTPUT_LINE_LIMIT = 300
DEFAULT_VISUAL_ITEM_LIMIT = 3
PRESSURE_TRIM_THRESHOLD = 0.55
PRESSURE_CHECKPOINT_THRESHOLD = 0.70

MODEL_PROFILES = {
    "current",
    "work_assessment",
    "work_executor_low_latest",
    "work_executor_highest_available",
}
SESSION_EXECUTION_PREFERENCES = {"default", "highest_throughout"}
EXECUTOR_STATES = {
    "none",
    "spawn_required",
    "spawn_pending",
    "running",
    "local_running",
    "verification_required",
    "succeeded",
    "recovery_required",
    "exhausted",
}
EXECUTOR_REVIEW_STATUSES = {
    "none",
    "review_required",
    "recovery_started",
    "passed",
    "failed",
    "exhausted",
}
EXECUTOR_FAILURE_KINDS = {
    "protocol_missing_model",
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
STALL_STATES = {
    "none",
    "diagnosis_required",
    "diagnosis_pending",
    "diagnosing",
    "resume_required",
    "resuming",
    "resolved",
    "exhausted",
}
STALL_RESUME_PROFILES = {
    "work_executor_low_latest",
    "work_executor_highest_available",
}
MAX_STALL_DIAGNOSIS_ATTEMPTS = 2
ASSESSOR_STATES = {"none", "spawn_required", "spawn_pending", "running", "simple_execution_required", "simple_running", "simple_complete", "hard_plan_ready", "recovery_required", "failed"}
CAUSAL_REVIEW_STATES = {"none", "triage_required", "triaging", "resolved"}
CAUSAL_REVIEW_OUTCOMES = {"introduced", "fix_ineffective", "unrelated", "uncertain"}
BASELINE_ACCEPTANCE_STATUSES = {"passed", "failed", "incomplete", "unknown"}
REFERENCE_ACCEPTANCE_STATES = {"disabled", "planned", "candidate", "accepted", "failed"}
PLAN_ARTIFACT_LIFECYCLE_STATUSES = {
    "none",
    "ready",
    "confirmed",
    "executing",
    "succeeded",
    "invalidated",
}
PLAN_ARTIFACT_WRITE_STATUSES = {
    "none",
    "written",
    "write_failed",
    "content_drift",
    "legacy_unavailable",
    "revision_too_large",
    "journal_full",
    "transaction_recovery_failed",
}
PLAN_ARTIFACT_WARNING_CODES = {
    "none",
    "unsafe_data_root",
    "unsafe_path",
    "write_error",
    "content_drift",
    "legacy_unavailable",
    "revision_too_large",
    "journal_full",
    "transaction_incomplete",
    "transaction_recovery_failed",
    "execution_slices_invalid",
}
LEGACY_PLAN_ARTIFACT_OWNER = "<!-- workflow-manager-plan-artifact:v1"
PLAN_ARTIFACT_OWNER = LEGACY_PLAN_ARTIFACT_OWNER
PLAN_JOURNAL_OWNER = "<!-- workflow-manager-plan-journal:v2"
PLAN_REVISION_OWNER = "<!-- workflow-manager-plan-revision:v2"
PLAN_ARTIFACT_BODY_MARKER = "<!-- workflow-manager-plan-body -->"
PLAN_JOURNAL_NAME = "hard-plan.md"
LEGACY_PLAN_ARTIFACT_NAME_RE = re.compile(
    r"^hard-plan-g([0-9]{4,})-([0-9a-f]{32})\.md$"
)
PLAN_ARTIFACT_NAME_RE = re.compile(
    r"^(?:hard-plan\.md|hard-plan-g[0-9]{4,}-[0-9a-f]{32}\.md)$"
)
PLAN_TRANSACTION_MARKER_NAME = ".hard-plan.md.transaction.json"
MAX_PLAN_TRANSACTION_MARKER_BYTES = 4096
EXECUTION_SLICE_SCHEMA = 1
MAX_EXECUTION_SLICES = 8
MAX_SLICE_LIST_ITEMS = 8
MAX_SLICE_TEXT_BYTES = 240
EVIDENCE_DIGEST_PROFILE = "workflow-manager-host-evidence-v1"
EVIDENCE_DIGEST_SOURCE = "host_normalized"

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
    r"(?:api[_-]?key|(?:token|[a-z][a-z0-9_-]{0,63}token)|client[_-]?secret|secret|password|passwd|"
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


def canonical_json(value: Any) -> str:
    """Return the one CRLF-stable JSON representation used by execution contracts."""
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).replace("\r\n", "\n").replace("\r", "\n")


def _empty_execution_slices() -> dict[str, Any]:
    return {
        "schema": EXECUTION_SLICE_SCHEMA,
        "plan_digest": None,
        "manifest_digest": None,
        "global_constraints_digest": None,
        "count": 0,
        "current_index": 0,
        "completed_chain": None,
        "items": [],
    }


def _bounded_manifest_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if (
        not normalized
        or len(normalized.encode("utf-8", errors="replace")) > MAX_SLICE_TEXT_BYTES
        or any(ord(character) < 32 and character not in {"\n", "\t"} for character in normalized)
    ):
        return None
    return normalized


def _bounded_manifest_list(value: Any) -> list[str] | None:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_SLICE_LIST_ITEMS:
        return None
    result = [_bounded_manifest_string(item) for item in value]
    return [str(item) for item in result] if all(item is not None for item in result) else None


def _strict_json_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


EXECUTION_SLICES_FENCE_RE = re.compile(
    r"(?ms)^```workflow-manager-execution-slices[ \t]*\n(.*?)^```[ \t]*(?:\n)?\Z"
)
EXECUTION_SLICES_FENCE_INTENT_RE = re.compile(
    r"(?m)^\s*```workflow-manager-execution-slices\b"
)
EXECUTION_SLICE_FIELDS = (
    "title",
    "scope",
    "acceptance",
    "rollback",
    "stop_conditions",
    "expected_artifacts",
)


def parse_execution_slice_manifest(plan_body: str) -> dict[str, Any]:
    """Strictly parse the single machine-readable execution-slice block."""
    normalized = str(plan_body or "").replace("\r\n", "\n").replace("\r", "\n")
    intents = EXECUTION_SLICES_FENCE_INTENT_RE.findall(normalized)
    matches = EXECUTION_SLICES_FENCE_RE.findall(normalized)
    if len(intents) != 1 or len(matches) != 1:
        raise PlanArtifactError("execution_slices_invalid")
    try:
        decoded = json.loads(matches[0], object_pairs_hook=_strict_json_object_pairs)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise PlanArtifactError("execution_slices_invalid") from error
    if not isinstance(decoded, dict) or set(decoded) != {"version", "global_constraints", "slices"}:
        raise PlanArtifactError("execution_slices_invalid")
    if decoded.get("version") != EXECUTION_SLICE_SCHEMA:
        raise PlanArtifactError("execution_slices_invalid")
    global_constraints = _bounded_manifest_list(decoded.get("global_constraints"))
    raw_slices = decoded.get("slices")
    if (
        global_constraints is None
        or not isinstance(raw_slices, list)
        or not 1 <= len(raw_slices) <= MAX_EXECUTION_SLICES
    ):
        raise PlanArtifactError("execution_slices_invalid")
    canonical_slices: list[dict[str, Any]] = []
    expected_keys = {"id", *EXECUTION_SLICE_FIELDS}
    for index, raw in enumerate(raw_slices, start=1):
        expected_id = f"s{index:02d}"
        if not isinstance(raw, dict) or set(raw) != expected_keys or raw.get("id") != expected_id:
            raise PlanArtifactError("execution_slices_invalid")
        title = _bounded_manifest_string(raw.get("title"))
        if title is None:
            raise PlanArtifactError("execution_slices_invalid")
        item: dict[str, Any] = {"id": expected_id, "title": title}
        for field in EXECUTION_SLICE_FIELDS[1:]:
            values = _bounded_manifest_list(raw.get(field))
            if values is None:
                raise PlanArtifactError("execution_slices_invalid")
            item[field] = values
        canonical_slices.append(item)
    canonical_manifest = {
        "global_constraints": global_constraints,
        "slices": canonical_slices,
        "version": EXECUTION_SLICE_SCHEMA,
    }
    manifest_digest = stable_hash(
        "workflow-manager-execution-slices-v1\0" + canonical_json(canonical_manifest),
        32,
    )
    global_digest = stable_hash(
        "workflow-manager-global-constraints-v1\0" + canonical_json(global_constraints),
        32,
    )
    items: list[dict[str, Any]] = []
    for item in canonical_slices:
        slice_digest = stable_hash(
            "workflow-manager-execution-slice-v1\0" + canonical_json(item), 32
        )
        items.append(
            {
                **item,
                "slice_digest": slice_digest,
                "status": "pending",
                "completion_digest": None,
                "review_digest": None,
                "operation_digest": None,
                "change_evidence": False,
                "verification_evidence": False,
            }
        )
    return {
        "schema": EXECUTION_SLICE_SCHEMA,
        "plan_digest": None,
        "manifest_digest": manifest_digest,
        "global_constraints_digest": global_digest,
        "global_constraints": global_constraints,
        "count": len(items),
        "current_index": 1,
        "completed_chain": stable_hash(
            f"workflow-manager-slice-chain-v1\0{manifest_digest}", 32
        ),
        "items": items,
    }


def _safe_execution_slices(value: Any) -> dict[str, Any]:
    empty = _empty_execution_slices()
    if not isinstance(value, dict) or safe_int(value.get("schema")) != EXECUTION_SLICE_SCHEMA:
        return empty
    plan_digest = _coordination_fp32(value.get("plan_digest"))
    manifest_digest = _coordination_fp32(value.get("manifest_digest"))
    global_digest = _coordination_fp32(value.get("global_constraints_digest"))
    raw_items = value.get("items")
    count = safe_int(value.get("count"))
    current_index = safe_int(value.get("current_index"))
    completed_chain = _coordination_fp32(value.get("completed_chain"))
    if (
        not plan_digest
        or not manifest_digest
        or not global_digest
        or not isinstance(raw_items, list)
        or not 1 <= count == len(raw_items) <= MAX_EXECUTION_SLICES
        or not 1 <= current_index <= count + 1
        or not completed_chain
    ):
        return empty
    items: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_items, start=1):
        if not isinstance(raw, dict) or raw.get("id") != f"s{index:02d}":
            return empty
        slice_digest = _coordination_fp32(raw.get("slice_digest"))
        if not slice_digest:
            return empty
        status = raw.get("status") if raw.get("status") in {"pending", "passed"} else "pending"
        completion = _coordination_fp32(raw.get("completion_digest"))
        review = _coordination_fp32(raw.get("review_digest"))
        operation = _coordination_fp32(raw.get("operation_digest"))
        change_evidence = bool(raw.get("change_evidence"))
        verification_evidence = bool(raw.get("verification_evidence"))
        if status == "passed" and not (completion and review and operation and verification_evidence):
            return empty
        if index < current_index and status != "passed":
            return empty
        if index >= current_index and status == "passed":
            return empty
        items.append(
            {
                "id": raw["id"],
                "slice_digest": slice_digest,
                "status": status,
                "completion_digest": completion if status == "passed" else None,
                "review_digest": review if status == "passed" else None,
                "operation_digest": operation if status == "passed" else None,
                "change_evidence": change_evidence if status == "passed" else False,
                "verification_evidence": verification_evidence if status == "passed" else False,
            }
        )
    expected_chain = stable_hash(
        f"workflow-manager-slice-chain-v1\0{manifest_digest}", 32
    )
    for item in items:
        if item["status"] != "passed":
            break
        expected_chain = stable_hash(
            "workflow-manager-slice-chain-step-v1\0"
            + canonical_json(
                {
                    "completion_digest": item["completion_digest"],
                    "previous_chain": expected_chain,
                    "slice_digest": item["slice_digest"],
                    "slice_id": item["id"],
                }
            ),
            32,
        )
    if expected_chain != completed_chain:
        return empty
    return {
        "schema": EXECUTION_SLICE_SCHEMA,
        "plan_digest": plan_digest,
        "manifest_digest": manifest_digest,
        "global_constraints_digest": global_digest,
        "count": count,
        "current_index": current_index,
        "completed_chain": completed_chain,
        "items": items,
    }


def persisted_execution_slices(parsed: dict[str, Any], plan_digest: str) -> dict[str, Any]:
    """Project a trusted parsed manifest to bounded digest-only persisted state."""
    return {
        "schema": EXECUTION_SLICE_SCHEMA,
        "plan_digest": plan_digest,
        "manifest_digest": parsed["manifest_digest"],
        "global_constraints_digest": parsed["global_constraints_digest"],
        "count": parsed["count"],
        "current_index": 1,
        "completed_chain": parsed["completed_chain"],
        "items": [
            {
                "id": item["id"],
                "slice_digest": item["slice_digest"],
                "status": "pending",
                "completion_digest": None,
                "review_digest": None,
                "operation_digest": None,
                "change_evidence": False,
                "verification_evidence": False,
            }
            for item in parsed["items"]
        ],
    }


def current_execution_slice(state: dict[str, Any]) -> dict[str, Any] | None:
    slices = _safe_execution_slices(state.get("execution_slices"))
    index = safe_int(slices.get("current_index"))
    items = slices.get("items") if isinstance(slices.get("items"), list) else []
    return items[index - 1] if 1 <= index <= len(items) else None


def slice_contract_id(state: dict[str, Any]) -> str | None:
    contract = _coordination_fp32(state.get("execution_contract_id"))
    slices = _safe_execution_slices(state.get("execution_slices"))
    item = current_execution_slice(state)
    if not contract or not item:
        return None
    return stable_hash(
        "workflow-manager-slice-contract-v1\0"
        + canonical_json(
            {
                "completed_chain": slices["completed_chain"],
                "execution_contract_id": contract,
                "index": slices["current_index"],
                "manifest_digest": slices["manifest_digest"],
                "slice_digest": item["slice_digest"],
                "slice_id": item["id"],
            }
        ),
        32,
    )


def slice_task_token(state: dict[str, Any]) -> str | None:
    slice_contract = slice_contract_id(state)
    return stable_hash(f"workflow-manager-slice-task-v1\0{slice_contract}", 16) if slice_contract else None


def recompute_completed_slice_chain(slices: dict[str, Any]) -> str | None:
    safe = _safe_execution_slices(slices)
    manifest = _coordination_fp32(safe.get("manifest_digest"))
    if not manifest:
        return None
    chain = stable_hash(f"workflow-manager-slice-chain-v1\0{manifest}", 32)
    for item in safe.get("items", []):
        if item.get("status") != "passed" or not item.get("completion_digest"):
            break
        chain = stable_hash(
            "workflow-manager-slice-chain-step-v1\0"
            + canonical_json(
                {
                    "completion_digest": item["completion_digest"],
                    "previous_chain": chain,
                    "slice_digest": item["slice_digest"],
                    "slice_id": item["id"],
                }
            ),
            32,
        )
    return chain


COORDINATION_ENVELOPE_START = "WORKFLOW_COORDINATION_V1"
COORDINATION_ENVELOPE_END = "END_WORKFLOW_COORDINATION"
COORDINATION_RESOURCE_STAGES = {
    "build_account": {"build", "compile", "package"},
    "adb_device": {"adb", "deploy", "device_verify", "flash", "install", "reboot"},
}
COORDINATION_TRANSITIONS = {"blocked", "released"}
COORDINATION_NOTICE_STATES = {"pending", "sent", "failed", "exhausted", "unconfirmed"}
COORDINATION_THREAD_STATUSES = {"active", "idle", "notLoaded", "completed", "missing", "unknown"}
COORDINATION_ENVELOPE_FIELDS = (
    "source_task_fingerprint",
    "source_host_fingerprint",
    "target_task_fingerprint",
    "target_host_fingerprint",
    "sender_resource_identity",
    "target_resource_identity",
    "resource_kind",
    "sender_stage",
    "target_stage",
    "lease_generation",
    "transition",
)
CODEX_DELEGATION_MAX_BYTES = 1024 * 1024


def codex_delegation_input(value: str) -> str | None:
    """Extract the official create-thread wrapper without trusting mixed XML."""
    if not value.startswith("<codex_delegation>"):
        return None
    if len(value.encode("utf-8", errors="replace")) > CODEX_DELEGATION_MAX_BYTES:
        return None
    try:
        root = ET.fromstring(value)
    except ET.ParseError:
        return None
    children = list(root)
    if (
        root.tag != "codex_delegation"
        or root.attrib
        or len(children) != 2
        or [child.tag for child in children] != ["source_thread_id", "input"]
        or any(child.attrib or list(child) for child in children)
        or (root.text or "").strip()
        or any((child.tail or "").strip() for child in children)
    ):
        return None
    source_thread_id = str(children[0].text or "").strip()
    delegated = str(children[1].text or "")
    if not re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        source_thread_id,
    ):
        return None
    return delegated if delegated.strip() else None


def coordination_host_fingerprint(host_id: Any) -> str:
    return stable_hash(f"workflow-coordination-host-v1\0{str(host_id or '')}", 32)


def coordination_task_fingerprint_for_host(thread_id: Any, host_fingerprint: Any) -> str:
    return stable_hash(
        f"workflow-coordination-task-v1\0{str(host_fingerprint or '')}\0{str(thread_id or '')}",
        32,
    )


def coordination_task_fingerprint(thread_id: Any, host_id: Any) -> str:
    return coordination_task_fingerprint_for_host(
        thread_id, coordination_host_fingerprint(host_id)
    )


def is_list_threads_tool(payload: dict[str, Any]) -> bool:
    return str(payload.get("tool_name") or "") in {"list_threads", "codex_app__list_threads"}


def is_send_message_to_thread_tool(payload: dict[str, Any]) -> bool:
    return str(payload.get("tool_name") or "") in {
        "send_message_to_thread",
        "codex_app__send_message_to_thread",
    }


def coordination_send_fields(payload: dict[str, Any]) -> tuple[dict[str, str] | None, str | None]:
    value = payload.get("tool_input")
    if not isinstance(value, dict):
        return None, "send_message_to_thread requires one structured tool_input object"
    aliases = {
        "thread_id": ("threadId",),
        "host_id": ("hostId",),
        "message": ("prompt", "message"),
    }
    result: dict[str, str] = {}
    for field, names in aliases.items():
        raw_values = [value.get(name) for name in names if value.get(name) not in (None, "")]
        if any(not isinstance(raw, str) for raw in raw_values):
            return None, f"send_message_to_thread {field} must be a string"
        observed = [raw for raw in raw_values if isinstance(raw, str)]
        if len(set(observed)) > 1:
            return None, f"send_message_to_thread has conflicting {field} aliases"
        if not observed:
            return None, f"send_message_to_thread lacks {field}"
        if len(observed[0].encode("utf-8", errors="replace")) > COORDINATION_ID_MAX_BYTES:
            return None, f"send_message_to_thread {field} exceeds the bounded input limit"
        result[field] = observed[0]
    return result, None


def coordination_control_text(payload: dict[str, Any]) -> str | None:
    value = payload.get("tool_input")
    if not isinstance(value, dict):
        return None
    messages = [value.get(key) for key in ("prompt", "message")]
    return next(
        (
            message
            for message in messages
            if isinstance(message, str)
            and (
                message.startswith(COORDINATION_ENVELOPE_START)
                or message.startswith("<codex_delegation>")
            )
        ),
        None,
    )


def parse_coordination_envelope(text: Any) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(text, str) or not text.startswith(COORDINATION_ENVELOPE_START):
        return None, "missing WORKFLOW_COORDINATION_V1 marker"
    if len(text.encode("utf-8", errors="replace")) > 8192:
        return None, "coordination envelope exceeds the bounded byte limit"
    lines = text.splitlines()
    if len(lines) != len(COORDINATION_ENVELOPE_FIELDS) + 2:
        return None, "coordination envelope has extra, missing, or mixed content"
    if lines[0] != COORDINATION_ENVELOPE_START or lines[-1] != COORDINATION_ENVELOPE_END:
        return None, "coordination envelope markers must be exact and complete"
    result: dict[str, Any] = {}
    for line, expected in zip(lines[1:-1], COORDINATION_ENVELOPE_FIELDS):
        if "=" not in line:
            return None, f"coordination envelope lacks {expected}"
        key, value = line.split("=", 1)
        if key != expected or not value or value != value.strip():
            return None, f"coordination envelope field order/value is invalid at {expected}"
        result[key] = value
    for key in (
        "source_task_fingerprint",
        "source_host_fingerprint",
        "target_task_fingerprint",
        "target_host_fingerprint",
        "sender_resource_identity",
        "target_resource_identity",
    ):
        if not re.fullmatch(r"[0-9a-f]{32}", str(result.get(key) or "")):
            return None, f"coordination envelope {key} must be 32hex"
    kind = str(result.get("resource_kind") or "")
    if kind not in COORDINATION_RESOURCE_STAGES:
        return None, "coordination resource_kind must be build_account or adb_device"
    if result.get("transition") not in COORDINATION_TRANSITIONS:
        return None, "coordination transition must be blocked or released"
    generation = str(result.get("lease_generation") or "")
    if not re.fullmatch(r"[1-9]\d{0,8}", generation):
        return None, "coordination lease_generation must be positive"
    result["lease_generation"] = int(generation)
    return result, None


def coordination_conflict_class(envelope: dict[str, Any]) -> str | None:
    kind = str(envelope.get("resource_kind") or "")
    allowed = COORDINATION_RESOURCE_STAGES.get(kind, set())
    sender = str(envelope.get("sender_stage") or "")
    target = str(envelope.get("target_stage") or "")
    if sender not in allowed or target not in allowed:
        return None
    stages = sorted((sender, target))
    return stable_hash(f"workflow-coordination-conflict-v1\0{kind}\0{stages[0]}\0{stages[1]}", 32)


def coordination_notice_identity(envelope: dict[str, Any]) -> dict[str, str]:
    peers = sorted(
        (
            f"{envelope['source_host_fingerprint']}:{envelope['source_task_fingerprint']}",
            f"{envelope['target_host_fingerprint']}:{envelope['target_task_fingerprint']}",
        )
    )
    peer_pair = stable_hash(f"workflow-coordination-peers-v1\0{peers[0]}\0{peers[1]}", 32)
    conflict = coordination_conflict_class(envelope) or ""
    owner = stable_hash(
        f"workflow-coordination-owner-v1\0{envelope['source_host_fingerprint']}\0{envelope['source_task_fingerprint']}",
        32,
    )
    scope = stable_hash(
        "\0".join(
            (
                "workflow-coordination-scope-v1",
                peer_pair,
                str(envelope.get("sender_resource_identity") or ""),
                str(envelope.get("resource_kind") or ""),
                conflict,
            )
        ),
        32,
    )
    phase = stable_hash(
        "\0".join(
            (
                "workflow-coordination-phase-v1",
                owner,
                str(envelope.get("sender_stage") or ""),
                str(envelope.get("target_stage") or ""),
            )
        ),
        32,
    )
    notice = stable_hash(
        "\0".join(
            (
                "workflow-coordination-notice-v1",
                peer_pair,
                str(envelope.get("sender_resource_identity") or ""),
                conflict,
                str(envelope.get("lease_generation") or ""),
                str(envelope.get("transition") or ""),
            )
        ),
        32,
    )
    return {
        "peer_pair_fingerprint": peer_pair,
        "conflict_class_fingerprint": conflict,
        "notice_fingerprint": notice,
        "owner_fingerprint": owner,
        "scope_fingerprint": scope,
        "phase_fingerprint": phase,
    }


def coordination_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def coordination_activity_from_response(response: Any) -> list[dict[str, Any]]:
    if not isinstance(response, dict) or response_status(response) == "error":
        return []
    direct = "schemaVersion" in response or "threads" in response
    structured = "structuredContent" in response
    if direct == structured:
        return []
    leaf = response if direct else response.get("structuredContent")
    if not isinstance(leaf, dict) or set(("schemaVersion", "threads")) - set(leaf):
        return []
    schema = leaf.get("schemaVersion")
    if isinstance(schema, bool) or not re.fullmatch(r"[1-9]\d{0,2}", str(schema or "")):
        return []
    threads = leaf.get("threads")
    if not isinstance(threads, list) or len(threads) > MAX_COORDINATION_ACTIVITY:
        return []
    observed_at = coordination_now()
    result: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    total_bytes = 0
    for item in threads:
        if not isinstance(item, dict):
            return []
        ids = [
            str(item.get(key)).strip()
            for key in ("id", "threadId")
            if item.get(key) not in (None, "")
        ]
        if not ids or len(set(ids)) > 1:
            return []
        thread_id = ids[0]
        host_id = item.get("hostId")
        if not isinstance(thread_id, str) or not isinstance(host_id, str) or not thread_id or not host_id:
            return []
        total_bytes += len(thread_id.encode("utf-8", errors="replace")) + len(
            host_id.encode("utf-8", errors="replace")
        )
        if (
            max(
                len(thread_id.encode("utf-8", errors="replace")),
                len(host_id.encode("utf-8", errors="replace")),
            )
            > COORDINATION_ID_MAX_BYTES
            or total_bytes > COORDINATION_ID_MAX_BYTES * MAX_COORDINATION_ACTIVITY
        ):
            return []
        raw_status = item.get("status")
        status = raw_status if raw_status in COORDINATION_THREAD_STATUSES else "missing" if raw_status is None else "unknown"
        task_fp = coordination_task_fingerprint(thread_id, host_id)
        host_fp = coordination_host_fingerprint(host_id)
        if task_fp in seen:
            if seen[task_fp] != status:
                return []
            continue
        seen[task_fp] = status
        result.append(
            {
                "task_fingerprint": task_fp,
                "host_fingerprint": host_fp,
                "status": status,
                "snapshot_fingerprint": stable_hash(
                    f"workflow-coordination-snapshot-v1\0{task_fp}\0{host_fp}\0{status}\0{observed_at}",
                    32,
                ),
                "observed_at": observed_at,
            }
        )
    return result


def coordination_snapshot_fresh(item: dict[str, Any]) -> bool:
    try:
        observed = datetime.fromisoformat(str(item.get("observed_at") or "")).timestamp()
        age = time.time() - observed
        return -5 <= age <= COORDINATION_SNAPSHOT_TTL_SECONDS
    except (TypeError, ValueError):
        return False


def execution_contract_id(state: dict[str, Any]) -> str | None:
    """Bind one executor to the exact confirmed objective and plan without storing plan text."""
    objective = safe_fingerprint(state.get("objective", {}).get("fingerprint"))
    difficulty = safe_fingerprint(state.get("difficulty_decision_id"))
    plan = safe_fingerprint(state.get("plan_digest"))
    generation = max(safe_int(state.get("plan_generation")), 0)
    artifact = _safe_plan_artifact(state.get("plan_artifact"))
    canonical_path = str(artifact.get("relative_path") or "")
    revision_digest = safe_fingerprint(artifact.get("current_revision_digest"))
    journal_digest = safe_fingerprint(artifact.get("journal_digest"))
    slices = _safe_execution_slices(state.get("execution_slices"))
    manifest_digest = safe_fingerprint(slices.get("manifest_digest"))
    if not (
        objective
        and difficulty
        and plan
        and generation > 0
        and artifact.get("format_version") == 2
        and artifact.get("write_status") == "written"
        and re.fullmatch(
            r"plans/[A-Za-z0-9._-]+-[0-9a-f]{16}/hard-plan\.md",
            canonical_path,
        )
        and revision_digest == plan
        and journal_digest
        and artifact.get("generation") == generation
        and slices.get("plan_digest") == plan
        and manifest_digest
        and safe_int(slices.get("count")) > 0
    ):
        return None
    preference = safe_session_execution_preference(
        state.get("session_execution_preference")
    )
    profile_version = safe_label(
        state.get("execution_profile_version") or EXECUTION_PROFILE_VERSION,
        16,
    )
    material = (
        f"{profile_version}\0{preference}\0{objective}\0{difficulty}"
        f"\0{generation}\0{plan}\0{canonical_path}\0{revision_digest}\0{journal_digest}"
        f"\0{manifest_digest}\0{slices.get('global_constraints_digest')}\0{slices.get('count')}"
    )
    if preference == "highest_throughout":
        material += (
            f"\0{highest_execution_model(state) or 'unresolved'}"
            f"\0{highest_execution_effort(state) or 'unresolved'}"
        )
    review = state.get("causal_review") if isinstance(state.get("causal_review"), dict) else {}
    review_id = safe_fingerprint(review.get("review_id"))
    if review.get("state") == "resolved" and review_id:
        material += f"\0{review_id}"
    reference = _safe_reference_acceptance(state.get("reference_acceptance"))
    if reference["enabled"] and reference["contract_digest"]:
        material += f"\0{reference['contract_digest']}"
    return stable_hash(material, 32)


def assessor_binding_id(state: dict[str, Any]) -> str | None:
    objective = safe_fingerprint(state.get("objective", {}).get("fingerprint"))
    generation = max(safe_int(state.get("assessor_generation")), 0)
    return stable_hash(f"assessor-v1\0{objective}\0{generation}", 32) if objective and generation else None


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


def _empty_executor_review() -> dict[str, Any]:
    return {
        "status": "none",
        "execution_contract_id": None,
        "slice_id": None,
        "slice_contract_id": None,
        "attempt": 0,
        "candidate_result_fingerprint": None,
        "candidate_agent_fingerprint": None,
        "candidate_evidence_digest": None,
        "review_evidence_digest": None,
        "digest_profile": None,
        "digest_source": None,
        "terminal_status": None,
        "terminal_status_source": None,
        "at": None,
    }


def _safe_executor_review(item: Any) -> dict[str, Any]:
    result = _empty_executor_review()
    if not isinstance(item, dict):
        return result
    def fp32(value: Any) -> str | None:
        candidate = str(value or "")
        return candidate if re.fullmatch(r"[0-9a-f]{32}", candidate) else None

    status_value = item.get("status")
    result.update(
        {
            "status": (
                status_value
                if status_value in EXECUTOR_REVIEW_STATUSES
                else "none"
            ),
            "execution_contract_id": (
                fp32(item.get("execution_contract_id"))
            ),
            "slice_id": (
                safe_label(item.get("slice_id"), 3)
                if re.fullmatch(r"s(?:0[1-9]|[12][0-9]|3[0-2])", str(item.get("slice_id") or ""))
                else None
            ),
            "slice_contract_id": fp32(item.get("slice_contract_id")),
            "attempt": min(
                max(safe_int(item.get("attempt")), 0), MAX_EXECUTOR_ATTEMPTS
            ),
            "candidate_result_fingerprint": (
                fp32(item.get("candidate_result_fingerprint"))
            ),
            "candidate_agent_fingerprint": (
                fp32(item.get("candidate_agent_fingerprint"))
            ),
            "candidate_evidence_digest": (
                fp32(item.get("candidate_evidence_digest"))
            ),
            "review_evidence_digest": (
                fp32(item.get("review_evidence_digest"))
            ),
            "digest_profile": (
                safe_label(item.get("digest_profile"), 64)
                if item.get("digest_profile")
                else None
            ),
            "digest_source": (
                safe_label(item.get("digest_source"), 32)
                if item.get("digest_source")
                else None
            ),
            "terminal_status": (
                item.get("terminal_status")
                if item.get("terminal_status") in {"missing", "completed"}
                else None
            ),
            "terminal_status_source": (
                item.get("terminal_status_source")
                if item.get("terminal_status_source")
                in {"host_missing", "host_declared_success"}
                else None
            ),
            "at": str(item.get("at") or "")[:40] or None,
        }
    )
    if result["status"] != "none" and (
        not result["execution_contract_id"]
        or result["attempt"] <= 0
    ):
        return _empty_executor_review()
    if result["status"] in {"review_required", "recovery_started"} and (
        not result["candidate_result_fingerprint"]
        or not result["candidate_agent_fingerprint"]
        or not result["candidate_evidence_digest"]
    ):
        return _empty_executor_review()
    if result["digest_profile"] == EVIDENCE_DIGEST_PROFILE and (
        result["digest_source"] != EVIDENCE_DIGEST_SOURCE
        or not result["slice_id"]
        or not result["slice_contract_id"]
        or result["terminal_status"] not in {"missing", "completed"}
        or result["terminal_status_source"]
        not in {"host_missing", "host_declared_success"}
    ):
        return _empty_executor_review()
    return result


def _legacy_candidate_executor_review(
    value: dict[str, Any], contract_id: str, attempt: int,
    baseline: dict[str, Any],
) -> dict[str, Any]:
    """Recover the bounded review boundary that Schema 22 did not persist."""
    result_fingerprint: str | None = None
    candidate_agent = (
        safe_label(value.get("executor_agent_id"), 120)
        if value.get("executor_agent_id")
        else None
    )
    candidate_evidence: str | None = None
    for item in reversed(as_list(value.get("subagents"))):
        if not isinstance(item, dict) or item.get("event") != "stop":
            continue
        if item.get("role") != "confirmed_executor":
            continue
        if safe_fingerprint(item.get("contract_id")) != contract_id:
            continue
        if safe_int(item.get("attempt")) not in {0, attempt}:
            continue
        result_meta = item.get("result_meta")
        if item.get("agent_id"):
            candidate_agent = safe_label(item.get("agent_id"), 120)
        if isinstance(result_meta, dict):
            raw_result_fingerprint = (
                safe_fingerprint(result_meta.get("fingerprint")) or None
            )
            if raw_result_fingerprint:
                result_fingerprint = (
                    raw_result_fingerprint
                    if len(raw_result_fingerprint) == 32
                    else stable_hash(raw_result_fingerprint, 32)
                )
        candidate_evidence = (
            safe_fingerprint(item.get("execution_result_evidence_digest")) or None
        )
        if result_fingerprint or candidate_evidence:
            break
    # Schema 22's sanitizer could already have dropped the result-marker fields
    # after a later denied tool call. Preserve only bounded facts from its
    # existing baseline; never recover or persist executor prose.
    result_fingerprint = result_fingerprint or stable_hash(
        f"schema22-candidate\0{contract_id}\0{baseline.get('baseline_id') or ''}",
        32,
    )
    candidate_evidence = candidate_evidence or safe_fingerprint(
        baseline.get("verification_digest")
    )
    if candidate_evidence and len(candidate_evidence) != 32:
        candidate_evidence = stable_hash(candidate_evidence, 32)
    candidate_evidence = candidate_evidence or stable_hash(
        f"schema22-candidate-evidence\0{contract_id}\0{baseline.get('baseline_id') or ''}",
        32,
    )
    return _safe_executor_review(
        {
            "status": "review_required",
            "execution_contract_id": contract_id,
            "attempt": max(attempt, 1),
            "candidate_result_fingerprint": result_fingerprint,
            "candidate_agent_fingerprint": (
                stable_hash(candidate_agent, 32) if candidate_agent else None
            ),
            "candidate_evidence_digest": candidate_evidence,
            "review_evidence_digest": None,
            "at": utc_now(),
        }
    )


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
    state["executor_observed_effective"] = False
    state["executor_observed_model"] = None
    state["executor_observed_reasoning_effort"] = None
    state["executor_review"] = _empty_executor_review()
    state["stall"] = _safe_stall(None)


def safe_session_execution_preference(value: Any) -> str:
    return str(value) if value in SESSION_EXECUTION_PREFERENCES else "default"


def requested_assessor_reasoning_effort(state: dict[str, Any]) -> str:
    """Request the default max planning effort; preserve an explicit session-highest override."""
    return (
        HIGHEST_SESSION_REASONING_EFFORT
        if safe_session_execution_preference(
            state.get("session_execution_preference")
        )
        == "highest_throughout"
        else DEFAULT_PLAN_REASONING_EFFORT
    )


def highest_execution_model(state: dict[str, Any]) -> str | None:
    """Resolve the bound same-tier request without claiming host availability."""
    candidates = (
        state.get("assessor_observed_model")
        if state.get("assessor_observed_effective")
        else None,
        state.get("assessor_model"),
        state.get("model"),
    )
    for candidate in candidates:
        model = safe_label(candidate, 80) if candidate else ""
        if model and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{1,79}", model):
            return model
    return None


def highest_execution_effort(state: dict[str, Any]) -> str | None:
    effort = str(state.get("assessor_reasoning_effort") or "").lower()
    return effort if effort in {"high", "xhigh", "max", "ultra"} else None


def confirmed_executor_model_profile(state: dict[str, Any]) -> str:
    if safe_route(state.get("last_route")).get("delegation_opt_out"):
        return "current"
    if safe_session_execution_preference(
        state.get("session_execution_preference")
    ) == "highest_throughout":
        return "work_executor_highest_available"
    return "work_executor_low_latest"


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
    if safe_route(state.get("last_route")).get("delegation_opt_out"):
        state["executor_state"] = "local_running"
    state["model_profile"] = confirmed_executor_model_profile(state)
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


def plan_artifact_session_id(value: Any) -> str:
    """Keep private plan paths unlinkable to readable host session labels."""
    raw = str(value or "")
    return f"session-{stable_hash('plan-artifact-session' + chr(0) + raw)}"


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


def _coordination_fp32(value: Any) -> str | None:
    text = str(value or "")
    return text if re.fullmatch(r"[0-9a-f]{32}", text) else None


def _safe_coordination_activity(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    task = _coordination_fp32(item.get("task_fingerprint"))
    host = _coordination_fp32(item.get("host_fingerprint"))
    snapshot = _coordination_fp32(item.get("snapshot_fingerprint"))
    if not task or not host or not snapshot:
        return None
    return {
        "task_fingerprint": task,
        "host_fingerprint": host,
        "status": item.get("status") if item.get("status") in COORDINATION_THREAD_STATUSES else "unknown",
        "snapshot_fingerprint": snapshot,
        "observed_at": str(item.get("observed_at") or "")[:40],
    }


def _safe_coordination_notice(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    fingerprints = {
        key: _coordination_fp32(item.get(key))
        for key in (
            "notice_fingerprint",
            "peer_pair_fingerprint",
            "resource_identity",
            "conflict_class_fingerprint",
            "owner_fingerprint",
            "scope_fingerprint",
            "phase_fingerprint",
            "request_fingerprint",
        )
    }
    if not all(fingerprints.values()):
        return None
    return {
        **fingerprints,
        "resource_kind": item.get("resource_kind") if item.get("resource_kind") in COORDINATION_RESOURCE_STAGES else None,
        "lease_generation": min(max(safe_int(item.get("lease_generation")), 1), 999_999_999),
        "transition": item.get("transition") if item.get("transition") in COORDINATION_TRANSITIONS else None,
        "state": item.get("state") if item.get("state") in COORDINATION_NOTICE_STATES else "failed",
        "attempt": min(max(safe_int(item.get("attempt")), 1), 2),
        "at": str(item.get("at") or "")[:40],
    }


def _safe_coordination_inbound(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    fingerprints = {
        key: _coordination_fp32(item.get(key))
        for key in (
            "notice_fingerprint",
            "peer_pair_fingerprint",
            "resource_identity",
            "conflict_class_fingerprint",
            "owner_fingerprint",
            "scope_fingerprint",
            "phase_fingerprint",
        )
    }
    if not all(fingerprints.values()):
        return None
    return {
        **fingerprints,
        "resource_kind": item.get("resource_kind") if item.get("resource_kind") in COORDINATION_RESOURCE_STAGES else None,
        "lease_generation": min(max(safe_int(item.get("lease_generation")), 1), 999_999_999),
        "transition": item.get("transition") if item.get("transition") in COORDINATION_TRANSITIONS else None,
        "received_at": str(item.get("received_at") or "")[:40],
    }


def _safe_stall(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        item = {}
    state_value = item.get("state") if item.get("state") in STALL_STATES else "none"
    result = {
        "state": state_value,
        "stall_id": _coordination_fp32(item.get("stall_id")),
        "objective_fingerprint": safe_fingerprint(item.get("objective_fingerprint")) or None,
        "plan_digest": _coordination_fp32(item.get("plan_digest")),
        "execution_contract_id": _coordination_fp32(item.get("execution_contract_id")),
        "evidence_digest": _coordination_fp32(item.get("evidence_digest")),
        "diagnosis_request_fingerprint": _coordination_fp32(item.get("diagnosis_request_fingerprint")),
        "remediation_digest": _coordination_fp32(item.get("remediation_digest")),
        "correction_digest": _coordination_fp32(item.get("correction_digest")),
        "failure_kind": item.get("failure_kind") if item.get("failure_kind") in EXECUTOR_FAILURE_KINDS else None,
        "resume_profile": item.get("resume_profile") if item.get("resume_profile") in STALL_RESUME_PROFILES else None,
        "executor_attempt": min(max(safe_int(item.get("executor_attempt")), 0), MAX_EXECUTOR_ATTEMPTS),
        "diagnosis_attempt": min(max(safe_int(item.get("diagnosis_attempt")), 0), MAX_STALL_DIAGNOSIS_ATTEMPTS),
        "at": str(item.get("at") or "")[:40] or None,
    }
    required = (
        result["stall_id"],
        result["objective_fingerprint"],
        result["plan_digest"],
        result["execution_contract_id"],
        result["failure_kind"],
        result["resume_profile"],
    )
    if state_value != "none" and not all(required):
        result["state"] = "exhausted"
    return result


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
        "session_execution_preference": "default",
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
        "assessor_generation": 0,
        "assessor_binding_id": None,
        "assessor_failure_kind": None,
        "assessor_observed_effective": False,
        "assessor_observed_model": None,
        "assessor_observed_reasoning_effort": None,
        "assessor_start_observed": "absent",
        "assessor_observation_source": None,
        "assessor_fork_turns": None,
        "assessor_attempt": 0,
        "plan_state": "none",
        "plan_generation": 0,
        "plan_digest": None,
        "plan_objective_fingerprint": None,
        "plan_difficulty_decision_id": None,
        "confirmed_plan_digest": None,
        "confirmed_at": None,
        "plan_artifact": empty_plan_artifact(),
        "execution_slices": _empty_execution_slices(),
        "execution_profile_version": EXECUTION_PROFILE_VERSION,
        "executor_state": "none",
        "execution_contract_id": None,
        "executor_agent_id": None,
        "executor_attempt": 0,
        "executor_failure_kind": None,
        "executor_model": None,
        "executor_reasoning_effort": None,
        "executor_fork_turns": None,
        "executor_observed_effective": False,
        "executor_observed_model": None,
        "executor_observed_reasoning_effort": None,
        "executor_start_observed": "absent",
        "executor_observation_source": None,
        "executor_review": _empty_executor_review(),
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
        "stall": _safe_stall(None),
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
        "coordination_activity": [],
        "coordination_notices": [],
        "coordination_inbound": [],
        # A bounded, fingerprint-only freshness boundary.  Requests and host
        # Start echoes deliberately remain separate: a request is never proof
        # that the host applied it.
        "change_epoch": 0,
        "change_epoch_ledger": [],
        "identity_evidence": {
            "requested_profile": None,
            "start_echo_profile": None,
            "plugin_root_fingerprint": current_plugin_root_fingerprint(),
        },
    }


def current_plugin_root_fingerprint() -> str | None:
    raw = str(os.environ.get("PLUGIN_ROOT") or "").strip()
    return stable_hash(os.path.normpath(raw), 32) if raw else None


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
        "host_event_turn_id": safe_label(item.get("host_event_turn_id"), 120) if item.get("host_event_turn_id") else None,
        "host_input_digest": safe_fingerprint(item.get("host_input_digest")) or None,
        "legacy_host_input_digest": safe_fingerprint(item.get("legacy_host_input_digest")) or None,
        "reconciliation_source": item.get("reconciliation_source") if item.get("reconciliation_source") in {"legacy_unique_turn_patch_event_v1", "host_rollout_exact_patch_digest_v1"} else None,
        "tool": safe_label(item.get("tool"), 120),
        "fingerprint": fingerprint[:64],
        "status": status_value,
        "category": safe_label(item.get("category"), 32) if item.get("category") else "other",
        "plan_digest": plan_digest,
        "execution_contract_id": contract_id,
        "slice_id": (
            safe_label(item.get("slice_id"), 3)
            if re.fullmatch(r"s(?:0[1-9]|[12][0-9]|3[0-2])", str(item.get("slice_id") or ""))
            else None
        ),
        "slice_contract_id": _coordination_fp32(item.get("slice_contract_id")),
        "assessor_binding_id": safe_fingerprint(item.get("assessor_binding_id")) or None,
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
        "change_epoch": min(max(safe_int(item.get("change_epoch")), 0), MAX_EVENT_COUNT),
    }


def host_patch_digest(value: str) -> str:
    """One literal-only patch identity for host input and rollout repair."""
    return stable_hash("host-operation-patch-v1\0" + canonical_json(value.replace("\r\n", "\n").replace("\r", "\n")), 32)


def host_operation_input_digest(payload: dict[str, Any], command: str) -> str | None:
    """Stable input identity used only for later same-turn host reconciliation."""
    tool = normalized_key(payload.get("tool_name"))
    if tool in {"applypatch", "edit", "write"}:
        raw_input = payload.get("tool_input")
        if tool == "applypatch" and isinstance(raw_input, dict) and isinstance(raw_input.get("patch"), str):
            return host_patch_digest(raw_input["patch"])
        try:
            value = canonical_json(raw_input)
        except (TypeError, ValueError):
            return None
        return stable_hash("host-operation-patch-v1\0" + value[:65_536], 32) if len(value) <= 65_536 else None
    if not command:
        return None
    cwd = str(effective_tool_cwd(payload) or "")
    return stable_hash("host-operation-command-v1\0" + command.replace("\r\n", "\n").replace("\r", "\n") + "\0" + cwd, 32)


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
        if item.get("role") in {"lane", "confirmed_executor", "high_assessor"}
        else "lane"
    )
    fork_turns = str(item.get("fork_turns") or "")
    if fork_turns != "none" and not re.fullmatch(r"[1-9]\d*", fork_turns):
        fork_turns = ""
    value = {
        "at": item.get("at"),
        "event": item.get("event") if item.get("event") in {"request", "start", "stop"} else "unknown",
        "turn_id": safe_label(item.get("turn_id"), 120) if item.get("turn_id") else None,
        "agent_id": safe_label(item.get("agent_id"), 120) if item.get("agent_id") else None,
        "agent_type": safe_label(item.get("agent_type"), 80) if item.get("agent_type") else None,
        "task_name": safe_label(item.get("task_name"), 120) if item.get("task_name") else None,
        "status": safe_label(item.get("status"), 32).lower() if item.get("status") else None,
        "requested": bool(item.get("requested")),
        "host_accepted": (
            bool(item.get("host_accepted")) if item.get("host_accepted") is not None else None
        ),
        "host_acceptance_status": safe_label(item.get("host_acceptance_status"), 32) if item.get("host_acceptance_status") else None,
        "host_acceptance_source": safe_label(item.get("host_acceptance_source"), 32) if item.get("host_acceptance_source") else None,
        "start_observed": item.get("start_observed") if item.get("start_observed") in {"full", "partial", "absent", "mismatch"} else None,
        "observation_source": safe_label(item.get("observation_source"), 80) if item.get("observation_source") else None,
        "scope_fingerprint": safe_label(item.get("scope_fingerprint"), 64) if item.get("scope_fingerprint") else None,
        "request_fingerprint": request_fingerprint or None,
        "objective_fingerprint": objective_fingerprint or None,
        "stale": bool(item.get("stale")),
        "request_gate": item.get("request_gate")
        if item.get("request_gate") in {"audit", "open"}
        else None,
        "request_visibility": item.get("request_visibility")
        if item.get("request_visibility") in {"plaintext", "opaque_v2"}
        else None,
        "request_cap": min(max(safe_int(item.get("request_cap")), 0), 3),
        "reaudited": bool(item.get("reaudited")),
        "role": role,
        "contract_id": contract_id,
        "slice_id": (
            safe_label(item.get("slice_id"), 3)
            if re.fullmatch(r"s(?:0[1-9]|[12][0-9]|3[0-2])", str(item.get("slice_id") or ""))
            else None
        ),
        "slice_contract_id": _coordination_fp32(item.get("slice_contract_id")),
        "model": safe_label(item.get("model"), 80) if item.get("model") else None,
        "reasoning_effort": (
            safe_label(item.get("reasoning_effort"), 24)
            if item.get("reasoning_effort")
            else None
        ),
        "fork_turns": fork_turns or None,
        "attempt": min(max(safe_int(item.get("attempt")), 0), MAX_EXECUTOR_ATTEMPTS),
        "recovery_from": (
            item.get("recovery_from")
            if item.get("recovery_from") in EXECUTOR_FAILURE_KINDS
            else None
        ),
        "plan_handoff_digest": safe_fingerprint(item.get("plan_handoff_digest")) or None,
        "execution_result_contract_match": (
            bool(item.get("execution_result_contract_match"))
            if item.get("execution_result_contract_match") is not None
            else None
        ),
        "execution_result_outcome": (
            item.get("execution_result_outcome")
            if item.get("execution_result_outcome") in {"succeeded", "failed"}
            else None
        ),
        "execution_result_evidence_digest": (
            safe_fingerprint(item.get("execution_result_evidence_digest")) or None
        ),
        "evidence_digest_profile": (
            safe_label(item.get("evidence_digest_profile"), 64)
            if item.get("evidence_digest_profile")
            else None
        ),
        "evidence_digest_source": (
            safe_label(item.get("evidence_digest_source"), 32)
            if item.get("evidence_digest_source")
            else None
        ),
        "terminal_status": (
            item.get("terminal_status")
            if item.get("terminal_status") in {"missing", "completed"}
            else None
        ),
        "terminal_status_source": (
            item.get("terminal_status_source")
            if item.get("terminal_status_source")
            in {"host_missing", "host_declared_success"}
            else None
        ),
    }
    if isinstance(result_meta, dict):
        value["result_meta"] = {
            "fingerprint": safe_label(result_meta.get("fingerprint"), 64),
            "length": max(safe_int(result_meta.get("length")), 0),
        }
    return value


def subagent_lifecycle_groups(value: Any) -> list[dict[str, Any]]:
    records = value.get("subagents", []) if isinstance(value, dict) else as_list(value)
    groups: list[dict[str, Any]] = []
    live_by_agent: dict[str, dict[str, Any]] = {}
    terminal_by_agent: dict[str, dict[str, Any]] = {}

    def new_group(index: int, item: dict[str, Any], state_value: str) -> dict[str, Any]:
        group = {
            "state": state_value,
            "agent_id": str(item.get("agent_id") or "") or None,
            "request": item if item.get("event") == "request" else None,
            "start": item if item.get("event") == "start" else None,
            "stop": item if item.get("event") == "stop" else None,
            "records": [(index, item)],
            "first_index": index,
            "last_index": index,
        }
        groups.append(group)
        return group

    for index, item in enumerate(records):
        if not isinstance(item, dict):
            continue
        event = item.get("event")
        if event == "request":
            if not safe_fingerprint(item.get("request_fingerprint")):
                continue
            group = new_group(index, item, "result_pending" if item.get("agent_id") else "pending")
            if group["state"] == "result_pending":
                group["agent_id"] = str(item.get("agent_id"))
            continue
        agent_id = str(item.get("agent_id") or "")
        if not agent_id:
            continue
        if event == "start":
            if agent_id in live_by_agent:
                continue
            request_fingerprint = safe_fingerprint(item.get("request_fingerprint"))
            request_group = next(
                (
                    group
                    for group in groups
                    if group.get("state") == "pending"
                    and request_fingerprint
                    and safe_fingerprint((group.get("request") or {}).get("request_fingerprint"))
                    == request_fingerprint
                ),
                None,
            )
            prior_terminal = terminal_by_agent.get(agent_id)
            if prior_terminal and (
                not request_group
                or safe_int(request_group.get("first_index")) <= safe_int(prior_terminal.get("last_index"))
            ):
                continue
            group = request_group or new_group(index, item, "live")
            if request_group:
                group["records"].append((index, item))
                group["start"] = item
                group["last_index"] = index
                group["state"] = "live"
            group["agent_id"] = agent_id
            live_by_agent[agent_id] = group
            continue
        if event == "stop":
            group = live_by_agent.pop(agent_id, None)
            if group is None:
                group = next(
                    (
                        candidate
                        for candidate in reversed(groups)
                        if candidate.get("state") == "result_pending"
                        and candidate.get("agent_id") == agent_id
                    ),
                    None,
                )
            if group is None:
                if agent_id in terminal_by_agent:
                    continue
                group = new_group(index, item, "terminal")
            else:
                group["records"].append((index, item))
                group["stop"] = item
                group["last_index"] = index
                group["state"] = "terminal"
            group["agent_id"] = agent_id
            terminal_by_agent[agent_id] = group
    return groups


def subagent_lifecycle_is_bound(state: dict[str, Any], group: dict[str, Any]) -> bool:
    records = [item for _, item in group.get("records", [])]
    role = next((item.get("role") for item in reversed(records) if item.get("role")), "lane")
    contract_id = next((item.get("contract_id") for item in reversed(records) if item.get("contract_id")), None)
    agent_id = group.get("agent_id")
    return bool(
        role == "high_assessor"
        and (
            (agent_id and agent_id == state.get("assessor_agent_id"))
            or (contract_id and contract_id == state.get("assessor_binding_id"))
        )
    ) or bool(
        role == "confirmed_executor"
        and (
            (agent_id and agent_id == state.get("executor_agent_id"))
            or (contract_id and contract_id == state.get("execution_contract_id"))
        )
    )


def retained_subagent_records(state: dict[str, Any], records: Any = None) -> list[dict[str, Any]]:
    groups = subagent_lifecycle_groups(state if records is None else records)
    protected = [
        group
        for group in groups
        if group.get("state") in {"pending", "result_pending", "live"} or subagent_lifecycle_is_bound(state, group)
    ]
    terminal = [group for group in groups if group.get("state") == "terminal" and group not in protected]
    kept_ids = {id(group) for group in protected + terminal[-MAX_TERMINAL_SUBAGENT_LIFECYCLES:]}
    kept = [pair for group in groups if id(group) in kept_ids for pair in group.get("records", [])]
    return [item for _, item in sorted(kept, key=lambda pair: pair[0])]


def protected_subagent_lifecycle_count(state: dict[str, Any]) -> int:
    return sum(
        group.get("state") in {"pending", "result_pending", "live"} or subagent_lifecycle_is_bound(state, group)
        for group in subagent_lifecycle_groups(state)
    )


def append_result_pending_subagent(
    state: dict[str, Any], *, agent_id: Any, request_fingerprint: Any
) -> bool:
    agent = safe_label(agent_id, 120) if agent_id else ""
    request_fp = safe_fingerprint(request_fingerprint)
    if not agent or not request_fp:
        return False
    if any(
        group.get("state") == "result_pending" and group.get("agent_id") == agent
        for group in subagent_lifecycle_groups(state)
    ):
        return False
    state.setdefault("subagents", []).append(
        {
            "at": utc_now(),
            "event": "request",
            "agent_id": agent,
            "task_name": "high_assessor_followup",
            "status": "pending",
            "request_fingerprint": request_fp,
            "objective_fingerprint": state.get("objective", {}).get("fingerprint"),
            "role": "high_assessor",
            "contract_id": state.get("assessor_binding_id"),
            "attempt": state.get("assessor_attempt"),
        }
    )
    return True


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


def empty_plan_artifact() -> dict[str, Any]:
    return {
        "relative_path": None,
        "format_version": 0,
        "objective_fingerprint": None,
        "difficulty_decision_id": None,
        "plan_digest": None,
        "content_digest": None,
        "current_revision_digest": None,
        "journal_digest": None,
        "generation": 0,
        "revision_count": 0,
        "lifecycle_status": "none",
        "write_status": "none",
        "warning_code": "none",
        "created_at": None,
        "updated_at": None,
    }


def _safe_plan_artifact(item: Any) -> dict[str, Any]:
    result = empty_plan_artifact()
    if not isinstance(item, dict):
        return result
    relative = str(item.get("relative_path") or "")
    canonical_match = re.fullmatch(
        r"plans/[A-Za-z0-9._-]+-[0-9a-f]{16}/hard-plan\.md", relative
    )
    legacy_match = re.fullmatch(
        r"plans/[A-Za-z0-9._-]+-[0-9a-f]{16}/hard-plan-g[0-9]{4,}-[0-9a-f]{32}\.md",
        relative,
    )
    plan_digest = safe_fingerprint(item.get("plan_digest")) or None
    content_digest = safe_fingerprint(item.get("content_digest")) or None
    current_revision_digest = (
        safe_fingerprint(item.get("current_revision_digest"))
        or (content_digest if canonical_match else None)
        or (plan_digest if canonical_match else None)
    )
    result.update(
        {
            "relative_path": relative if canonical_match or legacy_match else None,
            "format_version": 2 if canonical_match else 1 if legacy_match else 0,
            "objective_fingerprint": safe_fingerprint(item.get("objective_fingerprint")) or None,
            "difficulty_decision_id": safe_fingerprint(item.get("difficulty_decision_id")) or None,
            "plan_digest": plan_digest,
            "content_digest": content_digest,
            "current_revision_digest": current_revision_digest,
            "journal_digest": safe_fingerprint(item.get("journal_digest")) or None,
            "generation": max(safe_int(item.get("generation")), 0),
            "revision_count": max(safe_int(item.get("revision_count")), 0),
            "lifecycle_status": item.get("lifecycle_status") if item.get("lifecycle_status") in PLAN_ARTIFACT_LIFECYCLE_STATUSES else "none",
            "write_status": item.get("write_status") if item.get("write_status") in PLAN_ARTIFACT_WRITE_STATUSES else "none",
            "warning_code": item.get("warning_code") if item.get("warning_code") in PLAN_ARTIFACT_WARNING_CODES else "none",
            "created_at": str(item.get("created_at"))[:40] if item.get("created_at") else None,
            "updated_at": str(item.get("updated_at"))[:40] if item.get("updated_at") else None,
        }
    )
    if legacy_match and result["plan_digest"] not in result["relative_path"]:
        result["relative_path"] = None
        result["format_version"] = 0
    if result["format_version"] == 2:
        if result["plan_digest"] != result["current_revision_digest"]:
            result["current_revision_digest"] = None
        if result["revision_count"] <= 0:
            result["revision_count"] = 0
    return result


def _plan_artifact_lifecycle(state: dict[str, Any], artifact_digest: str | None) -> str:
    if not artifact_digest:
        return "none"
    if state.get("plan_digest") != artifact_digest or state.get("plan_state") in {"none", "analyzing", "invalidated"}:
        return "invalidated"
    if state.get("executor_state") == "succeeded":
        return "succeeded"
    if state.get("plan_state") == "confirmed":
        if state.get("executor_state") in {
            "spawn_pending",
            "running",
            "local_running",
            "verification_required",
            "recovery_required",
            "exhausted",
        }:
            return "executing"
        return "confirmed"
    return "ready"


def _legacy_plan_artifact(state: dict[str, Any]) -> dict[str, Any]:
    artifact = empty_plan_artifact()
    plan_digest = safe_fingerprint(state.get("plan_digest")) or None
    if not plan_digest:
        return artifact
    artifact.update(
        {
            "objective_fingerprint": safe_fingerprint(state.get("plan_objective_fingerprint")) or None,
            "difficulty_decision_id": safe_fingerprint(state.get("plan_difficulty_decision_id")) or None,
            "plan_digest": plan_digest,
            "generation": max(safe_int(state.get("plan_generation")), 0),
            "format_version": 1,
            "lifecycle_status": _plan_artifact_lifecycle(state, plan_digest),
            "write_status": "legacy_unavailable",
            "warning_code": "legacy_unavailable",
            "updated_at": utc_now(),
        }
    )
    return artifact


def sync_plan_artifact_lifecycle(state: dict[str, Any]) -> None:
    artifact = _safe_plan_artifact(state.get("plan_artifact"))
    lifecycle = _plan_artifact_lifecycle(state, artifact.get("plan_digest"))
    if artifact["lifecycle_status"] != lifecycle:
        artifact["lifecycle_status"] = lifecycle
        artifact["updated_at"] = utc_now()
    state["plan_artifact"] = artifact

def sanitize_plan_artifact_body(value: Any) -> str:
    source = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    bidi_controls = {0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A)}
    characters: list[str] = []
    for character in source:
        codepoint = ord(character)
        if codepoint in bidi_controls:
            continue
        if character not in {"\n", "\t"} and (codepoint < 32 or codepoint == 127):
            continue
        characters.append(character)
    text = "".join(characters)
    protocol = re.compile(
        r"^\s*(?:WORK_ASSESSMENT|SIMPLE_EXECUTION|LOCAL_EXECUTION|EXECUTION_STALL|"
        r"STALL_DIAGNOSIS|CAUSAL_REVIEW|WORKFLOW_COORDINATION_V1|END_WORKFLOW_COORDINATION)\b",
        re.I,
    )
    lines = [
        line
        for line in text.splitlines()
        if not protocol.match(line)
        and not re.fullmatch(
            r"\s*计划已就绪，等待确认后执行[。.!！\s]*",
            line,
        )
    ]
    body = redact_text("\n".join(lines))
    body = body.replace("<redacted-token>", "[REDACTED]").replace(
        "<redacted>", "[REDACTED]"
    )
    body = "\n".join(line.rstrip() for line in body.splitlines()).strip()
    body = body.rstrip() + "\n"
    if len(body.encode("utf-8")) > MAX_PLAN_REVISION_BYTES:
        raise PlanArtifactError("revision_too_large")
    return body


def _plan_artifact_body(document: str) -> str | None:
    marker = PLAN_ARTIFACT_BODY_MARKER + "\n"
    return document.split(marker, 1)[1] if marker in document else None


def plan_artifact_body_digest(document: str) -> str | None:
    body = _plan_artifact_body(str(document or ""))
    return stable_hash(body, 32) if body is not None else None


PLAN_JOURNAL_PREAMBLE = (
    "# Workflow Manager Hard Plan\n\n"
    "> Canonical private plan journal. Trusted Hook revisions define plan content; "
    "the journal alone never grants execution authority.\n\n"
)
PLAN_JOURNAL_HEADER_RE = re.compile(
    rb"\A<!-- workflow-manager-plan-journal:v2\n"
    rb"session_token: (session-[0-9a-f]{16})\n"
    rb"created_at: ([^\r\n]{1,40})\n"
    rb"-->\n"
)
PLAN_REVISION_HEADER_RE = re.compile(
    rb"<!-- workflow-manager-plan-revision:v2\n"
    rb"generation: ([1-9][0-9]*)\n"
    rb"revision_digest: ([0-9a-f]{32})\n"
    rb"objective_fingerprint: ([0-9a-f]{16,64}|none)\n"
    rb"difficulty_decision_id: ([0-9a-f]{16,64}|none)\n"
    rb"created_at: ([^\r\n]{1,40})\n"
    rb"revision_bytes: ([0-9]+)\n"
    rb"-->\n"
)


def parse_plan_journal(
    document: bytes, *, expected_session: str | None = None
) -> dict[str, Any]:
    if len(document) > MAX_PLAN_JOURNAL_BYTES:
        raise PlanArtifactError("journal_full")
    header = PLAN_JOURNAL_HEADER_RE.match(document)
    if header is None:
        raise PlanArtifactError("content_drift")
    session = header.group(1).decode("ascii")
    if expected_session is not None and session != expected_session:
        raise PlanArtifactError("content_drift")
    position = header.end()
    preamble = PLAN_JOURNAL_PREAMBLE.encode("utf-8")
    if document[position : position + len(preamble)] != preamble:
        raise PlanArtifactError("content_drift")
    position += len(preamble)
    revisions: list[dict[str, Any]] = []
    previous_generation = 0
    while position < len(document):
        revision_header = PLAN_REVISION_HEADER_RE.match(document, position)
        if revision_header is None:
            raise PlanArtifactError("content_drift")
        generation = safe_int(revision_header.group(1).decode("ascii"))
        if generation <= previous_generation:
            raise PlanArtifactError("content_drift")
        revision_digest = revision_header.group(2).decode("ascii")
        objective = revision_header.group(3).decode("ascii")
        difficulty = revision_header.group(4).decode("ascii")
        revision_bytes = safe_int(revision_header.group(6).decode("ascii"))
        if revision_bytes <= 0 or revision_bytes > MAX_PLAN_REVISION_BYTES:
            raise PlanArtifactError("content_drift")
        heading = f"## Revision {generation}\n\n".encode("utf-8")
        body_start = revision_header.end()
        if document[body_start : body_start + len(heading)] != heading:
            raise PlanArtifactError("content_drift")
        body_start += len(heading)
        body_end = body_start + revision_bytes
        body_bytes = document[body_start:body_end]
        if len(body_bytes) != revision_bytes or not body_bytes.endswith(b"\n"):
            raise PlanArtifactError("content_drift")
        try:
            body = body_bytes.decode("utf-8")
        except UnicodeError as error:
            raise PlanArtifactError("content_drift") from error
        if stable_hash(body_bytes, 32) != revision_digest:
            raise PlanArtifactError("content_drift")
        revisions.append(
            {
                "generation": generation,
                "revision_digest": revision_digest,
                "objective_fingerprint": None if objective == "none" else objective,
                "difficulty_decision_id": None if difficulty == "none" else difficulty,
                "created_at": revision_header.group(5).decode("utf-8"),
                "body": body,
            }
        )
        previous_generation = generation
        position = body_end
    if not revisions:
        raise PlanArtifactError("content_drift")
    current = revisions[-1]
    return {
        "session_token": session,
        "created_at": header.group(2).decode("utf-8"),
        "revisions": revisions,
        "revision_count": len(revisions),
        "generation": current["generation"],
        "current_revision_digest": current["revision_digest"],
        "objective_fingerprint": current["objective_fingerprint"],
        "difficulty_decision_id": current["difficulty_decision_id"],
        "journal_digest": stable_hash(document, 32),
    }


def append_plan_journal_revision(
    existing: bytes | None,
    *,
    session: str,
    generation: int,
    body: str,
    objective_fingerprint: str | None,
    difficulty_decision_id: str | None,
    created_at: str,
) -> tuple[bytes, dict[str, Any]]:
    body_bytes = body.encode("utf-8")
    if len(body_bytes) > MAX_PLAN_REVISION_BYTES:
        raise PlanArtifactError("revision_too_large")
    if existing is None:
        prefix = (
            f"{PLAN_JOURNAL_OWNER}\n"
            f"session_token: {session}\n"
            f"created_at: {created_at}\n"
            "-->\n"
            f"{PLAN_JOURNAL_PREAMBLE}"
        ).encode("utf-8")
        prior_generation = 0
    else:
        parsed = parse_plan_journal(existing, expected_session=session)
        prior_generation = parsed["generation"]
        prefix = existing
    if generation <= prior_generation:
        raise PlanArtifactError("content_drift")
    revision_digest = stable_hash(body_bytes, 32)
    revision = (
        f"{PLAN_REVISION_OWNER}\n"
        f"generation: {generation}\n"
        f"revision_digest: {revision_digest}\n"
        f"objective_fingerprint: {objective_fingerprint or 'none'}\n"
        f"difficulty_decision_id: {difficulty_decision_id or 'none'}\n"
        f"created_at: {created_at}\n"
        f"revision_bytes: {len(body_bytes)}\n"
        "-->\n"
        f"## Revision {generation}\n\n"
    ).encode("utf-8") + body_bytes
    document = prefix + revision
    if len(document) > MAX_PLAN_JOURNAL_BYTES:
        raise PlanArtifactError("journal_full")
    parsed = parse_plan_journal(document, expected_session=session)
    if parsed["generation"] != generation or parsed["current_revision_digest"] != revision_digest:
        raise PlanArtifactError("content_drift")
    return document, parsed


class PlanArtifactError(OSError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code if code in PLAN_ARTIFACT_WARNING_CODES else "write_error"


class PlanTransactionPendingError(PlanArtifactError):
    def __init__(self, transaction: dict[str, Any]) -> None:
        super().__init__("write_error")
        self.transaction = transaction


def _canonical_plan_data_root(payload: dict[str, Any]) -> Path:
    configured = os.environ.get("PLUGIN_DATA") or os.environ.get("CLAUDE_PLUGIN_DATA")
    root = Path(configured) if configured else Path.home() / ".codex" / "workflow-manager"
    if not root.is_absolute() or Path(os.path.abspath(str(root))) != root:
        raise PlanArtifactError("unsafe_data_root")
    try:
        info = root.lstat()
    except FileNotFoundError:
        pass
    else:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise PlanArtifactError("unsafe_data_root")
    resolved = root.resolve(strict=False)
    cwd_value = payload.get("cwd")
    if cwd_value:
        cwd = Path(str(cwd_value))
        if cwd.is_absolute():
            try:
                resolved.relative_to(cwd.resolve(strict=False))
            except ValueError:
                pass
            else:
                raise PlanArtifactError("unsafe_data_root")
    return resolved


def _ensure_private_real_dir(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        path.mkdir(mode=0o700)
    else:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise PlanArtifactError("unsafe_path")
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _directory_identity(info: os.stat_result) -> tuple[int, int]:
    return (int(info.st_dev), int(info.st_ino))


def _require_directory_identity(info: os.stat_result, expected: tuple[int, int]) -> None:
    if not stat.S_ISDIR(info.st_mode) or _directory_identity(info) != expected:
        raise PlanArtifactError("unsafe_path")


def _windows_api_directory_path(path: Path) -> str:
    value = os.path.abspath(str(path))
    if value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


def _windows_open_directory_guard(path: Path) -> tuple[Any, tuple[int, int]]:
    import ctypes
    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = [wintypes.HANDLE, ctypes.POINTER(ByHandleFileInformation)]
    get_information.restype = wintypes.BOOL
    handle = create_file(
        _windows_api_directory_path(path),
        0x80000000,
        0x0001 | 0x0002,
        None,
        3,
        0x02000000 | 0x00200000,
        None,
    )
    if handle in (None, ctypes.c_void_p(-1).value):
        raise PlanArtifactError("unsafe_path")
    information = ByHandleFileInformation()
    if not get_information(handle, ctypes.byref(information)):
        _windows_close_handle(handle)
        raise PlanArtifactError("unsafe_path")
    attributes = int(information.dwFileAttributes)
    if not attributes & 0x0010 or attributes & 0x0400:
        _windows_close_handle(handle)
        raise PlanArtifactError("unsafe_path")
    identity = (
        int(information.dwVolumeSerialNumber),
        (int(information.nFileIndexHigh) << 32) | int(information.nFileIndexLow),
    )
    return handle, identity


def _windows_close_handle(handle: Any) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    if not close_handle(handle):
        raise PlanArtifactError("unsafe_path")


@contextmanager
def plan_session_directory_guard(
    root: Path, session: str, *, create: bool = True
) -> Iterator[dict[str, Any]]:
    if not re.fullmatch(r"session-[0-9a-f]{16}", session):
        raise PlanArtifactError("unsafe_path")
    if create:
        _ensure_private_real_dir(root)
    if os.name == "nt":
        handles: list[Any] = []
        guarded: list[tuple[Path, tuple[int, int]]] = []
        try:
            for path in (root, root / "plans", root / "plans" / session):
                if create:
                    _ensure_private_real_dir(path)
                handle, identity = _windows_open_directory_guard(path)
                handles.append(handle)
                guarded.append((path, identity))

            def verify() -> None:
                for path, expected in guarded:
                    handle, observed = _windows_open_directory_guard(path)
                    try:
                        if observed != expected:
                            raise PlanArtifactError("unsafe_path")
                    finally:
                        _windows_close_handle(handle)

            yield {"directory_fd": None, "verify": verify}
        finally:
            for handle in reversed(handles):
                _windows_close_handle(handle)
        return

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if not nofollow or not directory:
        raise PlanArtifactError("unsafe_path")
    flags = os.O_RDONLY | nofollow | directory
    descriptors: list[int] = []
    try:
        root_fd = os.open(str(root), flags)
        descriptors.append(root_fd)
        if create:
            os.fchmod(root_fd, 0o700)
        root_identity = _directory_identity(os.fstat(root_fd))
        for name in ("plans", session):
            parent_fd = descriptors[-1]
            if create:
                try:
                    os.mkdir(name, mode=0o700, dir_fd=parent_fd)
                except FileExistsError:
                    pass
            child_fd = os.open(name, flags, dir_fd=parent_fd)
            descriptors.append(child_fd)
            if create:
                os.fchmod(child_fd, 0o700)
        plans_fd, session_fd = descriptors[1], descriptors[2]
        plans_identity = _directory_identity(os.fstat(plans_fd))
        session_identity = _directory_identity(os.fstat(session_fd))

        def verify() -> None:
            _require_directory_identity(root.lstat(), root_identity)
            _require_directory_identity(
                os.stat("plans", dir_fd=root_fd, follow_symlinks=False), plans_identity
            )
            _require_directory_identity(
                os.stat(session, dir_fd=plans_fd, follow_symlinks=False), session_identity
            )

        verify()
        yield {"directory_fd": session_fd, "verify": verify}
    except OSError as error:
        if isinstance(error, PlanArtifactError):
            raise
        raise PlanArtifactError("unsafe_path") from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _fsync_plan_directory(directory_fd: int | None) -> None:
    if directory_fd is None:
        return
    try:
        os.fsync(directory_fd)
    except OSError as error:
        raise PlanArtifactError("write_error") from error


def _atomic_write_plan_file(
    path: Path,
    payload: bytes,
    *,
    expected_old_bytes: bytes | None,
    directory_fd: int | None = None,
    verify_binding: Callable[[], None] | None = None,
    prepared_backup_name: str | None = None,
) -> dict[str, Any]:
    if not PLAN_ARTIFACT_NAME_RE.fullmatch(path.name):
        raise PlanArtifactError("unsafe_path")
    transaction: dict[str, Any] = {
        "path": path,
        "directory_fd": directory_fd,
        "old_identity": None,
        "old_mode": None,
        "expected_old_bytes": expected_old_bytes,
        "expected_new_bytes": payload,
        "backup_name": None,
        "new_identity": None,
        "new_mode": None,
    }
    try:
        existing = _plan_lstat(path, directory_fd)
    except FileNotFoundError:
        pass
    else:
        if (
            stat.S_ISLNK(existing.st_mode)
            or not stat.S_ISREG(existing.st_mode)
            or existing.st_nlink != 1
        ):
            raise PlanArtifactError("unsafe_path")
        transaction["old_identity"] = _plan_file_identity(existing)
        transaction["old_mode"] = stat.S_IMODE(existing.st_mode)
    if expected_old_bytes is None:
        if transaction["old_identity"] is not None:
            raise PlanArtifactError("content_drift")
    else:
        if transaction["old_identity"] is None:
            raise PlanArtifactError("content_drift")
        observed_old = _read_exact_plan_bytes(
            path,
            transaction["old_identity"],
            directory_fd,
            max_bytes=MAX_PLAN_JOURNAL_BYTES,
        )
        if observed_old != expected_old_bytes:
            raise PlanArtifactError("content_drift")
    descriptor = -1
    temporary_name: str | None = None
    temporary_path: Path | None = None
    try:
        if directory_fd is not None:
            for _ in range(64):
                candidate = f".{path.name}.{secrets.token_hex(12)}.tmp"
                try:
                    descriptor = os.open(
                        candidate,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=directory_fd,
                    )
                except FileExistsError:
                    continue
                temporary_name = candidate
                break
        else:
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
            )
            temporary_path = Path(temporary)
            temporary_name = temporary_path.name
        if descriptor < 0 or temporary_name is None:
            raise PlanArtifactError("write_error")
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if verify_binding is not None:
            verify_binding()
        if transaction["old_identity"] is not None:
            backup_name = prepared_backup_name or _transaction_name(
                path, "backup", directory_fd
            )
            if not re.fullmatch(
                rf"\.{re.escape(path.name)}\.backup\.[0-9a-f]{{24}}",
                backup_name,
            ):
                raise PlanArtifactError("unsafe_path")
            try:
                _plan_lstat(path.parent / backup_name, directory_fd)
            except FileNotFoundError:
                pass
            else:
                raise PlanArtifactError("unsafe_path")
            current = _plan_lstat(path, directory_fd)
            if (
                stat.S_ISLNK(current.st_mode)
                or not stat.S_ISREG(current.st_mode)
                or current.st_nlink != 1
                or _plan_file_identity(current) != transaction["old_identity"]
            ):
                raise PlanArtifactError("unsafe_path")
            observed_old = _read_exact_plan_bytes(
                path,
                transaction["old_identity"],
                directory_fd,
                max_bytes=MAX_PLAN_JOURNAL_BYTES,
            )
            if observed_old != expected_old_bytes:
                raise PlanArtifactError("content_drift")
            _plan_rename(path, backup_name, directory_fd)
            transaction["backup_name"] = backup_name
            backup_info = _plan_lstat(path.parent / backup_name, directory_fd)
            if _plan_file_identity(backup_info) != transaction["old_identity"]:
                raise PlanArtifactError("unsafe_path")
            backup_bytes = _read_exact_plan_bytes(
                path.parent / backup_name,
                transaction["old_identity"],
                directory_fd,
                max_bytes=MAX_PLAN_JOURNAL_BYTES,
            )
            if backup_bytes != expected_old_bytes:
                raise PlanArtifactError("content_drift")
        else:
            try:
                _plan_lstat(path, directory_fd)
            except FileNotFoundError:
                pass
            else:
                raise PlanArtifactError("content_drift")
        if directory_fd is not None:
            os.replace(
                temporary_name,
                path.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
        else:
            if temporary_path is None:
                raise PlanArtifactError("write_error")
            os.replace(temporary_path, path)
        temporary_name = None
        temporary_path = None
        installed = _plan_lstat(path, directory_fd)
        transaction["new_identity"] = _plan_file_identity(installed)
        transaction["new_mode"] = stat.S_IMODE(installed.st_mode)
        if (
            stat.S_ISLNK(installed.st_mode)
            or not stat.S_ISREG(installed.st_mode)
            or installed.st_nlink != 1
        ):
            raise PlanArtifactError("unsafe_path")
        try:
            if directory_fd is not None:
                os.chmod(path.name, 0o600, dir_fd=directory_fd, follow_symlinks=False)
            else:
                path.chmod(0o600)
        except (NotImplementedError, OSError):
            pass
        try:
            _fsync_plan_directory(directory_fd)
        except PlanArtifactError as error:
            raise PlanTransactionPendingError(transaction) from error
        if verify_binding is not None:
            verify_binding()
        return transaction
    except PlanTransactionPendingError:
        raise
    except Exception as error:
        if transaction["backup_name"] is not None or transaction["new_identity"] is not None:
            try:
                _rollback_plan_write(transaction)
            except PlanArtifactError as rollback_error:
                raise rollback_error from error
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_name is not None:
            try:
                if directory_fd is not None:
                    os.unlink(temporary_name, dir_fd=directory_fd)
                elif temporary_path is not None:
                    temporary_path.unlink()
            except FileNotFoundError:
                pass


def _plan_file_identity(info: os.stat_result) -> tuple[int, int, int]:
    return (int(info.st_dev), int(info.st_ino), int(info.st_nlink))


def _plan_lstat(path: Path, directory_fd: int | None) -> os.stat_result:
    return (
        os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        if directory_fd is not None
        else path.lstat()
    )


def _plan_rename(path: Path, target_name: str, directory_fd: int | None) -> None:
    if directory_fd is not None:
        os.rename(
            path.name,
            target_name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
    else:
        path.rename(path.parent / target_name)


def _plan_rename_if_absent(
    path: Path, target_name: str, directory_fd: int | None
) -> None:
    if os.name == "nt":
        if directory_fd is not None:
            raise PlanArtifactError("unsafe_path")
        try:
            path.rename(path.parent / target_name)
        except FileExistsError as error:
            raise PlanArtifactError("unsafe_path") from error
        return

    import ctypes

    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as error:
        raise PlanArtifactError("unsafe_path") from error
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    at_fdcwd = -100
    source_fd = directory_fd if directory_fd is not None else at_fdcwd
    target_fd = directory_fd if directory_fd is not None else at_fdcwd
    source = os.fsencode(path.name if directory_fd is not None else path)
    target = os.fsencode(
        target_name if directory_fd is not None else path.parent / target_name
    )
    if renameat2(source_fd, source, target_fd, target, 1) == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise PlanArtifactError("unsafe_path")
    if error_number in {
        errno.EINVAL,
        errno.ENOSYS,
        errno.ENOTSUP,
        errno.EOPNOTSUPP,
    }:
        _plan_link_unlink_if_absent(path, target_name, directory_fd)
        return
    raise OSError(error_number, os.strerror(error_number))


def _plan_link_unlink_if_absent(
    path: Path, target_name: str, directory_fd: int | None
) -> None:
    """Publish a private temporary file without clobbering on renameat2-less mounts."""
    source_info = _plan_lstat(path, directory_fd)
    source_identity = _plan_file_identity(source_info)
    if (
        stat.S_ISLNK(source_info.st_mode)
        or not stat.S_ISREG(source_info.st_mode)
        or source_info.st_nlink != 1
    ):
        raise PlanArtifactError("unsafe_path")
    target = path.parent / target_name
    linked = False
    try:
        try:
            if directory_fd is not None:
                os.link(
                    path.name,
                    target_name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            else:
                os.link(path, target, follow_symlinks=False)
        except FileExistsError as error:
            raise PlanArtifactError("unsafe_path") from error
        linked = True
        source_after = _plan_lstat(path, directory_fd)
        target_after = _plan_lstat(target, directory_fd)
        if (
            stat.S_ISLNK(source_after.st_mode)
            or not stat.S_ISREG(source_after.st_mode)
            or stat.S_ISLNK(target_after.st_mode)
            or not stat.S_ISREG(target_after.st_mode)
            or _plan_file_identity(source_after) != _plan_file_identity(target_after)
            or source_identity[:2] != _plan_file_identity(source_after)[:2]
            or source_after.st_nlink != 2
            or target_after.st_nlink != 2
        ):
            raise PlanArtifactError("unsafe_path")
        if directory_fd is not None:
            os.unlink(path.name, dir_fd=directory_fd)
        else:
            path.unlink()
        linked = False
        installed = _plan_lstat(target, directory_fd)
        if (
            stat.S_ISLNK(installed.st_mode)
            or not stat.S_ISREG(installed.st_mode)
            or installed.st_nlink != 1
            or source_identity[:2] != _plan_file_identity(installed)[:2]
        ):
            raise PlanArtifactError("unsafe_path")
    except Exception:
        if linked:
            try:
                installed = _plan_lstat(target, directory_fd)
                if source_identity[:2] == _plan_file_identity(installed)[:2]:
                    if directory_fd is not None:
                        os.unlink(target_name, dir_fd=directory_fd)
                    else:
                        target.unlink()
            except (FileNotFoundError, OSError):
                pass
        raise


def _transaction_name(path: Path, purpose: str, directory_fd: int | None) -> str:
    for _ in range(64):
        name = f".{path.name}.{purpose}.{secrets.token_hex(12)}"
        try:
            _plan_lstat(path.parent / name, directory_fd)
        except FileNotFoundError:
            return name
    raise PlanArtifactError("write_error")


def _open_plan_delete_descriptor(path: Path, directory_fd: int | None) -> int:
    if os.name != "nt":
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        if directory_fd is not None:
            return os.open(path.name, flags, dir_fd=directory_fd)
        return os.open(path, flags)

    if directory_fd is not None:
        raise PlanArtifactError("unsafe_path")
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        _windows_api_directory_path(path),
        0x80000000,
        0x0001 | 0x0004,
        None,
        3,
        0x00200000,
        None,
    )
    if handle in (None, ctypes.c_void_p(-1).value):
        raise PlanArtifactError("unsafe_path")
    try:
        return msvcrt.open_osfhandle(
            int(handle), os.O_RDONLY | getattr(os, "O_BINARY", 0)
        )
    except Exception as error:
        _windows_close_handle(handle)
        raise PlanArtifactError("unsafe_path") from error


def _read_plan_delete_descriptor(descriptor: int, max_bytes: int) -> bytes:
    limit = max(max_bytes, 0)
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    observed_size = 0
    while observed_size <= limit:
        chunk = os.read(descriptor, min(64 * 1024, limit + 1 - observed_size))
        if not chunk:
            break
        chunks.append(chunk)
        observed_size += len(chunk)
    document = b"".join(chunks)
    if len(document) > limit:
        raise PlanArtifactError("content_drift")
    return document


def _unlink_plan_file_if_identity(
    path: Path,
    expected: tuple[int, int, int],
    directory_fd: int | None,
    *,
    expected_bytes: bytes | None = None,
    expected_mode: int | None = None,
) -> bool:
    try:
        observed = _plan_lstat(path, directory_fd)
    except FileNotFoundError:
        return True
    if (
        stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISREG(observed.st_mode)
        or observed.st_nlink != 1
        or _plan_file_identity(observed) != expected
    ):
        return False
    if expected_bytes is not None:
        descriptor = -1
        unlinked = False
        original_mode = stat.S_IMODE(observed.st_mode)
        restore_document: bytes | None = None
        try:
            descriptor = _open_plan_delete_descriptor(path, directory_fd)
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or _plan_file_identity(opened) != expected
                or (
                    expected_mode is not None
                    and original_mode != int(expected_mode)
                )
            ):
                return False
            if os.name != "nt":
                try:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    os.fchmod(descriptor, original_mode & ~0o222)
                except (AttributeError, NotImplementedError, OSError) as error:
                    raise PlanArtifactError("unsafe_path") from error
            after_guard = _plan_lstat(path, directory_fd)
            if (
                stat.S_ISLNK(after_guard.st_mode)
                or not stat.S_ISREG(after_guard.st_mode)
                or after_guard.st_nlink != 1
                or _plan_file_identity(after_guard) != expected
            ):
                return False
            before_unlink = _read_plan_delete_descriptor(
                descriptor, MAX_PLAN_JOURNAL_BYTES
            )
            if before_unlink != expected_bytes:
                raise PlanArtifactError("content_drift")
            if directory_fd is not None:
                os.unlink(path.name, dir_fd=directory_fd)
            else:
                path.unlink()
            unlinked = True
            after_unlink = _read_plan_delete_descriptor(
                descriptor, MAX_PLAN_JOURNAL_BYTES
            )
            if after_unlink != expected_bytes:
                restore_document = after_unlink
        finally:
            if descriptor >= 0:
                if not unlinked and os.name != "nt":
                    try:
                        os.fchmod(descriptor, original_mode)
                    except OSError:
                        pass
                os.close(descriptor)
        if restore_document is not None:
            snapshot = {"document": restore_document, "mode": original_mode}
            try:
                _install_retention_snapshot(path, snapshot, directory_fd)
            except PlanArtifactError:
                recovery = path.parent / _transaction_name(
                    path, "recovered", directory_fd
                )
                _install_retention_snapshot(recovery, snapshot, directory_fd)
            raise PlanArtifactError("content_drift")
        try:
            remaining = _plan_lstat(path, directory_fd)
        except FileNotFoundError:
            return True
        return _plan_file_identity(remaining) != expected
    if directory_fd is not None:
        os.unlink(path.name, dir_fd=directory_fd)
    else:
        path.unlink()
    try:
        remaining = _plan_lstat(path, directory_fd)
    except FileNotFoundError:
        return True
    return _plan_file_identity(remaining) != expected


def _rollback_plan_write(transaction: dict[str, Any]) -> None:
    path = transaction["path"]
    directory_fd = transaction.get("directory_fd")
    new_identity = transaction.get("new_identity")
    if new_identity and not _unlink_plan_file_if_identity(
        path,
        new_identity,
        directory_fd,
        expected_bytes=transaction.get("expected_new_bytes"),
        expected_mode=transaction.get("new_mode"),
    ):
        raise PlanArtifactError("unsafe_path")
    backup_name = transaction.get("backup_name")
    old_identity = transaction.get("old_identity")
    if not backup_name:
        return
    backup = path.parent / backup_name
    try:
        target = _plan_lstat(path, directory_fd)
    except FileNotFoundError:
        pass
    else:
        if not old_identity or _plan_file_identity(target) != old_identity:
            raise PlanArtifactError("unsafe_path")
        transaction["backup_name"] = None
        return
    backup_info = _plan_lstat(backup, directory_fd)
    if (
        not old_identity
        or stat.S_ISLNK(backup_info.st_mode)
        or not stat.S_ISREG(backup_info.st_mode)
        or backup_info.st_nlink != 1
        or _plan_file_identity(backup_info) != old_identity
    ):
        raise PlanArtifactError("unsafe_path")
    _plan_rename(backup, path.name, directory_fd)
    restored = _plan_lstat(path, directory_fd)
    if _plan_file_identity(restored) != old_identity:
        raise PlanArtifactError("unsafe_path")
    transaction["backup_name"] = None


def _commit_plan_write(
    transaction: dict[str, Any], verify_binding: Callable[[], None] | None
) -> None:
    backup_name = transaction.get("backup_name")
    if not backup_name:
        return
    if verify_binding is not None:
        verify_binding()
    backup = transaction["path"].parent / backup_name
    old_identity = transaction.get("old_identity")
    expected_old_bytes = transaction.get("expected_old_bytes")
    if not old_identity or not isinstance(expected_old_bytes, bytes):
        raise PlanArtifactError("unsafe_path")
    backup_bytes = _read_exact_plan_bytes(
        backup,
        old_identity,
        transaction.get("directory_fd"),
        max_bytes=MAX_PLAN_JOURNAL_BYTES,
    )
    if backup_bytes != expected_old_bytes:
        raise PlanArtifactError("content_drift")
    if not _unlink_plan_file_if_identity(
        backup,
        old_identity,
        transaction.get("directory_fd"),
        expected_bytes=expected_old_bytes,
        expected_mode=transaction.get("old_mode"),
    ):
        raise PlanArtifactError("unsafe_path")
    transaction["backup_name"] = None


def _owned_plan_artifact_record(
    path: Path, *, directory_fd: int | None = None
) -> tuple[int, str, tuple[int, int, int]] | None:
    descriptor = -1
    try:
        match = LEGACY_PLAN_ARTIFACT_NAME_RE.fullmatch(path.name)
        if not match:
            return None
        if directory_fd is not None:
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
            info = os.fstat(descriptor)
            header_bytes = os.read(descriptor, 4097)
        else:
            info = path.lstat()
            with path.open("rb") as stream:
                header_bytes = stream.read(4097)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return None
        if len(header_bytes) > 4096:
            header_bytes = header_bytes[:4096]
        header = header_bytes.decode("utf-8")
    except (OSError, UnicodeError):
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not header.startswith(PLAN_ARTIFACT_OWNER + "\n"):
        return None
    generation = re.search(r"(?m)^generation: ([0-9]+)$", header)
    digest = re.search(r"(?m)^plan_digest: ([0-9a-f]{32})$", header)
    if not generation or not digest:
        return None
    parsed = (safe_int(generation.group(1)), digest.group(1))
    if parsed != (safe_int(match.group(1)), match.group(2)):
        return None
    return (parsed[0], parsed[1], _plan_file_identity(info))


def _owned_plan_artifact(
    path: Path, *, directory_fd: int | None = None
) -> tuple[int, str] | None:
    record = _owned_plan_artifact_record(path, directory_fd=directory_fd)
    return (record[0], record[1]) if record is not None else None


def _read_exact_plan_bytes(
    path: Path,
    expected: tuple[int, int, int],
    directory_fd: int | None,
    *,
    max_bytes: int = MAX_PLAN_REVISION_BYTES + 16 * 1024,
) -> bytes:
    limit = max(max_bytes, 0)
    descriptor = -1
    try:
        if directory_fd is not None:
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
            before = os.fstat(descriptor)
            if (
                stat.S_ISLNK(before.st_mode)
                or not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or _plan_file_identity(before) != expected
                or before.st_size > limit
            ):
                raise PlanArtifactError("unsafe_path")
            chunks: list[bytes] = []
            observed_size = 0
            while observed_size <= limit:
                chunk = os.read(descriptor, min(64 * 1024, limit + 1 - observed_size))
                if not chunk:
                    break
                chunks.append(chunk)
                observed_size += len(chunk)
            document = b"".join(chunks)
            after = os.fstat(descriptor)
            after_path = _plan_lstat(path, directory_fd)
            if (
                stat.S_ISLNK(after_path.st_mode)
                or not stat.S_ISREG(after_path.st_mode)
                or after_path.st_nlink != 1
                or _plan_file_identity(after) != expected
                or _plan_file_identity(after_path) != expected
            ):
                raise PlanArtifactError("unsafe_path")
        else:
            before = path.lstat()
            if (
                stat.S_ISLNK(before.st_mode)
                or not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or _plan_file_identity(before) != expected
                or before.st_size > limit
            ):
                raise PlanArtifactError("unsafe_path")
            with path.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                if _plan_file_identity(opened) != expected:
                    raise PlanArtifactError("unsafe_path")
                document = stream.read(limit + 1)
                after_handle = os.fstat(stream.fileno())
                after_path = path.lstat()
            if (
                stat.S_ISLNK(after_path.st_mode)
                or not stat.S_ISREG(after_path.st_mode)
                or after_path.st_nlink != 1
                or _plan_file_identity(after_handle) != expected
                or _plan_file_identity(after_path) != expected
            ):
                raise PlanArtifactError("unsafe_path")
        if len(document) > limit:
            raise PlanArtifactError("unsafe_path")
        return document
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _retention_snapshot(
    path: Path,
    identity: tuple[int, int, int],
    directory_fd: int | None,
) -> dict[str, Any] | None:
    try:
        info = _plan_lstat(path, directory_fd)
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or _plan_file_identity(info) != identity
        ):
            return None
        document = _read_exact_plan_bytes(path, identity, directory_fd)
        after = _plan_lstat(path, directory_fd)
        if (
            stat.S_ISLNK(after.st_mode)
            or not stat.S_ISREG(after.st_mode)
            or after.st_nlink != 1
            or _plan_file_identity(after) != identity
            or stat.S_IMODE(after.st_mode) != stat.S_IMODE(info.st_mode)
        ):
            return None
    except (OSError, PlanArtifactError):
        return None
    return {
        "document": document,
        "identity": identity,
        "mode": stat.S_IMODE(info.st_mode),
    }


def _install_retention_snapshot(
    path: Path, snapshot: dict[str, Any], directory_fd: int | None
) -> None:
    descriptor = -1
    temporary_name: str | None = None
    temporary_path: Path | None = None
    try:
        if directory_fd is not None:
            for _ in range(64):
                candidate = f".{path.name}.restore.{secrets.token_hex(12)}"
                try:
                    descriptor = os.open(
                        candidate,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=directory_fd,
                    )
                except FileExistsError:
                    continue
                temporary_name = candidate
                break
        else:
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{path.name}.restore.", dir=str(path.parent)
            )
            temporary_path = Path(temporary)
            temporary_name = temporary_path.name
        if descriptor < 0 or temporary_name is None:
            raise PlanArtifactError("unsafe_path")
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(snapshot["document"])
            stream.flush()
            os.fsync(stream.fileno())
            try:
                os.fchmod(stream.fileno(), int(snapshot["mode"]))
            except (AttributeError, NotImplementedError, OSError):
                pass
        temporary = path.parent / temporary_name
        _plan_rename_if_absent(temporary, path.name, directory_fd)
        temporary_name = None
        temporary_path = None
    except PlanArtifactError:
        raise
    except OSError as error:
        raise PlanArtifactError("unsafe_path") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_name is not None:
            try:
                if directory_fd is not None:
                    os.unlink(temporary_name, dir_fd=directory_fd)
                elif temporary_path is not None:
                    temporary_path.unlink()
            except FileNotFoundError:
                pass


def _restore_retention_snapshot(
    path: Path, snapshot: dict[str, Any], directory_fd: int | None
) -> None:
    expected_document = snapshot["document"]
    installed = False
    try:
        existing = _plan_lstat(path, directory_fd)
    except FileNotFoundError:
        _install_retention_snapshot(path, snapshot, directory_fd)
        existing = _plan_lstat(path, directory_fd)
        installed = True
    if (
        stat.S_ISLNK(existing.st_mode)
        or not stat.S_ISREG(existing.st_mode)
        or existing.st_nlink != 1
        or (
            not installed
            and _plan_file_identity(existing) != snapshot["identity"]
        )
        or stat.S_IMODE(existing.st_mode) != int(snapshot["mode"])
    ):
        raise PlanArtifactError("unsafe_path")
    identity = _plan_file_identity(existing)
    observed = _read_exact_plan_bytes(path, identity, directory_fd)
    if observed != expected_document:
        raise PlanArtifactError("unsafe_path")


def _retain_plan_artifacts(
    directory: Path,
    current: Path,
    *,
    directory_fd: int | None = None,
    verify_binding: Callable[[], None] | None = None,
    keep_old: int = MAX_OLD_PLAN_ARTIFACTS,
) -> None:
    def restore(staged: list[dict[str, Any]]) -> None:
        try:
            for entry in reversed(staged):
                original = entry["original"]
                quarantine = entry["quarantine"]
                identity = entry["identity"]
                snapshot = entry["snapshot"]
                try:
                    existing = _plan_lstat(original, directory_fd)
                except FileNotFoundError:
                    try:
                        quarantined = _plan_lstat(quarantine, directory_fd)
                    except FileNotFoundError:
                        _restore_retention_snapshot(original, snapshot, directory_fd)
                    else:
                        if (
                            stat.S_ISLNK(quarantined.st_mode)
                            or not stat.S_ISREG(quarantined.st_mode)
                            or quarantined.st_nlink != 1
                            or _plan_file_identity(quarantined) != identity
                            or stat.S_IMODE(quarantined.st_mode) != snapshot["mode"]
                            or _read_exact_plan_bytes(
                                quarantine, identity, directory_fd
                            )
                            != snapshot["document"]
                        ):
                            raise PlanArtifactError("unsafe_path")
                        _plan_rename_if_absent(
                            quarantine, original.name, directory_fd
                        )
                        _restore_retention_snapshot(
                            original, snapshot, directory_fd
                        )
                else:
                    if (
                        stat.S_ISLNK(existing.st_mode)
                        or not stat.S_ISREG(existing.st_mode)
                        or existing.st_nlink != 1
                        or _plan_file_identity(existing) != identity
                    ):
                        raise PlanArtifactError("unsafe_path")
                    _restore_retention_snapshot(original, snapshot, directory_fd)
                try:
                    _plan_lstat(quarantine, directory_fd)
                except FileNotFoundError:
                    pass
                else:
                    raise PlanArtifactError("unsafe_path")
        except PlanArtifactError:
            raise
        except Exception as error:
            raise PlanArtifactError("unsafe_path") from error

    def retain_transaction(
        candidates: list[
            tuple[tuple[int, str, tuple[int, int, int]], Path]
        ],
    ) -> None:
        staged: list[dict[str, Any]] = []
        try:
            for record, candidate in candidates:
                if verify_binding is not None:
                    verify_binding()
                snapshot = _retention_snapshot(
                    candidate, record[2], directory_fd
                )
                if snapshot is None:
                    continue
                observed = _plan_lstat(candidate, directory_fd)
                if (
                    stat.S_ISLNK(observed.st_mode)
                    or not stat.S_ISREG(observed.st_mode)
                    or observed.st_nlink != 1
                    or _plan_file_identity(observed) != record[2]
                    or stat.S_IMODE(observed.st_mode) != snapshot["mode"]
                ):
                    raise PlanArtifactError("unsafe_path")
                quarantine_name = _transaction_name(
                    candidate, "quarantine", directory_fd
                )
                quarantine = directory / quarantine_name
                _plan_rename_if_absent(
                    candidate, quarantine_name, directory_fd
                )
                entry = {
                    "original": candidate,
                    "quarantine": quarantine,
                    "identity": record[2],
                    "snapshot": snapshot,
                    "unlinked": False,
                }
                staged.append(entry)
                quarantined = _plan_lstat(quarantine, directory_fd)
                if (
                    stat.S_ISLNK(quarantined.st_mode)
                    or not stat.S_ISREG(quarantined.st_mode)
                    or quarantined.st_nlink != 1
                    or _plan_file_identity(quarantined) != record[2]
                    or stat.S_IMODE(quarantined.st_mode) != snapshot["mode"]
                    or _read_exact_plan_bytes(
                        quarantine, record[2], directory_fd
                    )
                    != snapshot["document"]
                ):
                    raise PlanArtifactError("unsafe_path")
            if verify_binding is not None:
                verify_binding()
            for entry in staged:
                if verify_binding is not None:
                    verify_binding()
                quarantine = entry["quarantine"]
                identity = entry["identity"]
                snapshot = entry["snapshot"]
                observed = _plan_lstat(quarantine, directory_fd)
                if (
                    stat.S_ISLNK(observed.st_mode)
                    or not stat.S_ISREG(observed.st_mode)
                    or observed.st_nlink != 1
                    or _plan_file_identity(observed) != identity
                    or stat.S_IMODE(observed.st_mode) != snapshot["mode"]
                    or _read_exact_plan_bytes(
                        quarantine, identity, directory_fd
                    )
                    != snapshot["document"]
                ):
                    raise PlanArtifactError("unsafe_path")
                if not _unlink_plan_file_if_identity(
                    quarantine,
                    identity,
                    directory_fd,
                    expected_bytes=snapshot["document"],
                    expected_mode=snapshot["mode"],
                ):
                    raise PlanArtifactError("unsafe_path")
                entry["unlinked"] = True
                if verify_binding is not None:
                    verify_binding()
        except Exception as error:
            try:
                restore(staged)
            except Exception as restore_error:
                raise PlanArtifactError("unsafe_path") from restore_error
            raise PlanArtifactError("unsafe_path") from error

    if verify_binding is not None:
        verify_binding()
    try:
        names = os.listdir(directory_fd) if directory_fd is not None else [item.name for item in directory.iterdir()]
    except PlanArtifactError:
        raise
    except OSError:
        return
    owned = []
    for name in names:
        candidate = directory / name
        record = _owned_plan_artifact_record(candidate, directory_fd=directory_fd)
        if record is not None:
            owned.append((record, candidate))
    old = sorted(
        (item for item in owned if item[1].name != current.name),
        key=lambda item: (item[0][0], item[0][1], item[1].name),
        reverse=True,
    )
    stale = old[max(keep_old, 0):]
    for offset in range(0, len(stale), MAX_RETENTION_TRANSACTION_ITEMS):
        retain_transaction(
            stale[offset : offset + MAX_RETENTION_TRANSACTION_ITEMS]
        )



def _read_plan_artifact_document(
    path: Path, *, directory_fd: int | None = None
) -> bytes:
    try:
        info = _plan_lstat(path, directory_fd)
    except FileNotFoundError as error:
        raise PlanArtifactError("content_drift") from error
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
    ):
        raise PlanArtifactError("unsafe_path")
    try:
        return _read_exact_plan_bytes(
            path,
            _plan_file_identity(info),
            directory_fd,
            max_bytes=MAX_PLAN_JOURNAL_BYTES,
        )
    except PlanArtifactError as error:
        if error.code == "unsafe_path" and info.st_size > MAX_PLAN_JOURNAL_BYTES:
            raise PlanArtifactError("journal_full") from error
        raise


def _write_plan_transaction_marker(
    path: Path,
    marker: dict[str, Any],
    *,
    directory_fd: int | None,
    verify_binding: Callable[[], None],
) -> tuple[int, int, int]:
    encoded = (
        json.dumps(marker, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("ascii")
    if len(encoded) > MAX_PLAN_TRANSACTION_MARKER_BYTES:
        raise PlanArtifactError("write_error")
    try:
        _plan_lstat(path, directory_fd)
    except FileNotFoundError:
        pass
    else:
        raise PlanArtifactError("transaction_incomplete")
    descriptor = -1
    temporary_name: str | None = None
    temporary_path: Path | None = None
    try:
        for _ in range(64):
            candidate = f".{PLAN_JOURNAL_NAME}.transaction.{secrets.token_hex(12)}.tmp"
            try:
                if directory_fd is not None:
                    descriptor = os.open(
                        candidate,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=directory_fd,
                    )
                else:
                    temporary_path = path.parent / candidate
                    descriptor = os.open(
                        temporary_path,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                    )
                temporary_name = candidate
                break
            except FileExistsError:
                continue
        if descriptor < 0 or temporary_name is None:
            raise PlanArtifactError("write_error")
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        verify_binding()
        temporary = path.parent / temporary_name
        _plan_rename_if_absent(temporary, path.name, directory_fd)
        temporary_name = None
        temporary_path = None
        installed = _plan_lstat(path, directory_fd)
        if (
            stat.S_ISLNK(installed.st_mode)
            or not stat.S_ISREG(installed.st_mode)
            or installed.st_nlink != 1
        ):
            raise PlanArtifactError("unsafe_path")
        try:
            if directory_fd is not None:
                os.chmod(path.name, 0o600, dir_fd=directory_fd, follow_symlinks=False)
            else:
                path.chmod(0o600)
        except (NotImplementedError, OSError):
            pass
        _fsync_plan_directory(directory_fd)
        identity = _plan_file_identity(installed)
        observed = _read_exact_plan_bytes(
            path,
            identity,
            directory_fd,
            max_bytes=MAX_PLAN_TRANSACTION_MARKER_BYTES,
        )
        if observed != encoded:
            raise PlanArtifactError("unsafe_path")
        verify_binding()
        return identity
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_name is not None:
            try:
                if directory_fd is not None:
                    os.unlink(temporary_name, dir_fd=directory_fd)
                elif temporary_path is not None:
                    temporary_path.unlink()
            except FileNotFoundError:
                pass


def _read_plan_transaction_marker(
    path: Path, *, directory_fd: int | None
) -> tuple[dict[str, Any], tuple[int, int, int]]:
    info = _plan_lstat(path, directory_fd)
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_size > MAX_PLAN_TRANSACTION_MARKER_BYTES
    ):
        raise PlanArtifactError("transaction_incomplete")
    identity = _plan_file_identity(info)
    raw = _read_exact_plan_bytes(
        path,
        identity,
        directory_fd,
        max_bytes=MAX_PLAN_TRANSACTION_MARKER_BYTES,
    )
    try:
        marker = json.loads(raw.decode("ascii"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise PlanArtifactError("transaction_incomplete") from error
    required = {
        "schema",
        "transaction_id",
        "session_token",
        "journal_name",
        "backup_name",
        "cleanup_kind",
        "old_journal_digest",
        "new_journal_digest",
        "old_generation",
        "new_generation",
    }
    if not isinstance(marker, dict) or set(marker) != required:
        raise PlanArtifactError("transaction_incomplete")
    if (
        marker.get("schema") != 1
        or not re.fullmatch(r"[0-9a-f]{32}", str(marker.get("transaction_id") or ""))
        or not re.fullmatch(r"session-[0-9a-f]{16}", str(marker.get("session_token") or ""))
        or marker.get("journal_name") != PLAN_JOURNAL_NAME
        or marker.get("cleanup_kind") not in {"none", "legacy_v1"}
        or not re.fullmatch(r"[0-9a-f]{32}|none", str(marker.get("old_journal_digest") or ""))
        or not re.fullmatch(r"[0-9a-f]{32}", str(marker.get("new_journal_digest") or ""))
        or not isinstance(marker.get("old_generation"), int)
        or not isinstance(marker.get("new_generation"), int)
        or marker["old_generation"] < 0
        or marker["new_generation"] <= marker["old_generation"]
    ):
        raise PlanArtifactError("transaction_incomplete")
    backup_name = marker.get("backup_name")
    if backup_name != "none" and not re.fullmatch(
        rf"\.{re.escape(PLAN_JOURNAL_NAME)}\.backup\.[0-9a-f]{{24}}",
        str(backup_name or ""),
    ):
        raise PlanArtifactError("transaction_incomplete")
    return marker, identity


def _remove_verified_transaction_file(
    path: Path,
    identity: tuple[int, int, int],
    directory_fd: int | None,
    *,
    expected_bytes: bytes | None = None,
    expected_mode: int | None = None,
) -> None:
    if not _unlink_plan_file_if_identity(
        path,
        identity,
        directory_fd,
        expected_bytes=expected_bytes,
        expected_mode=expected_mode,
    ):
        raise PlanArtifactError("unsafe_path")


def _finish_plan_transaction(
    pending: dict[str, Any], *, commit: bool
) -> None:
    guard = pending["guard"]
    context = pending["guard_context"]
    transaction = pending["transaction"]
    marker_path = pending["marker_path"]
    marker_identity = pending["marker_identity"]
    try:
        guard["verify"]()
        if commit:
            _commit_plan_write(transaction, guard["verify"])
            cleanup = pending.get("legacy_cleanup")
            if isinstance(cleanup, dict):
                _retain_plan_artifacts(
                    cleanup["directory"],
                    cleanup["current"],
                    directory_fd=guard["directory_fd"],
                    verify_binding=guard["verify"],
                    keep_old=0,
                )
        else:
            _rollback_plan_write(transaction)
        guard["verify"]()
        _fsync_plan_directory(guard["directory_fd"])
        _remove_verified_transaction_file(
            marker_path, marker_identity, guard["directory_fd"]
        )
        _fsync_plan_directory(guard["directory_fd"])
        guard["verify"]()
    finally:
        context.__exit__(None, None, None)


def _leave_plan_transaction_for_recovery(pending: dict[str, Any]) -> None:
    """Release held handles without deleting recovery evidence."""
    pending["guard_context"].__exit__(None, None, None)


def _persisted_plan_transaction_side(
    path: Path, pending: dict[str, Any]
) -> str:
    """Classify the durable state side after an ambiguous state-write error."""
    marker = pending.get("marker")
    if not isinstance(marker, dict):
        return "unknown"
    try:
        info = path.lstat()
    except FileNotFoundError:
        digest = None
        generation = 0
    except OSError:
        return "unknown"
    else:
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size > MAX_STATE_BYTES
        ):
            return "unknown"
        try:
            with path.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                if _plan_file_identity(opened) != _plan_file_identity(info):
                    return "unknown"
                raw = stream.read(MAX_STATE_BYTES + 1)
                after_handle = os.fstat(stream.fileno())
            after = path.lstat()
            if (
                len(raw) > MAX_STATE_BYTES
                or stat.S_ISLNK(after.st_mode)
                or not stat.S_ISREG(after.st_mode)
                or after.st_nlink != 1
                or _plan_file_identity(after_handle) != _plan_file_identity(info)
                or _plan_file_identity(after) != _plan_file_identity(info)
            ):
                return "unknown"
            decoded = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return "unknown"
        if not isinstance(decoded, dict):
            return "unknown"
        artifact = _safe_plan_artifact(decoded.get("plan_artifact"))
        if artifact.get("format_version") == 2:
            digest = safe_fingerprint(artifact.get("journal_digest")) or None
            generation = safe_int(artifact.get("generation"))
        else:
            digest = None
            generation = 0
    old_digest = (
        None
        if marker.get("old_journal_digest") == "none"
        else marker.get("old_journal_digest")
    )
    if (
        digest == marker.get("new_journal_digest")
        and generation == marker.get("new_generation")
    ):
        return "new"
    if digest == old_digest and generation == marker.get("old_generation"):
        return "old"
    return "unknown"


def recover_plan_transaction(state: dict[str, Any], payload: dict[str, Any]) -> bool:
    try:
        try:
            root = _canonical_plan_data_root(payload)
        except PlanArtifactError:
            artifact = _safe_plan_artifact(state.get("plan_artifact"))
            if not artifact.get("journal_digest"):
                return True
            raise
        session = plan_artifact_session_id(payload.get("session_id"))
        directory = root / "plans" / session
        try:
            directory_info = directory.lstat()
        except (FileNotFoundError, NotADirectoryError):
            artifact = _safe_plan_artifact(state.get("plan_artifact"))
            if artifact.get("format_version") == 2 and artifact.get(
                "journal_digest"
            ):
                raise PlanArtifactError("transaction_incomplete")
            return True
        if stat.S_ISLNK(directory_info.st_mode) or not stat.S_ISDIR(
            directory_info.st_mode
        ):
            raise PlanArtifactError("transaction_incomplete")
        with plan_session_directory_guard(root, session, create=False) as guard:
            directory_fd = guard["directory_fd"]
            marker_path = directory / PLAN_TRANSACTION_MARKER_NAME
            try:
                marker, marker_identity = _read_plan_transaction_marker(
                    marker_path, directory_fd=directory_fd
                )
            except FileNotFoundError:
                return True
            if marker["session_token"] != session:
                raise PlanArtifactError("transaction_incomplete")
            target = directory / PLAN_JOURNAL_NAME
            current_bytes: bytes | None
            current_identity: tuple[int, int, int] | None
            current_mode: int | None
            try:
                current_info = _plan_lstat(target, directory_fd)
            except FileNotFoundError:
                current_bytes = None
                current_identity = None
                current_mode = None
            else:
                current_identity = _plan_file_identity(current_info)
                current_mode = stat.S_IMODE(current_info.st_mode)
                current_bytes = _read_exact_plan_bytes(
                    target,
                    current_identity,
                    directory_fd,
                    max_bytes=MAX_PLAN_JOURNAL_BYTES,
                )
            current_digest = stable_hash(current_bytes, 32) if current_bytes is not None else None
            artifact = _safe_plan_artifact(state.get("plan_artifact"))
            state_digest = (
                artifact.get("journal_digest")
                if artifact.get("format_version") == 2
                else None
            )
            state_generation = (
                safe_int(artifact.get("generation"))
                if artifact.get("format_version") == 2
                else 0
            )
            old_digest = (
                None
                if marker["old_journal_digest"] == "none"
                else marker["old_journal_digest"]
            )
            state_is_old = bool(
                state_digest == old_digest
                and state_generation == marker["old_generation"]
            )
            state_is_new = bool(
                state_digest == marker["new_journal_digest"]
                and state_generation == marker["new_generation"]
            )
            journal_is_old = current_digest == old_digest
            journal_is_new = current_digest == marker["new_journal_digest"]
            backup_name = marker["backup_name"]
            backup_identity: tuple[int, int, int] | None = None
            backup_bytes: bytes | None = None
            backup_mode: int | None = None
            if backup_name != "none":
                backup = directory / backup_name
                try:
                    backup_info = _plan_lstat(backup, directory_fd)
                except FileNotFoundError:
                    backup_identity = None
                else:
                    backup_identity = _plan_file_identity(backup_info)
                    backup_mode = stat.S_IMODE(backup_info.st_mode)
                    backup_bytes = _read_exact_plan_bytes(
                        backup,
                        backup_identity,
                        directory_fd,
                        max_bytes=MAX_PLAN_JOURNAL_BYTES,
                    )
                    if old_digest is None or stable_hash(backup_bytes, 32) != old_digest:
                        raise PlanArtifactError("transaction_incomplete")
            if state_is_new and journal_is_new:
                parse_plan_journal(current_bytes or b"", expected_session=session)
                if marker.get("cleanup_kind") == "legacy_v1":
                    _retain_plan_artifacts(
                        directory,
                        target,
                        directory_fd=directory_fd,
                        verify_binding=guard["verify"],
                        keep_old=0,
                    )
                if backup_identity is not None:
                    _remove_verified_transaction_file(
                        directory / backup_name,
                        backup_identity,
                        directory_fd,
                        expected_bytes=backup_bytes,
                        expected_mode=backup_mode,
                    )
                _fsync_plan_directory(directory_fd)
                _remove_verified_transaction_file(
                    marker_path, marker_identity, directory_fd
                )
                _fsync_plan_directory(directory_fd)
                return True
            if state_is_old and journal_is_new:
                parse_plan_journal(current_bytes or b"", expected_session=session)
                if old_digest is not None and backup_identity is None:
                    raise PlanArtifactError("transaction_incomplete")
                transaction = {
                    "path": target,
                    "directory_fd": directory_fd,
                    "old_identity": backup_identity,
                    "old_mode": backup_mode,
                    "expected_old_bytes": backup_bytes,
                    "expected_new_bytes": current_bytes,
                    "backup_name": None if backup_name == "none" else backup_name,
                    "new_identity": current_identity,
                    "new_mode": current_mode,
                }
                _rollback_plan_write(transaction)
                if old_digest is None:
                    try:
                        _plan_lstat(target, directory_fd)
                    except FileNotFoundError:
                        pass
                    else:
                        raise PlanArtifactError("transaction_incomplete")
                else:
                    restored = _read_plan_artifact_document(
                        target, directory_fd=directory_fd
                    )
                    if stable_hash(restored, 32) != old_digest:
                        raise PlanArtifactError("transaction_incomplete")
                _fsync_plan_directory(directory_fd)
                _remove_verified_transaction_file(
                    marker_path, marker_identity, directory_fd
                )
                _fsync_plan_directory(directory_fd)
                return True
            if state_is_old and journal_is_old:
                if backup_identity is not None:
                    _remove_verified_transaction_file(
                        directory / backup_name,
                        backup_identity,
                        directory_fd,
                        expected_bytes=backup_bytes,
                        expected_mode=backup_mode,
                    )
                _fsync_plan_directory(directory_fd)
                _remove_verified_transaction_file(
                    marker_path, marker_identity, directory_fd
                )
                _fsync_plan_directory(directory_fd)
                return True
            raise PlanArtifactError("transaction_incomplete")
    except (OSError, PlanArtifactError):
        invalidate_plan_authority(
            state, warning_code="transaction_recovery_failed"
        )
        return False


LEGACY_PLAN_DOCUMENT_RE = re.compile(
    r"\A<!-- workflow-manager-plan-artifact:v1\n"
    r"generation: ([0-9]+)\n"
    r"plan_digest: ([0-9a-f]{32})\n"
    r"content_digest: ([0-9a-f]{32})\n"
    r"objective_fingerprint: ([0-9a-f]{16,64}|none)\n"
    r"difficulty_decision_id: ([0-9a-f]{16,64}|none)\n"
    r"-->\n# Workflow Manager Hard Plan\n\n"
    r"> This Markdown file is a private review mirror\. The bound state plan_digest remains authoritative\.\n\n"
    r"<!-- workflow-manager-plan-body -->\n",
)


def parse_legacy_plan_artifact(
    document: bytes,
    *,
    expected_generation: int,
    expected_plan_digest: str,
) -> dict[str, Any]:
    if len(document) > 96 * 1024 + 16 * 1024:
        raise PlanArtifactError("legacy_unavailable")
    try:
        text = document.decode("utf-8")
    except UnicodeError as error:
        raise PlanArtifactError("legacy_unavailable") from error
    match = LEGACY_PLAN_DOCUMENT_RE.match(text)
    if match is None:
        raise PlanArtifactError("legacy_unavailable")
    generation = safe_int(match.group(1))
    plan_digest = match.group(2)
    content_digest = match.group(3)
    if (
        generation <= 0
        or generation != expected_generation
        or plan_digest != expected_plan_digest
    ):
        raise PlanArtifactError("legacy_unavailable")
    body = text[match.end() :]
    if (
        not body.endswith("\n")
        or stable_hash(body, 32) != content_digest
        or "plan mirror truncated at the private artifact byte limit" in body
    ):
        raise PlanArtifactError("legacy_unavailable")
    return {
        "generation": generation,
        "legacy_plan_digest": plan_digest,
        "content_digest": content_digest,
        "objective_fingerprint": None if match.group(4) == "none" else match.group(4),
        "difficulty_decision_id": None if match.group(5) == "none" else match.group(5),
        "body": body,
    }


def migrate_legacy_plan_artifacts(
    state: dict[str, Any], payload: dict[str, Any], source_schema: int
) -> bool:
    legacy_plan_state = state.pop("_legacy_plan_state", state.get("plan_state"))
    legacy_executor_state = state.pop(
        "_legacy_executor_state", state.get("executor_state")
    )
    if source_schema >= SCHEMA_VERSION:
        return True
    artifact = _safe_plan_artifact(state.get("plan_artifact"))
    # Schema 20 already owns the canonical v2 journal. Later schemas change state,
    # routing, and private handoff bindings only; re-verify the journal below
    # instead of treating a valid v2 artifact as an unavailable legacy mirror.
    if source_schema >= 20:
        return True
    if source_schema != 19 or artifact.get("format_version") != 1:
        if state.get("plan_state") in {
            "plan_ready",
            "awaiting_confirmation",
            "confirmed",
        } or state.get("executor_state") not in {None, "none", "succeeded"}:
            invalidate_plan_authority(state, warning_code="legacy_unavailable")
        return False
    try:
        root = _canonical_plan_data_root(payload)
        session = plan_artifact_session_id(payload.get("session_id"))
        expected_prefix = f"plans/{session}/"
        if not str(artifact.get("relative_path") or "").startswith(expected_prefix):
            raise PlanArtifactError("legacy_unavailable")
        directory = root / "plans" / session
        guard_context = plan_session_directory_guard(root, session, create=False)
        guard = guard_context.__enter__()
        guard_owned = True
        marker_path = directory / PLAN_TRANSACTION_MARKER_NAME
        marker_identity: tuple[int, int, int] | None = None
        transaction: dict[str, Any] | None = None
        try:
            directory_fd = guard["directory_fd"]
            guard["verify"]()
            names = (
                os.listdir(directory_fd)
                if directory_fd is not None
                else [entry.name for entry in directory.iterdir()]
            )
            managed_names = sorted(
                name
                for name in names
                if LEGACY_PLAN_ARTIFACT_NAME_RE.fullmatch(name)
            )
            if (
                not managed_names
                or len(managed_names) > MAX_LEGACY_PLAN_ARTIFACTS
            ):
                raise PlanArtifactError("legacy_unavailable")
            records: list[
                tuple[tuple[int, str, tuple[int, int, int]], Path]
            ] = []
            for name in managed_names:
                candidate = directory / name
                record = _owned_plan_artifact_record(
                    candidate, directory_fd=directory_fd
                )
                if record is None:
                    raise PlanArtifactError("legacy_unavailable")
                records.append((record, candidate))
            records.sort(key=lambda item: (item[0][0], item[0][1], item[1].name))
            generations = [record[0][0] for record in records]
            if len(set(generations)) != len(generations):
                raise PlanArtifactError("legacy_unavailable")
            revisions: list[dict[str, Any]] = []
            for record, candidate in records:
                document = _read_exact_plan_bytes(
                    candidate,
                    record[2],
                    directory_fd,
                    max_bytes=96 * 1024 + 16 * 1024,
                )
                revisions.append(
                    parse_legacy_plan_artifact(
                        document,
                        expected_generation=record[0],
                        expected_plan_digest=record[1],
                    )
                )
            current = revisions[-1]
            if (
                current["generation"] != artifact.get("generation")
                or current["legacy_plan_digest"] != artifact.get("plan_digest")
                or current["content_digest"] != artifact.get("content_digest")
            ):
                raise PlanArtifactError("legacy_unavailable")
            canonical: bytes | None = None
            parsed: dict[str, Any] | None = None
            migrated_at = utc_now()
            for revision in revisions:
                canonical, parsed = append_plan_journal_revision(
                    canonical,
                    session=session,
                    generation=revision["generation"],
                    body=revision["body"],
                    objective_fingerprint=revision["objective_fingerprint"],
                    difficulty_decision_id=revision["difficulty_decision_id"],
                    created_at=migrated_at,
                )
            if canonical is None or parsed is None:
                raise PlanArtifactError("legacy_unavailable")
            target = directory / PLAN_JOURNAL_NAME
            try:
                _plan_lstat(target, directory_fd)
            except FileNotFoundError:
                pass
            else:
                raise PlanArtifactError("legacy_unavailable")
            marker = {
                "schema": 1,
                "transaction_id": stable_hash(
                    f"plan-journal-migration-v1\0{session}\0{parsed['journal_digest']}\0{secrets.token_hex(16)}",
                    32,
                ),
                "session_token": session,
                "journal_name": PLAN_JOURNAL_NAME,
                "backup_name": "none",
                "cleanup_kind": "legacy_v1",
                "old_journal_digest": "none",
                "new_journal_digest": parsed["journal_digest"],
                "old_generation": 0,
                "new_generation": parsed["generation"],
            }
            marker_identity = _write_plan_transaction_marker(
                marker_path,
                marker,
                directory_fd=directory_fd,
                verify_binding=guard["verify"],
            )
            try:
                transaction = _atomic_write_plan_file(
                    target,
                    canonical,
                    expected_old_bytes=None,
                    directory_fd=directory_fd,
                    verify_binding=guard["verify"],
                )
                installed = _read_plan_artifact_document(
                    target, directory_fd=directory_fd
                )
                if installed != canonical or parse_plan_journal(
                    installed, expected_session=session
                ) != parsed:
                    raise PlanArtifactError("legacy_unavailable")
            except PlanTransactionPendingError as error:
                transaction = error.transaction
                raise PlanArtifactError("legacy_unavailable") from error
            except Exception as error:
                if transaction is not None:
                    _rollback_plan_write(transaction)
                    _fsync_plan_directory(directory_fd)
                if marker_identity is not None:
                    _remove_verified_transaction_file(
                        marker_path, marker_identity, directory_fd
                    )
                    _fsync_plan_directory(directory_fd)
                raise PlanArtifactError("legacy_unavailable") from error
            old_plan_state = legacy_plan_state
            old_executor_state = legacy_executor_state
            revision_digest = parsed["current_revision_digest"]
            state["plan_generation"] = parsed["generation"]
            state["plan_digest"] = revision_digest
            state["plan_objective_fingerprint"] = parsed["objective_fingerprint"]
            state["plan_difficulty_decision_id"] = parsed["difficulty_decision_id"]
            state["confirmed_plan_digest"] = None
            state["confirmed_at"] = None
            state["plan_artifact"] = {
                "relative_path": f"plans/{session}/{PLAN_JOURNAL_NAME}",
                "format_version": 2,
                "objective_fingerprint": parsed["objective_fingerprint"],
                "difficulty_decision_id": parsed["difficulty_decision_id"],
                "plan_digest": revision_digest,
                "content_digest": revision_digest,
                "current_revision_digest": revision_digest,
                "journal_digest": parsed["journal_digest"],
                "generation": parsed["generation"],
                "revision_count": parsed["revision_count"],
                "lifecycle_status": "ready",
                "write_status": "written",
                "warning_code": "none",
                "created_at": migrated_at,
                "updated_at": migrated_at,
            }
            running = old_executor_state in {
                "spawn_pending",
                "running",
                "local_running",
                "recovery_required",
                "exhausted",
            }
            if running:
                state["executor_failure_kind"] = "stale_contract"
                reset_executor_binding(state, preserve_failure=True)
                state["plan_state"] = "invalidated"
            elif old_plan_state in {
                "plan_ready",
                "awaiting_confirmation",
                "confirmed",
            }:
                reset_executor_binding(state)
                state["plan_state"] = "awaiting_confirmation"
            else:
                reset_executor_binding(state)
                state["plan_state"] = "invalidated"
            pending = {
                "guard_context": guard_context,
                "guard": guard,
                "transaction": transaction,
                "marker_path": marker_path,
                "marker_identity": marker_identity,
                "marker": marker,
                "legacy_cleanup": {
                    "directory": directory,
                    "current": target,
                },
            }
            if state.get("_defer_plan_transaction"):
                state["_plan_transaction"] = pending
                guard_owned = False
            else:
                _finish_plan_transaction(pending, commit=True)
                guard_owned = False
            return True
        finally:
            if guard_owned:
                guard_context.__exit__(None, None, None)
    except (OSError, PlanArtifactError):
        invalidate_plan_authority(state, warning_code="legacy_unavailable")
        return False

def invalidate_plan_authority(
    state: dict[str, Any], *, warning_code: str = "content_drift"
) -> None:
    artifact = _safe_plan_artifact(state.get("plan_artifact"))
    had_executor = bool(
        state.get("plan_state") == "confirmed"
        or state.get("confirmed_plan_digest")
        or state.get("executor_state") not in {None, "none"}
        or state.get("execution_contract_id")
    )
    state["plan_state"] = "invalidated"
    state["confirmed_plan_digest"] = None
    state["confirmed_at"] = None
    if had_executor:
        state["executor_failure_kind"] = "stale_contract"
        reset_executor_binding(state, preserve_failure=True)
    else:
        reset_executor_binding(state)
    artifact["lifecycle_status"] = "invalidated"
    artifact["write_status"] = (
        warning_code
        if warning_code
        in {
            "revision_too_large",
            "journal_full",
            "transaction_recovery_failed",
            "legacy_unavailable",
        }
        else "content_drift"
    )
    artifact["warning_code"] = (
        warning_code if warning_code in PLAN_ARTIFACT_WARNING_CODES else "content_drift"
    )
    artifact["updated_at"] = utc_now()
    state["plan_artifact"] = artifact


def _plan_artifact_binding_valid(
    state: dict[str, Any], artifact: dict[str, Any], parsed: dict[str, Any]
) -> bool:
    return bool(
        _stored_artifact_matches_journal(artifact, parsed)
        and artifact.get("write_status") == "written"
        and artifact.get("plan_digest") == state.get("plan_digest")
        and artifact.get("generation")
        == max(safe_int(state.get("plan_generation")), 0)
        and artifact.get("objective_fingerprint")
        == safe_fingerprint(state.get("plan_objective_fingerprint"))
        and artifact.get("difficulty_decision_id")
        == safe_fingerprint(state.get("plan_difficulty_decision_id"))
    )


def _stored_artifact_matches_journal(
    artifact: dict[str, Any], parsed: dict[str, Any]
) -> bool:
    return bool(
        artifact.get("format_version") == 2
        and artifact.get("write_status")
        in {"written", "write_failed", "revision_too_large", "journal_full"}
        and artifact.get("plan_digest")
        == artifact.get("current_revision_digest")
        == parsed.get("current_revision_digest")
        and artifact.get("journal_digest") == parsed.get("journal_digest")
        and artifact.get("generation") == parsed.get("generation")
        and artifact.get("revision_count") == parsed.get("revision_count")
        and artifact.get("objective_fingerprint")
        == parsed.get("objective_fingerprint")
        and artifact.get("difficulty_decision_id")
        == parsed.get("difficulty_decision_id")
    )


def write_plan_artifact(
    state: dict[str, Any], payload: dict[str, Any], message: str
) -> bool:
    previous = _safe_plan_artifact(state.get("plan_artifact"))
    session = plan_artifact_session_id(payload.get("session_id"))
    relative = f"plans/{session}/{PLAN_JOURNAL_NAME}"
    now = utc_now()
    failure = dict(previous)
    if failure.get("format_version") != 2:
        failure = empty_plan_artifact()
        failure.update(
            {
                "relative_path": relative,
                "format_version": 2,
                "created_at": now,
            }
        )
    failure["updated_at"] = now
    try:
        body = sanitize_plan_artifact_body(message)
        execution_slices = parse_execution_slice_manifest(body)
        objective = safe_fingerprint(state.get("objective", {}).get("fingerprint"))
        difficulty = safe_fingerprint(state.get("difficulty_decision_id"))
        if not objective or not difficulty:
            raise PlanArtifactError("write_error")
        root = _canonical_plan_data_root(payload)
        directory = root / "plans" / session
        target = directory / PLAN_JOURNAL_NAME
        generation = max(
            safe_int(state.get("plan_generation")),
            safe_int(previous.get("generation")),
        ) + 1
        guard_context = plan_session_directory_guard(root, session)
        guard = guard_context.__enter__()
        guard_owned = True
        marker_path = directory / PLAN_TRANSACTION_MARKER_NAME
        marker_identity: tuple[int, int, int] | None = None
        transaction: dict[str, Any] | None = None
        try:
            guard["verify"]()
            directory_fd = guard["directory_fd"]
            existing: bytes | None = None
            parsed_existing: dict[str, Any] | None = None
            try:
                _plan_lstat(target, directory_fd)
            except FileNotFoundError:
                if previous.get("format_version") == 2 and previous.get("journal_digest"):
                    raise PlanArtifactError("content_drift")
            else:
                existing = _read_plan_artifact_document(
                    target, directory_fd=directory_fd
                )
                parsed_existing = parse_plan_journal(
                    existing, expected_session=session
                )
                if previous.get("format_version") != 2 or not _stored_artifact_matches_journal(
                    previous, parsed_existing
                ):
                    raise PlanArtifactError("content_drift")
            document, parsed = append_plan_journal_revision(
                existing,
                session=session,
                generation=generation,
                body=body,
                objective_fingerprint=objective,
                difficulty_decision_id=difficulty,
                created_at=now,
            )
            prepared_backup_name = (
                _transaction_name(target, "backup", directory_fd)
                if existing is not None
                else None
            )
            marker = {
                "schema": 1,
                "transaction_id": stable_hash(
                    f"plan-journal-transaction-v1\0{session}\0{parsed['journal_digest']}\0{secrets.token_hex(16)}",
                    32,
                ),
                "session_token": session,
                "journal_name": PLAN_JOURNAL_NAME,
                "backup_name": prepared_backup_name or "none",
                "cleanup_kind": "none",
                "old_journal_digest": (
                    parsed_existing["journal_digest"]
                    if parsed_existing is not None
                    else "none"
                ),
                "new_journal_digest": parsed["journal_digest"],
                "old_generation": (
                    parsed_existing["generation"]
                    if parsed_existing is not None
                    else 0
                ),
                "new_generation": generation,
            }
            marker_identity = _write_plan_transaction_marker(
                marker_path,
                marker,
                directory_fd=directory_fd,
                verify_binding=guard["verify"],
            )
            try:
                transaction = _atomic_write_plan_file(
                    target,
                    document,
                    expected_old_bytes=existing,
                    directory_fd=directory_fd,
                    verify_binding=guard["verify"],
                    prepared_backup_name=prepared_backup_name,
                )
                guard["verify"]()
                installed = _read_plan_artifact_document(
                    target, directory_fd=directory_fd
                )
                installed_parsed = parse_plan_journal(
                    installed, expected_session=session
                )
                if installed != document or installed_parsed != parsed:
                    raise PlanArtifactError("content_drift")
            except PlanTransactionPendingError as error:
                transaction = error.transaction
                raise PlanArtifactError("write_error") from error
            except Exception as error:
                if transaction is not None:
                    try:
                        _rollback_plan_write(transaction)
                        _fsync_plan_directory(directory_fd)
                    except PlanArtifactError as rollback_error:
                        raise rollback_error from error
                if marker_identity is not None:
                    _remove_verified_transaction_file(
                        marker_path, marker_identity, directory_fd
                    )
                    _fsync_plan_directory(directory_fd)
                raise
            pending = {
                "guard_context": guard_context,
                "guard": guard,
                "transaction": transaction,
                "marker_path": marker_path,
                "marker_identity": marker_identity,
                "marker": marker,
            }
            if state.get("_defer_plan_transaction"):
                state["_plan_transaction"] = pending
                guard_owned = False
            else:
                _finish_plan_transaction(pending, commit=True)
                guard_owned = False
        finally:
            if guard_owned:
                guard_context.__exit__(None, None, None)
        plan_digest = parsed["current_revision_digest"]
        artifact = empty_plan_artifact()
        artifact.update(
            {
                "relative_path": relative,
                "format_version": 2,
                "objective_fingerprint": objective,
                "difficulty_decision_id": difficulty,
                "plan_digest": plan_digest,
                "content_digest": plan_digest,
                "current_revision_digest": plan_digest,
                "journal_digest": parsed["journal_digest"],
                "generation": generation,
                "revision_count": parsed["revision_count"],
                "lifecycle_status": "ready",
                "write_status": "written",
                "warning_code": "none",
                "created_at": previous.get("created_at") or now,
                "updated_at": now,
            }
        )
        state["plan_generation"] = generation
        state["plan_digest"] = plan_digest
        state["plan_objective_fingerprint"] = objective
        state["plan_difficulty_decision_id"] = difficulty
        state["plan_artifact"] = artifact
        state["execution_slices"] = persisted_execution_slices(
            execution_slices, plan_digest
        )
        return True
    except PlanArtifactError as error:
        failure["write_status"] = (
            error.code
            if error.code in {"revision_too_large", "journal_full"}
            else "write_failed"
        )
        failure["warning_code"] = error.code
    except OSError:
        failure["write_status"] = "write_failed"
        failure["warning_code"] = "write_error"
    state["plan_artifact"] = failure
    return False


def verify_plan_artifact(state: dict[str, Any], payload: dict[str, Any]) -> bool:
    artifact = _safe_plan_artifact(state.get("plan_artifact"))
    if artifact.get("format_version") != 2 or not artifact.get("relative_path"):
        state["plan_artifact"] = artifact
        return False
    if (
        not artifact.get("journal_digest")
        or not artifact.get("current_revision_digest")
        or artifact.get("generation", 0) <= 0
    ):
        state["plan_artifact"] = artifact
        return False
    try:
        parts = artifact["relative_path"].split("/")
        if (
            len(parts) != 3
            or parts[0] != "plans"
            or parts[2] != PLAN_JOURNAL_NAME
        ):
            raise PlanArtifactError("unsafe_path")
        _, session, filename = parts
        if session != plan_artifact_session_id(payload.get("session_id")):
            raise PlanArtifactError("content_drift")
        root = _canonical_plan_data_root(payload)
        target = root / "plans" / session / filename
        with plan_session_directory_guard(root, session, create=False) as guard:
            guard["verify"]()
            document = _read_plan_artifact_document(
                target, directory_fd=guard["directory_fd"]
            )
            parsed = parse_plan_journal(document, expected_session=session)
            guard["verify"]()
        active_binding = state.get("plan_state") in {
            "plan_ready",
            "awaiting_confirmation",
            "confirmed",
        }
        valid = (
            _plan_artifact_binding_valid(state, artifact, parsed)
            if active_binding
            else _stored_artifact_matches_journal(artifact, parsed)
        )
        if not valid:
            raise PlanArtifactError("content_drift")
        if (
            active_binding
            and str(state.get("execution_profile_version"))
            == EXECUTION_PROFILE_VERSION
        ):
            parsed_manifest = parse_execution_slice_manifest(
                str(parsed["revisions"][-1]["body"])
            )
            persisted = _safe_execution_slices(state.get("execution_slices"))
            if (
                persisted.get("plan_digest") != state.get("plan_digest")
                or persisted.get("manifest_digest")
                != parsed_manifest.get("manifest_digest")
                or persisted.get("global_constraints_digest")
                != parsed_manifest.get("global_constraints_digest")
                or persisted.get("count") != parsed_manifest.get("count")
                or [item.get("slice_digest") for item in persisted.get("items", [])]
                != [item.get("slice_digest") for item in parsed_manifest.get("items", [])]
            ):
                raise PlanArtifactError("execution_slices_invalid")
        if (
            state.get("plan_state")
            in {"plan_ready", "awaiting_confirmation", "confirmed"}
            and (
                artifact.get("write_status") != "written"
                or artifact.get("warning_code") != "none"
            )
        ):
            artifact["write_status"] = "written"
            artifact["warning_code"] = "none"
            artifact["updated_at"] = utc_now()
        state["plan_artifact"] = artifact
        return True
    except PlanArtifactError as error:
        invalidate_plan_authority(
            state,
            warning_code=(
                error.code
                if error.code in {"unsafe_path", "content_drift", "journal_full", "execution_slices_invalid"}
                else "content_drift"
            ),
        )
    except OSError:
        invalidate_plan_authority(state, warning_code="content_drift")
    return False


def _read_verified_current_plan_revision(
    state: dict[str, Any], payload: dict[str, Any]
) -> str:
    artifact = _safe_plan_artifact(state.get("plan_artifact"))
    parts = str(artifact.get("relative_path") or "").split("/")
    if len(parts) != 3:
        raise PlanArtifactError("unsafe_path")
    _, session, filename = parts
    root = _canonical_plan_data_root(payload)
    target = root / "plans" / session / filename
    with plan_session_directory_guard(root, session, create=False) as guard:
        document = _read_plan_artifact_document(
            target, directory_fd=guard["directory_fd"]
        )
        parsed = parse_plan_journal(document, expected_session=session)
        guard["verify"]()
    if not _stored_artifact_matches_journal(artifact, parsed):
        raise PlanArtifactError("content_drift")
    return str(parsed["revisions"][-1]["body"])


def read_current_plan_revision(
    state: dict[str, Any], payload: dict[str, Any]
) -> str:
    if not verify_plan_artifact(state, payload):
        raise PlanArtifactError("content_drift")
    return _read_verified_current_plan_revision(state, payload)


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
        "phase": item.get("phase") if item.get("phase") in {"pre", "post", "rollout_reconciled"} else "unknown",
        "source": item.get("source") if item.get("source") in {"hook", "host_rollout_reconciled"} else "hook",
        "rollout_compaction_fingerprint": safe_fingerprint(item.get("rollout_compaction_fingerprint")) or None,
        "window_number": max(safe_int(item.get("window_number")), 0) or None,
        "window_id": safe_label(item.get("window_id"), 120) if item.get("window_id") else None,
        "previous_window_id": safe_label(item.get("previous_window_id"), 120) if item.get("previous_window_id") else None,
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
        "plan_artifact": _safe_plan_artifact(item.get("plan_artifact")),
        "execution_slices": _safe_execution_slices(item.get("execution_slices")),
        "session_execution_preference": safe_session_execution_preference(
            item.get("session_execution_preference")
        ),
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
        "executor_review": _safe_executor_review(item.get("executor_review")),
        "reference_acceptance": _safe_reference_acceptance(item.get("reference_acceptance")),
        "last_execution_baseline": _safe_execution_baseline(
            item.get("last_execution_baseline")
        ),
        "causal_review": _safe_causal_review(item.get("causal_review")),
        "stall": _safe_stall(item.get("stall")),
        "coordination_activity": [
            safe for raw in as_list(item.get("coordination_activity"))
            if (safe := _safe_coordination_activity(raw)) is not None
        ][:MAX_COORDINATION_ACTIVITY],
        "coordination_notices": [
            safe for raw in as_list(item.get("coordination_notices"))
            if (safe := _safe_coordination_notice(raw)) is not None
        ][-MAX_COORDINATION_NOTICES:],
        "coordination_inbound": [
            safe for raw in as_list(item.get("coordination_inbound"))
            if (safe := _safe_coordination_inbound(raw)) is not None
        ][-MAX_COORDINATION_INBOUND:],
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
    # Schema 14 had no session-wide preference. Never infer opt-in from legacy
    # profile names, prompts, or execution state.
    base["session_execution_preference"] = (
        safe_session_execution_preference(value.get("session_execution_preference"))
        if safe_int(value.get("schema_version")) >= 15
        else "default"
    )
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

    base["objective"] = safe_metadata(value.get("objective"))
    if not base["objective"] and value.get("last_objective"):
        base["objective"] = text_metadata(value.get("last_objective"))

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
    base["plan_artifact"] = (
        _safe_plan_artifact(value.get("plan_artifact"))
        if safe_int(value.get("schema_version")) >= 18
        else _legacy_plan_artifact(base)
    )
    source_schema = safe_int(value.get("schema_version"))
    base["execution_slices"] = (
        _safe_execution_slices(value.get("execution_slices"))
        if source_schema >= SCHEMA_VERSION
        else _empty_execution_slices()
    )
    source_writer = (
        safe_label(value.get("writer_version"), 64)
        if value.get("writer_version")
        else "unknown"
    )
    source_profile = safe_label(
        value.get("execution_profile_version") or EXECUTION_PROFILE_VERSION,
        16,
    )
    source_contract = safe_fingerprint(value.get("execution_contract_id")) or None
    source_baseline = _safe_execution_baseline(value.get("last_execution_baseline"))
    source_executor_review = _safe_executor_review(value.get("executor_review"))
    sealed_historical_success = bool(
        value.get("plan_state") == "confirmed"
        and value.get("executor_state") == "succeeded"
        and source_contract
        and source_baseline.get("execution_contract_id") == source_contract
        and source_baseline.get("objective_fingerprint")
        == base.get("objective", {}).get("fingerprint")
        and source_baseline.get("plan_digest") == base.get("plan_digest")
        and source_baseline.get("acceptance_status") == "passed"
    )
    legacy_verification_pending = bool(
        source_schema == 22
        and source_writer == "1.0.41"
        and source_profile == "5"
        and value.get("plan_state") == "confirmed"
        and value.get("executor_state") == "succeeded"
        and source_contract
        and source_baseline.get("execution_contract_id") == source_contract
        and source_baseline.get("objective_fingerprint")
        == base.get("objective", {}).get("fingerprint")
        and source_baseline.get("plan_digest") == base.get("plan_digest")
        and source_baseline.get("acceptance_status") == "incomplete"
    )
    # Profile v7 may retain a terminal candidate for a read-only parent review,
    # but no pending/running v7 state may gain v8 mutation authority by migration.
    review_profile_continuity = bool(
        source_schema >= 23
        and source_contract
        and source_executor_review.get("execution_contract_id") == source_contract
        and (
            source_executor_review.get("attempt")
            == min(
                max(safe_int(value.get("executor_attempt")), 0),
                MAX_EXECUTOR_ATTEMPTS,
            )
            or source_executor_review.get("status") == "recovery_started"
            and source_executor_review.get("attempt") == 1
            and safe_int(value.get("executor_attempt")) == 2
        )
        and source_executor_review.get("status")
        in {"review_required", "recovery_started", "failed", "exhausted"}
        and value.get("executor_state") in {"verification_required", "exhausted"}
    )
    # Active and failed contracts rebind to the current profile. A completed,
    # baseline-sealed contract keeps the profile it actually executed under;
    # rewriting it to v6 would either invent evidence or reopen finished work.
    # The one Schema 22 succeeded+incomplete shape is not sealed: preserve its
    # real v5 contract as a review candidate so a fresh v2 can repair it.
    base["execution_profile_version"] = (
        source_profile
        if sealed_historical_success
        or legacy_verification_pending
        or review_profile_continuity
        else EXECUTION_PROFILE_VERSION
    )
    base["executor_state"] = (
        "verification_required"
        if legacy_verification_pending
        else value.get("executor_state")
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
        None
        if legacy_verification_pending
        else value.get("executor_failure_kind")
        if value.get("executor_failure_kind") in EXECUTOR_FAILURE_KINDS
        else None
    )
    base["reference_acceptance"] = _safe_reference_acceptance(value.get("reference_acceptance"))
    base["assessor_generation"] = max(safe_int(value.get("assessor_generation")), 0)
    base["assessor_binding_id"] = safe_fingerprint(value.get("assessor_binding_id")) or None
    base["assessor_state"] = value.get("assessor_state") if value.get("assessor_state") in ASSESSOR_STATES else "none"
    base["assessor_failure_kind"] = safe_label(value.get("assessor_failure_kind"), 48) if value.get("assessor_failure_kind") else None
    base["assessor_observed_effective"] = bool(value.get("assessor_observed_effective"))
    base["assessor_observed_model"] = safe_label(value.get("assessor_observed_model"), 80) if value.get("assessor_observed_model") else None
    base["assessor_observed_reasoning_effort"] = safe_label(value.get("assessor_observed_reasoning_effort"), 24) if value.get("assessor_observed_reasoning_effort") else None
    base["assessor_start_observed"] = value.get("assessor_start_observed") if value.get("assessor_start_observed") in {"full", "partial", "absent", "mismatch"} else "absent"
    base["assessor_observation_source"] = safe_label(value.get("assessor_observation_source"), 80) if value.get("assessor_observation_source") else None
    base["assessor_agent_id"] = safe_label(value.get("assessor_agent_id"), 120) if value.get("assessor_agent_id") else None
    base["assessor_model"] = safe_label(value.get("assessor_model"), 80) if value.get("assessor_model") else None
    base["assessor_reasoning_effort"] = safe_label(value.get("assessor_reasoning_effort"), 24) if value.get("assessor_reasoning_effort") else None
    base["assessor_input_fingerprint"] = safe_fingerprint(value.get("assessor_input_fingerprint")) or None
    base["assessor_fork_turns"] = str(value.get("assessor_fork_turns")) if value.get("assessor_fork_turns") is not None else None
    base["assessor_attempt"] = min(max(safe_int(value.get("assessor_attempt")), 0), 2)
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
    base["executor_observed_effective"] = bool(
        value.get("executor_observed_effective")
    )
    base["executor_observed_model"] = (
        safe_label(value.get("executor_observed_model"), 80)
        if value.get("executor_observed_model") else None
    )
    base["executor_observed_reasoning_effort"] = (
        safe_label(value.get("executor_observed_reasoning_effort"), 24)
        if value.get("executor_observed_reasoning_effort") else None
    )
    base["executor_start_observed"] = value.get("executor_start_observed") if value.get("executor_start_observed") in {"full", "partial", "absent", "mismatch"} else "absent"
    base["executor_observation_source"] = safe_label(value.get("executor_observation_source"), 80) if value.get("executor_observation_source") else None
    base["last_execution_baseline"] = _safe_execution_baseline(
        value.get("last_execution_baseline")
    )
    base["executor_review"] = (
        _legacy_candidate_executor_review(
            value,
            source_contract,
            max(base["executor_attempt"], 1),
            source_baseline,
        )
        if legacy_verification_pending and source_contract
        else source_executor_review
    )
    if legacy_verification_pending:
        base["executor_attempt"] = max(base["executor_attempt"], 1)
        base["executor_agent_id"] = None
    base["causal_review"] = _safe_causal_review(value.get("causal_review"))
    if safe_int(value.get("schema_version")) >= 17:
        base["stall"] = _safe_stall(value.get("stall"))
    if safe_int(value.get("schema_version")) >= 16:
        base["coordination_activity"] = [
            safe for raw in as_list(value.get("coordination_activity"))
            if (safe := _safe_coordination_activity(raw)) is not None
        ][:MAX_COORDINATION_ACTIVITY]
        base["coordination_notices"] = [
            safe for raw in as_list(value.get("coordination_notices"))
            if (safe := _safe_coordination_notice(raw)) is not None
        ][-MAX_COORDINATION_NOTICES:]
        base["coordination_inbound"] = [
            safe for raw in as_list(value.get("coordination_inbound"))
            if (safe := _safe_coordination_inbound(raw)) is not None
        ][-MAX_COORDINATION_INBOUND:]

    base["last_assistant"] = safe_metadata(value.get("last_assistant"))
    expected_assessor = assessor_binding_id(base) if base["assessor_generation"] else None
    if base["assessor_state"] in {"spawn_pending", "running", "hard_plan_ready"} and base["assessor_binding_id"] != expected_assessor:
        base["assessor_state"] = "recovery_required"
        base["assessor_failure_kind"] = "stale_binding"
    # Schema 13 predates the assessor handoff.  Preserve a genuinely confirmed
    # executor contract, but never treat an old unconfirmed Work route as assessed.
    if (
        safe_int(value.get("schema_version")) <= 13
        and base["task_domain"] == "work"
        and base["plan_state"] != "confirmed"
        and base["assessor_state"] == "none"
        and base["objective"].get("fingerprint")
    ):
        base["assessor_generation"] = 1
        base["assessor_binding_id"] = assessor_binding_id(base)
        base["assessor_input_fingerprint"] = base["objective"].get("fingerprint")
        base["assessor_state"] = "spawn_required"

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

    if (
        (source_schema == 23 or source_profile == "7")
        and base["plan_state"] == "confirmed"
        and not sealed_historical_success
        and not review_profile_continuity
    ):
        # Older revisions never gain v8 write authority. Never invent a
        # synthetic slice or silently grant a fresh attempt. The next high-plan
        # revision must append the one strict manifest to the same journal and
        # receive a new explicit confirmation.
        base["plan_state"] = "analyzing"
        base["confirmed_plan_digest"] = None
        base["confirmed_at"] = None
        base["executor_state"] = "recovery_required"
        base["executor_failure_kind"] = "stale_contract"
        base["executor_agent_id"] = None
        base["executor_review"] = _empty_executor_review()
        base["assessor_state"] = "hard_plan_ready"
        base["model_profile"] = "work_assessment"
    expected_contract = (
        source_contract
        if sealed_historical_success or legacy_verification_pending or review_profile_continuity
        else execution_contract_id(base)
    ) if base["plan_state"] == "confirmed" else None
    valid_execution_binding = bool(
        expected_contract
        and base["execution_contract_id"] == expected_contract
        and (
            base["execution_profile_version"] == EXECUTION_PROFILE_VERSION
            or sealed_historical_success
            or legacy_verification_pending
            or review_profile_continuity
        )
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
        base["executor_review"] = _empty_executor_review()
        base["model_profile"] = confirmed_executor_model_profile(base)
        if base["last_route"].get("delegation_opt_out"):
            base["executor_state"] = "local_running"
    elif base["plan_state"] == "confirmed":
        base["model_profile"] = (
            "work_assessment"
            if base["executor_state"]
            in {"verification_required", "recovery_required", "exhausted"}
            else confirmed_executor_model_profile(base)
        )
    elif (
        base["plan_state"] == "invalidated"
        and base.get("executor_failure_kind") == "stale_contract"
    ):
        base["execution_contract_id"] = None
        base["executor_state"] = "recovery_required"
        base["executor_agent_id"] = None
        base["executor_model"] = None
        base["executor_reasoning_effort"] = None
        base["executor_fork_turns"] = None
        base["executor_review"] = _empty_executor_review()
        base["model_profile"] = "work_assessment"
    elif base["plan_state"] != "confirmed":
        base["execution_contract_id"] = None
        base["executor_state"] = "none"
        base["executor_agent_id"] = None
        base["executor_attempt"] = 0
        base["executor_failure_kind"] = None
        base["executor_model"] = None
        base["executor_reasoning_effort"] = None
        base["executor_fork_turns"] = None
        base["executor_review"] = _empty_executor_review()
    baseline = _safe_execution_baseline(base.get("last_execution_baseline"))
    if (
        base.get("plan_state") == "confirmed"
        and base.get("executor_state") == "succeeded"
        and baseline.get("acceptance_status") == "incomplete"
    ):
        base["executor_state"] = "verification_required"
        base["executor_agent_id"] = None
        base["executor_attempt"] = max(base.get("executor_attempt", 0), 1)
        base["executor_failure_kind"] = None
        review = _safe_executor_review(base.get("executor_review"))
        if (
            review.get("status") != "review_required"
            or review.get("execution_contract_id")
            != base.get("execution_contract_id")
            or review.get("attempt") != base.get("executor_attempt")
        ):
            review = _legacy_candidate_executor_review(
                value,
                str(base.get("execution_contract_id") or ""),
                base["executor_attempt"],
                baseline,
            )
        base["executor_review"] = review
        base["model_profile"] = "work_assessment"
    elif (
        base.get("plan_state") == "confirmed"
        and base.get("executor_state") == "succeeded"
        and baseline.get("acceptance_status") != "passed"
    ):
        base["executor_state"] = (
            "recovery_required"
            if safe_int(base.get("executor_attempt")) < MAX_EXECUTOR_ATTEMPTS
            else "exhausted"
        )
        base["executor_agent_id"] = None
        base["executor_failure_kind"] = "verification_failed"
        base["executor_review"] = _empty_executor_review()
        base["model_profile"] = "work_assessment"
    review = _safe_executor_review(base.get("executor_review"))
    if base.get("executor_state") == "verification_required":
        review_bound = bool(
            review.get("status") == "review_required"
            and review.get("execution_contract_id")
            == base.get("execution_contract_id")
            and review.get("attempt") == base.get("executor_attempt")
            and review.get("candidate_result_fingerprint")
            and review.get("candidate_evidence_digest")
        )
        if not review_bound:
            base["executor_state"] = (
                "recovery_required"
                if base.get("executor_attempt", 0) < MAX_EXECUTOR_ATTEMPTS
                else "exhausted"
            )
            base["executor_failure_kind"] = "verification_failed"
            base["executor_review"] = _empty_executor_review()
        base["model_profile"] = "work_assessment"
    elif review.get("status") != "none" and review.get(
        "execution_contract_id"
    ) != base.get("execution_contract_id"):
        base["executor_review"] = _empty_executor_review()
    stall = _safe_stall(base.get("stall"))
    if stall.get("state") not in {"none", "resolved"}:
        bound = bool(
            stall.get("objective_fingerprint") == base.get("objective", {}).get("fingerprint")
            and stall.get("plan_digest") == base.get("plan_digest")
            and stall.get("execution_contract_id") == base.get("execution_contract_id")
        )
        if not bound:
            stall["state"] = "exhausted"
            if base.get("plan_state") == "confirmed":
                base["executor_state"] = "exhausted"
        base["model_profile"] = (
            stall.get("resume_profile")
            if stall.get("state") in {"resume_required", "resuming"}
            else "work_assessment"
        )
        base["stall"] = stall
    if base["causal_review"].get("state") in {"triage_required", "triaging"}:
        base["model_profile"] = "work_assessment"

    base["prompts"] = [item for raw in as_list(value.get("prompts")) if (item := _safe_prompt(raw)) is not None][-MAX_PROMPTS:]
    base["operations"] = [item for raw in as_list(value.get("operations")) if (item := _safe_operation(raw)) is not None][-MAX_OPERATIONS:]
    safe_subagents = [
        item for raw in as_list(value.get("subagents"))
        if (item := _safe_subagent(raw)) is not None
    ]
    if safe_int(value.get("schema_version")) < 19:
        for item in safe_subagents:
            item["request_visibility"] = None
    base["subagents"] = retained_subagent_records(base, safe_subagents)
    base["change_epoch"] = min(max(safe_int(value.get("change_epoch")), 0), MAX_EVENT_COUNT)
    base["change_epoch_ledger"] = [
        {
            "epoch": min(max(safe_int(item.get("epoch")), 0), MAX_EVENT_COUNT),
            "fingerprint": safe_fingerprint(item.get("fingerprint")) or None,
            "kind": safe_label(item.get("kind"), 24),
            "at": str(item.get("at") or "")[:40] or None,
        }
        for item in as_list(value.get("change_epoch_ledger"))
        if isinstance(item, dict) and safe_fingerprint(item.get("fingerprint"))
    ][-MAX_CHANGE_EPOCH_LEDGER:]
    raw_identity = value.get("identity_evidence") if isinstance(value.get("identity_evidence"), dict) else {}
    base["identity_evidence"] = {
        "requested_profile": safe_fingerprint(raw_identity.get("requested_profile")) or None,
        "start_echo_profile": safe_fingerprint(raw_identity.get("start_echo_profile")) or None,
        "plugin_root_fingerprint": safe_fingerprint(raw_identity.get("plugin_root_fingerprint")) or None,
    }
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
    if (
        safe_int(value.get("schema_version")) < 19
        and base.get("plan_state")
        in {"plan_ready", "awaiting_confirmation", "confirmed"}
    ):
        invalidate_plan_authority(base, warning_code="legacy_unavailable")
    if source_writer != WRITER_VERSION:
        # Opaque lifecycle/request records are evidence from the old writer, not
        # authority for a resumed task. Preserve canonical plans and completed
        # baselines, but remove transient caches and rebind unfinished assessment.
        base["subagents"] = []
        base["processed_hook_runs"] = []
        base["duplicate_notices"] = []
        base["coordination_activity"] = []
        base["coordination_notices"] = []
        base["coordination_inbound"] = []
        base["assessor_agent_id"] = None
        base["assessor_model"] = None
        base["assessor_reasoning_effort"] = None
        base["assessor_failure_kind"] = None
        base["assessor_observed_effective"] = False
        base["assessor_observed_model"] = None
        base["assessor_observed_reasoning_effort"] = None
        base["assessor_fork_turns"] = None
        base["assessor_attempt"] = 0
        if (
            base["task_domain"] == "work"
            and base.get("objective", {}).get("fingerprint")
            and base.get("plan_state") in {"none", "analyzing"}
        ):
            base["assessor_generation"] = max(base["assessor_generation"], 0) + 1
            base["assessor_binding_id"] = assessor_binding_id(base)
            base["assessor_input_fingerprint"] = base["objective"]["fingerprint"]
            base["assessor_state"] = "spawn_required"
            base["model_profile"] = "work_assessment"
        else:
            base["assessor_binding_id"] = None
            base["assessor_input_fingerprint"] = None
            base["assessor_state"] = "none"
    sync_plan_artifact_lifecycle(base)
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
            raise ValueError("state exceeds byte limit")
        decoded = json.loads(raw)
        source_schema = (
            safe_int(decoded.get("schema_version"))
            if isinstance(decoded, dict)
            else 0
        )
        state = normalize_state(decoded, payload)
        state["_source_schema_version"] = source_schema
        if source_schema < SCHEMA_VERSION and isinstance(decoded, dict):
            state["_legacy_plan_state"] = decoded.get("plan_state")
            state["_legacy_executor_state"] = decoded.get("executor_state")
        return state
    except FileNotFoundError:
        return new_state(payload)
    except Exception:
        state = new_state(payload)
        state["_state_load_failure"] = "invalid_state"
        return state


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
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        if os.name != "nt":
            directory_fd = -1
            try:
                directory_fd = os.open(
                    path.parent,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                )
                os.fsync(directory_fd)
            finally:
                if directory_fd >= 0:
                    os.close(directory_fd)
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
    state["subagents"] = retained_subagent_records(state)
    state["compactions"] = list(state.get("compactions", []))[-MAX_COMPACTIONS:]
    state["guards"] = list(state.get("guards", []))[-MAX_GUARDS:]
    state["processed_hook_runs"] = list(state.get("processed_hook_runs", []))[-MAX_PROCESSED_RUNS:]
    state["duplicate_notices"] = list(state.get("duplicate_notices", []))[-MAX_DUPLICATE_NOTICES:]
    state["coordination_activity"] = list(state.get("coordination_activity", []))[:MAX_COORDINATION_ACTIVITY]
    state["coordination_notices"] = list(state.get("coordination_notices", []))[-MAX_COORDINATION_NOTICES:]
    state["coordination_inbound"] = list(state.get("coordination_inbound", []))[-MAX_COORDINATION_INBOUND:]


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
        state = new_state(payload)
        state["_snapshot_failure"] = "missing_session_id"
        return state
    try:
        with state_lock(path):
            existed = path.exists()
            state = load_state(path, payload)
            if not existed:
                state["_snapshot_failure"] = "missing_state"
                return state
            if state.get("_state_load_failure"):
                state["_snapshot_failure"] = "invalid_state"
                return state
            if not recover_plan_transaction(state, payload):
                state["_snapshot_failure"] = "transaction_recovery_failed"
                state.pop("_source_schema_version", None)
                state.pop("_legacy_plan_state", None)
                state.pop("_legacy_executor_state", None)
                atomic_write(path, state)
                return state
            source_schema = safe_int(
                state.pop("_source_schema_version", SCHEMA_VERSION)
            )
            before = json.dumps(state, ensure_ascii=False, sort_keys=True)
            canonical_current_body: str | None = None
            state["_defer_plan_transaction"] = True
            try:
                if source_schema < SCHEMA_VERSION:
                    migrate_legacy_plan_artifacts(
                        state, payload, source_schema
                    )
                artifact_valid = verify_plan_artifact(state, payload)
                if payload.get("_read_canonical_plan_body") and artifact_valid:
                    canonical_current_body = _read_verified_current_plan_revision(
                        state, payload
                    )
            finally:
                state.pop("_defer_plan_transaction", None)
            pending = state.pop("_plan_transaction", None)
            after = json.dumps(state, ensure_ascii=False, sort_keys=True)
            try:
                if after != before or pending is not None:
                    atomic_write(path, state)
            except Exception:
                if pending is not None:
                    side = _persisted_plan_transaction_side(path, pending)
                    if side == "old":
                        _finish_plan_transaction(pending, commit=False)
                    else:
                        _leave_plan_transaction_for_recovery(pending)
                raise
            if state.get("plan_state") == "confirmed" and not verify_plan_artifact(
                state, payload
            ):
                sync_plan_artifact_lifecycle(state)
                set_persistence_metadata(
                    state,
                    payload,
                    attempted=True,
                    ok=True,
                    outcome="written_invalidated",
                )
                atomic_write(path, state)
            if pending is not None:
                try:
                    _finish_plan_transaction(pending, commit=True)
                except (OSError, PlanArtifactError) as cleanup_error:
                    debug_persistence(
                        payload,
                        path_resolved=True,
                        outcome="transaction_cleanup_pending",
                        error=cleanup_error,
                    )
            if canonical_current_body is not None:
                state["_canonical_current_body"] = canonical_current_body
            return state
    except TimeoutError as error:
        debug_persistence(payload, path_resolved=True, outcome="lock_timeout", error=error)
        state = new_state(payload)
        state["_snapshot_failure"] = "lock_timeout"
        return state
    except OSError as error:
        debug_persistence(payload, path_resolved=True, outcome="read_error", error=error)
        state = new_state(payload)
        state["_snapshot_failure"] = "read_error"
        return state


def mutate_state(
    payload: dict[str, Any], change: Callable[[dict[str, Any]], None]
) -> tuple[dict[str, Any], bool]:
    path = state_path(payload)
    if path is None:
        state = new_state(payload)
        increment_event_count(state, payload)
        state["_defer_plan_transaction"] = True
        try:
            change(state)
        finally:
            state.pop("_defer_plan_transaction", None)
        pending = state.pop("_plan_transaction", None)
        if pending is not None:
            try:
                _finish_plan_transaction(pending, commit=False)
            finally:
                artifact = _safe_plan_artifact(state.get("plan_artifact"))
                artifact["write_status"] = "write_failed"
                artifact["warning_code"] = "write_error"
                state["plan_artifact"] = artifact
                state["plan_state"] = "analyzing"
        sync_plan_artifact_lifecycle(state)
        outcome = "disabled" if not persistence_enabled() else "missing_session_id"
        set_persistence_metadata(state, payload, attempted=False, ok=False, outcome=outcome)
        trim_state(state)
        debug_persistence(payload, path_resolved=False, outcome=outcome)
        return state, True
    try:
        with state_lock(path):
            state = load_state(path, payload)
            if state.pop("_state_load_failure", None):
                debug_persistence(
                    payload, path_resolved=True, outcome="invalid_state"
                )
                return state, False
            if not recover_plan_transaction(state, payload):
                state.pop("_source_schema_version", None)
                state.pop("_legacy_plan_state", None)
                state.pop("_legacy_executor_state", None)
                sync_plan_artifact_lifecycle(state)
                set_persistence_metadata(
                    state,
                    payload,
                    attempted=True,
                    ok=False,
                    outcome="transaction_recovery_failed",
                )
                atomic_write(path, state)
                return state, False
            source_schema = safe_int(
                state.pop("_source_schema_version", SCHEMA_VERSION)
            )
            if payload.get("cwd"):
                state["cwd_fingerprint"] = stable_hash(payload.get("cwd"))
            current_root = current_plugin_root_fingerprint()
            if current_root:
                # Runtime activation identity is evidence about the Hook that is
                # processing this event, not a historical session property.
                # Refresh it on every persisted event after a cachebuster/reload.
                state.setdefault("identity_evidence", {})[
                    "plugin_root_fingerprint"
                ] = current_root
            # SubagentStart model/effort fields describe the child runtime echo;
            # they must never replace the parent session model used to resolve a
            # lower-tier recovery profile.
            if payload.get("model") and payload.get("hook_event_name") not in {
                "SubagentStart",
                "SubagentStop",
            }:
                state["model"] = safe_label(payload.get("model"), 80)
            run_key = hook_run_key(payload)
            if run_key and run_key in state.get("processed_hook_runs", []):
                debug_persistence(payload, path_resolved=True, outcome="duplicate")
                return state, False
            state["_defer_plan_transaction"] = True
            try:
                if source_schema < SCHEMA_VERSION:
                    migrate_legacy_plan_artifacts(
                        state, payload, source_schema
                    )
                verify_plan_artifact(state, payload)
                increment_event_count(state, payload)
                change(state)
            except Exception:
                pending = state.pop("_plan_transaction", None)
                state.pop("_defer_plan_transaction", None)
                if pending is not None:
                    _finish_plan_transaction(pending, commit=False)
                raise
            state.pop("_defer_plan_transaction", None)
            pending = state.pop("_plan_transaction", None)
            sync_plan_artifact_lifecycle(state)
            if run_key:
                state.setdefault("processed_hook_runs", []).append(run_key)
            trim_state(state)
            set_persistence_metadata(state, payload, attempted=True, ok=True, outcome="written")
            state.pop("_snapshot_failure", None)
            try:
                atomic_write(path, state)
            except Exception:
                if pending is not None:
                    side = _persisted_plan_transaction_side(path, pending)
                    if side == "old":
                        _finish_plan_transaction(pending, commit=False)
                    else:
                        _leave_plan_transaction_for_recovery(pending)
                raise
            if state.get("plan_state") == "confirmed" and not verify_plan_artifact(
                state, payload
            ):
                sync_plan_artifact_lifecycle(state)
                set_persistence_metadata(
                    state,
                    payload,
                    attempted=True,
                    ok=True,
                    outcome="written_invalidated",
                )
                atomic_write(path, state)
            if pending is not None:
                try:
                    _finish_plan_transaction(pending, commit=True)
                except (OSError, PlanArtifactError) as cleanup_error:
                    # State and journal are already new/new.  Preserve the marker so
                    # the next locked hook can finish cleanup deterministically.
                    if (
                        isinstance(cleanup_error, PlanArtifactError)
                        and cleanup_error.code == "content_drift"
                    ):
                        invalidate_plan_authority(
                            state, warning_code="content_drift"
                        )
                        sync_plan_artifact_lifecycle(state)
                        set_persistence_metadata(
                            state,
                            payload,
                            attempted=True,
                            ok=False,
                            outcome="transaction_content_drift",
                        )
                        try:
                            atomic_write(path, state)
                        except OSError as write_error:
                            debug_persistence(
                                payload,
                                path_resolved=True,
                                outcome="transaction_invalidation_write_error",
                                error=write_error,
                            )
                    debug_persistence(
                        payload,
                        path_resolved=True,
                        outcome="transaction_cleanup_pending",
                        error=cleanup_error,
                    )
            debug_persistence(payload, path_resolved=True, outcome="written")
            return state, True
    except TimeoutError as error:
        debug_persistence(payload, path_resolved=True, outcome="lock_timeout", error=error)
        return new_state(payload), False
    except OSError as error:
        debug_persistence(payload, path_resolved=True, outcome="write_error", error=error)
        return new_state(payload), False


def cleanup_old_plugin_versions(
    plugin_root: Path | None = None,
    *,
    skill_paths_verified: bool = False,
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
        expected_cache_root = (
            _codex_home()
            / "plugins"
            / "cache"
            / "workflow-manager"
            / "workflow-manager"
        )
        expected_parent = expected_cache_root.resolve(strict=True)
        if os.path.normcase(str(root.parent)) != os.path.normcase(
            str(expected_parent)
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


def read_host_rollout_records(path_value: Any) -> list[dict[str, Any]]:
    """Read one bounded, regular host rollout without following links."""
    if not path_value:
        return []
    try:
        path = Path(str(path_value))
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_size <= 0
            or info.st_size > HOST_ROLLOUT_RECONCILE_BYTES
        ):
            return []
        with path.open("rb") as stream:
            records = []
            for raw in stream:
                if len(raw) > 1024 * 1024:
                    return []
                item = json.loads(raw)
                if not isinstance(item, dict):
                    return []
                records.append(item)
        return records
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return []


def _safe_rollout_window_id(value: Any) -> str | None:
    text = str(value or "")
    return text if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", text, re.I) else None


def reconcile_host_rollout_compactions(payload: dict[str, Any], state: dict[str, Any]) -> int:
    """Account host-owned compacted/context_compacted pairs at a resume boundary.

    Desktop does not currently dispatch PreCompact/PostCompact for its native
    TUI compaction.  This deliberately accepts only a same-session rollout at
    a supported SessionStart resume/compact boundary; prompt text and payload
    supplied window fields are never consulted.
    """
    if (
        payload.get("hook_event_name") != "SessionStart"
        or str(payload.get("source") or "") not in {"resume", "compact"}
    ):
        return 0
    session_id = safe_label(payload.get("session_id"), 120)
    if not session_id or state.get("session_fingerprint") != stable_hash(session_id):
        return 0
    records = read_host_rollout_records(payload.get("transcript_path"))
    if not records:
        return 0
    metas = []
    for item in records:
        if item.get("type") != "session_meta" or not isinstance(item.get("payload"), dict):
            continue
        meta = item["payload"]
        if meta.get("session_id") == session_id and meta.get("id") == session_id:
            metas.append(meta)
        else:
            return 0
    if len(metas) != 1:
        return 0
    candidates: list[dict[str, Any]] = []
    for index, item in enumerate(records):
        if item.get("type") != "compacted" or not isinstance(item.get("payload"), dict):
            continue
        compacted = item["payload"]
        window_number = safe_int(compacted.get("window_number"))
        window_id = _safe_rollout_window_id(compacted.get("window_id"))
        previous_window_id = _safe_rollout_window_id(compacted.get("previous_window_id"))
        if window_number < 1 or not window_id or not previous_window_id or window_id == previous_window_id:
            return 0
        # The only tolerated bridge record is telemetry between the native
        # compacted event and its context_compacted acknowledgement.
        acknowledgement = None
        for following in records[index + 1 :]:
            if following.get("type") != "event_msg" or not isinstance(following.get("payload"), dict):
                break
            event_type = following["payload"].get("type")
            if event_type == "token_count":
                continue
            if event_type == "context_compacted":
                acknowledgement = following
            break
        if acknowledgement is None:
            return 0
        fingerprint = stable_hash(
            "host-rollout-compaction-v1\0" + canonical_json(
                {
                    "session_id": session_id,
                    "window_number": window_number,
                    "window_id": window_id,
                    "previous_window_id": previous_window_id,
                }
            ),
            32,
        )
        candidates.append(
            {
                "fingerprint": fingerprint,
                "window_number": window_number,
                "window_id": window_id,
                "previous_window_id": previous_window_id,
            }
        )
    if not candidates or len({item["fingerprint"] for item in candidates}) != len(candidates):
        return 0
    candidates.sort(key=lambda item: item["window_number"])
    if any(
        right["window_number"] != left["window_number"] + 1
        or right["previous_window_id"] != left["window_id"]
        for left, right in zip(candidates, candidates[1:])
    ):
        return 0
    existing = [
        item for item in state.get("compactions", [])
        if isinstance(item, dict) and item.get("source") == "host_rollout_reconciled"
    ]
    recorded = {item.get("rollout_compaction_fingerprint") for item in existing}
    new_items = [item for item in candidates if item["fingerprint"] not in recorded]
    if not new_items:
        return 0
    if existing:
        prior = max(existing, key=lambda item: safe_int(item.get("window_number")))
        first = new_items[0]
        if (
            first["window_number"] != safe_int(prior.get("window_number")) + 1
            or first["previous_window_id"] != prior.get("window_id")
        ):
            return 0
    for item in new_items:
        state.setdefault("compactions", []).append(
            {
                "at": utc_now(),
                "phase": "rollout_reconciled",
                "source": "host_rollout_reconciled",
                "trigger": "native",
                "turn_id": safe_label(payload.get("turn_id"), 120) if payload.get("turn_id") else None,
                "rollout_compaction_fingerprint": item["fingerprint"],
                "window_number": item["window_number"],
                "window_id": item["window_id"],
                "previous_window_id": item["previous_window_id"],
                "telemetry": {},
                "objective_meta": state.get("objective", {}),
                "current_stage": current_execution_stage(state),
                "work_difficulty": state.get("work_difficulty", "unknown"),
                "difficulty_decision_id": state.get("difficulty_decision_id"),
                "plan_state": state.get("plan_state", "none"),
                "plan_generation": safe_int(state.get("plan_generation")),
                "plan_digest": state.get("plan_digest"),
                "confirmed_plan_digest": state.get("confirmed_plan_digest"),
                "plan_artifact": _safe_plan_artifact(state.get("plan_artifact")),
                "execution_slices": _safe_execution_slices(state.get("execution_slices")),
                "session_execution_preference": safe_session_execution_preference(state.get("session_execution_preference")),
                "execution_profile_version": state.get("execution_profile_version"),
                "executor_state": state.get("executor_state", "none"),
                "execution_contract_id": state.get("execution_contract_id"),
                "executor_attempt": safe_int(state.get("executor_attempt")),
                "executor_failure_kind": state.get("executor_failure_kind"),
                "executor_review": _safe_executor_review(state.get("executor_review")),
                "reference_acceptance": _safe_reference_acceptance(state.get("reference_acceptance")),
                "last_execution_baseline": _safe_execution_baseline(state.get("last_execution_baseline")),
                "causal_review": _safe_causal_review(state.get("causal_review")),
                "stall": _safe_stall(state.get("stall")),
                "coordination_activity": [],
                "coordination_notices": [],
                "coordination_inbound": [],
                "active_agent_scopes": active_agent_scope_summary(state),
                "recent_successes": [],
                "continuity": quality_continuity(state),
            }
        )
    return len(new_items)


def _host_event_turn_id(event: dict[str, Any]) -> str | None:
    """Return one coherent host-owned turn identifier, never a guessed one."""
    meta = event.get("internal_chat_message_metadata_passthrough") or {}
    top = safe_label(event.get("turn_id"), 120) if event.get("turn_id") else None
    nested = safe_label(meta.get("turn_id"), 120) if meta.get("turn_id") else None
    return top if top and (not nested or top == nested) else nested if nested and not top else None


def transcript_turn_structured_exec_result(path_value: Any, turn_id: str) -> tuple[str, str, str] | None:
    """Return (exact command digest, explicit status, command) for one host exec chain only."""
    calls: dict[str, str] = {}; outputs: dict[str, Any] = {}; count = 0
    for raw in read_transcript_tail(path_value):
        try:
            item = json.loads(raw); event = item.get("payload") or {}
            if _host_event_turn_id(event) != turn_id:
                continue
            if event.get("type") == "custom_tool_call" and event.get("name") == "exec":
                count += 1; calls[str(event.get("call_id") or "")] = str(event.get("input") or "")
            elif event.get("type") == "custom_tool_call_output":
                outputs[str(event.get("call_id") or "")] = event.get("output")
        except Exception:
            continue
    if count != 1 or len(calls) != 1 or set(calls) != set(outputs):
        return None
    source = next(iter(calls.values()))
    match = re.search(r'\bcmd\s*:\s*("(?:[^"\\]|\\.)*")', source, re.S)
    raw_match = re.search(r'\bcmd\s*:\s*String\.raw`([^`]*)`', source, re.S)
    cwd_match = re.search(r'\bworkdir\s*:\s*("(?:[^"\\]|\\.)*")', source, re.S)
    if bool(match) == bool(raw_match) or (raw_match and "${" in raw_match.group(1)):
        return None
    try:
        command = json.loads(match.group(1)) if match else raw_match.group(1)
        cwd = json.loads(cwd_match.group(1)) if cwd_match else ""
    except Exception:
        return None
    output = outputs[next(iter(outputs))]
    texts = [item.get("text") for item in output if isinstance(item, dict) and isinstance(item.get("text"), str)] if isinstance(output, list) else []
    structured = next((json.loads(text) for text in texts if text.lstrip()[:1] in "[{" and isinstance(json.loads(text), dict)), None)
    status = response_status(structured) if structured is not None else "unknown"
    if status == "unknown":
        return None
    digest = stable_hash("host-operation-command-v1\0" + command.replace("\r\n", "\n").replace("\r", "\n") + "\0" + cwd, 32)
    return digest, status, command


def reconcile_unknown_operations_from_transcript(payload: dict[str, Any], state: dict[str, Any]) -> None:
    """Upgrade only new digest-bearing operations from one exact host call chain."""
    turn_id = safe_label(payload.get("turn_id"), 120) if payload.get("turn_id") else None
    if not turn_id:
        return
    # apply_patch accepts only a deliberately tiny JS shape: a single JSON
    # string literal (directly or through one const).  No expressions, joins,
    # templates, or evaluation are accepted.
    patch_sources: list[tuple[str, str]] = []
    turn_events: list[dict[str, Any]] = []
    calls: dict[str, str] = {}
    call_count = 0
    outputs: dict[str, Any] = {}
    for raw in read_transcript_tail(payload.get("transcript_path")):
        try:
            item = json.loads(raw); event = item.get("payload") or {}
            if _host_event_turn_id(event) != turn_id:
                continue
            turn_events.append(event)
            if event.get("type") == "custom_tool_call" and event.get("name") == "exec":
                call_count += 1
                calls[str(event.get("call_id") or "")] = str(event.get("input") or "")
                source = str(event.get("input") or "")
                direct = re.search(r"tools\.apply_patch\(\s*(\"(?:[^\"\\]|\\.)*\")\s*\)", source, re.S)
                bound = re.search(r"const\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(\"(?:[^\"\\]|\\.)*\")\s*;[\s\S]*?tools\.apply_patch\(\s*\1\s*\)", source)
                literal = direct.group(1) if direct else bound.group(2) if bound else None
                if literal:
                    patch_sources.append((str(event.get("call_id") or ""), json.loads(literal)))
            elif event.get("type") == "custom_tool_call_output":
                outputs[str(event.get("call_id") or "")] = event.get("output")
        except Exception:
            continue
    patch_ops = [op for op in state.get("operations", []) if (op.get("status") == "unknown" or (op.get("status") in SUCCESS_STATUSES and not op.get("reconciliation_source"))) and op.get("host_event_turn_id") == turn_id and op.get("host_input_digest")]
    if len(patch_sources) == 1:
        patch_call_id, patch_source = patch_sources[0]
        patch_digest = host_patch_digest(patch_source)
        matches = [op for op in patch_ops if op.get("host_input_digest") == patch_digest]
        exact_patch_match = bool(matches)
        current = current_execution_slice(state) or {}
        if not matches and safe_int(state.get("schema_version")) == 25:
            legacy = [op for op in patch_ops if normalized_key(op.get("tool")) == "applypatch" and op.get("executor_agent_id") and op.get("execution_contract_id") == state.get("execution_contract_id") and op.get("slice_id") == current.get("id") and op.get("slice_contract_id") == slice_contract_id(state)]
            if len(legacy) == 1:
                matches = legacy
                matches[0]["legacy_host_input_digest"] = matches[0].get("host_input_digest")
                matches[0]["host_input_digest"] = patch_digest
                matches[0]["reconciliation_source"] = "legacy_unique_turn_patch_event_v1"
        if len(matches) == 1:
            # Host event is authoritative only when exactly one bounded success
            # event is present in this turn; missing/ambiguous remains unknown.
            call_index = next((index for index, event in enumerate(turn_events) if event.get("type") == "custom_tool_call" and event.get("call_id") == patch_call_id), -1)
            output_index = next((index for index, event in enumerate(turn_events[call_index + 1 :], call_index + 1) if event.get("type") == "custom_tool_call_output" and event.get("call_id") == patch_call_id), -1)
            successes = [event for event in turn_events[call_index + 1 : output_index] if event.get("type") == "patch_apply_end" and event.get("success") is True and str(event.get("status") or "").lower() == "completed"]
            if len(successes) == 1:
                matches[0]["status"] = "ok"; matches[0]["category"] = "implementation"
                if exact_patch_match:
                    matches[0]["reconciliation_source"] = "host_rollout_exact_patch_digest_v1"
    result = transcript_turn_structured_exec_result(payload.get("transcript_path"), turn_id)
    if not result:
        return
    digest, status, command = result
    candidates = [op for op in state.get("operations", []) if op.get("status") == "unknown" and op.get("host_event_turn_id") == turn_id and op.get("host_input_digest") == digest]
    if len(candidates) != 1:
        return
    op = candidates[0]
    op["status"] = status
    op["category"] = command_category({"tool_name": op.get("tool")}, command)


def transcript_has_exact_parent_review_pass(
    path_value: Any, contract_id: str, slice_id: str
) -> bool:
    """Accept only the host transcript's latest assistant final marker."""
    marker = f"EXECUTION_REVIEW execution_contract_id={contract_id} slice_id={slice_id} outcome=passed"
    for raw in reversed(read_transcript_tail(path_value)):
        try:
            item = json.loads(raw)
            payload = item.get("payload") or {}
            if item.get("type") != "response_item" or payload.get("type") != "message":
                continue
            if payload.get("role") != "assistant" or payload.get("phase") != "final_answer":
                continue
            content = payload.get("content")
            if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict):
                return False
            return str(content[0].get("text") or "").rstrip("\r\n") == marker
        except Exception:
            continue
    return False


def latest_parent_review_host_success(path_value: Any, contract_id: str, slice_id: str) -> tuple[str, str] | None:
    """Bind a final parent-pass marker to its sole structured host exec result."""
    marker = f"EXECUTION_REVIEW execution_contract_id={contract_id} slice_id={slice_id} outcome=passed"
    for raw in reversed(read_transcript_tail(path_value)):
        try:
            item = json.loads(raw); payload = item.get("payload") or {}
            if item.get("type") != "response_item" or payload.get("type") != "message" or payload.get("role") != "assistant" or payload.get("phase") != "final_answer":
                continue
            content = payload.get("content")
            if not (isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict) and str(content[0].get("text") or "").rstrip("\r\n") == marker):
                return None
            turn_id = _host_event_turn_id(payload)
            if not turn_id:
                return None
            result = transcript_turn_structured_exec_result(path_value, turn_id)
            if not result or result[1] not in SUCCESS_STATUSES:
                return None
            return turn_id, result[0]
        except Exception:
            continue
    return None


def reconcile_current_parent_review_on_resume(payload: dict[str, Any], state: dict[str, Any]) -> None:
    current = current_execution_slice(state) or {}
    candidates = [op for op in state.get("operations", []) if op.get("status") == "unknown" and op.get("category") == "verification" and op.get("executor_agent_id") is None and op.get("execution_contract_id") == state.get("execution_contract_id") and op.get("slice_id") == current.get("id") and op.get("slice_contract_id") == slice_contract_id(state) and op.get("host_input_digest") and op.get("host_event_turn_id")]
    if len(candidates) == 1:
        reconcile_unknown_operations_from_transcript({**payload, "turn_id": candidates[0]["host_event_turn_id"]}, state)


def reconcile_current_executor_rollout_on_resume(payload: dict[str, Any], state: dict[str, Any]) -> None:
    """Bounded child-rollout repair for current executor operations only."""
    current = current_execution_slice(state) or {}; contract = state.get("execution_contract_id")
    starts = [item for item in state.get("subagents", []) if item.get("event") == "start" and item.get("role") == "confirmed_executor" and item.get("contract_id") == contract and item.get("slice_id") == current.get("id") and item.get("agent_id") and item.get("at")]
    if len(starts) != 1:
        return
    start = starts[0]; agent = safe_label(start.get("agent_id"), 120); date = str(start.get("at"))[:10]
    if not agent or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date): return
    directory = _codex_home() / "sessions" / date.replace("-", "/")
    candidates = [p for p in directory.glob(f"rollout-*{agent}.jsonl") if p.is_file() and not p.is_symlink()]
    if len(candidates) != 1: return
    records = read_host_rollout_records(candidates[0]); metas=[x.get("payload") for x in records if x.get("type")=="session_meta" and isinstance(x.get("payload"),dict)]
    parent_session = safe_label(payload.get("session_id"), 120) if payload.get("session_id") else None
    if len(metas) != 1 or metas[0].get("id") != agent or not parent_session or metas[0].get("session_id") != parent_session: return
    for op in state.get("operations", []):
        if isinstance(op,dict) and op.get("status")=="unknown" and op.get("executor_agent_id")==agent and op.get("execution_contract_id")==contract and op.get("slice_id")==current.get("id") and op.get("host_event_turn_id") and op.get("host_input_digest"):
            reconcile_unknown_operations_from_transcript({"turn_id":op["host_event_turn_id"],"transcript_path":str(candidates[0])},state)


def transcript_has_exact_compaction_gate(path_value: Any, session_id: str, window_number: int, slice_id: str) -> bool:
    marker = f"COMPACTION_GATE_READY session_id={session_id} window_number={window_number} slice_id={slice_id}"
    records = read_host_rollout_records(path_value)
    if not records:
        return False
    metas = [item.get("payload") for item in records if item.get("type") == "session_meta" and isinstance(item.get("payload"), dict)]
    if len(metas) != 1 or metas[0].get("session_id") != session_id or metas[0].get("id") != session_id:
        return False
    for item in reversed(records):
        body = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        if item.get("type") != "response_item" or body.get("type") != "message" or body.get("role") != "assistant" or body.get("phase") != "final_answer":
            continue
        content = body.get("content")
        return bool(isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict) and content[0].get("text") == marker)
    return False


def resume_compaction_gate_misclassification_once(payload: dict[str, Any], state: dict[str, Any]) -> bool:
    """Repair one exact confirmed-plan invalidation caused by a forbidden-action prompt.

    The checkpoint itself and the host's final gate line are required.  Work on
    a copy first so any failed condition leaves the live state untouched.
    """
    if payload.get("hook_event_name") != "SessionStart" or str(payload.get("source") or "") != "resume":
        return False
    session_id = safe_label(payload.get("session_id"), 120)
    if not session_id or state.get("session_fingerprint") != stable_hash(session_id):
        return False
    checkpoint = next((item for item in reversed(state.get("compactions", [])) if isinstance(item, dict) and item.get("source") == "host_rollout_reconciled"), None)
    if not checkpoint:
        return False
    snapshot_slices = _safe_execution_slices(checkpoint.get("execution_slices"))
    current = current_execution_slice({"execution_slices": snapshot_slices})
    contract = safe_fingerprint(checkpoint.get("execution_contract_id"))
    objective = safe_metadata(checkpoint.get("objective_meta"))
    if not (
        checkpoint.get("plan_state") == "confirmed"
        and checkpoint.get("executor_state") == "spawn_required"
        and checkpoint.get("confirmed_plan_digest") == checkpoint.get("plan_digest")
        and contract and objective.get("fingerprint") and checkpoint.get("difficulty_decision_id")
        and current and current.get("status") == "pending"
        and safe_int(checkpoint.get("window_number")) > 0
        and state.get("plan_state") == "analyzing"
        and not state.get("plan_digest") and not state.get("confirmed_plan_digest")
        and not state.get("execution_contract_id") and state.get("executor_state") == "none"
        and canonical_json(_safe_execution_slices(state.get("execution_slices"))) == canonical_json(snapshot_slices)
    ):
        return False
    fingerprint = stable_hash(f"compaction-gate-resume-repair-v1\0{checkpoint.get('rollout_compaction_fingerprint')}\0{contract}", 32)
    if any(isinstance(item, dict) and item.get("kind") == "compaction_gate_resume_repair" and item.get("fingerprint") == fingerprint for item in state.get("guards", [])):
        return False
    checkpoint_at = str(checkpoint.get("at") or "")
    if not checkpoint_at:
        return False
    if any(str(item.get("at") or "") > checkpoint_at and item.get("executor_agent_id") for item in state.get("operations", []) if isinstance(item, dict)):
        return False
    if any(str(item.get("at") or "") > checkpoint_at and item.get("category") in {"implementation", "build_package", "delivery_device"} for item in state.get("operations", []) if isinstance(item, dict)):
        return False
    if any(str(item.get("at") or "") > checkpoint_at and item.get("event") in {"request", "start", "stop"} for item in state.get("subagents", []) if isinstance(item, dict)):
        return False
    if not transcript_has_exact_compaction_gate(payload.get("transcript_path"), session_id, safe_int(checkpoint.get("window_number")), current["id"]):
        return False
    assessor_groups: dict[str, list[dict[str, Any]]] = {}
    for item in state.get("subagents", []):
        if isinstance(item, dict) and item.get("role") == "high_assessor" and item.get("objective_fingerprint") == objective["fingerprint"] and item.get("contract_id"):
            assessor_groups.setdefault(str(item["contract_id"]), []).append(item)
    valid_assessors = []
    for binding, items in assessor_groups.items():
        requests = [item for item in items if item.get("event") == "request"]
        starts = [item for item in items if item.get("event") == "start"]
        if len(requests) != 1 or len(starts) != 1:
            continue
        request, started = requests[0], starts[0]
        if not (request.get("host_accepted") is True and started.get("start_observed") == "full" and request.get("attempt") == 1 and request.get("fork_turns") == "1" and request.get("model") and request.get("reasoning_effort")):
            continue
        valid_assessors.append((binding, request, started))
    if len(valid_assessors) != 1:
        return False
    assessor_binding, assessor_request, assessor_started = valid_assessors[0]
    candidate = json.loads(json.dumps(state))
    candidate.update({
        "objective": objective,
        "difficulty_decision_id": checkpoint.get("difficulty_decision_id"),
        "plan_state": "confirmed", "plan_generation": safe_int(checkpoint.get("plan_generation")), "plan_digest": checkpoint.get("plan_digest"),
        "plan_objective_fingerprint": objective["fingerprint"], "plan_difficulty_decision_id": checkpoint.get("difficulty_decision_id"),
        "confirmed_plan_digest": checkpoint.get("confirmed_plan_digest"),
        "plan_artifact": _safe_plan_artifact(checkpoint.get("plan_artifact")),
        "execution_slices": snapshot_slices, "execution_profile_version": checkpoint.get("execution_profile_version"),
        "execution_contract_id": contract, "executor_state": "spawn_required", "executor_agent_id": None,
        "executor_attempt": safe_int(checkpoint.get("executor_attempt")), "executor_failure_kind": None,
        "model_profile": confirmed_executor_model_profile(candidate),
        "assessor_generation": 1, "assessor_binding_id": assessor_binding, "assessor_state": "hard_plan_ready", "assessor_attempt": 1,
        "assessor_agent_id": None, "assessor_model": assessor_request["model"], "assessor_reasoning_effort": assessor_request["reasoning_effort"], "assessor_fork_turns": "1",
        "assessor_input_fingerprint": objective["fingerprint"], "assessor_failure_kind": None, "assessor_observed_effective": True,
        "assessor_observed_model": assessor_started.get("model") or assessor_request["model"], "assessor_observed_reasoning_effort": assessor_started.get("reasoning_effort") or assessor_request["reasoning_effort"], "assessor_start_observed": "full", "assessor_observation_source": assessor_started.get("observation_source"),
    })
    if not verify_plan_artifact(candidate, payload) or execution_contract_id(candidate) != contract:
        return False
    candidate.setdefault("guards", []).append({"at": utc_now(), "turn_id": safe_label(payload.get("turn_id"), 120) if payload.get("turn_id") else None, "kind": "compaction_gate_resume_repair", "action": "advise", "fingerprint": fingerprint})
    state.clear(); state.update(candidate)
    return True


def current_slice_host_change_digest(state: dict[str, Any]) -> str | None:
    current = current_execution_slice(state) or {}; contract = state.get("execution_contract_id")
    facts=[{k:op.get(k) for k in ("fingerprint","host_input_digest","legacy_host_input_digest","reconciliation_source","host_event_turn_id","status")} for op in state.get("operations",[]) if isinstance(op,dict) and op.get("execution_contract_id")==contract and op.get("slice_id")==current.get("id") and op.get("slice_contract_id")==slice_contract_id(state) and op.get("executor_agent_id") and op.get("status") in SUCCESS_STATUSES and op.get("category") in {"implementation","build_package","delivery_device"} and op.get("reconciliation_source")]
    return stable_hash("current-slice-host-change-v1\0"+canonical_json(facts),32) if facts else None


def resume_failed_review_evidence_once(payload: dict[str, Any], state: dict[str, Any]) -> bool:
    """One bounded repair for the known host Stop-status omission; never trusts child text."""
    review = _safe_executor_review(state.get("executor_review"))
    current = current_execution_slice(state)
    contract = safe_fingerprint(state.get("execution_contract_id"))
    if not (
        state.get("executor_state") == "recovery_required"
        and state.get("executor_failure_kind") == "verification_failed"
        and safe_int(state.get("executor_attempt")) == 1
        and review.get("status") == "failed"
        and review.get("attempt") == 1
        and contract
        and current
        and current.get("status") == "pending"
        and review.get("execution_contract_id") == contract
        and review.get("slice_id") == current.get("id")
        and review.get("slice_contract_id") == slice_contract_id(state)
        and review.get("candidate_result_fingerprint")
        and review.get("candidate_evidence_digest")
    ):
        return False
    guards = state.setdefault("guards", [])
    change_digest = current_slice_host_change_digest(state)
    repair_fingerprint = stable_hash(f"{contract}\0{current['id']}\0{review.get('candidate_result_fingerprint')}\0{change_digest or ''}", 32) if change_digest else stable_hash(f"{contract}\0{current['id']}\0{review.get('candidate_result_fingerprint')}", 32)
    if any(item.get("kind") == "verification_evidence_resume_repair" and item.get("fingerprint") == repair_fingerprint for item in guards if isinstance(item, dict)):
        return False
    host_success = latest_parent_review_host_success(payload.get("transcript_path"), contract, current["id"])
    if not host_success:
        return False
    host_turn, host_digest = host_success
    parent_ops = [op for op in state.get("operations", []) if op.get("category") == "verification" and op.get("executor_agent_id") is None and op.get("execution_contract_id") == contract and op.get("slice_id") == current.get("id") and op.get("slice_contract_id") == slice_contract_id(state)]
    successes = [op for op in parent_ops if op.get("status") in SUCCESS_STATUSES and op.get("host_input_digest") == host_digest]
    unbound = [op for op in state.get("operations", []) if op.get("category") == "verification" and op.get("executor_agent_id") is None and op.get("status") in SUCCESS_STATUSES and op.get("host_event_turn_id") == host_turn and op.get("host_input_digest") == host_digest and op.get("execution_contract_id") is None and op.get("slice_id") is None and op.get("slice_contract_id") is None]
    unbound_op = unbound[0] if not successes and len(unbound) == 1 else None
    if unbound_op is not None:
        successes = [unbound_op]
    if len(successes) != 1:
        return False
    parent_index = state.get("operations", []).index(successes[0])
    if any(op.get("executor_agent_id") or op.get("category") in {"implementation", "build_package", "delivery_device"} for op in state.get("operations", [])[parent_index + 1:] if isinstance(op, dict)):
        return False
    if unbound_op is not None:
        unbound_op["execution_contract_id"] = contract; unbound_op["slice_id"] = current["id"]; unbound_op["slice_contract_id"] = slice_contract_id(state)
    review["status"] = "review_required"
    review["review_evidence_digest"] = None
    review["at"] = utc_now()
    state["executor_review"] = review
    state["executor_state"] = "verification_required"
    state["executor_failure_kind"] = None
    state["model_profile"] = confirmed_executor_model_profile(state)
    guards.append(
        {
            "at": utc_now(),
            "turn_id": safe_label(payload.get("turn_id"), 120) if payload.get("turn_id") else None,
            "kind": "verification_evidence_resume_repair",
            "action": "advise",
            "fingerprint": repair_fingerprint,
        }
    )
    return True


def resume_failed_parent_probe_once(state: dict[str, Any], payload: dict[str, Any] | None = None) -> bool:
    review=_safe_executor_review(state.get("executor_review")); current=current_execution_slice(state) or {}; contract=state.get("execution_contract_id"); evidence=review.get("review_evidence_digest")
    if not (state.get("executor_state")=="recovery_required" and state.get("executor_failure_kind")=="verification_failed" and safe_int(state.get("executor_attempt"))==1 and review.get("status")=="failed" and review.get("attempt")==1 and evidence and review.get("candidate_result_fingerprint") and review.get("candidate_evidence_digest") and current.get("status")=="pending" and safe_fingerprint(contract) and contract==execution_contract_id(state) and review.get("execution_contract_id")==contract and review.get("slice_id")==current.get("id") and review.get("slice_contract_id")==slice_contract_id(state)):
        return False
    fp=stable_hash(str(evidence),32); guards=state.setdefault("guards",[])
    if any(g.get("kind")=="parent_review_probe_correction" and g.get("fingerprint")==fp for g in guards if isinstance(g,dict)): return False
    ops=[op for op in state.get("operations",[]) if op.get("execution_contract_id")==contract and op.get("slice_id")==current.get("id") and op.get("slice_contract_id")==slice_contract_id(state)]
    parent=next((op for op in reversed(ops) if op.get("category")=="verification" and op.get("executor_agent_id") is None and op.get("status","").startswith("error") and op.get("host_input_digest") and op.get("host_event_turn_id")),None)
    parent_index=ops.index(parent) if parent else -1
    if not parent or any(op.get("executor_agent_id") or (op.get("status") in SUCCESS_STATUSES and op.get("category") in {"implementation","build_package","delivery_device","evidence"}) for op in ops[parent_index+1:]): return False
    review["status"]="review_required"; review["review_evidence_digest"]=None; review["at"]=utc_now(); state["executor_review"]=review; state["executor_state"]="verification_required"; state["executor_failure_kind"]=None; state["model_profile"]=confirmed_executor_model_profile(state); guards.append({"at":utc_now(),"turn_id":safe_label((payload or {}).get("turn_id"),120) if (payload or {}).get("turn_id") else None,"kind":"parent_review_probe_correction","action":"advise","fingerprint":fp}); return True


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


def start_turn_observation(payload: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """Read only an exact host turn_context; never trust request or child text."""
    turn_id = safe_label(payload.get("turn_id"), 120) if payload.get("turn_id") else None
    payload_model = safe_label(payload.get("model"), 80) if payload.get("model") else None
    if not turn_id or not payload_model:
        return None, None, None
    for raw in reversed(read_transcript_tail(payload.get("transcript_path"))):
        try:
            item = json.loads(raw)
        except Exception:
            continue
        if not isinstance(item, dict):
            continue
        envelope_type = item.get("type")
        envelope_payload = item.get("payload")
        # The current host writes a top-level turn_context whose payload owns
        # the profile.  Retain only the evidenced older event_msg envelope.
        if envelope_type == "turn_context" and isinstance(envelope_payload, dict):
            context = envelope_payload
            source = "transcript_turn_context_effort"
            effort_key = "effort"
        elif (
            envelope_type == "event_msg"
            and isinstance(envelope_payload, dict)
            and envelope_payload.get("type") == "turn_context"
        ):
            context = envelope_payload
            source = "transcript_event_msg_reasoning_effort"
            effort_key = "reasoning_effort"
        else:
            continue
        context_turn = safe_label(context.get("turn_id"), 120) if context.get("turn_id") else None
        if context_turn != turn_id:
            continue
        transcript_model = safe_label(context.get("model"), 80) if context.get("model") else None
        if transcript_model != payload_model:
            return transcript_model, None, "transcript_turn_context_model_mismatch"
        effort = safe_label(context.get(effort_key), 24) if context.get(effort_key) else None
        return transcript_model, effort, source
    return None, None, None


def start_observation_status(model: str | None, effort: str | None, source: str | None) -> str:
    if source and source.endswith("_mismatch"):
        return "mismatch"
    if model and effort:
        return "full"
    if model or effort:
        return "partial"
    return "absent"


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
    (
        "work_file_artifact_contract",
        r"(?:(?<!不要)(?<!禁止)(?<!无需)(?:创建|写入|修改|编辑|验证|测试)|"
        r"(?<!do not )(?<!don't )(?<!never )\b(?:creat(?:e|es|ing)|write|modify|edit|verify|test)\b)"
        r".{0,48}(?:文件|产物|\b(?:files?|artifacts?)\b|\.[a-z0-9]{1,12}\b)",
    ),
    (
        "work_explicit_engineering_contract",
        r"(?:\bwork\s*/\s*(?:hard|simple)\b|\b(?:engineering|plugin|workflow)\s+"
        r"(?:acceptance|verification|test|contract)\b|工程(?:验收|验证|测试|合同))",
    ),
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
    # A prohibition is a boundary, not an engineering deliverable. Remove only
    # bounded negative file/artifact clauses before evaluating positive Work
    # signals; later affirmative clauses remain available to the classifier.
    work_scan = re.sub(
        r"\b(?:do not|don't|never)\s+"
        r"(?:creat(?:e|es|ing)|write|modify|edit|verify|test)"
        r"(?:\s+(?:or|and)\s+(?:creat(?:e|es|ing)|write|modify|edit|verify|test))*"
        r"\s+(?:any\s+)?(?:[a-z0-9_-]+\s+){0,3}(?:files?|artifacts?)\b",
        " ",
        lower,
        flags=re.I,
    )
    work_scan = re.sub(
        r"(?:不要|禁止|无需)(?:创建|写入|修改|编辑|验证|测试)"
        r"(?:(?:或|和|、)(?:创建|写入|修改|编辑|验证|测试))*"
        r"(?:任何|任意|这些|该)?[^，。；;]{0,8}(?:文件|产物)",
        " ",
        work_scan,
    )
    daily_codes = [code for code, pattern in DAILY_EXACT_PATTERNS if re.search(pattern, lower, re.I)]
    work_codes = [code for code, pattern in WORK_STRONG_PATTERNS if re.search(pattern, work_scan, re.I)]
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
    ("hard_host_continuity", r"(?:host\s+compaction|真实(?:宿主)?压缩|压缩).{0,96}(?:same[- ]session|同一会话|同会话|resume|恢复)"),
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


def identity_preflight_prompt(prompt: str) -> bool:
    """Recognize an explicit host-local activation/identity probe.

    This is intentionally narrow: the user must name the preflight purpose and
    independently prohibit both tool use and child creation. Incidental words
    such as ``Workflow Manager``, ``verify``, or ``Hard`` must not turn the
    probe itself into assessed engineering work.
    """
    normalized = re.sub(r"\s+", " ", str(prompt or "").strip())
    lower = normalized.lower()
    purpose = bool(
        re.search(r"\b(?:activation|identity)\s+preflight\b", lower)
        or re.search(r"(?:激活|身份)(?:预检|检查)", normalized)
        or "final_activation_preflight" in lower
    )
    no_tools = bool(
        re.search(r"\b(?:do not|don't|never|no)\b.{0,20}\btools?\b", lower)
        or re.search(r"(?:严禁|禁止|不要|无需).{0,12}(?:调用|使用).{0,8}(?:任何)?\s*tool", normalized, re.I)
    )
    no_child = bool(
        re.search(r"\b(?:do not|don't|never|no)\b.{0,24}\b(?:child|subagent|agent)\b", lower)
        or re.search(r"(?:严禁|禁止|不要|无需).{0,12}(?:启动|创建|调用).{0,8}(?:任何)?\s*(?:child|子智能体|子代理)", normalized, re.I)
    )
    return bool(purpose and no_tools and no_child)


def identity_preflight_route(prompt: str) -> dict[str, Any]:
    fingerprint = stable_hash(re.sub(r"\s+", " ", prompt.strip()), 32)
    return decorate_route(
        {
            "task_domain": "daily",
            "domain_confidence": "high",
            "domain_rule_codes": ["host_identity_preflight"],
            "model_profile": "current",
            "domain_classifier_version": DOMAIN_CLASSIFIER_VERSION,
            "domain_decision_id": stable_hash(
                f"{DOMAIN_CLASSIFIER_VERSION}\0identity-preflight\0{fingerprint}", 24
            ),
            "work_difficulty": "not_applicable",
            "difficulty_confidence": "high",
            "difficulty_rule_codes": ["identity_preflight_not_work"],
            "difficulty_classifier_version": DIFFICULTY_CLASSIFIER_VERSION,
            "difficulty_decision_id": stable_hash(
                f"{DIFFICULTY_CLASSIFIER_VERSION}\0identity-preflight\0{fingerprint}", 24
            ),
            "label": "direct",
            "score": 0,
            "future_token_range": "under 4k",
            "recommended_agent_cap": 0,
            "parallel_signal": False,
            "delegation_gate": "closed",
            "readiness_signal": "none",
            "dependency_signal": "none",
            "meta_delegation": True,
            "delegation_opt_out": True,
            "lane_signal": "none",
            "phase_hints": [],
            "dependency_hint": None,
            "route_source": "identity_preflight",
        }
    )


def classify_prompt(prompt: str) -> dict[str, Any]:
    normalized = prompt.strip()
    if identity_preflight_prompt(normalized):
        return identity_preflight_route(normalized)
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
COMMAND_REQUEST_WRAPPERS = ("args", "arguments", "input", "tool_input")
COMMAND_REQUEST_MAX_DEPTH = 4
COMMAND_REQUEST_MAX_NODES = 24
COMMAND_REQUEST_MAX_LIST_ITEMS = 8


def _normalize_structured_command_cwd(value: str, payload_cwd: Any) -> tuple[str | None, str | None]:
    candidate = value.strip()
    if not candidate:
        return None, "command cwd/workdir must be non-empty"
    normalized = candidate.replace("\\", "/")
    if (
        normalized.startswith("/")
        or normalized.startswith("//")
        or re.match(r"(?i)^[a-z]:/", normalized)
    ):
        return candidate, None

    base = str(payload_cwd or "").strip()
    if not base:
        return None, "relative command cwd/workdir has no payload cwd base"
    try:
        base_path = Path(base)
        if not base_path.is_absolute():
            return None, "relative command cwd/workdir has an ambiguous payload cwd base"
        resolved_base = base_path.resolve(strict=True)
        if not resolved_base.is_dir():
            return None, "relative command cwd/workdir base is not a real directory"
        resolved = (resolved_base / candidate).resolve(strict=True)
        if not resolved.is_dir():
            return None, "relative command cwd/workdir is not a real directory"
        return str(resolved), None
    except OSError:
        return None, "relative command cwd/workdir cannot be resolved from payload cwd"


def command_request_resolution(payload: dict[str, Any]) -> dict[str, Any]:
    """Bind command and workdir/cwd from one structured tool-input leaf."""
    tool_input = payload.get("tool_input")
    tool = normalized_key(payload.get("tool_name"))
    if isinstance(tool_input, str):
        raw_command_tool = tool in {"bash", "shell", "execcommand"} or tool.endswith("execcommand")
        return {
            "command": tool_input if raw_command_tool else "",
            "cwd": payload.get("cwd"),
            "error": None,
        }

    pending: list[tuple[Any, int]] = [(tool_input, 0)]
    seen: set[int] = set()
    leaves: list[tuple[str, Any]] = []
    errors: list[str] = []
    observed_commands: list[str] = []
    nodes = 0
    while pending:
        value, depth = pending.pop(0)
        nodes += 1
        if nodes > COMMAND_REQUEST_MAX_NODES:
            errors.append("command arguments exceed bounded node limit")
            break
        if depth > COMMAND_REQUEST_MAX_DEPTH:
            errors.append("command arguments exceed bounded depth limit")
            break
        if isinstance(value, list):
            if len(value) > COMMAND_REQUEST_MAX_LIST_ITEMS:
                errors.append("command arguments exceed bounded list limit")
                continue
            pending.extend((item, depth + 1) for item in value)
            continue
        if not isinstance(value, dict):
            continue
        identity = id(value)
        if identity in seen:
            continue
        seen.add(identity)

        command_values = [
            candidate.strip()
            for key in ("command", "cmd")
            for candidate in (value.get(key),)
            if isinstance(candidate, str) and candidate.strip()
        ]
        observed_commands.extend(command_values)
        structured_cwd_keys = [key for key in ("workdir", "cwd") if key in value]
        if structured_cwd_keys and not command_values:
            errors.append("structured cwd/workdir is outside the command leaf")
        if len(set(command_values)) > 1:
            errors.append("command leaf contains conflicting command/cmd aliases")
        elif command_values:
            cwd_values: list[str] = []
            invalid_cwd = False
            for key in ("workdir", "cwd"):
                if key not in value:
                    continue
                candidate = value.get(key)
                if not isinstance(candidate, str):
                    invalid_cwd = True
                    continue
                cwd_values.append(candidate.strip())
            if invalid_cwd:
                errors.append("command cwd/workdir must be scalar text")
            elif len(set(cwd_values)) > 1:
                errors.append("command leaf contains conflicting cwd/workdir aliases")
            else:
                cwd = payload.get("cwd")
                if cwd_values:
                    cwd, cwd_error = _normalize_structured_command_cwd(
                        cwd_values[0], payload.get("cwd")
                    )
                    if cwd_error:
                        errors.append(cwd_error)
                leaves.append(
                    (
                        command_values[0],
                        cwd,
                    )
                )
        for key in COMMAND_REQUEST_WRAPPERS:
            child = value.get(key)
            # Workdir/cwd embedded in JSON or another scalar is data, not executable structure.
            if isinstance(child, (dict, list)):
                pending.append((child, depth + 1))

    if not errors and len(leaves) > 1:
        errors.append("command arguments contain multiple command leaves")
    if len(leaves) == 1 and not errors:
        command, cwd = leaves[0]
    else:
        command = "\n".join(dict.fromkeys(observed_commands))
        cwd = payload.get("cwd")
    return {"command": command, "cwd": cwd, "error": errors[0] if errors else None}


def extract_command(payload: dict[str, Any]) -> str:
    return str(command_request_resolution(payload).get("command") or "")


def effective_tool_cwd(payload: dict[str, Any]) -> Any:
    return command_request_resolution(payload).get("cwd")


def is_subagent_spawn_tool(payload: dict[str, Any]) -> bool:
    name = normalized_key(payload.get("tool_name"))
    return (
        name == "agent"
        or name.endswith("spawnagent")
        or name in {"subagentspawn", "createsubagent", "spawnsubagent"}
    )


SUBAGENT_REQUEST_MAX_DEPTH = 6
SUBAGENT_REQUEST_MAX_NODES = 48
SUBAGENT_REQUEST_MAX_LIST_ITEMS = 16
SUBAGENT_REQUEST_MAX_BYTES = 64 * 1024
SUBAGENT_REQUEST_WRAPPERS = ("args", "arguments", "input", "tool_input", "content")
OPAQUE_V2_MESSAGE_RE = re.compile(r"gAAAAA[A-Za-z0-9_-]{80,65520}={0,2}")
SUBAGENT_REQUEST_ALIASES = {
    "task_name": ("task_name", "taskName", "name", "description"),
    "message": ("message", "prompt", "task"),
    "model": ("model",),
    "reasoning_effort": ("reasoning_effort", "reasoningEffort"),
    "fork_turns": ("fork_turns", "forkTurns"),
    "target": ("target",),
}


def assessor_task_name_has_intent(value: Any) -> bool:
    normalized = normalized_key(value)
    return normalized == "highassessor" or bool(
        re.fullmatch(r"highassessor[0-9a-f]{48}v1", normalized)
    )


def _json_container_text(value: str) -> bool:
    stripped = value.strip()
    return bool(stripped and stripped[0] in "{[")


def _content_block_text(value: Any) -> tuple[str | None, str | None]:
    """Read a finite text/content representation without following arbitrary keys."""
    pending: list[tuple[Any, int]] = [(value, 0)]
    texts: list[str] = []
    nodes = 0
    byte_count = 0
    while pending:
        current, depth = pending.pop(0)
        nodes += 1
        if nodes > SUBAGENT_REQUEST_MAX_NODES or depth > SUBAGENT_REQUEST_MAX_DEPTH:
            return None, "content blocks exceed bounded depth/node limits"
        if isinstance(current, str):
            encoded = current.encode("utf-8", errors="replace")
            byte_count += len(encoded)
            if byte_count > SUBAGENT_REQUEST_MAX_BYTES:
                return None, "content blocks exceed bounded byte limit"
            if current.strip():
                texts.append(current.strip())
            continue
        if isinstance(current, list):
            if len(current) > SUBAGENT_REQUEST_MAX_LIST_ITEMS:
                return None, "content blocks exceed bounded list limit"
            pending.extend((item, depth + 1) for item in current)
            continue
        if not isinstance(current, dict):
            return None, "content blocks must contain text objects"
        block_type = normalized_key(current.get("type"))
        if block_type and block_type not in {"text", "inputtext", "outputtext"}:
            return None, "content block type is not textual"
        if "text" in current:
            pending.append((current.get("text"), depth + 1))
        elif "content" in current:
            pending.append((current.get("content"), depth + 1))
        else:
            return None, "content block lacks text"
    return ("\n".join(texts) if texts else None), None


def _request_alias_value(field: str, value: Any) -> tuple[str | None, str | None]:
    if field == "message" and isinstance(value, (list, dict)):
        return _content_block_text(value)
    if value in (None, ""):
        return None, None
    if not isinstance(value, (str, int)):
        return None, f"{field} must be scalar text"
    normalized = str(value).strip()
    return (normalized or None), None


def _function_tool_request_wrapper(candidate: dict[str, Any]) -> bool:
    return bool(
        normalized_key(candidate.get("type"))
        in {"function", "tool", "functioncall", "toolcall"}
        and "arguments" in candidate
    )


def _canonical_request_leaf(
    candidate: dict[str, Any],
) -> tuple[dict[str, str], str | None, int]:
    leaf: dict[str, str] = {}
    consumed_bytes = 0
    for field, aliases in SUBAGENT_REQUEST_ALIASES.items():
        observed: list[str] = []
        for alias in aliases:
            if alias not in candidate:
                continue
            normalized, error = _request_alias_value(field, candidate.get(alias))
            if error:
                return {}, error, consumed_bytes
            if normalized is not None:
                observed.append(normalized)
                consumed_bytes += len(normalized.encode("utf-8", errors="replace"))
        if len(set(observed)) > 1:
            return {}, f"conflicting {field} aliases", consumed_bytes
        if observed:
            leaf[field] = observed[0]

    # Some hosts represent the message itself as a content block beside the scalar
    # model/fork fields. JSON content is a wrapper, not message text, and is parsed below.
    if "message" not in leaf and "content" in candidate:
        content_text, error = _content_block_text(candidate.get("content"))
        if error:
            return {}, error, consumed_bytes
        if content_text and not _json_container_text(content_text):
            leaf["message"] = content_text
            consumed_bytes += len(content_text.encode("utf-8", errors="replace"))
    return leaf, None, consumed_bytes


def _bounded_assessor_intent(payload: dict[str, Any]) -> bool:
    """Notice assessor markers even when the request wrapper itself is malformed."""
    root = payload.get("tool_input") if "tool_input" in payload else payload
    pending: list[tuple[Any, int]] = [(root, 0)]
    nodes = 0
    byte_count = 0
    while pending:
        value, depth = pending.pop(0)
        nodes += 1
        if nodes > SUBAGENT_REQUEST_MAX_NODES or depth > SUBAGENT_REQUEST_MAX_DEPTH:
            return False
        if isinstance(value, str):
            encoded = value.encode("utf-8", errors="replace")
            byte_count += len(encoded)
            if byte_count > SUBAGENT_REQUEST_MAX_BYTES:
                return False
            lower = value.lower()
            if "assessor_binding_id" in lower:
                return True
            continue
        if isinstance(value, list):
            pending.extend(
                (item, depth + 1)
                for item in value[:SUBAGENT_REQUEST_MAX_LIST_ITEMS]
            )
            continue
        if not isinstance(value, dict):
            continue
        for key, child in list(value.items())[:SUBAGENT_REQUEST_MAX_LIST_ITEMS]:
            if normalized_key(key) in {"taskname", "name", "description"} and assessor_task_name_has_intent(child):
                return True
            pending.append((child, depth + 1))
    return False


def subagent_request_resolution(payload: dict[str, Any]) -> dict[str, Any]:
    """Resolve exactly one bounded canonical request leaf; never splice sibling layers."""
    root = payload.get("tool_input") if "tool_input" in payload else payload
    pending: list[tuple[Any, int]] = [(root, 0)]
    leaves: list[dict[str, str]] = []
    errors: list[str] = []
    seen: set[int] = set()
    nodes = 0
    byte_count = 0
    while pending:
        value, depth = pending.pop(0)
        nodes += 1
        if nodes > SUBAGENT_REQUEST_MAX_NODES:
            errors.append("request arguments exceed bounded node limit")
            break
        if depth > SUBAGENT_REQUEST_MAX_DEPTH:
            errors.append("request arguments exceed bounded depth limit")
            break
        if isinstance(value, str):
            encoded = value.encode("utf-8", errors="replace")
            byte_count += len(encoded)
            if byte_count > SUBAGENT_REQUEST_MAX_BYTES:
                errors.append("request arguments exceed bounded byte limit")
                break
            if not _json_container_text(value):
                continue
            try:
                decoded = json.loads(value)
            except (TypeError, ValueError):
                errors.append("request arguments contain invalid JSON")
                continue
            pending.append((decoded, depth + 1))
            continue
        if isinstance(value, list):
            if len(value) > SUBAGENT_REQUEST_MAX_LIST_ITEMS:
                errors.append("request arguments exceed bounded list limit")
                continue
            pending.extend((item, depth + 1) for item in value)
            continue
        if not isinstance(value, dict):
            continue
        identity = id(value)
        if identity in seen:
            continue
        seen.add(identity)
        function_tool_wrapper = _function_tool_request_wrapper(value)
        if function_tool_wrapper:
            leaf, leaf_error, leaf_bytes = {}, None, 0
        else:
            leaf, leaf_error, leaf_bytes = _canonical_request_leaf(value)
        byte_count += leaf_bytes
        if byte_count > SUBAGENT_REQUEST_MAX_BYTES:
            errors.append("request arguments exceed bounded byte limit")
            break
        if leaf_error:
            errors.append(leaf_error)
        elif leaf:
            leaves.append(leaf)
        wrapper_keys = ("arguments",) if function_tool_wrapper else SUBAGENT_REQUEST_WRAPPERS
        for key in wrapper_keys:
            if key in value:
                pending.append((value.get(key), depth + 1))
        if normalized_key(value.get("type")) in {"text", "inputtext", "outputtext"}:
            text_value = value.get("text")
            if isinstance(text_value, str) and _json_container_text(text_value):
                pending.append((text_value, depth + 1))

    error = errors[0] if errors else None
    if not error and len(leaves) > 1:
        error = "request arguments contain multiple request leaves"
    return {
        "leaf": leaves[0] if len(leaves) == 1 and not error else {},
        "error": error,
        "assessor_intent": _bounded_assessor_intent(payload),
    }


def subagent_request_candidates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    resolution = subagent_request_resolution(payload)
    leaf = resolution.get("leaf")
    return [leaf] if isinstance(leaf, dict) and leaf else []


def subagent_request_fields(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    candidates = subagent_request_candidates(payload)
    candidate = candidates[0] if candidates else {}
    task_name = safe_label(candidate.get("task_name"), 120) if candidate.get("task_name") else None
    scope_value = candidate.get("message")
    return task_name, stable_hash(scope_value) if scope_value else None


def subagent_request_visibility(payload: dict[str, Any]) -> str:
    candidates = subagent_request_candidates(payload)
    value = candidates[0].get("message") if candidates else None
    if isinstance(value, str) and OPAQUE_V2_MESSAGE_RE.fullmatch(value.strip()):
        return "opaque_v2"
    return "plaintext"


def bound_assessor_task_name(state: dict[str, Any]) -> str | None:
    binding = safe_fingerprint(state.get("assessor_binding_id"))
    objective = safe_fingerprint(state.get("objective", {}).get("fingerprint"))
    if not binding or len(binding) != 32 or not objective or len(objective) != 16:
        return None
    return f"high_assessor_{binding}_{objective}_v1"


def bound_executor_task_name(state: dict[str, Any]) -> str | None:
    contract = safe_fingerprint(state.get("execution_contract_id"))
    if not contract or len(contract) != 32:
        return None
    if str(state.get("execution_profile_version")) == EXECUTION_PROFILE_VERSION:
        item = current_execution_slice(state)
        token = slice_task_token(state)
        if not item or not token:
            return None
        prefix = f"confirmed_executor_{contract}_{item['id']}_{token}"
        review = _safe_executor_review(state.get("executor_review"))
        verification_recovery = bool(
            safe_int(state.get("executor_attempt")) == 1
            and review.get("execution_contract_id") == contract
            and review.get("slice_id") == item["id"]
            and review.get("slice_contract_id") == slice_contract_id(state)
            and review.get("attempt") == 1
            and (
                state.get("executor_state") == "verification_required"
                and review.get("status") == "review_required"
                or state.get("executor_state") == "recovery_required"
                and state.get("executor_failure_kind") == "verification_failed"
                and review.get("status") == "failed"
            )
        )
        if verification_recovery:
            review_digest = review.get("review_evidence_digest")
            return (
                f"{prefix}_vf_{review_digest}_v2"
                if review_digest
                else None
            )
        failure = (
            safe_label(state.get("executor_failure_kind"), 48)
            if state.get("executor_failure_kind") in EXECUTOR_FAILURE_KINDS
            else ""
        )
        if state.get("executor_state") == "recovery_required" and safe_int(state.get("executor_attempt")) == 1 and failure:
            return f"{prefix}_{failure}_v2"
        return f"{prefix}_v1"
    review = _safe_executor_review(state.get("executor_review"))
    verification_recovery = bool(
        safe_int(state.get("executor_attempt")) == 1
        and review.get("execution_contract_id") == contract
        and review.get("attempt") == 1
        and (
            state.get("executor_state") == "verification_required"
            and review.get("status") == "review_required"
            or state.get("executor_state") == "recovery_required"
            and state.get("executor_failure_kind") == "verification_failed"
            and review.get("status") == "failed"
        )
    )
    if verification_recovery:
        evidence = review.get("review_evidence_digest") or "<32hex>"
        return (
            f"confirmed_executor_{contract}_verification_failed_{evidence}_v2"
        )
    failure = (
        safe_label(state.get("executor_failure_kind"), 48)
        if state.get("executor_failure_kind") in EXECUTOR_FAILURE_KINDS
        else ""
    )
    attempt = safe_int(state.get("executor_attempt"))
    recovery_v2 = (
        state.get("executor_state") == "recovery_required" and attempt == 1
    ) or (state.get("executor_state") == "spawn_pending" and attempt == 2)
    if failure and recovery_v2:
        return f"confirmed_executor_{contract}_{failure}_v2"
    return f"confirmed_executor_{contract}_v1"


def verification_recovery_evidence_digest(
    task_name: str | None, state: dict[str, Any]
) -> str | None:
    contract = safe_fingerprint(state.get("execution_contract_id"))
    if not task_name or not contract or len(contract) != 32:
        return None
    if str(state.get("execution_profile_version")) == EXECUTION_PROFILE_VERSION:
        review = _safe_executor_review(state.get("executor_review"))
        digest = _coordination_fp32(review.get("review_evidence_digest"))
        expected = bound_executor_task_name(state)
        return digest if digest and task_name == expected else None
    match = re.fullmatch(
        rf"confirmed_executor_{re.escape(contract)}_verification_failed_([0-9a-f]{{32}})_v2",
        task_name,
    )
    return match.group(1) if match else None


def executor_verification_recovery_pending(state: dict[str, Any]) -> bool:
    review = _safe_executor_review(state.get("executor_review"))
    contract = safe_fingerprint(state.get("execution_contract_id"))
    if (
        not contract
        or safe_int(state.get("executor_attempt")) != 1
        or review.get("execution_contract_id") != contract
        or review.get("attempt") != 1
        or not review.get("candidate_result_fingerprint")
        or not review.get("candidate_evidence_digest")
    ):
        return False
    if str(state.get("execution_profile_version")) == EXECUTION_PROFILE_VERSION:
        item = current_execution_slice(state)
        if (
            not item
            or review.get("slice_id") != item.get("id")
            or review.get("slice_contract_id") != slice_contract_id(state)
        ):
            return False
    return bool(
        state.get("executor_state") == "verification_required"
        and review.get("status") == "review_required"
        or state.get("executor_state") == "recovery_required"
        and state.get("executor_failure_kind") == "verification_failed"
        and review.get("status") == "failed"
        and review.get("review_evidence_digest")
    )


def executor_recovery_has_fresh_child_boundary(state: dict[str, Any]) -> bool:
    """Require ordinary attempt-one executors to become terminal before fresh v2."""
    if safe_int(state.get("executor_attempt")) != 1:
        return False
    if executor_verification_recovery_pending(state):
        # Schema upgrades intentionally discard transient lifecycle caches. The
        # bounded candidate/review record is the durable terminal proof.
        return True
    contract = safe_fingerprint(state.get("execution_contract_id"))
    matching = [
        group
        for group in subagent_lifecycle_groups(state)
        if isinstance(group.get("request"), dict)
        and group["request"].get("role") == "confirmed_executor"
        and group["request"].get("contract_id") == contract
        and (
            str(state.get("execution_profile_version")) != EXECUTION_PROFILE_VERSION
            or group["request"].get("slice_contract_id") == slice_contract_id(state)
        )
        and safe_int(group["request"].get("attempt")) == 1
    ]
    if any(group.get("state") in {"live", "result_pending"} for group in matching):
        return False
    if any(group.get("state") == "terminal" for group in matching):
        return True
    # A rejected spawn has no child to terminate. Its persisted failed request is
    # nevertheless a complete attempt boundary and recovery still needs fresh v2.
    return state.get("executor_failure_kind") == "spawn_failed"


def opaque_v2_bound_assessor_target(payload: dict[str, Any], state: dict[str, Any]) -> bool:
    if subagent_request_visibility(payload) != "opaque_v2":
        return False
    candidates = subagent_request_candidates(payload)
    target = str(candidates[0].get("target") or "").strip() if candidates else ""
    expected = bound_assessor_task_name(state)
    if not expected or not target or target.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] != expected:
        return False
    return any(
        item.get("event") == "request"
        and item.get("role") == "high_assessor"
        and item.get("contract_id") == state.get("assessor_binding_id")
        and item.get("task_name") == expected
        and item.get("request_visibility") == "opaque_v2"
        for item in state.get("subagents", [])
        if isinstance(item, dict)
    )


def subagent_request_options(payload: dict[str, Any]) -> dict[str, str | None]:
    candidates = subagent_request_candidates(payload)
    candidate = candidates[0] if candidates else {}
    result: dict[str, str | None] = {
        "model": None,
        "reasoning_effort": None,
        "fork_turns": None,
    }
    for key in tuple(result):
        if candidate.get(key) not in (None, ""):
            result[key] = str(candidate.get(key)).strip()
    return result


def confirmed_executor_request(payload: dict[str, Any], state: dict[str, Any]) -> tuple[bool, str | None]:
    resolution = subagent_request_resolution(payload)
    if resolution.get("error"):
        return False, f"executor {resolution['error']}"
    contract_id = str(state.get("execution_contract_id") or "")
    profile_v7 = str(state.get("execution_profile_version")) == EXECUTION_PROFILE_VERSION
    current_slice = current_execution_slice(state)
    current_slice_contract = slice_contract_id(state)
    request = subagent_request_text(payload)
    if not contract_id or not request or (profile_v7 and (not current_slice or not current_slice_contract)):
        return False, "missing execution contract"
    options = subagent_request_options(payload)
    task_name, _ = subagent_request_fields(payload)
    visibility = subagent_request_visibility(payload)
    opaque_v2 = visibility == "opaque_v2"
    verification_recovery = executor_verification_recovery_pending(state)
    verification_evidence = verification_recovery_evidence_digest(task_name, state)
    if verification_recovery:
        review = _safe_executor_review(state.get("executor_review"))
        caller_id = next(
            (
                safe_label(payload.get(key), 120)
                for key in ("agent_id", "subagent_id")
                if payload.get(key)
            ),
            None,
        )
        if caller_id:
            return False, "verification recovery must be dispatched by the parent reviewer"
        if not verification_evidence:
            return False, "verification recovery requires the evidence-bound visible v2 task_name"
        if (
            review.get("review_evidence_digest")
            and verification_evidence != review.get("review_evidence_digest")
        ):
            return False, "verification recovery evidence no longer matches the pending review"
    elif task_name != bound_executor_task_name(state):
        return False, "executor requires the exact visible attempt-bound task_name"
    model = str(options.get("model") or "").strip()
    effort = str(options.get("reasoning_effort") or "").strip().lower()
    fork_turns = str(options.get("fork_turns") or "").strip().lower()
    preference = safe_session_execution_preference(
        state.get("session_execution_preference")
    )
    contract_marker = opaque_v2 or bool(
        re.search(
            rf"(?:execution_contract_id|execution-contract-id|执行合同)\s*[:=：]\s*{re.escape(contract_id)}\b",
            request,
            re.I,
        )
    )
    slice_marker = not profile_v7 or opaque_v2 or bool(
        re.search(
            rf"(?:slice_id|slice-id|执行切片)\s*[:=：]\s*{re.escape(str((current_slice or {}).get('id') or ''))}\b",
            request,
            re.I,
        )
        and re.search(
            rf"(?:slice_contract_id|slice-contract-id|切片合同)\s*[:=：]\s*{re.escape(str(current_slice_contract or ''))}\b",
            request,
            re.I,
        )
    )
    plan_marker = opaque_v2 or str(state.get("plan_digest") or "") in request
    generation_marker = opaque_v2 or bool(
        re.search(
            rf"(?:plan_generation|plan-generation|计划代次)\s*[:=：]\s*{safe_int(state.get('plan_generation'))}\b",
            request,
            re.I,
        )
    )
    artifact = _safe_plan_artifact(state.get("plan_artifact"))
    canonical_marker = opaque_v2 or bool(
        artifact.get("relative_path")
        and str(artifact.get("relative_path")) in request
        and artifact.get("current_revision_digest")
        and str(artifact.get("current_revision_digest")) in request
        and artifact.get("journal_digest")
        and str(artifact.get("journal_digest")) in request
        and (
            re.search(r"\b(?:read|reread|load)\b.{0,48}\bcanonical\b", request, re.I)
            or any(term in request for term in ("重读权威计划", "读取权威计划", "从权威文档加载"))
        )
    )
    exclusive_scope = opaque_v2 or bool(
        re.search(r"\b(?:exclusive (?:owner|executor)|only executor)\b", request, re.I)
        or any(term in request for term in ("唯一执行者", "独占执行", "独占修改"))
    )
    acceptance = opaque_v2 or bool(
        re.search(r"\b(?:acceptance|verification|verify|test)\b", request, re.I)
        or any(term in request for term in ("验收", "验证", "测试"))
    )
    result_contract = opaque_v2 or bool(
        re.search(
            (
                rf"(?m)^EXECUTION_RESULT execution_contract_id={re.escape(contract_id)} "
                rf"slice_id={re.escape(str((current_slice or {}).get('id') or ''))} "
                r"outcome=succeeded\|failed$"
                if profile_v7
                else rf"(?m)^EXECUTION_RESULT execution_contract_id={re.escape(contract_id)} "
                r"outcome=succeeded\|failed evidence_digest=<32hex>$"
            ),
            request,
        )
    )
    if state.get("executor_state") == "recovery_required" or verification_recovery:
        if not executor_recovery_has_fresh_child_boundary(state):
            return False, "executor recovery requires the prior child to be terminal before fresh v2"
        stall = _safe_stall(state.get("stall"))
        if stall.get("state") not in {"none", "resume_required"}:
            return False, "stall diagnosis must complete before executor recovery"
        if stall.get("state") == "resume_required":
            if (
                f"stall_id={stall.get('stall_id')}" not in request
                or f"remediation_digest={stall.get('remediation_digest')}" not in request
            ):
                return False, "stall recovery lacks the bound stall_id and remediation_digest"
            if stall.get("resume_profile") != confirmed_executor_model_profile(state):
                return False, "stall recovery profile no longer matches the bound session policy"
        failure_kind = (
            "verification_failed"
            if verification_recovery
            else str(state.get("executor_failure_kind") or "")
        )
        recovery_marker = opaque_v2 or bool(
            failure_kind
            and re.search(
                rf"(?:recovery_from|recovery-from|恢复自)\s*[:=：]\s*{re.escape(failure_kind)}\b",
                request,
                re.I,
            )
        )
        correction_marker = opaque_v2 or bool(
            re.search(
                r"(?:material_correction|material-correction|实质修正)\s*[:=：]\s*.{8,}",
                request,
                re.I,
            )
        )
        if not recovery_marker or not correction_marker:
            return False, "recovery request lacks the typed failure and a material correction"
        if verification_recovery and not opaque_v2:
            evidence_marker = re.search(
                r"(?:verification_evidence_digest|verification-evidence-digest)"
                r"\s*[:=：]\s*([0-9a-f]{32})\b",
                request,
                re.I,
            )
            if (
                not evidence_marker
                or evidence_marker.group(1) != verification_evidence
            ):
                return False, "verification recovery lacks the task-bound review evidence digest"
    if not model:
        return False, "model_unavailable"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{1,79}", model):
        return False, "invalid executor model identifier"
    if preference == "highest_throughout":
        expected_model = highest_execution_model(state)
        expected_effort = highest_execution_effort(state)
        if not opaque_v2 and "profile_resolution=highest_available" not in request:
            return False, "highest executor requires profile_resolution=highest_available"
        if not expected_model or not expected_effort:
            return False, "model_unavailable"
        if safe_label(model, 80) != expected_model:
            return False, "executor model must match the bound highest-tier model"
        if effort != expected_effort:
            return False, "reasoning_effort must match the bound highest assessor request"
    else:
        current_model = str(state.get("model") or "").strip()
        if current_model and safe_label(model, 80) == safe_label(current_model, 80):
            return False, "executor model is not a lower-tier override"
        if effort != "medium":
            return False, "reasoning_effort must be medium"
    if fork_turns != "1":
        return False, "every bound executor requires fork_turns=1"
    if not contract_marker or not slice_marker or not plan_marker or not generation_marker:
        return False, "executor request is not bound to the exact confirmed plan"
    if not canonical_marker:
        return False, "executor must load the exact current revision from the canonical journal"
    if not exclusive_scope or not acceptance:
        return False, "executor request must declare exclusive execution ownership and acceptance"
    if not result_contract:
        return False, "executor request must require the exact bound EXECUTION_RESULT contract"
    return True, None


def confirmed_assessor_request(payload: dict[str, Any], state: dict[str, Any]) -> tuple[bool, str | None]:
    resolution = subagent_request_resolution(payload)
    if resolution.get("error"):
        return False, f"assessor {resolution['error']}"
    binding = str(state.get("assessor_binding_id") or "")
    request = subagent_request_text(payload)
    options = subagent_request_options(payload)
    task_name, _ = subagent_request_fields(payload)
    visibility = subagent_request_visibility(payload)
    opaque_v2 = visibility == "opaque_v2"
    if not binding or not request:
        return False, "missing assessor binding"
    if safe_int(state.get("assessor_attempt")) >= 2:
        return False, "assessor retry exhausted"
    if state.get("assessor_state") not in {"spawn_required", "recovery_required"}:
        return False, "duplicate assessor"
    objective = str(state.get("objective", {}).get("fingerprint") or "")
    if opaque_v2:
        if task_name != bound_assessor_task_name(state):
            return False, "opaque V2 assessor requires the exact visible task_name binding"
        if state.get("assessor_state") == "recovery_required":
            return False, "opaque V2 assessor recovery requires a fresh objective binding"
    else:
        if not re.search(rf"assessor_binding_id\s*[:=]\s*{re.escape(binding)}\b", request):
            return False, "assessor binding mismatch"
        if "profile_resolution=highest_available" not in request:
            return False, "assessor profile resolution mismatch"
        if not objective or not re.search(rf"objective_fingerprint\s*[:=]\s*{re.escape(objective)}\b", request):
            return False, "assessor objective mismatch"
    raw_model = str(options.get("model") or "").strip()
    model = safe_label(raw_model, 80)
    if not raw_model or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{1,79}", model):
        return False, "assessor requires explicit highest model and effort"
    expected_effort = requested_assessor_reasoning_effort(state)
    if str(options.get("reasoning_effort") or "").lower() != expected_effort:
        return False, "assessor reasoning_effort must match the bound highest available policy"
    fork = str(options.get("fork_turns") or "").lower()
    if fork != "1":
        return False, "every bound assessor requires fork_turns=1"
    simple_contract = opaque_v2 or bool(re.search(r"(?:simple.{0,32}(?:direct|solve|execute|解决|直接).{0,40}(?:verify|验证|测试)|(?:直接|解决).{0,40}simple)", request, re.I))
    hard_contract = opaque_v2 or bool(re.search(r"(?:hard.{0,48}(?:read[- ]only|plan|confirmation|只读|计划|确认)|hard.{0,48}(?:plan|只读|计划).{0,48}(?:read[- ]only|confirmation|只读|确认))", request, re.I))
    if not simple_contract or not hard_contract:
        return False, "assessor request lacks the Simple/Hard assessment contract"
    if state.get("assessor_state") == "recovery_required":
        failure = safe_label(state.get("assessor_failure_kind"), 48)
        if (
            not failure
            or f"recovery_from={failure}" not in request
            or not re.search(r"material_correction\s*[:=]\s*\S.{7,}", request)
        ):
            return False, "assessor recovery lacks typed cause or material correction"
    return True, None


def subagent_request_text(payload: dict[str, Any]) -> str:
    candidates = subagent_request_candidates(payload)
    value = candidates[0].get("message") if candidates else None
    return value if isinstance(value, str) else ""


def subagent_request_has_assessor_intent(payload: dict[str, Any]) -> bool:
    return bool(subagent_request_resolution(payload).get("assessor_intent"))


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
        or any(term in request for term in ("只读", "只读检查", "不修改", "不要修改", "不写入"))
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
            r"rm|mv|cp|install|truncate|tee|patch|mkdir)\b",
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
    if re.search(r"(?:不(?:构建|编译|安装|部署|重启|烧录|刷机|操作设备)|do not\s+(?:build|compile|install|deploy|reboot|flash|use device))", request, re.I):
        mutation = False
    analysis_only = bool(
        re.search(r"\b(?:inspect|review|analyze|analyse|research|search|read|explain)\b", lower)
        or any(term in request for term in ("检查", "审查", "分析", "调研", "搜索", "阅读", "解释"))
    )
    return (explicit_read_only or analysis_only) and not mutation


def request_touches_shared_resource(request: str) -> bool:
    text = str(request or "")
    positive = re.search(r"(?:\b(?:build|compile|install|deploy|reboot|flash|adb|device)\b|构建|编译|安装|部署|重启|烧录|刷机|设备|设备验证|操作设备)", text, re.I)
    if not positive:
        return False
    negated = re.search(r"(?:不(?:构建|编译|安装|部署|重启|烧录|刷机|操作设备|使用设备)|do not\s+(?:build|compile|install|deploy|reboot|flash|use device)|without\s+(?:building|deploying|device))", text, re.I)
    read_existing = re.search(r"(?:只读|read[- ]only|检查|审查|分析).{0,24}(?:日志|log|失败|输出|build|deploy|构建|部署)", text, re.I)
    return not bool(negated or read_existing)


def canonical_update_plan_projection(
    payload: dict[str, Any], state: dict[str, Any]
) -> bool:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return False
    artifact = _safe_plan_artifact(state.get("plan_artifact"))
    digest = safe_fingerprint(artifact.get("current_revision_digest"))
    explanation = str(tool_input.get("explanation") or "")
    marker = re.search(
        r"(?i)canonical_revision_digest=([0-9a-f]{32})\b", explanation
    )
    if (
        not digest
        or not marker
        or marker.group(1).lower() != digest
        or "projection_only" not in explanation.lower()
    ):
        return False
    items = tool_input.get("plan")
    if not isinstance(items, list) or not items:
        return False
    try:
        canonical = read_current_plan_revision(state, payload)
    except (OSError, PlanArtifactError):
        return False
    for item in items:
        if not isinstance(item, dict):
            return False
        step = " ".join(str(item.get("step") or "").split())
        if not step or step not in " ".join(canonical.split()):
            return False
    return True


def plan_confirmation_guard(payload: dict[str, Any], state: dict[str, Any]) -> str | None:
    if state.get("work_difficulty") != "hard":
        return None
    caller = next((safe_label(payload.get(key), 120) for key in ("agent_id", "subagent_id") if payload.get(key)), None)
    if caller and state.get("assessor_state") == "simple_running" and caller == state.get("assessor_agent_id"):
        return None
    if state.get("plan_state") == "confirmed" and state.get("confirmed_plan_digest") == state.get("plan_digest"):
        return None
    tool_key = normalized_key(payload.get("tool_name"))
    if "updateplan" in tool_key:
        return (
            None
            if canonical_update_plan_projection(payload, state)
            else "unbound update_plan split-brain mutation outside the canonical journal"
        )
    if "requestuserinput" in tool_key:
        return None
    if state.get("executor_state") == "local_running":
        if is_subagent_spawn_tool(payload):
            return "delegation is explicitly opted out for this confirmed local contract"
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
    if "requestuserinput" in tool_key:
        return None
    if is_subagent_spawn_tool(payload):
        if subagent_request_is_read_only(payload):
            return None
        valid, reason = confirmed_executor_request(payload, state)
        if valid and state.get("executor_state") in {
            "spawn_required",
            "verification_required",
            "recovery_required",
        }:
            return None
        if valid and state.get("executor_state") not in {
            "spawn_required",
            "verification_required",
            "recovery_required",
        }:
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
    live_group = next(
        (
            group
            for group in reversed(subagent_lifecycle_groups(state))
            if group.get("state") == "live"
            and group.get("agent_id") == caller_id
            and isinstance(group.get("request"), dict)
        ),
        None,
    )
    live_request = (
        live_group.get("request") if isinstance(live_group, dict) else {}
    )
    if (
        caller_id
        and state.get("executor_state") == "running"
        and caller_id == state.get("executor_agent_id")
        and state.get("execution_contract_id") == execution_contract_id(state)
        and live_request.get("role") == "confirmed_executor"
        and live_request.get("contract_id") == state.get("execution_contract_id")
        and live_request.get("slice_id")
        == (current_execution_slice(state) or {}).get("id")
        and live_request.get("slice_contract_id") == slice_contract_id(state)
        and safe_int(live_request.get("attempt"))
        == safe_int(state.get("executor_attempt"))
    ):
        return None
    return f"parent or unbound {mutating}"


def assessor_gate_guard(payload: dict[str, Any], state: dict[str, Any]) -> str | None:
    assessor_state = state.get("assessor_state")
    if assessor_state not in {"spawn_required", "spawn_pending", "running", "simple_execution_required", "simple_running", "recovery_required", "failed"}:
        return None
    if assessor_state == "failed" and state.get("assessor_failure_kind") == "delegation_opt_out":
        return None
    if is_subagent_spawn_tool(payload):
        if assessor_state == "running" and subagent_request_is_read_only(payload):
            route = safe_route(state.get("last_route"))
            request = subagent_request_text(payload)
            touches_shared = request_touches_shared_resource(request)
            if not touches_shared:
                return None
        return "non-assessor subagent while high assessment is incomplete"
    if assessor_state == "simple_execution_required":
        return "Simple assessment requires the bound assessor follow-up before mutation"
    mutating = plan_confirmation_guard(payload, {**state, "work_difficulty": "hard", "plan_state": "awaiting_confirmation"})
    if not mutating:
        return None
    caller = next((safe_label(payload.get(key), 120) for key in ("agent_id", "subagent_id") if payload.get(key)), None)
    if caller and caller == state.get("assessor_agent_id") and assessor_state == "simple_running":
        return None
    return f"parent or unbound {mutating} while high assessor is active; hard work plan is not strictly confirmed"


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


def git_invocation_count(command: str) -> int:
    return max(
        (
            sum(1 for _ in GIT_INVOCATION_RE.finditer(shell_syntax_view(candidate)))
            for candidate in command_views(command)
        ),
        default=0,
    )


def explicit_git_cwd(command: str, payload_cwd: Any) -> tuple[str | None, str | None]:
    """Resolve visible `git -C` options without trusting an unavailable tool workdir."""
    invocation = _git_invocation(command)
    if not invocation:
        return None, None
    candidate, match = invocation
    prefix = candidate[match.start() : match.start(1)]
    raw_paths = re.findall(r"(?:^|\s)-C\s+('[^']*'|\"[^\"]*\"|[^\s;&|]+)", prefix)
    if not raw_paths:
        return None, None
    effective = str(payload_cwd or "").strip() or None
    for raw_path in raw_paths:
        path = raw_path[1:-1] if len(raw_path) >= 2 and raw_path[0] == raw_path[-1] and raw_path[0] in "'\"" else raw_path
        if not path or any(marker in path for marker in ("$", "`", "\x00")):
            return None, "git -C path must be literal scalar text"
        effective, error = _normalize_structured_command_cwd(path, effective)
        if error:
            return None, f"git -C {error}"
    return effective, None


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
    if re.match(r"(?i)^[a-z]:/", normalized):
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


def real_tmp_git_directory(cwd: Any) -> bool:
    value = str(cwd or "").strip().replace("\\", "/")
    if not (value == "/tmp" or value.startswith("/tmp/")):
        return True
    try:
        path = Path(value)
        return path.is_absolute() and path.resolve(strict=True).is_dir()
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
    # Parent acceptance commonly combines only shell predicates and byte-level
    # readers.  It is verification only when the complete command remains
    # read-only; a write/build/device command must retain its stronger class.
    if (
        value
        and not command_mutates_files(value)
        and not any(re.search(r"(?i)(?:^|[;&|]\s*)(?:adb(?:\.exe)?|fastboot|flash)\b", candidate) for candidate in detections)
        and any(re.search(r"(?i)(?:^|[;&|\r\n]\s*)(?:test|\[|cmp|wc|od|stat|sha256sum)\b", candidate) for candidate in detections)
    ):
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
    resolution = command_request_resolution(payload)
    command = str(resolution.get("command") or "")
    if not command:
        return None
    subcommand = git_subcommand(command)
    if subcommand and git_invocation_count(command) != 1:
        return (
            "ambiguous_git_input",
            "Workflow Manager guard blocked multiple Git invocations in one command. Run one bounded Git "
            "operation per tool call so each execution directory and subcommand is checked independently.",
        )
    if subcommand and resolution.get("error"):
        return (
            "ambiguous_git_input",
            f"Workflow Manager guard blocked Git because {resolution['error']}; keep cmd and cwd/workdir in one "
            "structured tool_input leaf and remove conflicting aliases.",
        )
    explicit_cwd, explicit_cwd_error = explicit_git_cwd(command, resolution.get("cwd"))
    if subcommand and explicit_cwd_error:
        return (
            "ambiguous_git_input",
            f"Workflow Manager guard blocked Git because {explicit_cwd_error}; use one literal existing native "
            "Linux git -C directory.",
        )
    effective_cwd = explicit_cwd or resolution.get("cwd")
    if subcommand and cwd_is_wsl_or_network_mount(effective_cwd):
        return (
            "mounted_local_git",
            "Workflow Manager guard blocked Git in a WSL/DrvFS/CIFS/UNC working tree. Use android-remote-git, the "
            "authoritative remote Linux source tree, or encode an already verified native Linux cwd as an absolute "
            "git -C path when the Hook only exposes the mounted session cwd.",
        )
    if subcommand and not real_tmp_git_directory(effective_cwd):
        return (
            "invalid_tmp_git_cwd",
            "Workflow Manager guard blocked Git because a /tmp workdir is not a real existing /tmp directory. "
            "Use an existing native Linux directory and keep cmd plus workdir/cwd in the same structured tool_input leaf.",
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
    command_resolution = command_request_resolution(payload)
    fingerprint_cwd = (
        command_resolution.get("cwd")
        if command_resolution.get("command")
        else payload.get("cwd")
    )
    canonical_payload = {
        "cwd": str(fingerprint_cwd or ""),
        "session_id": str(payload.get("session_id") or ""),
        "tool": str(payload.get("tool_name") or "unknown"),
        "tool_input": payload.get("tool_input"),
    }
    try:
        canonical = json.dumps(canonical_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:
        canonical = str(canonical_payload)
    return stable_hash(canonical), tool


def _response_status_explicit(
    response: Any, depth: int = 0, *, allow_host_wrapper: bool = True
) -> str | None:
    """Read host-owned status without treating arbitrary command output as a wrapper."""
    if depth > 8:
        return None
    if isinstance(response, str):
        stripped = response.strip()
        if allow_host_wrapper and stripped.startswith("Script failed\nWall time") and "\nOutput:\nScript error:" in stripped:
            return "error"
        if allow_host_wrapper and stripped.startswith("Script completed\nWall time") and "\nOutput:" in stripped:
            return "ok"
        if stripped[:1] in "[{":
            try:
                return _response_status_explicit(
                    json.loads(stripped), depth + 1, allow_host_wrapper=False
                )
            except Exception:
                return None
        return None
    if isinstance(response, (list, tuple)):
        statuses = [
            _response_status_explicit(
                item, depth + 1, allow_host_wrapper=allow_host_wrapper and index == 0
            )
            for index, item in enumerate(response)
        ]
        if any(item and item.startswith("error") for item in statuses):
            return "error"
        return next((item for item in statuses if item), None)
    if not isinstance(response, dict):
        return None
    if response.get("error") or response.get("isError") is True or response.get("is_error") is True:
        return "error"
    if response.get("success") is False or response.get("ok") is False:
        return "error"
    if isinstance(response.get("text"), str):
        text_status = _response_status_explicit(
            response.get("text"), depth + 1, allow_host_wrapper=allow_host_wrapper
        )
        if text_status:
            return text_status
    for key in ("output", "content", "result", "tool_response", "response"):
        if key in response:
            nested = _response_status_explicit(
                response.get(key),
                depth + 1,
                allow_host_wrapper=key == "content" and depth == 0,
            )
            if nested and nested.startswith("error"):
                return nested
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
    return (
        _response_status_explicit(
            response.get("content"), depth + 1, allow_host_wrapper=depth == 0
        )
        if "content" in response
        else None
    )


def response_status(response: Any) -> str:
    explicit = _response_status_explicit(response)
    if explicit:
        return explicit
    if not isinstance(response, dict):
        return "unknown"
    code = response.get("exit_code")
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
    return sum(group.get("state") == "live" for group in subagent_lifecycle_groups(state))


def agent_activity_counts(state: dict[str, Any]) -> dict[str, int]:
    groups = subagent_lifecycle_groups(state)
    return {
        "started": sum(group.get("start") is not None for group in groups),
        "completed": sum(group.get("state") == "terminal" for group in groups),
        "active": sum(group.get("state") == "live" for group in groups),
        "pending": sum(group.get("state") in {"pending", "result_pending"} for group in groups),
        "result_pending": sum(group.get("state") == "result_pending" for group in groups),
    }


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
        skill_current = stable_skill.get("status") in {"current", "installed"}
        if skill_current:
            cleanup_old_plugin_versions(skill_paths_verified=True)
    except Exception:
        pass
    cleanup_old_sessions()
    telemetry = latest_token_telemetry(payload)
    source = str(payload.get("source") or "startup")

    def update(state: dict[str, Any]) -> None:
        if telemetry:
            state["telemetry"] = telemetry
        if source in {"compact", "resume"}:
            reconcile_host_rollout_compactions(payload, state)
            resume_compaction_gate_misclassification_once(payload, state)
            reconcile_current_parent_review_on_resume(payload, state)
            reconcile_current_executor_rollout_on_resume(payload, state)
            resume_failed_parent_probe_once(state, payload)
            resume_failed_review_evidence_once(payload, state)

    state, _ = mutate_state(payload, update)
    canonical_resume_body: str | None = None
    if source in {"compact", "resume"}:
        resume_payload = dict(payload)
        resume_payload["_read_canonical_plan_body"] = True
        state = snapshot_state(resume_payload)
        canonical_resume_body = state.pop("_canonical_current_body", None)
    preference = safe_session_execution_preference(
        state.get("session_execution_preference")
    )
    base = (
        "Workflow Manager: availability only, not proof of effectiveness. Acceptance outranks "
        "context savings. Protocol: Contract > Evidence > Change > Verify > Report; skip irrelevant stages. "
        "Direct stays local. For Complex/Extensive work audit wall-clock gain; use owned read/write/test/research/"
        "review lanes and bias low-risk close calls parallel. Reuse unchanged evidence; "
        f"checkpoint before compaction. Pressure: {pressure_text(telemetry)}."
    )
    if preference == "highest_throughout":
        base += (
            " Session execution preference=highest_throughout: request explicit highest_available child "
            "contracts matching the bound highest assessor model/reasoning profile. Daily remains current. "
            "This Hook cannot switch the parent or prove that the host applied a requested override; "
            "record request and observed start metadata separately."
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
            "session_execution_preference": preference,
            "model_profile": state.get("model_profile", "current"),
            "assessor_state": state.get("assessor_state", "none"),
            "assessor_binding_id": state.get("assessor_binding_id"),
            "assessor_attempt": safe_int(state.get("assessor_attempt")),
            "assessor_failure_kind": state.get("assessor_failure_kind"),
            "assessor_observed_effective": bool(state.get("assessor_observed_effective")),
            "assessor_observed_model": state.get("assessor_observed_model"),
            "assessor_observed_reasoning_effort": state.get("assessor_observed_reasoning_effort"),
            "plan_state": state.get("plan_state", "none"),
            "plan_generation": safe_int(state.get("plan_generation")),
            "plan_digest": state.get("plan_digest"),
            "confirmed_plan_digest": state.get("confirmed_plan_digest"),
            "plan_artifact": _safe_plan_artifact(state.get("plan_artifact")),
            "execution_profile_version": state.get("execution_profile_version"),
            "executor_state": state.get("executor_state", "none"),
            "execution_contract_id": state.get("execution_contract_id"),
            "executor_agent_id": state.get("executor_agent_id"),
            "executor_attempt": safe_int(state.get("executor_attempt")),
            "executor_failure_kind": state.get("executor_failure_kind"),
            "executor_model": state.get("executor_model"),
            "executor_reasoning_effort": state.get("executor_reasoning_effort"),
            "executor_observed_effective": bool(state.get("executor_observed_effective")),
            "executor_observed_model": state.get("executor_observed_model"),
            "executor_observed_reasoning_effort": state.get("executor_observed_reasoning_effort"),
            "executor_fork_turns": state.get("executor_fork_turns"),
            "executor_review": _safe_executor_review(state.get("executor_review")),
            "last_execution_baseline": _safe_execution_baseline(
                state.get("last_execution_baseline")
            ),
            "causal_review": _safe_causal_review(state.get("causal_review")),
            "stall": _safe_stall(state.get("stall")),
            "reference_acceptance": _safe_reference_acceptance(state.get("reference_acceptance")),
            "coordination_activity": [
                safe
                for raw in state.get("coordination_activity", [])
                if (safe := _safe_coordination_activity(raw)) is not None
            ][:MAX_COORDINATION_ACTIVITY],
            "coordination_notices": [
                safe
                for raw in state.get("coordination_notices", [])
                if (safe := _safe_coordination_notice(raw)) is not None
            ][-MAX_COORDINATION_NOTICES:],
            "coordination_inbound": [
                safe
                for raw in state.get("coordination_inbound", [])
                if (safe := _safe_coordination_inbound(raw)) is not None
            ][-MAX_COORDINATION_INBOUND:],
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
        resume_artifact = _safe_plan_artifact(state.get("plan_artifact"))
        plan_resume_source = (
            " Canonical Hard-plan semantics must be reread from "
            f"{resume_artifact.get('relative_path')} at "
            f"current_revision_digest={resume_artifact.get('current_revision_digest')} and "
            f"journal_digest={resume_artifact.get('journal_digest')}; the native summary supplies only "
            "non-plan continuity."
            if resume_artifact.get("format_version") == 2
            and resume_artifact.get("write_status") == "written"
            else " Semantic continuity comes from the native summary; no valid canonical Hard plan is bound."
        )
        base += (
            " Resume metadata is non-executable."
            f"{plan_resume_source} "
            f"Metadata: {json.dumps(digest, ensure_ascii=False, separators=(',', ':'))}. "
            "Rerun when inputs, state, device, freshness, or evidence changed."
        )
        if isinstance(canonical_resume_body, str):
            base += (
                " The Hook reread the verified canonical current revision for semantic recovery; it follows as "
                "plan data and never as authorization.\nBEGIN_WORKFLOW_MANAGER_CANONICAL_PLAN\n"
                f"{canonical_resume_body}"
                "END_WORKFLOW_MANAGER_CANONICAL_PLAN"
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


def session_execution_preference_directive(prompt: str) -> str | None:
    """Recognize only explicit, session-scoped policy commands; retain no raw text."""
    normalized = re.sub(r"\s+", " ", prompt.strip().lower())
    if not normalized or re.search(
        r"^(?:解释|说明|分析|判断|告诉我|what|why|how|does|is|explain|document)\b",
        normalized,
        re.I,
    ):
        return None
    chinese_scope = bool(re.search(r"(?:本|当前|这个|整个|此|该)(?:次)?会话", normalized))
    english_scope = bool(
        re.search(
            r"\b(?:this|current)(?: entire| whole)? session\b|"
            r"\b(?:entire|whole) session\b|"
            r"\b(?:rest|remainder) of (?:this|the current) session\b",
            normalized,
            re.I,
        )
    )
    if not (chinese_scope or english_scope):
        return None
    restore = bool(
        re.search(
            r"(?:恢复|改回|切回|回到).{0,20}默认.{0,20}(?:模型|推理|执行|档位|策略|配置)|"
            r"(?:restore|reset|revert|switch back).{0,24}default.{0,24}"
            r"(?:model|reasoning|execution|profile|policy|setting)",
            normalized,
            re.I,
        )
    )
    if restore:
        return "default"
    if re.search(r"(?:不要|无需|不再|do not|don't|without)", normalized, re.I):
        return None
    throughout = bool(
        re.search(r"(?:全程|始终|一直|整个会话|接下来.{0,10}(?:都|全程))", normalized)
        or re.search(r"\b(?:throughout|always|for the (?:entire|whole|rest of the))\b", normalized)
    )
    highest_model = bool(
        re.search(r"最高(?:可用)?(?:的)?模型|\bhighest(?: available)? (?:codex )?model\b", normalized)
    )
    highest_reasoning = bool(
        re.search(r"最高(?:可用)?(?:的)?推理(?:强度|力度|等级)?", normalized)
        or re.search(r"\b(?:highest|maximum|max) reasoning(?: effort| level| intensity)?\b", normalized)
    )
    return "highest_throughout" if throughout and highest_model and highest_reasoning else None


def pure_plan_confirmation(prompt: str) -> bool:
    normalized = re.sub(r"[?!？！。,.，]+", "", prompt.strip().lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized or any(marker in normalized for marker in PLAN_CHANGE_MARKERS):
        return False
    return any(re.fullmatch(pattern, normalized, re.I) for pattern in PLAN_CONFIRM_PATTERNS)


def plan_replan_request(prompt: str) -> bool:
    normalized = re.sub(r"\s+", " ", prompt.strip().lower())
    return any(re.fullmatch(pattern, normalized, re.I) for pattern in PLAN_REPLAN_PATTERNS)


def plan_details_request(prompt: str) -> bool:
    normalized = re.sub(r"[?!？！。,.，:：]+", "", prompt.strip().lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return bool(
        re.fullmatch(
            r"(?:查看|显示|打开|读取)(?:当前|这个|上述|该|此)?计划(?:详情|内容|全文)?",
            normalized,
        )
        or re.fullmatch(
            r"(?:show|view|open|read)(?: the| this| current)? plan(?: details| content| in full)?",
            normalized,
            re.I,
        )
    )


def prompt_changes_pending_plan(prompt: str) -> bool:
    normalized = re.sub(r"\s+", " ", prompt.strip().lower())
    if re.search(r"(?:先不要|不要(?:再)?)(?:创建|新建|增加|删除|修改|变更|add|remove|change|create)", normalized, re.I):
        return True
    # Only explicit safety/acceptance prohibitions are not plan changes.
    # Keep "先不要修改 X" and "不要创建第二个切片" intact: both alter
    # requested scope even though they contain a negation word.
    controls_stripped = re.sub(
        r"(?:严禁|不得|禁止|不允许)\s*(?:(?:删除|修改|写入|变更).{0,40}(?:任何)?(?:文件|file)|(?:创建|启动|spawn).{0,32}(?:child|子(?:代理|agent)|executor|执行器)|进入\s*executor)", "", normalized
    )
    controls_stripped = re.sub(
        r"\b(?:do not|must not)\b\s+(?:(?:remove|change|modify|write)(?:\s+or\s+(?:remove|change|modify|write))*\s+(?:any\s+)?files?|start\s+(?:a\s+)?child)\b", "", controls_stripped, flags=re.I
    )
    return any(marker in controls_stripped for marker in PLAN_CHANGE_MARKERS)


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
    fidelity_negative = fidelity_negative_feedback(normalized)
    return any(marker in normalized for marker in SUCCESS_FEEDBACK_MARKERS) and not fidelity_negative and not (
        regression_signal and contrast_signal
    )


def fidelity_negative_feedback(prompt: str) -> bool:
    return any(marker in prompt.lower() for marker in ("不一致", "不对", "不像", "方向错误", "方向不对", "动画方向不对"))


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
        and (
            any(marker in normalized for marker in REGRESSION_REPORT_MARKERS)
            or (_safe_reference_acceptance(previous.get("reference_acceptance"))["enabled"] and fidelity_negative_feedback(prompt))
        )
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
        if profile in {"work_executor_low_latest", "work_executor_highest_available"}
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


def handle_coordination_user_prompt(payload: dict[str, Any], prompt: str) -> bool:
    has_marker = COORDINATION_ENVELOPE_START in prompt
    has_legacy = "<codex_delegation>" in prompt
    if not has_marker and not has_legacy:
        return False
    envelope, error = (
        parse_coordination_envelope(prompt)
        if prompt.startswith(COORDINATION_ENVELOPE_START)
        else (None, "coordination marker is mixed with prefixed content")
    )
    conflict = coordination_conflict_class(envelope) if envelope else None
    session_id = str(payload.get("session_id") or "")
    target_for_session = (
        coordination_task_fingerprint_for_host(
            session_id, envelope["target_host_fingerprint"]
        )
        if envelope and session_id
        else None
    )
    valid = bool(
        not has_legacy
        and envelope
        and not error
        and target_for_session == envelope["target_task_fingerprint"]
        and envelope["source_host_fingerprint"] == envelope["target_host_fingerprint"]
        and envelope["source_task_fingerprint"] != envelope["target_task_fingerprint"]
        and envelope["sender_resource_identity"] == envelope["target_resource_identity"]
        and conflict
    )
    if valid and envelope:
        current = snapshot_state(payload)
        identity = coordination_notice_identity(envelope)
        existing = next(
            (
                item
                for item in current.get("coordination_inbound", [])
                if item.get("notice_fingerprint") == identity["notice_fingerprint"]
            ),
            None,
        )
        blocks = [
            item
            for item in current.get("coordination_inbound", [])
            if item.get("scope_fingerprint") == identity["scope_fingerprint"]
            and item.get("transition") == "blocked"
        ]
        generation = safe_int(envelope.get("lease_generation"))
        if envelope.get("transition") == "blocked" and not existing and blocks:
            valid = generation > max(safe_int(item.get("lease_generation")) for item in blocks)
        elif envelope.get("transition") == "released":
            latest = max(blocks, key=lambda item: safe_int(item.get("lease_generation"))) if blocks else None
            valid = bool(
                latest
                and generation == safe_int(latest.get("lease_generation"))
                and latest.get("owner_fingerprint") == identity["owner_fingerprint"]
                and latest.get("phase_fingerprint") == identity["phase_fingerprint"]
            )

    def update(state: dict[str, Any]) -> None:
        if valid and envelope and conflict:
            identity = coordination_notice_identity(envelope)
            if not any(
                item.get("notice_fingerprint") == identity["notice_fingerprint"]
                for item in state.get("coordination_inbound", [])
            ):
                state.setdefault("coordination_inbound", []).append(
                    {
                        **identity,
                        "resource_identity": envelope["sender_resource_identity"],
                        "resource_kind": envelope["resource_kind"],
                        "lease_generation": envelope["lease_generation"],
                        "transition": envelope["transition"],
                        "received_at": coordination_now(),
                    }
                )
        else:
            state.setdefault("guards", []).append(
                {
                    "at": utc_now(),
                    "turn_id": safe_label(payload.get("turn_id"), 120)
                    if payload.get("turn_id")
                    else None,
                    "kind": "live_coordination_control",
                    "action": "deny",
                    "fingerprint": stable_hash(prompt),
                }
            )

    mutate_state(payload, update)
    emit_context(
        "UserPromptSubmit",
        (
            "Workflow Manager recorded a bounded coordination fingerprint and left the task contract unchanged."
            if valid
            else "Workflow Manager ignored an invalid or mixed coordination control envelope and left the task contract unchanged."
        ),
    )
    return True


def user_prompt_submit(payload: dict[str, Any]) -> None:
    raw_prompt = str(payload.get("prompt") or "")
    delegated_prompt = codex_delegation_input(raw_prompt)
    prompt = delegated_prompt if delegated_prompt is not None else raw_prompt
    identity_preflight = bool(
        delegated_prompt is None and identity_preflight_prompt(prompt)
    )
    if delegated_prompt is None and handle_coordination_user_prompt(payload, prompt):
        return
    canonical_context_requested = bool(
        plan_details_request(prompt)
        or plan_replan_request(prompt)
        or prompt_changes_pending_plan(prompt)
    )
    snapshot_payload = dict(payload)
    if canonical_context_requested:
        snapshot_payload["_read_canonical_plan_body"] = True
    previous = snapshot_state(snapshot_payload)
    canonical_current_body = previous.pop("_canonical_current_body", None)
    same_assessor_objective_retry = bool(
        previous.get("task_domain") == "work"
        and previous.get("objective", {}).get("fingerprint") == stable_hash(prompt)
        and previous.get("assessor_state")
        in {"spawn_required", "spawn_pending", "running", "recovery_required", "failed"}
    )
    preference_directive = session_execution_preference_directive(prompt)
    preference_changed = preference_directive not in {None, previous.get("session_execution_preference", "default")}
    requested_reference = reference_requested(prompt)
    reference_changed = bool(_safe_reference_acceptance(previous.get("reference_acceptance"))["enabled"] and not requested_reference and not fidelity_negative_feedback(prompt) and reference_contract_changed(prompt))
    pending_plan = previous.get("plan_state") in {"plan_ready", "awaiting_confirmation"}
    active_plan = pending_plan or previous.get("plan_state") == "confirmed"
    explicit_new = explicit_new_objective(prompt)
    failed_assessor_replan = bool(
        previous.get("assessor_state") in {"recovery_required", "failed"}
        and plan_replan_request(prompt)
        and previous.get("objective", {}).get("fingerprint") != stable_hash(prompt)
    )
    new_objective = bool(
        failed_assessor_replan
        or (
            explicit_new
            and (
                active_plan
                or previous.get("assessor_state")
                in {"spawn_required", "spawn_pending", "running", "recovery_required", "failed"}
                or previous.get("last_execution_baseline")
                or previous.get("causal_review", {}).get("state") != "none"
            )
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
        and (any(marker in prompt.lower() for marker in REGRESSION_REPORT_MARKERS) or fidelity_negative_feedback(prompt))
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
        preference_directive is not None
        or is_control_followup(prompt)
        or is_progress_followup(prompt)
        or active_plan
        or reference_changed
        or same_assessor_objective_retry
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
    if pending_plan and pure_plan_confirmation(prompt):
        classification["difficulty_decision_id"] = previous.get("difficulty_decision_id")
        classification["work_difficulty"] = previous.get("work_difficulty", "hard")
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
    if preference_directive is not None and previous.get("last_route"):
        classification = merge_followup_route(previous["last_route"], classification)
    if identity_preflight:
        # A fresh host-identity probe is control-plane work, never a continuation
        # of a prior Hard route. Re-apply this after all continuation/preference
        # merges so words such as Work/Hard in the probe cannot request a child.
        continuation = False
        new_objective = True
        classification = identity_preflight_route(prompt)
    telemetry = latest_token_telemetry(payload)

    def update(state: dict[str, Any]) -> None:
        prompt_meta = text_metadata(prompt)
        if identity_preflight and state.get("assessor_state") in {
            "spawn_pending",
            "running",
            "recovery_required",
        }:
            state.setdefault("guards", []).append(
                {
                    "at": utc_now(),
                    "turn_id": safe_label(payload.get("turn_id"), 120)
                    if payload.get("turn_id")
                    else None,
                    "kind": "identity_preflight_stale_child_cleared",
                    "action": "advise",
                    "fingerprint": stable_hash(
                        f"identity-preflight-stale-child\0{state.get('assessor_agent_id')}\0{state.get('assessor_binding_id')}",
                        32,
                    ),
                }
            )
        if preference_directive is not None:
            state["session_execution_preference"] = preference_directive
            if preference_changed and state.get("plan_state") == "confirmed":
                reset_executor_binding(state)
                state["execution_contract_id"] = execution_contract_id(state)
                state["executor_state"] = (
                    "local_running"
                    if safe_route(state.get("last_route")).get("delegation_opt_out")
                    else "spawn_required"
                )
                state["model_profile"] = confirmed_executor_model_profile(state)
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
                state["plan_digest"] = None
                state["plan_objective_fingerprint"] = None
                state["plan_difficulty_decision_id"] = None
                state["confirmed_plan_digest"] = None
                state["confirmed_at"] = None
                reset_executor_binding(state)
        elif reference["enabled"] and (causal_report or reference_failure or reference_rejection):
            reference.update({"state": "failed", "fidelity_candidate": "failed", "user_final_acceptance": "failed"})
            state["reference_acceptance"] = reference
        elif reference["enabled"] and reference_changed:
            reference.update({"contract_digest": stable_hash(f"{reference['contract_digest']}\0{prompt_meta['fingerprint']}", 32), "version_fingerprint": prompt_meta["fingerprint"], "phase": "planned", "state": "planned", "fidelity_candidate": "unknown", "user_final_acceptance": "unknown", "evidence_digest": None})
            state["reference_acceptance"] = reference
            state["plan_state"] = "analyzing"
            state["plan_digest"] = state["plan_objective_fingerprint"] = state["plan_difficulty_decision_id"] = None
            state["confirmed_plan_digest"] = state["confirmed_at"] = None
            reset_executor_binding(state)
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
            state["model_profile"] = (
                confirmed_executor_model_profile(state) if preference_changed else previous.get("model_profile", "work_executor_low_latest")
            )
        objective_fingerprint = state.get("objective", {}).get("fingerprint")
        if causal_report:
            # Freeze the old plan/contract for comparison. The causal guard allows only
            # evidence collection until a structured, baseline-bound conclusion is recorded.
            if state.get("last_execution_baseline"):
                state["last_execution_baseline"]["acceptance_status"] = "failed"
        elif reference_failure:
            state["plan_state"] = "analyzing"
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
            artifact = _safe_plan_artifact(state.get("plan_artifact"))
            binding_valid = bool(
                state.get("plan_state") == "awaiting_confirmation"
                and state.get("plan_digest")
                and state.get("plan_objective_fingerprint") == objective_fingerprint
                and state.get("plan_difficulty_decision_id")
                == state.get("difficulty_decision_id")
                and artifact.get("format_version") == 2
                and artifact.get("write_status") == "written"
                and artifact.get("relative_path")
                and artifact.get("generation") == state.get("plan_generation")
                and artifact.get("plan_digest")
                == artifact.get("current_revision_digest")
                == state.get("plan_digest")
                and safe_fingerprint(artifact.get("journal_digest"))
            )
            if binding_valid and verify_plan_artifact(state, payload):
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
            state["plan_digest"] = None
            state["plan_objective_fingerprint"] = None
            state["plan_difficulty_decision_id"] = None
            state["confirmed_plan_digest"] = None
            state["confirmed_at"] = None
            reset_executor_binding(state)
            state["model_profile"] = "work_assessment"
        elif not continuation:
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
        assessor_needed = classification.get("task_domain") == "work" and (
            not continuation or causal_report or reference_failure or replan or plan_changed
        )
        if assessor_needed:
            local_simple = bool(
                classification.get("work_difficulty") == "simple"
                and classification.get("difficulty_confidence") == "high"
                and not causal_report
                and not reference_failure
            )
            state["assessor_generation"] = (
                safe_int(state.get("assessor_generation"))
                if local_simple
                else max(safe_int(state.get("assessor_generation")), 0) + 1
            )
            state["assessor_binding_id"] = None if local_simple else assessor_binding_id(state)
            state["assessor_state"] = "simple_complete" if local_simple else "spawn_required"
            state["assessor_agent_id"] = None
            state["assessor_model"] = None
            state["assessor_reasoning_effort"] = None
            state["assessor_failure_kind"] = None
            state["assessor_observed_effective"] = False
            state["assessor_observed_model"] = None
            state["assessor_observed_reasoning_effort"] = None
            state["assessor_input_fingerprint"] = None if local_simple else state.get("objective", {}).get("fingerprint")
            state["assessor_fork_turns"] = None
            state["assessor_attempt"] = 0
            if classification.get("delegation_opt_out"):
                state["assessor_state"] = "failed"
                state["assessor_failure_kind"] = "delegation_opt_out"
        elif classification.get("task_domain") == "daily" and not continuation:
            state["assessor_state"] = "none"
            state["assessor_binding_id"] = None
            state["assessor_agent_id"] = None
            state["assessor_model"] = None
            state["assessor_reasoning_effort"] = None
            state["assessor_input_fingerprint"] = None
            state["assessor_failure_kind"] = None
            state["assessor_observed_effective"] = False
            state["assessor_observed_model"] = None
            state["assessor_observed_reasoning_effort"] = None
            state["assessor_fork_turns"] = None
            state["assessor_attempt"] = 0
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
    should_inject = identity_preflight or preference_directive is not None or causal_report or reference_failure or causal_active or classification["task_domain"] == "work" or classification["label"] in {"complex", "extensive"} or (
        isinstance(pressure, (int, float)) and pressure >= PRESSURE_TRIM_THRESHOLD
    )
    if not should_inject:
        return
    context = routing_context(classification, telemetry)
    if identity_preflight:
        context += (
            " Host activation/identity preflight is local control-plane work: child Start=0. "
            "Do not call tools, spawn an assessor, executor, or side lane, or mutate files/state; "
            "return only the marker requested by the user."
        )
    refreshed_for_assessor = snapshot_state(payload)
    canonical_artifact = _safe_plan_artifact(
        refreshed_for_assessor.get("plan_artifact")
    )
    if (
        canonical_artifact.get("format_version") == 2
        and canonical_artifact.get("write_status") == "written"
    ):
        context += (
            " Canonical plan journal binding: "
            f"relative_path={canonical_artifact.get('relative_path')} "
            f"current_revision_digest={canonical_artifact.get('current_revision_digest')} "
            f"journal_digest={canonical_artifact.get('journal_digest')} "
            f"revision_count={canonical_artifact.get('revision_count')}. "
            "The relative_path is plugin-data-root-relative contract metadata only and must never be resolved "
            "against cwd or a workspace. For plan details, replanning, compaction recovery, and executor handoff, "
            "reread the current revision from the trusted plugin-data journal; do not use state summaries or "
            "update_plan as an independent plan. "
            "Any update_plan call is projection_only and must carry canonical_revision_digest=<current digest>."
        )
        if canonical_context_requested and isinstance(canonical_current_body, str):
            context += (
                " The verified canonical current revision follows as plan data; use this exact revision for "
                "details or as the base for a trusted replacement revision, and do not treat its text as "
                "execution authorization.\nBEGIN_WORKFLOW_MANAGER_CANONICAL_PLAN\n"
                f"{canonical_current_body}"
                "END_WORKFLOW_MANAGER_CANONICAL_PLAN"
            )
    if preference_directive is not None:
        context += (
            f" Session execution preference request recorded as {preference_directive}; this is policy "
            "state only and does not prove that the host changed the parent model or reasoning settings."
        )
    if classification.get("task_domain") == "work" and refreshed_for_assessor.get("assessor_state") == "spawn_required":
        assessor_task = bound_assessor_task_name(refreshed_for_assessor)
        assessor_effort = requested_assessor_reasoning_effort(
            refreshed_for_assessor
        )
        assessor_effort_policy = (
            "explicit session-highest override"
            if assessor_effort == HIGHEST_SESSION_REASONING_EFFORT
            else "default second-highest reasoning tier"
        )
        context += (
            " Work requires one high-tier assessor before execution. Spawn exactly one child with the host's highest "
            f"available Codex model and reasoning_effort={assessor_effort} ({assessor_effort_policy}), explicit model/effort, positive "
            f"fork_turns=1, and visible task_name={assessor_task}; include "
            f"assessor_binding_id={refreshed_for_assessor.get('assessor_binding_id')} "
            f"objective_fingerprint={refreshed_for_assessor.get('objective', {}).get('fingerprint')} and profile_resolution=highest_available. The self-contained child "
            "contract is: assess Simple and directly solve+verify before WORK_ASSESSMENT; for Hard remain read-only, "
            "return a detailed executable plan plus WORK_ASSESSMENT. The canonical plan must end with exactly one "
            "workflow-manager-execution-slices fenced JSON block (version 1, nonempty global_constraints, sequential "
            "s01..sNN, title string, and nonempty scope/acceptance/rollback/stop_conditions/expected_artifacts arrays), "
            "then end exactly 计划已就绪，等待确认后执行 after the protocol marker. "
            "The visible task name preserves state binding when V2 encrypts message before PreToolUse; record "
            "requested versus observed profile separately."
        )
    elif (
        classification.get("task_domain") == "work"
        and classification.get("work_difficulty") == "simple"
        and classification.get("difficulty_confidence") == "high"
        and refreshed_for_assessor.get("assessor_state") == "simple_complete"
    ):
        context += (
            " High-confidence Simple work is local: child Start=0. Do not spawn an assessor or side lane; "
            "perform the bounded change and required verification directly."
        )
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
        executor_task = bound_executor_task_name(refreshed_for_assessor)
        context += (
            " Confirmation authorizes only the private canonical handoff: matching SubagentStart must first "
            "verify the current revision from the trusted plugin-data journal and will receive that exact body "
            "through additional context. The canonical relative_path is plugin-data-root-relative metadata, "
            "never a cwd/workspace path; failed read or digest drift must not unlock mutation."
        )
        if refreshed_for_assessor.get("session_execution_preference") == "highest_throughout":
            context += (
                " Confirmed plan binding is valid. Before mutation, spawn exactly one exclusive executor with "
                "profile_resolution=highest_available, the bound assessor/current highest-tier model, the same "
                f"requested reasoning_effort as the bound assessor, fork_turns=1, and visible task_name={executor_task}. Bind the exact "
                "execution contract, current slice contract, ownership, and acceptance. This is a request contract, not proof "
                "that the host applied the override; wait for matching SubagentStart metadata."
            )
        else:
            context += (
                " Confirmed plan binding is valid. The parent remains the high-reasoning coordinator; before any "
                "mutation, spawn exactly one exclusive confirmed-plan executor using the newest actually available "
                f"lower-tier Codex model, reasoning_effort=medium, fork_turns=1, and visible task_name={executor_task}. Bind its "
                "request to execution_contract_id, plan_digest, plan_generation, current slice_id/slice_contract_id, scope, and "
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
        if refreshed_for_assessor.get("executor_state") == "recovery_required":
            context += (
                " A terminal executor must never receive followup_task. After one material correction, use the "
                "only remaining attempt by spawning a fresh child with visible task_name="
                f"{bound_executor_task_name(refreshed_for_assessor)}, the same contract/profile, typed "
                "recovery_from, and fork_turns=1."
            )
        elif refreshed_for_assessor.get("executor_state") == "verification_required":
            context += (
                " Executor completion is only a candidate. Independently inspect the artifacts and acceptance "
                "evidence, then end with exactly EXECUTION_REVIEW execution_contract_id="
                f"{refreshed_for_assessor.get('execution_contract_id')} slice_id="
                f"{(current_execution_slice(refreshed_for_assessor) or {}).get('id')} outcome=passed|failed. The Hook generates the "
                "normalized evidence digest. Passed with bound verification evidence advances only this slice. If evidence instead proves a material "
                "verification failure on attempt one, the only recovery is a fresh evidence-bound "
                "verification_failed v2 child; never follow up v1."
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


def change_epoch_probe_kind(payload: dict[str, Any], category: str) -> str | None:
    """Classify only exact, read-only probes whose repetition has no new evidence value."""
    command = extract_command(payload).lower()
    tool = normalized_key(payload.get("tool_name"))
    if "viewimage" in tool or "screenshot" in tool:
        return "visual"
    if category in {"analysis", "research", "evidence"} and not command_mutates_files(command):
        return "search" if re.search(r"\b(?:rg|grep|find|search)\b", command) else "read"
    return None


def same_epoch_probe_seen(state: dict[str, Any], fingerprint: str, kind: str) -> bool:
    epoch = safe_int(state.get("change_epoch"))
    return any(
        item.get("fingerprint") == fingerprint
        and item.get("kind") == kind
        and safe_int(item.get("epoch")) == epoch
        for item in state.get("change_epoch_ledger", [])
    )


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


def handle_subagent_pretool(payload: dict[str, Any], state: dict[str, Any], fingerprint: str, *, assessor_safe_side_lane: bool = False) -> bool:
    if not is_subagent_spawn_tool(payload):
        return False

    caller = next((safe_label(payload.get(key), 120) for key in ("agent_id", "subagent_id") if payload.get(key)), None)
    # A child never inherits parent delegation authority.  This prevents an
    # exclusive executor slice from becoming an untracked delegation tree.
    if caller:
        def record_child_origin_guard(current: dict[str, Any]) -> None:
            current.setdefault("guards", []).append({
                "at": utc_now(),
                "turn_id": safe_label(payload.get("turn_id"), 120) if payload.get("turn_id") else None,
                "kind": "child_origin_spawn", "action": "deny", "fingerprint": fingerprint,
            })

        mutate_state(payload, record_child_origin_guard)
        emit_pretool_deny("Subagent spawn denied: child-origin delegation is not authorized by the parent contract.")
        return True

    executor_request, _ = confirmed_executor_request(payload, state)
    route_value = state.get("last_route")
    route_known = isinstance(route_value, dict) and bool(route_value)
    route = safe_route(route_value)
    gate = str(route.get("delegation_gate") or "closed")
    cap = safe_int(route.get("recommended_agent_cap"))
    if assessor_safe_side_lane:
        route_known = True
        gate = "audit"
        cap = max(cap, 1)
    if executor_request and state.get("executor_state") in {
        "spawn_required",
        "verification_required",
        "recovery_required",
    }:
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
    active = [] if executor_request else [
        item for item in active_agent_records(state)
        if not (assessor_safe_side_lane and item.get("role") == "high_assessor")
    ]
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
    elif not executor_request:
        # This is a monotonic start budget, rather than a reusable slot cap.
        # A second lane requires the explicit ready-and-disjoint re-audit.
        side_lane_budget = 2 if reaudited else 1
        side_lane_starts = sum(
            1 for item in state.get("subagents", [])
            if item.get("event") == "request" and item.get("role") == "lane"
        )
        if side_lane_starts >= side_lane_budget:
            deny_kind = "side_lane_budget"
            deny_reason = (
                "Subagent spawn denied: the monotonic side-lane start budget is exhausted; "
                "do not substitute delegation for required acceptance."
            )

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
        identity = current.setdefault("identity_evidence", {})
        # Persist a digest of the requested override only.  It is intentionally
        # distinct from SubagentStart's host echo below.
        identity["requested_profile"] = stable_hash(
            canonical_json({"model": safe_label(options.get("model"), 80), "reasoning_effort": safe_label(options.get("reasoning_effort"), 24), "fork_turns": str(options.get("fork_turns") or "")}), 32
        )
        verification_recovery = executor_verification_recovery_pending(current)
        verification_evidence = verification_recovery_evidence_digest(
            task_name, current
        )
        recovery_from = (
            "verification_failed"
            if executor_request and verification_recovery
            else (
                current.get("executor_failure_kind")
                if executor_request
                and current.get("executor_state") == "recovery_required"
                and current.get("executor_failure_kind") in EXECUTOR_FAILURE_KINDS
                else None
            )
        )
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
                "requested": True,
                "host_accepted": None,
                "request_gate": gate,
                "request_visibility": subagent_request_visibility(payload),
                "request_cap": cap,
                "reaudited": reaudited,
                "role": "confirmed_executor" if executor_request else "lane",
                "contract_id": current.get("execution_contract_id") if executor_request else None,
                "slice_id": (
                    (current_execution_slice(current) or {}).get("id")
                    if executor_request
                    else None
                ),
                "slice_contract_id": (
                    slice_contract_id(current) if executor_request else None
                ),
                "model": safe_label(options.get("model"), 80) if options.get("model") else None,
                "reasoning_effort": (
                    safe_label(options.get("reasoning_effort"), 24)
                    if options.get("reasoning_effort")
                    else None
                ),
                "fork_turns": options.get("fork_turns"),
                "attempt": attempt,
                "recovery_from": recovery_from,
            }
        )
        if executor_request:
            if verification_recovery:
                review = _safe_executor_review(current.get("executor_review"))
                review["status"] = "recovery_started"
                review["review_evidence_digest"] = verification_evidence
                review["at"] = utc_now()
                current["executor_review"] = review
                baseline = _safe_execution_baseline(
                    current.get("last_execution_baseline")
                ) or build_execution_baseline(current)
                if baseline:
                    baseline["acceptance_status"] = "failed"
                    current["last_execution_baseline"] = baseline
            current["executor_state"] = "spawn_pending"
            current["executor_attempt"] = attempt
            current["executor_failure_kind"] = recovery_from
            current["executor_model"] = safe_label(options.get("model"), 80)
            current["executor_reasoning_effort"] = safe_label(
                options.get("reasoning_effort"), 24
            )
            current["executor_fork_turns"] = str(options.get("fork_turns"))
            current["model_profile"] = confirmed_executor_model_profile(current)
            stall = _safe_stall(current.get("stall"))
            if stall.get("state") == "resume_required":
                correction = re.search(
                    r"material_correction\s*[:=]\s*(.{8,}?)(?=\s+stall_id=|$)",
                    subagent_request_text(payload),
                    re.I,
                )
                stall["state"] = "resuming"
                stall["correction_digest"] = stable_hash(correction.group(1), 32) if correction else None
                stall["at"] = utc_now()
                current["stall"] = stall

    mutate_state(payload, record_request)
    if executor_request:
        preference = safe_session_execution_preference(state.get("session_execution_preference"))
        profile_text = (
            "highest_available model/reasoning request"
            if preference == "highest_throughout"
            else "lower-tier model and medium reasoning request"
        )
        emit_context(
            "PreToolUse",
            f"Confirmed-plan executor request accepted: the explicit {profile_text}, non-full-history fork, "
            "exact execution contract, exclusive ownership, and acceptance markers match. This records a "
            "requested profile, not proof that the child started or that the host applied it; wait for a matching "
            "SubagentStart. The "
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


def _record_coordination_guard(
    payload: dict[str, Any], fingerprint: str, kind: str = "live_coordination"
) -> None:
    def update(state: dict[str, Any]) -> None:
        state.setdefault("guards", []).append(
            {
                "at": utc_now(),
                "turn_id": safe_label(payload.get("turn_id"), 120) if payload.get("turn_id") else None,
                "kind": kind,
                "action": "deny",
                "fingerprint": fingerprint,
            }
        )

    mutate_state(payload, update)


def handle_coordination_pretool(
    payload: dict[str, Any], state: dict[str, Any], fingerprint: str
) -> bool:
    if not is_send_message_to_thread_tool(payload):
        return False
    message = coordination_control_text(payload)
    if message is None:
        return False
    if message.startswith("<codex_delegation>"):
        _record_coordination_guard(payload, fingerprint, "legacy_coordination")
        emit_pretool_deny(
            "Workflow Manager blocked legacy <codex_delegation> coordination. Call list_threads first, then send "
            "one complete WORKFLOW_COORDINATION_V1 envelope only to a fresh active target."
        )
        return True
    if not message.startswith(COORDINATION_ENVELOPE_START):
        return False

    fields, fields_error = coordination_send_fields(payload)
    envelope, envelope_error = parse_coordination_envelope(message)
    if fields_error or envelope_error or not fields or not envelope:
        _record_coordination_guard(payload, fingerprint)
        emit_pretool_deny(
            f"Workflow Manager blocked invalid coordination envelope: {fields_error or envelope_error}. "
            "Call list_threads and send only the exact bounded WORKFLOW_COORDINATION_V1 contract."
        )
        return True
    actual_task = coordination_task_fingerprint(fields["thread_id"], fields["host_id"])
    actual_host = coordination_host_fingerprint(fields["host_id"])
    session_id = payload.get("session_id")
    source_task = coordination_task_fingerprint(session_id, fields["host_id"])
    if (
        not isinstance(session_id, str)
        or not session_id
        or len(session_id.encode("utf-8", errors="replace")) > COORDINATION_ID_MAX_BYTES
        or envelope["source_host_fingerprint"] != actual_host
        or envelope["source_task_fingerprint"] != source_task
        or envelope["source_task_fingerprint"] == envelope["target_task_fingerprint"]
    ):
        _record_coordination_guard(payload, fingerprint)
        emit_pretool_deny(
            "Workflow Manager blocked coordination because source must bind the current session on the target host, "
            "and source/target must be different tasks on that same host."
        )
        return True
    if envelope["target_task_fingerprint"] != actual_task or envelope["target_host_fingerprint"] != actual_host:
        _record_coordination_guard(payload, fingerprint)
        emit_pretool_deny(
            "Workflow Manager blocked coordination because the envelope target fingerprints do not bind the actual "
            "threadId/hostId. Call list_threads again and rebuild the envelope from the exact active peer."
        )
        return True
    if envelope["sender_resource_identity"] != envelope["target_resource_identity"]:
        _record_coordination_guard(payload, fingerprint)
        emit_pretool_deny(
            "Workflow Manager blocked coordination because sender/target resource identities differ; unrelated "
            "resources do not justify a cross-task notification."
        )
        return True
    conflict = coordination_conflict_class(envelope)
    if not conflict:
        _record_coordination_guard(payload, fingerprint)
        emit_pretool_deny(
            "Workflow Manager blocked coordination because the declared stages are compatible with the resource kind; "
            "do not notify a peer without a real conflicting stage."
        )
        return True
    identity = coordination_notice_identity(envelope)
    request_fingerprint = stable_hash(f"workflow-coordination-request-v1\0{fingerprint}", 32)
    decision: dict[str, Any] = {}

    def reserve_pending(current: dict[str, Any]) -> None:
        def deny(reason: str) -> None:
            decision.update({"allowed": False, "reason": reason})
            current.setdefault("guards", []).append(
                {
                    "at": utc_now(),
                    "turn_id": safe_label(payload.get("turn_id"), 120)
                    if payload.get("turn_id")
                    else None,
                    "kind": "live_coordination",
                    "action": "deny",
                    "fingerprint": fingerprint,
                }
            )

        activities = current.get("coordination_activity", [])
        source_snapshot = next(
            (
                item
                for item in activities
                if item.get("task_fingerprint") == source_task
                and item.get("host_fingerprint") == actual_host
            ),
            None,
        )
        target_snapshot = next(
            (
                item
                for item in activities
                if item.get("task_fingerprint") == actual_task
                and item.get("host_fingerprint") == actual_host
            ),
            None,
        )
        if not source_snapshot or not coordination_snapshot_fresh(source_snapshot):
            deny(
                "Workflow Manager blocked coordination because the current session lacks a fresh list_threads "
                "snapshot on the target's exact host. Call list_threads; the Hook cannot query host activity itself."
            )
            return
        if source_snapshot.get("status") != "active":
            deny(
                f"Workflow Manager blocked coordination because the current session is not active "
                f"(status={source_snapshot.get('status')}) on the target's exact host."
            )
            return
        if not target_snapshot or not coordination_snapshot_fresh(target_snapshot):
            deny(
                "Workflow Manager blocked coordination without a fresh list_threads snapshot for this exact peer. "
                "Call list_threads; the Hook cannot query host activity itself."
            )
            return
        if target_snapshot.get("status") != "active":
            deny(
                f"Workflow Manager blocked coordination because the target is not active "
                f"(status={target_snapshot.get('status')}); idle, notLoaded, completed, or missing peers must not be notified."
            )
            return

        notices = current.setdefault("coordination_notices", [])
        existing = next(
            (item for item in reversed(notices) if item.get("notice_fingerprint") == identity["notice_fingerprint"]),
            None,
        )
        if existing and any(
            existing.get(key) != identity[key]
            for key in ("owner_fingerprint", "scope_fingerprint", "phase_fingerprint")
        ):
            deny("Workflow Manager blocked coordination because the existing lease owner or phase does not match.")
            return
        scope_blocks = [
            item
            for item in notices
            if item.get("scope_fingerprint") == identity["scope_fingerprint"]
            and item.get("transition") == "blocked"
        ]
        generation = safe_int(envelope.get("lease_generation"))
        if envelope.get("transition") == "blocked" and not existing and scope_blocks:
            if generation <= max(safe_int(item.get("lease_generation")) for item in scope_blocks):
                deny("Workflow Manager blocked coordination because a new blocked lease generation must increase monotonically.")
                return
        if envelope.get("transition") == "released":
            latest = max(scope_blocks, key=lambda item: safe_int(item.get("lease_generation"))) if scope_blocks else None
            if (
                not latest
                or latest.get("state") not in {"sent", "unconfirmed"}
                or generation != safe_int(latest.get("lease_generation"))
                or latest.get("owner_fingerprint") != identity["owner_fingerprint"]
                or latest.get("phase_fingerprint") != identity["phase_fingerprint"]
            ):
                deny(
                    "Workflow Manager blocked released coordination because it does not match the current blocked "
                    "generation, owner, resource, and phase."
                )
                return
        if existing and existing.get("state") in {"sent", "unconfirmed"}:
            deny("Workflow Manager blocked coordination because this peer/resource/generation/transition notice was already sent or is otherwise terminal.")
            return
        if existing and existing.get("state") == "pending":
            deny("Workflow Manager blocked coordination because the identical notice is already pending.")
            return
        if existing and (existing.get("state") == "exhausted" or safe_int(existing.get("attempt")) >= 2):
            deny("Workflow Manager blocked coordination because the one normal retry is exhausted.")
            return
        if existing and existing.get("state") == "failed":
            try:
                failure_time = datetime.fromisoformat(str(existing.get("at") or ""))
                source_time = datetime.fromisoformat(str(source_snapshot.get("observed_at") or ""))
                target_time = datetime.fromisoformat(str(target_snapshot.get("observed_at") or ""))
            except ValueError:
                failure_time = source_time = target_time = datetime.min.replace(tzinfo=timezone.utc)
            if min(source_time, target_time) <= failure_time:
                deny(
                    "Workflow Manager blocked the retry until a fresh successful list_threads snapshot is observed "
                    "after the failed send."
                )
                return

        attempt = 2 if existing and existing.get("state") == "failed" else 1
        if existing:
            notices.remove(existing)
        notices.append(
            {
                **identity,
                "resource_identity": envelope["sender_resource_identity"],
                "resource_kind": envelope["resource_kind"],
                "lease_generation": envelope["lease_generation"],
                "transition": envelope["transition"],
                "state": "pending",
                "attempt": attempt,
                "request_fingerprint": request_fingerprint,
                "at": coordination_now(),
            }
        )
        decision["allowed"] = True

    if state_path(payload) is None:
        emit_pretool_deny(
            "Workflow Manager blocked coordination because an atomic session ledger is unavailable."
        )
        return True
    _, changed = mutate_state(payload, reserve_pending)
    if not changed or "allowed" not in decision:
        emit_pretool_deny(
            "Workflow Manager blocked coordination because the atomic session reservation was unavailable or already processed."
        )
    elif not decision["allowed"]:
        emit_pretool_deny(str(decision.get("reason") or "Workflow Manager blocked coordination."))
    return True


STALL_DIAGNOSIS_REQUEST_RE = re.compile(
    r"^STALL_DIAGNOSIS_REQUEST stall_id=([0-9a-f]{32}) "
    r"assessor_binding_id=([0-9a-f]{32}) objective_fingerprint=([0-9a-f]{8,64}) "
    r"execution_contract_id=([0-9a-f]{32}) mode=read_only$"
)


def handle_stall_diagnosis_pretool(
    payload: dict[str, Any], state: dict[str, Any], fingerprint: str
) -> bool:
    if not normalized_key(payload.get("tool_name")).endswith("followuptask"):
        return False
    stall = _safe_stall(state.get("stall"))
    if stall.get("state") == "none":
        return False
    resolution = subagent_request_resolution(payload)
    leaf = resolution.get("leaf") if isinstance(resolution.get("leaf"), dict) else {}
    request = str(leaf.get("message") or "")
    target = str(leaf.get("target") or "")
    marker_lines = [line for line in request.splitlines() if line.startswith("STALL_DIAGNOSIS_REQUEST")]
    if stall.get("state") == "resolved" and not marker_lines:
        return False
    matches = [match for line in marker_lines if (match := STALL_DIAGNOSIS_REQUEST_RE.fullmatch(line))]
    marker = matches[0] if len(marker_lines) == len(matches) == 1 else None
    reason = resolution.get("error")
    if subagent_request_visibility(payload) == "opaque_v2":
        reason = reason or (
            "opaque V2 followup does not expose stall_id and execution_contract_id for pre-dispatch binding; "
            "fail closed and replan instead of weakening diagnosis authorization"
        )
    elif stall.get("state") != "diagnosis_required":
        reason = reason or "stall diagnosis is not awaiting delivery"
    elif safe_int(stall.get("diagnosis_attempt")) >= MAX_STALL_DIAGNOSIS_ATTEMPTS:
        reason = reason or "stall diagnosis delivery retry is exhausted"
    elif not marker:
        reason = reason or "stall diagnosis marker is missing or malformed"
    elif target != str(state.get("assessor_agent_id") or ""):
        reason = reason or "stall diagnosis target does not match the original assessor"
    elif marker.group(1) != stall.get("stall_id"):
        reason = reason or "stall_id mismatch"
    elif marker.group(2) != state.get("assessor_binding_id"):
        reason = reason or "assessor binding mismatch"
    elif marker.group(3) != state.get("objective", {}).get("fingerprint"):
        reason = reason or "stall objective mismatch"
    elif marker.group(4) != state.get("execution_contract_id"):
        reason = reason or "stall execution contract mismatch"
    elif not subagent_request_is_read_only(payload):
        reason = reason or "stall diagnosis first round must be strictly read-only"
    if reason:
        def reject(current: dict[str, Any]) -> None:
            current.setdefault("guards", []).append(
                {"at": utc_now(), "turn_id": safe_label(payload.get("turn_id"), 120) if payload.get("turn_id") else None, "kind": "stall_diagnosis", "action": "deny", "fingerprint": fingerprint}
            )
        mutate_state(payload, reject)
        emit_pretool_deny(f"Workflow Manager blocked stall diagnosis follow-up: {reason}.")
        return True

    decision = {"owned": False}

    def reserve(current: dict[str, Any]) -> None:
        current_stall = _safe_stall(current.get("stall"))
        if (
            current_stall.get("state") != "diagnosis_required"
            or current_stall.get("stall_id") != stall.get("stall_id")
            or safe_int(current_stall.get("diagnosis_attempt")) >= MAX_STALL_DIAGNOSIS_ATTEMPTS
        ):
            current.setdefault("guards", []).append(
                {"at": utc_now(), "turn_id": safe_label(payload.get("turn_id"), 120) if payload.get("turn_id") else None, "kind": "stall_diagnosis", "action": "deny", "fingerprint": fingerprint}
            )
            return
        request_fingerprint = stable_hash(f"stall-diagnosis-request-v1\0{fingerprint}", 32)
        if not append_result_pending_subagent(
            current,
            agent_id=current.get("assessor_agent_id"),
            request_fingerprint=request_fingerprint,
        ):
            current.setdefault("guards", []).append(
                {"at": utc_now(), "turn_id": safe_label(payload.get("turn_id"), 120) if payload.get("turn_id") else None, "kind": "stall_diagnosis", "action": "deny", "fingerprint": fingerprint}
            )
            return
        current_stall["state"] = "diagnosis_pending"
        current_stall["diagnosis_attempt"] = safe_int(current_stall.get("diagnosis_attempt")) + 1
        current_stall["diagnosis_request_fingerprint"] = request_fingerprint
        current_stall["at"] = utc_now()
        current["stall"] = current_stall
        decision["owned"] = True

    _, changed = mutate_state(payload, reserve)
    if not changed or not decision["owned"]:
        emit_pretool_deny("Workflow Manager blocked duplicate or stale stall diagnosis follow-up.")
    return True


def terminal_executor_followup_reason(
    payload: dict[str, Any], state: dict[str, Any]
) -> str | None:
    """A terminal executor is immutable; recovery is always a fresh child spawn."""
    if not normalized_key(payload.get("tool_name")).endswith("followuptask"):
        return None
    candidates = subagent_request_candidates(payload)
    target = str(candidates[0].get("target") or "").strip() if candidates else ""
    if not target:
        return None
    visible_target = target.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    for group in reversed(subagent_lifecycle_groups(state)):
        if group.get("state") != "terminal":
            continue
        request = group.get("request") if isinstance(group.get("request"), dict) else {}
        start = group.get("start") if isinstance(group.get("start"), dict) else {}
        if request.get("role") != "confirmed_executor":
            continue
        identities = {
            str(group.get("agent_id") or ""),
            str(start.get("agent_id") or ""),
            str(request.get("task_name") or ""),
        }
        if target in identities or visible_target in identities:
            return (
                "terminal confirmed executor follow-up is forbidden; terminal v1 cannot be revived. "
                f"Spawn a fresh child with visible task_name={bound_executor_task_name(state)} and the "
                "current contract, failure kind, attempt, and material correction"
            )
    return None


def pre_tool_use(payload: dict[str, Any]) -> None:
    fingerprint, tool = tool_fingerprint(payload)
    state = snapshot_state(payload)
    snapshot_failure = str(state.get("_snapshot_failure") or "")
    if snapshot_failure:
        mutating = plan_confirmation_guard(
            payload,
            {
                **state,
                "work_difficulty": "hard",
                "plan_state": "awaiting_confirmation",
                "confirmed_plan_digest": None,
            },
        )
        if mutating:
            emit_pretool_deny(
                "Workflow Manager blocked "
                f"{mutating}: canonical state/journal availability failed "
                f"({snapshot_failure}); mutation cannot fail open."
            )
            return
    if handle_coordination_pretool(payload, state, fingerprint):
        return
    terminal_followup = terminal_executor_followup_reason(payload, state)
    if terminal_followup:
        def record_terminal_followup_guard(current: dict[str, Any]) -> None:
            current.setdefault("guards", []).append(
                {
                    "at": utc_now(),
                    "turn_id": safe_label(payload.get("turn_id"), 120)
                    if payload.get("turn_id")
                    else None,
                    "kind": "executor_terminal_followup",
                    "action": "deny",
                    "fingerprint": fingerprint,
                }
            )

        mutate_state(payload, record_terminal_followup_guard)
        emit_pretool_deny(f"Workflow Manager blocked follow-up: {terminal_followup}.")
        return
    if handle_stall_diagnosis_pretool(payload, state, fingerprint):
        return
    stall_state = _safe_stall(state.get("stall")).get("state")
    if is_subagent_spawn_tool(payload) and stall_state in {
        "diagnosis_required", "diagnosis_pending", "diagnosing", "exhausted"
    }:
        emit_pretool_deny(
            "Workflow Manager blocked a new executor or assessor: stall diagnosis must reuse the original bound "
            "high assessor and complete before recovery; exhausted stalls require replan."
        )
        return
    if is_subagent_spawn_tool(payload) and protected_subagent_lifecycle_count(state) >= MAX_SUBAGENTS:
        def record_lifecycle_overflow(current: dict[str, Any]) -> None:
            current.setdefault("guards", []).append(
                {
                    "at": utc_now(),
                    "turn_id": safe_label(payload.get("turn_id"), 120) if payload.get("turn_id") else None,
                    "kind": "subagent_lifecycle_overflow",
                    "action": "deny",
                    "fingerprint": fingerprint,
                }
            )

        mutate_state(payload, record_lifecycle_overflow)
        emit_pretool_deny(
            "Subagent spawn denied: the protected lifecycle limit is reached; preserve pending, live, and current "
            "bound assessor/executor records instead of dropping them."
        )
        return
    if normalized_key(payload.get("tool_name")).endswith("followuptask") and state.get("assessor_state") in {"simple_execution_required", "recovery_required"}:
        request = subagent_request_text(payload)
        target = next((str(candidate.get("target") or "") for candidate in subagent_request_candidates(payload) if candidate.get("target")), "")
        binding = str(state.get("assessor_binding_id") or "")
        opaque_bound_target = opaque_v2_bound_assessor_target(payload, state)
        solve = opaque_bound_target or bool(re.search(r"(?:solve|解决)", request, re.I))
        verify = opaque_bound_target or bool(re.search(r"(?:verify|验证|测试)", request, re.I))
        binding_marker = opaque_bound_target or f"assessor_binding_id={binding}" in request
        target_matches = opaque_bound_target or target == str(state.get("assessor_agent_id") or "")
        recovery_ok = state.get("assessor_state") != "recovery_required" or (
            f"recovery_from={state.get('assessor_failure_kind')}" in request
            and bool(re.search(r"material_correction\s*[:=]\s*\S.{7,}", request))
            and safe_int(state.get("assessor_attempt")) < 2
        )
        if target_matches and binding and binding_marker and solve and verify and recovery_ok:
            followup_decision = {"accepted": False}

            def start_simple_execution(current: dict[str, Any]) -> None:
                if current.get("assessor_state") not in {"simple_execution_required", "recovery_required"} or not append_result_pending_subagent(
                    current,
                    agent_id=(current.get("assessor_agent_id") if opaque_bound_target else target),
                    request_fingerprint=fingerprint,
                ):
                    current.setdefault("guards", []).append(
                        {"at": utc_now(), "turn_id": safe_label(payload.get("turn_id"), 120) if payload.get("turn_id") else None, "kind": "subagent_lifecycle", "action": "deny", "fingerprint": fingerprint}
                    )
                    return
                current["assessor_state"] = "simple_running"
                if current.get("assessor_failure_kind") == "simple_execution_invalid":
                    current["assessor_attempt"] = safe_int(current.get("assessor_attempt")) + 1
                current["assessor_failure_kind"] = None
                followup_decision["accepted"] = True
            mutate_state(payload, start_simple_execution)
            if not followup_decision["accepted"]:
                emit_pretool_deny("Workflow Manager blocked duplicate or stale Simple assessor follow-up lifecycle.")
            return
        emit_pretool_deny("Workflow Manager blocked Simple assessor follow-up: target, binding, and solve/verify contract must match the original assessor.")
        return
    if is_subagent_spawn_tool(payload):
        request_resolution = subagent_request_resolution(payload)
        assessor_intent = subagent_request_has_assessor_intent(payload)
        assessor_ok, assessor_reason = confirmed_assessor_request(payload, state)
        if assessor_ok:
            def record_assessor(current: dict[str, Any]) -> None:
                options = subagent_request_options(payload)
                task_name, scope_fingerprint = subagent_request_fields(payload)
                current["assessor_state"] = "spawn_pending"
                current["assessor_attempt"] = safe_int(current.get("assessor_attempt")) + 1
                current["assessor_model"] = safe_label(options.get("model"), 80)
                current["assessor_reasoning_effort"] = safe_label(options.get("reasoning_effort"), 24)
                current["assessor_fork_turns"] = options.get("fork_turns")
                current.setdefault("subagents", []).append({"at": utc_now(), "event": "request", "turn_id": safe_label(payload.get("turn_id"), 120) if payload.get("turn_id") else None, "task_name": task_name, "scope_fingerprint": scope_fingerprint, "status": "pending", "requested": True, "host_accepted": None, "request_gate": "open", "request_visibility": subagent_request_visibility(payload), "request_cap": 1, "role": "high_assessor", "contract_id": current.get("assessor_binding_id"), "request_fingerprint": fingerprint, "objective_fingerprint": current.get("objective", {}).get("fingerprint"), "model": current["assessor_model"], "reasoning_effort": current["assessor_reasoning_effort"], "fork_turns": current["assessor_fork_turns"], "attempt": current["assessor_attempt"]})
            mutate_state(payload, record_assessor)
            return
        request_text = subagent_request_text(payload)
        if assessor_intent:
            if assessor_reason == "assessor retry exhausted":
                def exhaust_assessor(current: dict[str, Any]) -> None:
                    current["assessor_state"] = "failed"
                    current["assessor_failure_kind"] = "retry_exhausted"
                mutate_state(payload, exhaust_assessor)
            emit_pretool_deny(
                f"Workflow Manager blocked assessor spawn: {assessor_reason or 'invalid assessor request'}."
            )
            return
        if request_resolution.get("error"):
            emit_pretool_deny(
                f"Workflow Manager blocked subagent spawn: {request_resolution['error']}; provide exactly one "
                "bounded request leaf and do not split fields across wrappers."
            )
            return
        route = safe_route(state.get("last_route"))
        request_is_shared = request_touches_shared_resource(request_text)
        if state.get("assessor_state") in {"spawn_required", "spawn_pending", "running", "recovery_required"} and route.get("delegation_gate") == "closed" and route.get("dependency_signal") in {"shared_resource", "ordered_shared"} and request_is_shared:
            def record_shared_gate(current: dict[str, Any]) -> None:
                current.setdefault("guards", []).append({"at": utc_now(), "turn_id": safe_label(payload.get("turn_id"), 120) if payload.get("turn_id") else None, "kind": "subagent_gate", "action": "deny", "fingerprint": fingerprint})
            mutate_state(payload, record_shared_gate)
            emit_pretool_deny("Workflow Manager blocked subagent spawn: delegation gate is closed by dependency/shared-resource policy.")
            return
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
    assessor_block = assessor_gate_guard(payload, state)
    if assessor_block:
        emit_pretool_deny(f"Workflow Manager blocked {assessor_block}; complete the bound high-tier assessment first.")
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
                if current.get("executor_state") == "spawn_required":
                    current["executor_failure_kind"] = (
                        "model_unavailable"
                        if executor_block == "model_unavailable"
                        else "invalid_spawn_config"
                    )
                    current["model_profile"] = "work_assessment"

        mutate_state(payload, record_executor_guard)
        if safe_session_execution_preference(state.get("session_execution_preference")) == "highest_throughout":
            profile_instruction = (
                "Use profile_resolution=highest_available and explicitly request the bound assessor/current "
                "highest-tier model plus the exact bound assessor reasoning_effort"
            )
        else:
            profile_instruction = (
                "Resolve the newest actually available lower-tier model and explicitly request "
                "reasoning_effort=medium"
            )
        emit_pretool_deny(
            f"Workflow Manager blocked {executor_block}: this confirmed hard-work plan requires exactly one "
            f"contract-bound executor. {profile_instruction} with visible task_name={bound_executor_task_name(state)}, "
            "fork_turns=1, and "
            "include the exact execution_contract_id, plan_digest, plan_generation, current slice_id and "
            "slice_contract_id, exclusive scope, and acceptance. The Hook did not switch the parent and cannot prove host support."
        )
        return
    if is_subagent_spawn_tool(payload):
        valid_executor, _ = confirmed_executor_request(payload, state)
        if valid_executor:
            if handle_subagent_pretool(payload, state, fingerprint):
                return
        route = safe_route(state.get("last_route"))
        gate = str(route.get("delegation_gate") or "closed")
        assessor_safe_side_lane = bool(
            state.get("assessor_state") == "running"
            and subagent_request_is_read_only(payload)
            and not request_touches_shared_resource(subagent_request_text(payload))
            and not route.get("delegation_opt_out")
        )
        caller = next((safe_label(payload.get(key), 120) for key in ("agent_id", "subagent_id") if payload.get(key)), None)
        assessor_safe_side_lane = assessor_safe_side_lane and caller != state.get("assessor_agent_id")
        if gate == "closed":
            if handle_subagent_pretool(payload, state, fingerprint, assessor_safe_side_lane=assessor_safe_side_lane):
                return
        elif subagent_request_is_read_only(payload):
            if handle_subagent_pretool(payload, state, fingerprint, assessor_safe_side_lane=assessor_safe_side_lane):
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
    current_category = command_category(payload)
    probe_kind = change_epoch_probe_kind(payload, current_category)
    if probe_kind and same_epoch_probe_seen(state, fingerprint, probe_kind):
        def record_epoch_throttle(current: dict[str, Any]) -> None:
            current.setdefault("guards", []).append(
                {
                    "at": utc_now(),
                    "turn_id": safe_label(payload.get("turn_id"), 120) if payload.get("turn_id") else None,
                    "kind": "change_epoch_throttle",
                    "action": "deny",
                    "fingerprint": fingerprint,
                }
            )
        mutate_state(payload, record_epoch_throttle)
        emit_pretool_deny(
            "Workflow Manager blocked an identical read-only probe in the current change epoch; reuse fresh evidence, make a material change, or narrow/split the question."
        )
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
    coordination_activity = (
        coordination_activity_from_response(response)
        if is_list_threads_tool(payload)
        else None
    )
    coordination_post: dict[str, str] | None = None
    if is_send_message_to_thread_tool(payload):
        fields, _ = coordination_send_fields(payload)
        envelope, _ = parse_coordination_envelope(fields.get("message") if fields else None)
        if fields and envelope:
            coordination_post = coordination_notice_identity(envelope)
            coordination_post["request_fingerprint"] = stable_hash(
                f"workflow-coordination-request-v1\0{fingerprint}", 32
            )
    coordination_send_state = "unconfirmed"
    if status_value.startswith("error") or status_value in ERROR_STATUSES:
        coordination_send_state = "failed"
    elif isinstance(response, dict):
        explicit = str(response.get("status") or response.get("state") or "").strip().lower()
        if explicit in {"ok", "accepted", "success"} or response.get("ok") is True or response.get("success") is True:
            coordination_send_state = "sent"
    stall_followup_fingerprint = (
        stable_hash(f"stall-diagnosis-request-v1\0{fingerprint}", 32)
        if normalized_key(payload.get("tool_name")).endswith("followuptask")
        else None
    )
    command = extract_command(payload)
    category = command_category(payload, command)
    host_input_digest = host_operation_input_digest(payload, command)
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
        if coordination_activity is not None:
            state["coordination_activity"] = coordination_activity
        if coordination_post:
            notice = next(
                (
                    item
                    for item in reversed(state.get("coordination_notices", []))
                    if item.get("notice_fingerprint") == coordination_post["notice_fingerprint"]
                    and item.get("request_fingerprint") == coordination_post["request_fingerprint"]
                    and item.get("state") == "pending"
                ),
                None,
            )
            if notice:
                if coordination_send_state == "sent":
                    notice["state"] = "sent"
                elif coordination_send_state == "unconfirmed":
                    notice["state"] = "unconfirmed"
                else:
                    notice["state"] = "exhausted" if safe_int(notice.get("attempt")) >= 2 else "failed"
                notice["at"] = coordination_now()
        if stall_followup_fingerprint:
            stall = _safe_stall(state.get("stall"))
            if (
                stall.get("state") == "diagnosis_pending"
                and stall.get("diagnosis_request_fingerprint") == stall_followup_fingerprint
            ):
                if status_value.startswith("error") or status_value in ERROR_STATUSES:
                    state.setdefault("subagents", []).append(
                        {
                            "at": utc_now(),
                            "event": "stop",
                            "agent_id": state.get("assessor_agent_id"),
                            "task_name": "high_assessor_followup",
                            "status": "failed",
                            "request_fingerprint": stall_followup_fingerprint,
                            "objective_fingerprint": state.get("objective", {}).get("fingerprint"),
                            "role": "high_assessor",
                            "contract_id": state.get("assessor_binding_id"),
                            "attempt": state.get("assessor_attempt"),
                        }
                    )
                    if safe_int(stall.get("diagnosis_attempt")) >= MAX_STALL_DIAGNOSIS_ATTEMPTS:
                        stall["state"] = "exhausted"
                        state["executor_state"] = "exhausted"
                    else:
                        stall["state"] = "diagnosis_required"
                else:
                    stall["state"] = "diagnosing"
                stall["at"] = utc_now()
                state["stall"] = stall
        active_plan_digest = (
            state.get("plan_digest")
            if state.get("plan_state") == "confirmed"
            and state.get("confirmed_plan_digest") == state.get("plan_digest")
            else None
        )
        epoch_before = safe_int(state.get("change_epoch"))
        active_executor_caller = bool(
            caller_id and caller_id == state.get("executor_agent_id")
        )
        parent_review_operation = bool(
            active_plan_digest
            and caller_id is None
            and state.get("executor_state") == "verification_required"
            and category in {"verification", "evidence"}
            and not command_mutates_files(command or "")
            and not command_risk_kind(payload, command or "")
            and not git_subcommand(command or "")
        )
        bind_current_slice = active_executor_caller or parent_review_operation
        state.setdefault("operations", []).append(
            {
                "at": utc_now(),
                "turn_id": safe_label(payload.get("turn_id"), 120) if payload.get("turn_id") else None,
                "host_event_turn_id": safe_label(payload.get("turn_id"), 120) if payload.get("turn_id") else None,
                "host_input_digest": host_input_digest,
                "tool": tool,
                "fingerprint": fingerprint,
                "status": status_value,
                "category": category,
                "plan_digest": active_plan_digest,
                "execution_contract_id": (
                    state.get("execution_contract_id")
                    if active_plan_digest and (active_executor_caller or parent_review_operation or state.get("executor_state") == "local_running")
                    else None
                ),
                "slice_id": (
                    (current_execution_slice(state) or {}).get("id")
                    if active_plan_digest and bind_current_slice
                    else None
                ),
                "slice_contract_id": (
                    slice_contract_id(state)
                    if active_plan_digest and bind_current_slice
                    else None
                ),
                "executor_agent_id": (
                    caller_id if active_executor_caller else None
                ),
                "assessor_binding_id": state.get("assessor_binding_id") if caller_id == state.get("assessor_agent_id") else None,
                "risk_kind": risk_kind,
                **response_meta,
                "budgeted": budgeted,
                "oversized": oversized,
                "compacted": compacted,
                "change_epoch": epoch_before,
            }
        )
        failed = status_value.startswith("error") or status_value in ERROR_STATUSES
        probe_kind = change_epoch_probe_kind(payload, category)
        if probe_kind and not failed:
            state.setdefault("change_epoch_ledger", []).append(
                {"epoch": epoch_before, "fingerprint": fingerprint, "kind": probe_kind, "at": utc_now()}
            )
            state["change_epoch_ledger"] = state["change_epoch_ledger"][-MAX_CHANGE_EPOCH_LEDGER:]
        # Only a completed implementation, build artifact, or deployment moves
        # the freshness boundary. Failed/denied probes never manufacture a new
        # epoch and therefore cannot weaken acceptance.
        if not failed and category in {"implementation", "build_package", "delivery_device"}:
            state["change_epoch"] = min(epoch_before + 1, MAX_EVENT_COUNT)
            state["change_epoch_ledger"] = []
        pending_spawn = next(
            (item for item in reversed(state.get("subagents", [])) if item.get("event") == "request" and item.get("request_fingerprint") == fingerprint),
            None,
        )
        # PreToolUse is only a request reservation.  A matching PostToolUse is
        # the sole host-acceptance signal and remains separate from Start.
        if pending_spawn:
            pending_spawn["host_accepted"] = not (
                status_value.startswith("error") or status_value in ERROR_STATUSES
            )
            pending_spawn["host_acceptance_status"] = status_value
            pending_spawn["host_acceptance_source"] = "PostToolUse"
            pending_spawn["host_accepted_at"] = utc_now()
        if failed and pending_spawn and pending_spawn.get("role") == "high_assessor" and state.get("assessor_state") == "spawn_pending":
            if safe_int(pending_spawn.get("attempt")) == safe_int(state.get("assessor_attempt")):
                state["assessor_state"] = "failed" if safe_int(state.get("assessor_attempt")) >= 2 else "recovery_required"
                state["assessor_failure_kind"] = "retry_exhausted" if state["assessor_state"] == "failed" else "spawn_failed"
        if failed and pending_spawn and pending_spawn.get("role") == "confirmed_executor" and state.get("executor_state") == "spawn_pending":
            if safe_int(pending_spawn.get("attempt")) == safe_int(state.get("executor_attempt")):
                state["executor_state"] = "exhausted" if safe_int(state.get("executor_attempt")) >= MAX_EXECUTOR_ATTEMPTS else "recovery_required"
                state["executor_failure_kind"] = "spawn_failed"
                state["model_profile"] = "work_assessment"
                if state["executor_state"] == "exhausted":
                    state["executor_review"] = _safe_executor_review(
                        {
                            "status": "exhausted",
                            "execution_contract_id": state.get(
                                "execution_contract_id"
                            ),
                            "attempt": state.get("executor_attempt"),
                            "at": utc_now(),
                        }
                    )
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
        stall = _safe_stall(state.get("stall"))
        resumed_failure = bool(
            failed
            and stall.get("state") == "resuming"
            and stall.get("execution_contract_id") == state.get("execution_contract_id")
            and (
                executor_operation
                or (
                    pending_spawn
                    and pending_spawn.get("role") == "confirmed_executor"
                    and pending_spawn.get("contract_id") == state.get("execution_contract_id")
                )
            )
        )
        if resumed_failure:
            stall["state"] = "exhausted"
            stall["at"] = utc_now()
            state["stall"] = stall
            state["executor_state"] = "exhausted"
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
                "source": "hook",
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
                "plan_artifact": _safe_plan_artifact(state.get("plan_artifact")),
                "execution_slices": _safe_execution_slices(
                    state.get("execution_slices")
                ),
                "session_execution_preference": safe_session_execution_preference(
                    state.get("session_execution_preference")
                ),
                "execution_profile_version": state.get("execution_profile_version"),
                "executor_state": state.get("executor_state", "none"),
                "execution_contract_id": state.get("execution_contract_id"),
                "executor_attempt": safe_int(state.get("executor_attempt")),
                "executor_failure_kind": state.get("executor_failure_kind"),
                "executor_review": _safe_executor_review(
                    state.get("executor_review")
                ),
                "assessor_state": state.get("assessor_state", "none"),
                "assessor_binding_id": state.get("assessor_binding_id"),
                "assessor_attempt": safe_int(state.get("assessor_attempt")),
                "assessor_failure_kind": state.get("assessor_failure_kind"),
                "assessor_observed_model": state.get("assessor_observed_model"),
                "assessor_observed_reasoning_effort": state.get("assessor_observed_reasoning_effort"),
                "assessor_start_observed": state.get("assessor_start_observed"),
                "assessor_observation_source": state.get("assessor_observation_source"),
                "executor_observed_model": state.get("executor_observed_model"),
                "executor_observed_reasoning_effort": state.get("executor_observed_reasoning_effort"),
                "executor_start_observed": state.get("executor_start_observed"),
                "executor_observation_source": state.get("executor_observation_source"),
                "reference_acceptance": _safe_reference_acceptance(state.get("reference_acceptance")),
                "last_execution_baseline": _safe_execution_baseline(
                    state.get("last_execution_baseline")
                ),
                "causal_review": _safe_causal_review(state.get("causal_review")),
                "stall": _safe_stall(state.get("stall")),
                "coordination_activity": [
                    safe
                    for raw in state.get("coordination_activity", [])
                    if (safe := _safe_coordination_activity(raw)) is not None
                ][:MAX_COORDINATION_ACTIVITY],
                "coordination_notices": [
                    safe
                    for raw in state.get("coordination_notices", [])
                    if (safe := _safe_coordination_notice(raw)) is not None
                ][-MAX_COORDINATION_NOTICES:],
                "coordination_inbound": [
                    safe
                    for raw in state.get("coordination_inbound", [])
                    if (safe := _safe_coordination_inbound(raw)) is not None
                ][-MAX_COORDINATION_INBOUND:],
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
    return [
        group["start"]
        for group in subagent_lifecycle_groups(state)
        if group.get("state") == "live" and isinstance(group.get("start"), dict)
    ]


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


EXECUTION_STALL_RE = re.compile(
    r"^EXECUTION_STALL contract_id=([0-9a-f]{32}) "
    r"failure_kind=([a-z_]+) evidence_digest=([0-9a-f]{32})$"
)
EXECUTION_RESULT_RE = re.compile(
    r"^EXECUTION_RESULT execution_contract_id=([0-9a-f]{32}) "
    r"slice_id=(s(?:0[1-9]|[12][0-9]|3[0-2])) "
    r"outcome=(succeeded|failed)$"
)
EXECUTION_RESULT_V6_RE = re.compile(
    r"^EXECUTION_RESULT execution_contract_id=([0-9a-f]{32}) "
    r"outcome=(succeeded|failed) evidence_digest=([0-9a-f]{32})$"
)
STALL_DIAGNOSIS_RE = re.compile(
    r"^STALL_DIAGNOSIS stall_id=([0-9a-f]{32}) assessor_binding_id=([0-9a-f]{32}) "
    r"outcome=(resume|replan) plan_digest=([0-9a-f]{32}) "
    r"execution_contract_id=([0-9a-f]{32}) remediation_digest=([0-9a-f]{32})$"
)


def _strict_terminal_marker(
    message: Any, keyword: str, exact: re.Pattern[str]
) -> tuple[re.Match[str] | None, str, bool]:
    """Return one unindented exact marker only when it is the final non-empty line."""
    normalized = str(message or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    intent_indexes = [
        index
        for index, line in enumerate(lines)
        if re.match(rf"^\s*{re.escape(keyword)}\b", line)
    ]
    nonempty = [index for index, line in enumerate(lines) if line.strip()]
    if len(intent_indexes) != 1:
        return None, normalized, bool(intent_indexes)
    index = intent_indexes[0]
    match = exact.fullmatch(lines[index])
    if match is None or not nonempty or index != nonempty[-1]:
        return None, normalized, True
    body = "\n".join(line for item, line in enumerate(lines) if item != index).strip()
    return match, body, True


def _slice_operations(state: dict[str, Any]) -> list[dict[str, Any]]:
    contract = state.get("execution_contract_id")
    item = current_execution_slice(state)
    bound_slice = slice_contract_id(state)
    if not contract or not item or not bound_slice:
        return []
    return [
        operation
        for operation in state.get("operations", [])
        if isinstance(operation, dict)
        and operation.get("execution_contract_id") == contract
        and operation.get("slice_id") == item.get("id")
        and operation.get("slice_contract_id") == bound_slice
    ]


def slice_operation_evidence(state: dict[str, Any]) -> dict[str, Any]:
    operations = _slice_operations(state)
    changes = {
        "implementation",
        "build_package",
        "delivery_device",
    }
    verification = {"verification", "evidence"}
    successful_change_indexes = [
        index
        for index, item in enumerate(operations)
        if item.get("category") in changes and item.get("status") in SUCCESS_STATUSES
    ]
    last_change = max(successful_change_indexes, default=-1)
    successful_verification_indexes = [
        index
        for index, item in enumerate(operations)
        if item.get("category") in verification
        and item.get("status") in SUCCESS_STATUSES
        and (last_change < 0 or index > last_change)
    ]
    # Executor verification proves only its candidate.  Terminal parent review
    # also requires a separate, host-recorded read-only parent operation.
    # executor_agent_id is populated only for the bound executor lane.
    successful_parent_review_indexes = [
        index
        for index, item in enumerate(operations)
        if item.get("category") in verification
        and item.get("status") in SUCCESS_STATUSES
        and item.get("executor_agent_id") is None
        and (last_change < 0 or index > last_change)
    ]
    facts = [
        {
            "category": item.get("category"),
            "fingerprint": item.get("fingerprint"),
            "status": item.get("status"),
            "tool": item.get("tool"),
        }
        for item in operations
    ]
    return {
        "change_evidence": bool(successful_change_indexes),
        "verification_evidence": bool(successful_verification_indexes),
        "parent_review_evidence": bool(successful_parent_review_indexes),
        "operation_digest": stable_hash(
            "workflow-manager-slice-operations-v1\0" + canonical_json(facts), 32
        ) if facts else None,
        "operation_count": len(facts),
    }


def host_evidence_digest(
    *,
    domain: str,
    state: dict[str, Any],
    agent_id: str,
    request_fingerprint: str | None,
    body_without_marker: str,
    outcome: str,
    candidate_review: dict[str, Any] | None = None,
    terminal_status: str | None = None,
    terminal_status_source: str | None = None,
) -> str:
    slice_item = current_execution_slice(state) or {}
    evidence = slice_operation_evidence(state)
    safe_candidate = _safe_executor_review(candidate_review) if candidate_review else {}
    candidate_projection = (
        {
            "attempt": safe_candidate.get("attempt"),
            "candidate_agent_fingerprint": safe_candidate.get("candidate_agent_fingerprint"),
            "candidate_evidence_digest": safe_candidate.get("candidate_evidence_digest"),
            "candidate_result_fingerprint": safe_candidate.get("candidate_result_fingerprint"),
            "digest_profile": safe_candidate.get("digest_profile"),
            "digest_source": safe_candidate.get("digest_source"),
            "execution_contract_id": safe_candidate.get("execution_contract_id"),
            "slice_contract_id": safe_candidate.get("slice_contract_id"),
            "slice_id": safe_candidate.get("slice_id"),
            "status": safe_candidate.get("status"),
            "terminal_status": safe_candidate.get("terminal_status"),
            "terminal_status_source": safe_candidate.get("terminal_status_source"),
        }
        if safe_candidate
        else None
    )
    material = {
        "agent_fingerprint": stable_hash(agent_id, 32),
        "attempt": safe_int(state.get("executor_attempt")),
        "baseline": evidence,
        "body": str(body_without_marker or "").replace("\r\n", "\n").replace("\r", "\n"),
        "candidate": candidate_projection,
        "execution_contract_id": state.get("execution_contract_id"),
        "execution_profile_version": str(state.get("execution_profile_version") or ""),
        "outcome": outcome,
        "plan_digest": state.get("plan_digest"),
        "request_fingerprint": safe_fingerprint(request_fingerprint) or None,
        "slice_contract_id": slice_contract_id(state),
        "slice_id": slice_item.get("id"),
        "terminal_status": terminal_status,
        "terminal_status_source": terminal_status_source,
    }
    return stable_hash(
        f"{EVIDENCE_DIGEST_PROFILE}:{domain}\0" + canonical_json(material), 32
    )


def executor_stall_id(state: dict[str, Any], failure_kind: str) -> str | None:
    objective = safe_fingerprint(state.get("objective", {}).get("fingerprint"))
    plan = _coordination_fp32(state.get("plan_digest"))
    contract = _coordination_fp32(state.get("execution_contract_id"))
    attempt = min(max(safe_int(state.get("executor_attempt")), 0), MAX_EXECUTOR_ATTEMPTS)
    if not objective or not plan or not contract or not attempt or failure_kind not in EXECUTOR_FAILURE_KINDS:
        return None
    return stable_hash(
        f"execution-stall-v1\0{objective}\0{plan}\0{contract}\0{attempt}\0{failure_kind}",
        32,
    )


def record_explicit_executor_stall(
    state: dict[str, Any], contract_id: str, failure_kind: str, evidence_digest: str
) -> bool:
    stall_id = executor_stall_id(state, failure_kind)
    contract_current = bool(
        contract_id == state.get("execution_contract_id")
        and contract_id == execution_contract_id(state)
        and failure_kind == state.get("executor_failure_kind")
        and state.get("plan_state") == "confirmed"
    )
    prior = _safe_stall(state.get("stall"))
    repeated = bool(
        prior.get("state") != "none"
        and prior.get("execution_contract_id") == contract_id
    )
    if not stall_id or not contract_current or repeated:
        if repeated:
            prior["state"] = "exhausted"
            prior["at"] = utc_now()
            state["stall"] = prior
        state["executor_state"] = "exhausted"
        state["model_profile"] = "work_assessment"
        return False
    state["stall"] = _safe_stall(
        {
            "state": "diagnosis_required",
            "stall_id": stall_id,
            "objective_fingerprint": state.get("objective", {}).get("fingerprint"),
            "plan_digest": state.get("plan_digest"),
            "execution_contract_id": contract_id,
            "evidence_digest": evidence_digest,
            "failure_kind": failure_kind,
            "resume_profile": confirmed_executor_model_profile(state),
            "executor_attempt": state.get("executor_attempt"),
            "diagnosis_attempt": 0,
            "at": utc_now(),
        }
    )
    state["executor_state"] = "recovery_required"
    state["executor_failure_kind"] = failure_kind
    state["model_profile"] = "work_assessment"
    return True


def pending_subagent_request(state: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any] | None:
    candidates = [
        group["request"]
        for group in subagent_lifecycle_groups(state)
        if group.get("state") == "pending" and isinstance(group.get("request"), dict)
    ]
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
                and (
                    str(state.get("execution_profile_version")) != EXECUTION_PROFILE_VERSION
                    or item.get("slice_contract_id") == slice_contract_id(state)
                )
                and safe_int(item.get("attempt")) == safe_int(state.get("executor_attempt"))
            ]
            return (executor_same_turn or same_turn)[-1]
    executor_pending = [
        item
        for item in candidates
        if item.get("role") == "confirmed_executor"
        and item.get("contract_id") == state.get("execution_contract_id")
        and (
            str(state.get("execution_profile_version")) != EXECUTION_PROFILE_VERSION
            or item.get("slice_contract_id") == slice_contract_id(state)
        )
        and safe_int(item.get("attempt")) == safe_int(state.get("executor_attempt"))
    ]
    assessor_pending = [item for item in candidates if item.get("role") == "high_assessor" and item.get("contract_id") == state.get("assessor_binding_id")]
    return (executor_pending or assessor_pending or candidates)[-1]


def subagent_start_conflict_reason(
    state: dict[str, Any], agent_id: str, request: dict[str, Any]
) -> str | None:
    if not agent_id:
        return "SubagentStart lacks a concrete agent_id"
    groups = subagent_lifecycle_groups(state)
    if any(group.get("state") == "live" and group.get("agent_id") == agent_id for group in groups):
        return "duplicate SubagentStart for an already-live agent"
    if (
        request.get("role") == "confirmed_executor"
        and safe_int(request.get("attempt")) == 2
    ):
        contract = safe_fingerprint(request.get("contract_id"))
        reused_terminal = any(
            group.get("state") == "terminal"
            and group.get("agent_id") == agent_id
            and isinstance(group.get("request"), dict)
            and group["request"].get("role") == "confirmed_executor"
            and group["request"].get("contract_id") == contract
            and group["request"].get("slice_contract_id")
            == request.get("slice_contract_id")
            and safe_int(group["request"].get("attempt")) == 1
            for group in groups
        )
        review = _safe_executor_review(state.get("executor_review"))
        reused_review_candidate = bool(
            review.get("execution_contract_id") == contract
            and review.get("slice_contract_id") == request.get("slice_contract_id")
            and review.get("candidate_agent_fingerprint")
            == stable_hash(agent_id, 32)
        )
        if reused_terminal or reused_review_candidate:
            return "fresh v2 confirmed executor cannot reuse the terminal v1 agent_id"
    terminal = [
        group for group in groups
        if group.get("state") == "terminal" and group.get("agent_id") == agent_id
    ]
    if not terminal:
        return None
    request_group = next(
        (group for group in groups if group.get("state") == "pending" and group.get("request") is request),
        None,
    )
    if not request_group or safe_int(request_group.get("first_index")) <= safe_int(terminal[-1].get("last_index")):
        return "late SubagentStart cannot revive a terminal agent without a newer bound request"
    return None


def subagent_start(payload: dict[str, Any]) -> None:
    previous = snapshot_state(payload)
    request = pending_subagent_request(previous, payload) or {}
    expected_request_fingerprint = safe_fingerprint(request.get("request_fingerprint"))
    scope_value = payload.get("prompt") or payload.get("task") or payload.get("message")
    scope_fingerprint = (stable_hash(scope_value) if scope_value else None) or request.get("scope_fingerprint")
    task_name = task_name_from_payload(payload) or request.get("task_name")
    request_fingerprint = request.get("request_fingerprint")
    executor_request = request.get("role") == "confirmed_executor"
    assessor_request = request.get("role") == "high_assessor"
    observed_model, observed_effort, observation_source = start_turn_observation(payload)
    observed_status = start_observation_status(
        observed_model, observed_effort, observation_source
    )
    payload_model = safe_label(payload.get("model"), 80) if payload.get("model") else None
    canonical_body: str | None = None
    canonical_handoff_error: str | None = None
    canonical_handoff_digest: str | None = None
    if executor_request:
        try:
            candidate_body = _read_verified_current_plan_revision(previous, payload)
            candidate_digest = stable_hash(candidate_body, 32)
            artifact = _safe_plan_artifact(previous.get("plan_artifact"))
            if candidate_digest != artifact.get("current_revision_digest"):
                raise PlanArtifactError("content_drift")
            parsed_slices = parse_execution_slice_manifest(candidate_body)
            persisted_slices = _safe_execution_slices(previous.get("execution_slices"))
            current_slice = current_execution_slice(previous)
            if (
                parsed_slices.get("manifest_digest")
                != persisted_slices.get("manifest_digest")
                or parsed_slices.get("global_constraints_digest")
                != persisted_slices.get("global_constraints_digest")
                or not current_slice
                or parsed_slices["items"][persisted_slices["current_index"] - 1]["slice_digest"]
                != current_slice.get("slice_digest")
            ):
                raise PlanArtifactError("content_drift")
            canonical_body = canonical_json(
                {
                    "global_constraints": parsed_slices["global_constraints"],
                    "slice": {
                        key: parsed_slices["items"][persisted_slices["current_index"] - 1][key]
                        for key in ("id", *EXECUTION_SLICE_FIELDS)
                    },
                    "slice_contract_id": slice_contract_id(previous),
                }
            ) + "\n"
            canonical_handoff_digest = candidate_digest
        except PlanArtifactError as error:
            canonical_handoff_error = error.code
        except OSError:
            canonical_handoff_error = "write_error"
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
    decision: dict[str, Any] = {"accepted": False}

    def update(state: dict[str, Any]) -> None:
        agent_id = safe_label(payload.get("agent_id"), 120)
        bound_request = pending_subagent_request(state, payload) or {}
        bound_request_fingerprint = safe_fingerprint(bound_request.get("request_fingerprint"))
        conflict = (
            "persisted SubagentStart request was already consumed or no longer matches"
            if expected_request_fingerprint
            and bound_request_fingerprint != expected_request_fingerprint
            else subagent_start_conflict_reason(state, agent_id, bound_request)
        )
        if conflict:
            state.setdefault("guards", []).append(
                {
                    "at": utc_now(),
                    "turn_id": safe_label(payload.get("turn_id"), 120) if payload.get("turn_id") else None,
                    "kind": "subagent_lifecycle",
                    "action": "deny",
                    "fingerprint": stable_hash(f"subagent-start\0{agent_id}\0{conflict}"),
                }
            )
            decision["reason"] = conflict
            return
        decision["accepted"] = True
        request_contract = bound_request.get("contract_id")
        highest = safe_session_execution_preference(
            state.get("session_execution_preference")
        ) == "highest_throughout"
        expected_model = highest_execution_model(state) if highest else None
        expected_effort = highest_execution_effort(state) if highest else "medium"
        echoed_model = payload_model
        echoed_effort = observed_effort
        identity = state.setdefault("identity_evidence", {})
        identity["start_echo_profile"] = stable_hash(
            canonical_json({"model": observed_model, "reasoning_effort": observed_effort, "source": observation_source}), 32
        ) if observed_model or observed_effort else None
        contract_matches = bool(
            executor_request
            and canonical_body is not None
            and canonical_handoff_digest == state.get("plan_digest")
            and state.get("executor_state") == "spawn_pending"
            and request_contract
            and request_contract == state.get("execution_contract_id")
            and request_contract == execution_contract_id(state)
            and bound_request.get("slice_id")
            == (current_execution_slice(state) or {}).get("id")
            and bound_request.get("slice_contract_id") == slice_contract_id(state)
            and bound_request.get("model") == state.get("executor_model")
            and bound_request.get("reasoning_effort") == expected_effort
            and (not highest or bound_request.get("model") == expected_model)
            and bound_request.get("host_accepted") is True
            and observed_status == "full"
            and echoed_model == bound_request.get("model")
            and observed_model == echoed_model
            and echoed_effort == bound_request.get("reasoning_effort")
            and bound_request.get("fork_turns") == state.get("executor_fork_turns")
            and safe_int(bound_request.get("attempt")) == safe_int(state.get("executor_attempt"))
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
                "requested": True,
                "host_accepted": bound_request.get("host_accepted"),
                "start_observed": observed_status,
                "observation_source": observation_source,
                "role": bound_request.get("role") or "lane",
                "contract_id": request_contract,
                "model": bound_request.get("model"),
                "reasoning_effort": bound_request.get("reasoning_effort"),
                "fork_turns": bound_request.get("fork_turns"),
                "attempt": bound_request.get("attempt"),
                "slice_id": bound_request.get("slice_id"),
                "slice_contract_id": bound_request.get("slice_contract_id"),
                "recovery_from": bound_request.get("recovery_from"),
                "plan_handoff_digest": canonical_handoff_digest,
            }
        )
        if (
            (executor_request or assessor_request)
            and observed_status in {"absent", "partial"}
            and not any(item.get("kind") == "start_profile_capability" for item in state.get("guards", []))
        ):
            state.setdefault("guards", []).append(
                {
                    "at": utc_now(),
                    "turn_id": safe_label(payload.get("turn_id"), 120) if payload.get("turn_id") else None,
                    "kind": "start_profile_capability",
                    "action": "advise",
                    "fingerprint": stable_hash(f"start-profile-capability\0{observed_status}", 32),
                }
            )
            decision["capability_notice"] = observed_status
        if executor_request:
            state["executor_observed_model"] = observed_model
            state["executor_observed_reasoning_effort"] = echoed_effort
            state["executor_start_observed"] = observed_status
            state["executor_observation_source"] = observation_source
            state["executor_observed_effective"] = bool(contract_matches)
            if contract_matches:
                state["executor_state"] = "running"
                state["executor_agent_id"] = agent_id
                state["executor_failure_kind"] = None
            else:
                state["executor_state"] = (
                    "recovery_required"
                    if safe_int(state.get("executor_attempt"))
                    < MAX_EXECUTOR_ATTEMPTS
                    else "exhausted"
                )
                state["executor_agent_id"] = None
                state["executor_failure_kind"] = (
                    "protocol_missing_model"
                    if not payload_model
                    else
                    "stale_contract"
                    if canonical_handoff_error in {"unsafe_path", "content_drift", "journal_full"}
                    or state.get("execution_contract_id") != execution_contract_id(state)
                    else "start_mismatch"
                )
                if state["executor_state"] == "exhausted":
                    state["executor_review"] = _safe_executor_review(
                        {
                            "status": "exhausted",
                            "execution_contract_id": state.get(
                                "execution_contract_id"
                            ),
                            "attempt": state.get("executor_attempt"),
                            "at": utc_now(),
                        }
                    )
        if assessor_request:
            bound = bool(state.get("assessor_state") == "spawn_pending" and bound_request.get("contract_id") == state.get("assessor_binding_id") and objective_fingerprint == state.get("objective", {}).get("fingerprint") and safe_int(bound_request.get("attempt")) == safe_int(state.get("assessor_attempt")))
            matched = bool(
                bound
                and bound_request.get("host_accepted") is True
                and observed_status == "full"
                and echoed_model == state.get("assessor_model")
                and observed_model == echoed_model
                and echoed_effort == state.get("assessor_reasoning_effort")
            )
            state["assessor_agent_id"] = agent_id if matched else None
            state["assessor_observed_model"] = observed_model
            state["assessor_observed_reasoning_effort"] = echoed_effort
            state["assessor_start_observed"] = observed_status
            state["assessor_observation_source"] = observation_source
            state["assessor_observed_effective"] = bool(matched)
            state["assessor_state"] = "running" if matched else ("failed" if safe_int(state.get("assessor_attempt")) >= 2 else "recovery_required")
            state["assessor_failure_kind"] = None if matched else ("retry_exhausted" if state["assessor_state"] == "failed" else ("protocol_missing_model" if not payload_model else "start_mismatch"))

    _, changed = mutate_state(payload, update)
    if not decision["accepted"]:
        emit_context(
            "SubagentStart",
            f"Workflow Manager ignored an invalid lifecycle transition: {decision.get('reason') or 'state update unavailable'}. "
            "A terminal agent stays terminal unless a newer persisted request explicitly binds a new generation.",
        )
        return
    warnings: list[str] = []
    if decision.get("capability_notice"):
        warnings.append(
            "Host Start profile capability is incomplete "
            f"(state={decision['capability_notice']}, model={observed_model}, effort={observed_effort}, source={observation_source}); "
            "bound roles remain fail-closed. This capability boundary is recorded once per session."
        )
    if executor_request:
        refreshed = snapshot_state(payload)
        if refreshed.get("executor_state") != "running":
            warnings.append(
                "Confirmed executor start did not match the persisted contract/config or the private canonical "
                "handoff could not be verified; do not mutate and return control for recovery."
            )
        else:
            if refreshed.get("executor_observed_effective"):
                warnings.append(
                    "Confirmed executor request and observed start profile match "
                    f"(model={refreshed.get('executor_observed_model')}, effort={refreshed.get('executor_observed_reasoning_effort')}, "
                    f"source={refreshed.get('executor_observation_source')}). Execute only the bound plan and acceptance."
                )
            else:
                warnings.append(
                    "Confirmed executor start is fail-closed: observed "
                    f"model={refreshed.get('executor_observed_model')}, effort={refreshed.get('executor_observed_reasoning_effort')}, "
                    f"source={refreshed.get('executor_observation_source')}, state={refreshed.get('executor_start_observed')}; do not mutate."
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
    private_handoff = ""
    if executor_request and canonical_body is not None and refreshed.get("executor_state") == "running":
        relative_path = _safe_plan_artifact(refreshed.get("plan_artifact")).get(
            "relative_path"
        )
        private_handoff = (
            " Canonical executor handoff was verified from the trusted plugin-data journal. "
            f"relative_path={relative_path} is plugin-data-root-relative contract metadata only; never resolve "
            "it against cwd or a workspace. Only the global constraints and current execution "
            "slice are injected; the rest of the large plan is intentionally withheld:\n"
            "BEGIN_WORKFLOW_MANAGER_EXECUTION_SLICE\n"
            f"{canonical_body}"
            "END_WORKFLOW_MANAGER_EXECUTION_SLICE"
        )
    elif executor_request:
        private_handoff = (
            " Canonical executor handoff verification failed"
            f" ({canonical_handoff_error or 'binding_mismatch'}); no plan body was delivered. Do not mutate."
        )
    emit_context(
        "SubagentStart",
        "Workflow Manager subagent contract: stay inside the assigned scope, do not redo parent or sibling work, keep "
        "raw logs out of the result, and return only decisive evidence, exact paths/identifiers, uncertainty, "
        f"verification, and the next action. Use a concise Chinese purpose summary in user-facing updates."
        f"{warning_text}{private_handoff}",
    )
def subagent_stop(payload: dict[str, Any]) -> None:
    result = payload.get("last_assistant_message")
    declared_status = str(payload.get("status") or "").strip().lower()
    declared_success = declared_status in {
        "complete",
        "completed",
        "done",
        "ok",
        "success",
        "succeeded",
    }
    explicit_unknown_status = bool(
        declared_status
        and not declared_success
        and declared_status not in ERROR_STATUSES
    )
    terminal_status = "completed" if declared_success else "missing" if not declared_status else None
    terminal_status_source = (
        "host_declared_success"
        if declared_success
        else "host_missing" if not declared_status else None
    )
    status_value = (
        "completed"
        if declared_success
        else declared_status if declared_status in ERROR_STATUSES else "unknown"
    )
    agent_id = safe_label(payload.get("agent_id"), 120)
    previous = snapshot_state(payload)
    started = next(
        (item for item in reversed(active_agent_records(previous)) if item.get("agent_id") == agent_id),
        None,
    )
    if started is None:
        result_group = next(
            (
                group
                for group in reversed(subagent_lifecycle_groups(previous))
                if group.get("state") == "result_pending" and group.get("agent_id") == agent_id
            ),
            None,
        )
        started = (result_group or {}).get("request")
    started_objective = str((started or {}).get("objective_fingerprint") or "")
    current_objective = str(previous.get("objective", {}).get("fingerprint") or "")
    stale = bool(started_objective and current_objective and started_objective != current_objective)
    executor_agent = bool((started or {}).get("role") == "confirmed_executor")
    previous_stall = _safe_stall(previous.get("stall"))
    stall_assessor = bool(
        previous_stall.get("state") == "diagnosing"
        and agent_id == previous.get("assessor_agent_id")
    )
    assessor_agent = bool((started or {}).get("role") == "high_assessor") or bool(
        previous.get("assessor_state") == "simple_running" and agent_id == previous.get("assessor_agent_id")
    ) or stall_assessor
    assessment = re.search(r"(?im)^\s*WORK_ASSESSMENT\s+binding_id=([0-9a-f]{32})\s+outcome=(simple|hard)\s+evidence_digest=([0-9a-f]{32})\s*$", str(result or ""))
    hard_plan_detailed = bool(len(re.findall(r"(?m)^\s*(?:[-*]|\d+[.)、])\s+", str(result or ""))) >= 2 and re.search(r"(?:验收|验证|test|verify|acceptance)", str(result or ""), re.I) and re.search(r"计划已就绪，等待确认后执行[。.!！\s]*$", str(result or "")))
    simple_execution = re.search(r"(?im)^\s*SIMPLE_EXECUTION\s+binding_id=([0-9a-f]{32})\s+evidence_digest=([0-9a-f]{32})\s*$", str(result or ""))
    stall_lines = [line for line in str(result or "").splitlines() if line.startswith("EXECUTION_STALL")]
    stall_matches = [match for line in stall_lines if (match := EXECUTION_STALL_RE.fullmatch(line))]
    execution_stall_intent = bool(stall_lines)
    execution_stall = stall_matches[0] if len(stall_lines) == len(stall_matches) == 1 else None
    result_profile_v7 = str(previous.get("execution_profile_version")) == EXECUTION_PROFILE_VERSION
    execution_result, execution_result_body, execution_result_intent = _strict_terminal_marker(
        result,
        "EXECUTION_RESULT",
        EXECUTION_RESULT_RE if result_profile_v7 else EXECUTION_RESULT_V6_RE,
    )
    diagnosis_lines = [line for line in str(result or "").splitlines() if line.startswith("STALL_DIAGNOSIS")]
    diagnosis_matches = [match for line in diagnosis_lines if (match := STALL_DIAGNOSIS_RE.fullmatch(line))]
    stall_diagnosis = diagnosis_matches[0] if len(diagnosis_lines) == len(diagnosis_matches) == 1 else None
    decision: dict[str, Any] = {"recorded": False}

    def terminal_succeeded(bound_start: dict[str, Any] | None) -> bool:
        return bool(
            bound_start is not None
            and str(result or "").strip()
            and (status_value == "completed" or not declared_status)
            and not explicit_unknown_status
        )

    def update(state: dict[str, Any]) -> None:
        reconcile_unknown_operations_from_transcript(payload, state)
        current_result_group = None
        current_started = next(
            (item for item in reversed(active_agent_records(state)) if item.get("agent_id") == agent_id),
            None,
        )
        if current_started is None:
            current_result_group = next(
                (
                    group
                    for group in reversed(subagent_lifecycle_groups(state))
                    if group.get("state") == "result_pending" and group.get("agent_id") == agent_id
                ),
                None,
            )
            current_started = (current_result_group or {}).get("request")
        successful = terminal_succeeded(current_started)
        execution_result_current = bool(
            execution_result
            and execution_result.group(1) == state.get("execution_contract_id")
            and (
                not result_profile_v7
                or execution_result.group(2) == (current_execution_slice(state) or {}).get("id")
            )
        )
        execution_result_outcome = (
            execution_result.group(3)
            if execution_result and result_profile_v7
            else execution_result.group(2) if execution_result else None
        )
        execution_result_succeeded = bool(
            execution_result_current and execution_result_outcome == "succeeded"
        )
        if executor_agent:
            # Desktop can omit terminal status.  That route needs the unique exact
            # result marker; a declared failed/cancelled status can never succeed.
            successful = bool(
                current_started is not None
                and execution_result_succeeded
                and status_value not in ERROR_STATUSES
                and not explicit_unknown_status
                and not (execution_result_current and execution_result_outcome == "failed")
            )
        already_terminal = any(
            group.get("state") == "terminal" and group.get("agent_id") == agent_id
            for group in subagent_lifecycle_groups(state)
        )
        if not agent_id or (current_started is None and already_terminal):
            reason = "SubagentStop lacks a concrete agent_id" if not agent_id else "duplicate or late SubagentStop for a terminal agent"
            state.setdefault("guards", []).append(
                {
                    "at": utc_now(),
                    "turn_id": safe_label(payload.get("turn_id"), 120) if payload.get("turn_id") else None,
                    "kind": "subagent_lifecycle",
                    "action": "deny",
                    "fingerprint": stable_hash(f"subagent-stop\0{agent_id}\0{reason}"),
                }
            )
            decision["reason"] = reason
            return
        if already_terminal and current_started is not None:
            payload_request = safe_fingerprint(payload.get("request_fingerprint"))
            current_request = safe_fingerprint(current_started.get("request_fingerprint"))
            payload_turn = safe_label(payload.get("turn_id"), 120) if payload.get("turn_id") else None
            current_turn = current_started.get("turn_id")
            exact_request = bool(payload_request and current_request and payload_request == current_request)
            exact_turn = bool(payload_turn and current_turn and payload_turn == current_turn)
            bound_simple = bool(
                current_result_group
                and state.get("assessor_state") == "simple_running"
                and agent_id == state.get("assessor_agent_id")
                and simple_execution
                and simple_execution.group(1) == state.get("assessor_binding_id")
            )
            bound_stall = bool(
                current_result_group
                and _safe_stall(state.get("stall")).get("state") == "diagnosing"
                and agent_id == state.get("assessor_agent_id")
                and diagnosis_lines
            )
            if not (exact_request or exact_turn or bound_simple or bound_stall):
                reason = "SubagentStop is ambiguous after agent_id reuse and requires generation reconciliation"
                state.setdefault("guards", []).append(
                    {
                        "at": utc_now(),
                        "turn_id": payload_turn,
                        "kind": "subagent_lifecycle_ambiguous",
                        "action": "deny",
                        "fingerprint": stable_hash(f"subagent-stop\0{agent_id}\0ambiguous"),
                    }
                )
                decision["reason"] = reason
                return
        effective_started = current_started
        candidate_evidence_digest = (
            host_evidence_digest(
                domain="executor-result-v1",
                state=state,
                agent_id=agent_id,
                request_fingerprint=(effective_started or {}).get("request_fingerprint"),
                body_without_marker=execution_result_body,
                outcome=str(execution_result_outcome or "invalid"),
                terminal_status=terminal_status,
                terminal_status_source=terminal_status_source,
            )
            if executor_agent and execution_result_current
            else None
        )
        state.setdefault("subagents", []).append(
            {
                "at": utc_now(),
                "event": "stop",
                "agent_id": agent_id,
                "agent_type": safe_label(payload.get("agent_type"), 80),
                "task_name": task_name_from_payload(payload) or (effective_started or {}).get("task_name"),
                "scope_fingerprint": (effective_started or {}).get("scope_fingerprint"),
                "request_fingerprint": (effective_started or {}).get("request_fingerprint"),
                "objective_fingerprint": started_objective or None,
                "stale": stale,
                "status": status_value,
                "result_meta": text_metadata(result),
                "execution_result_contract_match": execution_result_current if executor_agent else None,
                "execution_result_outcome": execution_result_outcome if execution_result_current else None,
                "execution_result_evidence_digest": candidate_evidence_digest,
                "evidence_digest_profile": EVIDENCE_DIGEST_PROFILE if candidate_evidence_digest else None,
                "evidence_digest_source": EVIDENCE_DIGEST_SOURCE if candidate_evidence_digest else None,
                "terminal_status": terminal_status if candidate_evidence_digest else None,
                "terminal_status_source": terminal_status_source if candidate_evidence_digest else None,
                "role": (effective_started or {}).get("role") or "lane",
                "contract_id": (effective_started or {}).get("contract_id"),
                "slice_id": (effective_started or {}).get("slice_id"),
                "slice_contract_id": (effective_started or {}).get("slice_contract_id"),
                "model": (effective_started or {}).get("model"),
                "reasoning_effort": (effective_started or {}).get("reasoning_effort"),
                "fork_turns": (effective_started or {}).get("fork_turns"),
                "attempt": (effective_started or {}).get("attempt"),
            }
        )
        decision["recorded"] = True
        decision["successful"] = successful
        if executor_agent and state.get("executor_agent_id") == agent_id:
            contract_current = bool(
                not stale
                and (effective_started or {}).get("contract_id") == state.get("execution_contract_id")
                and state.get("execution_contract_id") == execution_contract_id(state)
                and (
                    not result_profile_v7
                    or (effective_started or {}).get("slice_id")
                    == (current_execution_slice(state) or {}).get("id")
                    and (effective_started or {}).get("slice_contract_id")
                    == slice_contract_id(state)
                )
            )
            if execution_stall_intent:
                valid_stall = bool(
                    not successful
                    and contract_current
                    and execution_stall
                    and execution_stall.group(1) == state.get("execution_contract_id")
                    and execution_stall.group(2) in EXECUTOR_FAILURE_KINDS
                )
                if valid_stall:
                    record_explicit_executor_stall(
                        state,
                        execution_stall.group(1),
                        execution_stall.group(2),
                        execution_stall.group(3),
                    )
                else:
                    state["executor_state"] = "exhausted"
                    state["model_profile"] = "work_assessment"
                return
            if not contract_current:
                state["executor_state"] = "recovery_required"
                state["executor_failure_kind"] = "stale_contract"
                state["model_profile"] = "work_assessment"
            elif successful and state.get("executor_state") == "running":
                state["executor_state"] = "verification_required"
                state["executor_agent_id"] = None
                state["executor_failure_kind"] = None
                baseline = build_execution_baseline(state)
                if baseline:
                    baseline["acceptance_status"] = "incomplete"
                    state["last_execution_baseline"] = baseline
                    state["causal_review"] = _safe_causal_review(None)
                state["executor_review"] = _safe_executor_review(
                    {
                        "status": "review_required",
                        "execution_contract_id": state.get("execution_contract_id"),
                        "slice_id": (current_execution_slice(state) or {}).get("id"),
                        "slice_contract_id": slice_contract_id(state),
                        "attempt": state.get("executor_attempt"),
                        "candidate_result_fingerprint": stable_hash(
                            execution_result_body, 32
                        ),
                        "candidate_agent_fingerprint": stable_hash(agent_id, 32),
                        "candidate_evidence_digest": candidate_evidence_digest,
                        "review_evidence_digest": None,
                        "digest_profile": EVIDENCE_DIGEST_PROFILE,
                        "digest_source": EVIDENCE_DIGEST_SOURCE,
                        "terminal_status": terminal_status,
                        "terminal_status_source": terminal_status_source,
                        "at": utc_now(),
                    }
                )
                state["model_profile"] = "work_assessment"
            elif not successful:
                state["executor_state"] = "recovery_required" if safe_int(state.get("executor_attempt")) < MAX_EXECUTOR_ATTEMPTS else "exhausted"
                state["executor_failure_kind"] = (
                    state.get("executor_failure_kind")
                    if state.get("executor_failure_kind") in EXECUTOR_FAILURE_KINDS
                    else "executor_failed"
                )
                state["model_profile"] = "work_assessment"
                stall = _safe_stall(state.get("stall"))
                if (
                    stall.get("state") == "resuming"
                    and stall.get("execution_contract_id") == state.get("execution_contract_id")
                ):
                    stall["state"] = "exhausted"
                    stall["at"] = utc_now()
                    state["stall"] = stall
                    state["executor_state"] = "exhausted"
                if safe_int(state.get("executor_attempt")) >= MAX_EXECUTOR_ATTEMPTS:
                    state["executor_review"] = _safe_executor_review(
                        {
                            "status": "exhausted",
                            "execution_contract_id": state.get(
                                "execution_contract_id"
                            ),
                            "attempt": state.get("executor_attempt"),
                            "at": utc_now(),
                        }
                    )
        if stall_assessor:
            stall = _safe_stall(state.get("stall"))
            valid = bool(
                successful
                and stall.get("state") == "diagnosing"
                and stall_diagnosis
                and stall_diagnosis.group(1) == stall.get("stall_id")
                and stall_diagnosis.group(2) == state.get("assessor_binding_id")
                and stall_diagnosis.group(4) == stall.get("plan_digest") == state.get("plan_digest")
                and stall_diagnosis.group(5) == stall.get("execution_contract_id") == state.get("execution_contract_id")
            )
            if not valid:
                stall["state"] = "exhausted"
                state["executor_state"] = "exhausted"
                state["model_profile"] = "work_assessment"
                state["stall"] = stall
                return
            stall["remediation_digest"] = stall_diagnosis.group(6)
            stall["at"] = utc_now()
            if stall_diagnosis.group(3) == "resume":
                stall["state"] = "resume_required"
                state["executor_state"] = "recovery_required"
                state["model_profile"] = stall.get("resume_profile")
            else:
                stall["state"] = "resolved"
                state["plan_state"] = "analyzing"
                state["plan_digest"] = None
                state["plan_objective_fingerprint"] = None
                state["plan_difficulty_decision_id"] = None
                state["confirmed_plan_digest"] = None
                state["confirmed_at"] = None
                reset_executor_binding(state)
                state["model_profile"] = "work_assessment"
            state["stall"] = stall
            return
        if assessor_agent and state.get("assessor_agent_id") == agent_id:
            if state.get("assessor_state") == "simple_running":
                if successful and simple_execution and simple_execution.group(1) == state.get("assessor_binding_id"):
                    state["assessor_state"] = "simple_complete"
                    state["assessor_failure_kind"] = None
                else:
                    state["assessor_state"] = "failed" if safe_int(state.get("assessor_attempt")) >= 2 else "recovery_required"
                    state["assessor_failure_kind"] = "retry_exhausted" if state["assessor_state"] == "failed" else "simple_execution_invalid"
                return
            mutated = any(item.get("assessor_binding_id") == state.get("assessor_binding_id") and item.get("category") in {"implementation", "build_package", "delivery_device", "git"} and item.get("status") in SUCCESS_STATUSES for item in state.get("operations", []))
            valid = bool(not stale and successful and assessment and assessment.group(1) == state.get("assessor_binding_id"))
            if assessment and assessment.group(2).lower() == "hard" and mutated:
                state["assessor_state"] = "failed"
                state["assessor_failure_kind"] = "hard_mutation_before_confirmation"
            elif not valid:
                state["assessor_state"] = "failed" if safe_int(state.get("assessor_attempt")) >= 2 else "recovery_required"
                state["assessor_failure_kind"] = "retry_exhausted" if state["assessor_state"] == "failed" else "assessment_result_invalid"
            elif assessment.group(2).lower() == "simple":
                state["assessor_state"] = "simple_execution_required"
                state["work_difficulty"] = "simple"
                state["difficulty_confidence"] = "high"
                state["difficulty_rule_codes"] = ["assessor_simple"]
                state["difficulty_decision_id"] = stable_hash(f"{state.get('assessor_binding_id')}\0{assessment.group(3)}", 24)
                state["plan_state"] = "none"
            else:
                detailed = bool(len(re.findall(r"(?m)^\s*(?:[-*]|\d+[.)、])\s+", str(result or ""))) >= 2 and re.search(r"(?:验收|验证|test|verify|acceptance)", str(result or ""), re.I) and re.search(r"计划已就绪，等待确认后执行[。.!！\s]*$", str(result or "")))
                if not detailed:
                    state["assessor_state"] = "failed" if safe_int(state.get("assessor_attempt")) >= 2 else "recovery_required"
                    state["assessor_failure_kind"] = "retry_exhausted" if state["assessor_state"] == "failed" else "hard_plan_incomplete"
                    return
                state["assessor_state"] = "hard_plan_ready"
                state["work_difficulty"] = "hard"
                state["difficulty_confidence"] = "high"
                state["difficulty_rule_codes"] = ["assessor_hard"]
                state["difficulty_decision_id"] = stable_hash(f"{state.get('assessor_binding_id')}\0{assessment.group(3)}", 24)
                state["plan_state"] = "analyzing"
                state["confirmed_plan_digest"] = None
                state["confirmed_at"] = None
                reset_executor_binding(state)
                state["model_profile"] = confirmed_executor_model_profile(state)
                if write_plan_artifact(state, payload, str(result or "")):
                    state["plan_state"] = "awaiting_confirmation"
            state["last_route"] = {**safe_route(state.get("last_route")), "work_difficulty": state.get("work_difficulty"), "difficulty_confidence": state.get("difficulty_confidence"), "difficulty_rule_codes": state.get("difficulty_rule_codes"), "difficulty_classifier_version": DIFFICULTY_CLASSIFIER_VERSION, "difficulty_decision_id": state.get("difficulty_decision_id"), "model_profile": state.get("model_profile"), "at": utc_now()}

    updated_state, _ = mutate_state(payload, update)
    artifact = _safe_plan_artifact(updated_state.get("plan_artifact"))
    if artifact.get("write_status") in {
        "write_failed",
        "content_drift",
        "revision_too_large",
        "journal_full",
        "transaction_recovery_failed",
    }:
        emit_context(
            "SubagentStop",
            f"Workflow Manager canonical plan journal {artifact['write_status']} warning_code={artifact['warning_code']}; confirmation and execution remain locked until a trusted revision commits.",
        )
        return
    if not decision["recorded"]:
        emit_context(
            "SubagentStop",
            f"Workflow Manager treated this as a no-op terminal lifecycle event: {decision.get('reason') or 'state update unavailable'}.",
        )
        return
    if stale:
        emit_context(
            "SubagentStop",
            "Stale subagent result: the objective changed after this agent started. Use it only as verification; "
            "it must not drive mutation for the previous objective.",
        )
    elif executor_agent and not decision.get("successful"):
        emit_context(
            "SubagentStop",
            "Confirmed executor failed. Return to the high-reasoning parent for one diagnosis. Do not repeat "
            "unchanged actions: either make a material correction and use the one remaining bounded executor "
            "attempt, or invalidate/replan when scope or acceptance changes.",
        )
    elif executor_agent and updated_state.get("executor_state") == "verification_required":
        contract = updated_state.get("execution_contract_id")
        slice_item = current_execution_slice(updated_state) or {}
        slice_contract = slice_contract_id(updated_state)
        emit_context(
            "SubagentStop",
            "Executor self-report is only a candidate. The high-reasoning parent must independently inspect "
            "the bounded artifacts and verification evidence. End the parent turn with exactly one line: "
            f"EXECUTION_REVIEW execution_contract_id={contract} slice_id={slice_item.get('id')} "
            "outcome=passed|failed. The Hook generates and normalizes "
            "the evidence digest. Only passed with bound operation evidence advances the slice; a failed review may use one fresh, "
            "evidence-bound verification_failed v2 child and must never revive v1.",
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
EXECUTION_REVIEW_RE = re.compile(
    r"^EXECUTION_REVIEW execution_contract_id=([0-9a-f]{32}) "
    r"slice_id=(s(?:0[1-9]|[12][0-9]|3[0-2])) "
    r"outcome=(passed|failed)$"
)
EXECUTION_REVIEW_V6_RE = re.compile(
    r"^EXECUTION_REVIEW execution_contract_id=([0-9a-f]{32}) "
    r"outcome=(passed|failed) evidence_digest=([0-9a-f]{32})$"
)


def stop(payload: dict[str, Any]) -> None:
    assistant_message = str(payload.get("last_assistant_message") or "")
    previous = snapshot_state(payload)
    review_profile_v7 = (
        str(previous.get("execution_profile_version")) == EXECUTION_PROFILE_VERSION
    )
    execution_review, execution_review_body, execution_review_intent = _strict_terminal_marker(
        assistant_message,
        "EXECUTION_REVIEW",
        EXECUTION_REVIEW_RE if review_profile_v7 else EXECUTION_REVIEW_V6_RE,
    )
    local_execution = re.search(r"(?im)^\s*LOCAL_EXECUTION\s+execution_contract_id=([0-9a-f]{32})\s+outcome=(succeeded|failed)\s+evidence_digest=([0-9a-f]{32})\s*$", assistant_message)
    causal_match = CAUSAL_REVIEW_RESULT_RE.search(assistant_message)
    plan_ready = bool(
        len(re.findall(r"(?m)^\s*(?:[-*]|\d+[.)、])\s+", assistant_message)) >= 2
        and re.search(r"(?:验收|验证|test|verify|acceptance)", assistant_message, re.I)
        and re.search(r"计划已就绪，等待确认后执行[。.!！\s]*$", assistant_message)
    )

    def update(state: dict[str, Any]) -> None:
        reconcile_unknown_operations_from_transcript(payload, state)
        state["last_assistant"] = text_metadata(assistant_message)
        state["last_stop_at"] = utc_now()
        if (
            state.get("plan_state") == "confirmed"
            and state.get("executor_state") == "verification_required"
        ):
            review = _safe_executor_review(state.get("executor_review"))
            contract_id = state.get("execution_contract_id")
            profile_v7 = (
                str(state.get("execution_profile_version"))
                == EXECUTION_PROFILE_VERSION
            )
            current_slice = current_execution_slice(state)
            current_slice_contract = slice_contract_id(state)
            review_outcome = (
                execution_review.group(3)
                if execution_review and profile_v7
                else execution_review.group(2) if execution_review else None
            )
            binding_valid = bool(
                execution_review
                and execution_review.group(1) == contract_id
                and (
                    contract_id == execution_contract_id(state)
                    if profile_v7
                    else str(state.get("execution_profile_version")) in {"5", "6"}
                )
                and (
                    not profile_v7
                    or current_slice
                    and execution_review.group(2) == current_slice.get("id")
                    and review.get("slice_id") == current_slice.get("id")
                    and review.get("slice_contract_id") == current_slice_contract
                )
                and review.get("status") == "review_required"
                and review.get("execution_contract_id") == contract_id
                and review.get("attempt") == state.get("executor_attempt")
                and review.get("candidate_result_fingerprint")
                and review.get("candidate_evidence_digest")
            )
            if not binding_valid:
                if execution_review_intent:
                    state.setdefault("guards", []).append(
                        {
                            "at": utc_now(),
                            "turn_id": (
                                safe_label(payload.get("turn_id"), 120)
                                if payload.get("turn_id")
                                else None
                            ),
                            "kind": "executor_review",
                            "action": "deny",
                            "fingerprint": stable_hash(assistant_message, 32),
                        }
                    )
                return
            if not profile_v7:
                # A durable Schema 23/profile 5 or 6 candidate is review-only
                # compatibility. 1.0.42 could persist a migrated Schema 22
                # candidate with profile 5 before the parent review arrived.
                # Ignore its self-reported digest. A pass may seal the existing
                # bounded baseline; a failure invalidates it and never grants a
                # new v7 attempt budget.
                baseline = _safe_execution_baseline(state.get("last_execution_baseline"))
                baseline_bound = bool(
                    baseline
                    and baseline.get("execution_contract_id") == contract_id
                    and baseline.get("objective_fingerprint")
                    == state.get("objective", {}).get("fingerprint")
                    and baseline.get("plan_digest") == state.get("plan_digest")
                    and _coordination_fp32(baseline.get("change_set_digest"))
                    and _coordination_fp32(baseline.get("verification_digest"))
                )
                legacy_digest = host_evidence_digest(
                    domain="parent-review-v1",
                    state=state,
                    agent_id=str(payload.get("agent_id") or "parent"),
                    request_fingerprint=review.get("candidate_result_fingerprint"),
                    body_without_marker=execution_review_body,
                    outcome=str(review_outcome or "invalid"),
                    candidate_review=review,
                )
                review["review_evidence_digest"] = legacy_digest
                review["digest_profile"] = f"{EVIDENCE_DIGEST_PROFILE}-legacy-v6"
                review["digest_source"] = EVIDENCE_DIGEST_SOURCE
                review["at"] = utc_now()
                if review_outcome == "passed" and baseline_bound:
                    baseline["acceptance_status"] = "passed"
                    state["last_execution_baseline"] = baseline
                    state["executor_state"] = "succeeded"
                    state["executor_failure_kind"] = None
                    review["status"] = "passed"
                else:
                    if baseline_bound:
                        baseline["acceptance_status"] = "failed"
                        state["last_execution_baseline"] = baseline
                    state["plan_state"] = "invalidated"
                    state["confirmed_plan_digest"] = None
                    state["confirmed_at"] = None
                    state["executor_state"] = "exhausted"
                    state["executor_failure_kind"] = "verification_failed"
                    review["status"] = "exhausted"
                    state["model_profile"] = "work_assessment"
                state["executor_review"] = review
                return

            operation_evidence = slice_operation_evidence(state)
            evidence_digest = host_evidence_digest(
                domain="parent-review-v1",
                state=state,
                agent_id=str(payload.get("agent_id") or "parent"),
                request_fingerprint=review.get("candidate_result_fingerprint"),
                body_without_marker=execution_review_body,
                outcome=str(review_outcome or "invalid"),
                candidate_review=review,
            )
            review["review_evidence_digest"] = evidence_digest
            review["digest_profile"] = EVIDENCE_DIGEST_PROFILE
            review["digest_source"] = EVIDENCE_DIGEST_SOURCE
            review["at"] = utc_now()
            passed_with_evidence = bool(
                review_outcome == "passed"
                and operation_evidence.get("verification_evidence")
                and operation_evidence.get("parent_review_evidence")
                and operation_evidence.get("operation_digest")
            )
            if passed_with_evidence:
                slices = _safe_execution_slices(state.get("execution_slices"))
                item_index = safe_int(slices.get("current_index")) - 1
                item = slices["items"][item_index]
                prior_chain = slices["completed_chain"]
                completion_digest = stable_hash(
                    "workflow-manager-slice-completion-v1\0"
                    + canonical_json(
                        {
                            "candidate_evidence_digest": review.get("candidate_evidence_digest"),
                            "execution_contract_id": contract_id,
                            "operation_digest": operation_evidence["operation_digest"],
                            "review_evidence_digest": evidence_digest,
                            "slice_contract_id": current_slice_contract,
                            "slice_digest": item["slice_digest"],
                            "slice_id": item["id"],
                        }
                    ),
                    32,
                )
                item.update(
                    {
                        "status": "passed",
                        "completion_digest": completion_digest,
                        "review_digest": evidence_digest,
                        "operation_digest": operation_evidence["operation_digest"],
                        "change_evidence": bool(operation_evidence.get("change_evidence")),
                        "verification_evidence": True,
                    }
                )
                slices["current_index"] = item_index + 2
                slices["completed_chain"] = stable_hash(
                    "workflow-manager-slice-chain-step-v1\0"
                    + canonical_json(
                        {
                            "completion_digest": completion_digest,
                            "previous_chain": prior_chain,
                            "slice_digest": item["slice_digest"],
                            "slice_id": item["id"],
                        }
                    ),
                    32,
                )
                state["execution_slices"] = slices
                if slices["current_index"] <= slices["count"]:
                    state["executor_state"] = "spawn_required"
                    state["executor_agent_id"] = None
                    state["executor_attempt"] = 0
                    state["executor_failure_kind"] = None
                    state["executor_review"] = _empty_executor_review()
                    state["model_profile"] = confirmed_executor_model_profile(state)
                    baseline = build_execution_baseline(state)
                    if baseline:
                        baseline["acceptance_status"] = "incomplete"
                        state["last_execution_baseline"] = baseline
                    return
                expected_chain = recompute_completed_slice_chain(slices)
                all_passed = all(
                    item.get("status") == "passed"
                    and item.get("verification_evidence")
                    for item in slices["items"]
                )
                any_change = any(item.get("change_evidence") for item in slices["items"])
                chain_valid = bool(expected_chain and expected_chain == slices.get("completed_chain"))
                if all_passed and any_change and chain_valid:
                    operation_digests = [item["operation_digest"] for item in slices["items"]]
                    change_digests = [
                        item["operation_digest"]
                        for item in slices["items"]
                        if item.get("change_evidence")
                    ]
                    baseline = {
                        "baseline_id": stable_hash(
                            "workflow-manager-sliced-baseline-v1\0"
                            + canonical_json(
                                {
                                    "chain": expected_chain,
                                    "contract": contract_id,
                                    "operations": operation_digests,
                                }
                            ),
                            32,
                        ),
                        "objective_fingerprint": state.get("objective", {}).get("fingerprint"),
                        "plan_digest": state.get("plan_digest"),
                        "execution_contract_id": contract_id,
                        "change_set_digest": stable_hash(canonical_json(change_digests), 32),
                        "verification_digest": stable_hash(canonical_json(operation_digests), 32),
                        "acceptance_status": "passed",
                    }
                    state["last_execution_baseline"] = baseline
                    state["executor_state"] = "succeeded"
                    state["executor_failure_kind"] = None
                    review["status"] = "passed"
                    state["executor_review"] = review
                    state["model_profile"] = confirmed_executor_model_profile(state)
                    state["causal_review"] = _safe_causal_review(None)
                    stall = _safe_stall(state.get("stall"))
                    if (
                        stall.get("state") == "resuming"
                        and stall.get("execution_contract_id") == contract_id
                    ):
                        stall["state"] = "resolved"
                        stall["at"] = utc_now()
                        state["stall"] = stall
                    return
                passed_with_evidence = False
                # Roll back the unsealable final completion before bounded recovery.
                item.update(
                    {
                        "status": "pending",
                        "completion_digest": None,
                        "review_digest": None,
                        "operation_digest": None,
                        "change_evidence": False,
                        "verification_evidence": False,
                    }
                )
                slices["current_index"] = item_index + 1
                slices["completed_chain"] = prior_chain
                state["execution_slices"] = slices
                state["executor_state"] = (
                    "recovery_required"
                    if safe_int(state.get("executor_attempt")) < MAX_EXECUTOR_ATTEMPTS
                    else "exhausted"
                )
                state["executor_failure_kind"] = "verification_failed"
                baseline = _safe_execution_baseline(state.get("last_execution_baseline")) or build_execution_baseline(state)
                if baseline:
                    baseline["acceptance_status"] = "failed"
                    state["last_execution_baseline"] = baseline
                review["status"] = (
                    "failed" if state["executor_state"] == "recovery_required" else "exhausted"
                )
                state["model_profile"] = "work_assessment"
                state["executor_review"] = review
                return
            else:
                if review_outcome == "passed":
                    # A pass without host-bound parent verification is an
                    # evidence repair, not a failed implementation.  Preserve
                    # attempt one and leave the parent able to add the missing
                    # read-only evidence then submit a fresh review marker.
                    review["status"] = "review_required"
                    state["executor_state"] = "verification_required"
                    state["executor_failure_kind"] = None
                    state.setdefault("guards", []).append(
                        {
                            "at": utc_now(),
                            "turn_id": safe_label(payload.get("turn_id"), 120) if payload.get("turn_id") else None,
                            "kind": "parent_review_evidence_missing",
                            "action": "advise",
                            "fingerprint": stable_hash(
                                f"{contract_id}\0{current_slice_contract}\0{review.get('candidate_result_fingerprint')}", 32
                            ),
                        }
                    )
                    state["executor_review"] = review
                    return
                baseline = _safe_execution_baseline(state.get("last_execution_baseline")) or build_execution_baseline(state)
                if baseline:
                    baseline["acceptance_status"] = "failed"
                    state["last_execution_baseline"] = baseline
                state["executor_state"] = (
                    "recovery_required"
                    if safe_int(state.get("executor_attempt"))
                    < MAX_EXECUTOR_ATTEMPTS
                    else "exhausted"
                )
                state["executor_failure_kind"] = "verification_failed"
                review["status"] = (
                    "failed"
                    if state["executor_state"] == "recovery_required"
                    else "exhausted"
                )
                state["model_profile"] = "work_assessment"
                stall = _safe_stall(state.get("stall"))
                if (
                    state["executor_state"] == "exhausted"
                    and stall.get("state") == "resuming"
                    and stall.get("execution_contract_id") == contract_id
                ):
                    stall["state"] = "exhausted"
                    stall["at"] = utc_now()
                    state["stall"] = stall
            state["executor_review"] = review
            return
        if state.get("plan_state") == "confirmed" and state.get("executor_state") == "local_running":
            if local_execution and local_execution.group(1) == state.get("execution_contract_id"):
                if local_execution.group(2) == "succeeded":
                    bound_operations = [item for item in state.get("operations", []) if item.get("execution_contract_id") == state.get("execution_contract_id") and item.get("status") in SUCCESS_STATUSES]
                    substantive_indexes = [index for index, item in enumerate(bound_operations) if item.get("category") in {"implementation", "build_package", "delivery_device", "evidence"}]
                    verification_indexes = [index for index, item in enumerate(bound_operations) if item.get("category") == "verification"]
                    if substantive_indexes and verification_indexes and max(verification_indexes) > min(substantive_indexes):
                        state["executor_state"] = "succeeded"
                        state["executor_failure_kind"] = None
                        baseline = build_execution_baseline(state)
                        if baseline:
                            state["last_execution_baseline"] = baseline
                            state["causal_review"] = _safe_causal_review(None)
                    else:
                        state["executor_state"] = "recovery_required"
                        state["executor_failure_kind"] = "verification_failed"
                else:
                    state["executor_state"] = "recovery_required"
                    state["executor_failure_kind"] = "executor_failed"
                    state["model_profile"] = "current"
            return
        review_state = _safe_causal_review(state.get("causal_review")).get("state")
        if state.get("task_domain") == "work" and review_state not in {"triage_required", "triaging"} and state.get("assessor_state") not in {"hard_plan_ready", "simple_complete"} and state.get("assessor_failure_kind") != "delegation_opt_out":
            return
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
                        state["assessor_generation"] = max(safe_int(state.get("assessor_generation")), 0) + 1
                        state["assessor_binding_id"] = assessor_binding_id(state)
                        state["assessor_state"] = "spawn_required"
                        state["assessor_agent_id"] = None
                        state["assessor_attempt"] = 0
                        state["assessor_failure_kind"] = None
                        state["assessor_input_fingerprint"] = state.get("objective", {}).get("fingerprint")
                    state["causal_review"] = review
            return
        if state.get("work_difficulty") == "hard" and state.get("plan_state") in {
            "analyzing",
            "invalidated",
        }:
            if plan_ready:
                state["confirmed_plan_digest"] = None
                state["confirmed_at"] = None
                reset_executor_binding(state)
                state["plan_state"] = "analyzing"
                if write_plan_artifact(state, payload, assistant_message):
                    state["plan_state"] = "awaiting_confirmation"
            else:
                state["plan_state"] = "analyzing"

    updated_state, _ = mutate_state(payload, update)
    artifact = _safe_plan_artifact(updated_state.get("plan_artifact"))
    if artifact.get("write_status") in {
        "write_failed",
        "content_drift",
        "revision_too_large",
        "journal_full",
        "transaction_recovery_failed",
    }:
        emit_context(
            "Stop",
            f"Workflow Manager canonical plan journal {artifact['write_status']} warning_code={artifact['warning_code']}; confirmation and execution remain locked until a trusted revision commits.",
        )
        return
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
