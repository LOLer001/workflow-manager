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
    def test_optimized_policy_removes_repeated_context_without_lowering_evidence(self) -> None:
        fixture = json.loads(
            (FIXTURE_DIR / "workflow_overhead_ab.json").read_text(encoding="utf-8")
        )
        old = context_metrics(fixture["events"], "v1044")
        optimized = context_metrics(fixture["events"], "optimized")
        self.assertLessEqual(optimized["messages"], old["messages"] * 0.4)
        self.assertLessEqual(optimized["utf8_bytes"], old["utf8_bytes"] * 0.4)

        arms = fixture["arms"]
        evidence = arms["no_workflow_manager"]["correctness_evidence"]
        self.assertEqual(arms["v1.0.44"]["correctness_evidence"], evidence)
        self.assertEqual(arms["optimized"]["correctness_evidence"], evidence)
        self.assertEqual(arms["optimized"]["workflow_confirmations"], 1)
        self.assertLess(arms["optimized"]["child_starts"], arms["v1.0.44"]["child_starts"])
        self.assertLessEqual(
            arms["optimized"]["tool_attempts"],
            arms["no_workflow_manager"]["tool_attempts"],
        )

    def test_simulation_does_not_present_estimated_tokens_as_runtime_measurement(self) -> None:
        fixture = json.loads(
            (FIXTURE_DIR / "workflow_overhead_ab.json").read_text(encoding="utf-8")
        )
        serialized = json.dumps(fixture, ensure_ascii=False).lower()
        self.assertNotIn("estimated_tokens", serialized)
        self.assertNotIn("real_tokens", serialized)


if __name__ == "__main__":
    unittest.main()
