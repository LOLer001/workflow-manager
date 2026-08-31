#!/usr/bin/env python3
"""Fail-closed, checkpointed local release transaction.

The module intentionally contains no publisher implementation. A caller can
inject a runner that knows how to query or perform a particular stage, but a
stage advances only with typed, transaction-bound evidence. In particular,
arbitrary command output or a pasted CLI success message is never evidence.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterator, Mapping, Protocol


STAGES = (
    "preflight", "prepare", "test", "commit_push", "ci", "tag_release",
    "marketplace", "install_doctor_smoke", "final_seal",
)
EXTERNAL_STAGES = frozenset(STAGES[3:-1])
STATE_SCHEMA = 2
EVIDENCE_SCHEMA = "workflow-manager-release-evidence-v1"
MAX_FAILURES = 32


class ReleaseTransactionError(RuntimeError):
    """A release checkpoint cannot safely advance."""


class ExternalActionUncertain(ReleaseTransactionError):
    """An interrupted external action cannot be repeated without a query fact."""


class ReleaseRunner(Protocol):
    """Local injectable boundary; implementations own real integrations."""

    def query(self, stage: str, context: Mapping[str, Any]) -> Mapping[str, Any]:
        """Return absent, completed-with-evidence, or unknown."""

    def run(self, stage: str, context: Mapping[str, Any]) -> Mapping[str, Any]:
        """Return typed evidence or a structured failed result; never plain text."""


class DeniedRunner:
    """Default runner: it can neither infer nor execute a release action."""

    def query(self, stage: str, context: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"status": "unknown", "stage": stage}

    def run(self, stage: str, context: Mapping[str, Any]) -> Mapping[str, Any]:
        raise PermissionError("no release runner is configured")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()[:32]


def _fingerprint(value: Any) -> str | None:
    raw = str(value or "")
    return raw if re.fullmatch(r"[0-9a-f]{32}", raw) else None


def _binding(value: Mapping[str, Any] | None = None) -> dict[str, str | None]:
    value = value if isinstance(value, Mapping) else {}
    return {
        "task_epoch_id": _fingerprint(value.get("task_epoch_id")),
        "execution_contract_id": _fingerprint(value.get("execution_contract_id")),
    }


def _binding_is_complete(value: Mapping[str, Any] | None) -> bool:
    binding = _binding(value)
    return bool(binding["task_epoch_id"] and binding["execution_contract_id"])


def _empty(version: str, *, binding: Mapping[str, Any] | None = None) -> dict[str, Any]:
    safe_binding = _binding(binding)
    transaction_id = _digest(["release-v2", version, safe_binding])
    return {
        "schema": STATE_SCHEMA,
        "version": version,
        "transaction_id": transaction_id,
        "binding": safe_binding,
        "completed": [],
        "failures": [],
        "inflight": None,
    }


def make_evidence(
    state: Mapping[str, Any], stage: str, *, subject: str,
    evidence_id: str | None = None,
) -> dict[str, Any]:
    """Build deterministic typed evidence for an injected local runner."""
    if stage not in STAGES or not isinstance(subject, str) or not subject.strip():
        raise ValueError("release evidence needs a valid stage and subject")
    transaction_id = _fingerprint(state.get("transaction_id"))
    if not transaction_id:
        raise ValueError("release state lacks a transaction id")
    stable_id = evidence_id or _digest([transaction_id, stage, subject])
    return {
        "schema": EVIDENCE_SCHEMA,
        "transaction_id": transaction_id,
        "stage": stage,
        "outcome": "passed",
        "subject": subject.strip()[:256],
        "evidence_id": stable_id,
        "binding": _binding(state.get("binding")),
    }


def _normalize_evidence(
    state: Mapping[str, Any], stage: str, evidence: Any
) -> dict[str, Any]:
    if not isinstance(evidence, Mapping):
        raise ValueError("release evidence must be a typed object, not CLI text")
    expected_binding = _binding(state.get("binding"))
    schema = evidence.get("schema")
    transaction_id = _fingerprint(evidence.get("transaction_id"))
    outcome = evidence.get("outcome")
    subject = evidence.get("subject")
    evidence_id = _fingerprint(evidence.get("evidence_id"))
    if not (
        schema == EVIDENCE_SCHEMA
        and transaction_id == state.get("transaction_id")
        and evidence.get("stage") == stage
        and outcome in {"passed", "completed"}
        and isinstance(subject, str) and subject.strip() and len(subject.encode("utf-8")) <= 1024
        and evidence_id
        and _binding(evidence.get("binding")) == expected_binding
    ):
        raise ValueError("release evidence is not bound to this stage/transaction")
    return {
        "schema": EVIDENCE_SCHEMA,
        "transaction_id": transaction_id,
        "stage": stage,
        "outcome": "passed",
        "subject": subject.strip(),
        "evidence_id": evidence_id,
        "binding": expected_binding,
    }


def _checkpoint_record(
    state: Mapping[str, Any], stage: str, evidence: Mapping[str, Any], *, source: str,
    query_digest: str | None = None,
) -> dict[str, Any]:
    normalized = _normalize_evidence(state, stage, evidence)
    return {
        "stage": stage,
        "source": source,
        "evidence": normalized,
        "evidence_digest": _digest([
            state.get("transaction_id"), stage, source, normalized, query_digest,
        ]),
        "query_digest": _fingerprint(query_digest),
    }


def resume(state: Mapping[str, Any]) -> str | None:
    completed = state.get("completed")
    if not isinstance(completed, list):
        raise ValueError("release transaction completed checkpoint list is invalid")
    count = len(completed)
    return STAGES[count] if count < len(STAGES) else None


def _validate_state(value: Any, version: str) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema") != STATE_SCHEMA or value.get("version") != version:
        raise ValueError("release transaction state does not match requested version")
    expected = _empty(version, binding=value.get("binding"))
    if value.get("transaction_id") != expected["transaction_id"]:
        raise ValueError("release transaction binding or identity is invalid")
    completed = value.get("completed")
    if not isinstance(completed, list) or len(completed) > len(STAGES):
        raise ValueError("release transaction completed checkpoint list is invalid")
    if [item.get("stage") if isinstance(item, dict) else None for item in completed] != list(STAGES[:len(completed)]):
        raise ValueError("release transaction checkpoints are not an ordered prefix")
    normalized_completed: list[dict[str, Any]] = []
    for index, (stage, item) in enumerate(zip(STAGES, completed)):
        if not isinstance(item, dict) or item.get("source") not in {"query", "runner", "manual"}:
            raise ValueError("release transaction checkpoint evidence is invalid")
        prior = expected | {"completed": normalized_completed[:index]}
        normalized = _checkpoint_record(
            prior, stage, item.get("evidence"), source=item.get("source"),
            query_digest=item.get("query_digest"),
        )
        if item != normalized:
            raise ValueError("release transaction checkpoint digest is invalid")
        normalized_completed.append(normalized)
    failures = value.get("failures", [])
    if not isinstance(failures, list) or len(failures) > MAX_FAILURES:
        raise ValueError("release transaction failures are invalid")
    normalized_failures: list[dict[str, Any]] = []
    for failure in failures:
        if not isinstance(failure, dict) or failure.get("stage") not in STAGES:
            raise ValueError("release transaction failure is invalid")
        code = str(failure.get("code") or "")
        attempt = failure.get("attempt")
        if (
            not re.fullmatch(r"[a-z0-9_.-]{1,80}", code)
            or not isinstance(attempt, int) or isinstance(attempt, bool) or attempt <= 0
        ):
            raise ValueError("release transaction failure code is invalid")
        record = {
            "stage": failure["stage"], "code": code, "attempt": attempt,
            "digest": _digest([expected["transaction_id"], failure["stage"], code, attempt]),
        }
        if failure != record:
            raise ValueError("release transaction failure digest is invalid")
        normalized_failures.append(record)
    inflight = value.get("inflight")
    if inflight is not None:
        if not isinstance(inflight, dict) or inflight.get("stage") != resume({"completed": completed}):
            raise ValueError("release transaction inflight checkpoint is invalid")
        if not _fingerprint(inflight.get("action_id")) or not _fingerprint(inflight.get("query_digest")):
            raise ValueError("release transaction inflight evidence is invalid")
        if bool(inflight.get("external")) != (inflight.get("stage") in EXTERNAL_STAGES):
            raise ValueError("release transaction inflight external scope is invalid")
        inflight = {
            "stage": inflight["stage"], "action_id": inflight["action_id"],
            "query_digest": inflight["query_digest"], "external": bool(inflight.get("external")),
        }
    return {
        **expected,
        "completed": normalized_completed,
        "failures": normalized_failures,
        "inflight": inflight,
    }


def _load(path: Path, version: str) -> dict[str, Any]:
    if not path.exists():
        return _empty(version)
    value = json.loads(path.read_text(encoding="utf-8"))
    return _validate_state(value, version)


def _store(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".release-transaction-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(_canonical(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def _lock(path: Path) -> Iterator[None]:
    """Serialize local checkpoint writers; Windows falls back to atomic replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        yield
    finally:
        try:
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        os.close(descriptor)


def complete(
    state: dict[str, Any], stage: str, evidence: Any, *, allow_external: bool = False,
    source: str = "manual", query_digest: str | None = None,
) -> dict[str, Any]:
    """Persist exactly the next typed checkpoint; raw evidence is rejected."""
    if not _binding_is_complete(state.get("binding")):
        raise ValueError("release transaction requires a task epoch and execution contract binding")
    if stage not in STAGES or stage != resume(state):
        raise ValueError("stage is not the next checkpoint")
    if source not in {"query", "runner", "manual"}:
        raise ValueError("release checkpoint source is invalid")
    # A query-only checkpoint cannot perform an external side effect. It is
    # safe (and necessary after a crash) to record independently observed
    # completion without re-authorising the underlying action.
    if stage in EXTERNAL_STAGES and not allow_external and source != "query":
        raise PermissionError("external release stages require a separately authorized runner")
    state["completed"].append(
        _checkpoint_record(state, stage, evidence, source=source, query_digest=query_digest)
    )
    state["inflight"] = None
    return state


def _query_result(
    state: Mapping[str, Any], stage: str, result: Any
) -> tuple[str, Mapping[str, Any] | None, str]:
    if not isinstance(result, Mapping) or result.get("stage") not in {None, stage}:
        return "unknown", None, _digest([stage, "invalid-query"])
    status = result.get("status")
    digest = _digest([state.get("transaction_id"), stage, dict(result)])
    if status == "completed":
        try:
            evidence = _normalize_evidence(state, stage, result.get("evidence"))
        except ValueError:
            return "unknown", None, digest
        return "completed", evidence, digest
    if status == "absent":
        return "absent", None, digest
    return "unknown", None, digest


def _runner_result(
    state: Mapping[str, Any], stage: str, result: Any
) -> tuple[str, Mapping[str, Any] | None, str]:
    if not isinstance(result, Mapping):
        return "failed", None, "runner_non_structured"
    if result.get("status") == "failed":
        code = str(result.get("failure_code") or "runner_failed").lower()
        return "failed", None, re.sub(r"[^a-z0-9_.-]", "-", code)[:80] or "runner_failed"
    try:
        return "passed", _normalize_evidence(state, stage, result), "passed"
    except ValueError:
        return "failed", None, "runner_evidence_invalid"


def _record_failure(
    state: dict[str, Any], stage: str, code: str, *, clear_inflight: bool = True,
) -> None:
    attempts = 1 + sum(1 for item in state.get("failures", []) if item.get("stage") == stage)
    record = {
        "stage": stage, "code": code, "attempt": attempts,
        "digest": _digest([state.get("transaction_id"), stage, code, attempts]),
    }
    state["failures"] = [*state.get("failures", []), record][-MAX_FAILURES:]
    if clear_inflight:
        state["inflight"] = None


def _injected_code(stage: str, failure_injection: Any) -> str | None:
    if failure_injection is None:
        return None
    if callable(failure_injection):
        value = failure_injection(stage)
    elif isinstance(failure_injection, Mapping):
        value = failure_injection.get(stage)
    else:
        value = stage if stage in failure_injection else None
    if value in (None, False):
        return None
    raw = str(value if value is not True else "injected_failure").lower()
    return re.sub(r"[^a-z0-9_.-]", "-", raw)[:80] or "injected_failure"


def _context(state: Mapping[str, Any], stage: str) -> dict[str, Any]:
    return {
        "transaction_id": state.get("transaction_id"), "version": state.get("version"),
        "stage": stage, "binding": _binding(state.get("binding")),
        "completed": [item.get("stage") for item in state.get("completed", [])],
    }


def advance(
    state: dict[str, Any], runner: ReleaseRunner, *, allow_external: bool = False,
    failure_injection: Any = None,
) -> dict[str, Any]:
    """Query first, then run at most one stage with typed evidence only."""
    if not _binding_is_complete(state.get("binding")):
        raise ValueError("release transaction requires a task epoch and execution contract binding")
    stage = resume(state)
    if stage is None:
        return {"action": "sealed", "stage": None}
    status, evidence, query_digest = _query_result(
        state, stage, runner.query(stage, _context(state, stage))
    )
    if status == "completed":
        complete(state, stage, evidence, allow_external=allow_external, source="query", query_digest=query_digest)
        return {"action": "query_complete", "stage": stage}
    inflight = state.get("inflight")
    if status == "unknown":
        _record_failure(
            state, stage, "query_unknown",
            clear_inflight=not bool(inflight and inflight.get("external")),
        )
        return {"action": "query_unknown", "stage": stage}
    if inflight and inflight.get("stage") == stage and inflight.get("external"):
        raise ExternalActionUncertain("external stage has an unresolved inflight checkpoint")
    injected = _injected_code(stage, failure_injection)
    if injected:
        _record_failure(state, stage, injected)
        return {"action": "injected_failure", "stage": stage}
    if stage in EXTERNAL_STAGES and not allow_external:
        raise PermissionError("external release stages require a separately authorized runner")
    state["inflight"] = {
        "stage": stage,
        "action_id": _digest([state.get("transaction_id"), stage, query_digest, len(state.get("failures", []))]),
        "query_digest": query_digest,
        "external": stage in EXTERNAL_STAGES,
    }
    outcome, evidence, code = _runner_result(state, stage, runner.run(stage, _context(state, stage)))
    if outcome != "passed":
        # A runner failure after an external invocation is not proof that no
        # side effect escaped. Preserve the durable inflight checkpoint and
        # force a subsequent query rather than making a duplicate eligible.
        _record_failure(state, stage, code, clear_inflight=stage not in EXTERNAL_STAGES)
        return {"action": "runner_failure", "stage": stage}
    complete(state, stage, evidence, allow_external=allow_external, source="runner", query_digest=query_digest)
    return {"action": "runner_complete", "stage": stage}


def run_next(
    path: Path, version: str, runner: ReleaseRunner, *, allow_external: bool = False,
    failure_injection: Any = None,
    binding: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Persist one stage, querying before any possible repeat."""
    if not _binding_is_complete(binding):
        raise ValueError("release transaction requires a task epoch and execution contract binding")
    with _lock(path):
        state = _load(path, version) if path.exists() else _empty(version, binding=binding)
        if _binding(binding) != state.get("binding"):
            raise ValueError("release transaction binding does not match requested task epoch")
        stage = resume(state)
        if stage is None:
            return state, {"action": "sealed", "stage": None}
        status, evidence, query_digest = _query_result(
            state, stage, runner.query(stage, _context(state, stage))
        )
        if status == "completed":
            complete(state, stage, evidence, allow_external=allow_external, source="query", query_digest=query_digest)
            _store(path, state)
            return state, {"action": "query_complete", "stage": stage}
        inflight = state.get("inflight")
        if status == "unknown":
            _record_failure(
                state, stage, "query_unknown",
                clear_inflight=not bool(inflight and inflight.get("external")),
            )
            _store(path, state)
            return state, {"action": "query_unknown", "stage": stage}
        if inflight and inflight.get("stage") == stage and inflight.get("external"):
            raise ExternalActionUncertain("external stage has an unresolved inflight checkpoint")
        injected = _injected_code(stage, failure_injection)
        if injected:
            _record_failure(state, stage, injected)
            _store(path, state)
            return state, {"action": "injected_failure", "stage": stage}
        if stage in EXTERNAL_STAGES and not allow_external:
            raise PermissionError("external release stages require a separately authorized runner")
        state["inflight"] = {
            "stage": stage,
            "action_id": _digest([state.get("transaction_id"), stage, query_digest, len(state.get("failures", []))]),
            "query_digest": query_digest,
            "external": stage in EXTERNAL_STAGES,
        }
        _store(path, state)
        outcome, evidence, code = _runner_result(state, stage, runner.run(stage, _context(state, stage)))
        if outcome != "passed":
            _record_failure(
                state, stage, code, clear_inflight=stage not in EXTERNAL_STAGES
            )
            _store(path, state)
            return state, {"action": "runner_failure", "stage": stage}
        complete(state, stage, evidence, allow_external=allow_external, source="runner", query_digest=query_digest)
        _store(path, state)
        return state, {"action": "runner_complete", "stage": stage}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--show", action="store_true")
    parser.add_argument("--task-epoch-id", required=True)
    parser.add_argument("--execution-contract-id", required=True)
    args = parser.parse_args()
    binding = _binding({
        "task_epoch_id": args.task_epoch_id,
        "execution_contract_id": args.execution_contract_id,
    })
    state = _load(args.state, args.version) if args.state.exists() else _empty(args.version, binding=binding)
    if not _binding_is_complete(binding) or binding != state["binding"]:
        raise ValueError("release transaction binding does not match requested task epoch")
    print(json.dumps({
        "transaction_id": state["transaction_id"], "next_checkpoint": resume(state),
        "completed": [item["stage"] for item in state["completed"]], "inflight": state["inflight"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
