from __future__ import annotations

import json
import unittest
from pathlib import Path


FIXTURE_DIR = Path(__file__).with_name("fixtures")


def context_metrics(events: list[dict[str, str]], field: str) -> dict[str, int]:
    messages = [event[field] for event in events if event.get(field)]
    return {
        "messages": len(messages),
        "utf8_bytes": sum(len(message.encode("utf-8")) for message in messages),
    }


def trace_metrics(events: list[dict[str, str]]) -> dict[str, int]:
    return {
        "child_starts": sum(event.get("kind") == "child_start" for event in events),
        "additional_context_bytes": sum(
            len(event.get("additional_context", "").encode("utf-8")) for event in events
        ),
        # The frozen trace has no artifact/test payload. The narrow observable
        # proxy for preserved strong acceptance is the number of bound Hard
        # executor checkpoints; it deliberately makes no runtime-token claim.
        "strong_acceptance_checkpoints": sum(
            event.get("kind") == "child_start"
            and event.get("case") == "hard"
            and event.get("role") == "executor"
            for event in events
        ),
    }


class AndroidNativeDemoAggregateAuditTests(unittest.TestCase):
    def test_fixture_is_anonymous_and_does_not_claim_marginal_tokens(self) -> None:
        fixture = json.loads(
            (FIXTURE_DIR / "androidnativedemo_workflow_metrics.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(fixture["fixture_version"], 1)
        self.assertIn("cannot be attributed", fixture["measurement_boundary"])
        self.assertIn("no session id", fixture["privacy"])
        self.assertGreater(fixture["workflow_manager_developer_messages"], 0)
        self.assertGreater(fixture["oversized_notices"], fixture["task_cycles"])
        self.assertGreater(fixture["cumulative_total_tokens"], 0)
        self.assertNotIn("session_id", fixture)
        self.assertNotIn("rollout_path", fixture)


class WorkflowOverheadABTests(unittest.TestCase):
    def test_v1043_frozen_trace_meets_current_production_ab_thresholds(self) -> None:
        fixture = json.loads(
            (FIXTURE_DIR / "v1043_frozen_trace.json").read_text(encoding="utf-8")
        )
        self.assertEqual(fixture["fixture_version"], 1)
        self.assertEqual(fixture["writer_version"], "1.0.43")
        self.assertIn("Frozen compatibility trace", fixture["boundary"])
        self.assertIn("not real token usage", fixture["boundary"])

        old = trace_metrics(fixture["old_events"])
        current = trace_metrics(fixture["new_events"])
        child_reduction = 1 - current["child_starts"] / old["child_starts"]
        context_reduction = 1 - (
            current["additional_context_bytes"] / old["additional_context_bytes"]
        )

        self.assertGreaterEqual(child_reduction, 0.60)
        self.assertGreaterEqual(context_reduction, 0.50)
        self.assertGreaterEqual(
            current["strong_acceptance_checkpoints"],
            old["strong_acceptance_checkpoints"],
        )
        self.assertEqual(
            sum(event.get("case") == "simple" for event in fixture["new_events"]), 0
        )

    def test_lean_policy_removes_native_duplicates_without_lowering_evidence(self) -> None:
        fixture = json.loads(
            (FIXTURE_DIR / "workflow_overhead_ab.json").read_text(encoding="utf-8")
        )
        self.assertEqual(fixture["fixture_version"], 2)
        old = context_metrics(fixture["events"], "v1045_context")
        lean = context_metrics(fixture["events"], "v1046_context")
        self.assertLess(lean["messages"], old["messages"])
        self.assertLessEqual(lean["utf8_bytes"], old["utf8_bytes"] * 0.5)

        arms = fixture["arms"]
        evidence = arms["native_codex"]["correctness_evidence"]
        self.assertEqual(arms["v1.0.45"]["correctness_evidence"], evidence)
        self.assertEqual(arms["v1.0.46"]["correctness_evidence"], evidence)
        self.assertEqual(arms["v1.0.46"]["workflow_confirmations"], 1)
        self.assertLess(arms["v1.0.46"]["child_starts"], arms["v1.0.45"]["child_starts"])
        self.assertEqual(arms["v1.0.46"]["tool_attempts"], arms["native_codex"]["tool_attempts"])

    def test_native_process_is_not_duplicated_as_plugin_context(self) -> None:
        fixture = json.loads(
            (FIXTURE_DIR / "workflow_overhead_ab.json").read_text(encoding="utf-8")
        )
        for event in fixture["events"]:
            self.assertTrue(event["native_process"])
            if event["id"] in {"bounded_read", "reproduction"}:
                self.assertEqual(event["v1046_context"], "")

    def test_simulation_does_not_present_estimated_tokens_as_runtime_measurement(self) -> None:
        fixture = json.loads(
            (FIXTURE_DIR / "workflow_overhead_ab.json").read_text(encoding="utf-8")
        )
        serialized = json.dumps(fixture, ensure_ascii=False).lower()
        self.assertNotIn("estimated_tokens", serialized)
        self.assertNotIn("real_tokens", serialized)
        self.assertIn("not measured or estimated", fixture["measurement_boundary"])


if __name__ == "__main__":
    unittest.main()
