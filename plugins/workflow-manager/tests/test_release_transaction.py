from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "release_transaction.py"
SPEC = importlib.util.spec_from_file_location("release_transaction", SCRIPT)
assert SPEC and SPEC.loader
TX = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TX)

BOUND = {
    "task_epoch_id": "a" * 32,
    "execution_contract_id": "b" * 32,
}


class ScriptedRunner:
    def __init__(self) -> None:
        self.query_results: dict[str, dict] = {}
        self.run_results: dict[str, object] = {}
        self.query_calls: list[str] = []
        self.run_calls: list[str] = []

    def query(self, stage: str, context: dict) -> dict:
        self.query_calls.append(stage)
        result = self.query_results.get(stage)
        return dict(result) if result is not None else {"status": "absent", "stage": stage}

    def run(self, stage: str, context: dict) -> dict:
        self.run_calls.append(stage)
        result = self.run_results.get(stage)
        if isinstance(result, BaseException):
            raise result
        if isinstance(result, dict):
            return dict(result)
        return TX.make_evidence(context, stage, subject=f"{stage}-runner")


class ReleaseTransactionTests(unittest.TestCase):
    def test_typed_evidence_checkpoint_and_default_external_refusal(self) -> None:
        state = TX._empty("1.0.57", binding=BOUND)
        runner = ScriptedRunner()
        self.assertEqual(TX.resume(state), "preflight")
        self.assertEqual(TX.advance(state, runner), {"action": "runner_complete", "stage": "preflight"})
        self.assertEqual(TX.resume(state), "prepare")
        with self.assertRaises(ValueError):
            TX.complete(state, "prepare", "CLI said success")
        self.assertEqual(TX.advance(state, runner)["stage"], "prepare")
        self.assertEqual(TX.advance(state, runner)["stage"], "test")
        self.assertEqual(TX.resume(state), "commit_push")
        with self.assertRaises(PermissionError):
            TX.advance(state, runner)
        self.assertEqual(runner.run_calls, ["preflight", "prepare", "test"])

    def test_query_before_repeat_accepts_only_bound_receipt(self) -> None:
        state = TX._empty(
            "1.0.57",
            binding=BOUND,
        )
        runner = ScriptedRunner()
        for stage in ("preflight", "prepare", "test"):
            TX.advance(state, runner)
            self.assertIn(stage, runner.query_calls)
        evidence = TX.make_evidence(state, "commit_push", subject="remote-commit-observed")
        runner.query_results["commit_push"] = {
            "status": "completed", "stage": "commit_push", "evidence": evidence,
        }
        result = TX.advance(state, runner)
        self.assertEqual(result, {"action": "query_complete", "stage": "commit_push"})
        self.assertNotIn("commit_push", runner.run_calls)
        self.assertEqual(state["completed"][-1]["source"], "query")
        self.assertEqual(state["completed"][-1]["evidence"]["binding"], state["binding"])

    def test_all_release_stages_resume_from_typed_query_checkpoints(self) -> None:
        self.assertEqual(
            TX.STAGES,
            (
                "preflight", "prepare", "test", "commit_push", "ci",
                "tag_release", "marketplace", "install_doctor_smoke", "final_seal",
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "transaction.json"
            runner = ScriptedRunner()
            for stage in TX.STAGES:
                state = TX._load(path, "1.0.57") if path.exists() else TX._empty("1.0.57", binding=BOUND)
                runner.query_results[stage] = {
                    "status": "completed", "stage": stage,
                    "evidence": TX.make_evidence(state, stage, subject=f"{stage}-query"),
                }
                state, result = TX.run_next(path, "1.0.57", runner, binding=BOUND)
                self.assertEqual(result, {"action": "query_complete", "stage": stage})
            self.assertIsNone(TX.resume(state))
            self.assertEqual([item["stage"] for item in state["completed"]], list(TX.STAGES))
            self.assertEqual(runner.run_calls, [])

    def test_persisted_failure_injection_and_resume_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "transaction.json"
            runner = ScriptedRunner()
            state, result = TX.run_next(path, "1.0.57", runner, binding=BOUND)
            self.assertEqual(result, {"action": "runner_complete", "stage": "preflight"})
            self.assertEqual(TX.resume(state), "prepare")
            state, result = TX.run_next(
                path, "1.0.57", runner,
                failure_injection={"prepare": "fault-injected"}, binding=BOUND,
            )
            self.assertEqual(result, {"action": "injected_failure", "stage": "prepare"})
            self.assertEqual(TX.resume(state), "prepare")
            self.assertEqual(state["failures"][-1]["code"], "fault-injected")
            resumed, result = TX.run_next(path, "1.0.57", runner, binding=BOUND)
            self.assertEqual(result, {"action": "runner_complete", "stage": "prepare"})
            self.assertEqual(TX.resume(resumed), "test")
            self.assertGreaterEqual(runner.query_calls.count("prepare"), 2)
            self.assertEqual(TX._load(path, "1.0.57"), resumed)

    def test_inflight_external_stage_never_duplicates_after_crash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "transaction.json"
            runner = ScriptedRunner()
            for _ in range(3):
                TX.run_next(path, "1.0.57", runner, binding=BOUND)
            runner.run_results["commit_push"] = RuntimeError("simulated crash")
            with self.assertRaises(RuntimeError):
                TX.run_next(path, "1.0.57", runner, allow_external=True, binding=BOUND)
            interrupted = TX._load(path, "1.0.57")
            self.assertEqual(interrupted["inflight"]["stage"], "commit_push")
            calls = runner.run_calls.count("commit_push")
            with self.assertRaises(TX.ExternalActionUncertain):
                TX.run_next(path, "1.0.57", runner, allow_external=True, binding=BOUND)
            self.assertEqual(runner.run_calls.count("commit_push"), calls)
            # An inconclusive query is itself fail-closed.  It is persisted as
            # a diagnostic but cannot clear the external inflight guard and
            # make a second commit/push call eligible.
            runner.query_results["commit_push"] = {"status": "unknown", "stage": "commit_push"}
            unknown, result = TX.run_next(path, "1.0.57", runner, allow_external=True, binding=BOUND)
            self.assertEqual(result, {"action": "query_unknown", "stage": "commit_push"})
            self.assertEqual(unknown["inflight"]["stage"], "commit_push")
            runner.query_results["commit_push"] = {"status": "absent", "stage": "commit_push"}
            with self.assertRaises(TX.ExternalActionUncertain):
                TX.run_next(path, "1.0.57", runner, allow_external=True, binding=BOUND)
            self.assertEqual(runner.run_calls.count("commit_push"), calls)
            evidence = TX.make_evidence(interrupted, "commit_push", subject="remote-query-after-crash")
            runner.query_results["commit_push"] = {
                "status": "completed", "stage": "commit_push", "evidence": evidence,
            }
            recovered, result = TX.run_next(path, "1.0.57", runner, binding=BOUND)
            self.assertEqual(result, {"action": "query_complete", "stage": "commit_push"})
            self.assertIsNone(recovered["inflight"])

    def test_external_runner_failure_remains_query_only_until_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "transaction.json"
            runner = ScriptedRunner()
            for _ in range(3):
                TX.run_next(path, "1.0.57", runner, binding=BOUND)
            runner.run_results["commit_push"] = {
                "status": "failed", "failure_code": "remote-timeout",
            }
            failed, result = TX.run_next(
                path, "1.0.57", runner, allow_external=True, binding=BOUND
            )
            self.assertEqual(result, {"action": "runner_failure", "stage": "commit_push"})
            self.assertEqual(failed["inflight"]["stage"], "commit_push")
            calls = runner.run_calls.count("commit_push")
            with self.assertRaises(TX.ExternalActionUncertain):
                TX.run_next(path, "1.0.57", runner, allow_external=True, binding=BOUND)
            self.assertEqual(runner.run_calls.count("commit_push"), calls)

    def test_state_rejects_untyped_or_binding_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "transaction.json"
            state = TX._empty("1.0.57", binding=BOUND)
            state["completed"].append({"stage": "preflight", "evidence_digest": "a" * 32})
            TX._store(path, state)
            with self.assertRaises(ValueError):
                TX._load(path, "1.0.57")
            state = TX._empty("1.0.57", binding=BOUND)
            TX._store(path, state)
            with self.assertRaises(ValueError):
                TX.run_next(
                    path, "1.0.57", ScriptedRunner(),
                    binding={"task_epoch_id": "c" * 32, "execution_contract_id": "d" * 32},
                )

    def test_runner_requires_task_epoch_contract_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                TX.run_next(
                    Path(temporary) / "transaction.json", "1.0.57", ScriptedRunner()
                )


if __name__ == "__main__":
    unittest.main()
