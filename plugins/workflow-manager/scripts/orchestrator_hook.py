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


def _release_metadata() -> dict[str, Any]:
    """Read the one checked-in release identity before accepting any state."""
    # The POSIX runner executes a content-addressed copy of this file.  Its
    # sibling directory deliberately contains only the hook, so prefer the
    # trusted plugin root supplied by that runner and fall back to source.
    configured_root = os.environ.get("PLUGIN_ROOT")
    path = (
        Path(configured_root) / "release_metadata.json"
        if configured_root
        else Path(__file__).resolve().parents[1] / "release_metadata.json"
    )
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except OSError:
        # A legacy single-file cache may predate the metadata companion.  It
        # has no authority to upgrade state; retain the last compatible
        # release identity so the lifecycle hook can fail open and the next
        # normal runner refresh restores the structured source of truth.
        value = {"version": "1.0.60", "schema": 33, "execution_profile": "12", "stable_skill_schema": 9}
    if not (
        isinstance(value, dict)
        and isinstance(value.get("version"), str)
        and re.fullmatch(r"\d+\.\d+\.\d+", value["version"])
        and isinstance(value.get("schema"), int)
        and isinstance(value.get("execution_profile"), str)
        and value["execution_profile"].isdigit()
        and isinstance(value.get("stable_skill_schema"), int)
    ):
        raise RuntimeError("invalid Workflow Manager release metadata")
    return value


RELEASE_METADATA = _release_metadata()
SCHEMA_VERSION = RELEASE_METADATA["schema"]
WRITER_VERSION = RELEASE_METADATA["version"]
DOMAIN_CLASSIFIER_VERSION = "2"
DIFFICULTY_CLASSIFIER_VERSION = "3"
EXECUTION_PROFILE_VERSION = RELEASE_METADATA["execution_profile"]
# The assessor is the hard-work safety gate: use the highest generally exposed
# effort (max); ultra is reserved for the explicit whole-session policy.
DEFAULT_PLAN_REASONING_EFFORT = "max"
HIGHEST_SESSION_REASONING_EFFORT = "ultra"
STABLE_SKILL_NAME = "workflow-manager"
STABLE_SKILL_SCHEMA = RELEASE_METADATA["stable_skill_schema"]
STABLE_SKILL_MARKER = ".workflow-manager-managed.json"
# 1.0.46 removed these generic workflow references from the bundled Skill, but
# the old updater only overlaid current files.  Delete a retired path only when
# its bytes still match a released Workflow Manager asset; preserve user edits,
# symlinks, and unrelated files.
RETIRED_STABLE_SKILL_FILE_DIGESTS = {
    "references/agent-lifecycle.md": frozenset(
        {"1660459ebc40e297bf4733e71de53739ce6eb0a903b70753aa88e14e8be74c04"}
    ),
    "references/live-coordination.md": frozenset(
        {"da0814dcf89aaef8d6ee65e3ecb72824f0583c05fa5de066dfc363a2f10f576b"}
    ),
}
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
MAX_LIFECYCLE_DIAGNOSTICS = 32
MAX_PROCESSED_RUNS = 128
MAX_DUPLICATE_NOTICES = 64
MAX_STATE_BYTES = 1024 * 1024
MAX_STATE_NODES = 16_384
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
DUPLICATE_TTL_SECONDS = 15 * 60
DEFAULT_RETENTION_DAYS = 30
DEFAULT_MAX_SESSION_FILES = 200
DEFAULT_OUTPUT_CHAR_LIMIT = 16_000
DEFAULT_OUTPUT_LINE_LIMIT = 300
DEFAULT_VISUAL_ITEM_LIMIT = 3
DISPATCH_RECEIPT_SCHEMA = 1
MAX_DISPATCH_RECEIPT_BYTES = 4096
MAX_DISPATCH_RECEIPT_EVENTS = 32
DISPATCH_RECEIPT_MAX_AGE_SECONDS = 15 * 60
DISPATCH_RUNNER_KINDS = frozenset({"posix_direct", "posix_cached", "windows_py", "windows_python"})

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
    "incomplete_execution",
}
TERMINAL_SUBAGENT_EVENTS = frozenset({"stop", "mailbox_terminal"})
# Executor and assessor sequence values are monotonic identities, never retry
# budgets.  Persistence byte/node budgets are the only generic growth bound.
RECOVERY_EXECUTOR_MODEL = "gpt-5.6-sol"
RECOVERY_EXECUTOR_REASONING_EFFORT = "max"
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
ASSESSOR_STATES = {"none", "spawn_required", "spawn_pending", "running", "hard_plan_ready", "recovery_required", "failed"}
ASSESSMENT_WAIT_SECONDS = 600
ASSESSMENT_IDLE_SECONDS = 1200


def _empty_assessment_liveness() -> dict[str, Any]:
    """Bound progress facts for the one live Hard assessor (never transcript text)."""
    return {"binding_id": None, "agent_id": None, "attempt": 0, "progress_digest": None,
            "last_progress_at": None, "last_observed_at": None, "unblock": "none",
            "unblock_at": None, "recovery_from": None}
CAUSAL_REVIEW_STATES = {"none", "triage_required", "triaging", "resolved"}
# Keep the four profile-10 spellings readable during a lazy migration.  New
# records always use the explicit v3 causal type names below.
CAUSAL_REVIEW_OUTCOMES = {
    "introduced", "unrelated", "direct_followup", "introduced_regression",
    "verified_side_effect", "fix_ineffective", "acceptance_gap_no_change",
    "execution_exposed_gap", "uncertain", "explanatory_conclusion",
    "unrelated_new_objective",
}
LEGACY_CAUSAL_OUTCOME_MAP = {
    "introduced": "introduced_regression",
    "unrelated": "unrelated_new_objective",
}
LIFECYCLE_DIAGNOSTIC_CODES = frozenset({
    "ordinary_spawn_no_active_hard", "pretool_missing", "start_missing",
    "contract_mismatch", "legacy_plan_rejected", "root_identity_mismatch",
    "epoch_switch_live_writer", "epoch_switch_lifecycle_conflict",
    "epoch_switch_irreversible_pending", "epoch_authority_unknown",
    "legacy_writer_isolated", "late_event_isolated_epoch",
    "late_event_epoch_ambiguous", "inventory_writer_absent",
    "inventory_writer_unknown", "inventory_writer_live",
    "spawn_envelope_conflict", "parent_filechange_reconciled",
})
LIFECYCLE_DIAGNOSTIC_LEVELS = frozenset({"info", "warning", "error"})
CHILD_LIVENESS_STATES = frozenset(
    {"none", "live", "absent", "terminal", "unknown", "isolated_incomplete"}
)
PARENT_WRITER_LEASE_STATES = frozenset({"none", "live", "sealed"})
ISOLATED_LIFECYCLE_STATES = frozenset(
    {"isolated_incomplete", "late_start", "late_terminal", "late_post"}
)
MAX_ISOLATED_LIFECYCLES = 8
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
    "manifest_invalid",
    "manifest_too_large",
    "manifest_too_many_nodes",
    "global_constraints_invalid",
    "slice_invalid",
    "slice_id_invalid",
    "field_invalid",
    "field_too_large",
    "manifest_fence_missing",
    "manifest_fence_ambiguous",
    "manifest_fence_not_tail",
    "manifest_json_invalid",
    "manifest_duplicate_key",
    "manifest_root_type_invalid",
    "manifest_root_shape_invalid",
    "manifest_version_invalid",
    "slice_count_invalid",
    "slice_shape_invalid",
    "field_type_invalid",
    "field_empty",
    "field_control_invalid",
}
PLAN_ARTIFACT_DIAGNOSTIC_CODES = PLAN_ARTIFACT_WARNING_CODES
LEGACY_PLAN_ARTIFACT_OWNER = "<!-- workflow-manager-plan-artifact:v1"
PLAN_ARTIFACT_OWNER = LEGACY_PLAN_ARTIFACT_OWNER
PLAN_JOURNAL_OWNER = "<!-- workflow-manager-plan-journal:v2"
PLAN_REVISION_OWNER = "<!-- workflow-manager-plan-revision:v2"
PLAN_JOURNAL_V3_RECORD_OWNER = "<!-- workflow-manager-plan-record:v3"
CAUSAL_RECORD_TYPES = frozenset({
    "executable_revision", "terminal_seal", "durable_conclusion",
})
CAUSAL_TYPES = frozenset({
    "direct_followup", "introduced_regression", "verified_side_effect",
    "fix_ineffective", "acceptance_gap_no_change", "execution_exposed_gap",
    "uncertain", "explanatory_conclusion", "unrelated_new_objective",
})
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
# Execution manifests are bounded by canonical bytes and nodes, not an
# arbitrary semantic item count.
# The execution manifest is an authority boundary.  These are byte budgets on
# canonical JSON, not character counts, so escaping and separators are charged.
MAX_EXECUTION_MANIFEST_BYTES = 196_608
MAX_EXECUTION_MANIFEST_NODES = 1_024
MAX_GLOBAL_CONSTRAINTS_BYTES = 32_768
MAX_SLICE_BYTES = 49_152
MAX_SLICE_TITLE_BYTES = 1_024
MAX_SLICE_SCOPE_BYTES = 16_384
MAX_SLICE_ACCEPTANCE_BYTES = 16_384
MAX_SLICE_SMALL_LIST_BYTES = 8_192
MAX_SLICE_TEXT_BYTES = 16_384
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


def _liveness_now() -> float:
    """One wall clock only for elapsed observations; rollback never manufactures idleness."""
    return time.time()


def _safe_assessment_liveness(value: Any) -> dict[str, Any]:
    base = _empty_assessment_liveness()
    if not isinstance(value, dict):
        return base
    base["binding_id"] = safe_fingerprint(value.get("binding_id")) or None
    base["agent_id"] = safe_label(value.get("agent_id"), 120) if value.get("agent_id") else None
    base["attempt"] = safe_sequence(value.get("attempt"))
    base["progress_digest"] = safe_fingerprint(value.get("progress_digest")) or None
    for key in ("last_progress_at", "last_observed_at", "unblock_at"):
        raw = value.get(key)
        base[key] = float(raw) if isinstance(raw, (int, float)) and raw >= 0 else None
    base["unblock"] = value.get("unblock") if value.get("unblock") in {"none", "pending", "delivered", "failed", "unknown"} else "none"
    base["recovery_from"] = safe_label(value.get("recovery_from"), 48) if value.get("recovery_from") else None
    return base


def assessment_liveness_tick(state: dict[str, Any], *, now: float | None = None,
                             progress_digest: str | None = None) -> str | None:
    """Advance the assessor liveness state without treating polls as progress.

    A digest is accepted only from the current binding, current live agent and
    current attempt.  The function intentionally has no total wall-clock
    deadline: a 600s tick is merely an observation, exactly 1200s is still
    live, and only strictly later time starts the one bounded unblock path.
    """
    if state.get("assessor_state") != "running":
        return None
    current = _safe_assessment_liveness(state.get("assessment_liveness"))
    binding, agent = state.get("assessor_binding_id"), state.get("assessor_agent_id")
    attempt = safe_int(state.get("assessor_attempt"))
    observed = _liveness_now() if now is None else now
    if not isinstance(observed, (int, float)):
        return None
    if (current["binding_id"], current["agent_id"], current["attempt"]) != (binding, agent, attempt):
        # Migration/restart re-anchors at the first current observation and
        # therefore cannot retroactively create an idle timeout.
        current = _empty_assessment_liveness()
        current.update({"binding_id": binding, "agent_id": agent, "attempt": attempt,
                        "last_progress_at": observed, "last_observed_at": observed})
        state["assessment_liveness"] = current
        return None
    prior_observed = current.get("last_observed_at")
    if prior_observed is not None and observed < prior_observed:
        observed = prior_observed
    current["last_observed_at"] = observed
    digest = safe_fingerprint(progress_digest)
    if digest and digest != current.get("progress_digest"):
        current["progress_digest"] = digest
        current["last_progress_at"] = observed
        current["unblock"] = "none"
        current["unblock_at"] = None
        state["assessment_liveness"] = current
        return "progress"
    last_progress = current.get("last_progress_at")
    if last_progress is None:
        current["last_progress_at"] = observed
        state["assessment_liveness"] = current
        return None
    idle = observed - last_progress
    if idle <= ASSESSMENT_IDLE_SECONDS:
        state["assessment_liveness"] = current
        return "observe" if idle >= ASSESSMENT_WAIT_SECONDS else None
    if current["unblock"] == "none":
        current["unblock"] = "pending"
        current["unblock_at"] = observed
        state["assessment_liveness"] = current
        return "unblock_required"
    # The caller must record delivery and prove the old child has stopped. A
    # later observation may open one fresh, non-overlapping sequence; there is
    # no semantic v1/v2 retry ceiling.
    if current["unblock"] == "delivered" and observed - float(current["unblock_at"] or observed) > ASSESSMENT_WAIT_SECONDS:
        live = any(item.get("agent_id") == agent for item in active_agent_records(state))
        if not live:
            state["assessor_state"] = "recovery_required"
            state["assessor_failure_kind"] = "assessment_stalled"
            current["recovery_from"] = "assessment_stalled"
            state["assessment_liveness"] = current
            return "recovery_required"
    state["assessment_liveness"] = current
    return "observe"


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


def _manifest_json_bytes(value: Any) -> int:
    return len(canonical_json(value).encode("utf-8"))


def _manifest_node_count(value: Any) -> int:
    if isinstance(value, dict):
        return 1 + sum(1 + _manifest_node_count(item) for item in value.values())
    if isinstance(value, list):
        return 1 + sum(_manifest_node_count(item) for item in value)
    return 1


def _bounded_manifest_string(value: Any, *, path: str, limit: int = MAX_SLICE_TEXT_BYTES) -> str:
    if not isinstance(value, str):
        raise PlanArtifactError("field_type_invalid", path=path)
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise PlanArtifactError("field_empty", path=path)
    if any(ord(character) < 32 and character not in {"\n", "\t"} for character in normalized):
        raise PlanArtifactError("field_control_invalid", path=path)
    actual = _manifest_json_bytes(normalized)
    if actual > limit:
        raise PlanArtifactError("field_too_large", path=path, actual=actual, limit=limit, unit="bytes")
    return normalized


def _bounded_manifest_list(value: Any, *, path: str, limit: int) -> list[str]:
    if not isinstance(value, list):
        raise PlanArtifactError("field_type_invalid", path=path)
    if not value:
        raise PlanArtifactError("field_empty", path=path)
    typed = [_bounded_manifest_string(item, path=f"{path}[{index}]", limit=limit) for index, item in enumerate(value)]
    actual = _manifest_json_bytes(typed)
    if actual > limit:
        raise PlanArtifactError("field_too_large", path=path, actual=actual, limit=limit, unit="bytes")
    return typed


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
EXECUTION_SLICES_JSON_FENCE_RE = re.compile(
    r"(?ms)^```json[ \t]*\n(.*?)^```[ \t]*(?:\n)?\Z"
)
EXECUTION_SLICES_JSON_FENCE_INTENT_RE = re.compile(r"(?m)^\s*```json\b")
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
    intents = list(EXECUTION_SLICES_FENCE_INTENT_RE.finditer(normalized))
    matches = list(EXECUTION_SLICES_FENCE_RE.finditer(normalized))
    if not intents:
        raise PlanArtifactError("manifest_fence_missing", path="manifest")
    if len(intents) != 1:
        raise PlanArtifactError("manifest_fence_ambiguous", path="manifest", actual=len(intents), limit=1, unit="items")
    if len(matches) != 1:
        raise PlanArtifactError("manifest_fence_not_tail", path="manifest")
    try:
        decoded = json.loads(matches[0].group(1), object_pairs_hook=_strict_json_object_pairs)
    except ValueError as error:
        code = "manifest_duplicate_key" if "duplicate JSON key" in str(error) else "manifest_json_invalid"
        raise PlanArtifactError(code, path="manifest") from error
    except (TypeError, json.JSONDecodeError) as error:
        raise PlanArtifactError("manifest_json_invalid", path="manifest") from error
    if not isinstance(decoded, dict):
        raise PlanArtifactError("manifest_root_type_invalid", path="manifest")
    if set(decoded) != {"version", "global_constraints", "slices"}:
        raise PlanArtifactError("manifest_root_shape_invalid", path="manifest")
    if decoded.get("version") != EXECUTION_SLICE_SCHEMA:
        raise PlanArtifactError("manifest_version_invalid", path="version")
    global_constraints = _bounded_manifest_list(decoded.get("global_constraints"), path="global_constraints", limit=MAX_GLOBAL_CONSTRAINTS_BYTES)
    raw_slices = decoded.get("slices")
    if not isinstance(raw_slices, list) or not raw_slices:
        raise PlanArtifactError("slice_count_invalid", path="slices", actual=len(raw_slices) if isinstance(raw_slices, list) else None, limit=1, unit="items")
    canonical_slices: list[dict[str, Any]] = []
    expected_keys = {"id", *EXECUTION_SLICE_FIELDS}
    for index, raw in enumerate(raw_slices, start=1):
        expected_id = f"s{index:02d}"
        if not isinstance(raw, dict) or set(raw) != expected_keys:
            raise PlanArtifactError("slice_shape_invalid", path=f"slices[{index}]")
        if raw.get("id") != expected_id:
            raise PlanArtifactError("slice_id_invalid", path=f"slices[{index}].id", actual=index + 1, unit="items")
        title = _bounded_manifest_string(raw.get("title"), path=f"slices[{index}].title", limit=MAX_SLICE_TITLE_BYTES)
        item: dict[str, Any] = {"id": expected_id, "title": title}
        field_limits = {"scope": MAX_SLICE_SCOPE_BYTES, "acceptance": MAX_SLICE_ACCEPTANCE_BYTES,
                        "rollback": MAX_SLICE_SMALL_LIST_BYTES, "stop_conditions": MAX_SLICE_SMALL_LIST_BYTES,
                        "expected_artifacts": MAX_SLICE_SMALL_LIST_BYTES}
        for field in EXECUTION_SLICE_FIELDS[1:]:
            values = _bounded_manifest_list(raw.get(field), path=f"slices[{index}].{field}", limit=field_limits[field])
            item[field] = values
        if _manifest_json_bytes(item) > MAX_SLICE_BYTES:
            raise PlanArtifactError("field_too_large", path=f"slices[{index}]", actual=_manifest_json_bytes(item), limit=MAX_SLICE_BYTES, unit="bytes")
        canonical_slices.append(item)
    canonical_manifest = {
        "global_constraints": global_constraints,
        "slices": canonical_slices,
        "version": EXECUTION_SLICE_SCHEMA,
    }
    manifest_bytes = _manifest_json_bytes(canonical_manifest)
    if manifest_bytes > MAX_EXECUTION_MANIFEST_BYTES:
        raise PlanArtifactError("manifest_too_large", path="manifest", actual=manifest_bytes, limit=MAX_EXECUTION_MANIFEST_BYTES, unit="bytes")
    manifest_nodes = _manifest_node_count(canonical_manifest)
    if manifest_nodes > MAX_EXECUTION_MANIFEST_NODES:
        raise PlanArtifactError("manifest_too_many_nodes", path="manifest", actual=manifest_nodes, limit=MAX_EXECUTION_MANIFEST_NODES, unit="nodes")
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
                "checklist_digest": stable_hash("workflow-manager-slice-checklist-v1\0" + canonical_json(item["acceptance"]), 32),
                "required_count": len(item["acceptance"]),
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


def execution_slice_manifest_for_plan(plan_body: str) -> dict[str, Any]:
    """Use an optional structured manifest, otherwise one native plan slice.

    Current Codex models already own planning and decomposition. The plugin
    therefore treats the dedicated JSON fence as an optional projection, not a
    second plan-quality gate. A plain native plan is bound as one logical slice
    while its complete body remains authoritative in the private journal.
    """
    try:
        return parse_execution_slice_manifest(plan_body)
    except PlanArtifactError:
        # Absence means native planning. Explicit malformed manifest intent is
        # contradictory data and must not be silently reinterpreted.
        if EXECUTION_SLICES_FENCE_INTENT_RE.search(str(plan_body or "")):
            raise
        plan_reference = stable_hash(str(plan_body or ""), 16)
        implicit = {
            "version": EXECUTION_SLICE_SCHEMA,
            "global_constraints": [
                "Follow the confirmed objective, explicit acceptance, risk category, and irreversible-action boundary."
            ],
            "slices": [
                {
                    "id": "s01",
                    "title": "Execute and verify the confirmed native plan",
                    "scope": [
                        f"Use private canonical plan revision {plan_reference} as the complete execution scope."
                    ],
                    "acceptance": [
                        "Satisfy the explicit acceptance in the authorization envelope and canonical plan."
                    ],
                    "rollback": [
                        "Use the reversible rollback appropriate to the confirmed plan and current evidence."
                    ],
                    "stop_conditions": [
                        "Stop for an authorization-boundary change, an irreversible action not already authorized, or failed strong acceptance."
                    ],
                    "expected_artifacts": [
                        "The artifacts and verification evidence required by the confirmed native plan."
                    ],
                }
            ],
        }
        projected = (
            "```workflow-manager-execution-slices\n"
            + canonical_json(implicit)
            + "\n```\n"
        )
        return parse_execution_slice_manifest(projected)


def normalize_execution_slice_manifest_fence(plan_body: str) -> str:
    """Canonicalize an unambiguous tail JSON manifest from a bound assessor.

    Current Codex models occasionally emit the exact execution-slice schema in a
    generic ``json`` fence. Requiring a fresh highest-tier assessor merely to
    relabel that already-validated payload wastes quota and does not add trust:
    the bound Start/Stop lifecycle and strict schema remain authoritative.
    """
    normalized = str(plan_body or "").replace("\r\n", "\n").replace("\r", "\n")
    if EXECUTION_SLICES_FENCE_INTENT_RE.search(normalized):
        return normalized
    matches = list(EXECUTION_SLICES_JSON_FENCE_RE.finditer(normalized))
    intents = EXECUTION_SLICES_JSON_FENCE_INTENT_RE.findall(normalized)
    if len(matches) != 1 or len(intents) != 1:
        return normalized
    match = matches[0]
    try:
        decoded = json.loads(match.group(1), object_pairs_hook=_strict_json_object_pairs)
    except (TypeError, ValueError, json.JSONDecodeError):
        return normalized
    if not isinstance(decoded, dict) or set(decoded) != {
        "version",
        "global_constraints",
        "slices",
    }:
        return normalized
    replacement = (
        "```workflow-manager-execution-slices\n" + match.group(1) + "```\n"
    )
    return normalized[: match.start()] + replacement


def _safe_execution_slices(value: Any) -> dict[str, Any]:
    empty = _empty_execution_slices()
    if not isinstance(value, dict) or safe_int(value.get("schema")) != EXECUTION_SLICE_SCHEMA:
        return empty
    plan_digest = _fingerprint32(value.get("plan_digest"))
    manifest_digest = _fingerprint32(value.get("manifest_digest"))
    global_digest = _fingerprint32(value.get("global_constraints_digest"))
    raw_items = value.get("items")
    count = safe_int(value.get("count"))
    current_index = safe_int(value.get("current_index"))
    completed_chain = _fingerprint32(value.get("completed_chain"))
    if (
        not plan_digest
        or not manifest_digest
        or not global_digest
        or not isinstance(raw_items, list)
        or count < 1 or count != len(raw_items)
        or not 1 <= current_index <= count + 1
        or not completed_chain
    ):
        return empty
    items: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_items, start=1):
        if not isinstance(raw, dict) or raw.get("id") != f"s{index:02d}":
            return empty
        slice_digest = _fingerprint32(raw.get("slice_digest"))
        checklist_digest = _fingerprint32(raw.get("checklist_digest"))
        required_count = safe_int(raw.get("required_count"))
        if not slice_digest or (checklist_digest is not None and required_count < 1):
            return empty
        status = raw.get("status") if raw.get("status") in {"pending", "passed"} else "pending"
        completion = _fingerprint32(raw.get("completion_digest"))
        review = _fingerprint32(raw.get("review_digest"))
        operation = _fingerprint32(raw.get("operation_digest"))
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
                "checklist_digest": checklist_digest,
                "required_count": required_count if checklist_digest else 0,
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
                "checklist_digest": item["checklist_digest"],
                "required_count": item["required_count"],
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
    contract = _fingerprint32(state.get("execution_contract_id"))
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
    manifest = _fingerprint32(safe.get("manifest_digest"))
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



def execution_contract_id(state: dict[str, Any]) -> str | None:
    """Bind one executor to the exact confirmed objective and plan without storing plan text."""
    epoch_id = current_task_epoch_id(state)
    objective = safe_fingerprint(state.get("objective", {}).get("fingerprint"))
    difficulty = safe_fingerprint(state.get("difficulty_decision_id"))
    plan = safe_fingerprint(state.get("plan_digest"))
    generation = max(safe_int(state.get("plan_generation")), 0)
    artifact = _safe_plan_artifact(state.get("plan_artifact"))
    canonical_path = str(artifact.get("relative_path") or "")
    revision_digest = safe_fingerprint(artifact.get("current_revision_digest"))
    # A v3 journal is append-only.  Terminal seals and durable conclusions are
    # deliberately allowed after an executable revision completes, so the
    # active contract must bind the immutable prefix selected at confirmation,
    # rather than the mutable journal tail.
    journal_digest = safe_fingerprint(
        artifact.get("journal_prefix_digest") or artifact.get("journal_digest")
    )
    journal_prefix_bytes = max(safe_int(artifact.get("journal_prefix_bytes")), 0)
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
    profile_version = safe_label(
        state.get("execution_profile_version") or EXECUTION_PROFILE_VERSION,
        16,
    )
    if profile_version == EXECUTION_PROFILE_VERSION:
        lineage = _safe_causal_lineage(state.get("causal_lineage"))
        if (
            not epoch_id
            or _safe_task_epoch(state.get("task_epoch")).get("status") != "active"
            or _safe_task_epoch(state.get("task_epoch")).get("objective_fingerprint")
            != objective
            or lineage.get("selected_revision_digest") != plan
            or lineage.get("selected_prefix_digest") != journal_digest
            or journal_prefix_bytes <= 0
        ):
            return None
    preference = safe_session_execution_preference(
        state.get("session_execution_preference")
    )
    material = (
        f"{profile_version}\0{preference}\0{epoch_id or 'legacy'}\0{objective}\0{difficulty}"
        f"\0{generation}\0{plan}\0{canonical_path}\0{revision_digest}\0{journal_digest}"
        f"\0{manifest_digest}\0{slices.get('global_constraints_digest')}\0{slices.get('count')}"
    )
    if profile_version == EXECUTION_PROFILE_VERSION:
        material += f"\0prefix_bytes={journal_prefix_bytes}"
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
    epoch_id = current_task_epoch_id(state)
    return stable_hash(
        f"assessor-v2\0{epoch_id or 'legacy'}\0{objective}\0{generation}", 32
    ) if objective and generation else None


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
    # A bare "version" occurs in ordinary product questions.  Only an
    # explicit request to alter a reference/fidelity contract is material.
    return bool(re.search(
        r"(?:参考|reference|视觉|fidelity).{0,48}(?:版本|version|方向|orientation|视口|viewport|场景|scene|时相|phase|稳定态|过渡态)"
        r"|(?:版本|version|方向|orientation|视口|viewport|场景|scene|时相|phase|稳定态|过渡态).{0,48}(?:参考|reference|视觉|fidelity)"
        r"|(?:切换|改为|改成|变更|switch|change).{0,48}(?:方向|orientation|视口|viewport|场景|scene|时相|phase|稳定态|过渡态)",
        prompt, re.I,
    ))


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
        "child_summary_digest": None,
        "parent_summary_digest": None,
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
            "slice_id": safe_slice_id(item.get("slice_id")),
            "slice_contract_id": fp32(item.get("slice_contract_id")),
            "attempt": safe_sequence(item.get("attempt")),
            "candidate_result_fingerprint": (
                fp32(item.get("candidate_result_fingerprint"))
            ),
            "candidate_agent_fingerprint": (
                fp32(item.get("candidate_agent_fingerprint"))
            ),
            "candidate_evidence_digest": (
                fp32(item.get("candidate_evidence_digest"))
            ),
            "child_summary_digest": fp32(item.get("child_summary_digest")),
            "parent_summary_digest": fp32(item.get("parent_summary_digest")),
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
        if not isinstance(item, dict) or item.get("event") not in TERMINAL_SUBAGENT_EVENTS:
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
    sequence = safe_sequence(state.get("executor_attempt"))
    state["execution_profile_version"] = EXECUTION_PROFILE_VERSION
    state["executor_state"] = "recovery_required" if failure else "none"
    state["execution_contract_id"] = None
    state["executor_agent_id"] = None
    state["executor_attempt"] = sequence
    state["executor_failure_kind"] = failure if failure in EXECUTOR_FAILURE_KINDS else None
    state["pending_recovery_facts"] = None
    state["pending_recovery_reservation"] = None
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
    """Return only the model proved by the original assessor lifecycle."""
    lifecycle, _ = original_assessor_lifecycle(state)
    return str(lifecycle.get("model") or "") or None


def highest_execution_effort(state: dict[str, Any]) -> str | None:
    lifecycle, _ = original_assessor_lifecycle(state)
    effort = str(lifecycle.get("reasoning_effort") or "").lower()
    return effort if effort in {"high", "xhigh", "max", "ultra"} else None


def original_assessor_lifecycle(
    state: dict[str, Any],
) -> tuple[dict[str, Any], str | None]:
    """Resolve the one request/PostToolUse/full-Start assessor lifecycle.

    Flat fields are cross-checks only.  They never manufacture a missing
    request, host acceptance, or Start observation.
    """
    binding = _fingerprint32(state.get("assessor_binding_id"))
    objective = safe_fingerprint(state.get("objective", {}).get("fingerprint"))
    attempt = safe_sequence(state.get("assessor_attempt"))
    if not binding or not objective or attempt <= 0:
        return {}, "start_mismatch"
    records = [item for item in as_list(state.get("subagents")) if isinstance(item, dict)]
    requests = [
        item
        for item in records
        if item.get("event") == "request"
        and item.get("role") == "high_assessor"
        and item.get("requested") is True
        and not item.get("agent_id")
        and item.get("contract_id") == binding
        and item.get("objective_fingerprint") == objective
        and safe_sequence(item.get("attempt")) == attempt
    ]
    if len(requests) != 1:
        return {}, "start_mismatch"
    request = requests[0]
    if (
        request.get("host_acceptance_conflict")
        or (
            request.get("host_acceptance_fingerprint")
            and request.get("host_acceptance_fingerprint")
            != request.get("request_fingerprint")
        )
    ):
        return {}, "start_mismatch"
    if (
        request.get("host_acceptance_source") != "PostToolUse"
        or request.get("host_accepted") is not True
        or request.get("host_acceptance_status") not in SUCCESS_STATUSES | {"running"}
        or not request.get("host_acceptance_fingerprint")
        or not _fingerprint32(request.get("host_acceptance_receipt_digest"))
    ):
        return {}, "model_unavailable"
    starts = [
        item
        for item in records
        if item.get("event") == "start"
        and item.get("role") == "high_assessor"
        and item.get("requested") is True
        and item.get("contract_id") == binding
        and item.get("objective_fingerprint") == objective
        and safe_sequence(item.get("attempt")) == attempt
    ]
    if len(starts) != 1:
        return {}, "start_mismatch"
    started = starts[0]
    model = safe_label(request.get("model"), 80)
    effort = safe_label(request.get("reasoning_effort"), 24).lower()
    fork = str(request.get("fork_turns") or "")
    flat_matches = bool(
        state.get("assessor_binding_id") == binding
        and state.get("objective", {}).get("fingerprint") == objective
        and safe_sequence(state.get("assessor_attempt")) == attempt
        and state.get("assessor_model") == model
        and str(state.get("assessor_reasoning_effort") or "").lower() == effort
        and str(state.get("assessor_fork_turns") or "") == fork
        and state.get("assessor_observed_effective") is True
        and state.get("assessor_start_observed") == "full"
        and state.get("assessor_observed_model") == model
        and str(state.get("assessor_observed_reasoning_effort") or "").lower()
        == effort
    )
    start_matches = bool(
        started.get("start_observed") == "full"
        and started.get("host_accepted") is True
        and started.get("request_fingerprint") == request.get("request_fingerprint")
        and started.get("model") == model
        and str(started.get("reasoning_effort") or "").lower() == effort
        and str(started.get("fork_turns") or "") == fork == "1"
        and started.get("agent_id")
        and started.get("observation_source")
        and (
            not state.get("assessor_agent_id")
            or state.get("assessor_agent_id") == started.get("agent_id")
        )
    )
    if (
        not flat_matches
        or not start_matches
        or not model
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{1,79}", model)
        or effort not in {"high", "xhigh", "max", "ultra"}
    ):
        return {}, "start_mismatch"
    return {
        "binding_id": binding,
        "objective_fingerprint": objective,
        "attempt": attempt,
        "model": model,
        "reasoning_effort": effort,
        "fork_turns": fork,
        "request_fingerprint": request.get("request_fingerprint"),
        "agent_id": started.get("agent_id"),
    }, None


def original_assessor_result_receipt(state: dict[str, Any]) -> str | None:
    """Resolve one terminal, nonempty result for the proven assessor lifecycle.

    This receipt contains only bounded metadata already persisted by the Hook.
    It can restore parent plan composition after a format-only assessor failure,
    but it cannot authorize execution without a verified canonical manifest.
    """
    lifecycle, error = original_assessor_lifecycle(state)
    if error:
        return None
    stops = [
        item
        for item in as_list(state.get("subagents"))
        if isinstance(item, dict)
        and item.get("event") in TERMINAL_SUBAGENT_EVENTS
        and item.get("role") == "high_assessor"
        and item.get("agent_id") == lifecycle.get("agent_id")
        and item.get("contract_id") == lifecycle.get("binding_id")
        and item.get("objective_fingerprint")
        == lifecycle.get("objective_fingerprint")
        and safe_sequence(item.get("attempt")) == lifecycle.get("attempt")
        and item.get("model") == lifecycle.get("model")
        and str(item.get("reasoning_effort") or "").lower()
        == lifecycle.get("reasoning_effort")
        and str(item.get("fork_turns") or "") == lifecycle.get("fork_turns")
    ]
    if len(stops) != 1:
        return None
    stopped = stops[0]
    result_meta = safe_metadata(stopped.get("result_meta"))
    result_fingerprint = safe_fingerprint(result_meta.get("fingerprint"))
    result_length = safe_int(result_meta.get("length"))
    if (
        stopped.get("stale")
        or stopped.get("status") not in {"completed", "unknown"}
        or not result_fingerprint
        or result_length <= 0
        or result_length > MAX_PLAN_REVISION_BYTES
    ):
        return None
    return stable_hash(
        "workflow-manager-native-assessor-stop-receipt-v1\0"
        + canonical_json(
            {
                "agent": stable_hash(str(lifecycle.get("agent_id")), 32),
                "attempt": lifecycle.get("attempt"),
                "binding": lifecycle.get("binding_id"),
                "objective": lifecycle.get("objective_fingerprint"),
                "result_fingerprint": result_fingerprint,
                "result_length": result_length,
            }
        ),
        32,
    )


def restore_native_executor_review_candidate(
    state: dict[str, Any], payload: dict[str, Any] | None = None
) -> bool:
    """Repair a format-only incomplete result from persisted host evidence."""
    if (
        state.get("plan_state") != "confirmed"
        or state.get("executor_state") != "recovery_required"
        or state.get("executor_failure_kind") != "incomplete_execution"
        or state.get("execution_contract_id") != execution_contract_id(state)
    ):
        return False
    if payload and payload.get("session_id"):
        # Resolve the exact current child rollout by its persisted Start
        # identity and validate its parent-session metadata. This lets a hot
        # update repair current Desktop's compact `{}` patch receipts without
        # persisting a transcript path or trusting child prose.
        reconcile_current_executor_rollout_on_resume(payload, state)
    item = current_execution_slice(state) or {}
    evidence = slice_operation_evidence(state)
    if not evidence.get("verification_evidence"):
        return False
    stops = [
        record
        for record in as_list(state.get("subagents"))
        if isinstance(record, dict)
        and record.get("event") in TERMINAL_SUBAGENT_EVENTS
        and record.get("role") == "confirmed_executor"
        and record.get("contract_id") == state.get("execution_contract_id")
        and record.get("slice_id") == item.get("id")
        and record.get("slice_contract_id") == slice_contract_id(state)
        and safe_sequence(record.get("attempt"))
        == safe_sequence(state.get("executor_attempt"))
        and record.get("execution_result_contract_match") is True
        and record.get("execution_result_outcome") == "succeeded"
        and _fingerprint32(record.get("execution_result_evidence_digest"))
    ]
    if len(stops) != 1:
        return False
    stopped = stops[0]
    result_meta = safe_metadata(stopped.get("result_meta"))
    result_fingerprint = safe_fingerprint(result_meta.get("fingerprint"))
    if not result_fingerprint or not stopped.get("agent_id"):
        return False
    child_summary = _acceptance_summary_digest("", state)
    if not child_summary:
        return False
    state["executor_state"] = "verification_required"
    state["executor_agent_id"] = None
    state["executor_failure_kind"] = None
    state["executor_review"] = _safe_executor_review(
        {
            "status": "review_required",
            "execution_contract_id": state.get("execution_contract_id"),
            "slice_id": item.get("id"),
            "slice_contract_id": slice_contract_id(state),
            "attempt": state.get("executor_attempt"),
            "candidate_result_fingerprint": stable_hash(
                "workflow-manager-persisted-result-v1\0" + result_fingerprint,
                32,
            ),
            "candidate_agent_fingerprint": stable_hash(
                str(stopped.get("agent_id")), 32
            ),
            "candidate_evidence_digest": stopped.get(
                "execution_result_evidence_digest"
            ),
            "child_summary_digest": child_summary,
            "review_evidence_digest": None,
            "digest_profile": EVIDENCE_DIGEST_PROFILE,
            "digest_source": EVIDENCE_DIGEST_SOURCE,
            "terminal_status": stopped.get("terminal_status"),
            "terminal_status_source": stopped.get("terminal_status_source"),
            "at": utc_now(),
        }
    )
    baseline = _safe_execution_baseline(
        state.get("last_execution_baseline")
    ) or build_execution_baseline(state)
    if baseline:
        baseline["acceptance_status"] = "incomplete"
        state["last_execution_baseline"] = baseline
    state["model_profile"] = "work_assessment"
    return True


def executor_is_typed_recovery(
    state: dict[str, Any], request: dict[str, Any] | None = None
) -> bool:
    if isinstance(request, dict):
        # A persisted request is the sequence authority.  Do not let the
        # temporary fail-closed state created by a Start-before-Post ordering
        # retroactively turn a normal lower-tier request into typed recovery.
        return request.get("recovery_from") in EXECUTOR_FAILURE_KINDS
    return bool(
        state.get("executor_failure_kind") in EXECUTOR_FAILURE_KINDS
        and state.get("executor_state")
        in {"recovery_required", "spawn_pending", "running"}
        and safe_sequence(state.get("executor_attempt")) > 0
    )


def expected_executor_profile(
    state: dict[str, Any], request: dict[str, Any] | None = None
) -> dict[str, Any]:
    preference = safe_session_execution_preference(
        state.get("session_execution_preference")
    )
    if preference == "highest_throughout":
        lifecycle, error = original_assessor_lifecycle(state)
        return {
            "profile": "work_executor_highest_available",
            "model": lifecycle.get("model"),
            "reasoning_effort": HIGHEST_SESSION_REASONING_EFFORT,
            "error": error,
        }
    if executor_is_typed_recovery(state, request):
        _, error = original_assessor_lifecycle(state)
        return {
            "profile": "work_executor_highest_available",
            "model": RECOVERY_EXECUTOR_MODEL,
            "reasoning_effort": RECOVERY_EXECUTOR_REASONING_EFFORT,
            "error": error,
        }
    return {
        "profile": "work_executor_low_latest",
        "model": None,
        "reasoning_effort": "medium",
        "error": None,
    }


def _restore_mapping_values(target: dict[str, Any], snapshot: dict[str, Any]) -> None:
    for key, value in snapshot.items():
        target[key] = value


def verified_current_execution_handoff(
    state: dict[str, Any], payload: dict[str, Any]
) -> tuple[str | None, str | None, str | None]:
    """Read and bind the current slice from the trusted private journal."""
    try:
        candidate_body = _read_verified_current_plan_revision(state, payload)
        candidate_digest = stable_hash(candidate_body, 32)
        artifact = _safe_plan_artifact(state.get("plan_artifact"))
        if candidate_digest != artifact.get("current_revision_digest"):
            raise PlanArtifactError("content_drift")
        parsed_slices = execution_slice_manifest_for_plan(candidate_body)
        persisted_slices = _safe_execution_slices(state.get("execution_slices"))
        current_slice = current_execution_slice(state)
        current_index = safe_int(persisted_slices.get("current_index"))
        if (
            parsed_slices.get("manifest_digest")
            != persisted_slices.get("manifest_digest")
            or parsed_slices.get("global_constraints_digest")
            != persisted_slices.get("global_constraints_digest")
            or not current_slice
            or current_index <= 0
            or current_index > len(parsed_slices.get("items", []))
            or parsed_slices["items"][current_index - 1]["slice_digest"]
            != current_slice.get("slice_digest")
        ):
            raise PlanArtifactError("content_drift")
        contract_id = _fingerprint32(state.get("execution_contract_id"))
        objective = safe_fingerprint(
            state.get("objective", {}).get("fingerprint")
        )
        plan_digest = _fingerprint32(state.get("plan_digest"))
        current_slice_id = parsed_slices["items"][current_index - 1]["id"]
        if not contract_id or not objective or not plan_digest:
            raise PlanArtifactError("content_drift")
        handoff = {
            "execution_contract_id": contract_id,
            "objective_fingerprint": objective,
            "plan_digest": plan_digest,
            "plan_generation": safe_int(state.get("plan_generation")),
            "slice_contract_id": slice_contract_id(state),
        }
        if EXECUTION_SLICES_FENCE_INTENT_RE.search(candidate_body):
            handoff["global_constraints"] = parsed_slices["global_constraints"]
            handoff["slice"] = {
                key: parsed_slices["items"][current_index - 1][key]
                for key in ("id", *EXECUTION_SLICE_FIELDS)
            }
        else:
            handoff["slice_id"] = current_slice_id
            handoff["canonical_plan"] = candidate_body
        body = canonical_json(handoff) + "\n"
        return body, candidate_digest, None
    except PlanArtifactError as error:
        return None, None, error.code
    except OSError:
        return None, None, "write_error"


def reconcile_post_accepted_bound_start(
    state: dict[str, Any], request: dict[str, Any], payload: dict[str, Any]
) -> bool:
    """Bind an exact full Start when its Post receipt becomes authoritative.

    Hosts normally deliver PostToolUse before SubagentStart, but the two event
    streams are not ordered by the Hook contract.  A Start observed while host
    acceptance is missing remains fail-closed.  A read-only assessor can be
    rebound after the matching receipt.  An executor can be rebound only when
    its Start delivered a digest-bound canonical slice under mutation lock and
    the same private journal is reverified inside this Post transition.
    """
    if (
        not isinstance(request, dict)
        or request.get("host_acceptance_source") != "PostToolUse"
        or request.get("host_accepted") is not True
        or request.get("host_acceptance_status") not in SUCCESS_STATUSES | {"running"}
        or request.get("host_acceptance_fingerprint")
        != request.get("request_fingerprint")
    ):
        return False
    role = request.get("role")
    if role not in {"high_assessor", "confirmed_executor"}:
        return False
    records = [item for item in as_list(state.get("subagents")) if isinstance(item, dict)]
    matching_requests = [
        item
        for item in records
        if item.get("event") == "request"
        and item.get("role") == role
        and item.get("request_fingerprint") == request.get("request_fingerprint")
    ]
    matching_starts = [
        item
        for item in records
        if item.get("event") == "start"
        and item.get("role") == role
        and item.get("request_fingerprint") == request.get("request_fingerprint")
    ]
    if len(matching_requests) != 1 or matching_requests[0] is not request or len(matching_starts) != 1:
        return False
    started = matching_starts[0]
    agent_id = safe_label(started.get("agent_id"), 120)
    expected_lifecycle = lifecycle_binding_fingerprint(
        epoch_id=current_task_epoch_id(state), role=role, agent_id=agent_id,
        request_fingerprint=request.get("request_fingerprint"),
        contract_id=request.get("contract_id"), attempt=request.get("attempt"),
    )
    if (
        not agent_id
        or request.get("epoch_id") != current_task_epoch_id(state)
        or started.get("epoch_id") != current_task_epoch_id(state)
        or not expected_lifecycle
        or started.get("lifecycle_fingerprint") != expected_lifecycle
        or started.get("start_observed") != "full"
        or not started.get("observation_source")
        or any(
            item.get("event") in TERMINAL_SUBAGENT_EVENTS
            and item.get("agent_id") == agent_id
            for item in records
        )
    ):
        return False

    if role == "high_assessor":
        if (
            state.get("assessor_state") not in {"spawn_pending", "recovery_required"}
            or state.get("assessor_failure_kind") not in {None, "model_unavailable"}
            or state.get("assessor_agent_id") is not None
            or state.get("assessor_observed_effective") is not False
            or state.get("assessor_observed_model") != started.get("model")
            or str(state.get("assessor_observed_reasoning_effort") or "").lower()
            != str(started.get("reasoning_effort") or "").lower()
            or state.get("assessor_start_observed") != started.get("start_observed")
            or state.get("assessor_observation_source")
            != started.get("observation_source")
        ):
            return False
        flat_keys = (
            "assessor_agent_id",
            "assessor_observed_model",
            "assessor_observed_reasoning_effort",
            "assessor_start_observed",
            "assessor_observation_source",
            "assessor_observed_effective",
            "assessor_state",
            "assessor_failure_kind",
            "assessment_liveness",
        )
        flat_before = {key: state.get(key) for key in flat_keys}
        start_before = started.get("host_accepted")
        started["host_accepted"] = True
        state["assessor_agent_id"] = agent_id
        state["assessor_observed_effective"] = True
        lifecycle, error = original_assessor_lifecycle(state)
        if error or lifecycle.get("agent_id") != agent_id:
            started["host_accepted"] = start_before
            _restore_mapping_values(state, flat_before)
            return False
        now = _liveness_now()
        state["assessor_state"] = "running"
        state["assessor_failure_kind"] = None
        state["assessment_liveness"] = {
            "binding_id": state.get("assessor_binding_id"),
            "agent_id": agent_id,
            "attempt": safe_sequence(state.get("assessor_attempt")),
            "progress_digest": None,
            "last_progress_at": now,
            "last_observed_at": now,
            "unblock": "none",
            "unblock_at": None,
            "recovery_from": None,
        }
        _set_writer_liveness(
            state, status="live",
            binding=_writer_liveness_binding(
                state, role, request=request, agent_id=agent_id
            ),
            source="host_lifecycle",
            observation={"event": "late_post_rebind", "role": role},
        )
        return True
    if (
        state.get("executor_state") != "recovery_required"
        or state.get("executor_failure_kind") != "model_unavailable"
        or state.get("executor_agent_id") is not None
        or state.get("executor_observed_effective") is not False
        or state.get("plan_state") != "confirmed"
        or state.get("confirmed_plan_digest") != state.get("plan_digest")
    ):
        return False
    handoff_body, handoff_digest, handoff_error = verified_current_execution_handoff(
        state, payload
    )
    if (
        handoff_error
        or handoff_body is None
        or handoff_digest != state.get("plan_digest")
        or started.get("plan_handoff_digest") != handoff_digest
        or started.get("plan_handoff_delivered") is not True
        or started.get("plan_handoff_delivery_digest")
        != stable_hash(handoff_body, 32)
    ):
        state["executor_failure_kind"] = "stale_contract"
        return False
    current_slice = current_execution_slice(state) or {}
    profile = expected_executor_profile(state, request)
    expected_effort = str(profile.get("reasoning_effort") or "").lower()
    expected_model = safe_label(profile.get("model"), 80) if profile.get("model") else None
    highest_profile = profile.get("profile") == "work_executor_highest_available"
    exact = bool(
        not profile.get("error")
        and request.get("contract_id") == state.get("execution_contract_id")
        and request.get("contract_id") == execution_contract_id(state)
        and request.get("objective_fingerprint")
        == state.get("objective", {}).get("fingerprint")
        and request.get("slice_id") == current_slice.get("id")
        and request.get("slice_contract_id") == slice_contract_id(state)
        and safe_sequence(request.get("attempt"))
        == safe_sequence(state.get("executor_attempt"))
        and request.get("model") == state.get("executor_model")
        and str(request.get("reasoning_effort") or "").lower() == expected_effort
        and str(state.get("executor_reasoning_effort") or "").lower()
        == expected_effort
        and str(request.get("fork_turns") or "")
        == str(state.get("executor_fork_turns") or "")
        == "1"
        and (not highest_profile or request.get("model") == expected_model)
        and started.get("contract_id") == request.get("contract_id")
        and started.get("objective_fingerprint") == request.get("objective_fingerprint")
        and started.get("slice_id") == request.get("slice_id")
        and started.get("slice_contract_id") == request.get("slice_contract_id")
        and safe_sequence(started.get("attempt")) == safe_sequence(request.get("attempt"))
        and started.get("task_name") == request.get("task_name")
        and started.get("model") == request.get("model")
        and str(started.get("reasoning_effort") or "").lower() == expected_effort
        and str(started.get("fork_turns") or "") == "1"
        and state.get("executor_observed_model") == started.get("model")
        and str(state.get("executor_observed_reasoning_effort") or "").lower()
        == expected_effort
        and state.get("executor_start_observed") == "full"
        and state.get("executor_observation_source") == started.get("observation_source")
    )
    other_live_writer = any(
        group.get("state") == "live"
        and group.get("agent_id") != agent_id
        and isinstance(group.get("request"), dict)
        and group["request"].get("role") == "confirmed_executor"
        for group in subagent_lifecycle_groups(state)
    )
    if not exact or other_live_writer:
        state["executor_failure_kind"] = "start_mismatch"
        return False
    started["host_accepted"] = True
    state["executor_observed_effective"] = True
    state["executor_state"] = "running"
    state["executor_agent_id"] = agent_id
    state["executor_failure_kind"] = None
    _set_writer_liveness(
        state, status="live",
        binding=_writer_liveness_binding(
            state, role, request=request, agent_id=agent_id
        ),
        source="host_lifecycle",
        observation={"event": "late_post_rebind", "role": role},
    )
    return True


def confirmed_executor_model_profile(state: dict[str, Any]) -> str:
    return str(expected_executor_profile(state).get("profile"))


def incomplete_execution_highest_recovery(state: dict[str, Any]) -> bool:
    """Compatibility alias retained for callers; evidence is lifecycle-derived."""
    return executor_is_typed_recovery(state)


def _safe_authorization_scope(value: Any) -> dict[str, str | None]:
    value = value if isinstance(value, dict) else {}
    return {
        key: _fingerprint32(value.get(key))
        for key in (
            "acceptance_digest",
            "risk_category_digest",
            "irreversible_action_digest",
        )
    }


def _explicit_clause_digest(prompt: str, markers: tuple[str, ...], domain: str) -> str | None:
    normalized = str(prompt or "").replace("\r\n", "\n").replace("\r", "\n")
    clauses = [
        re.sub(r"\s+", " ", clause).strip().lower()
        for clause in re.split(r"[\n。；;]+", normalized[:65_536])
        if clause.strip() and any(marker in clause.lower() for marker in markers)
    ]
    return (
        stable_hash(f"workflow-manager-{domain}-v1\0" + canonical_json(clauses), 32)
        if clauses
        else None
    )


def authorization_scope_from_prompt(
    prompt: str, previous: Any = None
) -> dict[str, str | None]:
    """Hash normalized explicit acceptance/risk/action clauses; retain no prose."""
    prior = _safe_authorization_scope(previous)
    acceptance = _explicit_clause_digest(
        prompt,
        ("acceptance", "acceptance criteria", "verify", "verification", "test", "验收", "验证", "测试"),
        "acceptance-scope",
    )
    risk = _explicit_clause_digest(
        prompt,
        ("risk", "production", "security", "privacy", "credential", "destructive", "风险", "生产", "安全", "隐私", "凭据", "破坏"),
        "risk-category",
    )
    irreversible = _explicit_clause_digest(
        prompt,
        ("irreversible", "publish", "release", "push", "deploy", "delete", "erase", "purchase", "不可逆", "发布", "推送", "部署", "删除", "销毁", "购买"),
        "irreversible-action",
    )
    return {
        "acceptance_digest": acceptance or prior.get("acceptance_digest"),
        "risk_category_digest": risk or prior.get("risk_category_digest"),
        "irreversible_action_digest": irreversible
        or prior.get("irreversible_action_digest"),
    }


def authorization_scope_change_requested(prompt: str) -> bool:
    normalized = re.sub(r"\s+", " ", str(prompt or "").strip().lower())
    change = re.search(
        r"(?:新增|增加|扩大|改为|改成|变更|new|add|expand|change)",
        normalized,
        re.I,
    )
    boundary = re.search(
        r"(?:验收|acceptance|风险|risk|生产|production|发布|release|publish|"
        r"部署|deploy|删除|delete|不可逆|irreversible)",
        normalized,
        re.I,
    )
    return bool(change and boundary)


def authorization_envelope_digest(state: dict[str, Any]) -> str | None:
    """Digest-only confirmation scope; never persist plan/prompt prose."""
    objective = safe_fingerprint(state.get("objective", {}).get("fingerprint"))
    if not objective:
        return None
    scope = _safe_authorization_scope(state.get("authorization_scope"))
    return stable_hash(
        "workflow-manager-authorization-envelope-v2\0"
        + canonical_json({"v": 2, "objective": objective, **scope}),
        32,
    )


def pending_confirmation_receipt_for_state(state: dict[str, Any]) -> str | None:
    session = safe_fingerprint(state.get("session_fingerprint"))
    binding = _fingerprint32(state.get("assessor_binding_id"))
    objective = safe_fingerprint(state.get("objective", {}).get("fingerprint"))
    envelope = authorization_envelope_digest(state)
    if not session or not binding or not objective or not envelope:
        return None
    return stable_hash(
        "workflow-manager-host-bound-pending-confirmation-v1\0"
        + canonical_json(
            {
                "assessor_binding_id": binding,
                "authorization_envelope": envelope,
                "objective_fingerprint": objective,
                "session_fingerprint": session,
            }
        ),
        32,
    )


def _safe_authorization_envelope(value: Any) -> dict[str, Any]:
    value = value if isinstance(value, dict) else {}
    digest = _fingerprint32(value.get("digest"))
    receipt = _fingerprint32(value.get("strict_confirm_receipt"))
    return {
        "digest": digest,
        "strict_confirm_receipt": receipt,
        "confirmation_count": safe_sequence(value.get("confirmation_count")),
    } if digest and receipt else {"digest": None, "strict_confirm_receipt": None, "confirmation_count": 0}


def pending_causal_successor(
    state: dict[str, Any], *, causal_type: str, issue_fingerprint: Any,
    evidence_digest: Any = None,
) -> dict[str, Any]:
    """Capture the completed parent's exact digest-only successor boundary."""
    if causal_type not in EXECUTABLE_CAUSAL_TYPES:
        return {}
    baseline = _safe_execution_baseline(state.get("last_execution_baseline"))
    artifact = _safe_plan_artifact(state.get("plan_artifact"))
    envelope = _safe_authorization_envelope(state.get("authorization_envelope"))
    lineage = _safe_causal_lineage(state.get("causal_lineage"))
    root = (
        lineage.get("root_objective_fingerprint")
        or safe_fingerprint(state.get("objective", {}).get("fingerprint"))
    )
    issue = safe_fingerprint(issue_fingerprint)
    contract = _fingerprint32(state.get("execution_contract_id"))
    parent_revision = _fingerprint32(state.get("plan_digest"))
    parent_prefix = _fingerprint32(artifact.get("journal_prefix_digest"))
    if not (
        state.get("plan_state") == "confirmed"
        and state.get("executor_state") == "succeeded"
        and baseline.get("acceptance_status") in {"passed", "failed"}
        and baseline.get("execution_contract_id") == contract
        and baseline.get("plan_digest") == parent_revision
        and baseline.get("objective_fingerprint") == root
        and lineage.get("terminal_baseline_id") == baseline.get("baseline_id")
        and lineage.get("terminal_seal_digest")
        and envelope.get("digest") == authorization_envelope_digest(state)
        and envelope.get("strict_confirm_receipt")
        and root and issue and contract and parent_revision and parent_prefix
    ):
        return {}
    if causal_type == "introduced_regression" and not baseline.get("change_set_digest"):
        return {}
    scope = _safe_authorization_scope(state.get("authorization_scope"))
    return _safe_pending_causal_revision({
        "causal_type": causal_type,
        "creation_state": "assessment_required",
        "parent_revision_digest": parent_revision,
        "parent_contract_id": contract,
        "parent_prefix_digest": parent_prefix,
        "terminal_baseline_id": baseline.get("baseline_id"),
        "root_objective_fingerprint": root,
        "issue_fingerprint": issue,
        "evidence_digest": evidence_digest,
        "change_set_digest": baseline.get("change_set_digest"),
        "authorization_envelope_digest": envelope.get("digest"),
        "strict_confirm_receipt": envelope.get("strict_confirm_receipt"),
        **scope,
    })


def causal_successor_inheritance_valid(state: dict[str, Any]) -> bool:
    pending = _safe_pending_causal_revision(state.get("pending_causal_revision"))
    if not pending:
        return True
    artifact = _safe_plan_artifact(state.get("plan_artifact"))
    baseline = _safe_execution_baseline(state.get("last_execution_baseline"))
    lineage = _safe_causal_lineage(state.get("causal_lineage"))
    envelope = _safe_authorization_envelope(state.get("authorization_envelope"))
    scope = _safe_authorization_scope(state.get("authorization_scope"))
    return bool(
        pending.get("creation_state") == "plan_composition"
        and pending.get("parent_revision_digest") != state.get("plan_digest")
        and pending.get("parent_contract_id")
        == baseline.get("execution_contract_id")
        and pending.get("parent_revision_digest") == baseline.get("plan_digest")
        and pending.get("terminal_baseline_id") == baseline.get("baseline_id")
        and pending.get("root_objective_fingerprint")
        == state.get("objective", {}).get("fingerprint")
        == lineage.get("root_objective_fingerprint")
        and lineage.get("selected_revision_digest") == state.get("plan_digest")
        and lineage.get("current_issue_fingerprint") == pending.get("issue_fingerprint")
        and lineage.get("current_causal_type") == pending.get("causal_type")
        and artifact.get("current_revision_digest") == state.get("plan_digest")
        and artifact.get("journal_prefix_digest")
        == lineage.get("selected_prefix_digest")
        and pending.get("authorization_envelope_digest")
        == envelope.get("digest")
        == authorization_envelope_digest(state)
        and pending.get("strict_confirm_receipt")
        == envelope.get("strict_confirm_receipt")
        and all(
            pending.get(key) == scope.get(key)
            for key in (
                "acceptance_digest", "risk_category_digest",
                "irreversible_action_digest",
            )
        )
    )


def recovery_fingerprint(
    state: dict[str, Any], failure_kind: str, evidence_digest: str | None = None
) -> str | None:
    """A privacy-safe identity for a failed executor boundary."""
    contract = _fingerprint32(state.get("execution_contract_id"))
    item = current_execution_slice(state) or {}
    evidence = _fingerprint32(evidence_digest)
    if not contract or failure_kind not in EXECUTOR_FAILURE_KINDS:
        return None
    return stable_hash(
        "workflow-manager-recovery-failure-v3\0"
        + canonical_json(
            {
                "v": 3,
                "contract": contract,
                "evidence": evidence,
                "slice": item.get("id"),
                "failure": failure_kind,
            }
        ),
        32,
    )


def _safe_recovery_record(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    failure = value.get("failure_kind")
    fingerprint = _fingerprint32(value.get("failure_fingerprint"))
    correction = _fingerprint32(value.get("correction_digest"))
    sequence = safe_sequence(value.get("sequence"))
    if failure not in EXECUTOR_FAILURE_KINDS or not fingerprint or not correction or sequence <= 0:
        return None
    return {
        "sequence": sequence,
        "failure_kind": failure,
        "failure_fingerprint": fingerprint,
        "evidence_digest": _fingerprint32(value.get("evidence_digest")),
        "progress_digest": _fingerprint32(value.get("progress_digest")),
        "root_cause_digest": _fingerprint32(value.get("root_cause_digest")),
        "correction_digest": correction,
        "review_digest": _fingerprint32(value.get("review_digest")),
    }


def _normalized_recovery_digest(value: Any, domain: str) -> str | None:
    """Normalize a visible 32/64-hex fact without retaining host output."""
    token = safe_fingerprint(value)
    if not token or len(token) not in {32, 64}:
        return None
    return token if len(token) == 32 else stable_hash(f"{domain}\0{token}", 32)


def _recovery_prompt_claim(
    prompt: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Parse digest-only recovery input without deciding that recovery exists."""
    failures = re.findall(
        r"(?:recovery_from|recovery-from|恢复自)\s*[:=：]\s*([a-z_]+)\b",
        prompt,
        re.I,
    )
    failure_values = {value.lower() for value in failures}
    failure = next(iter(failure_values)) if len(failure_values) == 1 else None
    if failure not in EXECUTOR_FAILURE_KINDS:
        return None, "recovery reservation requires one typed failure"

    def one_token(marker: str) -> str | None:
        values = [value.lower() for value in re.findall(marker, prompt, re.I)]
        return values[0] if values and len(set(values)) == 1 else None

    failure_fingerprint = _normalized_recovery_digest(
        one_token(
            r"(?:failure_fingerprint|failure-fingerprint|失败指纹)\s*[:=：]\s*([0-9a-f]{32,64})\b"
        ),
        "workflow-manager-external-failure-v1",
    )
    evidence = _normalized_recovery_digest(
        one_token(
            r"(?:evidence_digest|evidence-digest|verification_evidence_digest|verification-evidence-digest)\s*[:=：]\s*([0-9a-f]{32,64})\b"
        ),
        "workflow-manager-external-evidence-v1",
    )
    progress_token = one_token(
        r"(?:progress_digest|progress-digest|进展摘要)\s*[:=：]\s*([0-9a-f]{32,64})\b"
    )
    progress = (
        _normalized_recovery_digest(
            progress_token, "workflow-manager-external-progress-v1"
        )
        if progress_token
        else None
    )
    if not failure_fingerprint or not evidence:
        return None, (
            "recovery reservation requires one failure fingerprint and evidence digest"
        )
    root_matches = re.findall(
        r"(?:root_cause|root-cause|根因)\s*[:=：]\s*([^\n\r]{2,512})",
        prompt,
        re.I,
    )
    correction_matches = re.findall(
        r"(?:material_correction|material-correction|实质修正)\s*[:=：]\s*([^\n\r]{8,2048})",
        prompt,
        re.I,
    )
    if len(root_matches) != 1 or len(correction_matches) != 1:
        return None, (
            "recovery reservation requires one root cause and material correction"
        )
    return {
        "failure_kind": failure,
        "failure_fingerprint": failure_fingerprint,
        "evidence_digest": evidence,
        "progress_digest": progress,
        "root_cause_digest": stable_hash(root_matches[0].strip(), 32),
        "correction_digest": stable_hash(correction_matches[0].strip(), 32),
    }, None


def _recovery_lifecycle_projection(
    state: dict[str, Any], event: str, contract: str, attempt: int
) -> dict[str, Any] | None:
    """Project one bounded host lifecycle record without child prose."""
    record = next(
        (
            item
            for item in reversed(as_list(state.get("subagents")))
            if isinstance(item, dict)
            and (
                item.get("event") == event
                or event == "stop"
                and item.get("event") in TERMINAL_SUBAGENT_EVENTS
            )
            and item.get("role") == "confirmed_executor"
            and item.get("contract_id") == contract
            and safe_sequence(item.get("attempt")) == attempt
        ),
        None,
    )
    if not record:
        return None
    record = _safe_subagent(record) or record
    projection = {
        key: record.get(key)
        for key in (
            "attempt",
            "contract_id",
            "evidence_digest_profile",
            "evidence_digest_source",
            "execution_result_contract_match",
            "execution_result_evidence_digest",
            "execution_result_outcome",
            "fork_turns",
            "host_acceptance_fingerprint",
            "host_acceptance_receipt_digest",
            "host_acceptance_source",
            "host_acceptance_status",
            "host_accepted",
            "model",
            "objective_fingerprint",
            "observation_source",
            "plan_handoff_delivery_digest",
            "plan_handoff_digest",
            "plan_handoff_delivered",
            "reasoning_effort",
            "recovery_from",
            "request_fingerprint",
            "slice_contract_id",
            "slice_id",
            "start_observed",
            "status",
            "terminal_status",
            "terminal_status_source",
        )
        if record.get(key) is not None
    }
    if record.get("agent_id"):
        projection["agent_fingerprint"] = stable_hash(
            safe_label(record.get("agent_id"), 120), 32
        )
    result_meta = safe_metadata(record.get("result_meta"))
    if result_meta:
        projection["result_meta"] = result_meta
    return projection


def executor_recovery_evidence_digest(
    state: dict[str, Any], failure_kind: str
) -> str | None:
    """Derive one digest from host-owned lifecycle, operation, and review facts."""
    contract = _fingerprint32(state.get("execution_contract_id"))
    objective = safe_fingerprint(state.get("objective", {}).get("fingerprint"))
    item = current_execution_slice(state) or {}
    slice_id = safe_slice_id(item.get("id"))
    slice_contract = _fingerprint32(slice_contract_id(state))
    attempt = safe_sequence(state.get("executor_attempt"))
    if not all((contract, objective, slice_id, slice_contract, attempt > 0)):
        return None
    operations: list[dict[str, Any]] = []
    for operation in as_list(state.get("operations")):
        safe_operation = _safe_operation(operation)
        if not safe_operation or not (
            safe_operation.get("execution_contract_id") == contract
            and safe_operation.get("slice_id") == slice_id
            and safe_operation.get("slice_contract_id") == slice_contract
        ):
            continue
        projected = {
            key: safe_operation.get(key)
            for key in (
                "category",
                "host_event_turn_id",
                "host_input_digest",
                "status",
                "tool",
            )
            if safe_operation.get(key) is not None
        }
        if safe_operation.get("executor_agent_id"):
            projected["executor_agent_fingerprint"] = stable_hash(
                safe_label(safe_operation.get("executor_agent_id"), 120), 32
            )
        operations.append(projected)
    review = _safe_executor_review(state.get("executor_review"))
    review_projection = {
        key: review.get(key)
        for key in (
            "attempt",
            "candidate_agent_fingerprint",
            "candidate_evidence_digest",
            "candidate_result_fingerprint",
            "child_summary_digest",
            "execution_contract_id",
            "parent_summary_digest",
            "review_evidence_digest",
            "slice_contract_id",
            "slice_id",
            "status",
        )
        if review.get(key) is not None
    }
    stall = _safe_stall(state.get("stall"))
    stall_projection = {
        key: stall.get(key)
        for key in (
            "correction_digest",
            "evidence_digest",
            "failure_kind",
            "remediation_digest",
            "resume_profile",
            "stall_id",
            "state",
        )
        if stall.get(key) is not None
    }
    facts = {
        "attempt": attempt,
        "contract": contract,
        "failure": failure_kind,
        "lifecycles": {
            event: _recovery_lifecycle_projection(
                state, event, contract, attempt
            )
            for event in ("request", "start", "stop")
        },
        "objective": objective,
        "operations": operations[-16:],
        "plan_digest": _fingerprint32(state.get("plan_digest")),
        "plan_generation": safe_int(state.get("plan_generation")),
        "review": review_projection,
        "slice": slice_id,
        "slice_contract": slice_contract,
        "stall": stall_projection,
    }
    return stable_hash(
        "workflow-manager-executor-recovery-evidence-v1\0"
        + canonical_json(facts),
        32,
    )


def _safe_pending_recovery_facts(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    failure = value.get("failure_kind")
    fingerprint = _fingerprint32(value.get("failure_fingerprint"))
    evidence = _fingerprint32(value.get("evidence_digest"))
    contract = _fingerprint32(value.get("execution_contract_id"))
    objective = safe_fingerprint(value.get("objective_fingerprint"))
    slice_id = safe_slice_id(value.get("slice_id"))
    slice_contract = _fingerprint32(value.get("slice_contract_id"))
    sequence = safe_sequence(value.get("sequence"))
    if not all(
        (
            failure in EXECUTOR_FAILURE_KINDS,
            fingerprint,
            evidence,
            contract,
            objective,
            slice_id,
            slice_contract,
            sequence > 0,
        )
    ):
        return None
    return {
        "sequence": sequence,
        "failure_kind": failure,
        "failure_fingerprint": fingerprint,
        "evidence_digest": evidence,
        "execution_contract_id": contract,
        "objective_fingerprint": objective,
        "slice_id": slice_id,
        "slice_contract_id": slice_contract,
        "source": (
            value.get("source")
            if value.get("source") in {"host_lifecycle", "mailbox_final"}
            else "host_lifecycle"
        ),
    }


def _build_pending_recovery_facts(state: dict[str, Any]) -> dict[str, Any] | None:
    failure = state.get("executor_failure_kind")
    contract = _fingerprint32(state.get("execution_contract_id"))
    objective = safe_fingerprint(state.get("objective", {}).get("fingerprint"))
    item = current_execution_slice(state) or {}
    slice_id = safe_slice_id(item.get("id"))
    slice_contract = _fingerprint32(slice_contract_id(state))
    if not (
        state.get("executor_state") == "recovery_required"
        and failure in EXECUTOR_FAILURE_KINDS
        and contract
        and objective
        and slice_id
        and slice_contract
    ):
        return None
    mailbox_terminal = next(
        (
            item
            for item in reversed(as_list(state.get("subagents")))
            if isinstance(item, dict)
            and item.get("event") == "mailbox_terminal"
            and item.get("terminal_lifecycle_source") == "mailbox_completed"
            and item.get("role") == "confirmed_executor"
            and item.get("contract_id") == contract
            and item.get("slice_id") == slice_id
            and item.get("slice_contract_id") == slice_contract
            and safe_sequence(item.get("attempt"))
            == safe_sequence(state.get("executor_attempt"))
            and item.get("execution_result_contract_match") is True
            and item.get("execution_result_outcome") == "failed"
            and item.get("reported_failure_kind") == failure
            and _fingerprint32(item.get("reported_failure_fingerprint"))
            and _fingerprint32(item.get("reported_evidence_digest"))
        ),
        None,
    )
    evidence = (
        _fingerprint32(mailbox_terminal.get("reported_evidence_digest"))
        if mailbox_terminal
        else executor_recovery_evidence_digest(state, str(failure))
    )
    fingerprint = (
        _fingerprint32(mailbox_terminal.get("reported_failure_fingerprint"))
        if mailbox_terminal
        else recovery_fingerprint(state, str(failure), evidence)
    )
    if not evidence or not fingerprint:
        return None
    return {
        "sequence": next_sequence(state.get("executor_attempt")),
        "failure_kind": failure,
        "failure_fingerprint": fingerprint,
        "evidence_digest": evidence,
        "execution_contract_id": contract,
        "objective_fingerprint": objective,
        "slice_id": slice_id,
        "slice_contract_id": slice_contract,
        "source": "mailbox_final" if mailbox_terminal else "host_lifecycle",
    }


def pending_recovery_facts_for_state(
    state: dict[str, Any]
) -> dict[str, Any] | None:
    """Return current host-issued recovery facts, never user or child prose."""
    return _build_pending_recovery_facts(state)


def recovery_reservation_context(state: dict[str, Any]) -> str | None:
    facts = pending_recovery_facts_for_state(state)
    if not facts:
        return None
    return (
        "RECOVERY_CHILD_FACTS\n"
        f"recovery_from={facts['failure_kind']}\n"
        f"failure_fingerprint={facts['failure_fingerprint']}\n"
        f"evidence_digest={facts['evidence_digest']}"
    )


def _safe_pending_recovery_reservation(value: Any) -> dict[str, Any] | None:
    record = _safe_recovery_record(value)
    if not record or not isinstance(value, dict):
        return None
    contract = _fingerprint32(value.get("execution_contract_id"))
    objective = safe_fingerprint(value.get("objective_fingerprint")) or None
    slice_id = safe_slice_id(value.get("slice_id"))
    slice_contract = _fingerprint32(value.get("slice_contract_id"))
    prompt_receipt = _fingerprint32(value.get("prompt_receipt"))
    if not all((contract, objective, slice_id, slice_contract, prompt_receipt)):
        return None
    result = {
        **record,
        "execution_contract_id": contract,
        "objective_fingerprint": objective,
        "slice_id": slice_id,
        "slice_contract_id": slice_contract,
        "prompt_receipt": prompt_receipt,
    }
    if value.get("stage") == "terminal_pending":
        terminal_attempt = safe_sequence(value.get("terminal_attempt"))
        terminal_agent_id = (
            safe_label(value.get("terminal_agent_id"), 120)
            if value.get("terminal_agent_id")
            else None
        )
        terminal_task_name = (
            safe_label(value.get("terminal_task_name"), 120)
            if value.get("terminal_task_name")
            else None
        )
        terminal_request_fingerprint = safe_fingerprint(
            value.get("terminal_request_fingerprint")
        )
        if not all(
            (
                terminal_attempt > 0,
                terminal_agent_id,
                terminal_task_name,
                terminal_request_fingerprint,
            )
        ):
            return None
        result.update(
            {
                "stage": "terminal_pending",
                "terminal_attempt": terminal_attempt,
                "terminal_agent_id": terminal_agent_id,
                "terminal_task_name": terminal_task_name,
                "terminal_request_fingerprint": terminal_request_fingerprint,
            }
        )
    return result


def _unique_running_executor_group(
    state: dict[str, Any], reservation: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Resolve one fully bound live executor; ambiguity always returns None."""
    contract = _fingerprint32(state.get("execution_contract_id"))
    current = current_execution_slice(state) or {}
    attempt = safe_sequence(state.get("executor_attempt"))
    agent_id = (
        safe_label(state.get("executor_agent_id"), 120)
        if state.get("executor_agent_id")
        else None
    )
    if not (
        state.get("plan_state") == "confirmed"
        and state.get("confirmed_plan_digest") == state.get("plan_digest")
        and state.get("executor_state") == "running"
        and contract
        and contract == execution_contract_id(state)
        and current.get("id")
        and slice_contract_id(state)
        and attempt > 0
        and agent_id
    ):
        return None
    matches: list[dict[str, Any]] = []
    for group in subagent_lifecycle_groups(state):
        request = group.get("request")
        started = group.get("start")
        if not (
            group.get("state") == "live"
            and group.get("agent_id") == agent_id
            and isinstance(request, dict)
            and isinstance(started, dict)
            and request.get("role") == "confirmed_executor"
            and request.get("requested") is True
            and request.get("host_accepted") is True
            and request.get("contract_id") == contract
            and request.get("slice_id") == current.get("id")
            and request.get("slice_contract_id") == slice_contract_id(state)
            and safe_sequence(request.get("attempt")) == attempt
            and started.get("request_fingerprint")
            == request.get("request_fingerprint")
            and started.get("contract_id") == contract
            and started.get("slice_id") == current.get("id")
            and started.get("slice_contract_id") == slice_contract_id(state)
            and safe_sequence(started.get("attempt")) == attempt
            and started.get("start_observed") == "full"
            and started.get("agent_id") == agent_id
            and request.get("task_name")
        ):
            continue
        if reservation and not (
            reservation.get("stage") == "terminal_pending"
            and reservation.get("execution_contract_id") == contract
            and reservation.get("objective_fingerprint")
            == state.get("objective", {}).get("fingerprint")
            and reservation.get("slice_id") == current.get("id")
            and reservation.get("slice_contract_id") == slice_contract_id(state)
            and reservation.get("sequence") == next_sequence(attempt)
            and reservation.get("terminal_attempt") == attempt
            and reservation.get("terminal_agent_id") == agent_id
            and reservation.get("terminal_task_name") == request.get("task_name")
            and reservation.get("terminal_request_fingerprint")
            == request.get("request_fingerprint")
        ):
            continue
        matches.append(group)
    return matches[0] if len(matches) == 1 else None


def pending_recovery_reservation_for_state(
    state: dict[str, Any], value: Any = None
) -> dict[str, Any] | None:
    reservation = _safe_pending_recovery_reservation(
        state.get("pending_recovery_reservation") if value is None else value
    )
    if reservation and reservation.get("stage") == "terminal_pending":
        return (
            reservation
            if _unique_running_executor_group(state, reservation) is not None
            else None
        )
    current = current_execution_slice(state) or {}
    facts = pending_recovery_facts_for_state(state)
    if not reservation or not (
        facts
        and state.get("executor_state") == "recovery_required"
        and reservation.get("failure_kind") == state.get("executor_failure_kind")
        and reservation.get("failure_kind") == facts.get("failure_kind")
        and reservation.get("failure_fingerprint")
        == facts.get("failure_fingerprint")
        and reservation.get("evidence_digest") == facts.get("evidence_digest")
        and reservation.get("execution_contract_id")
        == state.get("execution_contract_id")
        and reservation.get("objective_fingerprint")
        == state.get("objective", {}).get("fingerprint")
        and reservation.get("slice_id") == current.get("id")
        and reservation.get("slice_contract_id") == slice_contract_id(state)
        and reservation.get("sequence") == next_sequence(state.get("executor_attempt"))
    ):
        return None
    return reservation


def safe_recovery_chain(value: Any) -> list[dict[str, Any]]:
    return [
        record
        for item in as_list(value)
        if (record := _safe_recovery_record(item)) is not None
    ]


def recovery_chain_allows(
    state: dict[str, Any],
    failure: str,
    evidence: str | None,
    correction: str | None,
    root_cause: str | None,
    progress: str | None = None,
    review: str | None = None,
    failure_fingerprint: str | None = None,
) -> bool:
    """Deny unchanged replay, not a numeric attempt count."""
    fingerprint = _fingerprint32(failure_fingerprint) or recovery_fingerprint(
        state, failure, evidence
    )
    if not fingerprint or not correction:
        return False
    evidence_digest = _fingerprint32(evidence)
    progress_digest = _fingerprint32(progress)
    correction_digest = _fingerprint32(correction) or stable_hash(correction, 32)
    root_digest = _fingerprint32(root_cause) or (
        stable_hash(root_cause, 32) if root_cause else None
    )
    review_digest = _fingerprint32(review)
    for prior in reversed(safe_recovery_chain(state.get("recovery_chain"))):
        if prior.get("failure_fingerprint") != fingerprint:
            continue
        return any(
            current is not None and current != prior.get(key)
            for key, current in (
                ("evidence_digest", evidence_digest),
                ("progress_digest", progress_digest),
                ("root_cause_digest", root_digest),
                ("correction_digest", correction_digest),
                ("review_digest", review_digest),
            )
        )
    return True


def parse_pending_recovery_reservation(
    prompt: str, state: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    """Bind visible recovery facts before Desktop encrypts a spawn message."""
    if not re.search(r"(?:recovery_from|recovery-from|恢复自)\s*[:=：]", prompt, re.I):
        return None, None
    if not (
        state.get("plan_state") == "confirmed"
        and state.get("executor_state") == "recovery_required"
        and state.get("executor_failure_kind") in EXECUTOR_FAILURE_KINDS
    ):
        return None, "recovery reservation is not pending"
    host_facts = pending_recovery_facts_for_state(state)
    if not host_facts:
        return None, "recovery reservation lacks host-issued failure evidence"
    failures = re.findall(
        r"(?:recovery_from|recovery-from|恢复自)\s*[:=：]\s*([a-z_]+)\b",
        prompt,
        re.I,
    )
    failure = failures[0].lower() if len(set(failures)) == 1 and failures else None
    if failure != state.get("executor_failure_kind"):
        return None, "recovery failure no longer matches state"

    def one_token(marker: str) -> str | None:
        values = re.findall(marker, prompt, re.I)
        return values[0].lower() if len(set(values)) == 1 and values else None

    raw_failure_fingerprint = one_token(
        r"(?:failure_fingerprint|failure-fingerprint|失败指纹)\s*[:=：]\s*([0-9a-f]{32,64})\b"
    )
    raw_evidence = one_token(
        r"(?:evidence_digest|evidence-digest|verification_evidence_digest|verification-evidence-digest)\s*[:=：]\s*([0-9a-f]{32,64})\b"
    )
    raw_progress = one_token(
        r"(?:progress_digest|progress-digest|进展摘要)\s*[:=：]\s*([0-9a-f]{32,64})\b"
    )
    root_matches = re.findall(
        r"(?:root_cause|root-cause|根因)\s*[:=：]\s*([^\n\r]{2,512})",
        prompt,
        re.I,
    )
    correction_matches = re.findall(
        r"(?:material_correction|material-correction|实质修正)\s*[:=：]\s*([^\n\r]{8,2048})",
        prompt,
        re.I,
    )
    failure_fingerprint = _normalized_recovery_digest(
        raw_failure_fingerprint, "workflow-manager-external-failure-v1"
    )
    evidence = _normalized_recovery_digest(
        raw_evidence, "workflow-manager-external-evidence-v1"
    )
    progress = (
        _normalized_recovery_digest(
            raw_progress, "workflow-manager-external-progress-v1"
        )
        if raw_progress
        else None
    )
    if not failure_fingerprint or not evidence:
        return None, "recovery reservation requires one failure fingerprint and evidence digest"
    if (
        failure_fingerprint != host_facts.get("failure_fingerprint")
        or evidence != host_facts.get("evidence_digest")
    ):
        return None, "recovery reservation no longer matches host-issued failure evidence"
    if len(root_matches) != 1 or len(correction_matches) != 1:
        return None, "recovery reservation requires one root cause and material correction"
    root_digest = stable_hash(root_matches[0].strip(), 32)
    correction_digest = stable_hash(correction_matches[0].strip(), 32)
    bound_review = _fingerprint32(
        _safe_executor_review(state.get("executor_review")).get(
            "review_evidence_digest"
        )
    )
    review = bound_review or stable_hash(
        "workflow-manager-recovery-review-v1\0"
        + canonical_json(
            {
                "correction": correction_digest,
                "evidence": evidence,
                "failure": failure,
                "failure_fingerprint": failure_fingerprint,
                "progress": progress,
                "root": root_digest,
            }
        ),
        32,
    )
    if not recovery_chain_allows(
        state,
        failure,
        evidence,
        correction_digest,
        root_digest,
        progress,
        review,
        failure_fingerprint,
    ):
        return None, (
            "unchanged recovery replay requires new evidence, root cause, "
            "progress, or material correction"
        )
    current = current_execution_slice(state) or {}
    sequence = next_sequence(state.get("executor_attempt"))
    receipt = stable_hash(
        "workflow-manager-pending-recovery-reservation-v1\0"
        + canonical_json(
            {
                "contract": state.get("execution_contract_id"),
                "correction": correction_digest,
                "evidence": evidence,
                "failure": failure,
                "failure_fingerprint": failure_fingerprint,
                "objective": state.get("objective", {}).get("fingerprint"),
                "progress": progress,
                "review": review,
                "root": root_digest,
                "sequence": sequence,
                "slice": current.get("id"),
                "slice_contract": slice_contract_id(state),
            }
        ),
        32,
    )
    return {
        "sequence": sequence,
        "failure_kind": failure,
        "failure_fingerprint": failure_fingerprint,
        "evidence_digest": evidence,
        "progress_digest": progress,
        "root_cause_digest": root_digest,
        "correction_digest": correction_digest,
        "review_digest": review,
        "execution_contract_id": state.get("execution_contract_id"),
        "objective_fingerprint": state.get("objective", {}).get("fingerprint"),
        "slice_id": current.get("id"),
        "slice_contract_id": slice_contract_id(state),
        "prompt_receipt": receipt,
    }, None


def parse_running_recovery_reservation(
    prompt: str, state: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    """Stage an exact recovery claim while its bound executor is still live.

    The record carries no mutation authority.  It becomes usable only after a
    separately observed, matching mailbox terminal boundary reports the same
    failure identity.
    """
    if not re.search(
        r"(?:recovery_from|recovery-from|恢复自)\s*[:=：]", prompt, re.I
    ):
        return None, None
    group = _unique_running_executor_group(state)
    if group is None:
        return None, "early recovery has no unique live executor binding"
    claim, error = _recovery_prompt_claim(prompt)
    if not claim:
        return None, error
    if not recovery_chain_allows(
        state,
        claim["failure_kind"],
        claim.get("evidence_digest"),
        claim.get("correction_digest"),
        claim.get("root_cause_digest"),
        claim.get("progress_digest"),
        None,
        claim.get("failure_fingerprint"),
    ):
        return None, (
            "unchanged recovery replay requires new evidence, root cause, "
            "progress, or material correction"
        )
    request = group["request"]
    current = current_execution_slice(state) or {}
    sequence = next_sequence(state.get("executor_attempt"))
    review = stable_hash(
        "workflow-manager-early-recovery-review-v1\0"
        + canonical_json(
            {
                "agent": stable_hash(str(group.get("agent_id")), 32),
                "attempt": safe_sequence(state.get("executor_attempt")),
                "contract": state.get("execution_contract_id"),
                "correction": claim.get("correction_digest"),
                "evidence": claim.get("evidence_digest"),
                "failure": claim.get("failure_kind"),
                "failure_fingerprint": claim.get("failure_fingerprint"),
                "request": request.get("request_fingerprint"),
                "root": claim.get("root_cause_digest"),
                "sequence": sequence,
                "slice": current.get("id"),
            }
        ),
        32,
    )
    receipt = stable_hash(
        "workflow-manager-terminal-pending-recovery-v1\0"
        + canonical_json(
            {
                "attempt": safe_sequence(state.get("executor_attempt")),
                "contract": state.get("execution_contract_id"),
                "correction": claim.get("correction_digest"),
                "evidence": claim.get("evidence_digest"),
                "failure": claim.get("failure_kind"),
                "failure_fingerprint": claim.get("failure_fingerprint"),
                "objective": state.get("objective", {}).get("fingerprint"),
                "progress": claim.get("progress_digest"),
                "request": request.get("request_fingerprint"),
                "review": review,
                "root": claim.get("root_cause_digest"),
                "sequence": sequence,
                "slice": current.get("id"),
                "slice_contract": slice_contract_id(state),
                "task": request.get("task_name"),
            }
        ),
        32,
    )
    return {
        **claim,
        "sequence": sequence,
        "review_digest": review,
        "execution_contract_id": state.get("execution_contract_id"),
        "objective_fingerprint": state.get("objective", {}).get("fingerprint"),
        "slice_id": current.get("id"),
        "slice_contract_id": slice_contract_id(state),
        "prompt_receipt": receipt,
        "stage": "terminal_pending",
        "terminal_attempt": safe_sequence(state.get("executor_attempt")),
        "terminal_agent_id": group.get("agent_id"),
        "terminal_task_name": request.get("task_name"),
        "terminal_request_fingerprint": request.get("request_fingerprint"),
    }, None


def parse_recovery_contract(
    request_text: str, state: dict[str, Any], *, opaque: bool = False
) -> tuple[dict[str, Any] | None, str | None]:
    """Parse a typed recovery before any executor authority is changed."""
    if opaque:
        pending = pending_recovery_reservation_for_state(state)
        if not pending:
            return None, "opaque recovery requires a host-bound prompt reservation"
        if not recovery_chain_allows(
            state,
            pending["failure_kind"],
            pending.get("evidence_digest"),
            pending.get("correction_digest"),
            pending.get("root_cause_digest"),
            pending.get("progress_digest"),
            pending.get("review_digest"),
            pending.get("failure_fingerprint"),
        ):
            return None, "opaque recovery reservation is an unchanged replay"
        return {
            key: pending.get(key)
            for key in (
                "sequence",
                "failure_kind",
                "failure_fingerprint",
                "evidence_digest",
                "progress_digest",
                "root_cause_digest",
                "correction_digest",
                "review_digest",
            )
        }, None
    failures = re.findall(
        r"(?:recovery_from|recovery-from|恢复自)\s*[:=：]\s*([a-z_]+)\b",
        request_text,
        re.I,
    )
    if len(failures) != 1 or failures[0] not in EXECUTOR_FAILURE_KINDS:
        return None, "recovery request lacks one typed failure"
    failure = failures[0]
    if failure != state.get("executor_failure_kind"):
        return None, "recovery failure no longer matches state"

    def one_digest(pattern: str) -> str | None:
        matches = re.findall(pattern, request_text, re.I)
        return matches[0].lower() if len(set(matches)) == 1 and matches else None

    failure_fingerprint = _normalized_recovery_digest(
        one_digest(
            r"(?:failure_fingerprint|failure-fingerprint|失败指纹)\s*[:=：]\s*([0-9a-f]{32,64})\b"
        ),
        "workflow-manager-external-failure-v1",
    ) or recovery_fingerprint(state, failure)
    evidence = _normalized_recovery_digest(
        one_digest(
            r"(?:evidence_digest|evidence-digest|verification_evidence_digest|verification-evidence-digest)\s*[:=：]\s*([0-9a-f]{32,64})\b"
        ),
        "workflow-manager-external-evidence-v1",
    )
    raw_progress = one_digest(
        r"(?:progress_digest|progress-digest|进展摘要)\s*[:=：]\s*([0-9a-f]{32,64})\b"
    )
    progress = (
        _normalized_recovery_digest(
            raw_progress, "workflow-manager-external-progress-v1"
        )
        if raw_progress
        else None
    )
    root_matches = re.findall(
        r"(?:root_cause|root-cause|根因)\s*[:=：]\s*([^\n\r]{2,512})",
        request_text,
        re.I,
    )
    correction_matches = re.findall(
        r"(?:material_correction|material-correction|实质修正)\s*[:=：]\s*([^\n\r]{8,2048})",
        request_text,
        re.I,
    )
    if len(correction_matches) != 1:
        return None, "recovery request lacks one material correction"
    root_digest = stable_hash(root_matches[0].strip(), 32) if len(root_matches) == 1 else None
    correction_digest = stable_hash(correction_matches[0].strip(), 32)
    review_data = _safe_executor_review(state.get("executor_review"))
    review_digest = _fingerprint32(review_data.get("review_evidence_digest")) or stable_hash(
        "workflow-manager-recovery-review-v1\0"
        + canonical_json(
            {
                "correction": correction_digest,
                "evidence": evidence,
                "failure": failure,
                "failure_fingerprint": failure_fingerprint,
                "progress": progress,
                "root": root_digest,
            }
        ),
        32,
    )
    if not recovery_chain_allows(
        state,
        failure,
        evidence,
        correction_digest,
        root_digest,
        progress,
        review_digest,
        failure_fingerprint,
    ):
        return None, "unchanged recovery replay requires new evidence, root cause, progress, or material correction"
    return {
        "sequence": next_sequence(state.get("executor_attempt")),
        "failure_kind": failure,
        "failure_fingerprint": failure_fingerprint,
        "evidence_digest": _fingerprint32(evidence),
        "progress_digest": _fingerprint32(progress),
        "root_cause_digest": root_digest,
        "correction_digest": correction_digest,
        "review_digest": review_digest,
    }, None


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
    state["model_profile"] = confirmed_executor_model_profile(state)
    return True


def trusted_plan_binding_valid(state: dict[str, Any], payload: dict[str, Any]) -> bool:
    artifact = _safe_plan_artifact(state.get("plan_artifact"))
    return bool(
        state.get("plan_digest")
        and state.get("plan_objective_fingerprint")
        == state.get("objective", {}).get("fingerprint")
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
        and verify_plan_artifact(state, payload)
    )


def activate_trusted_plan(
    state: dict[str, Any],
    payload: dict[str, Any],
    *,
    receipt: str,
    increment_confirmation: bool,
) -> bool:
    """Bind confirmation to scope while execution remains revision-exact."""
    envelope_digest = authorization_envelope_digest(state)
    receipt_digest = _fingerprint32(receipt)
    if not envelope_digest or not receipt_digest or not trusted_plan_binding_valid(state, payload):
        return False
    prior = _safe_authorization_envelope(state.get("authorization_envelope"))
    count = (
        next_sequence(prior.get("confirmation_count"))
        if increment_confirmation and prior.get("digest") == envelope_digest
        else 1
        if increment_confirmation
        else max(safe_sequence(prior.get("confirmation_count")), 1)
    )
    state["plan_state"] = "confirmed"
    state["confirmed_plan_digest"] = state.get("plan_digest")
    state["confirmed_at"] = utc_now()
    state["authorization_envelope"] = {
        "digest": envelope_digest,
        "strict_confirm_receipt": receipt_digest,
        "confirmation_count": count,
    }
    state["pending_confirmation_receipt"] = None
    if initialize_confirmed_executor(state):
        lineage = _safe_causal_lineage(state.get("causal_lineage"))
        if lineage.get("selected_revision_digest") == state.get("plan_digest"):
            lineage["selected_contract_id"] = state.get("execution_contract_id")
            state["causal_lineage"] = _safe_causal_lineage(lineage)
        state["pending_causal_revision"] = {}
        return True
    state["plan_state"] = "invalidated"
    state["confirmed_plan_digest"] = None
    state["confirmed_at"] = None
    return False


def auto_confirm_trusted_plan(state: dict[str, Any], payload: dict[str, Any]) -> bool:
    """Consume an early receipt or inherit an unchanged authorization envelope."""
    expected = authorization_envelope_digest(state)
    pending = _fingerprint32(state.get("pending_confirmation_receipt"))
    if pending and pending == pending_confirmation_receipt_for_state(state):
        return activate_trusted_plan(
            state,
            payload,
            receipt=pending,
            increment_confirmation=True,
        )
    envelope = _safe_authorization_envelope(state.get("authorization_envelope"))
    if (
        expected
        and envelope.get("digest") == expected
        and envelope.get("strict_confirm_receipt")
        and causal_successor_inheritance_valid(state)
    ):
        return activate_trusted_plan(
            state,
            payload,
            receipt=str(envelope["strict_confirm_receipt"]),
            increment_confirmation=False,
        )
    return False


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


def metadata_is_exact_confirmation(value: Any) -> bool:
    """Recognize the one canonical confirmation from digest-only metadata."""
    observed = safe_metadata(value)
    expected = text_metadata("确认执行")
    return bool(
        observed.get("fingerprint") == expected["fingerprint"]
        and observed.get("length") == expected["length"]
    )


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_sequence(value: Any) -> int:
    """Normalize a monotonic identity without imposing a retry-count cap."""
    candidate = safe_int(value)
    return candidate if candidate >= 0 else 0


def next_sequence(value: Any) -> int:
    return safe_sequence(value) + 1


def safe_slice_id(value: Any) -> str | None:
    candidate = str(value or "")
    return (
        candidate
        if len(candidate.encode("utf-8")) <= 32
        and re.fullmatch(r"s(?:0[1-9]|[1-9][0-9]+)", candidate)
        else None
    )


def state_node_count(value: Any) -> int:
    if isinstance(value, dict):
        return 1 + sum(1 + state_node_count(item) for item in value.values())
    if isinstance(value, list):
        return 1 + sum(state_node_count(item) for item in value)
    return 1


def state_within_budget(value: dict[str, Any]) -> bool:
    """Apply the same bounded-state contract before authority is reserved."""
    try:
        encoded = canonical_json(value).encode("utf-8")
    except (TypeError, ValueError):
        return False
    return len(encoded) <= MAX_STATE_BYTES and state_node_count(value) <= MAX_STATE_NODES


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
    if not value.get("task_domain") and not value.get("work_difficulty"):
        return {}
    return decorate_route(
        {
            "task_domain": value.get("task_domain"),
            "domain_confidence": value.get("domain_confidence"),
            "domain_rule_codes": as_list(value.get("domain_rule_codes")),
            "model_profile": value.get("model_profile"),
            "domain_classifier_version": value.get("domain_classifier_version"),
            "domain_decision_id": value.get("domain_decision_id"),
            "work_difficulty": value.get("work_difficulty"),
            "difficulty_confidence": value.get("difficulty_confidence"),
            "difficulty_rule_codes": as_list(value.get("difficulty_rule_codes")),
            "difficulty_classifier_version": value.get("difficulty_classifier_version"),
            "difficulty_decision_id": value.get("difficulty_decision_id"),
            "phase_hints": as_list(value.get("phase_hints")),
            "route_source": safe_label(value.get("route_source"), 32)
            if value.get("route_source")
            else "authorization_classifier",
        }
    )
def safe_label(value: Any, limit: int = 96) -> str:
    text = re.sub(r"[^A-Za-z0-9._:@/+-]+", "_", str(value or "unknown"))
    return (text[:limit] or "unknown").strip("_") or "unknown"


def safe_id(value: Any) -> str:
    raw = str(value or "")
    readable = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip("._-")[:56] or "session"
    return f"{readable}-{stable_hash(raw)}"


def plan_artifact_session_id(value: Any, epoch_id: Any = None) -> str:
    """Keep each private canonical journal unlinkable and epoch-scoped.

    A session may legitimately host several unrelated objectives.  The old
    schema used one directory per session, which let a new cwd/objective reuse
    an old journal identity.  Epoch-less calls intentionally retain the v31
    path so migration can read historical journals without rewriting them.
    """
    raw = str(value or "")
    scope = raw if not epoch_id else raw + chr(0) + str(epoch_id)
    return f"session-{stable_hash('plan-artifact-session' + chr(0) + scope)}"


def _safe_task_epoch(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"id": None, "sequence": 0, "status": "none", "objective_fingerprint": None}
    epoch_id = safe_fingerprint(value.get("id"))
    status = value.get("status") if value.get("status") in {
        "active", "archived", "isolated_incomplete", "authority_unknown", "blocked"
    } else "none"
    return {
        "id": epoch_id or None,
        "sequence": max(safe_int(value.get("sequence")), 0),
        "status": status,
        "objective_fingerprint": safe_fingerprint(value.get("objective_fingerprint")) or None,
    }


def _safe_child_liveness(value: Any) -> dict[str, Any]:
    item = value if isinstance(value, dict) else {}
    status = item.get("status") if item.get("status") in CHILD_LIVENESS_STATES else "none"
    role = item.get("role") if item.get("role") in {
        "high_assessor", "confirmed_executor"
    } else None
    epoch_id = safe_fingerprint(item.get("epoch_id")) or None
    agent_fingerprint = _fingerprint32(item.get("agent_fingerprint"))
    request_fingerprint = safe_fingerprint(item.get("request_fingerprint")) or None
    # A host can prove that a reserved writer was never created (for example
    # capacity exhaustion or a SIGKILL before an id was assigned).  Such an
    # explicit absence is still a structured lifecycle fact, not ``unknown``.
    # Every other non-empty status needs an agent identity; otherwise it must
    # remain fail-closed.
    if status == "absent" and not (role and request_fingerprint):
        status = "unknown"
    elif status != "none" and status != "absent" and not (role and agent_fingerprint):
        status = "unknown"
    return {
        "status": status,
        "role": role,
        "epoch_id": epoch_id,
        "agent_fingerprint": agent_fingerprint,
        "request_fingerprint": request_fingerprint,
        "source": (
            item.get("source")
            if item.get("source") in {"host_inventory", "schema_migration", "host_lifecycle"}
            else None
        ),
        "observation_digest": _fingerprint32(item.get("observation_digest")),
        "at": str(item.get("at") or "")[:40] or None,
    }


def _safe_parent_writer_lease(value: Any) -> dict[str, Any]:
    item = value if isinstance(value, dict) else {}
    status = item.get("status") if item.get("status") in PARENT_WRITER_LEASE_STATES else "none"
    return {
        "status": status,
        "epoch_id": safe_fingerprint(item.get("epoch_id")) or None,
        "execution_contract_id": _fingerprint32(item.get("execution_contract_id")),
        "slice_id": safe_slice_id(item.get("slice_id")),
        "slice_contract_id": _fingerprint32(item.get("slice_contract_id")),
        "attempt": safe_sequence(item.get("attempt")),
        "acquired_at": str(item.get("acquired_at") or "")[:40] or None,
        "last_operation_digest": _fingerprint32(item.get("last_operation_digest")),
    }


def parent_writer_lease_current(state: dict[str, Any]) -> bool:
    lease = _safe_parent_writer_lease(state.get("parent_writer_lease"))
    current = current_execution_slice(state) or {}
    return bool(
        lease.get("status") == "live"
        and lease.get("epoch_id") == current_task_epoch_id(state)
        and lease.get("execution_contract_id") == state.get("execution_contract_id")
        and lease.get("slice_id") == current.get("id")
        and lease.get("slice_contract_id") == slice_contract_id(state)
        and lease.get("attempt") == safe_sequence(state.get("executor_attempt"))
        and state.get("execution_contract_id") == execution_contract_id(state)
    )


def parent_writer_acquisition_block(state: dict[str, Any]) -> str | None:
    if state.get("plan_state") != "confirmed" or state.get("confirmed_plan_digest") != state.get("plan_digest"):
        return "the Hard plan is not currently confirmed"
    if state.get("execution_contract_id") != execution_contract_id(state):
        return "the execution contract is stale"
    if _safe_causal_review(state.get("causal_review")).get("state") in {"triage_required", "triaging"}:
        return "causal diagnosis is unfinished"
    if _safe_stall(state.get("stall")).get("state") not in {"none", "resolved"}:
        return "stall diagnosis is unfinished"
    liveness = _safe_child_liveness(state.get("child_liveness")).get("status")
    if liveness in {"live", "unknown"}:
        return f"child writer liveness is {liveness}"
    if any(group.get("state") in {"pending", "result_pending", "live"} for group in subagent_lifecycle_groups(state)):
        return "a child writer is reserved or live"
    if state.get("executor_state") not in {"spawn_required", "recovery_required", "verification_required", "running"}:
        return "the current slice is not writable"
    return None


def acquire_parent_writer_lease(state: dict[str, Any]) -> bool:
    if parent_writer_lease_current(state):
        return True
    if parent_writer_acquisition_block(state):
        return False
    state["executor_attempt"] = next_sequence(state.get("executor_attempt"))
    state["executor_agent_id"] = None
    state["executor_state"] = "running"
    state["executor_failure_kind"] = None
    state["executor_review"] = _empty_executor_review()
    state["pending_recovery_facts"] = None
    state["pending_recovery_reservation"] = None
    current = current_execution_slice(state) or {}
    state["parent_writer_lease"] = _safe_parent_writer_lease({
        "status": "live", "epoch_id": current_task_epoch_id(state),
        "execution_contract_id": state.get("execution_contract_id"),
        "slice_id": current.get("id"), "slice_contract_id": slice_contract_id(state),
        "attempt": state.get("executor_attempt"), "acquired_at": utc_now(),
    })
    return True


def _safe_isolated_lifecycle(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    status = value.get("status")
    role = value.get("role")
    agent = _fingerprint32(value.get("agent_fingerprint"))
    request = safe_fingerprint(value.get("request_fingerprint")) or None
    # An explicit host absence can precede assignment of an agent id.  Keep a
    # bounded synthetic identity for the tombstone so a late event cannot be
    # mistaken for a successor, while never inventing a live agent.
    if (
        status not in ISOLATED_LIFECYCLE_STATES
        or role not in {"high_assessor", "confirmed_executor"}
        or (not agent and not (status == "isolated_incomplete" and request))
    ):
        return None
    return {
        "status": status,
        "role": role,
        "epoch_id": safe_fingerprint(value.get("epoch_id")) or None,
        "agent_fingerprint": agent,
        "request_fingerprint": request,
        "contract_id": _fingerprint32(value.get("contract_id")),
        "attempt": safe_sequence(value.get("attempt")),
        "event_digest": _fingerprint32(value.get("event_digest")),
        "at": str(value.get("at") or "")[:40] or None,
    }


def current_task_epoch_id(state: dict[str, Any]) -> str | None:
    return _safe_task_epoch(state.get("task_epoch")).get("id")


def task_epoch_id(payload: dict[str, Any], sequence: int, objective_fingerprint: Any) -> str:
    return stable_hash(
        "task-epoch-v1\0%s\0%s\0%s" % (
            safe_label(payload.get("session_id"), 120), max(sequence, 1),
            safe_fingerprint(objective_fingerprint) or "unbound",
        ), 32,
    )


def _epoch_has_live_writer(state: dict[str, Any]) -> bool:
    liveness = _safe_child_liveness(state.get("child_liveness"))
    return bool(
        state.get("executor_state") in {"spawn_pending", "running"}
        or state.get("assessor_state") in {"spawn_pending", "running"}
        or liveness.get("status") in {"live", "unknown"}
    )


def _active_legacy_lifecycle(value: dict[str, Any]) -> dict[str, Any] | None:
    """Project one old-writer owner without granting it current authority."""
    executor_active = value.get("executor_state") in {"spawn_pending", "running"}
    assessor_active = value.get("assessor_state") in {"spawn_pending", "running"}
    if not (executor_active or assessor_active):
        return None
    role = "confirmed_executor" if executor_active else "high_assessor"
    agent_id = value.get("executor_agent_id") if executor_active else value.get("assessor_agent_id")
    contract = value.get("execution_contract_id") if executor_active else value.get("assessor_binding_id")
    attempt = value.get("executor_attempt") if executor_active else value.get("assessor_attempt")
    request = next(
        (
            item
            for item in reversed(as_list(value.get("subagents")))
            if isinstance(item, dict)
            and item.get("event") == "request"
            and item.get("role") == role
            and (
                not contract
                or safe_fingerprint(item.get("contract_id"))
                == safe_fingerprint(contract)
            )
            and safe_sequence(item.get("attempt")) == safe_sequence(attempt)
        ),
        {},
    )
    started = next(
        (
            item
            for item in reversed(as_list(value.get("subagents")))
            if isinstance(item, dict)
            and item.get("event") == "start"
            and item.get("role") == role
            and (
                not agent_id or str(item.get("agent_id") or "") == str(agent_id)
            )
        ),
        {},
    )
    resolved_agent = str(agent_id or started.get("agent_id") or "").strip()
    if not resolved_agent:
        return None
    return _safe_isolated_lifecycle(
        {
            "status": "isolated_incomplete",
            "role": role,
            "epoch_id": _safe_task_epoch(value.get("task_epoch")).get("id"),
            "agent_fingerprint": stable_hash(resolved_agent, 32),
            "request_fingerprint": request.get("request_fingerprint")
            or started.get("request_fingerprint"),
            "contract_id": contract,
            "attempt": attempt,
            "at": utc_now(),
        }
    )


def isolate_legacy_writer(
    state: dict[str, Any], value: dict[str, Any], *, source_writer: str
) -> bool:
    """Tombstone one pre-v12 owner and require a fresh current lifecycle.

    The tombstone keeps only bounded routing identities.  It lets a late
    Start/Stop be attributed to the old epoch after transient lifecycle rows
    are discarded, without ever making the old process a current writer.
    """
    isolated = _active_legacy_lifecycle(value)
    if isolated is None:
        return False
    existing = [
        item
        for raw in as_list(state.get("isolated_lifecycles"))
        if (item := _safe_isolated_lifecycle(raw)) is not None
    ]
    identity = (
        isolated.get("role"), isolated.get("agent_fingerprint"),
        isolated.get("request_fingerprint"), isolated.get("attempt"),
    )
    if not any(
        (
            item.get("role"), item.get("agent_fingerprint"),
            item.get("request_fingerprint"), item.get("attempt"),
        ) == identity
        for item in existing
    ):
        existing.append(isolated)
    state["isolated_lifecycles"] = existing[-MAX_ISOLATED_LIFECYCLES:]
    state["child_liveness"] = _safe_child_liveness(
        {
            "status": "isolated_incomplete",
            "role": isolated["role"],
            "epoch_id": isolated.get("epoch_id"),
            "agent_fingerprint": isolated["agent_fingerprint"],
            "request_fingerprint": isolated.get("request_fingerprint"),
            "source": "schema_migration",
            "observation_digest": stable_hash(
                f"schema33-isolation\0{source_writer}\0{isolated['agent_fingerprint']}",
                32,
            ),
            "at": utc_now(),
        }
    )
    record_lifecycle_diagnostic(
        state,
        "legacy_writer_isolated",
        level="warning",
        role=isolated["role"],
        request_fingerprint=isolated.get("request_fingerprint"),
        contract_id=isolated.get("contract_id"),
    )
    if isolated["role"] == "confirmed_executor":
        state["executor_state"] = "recovery_required"
        state["executor_failure_kind"] = "stale_contract"
        state["executor_agent_id"] = None
        state["executor_observed_effective"] = False
        state["executor_review"] = _empty_executor_review()
        state["model_profile"] = "work_assessment"
    else:
        state["assessor_state"] = "recovery_required"
        state["assessor_failure_kind"] = "stale_binding"
        state["assessor_agent_id"] = None
        state["assessor_observed_effective"] = False
        state["model_profile"] = "work_assessment"
    return True


def rotate_task_epoch(state: dict[str, Any], payload: dict[str, Any], objective: dict[str, Any]) -> bool:
    """Archive a terminal epoch and create an isolated successor.

    This function is deliberately called only after the prompt classifier has
    ruled out a same-objective worktree migration.  It never revokes an active
    writer: callers must retain the old epoch and surface a diagnostic instead.
    """
    if _epoch_has_live_writer(state):
        record_lifecycle_diagnostic(state, "epoch_switch_live_writer", level="error")
        return False
    current = _safe_task_epoch(state.get("task_epoch"))
    archive = list(state.get("archived_epochs", []))[-7:]
    if current.get("id"):
        status = "archived"
        if state.get("executor_state") not in {"none", "succeeded"}:
            status = "isolated_incomplete"
        if state.get("executor_start_observed") == "full" and not state.get("executor_observed_effective"):
            status = "authority_unknown"
            record_lifecycle_diagnostic(state, "epoch_authority_unknown", level="warning")
        archive.append({
            "id": current["id"], "sequence": current["sequence"], "status": status,
            "objective_fingerprint": current.get("objective_fingerprint"),
            "plan_digest": safe_fingerprint(state.get("plan_digest")) or None,
            "execution_contract_id": safe_fingerprint(state.get("execution_contract_id")) or None,
        })
    sequence = max(current.get("sequence", 0), 0) + 1
    state["task_epoch"] = {
        "id": task_epoch_id(payload, sequence, objective.get("fingerprint")),
        "sequence": sequence, "status": "active",
        "objective_fingerprint": safe_fingerprint(objective.get("fingerprint")) or None,
    }
    state["cwd_fingerprint"] = stable_hash(payload.get("cwd"))
    state["root_cwd_fingerprint"] = state["cwd_fingerprint"]
    state["root_session_fingerprint"] = stable_hash(payload.get("session_id") or payload.get("hook_run_id"))
    state["root_rollout_identity"] = None
    state["archived_epochs"] = archive[-8:]
    return True


def _lifecycle_agent_fingerprint(agent_id: Any, request_fingerprint: Any) -> str:
    """Give pre-assignment tombstones a deterministic non-live identity."""
    agent = safe_label(agent_id, 120) if agent_id else ""
    if agent:
        return stable_hash("workflow-manager-agent-v1\0" + agent, 32)
    return stable_hash(
        "workflow-manager-unassigned-writer-v1\0"
        + (safe_fingerprint(request_fingerprint) or "unknown"),
        32,
    )


def _append_isolated_lifecycle(
    state: dict[str, Any], *, status: str, role: str, agent_id: Any,
    request_fingerprint: Any, contract_id: Any, attempt: Any,
    event_material: Any, epoch_id: Any = None,
) -> None:
    """Keep a bounded tombstone; it never re-grants writer ownership."""
    if status not in ISOLATED_LIFECYCLE_STATES or role not in {
        "high_assessor", "confirmed_executor"
    }:
        return
    request = safe_fingerprint(request_fingerprint) or None
    item = _safe_isolated_lifecycle(
        {
            "status": status,
            "role": role,
            "epoch_id": safe_fingerprint(epoch_id) or current_task_epoch_id(state),
            "agent_fingerprint": _lifecycle_agent_fingerprint(agent_id, request),
            "request_fingerprint": request,
            "contract_id": contract_id,
            "attempt": attempt,
            "event_digest": stable_hash(
                "workflow-manager-isolated-lifecycle-v1\0"
                + canonical_json(event_material), 32,
            ),
            "at": utc_now(),
        }
    )
    if item is None:
        return
    current = [
        value for raw in as_list(state.get("isolated_lifecycles"))
        if (value := _safe_isolated_lifecycle(raw)) is not None
    ]
    identity = (
        item.get("status"), item.get("role"), item.get("epoch_id"),
        item.get("agent_fingerprint"), item.get("request_fingerprint"),
        item.get("attempt"), item.get("event_digest"),
    )
    if not any(
        (
            existing.get("status"), existing.get("role"), existing.get("epoch_id"),
            existing.get("agent_fingerprint"), existing.get("request_fingerprint"),
            existing.get("attempt"), existing.get("event_digest"),
        ) == identity
        for existing in current
    ):
        current.append(item)
    state["isolated_lifecycles"] = current[-MAX_ISOLATED_LIFECYCLES:]


def _writer_liveness_binding(
    state: dict[str, Any], role: str, *, request: dict[str, Any] | None = None,
    agent_id: Any = None,
) -> dict[str, Any]:
    """Project the current epoch/role/request/attempt identity once."""
    record = request if isinstance(request, dict) else {}
    if role == "confirmed_executor":
        contract = state.get("execution_contract_id")
        attempt = state.get("executor_attempt")
        live_agent = state.get("executor_agent_id")
    else:
        contract = state.get("assessor_binding_id")
        attempt = state.get("assessor_attempt")
        live_agent = state.get("assessor_agent_id")
    resolved_agent = agent_id if agent_id not in (None, "") else live_agent
    request_fp = record.get("request_fingerprint")
    return {
        "role": role,
        "epoch_id": current_task_epoch_id(state),
        "agent_id": safe_label(resolved_agent, 120) if resolved_agent else None,
        "agent_fingerprint": _lifecycle_agent_fingerprint(resolved_agent, request_fp),
        "request_fingerprint": safe_fingerprint(request_fp) or None,
        "contract_id": _fingerprint32(record.get("contract_id") or contract),
        "attempt": safe_sequence(record.get("attempt") or attempt),
    }


def _set_writer_liveness(
    state: dict[str, Any], *, status: str, binding: dict[str, Any], source: str,
    observation: Any,
) -> None:
    """Persist the only allowed inventory states: live, absent, or unknown."""
    state["child_liveness"] = _safe_child_liveness(
        {
            "status": status,
            "role": binding.get("role"),
            "epoch_id": binding.get("epoch_id"),
            "agent_fingerprint": binding.get("agent_fingerprint"),
            "request_fingerprint": binding.get("request_fingerprint"),
            "source": source,
            "observation_digest": stable_hash(
                "workflow-manager-writer-inventory-v1\0"
                + canonical_json(observation), 32,
            ),
            "at": utc_now(),
        }
    )


def _release_writer_after_explicit_absence(
    state: dict[str, Any], *, binding: dict[str, Any], reason: str,
    source: str, observation: Any,
) -> None:
    """A trusted explicit absence frees the slot but leaves a tombstone."""
    role = str(binding.get("role") or "")
    if role not in {"high_assessor", "confirmed_executor"}:
        return
    # Retire a request-only reservation before opening its successor slot.
    # Otherwise it remains pending, blocking the successor or capturing a
    # delayed Start intended for the old request.
    if role == "high_assessor" and binding.get("agent_id") is None:
        matching_requests = [
            item for item in as_list(state.get("subagents"))
            if isinstance(item, dict)
            and item.get("event") == "request"
            and item.get("role") == role
            and item.get("epoch_id") == binding.get("epoch_id")
            and item.get("request_fingerprint") == binding.get("request_fingerprint")
            and item.get("contract_id") == binding.get("contract_id")
            and safe_sequence(item.get("attempt")) == safe_sequence(binding.get("attempt"))
        ]
        if len(matching_requests) == 1:
            matching_requests[0]["status"] = "isolated_incomplete"
    _append_isolated_lifecycle(
        state,
        status="isolated_incomplete",
        role=role,
        agent_id=binding.get("agent_id"),
        request_fingerprint=binding.get("request_fingerprint"),
        contract_id=binding.get("contract_id"),
        attempt=binding.get("attempt"),
        event_material={"reason": reason, "source": source, "observation": observation},
    )
    # ``isolated_incomplete`` is deliberately non-live, so the current parent
    # can decide whether to recover.  It cannot make a late event authoritative.
    _set_writer_liveness(
        state, status="isolated_incomplete", binding=binding,
        source=source, observation={"reason": reason, "observation": observation},
    )
    record_lifecycle_diagnostic(
        state, "inventory_writer_absent", level="warning", role=role,
        request_fingerprint=binding.get("request_fingerprint"),
        contract_id=binding.get("contract_id"),
    )
    if role == "confirmed_executor":
        state["executor_state"] = "recovery_required"
        state["executor_failure_kind"] = (
            "model_unavailable" if reason == "capacity" else "incomplete_execution"
        )
        state["executor_agent_id"] = None
        state["executor_observed_effective"] = False
        state["model_profile"] = "work_assessment"
    else:
        state["assessor_state"] = "recovery_required"
        state["assessor_failure_kind"] = "model_unavailable"
        state["assessor_agent_id"] = None
        state["assessor_observed_effective"] = False
        state["model_profile"] = "work_assessment"


def _mark_writer_inventory_unknown(
    state: dict[str, Any], *, binding: dict[str, Any], source: str,
    observation: Any, preserve_writer: bool = False,
) -> None:
    """Unknown inventory keeps the one-writer gate closed until host truth arrives."""
    role = str(binding.get("role") or "")
    _set_writer_liveness(
        state, status="unknown", binding=binding, source=source,
        observation=observation,
    )
    record_lifecycle_diagnostic(
        state, "inventory_writer_unknown", level="error", role=role,
        request_fingerprint=binding.get("request_fingerprint"),
        contract_id=binding.get("contract_id"),
    )
    if preserve_writer:
        # A malformed or partial inventory says neither that the writer is
        # gone nor that its terminal result is safe. Keep the current host
        # lifecycle identity so an exact Stop/mailbox result can still close
        # it, while the ``unknown`` liveness gate blocks every successor.
        return
    if role == "confirmed_executor":
        state["executor_state"] = "recovery_required"
        state["executor_failure_kind"] = (
            state.get("executor_failure_kind")
            if state.get("executor_failure_kind") in EXECUTOR_FAILURE_KINDS
            else "incomplete_execution"
        )
        state["executor_agent_id"] = None
        state["executor_observed_effective"] = False
        state["model_profile"] = "work_assessment"
    elif role == "high_assessor":
        state["assessor_state"] = "recovery_required"
        state["assessor_failure_kind"] = (
            state.get("assessor_failure_kind")
            if state.get("assessor_failure_kind")
            in {"model_unavailable", "start_mismatch", "assessment_result_invalid"}
            else "start_mismatch"
        )
        state["assessor_agent_id"] = None
        state["assessor_observed_effective"] = False
        state["model_profile"] = "work_assessment"


def writer_liveness_blocks_successor(state: dict[str, Any]) -> bool:
    return _safe_child_liveness(state.get("child_liveness")).get("status") in {
        "live", "unknown"
    }


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


def dispatch_receipt_path(payload: dict[str, Any]) -> Path | None:
    """Return an opaque, session-bound receipt path without retaining the session id."""
    if not persistence_enabled() or payload.get("session_id") in (None, ""):
        return None
    token = stable_hash("workflow-manager-dispatch-receipt-v1\0" + str(payload["session_id"]), 32)
    return data_root() / "dispatch-receipts" / f"{token}.json"


def record_dispatch_receipt(payload: dict[str, Any]) -> None:
    """Best-effort private receipt. It must never alter the Hook's fail-open behavior."""
    path = dispatch_receipt_path(payload)
    event = str(payload.get("hook_event_name") or "")
    if path is None or event not in STATE_EVENTS:
        return
    try:
        # Do not copy host input into the receipt.  These are deliberately one-way
        # identifiers: they permit the doctor to bind a dispatch to this release
        # without turning the private receipt into a second transcript.
        configured_kind = os.environ.get("WORKFLOW_MANAGER_RUNNER_KIND", "posix_direct")
        runner_kind = configured_kind if configured_kind in DISPATCH_RUNNER_KINDS else "posix_direct"
        plugin_root = os.environ.get("PLUGIN_ROOT") or str(Path(__file__).resolve().parents[1])
        source_file = Path(__file__).resolve()
        source_digest = stable_hash(source_file.read_bytes(), 32)
        root_fingerprint = stable_hash(str(Path(plugin_root).resolve(strict=True)), 32)
        stable = _stable_source_files(_stable_skill_source(Path(plugin_root)))
        skill_digest = stable[1][:32] if stable is not None else "unknown"
        run_value = payload.get("hook_run_id", payload.get("event_id", payload.get("id", "")))
        # Older hosts do not provide a run id.  This conservative fallback makes a
        # retry idempotent; it never records the session or any host identifier.
        run_fingerprint = stable_hash(
            "workflow-manager-hook-run-v1\0" + str(payload.get("session_id"))
            + "\0" + event + "\0" + str(run_value), 32
        )
        ensure_private_dir(path.parent)
        if path.exists() and (path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode)):
            return
        with state_lock(path):
            timeline: list[dict[str, str]] = []
            if path.exists():
                info = path.lstat()
                if info.st_size > MAX_DISPATCH_RECEIPT_BYTES or not stat.S_ISREG(info.st_mode):
                    return
                decoded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(decoded, dict) and decoded.get("schema") == DISPATCH_RECEIPT_SCHEMA:
                    candidate = decoded.get("timeline")
                    if isinstance(candidate, list):
                        for item in candidate[-MAX_DISPATCH_RECEIPT_EVENTS:]:
                            if (isinstance(item, dict) and item.get("event") in STATE_EVENTS
                                    and isinstance(item.get("at"), str) and isinstance(item.get("run"), str)):
                                timeline.append({"event": item["event"], "at": item["at"], "run": item["run"]})
            if not any(item["run"] == run_fingerprint for item in timeline):
                timeline.append({"event": event, "at": utc_now(), "run": run_fingerprint})
            timeline = timeline[-MAX_DISPATCH_RECEIPT_EVENTS:]
            atomic_write(path, {
                "schema": DISPATCH_RECEIPT_SCHEMA,
                "writer_version": WRITER_VERSION,
                "state_schema": SCHEMA_VERSION,
                "execution_profile": EXECUTION_PROFILE_VERSION,
                "stable_skill_schema": STABLE_SKILL_SCHEMA,
                "source": "hook",
                "runner_kind": runner_kind,
                "plugin_root_fingerprint": root_fingerprint,
                "source_fingerprint": source_digest,
                "stable_skill_fingerprint": skill_digest,
                "at": timeline[-1]["at"],
                "events": [item["event"] for item in timeline],
                "timeline": timeline,
                "event_count": len(timeline),
            })
    except Exception:
        return


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


@contextmanager
def state_lock(path: Path, timeout: float | None = None) -> Iterator[None]:
    """Serialize state changes without dropping normal events on contention.

    Core lifecycle events wait for the process-owned OS lock. Explicit cleanup
    and migration probes may still pass a finite or zero timeout because they
    are opportunistic and never authorize work.
    """
    lock_path = path.with_suffix(".lock")
    ensure_private_dir(lock_path.parent)
    handle = lock_path.open("a+b")
    try:
        lock_path.chmod(0o600)
    except OSError:
        pass
    acquired = False
    deadline = (
        time.monotonic() + max(timeout, 0.0)
        if timeout is not None
        else None
    )
    try:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(
                        handle.fileno(),
                        msvcrt.LK_LOCK if timeout is None else msvcrt.LK_NBLCK,
                        1,
                    )
                else:
                    import fcntl

                    fcntl.flock(
                        handle.fileno(),
                        fcntl.LOCK_EX
                        if timeout is None
                        else fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
                acquired = True
                break
            except OSError:
                if deadline is not None and time.monotonic() >= deadline:
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


def _stable_skill_file_digests(files: dict[str, bytes]) -> dict[str, str]:
    return {
        relative: hashlib.sha256(payload).hexdigest()
        for relative, payload in sorted(files.items())
    }


def _stable_skill_relative_path(target: Path, relative: str) -> Path | None:
    if not relative or "\\" in relative or relative.startswith("/"):
        return None
    parts = relative.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return None
    return target.joinpath(*parts)


def _prune_retired_stable_skill_files(
    target: Path,
    current_marker: dict[str, Any],
    files: dict[str, bytes],
) -> tuple[list[str], list[str]]:
    """Remove only byte-identical files previously shipped by this Skill."""
    expected: dict[str, set[str]] = {
        relative: set(digests)
        for relative, digests in RETIRED_STABLE_SKILL_FILE_DIGESTS.items()
        if relative not in files
    }
    marker_digests = current_marker.get("file_digests")
    if isinstance(marker_digests, dict):
        for relative, digest in marker_digests.items():
            if (
                relative not in files
                and isinstance(relative, str)
                and isinstance(digest, str)
                and re.fullmatch(r"[0-9a-f]{64}", digest)
            ):
                expected.setdefault(relative, set()).add(digest)

    removed: list[str] = []
    retained: list[str] = []
    removable_parents: set[Path] = set()
    for relative in sorted(expected):
        path = _stable_skill_relative_path(target, relative)
        if path is None:
            retained.append(relative)
            continue
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_size > 1024 * 1024
        ):
            retained.append(relative)
            continue
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            retained.append(relative)
            continue
        if digest not in expected[relative]:
            retained.append(relative)
            continue
        path.unlink()
        removed.append(relative)
        removable_parents.add(path.parent)

    for parent in sorted(removable_parents, key=lambda item: len(item.parts), reverse=True):
        if parent == target:
            continue
        try:
            info = parent.lstat()
            if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                parent.rmdir()
        except (FileNotFoundError, OSError):
            pass
    return removed, retained


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
    file_digests = _stable_skill_file_digests(files)
    marker_payload = {
        "schema": STABLE_SKILL_SCHEMA,
        "managed_by": STABLE_SKILL_NAME,
        "writer_version": WRITER_VERSION,
        "source_digest": digest,
        "files": sorted(files),
        "file_digests": file_digests,
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
                removed, retained = _prune_retired_stable_skill_files(
                    target, current_marker, files
                )
                if (
                    current_marker.get("source_digest") == digest
                    and current_marker.get("writer_version") == WRITER_VERSION
                    and current_marker.get("files") == sorted(files)
                    and current_marker.get("file_digests") == file_digests
                    and not removed
                ):
                    current_result = {**result, "status": "current", "digest": digest}
                    if retained:
                        current_result["retained_retired_files"] = retained
                    return current_result
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
                updated_result = {**result, "status": "updated", "digest": digest}
                if removed:
                    updated_result["removed_files"] = removed
                if retained:
                    updated_result["retained_retired_files"] = retained
                return updated_result

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


def _fingerprint32(value: Any) -> str | None:
    text = str(value or "")
    return text if re.fullmatch(r"[0-9a-f]{32}", text) else None



def _safe_stall(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        item = {}
    state_value = item.get("state") if item.get("state") in STALL_STATES else "none"
    result = {
        "state": state_value,
        "stall_id": _fingerprint32(item.get("stall_id")),
        "objective_fingerprint": safe_fingerprint(item.get("objective_fingerprint")) or None,
        "plan_digest": _fingerprint32(item.get("plan_digest")),
        "execution_contract_id": _fingerprint32(item.get("execution_contract_id")),
        "evidence_digest": _fingerprint32(item.get("evidence_digest")),
        "diagnosis_request_fingerprint": _fingerprint32(item.get("diagnosis_request_fingerprint")),
        "remediation_digest": _fingerprint32(item.get("remediation_digest")),
        "correction_digest": _fingerprint32(item.get("correction_digest")),
        "failure_kind": item.get("failure_kind") if item.get("failure_kind") in EXECUTOR_FAILURE_KINDS else None,
        "resume_profile": item.get("resume_profile") if item.get("resume_profile") in STALL_RESUME_PROFILES else None,
        "executor_attempt": safe_sequence(item.get("executor_attempt")),
        "diagnosis_attempt": safe_sequence(item.get("diagnosis_attempt")),
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


def _safe_continuation_lease(item: Any) -> dict[str, Any]:
    item = item if isinstance(item, dict) else {}
    phase = item.get("phase") if item.get("phase") in {"none", "pending", "emitted", "consumed"} else "none"
    key = _fingerprint32(item.get("key"))
    reason_digest = _fingerprint32(item.get("reason_digest"))
    if phase == "none" or not key or not reason_digest:
        return {
            "phase": "none", "key": None, "reason_digest": None,
            "epoch_id": None, "contract_id": None, "ack_source": None,
            "ack_digest": None, "at": None,
        }
    return {
        "phase": phase, "key": key, "reason_digest": reason_digest,
        "epoch_id": safe_fingerprint(item.get("epoch_id")) or None,
        "contract_id": _fingerprint32(item.get("contract_id")),
        "ack_source": (
            item.get("ack_source")
            if item.get("ack_source") in {"host_posttool", "root_visible"}
            else None
        ),
        "ack_digest": _fingerprint32(item.get("ack_digest")),
        "at": str(item.get("at") or "")[:40] or None,
    }


def continuation_lease_key(
    payload: dict[str, Any], reason: str, epoch_id: Any = None,
    contract_id: Any = None,
) -> str:
    """Stable idempotency key for a host continuation request.

    A hook run is only a delivery attempt, never part of the logical outbox
    identity.  This deliberately binds the current epoch and contract so a
    delayed receipt cannot consume a successor's continuation.
    """
    return stable_hash(
        "continuation-lease-v3\0%s\0%s\0%s" % (
            safe_fingerprint(epoch_id) or "legacy",
            # ``payload`` is only retained as a call-shape compatibility
            # parameter.  A hook event never supplies the contract component;
            # production claimants pass the locked state value explicitly.
            _fingerprint32(contract_id) or "unbound",
            stable_hash(reason, 32),
        ), 32,
    )


def claim_continuation_lease(state: dict[str, Any], payload: dict[str, Any], reason: str) -> dict[str, Any]:
    """Atomically claim/recover one continuation emission.

    An emitted-but-unconsumed lease is deliberately replayed with the *same*
    key.  That covers a SIGKILL between stdout and the final consume write
    without allowing the host to create a second logical continuation.
    """
    epoch_id = _safe_task_epoch(state.get("task_epoch")).get("id")
    # The external payload is untrusted input.  The contract component comes
    # only from the state that owns this locked mutation boundary.
    contract_id = _fingerprint32(state.get("execution_contract_id"))
    key = continuation_lease_key(payload, reason, epoch_id, contract_id)
    digest = stable_hash(reason, 32)
    lease = _safe_continuation_lease(state.get("continuation_lease"))
    if (
        lease["key"] != key
        or lease["reason_digest"] != digest
        or lease.get("epoch_id") != epoch_id
        or lease.get("contract_id") != contract_id
    ):
        lease = {
            "phase": "pending", "key": key, "reason_digest": digest,
            "epoch_id": epoch_id, "contract_id": contract_id,
            "ack_source": None, "ack_digest": None, "at": utc_now(),
        }
    if lease["phase"] == "consumed":
        state["continuation_lease"] = lease
        return {**lease, "emit": False}
    # The state write enclosing this function is the atomic ownership claim.
    lease["phase"] = "emitted"
    lease["at"] = utc_now()
    state["continuation_lease"] = lease
    return {**lease, "emit": True}


def continuation_lease_is_current(state: dict[str, Any], lease: dict[str, Any]) -> bool:
    """A delayed acknowledgement may never cross an epoch or contract."""
    return bool(
        lease.get("epoch_id") == current_task_epoch_id(state)
        and lease.get("contract_id") == _fingerprint32(state.get("execution_contract_id"))
    )


def consume_continuation_lease(
    state: dict[str, Any], key: str, *, source: str | None = None,
    receipt: Any = None,
) -> bool:
    """Consume one outbox item only at a trusted host acknowledgement edge.

    Rendering a Stop response is intentionally absent from this function's
    authority model.  Production callers must provide either a structured
    PostTool receipt or a root-visible echo; direct unit helpers cannot turn
    stdout into an acknowledgement by omitting ``source``.
    """
    lease = _safe_continuation_lease(state.get("continuation_lease"))
    if (
        source in {"host_posttool", "root_visible"}
        and _fingerprint32(key)
        and lease["key"] == key
        and lease["phase"] == "emitted"
        and continuation_lease_is_current(state, lease)
    ):
        lease["phase"] = "consumed"
        lease["ack_source"] = source
        lease["ack_digest"] = stable_hash(
            "workflow-manager-continuation-ack-v1\0"
            + source + "\0" + canonical_json(receipt),
            32,
        )
        lease["at"] = utc_now()
        state["continuation_lease"] = lease
        return True
    return False


def _strict_continuation_ack_record(value: Any) -> str | None:
    """Read only an explicit structured host acknowledgement, never text."""
    if not isinstance(value, dict) or len(value) > 16:
        return None
    key_fields = [key for key in value if normalized_key(key) in {"continuationkey", "continuationleasekey"}]
    accepted_fields = [key for key in value if normalized_key(key) in {"hostaccepted", "accepted", "acknowledged"}]
    if len(key_fields) == 1 and len(accepted_fields) == 1:
        key = _fingerprint32(value.get(key_fields[0]))
        if key and value.get(accepted_fields[0]) is True:
            return key
    return None


def trusted_posttool_continuation_ack(
    response: Any, depth: int = 0, in_receipt: bool = False,
) -> str | None:
    """Return one unique host receipt key without parsing CLI/MCP text output."""
    if depth > 4 or not isinstance(response, dict) or len(response) > 32:
        return None
    # A bare result object is not a receipt: arbitrary tools may return
    # JSON-shaped values.  Only an explicit host receipt envelope reaches the
    # strict key/accepted pair below.
    direct = _strict_continuation_ack_record(response) if in_receipt else None
    candidates = [direct] if direct else []
    # These containers are host protocol envelopes.  Deliberately exclude
    # output/content/text/stdout/stderr: an arbitrary command's printed JSON
    # is not a host acceptance receipt.
    wrappers = {
        "continuationreceipt", "receipt", "structuredcontent", "toolresponse",
        "response", "result",
    }
    for raw_key, nested in response.items():
        wrapper = normalized_key(raw_key)
        if wrapper not in wrappers:
            continue
        nested_key = trusted_posttool_continuation_ack(
            nested, depth + 1,
            in_receipt=in_receipt or wrapper in {"continuationreceipt", "receipt"},
        )
        if nested_key:
            candidates.append(nested_key)
    unique = {item for item in candidates if item}
    return next(iter(unique)) if len(unique) == 1 else None


CONTINUATION_ROOT_KEY_RE = re.compile(
    r"\s*continuation_key=([0-9a-f]{32})\s*\Z"
)


def payload_claims_child_identity(payload: dict[str, Any]) -> bool:
    """A continuation acknowledgement is root-only, never child-attributed."""
    return any(
        payload.get(field) not in (None, "")
        for field in (
            "agent_id", "subagent_id", "agent_name", "parent_agent_id", "role",
        )
    )


def root_visible_continuation_ack(payload: dict[str, Any]) -> str | None:
    """A user/root prompt may echo one exact outbox key; child prose may not."""
    if payload.get("hook_event_name") != "UserPromptSubmit":
        return None
    if payload_claims_child_identity(payload):
        return None
    raw = payload.get("prompt")
    if not isinstance(raw, str) or len(raw.encode("utf-8", errors="replace")) > 4096:
        return None
    match = CONTINUATION_ROOT_KEY_RE.fullmatch(raw)
    return match.group(1) if match else None


def new_state(payload: dict[str, Any]) -> dict[str, Any]:
    now = utc_now()
    return {
        "schema_version": SCHEMA_VERSION,
        "writer_version": WRITER_VERSION,
        "session_fingerprint": stable_hash(payload.get("session_id") or payload.get("hook_run_id")),
        "cwd_fingerprint": stable_hash(payload.get("cwd")),
        # These are immutable root-task facts.  Later child/resume events may
        # observe a different cwd or rollout, but can never retarget recovery.
        "root_session_fingerprint": stable_hash(payload.get("session_id") or payload.get("hook_run_id")),
        "root_cwd_fingerprint": stable_hash(payload.get("cwd")),
        "root_rollout_identity": None,
        # Epoch 0 is intentionally unbound until UserPromptSubmit supplies an
        # objective.  Schema-31 migration keeps its legacy journal untouched.
        "task_epoch": {"id": None, "sequence": 0, "status": "none", "objective_fingerprint": None},
        "archived_epochs": [],
        "isolated_lifecycles": [],
        "child_liveness": _safe_child_liveness(None),
        "parent_writer_lease": _safe_parent_writer_lease(None),
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
        "assessment_liveness": _empty_assessment_liveness(),
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
        "authorization_scope": _safe_authorization_scope(None),
        "authorization_envelope": _safe_authorization_envelope(None),
        "pending_confirmation_receipt": None,
        "recovery_chain": [],
        "pending_recovery_facts": None,
        "pending_recovery_reservation": None,
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
            "causal_type": None,
            "evidence_digest": None,
        },
        "causal_lineage": _safe_causal_lineage(None),
        "pending_causal_revision": {},
        "plan_composition": _safe_plan_composition(None),
        "lifecycle_diagnostics": [],
        "stall": _safe_stall(None),
        "continuation_lease": _safe_continuation_lease(None),
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
        # A bounded, fingerprint-only freshness boundary.  Requests and host
        # Start echoes deliberately remain separate: a request is never proof
        # that the host applied it.
        "change_epoch": 0,
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
    return decorate_route(
        {
            "at": item.get("at"),
            "turn_id": safe_label(item.get("turn_id"), 120)
            if item.get("turn_id")
            else None,
            "prompt_meta": {
                "fingerprint": safe_label(prompt_meta.get("fingerprint"), 64),
                "length": max(safe_int(prompt_meta.get("length")), 0),
            },
            "task_domain": item.get("task_domain"),
            "domain_confidence": item.get("domain_confidence"),
            "domain_rule_codes": as_list(item.get("domain_rule_codes")),
            "model_profile": item.get("model_profile"),
            "domain_classifier_version": item.get("domain_classifier_version"),
            "domain_decision_id": item.get("domain_decision_id"),
            "work_difficulty": item.get("work_difficulty"),
            "difficulty_confidence": item.get("difficulty_confidence"),
            "difficulty_rule_codes": as_list(item.get("difficulty_rule_codes")),
            "difficulty_classifier_version": item.get("difficulty_classifier_version"),
            "difficulty_decision_id": item.get("difficulty_decision_id"),
            "phase_hints": as_list(item.get("phase_hints")),
            "route_source": item.get("route_source") or "authorization_classifier",
        }
    )
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
        "host_command_digest": safe_fingerprint(item.get("host_command_digest")) or None,
        "legacy_host_input_digest": safe_fingerprint(item.get("legacy_host_input_digest")) or None,
        "reconciliation_source": item.get("reconciliation_source") if item.get("reconciliation_source") in {"legacy_unique_turn_patch_event_v1", "host_rollout_exact_patch_digest_v1", "host_rollout_exact_patch_receipt_v2", "host_posttool_patch_receipt_v2", "host_rollout_exact_command_text_v1", "host_rollout_exact_completed_file_change_v1"} else None,
        "host_receipt_digest": _fingerprint32(item.get("host_receipt_digest")),
        "tool": safe_label(item.get("tool"), 120),
        "fingerprint": fingerprint[:64],
        "status": status_value,
        "envelope_status": safe_label(item.get("envelope_status"), 32).lower()
        if item.get("envelope_status") else None,
        "leaf_status": safe_label(item.get("leaf_status"), 32).lower()
        if item.get("leaf_status") else None,
        "category": safe_label(item.get("category"), 32) if item.get("category") else "other",
        "plan_digest": plan_digest,
        "execution_contract_id": contract_id,
        "epoch_id": safe_fingerprint(item.get("epoch_id")) or None,
        "slice_id": safe_slice_id(item.get("slice_id")),
        "slice_contract_id": _fingerprint32(item.get("slice_contract_id")),
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
        "event": (
            item.get("event")
            if item.get("event") in {"request", "start", *TERMINAL_SUBAGENT_EVENTS}
            else "unknown"
        ),
        "turn_id": safe_label(item.get("turn_id"), 120) if item.get("turn_id") else None,
        "epoch_id": safe_fingerprint(item.get("epoch_id")) or None,
        "lifecycle_fingerprint": _fingerprint32(item.get("lifecycle_fingerprint")),
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
        "host_acceptance_fingerprint": safe_fingerprint(item.get("host_acceptance_fingerprint")) or None,
        "host_acceptance_receipt_digest": _fingerprint32(
            item.get("host_acceptance_receipt_digest")
        ),
        "host_acceptance_conflict": bool(item.get("host_acceptance_conflict")),
        "start_observed": item.get("start_observed") if item.get("start_observed") in {"full", "partial", "absent", "mismatch"} else None,
        "observation_source": safe_label(item.get("observation_source"), 80) if item.get("observation_source") else None,
        "scope_fingerprint": safe_label(item.get("scope_fingerprint"), 64) if item.get("scope_fingerprint") else None,
        "request_fingerprint": request_fingerprint or None,
        "objective_fingerprint": objective_fingerprint or None,
        "stale": bool(item.get("stale")),
        "request_gate": item.get("request_gate")
        if item.get("request_gate") in {"audit", "open", "contract"}
        else None,
        "request_visibility": item.get("request_visibility")
        if item.get("request_visibility") in {"plaintext", "opaque_v2"}
        else None,
        "request_cap": min(max(safe_int(item.get("request_cap")), 0), 3),
        "reaudited": bool(item.get("reaudited")),
        "role": role,
        "contract_id": contract_id,
        "slice_id": safe_slice_id(item.get("slice_id")),
        "slice_contract_id": _fingerprint32(item.get("slice_contract_id")),
        "model": safe_label(item.get("model"), 80) if item.get("model") else None,
        "reasoning_effort": (
            safe_label(item.get("reasoning_effort"), 24)
            if item.get("reasoning_effort")
            else None
        ),
        "fork_turns": fork_turns or None,
        "attempt": safe_sequence(item.get("attempt")),
        "recovery_from": (
            item.get("recovery_from")
            if item.get("recovery_from") in EXECUTOR_FAILURE_KINDS
            else None
        ),
        "plan_handoff_digest": safe_fingerprint(item.get("plan_handoff_digest")) or None,
        "plan_handoff_delivery_digest": _fingerprint32(
            item.get("plan_handoff_delivery_digest")
        ),
        "plan_handoff_delivered": bool(item.get("plan_handoff_delivered")),
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
        "terminal_lifecycle_source": (
            item.get("terminal_lifecycle_source")
            if item.get("terminal_lifecycle_source") == "mailbox_completed"
            else None
        ),
        "reported_failure_kind": (
            item.get("reported_failure_kind")
            if item.get("reported_failure_kind") in EXECUTOR_FAILURE_KINDS
            else None
        ),
        "reported_failure_fingerprint": _fingerprint32(
            item.get("reported_failure_fingerprint")
        ),
        "reported_evidence_digest": _fingerprint32(
            item.get("reported_evidence_digest")
        ),
    }
    if isinstance(result_meta, dict):
        value["result_meta"] = {
            "fingerprint": safe_label(result_meta.get("fingerprint"), 64),
            "length": max(safe_int(result_meta.get("length")), 0),
        }
    if value["event"] == "mailbox_terminal" and not (
        value["role"] == "confirmed_executor"
        and value["terminal_lifecycle_source"] == "mailbox_completed"
        and value["status"] == "completed"
        and value["terminal_status"] == "completed"
        and value["terminal_status_source"] == "host_declared_success"
        and value["execution_result_contract_match"] is True
        and value["execution_result_outcome"] in {"succeeded", "failed"}
        and _fingerprint32(value["execution_result_evidence_digest"])
        and value["evidence_digest_profile"] == EVIDENCE_DIGEST_PROFILE
        and value["evidence_digest_source"] == EVIDENCE_DIGEST_SOURCE
        and value.get("result_meta", {}).get("fingerprint")
        and value["agent_id"]
        and value["task_name"]
        and value["request_fingerprint"]
        and value["objective_fingerprint"]
        and value["contract_id"]
        and value["slice_id"]
        and value["slice_contract_id"]
        and value["attempt"] > 0
    ):
        return None
    return value


def subagent_lifecycle_groups(value: Any) -> list[dict[str, Any]]:
    records = value.get("subagents", []) if isinstance(value, dict) else as_list(value)
    groups: list[dict[str, Any]] = []
    # A process id may recur after an epoch change.  Keep the grouping key
    # generation-scoped so a delayed terminal record cannot attach itself to a
    # successor merely because the host reused an agent string.
    live_by_agent: dict[tuple[str, str | None], dict[str, Any]] = {}
    terminal_by_agent: dict[tuple[str, str | None], dict[str, Any]] = {}

    def new_group(index: int, item: dict[str, Any], state_value: str) -> dict[str, Any]:
        group = {
            "state": state_value,
            "agent_id": str(item.get("agent_id") or "") or None,
            "request": item if item.get("event") == "request" else None,
            "start": item if item.get("event") == "start" else None,
            "stop": item if item.get("event") in TERMINAL_SUBAGENT_EVENTS else None,
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
            group = new_group(
                index,
                item,
                "isolated" if item.get("status") == "isolated_incomplete"
                else "result_pending" if item.get("agent_id") else "pending",
            )
            if group["state"] == "result_pending":
                group["agent_id"] = str(item.get("agent_id"))
            continue
        agent_id = str(item.get("agent_id") or "")
        if not agent_id:
            continue
        identity = (agent_id, safe_fingerprint(item.get("epoch_id")) or None)
        if event == "start":
            if identity in live_by_agent:
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
                    and safe_fingerprint((group.get("request") or {}).get("epoch_id"))
                    == identity[1]
                ),
                None,
            )
            prior_terminal = terminal_by_agent.get(identity)
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
            live_by_agent[identity] = group
            continue
        if event in TERMINAL_SUBAGENT_EVENTS:
            group = live_by_agent.pop(identity, None)
            if group is None:
                group = next(
                    (
                        candidate
                        for candidate in reversed(groups)
                        if candidate.get("state") == "result_pending"
                        and candidate.get("agent_id") == agent_id
                        and safe_fingerprint(
                            (candidate.get("request") or {}).get("epoch_id")
                        ) == identity[1]
                    ),
                    None,
                )
            if group is None:
                if identity in terminal_by_agent:
                    continue
                group = new_group(index, item, "terminal")
            else:
                group["records"].append((index, item))
                group["stop"] = item
                group["last_index"] = index
                group["state"] = "terminal"
            group["agent_id"] = agent_id
            terminal_by_agent[identity] = group
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
            "epoch_id": current_task_epoch_id(state),
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
        "causal_type": (
            LEGACY_CAUSAL_OUTCOME_MAP.get(str(item.get("causal_type") or ""), str(item.get("causal_type") or ""))
            if LEGACY_CAUSAL_OUTCOME_MAP.get(str(item.get("causal_type") or ""), str(item.get("causal_type") or "")) in CAUSAL_TYPES
            else LEGACY_CAUSAL_OUTCOME_MAP.get(str(outcome or ""), str(outcome or ""))
            if LEGACY_CAUSAL_OUTCOME_MAP.get(str(outcome or ""), str(outcome or "")) in CAUSAL_TYPES
            else None
        ),
        "evidence_digest": safe_fingerprint(item.get("evidence_digest")) or None,
    }


EXECUTABLE_CAUSAL_TYPES = CAUSAL_TYPES - frozenset({
    "uncertain", "explanatory_conclusion", "unrelated_new_objective",
})


def _safe_causal_lineage(item: Any) -> dict[str, Any]:
    item = item if isinstance(item, dict) else {}
    causal_type = str(item.get("current_causal_type") or "")
    return {
        "root_objective_fingerprint": safe_fingerprint(
            item.get("root_objective_fingerprint")
        ) or None,
        "selected_revision_digest": _fingerprint32(
            item.get("selected_revision_digest")
        ),
        "selected_contract_id": _fingerprint32(item.get("selected_contract_id")),
        "selected_prefix_digest": _fingerprint32(
            item.get("selected_prefix_digest")
        ),
        "terminal_baseline_id": _fingerprint32(item.get("terminal_baseline_id")),
        "terminal_seal_digest": _fingerprint32(item.get("terminal_seal_digest")),
        "current_issue_fingerprint": safe_fingerprint(
            item.get("current_issue_fingerprint")
        ) or None,
        "current_causal_type": causal_type if causal_type in CAUSAL_TYPES else None,
        "tail_record_type": (
            item.get("tail_record_type")
            if item.get("tail_record_type") in CAUSAL_RECORD_TYPES else None
        ),
        "tail_record_digest": _fingerprint32(item.get("tail_record_digest")),
    }


def _safe_pending_causal_revision(item: Any) -> dict[str, Any]:
    item = item if isinstance(item, dict) else {}
    causal_type = str(item.get("causal_type") or "")
    creation_state = str(item.get("creation_state") or "")
    result = {
        "causal_type": causal_type if causal_type in EXECUTABLE_CAUSAL_TYPES else None,
        "creation_state": (
            creation_state
            if creation_state in {"assessment_required", "plan_composition"}
            else None
        ),
        "parent_revision_digest": _fingerprint32(
            item.get("parent_revision_digest")
        ),
        "parent_contract_id": _fingerprint32(item.get("parent_contract_id")),
        "parent_prefix_digest": _fingerprint32(item.get("parent_prefix_digest")),
        "terminal_baseline_id": _fingerprint32(item.get("terminal_baseline_id")),
        "root_objective_fingerprint": safe_fingerprint(
            item.get("root_objective_fingerprint")
        ) or None,
        "issue_fingerprint": safe_fingerprint(item.get("issue_fingerprint")) or None,
        "evidence_digest": _fingerprint32(item.get("evidence_digest")),
        "change_set_digest": _fingerprint32(item.get("change_set_digest")),
        "authorization_envelope_digest": _fingerprint32(
            item.get("authorization_envelope_digest")
        ),
        "strict_confirm_receipt": _fingerprint32(
            item.get("strict_confirm_receipt")
        ),
        "acceptance_digest": _fingerprint32(item.get("acceptance_digest")),
        "risk_category_digest": _fingerprint32(item.get("risk_category_digest")),
        "irreversible_action_digest": _fingerprint32(
            item.get("irreversible_action_digest")
        ),
    }
    required = (
        result["causal_type"], result["creation_state"],
        result["parent_revision_digest"], result["parent_contract_id"],
        result["parent_prefix_digest"], result["terminal_baseline_id"],
        result["root_objective_fingerprint"], result["issue_fingerprint"],
        result["authorization_envelope_digest"], result["strict_confirm_receipt"],
    )
    if not all(required):
        return {}
    if result["causal_type"] == "introduced_regression" and not result["change_set_digest"]:
        return {}
    return result


def _safe_plan_composition(item: Any) -> dict[str, Any]:
    item = item if isinstance(item, dict) else {}
    status = item.get("status") if item.get("status") in {"none", "pending"} else "none"
    binding = _fingerprint32(item.get("assessor_binding_id"))
    objective = safe_fingerprint(item.get("objective_fingerprint")) or None
    receipt = _fingerprint32(item.get("assessment_receipt"))
    if status != "pending" or not (binding and objective and receipt):
        return {
            "status": "none", "assessor_binding_id": None,
            "objective_fingerprint": None, "assessment_receipt": None,
            "turn_id": None,
        }
    return {
        "status": "pending",
        "assessor_binding_id": binding,
        "objective_fingerprint": objective,
        "assessment_receipt": receipt,
        "turn_id": safe_label(item.get("turn_id"), 120) if item.get("turn_id") else None,
    }


def _safe_lifecycle_diagnostic(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict) or item.get("code") not in LIFECYCLE_DIAGNOSTIC_CODES:
        return None
    level = item.get("level") if item.get("level") in LIFECYCLE_DIAGNOSTIC_LEVELS else "error"
    return {
        "code": item["code"],
        "level": level,
        "role": item.get("role") if item.get("role") in {"lane", "high_assessor", "confirmed_executor"} else "lane",
        "request_fingerprint": safe_fingerprint(item.get("request_fingerprint")) or None,
        "agent_fingerprint": _fingerprint32(item.get("agent_fingerprint")),
        "contract_id": _fingerprint32(item.get("contract_id")),
        "at": str(item.get("at") or "")[:40] or None,
    }


def record_lifecycle_diagnostic(
    state: dict[str, Any], code: str, *, level: str, role: str = "lane",
    request_fingerprint: Any = None, agent_id: Any = None, contract_id: Any = None,
) -> None:
    if code not in LIFECYCLE_DIAGNOSTIC_CODES:
        return
    item = _safe_lifecycle_diagnostic({
        "code": code,
        "level": level,
        "role": role,
        "request_fingerprint": request_fingerprint,
        "agent_fingerprint": stable_hash(str(agent_id), 32) if agent_id else None,
        "contract_id": contract_id,
        "at": utc_now(),
    })
    if item is None:
        return
    diagnostics = [
        value for raw in as_list(state.get("lifecycle_diagnostics"))
        if (value := _safe_lifecycle_diagnostic(raw)) is not None
    ]
    if diagnostics and canonical_json(diagnostics[-1]) == canonical_json(item):
        return
    state["lifecycle_diagnostics"] = [*diagnostics, item][-MAX_LIFECYCLE_DIAGNOSTICS:]


def active_hard_lifecycle(state: dict[str, Any]) -> bool:
    return bool(
        state.get("task_domain") == "work"
        and state.get("work_difficulty") == "hard"
        and (
            state.get("plan_state") not in {None, "none"}
            or state.get("assessor_state") not in {None, "none"}
            or state.get("executor_state") not in {None, "none", "succeeded"}
            or _safe_causal_review(state.get("causal_review")).get("state")
            in {"triage_required", "triaging"}
        )
    )


def expected_bound_role(state: dict[str, Any]) -> str | None:
    assessor_due = state.get("assessor_state") in {
        "spawn_required", "spawn_pending", "running", "recovery_required",
    }
    executor_due = (
        state.get("plan_state") == "confirmed"
        and state.get("executor_state") in {
            "spawn_required", "spawn_pending", "running", "recovery_required",
            "verification_required",
        }
    )
    if assessor_due == executor_due:
        return None
    return "high_assessor" if assessor_due else "confirmed_executor"


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
        "journal_prefix_digest": None,
        "journal_prefix_bytes": 0,
        "generation": 0,
        "revision_count": 0,
        "lifecycle_status": "none",
        "write_status": "none",
        "warning_code": "none",
        "diagnostic": None,
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
    plan_digest = safe_fingerprint(item.get("plan_digest")) or None
    content_digest = safe_fingerprint(item.get("content_digest")) or None
    current_revision_digest = (
        safe_fingerprint(item.get("current_revision_digest"))
        or (content_digest if canonical_match else None)
        or (plan_digest if canonical_match else None)
    )
    result.update(
        {
            "relative_path": relative if canonical_match else None,
            "format_version": 2 if canonical_match else 0,
            "objective_fingerprint": safe_fingerprint(item.get("objective_fingerprint")) or None,
            "difficulty_decision_id": safe_fingerprint(item.get("difficulty_decision_id")) or None,
            "plan_digest": plan_digest,
            "content_digest": content_digest,
            "current_revision_digest": current_revision_digest,
            "journal_digest": safe_fingerprint(item.get("journal_digest")) or None,
            "journal_prefix_digest": safe_fingerprint(item.get("journal_prefix_digest")) or None,
            "journal_prefix_bytes": max(safe_int(item.get("journal_prefix_bytes")), 0),
            "generation": max(safe_int(item.get("generation")), 0),
            "revision_count": max(safe_int(item.get("revision_count")), 0),
            "lifecycle_status": item.get("lifecycle_status") if item.get("lifecycle_status") in PLAN_ARTIFACT_LIFECYCLE_STATUSES else "none",
            "write_status": item.get("write_status") if item.get("write_status") in PLAN_ARTIFACT_WRITE_STATUSES else "none",
            "warning_code": item.get("warning_code") if item.get("warning_code") in PLAN_ARTIFACT_WARNING_CODES else "none",
            "diagnostic": _safe_plan_artifact_diagnostic(item.get("diagnostic")),
            "created_at": str(item.get("created_at"))[:40] if item.get("created_at") else None,
            "updated_at": str(item.get("updated_at"))[:40] if item.get("updated_at") else None,
        }
    )
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
        r"^\s*(?:WORK_ASSESSMENT|EXECUTION_STALL|"
        r"STALL_DIAGNOSIS|CAUSAL_REVIEW)\b",
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
    body = normalize_execution_slice_manifest_fence(body)
    body = body.rstrip() + "\n"
    if len(body.encode("utf-8")) > MAX_PLAN_REVISION_BYTES:
        raise PlanArtifactError("revision_too_large")
    return body


def canonical_plan_message_ready(value: Any) -> bool:
    """Accept a bounded native parent plan without a plugin prose protocol."""
    try:
        body = sanitize_plan_artifact_body(value)
    except PlanArtifactError:
        return False
    # Content quality belongs to the parent model and the user's confirmation.
    # The Hook only needs a nonempty bounded revision; an arbitrary prose-length
    # threshold would be a second planning gate with no host-trust value.
    return bool(body.strip())


def native_assessor_result_digest(value: Any) -> str | None:
    """Bind a current-model read-only assessment without a plugin prose DSL.

    The assessor result is evidence for the parent, never execution authority.
    The host-bound assessor lifecycle and terminal status establish provenance;
    the model, not keyword matching, judges the assessment content. The parent
    still owns the only plan artifact that can unlock an executor.
    """
    source = str(value or "").strip()
    size = len(source.encode("utf-8"))
    if not 0 < size <= MAX_PLAN_REVISION_BYTES:
        return None
    if re.search(r"(?i)\bWORK_ASSESSMENT\b", source):
        return None
    if EXECUTION_SLICES_FENCE_INTENT_RE.search(source):
        return None
    return stable_hash("workflow-manager-native-assessor-result-v1\0" + source, 32)


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
PLAN_JOURNAL_V3_RECORD_RE = re.compile(
    rb"<!-- workflow-manager-plan-record:v3\n"
    rb"record_type: (executable_revision|terminal_seal|durable_conclusion)\n"
    rb"record_digest: ([0-9a-f]{32})\n"
    rb"record_bytes: ([0-9]+)\n"
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
    records: list[dict[str, Any]] = []
    previous_generation = 0
    first_tail_position: int | None = None
    selected_prefix_end: int | None = None
    selected_record_digest: str | None = None
    selected_revision_digest: str | None = None
    pending_revision_after_tail: str | None = None
    while position < len(document):
        revision_header = PLAN_REVISION_HEADER_RE.match(document, position)
        if revision_header is None:
            record_header = PLAN_JOURNAL_V3_RECORD_RE.match(document, position)
            if record_header is None:
                raise PlanArtifactError("content_drift")
            if first_tail_position is None:
                first_tail_position = position
            record_type = record_header.group(1).decode("ascii")
            record_digest = record_header.group(2).decode("ascii")
            record_bytes = safe_int(record_header.group(3).decode("ascii"))
            record_start = record_header.end()
            record_end = record_start + record_bytes
            record_body = document[record_start:record_end]
            if (record_bytes <= 0 or record_end > len(document)
                    or not record_body.endswith(b"\n")
                    or stable_hash(record_body, 32) != record_digest):
                raise PlanArtifactError("content_drift")
            try:
                decoded = json.loads(record_body)
            except (UnicodeError, json.JSONDecodeError) as error:
                raise PlanArtifactError("content_drift") from error
            if not isinstance(decoded, dict) or decoded.get("record_type") != record_type:
                raise PlanArtifactError("content_drift")
            # Typed tails intentionally never contain transcript/prompt/result prose.
            if any(key in decoded for key in ("prompt", "response", "transcript", "message")):
                raise PlanArtifactError("content_drift")
            if record_type == "executable_revision":
                executable_digest = _fingerprint32(
                    decoded.get("executable_revision_digest")
                )
                if (
                    not pending_revision_after_tail
                    or executable_digest != pending_revision_after_tail
                    or executable_digest
                    != (revisions[-1].get("revision_digest") if revisions else None)
                ):
                    raise PlanArtifactError("content_drift")
                selected_prefix_end = record_end
                selected_record_digest = record_digest
                selected_revision_digest = executable_digest
                pending_revision_after_tail = None
            elif pending_revision_after_tail:
                # A causal plan revision and its typed selector are one logical
                # append.  No terminal/evidence record may observe it halfway.
                raise PlanArtifactError("content_drift")
            records.append({
                "record_type": record_type,
                "record_digest": record_digest,
                "record_start": position,
                "record_end": record_end,
                "data": decoded,
            })
            position = record_end
            continue
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
        if first_tail_position is not None:
            # A typed tail seals the preceding executable prefix.  More than
            # one ordinary root may subsequently be appended in the same
            # journal; the newest complete root is authoritative.  Only a
            # typed selector is atomic with the revision immediately before
            # it, so do not mistake consecutive roots for a broken pair.
            pending_revision_after_tail = revision_digest
        position = body_end
    if pending_revision_after_tail is not None:
        # A plain later revision is a new root objective in the same private
        # session journal, not a causal successor.  It receives a fresh
        # immutable prefix but no parent link or inherited confirmation.
        selected_prefix_end = len(document)
        selected_record_digest = None
        selected_revision_digest = pending_revision_after_tail
    if not revisions:
        raise PlanArtifactError("content_drift")
    current = revisions[-1]
    immutable_prefix_end = (
        selected_prefix_end
        if selected_prefix_end is not None
        else first_tail_position
        if first_tail_position is not None
        else len(document)
    )
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
        "journal_prefix_digest": stable_hash(document[:immutable_prefix_end], 32),
        "journal_prefix_bytes": immutable_prefix_end,
        "records": records,
        "selected_executable_record_digest": selected_record_digest,
        "selected_executable_revision_digest": (
            selected_revision_digest or current["revision_digest"]
        ),
        "journal_format_version": 3 if records else 2,
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
    causal_record: dict[str, Any] | None = None,
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
    if causal_record is not None:
        bound_causal_record = dict(causal_record)
        bound_causal_record["executable_revision_digest"] = revision_digest
        document += _encoded_plan_journal_record(
            "executable_revision", bound_causal_record
        )
    if len(document) > MAX_PLAN_JOURNAL_BYTES:
        raise PlanArtifactError("journal_full")
    parsed = parse_plan_journal(document, expected_session=session)
    if parsed["generation"] != generation or parsed["current_revision_digest"] != revision_digest:
        raise PlanArtifactError("content_drift")
    return document, parsed


def _encoded_plan_journal_record(record_type: str, data: dict[str, Any]) -> bytes:
    if record_type not in CAUSAL_RECORD_TYPES or not isinstance(data, dict):
        raise PlanArtifactError("content_drift")
    if any(key in data for key in ("prompt", "response", "transcript", "message")):
        raise PlanArtifactError("content_drift")
    canonical = dict(data)
    canonical["record_type"] = record_type
    if record_type == "executable_revision":
        causal = canonical.get("causal_type")
        if causal not in CAUSAL_TYPES:
            raise PlanArtifactError("content_drift")
        required = (
            _fingerprint32(canonical.get("executable_revision_digest")),
            _fingerprint32(canonical.get("parent_revision_digest")),
            _fingerprint32(canonical.get("parent_contract_id")),
            _fingerprint32(canonical.get("parent_prefix_digest")),
            _fingerprint32(canonical.get("terminal_baseline_id")),
            safe_fingerprint(canonical.get("root_objective_fingerprint")),
            safe_fingerprint(canonical.get("issue_fingerprint")),
            _fingerprint32(canonical.get("authorization_envelope_digest")),
            _fingerprint32(canonical.get("strict_confirm_receipt")),
        )
        if not all(required) or canonical.get("creation_state") != "executable":
            raise PlanArtifactError("content_drift")
        if causal == "introduced_regression" and not safe_fingerprint(canonical.get("change_set_digest")):
            raise PlanArtifactError("content_drift")
        if causal == "uncertain" and canonical.get("creation_state") not in {"read_only", "triage"}:
            raise PlanArtifactError("content_drift")
    body = (canonical_json(canonical) + "\n").encode("utf-8")
    digest = stable_hash(body, 32)
    return (
        f"{PLAN_JOURNAL_V3_RECORD_OWNER}\n"
        f"record_type: {record_type}\n"
        f"record_digest: {digest}\n"
        f"record_bytes: {len(body)}\n"
        "-->\n"
    ).encode("utf-8") + body


def append_plan_journal_record(existing: bytes, *, record_type: str, data: dict[str, Any]) -> tuple[bytes, dict[str, Any]]:
    """Append an immutable v3 causal record without rewriting prior bytes.

    This is deliberately separate from plan composition.  A terminal seal or
    durable conclusion is evidence, never a new execution authority.
    """
    record = _encoded_plan_journal_record(record_type, data)
    record_header = PLAN_JOURNAL_V3_RECORD_RE.match(record)
    expected_digest = (
        record_header.group(2).decode("ascii") if record_header is not None else None
    )
    document = existing + record
    if len(document) > MAX_PLAN_JOURNAL_BYTES:
        raise PlanArtifactError("journal_full")
    parsed = parse_plan_journal(document)
    if not parsed["records"] or parsed["records"][-1]["record_digest"] != expected_digest:
        raise PlanArtifactError("content_drift")
    return document, parsed


class PlanArtifactError(OSError):
    """A public plan failure is a bounded diagnostic, never source content."""
    def __init__(self, code: str, *, path: str | None = None, actual: int | None = None,
                 limit: int | None = None, unit: str | None = None,
                 recoverability: str = "repair_required") -> None:
        super().__init__(code)
        self.code = code if code in PLAN_ARTIFACT_DIAGNOSTIC_CODES else "write_error"
        safe_path = path if isinstance(path, str) and (
            path in {"manifest", "global_constraints", "slice", "slices", "title", "scope", "acceptance", "rollback", "stop_conditions", "expected_artifacts", "journal", "revision", "version"}
            or re.fullmatch(r"slices\[(?:0|[1-9][0-9]{0,5})\](?:\.(?:id|title|scope|acceptance|rollback|stop_conditions|expected_artifacts)(?:\[(?:0|[1-9][0-9]{0,5})\])?)?", path)
        ) else None
        self.metadata = {
            "code": self.code,
            "path": safe_path,
            "actual": actual if isinstance(actual, int) and 0 <= actual <= 2**31 - 1 else None,
            "limit": limit if isinstance(limit, int) and 0 <= limit <= 2**31 - 1 else None,
            "unit": unit if unit in {"bytes", "nodes", "items"} else None,
            "recoverability": recoverability if recoverability in {"repair_required", "retryable", "terminal"} else "repair_required",
        }
        # Keep a hard, future-proof ceiling even if this structure grows.
        if len(canonical_json(self.metadata).encode("utf-8")) > 512:
            self.metadata = {"code": self.code, "path": None, "actual": None, "limit": None,
                             "unit": None, "recoverability": "repair_required"}


def _safe_plan_artifact_diagnostic(value: Any) -> dict[str, Any] | None:
    """Keep only the fixed public error shape; never persist exception text."""
    if not isinstance(value, dict):
        return None
    return PlanArtifactError(
        str(value.get("code") or "write_error"),
        path=value.get("path"), actual=value.get("actual"), limit=value.get("limit"),
        unit=value.get("unit"), recoverability=value.get("recoverability", "repair_required"),
    ).metadata


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
        session = plan_artifact_session_id(payload.get("session_id"), _safe_task_epoch(state.get("task_epoch")).get("id"))
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
    # Project-local .codex/plans was a legacy review mirror.  It is
    # untrusted input, not a migration source: never open it, parse a plan
    # body, create a journal from it, remove it, or pass it to an executor.
    # Active authority therefore fails closed.  The files are deliberately
    # left untouched for the project owner to inspect or remove themselves.
    if source_schema < 20:
        active = state.get("plan_state") in {
            "plan_ready", "awaiting_confirmation", "confirmed",
        } or state.get("executor_state") not in {None, "none", "succeeded"}
        if active:
            invalidate_plan_authority(state, warning_code="legacy_unavailable")
        record_lifecycle_diagnostic(
            state, "legacy_plan_rejected", level="warning",
            contract_id=state.get("execution_contract_id"),
        )
        return False
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
        session = plan_artifact_session_id(payload.get("session_id"), _safe_task_epoch(state.get("task_epoch")).get("id"))
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
                "journal_prefix_digest": parsed["journal_prefix_digest"],
                "journal_prefix_bytes": parsed["journal_prefix_bytes"],
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
        and (not artifact.get("journal_prefix_digest")
             or artifact.get("journal_prefix_digest") == parsed.get("journal_prefix_digest"))
        and (not artifact.get("journal_prefix_bytes")
             or artifact.get("journal_prefix_bytes") == parsed.get("journal_prefix_bytes"))
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
    session = plan_artifact_session_id(payload.get("session_id"), _safe_task_epoch(state.get("task_epoch")).get("id"))
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
        execution_slices = execution_slice_manifest_for_plan(body)
        pending_causal = _safe_pending_causal_revision(
            state.get("pending_causal_revision")
        )
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
            if pending_causal:
                baseline = _safe_execution_baseline(
                    state.get("last_execution_baseline")
                )
                if not (
                    parsed_existing
                    and pending_causal.get("parent_revision_digest")
                    == parsed_existing.get("current_revision_digest")
                    == previous.get("current_revision_digest")
                    and pending_causal.get("parent_prefix_digest")
                    == parsed_existing.get("journal_prefix_digest")
                    == previous.get("journal_prefix_digest")
                    and pending_causal.get("parent_contract_id")
                    == baseline.get("execution_contract_id")
                    and pending_causal.get("terminal_baseline_id")
                    == baseline.get("baseline_id")
                    and pending_causal.get("root_objective_fingerprint")
                    == objective
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
                causal_record=(
                    {**pending_causal, "creation_state": "executable"}
                    if pending_causal else None
                ),
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
                "journal_prefix_digest": parsed["journal_prefix_digest"],
                "journal_prefix_bytes": parsed["journal_prefix_bytes"],
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
        if pending_causal:
            latest = parsed["records"][-1]
            state["causal_lineage"] = _safe_causal_lineage({
                "root_objective_fingerprint": pending_causal.get(
                    "root_objective_fingerprint"
                ),
                "selected_revision_digest": plan_digest,
                "selected_contract_id": None,
                "selected_prefix_digest": parsed.get("journal_prefix_digest"),
                "terminal_baseline_id": pending_causal.get("terminal_baseline_id"),
                "terminal_seal_digest": _safe_causal_lineage(
                    state.get("causal_lineage")
                ).get("terminal_seal_digest"),
                "current_issue_fingerprint": pending_causal.get("issue_fingerprint"),
                "current_causal_type": pending_causal.get("causal_type"),
                "tail_record_type": latest.get("record_type"),
                "tail_record_digest": latest.get("record_digest"),
            })
            pending_causal["creation_state"] = "plan_composition"
            state["pending_causal_revision"] = pending_causal
        else:
            state["causal_lineage"] = _safe_causal_lineage({
                "root_objective_fingerprint": objective,
                "selected_revision_digest": plan_digest,
                "selected_prefix_digest": parsed.get("journal_prefix_digest"),
            })
        return True
    except PlanArtifactError as error:
        failure["write_status"] = (
            error.code
            if error.code in {"revision_too_large", "journal_full"}
            else "write_failed"
        )
        failure["warning_code"] = error.code
        failure["diagnostic"] = error.metadata
    except OSError:
        failure["write_status"] = "write_failed"
        failure["warning_code"] = "write_error"
        failure["diagnostic"] = PlanArtifactError("write_error").metadata
    state["plan_artifact"] = failure
    return False


def append_runtime_plan_record(
    state: dict[str, Any], payload: dict[str, Any], *, record_type: str, data: dict[str, Any]
) -> bool:
    """Append bounded v3 evidence without changing executable authority.

    Runtime tails are written only after their state transition is otherwise
    proven.  The immutable executable prefix stays pinned in state, while the
    full digest is advanced atomically so later reads still detect tampering.
    """
    artifact = _safe_plan_artifact(state.get("plan_artifact"))
    parts = str(artifact.get("relative_path") or "").split("/")
    if len(parts) != 3 or not artifact.get("journal_digest"):
        return False
    try:
        root = _canonical_plan_data_root(payload)
        session = plan_artifact_session_id(payload.get("session_id"), _safe_task_epoch(state.get("task_epoch")).get("id"))
        if parts[1] != session or parts[2] != PLAN_JOURNAL_NAME:
            raise PlanArtifactError("unsafe_path")
        target = root / "plans" / session / PLAN_JOURNAL_NAME
        with plan_session_directory_guard(root, session, create=False) as guard:
            guard["verify"]()
            existing = _read_plan_artifact_document(target, directory_fd=guard["directory_fd"])
            parsed = parse_plan_journal(existing, expected_session=session)
            if not _stored_artifact_matches_journal(artifact, parsed):
                raise PlanArtifactError("content_drift")
            document, updated = append_plan_journal_record(existing, record_type=record_type, data=data)
            transaction = _atomic_write_plan_file(
                target, document, expected_old_bytes=existing,
                directory_fd=guard["directory_fd"], verify_binding=guard["verify"],
            )
            _commit_plan_write(transaction, guard["verify"])
            _fsync_plan_directory(guard["directory_fd"])
            installed = _read_plan_artifact_document(target, directory_fd=guard["directory_fd"])
            if installed != document or parse_plan_journal(installed, expected_session=session) != updated:
                raise PlanArtifactError("content_drift")
        artifact["journal_digest"] = updated["journal_digest"]
        artifact["journal_prefix_digest"] = updated["journal_prefix_digest"]
        artifact["journal_prefix_bytes"] = updated["journal_prefix_bytes"]
        artifact["updated_at"] = utc_now()
        state["plan_artifact"] = artifact
        latest = updated["records"][-1]
        lineage = _safe_causal_lineage(state.get("causal_lineage"))
        lineage.update({
            "root_objective_fingerprint": (
                lineage.get("root_objective_fingerprint")
                or safe_fingerprint(state.get("objective", {}).get("fingerprint"))
            ),
            "tail_record_type": latest.get("record_type"),
            "tail_record_digest": latest.get("record_digest"),
        })
        if latest.get("record_type") == "terminal_seal":
            lineage["terminal_baseline_id"] = _fingerprint32(
                latest.get("data", {}).get("terminal_baseline_id")
                or latest.get("data", {}).get("baseline_id")
            )
            lineage["terminal_seal_digest"] = latest.get("record_digest")
        state["causal_lineage"] = _safe_causal_lineage(lineage)
        return True
    except (OSError, PlanArtifactError):
        # A failed evidence append must not silently turn a verified success
        # into a different execution contract.  Preserve the prior state for
        # recovery and let normal artifact verification fail closed if needed.
        return False


def seal_completed_execution(state: dict[str, Any], payload: dict[str, Any]) -> bool:
    """Persist the terminal baseline as a non-authorizing immutable v3 tail."""
    baseline = _safe_execution_baseline(state.get("last_execution_baseline"))
    contract = safe_fingerprint(state.get("execution_contract_id"))
    if not baseline or not contract or baseline.get("execution_contract_id") != contract:
        return False
    return append_runtime_plan_record(state, payload, record_type="terminal_seal", data={
        "parent_revision_digest": safe_fingerprint(state.get("plan_digest")),
        "parent_contract_id": contract,
        "terminal_baseline_id": baseline.get("baseline_id"),
        "root_objective_fingerprint": state.get("objective", {}).get("fingerprint"),
        "acceptance_status": baseline.get("acceptance_status"),
        "change_set_digest": baseline.get("change_set_digest"),
        "verification_digest": baseline.get("verification_digest"),
    })


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
        if session != plan_artifact_session_id(payload.get("session_id"), _safe_task_epoch(state.get("task_epoch")).get("id")):
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
            parsed_manifest = execution_slice_manifest_for_plan(
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
        in {"none", "analyzing", "plan_ready", "awaiting_confirmation", "confirmed", "invalidated", "repair_required"}
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
        "executor_attempt": safe_sequence(item.get("executor_attempt")),
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
        "causal_lineage": _safe_causal_lineage(item.get("causal_lineage")),
        "lifecycle_diagnostics": [
            value for raw in as_list(item.get("lifecycle_diagnostics"))[-4:]
            if (value := _safe_lifecycle_diagnostic(raw)) is not None
        ],
        "stall": _safe_stall(item.get("stall")),
        "continuation_lease": _safe_continuation_lease(item.get("continuation_lease")),
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
    for key in (
        "session_fingerprint", "cwd_fingerprint", "root_session_fingerprint",
        "root_cwd_fingerprint",
    ):
        fingerprint = safe_fingerprint(value.get(key))
        if fingerprint:
            base[key] = fingerprint
    # A pre-v31 state did not distinguish a root identity.  Its persisted
    # session/cwd are the only safe historical roots; never substitute the
    # cwd from this resume event.
    if not safe_fingerprint(value.get("root_session_fingerprint")):
        base["root_session_fingerprint"] = base["session_fingerprint"]
    if not safe_fingerprint(value.get("root_cwd_fingerprint")):
        base["root_cwd_fingerprint"] = base["cwd_fingerprint"]
    # Do not invent an epoch id for schema-31 state: its journal path is part
    # of the historical evidence.  It becomes an explicit successor only on a
    # later safe new-objective boundary.
    base["task_epoch"] = _safe_task_epoch(value.get("task_epoch"))
    base["archived_epochs"] = [
        _safe_task_epoch(item) | {
            "plan_digest": safe_fingerprint(item.get("plan_digest")) or None,
            "execution_contract_id": safe_fingerprint(item.get("execution_contract_id")) or None,
        }
        for item in as_list(value.get("archived_epochs")) if isinstance(item, dict)
    ][-8:]
    base["isolated_lifecycles"] = [
        item
        for raw in as_list(value.get("isolated_lifecycles"))
        if (item := _safe_isolated_lifecycle(raw)) is not None
    ][-MAX_ISOLATED_LIFECYCLES:]
    base["child_liveness"] = _safe_child_liveness(value.get("child_liveness"))
    base["parent_writer_lease"] = _safe_parent_writer_lease(value.get("parent_writer_lease"))
    root_rollout_identity = value.get("root_rollout_identity")
    base["root_rollout_identity"] = (
        root_rollout_identity
        if isinstance(root_rollout_identity, dict)
        and set(root_rollout_identity) == {"device", "inode"}
        and all(isinstance(root_rollout_identity.get(key), int) and root_rollout_identity[key] >= 0
                for key in ("device", "inode"))
        else None
    )
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
            legacy_route = (
                value.get("last_route")
                if isinstance(value.get("last_route"), dict)
                else {}
            )
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
        in {"none", "analyzing", "plan_ready", "awaiting_confirmation", "confirmed", "invalidated", "repair_required"}
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
        if source_schema >= 28
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
    # A verified legacy candidate may retain one read-only parent review, but
    # no pending/running legacy state gains current-profile mutation authority.
    review_profile_continuity = bool(
        source_schema >= 23
        and source_contract
        and source_executor_review.get("execution_contract_id") == source_contract
        and source_executor_review.get("attempt")
        == safe_sequence(value.get("executor_attempt"))
        and source_executor_review.get("status")
        in {"review_required", "recovery_started", "failed", "exhausted"}
        and value.get("executor_state") in {"verification_required", "exhausted"}
    )
    active_profile10_continuity = bool(
        source_schema == 28
        and source_profile == "10"
        and value.get("plan_state") == "confirmed"
        and source_contract
        and value.get("executor_state") in {"running", "verification_required"}
    )
    # v11 lifecycle records remain audit evidence only.  They must not gain
    # v12 write authority during a schema migration.
    active_profile11_continuity = False
    # Active and failed contracts rebind to the current profile. A completed,
    # baseline-sealed contract keeps the profile it actually executed under;
    # rewriting it to v6 would either invent evidence or reopen finished work.
    # The one Schema 22 succeeded+incomplete shape is not sealed: preserve its
    # real v5 contract as a review candidate so a typed successor can repair it.
    base["execution_profile_version"] = (
        source_profile
        if sealed_historical_success
        or legacy_verification_pending
        or review_profile_continuity
        or active_profile10_continuity
        or active_profile11_continuity
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
    base["executor_attempt"] = safe_sequence(value.get("executor_attempt"))
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
    base["assessor_attempt"] = safe_sequence(value.get("assessor_attempt"))
    base["authorization_scope"] = _safe_authorization_scope(
        value.get("authorization_scope")
    )
    base["authorization_envelope"] = _safe_authorization_envelope(value.get("authorization_envelope"))
    base["pending_confirmation_receipt"] = _fingerprint32(
        value.get("pending_confirmation_receipt")
    )
    base["recovery_chain"] = safe_recovery_chain(value.get("recovery_chain"))
    base["pending_recovery_facts"] = _safe_pending_recovery_facts(
        value.get("pending_recovery_facts")
    )
    base["pending_recovery_reservation"] = _safe_pending_recovery_reservation(
        value.get("pending_recovery_reservation")
    )
    # Schema 27 did not persist progress liveness. Re-anchor the still-live
    # assessor on its first v28 observation; do not infer elapsed idle time.
    base["assessment_liveness"] = _safe_assessment_liveness(value.get("assessment_liveness"))
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
    base["causal_lineage"] = _safe_causal_lineage(value.get("causal_lineage"))
    base["pending_causal_revision"] = _safe_pending_causal_revision(
        value.get("pending_causal_revision")
    )
    base["plan_composition"] = _safe_plan_composition(value.get("plan_composition"))
    base["lifecycle_diagnostics"] = [
        item for raw in as_list(value.get("lifecycle_diagnostics"))
        if (item := _safe_lifecycle_diagnostic(raw)) is not None
    ][-MAX_LIFECYCLE_DIAGNOSTICS:]
    if safe_int(value.get("schema_version")) >= 17:
        base["stall"] = _safe_stall(value.get("stall"))
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
    pending_receipt = _fingerprint32(base.get("pending_confirmation_receipt"))
    if pending_receipt and pending_receipt != pending_confirmation_receipt_for_state(base):
        base["pending_confirmation_receipt"] = None
    if base["plan_state"] == "confirmed":
        envelope = _safe_authorization_envelope(base.get("authorization_envelope"))
        if envelope.get("digest") != authorization_envelope_digest(base):
            base["plan_state"] = "awaiting_confirmation"
            base["confirmed_plan_digest"] = None
            base["confirmed_at"] = None
            base["authorization_envelope"] = _safe_authorization_envelope(None)

    if (
        (source_schema == 23 or source_profile == "7")
        and base["plan_state"] == "confirmed"
        and not sealed_historical_success
        and not review_profile_continuity
    ):
        # Older revisions never gain current-profile write authority. Never invent a
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
    # Highest-profile contract identity depends on the original assessor's
    # request/Post/Start lifecycle. Normalize that evidence before computing
    # the expected execution contract; flat assessor fields must not stand in
    # for the three host records during a reload.
    contract_subagents = [
        item
        for raw in as_list(value.get("subagents"))
        if (item := _safe_subagent(raw)) is not None
    ]
    if source_schema < 19:
        for item in contract_subagents:
            item["request_visibility"] = None
    base["subagents"] = retained_subagent_records(base, contract_subagents)
    expected_contract = (
        source_contract
        if sealed_historical_success or legacy_verification_pending or review_profile_continuity or active_profile10_continuity or active_profile11_continuity
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
            or active_profile10_continuity
            or active_profile11_continuity
        )
    )
    if base["plan_state"] == "confirmed" and not valid_execution_binding:
        # Schema 9 confirmations never proved an executor handoff. Recreate the contract, but never
        # infer that execution had started or succeeded.
        base["execution_profile_version"] = EXECUTION_PROFILE_VERSION
        base["execution_contract_id"] = expected_contract
        base["executor_state"] = "spawn_required"
        base["executor_agent_id"] = None
        base["executor_attempt"] = safe_sequence(base.get("executor_attempt"))
        base["executor_failure_kind"] = None
        base["executor_model"] = None
        base["executor_reasoning_effort"] = None
        base["executor_fork_turns"] = None
        base["executor_review"] = _empty_executor_review()
        base["model_profile"] = confirmed_executor_model_profile(base)
    elif base["plan_state"] == "confirmed":
        base["model_profile"] = (
            "work_assessment"
            if base["executor_state"] in {"verification_required", "exhausted"}
            else confirmed_executor_model_profile(base)
        )
    if (
        source_profile == "11"
        and not sealed_historical_success
        and value.get("executor_state") in {"spawn_pending", "running", "verification_required"}
        and base.get("plan_state") == "confirmed"
    ):
        # A currently active v11 writer is not terminal proof.  Isolate it
        # and require a v12 recovery lifecycle instead of silently reusing it.
        base["executor_state"] = "recovery_required"
        base["executor_failure_kind"] = "stale_contract"
        base["executor_agent_id"] = None
        base["executor_review"] = _empty_executor_review()
        base["model_profile"] = "work_assessment"
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
        base["executor_attempt"] = safe_sequence(base.get("executor_attempt"))
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
        base["executor_state"] = "recovery_required"
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
            base["executor_state"] = "recovery_required"
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
    if source_schema < 19:
        for item in safe_subagents:
            item["request_visibility"] = None
    base["subagents"] = retained_subagent_records(base, safe_subagents)
    if (
        source_schema == SCHEMA_VERSION
        and source_writer == WRITER_VERSION
        and base.get("task_domain") == "work"
        and base.get("work_difficulty") == "hard"
        and base.get("plan_state") in {"analyzing", "repair_required"}
        and base.get("assessor_state") == "recovery_required"
        and base.get("assessor_failure_kind") == "assessment_result_invalid"
        and original_assessor_result_receipt(base)
    ):
        # A previous 1.0.48 process could misclassify a current-model native
        # assessment as invalid solely because it omitted the plugin's prose
        # marker. Restore only the proven request/Post/full-Start/Stop
        # lifecycle. Execution still needs the parent's strict manifest.
        base["assessor_state"] = "hard_plan_ready"
        base["assessor_failure_kind"] = None
        base["model_profile"] = "work_assessment"
        assessment_receipt = original_assessor_result_receipt(base)
        if assessment_receipt:
            base["plan_composition"] = _safe_plan_composition(
                {
                    "status": "pending",
                    "assessor_binding_id": base.get("assessor_binding_id"),
                    "objective_fingerprint": base.get("objective", {}).get(
                        "fingerprint"
                    ),
                    "assessment_receipt": assessment_receipt,
                }
            )
        latest_prompt = (
            base["prompts"][-1] if as_list(base.get("prompts")) else {}
        )
        if metadata_is_exact_confirmation(latest_prompt.get("prompt_meta")):
            receipt = pending_confirmation_receipt_for_state(base)
            if receipt:
                base["pending_confirmation_receipt"] = receipt
    if source_schema == SCHEMA_VERSION and source_writer == WRITER_VERSION:
        restore_native_executor_review_candidate(base, payload)
    base["change_epoch"] = min(max(safe_int(value.get("change_epoch")), 0), MAX_EVENT_COUNT)
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
    legacy_assessor_reanchor = bool(
        source_schema == 27
        and value.get("assessor_state") == "running"
        and safe_fingerprint(value.get("assessor_binding_id"))
        and value.get("assessor_agent_id")
        and value.get("assessor_observed_effective")
    )
    legacy_writer_isolated = bool(
        source_writer != WRITER_VERSION
        and not sealed_historical_success
        and not legacy_assessor_reanchor
        and isolate_legacy_writer(base, value, source_writer=source_writer)
    )
    if (
        source_writer != WRITER_VERSION
        and not legacy_assessor_reanchor
        and not active_profile10_continuity
        and not active_profile11_continuity
    ):
        # Opaque lifecycle/request records are evidence from the old writer, not
        # authority for a resumed task. Preserve canonical plans and completed
        # baselines, but remove transient caches and rebind unfinished assessment.
        base["subagents"] = []
        base["processed_hook_runs"] = []
        base["duplicate_notices"] = []
        isolated_role = _safe_child_liveness(
            base.get("child_liveness")
        ).get("role")
        if not (legacy_writer_isolated and isolated_role == "high_assessor"):
            base["assessor_agent_id"] = None
            base["assessor_model"] = None
            base["assessor_reasoning_effort"] = None
            base["assessor_failure_kind"] = None
            base["assessor_observed_effective"] = False
            base["assessor_observed_model"] = None
            base["assessor_observed_reasoning_effort"] = None
            base["assessor_fork_turns"] = None
            base["assessor_attempt"] = 0
        if legacy_writer_isolated:
            # The current plan/contract can be repaired by a fresh v12
            # recovery child, but the pre-v12 process is only a tombstone.
            # Do not reopen assessment or overwrite the explicit recovery.
            pass
        elif (
            base["task_domain"] == "work"
            and base.get("work_difficulty") == "hard"
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
    elif source_schema == 27:
        # v27's active assessor remains the sole owner; v28 intentionally
        # starts its elapsed clock at the next observation.
        base["assessment_liveness"] = _empty_assessment_liveness()
    base["pending_recovery_facts"] = pending_recovery_facts_for_state(base)
    base["pending_recovery_reservation"] = pending_recovery_reservation_for_state(base)
    base["continuation_lease"] = _safe_continuation_lease(value.get("continuation_lease"))
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
        state["_normalized_state_changed"] = bool(
            isinstance(decoded, dict)
            and json.dumps(decoded, ensure_ascii=False, sort_keys=True)
            != json.dumps(state, ensure_ascii=False, sort_keys=True)
        )
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
    state["lifecycle_diagnostics"] = [
        item for raw in as_list(state.get("lifecycle_diagnostics"))
        if (item := _safe_lifecycle_diagnostic(raw)) is not None
    ][-MAX_LIFECYCLE_DIAGNOSTICS:]
    state["isolated_lifecycles"] = [
        item
        for raw in as_list(state.get("isolated_lifecycles"))
        if (item := _safe_isolated_lifecycle(raw)) is not None
    ][-MAX_ISOLATED_LIFECYCLES:]
    state["child_liveness"] = _safe_child_liveness(state.get("child_liveness"))
    state["processed_hook_runs"] = list(state.get("processed_hook_runs", []))[-MAX_PROCESSED_RUNS:]
    state["duplicate_notices"] = list(state.get("duplicate_notices", []))[-MAX_DUPLICATE_NOTICES:]
    state["pending_recovery_facts"] = pending_recovery_facts_for_state(state)
    state["pending_recovery_reservation"] = pending_recovery_reservation_for_state(
        state
    )


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


def _rollout_user_message(record: dict[str, Any]) -> str | None:
    if record.get("type") != "response_item":
        return None
    value = record.get("payload")
    if not isinstance(value, dict) or value.get("type") != "message" or value.get("role") != "user":
        return None
    parts = []
    for item in as_list(value.get("content")):
        if not isinstance(item, dict) or item.get("type") not in {"input_text", "text"}:
            continue
        if isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n".join(parts) if parts else None


def root_rollout_regular_file_identity(path_value: Any) -> dict[str, int] | None:
    """Return an immutable identity for one regular host-owned rollout.

    Size and mtime intentionally are not part of the identity: Desktop may
    append a rollout while a task is alive.  Device/inode pin the actual file
    and lstat prevents symlink substitution on both POSIX and Windows.
    """
    if not path_value:
        return None
    try:
        info = Path(str(path_value)).lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            return None
        return {"device": int(info.st_dev), "inode": int(info.st_ino)}
    except (OSError, TypeError, ValueError):
        return None


def reconcile_missed_parent_controls_from_rollout(
    payload: dict[str, Any], state: dict[str, Any]
) -> bool:
    """Recover a missed parent plan/confirmation from same-session host truth.

    Desktop may start a delegated follow-up immediately after ``task_complete``
    and omit the corresponding Stop/UserPrompt hook deliveries. The rollout is
    already the host's durable lifecycle record, so use it as one bounded
    continuity bridge instead of requiring the user to repeat confirmation or
    encoding more semantics into task names.
    """
    if not (
        state.get("task_domain") == "work"
        and state.get("work_difficulty") == "hard"
        and state.get("assessor_state") == "hard_plan_ready"
        and state.get("plan_state")
        in {"analyzing", "repair_required", "awaiting_confirmation"}
    ):
        return False
    session_id = safe_label(payload.get("session_id"), 120)
    if not session_id or state.get("root_session_fingerprint") != stable_hash(session_id):
        return False
    root_identity = root_rollout_regular_file_identity(payload.get("transcript_path"))
    if root_identity is None:
        record_lifecycle_diagnostic(state, "root_identity_mismatch", level="warning")
        return False
    bound_identity = state.get("root_rollout_identity")
    if bound_identity is None:
        state["root_rollout_identity"] = root_identity
    elif bound_identity != root_identity:
        record_lifecycle_diagnostic(state, "root_identity_mismatch", level="error")
        return False
    records = read_host_rollout_records(payload.get("transcript_path"))
    if not records:
        return False
    metas = [
        item.get("payload")
        for item in records
        if item.get("type") == "session_meta"
        and isinstance(item.get("payload"), dict)
    ]
    if len(metas) != 1:
        return False
    meta = metas[0]
    if meta.get("id") != session_id or meta.get("session_id") != session_id:
        return False
    if state.get("root_cwd_fingerprint") and stable_hash(meta.get("cwd")) != state.get("root_cwd_fingerprint"):
        return False

    confirmations: list[int] = []
    plans: list[tuple[int, str, str | None]] = []
    for index, record in enumerate(records):
        user_text = _rollout_user_message(record)
        if user_text is not None:
            delegated = codex_delegation_input(user_text)
            if pure_plan_confirmation(delegated if delegated is not None else user_text):
                confirmations.append(index)
        value = record.get("payload")
        if (
            record.get("type") == "event_msg"
            and isinstance(value, dict)
            and value.get("type") == "task_complete"
        ):
            message = value.get("last_agent_message")
            if isinstance(message, str) and canonical_plan_message_ready(message):
                plans.append(
                    (
                        index,
                        message,
                        safe_label(value.get("turn_id"), 120)
                        if value.get("turn_id")
                        else None,
                    )
                )
    # A rollout is a recovery bridge, not an event log from which we may pick
    # a convenient historical plan.  In particular, a child completion can
    # look exactly like a parent ``task_complete`` record.  Without a unique
    # parent candidate there is no host fact tying a plan to this live Hard
    # contract, so leave the normal parent Stop path in charge.
    if len(plans) != 1:
        return False
    plan_index, plan_message, plan_turn = plans[0]
    later_confirmations = [position for position in confirmations if position > plan_index]
    # A pure confirmation is similarly one causal boundary.  Duplicate or
    # earlier confirmations may belong to a prior/child turn and must never
    # unlock the recovered plan.
    if len(later_confirmations) > 1:
        return False
    has_confirmation = len(later_confirmations) == 1
    changed = False
    if has_confirmation:
        receipt = pending_confirmation_receipt_for_state(state)
        if receipt:
            state["pending_confirmation_receipt"] = receipt
            changed = True
    if state.get("plan_state") in {"analyzing", "repair_required"}:
        state["confirmed_plan_digest"] = None
        state["confirmed_at"] = None
        reset_executor_binding(state)
        state["plan_state"] = "analyzing"
        if write_plan_artifact(state, payload, plan_message):
            state["plan_state"] = "awaiting_confirmation"
            changed = True
        else:
            state["plan_state"] = "repair_required"
            return True
    if has_confirmation and state.get("plan_state") == "awaiting_confirmation":
        changed = auto_confirm_trusted_plan(state, payload) or changed
    if changed:
        state.setdefault("guards", []).append(
            {
                "at": utc_now(),
                "turn_id": plan_turn,
                "kind": "host_rollout_parent_control_reconciled",
                "action": "advise",
                "fingerprint": stable_hash(
                    f"host-rollout-parent-control-v1\0{session_id}\0{plan_turn}\0{has_confirmation}",
                    32,
                ),
            }
        )
    return changed


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
            normalized_state_changed = bool(
                state.pop("_normalized_state_changed", False)
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
                if reconcile_missed_parent_controls_from_rollout(payload, state):
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
                if normalized_state_changed or after != before or pending is not None:
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
            state.pop("_normalized_state_changed", None)
            # The initial cwd is part of the root task identity.  Do not let a
            # later hook event (especially a child rollout or resumed window)
            # overwrite it: reconciliation compares against this immutable
            # value and fails closed on cross-cwd evidence.
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
        older: list[tuple[tuple[int, int, int], Path]] = []
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
                older.append((version, candidate))
        # Keep the immediately preceding official release for live task
        # rollback.  Do not touch links, future versions, or non-semver names.
        keep = max(older, default=None, key=lambda item: item[0])
        removed = 0
        for version, candidate in older:
            if keep is not None and candidate == keep[1]:
                continue
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
                "active_agent_scopes": active_agent_scope_summary(state),
                "recent_successes": [],
            }
        )
    return len(new_items)


def _host_event_turn_id(event: dict[str, Any]) -> str | None:
    """Return one coherent host-owned turn identifier, never a guessed one."""
    meta = event.get("internal_chat_message_metadata_passthrough") or {}
    top = safe_label(event.get("turn_id"), 120) if event.get("turn_id") else None
    nested = safe_label(meta.get("turn_id"), 120) if meta.get("turn_id") else None
    return top if top and (not nested or top == nested) else nested if nested and not top else None


def literal_exec_command_source(source: str) -> tuple[str, str] | None:
    """Parse one inert JS/JSON ``tools.exec_command`` literal without eval."""
    if source.count("tools.exec_command(") != 1:
        return None
    json_call = re.search(
        r"tools\.exec_command\(\s*(\{.*\})\s*\)\s*;", source, re.S
    )
    if json_call:
        try:
            value = json.loads(json_call.group(1))
        except Exception:
            value = None
        if isinstance(value, dict) and isinstance(value.get("cmd"), str):
            workdir = value.get("workdir", "")
            return (value["cmd"], workdir) if isinstance(workdir, str) else None
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
    return command, cwd


def literal_apply_patch_source(source: str) -> str | None:
    """Parse one inert literal ``tools.apply_patch`` call without eval."""
    if source.count("tools.apply_patch(") != 1:
        return None
    direct = re.search(
        r'tools\.apply_patch\(\s*("(?:[^"\\]|\\.)*")\s*\)', source, re.S
    )
    bound = re.search(
        r'const\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*'
        r'("(?:[^"\\]|\\.)*")\s*;[\s\S]*?'
        r'tools\.apply_patch\(\s*\1\s*\)',
        source,
    )
    if bool(direct) == bool(bound):
        return None
    try:
        value = json.loads(direct.group(1) if direct else bound.group(2))
    except Exception:
        return None
    return value if isinstance(value, str) else None


def _normalized_patch_target(path_value: Any, cwd: str) -> str | None:
    """Resolve one apply_patch/FileChange path lexically and portably."""
    if not isinstance(path_value, str) or not path_value or "\x00" in path_value:
        return None
    if path_value != path_value.strip() or not isinstance(cwd, str) or not cwd:
        return None
    try:
        root = os.path.normpath(cwd)
        if not os.path.isabs(root):
            return None
        if os.path.isabs(path_value):
            resolved = os.path.normpath(path_value)
        else:
            resolved = os.path.normpath(os.path.join(root, path_value))
            if os.path.normcase(os.path.commonpath((root, resolved))) != os.path.normcase(root):
                return None
        return os.path.normcase(resolved)
    except (OSError, TypeError, ValueError):
        return None


def literal_patch_target_kinds(patch: str, cwd: str) -> dict[str, str] | None:
    """Return the exact path/kind manifest for one non-moving literal patch."""
    normalized = patch.replace("\r\n", "\n").replace("\r", "\n")
    if (
        normalized.count("*** Begin Patch") != 1
        or normalized.count("*** End Patch") != 1
        or re.search(r"(?m)^\*\*\* Move to:", normalized)
    ):
        return None
    headers = re.findall(
        r"(?m)^\*\*\* (Add|Update|Delete) File: ([^\n]+)$", normalized
    )
    visible_headers = re.findall(
        r"(?m)^\*\*\* (?:Add|Update|Delete) File:", normalized
    )
    if not headers or len(headers) != len(visible_headers):
        return None
    kind_map = {"Add": "add", "Update": "update", "Delete": "delete"}
    result: dict[str, str] = {}
    for action, raw_path in headers:
        target = _normalized_patch_target(raw_path, cwd)
        if not target or target in result:
            return None
        result[target] = kind_map[action]
    return result


def completed_file_change_receipt(
    event: dict[str, Any], cwd: str
) -> tuple[dict[str, str], str] | None:
    """Validate one host-owned completed FileChange and return its digest."""
    if event.get("type") != "item_completed" or not isinstance(event.get("item"), dict):
        return None
    item = event["item"]
    changes = item.get("changes")
    stdout = item.get("stdout")
    if not (
        item.get("type") == "FileChange"
        and item.get("status") == "completed"
        and item.get("stderr") == ""
        and isinstance(stdout, str)
        and stdout.startswith("Success.")
        and isinstance(changes, dict)
        and changes
    ):
        return None
    manifest: dict[str, str] = {}
    digest_changes: dict[str, dict[str, str]] = {}
    for raw_path, change in changes.items():
        target = _normalized_patch_target(raw_path, cwd)
        if not target or target in manifest or not isinstance(change, dict):
            return None
        kind = str(change.get("type") or "").lower()
        if (
            kind not in {"add", "update", "delete"}
            or change.get("move_path") not in (None, "")
        ):
            return None
        manifest[target] = kind
        if kind == "add":
            content = change.get("content")
            if not isinstance(content, str) or change.get("unified_diff") not in (None, ""):
                return None
            digest_changes[target] = {
                "kind": kind,
                "content_digest": stable_hash(
                    content.replace("\r\n", "\n").replace("\r", "\n"), 32
                ),
            }
        else:
            unified_diff = change.get("unified_diff")
            if not isinstance(unified_diff, str) or not unified_diff:
                return None
            digest_changes[target] = {
                "kind": kind,
                "unified_diff_digest": stable_hash(
                    unified_diff.replace("\r\n", "\n").replace("\r", "\n"), 32
                ),
            }
    receipt_digest = stable_hash(
        "workflow-manager-completed-file-change-v1\0"
        + canonical_json(
            {
                "changes": digest_changes,
                "stdout_digest": stable_hash(stdout, 32),
            }
        ),
        32,
    )
    return manifest, receipt_digest


def rollout_completed_file_change_after_patch(
    records: list[dict[str, Any]], *, turn_id: str, call_id: str,
    patch_source: str, cwd: str,
) -> str | None:
    """Bind one missing apply_patch output to one immediately completed FileChange."""
    call_indexes: list[int] = []
    patch_calls = 0
    output_indexes: list[int] = []
    output_values: list[Any] = []
    for index, record in enumerate(records):
        event = record.get("payload") or {}
        if not isinstance(event, dict) or _host_event_turn_id(event) != turn_id:
            continue
        if event.get("type") == "custom_tool_call" and event.get("name") == "exec":
            parsed_patch = literal_apply_patch_source(str(event.get("input") or ""))
            if parsed_patch is not None:
                patch_calls += 1
            if str(event.get("call_id") or "") == call_id:
                call_indexes.append(index)
                if parsed_patch != patch_source:
                    return None
        elif (
            event.get("type") == "custom_tool_call_output"
            and str(event.get("call_id") or "") == call_id
        ):
            output_indexes.append(index)
            output_values.append(event.get("output"))
    if (
        len(call_indexes) != 1
        or patch_calls != 1
        or len(output_indexes) > 1
        or output_indexes and output_indexes[0] <= call_indexes[0]
        or output_values and not host_apply_patch_receipt_success(output_values[0])
    ):
        return None

    file_changes: list[dict[str, Any]] = []
    saw_output = False
    for record in records[call_indexes[0] + 1 :]:
        event = record.get("payload") or {}
        if not isinstance(event, dict):
            continue
        event_turn = _host_event_turn_id(event)
        if event_turn and event_turn != turn_id:
            break
        if record.get("type") in {"compacted", "context_compacted"}:
            break
        if event.get("type") == "custom_tool_call":
            break
        if event.get("type") == "custom_tool_call_output" and str(
            event.get("call_id") or ""
        ) == call_id:
            if not file_changes:
                return None
            saw_output = True
            break
        if (
            event.get("type") == "item_completed"
            and isinstance(event.get("item"), dict)
            and event["item"].get("type") == "FileChange"
        ):
            file_changes.append(event)
    if len(file_changes) != 1 or bool(output_indexes) != saw_output:
        return None
    expected = literal_patch_target_kinds(patch_source, cwd)
    observed = completed_file_change_receipt(file_changes[0], cwd)
    if expected is None or observed is None or observed[0] != expected:
        return None
    return stable_hash(
        "workflow-manager-parent-patch-file-change-receipt-v1\0"
        + canonical_json(
            {
                "call_id": call_id,
                "file_change_receipt": observed[1],
                "outer_receipt": "host_success" if saw_output else "absent",
                "patch_digest": host_patch_digest(patch_source),
                "turn_id": turn_id,
            }
        ),
        32,
    )


def host_exec_receipt_statuses(output: Any) -> tuple[str, str | None]:
    """Separate an outer ``functions.exec`` envelope from its one tool leaf.

    Desktop's custom-tool output can wrap a real ``exec_command`` or
    ``write_stdin`` result in content/text/JSON layers.  The wrapper's
    ``completed``/``ok`` only says that transport succeeded; it must never
    overwrite the nested command exit code.  More than one terminal leaf is
    deliberately unknown: there is no safe call/session association to pick.
    """
    # Do not use response_status here: it deliberately descends into content,
    # which would make the leaf's failure look like an outer transport error.
    envelope = "unknown"
    if isinstance(output, dict):
        if output.get("error") or output.get("isError") is True or output.get("is_error") is True:
            envelope = "error"
        elif output.get("success") is False or output.get("ok") is False:
            envelope = "error"
        else:
            direct = str(output.get("status") or output.get("state") or "").strip().lower()
            if direct in ERROR_STATUSES:
                envelope = "error"
            elif direct in RUNNING_STATUSES:
                envelope = "running"
            elif direct in {"complete", "completed", "done", "ok", "success", "succeeded"}:
                envelope = "ok"
    leaves: list[str] = []

    def visit(value: Any, depth: int = 0) -> None:
        if depth > 12 or len(leaves) > 1:
            return
        if isinstance(value, str):
            text = value.strip()
            if text[:1] in "[{":
                try:
                    visit(json.loads(text), depth + 1)
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                visit(item, depth + 1)
            return
        if not isinstance(value, dict):
            return
        # An exit_code belongs to the actual shell/session tool result, unlike
        # generic host wrapper status.  Preserve even a nonzero code.
        if isinstance(value.get("exit_code"), int):
            leaves.append("ok" if value["exit_code"] == 0 else f"error:{value['exit_code']}")
            return
        for key in ("content", "output", "result", "tool_response", "response", "text"):
            if key in value:
                visit(value[key], depth + 1)

    visit(output)
    return envelope, leaves[0] if len(leaves) == 1 else None


def host_exec_output_status(output: Any) -> str:
    """Use one exact nested tool receipt; outer transport status is advisory."""
    _envelope, leaf = host_exec_receipt_statuses(output)
    return leaf or "unknown"


def host_apply_patch_receipt_success(response: Any) -> bool:
    """Recognize only the current supported empty patch receipt shape."""
    if response == {}:
        return True
    if not isinstance(response, list) or len(response) != 2:
        return False
    first, second = response
    if not (
        isinstance(first, dict)
        and isinstance(first.get("text"), str)
        and first.get("type") in {"input_text", "text"}
        and first["text"].startswith("Script completed\nWall time ")
        and first["text"].endswith("\nOutput:\n")
        and isinstance(second, dict)
        and isinstance(second.get("text"), str)
        and second.get("type") in {"input_text", "text"}
    ):
        return False
    try:
        return json.loads(second["text"]) == {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return False


def apply_patch_response_status(response: Any) -> str:
    """Resolve the tool-specific empty receipt without weakening generic tools."""
    generic = response_status(response)
    if generic.startswith("error") or generic == "running":
        return generic
    if host_apply_patch_receipt_success(response):
        return "ok"
    if isinstance(response, dict) and generic == "ok" and (
        isinstance(response.get("exit_code"), int)
        or str(response.get("status") or response.get("state") or "")
        .strip()
        .lower()
        in {"complete", "completed", "done", "ok", "success", "succeeded"}
        or response.get("isError") is False
    ):
        return "ok"
    return "unknown"


def transcript_turn_structured_exec_results(
    path_value: Any, turn_id: str
) -> list[tuple[str, str, str]]:
    """Return independently bound host exec chains from one turn.

    Current Codex may issue several native ``exec`` calls in one turn.  Each
    chain remains admissible only when its call id, literal command, optional
    workdir, output, and resulting digest are individually unique.  Ambiguous
    duplicate ids or duplicate command digests stay unknown without poisoning
    unrelated exact chains.
    """
    records: list[dict[str, Any]] = []
    for raw in read_transcript_tail(path_value):
        try:
            item = json.loads(raw)
            if isinstance(item, dict):
                records.append(item)
        except Exception:
            continue
    return rollout_turn_structured_exec_results(records, turn_id)


def rollout_turn_structured_exec_results(
    records: list[dict[str, Any]], turn_id: str
) -> list[tuple[str, str, str]]:
    """Return exact exec chains from an already bounded host rollout."""
    calls: dict[str, list[str]] = {}
    outputs: dict[str, list[Any]] = {}
    for item in records:
        event = item.get("payload") or {}
        if not isinstance(event, dict) or _host_event_turn_id(event) != turn_id:
            continue
        if event.get("type") == "custom_tool_call" and event.get("name") == "exec":
            calls.setdefault(str(event.get("call_id") or ""), []).append(
                str(event.get("input") or "")
            )
        elif event.get("type") == "custom_tool_call_output":
            outputs.setdefault(str(event.get("call_id") or ""), []).append(
                event.get("output")
            )
    parsed: list[tuple[str, str, str]] = []
    for call_id, sources in calls.items():
        if not call_id or len(sources) != 1 or len(outputs.get(call_id, [])) != 1:
            continue
        parsed_source = literal_exec_command_source(sources[0])
        if not parsed_source:
            continue
        command, cwd = parsed_source
        output = outputs[call_id][0]
        status = host_exec_output_status(output)
        if status == "unknown":
            continue
        digest = stable_hash(
            "host-operation-command-v1\0"
            + command.replace("\r\n", "\n").replace("\r", "\n")
            + "\0"
            + cwd,
            32,
        )
        parsed.append((digest, status, command))
    digest_counts: dict[str, int] = {}
    for digest, _, _ in parsed:
        digest_counts[digest] = digest_counts.get(digest, 0) + 1
    return [item for item in parsed if digest_counts[item[0]] == 1]


def transcript_turn_structured_exec_result(
    path_value: Any, turn_id: str
) -> tuple[str, str, str] | None:
    """Compatibility helper for callers that require one sole exec chain."""
    results = transcript_turn_structured_exec_results(path_value, turn_id)
    return results[0] if len(results) == 1 else None


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
    call_counts: dict[str, int] = {}
    outputs: dict[str, list[Any]] = {}
    for raw in read_transcript_tail(payload.get("transcript_path")):
        try:
            item = json.loads(raw); event = item.get("payload") or {}
            if _host_event_turn_id(event) != turn_id:
                continue
            turn_events.append(event)
            if event.get("type") == "custom_tool_call" and event.get("name") == "exec":
                call_id = str(event.get("call_id") or "")
                call_counts[call_id] = call_counts.get(call_id, 0) + 1
                patch_source = literal_apply_patch_source(str(event.get("input") or ""))
                if patch_source is not None:
                    patch_sources.append((call_id, patch_source))
            elif event.get("type") == "custom_tool_call_output":
                outputs.setdefault(str(event.get("call_id") or ""), []).append(
                    event.get("output")
                )
        except Exception:
            continue
    all_turn_patch_ops = [
        op
        for op in state.get("operations", [])
        if normalized_key(op.get("tool")) == "applypatch"
        and op.get("host_event_turn_id") == turn_id
        and op.get("host_input_digest")
    ]
    patch_ops = [
        op
        for op in all_turn_patch_ops
        if op.get("status") == "unknown"
        or (
            op.get("status") in SUCCESS_STATUSES
            and not op.get("reconciliation_source")
        )
    ]
    patch_digest_counts: dict[str, int] = {}
    for _, patch_source in patch_sources:
        digest = host_patch_digest(patch_source)
        patch_digest_counts[digest] = patch_digest_counts.get(digest, 0) + 1
    ordered_receipt_binding = bool(
        len(patch_sources) == len(all_turn_patch_ops) > 0
        and len({call_id for call_id, _ in patch_sources}) == len(patch_sources)
        and len({host_patch_digest(source) for _, source in patch_sources})
        == len(patch_sources)
        and len({op.get("fingerprint") for op in all_turn_patch_ops})
        == len(all_turn_patch_ops)
        and len({op.get("host_input_digest") for op in all_turn_patch_ops})
        == len(all_turn_patch_ops)
        and all(
            op.get("executor_agent_id")
            and op.get("execution_contract_id") == state.get("execution_contract_id")
            and op.get("slice_id") == (current_execution_slice(state) or {}).get("id")
            and op.get("slice_contract_id") == slice_contract_id(state)
            for op in all_turn_patch_ops
        )
    )
    for patch_index, (patch_call_id, patch_source) in enumerate(patch_sources):
        if (
            not patch_call_id
            or call_counts.get(patch_call_id) != 1
            or len(outputs.get(patch_call_id, [])) != 1
        ):
            continue
        patch_digest = host_patch_digest(patch_source)
        if patch_digest_counts.get(patch_digest) != 1:
            continue
        matches = [op for op in patch_ops if op.get("host_input_digest") == patch_digest]
        ordered_receipt_match = False
        if not matches and ordered_receipt_binding:
            ordered = all_turn_patch_ops[patch_index]
            if ordered in patch_ops:
                matches = [ordered]
                ordered_receipt_match = True
        exact_patch_match = bool(matches)
        current = current_execution_slice(state) or {}
        call_index = next((index for index, event in enumerate(turn_events) if event.get("type") == "custom_tool_call" and event.get("call_id") == patch_call_id), -1)
        output_index = next((index for index, event in enumerate(turn_events[call_index + 1 :], call_index + 1) if event.get("type") == "custom_tool_call_output" and event.get("call_id") == patch_call_id), -1)
        if call_index < 0 or output_index <= call_index:
            continue
        patch_events = [
            event
            for event in turn_events[call_index + 1 : output_index]
            if event.get("type") == "patch_apply_end"
        ]
        successes = [
            event
            for event in patch_events
            if (
                event.get("success") is True
                and str(event.get("status") or "").lower() == "completed"
            )
        ]
        if not matches and (
            safe_int(state.get("_source_schema_version")) == 25
            or safe_int(state.get("schema_version")) == 25
        ):
            legacy = [op for op in patch_ops if normalized_key(op.get("tool")) == "applypatch" and op.get("executor_agent_id") and op.get("execution_contract_id") == state.get("execution_contract_id") and op.get("slice_id") == current.get("id") and op.get("slice_contract_id") == slice_contract_id(state)]
            if len(legacy) == 1:
                matches = legacy
                matches[0]["legacy_host_input_digest"] = matches[0].get("host_input_digest")
                matches[0]["host_input_digest"] = patch_digest
                matches[0]["reconciliation_source"] = "legacy_unique_turn_patch_event_v1"
        if len(matches) == 1:
            legacy_success = len(patch_events) == len(successes) == 1
            current_receipt = bool(
                not patch_events
                and host_apply_patch_receipt_success(
                    outputs[patch_call_id][0]
                )
            )
            if legacy_success or current_receipt:
                if ordered_receipt_match:
                    matches[0]["legacy_host_input_digest"] = matches[0].get(
                        "host_input_digest"
                    )
                    matches[0]["host_input_digest"] = patch_digest
                matches[0]["status"] = "ok"
                matches[0]["category"] = "implementation"
                if exact_patch_match or ordered_receipt_match:
                    matches[0]["reconciliation_source"] = (
                        "host_rollout_exact_patch_digest_v1"
                        if legacy_success and exact_patch_match
                        else "host_rollout_exact_patch_receipt_v2"
                    )
    payload_cwd = str(payload.get("cwd") or "")
    for digest, status, command in transcript_turn_structured_exec_results(
        payload.get("transcript_path"), turn_id
    ):
        normalized_command = command.replace("\r\n", "\n").replace("\r", "\n")
        command_digest = stable_hash(
            "host-operation-command-text-v1\0" + normalized_command, 32
        )
        payload_cwd_digest = stable_hash(
            "host-operation-command-v1\0"
            + normalized_command
            + "\0"
            + payload_cwd,
            32,
        )
        candidates = [
            op
            for op in state.get("operations", [])
            if (
                op.get("status") == "unknown"
                or op.get("status") in SUCCESS_STATUSES
            )
            and op.get("host_event_turn_id") == turn_id
            and (
                op.get("host_input_digest") == digest
                or op.get("host_command_digest") == command_digest
                or (
                    not op.get("host_command_digest")
                    and payload_cwd
                    and op.get("host_input_digest") == payload_cwd_digest
                )
            )
        ]
        if len(candidates) != 1:
            continue
        op = candidates[0]
        if op.get("host_input_digest") != digest:
            op["legacy_host_input_digest"] = op.get("host_input_digest")
            op["host_input_digest"] = digest
        op["reconciliation_source"] = "host_rollout_exact_command_text_v1"
        op["host_command_digest"] = command_digest
        op["status"] = status
        op["category"] = command_category({"tool_name": op.get("tool")}, command)
    trusted = trusted_current_root_rollout(payload, state)
    if trusted is not None:
        records, root_cwd = trusted
        if reconcile_parent_filechange_turn(
            records, root_cwd, turn_id, state
        ):
            promote_reconciled_parent_review(state)


def trusted_current_root_rollout(
    payload: dict[str, Any], state: dict[str, Any]
) -> tuple[list[dict[str, Any]], str] | None:
    """Return one identity-pinned same-session parent rollout and its root cwd."""
    session_id = safe_label(payload.get("session_id"), 120)
    if (
        not session_id
        or state.get("root_session_fingerprint") != stable_hash(session_id)
    ):
        return None
    identity = root_rollout_regular_file_identity(payload.get("transcript_path"))
    if identity is None:
        return None
    bound_identity = state.get("root_rollout_identity")
    if bound_identity is not None and bound_identity != identity:
        record_lifecycle_diagnostic(
            state, "root_identity_mismatch", level="error"
        )
        return None
    records = read_host_rollout_records(payload.get("transcript_path"))
    metas = [
        item.get("payload")
        for item in records
        if item.get("type") == "session_meta"
        and isinstance(item.get("payload"), dict)
    ]
    if len(metas) != 1:
        return None
    meta = metas[0]
    cwd = meta.get("cwd")
    if (
        meta.get("id") != session_id
        or meta.get("session_id") != session_id
        or not isinstance(cwd, str)
        or not cwd
        or state.get("root_cwd_fingerprint") != stable_hash(cwd)
    ):
        return None
    if bound_identity is None:
        state["root_rollout_identity"] = identity
    return records, cwd


def reconcile_parent_filechange_turn(
    records: list[dict[str, Any]], cwd: str, turn_id: str,
    state: dict[str, Any],
) -> bool:
    """Upgrade one interrupted parent apply_patch from exact host FileChange truth."""
    if not parent_writer_lease_current(state):
        return False
    lease = _safe_parent_writer_lease(state.get("parent_writer_lease"))
    current = current_execution_slice(state) or {}
    acquired_at = str(lease.get("acquired_at") or "")
    candidates = [
        op
        for op in state.get("operations", [])
        if isinstance(op, dict)
        and normalized_key(op.get("tool")) == "applypatch"
        and op.get("status") == "unknown"
        and op.get("executor_agent_id") is None
        and op.get("host_event_turn_id") == turn_id
        and _fingerprint32(op.get("host_input_digest"))
        and op.get("epoch_id") == lease.get("epoch_id")
        and op.get("execution_contract_id") == lease.get("execution_contract_id")
        and op.get("slice_id") == current.get("id") == lease.get("slice_id")
        and op.get("slice_contract_id") == lease.get("slice_contract_id")
        and op.get("plan_digest") == state.get("plan_digest")
        and acquired_at
        and str(op.get("at") or "") >= acquired_at
    ]
    if len(candidates) != 1:
        return False
    patch_calls: list[tuple[str, str]] = []
    for record in records:
        event = record.get("payload") or {}
        if not isinstance(event, dict) or _host_event_turn_id(event) != turn_id:
            continue
        if event.get("type") != "custom_tool_call" or event.get("name") != "exec":
            continue
        patch_source = literal_apply_patch_source(str(event.get("input") or ""))
        if patch_source is not None:
            patch_calls.append((str(event.get("call_id") or ""), patch_source))
    if len(patch_calls) != 1:
        return False
    call_id, patch_source = patch_calls[0]
    operation = candidates[0]
    patch_digest = host_patch_digest(patch_source)
    command_digest = stable_hash(
        "host-operation-command-text-v1\0"
        + patch_source.replace("\r\n", "\n").replace("\r", "\n"),
        32,
    )
    if (
        not call_id
        or (
            operation.get("host_input_digest") != patch_digest
            and operation.get("host_command_digest") != command_digest
        )
    ):
        return False
    receipt_digest = rollout_completed_file_change_after_patch(
        records,
        turn_id=turn_id,
        call_id=call_id,
        patch_source=patch_source,
        cwd=cwd,
    )
    if not receipt_digest:
        return False
    if operation.get("host_input_digest") != patch_digest:
        operation["legacy_host_input_digest"] = operation.get("host_input_digest")
        operation["host_input_digest"] = patch_digest
    operation["host_command_digest"] = command_digest
    operation["status"] = "ok"
    operation["category"] = "implementation"
    operation["reconciliation_source"] = (
        "host_rollout_exact_completed_file_change_v1"
    )
    operation["host_receipt_digest"] = receipt_digest
    record_lifecycle_diagnostic(
        state,
        "parent_filechange_reconciled",
        level="info",
        role="confirmed_executor",
        contract_id=state.get("execution_contract_id"),
    )
    return True


def promote_reconciled_parent_review(state: dict[str, Any]) -> bool:
    """Rebuild the parent candidate after change and later verification agree."""
    if (
        not parent_writer_lease_current(state)
        or state.get("plan_state") != "confirmed"
        or state.get("confirmed_plan_digest") != state.get("plan_digest")
        or state.get("executor_state") not in {"running", "verification_required"}
    ):
        return False
    operations = _slice_operations(state)
    change_indexes = [
        index
        for index, item in enumerate(operations)
        if item.get("category")
        in {"implementation", "build_package", "delivery_device"}
        and item.get("status") in SUCCESS_STATUSES
    ]
    if not change_indexes:
        return False
    last_change = max(change_indexes)
    parent_verifications = [
        item
        for index, item in enumerate(operations)
        if index > last_change
        and item.get("category") in {"verification", "evidence"}
        and item.get("status") in SUCCESS_STATUSES
        and item.get("executor_agent_id") is None
    ]
    if not parent_verifications:
        return False
    evidence = slice_operation_evidence(state)
    if not (
        evidence.get("change_evidence")
        and evidence.get("verification_evidence")
        and evidence.get("parent_review_evidence")
    ):
        return False
    operation = parent_verifications[-1]
    current = current_execution_slice(state) or {}
    candidate = stable_hash(
        "workflow-manager-parent-writer-candidate-v1\0"
        + canonical_json(
            {
                "attempt": state.get("executor_attempt"),
                "contract": state.get("execution_contract_id"),
                "operation": operation.get("fingerprint"),
                "slice": current.get("id"),
            }
        ),
        32,
    )
    state["executor_review"] = _safe_executor_review(
        {
            "status": "review_required",
            "attempt": state.get("executor_attempt"),
            "execution_contract_id": state.get("execution_contract_id"),
            "slice_id": current.get("id"),
            "slice_contract_id": slice_contract_id(state),
            "candidate_result_fingerprint": candidate,
            "candidate_agent_fingerprint": stable_hash("parent", 32),
            "candidate_evidence_digest": evidence.get("operation_digest"),
            "child_summary_digest": stable_hash(
                "parent-writer-host-operations\0" + candidate, 32
            ),
            "terminal_status": "completed",
            "terminal_status_source": "host_declared_success",
            "at": utc_now(),
        }
    )
    state["executor_state"] = "verification_required"
    state["executor_failure_kind"] = None
    return True


def reconcile_current_parent_rollout_on_resume(
    payload: dict[str, Any], state: dict[str, Any]
) -> int:
    """Recover current parent writes even when this Hook payload lacks a turn id."""
    trusted = trusted_current_root_rollout(payload, state)
    if trusted is None or not parent_writer_lease_current(state):
        return 0
    records, cwd = trusted
    current = current_execution_slice(state) or {}
    turn_ids = dict.fromkeys(
        str(op["host_event_turn_id"])
        for op in state.get("operations", [])
        if isinstance(op, dict)
        and normalized_key(op.get("tool")) == "applypatch"
        and op.get("status") == "unknown"
        and op.get("executor_agent_id") is None
        and op.get("execution_contract_id") == state.get("execution_contract_id")
        and op.get("slice_id") == current.get("id")
        and op.get("slice_contract_id") == slice_contract_id(state)
        and op.get("host_event_turn_id")
        and op.get("host_input_digest")
    )
    reconciled = 0
    for turn_id in turn_ids:
        pending = [
            op
            for op in state.get("operations", [])
            if isinstance(op, dict)
            and normalized_key(op.get("tool")) == "applypatch"
            and op.get("status") == "unknown"
            and op.get("host_event_turn_id") == turn_id
        ]
        turn_payload = dict(payload)
        turn_payload["turn_id"] = turn_id
        turn_payload["cwd"] = cwd
        reconcile_unknown_operations_from_transcript(turn_payload, state)
        reconciled += sum(
            1 for op in pending if op.get("status") in SUCCESS_STATUSES
        )
    promote_reconciled_parent_review(state)
    return reconciled


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


def completed_parent_review_rollout(
    payload: dict[str, Any], contract_id: str, slice_id: str
) -> dict[str, Any] | None:
    """Prove one exact, completed parent-pass turn from the host rollout.

    This bridge exists for Desktop builds whose Stop payload omits the final
    assistant body/status even though the durable rollout contains it.  It
    deliberately requires the full regular rollout, one same-session meta,
    one exact final marker, and the matching task_complete record.
    """
    session_id = safe_label(payload.get("session_id"), 120)
    if not session_id:
        return None
    records = read_host_rollout_records(payload.get("transcript_path"))
    if not records:
        return None
    metas = [
        item.get("payload")
        for item in records
        if item.get("type") == "session_meta"
        and isinstance(item.get("payload"), dict)
    ]
    if (
        len(metas) != 1
        or metas[0].get("session_id") != session_id
        or metas[0].get("id") != session_id
    ):
        return None

    candidates: list[tuple[int, str, str, str]] = []
    for index, item in enumerate(records):
        event = item.get("payload") or {}
        if (
            item.get("type") != "response_item"
            or not isinstance(event, dict)
            or event.get("type") != "message"
            or event.get("role") != "assistant"
            or event.get("phase") != "final_answer"
        ):
            continue
        content = event.get("content")
        if not (
            isinstance(content, list)
            and len(content) == 1
            and isinstance(content[0], dict)
            and isinstance(content[0].get("text"), str)
        ):
            continue
        message = content[0]["text"]
        match, body, intent = _strict_terminal_marker(
            message, "EXECUTION_REVIEW", EXECUTION_REVIEW_RE
        )
        if not intent:
            continue
        if not match:
            if contract_id in message and f"slice_id={slice_id}" in message:
                return None
            continue
        if match.group(1) != contract_id or match.group(2) != slice_id:
            continue
        if match.group(3) != "passed":
            return None
        turn_id = _host_event_turn_id(event)
        if not turn_id:
            return None
        candidates.append((index, turn_id, message, body))
    if len(candidates) != 1:
        return None
    message_index, turn_id, message, body = candidates[0]
    completions = [
        (index, item)
        for index, item in enumerate(records)
        if item.get("type") == "event_msg"
        and isinstance(item.get("payload"), dict)
        and item["payload"].get("type") == "task_complete"
        and item["payload"].get("turn_id") == turn_id
        and item["payload"].get("last_agent_message") == message
    ]
    if len(completions) != 1 or completions[0][0] <= message_index:
        return None
    completion_index, completion = completions[0]
    for item in records[message_index + 1 : completion_index]:
        event = item.get("payload") or {}
        if not isinstance(event, dict) or _host_event_turn_id(event) != turn_id:
            continue
        if event.get("type") in {"custom_tool_call", "custom_tool_call_output"}:
            return None
        if event.get("type") == "item_completed" and isinstance(event.get("item"), dict):
            if event["item"].get("type") in {"CommandExecution", "FileChange"}:
                return None
    results = rollout_turn_structured_exec_results(records, turn_id)
    if not results:
        return None
    return {
        "body": body,
        "completion_at": completion.get("timestamp"),
        "message": message,
        "result_digests": {digest: status for digest, status, _ in results},
        "turn_id": turn_id,
    }


def reconcile_current_parent_review_on_resume(payload: dict[str, Any], state: dict[str, Any]) -> None:
    current = current_execution_slice(state) or {}
    candidates = [op for op in state.get("operations", []) if op.get("status") == "unknown" and op.get("category") == "verification" and op.get("executor_agent_id") is None and op.get("execution_contract_id") == state.get("execution_contract_id") and op.get("slice_id") == current.get("id") and op.get("slice_contract_id") == slice_contract_id(state) and op.get("host_input_digest") and op.get("host_event_turn_id")]
    for turn_id in dict.fromkeys(str(item["host_event_turn_id"]) for item in candidates):
        reconcile_unknown_operations_from_transcript(
            {**payload, "turn_id": turn_id}, state
        )


def reconcile_current_executor_rollout_on_resume(payload: dict[str, Any], state: dict[str, Any]) -> None:
    """Bounded child-rollout repair for current executor operations only."""
    current = current_execution_slice(state) or {}; contract = state.get("execution_contract_id")
    review = _safe_executor_review(state.get("executor_review"))
    current_attempt = safe_int(state.get("executor_attempt"))
    candidate_agent = _fingerprint32(review.get("candidate_agent_fingerprint"))
    starts = [item for item in state.get("subagents", []) if item.get("event") == "start" and item.get("role") == "confirmed_executor" and item.get("contract_id") == contract and item.get("slice_id") == current.get("id") and safe_int(item.get("attempt")) == current_attempt and item.get("agent_id") and item.get("at") and (not candidate_agent or stable_hash(str(item.get("agent_id")), 32) == candidate_agent)]
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
    turn_ids = dict.fromkeys(
        str(op["host_event_turn_id"])
        for op in state.get("operations", [])
        if isinstance(op, dict)
        and op.get("executor_agent_id") == agent
        and op.get("execution_contract_id") == contract
        and op.get("slice_id") == current.get("id")
        and op.get("host_event_turn_id")
        and op.get("host_input_digest")
        and (
            op.get("status") == "unknown"
            or (
                op.get("status") in SUCCESS_STATUSES
                and not op.get("reconciliation_source")
            )
        )
    )
    for turn_id in turn_ids:
        reconcile_unknown_operations_from_transcript(
            {"turn_id": turn_id, "transcript_path": str(candidates[0])}, state
        )


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
    if any(str(item.get("at") or "") > checkpoint_at and item.get("event") in {"request", "start", *TERMINAL_SUBAGENT_EVENTS} for item in state.get("subagents", []) if isinstance(item, dict)):
        return False
    if not transcript_has_exact_compaction_gate(payload.get("transcript_path"), session_id, safe_int(checkpoint.get("window_number")), current["id"]):
        return False
    assessor_groups: dict[str, list[dict[str, Any]]] = {}
    for item in state.get("subagents", []):
        if isinstance(item, dict) and item.get("role") == "high_assessor" and item.get("objective_fingerprint") == objective["fingerprint"] and item.get("contract_id"):
            assessor_groups.setdefault(str(item["contract_id"]), []).append(item)
    valid_assessors: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for binding, items in assessor_groups.items():
        requests = [item for item in items if item.get("event") == "request"]
        starts = [item for item in items if item.get("event") == "start"]
        if len(requests) != 1 or len(starts) != 1:
            continue
        request, started = requests[0], starts[0]
        attempt = safe_sequence(request.get("attempt"))
        probe = json.loads(json.dumps(state))
        probe.update({
            "objective": objective,
            "assessor_binding_id": binding,
            "assessor_attempt": attempt,
            "assessor_agent_id": None,
            "assessor_model": request.get("model"),
            "assessor_reasoning_effort": request.get("reasoning_effort"),
            "assessor_fork_turns": request.get("fork_turns"),
            "assessor_observed_effective": True,
            "assessor_observed_model": started.get("model"),
            "assessor_observed_reasoning_effort": started.get("reasoning_effort"),
            "assessor_start_observed": "full",
            "assessor_observation_source": started.get("observation_source"),
        })
        _, lifecycle_error = original_assessor_lifecycle(probe)
        if lifecycle_error:
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
        "executor_attempt": safe_sequence(checkpoint.get("executor_attempt")), "executor_failure_kind": None,
        "assessor_generation": max(safe_sequence(state.get("assessor_generation")), 1), "assessor_binding_id": assessor_binding, "assessor_state": "hard_plan_ready", "assessor_attempt": safe_sequence(assessor_request.get("attempt")),
        "assessor_agent_id": None, "assessor_model": assessor_request["model"], "assessor_reasoning_effort": assessor_request["reasoning_effort"], "assessor_fork_turns": "1",
        "assessor_input_fingerprint": objective["fingerprint"], "assessor_failure_kind": None, "assessor_observed_effective": True,
        "assessor_observed_model": assessor_started.get("model") or assessor_request["model"], "assessor_observed_reasoning_effort": assessor_started.get("reasoning_effort") or assessor_request["reasoning_effort"], "assessor_start_observed": "full", "assessor_observation_source": assessor_started.get("observation_source"),
    })
    lifecycle, lifecycle_error = original_assessor_lifecycle(candidate)
    if lifecycle_error or lifecycle.get("binding_id") != assessor_binding:
        return False
    candidate["model_profile"] = confirmed_executor_model_profile(candidate)
    if not verify_plan_artifact(candidate, payload) or execution_contract_id(candidate) != contract:
        return False
    candidate.setdefault("guards", []).append({"at": utc_now(), "turn_id": safe_label(payload.get("turn_id"), 120) if payload.get("turn_id") else None, "kind": "compaction_gate_resume_repair", "action": "advise", "fingerprint": fingerprint})
    state.clear(); state.update(candidate)
    return True


def current_slice_host_change_digest(state: dict[str, Any]) -> str | None:
    current = current_execution_slice(state) or {}; contract = state.get("execution_contract_id")
    facts=[{k:op.get(k) for k in ("fingerprint","host_input_digest","legacy_host_input_digest","reconciliation_source","host_event_turn_id","status")} for op in state.get("operations",[]) if isinstance(op,dict) and op.get("execution_contract_id")==contract and op.get("slice_id")==current.get("id") and op.get("slice_contract_id")==slice_contract_id(state) and op.get("executor_agent_id") and op.get("status") in SUCCESS_STATUSES and op.get("category") in {"implementation","build_package","delivery_device"} and op.get("reconciliation_source")]
    return stable_hash("current-slice-host-change-v1\0"+canonical_json(facts),32) if facts else None


def repair_prior_slice_change_status_omission_once(state: dict[str, Any]) -> bool:
    """Recover one sealed change whose V2 child Stop omitted terminal status.

    The repair never converts an arbitrary attempted write into success.  It
    requires a uniquely bound full-Start executor, its exact succeeded result,
    a missing host terminal status, the already-passed parent review, and a
    host-recorded implementation operation with no conflicting error.
    """
    slices = _safe_execution_slices(state.get("execution_slices"))
    if any(item.get("status") == "passed" and item.get("change_evidence") for item in slices.get("items", [])):
        return True
    contract = safe_fingerprint(state.get("execution_contract_id"))
    if not contract:
        return False
    candidates: list[tuple[int, dict[str, Any], list[dict[str, Any]]]] = []
    for index, item in enumerate(slices.get("items", [])):
        if not (
            item.get("status") == "passed"
            and item.get("completion_digest")
            and item.get("review_digest")
            and item.get("operation_digest")
            and item.get("verification_evidence")
        ):
            continue
        starts = [
            event
            for event in state.get("subagents", [])
            if isinstance(event, dict)
            and event.get("event") == "start"
            and event.get("role") == "confirmed_executor"
            and event.get("contract_id") == contract
            and event.get("slice_id") == item.get("id")
            and event.get("agent_id")
            and event.get("host_accepted") is True
            and event.get("start_observed") == "full"
        ]
        if len(starts) != 1:
            continue
        start = starts[0]
        stops = [
            event
            for event in state.get("subagents", [])
            if isinstance(event, dict)
            and event.get("event") == "stop"
            and event.get("role") == "confirmed_executor"
            and event.get("contract_id") == contract
            and event.get("slice_id") == item.get("id")
            and event.get("agent_id") == start.get("agent_id")
            and event.get("execution_result_contract_match") is True
            and event.get("execution_result_outcome") == "succeeded"
            and event.get("terminal_status") == "missing"
            and event.get("terminal_status_source") == "host_missing"
        ]
        if len(stops) != 1:
            continue
        slice_contract = start.get("slice_contract_id")
        operations = [
            operation
            for operation in state.get("operations", [])
            if isinstance(operation, dict)
            and operation.get("execution_contract_id") == contract
            and operation.get("slice_id") == item.get("id")
            and operation.get("slice_contract_id") == slice_contract
        ]
        changes = [
            operation
            for operation in operations
            if operation.get("executor_agent_id") == start.get("agent_id")
            and operation.get("category") in {"implementation", "build_package", "delivery_device"}
            and operation.get("host_input_digest")
        ]
        parent_verification = [
            operation
            for operation in operations
            if operation.get("executor_agent_id") is None
            and operation.get("category") in {"verification", "evidence"}
            and operation.get("status") in SUCCESS_STATUSES
        ]
        if (
            changes
            and parent_verification
            and not any(str(operation.get("status") or "").startswith("error") for operation in changes)
        ):
            candidates.append((index, start, changes))
    if len(candidates) != 1:
        return False
    index, start, changes = candidates[0]
    fingerprint = stable_hash(
        "accepted-slice-change-status-omission-v1\0"
        + canonical_json(
            {
                "agent": stable_hash(str(start.get("agent_id")), 32),
                "completion": slices["items"][index].get("completion_digest"),
                "contract": contract,
                "inputs": [operation.get("host_input_digest") for operation in changes],
                "slice": slices["items"][index].get("id"),
            }
        ),
        32,
    )
    guards = state.setdefault("guards", [])
    if any(
        isinstance(item, dict)
        and item.get("kind") == "accepted_slice_change_status_omission_repair"
        and item.get("fingerprint") == fingerprint
        for item in guards
    ):
        return False
    slices["items"][index]["change_evidence"] = True
    state["execution_slices"] = slices
    guards.append(
        {
            "at": utc_now(),
            "turn_id": None,
            "kind": "accepted_slice_change_status_omission_repair",
            "action": "repair",
            "fingerprint": fingerprint,
        }
    )
    return True


def resume_completed_parent_review_once(
    payload: dict[str, Any], state: dict[str, Any]
) -> bool:
    """Seal a host-completed parent pass that an incomplete Stop misclassified."""
    review = _safe_executor_review(state.get("executor_review"))
    current = current_execution_slice(state)
    contract = safe_fingerprint(state.get("execution_contract_id"))
    if not (
        payload.get("hook_event_name") == "SessionStart"
        and str(payload.get("source") or "") in {"resume", "compact"}
        and state.get("plan_state") == "confirmed"
        and state.get("confirmed_plan_digest") == state.get("plan_digest")
        and state.get("executor_state") == "recovery_required"
        and state.get("executor_failure_kind") == "verification_failed"
        and str(state.get("execution_profile_version")) == EXECUTION_PROFILE_VERSION
        and safe_sequence(state.get("executor_attempt")) > 0
        and review.get("status") == "failed"
        and review.get("attempt") == safe_sequence(state.get("executor_attempt"))
        and contract
        and contract == execution_contract_id(state)
        and current
        and current.get("status") == "pending"
        and review.get("execution_contract_id") == contract
        and review.get("slice_id") == current.get("id")
        and review.get("slice_contract_id") == slice_contract_id(state)
        and review.get("candidate_result_fingerprint")
        and review.get("candidate_evidence_digest")
    ):
        return False
    proof = completed_parent_review_rollout(payload, contract, current["id"])
    if not proof:
        return False
    result_digests = proof["result_digests"]
    parent_operations = [
        operation
        for operation in state.get("operations", [])
        if isinstance(operation, dict)
        and operation.get("host_event_turn_id") == proof["turn_id"]
        and operation.get("executor_agent_id") is None
        and operation.get("execution_contract_id") == contract
        and operation.get("slice_id") == current.get("id")
        and operation.get("slice_contract_id") == slice_contract_id(state)
        and operation.get("category") in {"verification", "evidence"}
        and operation.get("host_input_digest")
    ]
    if not parent_operations or any(
        operation.get("status") not in SUCCESS_STATUSES
        or result_digests.get(operation.get("host_input_digest")) not in SUCCESS_STATUSES
        for operation in parent_operations
    ):
        return False
    operation_indexes = [state.get("operations", []).index(operation) for operation in parent_operations]
    if any(
        operation.get("executor_agent_id")
        or operation.get("category") in {"implementation", "build_package", "delivery_device"}
        for operation in state.get("operations", [])[max(operation_indexes) + 1 :]
        if isinstance(operation, dict)
    ):
        return False
    completion_at = str(proof.get("completion_at") or "")
    if completion_at and any(
        str(event.get("at") or "") > completion_at
        and event.get("event") in {"request", "start", *TERMINAL_SUBAGENT_EVENTS}
        for event in state.get("subagents", [])
        if isinstance(event, dict)
    ):
        return False

    operation_evidence = slice_operation_evidence(state)
    if not (
        operation_evidence.get("verification_evidence")
        and operation_evidence.get("parent_review_evidence")
        and operation_evidence.get("operation_digest")
    ):
        return False
    slices = _safe_execution_slices(state.get("execution_slices"))
    item_index = safe_int(slices.get("current_index")) - 1
    if item_index < 0 or item_index >= len(slices.get("items", [])):
        return False
    if item_index + 1 == slices.get("count") and not repair_prior_slice_change_status_omission_once(state):
        return False
    slices = _safe_execution_slices(state.get("execution_slices"))
    item = slices["items"][item_index]
    prior_chain = slices["completed_chain"]
    evidence_digest = host_evidence_digest(
        domain="parent-review-v1",
        state=state,
        agent_id="parent",
        request_fingerprint=review.get("candidate_result_fingerprint"),
        body_without_marker=proof["body"],
        outcome="passed",
        candidate_review=review,
    )
    completion_digest = stable_hash(
        "workflow-manager-slice-completion-v1\0"
        + canonical_json(
            {
                "candidate_evidence_digest": review.get("candidate_evidence_digest"),
                "execution_contract_id": contract,
                "operation_digest": operation_evidence["operation_digest"],
                "review_evidence_digest": evidence_digest,
                "slice_contract_id": slice_contract_id(state),
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
    expected_chain = recompute_completed_slice_chain(slices)
    if not (
        slices["current_index"] == slices["count"] + 1
        and all(
            item.get("status") == "passed" and item.get("verification_evidence")
            for item in slices["items"]
        )
        and any(item.get("change_evidence") for item in slices["items"])
        and expected_chain
        and expected_chain == slices.get("completed_chain")
    ):
        return False
    operation_digests = [item["operation_digest"] for item in slices["items"]]
    change_digests = [
        item["operation_digest"]
        for item in slices["items"]
        if item.get("change_evidence")
    ]
    state["execution_slices"] = slices
    state["last_execution_baseline"] = {
        "baseline_id": stable_hash(
            "workflow-manager-sliced-baseline-v1\0"
            + canonical_json(
                {"chain": expected_chain, "contract": contract, "operations": operation_digests}
            ),
            32,
        ),
        "objective_fingerprint": state.get("objective", {}).get("fingerprint"),
        "plan_digest": state.get("plan_digest"),
        "execution_contract_id": contract,
        "change_set_digest": stable_hash(canonical_json(change_digests), 32),
        "verification_digest": stable_hash(canonical_json(operation_digests), 32),
        "acceptance_status": "passed",
    }
    review.update(
        {
            "status": "passed",
            "review_evidence_digest": evidence_digest,
            "digest_profile": EVIDENCE_DIGEST_PROFILE,
            "digest_source": EVIDENCE_DIGEST_SOURCE,
            "at": utc_now(),
        }
    )
    state["executor_review"] = review
    state["executor_state"] = "succeeded"
    state["executor_failure_kind"] = None
    state["model_profile"] = confirmed_executor_model_profile(state)
    state["causal_review"] = _safe_causal_review(None)
    parent_lease = _safe_parent_writer_lease(state.get("parent_writer_lease"))
    if (
        parent_lease.get("status") == "live"
        and parent_lease.get("execution_contract_id") == contract
        and parent_lease.get("slice_id") == current.get("id")
        and parent_lease.get("slice_contract_id") == review.get("slice_contract_id")
        and parent_lease.get("attempt")
        == safe_sequence(state.get("executor_attempt"))
    ):
        parent_lease["status"] = "sealed"
        state["parent_writer_lease"] = parent_lease
    repair_fingerprint = stable_hash(
        f"completed-parent-review-rollout-v1\0{contract}\0{current['id']}\0{proof['turn_id']}\0{evidence_digest}",
        32,
    )
    state.setdefault("guards", []).append(
        {
            "at": utc_now(),
            "turn_id": safe_label(payload.get("turn_id"), 120) if payload.get("turn_id") else None,
            "kind": "completed_parent_review_rollout_repair",
            "action": "repair",
            "fingerprint": repair_fingerprint,
        }
    )
    return True


def resume_failed_review_evidence_once(payload: dict[str, Any], state: dict[str, Any]) -> bool:
    """One bounded repair for the known host Stop-status omission; never trusts child text."""
    review = _safe_executor_review(state.get("executor_review"))
    current = current_execution_slice(state)
    contract = safe_fingerprint(state.get("execution_contract_id"))
    if not (
        state.get("executor_state") == "recovery_required"
        and state.get("executor_failure_kind") == "verification_failed"
        and safe_sequence(state.get("executor_attempt")) > 0
        and review.get("status") == "failed"
        and review.get("attempt") == safe_sequence(state.get("executor_attempt"))
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
    if not (state.get("executor_state")=="recovery_required" and state.get("executor_failure_kind")=="verification_failed" and safe_sequence(state.get("executor_attempt"))>0 and review.get("status")=="failed" and review.get("attempt")==safe_sequence(state.get("executor_attempt")) and evidence and review.get("candidate_result_fingerprint") and review.get("candidate_evidence_digest") and current.get("status")=="pending" and safe_fingerprint(contract) and contract==execution_contract_id(state) and review.get("execution_contract_id")==contract and review.get("slice_id")==current.get("id") and review.get("slice_contract_id")==slice_contract_id(state)):
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

DAILY_EXACT_PATTERNS = (
    ("daily_weather", r"(?:天气|气温|下雨|空气质量|weather|forecast)"),
    ("daily_report", r"(?:生成|整理|写|帮我写|汇总).{0,10}(?:日报|周报|月报|daily report|weekly report)"),
    ("daily_cleanup", r"(?:清理|删除|整理).{0,16}(?:电脑|磁盘|缓存|垃圾文件|临时文件|重复文件|computer|disk|cache|junk|temporary files?)"),
    ("daily_chat", r"^(?:你好|您好|嗨|hello|hi|聊聊|陪我聊天|谢谢|早上好|晚上好)[!！。,.，\s]*$"),
)
WORK_STRONG_PATTERNS = (
    ("work_workflow_manager_maintenance", r"(?:workflow[ -]?manager|工作流管理器).{0,80}(?:修复|fix|测试|test|版本|release|发布|publish|代码)"),
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


CRITICAL_HARD_PATTERNS = (
    (
        "critical_workflow_manager_versioned_release",
        r"(?:workflow[ -]?manager|工作流管理器).{0,120}(?:插件|plugin|hook|skill).{0,120}"
        r"(?:发布|publish|release).{0,48}(?:\b(?:v?\d+\.\d+\.\d+)\b|版本)"
        r"|(?:发布|publish|release).{0,120}(?:workflow[ -]?manager|工作流管理器).{0,120}"
        r"(?:插件|plugin|hook|skill).{0,48}(?:\b(?:v?\d+\.\d+\.\d+)\b|版本)"
        r"|(?:workflow[ -]?manager|工作流管理器).{0,120}(?:发布|publish|release).{0,48}"
        r"(?:\b(?:v?\d+\.\d+\.\d+)\b|版本)",
    ),
    (
        "critical_irreversible_or_production",
        r"(?:生产发布|production\s+(?:release|deployment)|不可逆|irreversible|"
        r"数据丢失|data\s+loss|安全漏洞|security\s+(?:incident|vulnerability)|"
        r"销毁|wipe|erase|rotate\s+(?:production\s+)?credentials?)",
    ),
    (
        "critical_architecture_delivery",
        r"(?:从零开发|完整开发|zero[- ]downtime|零停机|完整(?:系统|应用|平台)(?:开发|迁移)|"
        r"from\s+scratch.{0,32}(?:system|application|platform))",
    ),
    (
        "critical_host_continuity",
        r"(?:host\s+compaction|真实(?:宿主)?压缩|压缩).{0,96}"
        r"(?:same[- ]session|同一会话|同会话|resume|恢复)",
    ),
    (
        "critical_host_lifecycle_acceptance",
        r"(?:(?:hard|困难|复杂).{0,64}(?:宿主|host).{0,64}(?:验收|acceptance)|"
        r"(?:宿主|host).{0,64}(?:验收|acceptance).{0,64}(?:hard|困难|复杂)|"
        r"host[- ](?:lifecycle|continuity)[- ]acceptance)",
    ),
)
HARD_WORK_PATTERNS = (
    ("hard_unknown_root_cause", r"(?:根因未知|未知根因|原因不明|反复|间歇|偶现|复现|root cause|intermittent|flaky|keeps|repeated)"),
    ("hard_cross_module", r"(?:跨模块|多个模块|多模块|跨组件|多个组件|framework.{0,28}systemui|settings.{0,28}framework|cross[- ]module|multiple modules?|several modules?)"),
    ("hard_architecture", r"(?:架构|离线同步|后台同步|认证系统|rollback|migration|迁移)"),
    ("hard_host_continuity", r"(?:host\s+compaction|真实(?:宿主)?压缩|压缩).{0,96}(?:same[- ]session|同一会话|同会话|resume|恢复)"),
    ("hard_external_chain", r"(?:编译|构建|compile|build).{0,60}(?:部署|安装|烧录|刷机|实机|deploy|install|flash|device)"),
    ("hard_shared_resource", r"(?:唯一|同一|共享|only|single|same|shared).{0,20}(?:设备|构建服务器|账号|资源|device|build server|account|resource)"),
)
HARD_SIGNAL_GROUPS = {
    "hard_unknown_root_cause": "diagnosis",
    "hard_cross_module": "scope",
    "hard_architecture": "scope",
    "hard_host_continuity": "continuity",
    "hard_external_chain": "external_state",
    "hard_device_change": "external_state",
    "hard_shared_resource": "coordination",
    "hard_shared_or_ordered": "coordination",
    "hard_three_phase_chain": "workflow",
}
PRIMARY_HARD_SIGNAL_GROUPS = {"diagnosis", "scope", "continuity"}
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
        # Explicitly declaring that no irreversible action exists is scope
        # metadata, not evidence of irreversible risk.
        critical_text = re.sub(
            r"(?:irreversible[_ -]?action|不可逆(?:外部)?动作)\s*(?:[:=：]\s*)?"
            r"(?:none|no|false|无|否|不适用|n/?a)(?=$|[\s,;，；])",
            "",
            lower,
            flags=re.I,
        )
        critical_codes = [
            code
            for code, pattern in CRITICAL_HARD_PATTERNS
            if re.search(pattern, critical_text, re.I)
        ]
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
            critical_codes = []
            hard_codes = []
        domain_codes = set(as_list(domain.get("domain_rule_codes")))
        phases = set(as_list(route.get("phase_hints")))
        if domain_codes & {"work_device_bug", "work_device_customization"}:
            hard_codes.append("hard_device_change")
        if len(phases) >= 3:
            hard_codes.append("hard_three_phase_chain")
        if route.get("dependency_signal") in {"shared_resource", "ordered_shared"}:
            hard_codes.append("hard_shared_or_ordered")
        signal_groups = {
            HARD_SIGNAL_GROUPS[code]
            for code in hard_codes
            if code in HARD_SIGNAL_GROUPS
        }
        qualified_hard = bool(
            critical_codes
            or (
                len(signal_groups) >= 2
                and bool(signal_groups & PRIMARY_HARD_SIGNAL_GROUPS)
            )
        )
        if question_only and not qualified_hard:
            difficulty = "simple"
            confidence = "high"
            rule_codes = ["simple_explanation_request"]
        elif qualified_hard:
            difficulty = "hard"
            confidence = "high"
            rule_codes = list(dict.fromkeys([*critical_codes, *hard_codes]))[:8]
        else:
            simple_codes = [
                code for code, pattern in SIMPLE_WORK_PATTERNS if re.search(pattern, lower, re.I)
            ]
            bounded_shape = len(phases) <= 2
            if bounded_shape:
                simple_codes.append("simple_bounded_scope")
            if simple_codes:
                difficulty = "simple"
                confidence = "high" if bounded_shape else "medium"
                rule_codes = list(dict.fromkeys(simple_codes))[:8]
            else:
                # Uncertainty is not itself evidence of difficulty.  Start with one
                # bounded local diagnosis and promote only when runtime evidence adds
                # a second independent signal or a critical-risk signal.
                difficulty = "simple"
                confidence = "medium"
                rule_codes = [
                    "simple_bounded_diagnostic",
                    *list(dict.fromkeys(hard_codes))[:3],
                ]
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


def decorate_route(route: dict[str, Any]) -> dict[str, Any]:
    """Normalize the retained authorization decision.

    Codex now owns ordinary task shaping and subagent scheduling.  Legacy route
    fields remain zeroed only so Schema <=26 state can migrate without
    manufacturing a new execution policy.
    """
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
    phases = list(dict.fromkeys(str(item) for item in as_list(result.get("phase_hints")) if item))[:8]
    result["phase_hints"] = phases
    for obsolete in (
        "label",
        "score",
        "future_token_range",
        "recommended_agent_cap",
        "parallel_signal",
        "delegation_gate",
        "readiness_signal",
        "dependency_signal",
        "meta_delegation",
        "delegation_opt_out",
        "lane_signal",
        "dependency_hint",
        "workflow_shape",
        "execution_order",
        "agent_mode",
    ):
        result.pop(obsolete, None)
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
            "phase_hints": [],
            "route_source": "identity_preflight",
        }
    )


def classify_prompt(prompt: str) -> dict[str, Any]:
    normalized = prompt.strip()
    if identity_preflight_prompt(normalized):
        return identity_preflight_route(normalized)
    domain = classify_task_domain(normalized)
    phases = phase_hints(normalized)
    dependency_signal = prompt_dependency_signal(normalized)
    route = {
        **domain,
        "dependency_signal": dependency_signal,
        "phase_hints": phases,
        "route_source": "authorization_classifier",
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
    return normalized == "highassessor" or normalized.startswith("highassessor")


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
    if not binding or len(binding) != 32:
        return None
    if state.get("assessor_state") in {"spawn_pending", "running"}:
        reserved = [
            item
            for item in as_list(state.get("subagents"))
            if isinstance(item, dict)
            and item.get("event") == "request"
            and item.get("role") == "high_assessor"
            and item.get("contract_id") == binding
            and safe_sequence(item.get("attempt"))
            == safe_sequence(state.get("assessor_attempt"))
            and item.get("task_name")
        ]
        if len(reserved) == 1:
            return safe_label(reserved[0]["task_name"], 120)
    sequence = (
        safe_sequence(state.get("assessor_attempt"))
        if state.get("assessor_state") in {"spawn_pending", "running"}
        else next_sequence(state.get("assessor_attempt"))
    )
    return f"assessor_{binding[:12]}_q{sequence}"


def executor_request_sequence(state: dict[str, Any]) -> int:
    current = safe_sequence(state.get("executor_attempt"))
    return (
        current
        if state.get("executor_state") in {"spawn_pending", "running"} and current > 0
        else next_sequence(current)
    )


def bound_executor_task_name(state: dict[str, Any]) -> str | None:
    contract = safe_fingerprint(state.get("execution_contract_id"))
    if not contract or len(contract) != 32:
        return None
    if state.get("executor_state") in {"spawn_pending", "running"}:
        reserved = [
            item
            for item in as_list(state.get("subagents"))
            if isinstance(item, dict)
            and item.get("event") == "request"
            and item.get("role") == "confirmed_executor"
            and item.get("contract_id") == contract
            and safe_sequence(item.get("attempt"))
            == safe_sequence(state.get("executor_attempt"))
            and item.get("task_name")
        ]
        if len(reserved) == 1:
            return safe_label(reserved[0]["task_name"], 120)
    sequence = executor_request_sequence(state)
    prefix = (
        "recovery"
        if state.get("executor_state") == "recovery_required"
        else "executor"
    )
    return f"{prefix}_{contract[:12]}_q{sequence}"


def verification_recovery_evidence_digest(
    task_name: str | None, state: dict[str, Any]
) -> str | None:
    contract = safe_fingerprint(state.get("execution_contract_id"))
    if not task_name or not contract or len(contract) != 32:
        return None
    review = _safe_executor_review(state.get("executor_review"))
    digest = _fingerprint32(review.get("review_evidence_digest"))
    expected = bound_executor_task_name(state)
    return digest if digest and task_name == expected else None


def executor_verification_recovery_pending(state: dict[str, Any]) -> bool:
    review = _safe_executor_review(state.get("executor_review"))
    contract = safe_fingerprint(state.get("execution_contract_id"))
    if (
        not contract
        or safe_sequence(state.get("executor_attempt")) <= 0
        or review.get("execution_contract_id") != contract
        or review.get("attempt") != safe_sequence(state.get("executor_attempt"))
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
        state.get("executor_state") == "recovery_required"
        and state.get("executor_failure_kind") == "verification_failed"
        and review.get("status") == "failed"
        and review.get("review_evidence_digest")
    )


def executor_recovery_has_fresh_child_boundary(state: dict[str, Any]) -> bool:
    """Require the immediately prior sequence to be terminal before recovery."""
    sequence = safe_sequence(state.get("executor_attempt"))
    if sequence <= 0:
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
        and safe_sequence(group["request"].get("attempt")) == sequence
    ]
    if any(group.get("state") in {"live", "result_pending"} for group in matching):
        return False
    if any(group.get("state") == "terminal" for group in matching):
        return True
    # A rejected spawn has no child to terminate. Its explicit rejected Post is
    # nevertheless a complete sequence boundary.
    return any(
        isinstance(group.get("request"), dict)
        and group["request"].get("host_accepted") is False
        for group in matching
    )


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


def bound_spawn_envelope_conflict(payload: dict[str, Any]) -> str | None:
    """Reject host-incompatible fork controls before reserving a writer.

    Current Desktop's collaboration API represents bounded history with
    ``fork_turns``.  Supplying the legacy ``fork_context`` switch or an
    explicit ``agent_type`` alongside that request can be rejected by the host
    before PostToolUse.  Reserving first would leave an unprovable orphan, so
    detect only those structured option keys (never prose) up front.
    """
    root = payload.get("tool_input") if "tool_input" in payload else payload
    pending: list[tuple[Any, int]] = [(root, 0)]
    seen: set[int] = set()
    nodes = 0
    while pending:
        value, depth = pending.pop(0)
        nodes += 1
        if nodes > SUBAGENT_REQUEST_MAX_NODES or depth > SUBAGENT_REQUEST_MAX_DEPTH:
            return "spawn envelope exceeds bounded inspection limits"
        if isinstance(value, str):
            if not _json_container_text(value):
                continue
            try:
                value = json.loads(value)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        if isinstance(value, list):
            if len(value) > SUBAGENT_REQUEST_MAX_LIST_ITEMS:
                return "spawn envelope exceeds bounded inspection limits"
            pending.extend((item, depth + 1) for item in value)
            continue
        if not isinstance(value, dict):
            continue
        identity = id(value)
        if identity in seen:
            continue
        seen.add(identity)
        for key, child in list(value.items())[:SUBAGENT_REQUEST_MAX_LIST_ITEMS]:
            normalized = normalized_key(key)
            if normalized == "agenttype" and child not in (None, ""):
                return "omit agent_type when fork_turns is used"
            if normalized == "forkcontext":
                return "omit legacy fork_context; use only fork_turns=1"
            if normalized in {
                "args", "arguments", "input", "toolinput", "content"
            }:
                pending.append((child, depth + 1))
    return None


def confirmed_executor_request(
    payload: dict[str, Any], state: dict[str, Any]
) -> tuple[bool, str | None]:
    """Validate only host-visible fields that current profile v10 cannot infer."""

    if str(state.get("execution_profile_version")) != EXECUTION_PROFILE_VERSION:
        return False, "missing current execution profile"
    resolution = subagent_request_resolution(payload)
    if resolution.get("error"):
        return False, f"executor {resolution['error']}"
    if envelope_error := bound_spawn_envelope_conflict(payload):
        return False, f"executor spawn envelope conflict: {envelope_error}"
    if (
        state.get("plan_state") != "confirmed"
        or state.get("confirmed_plan_digest") != state.get("plan_digest")
        or state.get("execution_contract_id") != execution_contract_id(state)
        or state.get("executor_state") not in {"spawn_required", "recovery_required"}
        or not current_execution_slice(state)
    ):
        return False, "missing confirmed execution contract"
    if writer_liveness_blocks_successor(state):
        return False, "writer inventory is live or unknown; do not overlap a successor"

    candidates = subagent_request_candidates(payload)
    raw_task_name = candidates[0].get("task_name") if candidates else None
    if not isinstance(raw_task_name, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,119}", raw_task_name
    ):
        return False, "executor requires one safe ASCII task_name"

    options = subagent_request_options(payload)
    model = str(options.get("model") or "").strip()
    effort = str(options.get("reasoning_effort") or "").strip().lower()
    fork_turns = str(options.get("fork_turns") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{1,79}", model):
        return False, "model_unavailable"
    if fork_turns != "1":
        return False, "every bound executor requires fork_turns=1"

    recovery = state.get("executor_state") == "recovery_required"
    if recovery:
        if not executor_recovery_has_fresh_child_boundary(state):
            return False, "executor recovery requires a terminal prior sequence"
        if not pending_recovery_reservation_for_state(state):
            inline_recovery, inline_error = parse_recovery_contract(
                subagent_request_text(payload),
                state,
                opaque=subagent_request_visibility(payload) == "opaque_v2",
            )
            if not inline_recovery:
                return False, inline_error or "recovery facts are incomplete"

    profile = expected_executor_profile(state)
    if profile.get("error"):
        return False, str(profile["error"])
    if profile.get("profile") == "work_executor_highest_available":
        if (
            model != str(profile.get("model") or "")
            or effort != str(profile.get("reasoning_effort") or "")
        ):
            return False, "executor profile does not match the typed recovery/session policy"
    elif model == RECOVERY_EXECUTOR_MODEL or effort != "medium":
        return False, "normal executor requires a lower-tier model at medium"
    return True, None


def confirmed_assessor_request(
    payload: dict[str, Any], state: dict[str, Any]
) -> tuple[bool, str | None]:
    """Treat assessor prose and task naming as opaque; validate host profile only."""

    if str(state.get("execution_profile_version")) != EXECUTION_PROFILE_VERSION:
        return False, "missing current assessment profile"
    resolution = subagent_request_resolution(payload)
    if resolution.get("error"):
        return False, f"assessor {resolution['error']}"
    if envelope_error := bound_spawn_envelope_conflict(payload):
        return False, f"assessor spawn envelope conflict: {envelope_error}"
    if state.get("assessor_state") not in {"spawn_required", "recovery_required"}:
        return False, "duplicate assessor"
    if writer_liveness_blocks_successor(state):
        return False, "writer inventory is live or unknown; do not overlap a successor"

    candidates = subagent_request_candidates(payload)
    raw_task_name = candidates[0].get("task_name") if candidates else None
    if not isinstance(raw_task_name, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,119}", raw_task_name
    ):
        return False, "assessor requires one safe ASCII task_name"

    options = subagent_request_options(payload)
    model = str(options.get("model") or "").strip()
    if model != RECOVERY_EXECUTOR_MODEL:
        return False, "assessor requires the current highest available model"
    if (
        str(options.get("reasoning_effort") or "").lower()
        != requested_assessor_reasoning_effort(state)
    ):
        return False, "assessor reasoning_effort does not match session policy"
    if str(options.get("fork_turns") or "") != "1":
        return False, "every bound assessor requires fork_turns=1"
    return True, None


def subagent_request_text(payload: dict[str, Any]) -> str:
    candidates = subagent_request_candidates(payload)
    value = candidates[0].get("message") if candidates else None
    return value if isinstance(value, str) else ""


def subagent_request_has_assessor_intent(payload: dict[str, Any]) -> bool:
    return bool(subagent_request_resolution(payload).get("assessor_intent"))


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
}


GIT_TAG_QUERY_OPTIONS = {
    "-l", "--list", "-n", "--points-at", "--contains", "--no-contains",
    "--merged", "--no-merged", "-v", "--verify", "--format", "--sort",
    "--column", "--color", "--ignore-case",
}
GIT_TAG_WRITE_OPTIONS = {
    "-a", "--annotate", "-s", "--sign", "-u", "--local-user", "-m",
    "--message", "-F", "--file", "-e", "--edit", "--trailer", "-d",
    "--delete", "-f", "--force", "--create-reflog",
}


def git_tag_disposition(command: str) -> str:
    """Classify every visible `git tag` invocation conservatively.

    Bare `git tag` is a query.  A tag name is a create unless list/verify mode
    is explicit; option values, dynamic argv and mixed query/write forms are
    intentionally unknown (and therefore mutation-gated).
    """
    invocations = []
    for view in command_views(command):
        masked = shell_syntax_view(view)
        invocations.extend(GIT_INVOCATION_RE.finditer(masked))
    # Nested shell views repeat their outer invocation.  Multiple visible git
    # starts in the original command are still an unsafe composite request.
    if git_invocation_count(command) > 1:
        return "mutation"
    invocation = _git_invocation(command)
    if not invocation or invocation[1].group(1).lower() != "tag":
        return "none"
    view, match = invocation
    segment = _outer_shell_segment(view, match.start())
    try:
        argv = shlex.split(segment, posix=True)
    except (TypeError, ValueError):
        return "unknown"
    try:
        git_index = next(i for i, value in enumerate(argv) if value.lower() in {"git", "git.exe"})
    except StopIteration:
        return "unknown"
    args = argv[git_index + 1:]
    # Strip only unambiguous global options before the `tag` command.
    index = 0
    while index < len(args):
        item = args[index]
        if item in {"-C", "-c", "--git-dir", "--work-tree"}:
            index += 2
        elif item.startswith(("-C", "-c", "--git-dir=", "--work-tree=")):
            index += 1
        else:
            break
    if index >= len(args) or args[index].lower() != "tag":
        return "unknown"
    tag_args = args[index + 1:]
    if any(any(marker in value for marker in ("$", "`", "\x00")) for value in tag_args):
        return "unknown"
    query_mode = False
    positionals = []
    option_needs_value = {"-n", "--points-at", "--contains", "--no-contains", "--merged", "--no-merged", "--format", "--sort", "--column", "--color", "-u", "--local-user", "-m", "--message", "-F", "--file", "--trailer"}
    index = 0
    while index < len(tag_args):
        item = tag_args[index]
        if item == "--":
            if index + 1 >= len(tag_args):
                return "unknown"
            positionals.extend(tag_args[index + 1:])
            break
        option = item.split("=", 1)[0]
        if option in GIT_TAG_WRITE_OPTIONS:
            return "mutation"
        if option in GIT_TAG_QUERY_OPTIONS or (option.startswith("-n") and option != "-n"):
            query_mode = True
            if option in option_needs_value and "=" not in item and option != "-n":
                if index + 1 >= len(tag_args):
                    return "unknown"
                index += 1
        elif item.startswith("-"):
            return "unknown"
        else:
            positionals.append(item)
        index += 1
    if query_mode:
        return "read_only"
    return "read_only" if not positionals else "mutation"


def git_command_mutates(command: str) -> bool:
    aggregated = static_git_invocations(command, None)
    if aggregated is not None and len(aggregated) > 1:
        return not all(item["disposition"] == "read_only" for item in aggregated)
    subcommand = git_subcommand(command)
    if subcommand == "tag":
        return git_tag_disposition(command) != "read_only"
    return bool(subcommand in PLAN_MUTATING_GIT_COMMANDS)


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
    if git_command_mutates(command):
        return "Git mutation"
    if any(BUILD_COMMAND_RE.search(shell_syntax_view(candidate)) for candidate in command_views(command)):
        return "build or package"
    if command_mutates_device(command):
        return "device mutation"
    if command_mutates_files(command):
        return "file mutation"
    return None


def executor_gate_guard(payload: dict[str, Any], state: dict[str, Any]) -> str | None:
    """Keep confirmed Hard mutation with exactly one current writer."""
    if state.get("work_difficulty") != "hard" or state.get("plan_state") != "confirmed":
        return None
    if state.get("confirmed_plan_digest") != state.get("plan_digest"):
        return "invalid confirmed-plan binding"
    tool_key = normalized_key(payload.get("tool_name"))
    if "requestuserinput" in tool_key:
        return None
    if is_subagent_spawn_tool(payload):
        if parent_writer_lease_current(state):
            return "child spawn while the parent owns the writer lease"
        if subagent_request_is_read_only(payload):
            return None
        valid, reason = confirmed_executor_request(payload, state)
        if valid and state.get("executor_state") in {
            "spawn_required",
            "recovery_required",
        }:
            return None
        if valid and state.get("executor_state") not in {
            "spawn_required",
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
    if caller_id is None and parent_writer_lease_current(state):
        return None
    if caller_id is None and parent_writer_acquisition_block(state) is None:
        return None
    return f"parent or unbound {mutating}"


def assessor_gate_guard(payload: dict[str, Any], state: dict[str, Any]) -> str | None:
    assessor_state = state.get("assessor_state")
    if assessor_state not in {"spawn_required", "spawn_pending", "running", "recovery_required", "failed"}:
        return None
    if is_subagent_spawn_tool(payload):
        # Codex owns ordinary subagent scheduling.  Workflow Manager intervenes
        # only when the request would create mutation authority before the Hard
        # plan is confirmed.
        mutating = plan_confirmation_guard(
            payload,
            {
                **state,
                "work_difficulty": "hard",
                "plan_state": "awaiting_confirmation",
                "confirmed_plan_digest": None,
            },
        )
        return f"unconfirmed Hard {mutating}" if mutating else None
    mutating = plan_confirmation_guard(payload, {**state, "work_difficulty": "hard", "plan_state": "awaiting_confirmation"})
    if not mutating:
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


def static_git_invocations(
    command: str, payload_cwd: Any
) -> list[dict[str, str | None]] | None:
    """Parse a small, deliberately static Git-only command list.

    This is the sole aggregation exception: every segment must be a literal
    Git invocation, all must be read-only, and all resolve to one native cwd.
    Shell expansion, pipelines, a non-Git segment, or an omitted cwd is not a
    static command list and therefore remains fail-closed.
    """
    if not isinstance(command, str) or not command.strip() or any(
        marker in command for marker in ("$", "`", "\x00", "\n", "\r")
    ):
        return None
    try:
        # A Windows-native Hook receives literal ``C:\\...`` paths.  POSIX
        # shlex consumes their backslashes before cwd classification, which
        # can turn an otherwise static aggregate into a generic composite.
        # Preserve Windows path text here; the same native/mounted gate below
        # still decides whether the resolved cwd is allowed.
        lexer = shlex.shlex(command, posix=os.name != "nt", punctuation_chars=";&|")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except (TypeError, ValueError):
        return None
    if any(token in {"&&", "&", "|", "||"} for token in tokens):
        return None
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token == ";":
            if not segments[-1]:
                return None
            segments.append([])
        else:
            segments[-1].append(token)
    if not segments[-1] or len(segments) < 2:
        return None
    results: list[dict[str, str | None]] = []
    common_cwd: str | None = None
    for argv in segments:
        index = 0
        if not argv or argv[index].lower() not in {"git", "git.exe"}:
            return None
        index += 1
        cwd = str(payload_cwd or "").strip() or None
        while index < len(argv):
            item = argv[index]
            if item in {"-C", "-c", "--git-dir", "--work-tree"}:
                if index + 1 >= len(argv) or item != "-C":
                    return None
                cwd, error = _normalize_structured_command_cwd(argv[index + 1], cwd)
                if error:
                    return None
                index += 2
            elif item.startswith("-C") and item != "-C":
                cwd, error = _normalize_structured_command_cwd(item[2:], cwd)
                if error:
                    return None
                index += 1
            else:
                break
        if index >= len(argv) or not cwd:
            return None
        subcommand = argv[index].lower()
        if not re.fullmatch(r"[a-z][a-z0-9-]*", subcommand):
            return None
        rendered = "git " + " ".join(shlex.quote(item) for item in argv[1:])
        disposition = (
            git_tag_disposition(rendered)
            if subcommand == "tag"
            else "mutation" if subcommand in PLAN_MUTATING_GIT_COMMANDS else "read_only"
        )
        if common_cwd is None:
            common_cwd = cwd
        elif cwd != common_cwd:
            return None
        results.append({"subcommand": subcommand, "cwd": cwd, "disposition": disposition})
    return results


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
    # Current Codex commonly runs a repository-local verifier directly rather
    # than through a test framework.  Its successful host exit is verification
    # evidence when the command is read-only and the executed program itself
    # (not a quoted ``-c`` body or a file inspected by sed/rg) has an explicit
    # verify/validate/check/acceptance/regression identity.
    custom_verifier = re.compile(
        r"(?i)\b(?:python(?:3(?:\.\d+)*)?|node|bash|sh|ruby|perl)\b\s+"
        r"(?:(?:-(?!c(?:\s|$))[A-Za-z]+)\s+)*"
        r"(?:-m\s+)?[^\s;&|]*"
        r"(?:verify|validate|check|acceptance|regression)"
        r"[^\s;&|]*"
    )
    if (
        value
        and not command_mutates_files(value)
        and not any(
            re.search(
                r"(?i)(?:^|[;&|]\s*)(?:adb(?:\.exe)?|fastboot|flash)\b",
                candidate,
            )
            for candidate in detections
        )
        and any(custom_verifier.search(candidate) for candidate in detections)
    ):
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
    aggregate = static_git_invocations(command, resolution.get("cwd"))
    if subcommand and git_invocation_count(command) != 1 and aggregate is None:
        return (
            "ambiguous_git_input",
            "Workflow Manager guard blocked a non-static Git composite. Only literal, Git-only, read-only "
            "invocations in one explicit native Linux cwd may be aggregated.",
        )
    if aggregate is not None:
        if not all(item["disposition"] == "read_only" for item in aggregate):
            return ("ambiguous_git_input", "Workflow Manager guard blocked a Git composite containing a write.")
        effective_cwd = aggregate[0]["cwd"]
        if cwd_is_wsl_or_network_mount(effective_cwd):
            return ("mounted_local_git", "Workflow Manager guard blocked Git in a WSL/DrvFS/CIFS/UNC working tree.")
        if not real_tmp_git_directory(effective_cwd):
            return ("invalid_tmp_git_cwd", "Workflow Manager guard blocked an unavailable native Git cwd.")
        return None
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
    # Git on a mounted/network working tree can corrupt identity and violates the
    # repository contract, so it remains a hard guard.  Output shape is not an
    # authorization boundary: builds, logs, recordings, and broad status are
    # observed as bounded telemetry but are no longer denied by the plugin.
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
    configured = {
        "output_chars": env_int("TOKEN_FRUGAL_OUTPUT_CHAR_LIMIT", DEFAULT_OUTPUT_CHAR_LIMIT, 1000, 500_000),
        "output_lines": env_int("TOKEN_FRUGAL_OUTPUT_LINE_LIMIT", DEFAULT_OUTPUT_LINE_LIMIT, 50, 10_000),
        "visual_items": env_int("TOKEN_FRUGAL_VISUAL_ITEM_LIMIT", DEFAULT_VISUAL_ITEM_LIMIT, 1, 50),
    }
    dynamic = {
        "output_chars": DEFAULT_OUTPUT_CHAR_LIMIT,
        "output_lines": DEFAULT_OUTPUT_LINE_LIMIT,
        "visual_items": DEFAULT_VISUAL_ITEM_LIMIT,
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


def _structured_spawn_task_receipt(response: Any, depth: int = 0) -> str | None:
    """Return only a host-structured spawn task receipt.

    Current Codex hosts may complete ``spawn_agent`` with a compact
    ``{"task_name":"/root/<name>"}`` result and no generic ``status`` field.
    That object is an explicit PostToolUse receipt, but arbitrary prose or a
    nested command-output string is not.  Keep the accepted envelope narrow so
    child text can never manufacture host acceptance.
    """
    if depth > 5:
        return None
    if isinstance(response, str):
        stripped = response.strip()
        if not _json_container_text(stripped):
            return None
        try:
            return _structured_spawn_task_receipt(json.loads(stripped), depth + 1)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    if isinstance(response, (list, tuple)):
        if len(response) != 1:
            return None
        return _structured_spawn_task_receipt(response[0], depth + 1)
    if not isinstance(response, dict):
        return None
    direct = [
        value for key, value in response.items() if normalized_key(key) == "taskname"
    ]
    if len(direct) == 1 and isinstance(direct[0], str) and direct[0]:
        return safe_label(direct[0], 240)
    if direct:
        return None
    if normalized_key(response.get("type")) in {"text", "inputtext", "outputtext"}:
        text_value = response.get("text")
        if isinstance(text_value, str):
            return _structured_spawn_task_receipt(text_value, depth + 1)
    content = response.get("content")
    if isinstance(content, (dict, list, tuple, str)):
        return _structured_spawn_task_receipt(content, depth + 1)
    return None


def _spawn_envelope_has_field(
    response: Any,
    normalized_names: set[str],
    depth: int = 0,
    _budget: list[int] | None = None,
) -> bool:
    # This is a rejection-side ambiguity scan, not an acceptance parser.  It
    # deliberately walks every bounded container branch so a success marker
    # cannot hide a conflicting task receipt in a sibling/wrapper.  Exhausting
    # the bound is ambiguous and therefore returns True (fail closed).
    budget = _budget if _budget is not None else [128]
    if depth > 8 or budget[0] <= 0:
        return True
    budget[0] -= 1
    if isinstance(response, str):
        stripped = response.strip()
        if not _json_container_text(stripped):
            return False
        try:
            return _spawn_envelope_has_field(
                json.loads(stripped), normalized_names, depth + 1, budget
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
    if isinstance(response, (list, tuple)):
        if len(response) > 32:
            return True
        return any(
            _spawn_envelope_has_field(item, normalized_names, depth + 1, budget)
            for item in response
        )
    if not isinstance(response, dict):
        return False
    if len(response) > 32:
        return True
    if any(normalized_key(key) in normalized_names for key in response):
        return True
    return any(
        _spawn_envelope_has_field(value, normalized_names, depth + 1, budget)
        for value in response.values()
    )


def _spawn_task_receipt_inventory(
    response: Any,
    depth: int = 0,
    _budget: list[int] | None = None,
    _result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Collect every bounded task_name occurrence for uniqueness checks."""
    budget = _budget if _budget is not None else [128]
    result = _result if _result is not None else {
        "count": 0,
        "values": [],
        "ambiguous": False,
    }
    if depth > 8 or budget[0] <= 0:
        result["ambiguous"] = True
        return result
    budget[0] -= 1
    if isinstance(response, str):
        stripped = response.strip()
        if not _json_container_text(stripped):
            return result
        try:
            return _spawn_task_receipt_inventory(
                json.loads(stripped), depth + 1, budget, result
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            return result
    if isinstance(response, (list, tuple)):
        if len(response) > 32:
            result["ambiguous"] = True
            return result
        for item in response:
            _spawn_task_receipt_inventory(item, depth + 1, budget, result)
        return result
    if not isinstance(response, dict):
        return result
    if len(response) > 32:
        result["ambiguous"] = True
        return result
    for key, value in response.items():
        if normalized_key(key) == "taskname":
            result["count"] = min(safe_int(result.get("count")) + 1, 3)
            if isinstance(value, str) and value:
                result.setdefault("values", []).append(safe_label(value, 240))
        _spawn_task_receipt_inventory(value, depth + 1, budget, result)
    return result


def _spawn_envelope_has_conflicting_signal(
    response: Any, depth: int = 0, _budget: list[int] | None = None
) -> bool:
    """Reject a success envelope containing any bounded contradictory signal."""
    budget = _budget if _budget is not None else [128]
    if depth > 8 or budget[0] <= 0:
        return True
    budget[0] -= 1
    if isinstance(response, str):
        stripped = response.strip()
        if not _json_container_text(stripped):
            return False
        try:
            return _spawn_envelope_has_conflicting_signal(
                json.loads(stripped), depth + 1, budget
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
    if isinstance(response, (list, tuple)):
        if len(response) > 32:
            return True
        return any(
            _spawn_envelope_has_conflicting_signal(item, depth + 1, budget)
            for item in response
        )
    if not isinstance(response, dict):
        return False
    if len(response) > 32:
        return True
    for key, value in response.items():
        normalized = normalized_key(key)
        if normalized in {"status", "state"}:
            status = str(value or "").strip().lower()
            if status not in (
                SUCCESS_STATUSES
                | RUNNING_STATUSES
                | {"complete", "completed", "done", "success", "succeeded"}
            ):
                return True
        elif normalized in {"error", "iserror"}:
            if value is not False and value is not None:
                return True
        elif normalized in {"success", "ok"}:
            if value is not True:
                return True
        elif normalized == "exitcode":
            if not isinstance(value, int) or isinstance(value, bool) or value != 0:
                return True
    return any(
        _spawn_envelope_has_conflicting_signal(value, depth + 1, budget)
        for value in response.values()
    )


def _spawn_task_receipt_matches(receipt: str | None, requested: str | None) -> bool:
    requested = safe_label(requested, 120) if requested else None
    receipt = safe_label(receipt, 240) if receipt else None
    if not requested or not receipt:
        return False
    if receipt == requested:
        return True
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", requested):
        return False
    return receipt == f"/root/{requested}"


def spawn_response_status(response: Any, requested_task_name: str | None) -> str:
    """Resolve a spawn PostToolUse without weakening generic response parsing."""
    explicit = _response_status_explicit(response)
    receipt = _structured_spawn_task_receipt(response)
    task_inventory = _spawn_task_receipt_inventory(response)
    task_present = bool(
        safe_int(task_inventory.get("count")) or task_inventory.get("ambiguous")
    )
    inventory_values = as_list(task_inventory.get("values"))
    task_unique_match = bool(
        not task_inventory.get("ambiguous")
        and safe_int(task_inventory.get("count")) == 1
        and len(inventory_values) == 1
        and _spawn_task_receipt_matches(inventory_values[0], requested_task_name)
    )
    task_matches = bool(
        task_unique_match
        and _spawn_task_receipt_matches(receipt, requested_task_name)
    )
    conflicting_signal = _spawn_envelope_has_conflicting_signal(response)
    if explicit and explicit.startswith("error"):
        return explicit
    if explicit:
        return (
            explicit
            if not conflicting_signal and (not task_present or task_unique_match)
            else "unknown"
        )
    # A generic session_id means a long-running command to response_status;
    # it is not a spawn receipt.  Likewise, a present but unknown status/state
    # must not be rescued by an otherwise plausible task name.
    if isinstance(response, dict):
        signal_keys = {
            "status", "state", "success", "ok", "error", "iserror", "exitcode"
        }
        if isinstance(response.get("content"), list) and response.get("isError") is False:
            return (
                "ok"
                if not conflicting_signal and (not task_present or task_unique_match)
                else "unknown"
            )
        if _spawn_envelope_has_field(response, signal_keys):
            return "unknown"
    return "ok" if task_matches else "unknown"


def spawn_acceptance_receipt_digest(
    response: Any, requested_task_name: str | None, status: str
) -> str:
    receipt = _structured_spawn_task_receipt(response)
    inventory = _spawn_task_receipt_inventory(response)
    values = as_list(inventory.get("values"))
    unique_receipt = (
        values[0]
        if not inventory.get("ambiguous")
        and safe_int(inventory.get("count")) == 1
        and len(values) == 1
        else None
    )
    return stable_hash(
        "workflow-manager-spawn-post-receipt-v1\0"
        + canonical_json(
            {
                "status": safe_label(status, 32),
                "task_match": bool(
                    unique_receipt
                    and _spawn_task_receipt_matches(
                        receipt, requested_task_name
                    )
                    and _spawn_task_receipt_matches(
                        unique_receipt, requested_task_name
                    )
                ),
                "task_receipt_digest": stable_hash(unique_receipt, 32)
                if unique_receipt
                else None,
                "task_receipt_count": safe_int(inventory.get("count")),
                "task_receipt_ambiguous": bool(inventory.get("ambiguous")),
            }
        ),
        32,
    )


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


def emit_stop_block(reason: str) -> None:
    """Return the strict Codex Stop schema and feed one continuation turn."""
    print(
        json.dumps(
            {
                "decision": "block",
                "reason": str(reason or "").strip(),
            },
            ensure_ascii=True,
        )
    )


def emit_leased_stop_block(payload: dict[str, Any], reason: str) -> None:
    """Persist a continuation send attempt without self-acknowledging it.

    stdout is not a host receipt.  A later exact structured receipt/root
    message is the only authority allowed to consume the durable outbox item.
    """
    lease_box: dict[str, Any] = {}

    def claim(state: dict[str, Any]) -> None:
        lease_box.update(claim_continuation_lease(state, payload, reason))

    # The business Stop mutation has already consumed its host hook-run key.
    # Lease claim is a separate persistence boundary and must not be filtered
    # as a duplicate of that business event.
    claim_payload = dict(payload)
    claim_payload.pop("hook_run_id", None)
    mutate_state(claim_payload, claim)
    if not lease_box.get("emit"):
        emit_continue()
        return
    key = str(lease_box["key"])
    # Test-only crash seam: production ignores it unless explicitly enabled.
    # It proves the persisted emitted lease survives a process SIGKILL window.
    if os.environ.get("WORKFLOW_MANAGER_TEST_SIGKILL_AFTER_LEASE_EMITTED") == "1":
        os._exit(137)
    emit_stop_block(f"{reason} continuation_key={key}")


def current_slice_resume_delta(
    state: dict[str, Any], canonical_body: str
) -> dict[str, Any] | None:
    """Project one verified current-slice handoff without replaying the plan."""
    if (
        state.get("plan_state") != "confirmed"
        or state.get("confirmed_plan_digest") != state.get("plan_digest")
        or str(state.get("execution_profile_version")) != EXECUTION_PROFILE_VERSION
    ):
        return None
    try:
        parsed = execution_slice_manifest_for_plan(canonical_body)
    except PlanArtifactError:
        return None
    persisted = _safe_execution_slices(state.get("execution_slices"))
    if (
        parsed.get("manifest_digest") != persisted.get("manifest_digest")
        or parsed.get("global_constraints_digest")
        != persisted.get("global_constraints_digest")
        or parsed.get("count") != persisted.get("count")
    ):
        return None
    index = safe_int(persisted.get("current_index"))
    delta: dict[str, Any] = {
        "schema": 1,
        "plan_digest": state.get("plan_digest"),
        "plan_generation": safe_int(state.get("plan_generation")),
        "execution_contract_id": state.get("execution_contract_id"),
        "executor_state": state.get("executor_state"),
        "executor_attempt": safe_int(state.get("executor_attempt")),
        "executor_failure_kind": state.get("executor_failure_kind"),
        "completed_prefix": {
            "count": min(max(index - 1, 0), persisted.get("count", 0)),
            "chain_digest": persisted.get("completed_chain"),
            "total_slices": persisted.get("count"),
        },
        "global_constraints": parsed.get("global_constraints"),
    }
    if 1 <= index <= persisted.get("count", 0):
        parsed_item = parsed["items"][index - 1]
        stored_item = persisted["items"][index - 1]
        if parsed_item.get("slice_digest") != stored_item.get("slice_digest"):
            return None
        delta["current_slice"] = {
            key: parsed_item[key]
            for key in ("id", "title", *EXECUTION_SLICE_FIELDS[1:])
        }
        delta["current_slice"].update(
            {
                "slice_digest": stored_item.get("slice_digest"),
                "slice_contract_id": slice_contract_id(state),
                "slice_task_token": slice_task_token(state),
            }
        )
    else:
        delta["current_slice"] = None
    review = _safe_executor_review(state.get("executor_review"))
    if review.get("status") != "none":
        delta["parent_review"] = {
            key: review.get(key)
            for key in (
                "status",
                "slice_id",
                "slice_contract_id",
                "attempt",
                "candidate_result_fingerprint",
                "candidate_agent_fingerprint",
                "candidate_evidence_digest",
                "review_evidence_digest",
            )
        }
    stall = _safe_stall(state.get("stall"))
    if stall.get("state") != "none":
        delta["stall"] = stall
    return delta


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
            reconcile_current_parent_rollout_on_resume(payload, state)
            reconcile_current_parent_review_on_resume(payload, state)
            promote_reconciled_parent_review(state)
            reconcile_current_executor_rollout_on_resume(payload, state)
            resume_completed_parent_review_once(payload, state)
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
    active_contract = bool(
        state.get("plan_state") not in {None, "none"}
        or state.get("assessor_state") not in {None, "none"}
        or state.get("executor_state") not in {None, "none"}
        or _safe_causal_review(state.get("causal_review")).get("state") != "none"
        or _safe_reference_acceptance(state.get("reference_acceptance")).get("enabled")
    )
    confirmed_execution = bool(
        state.get("plan_state") == "confirmed"
        and state.get("confirmed_plan_digest") == state.get("plan_digest")
        and str(state.get("execution_profile_version"))
        == EXECUTION_PROFILE_VERSION
    )
    base = (
        f"Workflow Manager {WRITER_VERSION} active. Codex owns ordinary execution, progress, recovery, "
        "compaction, and subagent scheduling; Workflow Manager adds only Hard authorization, canonical "
        "contracts, runtime-truth evidence, and fixed safety boundaries."
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
    if source in {"compact", "resume"} and active_contract:
        successful = [op for op in state.get("operations", []) if op.get("status") in SUCCESS_STATUSES][-6:]
        digest = {
            "schema": SCHEMA_VERSION,
            "objective_fingerprint": state.get("objective", {}).get("fingerprint"),
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
            "causal_lineage": _safe_causal_lineage(state.get("causal_lineage")),
            "lifecycle_diagnostics": [
                item for raw in as_list(state.get("lifecycle_diagnostics"))[-4:]
                if (item := _safe_lifecycle_diagnostic(raw)) is not None
            ],
            "stall": _safe_stall(state.get("stall")),
            "reference_acceptance": _safe_reference_acceptance(state.get("reference_acceptance")),
            "terminal_successes": [
                {"tool": op.get("tool"), "fingerprint": op.get("fingerprint")} for op in successful
            ],
            "guard_blocks": sum(1 for item in state.get("guards", []) if item.get("action") == "deny"),
            "compaction_count": len(state.get("compactions", [])),
        }
        if confirmed_execution:
            artifact = _safe_plan_artifact(state.get("plan_artifact"))
            current = current_execution_slice(state)
            slices = _safe_execution_slices(state.get("execution_slices"))
            digest = {
                "schema": SCHEMA_VERSION,
                "objective_fingerprint": state.get("objective", {}).get("fingerprint"),
                "plan_state": "confirmed",
                "plan_generation": safe_int(state.get("plan_generation")),
                "plan_digest": state.get("plan_digest"),
                "current_revision_digest": artifact.get("current_revision_digest"),
                "journal_digest": artifact.get("journal_digest"),
                "execution_profile_version": state.get("execution_profile_version"),
                "execution_contract_id": state.get("execution_contract_id"),
                "executor_state": state.get("executor_state"),
                "executor_attempt": safe_int(state.get("executor_attempt")),
                "executor_failure_kind": state.get("executor_failure_kind"),
                "current_slice_id": current.get("id") if current else None,
                "current_slice_contract_id": slice_contract_id(state),
                "current_slice_task_token": slice_task_token(state),
                "completed_prefix_count": min(
                    max(safe_int(slices.get("current_index")) - 1, 0),
                    safe_int(slices.get("count")),
                ),
                "completed_prefix_digest": slices.get("completed_chain"),
                "parent_review_status": _safe_executor_review(
                    state.get("executor_review")
                ).get("status"),
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
        if isinstance(canonical_resume_body, str) and confirmed_execution:
            slice_delta = current_slice_resume_delta(state, canonical_resume_body)
            if slice_delta is not None:
                base += (
                    " The Hook verified the full canonical revision internally and projected only the current "
                    "slice plus minimum continuity delta; do not replay the full plan.\n"
                    "BEGIN_WORKFLOW_MANAGER_CURRENT_SLICE_DELTA\n"
                    f"{json.dumps(slice_delta, ensure_ascii=False, separators=(',', ':'))}\n"
                    "END_WORKFLOW_MANAGER_CURRENT_SLICE_DELTA"
                )
            else:
                base += (
                    " Current-slice projection did not match the verified canonical revision; do not spawn or "
                    "mutate until the contract is revalidated."
                )
        elif isinstance(canonical_resume_body, str):
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
    elif source in {"compact", "resume"}:
        base += " Native summary continuity is sufficient; no active Workflow Manager contract was replayed."
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
    r"(?:继续(?:啊|吧)?(?:我)?[，, ]*)?(?:严格)?确认执行",
    r"(?:严格)?确认执行",
    r"(?:严格)?确认按(?:这个|上述|该|此|新)计划执行",
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
    "作废",
    "修正版",
    "写入",
    "先不要",
    "but",
    "except",
    "add",
    "remove",
    "change",
)
ENGLISH_PLAN_CHANGE_MARKERS = ("but", "except", "add", "remove", "change")
PLAN_REPLAN_PATTERNS = (
    r"重新规划",
    r"重做计划",
    r"修改计划",
    r"replan",
    r"revise (?:this|the) plan",
)


def _normalized_control_candidate(prompt: str) -> str:
    """Normalize only a compact, single-line control candidate.

    This deliberately is not a general HTML or Markdown normalizer: protocol
    bodies and code blocks must remain opaque to control recognition.
    """
    raw = str(prompt or "")
    # Bound the original Desktop payload before trimming.  Desktop commonly
    # adds a trailing LF/CRLF, but an embedded line break, fenced code, or
    # extra sentence must never become an authorization control by trimming.
    if not raw or len(raw.encode("utf-8")) > 512:
        return ""
    value = re.sub(r"(?:&#(?:32|x20);|&nbsp;)", " ", raw, flags=re.I).strip()
    if not value or "\n" in value or "\r" in value or "```" in value or "`" in value:
        return ""
    return re.sub(r"[ \t]+", " ", value).strip().lower()


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
    normalized = re.sub(r"[?!？！。,.，]+", "", _normalized_control_candidate(prompt))
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized or contains_plan_change_marker(normalized):
        return False
    return any(re.fullmatch(pattern, normalized, re.I) for pattern in PLAN_CONFIRM_PATTERNS)


def contains_plan_change_marker(normalized: str) -> bool:
    """Match English controls as words, never inside protocol identifiers."""
    chinese = [
        marker
        for marker in PLAN_CHANGE_MARKERS
        if marker not in ENGLISH_PLAN_CHANGE_MARKERS
    ]
    return any(marker in normalized for marker in chinese) or any(
        re.search(rf"(?<![a-z0-9_]){re.escape(marker)}(?![a-z0-9_])", normalized, re.I)
        for marker in ENGLISH_PLAN_CHANGE_MARKERS
    )


def plan_replan_request(prompt: str) -> bool:
    inline = re.sub(
        r"(?:&#(?:32|x20);|&nbsp;)", " ", str(prompt or ""), flags=re.I
    ).strip()
    inline_match = re.fullmatch(r"`([^`\r\n]+)`", inline)
    normalized = (
        re.sub(r"[ \t]+", " ", inline_match.group(1)).strip().lower()
        if inline_match
        else _normalized_control_candidate(prompt)
    )
    if re.search(r"\b(?:is|was|are|means|mean|described|explained)\b", normalized, re.I):
        return False
    for pattern in PLAN_REPLAN_PATTERNS:
        if re.fullmatch(pattern, normalized, re.I):
            return True
        if re.fullmatch(
            rf"(?:{pattern})(?:\s*[:：-]\s*|\s+|(?=[\u3400-\u9fff])).+",
            normalized,
            re.I,
        ):
            return True
    return False


def plan_details_request(prompt: str) -> bool:
    normalized = re.sub(r"[?!？！。,.，:：]+", "", _normalized_control_candidate(prompt))
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
    return contains_plan_change_marker(controls_stripped)


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


def causal_review_hint(prompt: str, previous: dict[str, Any]) -> str:
    """Return only a provisional safety class; evidence controls the outcome."""
    normalized = re.sub(r"\s+", " ", prompt.strip().lower())
    if any(marker in normalized for marker in ("仍然", "还是", "没修好", "未修好", "still fails", "not fixed")):
        return "fix_ineffective"
    if re.search(r"(?:执行|构建|编译|部署|测试|execution|build|deploy|test).{0,32}(?:暴露|发现|exposed|revealed)", normalized):
        return "execution_exposed_gap"
    return "uncertain"


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
    result = dict(current)
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


def authorization_context(classification: dict[str, Any]) -> str:
    """Describe only Workflow Manager's non-native authorization decision."""
    domain = str(classification.get("task_domain") or "unknown")
    difficulty = str(classification.get("work_difficulty") or "unknown")
    profile = str(classification.get("model_profile") or "current")
    return (
        f"Workflow Manager authorization: domain={domain}, difficulty={difficulty}, profile={profile}. "
        "Codex owns ordinary execution, progress, recovery, compaction, and subagent scheduling."
    )



def user_prompt_submit(payload: dict[str, Any]) -> None:
    raw_prompt = str(payload.get("prompt") or "")
    root_continuation_key = root_visible_continuation_ack(payload)
    continuation_ack_delivery: dict[str, bool] = {"consumed": False}
    delegated_prompt = codex_delegation_input(raw_prompt)
    prompt = delegated_prompt if delegated_prompt is not None else raw_prompt
    identity_preflight = bool(
        delegated_prompt is None and identity_preflight_prompt(prompt)
    )
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
    recovery_marker_present = bool(
        re.search(
            r"(?:recovery_from|recovery-from|恢复自)\s*[:=：]",
            prompt,
            re.I,
        )
    )
    recovery_control_followup = bool(
        recovery_marker_present
        and previous.get("plan_state") == "confirmed"
        and previous.get("executor_state") == "recovery_required"
    )
    early_recovery_control_followup = bool(
        recovery_marker_present
        and previous.get("plan_state") == "confirmed"
        and previous.get("executor_state") == "running"
    )
    recovery_followup = recovery_control_followup or early_recovery_control_followup
    pending_recovery_record, pending_recovery_error = (
        parse_pending_recovery_reservation(prompt, previous)
        if recovery_control_followup
        else parse_running_recovery_reservation(prompt, previous)
        if early_recovery_control_followup
        else (None, "recovery reservation is not pending")
        if recovery_marker_present
        else (None, None)
    )
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
    confirmable_pending = previous.get("plan_state") == "awaiting_confirmation"
    repair_pending = previous.get("plan_state") == "repair_required"
    early_confirmation = bool(
        pure_plan_confirmation(prompt)
        and previous.get("task_domain") == "work"
        and previous.get("work_difficulty") == "hard"
        and previous.get("plan_state") in {"analyzing", "repair_required"}
        and previous.get("assessor_state")
        in {"spawn_pending", "running", "hard_plan_ready"}
        and previous.get("assessor_binding_id")
        and previous.get("objective", {}).get("fingerprint")
    )
    pending_plan = confirmable_pending
    active_plan = confirmable_pending or repair_pending or previous.get("plan_state") == "confirmed"
    explicit_new = explicit_new_objective(prompt)
    failed_assessor_replan = bool(
        previous.get("assessor_state") in {"recovery_required", "failed"}
        and plan_replan_request(prompt)
        and previous.get("objective", {}).get("fingerprint") != stable_hash(prompt)
    )
    cwd_changed = bool(
        previous.get("cwd_fingerprint")
        and previous.get("cwd_fingerprint") != stable_hash(payload.get("cwd"))
    )
    new_objective = False if recovery_followup else bool(
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
    if root_continuation_key:
        # An exact root echo acknowledges transport only; it can never become
        # a fresh objective or reshape the confirmed envelope.
        new_objective = False
    # A cwd change is not itself a contract reset: an exact control/recovery
    # may be a worktree migration.  But ordinary new work in another workspace
    # must never inherit confirmation, journal, or writer ownership.
    if cwd_changed and not (
        is_control_followup(prompt) or pure_plan_confirmation(prompt)
        or same_assessor_objective_retry or recovery_followup
    ):
        new_objective = True
    epoch_switch_blocked = bool(new_objective and _epoch_has_live_writer(previous))
    if epoch_switch_blocked:
        # Retain the old contract intact.  A later host event can only settle
        # that epoch; it cannot be reinterpreted as the requested successor.
        new_objective = False
    delegated_control_followup = bool(
        delegated_prompt is not None
        and previous.get("task_domain") == "work"
        and (
            previous.get("plan_state")
            in {
                "analyzing",
                "plan_ready",
                "awaiting_confirmation",
                "confirmed",
                "invalidated",
            }
            or previous.get("assessor_state")
            in {
                "spawn_required",
                "spawn_pending",
                "running",
                "recovery_required",
                "failed",
            }
            or previous.get("executor_state")
            in {
                "spawn_required",
                "spawn_pending",
                "running",
                "verification_required",
                "recovery_required",
                "exhausted",
            }
        )
        and re.search(
            r"(?:\bbinding\b|\bassessor\b|\bexecutor\b|\brecovery\b|"
            r"\bgeneration\b|\brevision\b|恢复|授权|允许|确认执行|修改计划|重新规划|作废)",
            prompt,
            re.I,
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
    scope_changed = bool(
        not new_objective
        and active_plan
        and authorization_scope_change_requested(prompt)
    )
    reference_failure = acceptance_miss or (reference_rejection and not causal_report)
    already_confirmed = previous.get("plan_state") == "confirmed"
    confirmed_plan = confirmable_pending and pure_plan_confirmation(prompt)
    replan = (
        active_plan
        and not causal_active
        and not recovery_followup
        and plan_replan_request(prompt)
    )
    plan_changed = (
        active_plan
        and not causal_active
        and not recovery_followup
        and not causal_report
        and not acceptance_miss
        and not new_objective
        and prompt_changes_pending_plan(prompt)
        and not confirmed_plan
    )
    causal_hint = causal_review_hint(prompt, previous) if causal_report else None
    completed_direct_followup = bool(
        plan_changed
        and previous.get("executor_state") == "succeeded"
        and _safe_execution_baseline(previous.get("last_execution_baseline"))
    )
    pending_successor = (
        pending_causal_successor(
            previous,
            causal_type=(
                causal_hint
                if causal_hint in EXECUTABLE_CAUSAL_TYPES
                else "direct_followup"
            ),
            issue_fingerprint=text_metadata(prompt).get("fingerprint"),
        )
        if causal_report
        else
        pending_causal_successor(
            previous,
            causal_type="acceptance_gap_no_change",
            issue_fingerprint=text_metadata(prompt).get("fingerprint"),
        )
        if acceptance_miss
        else pending_causal_successor(
            previous,
            causal_type="direct_followup",
            issue_fingerprint=text_metadata(prompt).get("fingerprint"),
        )
        if completed_direct_followup
        else {}
    )
    continuation = bool(root_continuation_key) or epoch_switch_blocked or (not new_objective and (
        preference_directive is not None
        or is_control_followup(prompt)
        or is_progress_followup(prompt)
        or active_plan
        or reference_changed
        or same_assessor_objective_retry
        or delegated_control_followup
        or early_confirmation
        or recovery_followup
    ))
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
    if continuation and previous.get("last_route"):
        classification = merge_followup_route(previous["last_route"], classification)
    if (confirmable_pending or early_confirmation) and pure_plan_confirmation(prompt):
        classification["difficulty_decision_id"] = previous.get("difficulty_decision_id")
        classification["work_difficulty"] = previous.get("work_difficulty", "hard")
        classification["task_domain"] = previous.get("task_domain", "work")
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
        classification["model_profile"] = confirmed_executor_model_profile(previous)
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
        if root_continuation_key:
            continuation_ack_delivery["consumed"] = consume_continuation_lease(
                state, root_continuation_key, source="root_visible",
                receipt={"continuation_key": root_continuation_key},
            )
        prompt_meta = text_metadata(prompt)
        prior_authorization_digest = authorization_envelope_digest(state)
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
        if epoch_switch_blocked:
            record_lifecycle_diagnostic(state, "epoch_switch_live_writer", level="error")
        if preference_directive is not None:
            if preference_changed and _epoch_has_live_writer(state):
                record_lifecycle_diagnostic(
                    state, "epoch_switch_live_writer", level="error",
                    role=("confirmed_executor" if state.get("executor_agent_id") else "high_assessor"),
                )
            else:
                state["session_execution_preference"] = preference_directive
                if preference_changed and state.get("plan_state") == "confirmed":
                    reset_executor_binding(state)
                    state["execution_contract_id"] = execution_contract_id(state)
                    state["executor_state"] = "spawn_required"
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
                "causal_type": causal_hint,
                "evidence_digest": None,
            }
            state["model_profile"] = "work_assessment"
        elif acceptance_miss and state.get("objective") and not pending_successor:
            prior = state["objective"]
            state["objective"] = {
                "fingerprint": stable_hash(
                    f"{prior.get('fingerprint')}\0{prompt_meta.get('fingerprint')}", 16
                ),
                "length": max(safe_int(prior.get("length")), 0) + prompt_meta["length"],
                "updated_at": utc_now(),
            }
        elif plan_changed and state.get("objective") and not pending_successor:
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
        # A first task and every genuine successor receive a fresh private
        # epoch/journal.  Existing v31 state remains epoch-less until such a
        # boundary so its old evidence is only migrated, never rewritten.
        if (not _safe_task_epoch(state.get("task_epoch")).get("id") and not state.get("objective", {}).get("fingerprint")):
            record_lifecycle_diagnostic(state, "epoch_switch_lifecycle_conflict", level="error")
        elif (
            (not _safe_task_epoch(state.get("task_epoch")).get("id") and not state.get("plan_digest"))
            or new_objective
            or (
                not continuation
                and _safe_task_epoch(state.get("task_epoch")).get("objective_fingerprint")
                != state.get("objective", {}).get("fingerprint")
            )
        ):
            if not rotate_task_epoch(state, payload, state.get("objective", {})):
                record_lifecycle_diagnostic(state, "epoch_switch_live_writer", level="error")
        if not continuation or not state.get("authorization_scope"):
            state["authorization_scope"] = authorization_scope_from_prompt(prompt)
        elif plan_changed or acceptance_miss or scope_changed:
            state["authorization_scope"] = authorization_scope_from_prompt(
                prompt, state.get("authorization_scope")
            )
        current_authorization_digest = authorization_envelope_digest(state)
        if (
            prior_authorization_digest
            and current_authorization_digest != prior_authorization_digest
        ):
            state["authorization_envelope"] = _safe_authorization_envelope(None)
            state["pending_confirmation_receipt"] = None
        if pending_successor:
            state["pending_causal_revision"] = pending_successor
        if early_confirmation:
            state["pending_confirmation_receipt"] = (
                pending_confirmation_receipt_for_state(state)
            )
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
            state["model_profile"] = confirmed_executor_model_profile(state)
        objective_fingerprint = state.get("objective", {}).get("fingerprint")
        if causal_report:
            # Freeze the old plan/contract for comparison. The causal guard allows only
            # evidence collection until a structured, baseline-bound conclusion is recorded.
            pass
        elif reference_failure:
            state["plan_state"] = "analyzing"
            state["plan_digest"] = None
            state["plan_objective_fingerprint"] = None
            state["plan_difficulty_decision_id"] = None
            state["confirmed_plan_digest"] = None
            state["confirmed_at"] = None
            reset_executor_binding(state)
            state["causal_review"] = _safe_causal_review(None)
            state["model_profile"] = "work_assessment"
        elif confirmed_plan:
            envelope_digest = authorization_envelope_digest(state)
            strict_receipt = (
                stable_hash(
                    f"strict-confirm-v2\0{envelope_digest}\0{prompt_meta.get('fingerprint')}\0host_bound",
                    32,
                )
                if envelope_digest
                else ""
            )
            if not activate_trusted_plan(
                state,
                payload,
                receipt=strict_receipt,
                increment_confirmation=True,
            ):
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
        if recovery_followup:
            reservation_error = pending_recovery_error
            if early_recovery_control_followup:
                existing = _safe_pending_recovery_reservation(
                    state.get("pending_recovery_reservation")
                )
                staged = _safe_pending_recovery_reservation(
                    pending_recovery_record
                )
                if staged and (
                    not existing
                    or existing.get("stage") != "terminal_pending"
                    or existing.get("prompt_receipt") == staged.get("prompt_receipt")
                ):
                    state["pending_recovery_reservation"] = staged
                elif existing and existing.get("stage") == "terminal_pending":
                    state["pending_recovery_reservation"] = existing
                    reservation_error = (
                        reservation_error
                        or "conflicting early recovery reservation"
                    )
                else:
                    state["pending_recovery_reservation"] = None
            else:
                state["pending_recovery_reservation"] = (
                    pending_recovery_reservation_for_state(
                        state, pending_recovery_record
                    )
                    if pending_recovery_record
                    else None
                )
            state["model_profile"] = confirmed_executor_model_profile(state)
            if reservation_error or not state["pending_recovery_reservation"]:
                state.setdefault("guards", []).append(
                    {
                        "at": utc_now(),
                        "turn_id": safe_label(payload.get("turn_id"), 120)
                        if payload.get("turn_id")
                        else None,
                        "kind": "recovery_reservation",
                        "action": "deny",
                        "fingerprint": stable_hash(
                            "workflow-manager-recovery-reservation-deny-v1\0"
                            + str(reservation_error or "stale binding"),
                            32,
                        ),
                    }
                )
        assessor_needed = classification.get("task_domain") == "work" and (
            not continuation or reference_failure or replan or plan_changed
        )
        if assessor_needed:
            hard_assessment = bool(
                classification.get("work_difficulty") == "hard"
                or causal_report
                or reference_failure
            )
            state["assessor_generation"] = (
                max(safe_int(state.get("assessor_generation")), 0) + 1
                if hard_assessment
                else safe_int(state.get("assessor_generation"))
            )
            state["assessor_binding_id"] = assessor_binding_id(state) if hard_assessment else None
            state["assessor_state"] = "spawn_required" if hard_assessment else "none"
            state["assessor_agent_id"] = None
            state["assessor_model"] = None
            state["assessor_reasoning_effort"] = None
            state["assessor_failure_kind"] = None
            state["assessor_observed_effective"] = False
            state["assessor_observed_model"] = None
            state["assessor_observed_reasoning_effort"] = None
            state["assessor_input_fingerprint"] = state.get("objective", {}).get("fingerprint") if hard_assessment else None
            state["assessor_fork_turns"] = None
            state["assessor_attempt"] = 0
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
    if continuation_ack_delivery["consumed"]:
        emit_context(
            "UserPromptSubmit",
            "Workflow Manager consumed one continuation lease from the exact root-visible key; ordinary text and stdout never acknowledge it.",
        )
        return
    prior_route = safe_route(previous.get("last_route"))
    route_changed = any(
        prior_route.get(key) != classification.get(key)
        for key in (
            "task_domain",
            "work_difficulty",
            "difficulty_decision_id",
        )
    )
    should_inject = (
        identity_preflight
        or preference_directive is not None
        or causal_report
        or reference_failure
        or causal_active
        or confirmed_plan
        or early_confirmation
        or replan
        or plan_changed
        or pending_plan
        or repair_pending
        or canonical_context_requested
        or recovery_marker_present
        or (already_confirmed and pure_plan_confirmation(prompt))
        or (
            already_confirmed
            and continuation
            and previous.get("executor_state")
            in {"spawn_required", "recovery_required", "verification_required"}
        )
        or (
            classification["task_domain"] == "work"
            and classification.get("work_difficulty") == "hard"
            and (not continuation or route_changed)
        )
    )
    if not should_inject:
        return
    context = authorization_context(classification)
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
            " Hard work needs one read-only high-tier assessment before mutation. Make exactly this collaboration "
            "call shape: collaboration.spawn_agent("
            f"task_name=\"{assessor_task}\", fork_turns=\"1\", model=\"{RECOVERY_EXECUTOR_MODEL}\", "
            f"reasoning_effort=\"{assessor_effort}\", message=<read-only assessment>). "
            f"This is the highest available Codex model at {assessor_effort} ({assessor_effort_policy}). "
            "Omit agent_type and do not construct fork_context; either option can be rejected by the host before "
            "a lifecycle receipt exists. "
            "Let the assessor judge objective/scope, acceptance, risk, rollback, and stop conditions natively. "
            "After it returns, the parent presents one ordinary human-readable plan; no plugin marker, JSON fence, "
            "fixed wording, or task-name encoding is required."
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
            "outcome=<direct_followup|introduced_regression|verified_side_effect|fix_ineffective|"
            "acceptance_gap_no_change|execution_exposed_gap|uncertain|explanatory_conclusion|"
            "unrelated_new_objective> evidence_digest=<32hex>."
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
            " Confirmation is bound. Exactly one writer may own the current slice. With no pending/live/unknown "
            "child writer or unfinished causal/stall diagnosis, the parent may acquire the slice lease directly. "
            "If a child is chosen, the Hook privately delivers the verified plan; task_name is only an opaque host label."
        )
        if refreshed_for_assessor.get("session_execution_preference") == "highest_throughout":
            context += (
                " Spawn one executor with the bound highest model, ultra reasoning, fork_turns=1, and any safe "
                f"ASCII task_name (suggestion: {executor_task})."
            )
        else:
            context += (
                " Spawn one executor with a current lower-tier Codex model, reasoning_effort=medium, fork_turns=1, "
                f"and any safe ASCII task_name (suggestion: {executor_task})."
            )
    elif early_confirmation:
        context += (
            " Early host-bound confirmation receipt recorded for the current objective and assessor binding. "
            "Keep the pending plan or repair intact; the receipt will be consumed automatically only after the "
            "matching canonical revision commits and verifies. The user does not need to repeat confirmation."
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
            pending = pending_recovery_reservation_for_state(
                refreshed_for_assessor
            )
            if pending:
                context += (
                    " A fresh recovery child is already host-bound if native diagnosis chooses that path; use "
                    f"model={RECOVERY_EXECUTOR_MODEL}, reasoning_effort={RECOVERY_EXECUTOR_REASONING_EFFORT}, "
                    "fork_turns=1 and any safe ASCII task_name. Parent-side diagnosis or verification may instead "
                    "finish without a child. Never follow up a terminal child."
                )
            else:
                host_recovery = recovery_reservation_context(
                    refreshed_for_assessor
                )
                context += (
                    " Choose natively between parent verification, further diagnosis, replanning, or one fresh child; "
                    "recovery state does not require another model turn by itself. No reservation is needed for "
                    "parent-side work or completion. Only if a fresh child is chosen, bind the Hook-issued facts "
                    "below to the diagnosed root cause and material correction; the child inherits the current "
                    "authorization envelope without another confirmation."
                )
                if host_recovery:
                    context += f"\n{host_recovery}"
                if pending_recovery_error:
                    context += f" The submitted reservation was rejected: {pending_recovery_error}."
        elif refreshed_for_assessor.get("executor_state") == "verification_required":
            context += (
                " Executor completion is only a candidate. Independently inspect the artifacts and run the acceptance "
                "verification, then report the result naturally. Host-recorded verification plus parent Stop seals the "
                "review; EXECUTION_REVIEW is optional. A material failure uses a fresh typed child and never revives a terminal child."
            )
        elif refreshed_for_assessor.get("executor_state") == "running":
            staged = _safe_pending_recovery_reservation(
                refreshed_for_assessor.get("pending_recovery_reservation")
            )
            if staged and staged.get("stage") == "terminal_pending":
                context += (
                    " The exact recovery claim arrived before the bound terminal lifecycle. It is retained as "
                    "digest-only terminal_pending evidence and carries no mutation or spawn authority. A unique "
                    "matching mailbox completed result plus final EXECUTION_RESULT must form the terminal boundary "
                    "before the reservation can bind automatically."
                )
    elif classification.get("work_difficulty") == "hard" and not pending_plan:
        context += (
            " Hard work: use the high-tier assessment, then present one ordinary plan covering scope, verification, "
            "risk, and rollback. Do not mutate before confirmation."
        )
    elif repair_pending:
        context += " Canonical plan repair is pending; confirmation or clarification cannot unlock execution. Explicitly replan to request one new assessor."
    elif pending_plan and not confirmed_plan:
        context += " Awaiting strict plan confirmation; answer plan questions but do not mutate, build, or deploy."
    emit_context("UserPromptSubmit", context)


def record_confirmed_executor_pretool(
    payload: dict[str, Any], state: dict[str, Any], fingerprint: str
) -> bool:
    """Atomically reserve one contract-bound executor sequence."""
    if not is_subagent_spawn_tool(payload):
        return False
    executor_request, _ = confirmed_executor_request(payload, state)
    if not executor_request:
        return False
    decision: dict[str, Any] = {"accepted": False, "reason": "stale executor request"}

    def reserve(current: dict[str, Any]) -> None:
        valid, reason = confirmed_executor_request(payload, current)
        if not valid:
            decision["reason"] = reason or "executor request no longer matches state"
            return
        if any(
            group.get("state") in {"pending", "result_pending", "live"}
            and isinstance(group.get("request"), dict)
            and group["request"].get("role") == "confirmed_executor"
            for group in subagent_lifecycle_groups(current)
        ):
            decision["reason"] = "one confirmed executor is already pending or live"
            return

        options = subagent_request_options(payload)
        task_name, scope_fingerprint = subagent_request_fields(payload)
        visibility = subagent_request_visibility(payload)
        recovery_from = (
            current.get("executor_failure_kind")
            if current.get("executor_state") == "recovery_required"
            and current.get("executor_failure_kind") in EXECUTOR_FAILURE_KINDS
            else None
        )
        recovery_record: dict[str, Any] | None = None
        if recovery_from:
            recovery_record = pending_recovery_reservation_for_state(current)
            recovery_error = None
            if recovery_record is None:
                recovery_record, recovery_error = parse_recovery_contract(
                    subagent_request_text(payload),
                    current,
                    opaque=visibility == "opaque_v2",
                )
            if not recovery_record:
                decision["reason"] = recovery_error or "invalid recovery contract"
                return

        sequence = next_sequence(current.get("executor_attempt"))
        candidate = json.loads(canonical_json(current))
        identity = candidate.setdefault("identity_evidence", {})
        identity["requested_profile"] = stable_hash(
            canonical_json(
                {
                    "model": safe_label(options.get("model"), 80),
                    "reasoning_effort": safe_label(
                        options.get("reasoning_effort"), 24
                    ),
                    "fork_turns": str(options.get("fork_turns") or ""),
                }
            ),
            32,
        )
        candidate.setdefault("subagents", []).append(
            {
                "at": utc_now(),
                "event": "request",
                "epoch_id": current_task_epoch_id(candidate),
                "turn_id": safe_label(payload.get("turn_id"), 120)
                if payload.get("turn_id")
                else None,
                "agent_id": None,
                "agent_type": None,
                "task_name": task_name,
                "scope_fingerprint": scope_fingerprint,
                "request_fingerprint": fingerprint,
                "objective_fingerprint": candidate.get("objective", {}).get(
                    "fingerprint"
                ),
                "stale": False,
                "status": "pending",
                "requested": True,
                "host_accepted": None,
                "request_gate": "contract",
                "request_visibility": visibility,
                "request_cap": 1,
                "reaudited": False,
                "role": "confirmed_executor",
                "contract_id": candidate.get("execution_contract_id"),
                "slice_id": (current_execution_slice(candidate) or {}).get("id"),
                "slice_contract_id": slice_contract_id(candidate),
                "model": safe_label(options.get("model"), 80),
                "reasoning_effort": safe_label(
                    options.get("reasoning_effort"), 24
                ),
                "fork_turns": options.get("fork_turns"),
                "attempt": sequence,
                "recovery_from": recovery_from,
            }
        )
        if recovery_record:
            recovery_record["sequence"] = sequence
            candidate["recovery_chain"] = [
                *safe_recovery_chain(candidate.get("recovery_chain")),
                recovery_record,
            ]
            candidate["pending_recovery_reservation"] = None
        if executor_verification_recovery_pending(candidate):
            review = _safe_executor_review(candidate.get("executor_review"))
            review["status"] = "recovery_started"
            review["at"] = utc_now()
            candidate["executor_review"] = review
            baseline = _safe_execution_baseline(
                candidate.get("last_execution_baseline")
            ) or build_execution_baseline(candidate)
            if baseline:
                baseline["acceptance_status"] = "failed"
                candidate["last_execution_baseline"] = baseline
        candidate["executor_state"] = "spawn_pending"
        candidate["executor_attempt"] = sequence
        candidate["executor_failure_kind"] = recovery_from
        candidate["executor_model"] = safe_label(options.get("model"), 80)
        candidate["executor_reasoning_effort"] = safe_label(
            options.get("reasoning_effort"), 24
        )
        candidate["executor_fork_turns"] = str(options.get("fork_turns"))
        candidate["model_profile"] = confirmed_executor_model_profile(candidate)
        stall = _safe_stall(candidate.get("stall"))
        if stall.get("state") == "resume_required":
            stall["state"] = "resuming"
            stall["correction_digest"] = (
                recovery_record.get("correction_digest")
                if recovery_record
                else None
            )
            stall["at"] = utc_now()
            candidate["stall"] = stall
        if not state_within_budget(candidate):
            decision["reason"] = (
                "state byte/node budget cannot reserve another recovery; split or stop"
            )
            return
        current.clear()
        current.update(candidate)
        decision["accepted"] = True
        decision["profile"] = candidate.get("model_profile")

    mutate_state(payload, reserve)
    if not decision["accepted"]:
        emit_pretool_deny(
            "Workflow Manager blocked executor reservation: "
            f"{decision['reason']}. Diagnose or use new evidence/root cause/material correction before replay."
        )
        return True
    profile_text = (
        "highest_available gpt-5.6-sol/max recovery request"
        if decision.get("profile") == "work_executor_highest_available"
        else "lower-tier model and medium reasoning request"
    )
    emit_context(
        "PreToolUse",
        f"Executor request reserved with the {profile_text} and fork_turns=1. task_name/prose are opaque; "
        "authority still requires the matching host acceptance and full Start before mutation.",
    )
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
                "terminal confirmed executor follow-up is forbidden; a terminal child cannot be revived. "
                "Use a fresh parent-spawned child after diagnosis and a host-bound recovery reservation"
            )
    return None


def pre_tool_use(payload: dict[str, Any]) -> None:
    fingerprint, tool = tool_fingerprint(payload)
    state = snapshot_state(payload)
    snapshot_failure = str(state.get("_snapshot_failure") or "")
    # A host may legitimately deliver PreToolUse before UserPromptSubmit/SessionStart.
    # Absence of state is not proof of an unconfirmed Hard task and must fail open;
    # a present-but-invalid state or failed transaction still fails closed.
    if snapshot_failure not in {"", "missing_state", "missing_session_id"}:
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
    if is_subagent_spawn_tool(payload) and not active_hard_lifecycle(state):
        def record_ordinary_spawn(current: dict[str, Any]) -> None:
            record_lifecycle_diagnostic(
                current,
                "ordinary_spawn_no_active_hard",
                level="info",
                role="lane",
                request_fingerprint=fingerprint,
            )

        # Informational only: ordinary Codex scheduling remains entirely native.
        mutate_state(payload, record_ordinary_spawn)
    nested_caller = next(
        (
            safe_label(payload.get(key), 120)
            for key in ("agent_id", "subagent_id")
            if payload.get(key)
        ),
        None,
    )
    if (
        nested_caller
        and _safe_child_liveness(state.get("child_liveness")).get("status") == "unknown"
        and known_writer_roles(state, nested_caller)
    ):
        # A partial inventory may not be used to keep working optimistically.
        # The original child remains available only to deliver its exact Stop;
        # all further tool actions are blocked until a structured live/absent
        # host fact resolves the ownership boundary.
        def record_unknown_writer_guard(current: dict[str, Any]) -> None:
            current.setdefault("guards", []).append(
                {
                    "at": utc_now(),
                    "turn_id": safe_label(payload.get("turn_id"), 120)
                    if payload.get("turn_id")
                    else None,
                    "kind": "writer_inventory_unknown",
                    "action": "deny",
                    "fingerprint": fingerprint,
                }
            )

        mutate_state(payload, record_unknown_writer_guard)
        emit_pretool_deny(
            "Workflow Manager blocked this child tool action: writer inventory is unknown; wait for an exact host lifecycle or complete inventory fact."
        )
        return
    if is_subagent_spawn_tool(payload) and nested_caller:
        def record_nested_guard(current: dict[str, Any]) -> None:
            current.setdefault("guards", []).append(
                {
                    "at": utc_now(),
                    "turn_id": safe_label(payload.get("turn_id"), 120)
                    if payload.get("turn_id")
                    else None,
                    "kind": "child_nesting",
                    "action": "deny",
                    "fingerprint": fingerprint,
                }
            )

        mutate_state(payload, record_nested_guard)
        emit_pretool_deny(
            "Workflow Manager blocked child nesting: only the parent may dispatch a bound child."
        )
        return
    if (
        is_subagent_spawn_tool(payload)
        and "identity_preflight_not_work"
        in as_list(state.get("difficulty_rule_codes"))
    ):
        def record_identity_preflight_guard(current: dict[str, Any]) -> None:
            current.setdefault("guards", []).append(
                {
                    "at": utc_now(),
                    "turn_id": safe_label(payload.get("turn_id"), 120)
                    if payload.get("turn_id")
                    else None,
                    "kind": "identity_preflight_child",
                    "action": "deny",
                    "fingerprint": fingerprint,
                }
            )

        mutate_state(payload, record_identity_preflight_guard)
        emit_pretool_deny(
            "Workflow Manager blocked child spawn: the active identity preflight explicitly requires child Start=0."
        )
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
    if is_subagent_spawn_tool(payload):
        request_resolution = subagent_request_resolution(payload)
        # While a Hard assessment is due, every parent spawn is either that
        # assessor or an unauthorized early child. This state fact is simpler
        # and more reliable than guessing intent from encrypted prose/name.
        causal_active = _safe_causal_review(state.get("causal_review")).get(
            "state"
        ) in {"triage_required", "triaging"}
        assessor_intent = bool(
            not causal_active
            and active_hard_lifecycle(state)
            and state.get("assessor_state")
            in {"spawn_required", "spawn_pending", "running", "recovery_required"}
        )
        assessor_ok, assessor_reason = confirmed_assessor_request(payload, state)
        if causal_active:
            assessor_ok = False
        if assessor_ok:
            assessor_decision: dict[str, Any] = {
                "accepted": False,
                "reason": "stale assessor request",
            }

            def record_assessor(current: dict[str, Any]) -> None:
                current_valid, current_reason = confirmed_assessor_request(
                    payload, current
                )
                if not current_valid:
                    assessor_decision["reason"] = (
                        current_reason or "assessor request no longer matches state"
                    )
                    return
                if any(
                    group.get("state") in {"pending", "result_pending", "live"}
                    and isinstance(group.get("request"), dict)
                    and group["request"].get("role") == "high_assessor"
                    for group in subagent_lifecycle_groups(current)
                ):
                    assessor_decision["reason"] = (
                        "one high assessor is already pending or live"
                    )
                    return
                options = subagent_request_options(payload)
                task_name, scope_fingerprint = subagent_request_fields(payload)
                sequence = next_sequence(current.get("assessor_attempt"))
                candidate = json.loads(canonical_json(current))
                candidate["assessor_state"] = "spawn_pending"
                candidate["assessor_attempt"] = sequence
                candidate["assessor_model"] = safe_label(
                    options.get("model"), 80
                )
                candidate["assessor_reasoning_effort"] = safe_label(
                    options.get("reasoning_effort"), 24
                )
                candidate["assessor_fork_turns"] = options.get("fork_turns")
                candidate["assessment_liveness"] = _empty_assessment_liveness()
                candidate.setdefault("subagents", []).append(
                    {
                        "at": utc_now(),
                        "event": "request",
                        "epoch_id": current_task_epoch_id(candidate),
                        "turn_id": safe_label(payload.get("turn_id"), 120)
                        if payload.get("turn_id")
                        else None,
                        "task_name": task_name,
                        "scope_fingerprint": scope_fingerprint,
                        "status": "pending",
                        "requested": True,
                        "host_accepted": None,
                        "request_gate": "open",
                        "request_visibility": subagent_request_visibility(payload),
                        "request_cap": 1,
                        "role": "high_assessor",
                        "contract_id": candidate.get("assessor_binding_id"),
                        "request_fingerprint": fingerprint,
                        "objective_fingerprint": candidate.get("objective", {}).get(
                            "fingerprint"
                        ),
                        "model": candidate["assessor_model"],
                        "reasoning_effort": candidate[
                            "assessor_reasoning_effort"
                        ],
                        "fork_turns": candidate["assessor_fork_turns"],
                        "attempt": sequence,
                    }
                )
                if not state_within_budget(candidate):
                    assessor_decision["reason"] = (
                        "state byte/node budget cannot reserve another assessor sequence"
                    )
                    return
                current.clear()
                current.update(candidate)
                assessor_decision["accepted"] = True

            mutate_state(payload, record_assessor)
            if not assessor_decision["accepted"]:
                emit_pretool_deny(
                    "Workflow Manager blocked assessor reservation: "
                    f"{assessor_decision['reason']}."
                )
            return
        request_text = subagent_request_text(payload)
        if assessor_intent:
            if assessor_reason and "spawn envelope conflict" in assessor_reason:
                def record_spawn_envelope_conflict(current: dict[str, Any]) -> None:
                    record_lifecycle_diagnostic(
                        current,
                        "spawn_envelope_conflict",
                        level="warning",
                        role="high_assessor",
                        contract_id=current.get("assessor_binding_id"),
                    )

                mutate_state(payload, record_spawn_envelope_conflict)
            emit_pretool_deny(
                f"Workflow Manager blocked assessor spawn: {assessor_reason or 'invalid assessor request'}."
            )
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
            # A rejected request is not an executor lifecycle event. Keep the
            # current sequence, failure identity, and profile unchanged.

        mutate_state(payload, record_executor_guard)
        expected_profile = expected_executor_profile(state)
        if expected_profile.get("profile") == "work_executor_highest_available":
            profile_instruction = (
                "Use profile_resolution=highest_available and explicitly request "
                f"model={expected_profile.get('model')} with reasoning_effort="
                f"{expected_profile.get('reasoning_effort')}"
            )
        else:
            profile_instruction = (
                "Resolve the newest actually available lower-tier model and explicitly request "
                "reasoning_effort=medium"
            )
        emit_pretool_deny(
            f"Workflow Manager blocked {executor_block}: this confirmed Hard contract permits exactly one "
            "writer at a time. The parent may write only while no child is reserved, live, or unknown; "
            f"otherwise {profile_instruction}, fork_turns=1, and use any safe ASCII task_name."
        )
        return
    if is_subagent_spawn_tool(payload):
        valid_executor, _ = confirmed_executor_request(payload, state)
        if valid_executor:
            if record_confirmed_executor_pretool(payload, state, fingerprint):
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
            "Continue read-only analysis or present an ordinary plan, then wait for confirmation."
        )
        return
    parent_mutation = plan_confirmation_guard(
        payload, {**state, "plan_state": "awaiting_confirmation", "confirmed_plan_digest": None},
    )
    if (parent_mutation and state.get("work_difficulty") == "hard"
            and state.get("plan_state") == "confirmed"
            and not payload_claims_child_identity(payload)):
        fixed_guard = command_guard(payload)
        if fixed_guard:
            _, reason = fixed_guard
            emit_pretool_deny(reason)
            return
        parent_command = extract_command(payload) or ""
        if command_mutates_device(parent_command):
            emit_pretool_deny(
                "Workflow Manager blocked device mutation: a parent writer lease does not relax the fixed device boundary."
            )
            return
        decision = {"acquired": False, "reason": None}
        def reserve_parent(current: dict[str, Any]) -> None:
            decision["reason"] = parent_writer_acquisition_block(current)
            if decision["reason"] is None and acquire_parent_writer_lease(current):
                decision["acquired"] = True
        mutate_state(payload, reserve_parent)
        if not decision["acquired"]:
            emit_pretool_deny("Workflow Manager blocked parent mutation: "
                              + str(decision["reason"] or "the unique writer lease is unavailable") + ".")
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

    # All remaining ordinary tool and subagent scheduling decisions are native
    # Codex behavior.  The Hook is silent after its fixed safety/contract gates.


def _mailbox_agents_payload(
    value: Any, depth: int = 0
) -> tuple[list[Any] | None, bool]:
    """Unwrap only the bounded host list-agents response envelope."""
    if depth > 5:
        return None, True
    if isinstance(value, str):
        stripped = value.strip()
        if not _json_container_text(stripped) or len(stripped) > 256_000:
            return None, False
        try:
            return _mailbox_agents_payload(json.loads(stripped), depth + 1)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None, False
    if isinstance(value, (list, tuple)):
        if len(value) != 1:
            return None, bool(value)
        return _mailbox_agents_payload(value[0], depth + 1)
    if not isinstance(value, dict) or len(value) > 32:
        return None, False
    agent_keys = [key for key in value if normalized_key(key) == "agents"]
    if agent_keys:
        if len(agent_keys) != 1 or not isinstance(value[agent_keys[0]], list):
            return None, True
        agents = value[agent_keys[0]]
        return (agents, False) if len(agents) <= 64 else (None, True)
    wrapper_keys = [
        key
        for key in value
        if normalized_key(key)
        in {"content", "output", "result", "toolresponse", "response", "text"}
    ]
    candidates: list[list[Any]] = []
    ambiguous = False
    for key in wrapper_keys:
        agents, child_ambiguous = _mailbox_agents_payload(value[key], depth + 1)
        ambiguous = ambiguous or child_ambiguous
        if agents is not None:
            candidates.append(agents)
    return (
        (candidates[0], False)
        if len(candidates) == 1 and not ambiguous
        else (None, bool(candidates) or ambiguous)
    )


def _mailbox_task_matches(agent_name: Any, expected_task_name: str) -> bool:
    if not isinstance(agent_name, str) or not expected_task_name:
        return False
    normalized = agent_name.replace("\\", "/").rstrip("/")
    return normalized == expected_task_name or normalized.endswith(
        "/" + expected_task_name
    )


def mailbox_completed_result(
    response: Any, expected_task_name: str
) -> str | None:
    """Return one explicit completed mailbox result for exactly one task."""
    agents, ambiguous = _mailbox_agents_payload(response)
    if ambiguous or agents is None:
        return None
    matches: list[tuple[str | None, bool]] = []
    for item in agents:
        if not isinstance(item, dict) or len(item) > 16:
            continue
        name_keys = [key for key in item if normalized_key(key) == "agentname"]
        status_keys = [key for key in item if normalized_key(key) == "agentstatus"]
        if len(name_keys) != 1 or not _mailbox_task_matches(
            item[name_keys[0]], expected_task_name
        ):
            continue
        if len(status_keys) != 1:
            matches.append((None, True))
            continue
        status = item[status_keys[0]]
        if isinstance(status, dict):
            keys = [normalized_key(key) for key in status]
            completed_keys = [
                key for key in status if normalized_key(key) == "completed"
            ]
            if (
                len(status) == 1
                and keys == ["completed"]
                and len(completed_keys) == 1
                and isinstance(status[completed_keys[0]], str)
                and status[completed_keys[0]].strip()
            ):
                matches.append((status[completed_keys[0]], False))
            else:
                matches.append((None, bool(completed_keys)))
        else:
            matches.append((None, False))
    if len(matches) != 1 or matches[0][1]:
        return None
    return matches[0][0]


def _inventory_live_binding(
    state: dict[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    """Find one current live writer without selecting by agent id alone."""
    liveness = _safe_child_liveness(state.get("child_liveness"))
    for role in ("confirmed_executor", "high_assessor"):
        request, started = _current_bound_live_writer(state, role)
        if request and started:
            return _writer_liveness_binding(
                state, role, request=request, agent_id=started.get("agent_id")
            ), request, started
    # A pending assessor reservation is not evidence that a child started,
    # but a host-complete inventory can still prove this exact reservation is
    # absent. Keep executors stricter: they still require a full Start.
    if state.get("assessor_state") == "spawn_pending" and state.get("assessor_agent_id") is None:
        pending = [
            group.get("request") for group in subagent_lifecycle_groups(state)
            if group.get("state") == "pending"
            and isinstance(group.get("request"), dict)
            and group["request"].get("role") == "high_assessor"
            and group["request"].get("epoch_id") == current_task_epoch_id(state)
            and group["request"].get("contract_id") == state.get("assessor_binding_id")
            and safe_sequence(group["request"].get("attempt")) == safe_sequence(state.get("assessor_attempt"))
            and group["request"].get("requested") is True
            and group["request"].get("host_accepted") is not False
        ]
        if len(pending) == 1:
            return _writer_liveness_binding(state, "high_assessor", request=pending[0]), pending[0], None
    # A Start observed before its matching Post is live but mutation-locked.
    # It can only be matched through its recorded epoch/request/agent triple,
    # never through a later agent-id reuse.
    if liveness.get("status") not in {"live", "unknown"}:
        return None, None, None
    role = liveness.get("role")
    for group in subagent_lifecycle_groups(state):
        request = group.get("request")
        started = group.get("start")
        if not (
            group.get("state") == "live" and isinstance(request, dict)
            and isinstance(started, dict) and request.get("role") == role
            and started.get("role") == role
            and request.get("epoch_id") == liveness.get("epoch_id")
            and started.get("epoch_id") == liveness.get("epoch_id")
            and request.get("request_fingerprint") == liveness.get("request_fingerprint")
            and _lifecycle_agent_fingerprint(
                started.get("agent_id"), request.get("request_fingerprint")
            ) == liveness.get("agent_fingerprint")
        ):
            continue
        expected = lifecycle_binding_fingerprint(
            epoch_id=started.get("epoch_id"), role=role,
            agent_id=started.get("agent_id"),
            request_fingerprint=request.get("request_fingerprint"),
            contract_id=request.get("contract_id"), attempt=request.get("attempt"),
        )
        if expected and started.get("lifecycle_fingerprint") == expected:
            return _writer_liveness_binding(
                state, role, request=request, agent_id=started.get("agent_id")
            ), request, started
    return None, None, None


def _inventory_status_value(value: Any) -> str:
    if isinstance(value, dict):
        completed = [key for key in value if normalized_key(key) == "completed"]
        if len(value) == len(completed) == 1:
            return "terminal"
        return "unknown"
    if not isinstance(value, str):
        return "unknown"
    normalized = normalized_key(value)
    if normalized in {"running", "live", "active", "working", "pending", "queued"}:
        return "live"
    if normalized in {"completed", "complete", "done", "succeeded", "failed", "cancelled", "canceled"}:
        return "terminal"
    if normalized in {"absent", "notfound", "agentnotfound", "capacity", "capacityerror", "sigkill", "killed"}:
        return "absent"
    return "unknown"


def _inventory_response_is_complete(value: Any, depth: int = 0) -> bool:
    """Accept an absence only from an explicit host-complete inventory fact."""
    if depth > 4 or not isinstance(value, dict) or len(value) > 32:
        return False
    complete_fields = {
        "complete", "inventorycomplete", "completeinventory", "iscomplete",
    }
    direct = [
        raw for raw, item in value.items()
        if normalized_key(raw) in complete_fields and item is True
    ]
    if len(direct) == 1:
        return True
    if direct:
        return False
    wrappers = {"receipt", "result", "response", "toolresponse", "structuredcontent"}
    nested = [
        _inventory_response_is_complete(item, depth + 1)
        for raw, item in value.items()
        if normalized_key(raw) in wrappers
    ]
    return len(nested) == 1 and nested[0]


def writer_inventory_observation(
    response: Any, binding: dict[str, Any], request: dict[str, Any]
) -> str:
    """Classify a structured host inventory; incomplete shapes are unknown."""
    agents, ambiguous = _mailbox_agents_payload(response)
    if ambiguous or agents is None:
        return "unknown"
    task = str(request.get("task_name") or "")
    if not task:
        return "unknown"
    matches: list[str] = []
    for item in agents:
        if not isinstance(item, dict) or len(item) > 16:
            return "unknown"
        name_keys = [key for key in item if normalized_key(key) == "agentname"]
        status_keys = [key for key in item if normalized_key(key) == "agentstatus"]
        if len(name_keys) != 1:
            continue
        if not _mailbox_task_matches(item.get(name_keys[0]), task):
            continue
        if len(status_keys) != 1:
            return "unknown"
        matches.append(_inventory_status_value(item.get(status_keys[0])))
    if not matches:
        # A list that omits the task may be filtered or partial.  Releasing a
        # writer needs an independently explicit complete/absent fact, not an
        # inference from a convenient-looking list shape.
        return "absent" if _inventory_response_is_complete(response) else "unknown"
    return matches[0] if len(matches) == 1 else "unknown"


def explicit_host_writer_absence(response: Any, depth: int = 0) -> str | None:
    """Recognize only structured capacity/SIGKILL host facts, never stdout."""
    if depth > 4 or not isinstance(response, dict) or len(response) > 32:
        return None
    values: set[str] = set()
    direct_fields = {"status", "errorcode", "code", "reason"}
    for raw_key, value in response.items():
        key = normalized_key(raw_key)
        if key in direct_fields and isinstance(value, str):
            normalized = normalized_key(value)
            if normalized in {"capacity", "capacityerror", "resourceexhausted"}:
                values.add("capacity")
            elif normalized in {"sigkill", "killed", "killedbyhost"}:
                values.add("sigkill")
        elif key in {"receipt", "result", "response", "toolresponse", "structuredcontent"}:
            nested = explicit_host_writer_absence(value, depth + 1)
            if nested:
                values.add(nested)
    return next(iter(values)) if len(values) == 1 else None


def reconcile_writer_inventory(
    state: dict[str, Any], response: Any, payload: dict[str, Any]
) -> str | None:
    """Consume a host inventory observation without trusting child output."""
    # wait_agent/wait_threads can report a completion notification but do not
    # promise a complete writer inventory.  Only the dedicated list_agents
    # host result can establish live/absent/unknown ownership facts; terminal
    # mailbox payloads remain subject to the stricter result reconciler above.
    if "listagents" not in normalized_key(payload.get("tool_name")):
        return None
    binding, request, _ = _inventory_live_binding(state)
    if not binding or not request:
        return None
    observation = writer_inventory_observation(response, binding, request)
    if observation == "live":
        _set_writer_liveness(
            state, status="live", binding=binding, source="host_inventory",
            observation={"event": "PostToolUse", "status": "live"},
        )
        record_lifecycle_diagnostic(
            state, "inventory_writer_live", level="info", role=binding.get("role"),
            request_fingerprint=binding.get("request_fingerprint"),
            contract_id=binding.get("contract_id"),
        )
        return "live"
    if observation == "absent":
        _release_writer_after_explicit_absence(
            state, binding=binding, reason="absent", source="host_inventory",
            observation={"event": "PostToolUse", "status": "absent"},
        )
        return "absent"
    if observation == "terminal":
        # A mailbox terminal result needs the stricter exact result protocol
        # in ``reconcile_bound_mailbox_terminal`` above.  A bare completed
        # status cannot close or release this writer, so retain it as unknown.
        _mark_writer_inventory_unknown(
            state, binding=binding, source="host_inventory",
            observation={"event": "PostToolUse", "status": "terminal_unbound"},
            preserve_writer=True,
        )
        return "unknown"
    _mark_writer_inventory_unknown(
        state, binding=binding, source="host_inventory",
        observation={"event": "PostToolUse", "status": "unknown"},
        preserve_writer=True,
    )
    return "unknown"


def _mailbox_reported_recovery_facts(body: str) -> dict[str, Any]:
    """Extract only a unique typed failure identity from a terminal report."""
    explicit = {
        value.lower()
        for value in re.findall(
            r"(?:recovery_from|recovery-from)\s*[:=]\s*([a-z_]+)\b",
            body,
            re.I,
        )
    }
    observed = {
        value
        for value in EXECUTOR_FAILURE_KINDS
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(value)}(?![A-Za-z0-9_])", body)
    }
    kinds = explicit or observed
    if len(kinds) != 1:
        return {}
    failure = next(iter(kinds))

    def one_line(name: str, domain: str) -> str | None:
        values = re.findall(
            rf"(?im)^\s*`?{name}=([0-9a-f]{{32,64}})`?\s*$", body
        )
        normalized = {
            value
            for raw in values
            if (value := _normalized_recovery_digest(raw, domain)) is not None
        }
        return next(iter(normalized)) if len(normalized) == 1 else None

    failure_fingerprint = one_line(
        "failure_fingerprint", "workflow-manager-external-failure-v1"
    )
    evidence = one_line(
        "evidence_digest", "workflow-manager-external-evidence-v1"
    )
    return (
        {
            "failure_kind": failure,
            "failure_fingerprint": failure_fingerprint,
            "evidence_digest": evidence,
        }
        if failure_fingerprint and evidence
        else {}
    )


def reconcile_bound_mailbox_terminal(
    state: dict[str, Any], response: Any, payload: dict[str, Any]
) -> str | None:
    """Persist an equivalent boundary when the host omits SubagentStop.

    This never manufactures a SubagentStop event.  It accepts only the one
    current request/Post/full-Start executor and one host-structured completed
    mailbox result whose final marker matches its contract and slice.
    """
    staged = _safe_pending_recovery_reservation(
        state.get("pending_recovery_reservation")
    )
    group = _unique_running_executor_group(
        state, staged if staged and staged.get("stage") == "terminal_pending" else None
    )
    if group is None:
        return None
    request = group["request"]
    started = group["start"]
    result = mailbox_completed_result(response, str(request.get("task_name") or ""))
    if result is None:
        return None
    marker, body, intent = _strict_terminal_marker(
        result, "EXECUTION_RESULT", EXECUTION_RESULT_RE
    )
    current = current_execution_slice(state) or {}
    if not (
        intent
        and marker
        and marker.group(1) == state.get("execution_contract_id")
        and marker.group(2) == current.get("id")
        and request.get("contract_id") == state.get("execution_contract_id")
        and request.get("slice_contract_id") == slice_contract_id(state)
        and safe_sequence(request.get("attempt"))
        == safe_sequence(state.get("executor_attempt"))
    ):
        return None
    outcome = marker.group(3)
    reported = (
        _mailbox_reported_recovery_facts(body) if outcome == "failed" else {}
    )
    candidate_evidence = host_evidence_digest(
        domain="executor-result-v1",
        state=state,
        agent_id=group.get("agent_id"),
        request_fingerprint=request.get("request_fingerprint"),
        body_without_marker=body,
        outcome=outcome,
        terminal_status="completed",
        terminal_status_source="host_declared_success",
    )
    state.setdefault("subagents", []).append(
        {
            "at": utc_now(),
            "event": "mailbox_terminal",
            "epoch_id": current_task_epoch_id(state),
            "lifecycle_fingerprint": lifecycle_binding_fingerprint(
                epoch_id=current_task_epoch_id(state), role="confirmed_executor",
                agent_id=group.get("agent_id"),
                request_fingerprint=request.get("request_fingerprint"),
                contract_id=request.get("contract_id"), attempt=request.get("attempt"),
            ),
            "turn_id": safe_label(payload.get("turn_id"), 120)
            if payload.get("turn_id")
            else None,
            "agent_id": group.get("agent_id"),
            "agent_type": started.get("agent_type"),
            "task_name": request.get("task_name"),
            "scope_fingerprint": request.get("scope_fingerprint"),
            "request_fingerprint": request.get("request_fingerprint"),
            "objective_fingerprint": request.get("objective_fingerprint"),
            "stale": False,
            "status": "completed",
            "result_meta": text_metadata(result),
            "execution_result_contract_match": True,
            "execution_result_outcome": outcome,
            "execution_result_evidence_digest": candidate_evidence,
            "evidence_digest_profile": EVIDENCE_DIGEST_PROFILE,
            "evidence_digest_source": EVIDENCE_DIGEST_SOURCE,
            "terminal_status": "completed",
            "terminal_status_source": "host_declared_success",
            "terminal_lifecycle_source": "mailbox_completed",
            "reported_failure_kind": reported.get("failure_kind"),
            "reported_failure_fingerprint": reported.get("failure_fingerprint"),
            "reported_evidence_digest": reported.get("evidence_digest"),
            "role": "confirmed_executor",
            "contract_id": request.get("contract_id"),
            "slice_id": request.get("slice_id"),
            "slice_contract_id": request.get("slice_contract_id"),
            "model": started.get("model"),
            "reasoning_effort": started.get("reasoning_effort"),
            "fork_turns": started.get("fork_turns"),
            "attempt": request.get("attempt"),
        }
    )
    _set_writer_liveness(
        state, status="terminal",
        binding=_writer_liveness_binding(
            state, "confirmed_executor", request=request,
            agent_id=group.get("agent_id"),
        ),
        source="host_inventory",
        observation={"event": "mailbox_terminal", "outcome": outcome},
    )
    state.setdefault("guards", []).append(
        {
            "at": utc_now(),
            "turn_id": safe_label(payload.get("turn_id"), 120)
            if payload.get("turn_id")
            else None,
            "kind": "mailbox_terminal_reconciled",
            "action": "advise",
            "fingerprint": stable_hash(
                "workflow-manager-mailbox-terminal-v1\0"
                + canonical_json(
                    {
                        "agent": stable_hash(str(group.get("agent_id")), 32),
                        "attempt": safe_sequence(request.get("attempt")),
                        "contract": request.get("contract_id"),
                        "result": text_metadata(result).get("fingerprint"),
                        "slice": request.get("slice_id"),
                    }
                ),
                32,
            ),
        }
    )
    state["executor_agent_id"] = None
    state["model_profile"] = "work_assessment"
    if outcome == "succeeded":
        state["executor_state"] = "verification_required"
        state["executor_failure_kind"] = None
        state["pending_recovery_reservation"] = None
        baseline = build_execution_baseline(state)
        if baseline:
            baseline["acceptance_status"] = "incomplete"
            state["last_execution_baseline"] = baseline
            state["causal_review"] = _safe_causal_review(None)
        state["executor_review"] = _safe_executor_review(
            {
                "status": "review_required",
                "execution_contract_id": state.get("execution_contract_id"),
                "slice_id": current.get("id"),
                "slice_contract_id": slice_contract_id(state),
                "attempt": state.get("executor_attempt"),
                "candidate_result_fingerprint": stable_hash(body, 32),
                "candidate_agent_fingerprint": stable_hash(
                    str(group.get("agent_id")), 32
                ),
                "candidate_evidence_digest": candidate_evidence,
                "child_summary_digest": _acceptance_summary_digest(body, state),
                "review_evidence_digest": None,
                "digest_profile": EVIDENCE_DIGEST_PROFILE,
                "digest_source": EVIDENCE_DIGEST_SOURCE,
                "terminal_status": "completed",
                "terminal_status_source": "host_declared_success",
                "at": utc_now(),
            }
        )
        return outcome
    state["executor_state"] = "recovery_required"
    state["executor_failure_kind"] = (
        reported.get("failure_kind") or "executor_failed"
    )
    promoted = None
    if staged and staged.get("stage") == "terminal_pending":
        if reported and all(
            staged.get(key) == reported.get(key)
            for key in (
                "failure_kind",
                "failure_fingerprint",
                "evidence_digest",
            )
        ):
            promoted = {
                key: value
                for key, value in staged.items()
                if key
                not in {
                    "stage",
                    "terminal_attempt",
                    "terminal_agent_id",
                    "terminal_task_name",
                    "terminal_request_fingerprint",
                }
            }
        else:
            state.setdefault("guards", []).append(
                {
                    "at": utc_now(),
                    "turn_id": safe_label(payload.get("turn_id"), 120)
                    if payload.get("turn_id")
                    else None,
                    "kind": "recovery_reservation",
                    "action": "deny",
                    "fingerprint": stable_hash(
                        "workflow-manager-early-recovery-mismatch-v1\0"
                        + str(staged.get("prompt_receipt") or ""),
                        32,
                    ),
                }
            )
    state["pending_recovery_reservation"] = (
        pending_recovery_reservation_for_state(state, promoted)
        if promoted
        else None
    )
    return outcome


def post_tool_use(payload: dict[str, Any]) -> None:
    fingerprint, tool = tool_fingerprint(payload)
    response = payload.get("tool_response")
    tool_key = normalized_key(payload.get("tool_name"))
    status_value = (
        apply_patch_response_status(response)
        if tool_key == "applypatch"
        else response_status(response)
    )
    exec_envelope_status: str | None = None
    exec_leaf_status: str | None = None
    if tool_key in {"functionsexec", "exec"}:
        # A PostToolUse event for the outer custom tool is admissible only when
        # exactly one inner shell/session receipt is present.  Keep both facts
        # in the ledger for parent diagnosis, but make the leaf authoritative.
        exec_envelope_status, exec_leaf_status = host_exec_receipt_statuses(response)
        status_value = exec_leaf_status or "unknown"
    requested_spawn_task_name: str | None = None
    if is_subagent_spawn_tool(payload):
        requested_spawn_task_name, _ = subagent_request_fields(payload)
        status_value = spawn_response_status(response, requested_spawn_task_name)
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
    parent_child_collection = bool(
        caller_id is None
        and any(
            marker in tool_key
            for marker in ("waitagent", "listagents", "waitthreads")
        )
    )
    continuation_ack_key = (
        trusted_posttool_continuation_ack(response)
        if caller_id is None and not payload_claims_child_identity(payload)
        else None
    )
    host_writer_absence = explicit_host_writer_absence(response)
    runtime_delivery: dict[str, str] = {}
    liveness_delivery: dict[str, str] = {}

    def update(state: dict[str, Any]) -> None:
        if continuation_ack_key:
            if consume_continuation_lease(
                state, continuation_ack_key, source="host_posttool", receipt=response
            ):
                runtime_delivery["continuation_ack"] = "host_posttool"
        bound_role, bound_request, bound_started = bound_writer_for_posttool(
            state, payload
        )
        known_bound_roles = known_writer_roles(state, caller_id)
        if caller_id and known_bound_roles and not bound_role:
            tombstone_late_lifecycle_event(
                state, payload, status="late_post",
            )
            record_lifecycle_diagnostic(
                state, "late_event_epoch_ambiguous", level="warning",
                role=(
                    "confirmed_executor"
                    if caller_id == state.get("executor_agent_id")
                    else "high_assessor"
                    if caller_id == state.get("assessor_agent_id")
                    else None
                ),
            )
            return
        # A parent wait/list is an observation only.  A child may explicitly
        # attach a bounded progress digest, but status/running/parent polling
        # and stale events never reset the idle clock.
        progress = None
        if caller_id and caller_id == state.get("assessor_agent_id"):
            candidate = payload.get("assessment_progress_digest")
            progress = candidate if isinstance(candidate, str) else None
        action = assessment_liveness_tick(state, progress_digest=progress)
        if action in {"unblock_required", "recovery_required"}:
            liveness_delivery["action"] = action
        liveness = _safe_assessment_liveness(state.get("assessment_liveness"))
        if (
            caller_id is None and tool_key.endswith("followuptask")
            and liveness.get("unblock") == "pending"
            and liveness.get("agent_id") == state.get("assessor_agent_id")
        ):
            liveness["unblock"] = "failed" if status_value in ERROR_STATUSES or status_value.startswith("error") else "delivered"
            liveness["unblock_at"] = _liveness_now()
            state["assessment_liveness"] = liveness
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
                            "epoch_id": current_task_epoch_id(state),
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
            bound_role == "confirmed_executor"
            and _safe_child_liveness(state.get("child_liveness")).get("status")
            == "live"
        )
        active_parent_writer = bool(caller_id is None and parent_writer_lease_current(state))
        recoverable_parent_candidate = bool(
            state.get("executor_state") == "recovery_required"
            and state.get("executor_failure_kind") == "incomplete_execution"
            and any(
                record.get("event") in TERMINAL_SUBAGENT_EVENTS
                and record.get("role") == "confirmed_executor"
                and record.get("contract_id") == state.get("execution_contract_id")
                and record.get("slice_id") == (current_execution_slice(state) or {}).get("id")
                and record.get("slice_contract_id") == slice_contract_id(state)
                and safe_sequence(record.get("attempt"))
                == safe_sequence(state.get("executor_attempt"))
                and record.get("execution_result_contract_match") is True
                and record.get("execution_result_outcome") == "succeeded"
                and _fingerprint32(record.get("execution_result_evidence_digest"))
                for record in as_list(state.get("subagents"))
                if isinstance(record, dict)
            )
        )
        parent_review_operation = bool(
            active_plan_digest
            and caller_id is None
            and (
                state.get("executor_state") == "verification_required"
                or recoverable_parent_candidate
            )
            and category in {"verification", "evidence"}
            and not command_mutates_files(command or "")
            and not command_risk_kind(payload, command or "")
            and not git_subcommand(command or "")
        )
        bind_current_slice = active_executor_caller or active_parent_writer or parent_review_operation
        state.setdefault("operations", []).append(
            {
                "at": utc_now(),
                "turn_id": safe_label(payload.get("turn_id"), 120) if payload.get("turn_id") else None,
                "host_event_turn_id": safe_label(payload.get("turn_id"), 120) if payload.get("turn_id") else None,
                "host_input_digest": host_input_digest,
                "host_command_digest": (
                    stable_hash(
                        "host-operation-command-text-v1\0"
                        + command.replace("\r\n", "\n").replace("\r", "\n"),
                        32,
                    )
                    if command
                    else None
                ),
                "reconciliation_source": (
                    "host_posttool_patch_receipt_v2"
                    if tool_key == "applypatch"
                    and host_apply_patch_receipt_success(response)
                    else None
                ),
                "tool": tool,
                "fingerprint": fingerprint,
                "status": status_value,
                "envelope_status": exec_envelope_status,
                "leaf_status": exec_leaf_status,
                "category": category,
                "plan_digest": active_plan_digest,
                "execution_contract_id": (
                    state.get("execution_contract_id")
                    if active_plan_digest and (active_executor_caller or active_parent_writer or parent_review_operation)
                    else None
                ),
                "epoch_id": current_task_epoch_id(state),
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
                "assessor_binding_id": (
                    state.get("assessor_binding_id")
                    if bound_role == "high_assessor" else None
                ),
                "risk_kind": risk_kind,
                **response_meta,
                "budgeted": budgeted,
                "oversized": oversized,
                "compacted": compacted,
                "change_epoch": epoch_before,
            }
        )
        failed = bool(
            status_value.startswith("error")
            or status_value in ERROR_STATUSES
            or host_writer_absence
        )
        if active_parent_writer:
            lease = _safe_parent_writer_lease(state.get("parent_writer_lease"))
            lease["last_operation_digest"] = fingerprint
            state["parent_writer_lease"] = lease
            if (not failed and category in {"verification", "evidence"}
                    and slice_operation_evidence(state).get("change_evidence")):
                candidate = stable_hash("workflow-manager-parent-writer-candidate-v1\0" + canonical_json({
                    "attempt": state.get("executor_attempt"), "contract": state.get("execution_contract_id"),
                    "operation": fingerprint, "slice": (current_execution_slice(state) or {}).get("id"),
                }), 32)
                state["executor_review"] = _safe_executor_review({
                    "status": "review_required", "attempt": state.get("executor_attempt"),
                    "execution_contract_id": state.get("execution_contract_id"),
                    "slice_id": (current_execution_slice(state) or {}).get("id"),
                    "slice_contract_id": slice_contract_id(state),
                    "candidate_result_fingerprint": candidate,
                    "candidate_agent_fingerprint": stable_hash("parent", 32),
                    "candidate_evidence_digest": slice_operation_evidence(state).get("operation_digest"),
                    "child_summary_digest": stable_hash("parent-writer-host-operations\0" + candidate, 32),
                    "terminal_status": "completed", "terminal_status_source": "host_declared_success", "at": utc_now(),
                })
                state["executor_state"] = "verification_required"
                state["executor_failure_kind"] = None
        # Only a completed implementation, build artifact, or deployment moves
        # the freshness boundary. Failed/denied probes never manufacture a new
        # epoch and therefore cannot weaken acceptance.
        if (
            not failed
            and category in {"implementation", "build_package", "delivery_device"}
            and (caller_id is None or active_executor_caller)
        ):
            state["change_epoch"] = min(epoch_before + 1, MAX_EVENT_COUNT)
        pending_spawn = next(
            (
                item for item in reversed(state.get("subagents", []))
                if item.get("event") == "request"
                and item.get("epoch_id") == current_task_epoch_id(state)
                and item.get("request_fingerprint") == fingerprint
            ),
            None,
        )
        # PreToolUse is only a request reservation.  A matching PostToolUse is
        # the sole host-acceptance signal and remains separate from Start.
        if pending_spawn:
            accepted_value = (
                True
                if status_value in SUCCESS_STATUSES | {"running"}
                else False
                if failed
                else None
            )
            receipt_digest = spawn_acceptance_receipt_digest(
                response,
                pending_spawn.get("task_name") or requested_spawn_task_name,
                status_value,
            )
            prior_post = pending_spawn.get("host_acceptance_source") == "PostToolUse"
            idempotent = bool(
                prior_post
                and not pending_spawn.get("host_acceptance_conflict")
                and pending_spawn.get("host_acceptance_receipt_digest")
                == receipt_digest
                and pending_spawn.get("host_accepted") is accepted_value
                and pending_spawn.get("host_acceptance_status") == status_value
            )
            if prior_post and not idempotent:
                pending_spawn["host_accepted"] = None
                pending_spawn["host_acceptance_status"] = "conflict"
                pending_spawn["host_acceptance_conflict"] = True
                if pending_spawn.get("role") == "high_assessor":
                    state["assessor_state"] = "recovery_required"
                    state["assessor_failure_kind"] = "start_mismatch"
                    state["assessor_agent_id"] = None
                    state["assessor_observed_effective"] = False
                elif pending_spawn.get("role") == "confirmed_executor":
                    state["executor_state"] = "recovery_required"
                    state["executor_failure_kind"] = "start_mismatch"
                    state["executor_agent_id"] = None
                    state["executor_observed_effective"] = False
            elif not prior_post:
                pending_spawn["host_accepted"] = accepted_value
                pending_spawn["host_acceptance_status"] = status_value
                pending_spawn["host_acceptance_source"] = "PostToolUse"
                pending_spawn["host_acceptance_fingerprint"] = fingerprint
                pending_spawn["host_acceptance_receipt_digest"] = receipt_digest
                pending_spawn["host_acceptance_conflict"] = False
                pending_spawn["host_accepted_at"] = utc_now()
            if (
                not pending_spawn.get("host_acceptance_conflict")
                and pending_spawn.get("host_accepted") is True
                and reconcile_post_accepted_bound_start(
                    state, pending_spawn, payload
                )
                and pending_spawn.get("role") == "confirmed_executor"
            ):
                runtime_delivery["late_executor"] = "rebound"
        if failed and pending_spawn and pending_spawn.get("role") == "high_assessor" and state.get("assessor_state") == "spawn_pending":
            if safe_int(pending_spawn.get("attempt")) == safe_int(state.get("assessor_attempt")):
                state["assessor_state"] = "recovery_required"
                state["assessor_failure_kind"] = "model_unavailable"
                absence = host_writer_absence
                if absence:
                    _release_writer_after_explicit_absence(
                        state,
                        binding=_writer_liveness_binding(
                            state, "high_assessor", request=pending_spawn
                        ),
                        reason="capacity" if absence == "capacity" else "sigkill",
                        source="host_lifecycle",
                        observation={"event": "PostToolUse", "absence": absence},
                    )
                else:
                    _mark_writer_inventory_unknown(
                        state,
                        binding=_writer_liveness_binding(
                            state, "high_assessor", request=pending_spawn
                        ),
                        source="host_lifecycle",
                        observation={"event": "PostToolUse", "absence": "unknown"},
                    )
        if failed and pending_spawn and pending_spawn.get("role") == "confirmed_executor" and state.get("executor_state") == "spawn_pending":
            if safe_int(pending_spawn.get("attempt")) == safe_int(state.get("executor_attempt")):
                state["executor_state"] = "recovery_required"
                state["executor_failure_kind"] = "model_unavailable"
                state["model_profile"] = "work_assessment"
                absence = host_writer_absence
                if absence:
                    _release_writer_after_explicit_absence(
                        state,
                        binding=_writer_liveness_binding(
                            state, "confirmed_executor", request=pending_spawn
                        ),
                        reason="capacity" if absence == "capacity" else "sigkill",
                        source="host_lifecycle",
                        observation={"event": "PostToolUse", "absence": absence},
                    )
                else:
                    _mark_writer_inventory_unknown(
                        state,
                        binding=_writer_liveness_binding(
                            state, "confirmed_executor", request=pending_spawn
                        ),
                        source="host_lifecycle",
                        observation={"event": "PostToolUse", "absence": "unknown"},
                    )
        if host_writer_absence and bound_role and bound_request:
            # A structured host SIGKILL/capacity result attached to the exact
            # live child is an explicit writer-absence fact, not child prose.
            # Tombstone and release it before any failed-operation routing can
            # accidentally retain the old writer slot.
            _release_writer_after_explicit_absence(
                state,
                binding=_writer_liveness_binding(
                    state, bound_role, request=bound_request,
                    agent_id=(bound_started or {}).get("agent_id"),
                ),
                reason=("capacity" if host_writer_absence == "capacity" else "sigkill"),
                source="host_lifecycle",
                observation={"event": "PostToolUse", "absence": host_writer_absence},
            )
        executor_operation = bool(
            active_executor_caller and state.get("executor_state") == "running"
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
            state["executor_state"] = "recovery_required"
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
            stall["state"] = "diagnosis_required"
            stall["at"] = utc_now()
            state["stall"] = stall
            state["executor_state"] = "recovery_required"
            state["model_profile"] = "work_assessment"
        if telemetry:
            state["telemetry"] = telemetry

        if parent_child_collection:
            mailbox_outcome = reconcile_bound_mailbox_terminal(
                state, response, payload
            )
            if mailbox_outcome:
                runtime_delivery["mailbox_terminal"] = mailbox_outcome
            if host_writer_absence:
                inventory_binding, inventory_request, _ = _inventory_live_binding(state)
                if inventory_binding and inventory_request:
                    _release_writer_after_explicit_absence(
                        state, binding=inventory_binding,
                        reason=("capacity" if host_writer_absence == "capacity" else "sigkill"),
                        source="host_inventory",
                        observation={
                            "event": "PostToolUse", "absence": host_writer_absence,
                        },
                    )
                    inventory_outcome = "absent"
                else:
                    inventory_outcome = None
            else:
                inventory_outcome = reconcile_writer_inventory(state, response, payload)
            if inventory_outcome:
                runtime_delivery["writer_inventory"] = inventory_outcome
            role: str | None = None
            role_binding = ""
            if state.get("executor_state") in {
                "recovery_required",
                "verification_required",
                "exhausted",
            } and state.get("executor_start_observed") in {
                "full",
                "partial",
                "mismatch",
            }:
                role = "confirmed_executor"
                role_binding = "\0".join(
                    (
                        str(state.get("execution_contract_id") or ""),
                        str((current_execution_slice(state) or {}).get("id") or ""),
                        str(safe_int(state.get("executor_attempt"))),
                    )
                )
            elif state.get("assessor_state") in {
                "hard_plan_ready",
                "recovery_required",
                "failed",
            } and state.get("assessor_start_observed") in {
                "full",
                "partial",
                "mismatch",
            }:
                role = "high_assessor"
                role_binding = str(state.get("assessor_binding_id") or "")
            if role and role_binding:
                delivery_fingerprint = stable_hash(
                    f"bound-runtime-truth-parent-v1\0{role}\0{role_binding}", 32
                )
                delivered = any(
                    item.get("kind") == "bound_runtime_truth_parent_delivery"
                    and item.get("fingerprint") == delivery_fingerprint
                    for item in state.get("guards", [])
                )
                if not delivered:
                    state.setdefault("guards", []).append(
                        {
                            "at": utc_now(),
                            "turn_id": safe_label(payload.get("turn_id"), 120)
                            if payload.get("turn_id")
                            else None,
                            "kind": "bound_runtime_truth_parent_delivery",
                            "action": "advise",
                            "fingerprint": delivery_fingerprint,
                        }
                    )
                    runtime_delivery["role"] = role

    updated_state, _ = mutate_state(payload, update)
    if runtime_delivery.get("continuation_ack"):
        emit_context(
            "PostToolUse",
            "Workflow Manager consumed one continuation lease from an exact structured host receipt; stdout alone never acknowledges it.",
        )
    if liveness_delivery.get("action") == "unblock_required":
        emit_context(
            "PostToolUse",
            "Workflow Manager assessor liveness: the current bound sequence has no new progress digest for strictly more than 1200 seconds. Deliver one idempotent diagnosis/unblock request to the live assessor; polling alone is not progress and no successor may overlap it.",
        )
    elif liveness_delivery.get("action") == "recovery_required":
        emit_context(
            "PostToolUse",
            "Workflow Manager assessor liveness: the delivered unblock received a further 600-second observation without progress and the prior Stop is verified. Start one materially corrected non-overlapping successor with any safe ASCII task_name.",
        )
    if runtime_delivery.get("mailbox_terminal"):
        outcome = runtime_delivery["mailbox_terminal"]
        pending = pending_recovery_reservation_for_state(updated_state)
        recovery = recovery_reservation_context(updated_state)
        detail = (
            " The already staged digest-only recovery reservation now matches and is host-bound; use one fresh "
            "executor spawn and never follow up the terminal child."
            if outcome == "failed" and pending
            else f"\n{recovery}"
            if outcome == "failed" and recovery
            else " The executor candidate now requires independent parent acceptance review."
        )
        emit_context(
            "PostToolUse",
            "Workflow Manager reconciled one missing terminal hook from a unique bound mailbox completed result. "
            "This is a mailbox_terminal equivalent boundary, not a fabricated SubagentStop; intermediate, running, "
            "unbound, ambiguous, or contract-mismatched observations remain non-terminal."
            + detail,
        )
    elif runtime_delivery.get("late_executor"):
        emit_context(
            "PostToolUse",
            "Workflow Manager accepted the one exact late spawn receipt, reverified the private canonical journal, and unlocked the already delivered digest-bound executor slice. The request, Post receipt, full Start, model, effort, fork, sequence, objective, contract, and current slice now agree.",
        )
    elif runtime_delivery.get("role"):
        runtime_truth = bound_runtime_truth_summary(
            updated_state, runtime_delivery["role"]
        )
        emit_context(
            "PostToolUse",
            f"Workflow Manager parent-visible {runtime_truth} Report these exact verified fields; when Start=full, "
            "do not describe the runtime echo as absent or unavailable.",
        )
    # Oversized results remain fully available to the host.  Persist bounded
    # metadata only; repeating an advisory after every large result was itself
    # a major source of context growth in long sessions.
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
                "causal_lineage": _safe_causal_lineage(state.get("causal_lineage")),
                "lifecycle_diagnostics": [
                    item for raw in as_list(state.get("lifecycle_diagnostics"))[-4:]
                    if (item := _safe_lifecycle_diagnostic(raw)) is not None
                ],
                "stall": _safe_stall(state.get("stall")),
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


def lifecycle_binding_fingerprint(
    *, epoch_id: Any, role: Any, agent_id: Any, request_fingerprint: Any,
    contract_id: Any, attempt: Any,
) -> str | None:
    epoch = safe_fingerprint(epoch_id)
    request = safe_fingerprint(request_fingerprint)
    contract = _fingerprint32(contract_id)
    agent = safe_label(agent_id, 120) if agent_id else ""
    if not (
        epoch and role in {"high_assessor", "confirmed_executor"}
        and agent and request and contract and safe_sequence(attempt) > 0
    ):
        return None
    return stable_hash(
        "workflow-manager-lifecycle-binding-v1\0"
        + canonical_json(
            {
                "agent": stable_hash(agent, 32), "attempt": safe_sequence(attempt),
                "contract": contract, "epoch": epoch, "request": request,
                "role": role,
            }
        ),
        32,
    )


def _current_bound_live_writer(
    state: dict[str, Any], role: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return one live record only when every current binding component agrees."""
    task_epoch = _safe_task_epoch(state.get("task_epoch"))
    epoch = task_epoch.get("id")
    if not epoch or task_epoch.get("status") != "active":
        return None, None
    expected_contract = (
        state.get("execution_contract_id")
        if role == "confirmed_executor"
        else state.get("assessor_binding_id")
    )
    expected_agent = (
        state.get("executor_agent_id")
        if role == "confirmed_executor"
        else state.get("assessor_agent_id")
    )
    expected_attempt = (
        state.get("executor_attempt")
        if role == "confirmed_executor"
        else state.get("assessor_attempt")
    )
    candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for group in subagent_lifecycle_groups(state):
        request = group.get("request")
        started = group.get("start")
        if (
            group.get("state") != "live" or not isinstance(request, dict)
            or not isinstance(started, dict) or request.get("role") != role
            or started.get("role") != role
            or request.get("epoch_id") != epoch or started.get("epoch_id") != epoch
            or request.get("contract_id") != expected_contract
            or started.get("contract_id") != expected_contract
            or safe_sequence(request.get("attempt")) != safe_sequence(expected_attempt)
            or safe_sequence(started.get("attempt")) != safe_sequence(expected_attempt)
            or str(started.get("agent_id") or "") != str(expected_agent or "")
        ):
            continue
        expected_fingerprint = lifecycle_binding_fingerprint(
            epoch_id=epoch, role=role, agent_id=started.get("agent_id"),
            request_fingerprint=request.get("request_fingerprint"),
            contract_id=expected_contract, attempt=expected_attempt,
        )
        if not expected_fingerprint or started.get("lifecycle_fingerprint") != expected_fingerprint:
            continue
        candidates.append((request, started))
    return candidates[0] if len(candidates) == 1 else (None, None)


def lifecycle_event_matches_start(
    payload: dict[str, Any], request: dict[str, Any], started: dict[str, Any],
    state: dict[str, Any] | None = None,
) -> bool:
    """Optional host fields constrain a Start/Stop/PostTool; none may widen it."""
    payload_epoch = safe_fingerprint(
        payload.get("task_epoch_id") or payload.get("epoch_id")
    )
    if payload_epoch and payload_epoch != started.get("epoch_id"):
        return False
    payload_request = safe_fingerprint(payload.get("request_fingerprint"))
    if payload_request and payload_request != request.get("request_fingerprint"):
        return False
    payload_contract = _fingerprint32(
        payload.get("execution_contract_id") or payload.get("contract_id")
    )
    if payload_contract and payload_contract != started.get("contract_id"):
        return False
    payload_role = payload.get("role")
    if payload_role and payload_role != started.get("role"):
        return False
    raw_attempt = payload.get("attempt")
    if raw_attempt not in (None, "") and safe_sequence(raw_attempt) != safe_sequence(started.get("attempt")):
        return False
    # A raw agent id is not a generation capability.  Older host events often
    # omit the optional lifecycle fields, which is safe only while that id has
    # never represented another writer binding.  Once an id was terminal (or
    # otherwise recorded) for a different epoch/request/attempt, accepting an
    # unqualified Stop/PostTool would let it terminate or mutate a successor.
    if state is not None and writer_agent_has_prior_binding(
        state, started.get("agent_id"), started
    ):
        return bool(
            payload_epoch == started.get("epoch_id")
            and payload_request == request.get("request_fingerprint")
            and payload_contract == started.get("contract_id")
            and payload_role == started.get("role")
            and raw_attempt not in (None, "")
            and safe_sequence(raw_attempt) == safe_sequence(started.get("attempt"))
        )
    return True


def known_writer_roles(state: dict[str, Any], agent_id: Any) -> set[str]:
    """Find historical writer roles without making an id an authorization."""
    agent = safe_label(agent_id, 120) if agent_id else ""
    if not agent:
        return set()
    roles: set[str] = set()
    for item in as_list(state.get("subagents")):
        if not isinstance(item, dict) or item.get("agent_id") != agent:
            continue
        role = item.get("role")
        if role in {"high_assessor", "confirmed_executor"}:
            roles.add(role)
    expected_fingerprint = _lifecycle_agent_fingerprint(agent, None)
    for raw in as_list(state.get("isolated_lifecycles")):
        item = _safe_isolated_lifecycle(raw)
        if item and item.get("agent_fingerprint") == expected_fingerprint:
            roles.add(str(item.get("role")))
    return roles


def writer_agent_has_prior_binding(
    state: dict[str, Any], agent_id: Any, current_started: dict[str, Any]
) -> bool:
    """Whether this id was already assigned to a different writer identity."""
    agent = safe_label(agent_id, 120) if agent_id else ""
    if not agent:
        return False
    current = (
        current_started.get("epoch_id"), current_started.get("role"),
        current_started.get("request_fingerprint"), current_started.get("contract_id"),
        safe_sequence(current_started.get("attempt")),
    )
    for item in as_list(state.get("subagents")):
        if not isinstance(item, dict) or item.get("agent_id") != agent:
            continue
        if item.get("role") not in {"high_assessor", "confirmed_executor"}:
            continue
        candidate = (
            item.get("epoch_id"), item.get("role"),
            item.get("request_fingerprint"), item.get("contract_id"),
            safe_sequence(item.get("attempt")),
        )
        if candidate != current:
            return True
    return False


def bound_writer_for_posttool(
    state: dict[str, Any], payload: dict[str, Any]
) -> tuple[str | None, dict[str, Any] | None, dict[str, Any] | None]:
    agent = next(
        (
            safe_label(payload.get(key), 120)
            for key in ("agent_id", "subagent_id") if payload.get(key)
        ),
        None,
    )
    if not agent:
        return None, None, None
    for role in ("confirmed_executor", "high_assessor"):
        request, started = _current_bound_live_writer(state, role)
        if (
            request and started and agent == started.get("agent_id")
            and lifecycle_event_matches_start(payload, request, started, state)
        ):
            return role, request, started
    return None, None, None


def rejected_assessor_start_for_terminal(
    state: dict[str, Any], payload: dict[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Resolve one current rejected Start solely so its real Stop can close it.

    A profile-mismatched assessor never gains result authority and its flat
    agent id is deliberately cleared. Even so, the host process remains live
    until a real terminal event arrives. Select that generation only through
    the complete persisted epoch/request/contract/attempt/agent tuple and the
    matching unknown-liveness record. Optional Stop fields may narrow this
    tuple but can never widen it; reused ids still require the full lifecycle
    fields through ``lifecycle_event_matches_start``.
    """
    agent = next(
        (
            safe_label(payload.get(key), 120)
            for key in ("agent_id", "subagent_id") if payload.get(key)
        ),
        None,
    )
    epoch = current_task_epoch_id(state)
    contract = _fingerprint32(state.get("assessor_binding_id"))
    attempt = safe_sequence(state.get("assessor_attempt"))
    liveness = _safe_child_liveness(state.get("child_liveness"))
    if not (
        agent and epoch and contract and attempt > 0
        and _safe_task_epoch(state.get("task_epoch")).get("status") == "active"
        and state.get("assessor_state") == "recovery_required"
        and state.get("assessor_failure_kind") == "start_mismatch"
        and state.get("assessor_agent_id") is None
        and state.get("assessor_observed_effective") is False
        and liveness.get("status") == "unknown"
        and liveness.get("role") == "high_assessor"
        and liveness.get("epoch_id") == epoch
        and liveness.get("agent_fingerprint")
        == _lifecycle_agent_fingerprint(agent, liveness.get("request_fingerprint"))
    ):
        return None, None
    candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for group in subagent_lifecycle_groups(state):
        request = group.get("request")
        started = group.get("start")
        if not (
            group.get("state") == "live"
            and isinstance(request, dict)
            and isinstance(started, dict)
            and request.get("role") == started.get("role") == "high_assessor"
            and started.get("status") == "rejected"
            and request.get("host_accepted") is True
            and request.get("epoch_id") == started.get("epoch_id") == epoch
            and request.get("contract_id") == started.get("contract_id") == contract
            and safe_sequence(request.get("attempt"))
            == safe_sequence(started.get("attempt")) == attempt
            and request.get("request_fingerprint")
            == started.get("request_fingerprint")
            == liveness.get("request_fingerprint")
            and started.get("agent_id") == agent
        ):
            continue
        expected = lifecycle_binding_fingerprint(
            epoch_id=epoch, role="high_assessor", agent_id=agent,
            request_fingerprint=request.get("request_fingerprint"),
            contract_id=contract, attempt=attempt,
        )
        if (
            expected
            and started.get("lifecycle_fingerprint") == expected
            and lifecycle_event_matches_start(payload, request, started, state)
        ):
            candidates.append((request, started))
    return candidates[0] if len(candidates) == 1 else (None, None)


def tombstone_late_lifecycle_event(
    state: dict[str, Any], payload: dict[str, Any], *, status: str,
    role: str | None = None, request: dict[str, Any] | None = None,
) -> None:
    """Record replay/conflict facts without selecting a successor by agent id."""
    record = request if isinstance(request, dict) else {}
    inferred_role = role or record.get("role")
    roles = (
        {str(inferred_role)}
        if inferred_role in {"high_assessor", "confirmed_executor"}
        else known_writer_roles(
            state, payload.get("agent_id") or payload.get("subagent_id")
        )
    )
    for resolved_role in sorted(roles):
        _append_isolated_lifecycle(
            state,
            status=status,
            role=resolved_role,
            agent_id=payload.get("agent_id") or payload.get("subagent_id"),
            request_fingerprint=(
                safe_fingerprint(payload.get("request_fingerprint"))
                or record.get("request_fingerprint")
            ),
            contract_id=(
                _fingerprint32(payload.get("execution_contract_id") or payload.get("contract_id"))
                or record.get("contract_id")
            ),
            attempt=payload.get("attempt") or record.get("attempt"),
            event_material={
                "event": payload.get("hook_event_name"),
                "epoch": safe_fingerprint(payload.get("task_epoch_id") or payload.get("epoch_id")),
                "run": safe_label(payload.get("hook_run_id"), 120),
                "status": status,
            },
            epoch_id=(
                safe_fingerprint(payload.get("task_epoch_id") or payload.get("epoch_id"))
                or record.get("epoch_id")
            ),
        )


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
    r"slice_id=(s(?:0[1-9]|[1-9][0-9]+)) "
    r"outcome=(succeeded|failed)$"
)
EXECUTION_RESULT_V6_RE = re.compile(
    r"^EXECUTION_RESULT execution_contract_id=([0-9a-f]{32}) "
    r"outcome=(succeeded|failed) evidence_digest=([0-9a-f]{32})$"
)
EXECUTION_ACCEPTANCE_SUMMARY_RE = re.compile(
    r"^EXECUTION_ACCEPTANCE_SUMMARY execution_contract_id=([0-9a-f]{32}) "
    r"slice_id=(s(?:0[1-9]|[1-9][0-9]+)) checklist_digest=([0-9a-f]{32}) "
    r"required=([0-9]{1,10}) completed=([0-9]{1,10}) pending=([0-9]{1,10})$"
)


def _bound_acceptance_summary(body: str, state: dict[str, Any]) -> bool:
    """Accept exactly one bounded summary immediately before the result marker."""
    item = current_execution_slice(state) or {}
    contract = _fingerprint32(state.get("execution_contract_id"))
    lines = [line for line in str(body or "").replace("\r\n", "\n").replace("\r", "\n").split("\n") if line.strip()]
    matches = [EXECUTION_ACCEPTANCE_SUMMARY_RE.fullmatch(line) for line in lines]
    matches = [match for match in matches if match]
    if len(matches) != 1 or not lines or not EXECUTION_ACCEPTANCE_SUMMARY_RE.fullmatch(lines[-1]):
        return False
    match = matches[0]
    required, completed, pending = (int(match.group(i)) for i in (4, 5, 6))
    return bool(
        contract and match.group(1) == contract and match.group(2) == item.get("id")
        and match.group(3) == item.get("checklist_digest") and required == safe_int(item.get("required_count"))
        and completed + pending == required and completed == required and pending == 0
    )


def _acceptance_summary_digest(body: str, state: dict[str, Any]) -> str | None:
    """Bind the canonical checklist without requiring child prose repetition."""
    item = current_execution_slice(state) or {}
    contract = _fingerprint32(state.get("execution_contract_id"))
    if not contract or not item.get("id") or not item.get("checklist_digest"):
        return None
    explicit = _bound_acceptance_summary(body, state)
    return stable_hash(
        "workflow-manager-child-candidate-checklist-v2\0"
        + canonical_json(
            {
                "checklist_digest": item.get("checklist_digest"),
                "contract": contract,
                "required": safe_int(item.get("required_count")),
                "slice": item.get("id"),
                "source": "explicit" if explicit else "canonical_manifest",
            }
        ),
        32,
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
        and operation.get("epoch_id") == current_task_epoch_id(state)
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
    # PostToolUse is itself host evidence that the bound operation completed.
    # Some Desktop builds return an opaque tool response, so an accepted
    # mutation can retain status=unknown even though its exact input, command,
    # turn, executor, contract and slice are all host-bound.  Treat that narrow
    # shape as change evidence; the independent parent verification below is
    # still required before the slice can pass.
    host_recorded_change_indexes = [
        index
        for index, item in enumerate(operations)
        if item.get("category") in changes
        and (
            item.get("status") in SUCCESS_STATUSES
            or (
                item.get("status") == "unknown"
                and item.get("executor_agent_id")
                and _fingerprint32(item.get("host_input_digest"))
                and _fingerprint32(item.get("host_command_digest"))
                and item.get("host_event_turn_id")
            )
        )
    ]
    last_change = max(host_recorded_change_indexes, default=-1)
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
        "change_evidence": bool(host_recorded_change_indexes),
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
        "task_epoch_id": current_task_epoch_id(state),
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
    plan = _fingerprint32(state.get("plan_digest"))
    contract = _fingerprint32(state.get("execution_contract_id"))
    attempt = safe_sequence(state.get("executor_attempt"))
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
            state["stall"] = prior
        state["executor_state"] = "recovery_required"
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
        and group["request"].get("epoch_id") == current_task_epoch_id(state)
    ]
    if not candidates:
        return None
    # A delayed Start carrying binding identity must not fall through to a
    # freshly reserved successor after its original request was isolated.
    explicit_request = safe_fingerprint(payload.get("request_fingerprint"))
    explicit_contract = _fingerprint32(
        payload.get("contract_id") or payload.get("execution_contract_id")
        or payload.get("assessor_binding_id")
    )
    raw_attempt = payload.get("attempt")
    if explicit_request or explicit_contract or raw_attempt not in (None, ""):
        candidates = [
            item for item in candidates
            if (not explicit_request or item.get("request_fingerprint") == explicit_request)
            and (not explicit_contract or item.get("contract_id") == explicit_contract)
            and (raw_attempt in (None, "") or safe_sequence(item.get("attempt")) == safe_sequence(raw_attempt))
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
                and item.get("epoch_id") == current_task_epoch_id(state)
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
        and item.get("epoch_id") == current_task_epoch_id(state)
        and (
            str(state.get("execution_profile_version")) != EXECUTION_PROFILE_VERSION
            or item.get("slice_contract_id") == slice_contract_id(state)
        )
        and safe_int(item.get("attempt")) == safe_int(state.get("executor_attempt"))
    ]
    assessor_pending = [
        item for item in candidates
        if item.get("role") == "high_assessor"
        and item.get("contract_id") == state.get("assessor_binding_id")
        and item.get("epoch_id") == current_task_epoch_id(state)
    ]
    return (executor_pending or assessor_pending or candidates)[-1]


def unique_bound_start_request(
    state: dict[str, Any], role: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Return the one pending request that is wholly bound to current state.

    A request reservation is deliberately not authority.  In particular, do
    not reconstruct a request from the flat executor/assessor fields: the
    persisted request, its matching PostToolUse acceptance, and Start are
    three separate host facts.
    """
    role_candidates = [
        group.get("request") for group in subagent_lifecycle_groups(state)
        if group.get("state") == "pending"
        and isinstance(group.get("request"), dict)
        and group["request"].get("role") == role
        and group["request"].get("epoch_id") == current_task_epoch_id(state)
    ]
    if len(role_candidates) == 1:
        acceptance = role_candidates[0]
        if (
            acceptance.get("host_acceptance_conflict")
            or (
                acceptance.get("host_acceptance_fingerprint")
                and acceptance.get("host_acceptance_fingerprint")
                != acceptance.get("request_fingerprint")
            )
        ):
            return acceptance, "mismatch"
        if (
            acceptance.get("host_acceptance_source") != "PostToolUse"
            or acceptance.get("host_accepted") is not True
            or acceptance.get("host_acceptance_status")
            not in SUCCESS_STATUSES | {"running"}
            or not acceptance.get("host_acceptance_fingerprint")
            or not _fingerprint32(
                acceptance.get("host_acceptance_receipt_digest")
            )
        ):
            return acceptance, "model_unavailable"
    candidates = role_candidates
    if role == "high_assessor":
        candidates = [
            item for item in candidates
            if state.get("assessor_state") == "spawn_pending"
            and item.get("contract_id") == state.get("assessor_binding_id")
            and item.get("epoch_id") == current_task_epoch_id(state)
            and item.get("objective_fingerprint")
            == state.get("objective", {}).get("fingerprint")
            and item.get("model") == state.get("assessor_model")
            and item.get("reasoning_effort") == state.get("assessor_reasoning_effort")
            and str(item.get("fork_turns")) == str(state.get("assessor_fork_turns"))
            and safe_int(item.get("attempt")) == safe_int(state.get("assessor_attempt"))
        ]
    elif role == "confirmed_executor":
        candidates = [
            item for item in candidates
            if state.get("executor_state") == "spawn_pending"
            and item.get("contract_id") == state.get("execution_contract_id")
            and item.get("contract_id") == execution_contract_id(state)
            and item.get("epoch_id") == current_task_epoch_id(state)
            and item.get("objective_fingerprint")
            == state.get("objective", {}).get("fingerprint")
            and item.get("slice_id") == (current_execution_slice(state) or {}).get("id")
            and item.get("slice_contract_id") == slice_contract_id(state)
            and item.get("model") == state.get("executor_model")
            and item.get("reasoning_effort") == state.get("executor_reasoning_effort")
            and str(item.get("fork_turns")) == str(state.get("executor_fork_turns"))
            and safe_int(item.get("attempt")) == safe_int(state.get("executor_attempt"))
            and item.get("task_name") == bound_executor_task_name(state)
        ]
    else:
        return None, "unknown bound role"
    if len(candidates) != 1:
        # Preserve a sole original request long enough to record the exact
        # flat-state conflict as start_mismatch.  Multiple candidates remain
        # ambiguous and never select an authority-bearing request.
        if not candidates and len(role_candidates) == 1:
            return role_candidates[0], "mismatch"
        return None, "ambiguous" if candidates or len(role_candidates) > 1 else "missing"
    request = candidates[0]
    # These are written solely by the matching PostToolUse transition.  A
    # request field, Start payload, child prose, or a flat state value cannot
    # substitute for them.
    return request, None


def subagent_start_conflict_reason(
    state: dict[str, Any], agent_id: str, request: dict[str, Any]
) -> str | None:
    if not agent_id:
        return "SubagentStart lacks a concrete agent_id"
    groups = subagent_lifecycle_groups(state)
    if any(group.get("state") == "live" and group.get("agent_id") == agent_id for group in groups):
        return "duplicate SubagentStart for an already-live agent"
    if request.get("role") == "confirmed_executor" and any(
        group.get("state") == "live"
        and isinstance(group.get("request"), dict)
        and group["request"].get("role") == "confirmed_executor"
        for group in groups
    ):
        return "a confirmed executor already owns the one live writer slot"
    terminal = [
        group for group in groups
        if group.get("state") == "terminal" and group.get("agent_id") == agent_id
    ]
    return "a terminal child identity cannot be revived" if terminal else None


def subagent_start(payload: dict[str, Any]) -> None:
    previous = snapshot_state(payload)
    request = pending_subagent_request(previous, payload) or {}
    payload_epoch = safe_fingerprint(
        payload.get("task_epoch_id") or payload.get("epoch_id")
    )
    if payload_epoch and payload_epoch != current_task_epoch_id(previous):
        def tombstone_old_epoch(state: dict[str, Any]) -> None:
            tombstone_late_lifecycle_event(
                state, payload, status="late_start", role=request.get("role"),
                request=request,
            )
            record_lifecycle_diagnostic(
                state, "late_event_isolated_epoch", level="warning",
                role=request.get("role"),
                request_fingerprint=request.get("request_fingerprint"),
                contract_id=request.get("contract_id"),
            )

        mutate_state(payload, tombstone_old_epoch)
        emit_continue()
        return
    expected_request_fingerprint = safe_fingerprint(request.get("request_fingerprint"))
    scope_value = payload.get("prompt") or payload.get("task") or payload.get("message")
    scope_fingerprint = (stable_hash(scope_value) if scope_value else None) or request.get("scope_fingerprint")
    task_name = task_name_from_payload(payload) or request.get("task_name")
    request_fingerprint = request.get("request_fingerprint")
    executor_request = request.get("role") == "confirmed_executor"
    assessor_request = request.get("role") == "high_assessor"
    if not executor_request and not assessor_request:
        expected_role = expected_bound_role(previous)

        def record_unbound_start(state: dict[str, Any]) -> None:
            # A delayed Start must never become an implicit claim on a newer
            # reservation.  Preserve a writer tombstone whenever its id was
            # previously bound, even after the flat fields were cleared.
            tombstone_late_lifecycle_event(
                state, payload, status="late_start", role=expected_role,
                request=request or None,
            )
            if expected_role and active_hard_lifecycle(state):
                record_lifecycle_diagnostic(
                    state,
                    "pretool_missing",
                    level="error",
                    role=expected_role,
                    agent_id=payload.get("agent_id"),
                    contract_id=(
                        state.get("assessor_binding_id")
                        if expected_role == "high_assessor"
                        else state.get("execution_contract_id")
                    ),
                )
            elif not active_hard_lifecycle(state):
                record_lifecycle_diagnostic(
                    state,
                    "ordinary_spawn_no_active_hard",
                    level="info",
                    role="lane",
                    agent_id=payload.get("agent_id"),
                )

        mutate_state(payload, record_unbound_start)
        emit_continue()
        return
    observed_model, observed_effort, observation_source = start_turn_observation(payload)
    observed_status = start_observation_status(
        observed_model, observed_effort, observation_source
    )
    payload_model = safe_label(payload.get("model"), 80) if payload.get("model") else None
    canonical_body: str | None = None
    canonical_handoff_error: str | None = None
    canonical_handoff_digest: str | None = None
    if executor_request:
        (
            canonical_body,
            canonical_handoff_digest,
            canonical_handoff_error,
        ) = verified_current_execution_handoff(previous, payload)
    objective_fingerprint = request.get("objective_fingerprint") or previous.get("objective", {}).get("fingerprint")
    decision: dict[str, Any] = {"accepted": False}

    def update(state: dict[str, Any]) -> None:
        agent_id = safe_label(payload.get("agent_id"), 120)
        bound_request, binding_error = unique_bound_start_request(
            state, "confirmed_executor" if executor_request else "high_assessor"
        )
        bound_request = bound_request or {}
        bound_request_fingerprint = safe_fingerprint(bound_request.get("request_fingerprint"))
        conflict = (
            "persisted SubagentStart request was already consumed or no longer matches"
            if expected_request_fingerprint
            and bound_request_fingerprint != expected_request_fingerprint
            else (
                "SubagentStart has no unique fully-bound request"
                if binding_error in {"missing", "ambiguous"}
                else None
            )
        )
        payload_request = safe_fingerprint(payload.get("request_fingerprint"))
        payload_contract = _fingerprint32(
            payload.get("execution_contract_id") or payload.get("contract_id")
        )
        raw_attempt = payload.get("attempt")
        if not conflict and payload_request and payload_request != bound_request.get("request_fingerprint"):
            conflict = "SubagentStart request fingerprint conflicts with the current reservation"
        if not conflict and payload_contract and payload_contract != bound_request.get("contract_id"):
            conflict = "SubagentStart contract conflicts with the current reservation"
        if (
            not conflict and raw_attempt not in (None, "")
            and safe_sequence(raw_attempt) != safe_sequence(bound_request.get("attempt"))
        ):
            conflict = "SubagentStart attempt conflicts with the current reservation"
        if not conflict and bound_request.get("epoch_id") != current_task_epoch_id(state):
            conflict = "SubagentStart epoch conflicts with the current reservation"
        if not conflict:
            # A missing/rejected Post acceptance is recorded as a typed
            # capability failure below, rather than silently treated as a
            # profile conflict.  It still never becomes a running role.
            conflict = subagent_start_conflict_reason(state, agent_id, bound_request)
        if conflict:
            tombstone_late_lifecycle_event(
                state, payload, status="late_start",
                role=("confirmed_executor" if executor_request else "high_assessor"),
                request=bound_request or request or None,
            )
            record_lifecycle_diagnostic(
                state,
                "contract_mismatch",
                level="error",
                role=("confirmed_executor" if executor_request else "high_assessor"),
                request_fingerprint=bound_request.get("request_fingerprint"),
                agent_id=agent_id,
                contract_id=bound_request.get("contract_id"),
            )
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
        executor_profile = expected_executor_profile(state, bound_request)
        expected_model = (
            safe_label(executor_profile.get("model"), 80)
            if executor_profile.get("model")
            else None
        )
        expected_effort = str(
            executor_profile.get("reasoning_effort") or ""
        ).lower()
        highest_profile = (
            executor_profile.get("profile")
            == "work_executor_highest_available"
        )
        echoed_model = payload_model
        echoed_effort = observed_effort
        identity = state.setdefault("identity_evidence", {})
        identity["start_echo_profile"] = stable_hash(
            canonical_json({"model": observed_model, "reasoning_effort": observed_effort, "source": observation_source}), 32
        ) if observed_model or observed_effort else None
        contract_ready = bool(
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
            and (not highest_profile or bound_request.get("model") == expected_model)
            and not executor_profile.get("error")
            and not bound_request.get("host_acceptance_conflict")
            and observed_status == "full"
            and echoed_model == bound_request.get("model")
            and observed_model == echoed_model
            and echoed_effort == bound_request.get("reasoning_effort")
            and bound_request.get("fork_turns") == state.get("executor_fork_turns")
            and safe_int(bound_request.get("attempt")) == safe_int(state.get("executor_attempt"))
        )
        contract_matches = bool(
            contract_ready and bound_request.get("host_accepted") is True
        )
        locked_handoff = bool(
            contract_ready
            and binding_error == "model_unavailable"
            and bound_request.get("host_accepted") is None
            and not bound_request.get("host_acceptance_source")
            and not bound_request.get("host_acceptance_status")
            and not bound_request.get("host_acceptance_fingerprint")
        )
        decision["locked_handoff"] = locked_handoff
        handoff_delivered = bool(contract_matches or locked_handoff)
        lifecycle_fingerprint = lifecycle_binding_fingerprint(
            epoch_id=current_task_epoch_id(state),
            role=bound_request.get("role"), agent_id=agent_id,
            request_fingerprint=bound_request.get("request_fingerprint"),
            contract_id=request_contract, attempt=bound_request.get("attempt"),
        )
        start_record = {
                "at": utc_now(),
                "event": "start",
                "epoch_id": current_task_epoch_id(state),
                "lifecycle_fingerprint": lifecycle_fingerprint,
                "turn_id": safe_label(payload.get("turn_id"), 120) if payload.get("turn_id") else None,
                "agent_id": agent_id,
                "agent_type": safe_label(payload.get("agent_type"), 80),
                "task_name": task_name,
                "scope_fingerprint": scope_fingerprint,
                "request_fingerprint": request_fingerprint,
                "objective_fingerprint": objective_fingerprint,
                "stale": False,
                "status": "running",
                "requested": bool(bound_request),
                "host_accepted": bound_request.get("host_accepted"),
                "start_observed": observed_status,
                "observation_source": observation_source,
                "role": bound_request.get("role") or "lane",
                "contract_id": request_contract,
                "model": observed_model,
                "reasoning_effort": observed_effort,
                "fork_turns": bound_request.get("fork_turns"),
                "attempt": bound_request.get("attempt"),
                "slice_id": bound_request.get("slice_id"),
                "slice_contract_id": bound_request.get("slice_contract_id"),
                "recovery_from": bound_request.get("recovery_from"),
                "plan_handoff_digest": canonical_handoff_digest,
                "plan_handoff_delivery_digest": (
                    stable_hash(canonical_body, 32)
                    if handoff_delivered and canonical_body is not None
                    else None
                ),
                "plan_handoff_delivered": handoff_delivered,
            }
        state.setdefault("subagents", []).append(start_record)
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
                _set_writer_liveness(
                    state, status="live",
                    binding=_writer_liveness_binding(
                        state, "confirmed_executor", request=bound_request,
                        agent_id=agent_id,
                    ),
                    source="host_lifecycle",
                    observation={"event": "SubagentStart", "profile": "full"},
                )
            else:
                state["executor_state"] = "recovery_required"
                state["executor_agent_id"] = None
                state["executor_failure_kind"] = (
                    "model_unavailable"
                    if binding_error == "model_unavailable"
                    or executor_profile.get("error") == "model_unavailable"
                    else "stale_contract"
                    if canonical_handoff_error in {"unsafe_path", "content_drift", "journal_full"}
                    or state.get("execution_contract_id") != execution_contract_id(state)
                    else "start_mismatch"
                )
                if state["executor_failure_kind"] in {"stale_contract", "start_mismatch"}:
                    record_lifecycle_diagnostic(
                        state,
                        "contract_mismatch",
                        level="error",
                        role="confirmed_executor",
                        request_fingerprint=bound_request.get("request_fingerprint"),
                        agent_id=agent_id,
                        contract_id=request_contract,
                    )
                if locked_handoff or binding_error == "model_unavailable":
                    _set_writer_liveness(
                        state, status="live",
                        binding=_writer_liveness_binding(
                            state, "confirmed_executor", request=bound_request,
                            agent_id=agent_id,
                        ),
                        source="host_lifecycle",
                        observation={"event": "SubagentStart", "profile": "locked"},
                    )
                else:
                    _mark_writer_inventory_unknown(
                        state,
                        binding=_writer_liveness_binding(
                            state, "confirmed_executor", request=bound_request,
                            agent_id=agent_id,
                        ),
                        source="host_lifecycle",
                        observation={"event": "SubagentStart", "profile": "conflict"},
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
            state["assessor_state"] = "running" if matched else "recovery_required"
            state["assessor_failure_kind"] = None if matched else (
                "model_unavailable"
                if binding_error == "model_unavailable"
                else "start_mismatch"
            )
            if not matched and binding_error != "model_unavailable":
                record_lifecycle_diagnostic(
                    state,
                    "contract_mismatch",
                    level="error",
                    role="high_assessor",
                    request_fingerprint=bound_request.get("request_fingerprint"),
                    agent_id=agent_id,
                    contract_id=bound_request.get("contract_id"),
                )
            if matched:
                state["assessment_liveness"] = {
                    "binding_id": state.get("assessor_binding_id"), "agent_id": agent_id,
                    "attempt": safe_int(state.get("assessor_attempt")), "progress_digest": None,
                    "last_progress_at": _liveness_now(), "last_observed_at": _liveness_now(),
                    "unblock": "none", "unblock_at": None, "recovery_from": None,
                }
                _set_writer_liveness(
                    state, status="live",
                    binding=_writer_liveness_binding(
                        state, "high_assessor", request=bound_request,
                        agent_id=agent_id,
                    ),
                    source="host_lifecycle",
                    observation={"event": "SubagentStart", "profile": "full"},
                )
            elif binding_error == "model_unavailable":
                _set_writer_liveness(
                    state, status="live",
                    binding=_writer_liveness_binding(
                        state, "high_assessor", request=bound_request,
                        agent_id=agent_id,
                    ),
                    source="host_lifecycle",
                    observation={"event": "SubagentStart", "profile": "locked"},
                )
            else:
                # The process exists but this profile-mismatched Start never
                # gained assessor authority. Preserve that disposition so an
                # exact real Stop can close only this rejected generation.
                start_record["status"] = "rejected"
                _mark_writer_inventory_unknown(
                    state,
                    binding=_writer_liveness_binding(
                        state, "high_assessor", request=bound_request,
                        agent_id=agent_id,
                    ),
                    source="host_lifecycle",
                    observation={"event": "SubagentStart", "profile": "conflict"},
                )

    _, changed = mutate_state(payload, update)
    if not decision["accepted"]:
        emit_context(
            "SubagentStart",
            f"Workflow Manager ignored an invalid lifecycle transition: {decision.get('reason') or 'state update unavailable'}. "
            "A terminal agent stays terminal unless a newer persisted request explicitly binds a new generation.",
        )
        return
    if not executor_request and not assessor_request:
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
                    f"source={refreshed.get('executor_observation_source')}). Execute the bound plan and acceptance; "
                    "use native judgment for decomposition, progress, verification, and reversible in-scope repair."
                )
            else:
                warnings.append(
                    "Confirmed executor start is fail-closed: observed "
                    f"model={refreshed.get('executor_observed_model')}, effort={refreshed.get('executor_observed_reasoning_effort')}, "
                    f"source={refreshed.get('executor_observation_source')}, state={refreshed.get('executor_start_observed')}; do not mutate."
                )
    warning_text = (" " + " ".join(warnings)) if warnings else ""
    private_handoff = ""
    if (
        executor_request
        and canonical_body is not None
        and (
            refreshed.get("executor_state") == "running"
            or decision.get("locked_handoff") is True
        )
    ):
        relative_path = _safe_plan_artifact(refreshed.get("plan_artifact")).get(
            "relative_path"
        )
        private_handoff = (
            " Canonical executor handoff was verified from the trusted plugin-data journal. "
            f"relative_path={relative_path} is plugin-data-root-relative contract metadata only; never resolve "
            "it against cwd or a workspace. The current native plan or optional structured slice follows:\n"
            "BEGIN_WORKFLOW_MANAGER_EXECUTION_SLICE\n"
            f"{canonical_body}"
            "END_WORKFLOW_MANAGER_EXECUTION_SLICE"
        )
        if decision.get("locked_handoff") is True:
            private_handoff += (
                " The slice was delivered under a locked Start because the matching "
                "PostToolUse acceptance receipt had not arrived. Mutation remains denied "
                "until that exact receipt is persisted and the journal is reverified; do "
                "not infer authorization from this handoff alone."
            )
    elif executor_request:
        private_handoff = (
            " Canonical executor handoff verification failed"
            f" ({canonical_handoff_error or 'binding_mismatch'}); no plan body was delivered. Do not mutate."
        )
    emit_context(
        "SubagentStart",
        "Workflow Manager bound-role evidence: authorization depends on the recorded request, host acceptance, "
        f"full Start observation, and exact contract identity.{warning_text}{private_handoff}",
    )
def bound_runtime_truth_summary(state: dict[str, Any], role: str) -> str:
    """Return the exact bound request/acceptance/Start facts for the parent."""
    prefix = "assessor" if role == "high_assessor" else "executor"
    contract_id = (
        state.get("assessor_binding_id")
        if prefix == "assessor"
        else state.get("execution_contract_id")
    )
    request = next(
        (
            item
            for item in reversed(state.get("subagents", []))
            if item.get("event") == "request"
            and item.get("role") == role
            and item.get("contract_id") == contract_id
        ),
        {},
    )
    accepted = request.get("host_accepted")
    accepted_text = "true" if accepted is True else "false" if accepted is False else "unknown"
    requested_model = state.get(f"{prefix}_model") or request.get("model") or "absent"
    requested_effort = state.get(f"{prefix}_reasoning_effort") or request.get("reasoning_effort") or "absent"
    fork_turns = request.get("fork_turns") or state.get(f"{prefix}_fork_turns") or "absent"
    observed_state = state.get(f"{prefix}_start_observed") or "absent"
    observed_model = state.get(f"{prefix}_observed_model") or "absent"
    observed_effort = state.get(f"{prefix}_observed_reasoning_effort") or "absent"
    observation_source = state.get(f"{prefix}_observation_source") or "absent"
    return (
        f"{prefix} runtime truth: requested model={requested_model}, effort={requested_effort}, "
        f"fork_turns={fork_turns}; host_accepted={accepted_text}; Start={observed_state}, "
        f"observed model={observed_model}, effort={observed_effort}, source={observation_source}."
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
    bound_role, bound_request, bound_started = bound_writer_for_posttool(
        previous, payload
    )
    started = bound_started
    if started is None:
        rejected_request, rejected_started = rejected_assessor_start_for_terminal(
            previous, payload
        )
        if rejected_request and rejected_started:
            bound_role = "high_assessor"
            bound_request = rejected_request
            started = rejected_started
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
    if started is None:
        # A known id without an exact current epoch/request/attempt binding is
        # a delayed or replayed lifecycle event.  Keep a tombstone only.  Do
        # this even after the active writer's flat id has been cleared: using
        # agent-id reuse as a successor capability would let an old Stop end a
        # new generation.
        expected_role = expected_bound_role(previous)
        known_roles = known_writer_roles(previous, agent_id)

        def tombstone_unbound_terminal(state: dict[str, Any]) -> None:
            tombstone_late_lifecycle_event(
                state, payload, status="late_terminal",
                role=bound_role or expected_role,
                request=bound_request,
            )
            if known_roles or (expected_role and active_hard_lifecycle(state)):
                record_lifecycle_diagnostic(
                    state,
                    "start_missing"
                    if expected_role and active_hard_lifecycle(state)
                    else "late_event_epoch_ambiguous",
                    level="error" if expected_role and active_hard_lifecycle(state) else "warning",
                    role=bound_role or expected_role or "lane",
                    agent_id=agent_id,
                    request_fingerprint=(bound_request or {}).get("request_fingerprint"),
                    contract_id=(
                        (bound_request or {}).get("contract_id")
                        or (
                            state.get("assessor_binding_id")
                            if expected_role == "high_assessor"
                            else state.get("execution_contract_id")
                            if expected_role == "confirmed_executor"
                            else None
                        )
                    ),
                )

        if known_roles or (expected_role and active_hard_lifecycle(previous)):
            mutate_state(payload, tombstone_unbound_terminal)
        emit_continue()
        return
    started_objective = str((started or {}).get("objective_fingerprint") or "")
    current_objective = str(previous.get("objective", {}).get("fingerprint") or "")
    stale = bool(started_objective and current_objective and started_objective != current_objective)
    executor_agent = bool((started or {}).get("role") == "confirmed_executor")
    previous_stall = _safe_stall(previous.get("stall"))
    stall_assessor = bool(
        previous_stall.get("state") == "diagnosing"
        and agent_id == previous.get("assessor_agent_id")
    )
    assessor_agent = bool((started or {}).get("role") == "high_assessor") or stall_assessor
    assessment = re.search(r"(?im)^\s*WORK_ASSESSMENT\s+binding_id=([0-9a-f]{32})\s+outcome=(hard)\s+evidence_digest=([0-9a-f]{32})\s*$", str(result or ""))
    derived_assessment_binding: str | None = None
    derived_assessment_digest: str | None = None
    if assessor_agent and assessment is None:
        try:
            derived_plan_body = sanitize_plan_artifact_body(result)
            execution_slice_manifest_for_plan(derived_plan_body)
        except PlanArtifactError:
            pass
        else:
            if re.search(
                r"计划已就绪，等待确认后执行[。.!！\s]*$",
                str(result or ""),
            ):
                derived_assessment_binding = safe_fingerprint(
                    previous.get("assessor_binding_id")
                )
                derived_assessment_digest = stable_hash(
                    "workflow-manager-derived-assessment-v1\0" + derived_plan_body,
                    32,
                )
    native_assessment_digest = (
        native_assessor_result_digest(result) if assessor_agent else None
    )
    # The assessor supplies read-only judgment only. The parent owns the native
    # plan, so assessor prose never doubles as a second plan protocol.
    canonical_assessor_plan = False
    stall_lines = [line for line in str(result or "").splitlines() if line.startswith("EXECUTION_STALL")]
    stall_matches = [match for line in stall_lines if (match := EXECUTION_STALL_RE.fullmatch(line))]
    execution_stall_intent = bool(stall_lines)
    execution_stall = stall_matches[0] if len(stall_lines) == len(stall_matches) == 1 else None
    result_profile_current = str(previous.get("execution_profile_version")) == EXECUTION_PROFILE_VERSION
    execution_result, execution_result_body, execution_result_intent = _strict_terminal_marker(
        result,
        "EXECUTION_RESULT",
        EXECUTION_RESULT_RE if result_profile_current else EXECUTION_RESULT_V6_RE,
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
        _, current_request, current_started = bound_writer_for_posttool(
            state, payload
        )
        current_rejected_terminal = False
        if current_started is None:
            rejected_request, rejected_started = rejected_assessor_start_for_terminal(
                state, payload
            )
            if rejected_request and rejected_started:
                current_request = rejected_request
                current_started = rejected_started
                current_rejected_terminal = True
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
        # Stall diagnosis is a bounded follow-up to the already-proven
        # assessor lifecycle, not a fresh child spawn needing another Start.
        start_missing = current_result_group is not None and not stall_assessor
        successful = terminal_succeeded(current_started) and not start_missing
        if start_missing:
            missing_role = str((current_started or {}).get("role") or "lane")
            record_lifecycle_diagnostic(
                state,
                "start_missing",
                level="error",
                role=missing_role,
                request_fingerprint=(current_started or {}).get("request_fingerprint"),
                agent_id=agent_id,
                contract_id=(current_started or {}).get("contract_id"),
            )
            if missing_role == "confirmed_executor":
                state["executor_state"] = "recovery_required"
                state["executor_failure_kind"] = "start_mismatch"
                state["executor_agent_id"] = None
            elif missing_role == "high_assessor":
                state["assessor_state"] = "recovery_required"
                state["assessor_failure_kind"] = "start_mismatch"
                state["assessor_agent_id"] = None
        marker_result_current = bool(
            execution_result
            and execution_result.group(1) == state.get("execution_contract_id")
            and (
                not result_profile_current
                or execution_result.group(2) == (current_execution_slice(state) or {}).get("id")
            )
        )
        native_result_current = bool(
            executor_agent
            and current_started is not None
            and not start_missing
            and not execution_result_intent
            and str(result or "").strip()
        )
        execution_result_current = marker_result_current or native_result_current
        execution_result_outcome = (
            execution_result.group(3)
            if execution_result and result_profile_current
            else execution_result.group(2)
            if execution_result
            else "failed"
            if status_value in ERROR_STATUSES or explicit_unknown_status
            else "succeeded"
            if native_result_current
            else None
        )
        execution_result_succeeded = bool(
            execution_result_current and execution_result_outcome == "succeeded"
        )
        effective_execution_body = (
            execution_result_body
            if execution_result
            else str(result or "").strip()
        )
        if executor_agent:
            # Native child prose is a candidate when the separately bound host
            # lifecycle is current. Explicit malformed marker intent or a host
            # failed/cancelled status still fails closed.
            successful = bool(
                current_started is not None
                and not start_missing
                and execution_result_succeeded
                and status_value not in ERROR_STATUSES
                and not explicit_unknown_status
                and not (marker_result_current and execution_result_outcome == "failed")
            )
        already_terminal = any(
            group.get("state") == "terminal" and group.get("agent_id") == agent_id
            for group in subagent_lifecycle_groups(state)
        )
        if not agent_id or (current_started is None and already_terminal):
            reason = "SubagentStop lacks a concrete agent_id" if not agent_id else "duplicate or late SubagentStop for a terminal agent"
            if agent_id:
                tombstone_late_lifecycle_event(
                    state, payload, status="late_terminal", role=bound_role,
                    request=current_request,
                )
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
            bound_stall = bool(
                current_result_group
                and _safe_stall(state.get("stall")).get("state") == "diagnosing"
                and agent_id == state.get("assessor_agent_id")
                and diagnosis_lines
            )
            if not (exact_request or exact_turn or bound_stall):
                reason = "SubagentStop is ambiguous after agent_id reuse and requires generation reconciliation"
                tombstone_late_lifecycle_event(
                    state, payload, status="late_terminal", role=bound_role,
                    request=current_request,
                )
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
                body_without_marker=effective_execution_body,
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
                "epoch_id": (effective_started or {}).get("epoch_id")
                or current_task_epoch_id(state),
                "lifecycle_fingerprint": lifecycle_binding_fingerprint(
                    epoch_id=(effective_started or {}).get("epoch_id")
                    or current_task_epoch_id(state),
                    role=(effective_started or {}).get("role"),
                    agent_id=agent_id,
                    request_fingerprint=(effective_started or {}).get("request_fingerprint"),
                    contract_id=(effective_started or {}).get("contract_id"),
                    attempt=(effective_started or {}).get("attempt"),
                ),
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
        decision["rejected_terminal"] = current_rejected_terminal
        if executor_agent or assessor_agent:
            _set_writer_liveness(
                state, status="terminal",
                binding=_writer_liveness_binding(
                    state,
                    "confirmed_executor" if executor_agent else "high_assessor",
                    request=(effective_started or current_request),
                    agent_id=agent_id,
                ),
                source="host_lifecycle",
                observation={"event": "SubagentStop", "status": status_value},
            )
        if current_rejected_terminal:
            # Process termination is not result acceptance. Keep the typed
            # mismatch so only a fresh monotonic assessor attempt can recover.
            state["assessor_state"] = "recovery_required"
            state["assessor_failure_kind"] = "start_mismatch"
            state["assessor_agent_id"] = None
            state["assessor_observed_effective"] = False
        if executor_agent and state.get("executor_agent_id") == agent_id:
            contract_current = bool(
                not stale
                and (effective_started or {}).get("contract_id") == state.get("execution_contract_id")
                and state.get("execution_contract_id") == execution_contract_id(state)
                and (
                    not result_profile_current
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
                    state["executor_state"] = "recovery_required"
                    state["executor_failure_kind"] = "executor_failed"
                    state["model_profile"] = "work_assessment"
                return
            if not contract_current:
                state["executor_state"] = "recovery_required"
                state["executor_failure_kind"] = "stale_contract"
                state["model_profile"] = "work_assessment"
            elif successful and state.get("executor_state") == "running":
                # A completed child is a candidate for parent review even when
                # verification is intentionally performed by the parent after
                # the child terminates.  Requiring child-side verification here
                # converted a normal review handoff into a recovery loop and
                # prevented later host-recorded parent evidence from sealing it.
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
                            effective_execution_body, 32
                        ),
                        "candidate_agent_fingerprint": stable_hash(agent_id, 32),
                        "candidate_evidence_digest": candidate_evidence_digest,
                        "child_summary_digest": _acceptance_summary_digest(
                            effective_execution_body, state
                        ),
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
                state["executor_state"] = "recovery_required"
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
                    stall["state"] = "diagnosis_required"
                    stall["at"] = utc_now()
                    state["stall"] = stall
                    state["executor_state"] = "recovery_required"
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
                stall["state"] = "diagnosis_required"
                state["executor_state"] = "recovery_required"
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
        if (
            assessor_agent
            and not current_rejected_terminal
            and state.get("assessor_agent_id") == agent_id
        ):
            mutated = any(item.get("assessor_binding_id") == state.get("assessor_binding_id") and item.get("category") in {"implementation", "build_package", "delivery_device", "git"} and item.get("status") in SUCCESS_STATUSES for item in state.get("operations", []))
            assessor_lifecycle, assessor_lifecycle_error = original_assessor_lifecycle(
                state
            )
            lifecycle_current = bool(
                not assessor_lifecycle_error
                and assessor_lifecycle.get("agent_id") == agent_id
                and assessor_lifecycle.get("binding_id")
                == state.get("assessor_binding_id")
            )
            assessment_binding = (
                assessment.group(1)
                if assessment
                else derived_assessment_binding
                or (
                    safe_fingerprint(state.get("assessor_binding_id"))
                    if native_assessment_digest
                    else None
                )
            )
            assessment_digest = (
                assessment.group(3)
                if assessment
                else derived_assessment_digest or native_assessment_digest
            )
            valid = bool(
                not stale
                and successful
                and assessment_binding
                and assessment_digest
                and assessment_binding == state.get("assessor_binding_id")
                and lifecycle_current
            )
            if start_missing:
                state["assessor_state"] = "recovery_required"
                state["assessor_failure_kind"] = "start_mismatch"
            elif assessment and mutated:
                state["assessor_state"] = "failed"
                state["assessor_failure_kind"] = "hard_mutation_before_confirmation"
            elif assessor_lifecycle_error:
                state["assessor_state"] = "recovery_required"
                state["assessor_failure_kind"] = assessor_lifecycle_error
            elif not valid:
                state["assessor_state"] = "recovery_required"
                state["assessor_failure_kind"] = "assessment_result_invalid"
            else:
                detailed = bool(
                    canonical_assessor_plan
                    or native_assessment_digest
                    or assessment_digest
                )
                if not detailed:
                    state["assessor_state"] = "recovery_required"
                    state["assessor_failure_kind"] = "hard_plan_incomplete"
                    return
                state["assessor_state"] = "hard_plan_ready"
                state["work_difficulty"] = "hard"
                state["difficulty_confidence"] = "high"
                state["difficulty_rule_codes"] = ["assessor_hard"]
                state["difficulty_decision_id"] = stable_hash(
                    f"{state.get('assessor_binding_id')}\0{assessment_digest}", 24
                )
                state["plan_state"] = "analyzing"
                state["confirmed_plan_digest"] = None
                state["confirmed_at"] = None
                reset_executor_binding(state)
                state["model_profile"] = confirmed_executor_model_profile(state)
                if canonical_assessor_plan and write_plan_artifact(
                    state, payload, str(result or "")
                ):
                    state["plan_state"] = "awaiting_confirmation"
                    auto_confirm_trusted_plan(state, payload)
                elif canonical_assessor_plan:
                    state["plan_state"] = "repair_required"
                else:
                    # Current Codex owns plan composition. The assessor's
                    # host-bound result is read-only evidence; the parent Stop
                    # writes the one native canonical plan without a second
                    # assessor or another user confirmation.
                    state["plan_state"] = "analyzing"
                    receipt = original_assessor_result_receipt(state)
                    if receipt:
                        state["plan_composition"] = _safe_plan_composition({
                            "status": "pending",
                            "assessor_binding_id": state.get("assessor_binding_id"),
                            "objective_fingerprint": state.get("objective", {}).get("fingerprint"),
                            "assessment_receipt": receipt,
                            "turn_id": (effective_started or {}).get("turn_id"),
                        })
                        pending_causal = _safe_pending_causal_revision(
                            state.get("pending_causal_revision")
                        )
                        if pending_causal:
                            pending_causal["creation_state"] = "plan_composition"
                            state["pending_causal_revision"] = pending_causal
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
    if stale and (executor_agent or assessor_agent):
        emit_context(
            "SubagentStop",
            "Stale subagent result: the objective changed after this agent started. Use it only as verification; "
            "it must not drive mutation for the previous objective.",
        )
    elif decision.get("rejected_terminal"):
        emit_context(
            "SubagentStop",
            "Workflow Manager recorded the exact real terminal boundary for the rejected assessor Start. "
            "Its result remains non-authoritative; recovery_required/start_mismatch is preserved and one fresh "
            "monotonic assessor attempt may now be reserved.",
        )
    elif assessor_agent:
        runtime_truth = bound_runtime_truth_summary(updated_state, "high_assessor")
        emit_context(
            "SubagentStop",
            f"Workflow Manager {runtime_truth} Report these exact verified fields to the user; when Start=full, "
            "do not describe the runtime echo as absent or unavailable.",
        )
    elif (
        executor_agent
        and updated_state.get("executor_state") == "recovery_required"
    ):
        runtime_truth = bound_runtime_truth_summary(updated_state, "confirmed_executor")
        host_recovery = recovery_reservation_context(updated_state)
        recovery_text = f"\n{host_recovery}" if host_recovery else ""
        emit_context(
            "SubagentStop",
            f"Workflow Manager {runtime_truth} Confirmed executor failed or requires recovery. Return to the high-reasoning parent for diagnosis. Do not repeat "
            "unchanged actions: use new evidence, root cause, progress, or a material correction in a fresh "
            "monotonic sequence, or invalidate/replan when scope or acceptance changes. The Hook-issued digest facts "
            "below are sufficient; do not inspect plugin state files or infer them from child prose."
            f"{recovery_text}",
        )
    elif executor_agent and updated_state.get("executor_state") == "verification_required":
        runtime_truth = bound_runtime_truth_summary(updated_state, "confirmed_executor")
        emit_context(
            "SubagentStop",
            f"Workflow Manager {runtime_truth} Executor self-report is only a candidate. The high-reasoning parent must independently inspect "
            "the bounded artifacts and run the acceptance verification. Native parent Stop plus host-recorded evidence is sufficient; "
            "a plugin review marker is optional. "
            "Only passed with bound operation evidence advances the slice; a failed review may use one fresh, "
            "evidence-bound typed successor and must never revive a terminal child.",
        )
    else:
        emit_continue()


CAUSAL_REVIEW_RESULT_RE = re.compile(
    r"(?im)^\s*CAUSAL_REVIEW\s+"
    r"baseline_id=([0-9a-f]{32})\s+"
    r"review_id=([0-9a-f]{32})\s+"
    r"outcome=(direct_followup|introduced_regression|verified_side_effect|"
    r"fix_ineffective|acceptance_gap_no_change|execution_exposed_gap|uncertain|"
    r"explanatory_conclusion|unrelated_new_objective|introduced|unrelated)\s+"
    r"evidence_digest=([0-9a-f]{32})\s*$"
)
EXECUTION_REVIEW_RE = re.compile(
    r"^EXECUTION_REVIEW execution_contract_id=([0-9a-f]{32}) "
    r"slice_id=(s(?:0[1-9]|[1-9][0-9]+)) "
    r"outcome=(passed|failed)$"
)
EXECUTION_REVIEW_SUMMARY_RE = re.compile(
    r"^EXECUTION_REVIEW_SUMMARY execution_contract_id=([0-9a-f]{32}) "
    r"slice_id=(s(?:0[1-9]|[1-9][0-9]+)) checklist_digest=([0-9a-f]{32}) "
    r"required=([0-9]{1,10}) completed=([0-9]{1,10}) pending=([0-9]{1,10})$"
)


def _bound_parent_review_summary(body: str, state: dict[str, Any]) -> str | None:
    item = current_execution_slice(state) or {}
    contract = _fingerprint32(state.get("execution_contract_id"))
    matches = [EXECUTION_REVIEW_SUMMARY_RE.fullmatch(line) for line in str(body or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    matches = [match for match in matches if match]
    explicit = False
    if len(matches) == 1:
        match = matches[0]
        required, completed, pending = (
            int(match.group(index)) for index in (4, 5, 6)
        )
        explicit = bool(
            contract
            and match.group(1) == contract
            and match.group(2) == item.get("id")
            and match.group(3) == item.get("checklist_digest")
            and required == safe_int(item.get("required_count"))
            and completed == required
            and pending == 0
            and completed + pending == required
        )
    evidence = slice_operation_evidence(state)
    if (
        not contract
        or not item.get("id")
        or not item.get("checklist_digest")
        or not evidence.get("parent_review_evidence")
        or not evidence.get("operation_digest")
    ):
        return None
    return stable_hash(
        "workflow-manager-parent-review-checklist-v2\0"
        + canonical_json(
            {
                "checklist_digest": item.get("checklist_digest"),
                "contract": contract,
                "operation_digest": evidence.get("operation_digest"),
                "required": safe_int(item.get("required_count")),
                "slice": item.get("id"),
                "source": "explicit" if explicit else "canonical_manifest",
            }
        ),
        32,
    )
EXECUTION_REVIEW_V6_RE = re.compile(
    r"^EXECUTION_REVIEW execution_contract_id=([0-9a-f]{32}) "
    r"outcome=(passed|failed) evidence_digest=([0-9a-f]{32})$"
)


def stop(payload: dict[str, Any]) -> None:
    assistant_message = str(payload.get("last_assistant_message") or "")
    previous = snapshot_state(payload)
    review_profile_current = (
        str(previous.get("execution_profile_version")) == EXECUTION_PROFILE_VERSION
    )
    execution_review, execution_review_body, execution_review_intent = _strict_terminal_marker(
        assistant_message,
        "EXECUTION_REVIEW",
        EXECUTION_REVIEW_RE if review_profile_current else EXECUTION_REVIEW_V6_RE,
    )
    causal_match = CAUSAL_REVIEW_RESULT_RE.search(assistant_message)
    plan_ready = canonical_plan_message_ready(assistant_message)

    def update(state: dict[str, Any]) -> None:
        reconcile_current_parent_rollout_on_resume(payload, state)
        reconcile_unknown_operations_from_transcript(payload, state)
        promote_reconciled_parent_review(state)
        state["last_assistant"] = text_metadata(assistant_message)
        state["last_stop_at"] = utc_now()
        if (
            state.get("plan_state") == "confirmed"
            and state.get("executor_state") == "verification_required"
        ):
            review = _safe_executor_review(state.get("executor_review"))
            contract_id = state.get("execution_contract_id")
            profile_current = (
                str(state.get("execution_profile_version"))
                == EXECUTION_PROFILE_VERSION
            )
            current_slice = current_execution_slice(state)
            current_slice_contract = slice_contract_id(state)
            review_outcome = (
                execution_review.group(3)
                if execution_review and profile_current
                else execution_review.group(2)
                if execution_review
                else "passed"
                if not execution_review_intent and assistant_message.strip()
                else None
            )
            # Native parent prose is allowed, but it cannot accidentally seal
            # a candidate when it expressly records a negative acceptance or
            # says that the requested release has not begun.  This also wins
            # over a contradictory optional ``outcome=passed`` marker.
            explicit_negative_review = bool(re.search(
                r"(?i)(?:\\b(?:not\\s+passed|failed|failure|not\\s+started)\\b|"
                r"未通过|失败|未开始|尚未发布|发布未开始)",
                assistant_message,
            ))
            if explicit_negative_review:
                review_outcome = "failed"
            native_review = bool(
                not execution_review_intent and assistant_message.strip()
            )
            marker_binding_valid = bool(
                execution_review
                and execution_review.group(1) == contract_id
                and (
                    contract_id == execution_contract_id(state)
                    if profile_current
                    else str(state.get("execution_profile_version")) in {"5", "6"}
                )
                and (
                    not profile_current
                    or current_slice
                    and execution_review.group(2) == current_slice.get("id")
                )
            )
            binding_valid = bool(
                (marker_binding_valid or native_review)
                and (
                    contract_id == execution_contract_id(state)
                    if profile_current
                    else str(state.get("execution_profile_version")) in {"5", "6"}
                )
                and (
                    not profile_current
                    or current_slice
                    and review.get("slice_id") == current_slice.get("id")
                    and review.get("slice_contract_id") == current_slice_contract
                )
                and review.get("status") == "review_required"
                and review.get("execution_contract_id") == contract_id
                and review.get("attempt") == state.get("executor_attempt")
                and review.get("candidate_result_fingerprint")
                and review.get("candidate_evidence_digest")
                and review.get("child_summary_digest")
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
            if not profile_current:
                # A durable Schema 23/profile 5 or 6 candidate is review-only
                # compatibility. 1.0.42 could persist a migrated Schema 22
                # candidate with profile 5 before the parent review arrived.
                # Ignore its self-reported digest. A pass may seal the existing
                # bounded baseline; a failure invalidates it and never grants a
                # new current-profile attempt budget.
                baseline = _safe_execution_baseline(state.get("last_execution_baseline"))
                baseline_bound = bool(
                    baseline
                    and baseline.get("execution_contract_id") == contract_id
                    and baseline.get("objective_fingerprint")
                    == state.get("objective", {}).get("fingerprint")
                    and baseline.get("plan_digest") == state.get("plan_digest")
                    and _fingerprint32(baseline.get("change_set_digest"))
                    and _fingerprint32(baseline.get("verification_digest"))
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

            parent_summary_digest = _bound_parent_review_summary(execution_review_body, state)
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
            review["parent_summary_digest"] = parent_summary_digest
            review["digest_profile"] = EVIDENCE_DIGEST_PROFILE
            review["digest_source"] = EVIDENCE_DIGEST_SOURCE
            review["at"] = utc_now()
            passed_with_evidence = bool(
                review_outcome == "passed"
                and parent_summary_digest
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
                            "child_summary_digest": review.get("child_summary_digest"),
                            "parent_summary_digest": parent_summary_digest,
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
                    if not seal_completed_execution(state, payload):
                        # A terminal lifecycle without its durable seal is not
                        # publishable completion. Keep the bounded candidate
                        # available for normal recovery instead of claiming a
                        # success that cannot be causally continued.
                        state["executor_state"] = "recovery_required"
                        state["executor_failure_kind"] = "verification_failed"
                        review["status"] = "failed"
                        state["executor_review"] = review
                        state["model_profile"] = "work_assessment"
                        return
                    parent_lease = _safe_parent_writer_lease(
                        state.get("parent_writer_lease")
                    )
                    if (
                        parent_lease.get("status") == "live"
                        and parent_lease.get("execution_contract_id") == contract_id
                        and parent_lease.get("slice_id") == current_slice.get("id")
                        and parent_lease.get("slice_contract_id")
                        == current_slice_contract
                        and parent_lease.get("attempt")
                        == safe_sequence(state.get("executor_attempt"))
                    ):
                        parent_lease["status"] = "sealed"
                        state["parent_writer_lease"] = parent_lease
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
                state["executor_state"] = "recovery_required"
                state["executor_failure_kind"] = "verification_failed"
                baseline = _safe_execution_baseline(state.get("last_execution_baseline")) or build_execution_baseline(state)
                if baseline:
                    baseline["acceptance_status"] = "failed"
                    state["last_execution_baseline"] = baseline
                review["status"] = "failed"
                state["model_profile"] = "work_assessment"
                state["executor_review"] = review
                return
            else:
                if review_outcome == "passed":
                    # A pass without host-bound parent verification is an
                    # evidence repair, not a failed implementation. Preserve
                    # the current sequence and let the parent add the missing
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
                state["executor_state"] = "recovery_required"
                state["executor_failure_kind"] = "verification_failed"
                review["status"] = "failed"
                state["model_profile"] = "work_assessment"
                stall = _safe_stall(state.get("stall"))
                if (
                    stall.get("state") == "resuming"
                    and stall.get("execution_contract_id") == contract_id
                ):
                    stall["state"] = "diagnosis_required"
                    stall["at"] = utc_now()
                    state["stall"] = stall
            state["executor_review"] = review
            return
        review_state = _safe_causal_review(state.get("causal_review")).get("state")
        if state.get("task_domain") == "work" and state.get("work_difficulty") == "hard" and review_state not in {"triage_required", "triaging"} and state.get("assessor_state") != "hard_plan_ready":
            return
        review = _safe_causal_review(state.get("causal_review"))
        if review.get("state") in {"triage_required", "triaging"}:
            if causal_match:
                baseline_id, review_id, outcome, evidence_digest = causal_match.groups()
                causal_type = LEGACY_CAUSAL_OUTCOME_MAP.get(outcome, outcome)
                baseline = _safe_execution_baseline(
                    state.get("last_execution_baseline")
                )
                binding_valid = bool(
                    baseline_id == review.get("baseline_id")
                    and review_id == review.get("review_id")
                    and baseline_id == baseline.get("baseline_id")
                    and causal_type in CAUSAL_TYPES
                    and not (
                        causal_type == "introduced_regression"
                        and not baseline.get("change_set_digest")
                    )
                )
                if binding_valid:
                    review["outcome"] = outcome
                    review["causal_type"] = causal_type
                    review["evidence_digest"] = evidence_digest
                    if causal_type == "uncertain":
                        review["state"] = "triaging"
                        state["model_profile"] = "work_assessment"
                    elif causal_type == "explanatory_conclusion":
                        if append_runtime_plan_record(
                            state,
                            payload,
                            record_type="durable_conclusion",
                            data={
                                "causal_type": causal_type,
                                "parent_revision_digest": state.get("plan_digest"),
                                "parent_contract_id": state.get("execution_contract_id"),
                                "terminal_baseline_id": baseline_id,
                                "root_objective_fingerprint": state.get("objective", {}).get("fingerprint"),
                                "issue_fingerprint": review.get("report_fingerprint"),
                                "conclusion_digest": evidence_digest,
                                "evidence_digest": evidence_digest,
                            },
                        ):
                            review["state"] = "resolved"
                        else:
                            review["state"] = "triaging"
                            state["model_profile"] = "work_assessment"
                    elif causal_type == "unrelated_new_objective":
                        review["state"] = "resolved"
                        report_fingerprint = str(review.get("report_fingerprint") or "")
                        state["objective"] = {
                            "fingerprint": report_fingerprint,
                            "length": 0,
                            "updated_at": utc_now(),
                        }
                        state["authorization_scope"] = _safe_authorization_scope(None)
                        state["authorization_envelope"] = _safe_authorization_envelope(None)
                        state["pending_confirmation_receipt"] = None
                        state["pending_causal_revision"] = {}
                        state["causal_lineage"] = _safe_causal_lineage(None)
                        state["plan_state"] = "analyzing"
                        state["plan_digest"] = None
                        state["plan_objective_fingerprint"] = None
                        state["plan_difficulty_decision_id"] = None
                        state["confirmed_plan_digest"] = None
                        state["confirmed_at"] = None
                        reset_executor_binding(state)
                        state["task_domain"] = "work"
                        state["model_profile"] = "work_assessment"
                        state["assessor_generation"] = max(
                            safe_int(state.get("assessor_generation")), 0
                        ) + 1
                        state["assessor_binding_id"] = assessor_binding_id(state)
                        state["assessor_state"] = "spawn_required"
                        state["assessor_agent_id"] = None
                        state["assessor_attempt"] = 0
                        state["assessor_failure_kind"] = None
                        state["assessor_input_fingerprint"] = report_fingerprint
                    else:
                        review["state"] = "resolved"
                        successor = _safe_pending_causal_revision(
                            state.get("pending_causal_revision")
                        )
                        if successor:
                            successor.update({
                                "causal_type": causal_type,
                                "creation_state": "assessment_required",
                                "evidence_digest": evidence_digest,
                            })
                            successor = _safe_pending_causal_revision(successor)
                        if not successor:
                            review["state"] = "triaging"
                            review["causal_type"] = "uncertain"
                            state["model_profile"] = "work_assessment"
                            state["causal_review"] = review
                            return
                        state["pending_causal_revision"] = successor
                        state["plan_state"] = "analyzing"
                        state["plan_digest"] = None
                        state["plan_objective_fingerprint"] = None
                        state["plan_difficulty_decision_id"] = None
                        state["confirmed_plan_digest"] = None
                        state["confirmed_at"] = None
                        reset_executor_binding(state)
                        state["task_domain"] = "work"
                        state["work_difficulty"] = "hard"
                        state["difficulty_confidence"] = "high"
                        state["difficulty_rule_codes"] = ["causal_followup_review"]
                        state["difficulty_decision_id"] = stable_hash(
                            f"causal\0{causal_type}\0{review_id}", 24
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
            "repair_required",
        }:
            composition = _safe_plan_composition(state.get("plan_composition"))
            composition_valid = bool(
                composition.get("status") == "pending"
                and composition.get("assessor_binding_id")
                == state.get("assessor_binding_id")
                and composition.get("objective_fingerprint")
                == state.get("objective", {}).get("fingerprint")
                and composition.get("assessment_receipt")
                == original_assessor_result_receipt(state)
            )
            if plan_ready and composition_valid:
                state["confirmed_plan_digest"] = None
                state["confirmed_at"] = None
                reset_executor_binding(state)
                state["plan_state"] = "analyzing"
                if write_plan_artifact(state, payload, assistant_message):
                    state["plan_composition"] = _safe_plan_composition(None)
                    state["plan_state"] = "awaiting_confirmation"
                    auto_confirm_trusted_plan(state, payload)
                else:
                    state["plan_state"] = "repair_required"
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
        emit_leased_stop_block(
            payload,
            f"Workflow Manager canonical plan journal {artifact['write_status']} warning_code={artifact['warning_code']} plan_generation={safe_int(updated_state.get('plan_generation'))} plan_digest={artifact.get('plan_digest') or 'none'} failure_instance={artifact.get('updated_at') or 'none'}; confirmation and execution remain locked until a trusted revision commits.",
        )
        return
    # Recovery facts are already delivered on SubagentStop and on the next
    # actionable UserPromptSubmit. Stop must not manufacture another model
    # turn: repeated blocking here created an unbounded self-dialogue with no
    # new host evidence. The parent remains responsible for choosing whether
    # to diagnose, retry, replan, or finish.
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
        # Independent observability is recorded before all business-state work.
        record_dispatch_receipt(payload)
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
