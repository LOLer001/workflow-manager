from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import re
import unittest
from pathlib import Path
from typing import Any
import io
import os
import tempfile
from contextlib import redirect_stdout
from unittest.mock import patch


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PLUGIN_ROOT / "scripts" / "orchestrator_hook.py"
SCENARIO_DIR = Path(__file__).with_name("scenarios")

SPEC = importlib.util.spec_from_file_location("orchestrator_hook_trace_eval", SCRIPT)
assert SPEC and SPEC.loader
HOOK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HOOK)

RELEASE_BLOCKERS = {
    "R02",
    "R03",
    "R04",
    "R06",
    "A02",
    "A03",
    "C01",
    "C02",
    "C03",
    "U01",
    "U02",
    "F01",
    "F02",
    "Q01",
}
PRODUCTION_ROUTE_FIELDS = {
    "delegation_gate",
    "readiness_signal",
    "dependency_signal",
    "meta_delegation",
    "lane_signal",
    "recommended_agent_cap",
}


class TraceEvaluationError(AssertionError):
    pass


def stable_fingerprint(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def dotted_value(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise KeyError(path)
        current = current[part]
    return current


class PolicyTraceEvaluator:
    """Small deterministic policy model for release-blocking orchestration traces."""

    def __init__(self, scenario: dict[str, Any]) -> None:
        self.scenario = scenario
        initial = scenario.get("initial", {})
        self.agent_cap = int(initial.get("agent_cap", 0))
        self.parent_active = bool(initial.get("parent_active", True))
        self.agent_cap_history = [self.agent_cap]
        self.objective: dict[str, Any] | None = None
        self.archived_objectives: list[str] = []
        self.active_agents: dict[str, dict[str, Any]] = {}
        self.max_active_subagents = 0
        self.max_total_lanes = 1 if self.parent_active else 0
        self.spawn_allowed = 0
        self.spawn_denied = 0
        self.accepted_results = 0
        self.stale_results = 0
        self.result_commits = 0
        self.artifacts: dict[str, bool] = {}
        self.serial_order = list(initial.get("serial_order", []))
        self.active_operations: dict[str, str] = {}
        self.completed_order: list[str] = []
        self.operation_started = 0
        self.operation_denied = 0
        self.max_parallel_operations = 0
        self.hook_operations: list[dict[str, Any]] = []
        self.resources: dict[str, str] = {}
        self.resource_acquires = 0
        self.resource_conflicts = 0
        self.max_resource_holders: dict[str, int] = {}
        self.pressure_decisions: list[dict[str, Any]] = []
        self.terminal_successes: dict[str, str] = {}
        self.execution_counts: dict[str, int] = {}
        self.unchanged_success_reuses = 0
        self.tool_attempt_denied = 0
        self.checkpoint_required: list[str] = []
        self.checkpoint_fields: dict[str, Any] = {}
        self.checkpoint_repair_fields: list[str] = []
        self.narrow_checks = 0
        self.workflow_replays = 0
        self.workflow_replay_denied = 0
        self.retry_ledger: dict[str, dict[str, int]] = {}
        self.compactions = 0
        self.resumes = 0
        self.resume_checkpoint: dict[str, Any] | None = None
        self.denials: dict[str, int] = {}
        self.decisions: list[dict[str, Any]] = []
        self.sequence = 0

    def run(self) -> dict[str, Any]:
        for index, event in enumerate(self.scenario.get("events", [])):
            self.sequence += 1
            decision = self._dispatch(event)
            self.decisions.append({"index": index, "type": event["type"], **decision})
            self._check_event_expectation(index, event, decision)
            self._check_invariants(index)
        return self.snapshot()

    def _allow(self, **details: Any) -> dict[str, Any]:
        return {"allowed": True, **details}

    def _deny(self, reason: str, **details: Any) -> dict[str, Any]:
        self.denials[reason] = self.denials.get(reason, 0) + 1
        return {"allowed": False, "reason": reason, **details}

    def _check_event_expectation(
        self, index: int, event: dict[str, Any], decision: dict[str, Any]
    ) -> None:
        expected = event.get("expect")
        if expected is None:
            return
        if not isinstance(expected, dict) or not expected:
            raise TraceEvaluationError(f"{self.scenario['id']} event {index}: empty event expectation")
        for key, value in expected.items():
            if decision.get(key) != value:
                raise TraceEvaluationError(
                    f"{self.scenario['id']} event {index} {event['type']}: "
                    f"expected {key}={value!r}, got {decision.get(key)!r}"
                )

    def _check_invariants(self, index: int) -> None:
        active = len(self.active_agents)
        if active > self.agent_cap:
            raise TraceEvaluationError(
                f"{self.scenario['id']} event {index}: {active} active subagents exceeds cap {self.agent_cap}"
            )
        if self.serial_order and len(self.active_operations) > 1:
            raise TraceEvaluationError(f"{self.scenario['id']} event {index}: serial stages overlap")
        if any(item["retries"] > 1 for item in self.retry_ledger.values()):
            raise TraceEvaluationError(f"{self.scenario['id']} event {index}: retry limit exceeded")

    def _dispatch(self, event: dict[str, Any]) -> dict[str, Any]:
        event_type = event.get("type")
        handler = getattr(self, f"_event_{event_type}", None)
        if not isinstance(event_type, str) or handler is None:
            raise TraceEvaluationError(f"{self.scenario['id']}: unsupported event {event_type!r}")
        return handler(event)

    def _event_objective(self, event: dict[str, Any]) -> dict[str, Any]:
        mode = event.get("mode")
        if mode == "append_constraint":
            if self.objective is None:
                return self._deny("no_active_objective")
            constraint = str(event.get("constraint") or "")
            if constraint and constraint not in self.objective["constraints"]:
                self.objective["constraints"].append(constraint)
            return self._allow(
                objective_id=self.objective["id"], generation=self.objective["generation"]
            )

        if mode not in {"start", "replace"}:
            return self._deny("invalid_objective_mode")
        if mode == "start" and self.objective is not None:
            return self._deny("objective_already_active")
        if self.objective is not None:
            self.archived_objectives.append(self.objective["id"])
        generation = 1 if self.objective is None else int(self.objective["generation"]) + 1
        self.objective = {
            "id": str(event["id"]),
            "generation": generation,
            "goal": str(event.get("goal") or ""),
            "constraints": list(event.get("constraints", [])),
        }
        return self._allow(objective_id=self.objective["id"], generation=generation)

    def _event_artifact_declare(self, event: dict[str, Any]) -> dict[str, Any]:
        artifact = str(event["artifact"])
        self.artifacts[artifact] = bool(event.get("ready", False))
        return self._allow(artifact=artifact, ready=self.artifacts[artifact])

    def _event_artifact_ready(self, event: dict[str, Any]) -> dict[str, Any]:
        artifact = str(event["artifact"])
        if artifact not in self.artifacts:
            return self._deny("artifact_unknown", artifact=artifact)
        self.artifacts[artifact] = True
        return self._allow(artifact=artifact, ready=True)

    def _event_spawn(self, event: dict[str, Any]) -> dict[str, Any]:
        agent = str(event["agent"])
        scope = str(event["scope"])
        requirements = [str(item) for item in event.get("requires", [])]
        missing = sorted(item for item in requirements if not self.artifacts.get(item, False))
        if missing:
            self.spawn_denied += 1
            return self._deny("artifact_not_ready", missing=missing)
        if any(item["scope"] == scope for item in self.active_agents.values()):
            self.spawn_denied += 1
            return self._deny("duplicate_scope", scope=scope)
        if agent in self.active_agents:
            self.spawn_denied += 1
            return self._deny("duplicate_agent", agent=agent)
        if len(self.active_agents) >= self.agent_cap:
            self.spawn_denied += 1
            return self._deny("agent_cap", cap=self.agent_cap)
        if self.objective is None:
            self.spawn_denied += 1
            return self._deny("no_active_objective")
        self.active_agents[agent] = {
            "agent": agent,
            "scope": scope,
            "objective_id": self.objective["id"],
            "objective_generation": self.objective["generation"],
        }
        self.spawn_allowed += 1
        self._update_agent_peaks()
        return self._allow(agent=agent, scope=scope)

    def _event_agent_result(self, event: dict[str, Any]) -> dict[str, Any]:
        agent = str(event["agent"])
        record = self.active_agents.pop(agent, None)
        if record is None:
            return self._deny("agent_not_active", agent=agent)
        current_key = (
            self.objective["id"],
            self.objective["generation"],
        ) if self.objective else (None, None)
        record_key = (record["objective_id"], record["objective_generation"])
        if record_key != current_key:
            self.stale_results += 1
            return self._allow(disposition="stale", committed=False)
        self.accepted_results += 1
        self.result_commits += 1
        return self._allow(disposition="accepted", committed=True)

    def _event_compact(self, event: dict[str, Any]) -> dict[str, Any]:
        checkpoint = {
            "objective": self.objective,
            "agent_cap": self.agent_cap,
            "active_agents": list(self.active_agents.values()),
            "artifacts": self.artifacts,
            "terminal_successes": self.terminal_successes,
        }
        self.resume_checkpoint = json.loads(json.dumps(checkpoint, ensure_ascii=False, sort_keys=True))
        self.compactions += 1
        return self._allow(compactions=self.compactions)

    def _event_resume(self, event: dict[str, Any]) -> dict[str, Any]:
        if self.resume_checkpoint is None:
            return self._deny("checkpoint_missing")
        checkpoint = copy.deepcopy(self.resume_checkpoint)
        self.objective = checkpoint["objective"]
        self.agent_cap = int(checkpoint["agent_cap"])
        self.agent_cap_history.append(self.agent_cap)
        self.active_agents = {item["agent"]: item for item in checkpoint["active_agents"]}
        self.artifacts = checkpoint["artifacts"]
        self.terminal_successes = checkpoint["terminal_successes"]
        self.resumes += 1
        self._update_agent_peaks()
        return self._allow(resumes=self.resumes, active_agents=len(self.active_agents))

    def _event_operation_start(self, event: dict[str, Any]) -> dict[str, Any]:
        operation = str(event["operation"])
        stage = str(event["stage"])
        if self.active_operations:
            self.operation_denied += 1
            return self._deny("serial_pipeline_busy")
        if self.serial_order:
            next_index = len(self.completed_order)
            expected = self.serial_order[next_index] if next_index < len(self.serial_order) else None
            if stage != expected:
                self.operation_denied += 1
                return self._deny("dependency_order", expected_stage=expected)
        self.active_operations[operation] = stage
        self.operation_started += 1
        self.max_parallel_operations = max(self.max_parallel_operations, len(self.active_operations))
        return self._allow(operation=operation, stage=stage)

    def _event_operation_end(self, event: dict[str, Any]) -> dict[str, Any]:
        operation = str(event["operation"])
        stage = self.active_operations.pop(operation, None)
        if stage is None:
            return self._deny("operation_not_active")
        self.completed_order.append(stage)
        category = {
            "build": "build_package",
            "deploy": "delivery_device",
            "device": "delivery_device",
        }.get(stage, stage)
        self.hook_operations.append({"category": category, "status": "ok"})
        return self._allow(operation=operation, stage=stage)

    def _event_resource_acquire(self, event: dict[str, Any]) -> dict[str, Any]:
        resource = str(event["resource"])
        owner = str(event["owner"])
        held_by = self.resources.get(resource)
        if held_by is not None and held_by != owner:
            self.resource_conflicts += 1
            return self._deny("resource_busy", resource=resource, held_by=held_by)
        self.resources[resource] = owner
        self.resource_acquires += 1
        self.max_resource_holders[resource] = max(self.max_resource_holders.get(resource, 0), 1)
        return self._allow(resource=resource, owner=owner)

    def _event_resource_release(self, event: dict[str, Any]) -> dict[str, Any]:
        resource = str(event["resource"])
        owner = str(event["owner"])
        if self.resources.get(resource) != owner:
            return self._deny("resource_not_owned", resource=resource)
        del self.resources[resource]
        return self._allow(resource=resource, owner=owner)

    def _event_pressure(self, event: dict[str, Any]) -> dict[str, Any]:
        pressure = float(event["value"])
        trim = pressure >= 0.55
        checkpoint = pressure >= 0.70
        decision = {
            "pressure": pressure,
            "trim": trim,
            "checkpoint": checkpoint,
            "stop_broad": checkpoint,
            "agent_cap": self.agent_cap,
        }
        self.pressure_decisions.append(decision)
        self.agent_cap_history.append(self.agent_cap)
        return self._allow(**decision)

    def _event_tool_success(self, event: dict[str, Any]) -> dict[str, Any]:
        operation = str(event["operation"])
        fingerprint = stable_fingerprint(event["state"])
        self.terminal_successes[operation] = fingerprint
        self.execution_counts[operation] = self.execution_counts.get(operation, 0) + 1
        return self._allow(operation=operation, fingerprint=fingerprint)

    def _event_tool_attempt(self, event: dict[str, Any]) -> dict[str, Any]:
        operation = str(event["operation"])
        fingerprint = stable_fingerprint(event["state"])
        if self.terminal_successes.get(operation) == fingerprint:
            self.unchanged_success_reuses += 1
            self.tool_attempt_denied += 1
            return self._deny("unchanged_success", operation=operation)
        self.execution_counts[operation] = self.execution_counts.get(operation, 0) + 1
        return self._allow(operation=operation)

    def _event_checkpoint(self, event: dict[str, Any]) -> dict[str, Any]:
        self.checkpoint_required = [str(item) for item in event["required_fields"]]
        self.checkpoint_fields = copy.deepcopy(event.get("fields", {}))
        unknown = sorted(set(self.checkpoint_fields) - set(self.checkpoint_required))
        if unknown:
            return self._deny("checkpoint_unknown_fields", fields=unknown)
        return self._allow(missing=self._missing_checkpoint_fields())

    def _event_checkpoint_repair(self, event: dict[str, Any]) -> dict[str, Any]:
        field = str(event["field"])
        if event.get("scope") != "exact":
            return self._deny("broad_checkpoint_repair", field=field)
        if field not in self.checkpoint_required:
            return self._deny("checkpoint_field_not_required", field=field)
        if field in self.checkpoint_fields:
            return self._deny("checkpoint_field_known", field=field)
        if field in self.checkpoint_repair_fields:
            return self._deny("checkpoint_repair_repeated", field=field)
        self.checkpoint_repair_fields.append(field)
        self.checkpoint_fields[field] = copy.deepcopy(event.get("value"))
        self.narrow_checks += 1
        return self._allow(field=field, scope="exact")

    def _event_workflow_replay(self, event: dict[str, Any]) -> dict[str, Any]:
        self.workflow_replay_denied += 1
        return self._deny("checkpoint_repair_only")

    def _ledger(self, operation: str) -> dict[str, int]:
        return self.retry_ledger.setdefault(
            operation,
            {
                "attempts": 0,
                "failures": 0,
                "retries": 0,
                "material_corrections": 0,
                "last_failure_seq": 0,
                "correction_seq": 0,
            },
        )

    def _event_attempt(self, event: dict[str, Any]) -> dict[str, Any]:
        operation = str(event["operation"])
        ledger = self._ledger(operation)
        if ledger["attempts"]:
            return self._deny("retry_event_required")
        ledger["attempts"] = 1
        return self._allow(operation=operation, attempt=1)

    def _event_failure(self, event: dict[str, Any]) -> dict[str, Any]:
        operation = str(event["operation"])
        ledger = self._ledger(operation)
        if not ledger["attempts"]:
            return self._deny("failure_without_attempt")
        ledger["failures"] += 1
        ledger["last_failure_seq"] = self.sequence
        ledger["correction_seq"] = 0
        return self._allow(operation=operation, failures=ledger["failures"])

    def _event_correction(self, event: dict[str, Any]) -> dict[str, Any]:
        operation = str(event["operation"])
        ledger = self._ledger(operation)
        if not bool(event.get("material")):
            return self._deny("non_material_correction", operation=operation)
        if not ledger["last_failure_seq"]:
            return self._deny("correction_without_failure", operation=operation)
        ledger["material_corrections"] += 1
        ledger["correction_seq"] = self.sequence
        return self._allow(operation=operation, material=True)

    def _event_retry(self, event: dict[str, Any]) -> dict[str, Any]:
        operation = str(event["operation"])
        ledger = self._ledger(operation)
        if ledger["retries"] >= 1:
            return self._deny("retry_limit", operation=operation)
        if ledger["correction_seq"] <= ledger["last_failure_seq"]:
            return self._deny("material_correction_required", operation=operation)
        ledger["retries"] += 1
        ledger["attempts"] += 1
        return self._allow(operation=operation, retry=ledger["retries"], attempt=ledger["attempts"])

    def _update_agent_peaks(self) -> None:
        active = len(self.active_agents)
        self.max_active_subagents = max(self.max_active_subagents, active)
        total = active + (1 if self.parent_active else 0)
        self.max_total_lanes = max(self.max_total_lanes, total)

    def _missing_checkpoint_fields(self) -> list[str]:
        return [field for field in self.checkpoint_required if field not in self.checkpoint_fields]

    def snapshot(self) -> dict[str, Any]:
        retry = {
            operation: {
                key: value
                for key, value in ledger.items()
                if key in {"attempts", "failures", "retries", "material_corrections"}
            }
            for operation, ledger in sorted(self.retry_ledger.items())
        }
        return {
            "objective": copy.deepcopy(self.objective),
            "archived_objectives": list(self.archived_objectives),
            "agent_cap": self.agent_cap,
            "max_agent_cap": max(self.agent_cap_history),
            "active_agent_ids": sorted(self.active_agents),
            "active_agent_scopes": sorted(item["scope"] for item in self.active_agents.values()),
            "max_active_subagents": self.max_active_subagents,
            "max_total_lanes": self.max_total_lanes,
            "spawn_allowed": self.spawn_allowed,
            "spawn_denied": self.spawn_denied,
            "accepted_results": self.accepted_results,
            "stale_results": self.stale_results,
            "result_commits": self.result_commits,
            "artifacts": dict(sorted(self.artifacts.items())),
            "completed_order": list(self.completed_order),
            "operation_started": self.operation_started,
            "operation_denied": self.operation_denied,
            "max_parallel_operations": self.max_parallel_operations,
            "resources": dict(sorted(self.resources.items())),
            "resource_acquires": self.resource_acquires,
            "resource_conflicts": self.resource_conflicts,
            "max_resource_holders": dict(sorted(self.max_resource_holders.items())),
            "pressure_decisions": copy.deepcopy(self.pressure_decisions),
            "execution_counts": dict(sorted(self.execution_counts.items())),
            "unchanged_success_reuses": self.unchanged_success_reuses,
            "tool_attempt_denied": self.tool_attempt_denied,
            "checkpoint": {
                "missing_fields": self._missing_checkpoint_fields(),
                "repair_fields": list(self.checkpoint_repair_fields),
                "narrow_checks": self.narrow_checks,
            },
            "workflow_replays": self.workflow_replays,
            "workflow_replay_denied": self.workflow_replay_denied,
            "retry": retry,
            "compactions": self.compactions,
            "resumes": self.resumes,
            "denials": dict(sorted(self.denials.items())),
        }

    def hook_state(self, route: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "last_route": route or {},
            "operations": copy.deepcopy(self.hook_operations),
            "subagents": [
                {
                    "event": "start",
                    "agent_id": item["agent"],
                    "scope_fingerprint": stable_fingerprint(item["scope"]),
                }
                for item in self.active_agents.values()
            ],
        }


def load_scenarios() -> list[dict[str, Any]]:
    scenarios: list[dict[str, Any]] = []
    for path in sorted(SCENARIO_DIR.glob("*.json")):
        scenario = json.loads(path.read_text(encoding="utf-8"))
        scenario["_path"] = str(path)
        scenarios.append(scenario)
    return scenarios


class PolicyTraceEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.scenarios = load_scenarios()

    @staticmethod
    def _capture_hook(function: Any, *args: Any) -> str:
        output = io.StringIO()
        with redirect_stdout(output):
            function(*args)
        return output.getvalue()

    @staticmethod
    def _load_hook_state(session_id: str) -> dict[str, Any]:
        payload = {"session_id": session_id}
        return HOOK.load_state(HOOK.state_path(payload), payload)

    def test_release_blocker_scenarios_are_complete_and_structured(self) -> None:
        ids = [scenario.get("id") for scenario in self.scenarios]
        self.assertEqual(set(ids), RELEASE_BLOCKERS)
        self.assertEqual(len(ids), len(set(ids)))
        for scenario in self.scenarios:
            with self.subTest(scenario=scenario.get("id")):
                self.assertEqual(scenario.get("schema_version"), 1)
                self.assertRegex(str(scenario.get("id")), r"^[A-Z][0-9]{2}$")
                self.assertIsInstance(scenario.get("events"), list)
                self.assertTrue(scenario["events"])
                self.assertIsInstance(scenario.get("expect"), dict)
                self.assertGreaterEqual(len(scenario["expect"]), 2)
                self.assertTrue(all(isinstance(event, dict) and event.get("type") for event in scenario["events"]))
                serialized = json.dumps(scenario, ensure_ascii=False).lower()
                self.assertNotIn("contains_text", serialized)
                self.assertNotIn("message_substring", serialized)

        r04 = next(item for item in self.scenarios if item["id"] == "R04")
        self.assertEqual(r04["coverage"]["trace"], "model_level_operation_order")
        self.assertEqual(r04["coverage"]["production"], "route_fields_and_spawn_gate_only")

    def test_release_blocker_traces_enforce_structural_invariants(self) -> None:
        for scenario in self.scenarios:
            with self.subTest(scenario=scenario["id"]):
                evaluator = PolicyTraceEvaluator(scenario)
                snapshot = evaluator.run()
                for path, expected in scenario["expect"].items():
                    try:
                        actual = dotted_value(snapshot, path)
                    except KeyError as error:
                        self.fail(f"{scenario['id']} unknown expectation path: {error}")
                    self.assertEqual(actual, expected, f"{scenario['id']} invariant {path}")

    def test_production_classifier_matches_trace_routing_contracts(self) -> None:
        checked: set[str] = set()
        for scenario in self.scenarios:
            production = scenario.get("production")
            if not production:
                continue
            checked.add(str(scenario["id"]))
            with self.subTest(scenario=scenario["id"]):
                route = HOOK.classify_prompt(production["prompt"])
                self.assertTrue(PRODUCTION_ROUTE_FIELDS <= set(route), route)
                self.assertIsInstance(route["meta_delegation"], bool)
                for path, expected in production["route"].items():
                    self.assertEqual(dotted_value(route, path), expected, f"{scenario['id']} route {path}")
                normalized = HOOK.safe_route(route)
                for field in PRODUCTION_ROUTE_FIELDS:
                    self.assertEqual(normalized[field], route[field], f"safe_route dropped {field}")
                if route["label"] == "complex":
                    self.assertLessEqual(route["recommended_agent_cap"], 1)
                if route["label"] == "extensive" and route["recommended_agent_cap"] == 2:
                    self.assertEqual(route["delegation_gate"], "open")
                    self.assertEqual(route["readiness_signal"], "ready_three_plus")
        self.assertGreaterEqual(len(checked), 3)
        self.assertIn("R06", checked, "shared-resource policy must be cross-checked against production routing")


    def test_r06_production_pretool_denies_shared_device_spawn(self) -> None:
        scenario = next(item for item in self.scenarios if item["id"] == "R06")
        native_tmp = "/tmp" if Path("/tmp").is_dir() else None
        with tempfile.TemporaryDirectory(prefix="trace-r06-", dir=native_tmp) as data:
            with patch.dict(os.environ, {"PLUGIN_DATA": data}, clear=False):
                session = "trace-r06-production"
                self._capture_hook(
                    HOOK.user_prompt_submit,
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "session_id": session,
                        "turn_id": "turn-r06",
                        "hook_run_id": "prompt",
                        "prompt": scenario["production"]["prompt"],
                    },
                )
                denied_output = self._capture_hook(
                    HOOK.pre_tool_use,
                    {
                        "hook_event_name": "PreToolUse",
                        "session_id": session,
                        "turn_id": "turn-r06",
                        "hook_run_id": "spawn",
                        "tool_name": "Agent",
                        "tool_input": {
                            "description": "device-lane",
                            "prompt": "Install, reboot, and validate on the only connected device",
                        },
                    },
                )
                denied = json.loads(denied_output)["hookSpecificOutput"]
                self.assertEqual(denied["permissionDecision"], "deny")
                self.assertIn("delegation gate is closed", denied["permissionDecisionReason"])
                state = self._load_hook_state(session)
                self.assertEqual(HOOK.active_agent_count(state), 0)
                self.assertFalse(any(item["event"] == "request" for item in state["subagents"]))
                self.assertEqual(state["guards"][-1]["kind"], "subagent_gate")

    def test_pressure_evaluation_does_not_mutate_production_agent_cap(self) -> None:
        scenario = next(item for item in self.scenarios if item["id"] == "C01")
        route = HOOK.classify_prompt(scenario["production"]["prompt"])
        baseline = copy.deepcopy(route)
        HOOK.routing_context(route, {"pressure": 0.55})
        self.assertEqual(route, baseline)
        HOOK.routing_context(route, {"pressure": 0.70})
        self.assertEqual(route, baseline)
        self.assertEqual(route["recommended_agent_cap"], scenario["initial"]["agent_cap"])

    def test_trace_snapshots_cross_check_production_state_helpers(self) -> None:
        scenarios = {scenario["id"]: scenario for scenario in self.scenarios}

        active_eval = PolicyTraceEvaluator(scenarios["A02"])
        active_eval.run()
        active_state = active_eval.hook_state()
        self.assertEqual(HOOK.active_agent_count(active_state), 1)
        self.assertEqual(len(HOOK.active_agent_records(active_state)), 1)

        parent_eval = PolicyTraceEvaluator(scenarios["R02"])
        parent_snapshot = parent_eval.run()
        parent_route = HOOK.classify_prompt(scenarios["R02"]["production"]["prompt"])
        parent_state = parent_eval.hook_state(parent_route)
        active_subagents = HOOK.active_agent_count(parent_state)
        self.assertEqual(parent_route["agent_mode"], "parent_plus_one")
        self.assertEqual(active_subagents, parent_route["recommended_agent_cap"])
        self.assertEqual(active_subagents + 1, parent_snapshot["max_total_lanes"])

        stage_eval = PolicyTraceEvaluator(scenarios["R04"])
        stage_eval.run()
        route = HOOK.classify_prompt(scenarios["R04"]["production"]["prompt"])
        stage_state = stage_eval.hook_state(route)
        self.assertEqual(HOOK.current_execution_stage(stage_state), "deliver")
    def test_production_hook_replay_persists_active_agent_across_compaction(self) -> None:
        native_tmp = "/tmp" if Path("/tmp").is_dir() else None
        scenario = next(item for item in self.scenarios if item["id"] == "R02")
        with tempfile.TemporaryDirectory(prefix="trace-a02-", dir=native_tmp) as data:
            with patch.dict(os.environ, {"PLUGIN_DATA": data}, clear=False):
                session = "trace-a02-production"
                self._capture_hook(
                    HOOK.user_prompt_submit,
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "session_id": session,
                        "turn_id": "turn-1",
                        "hook_run_id": "prompt",
                        "prompt": scenario["production"]["prompt"],
                    },
                )
                objective = self._load_hook_state(session)["objective"]["fingerprint"]
                approved_output = self._capture_hook(
                    HOOK.pre_tool_use,
                    {
                        "hook_event_name": "PreToolUse",
                        "session_id": session,
                        "turn_id": "turn-1",
                        "hook_run_id": "agent-request",
                        "tool_name": "Agent",
                        "tool_input": {
                            "description": "audit_01_source",
                            "prompt": "Inspect the source lane only",
                        },
                    },
                )
                approved_event = json.loads(approved_output)["hookSpecificOutput"]
                self.assertNotIn("permissionDecision", approved_event)
                self._capture_hook(
                    HOOK.subagent_start,
                    {
                        "hook_event_name": "SubagentStart",
                        "session_id": session,
                        "turn_id": "turn-1",
                        "hook_run_id": "agent-start",
                        "agent_id": "audit-1",
                        "agent_type": "default",
                    },
                )
                started = self._load_hook_state(session)
                active = HOOK.active_agent_records(started)
                self.assertEqual(len(active), 1)
                self.assertEqual(active[0]["objective_fingerprint"], objective)
                self.assertEqual(active[0]["task_name"], "audit_01_source")
                self.assertIsNotNone(active[0]["scope_fingerprint"])

                self._capture_hook(
                    HOOK.compact_event,
                    {
                        "hook_event_name": "PreCompact",
                        "session_id": session,
                        "hook_run_id": "compact",
                        "trigger": "auto",
                    },
                    "pre",
                )
                compacted = self._load_hook_state(session)
                checkpoint = compacted["compactions"][-1]
                self.assertEqual(checkpoint["current_stage"], "contract")
                self.assertEqual(len(checkpoint["active_agent_scopes"]), 1)
                scope = checkpoint["active_agent_scopes"][0]
                self.assertEqual(scope["objective_fingerprint"], objective)
                self.assertEqual(scope["scope_fingerprint"], active[0]["scope_fingerprint"])

                self._capture_hook(
                    HOOK.session_start,
                    {
                        "hook_event_name": "SessionStart",
                        "session_id": session,
                        "hook_run_id": "resume",
                        "source": "resume",
                    },
                )
                resumed = self._load_hook_state(session)
                self.assertEqual(HOOK.active_agent_count(resumed), 1)
                self.assertEqual(
                    HOOK.active_agent_records(resumed)[0]["scope_fingerprint"],
                    active[0]["scope_fingerprint"],
                )
                duplicate_output = self._capture_hook(
                    HOOK.pre_tool_use,
                    {
                        "hook_event_name": "PreToolUse",
                        "session_id": session,
                        "turn_id": "turn-1",
                        "hook_run_id": "agent-duplicate-request",
                        "tool_name": "Agent",
                        "tool_input": {
                            "description": "audit_01_source",
                            "prompt": "Inspect the source lane only",
                        },
                    },
                )
                duplicate_event = json.loads(duplicate_output)["hookSpecificOutput"]
                self.assertEqual(duplicate_event["permissionDecision"], "deny")
                self.assertIn("same task name or scope", duplicate_event["permissionDecisionReason"])

                after_denial = self._load_hook_state(session)
                self.assertEqual(HOOK.active_agent_count(after_denial), 1)
                self.assertEqual(after_denial["last_route"]["recommended_agent_cap"], 1)
                self.assertEqual(
                    sum(item["event"] == "start" for item in after_denial["subagents"]),
                    1,
                )


    def test_production_hook_replay_marks_old_objective_result_stale(self) -> None:
        native_tmp = "/tmp" if Path("/tmp").is_dir() else None
        with tempfile.TemporaryDirectory(prefix="trace-a03-", dir=native_tmp) as data:
            with patch.dict(os.environ, {"PLUGIN_DATA": data}, clear=False):
                session = "trace-a03-production"
                self._capture_hook(
                    HOOK.user_prompt_submit,
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "session_id": session,
                        "hook_run_id": "old-objective",
                        "prompt": "Implement and verify the parser.",
                    },
                )
                old_objective = self._load_hook_state(session)["objective"]["fingerprint"]
                self._capture_hook(
                    HOOK.subagent_start,
                    {
                        "hook_event_name": "SubagentStart",
                        "session_id": session,
                        "hook_run_id": "agent-start",
                        "agent_id": "audit-old",
                        "agent_type": "default",
                        "task_name": "audit_01_old",
                        "prompt": "Inspect the old parser",
                    },
                )
                self._capture_hook(
                    HOOK.user_prompt_submit,
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "session_id": session,
                        "hook_run_id": "new-objective",
                        "prompt": "What is 2 + 2?",
                    },
                )
                replaced = self._load_hook_state(session)
                self.assertNotEqual(replaced["objective"]["fingerprint"], old_objective)
                self._capture_hook(
                    HOOK.subagent_stop,
                    {
                        "hook_event_name": "SubagentStop",
                        "session_id": session,
                        "hook_run_id": "agent-stop",
                        "agent_id": "audit-old",
                        "agent_type": "default",
                        "status": "completed",
                        "last_assistant_message": "old objective result",
                    },
                )
                stopped = self._load_hook_state(session)
                stop_records = [item for item in stopped["subagents"] if item["event"] == "stop"]
                self.assertTrue(stop_records[-1]["stale"])
                self.assertEqual(stop_records[-1]["objective_fingerprint"], old_objective)
                self.assertEqual(HOOK.active_agent_count(stopped), 0)

    def test_production_hook_replay_reuses_only_unchanged_success(self) -> None:
        native_tmp = "/tmp" if Path("/tmp").is_dir() else None
        with tempfile.TemporaryDirectory(prefix="trace-c02-", dir=native_tmp) as data:
            with patch.dict(os.environ, {"PLUGIN_DATA": data}, clear=False):
                session = "trace-c02-production"
                tool = {
                    "session_id": session,
                    "cwd": "/repo",
                    "tool_name": "Bash",
                    "tool_input": {"command": "python3 -m unittest tests.parser -q"},
                }
                self._capture_hook(
                    HOOK.post_tool_use,
                    {
                        **tool,
                        "hook_event_name": "PostToolUse",
                        "hook_run_id": "success",
                        "turn_id": "turn-success",
                        "tool_response": {"exit_code": 0},
                    },
                )
                self._capture_hook(
                    HOOK.compact_event,
                    {
                        "hook_event_name": "PreCompact",
                        "session_id": session,
                        "hook_run_id": "compact",
                    },
                    "pre",
                )
                self._capture_hook(
                    HOOK.session_start,
                    {
                        "hook_event_name": "SessionStart",
                        "session_id": session,
                        "hook_run_id": "resume",
                        "source": "resume",
                    },
                )
                self._capture_hook(
                    HOOK.pre_tool_use,
                    {
                        **tool,
                        "hook_event_name": "PreToolUse",
                        "hook_run_id": "same-attempt",
                        "turn_id": "turn-same",
                    },
                )
                same_state = self._load_hook_state(session)
                self.assertEqual(len(same_state["duplicate_notices"]), 1)
                same_fingerprint = HOOK.tool_fingerprint(tool)[0]
                self.assertEqual(
                    [item["fingerprint"] for item in same_state["duplicate_notices"]],
                    [same_fingerprint],
                )

                changed = {
                    **tool,
                    "tool_input": {"command": "python3 -m unittest tests.parser_changed -q"},
                }
                self.assertNotEqual(HOOK.tool_fingerprint(tool)[0], HOOK.tool_fingerprint(changed)[0])
                self._capture_hook(
                    HOOK.pre_tool_use,
                    {
                        **changed,
                        "hook_event_name": "PreToolUse",
                        "hook_run_id": "changed-attempt",
                        "turn_id": "turn-changed",
                    },
                )
                changed_state = self._load_hook_state(session)
                self.assertEqual(len(changed_state["duplicate_notices"]), 1)
                self.assertEqual(len(changed_state["operations"]), 1)
                changed_fingerprint = HOOK.tool_fingerprint(changed)[0]
                self.assertNotIn(
                    changed_fingerprint,
                    {item["fingerprint"] for item in changed_state["duplicate_notices"]},
                )



if __name__ == "__main__":
    unittest.main()
