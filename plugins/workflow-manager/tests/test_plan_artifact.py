from __future__ import annotations

import ctypes
import errno
import importlib.util
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PLUGIN_ROOT / "scripts" / "orchestrator_hook.py"
SPEC = importlib.util.spec_from_file_location("plan_artifact_hook", SCRIPT)
assert SPEC and SPEC.loader
HOOK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HOOK)


class PlanArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        native_tmp = "/tmp" if Path("/tmp").is_dir() else None
        self.temporary = tempfile.TemporaryDirectory(prefix="workflow-plan-artifact-", dir=native_tmp)
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.codex_home = self.root / ".codex"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def symlink_or_skip(
        self,
        link: Path,
        target: Path,
        *,
        target_is_directory: bool = False,
    ) -> None:
        try:
            link.symlink_to(target, target_is_directory=target_is_directory)
        except OSError as error:
            if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows symlink privilege is unavailable")
            raise

    def run_hook(
        self,
        payload: dict,
        *,
        data: Path | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PLUGIN_DATA"] = str(data or self.data)
        env["CODEX_HOME"] = str(self.codex_home)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [sys.executable, str(SCRIPT)],
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            capture_output=True,
            env=env,
            timeout=10,
        )

    def state_path(self, data: Path | None = None) -> Path:
        files = list(((data or self.data) / "sessions").glob("*.json"))
        self.assertEqual(len(files), 1, files)
        return files[0]

    def state(self, data: Path | None = None) -> dict:
        return json.loads(self.state_path(data).read_text(encoding="utf-8"))

    def artifact_path(self, state: dict, data: Path | None = None) -> Path:
        relative = PurePosixPath(state["plan_artifact"]["relative_path"])
        self.assertFalse(relative.is_absolute())
        self.assertNotIn("..", relative.parts)
        return (data or self.data).joinpath(*relative.parts)

    @staticmethod
    def hard_body(label: str = "one", extra: str = "") -> str:
        return (
            f"1. 收集 {label} 日志并锁定 Settings 根因\n"
            "2. 修改 framework 与 SystemUI 的绑定实现\n"
            "3. 编译、回归并按 Unity 参考验收\n"
            f"验收：{label} 功能、回归和视觉证据全部通过。{extra}\n"
            "```workflow-manager-execution-slices\n"
            '{"version":1,"global_constraints":["preserve acceptance"],"slices":[{"id":"s01","title":"bounded repair","scope":["owned module"],"acceptance":["targeted verification"],"rollback":["revert bounded change"],"stop_conditions":["typed blocker"],"expected_artifacts":["verification log"]}]}\n'
            "```\n"
        )

    @staticmethod
    def legacy_document(
        generation: int,
        plan_digest: str,
        body: str,
        *,
        objective: str = "a" * 16,
        difficulty: str = "b" * 24,
    ) -> bytes:
        content_digest = HOOK.stable_hash(body, 32)
        return (
            f"{HOOK.LEGACY_PLAN_ARTIFACT_OWNER}\n"
            f"generation: {generation}\n"
            f"plan_digest: {plan_digest}\n"
            f"content_digest: {content_digest}\n"
            f"objective_fingerprint: {objective}\n"
            f"difficulty_decision_id: {difficulty}\n"
            "-->\n# Workflow Manager Hard Plan\n\n"
            "> This Markdown file is a private review mirror. The bound state plan_digest remains authoritative.\n\n"
            f"{HOOK.PLAN_ARTIFACT_BODY_MARKER}\n{body}"
        ).encode("utf-8")

    def begin_assessor(self, session: str, *, data: Path | None = None, suffix: str = "one") -> tuple[str, str]:
        selected = data or self.data
        started = self.run_hook(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": session,
                "hook_run_id": f"{suffix}-objective",
                "prompt": "修复 Android 设置与 SystemUI 跨模块故障，编译部署并完成 Unity 参考对齐验收",
            },
            data=selected,
        )
        self.assertEqual(started.returncode, 0, started.stderr)
        state = self.state(selected)
        binding = state["assessor_binding_id"]
        request = (
            f"assessor_binding_id={binding} objective_fingerprint={state['objective']['fingerprint']} "
            "profile_resolution=highest_available assess Simple directly solve and verify; "
            "Hard read-only plan then confirmation"
        )
        accepted = self.run_hook(
            {
                "hook_event_name": "PreToolUse",
                "session_id": session,
                "hook_run_id": f"{suffix}-request",
                "tool_name": "collaboration.spawn_agent",
                "tool_input": {
                    "task_name": "high_assessor",
                    "message": request,
                    "model": "gpt-5.6-sol",
                    "reasoning_effort": "max",
                    "fork_turns": "1",
                },
            },
            data=selected,
        )
        self.assertNotIn(
            "permissionDecision",
            json.loads(accepted.stdout or "{}").get("hookSpecificOutput", {}),
        )
        self.run_hook(
            {
                "hook_event_name": "PostToolUse",
                "session_id": session,
                "hook_run_id": f"{suffix}-request-post",
                "tool_name": "collaboration.spawn_agent",
                "tool_input": {
                    "task_name": "high_assessor", "message": request,
                    "model": "gpt-5.6-sol", "reasoning_effort": "max", "fork_turns": "1",
                },
                "tool_response": {"status": "ok"},
            },
            data=selected,
        )
        transcript = selected.parent / f"{suffix}-start.jsonl"
        transcript.write_text(json.dumps({"type": "turn_context", "payload": {"turn_id": f"{suffix}-turn", "model": "gpt-5.6-sol", "effort": "max"}}) + "\n", encoding="utf-8")
        agent_id = f"{suffix}-assessor"
        self.run_hook(
            {
                "hook_event_name": "SubagentStart",
                "session_id": session,
                "hook_run_id": f"{suffix}-start",
                "turn_id": f"{suffix}-turn",
                "agent_id": agent_id,
                "model": "gpt-5.6-sol",
                "transcript_path": str(transcript),
            },
            data=selected,
        )
        return binding, agent_id

    def assessor_result(self, binding: str, body: str) -> str:
        return (
            body
            + f"WORK_ASSESSMENT binding_id={binding} outcome=hard evidence_digest={'a' * 32}\n"
            + "计划已就绪，等待确认后执行"
        )

    def accept_assessor_plan(self, session: str, *, data: Path | None = None, body: str | None = None) -> tuple[dict, str]:
        selected = data or self.data
        binding, agent_id = self.begin_assessor(session, data=selected)
        message = self.assessor_result(binding, body or self.hard_body())
        result = self.run_hook(
            {
                "hook_event_name": "SubagentStop",
                "session_id": session,
                "hook_run_id": "assessor-stop",
                "agent_id": agent_id,
                "status": "completed",
                "last_assistant_message": message,
            },
            data=selected,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.state(selected), message

    def begin_parent_plan(self, session: str, *, data: Path | None = None) -> None:
        self.run_hook(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": session,
                "hook_run_id": "parent-objective",
                "prompt": "排查 Android 未知根因的跨 Settings/framework 故障并编译验证，但不要使用任何子智能体",
            },
            data=data,
        )

    def accept_parent_plan(self, session: str, body: str, *, data: Path | None = None, run_id: str = "parent-stop") -> dict:
        result = self.run_hook(
            {
                "hook_event_name": "Stop",
                "session_id": session,
                "hook_run_id": run_id,
                "last_assistant_message": body + "计划已就绪，等待确认后执行",
            },
            data=data,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return self.state(data)

    def test_m01_assessor_hard_plan_creates_bound_markdown(self) -> None:
        state, _ = self.accept_assessor_plan("m01")
        artifact = state["plan_artifact"]
        self.assertEqual((artifact["lifecycle_status"], artifact["write_status"]), ("ready", "written"))
        self.assertEqual(artifact["plan_digest"], state["plan_digest"])
        self.assertEqual(artifact["generation"], state["plan_generation"])
        path = self.artifact_path(state)
        self.assertTrue(path.is_file())
        self.assertEqual(path.name, HOOK.PLAN_JOURNAL_NAME)

    def test_c01_hard_plan_uses_one_bound_v2_canonical_journal(self) -> None:
        state, _ = self.accept_assessor_plan("c01")
        artifact = state["plan_artifact"]
        path = self.artifact_path(state)

        self.assertEqual(path.name, "hard-plan.md")
        self.assertEqual(artifact["relative_path"], f"plans/{path.parent.name}/hard-plan.md")
        self.assertEqual(artifact["current_revision_digest"], state["plan_digest"])
        self.assertEqual(artifact["revision_count"], 1)
        self.assertRegex(artifact["journal_digest"], r"^[0-9a-f]{32}$")
        document = path.read_text(encoding="utf-8")
        self.assertTrue(document.startswith("<!-- workflow-manager-plan-journal:v2\n"))
        self.assertEqual(document.count("<!-- workflow-manager-plan-revision:v2\n"), 1)

    def test_c02_artifact_write_failure_cannot_enter_or_confirm_pending_plan(self) -> None:
        self.data.mkdir(parents=True)
        (self.data / "plans").write_text("block plans directory", encoding="utf-8")
        binding, agent_id = self.begin_assessor("c02")
        failed_output = self.run_hook(
            {
                "hook_event_name": "SubagentStop",
                "session_id": "c02",
                "hook_run_id": "failed-plan",
                "agent_id": agent_id,
                "status": "completed",
                "last_assistant_message": self.assessor_result(
                    binding, self.hard_body("must-not-confirm")
                ),
            }
        )
        self.assertEqual(failed_output.returncode, 0, failed_output.stderr)
        failed = self.state()
        self.assertNotEqual(failed["plan_state"], "awaiting_confirmation")
        self.assertEqual(failed["plan_artifact"]["write_status"], "write_failed")

        confirmation = self.run_hook(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "c02",
                "hook_run_id": "cannot-confirm",
                "prompt": "确认按这个计划执行",
            }
        )
        self.assertEqual(confirmation.returncode, 0, confirmation.stderr)
        after = self.state()
        self.assertNotEqual(after["plan_state"], "confirmed")
        self.assertIsNone(after["confirmed_plan_digest"])
        self.assertIsNone(after["execution_contract_id"])

    def test_c03_replans_append_complete_revisions_to_the_same_journal(self) -> None:
        session = "c03"
        self.begin_parent_plan(session)
        first = self.accept_parent_plan(session, self.hard_body("first"), run_id="first-plan")
        path = self.artifact_path(first)
        first_bytes = path.read_bytes()

        self.run_hook(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": session,
                "hook_run_id": "request-replan",
                "prompt": "修改计划：保留原验收并增加 Windows 原生事务恢复验证",
            }
        )
        second = self.accept_parent_plan(
            session, self.hard_body("second"), run_id="second-plan"
        )
        self.assertEqual(self.artifact_path(second), path)
        self.assertEqual(second["plan_generation"], 2)
        self.assertEqual(second["plan_artifact"]["revision_count"], 2)
        document = path.read_bytes()
        self.assertNotEqual(document, first_bytes)
        self.assertIn(b"first", document)
        self.assertIn(b"second", document)
        self.assertEqual(
            document.count(b"<!-- workflow-manager-plan-revision:v2\n"), 2
        )

    def test_c04_external_edit_invalidates_confirmed_executor_before_mutation(self) -> None:
        session = "c04"
        self.begin_parent_plan(session)
        ready = self.accept_parent_plan(session, self.hard_body("bound"))
        path = self.artifact_path(ready)
        self.run_hook(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": session,
                "hook_run_id": "confirm",
                "prompt": "确认按这个计划执行",
            }
        )
        self.assertEqual(self.state()["plan_state"], "confirmed")
        path.write_bytes(path.read_bytes() + b"\nexternal edit\n")

        denied = self.run_hook(
            {
                "hook_event_name": "PreToolUse",
                "session_id": session,
                "hook_run_id": "mutate-after-drift",
                "tool_name": "apply_patch",
                "tool_input": {"patch": "*** Begin Patch\n*** End Patch"},
            }
        )
        self.assertEqual(denied.returncode, 0, denied.stderr)
        decision = json.loads(denied.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        drifted = self.state()
        self.assertEqual(drifted["plan_state"], "invalidated")
        self.assertIsNone(drifted["confirmed_plan_digest"])
        self.assertEqual(drifted["executor_failure_kind"], "stale_contract")

    def test_c05_revision_capacity_is_exact_and_never_truncates(self) -> None:
        self.assertEqual(HOOK.MAX_PLAN_REVISION_BYTES, 960 * 1024)
        self.assertEqual(HOOK.MAX_PLAN_JOURNAL_BYTES, 10 * 1024 * 1024)
        exact = "x" * (HOOK.MAX_PLAN_REVISION_BYTES - 1)
        sanitized = HOOK.sanitize_plan_artifact_body(exact)
        self.assertEqual(len(sanitized.encode("utf-8")), HOOK.MAX_PLAN_REVISION_BYTES)
        self.assertNotIn("truncated", sanitized)
        with self.assertRaises(HOOK.PlanArtifactError) as raised:
            HOOK.sanitize_plan_artifact_body(exact + "x")
        self.assertEqual(raised.exception.code, "revision_too_large")

    def test_c06_hard_update_plan_cannot_create_split_brain_plan_storage(self) -> None:
        session = "c06"
        self.begin_parent_plan(session)
        self.accept_parent_plan(session, self.hard_body("canonical"))
        result = self.run_hook(
            {
                "hook_event_name": "PreToolUse",
                "session_id": session,
                "hook_run_id": "split-update-plan",
                "tool_name": "update_plan",
                "tool_input": {
                    "plan": [{"step": "different unbound plan", "status": "in_progress"}]
                },
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        decision = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("canonical", decision["permissionDecisionReason"])

        state = self.state()
        projection = self.run_hook(
            {
                "hook_event_name": "PreToolUse",
                "session_id": session,
                "hook_run_id": "bound-update-plan-projection",
                "tool_name": "update_plan",
                "tool_input": {
                    "explanation": (
                        "projection_only canonical_revision_digest="
                        f"{state['plan_artifact']['current_revision_digest']}"
                    ),
                    "plan": [
                        {
                            "step": "收集 canonical 日志并锁定 Settings 根因",
                            "status": "in_progress",
                        }
                    ],
                },
            }
        )
        self.assertEqual(projection.returncode, 0, projection.stderr)
        self.assertEqual(projection.stdout, "")

    def test_c13_plan_details_and_resume_point_to_canonical_current_revision(self) -> None:
        session = "c13"
        self.begin_parent_plan(session)
        sentinel = "canonical-view-body-sentinel-42"
        ready = self.accept_parent_plan(session, self.hard_body(sentinel))
        artifact = ready["plan_artifact"]
        details = self.run_hook(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": session,
                "hook_run_id": "show-details",
                "prompt": "查看计划详情",
            }
        )
        self.assertIn(artifact["relative_path"], details.stdout)
        self.assertIn(artifact["current_revision_digest"], details.stdout)
        self.assertIn(sentinel, details.stdout)
        environment = patch.dict(
            os.environ,
            {"PLUGIN_DATA": str(self.data), "CODEX_HOME": str(self.codex_home)},
        )
        environment.start()
        self.addCleanup(environment.stop)
        current = HOOK.read_current_plan_revision(self.state(), {"session_id": session})
        self.assertIn(sentinel, current)
        resumed = self.run_hook(
            {
                "hook_event_name": "SessionStart",
                "session_id": session,
                "hook_run_id": "resume-details",
                "source": "resume",
            }
        )
        self.assertIn("Canonical Hard-plan semantics", resumed.stdout)
        self.assertIn(artifact["relative_path"], resumed.stdout)
        self.assertIn(sentinel, resumed.stdout)

    def test_c14_impossible_new_state_old_journal_combination_fails_closed(self) -> None:
        session = "c14"
        self.begin_parent_plan(session)
        stable = self.accept_parent_plan(session, self.hard_body("old"))
        payload = {"hook_event_name": "SessionStart", "session_id": session}
        environment = patch.dict(
            os.environ,
            {"PLUGIN_DATA": str(self.data), "CODEX_HOME": str(self.codex_home)},
        )
        environment.start()
        self.addCleanup(environment.stop)
        state = self.state()
        state["plan_state"] = "analyzing"
        state["plan_digest"] = None
        state["plan_objective_fingerprint"] = None
        state["plan_difficulty_decision_id"] = None
        state["_defer_plan_transaction"] = True
        self.assertTrue(
            HOOK.write_plan_artifact(state, payload, self.hard_body("new"))
        )
        state.pop("_defer_plan_transaction", None)
        pending = state.pop("_plan_transaction")
        state["plan_state"] = "awaiting_confirmation"
        HOOK.sync_plan_artifact_lifecycle(state)
        HOOK.atomic_write(self.state_path(), state)
        HOOK._rollback_plan_write(pending["transaction"])
        pending["guard_context"].__exit__(None, None, None)

        denied = self.run_hook(
            {
                "hook_event_name": "PreToolUse",
                "session_id": session,
                "hook_run_id": "impossible-combination",
                "tool_name": "apply_patch",
                "tool_input": {"patch": "*** Begin Patch\n*** End Patch"},
            }
        )
        decision = json.loads(denied.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        observed = self.state()
        self.assertEqual(observed["plan_state"], "invalidated")
        self.assertEqual(
            observed["plan_artifact"]["write_status"],
            "transaction_recovery_failed",
        )
        self.assertIsNone(observed["execution_contract_id"])
        self.assertTrue(
            (self.artifact_path(stable).parent / HOOK.PLAN_TRANSACTION_MARKER_NAME).exists()
        )

    def test_c15_state_replace_then_failure_leaves_recoverable_new_new_transaction(self) -> None:
        session = "c15"
        self.begin_parent_plan(session)
        stable = self.accept_parent_plan(session, self.hard_body("old"))
        journal = self.artifact_path(stable)
        old_bytes = journal.read_bytes()
        payload = {
            "hook_event_name": "Stop",
            "session_id": session,
            "hook_run_id": "replace-then-fail",
        }
        environment = patch.dict(
            os.environ,
            {"PLUGIN_DATA": str(self.data), "CODEX_HOME": str(self.codex_home)},
        )
        environment.start()
        self.addCleanup(environment.stop)

        def append_revision(state: dict) -> None:
            state["plan_state"] = "analyzing"
            state["plan_digest"] = None
            state["plan_objective_fingerprint"] = None
            state["plan_difficulty_decision_id"] = None
            self.assertTrue(
                HOOK.write_plan_artifact(state, payload, self.hard_body("new"))
            )
            state["plan_state"] = "awaiting_confirmation"

        real_atomic_write = HOOK.atomic_write

        def committed_then_failed(path: Path, state: dict) -> None:
            real_atomic_write(path, state)
            raise OSError("simulated failure after state replace")

        with patch.object(HOOK, "atomic_write", side_effect=committed_then_failed):
            _, changed = HOOK.mutate_state(payload, append_revision)
        self.assertFalse(changed)
        persisted = self.state()
        self.assertEqual(persisted["plan_generation"], 2)
        self.assertNotEqual(journal.read_bytes(), old_bytes)
        marker = journal.parent / HOOK.PLAN_TRANSACTION_MARKER_NAME
        self.assertTrue(marker.exists())
        self.assertEqual(len(list(journal.parent.glob(".*backup*"))), 1)

        recovered = HOOK.snapshot_state(payload)
        self.assertEqual(recovered["plan_generation"], 2)
        self.assertEqual(recovered["plan_artifact"]["write_status"], "written")
        self.assertFalse(marker.exists())
        self.assertEqual(list(journal.parent.glob(".*backup*")), [])

    def test_c07_journal_capacity_exact_boundary_and_plus_one_rejection(self) -> None:
        session = HOOK.plan_artifact_session_id("c07")
        objective = "a" * 16
        difficulty = "b" * 24
        timestamp = "2026-08-20T00:00:00+00:00"
        document: bytes | None = None
        generation = 0
        while document is None or (
            HOOK.MAX_PLAN_JOURNAL_BYTES - len(document)
            > HOOK.MAX_PLAN_REVISION_BYTES + 512
        ):
            generation += 1
            body = "x" * (900 * 1024 - 1) + "\n"
            document, _ = HOOK.append_plan_journal_revision(
                document,
                session=session,
                generation=generation,
                body=body,
                objective_fingerprint=objective,
                difficulty_decision_id=difficulty,
                created_at=timestamp,
            )

        remaining = HOOK.MAX_PLAN_JOURNAL_BYTES - len(document)
        target_body_bytes = min(HOOK.MAX_PLAN_REVISION_BYTES, remaining - 256)
        exact_document: bytes | None = None
        for _ in range(32):
            body = "y" * (target_body_bytes - 1) + "\n"
            try:
                candidate, parsed = HOOK.append_plan_journal_revision(
                    document,
                    session=session,
                    generation=generation + 1,
                    body=body,
                    objective_fingerprint=objective,
                    difficulty_decision_id=difficulty,
                    created_at=timestamp,
                )
            except HOOK.PlanArtifactError as error:
                self.assertEqual(error.code, "journal_full")
                target_body_bytes -= 1
                continue
            delta = HOOK.MAX_PLAN_JOURNAL_BYTES - len(candidate)
            if delta == 0:
                exact_document = candidate
                break
            target_body_bytes += delta
        self.assertIsNotNone(exact_document)
        assert exact_document is not None
        self.assertEqual(len(exact_document), HOOK.MAX_PLAN_JOURNAL_BYTES)
        self.assertEqual(
            HOOK.parse_plan_journal(exact_document)["generation"], generation + 1
        )
        with self.assertRaises(HOOK.PlanArtifactError) as raised:
            HOOK.append_plan_journal_revision(
                exact_document,
                session=session,
                generation=generation + 2,
                body="z\n",
                objective_fingerprint=objective,
                difficulty_decision_id=difficulty,
                created_at=timestamp,
            )
        self.assertEqual(raised.exception.code, "journal_full")

    def test_c08_oversized_revision_preserves_existing_journal_byte_for_byte(self) -> None:
        session = "c08"
        self.begin_parent_plan(session)
        persisted = self.accept_parent_plan(session, self.hard_body("old"))
        journal = self.artifact_path(persisted)
        before = journal.read_bytes()
        generation = persisted["plan_generation"]
        candidate = json.loads(json.dumps(persisted))
        candidate["plan_state"] = "analyzing"
        candidate["plan_digest"] = None
        candidate["plan_objective_fingerprint"] = None
        candidate["plan_difficulty_decision_id"] = None
        environment = patch.dict(
            os.environ,
            {"PLUGIN_DATA": str(self.data), "CODEX_HOME": str(self.codex_home)},
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.assertFalse(
            HOOK.write_plan_artifact(
                candidate,
                {"session_id": session},
                "x" * HOOK.MAX_PLAN_REVISION_BYTES,
            )
        )
        self.assertEqual(
            candidate["plan_artifact"]["warning_code"], "revision_too_large"
        )
        self.assertEqual(candidate["plan_generation"], generation)
        self.assertEqual(journal.read_bytes(), before)

    def test_c09_state_write_failure_rolls_back_first_and_later_revision(self) -> None:
        session = "c09"
        payload = {
            "hook_event_name": "Stop",
            "session_id": session,
            "hook_run_id": "first",
        }
        environment = patch.dict(
            os.environ,
            {"PLUGIN_DATA": str(self.data), "CODEX_HOME": str(self.codex_home)},
        )
        environment.start()
        self.addCleanup(environment.stop)

        def first_change(state: dict) -> None:
            state.update(
                {
                    "task_domain": "work",
                    "work_difficulty": "hard",
                    "difficulty_decision_id": "b" * 24,
                    "objective": {"fingerprint": "a" * 16, "length": 1},
                    "plan_state": "analyzing",
                }
            )
            self.assertTrue(
                HOOK.write_plan_artifact(state, payload, self.hard_body("first"))
            )
            state["plan_state"] = "awaiting_confirmation"

        with patch.object(HOOK, "atomic_write", side_effect=OSError("state fail")):
            _, changed = HOOK.mutate_state(payload, first_change)
        self.assertFalse(changed)
        directory = self.data / "plans" / HOOK.plan_artifact_session_id(session)
        self.assertFalse((directory / HOOK.PLAN_JOURNAL_NAME).exists())
        self.assertEqual(list(directory.glob(".*transaction*")), [])
        self.assertEqual(list(directory.glob(".*backup*")), [])

        payload["hook_run_id"] = "first-success"
        stable, changed = HOOK.mutate_state(payload, first_change)
        self.assertTrue(changed)
        journal = self.artifact_path(stable)
        state_path = self.state_path()
        old_journal = journal.read_bytes()
        old_state = state_path.read_bytes()

        def second_change(state: dict) -> None:
            state["plan_state"] = "analyzing"
            state["plan_digest"] = None
            state["plan_objective_fingerprint"] = None
            state["plan_difficulty_decision_id"] = None
            self.assertTrue(
                HOOK.write_plan_artifact(state, payload, self.hard_body("second"))
            )
            state["plan_state"] = "awaiting_confirmation"

        payload["hook_run_id"] = "second-fail"
        with patch.object(HOOK, "atomic_write", side_effect=OSError("state fail")):
            _, changed = HOOK.mutate_state(payload, second_change)
        self.assertFalse(changed)
        self.assertEqual(journal.read_bytes(), old_journal)
        self.assertEqual(state_path.read_bytes(), old_state)
        self.assertEqual(list(directory.glob(".*transaction*")), [])
        self.assertEqual(list(directory.glob(".*backup*")), [])

    def test_c10_crash_recovery_accepts_only_old_old_or_new_new(self) -> None:
        session = "c10"
        self.begin_parent_plan(session)
        stable = self.accept_parent_plan(session, self.hard_body("old"))
        journal = self.artifact_path(stable)
        old_bytes = journal.read_bytes()
        payload = {"hook_event_name": "SessionStart", "session_id": session}
        environment = patch.dict(
            os.environ,
            {"PLUGIN_DATA": str(self.data), "CODEX_HOME": str(self.codex_home)},
        )
        environment.start()
        self.addCleanup(environment.stop)

        def stage(label: str) -> tuple[dict, dict]:
            state = self.state()
            state["plan_state"] = "analyzing"
            state["plan_digest"] = None
            state["plan_objective_fingerprint"] = None
            state["plan_difficulty_decision_id"] = None
            state["_defer_plan_transaction"] = True
            self.assertTrue(
                HOOK.write_plan_artifact(state, payload, self.hard_body(label))
            )
            state.pop("_defer_plan_transaction", None)
            pending = state.pop("_plan_transaction")
            state["plan_state"] = "awaiting_confirmation"
            HOOK.sync_plan_artifact_lifecycle(state)
            return state, pending

        _, pending = stage("old-new-crash")
        pending["guard_context"].__exit__(None, None, None)
        self.assertNotEqual(journal.read_bytes(), old_bytes)
        recovered = HOOK.snapshot_state(payload)
        self.assertEqual(journal.read_bytes(), old_bytes)
        self.assertEqual(recovered["plan_generation"], stable["plan_generation"])
        self.assertEqual(list(journal.parent.glob(".*transaction*")), [])
        self.assertEqual(list(journal.parent.glob(".*backup*")), [])

        new_state, pending = stage("new-new-crash")
        new_bytes = journal.read_bytes()
        HOOK.atomic_write(self.state_path(), new_state)
        pending["guard_context"].__exit__(None, None, None)
        recovered = HOOK.snapshot_state(payload)
        self.assertEqual(journal.read_bytes(), new_bytes)
        self.assertEqual(recovered["plan_generation"], 2)
        self.assertEqual(recovered["plan_artifact"]["write_status"], "written")
        self.assertEqual(list(journal.parent.glob(".*transaction*")), [])
        self.assertEqual(list(journal.parent.glob(".*backup*")), [])

    def test_c11_schema19_merges_up_to_six_verified_mirrors_then_cleans(self) -> None:
        session = "c11"
        payload = {"hook_event_name": "SessionStart", "session_id": session}
        environment = patch.dict(
            os.environ,
            {"PLUGIN_DATA": str(self.data), "CODEX_HOME": str(self.codex_home)},
        )
        environment.start()
        self.addCleanup(environment.stop)
        token = HOOK.plan_artifact_session_id(session)
        directory = self.data / "plans" / token
        directory.mkdir(parents=True)
        current_digest = ""
        current_content_digest = ""
        for generation in (2, 4, 6):
            current_digest = f"{generation:032x}"
            body = self.hard_body(f"legacy-{generation}")
            current_content_digest = HOOK.stable_hash(body, 32)
            (directory / f"hard-plan-g{generation:04d}-{current_digest}.md").write_bytes(
                self.legacy_document(generation, current_digest, body)
            )
        legacy = HOOK.new_state(payload)
        legacy.update(
            {
                "schema_version": 19,
                "writer_version": "1.0.36",
                "task_domain": "work",
                "work_difficulty": "hard",
                "difficulty_decision_id": "b" * 24,
                "objective": {"fingerprint": "a" * 16, "length": 1},
                "plan_state": "awaiting_confirmation",
                "plan_generation": 6,
                "plan_digest": current_digest,
                "plan_objective_fingerprint": "a" * 16,
                "plan_difficulty_decision_id": "b" * 24,
                "plan_artifact": {
                    "relative_path": f"plans/{token}/hard-plan-g0006-{current_digest}.md",
                    "objective_fingerprint": "a" * 16,
                    "difficulty_decision_id": "b" * 24,
                    "plan_digest": current_digest,
                    "content_digest": current_content_digest,
                    "generation": 6,
                    "lifecycle_status": "ready",
                    "write_status": "written",
                    "warning_code": "none",
                },
            }
        )
        HOOK.atomic_write(HOOK.state_path(payload), legacy)

        migrated = HOOK.snapshot_state(payload)
        journal = directory / HOOK.PLAN_JOURNAL_NAME
        parsed = HOOK.parse_plan_journal(journal.read_bytes(), expected_session=token)
        self.assertEqual(
            [item["generation"] for item in parsed["revisions"]], [2, 4, 6]
        )
        self.assertEqual(migrated["schema_version"], HOOK.SCHEMA_VERSION)
        self.assertEqual(migrated["plan_state"], "invalidated")
        self.assertEqual(migrated["plan_artifact"]["revision_count"], 3)
        self.assertEqual(list(directory.glob("hard-plan-g*.md")), [])
        self.assertEqual(list(directory.glob(".*transaction*")), [])

    def test_c12_schema19_over_six_or_running_contract_fails_closed(self) -> None:
        for count, executor_state, expected_status in (
            (7, "none", "legacy_unavailable"),
            (1, "running", "written"),
        ):
            with self.subTest(count=count, executor_state=executor_state):
                selected = self.root / f"legacy-{count}-{executor_state}"
                payload = {
                    "hook_event_name": "SessionStart",
                    "session_id": f"c12-{count}-{executor_state}",
                }
                with patch.dict(
                    os.environ,
                    {
                        "PLUGIN_DATA": str(selected),
                        "CODEX_HOME": str(self.codex_home),
                    },
                ):
                    token = HOOK.plan_artifact_session_id(payload["session_id"])
                    directory = selected / "plans" / token
                    directory.mkdir(parents=True)
                    for generation in range(1, count + 1):
                        digest = f"{generation:032x}"
                        body = self.hard_body(f"legacy-{generation}")
                        (directory / f"hard-plan-g{generation:04d}-{digest}.md").write_bytes(
                            self.legacy_document(generation, digest, body)
                        )
                    current_digest = f"{count:032x}"
                    current_body = self.hard_body(f"legacy-{count}")
                    legacy = HOOK.new_state(payload)
                    legacy.update(
                        {
                            "schema_version": 19,
                            "writer_version": "1.0.36",
                            "task_domain": "work",
                            "work_difficulty": "hard",
                            "difficulty_decision_id": "b" * 24,
                            "objective": {"fingerprint": "a" * 16, "length": 1},
                            "plan_state": "confirmed" if executor_state == "running" else "awaiting_confirmation",
                            "plan_generation": count,
                            "plan_digest": current_digest,
                            "plan_objective_fingerprint": "a" * 16,
                            "plan_difficulty_decision_id": "b" * 24,
                            "confirmed_plan_digest": current_digest if executor_state == "running" else None,
                            "executor_state": executor_state,
                            "execution_contract_id": "e" * 32 if executor_state == "running" else None,
                            "plan_artifact": {
                                "relative_path": f"plans/{token}/hard-plan-g{count:04d}-{current_digest}.md",
                                "objective_fingerprint": "a" * 16,
                                "difficulty_decision_id": "b" * 24,
                                "plan_digest": current_digest,
                                "content_digest": HOOK.stable_hash(current_body, 32),
                                "generation": count,
                                "lifecycle_status": "executing" if executor_state == "running" else "ready",
                                "write_status": "written",
                                "warning_code": "none",
                            },
                        }
                    )
                    HOOK.atomic_write(HOOK.state_path(payload), legacy)
                    observed = HOOK.snapshot_state(payload)
                    self.assertEqual(observed["plan_state"], "invalidated")
                    self.assertIsNone(observed["confirmed_plan_digest"])
                    self.assertEqual(
                        observed["plan_artifact"]["write_status"], expected_status
                    )
                    if executor_state == "running":
                        self.assertEqual(
                            observed["executor_failure_kind"], "stale_contract"
                        )
                        self.assertTrue((directory / HOOK.PLAN_JOURNAL_NAME).exists())
                    else:
                        self.assertFalse((directory / HOOK.PLAN_JOURNAL_NAME).exists())
                        self.assertEqual(len(list(directory.glob("hard-plan-g*.md"))), 7)

    def test_c16_schema19_malformed_managed_mirror_fails_closed_without_cleanup(self) -> None:
        session = "c16"
        payload = {"hook_event_name": "SessionStart", "session_id": session}
        environment = patch.dict(
            os.environ,
            {"PLUGIN_DATA": str(self.data), "CODEX_HOME": str(self.codex_home)},
        )
        environment.start()
        self.addCleanup(environment.stop)
        token = HOOK.plan_artifact_session_id(session)
        directory = self.data / "plans" / token
        directory.mkdir(parents=True)
        body = self.hard_body("legacy-current")
        digest = "1" * 32
        current = directory / f"hard-plan-g0001-{digest}.md"
        current.write_bytes(self.legacy_document(1, digest, body))
        malformed = directory / f"hard-plan-g0002-{'2' * 32}.md"
        malformed.write_bytes(b"not a managed legacy plan document\n")
        legacy = HOOK.new_state(payload)
        legacy.update(
            {
                "schema_version": 19,
                "writer_version": "1.0.36",
                "task_domain": "work",
                "work_difficulty": "hard",
                "difficulty_decision_id": "b" * 24,
                "objective": {"fingerprint": "a" * 16, "length": 1},
                "plan_state": "awaiting_confirmation",
                "plan_generation": 1,
                "plan_digest": digest,
                "plan_objective_fingerprint": "a" * 16,
                "plan_difficulty_decision_id": "b" * 24,
                "plan_artifact": {
                    "relative_path": f"plans/{token}/{current.name}",
                    "objective_fingerprint": "a" * 16,
                    "difficulty_decision_id": "b" * 24,
                    "plan_digest": digest,
                    "content_digest": HOOK.stable_hash(body, 32),
                    "generation": 1,
                    "lifecycle_status": "ready",
                    "write_status": "written",
                    "warning_code": "none",
                },
            }
        )
        HOOK.atomic_write(HOOK.state_path(payload), legacy)

        observed = HOOK.snapshot_state(payload)
        self.assertEqual(observed["plan_state"], "invalidated")
        self.assertEqual(
            observed["plan_artifact"]["write_status"], "legacy_unavailable"
        )
        self.assertFalse((directory / HOOK.PLAN_JOURNAL_NAME).exists())
        self.assertEqual(current.read_bytes(), self.legacy_document(1, digest, body))
        self.assertEqual(malformed.read_bytes(), b"not a managed legacy plan document\n")

    def test_c17_unbound_subagent_cannot_retry_a_failed_canonical_write(self) -> None:
        self.data.mkdir(parents=True)
        (self.data / "plans").write_text("block plans directory", encoding="utf-8")
        binding, agent_id = self.begin_assessor("c17")
        message = self.assessor_result(binding, self.hard_body("trusted-retry"))
        first = self.run_hook(
            {
                "hook_event_name": "SubagentStop",
                "session_id": "c17",
                "hook_run_id": "trusted-first-stop",
                "agent_id": agent_id,
                "status": "completed",
                "last_assistant_message": message,
            }
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        failed = self.state()
        generation = failed["plan_generation"]
        self.assertEqual(failed["plan_artifact"]["write_status"], "write_failed")
        (self.data / "plans").unlink()

        forged = self.run_hook(
            {
                "hook_event_name": "SubagentStop",
                "session_id": "c17",
                "hook_run_id": "unbound-retry",
                "agent_id": "unbound-agent",
                "status": "completed",
                "last_assistant_message": message,
            }
        )
        self.assertEqual(forged.returncode, 0, forged.stderr)
        observed = self.state()
        self.assertEqual(observed["plan_generation"], generation)
        self.assertEqual(observed["plan_state"], "analyzing")
        self.assertEqual(observed["plan_artifact"]["write_status"], "write_failed")
        self.assertFalse((self.data / "plans").exists())

    def test_c18_atomic_replace_rechecks_old_identity_before_backup_rename(self) -> None:
        directory = self.root / "atomic-identity"
        directory.mkdir()
        target = directory / HOOK.PLAN_JOURNAL_NAME
        target.write_bytes(b"trusted-old\n")
        attacker = directory / "attacker.md"
        attacker.write_bytes(b"external-race\n")
        swapped = False

        def swap_before_backup() -> None:
            nonlocal swapped
            if not swapped:
                os.replace(attacker, target)
                swapped = True

        with self.assertRaises(HOOK.PlanArtifactError) as raised:
            HOOK._atomic_write_plan_file(
                target,
                b"trusted-new\n",
                expected_old_bytes=b"trusted-old\n",
                verify_binding=swap_before_backup,
            )
        self.assertEqual(raised.exception.code, "unsafe_path")
        self.assertEqual(target.read_bytes(), b"external-race\n")
        self.assertEqual(list(directory.glob(".*backup*")), [])
        self.assertEqual(list(directory.glob(".*tmp")), [])

    def test_c19_plan_directory_fsync_failure_stays_old_authority_and_recovers(self) -> None:
        session = "c19"
        self.begin_parent_plan(session)
        stable = self.accept_parent_plan(session, self.hard_body("old"))
        journal = self.artifact_path(stable)
        old_bytes = journal.read_bytes()
        payload = {
            "hook_event_name": "Stop",
            "session_id": session,
            "hook_run_id": "journal-fsync-failure",
        }
        environment = patch.dict(
            os.environ,
            {"PLUGIN_DATA": str(self.data), "CODEX_HOME": str(self.codex_home)},
        )
        environment.start()
        self.addCleanup(environment.stop)
        calls = 0
        real_fsync_directory = HOOK._fsync_plan_directory

        def fail_journal_fsync(directory_fd: int | None) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise HOOK.PlanArtifactError("write_error")
            real_fsync_directory(directory_fd)

        def append_revision(state: dict) -> None:
            state["plan_state"] = "analyzing"
            state["plan_digest"] = None
            state["plan_objective_fingerprint"] = None
            state["plan_difficulty_decision_id"] = None
            self.assertFalse(
                HOOK.write_plan_artifact(state, payload, self.hard_body("new"))
            )

        with patch.object(
            HOOK, "_fsync_plan_directory", side_effect=fail_journal_fsync
        ):
            _, changed = HOOK.mutate_state(payload, append_revision)
        self.assertTrue(changed)
        failed = self.state()
        self.assertEqual(failed["plan_generation"], stable["plan_generation"])
        self.assertNotEqual(failed["plan_state"], "awaiting_confirmation")
        self.assertEqual(failed["plan_artifact"]["write_status"], "write_failed")
        self.assertNotEqual(journal.read_bytes(), old_bytes)
        marker = journal.parent / HOOK.PLAN_TRANSACTION_MARKER_NAME
        self.assertTrue(marker.exists())

        recovered = HOOK.snapshot_state(payload)
        self.assertEqual(recovered["plan_generation"], stable["plan_generation"])
        self.assertEqual(journal.read_bytes(), old_bytes)
        self.assertFalse(marker.exists())
        self.assertEqual(list(journal.parent.glob(".*backup*")), [])

    def test_c20_schema19_cleanup_failure_is_retried_from_transaction_marker(self) -> None:
        session = "c20"
        payload = {"hook_event_name": "SessionStart", "session_id": session}
        environment = patch.dict(
            os.environ,
            {"PLUGIN_DATA": str(self.data), "CODEX_HOME": str(self.codex_home)},
        )
        environment.start()
        self.addCleanup(environment.stop)
        token = HOOK.plan_artifact_session_id(session)
        directory = self.data / "plans" / token
        directory.mkdir(parents=True)
        body = self.hard_body("legacy-cleanup")
        digest = "3" * 32
        legacy_path = directory / f"hard-plan-g0001-{digest}.md"
        legacy_path.write_bytes(self.legacy_document(1, digest, body))
        legacy = HOOK.new_state(payload)
        legacy.update(
            {
                "schema_version": 19,
                "writer_version": "1.0.36",
                "task_domain": "work",
                "work_difficulty": "hard",
                "difficulty_decision_id": "b" * 24,
                "objective": {"fingerprint": "a" * 16, "length": 1},
                "plan_state": "awaiting_confirmation",
                "plan_generation": 1,
                "plan_digest": digest,
                "plan_objective_fingerprint": "a" * 16,
                "plan_difficulty_decision_id": "b" * 24,
                "plan_artifact": {
                    "relative_path": f"plans/{token}/{legacy_path.name}",
                    "objective_fingerprint": "a" * 16,
                    "difficulty_decision_id": "b" * 24,
                    "plan_digest": digest,
                    "content_digest": HOOK.stable_hash(body, 32),
                    "generation": 1,
                    "lifecycle_status": "ready",
                    "write_status": "written",
                    "warning_code": "none",
                },
            }
        )
        HOOK.atomic_write(HOOK.state_path(payload), legacy)

        with patch.object(
            HOOK,
            "_retain_plan_artifacts",
            side_effect=HOOK.PlanArtifactError("unsafe_path"),
        ):
            first = HOOK.snapshot_state(payload)
        marker = directory / HOOK.PLAN_TRANSACTION_MARKER_NAME
        self.assertEqual(first["schema_version"], HOOK.SCHEMA_VERSION)
        self.assertTrue((directory / HOOK.PLAN_JOURNAL_NAME).exists())
        self.assertTrue(legacy_path.exists())
        self.assertTrue(marker.exists())

        recovered = HOOK.snapshot_state(payload)
        self.assertEqual(recovered["plan_state"], "invalidated")
        self.assertFalse(legacy_path.exists())
        self.assertFalse(marker.exists())

    def test_c21_confirmation_rereads_journal_inside_state_mutation(self) -> None:
        session = "c21"
        self.begin_parent_plan(session)
        ready = self.accept_parent_plan(session, self.hard_body("confirm-race"))
        journal = self.artifact_path(ready)
        environment = patch.dict(
            os.environ,
            {"PLUGIN_DATA": str(self.data), "CODEX_HOME": str(self.codex_home)},
        )
        environment.start()
        self.addCleanup(environment.stop)
        real_verify = HOOK.verify_plan_artifact
        verify_calls = 0

        def verify_then_drift(state: dict, payload: dict) -> bool:
            nonlocal verify_calls
            result = real_verify(state, payload)
            verify_calls += 1
            if verify_calls == 1 and result:
                journal.write_bytes(journal.read_bytes() + b"external-confirm-race\n")
            return result

        def snapshot_without_verification(payload: dict) -> dict:
            return HOOK.load_state(self.state_path(), payload)

        with patch.object(HOOK, "snapshot_state", side_effect=snapshot_without_verification), patch.object(
            HOOK, "verify_plan_artifact", side_effect=verify_then_drift
        ), redirect_stdout(io.StringIO()):
            HOOK.user_prompt_submit(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": session,
                    "hook_run_id": "confirm-race",
                    "prompt": "确认按这个计划执行",
                }
            )
        observed = self.state()
        self.assertGreaterEqual(verify_calls, 2)
        self.assertEqual(observed["plan_state"], "invalidated")
        self.assertIsNone(observed["confirmed_plan_digest"])
        self.assertIsNone(observed["execution_contract_id"])

    def test_c22_confirmation_rechecks_journal_after_state_commit(self) -> None:
        session = "c22"
        self.begin_parent_plan(session)
        ready = self.accept_parent_plan(session, self.hard_body("post-commit-race"))
        journal = self.artifact_path(ready)
        environment = patch.dict(
            os.environ,
            {"PLUGIN_DATA": str(self.data), "CODEX_HOME": str(self.codex_home)},
        )
        environment.start()
        self.addCleanup(environment.stop)
        real_verify = HOOK.verify_plan_artifact
        verify_calls = 0

        def verify_then_drift(state: dict, payload: dict) -> bool:
            nonlocal verify_calls
            result = real_verify(state, payload)
            verify_calls += 1
            if verify_calls == 2 and result:
                journal.write_bytes(journal.read_bytes() + b"external-post-commit-race\n")
            return result

        def snapshot_without_verification(payload: dict) -> dict:
            return HOOK.load_state(self.state_path(), payload)

        with patch.object(HOOK, "snapshot_state", side_effect=snapshot_without_verification), patch.object(
            HOOK, "verify_plan_artifact", side_effect=verify_then_drift
        ), redirect_stdout(io.StringIO()):
            HOOK.user_prompt_submit(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": session,
                    "hook_run_id": "post-commit-confirm-race",
                    "prompt": "确认按这个计划执行",
                }
            )
        observed = self.state()
        self.assertGreaterEqual(verify_calls, 3)
        self.assertEqual(observed["plan_state"], "invalidated")
        self.assertEqual(observed["executor_failure_kind"], "stale_contract")
        self.assertIsNone(observed["confirmed_plan_digest"])
        self.assertIsNone(observed["execution_contract_id"])

    def test_c23_append_rejects_in_place_edit_after_old_journal_read(self) -> None:
        session = "c23"
        self.begin_parent_plan(session)
        stable = self.accept_parent_plan(session, self.hard_body("old"))
        journal = self.artifact_path(stable)
        old_bytes = journal.read_bytes()
        external = b"EXTERNAL_IN_PLACE_EDIT\n"
        payload = {
            "hook_event_name": "Stop",
            "session_id": session,
            "hook_run_id": "append-in-place-race",
        }
        environment = patch.dict(
            os.environ,
            {"PLUGIN_DATA": str(self.data), "CODEX_HOME": str(self.codex_home)},
        )
        environment.start()
        self.addCleanup(environment.stop)
        real_atomic_write = HOOK._atomic_write_plan_file
        raced = False

        def edit_at_atomic_entry(path: Path, document: bytes, **kwargs: object) -> dict:
            nonlocal raced
            if not raced:
                path.write_bytes(path.read_bytes() + external)
                raced = True
            return real_atomic_write(path, document, **kwargs)

        def append_revision(state: dict) -> None:
            state["plan_state"] = "analyzing"
            state["plan_digest"] = None
            state["plan_objective_fingerprint"] = None
            state["plan_difficulty_decision_id"] = None
            self.assertFalse(
                HOOK.write_plan_artifact(state, payload, self.hard_body("new"))
            )

        with patch.object(
            HOOK, "_atomic_write_plan_file", side_effect=edit_at_atomic_entry
        ):
            _, changed = HOOK.mutate_state(payload, append_revision)
        self.assertTrue(changed)
        observed = self.state()
        self.assertTrue(raced)
        self.assertEqual(journal.read_bytes(), old_bytes + external)
        self.assertEqual(observed["plan_generation"], stable["plan_generation"])
        self.assertEqual(observed["plan_state"], "analyzing")
        self.assertEqual(observed["plan_artifact"]["write_status"], "write_failed")
        self.assertEqual(observed["plan_artifact"]["warning_code"], "content_drift")
        self.assertFalse((journal.parent / HOOK.PLAN_TRANSACTION_MARKER_NAME).exists())
        self.assertEqual(list(journal.parent.glob(".*backup*")), [])

    def test_c24_first_write_preserves_file_created_after_absence_check(self) -> None:
        session = "c24"
        self.begin_parent_plan(session)
        payload = {
            "hook_event_name": "Stop",
            "session_id": session,
            "hook_run_id": "first-write-absence-race",
        }
        token = HOOK.plan_artifact_session_id(session)
        journal = self.data / "plans" / token / HOOK.PLAN_JOURNAL_NAME
        external = b"EXTERNAL_CREATED_AFTER_ABSENCE_CHECK\n"
        environment = patch.dict(
            os.environ,
            {"PLUGIN_DATA": str(self.data), "CODEX_HOME": str(self.codex_home)},
        )
        environment.start()
        self.addCleanup(environment.stop)
        real_atomic_write = HOOK._atomic_write_plan_file
        raced = False

        def create_at_atomic_entry(path: Path, document: bytes, **kwargs: object) -> dict:
            nonlocal raced
            if not raced:
                path.write_bytes(external)
                raced = True
            return real_atomic_write(path, document, **kwargs)

        def first_revision(state: dict) -> None:
            self.assertFalse(
                HOOK.write_plan_artifact(state, payload, self.hard_body("first"))
            )

        with patch.object(
            HOOK, "_atomic_write_plan_file", side_effect=create_at_atomic_entry
        ):
            _, changed = HOOK.mutate_state(payload, first_revision)
        self.assertTrue(changed)
        observed = self.state()
        self.assertTrue(raced)
        self.assertEqual(journal.read_bytes(), external)
        self.assertEqual(observed["plan_generation"], 0)
        self.assertNotEqual(observed["plan_state"], "awaiting_confirmation")
        self.assertEqual(observed["plan_artifact"]["write_status"], "write_failed")
        self.assertEqual(observed["plan_artifact"]["warning_code"], "content_drift")
        self.assertFalse((journal.parent / HOOK.PLAN_TRANSACTION_MARKER_NAME).exists())
        self.assertEqual(list(journal.parent.glob(".*backup*")), [])

    def test_c25_schema19_migration_preserves_file_created_after_absence_check(self) -> None:
        session = "c25"
        payload = {"hook_event_name": "SessionStart", "session_id": session}
        environment = patch.dict(
            os.environ,
            {"PLUGIN_DATA": str(self.data), "CODEX_HOME": str(self.codex_home)},
        )
        environment.start()
        self.addCleanup(environment.stop)
        token = HOOK.plan_artifact_session_id(session)
        directory = self.data / "plans" / token
        directory.mkdir(parents=True)
        body = self.hard_body("legacy-race")
        digest = "4" * 32
        legacy_path = directory / f"hard-plan-g0001-{digest}.md"
        legacy_bytes = self.legacy_document(1, digest, body)
        legacy_path.write_bytes(legacy_bytes)
        legacy = HOOK.new_state(payload)
        legacy.update(
            {
                "schema_version": 19,
                "writer_version": "1.0.36",
                "task_domain": "work",
                "work_difficulty": "hard",
                "difficulty_decision_id": "b" * 24,
                "objective": {"fingerprint": "a" * 16, "length": 1},
                "plan_state": "awaiting_confirmation",
                "plan_generation": 1,
                "plan_digest": digest,
                "plan_objective_fingerprint": "a" * 16,
                "plan_difficulty_decision_id": "b" * 24,
                "plan_artifact": {
                    "relative_path": f"plans/{token}/{legacy_path.name}",
                    "objective_fingerprint": "a" * 16,
                    "difficulty_decision_id": "b" * 24,
                    "plan_digest": digest,
                    "content_digest": HOOK.stable_hash(body, 32),
                    "generation": 1,
                    "lifecycle_status": "ready",
                    "write_status": "written",
                    "warning_code": "none",
                },
            }
        )
        HOOK.atomic_write(HOOK.state_path(payload), legacy)
        journal = directory / HOOK.PLAN_JOURNAL_NAME
        external = b"EXTERNAL_CREATED_DURING_MIGRATION\n"
        real_atomic_write = HOOK._atomic_write_plan_file
        raced = False

        def create_at_atomic_entry(path: Path, document: bytes, **kwargs: object) -> dict:
            nonlocal raced
            if not raced:
                path.write_bytes(external)
                raced = True
            return real_atomic_write(path, document, **kwargs)

        with patch.object(
            HOOK, "_atomic_write_plan_file", side_effect=create_at_atomic_entry
        ):
            observed = HOOK.snapshot_state(payload)
        self.assertTrue(raced)
        self.assertEqual(observed["plan_state"], "invalidated")
        self.assertEqual(
            observed["plan_artifact"]["write_status"], "legacy_unavailable"
        )
        self.assertEqual(journal.read_bytes(), external)
        self.assertEqual(legacy_path.read_bytes(), legacy_bytes)
        self.assertFalse((directory / HOOK.PLAN_TRANSACTION_MARKER_NAME).exists())
        self.assertEqual(list(directory.glob(".*backup*")), [])

    def test_c26_atomic_replace_rechecks_bytes_after_binding_verification(self) -> None:
        directory = self.root / "atomic-byte-race"
        directory.mkdir()
        target = directory / HOOK.PLAN_JOURNAL_NAME
        old_bytes = b"trusted-old\n"
        external = b"EXTERNAL_DURING_BINDING_VERIFY\n"
        target.write_bytes(old_bytes)
        raced = False

        def edit_during_verification() -> None:
            nonlocal raced
            if not raced:
                target.write_bytes(target.read_bytes() + external)
                raced = True

        with self.assertRaises(HOOK.PlanArtifactError) as raised:
            HOOK._atomic_write_plan_file(
                target,
                b"trusted-new\n",
                expected_old_bytes=old_bytes,
                verify_binding=edit_during_verification,
            )
        self.assertEqual(raised.exception.code, "content_drift")
        self.assertEqual(target.read_bytes(), old_bytes + external)
        self.assertEqual(list(directory.glob(".*backup*")), [])
        self.assertEqual(list(directory.glob(".*tmp")), [])

    def test_c27_atomic_replace_verifies_backup_bytes_before_install(self) -> None:
        directory = self.root / "atomic-backup-byte-race"
        directory.mkdir()
        target = directory / HOOK.PLAN_JOURNAL_NAME
        old_bytes = b"trusted-old\n"
        external = b"EXTERNAL_AFTER_BACKUP_RENAME\n"
        target.write_bytes(old_bytes)
        real_rename = HOOK._plan_rename
        raced = False

        def rename_then_edit(path: Path, target_name: str, directory_fd: int | None) -> None:
            nonlocal raced
            real_rename(path, target_name, directory_fd)
            if not raced and ".backup." in target_name:
                backup = path.parent / target_name
                backup.write_bytes(backup.read_bytes() + external)
                raced = True

        with patch.object(HOOK, "_plan_rename", side_effect=rename_then_edit):
            with self.assertRaises(HOOK.PlanArtifactError) as raised:
                HOOK._atomic_write_plan_file(
                    target,
                    b"trusted-new\n",
                    expected_old_bytes=old_bytes,
                )
        self.assertEqual(raised.exception.code, "content_drift")
        self.assertTrue(raced)
        self.assertEqual(target.read_bytes(), old_bytes + external)
        self.assertEqual(list(directory.glob(".*backup*")), [])
        self.assertEqual(list(directory.glob(".*tmp")), [])

    def test_c28_atomic_first_write_rechecks_absence_after_binding_verification(self) -> None:
        directory = self.root / "atomic-absence-race"
        directory.mkdir()
        target = directory / HOOK.PLAN_JOURNAL_NAME
        external = b"EXTERNAL_DURING_ABSENCE_VERIFY\n"
        raced = False

        def create_during_verification() -> None:
            nonlocal raced
            if not raced:
                target.write_bytes(external)
                raced = True

        with self.assertRaises(HOOK.PlanArtifactError) as raised:
            HOOK._atomic_write_plan_file(
                target,
                b"trusted-first\n",
                expected_old_bytes=None,
                verify_binding=create_during_verification,
            )
        self.assertEqual(raised.exception.code, "content_drift")
        self.assertTrue(raced)
        self.assertEqual(target.read_bytes(), external)
        self.assertEqual(list(directory.glob(".*backup*")), [])
        self.assertEqual(list(directory.glob(".*tmp")), [])

    def test_c29_commit_unlink_preserves_backup_changed_after_byte_recheck(self) -> None:
        session = "c29"
        self.begin_parent_plan(session)
        stable = self.accept_parent_plan(session, self.hard_body("old"))
        journal = self.artifact_path(stable)
        payload = {
            "hook_event_name": "Stop",
            "session_id": session,
            "hook_run_id": "commit-unlink-race",
        }
        environment = patch.dict(
            os.environ,
            {"PLUGIN_DATA": str(self.data), "CODEX_HOME": str(self.codex_home)},
        )
        environment.start()
        self.addCleanup(environment.stop)
        real_unlink = HOOK._unlink_plan_file_if_identity
        external = b"EXTERNAL_AFTER_COMMIT_BYTE_RECHECK\n"
        raced = False

        def edit_at_unlink_entry(
            path: Path,
            expected: tuple[int, int, int],
            directory_fd: int | None,
            **kwargs: object,
        ) -> bool:
            nonlocal raced
            if not raced and ".backup." in path.name:
                path.write_bytes(path.read_bytes() + external)
                raced = True
            return real_unlink(path, expected, directory_fd, **kwargs)

        def append_revision(state: dict) -> None:
            state["plan_state"] = "analyzing"
            state["plan_digest"] = None
            state["plan_objective_fingerprint"] = None
            state["plan_difficulty_decision_id"] = None
            self.assertTrue(
                HOOK.write_plan_artifact(state, payload, self.hard_body("new"))
            )

        with patch.object(
            HOOK, "_unlink_plan_file_if_identity", side_effect=edit_at_unlink_entry
        ):
            state, changed = HOOK.mutate_state(payload, append_revision)
        self.assertTrue(changed)
        self.assertTrue(raced)
        self.assertEqual(state["plan_state"], "invalidated")
        self.assertEqual(state["plan_artifact"]["warning_code"], "content_drift")
        marker = journal.parent / HOOK.PLAN_TRANSACTION_MARKER_NAME
        backups = list(journal.parent.glob(".*backup*"))
        self.assertTrue(marker.exists())
        self.assertEqual(len(backups), 1)
        self.assertTrue(backups[0].read_bytes().endswith(external))
        persisted = self.state()
        self.assertEqual(persisted["plan_state"], "invalidated")
        self.assertEqual(
            persisted["plan_artifact"]["warning_code"], "content_drift"
        )

    @unittest.skipIf(os.name == "nt", "POSIX open-descriptor write race")
    def test_c30_commit_detects_write_through_open_fd_during_unlink(self) -> None:
        session = "c30"
        self.begin_parent_plan(session)
        stable = self.accept_parent_plan(session, self.hard_body("old"))
        journal = self.artifact_path(stable)
        payload = {
            "hook_event_name": "Stop",
            "session_id": session,
            "hook_run_id": "commit-open-fd-race",
        }
        environment = patch.dict(
            os.environ,
            {"PLUGIN_DATA": str(self.data), "CODEX_HOME": str(self.codex_home)},
        )
        environment.start()
        self.addCleanup(environment.stop)
        real_guarded_unlink = HOOK._unlink_plan_file_if_identity
        real_os_unlink = HOOK.os.unlink
        external = b"EXTERNAL_THROUGH_OPEN_FD_DURING_UNLINK\n"
        raced = False

        def unlink_with_open_writer(
            path: Path,
            expected: tuple[int, int, int],
            directory_fd: int | None,
            **kwargs: object,
        ) -> bool:
            nonlocal raced
            if ".backup." not in path.name:
                return real_guarded_unlink(
                    path, expected, directory_fd, **kwargs
                )
            writer = os.open(path, os.O_WRONLY | os.O_APPEND)

            def write_then_unlink(
                unlink_path: object, *args: object, **unlink_kwargs: object
            ) -> None:
                nonlocal raced
                if not raced:
                    os.write(writer, external)
                    os.fsync(writer)
                    raced = True
                real_os_unlink(unlink_path, *args, **unlink_kwargs)

            try:
                with patch.object(
                    HOOK.os, "unlink", side_effect=write_then_unlink
                ):
                    return real_guarded_unlink(
                        path, expected, directory_fd, **kwargs
                    )
            finally:
                os.close(writer)

        def append_revision(state: dict) -> None:
            state["plan_state"] = "analyzing"
            state["plan_digest"] = None
            state["plan_objective_fingerprint"] = None
            state["plan_difficulty_decision_id"] = None
            self.assertTrue(
                HOOK.write_plan_artifact(state, payload, self.hard_body("new"))
            )

        with patch.object(
            HOOK,
            "_unlink_plan_file_if_identity",
            side_effect=unlink_with_open_writer,
        ):
            state, changed = HOOK.mutate_state(payload, append_revision)
        self.assertTrue(changed)
        self.assertTrue(raced)
        self.assertEqual(state["plan_state"], "invalidated")
        marker = journal.parent / HOOK.PLAN_TRANSACTION_MARKER_NAME
        backups = list(journal.parent.glob(".*backup*"))
        self.assertTrue(marker.exists())
        self.assertEqual(len(backups), 1)
        self.assertTrue(backups[0].read_bytes().endswith(external))
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)

    def test_m02_supported_parent_hard_plan_creates_markdown(self) -> None:
        self.begin_parent_plan("m02")
        state = self.accept_parent_plan("m02", self.hard_body("parent"))
        self.assertEqual(state["plan_artifact"]["write_status"], "written")
        self.assertIn("parent", self.artifact_path(state).read_text(encoding="utf-8"))

    def test_m03_body_sanitizes_protocol_controls_and_explicit_secrets(self) -> None:
        secret = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"
        body = self.hard_body(
            "private",
            extra=(
                "\npassword=hunter2\nAuthorization: Bearer abc.def.ghi\n"
                f"api_key=sk-live-super-secret-value\n{secret}\n\x00\x01"
            ),
        )
        state, _ = self.accept_assessor_plan("m03", body=body)
        text = self.artifact_path(state).read_text(encoding="utf-8")
        for forbidden in ("hunter2", "abc.def.ghi", "sk-live-super-secret-value", secret, "WORK_ASSESSMENT", "\x00", "\x01"):
            self.assertNotIn(forbidden, text)
        self.assertIn("[REDACTED]", text)
        parsed = HOOK.parse_plan_journal(text.encode("utf-8"))
        self.assertEqual(
            state["plan_artifact"]["current_revision_digest"],
            parsed["current_revision_digest"],
        )

    def test_m04_any_external_header_or_body_edit_invalidates_authority(self) -> None:
        state, _ = self.accept_assessor_plan("m04")
        path = self.artifact_path(state)
        original_plan = state["plan_digest"]
        document = path.read_bytes()
        path.write_bytes(
            document.replace(
                b"# Workflow Manager Hard Plan",
                b"# Local display title",
            )
        )
        drift = self.run_hook({"hook_event_name": "SessionStart", "session_id": "m04", "hook_run_id": "header", "source": "resume"})
        observed = self.state()
        self.assertEqual(observed["plan_artifact"]["write_status"], "content_drift")
        self.assertEqual(observed["plan_digest"], original_plan)
        self.assertEqual(observed["plan_state"], "invalidated")
        self.assertIsNone(observed["confirmed_plan_digest"])
        self.assertIn("content_drift", drift.stdout)

    def test_m05_schema17_migrates_without_inventing_body(self) -> None:
        legacy = HOOK.new_state({"session_id": "m05"})
        legacy.update(
            {
                "schema_version": 17,
                "plan_state": "awaiting_confirmation",
                "plan_generation": 3,
                "plan_digest": "a" * 32,
                "plan_objective_fingerprint": "b" * 16,
                "plan_difficulty_decision_id": "c" * 24,
                "plan_artifact": {"raw_plan": "must-not-survive"},
            }
        )
        migrated = HOOK.normalize_state(legacy, {"session_id": "m05"})
        self.assertEqual(migrated["schema_version"], HOOK.SCHEMA_VERSION)
        self.assertEqual(migrated["plan_artifact"]["write_status"], "legacy_unavailable")
        self.assertEqual(migrated["plan_artifact"]["plan_digest"], "a" * 32)
        self.assertNotIn("must-not-survive", json.dumps(migrated))

    def test_m06_write_failure_requires_a_parent_plan_retry_not_a_duplicate_agent_stop(self) -> None:
        self.data.mkdir(parents=True)
        (self.data / "plans").write_text("block plans directory", encoding="utf-8")
        binding, agent_id = self.begin_assessor("m06")
        message = self.assessor_result(binding, self.hard_body("retry"))
        payload = {
            "hook_event_name": "SubagentStop",
            "session_id": "m06",
            "hook_run_id": "first-stop",
            "agent_id": agent_id,
            "status": "completed",
            "last_assistant_message": message,
        }
        first = self.run_hook(payload)
        failed = self.state()
        self.assertEqual(failed["plan_state"], "analyzing")
        self.assertEqual(failed["plan_artifact"]["write_status"], "write_failed")
        generation = failed["plan_generation"]
        self.assertIn("write_failed", first.stdout)
        (self.data / "plans").unlink()
        payload.update({"hook_run_id": "retry-stop"})
        duplicate = self.run_hook(payload)
        unchanged = self.state()
        self.assertEqual(duplicate.returncode, 0, duplicate.stderr)
        self.assertEqual(unchanged["plan_artifact"]["write_status"], "write_failed")
        self.assertEqual(unchanged["plan_generation"], generation)
        self.assertEqual(unchanged["plan_state"], "analyzing")

        retry = self.run_hook(
            {
                "hook_event_name": "Stop",
                "session_id": "m06",
                "hook_run_id": "parent-plan-retry",
                "last_assistant_message": message,
            }
        )
        recovered = self.state()
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(recovered["plan_artifact"]["write_status"], "written")
        self.assertEqual(recovered["plan_generation"], generation + 1)
        self.assertEqual(recovered["plan_state"], "awaiting_confirmation")

    def test_m07_plugin_data_inside_project_is_rejected_without_self_lock(self) -> None:
        project = self.root / "project"
        project.mkdir()
        data = project / ".workflow-data"
        binding, agent_id = self.begin_assessor("m07", data=data)
        result = self.run_hook(
            {
                "hook_event_name": "SubagentStop",
                "session_id": "m07",
                "hook_run_id": "stop",
                "cwd": str(project),
                "agent_id": agent_id,
                "status": "completed",
                "last_assistant_message": self.assessor_result(binding, self.hard_body("project")),
            },
            data=data,
        )
        state = self.state(data)
        self.assertEqual(result.returncode, 0)
        self.assertNotIn(state["plan_state"], {"awaiting_confirmation", "confirmed"})
        self.assertEqual(state["plan_artifact"]["warning_code"], "unsafe_data_root")

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support required")
    def test_m08_symlinked_plan_directory_is_rejected(self) -> None:
        self.data.mkdir(parents=True)
        outside = self.root / "outside"
        outside.mkdir()
        self.symlink_or_skip(
            self.data / "plans",
            outside,
            target_is_directory=True,
        )
        state, _ = self.accept_assessor_plan("m08")
        self.assertEqual(state["plan_artifact"]["write_status"], "write_failed")
        self.assertEqual(list(outside.iterdir()), [])

    def test_m09_concurrent_duplicate_stop_writes_one_artifact(self) -> None:
        binding, agent_id = self.begin_assessor("m09")
        message = self.assessor_result(binding, self.hard_body("concurrent"))

        def invoke(index: int) -> subprocess.CompletedProcess[str]:
            return self.run_hook(
                {
                    "hook_event_name": "SubagentStop",
                    "session_id": "m09",
                    "hook_run_id": f"stop-{index}",
                    "agent_id": agent_id,
                    "status": "completed",
                    "last_assistant_message": message,
                }
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(invoke, range(2)))
        self.assertTrue(all(item.returncode == 0 for item in results))
        state = self.state()
        self.assertEqual(state["plan_generation"], 1)
        self.assertEqual(len(list((self.data / "plans").glob("*/*.md"))), 1)
        self.assertEqual(list((self.data / "plans").rglob("*.tmp")), [])

    def test_m10_all_replans_remain_in_one_journal_and_user_file_is_preserved(self) -> None:
        session = "m10"
        self.begin_parent_plan(session)
        for generation in range(1, 8):
            if generation > 1:
                path = self.state_path()
                state = json.loads(path.read_text(encoding="utf-8"))
                state["plan_state"] = "analyzing"
                state["plan_digest"] = None
                state["plan_objective_fingerprint"] = None
                state["plan_difficulty_decision_id"] = None
                path.write_text(json.dumps(state), encoding="utf-8")
            state = self.accept_parent_plan(session, self.hard_body(f"g{generation}"), run_id=f"plan-{generation}")
            if generation == 1:
                session_dir = self.artifact_path(state).parent
                (session_dir / f"hard-plan-g0000-{'0' * 32}.md").write_text("user file", encoding="utf-8")
        owned = [path for path in session_dir.glob("*.md") if path.read_text(encoding="utf-8").startswith(HOOK.PLAN_JOURNAL_OWNER)]
        self.assertEqual(len(owned), 1)
        self.assertTrue((session_dir / f"hard-plan-g0000-{'0' * 32}.md").exists())
        self.assertTrue(self.artifact_path(state).exists())
        self.assertEqual(
            HOOK.parse_plan_journal(self.artifact_path(state).read_bytes())["revision_count"],
            7,
        )

    def test_m11_lifecycle_status_tracks_confirmation_execution_and_success(self) -> None:
        session = "m11"
        self.begin_parent_plan(session)
        ready = self.accept_parent_plan(session, self.hard_body("lifecycle"))
        self.assertEqual(ready["plan_artifact"]["lifecycle_status"], "ready")
        self.run_hook({"hook_event_name": "UserPromptSubmit", "session_id": session, "hook_run_id": "confirm", "prompt": "确认按这个计划执行"})
        executing = self.state()
        self.assertEqual(executing["plan_artifact"]["lifecycle_status"], "executing")
        self.run_hook({"hook_event_name": "PostToolUse", "session_id": session, "hook_run_id": "write", "tool_name": "apply_patch", "tool_input": {"patch": "*** Begin Patch\n*** End Patch"}, "tool_response": {"status": "completed"}})
        self.run_hook({"hook_event_name": "PostToolUse", "session_id": session, "hook_run_id": "verify", "tool_name": "exec_command", "tool_input": {"cmd": "python3 -m unittest -q"}, "tool_response": {"status": "completed"}})
        self.run_hook({"hook_event_name": "Stop", "session_id": session, "hook_run_id": "done", "last_assistant_message": f"LOCAL_EXECUTION execution_contract_id={executing['execution_contract_id']} outcome=succeeded evidence_digest={'e' * 32}"})
        self.assertEqual(self.state()["plan_artifact"]["lifecycle_status"], "succeeded")

    def test_m12_objective_change_invalidates_artifact_binding(self) -> None:
        state, _ = self.accept_assessor_plan("m12")
        path = state["plan_artifact"]["relative_path"]
        self.run_hook({"hook_event_name": "UserPromptSubmit", "session_id": "m12", "hook_run_id": "new-objective", "prompt": "改为实现一个全新的 Android 蓝牙应用并编译验证"})
        invalidated = self.state()["plan_artifact"]
        self.assertEqual(invalidated["lifecycle_status"], "invalidated")
        self.assertEqual(invalidated["relative_path"], path)

    def test_m13_compaction_resume_exposes_only_safe_binding(self) -> None:
        secret = "resume-secret-should-not-appear"
        state, _ = self.accept_assessor_plan("m13", body=self.hard_body("compact", f"\npassword={secret}\n"))
        relative = state["plan_artifact"]["relative_path"]
        self.run_hook({"hook_event_name": "PreCompact", "session_id": "m13", "hook_run_id": "compact", "trigger": "auto"})
        compacted = self.state()
        self.assertEqual(compacted["compactions"][-1]["plan_artifact"]["relative_path"], relative)
        resumed = self.run_hook({"hook_event_name": "SessionStart", "session_id": "m13", "hook_run_id": "resume", "source": "resume"})
        self.assertIn(relative, resumed.stdout)
        self.assertNotIn(secret, resumed.stdout)
        self.assertNotIn(secret, self.state_path().read_text(encoding="utf-8"))

    @unittest.skipIf(os.name == "nt", "POSIX permission bits are not a Windows ACL claim")
    def test_m14_private_modes_atomic_replace_and_no_temporary_files(self) -> None:
        state, _ = self.accept_assessor_plan("m14")
        path = self.artifact_path(state)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(path.parent.parent.stat().st_mode), 0o700)
        self.assertEqual(path.stat().st_nlink, 1)
        self.assertEqual(list(path.parent.glob(f".{path.name}.*")), [])

    def test_m15_state_is_fingerprint_only_and_filename_is_strict(self) -> None:
        body = self.hard_body("state-private")
        state, _ = self.accept_assessor_plan("session with spaces/中文", body=body)
        artifact = state["plan_artifact"]
        self.assertEqual(
            set(artifact),
            {
                "relative_path",
                "format_version",
                "objective_fingerprint",
                "difficulty_decision_id",
                "plan_digest",
                "content_digest",
                "current_revision_digest",
                "journal_digest",
                "generation",
                "revision_count",
                "lifecycle_status",
                "write_status",
                "warning_code",
                "created_at",
                "updated_at",
            },
        )
        self.assertNotIn(body, json.dumps(state, ensure_ascii=False))
        self.assertRegex(
            artifact["relative_path"],
            r"^plans/[A-Za-z0-9._-]+-[0-9a-f]{16}/hard-plan\.md$",
        )


    def test_s01_noncanonical_and_symlinked_data_roots_are_rejected(self) -> None:
        for configured in ("relative-data", "../escape-data"):
            with self.subTest(configured=configured), patch.dict(
                os.environ,
                {"PLUGIN_DATA": configured, "CLAUDE_PLUGIN_DATA": ""},
            ):
                with self.assertRaises(HOOK.PlanArtifactError) as caught:
                    HOOK._canonical_plan_data_root({})
                self.assertEqual(caught.exception.code, "unsafe_data_root")
        target = self.root / "real-data"
        target.mkdir()
        linked = self.root / "linked-data"
        self.symlink_or_skip(linked, target, target_is_directory=True)
        with patch.dict(os.environ, {"PLUGIN_DATA": str(linked), "CLAUDE_PLUGIN_DATA": ""}):
            with self.assertRaises(HOOK.PlanArtifactError) as caught:
                HOOK._canonical_plan_data_root({})
        self.assertEqual(caught.exception.code, "unsafe_data_root")

    @unittest.skipUnless(hasattr(os, "link") and hasattr(os, "symlink"), "link support required")
    def test_s02_file_symlink_and_hardlink_are_never_replaced(self) -> None:
        directory = self.root / "links"
        directory.mkdir()
        source = directory / "source.md"
        source.write_text("original", encoding="utf-8")
        symbolic = directory / "symbolic.md"
        self.symlink_or_skip(symbolic, source)
        with self.assertRaises(HOOK.PlanArtifactError):
            HOOK._atomic_write_plan_file(
                symbolic, b"changed", expected_old_bytes=b"original"
            )
        hard = directory / "hard.md"
        os.link(source, hard)
        with self.assertRaises(HOOK.PlanArtifactError):
            HOOK._atomic_write_plan_file(
                hard, b"changed", expected_old_bytes=b"original"
            )
        self.assertEqual(source.read_text(encoding="utf-8"), "original")
        self.assertEqual(hard.read_text(encoding="utf-8"), "original")

    def test_s03_sanitizer_removes_bidi_all_protocol_markers_and_bounds_bytes(self) -> None:
        markers = (
            "WORK_ASSESSMENT binding_id=" + "a" * 32,
            "SIMPLE_EXECUTION binding_id=" + "b" * 32,
            "LOCAL_EXECUTION execution_contract_id=" + "c" * 32,
            "EXECUTION_STALL stall_id=" + "d" * 32,
            "STALL_DIAGNOSIS stall_id=" + "e" * 32,
            "CAUSAL_REVIEW baseline_id=" + "f" * 32,
            "WORKFLOW_COORDINATION_V1",
            "END_WORKFLOW_COORDINATION",
        )
        raw = "1. safe plan\n" + "\n".join(markers) + "\n\u202e\u2066hidden\u2069\n"
        body = HOOK.sanitize_plan_artifact_body(raw)
        for marker in markers:
            self.assertNotIn(marker, body)
        for control in ("\u202e", "\u2066", "\u2069"):
            self.assertNotIn(control, body)
        self.assertLessEqual(len(body.encode("utf-8")), HOOK.MAX_PLAN_REVISION_BYTES)
        with self.assertRaises(HOOK.PlanArtifactError) as raised:
            HOOK.sanitize_plan_artifact_body("x" * HOOK.MAX_PLAN_REVISION_BYTES)
        self.assertEqual(raised.exception.code, "revision_too_large")

    def test_s04_replace_failure_preserves_old_file_and_removes_temporary(self) -> None:
        directory = self.root / "atomic"
        directory.mkdir()
        target = directory / "plan.md"
        target.write_text("old-content", encoding="utf-8")
        with patch.object(HOOK.os, "replace", side_effect=OSError("simulated")):
            with self.assertRaises(OSError):
                HOOK._atomic_write_plan_file(
                    target, b"new-content", expected_old_bytes=b"old-content"
                )
        self.assertEqual(target.read_text(encoding="utf-8"), "old-content")
        self.assertEqual(list(directory.glob(".plan.md.*.tmp")), [])

    def test_s05_concurrent_different_parent_plans_commit_only_one_generation(self) -> None:
        session = "s05"
        self.begin_parent_plan(session)

        def invoke(label: str) -> subprocess.CompletedProcess[str]:
            return self.run_hook(
                {
                    "hook_event_name": "Stop",
                    "session_id": session,
                    "hook_run_id": f"stop-{label}",
                    "last_assistant_message": self.hard_body(label) + "计划已就绪，等待确认后执行",
                }
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(invoke, ("alpha", "beta")))
        self.assertTrue(all(result.returncode == 0 for result in results))
        state = self.state()
        files = list((self.data / "plans").glob("*/*.md"))
        self.assertEqual(state["plan_generation"], 1)
        self.assertEqual(len(files), 1)
        parsed = HOOK.parse_plan_journal(files[0].read_bytes())
        self.assertEqual(parsed["current_revision_digest"], state["plan_digest"])

    @unittest.skipUnless(hasattr(os, "link") and hasattr(os, "symlink"), "link support required")
    def test_s06_retention_uses_generation_and_preserves_forged_links(self) -> None:
        payload = {"session_id": "s06"}
        environment = patch.dict(os.environ, {"PLUGIN_DATA": str(self.data), "CODEX_HOME": str(self.codex_home)})
        environment.start()
        self.addCleanup(environment.stop)
        directory = self.data / "plans" / HOOK.plan_artifact_session_id("s06")
        directory.mkdir(parents=True)
        for generation in range(1, 9):
            digest = f"{generation:032x}"
            candidate = directory / f"hard-plan-g{generation:04d}-{digest}.md"
            candidate.write_text(
                f"{HOOK.LEGACY_PLAN_ARTIFACT_OWNER}\n"
                f"generation: {generation}\n"
                f"plan_digest: {digest}\n-->\n",
                encoding="utf-8",
            )
            if generation == 1:
                hard = directory / f"hard-plan-g9998-{'8' * 32}.md"
                os.link(candidate, hard)
                forged = directory / f"hard-plan-g9999-{'9' * 32}.md"
                forged.write_text("forged", encoding="utf-8")
                linked = directory / f"hard-plan-g9997-{'7' * 32}.md"
                self.symlink_or_skip(linked, forged)
        current = directory / f"hard-plan-g0008-{8:032x}.md"
        with HOOK.plan_session_directory_guard(self.data, HOOK.plan_artifact_session_id("s06"), create=False) as guard:
            HOOK._retain_plan_artifacts(
                directory,
                current,
                directory_fd=guard["directory_fd"],
                verify_binding=guard["verify"],
            )
        self.assertTrue(forged.exists())
        self.assertTrue(hard.exists())
        self.assertTrue(linked.is_symlink())
        generations = sorted(
            metadata[0]
            for candidate in directory.iterdir()
            if (metadata := HOOK._owned_plan_artifact(candidate)) is not None
        )
        self.assertEqual(generations, [3, 4, 5, 6, 7, 8])

    def test_s07_schema17_migration_is_idempotent_and_preserves_continuity(self) -> None:
        payload = {"session_id": "s07"}
        legacy = HOOK.new_state(payload)
        legacy.update(
            {
                "schema_version": 17,
                "plan_state": "confirmed",
                "plan_generation": 2,
                "plan_digest": "a" * 32,
                "plan_objective_fingerprint": "b" * 16,
                "plan_difficulty_decision_id": "c" * 24,
                "confirmed_plan_digest": "a" * 32,
                "executor_state": "recovery_required",
                "execution_contract_id": "d" * 32,
                "executor_failure_kind": "build_failed",
                "stall": {
                    "state": "diagnosis_required",
                    "stall_id": "e" * 32,
                    "objective_fingerprint": "b" * 16,
                    "plan_digest": "a" * 32,
                    "execution_contract_id": "d" * 32,
                    "failure_kind": "build_failed",
                    "resume_profile": "work_executor_low_latest",
                },
                "coordination_activity": [
                    {
                        "task_fingerprint": "1" * 32,
                        "host_fingerprint": "2" * 32,
                        "status": "active",
                        "snapshot_fingerprint": "3" * 32,
                        "observed_at": "2026-08-14T00:00:00Z",
                    }
                ],
            }
        )
        legacy["objective"] = {"fingerprint": "b" * 16, "length": 1}
        legacy["difficulty_decision_id"] = "c" * 24
        legacy["execution_contract_id"] = HOOK.execution_contract_id(legacy)
        legacy["stall"]["objective_fingerprint"] = legacy["objective"]["fingerprint"]
        legacy["stall"]["execution_contract_id"] = legacy["execution_contract_id"]
        first = HOOK.normalize_state(legacy, payload)
        second = HOOK.normalize_state(first, payload)
        for key in ("executor_state", "execution_contract_id", "executor_failure_kind", "stall", "coordination_activity", "plan_artifact"):
            self.assertEqual(second[key], first[key])
        self.assertEqual(first["plan_artifact"]["write_status"], "legacy_unavailable")
        self.assertEqual(first["plan_artifact"]["lifecycle_status"], "invalidated")
        self.assertEqual(first["plan_state"], "invalidated")

    def test_s08_compaction_resume_never_exposes_raw_session_identifier(self) -> None:
        session = "customer-secret-session-42"
        state, _ = self.accept_assessor_plan(session)
        self.assertNotIn(session, state["plan_artifact"]["relative_path"])
        self.run_hook({"hook_event_name": "PreCompact", "session_id": session, "hook_run_id": "compact", "trigger": "auto"})
        resumed = self.run_hook({"hook_event_name": "SessionStart", "session_id": session, "hook_run_id": "resume", "source": "resume"})
        self.assertNotIn(session, resumed.stdout)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support required")
    def test_s09_session_swap_after_check_never_writes_outside(self) -> None:
        payload = {"session_id": "s09"}
        state = HOOK.new_state(payload)
        state.update(
            {
                "plan_state": "awaiting_confirmation",
                "plan_generation": 0,
                "plan_digest": "9" * 32,
                "objective": {"fingerprint": "a" * 16, "length": 1},
                "difficulty_decision_id": "b" * 24,
                "plan_objective_fingerprint": "a" * 16,
                "plan_difficulty_decision_id": "b" * 24,
            }
        )
        outside = self.root / "outside-write"
        outside.mkdir()
        parked = self.root / "parked-write-session"
        original = HOOK._atomic_write_plan_file
        rename_blocked = False

        def swap_then_write(target: Path, document: bytes, *args: object, **kwargs: object) -> None:
            nonlocal rename_blocked
            try:
                target.parent.rename(parked)
            except OSError as error:
                if (
                    os.name == "nt"
                    and getattr(error, "winerror", None) in {5, 32}
                ):
                    rename_blocked = True
                raise
            self.symlink_or_skip(
                target.parent,
                outside,
                target_is_directory=True,
            )
            original(target, document, *args, **kwargs)

        environment = patch.dict(os.environ, {"PLUGIN_DATA": str(self.data), "CODEX_HOME": str(self.codex_home)})
        environment.start()
        self.addCleanup(environment.stop)
        with patch.object(HOOK, "_atomic_write_plan_file", side_effect=swap_then_write):
            HOOK.write_plan_artifact(state, payload, self.hard_body("swap-write"))
        self.assertEqual(list(outside.iterdir()), [])
        observed_directory = (
            self.data / "plans" / HOOK.plan_artifact_session_id("s09")
            if rename_blocked
            else parked
        )
        self.assertEqual(list(observed_directory.glob("hard-plan-*.md")), [])
        self.assertEqual(list(observed_directory.glob(".*.tmp")), [])
        self.assertEqual(state["plan_artifact"]["write_status"], "write_failed")
        self.assertEqual(
            state["plan_artifact"]["warning_code"],
            "write_error" if rename_blocked else "unsafe_path",
        )

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support required")
    def test_s10_session_swap_during_retention_never_deletes_outside(self) -> None:
        payload = {"session_id": "s10"}
        state = HOOK.new_state(payload)
        state.update(
            {
                "plan_state": "awaiting_confirmation",
                "plan_generation": 1,
                "plan_digest": "1" * 32,
                "plan_objective_fingerprint": "a" * 16,
                "plan_difficulty_decision_id": "b" * 24,
            }
        )
        session_directory = self.data / "plans" / HOOK.plan_artifact_session_id("s10")
        session_directory.mkdir(parents=True)
        outside = self.root / "outside-retention"
        outside.mkdir()
        expected: dict[str, bytes] = {}
        parked_expected: dict[str, bytes] = {}
        for generation in range(100, 107):
            digest = f"{generation:032x}"
            name = f"hard-plan-g{generation:04d}-{digest}.md"
            document = (
                f"{HOOK.PLAN_ARTIFACT_OWNER}\n"
                f"generation: {generation}\n"
                f"plan_digest: {digest}\n"
                "-->\n"
            )
            for base, snapshot in ((outside, expected), (session_directory, parked_expected)):
                candidate = base / name
                candidate.write_bytes(document.encode("utf-8"))
                snapshot[name] = candidate.read_bytes()
        parked = self.root / "parked-retention-session"
        original = HOOK._unlink_plan_file_if_identity
        attack_attempted = False
        swapped = False
        quarantined_before_unlink: list[str] = []
        quarantined_after_unlink: list[str] = []

        def swap_then_unlink(
            path: Path,
            identity: tuple[int, int, int],
            directory_fd: int | None,
            **kwargs: object,
        ) -> bool:
            nonlocal attack_attempted, swapped
            first_unlink = not attack_attempted
            if not attack_attempted:
                attack_attempted = True
                names = (
                    os.listdir(directory_fd)
                    if directory_fd is not None
                    else [candidate.name for candidate in path.parent.iterdir()]
                )
                quarantined_before_unlink.extend(
                    sorted(name for name in names if ".quarantine." in name)
                )
                try:
                    session_directory.rename(parked)
                    self.symlink_or_skip(
                        session_directory,
                        outside,
                        target_is_directory=True,
                    )
                    swapped = True
                except OSError as error:
                    raise HOOK.PlanArtifactError("unsafe_path") from error
            removed = original(path, identity, directory_fd, **kwargs)
            if first_unlink and directory_fd is not None:
                quarantined_after_unlink.extend(
                    sorted(
                        name
                        for name in os.listdir(directory_fd)
                        if ".quarantine." in name
                    )
                )
            return removed

        environment = patch.dict(os.environ, {"PLUGIN_DATA": str(self.data), "CODEX_HOME": str(self.codex_home)})
        environment.start()
        self.addCleanup(environment.stop)
        with patch.object(HOOK, "_unlink_plan_file_if_identity", side_effect=swap_then_unlink):
            try:
                with HOOK.plan_session_directory_guard(
                    self.data,
                    HOOK.plan_artifact_session_id("s10"),
                    create=False,
                ) as guard:
                    HOOK._retain_plan_artifacts(
                        session_directory,
                        session_directory / HOOK.PLAN_JOURNAL_NAME,
                        directory_fd=guard["directory_fd"],
                        verify_binding=guard["verify"],
                    )
            except HOOK.PlanArtifactError:
                pass
        observed = {
            candidate.name: candidate.read_bytes()
            for candidate in outside.iterdir()
            if candidate.is_file()
        }
        retained_directory = parked if swapped else session_directory
        parked_observed = {
            name: candidate.read_bytes()
            for name in parked_expected
            if (candidate := retained_directory / name).is_file()
        }
        self.assertTrue(attack_attempted)
        self.assertEqual(len(quarantined_before_unlink), 2)
        if os.name != "nt":
            self.assertEqual(len(quarantined_after_unlink), 1)
        self.assertEqual(parked_observed, parked_expected)
        self.assertEqual(sorted(candidate.name for candidate in retained_directory.iterdir()), sorted(parked_expected))
        self.assertEqual(observed, expected)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support required")
    def test_s11_verify_session_swap_never_reads_external_mirror(self) -> None:
        payload = {"session_id": "s11"}
        state, _ = self.accept_assessor_plan("s11")
        target = self.artifact_path(state)
        outside = self.root / "outside-verify"
        outside.mkdir()
        (outside / target.name).write_bytes(target.read_bytes())
        parked = self.root / "parked-verify-session"
        original = HOOK._canonical_plan_data_root
        authority = (
            state["plan_digest"],
            state["plan_artifact"]["plan_digest"],
            state["plan_artifact"]["content_digest"],
        )

        def swap_then_return(request: dict) -> Path:
            root = original(request)
            target.parent.rename(parked)
            self.symlink_or_skip(
                target.parent,
                outside,
                target_is_directory=True,
            )
            return root

        environment = patch.dict(os.environ, {"PLUGIN_DATA": str(self.data), "CODEX_HOME": str(self.codex_home)})
        environment.start()
        self.addCleanup(environment.stop)
        with patch.object(HOOK, "_canonical_plan_data_root", side_effect=swap_then_return):
            HOOK.verify_plan_artifact(state, payload)
        self.assertEqual(state["plan_artifact"]["write_status"], "content_drift")
        self.assertEqual(state["plan_artifact"]["warning_code"], "unsafe_path")
        self.assertEqual(authority, (state["plan_digest"], state["plan_artifact"]["plan_digest"], state["plan_artifact"]["content_digest"]))

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support required")
    def test_s12_same_plan_rewrite_drift_restores_old_mirror_byte_for_byte(self) -> None:
        payload = {"session_id": "s12"}
        state = HOOK.new_state(payload)
        state.update(
            {
                "plan_state": "awaiting_confirmation",
                "plan_generation": 0,
                "plan_digest": "2" * 32,
                "objective": {"fingerprint": "a" * 16, "length": 1},
                "difficulty_decision_id": "b" * 24,
                "plan_objective_fingerprint": "a" * 16,
                "plan_difficulty_decision_id": "b" * 24,
            }
        )
        environment = patch.dict(
            os.environ,
            {"PLUGIN_DATA": str(self.data), "CODEX_HOME": str(self.codex_home)},
        )
        environment.start()
        self.addCleanup(environment.stop)
        HOOK.write_plan_artifact(state, payload, self.hard_body("old-mirror"))
        target = self.artifact_path(state)
        old_document = target.read_bytes()
        outside = self.root / "outside-rewrite"
        outside.mkdir()
        parked = self.root / "parked-rewrite-session"
        original = HOOK._atomic_write_plan_file
        rename_blocked = False
        swapped = False

        def swap_then_rewrite(
            path: Path, document: bytes, *args: object, **kwargs: object
        ) -> dict[str, object]:
            nonlocal rename_blocked, swapped
            try:
                path.parent.rename(parked)
            except OSError as error:
                if (
                    os.name == "nt"
                    and getattr(error, "winerror", None) in {5, 32}
                ):
                    rename_blocked = True
                raise
            self.symlink_or_skip(
                path.parent,
                outside,
                target_is_directory=True,
            )
            swapped = True
            return original(path, document, *args, **kwargs)

        with patch.object(HOOK, "_atomic_write_plan_file", side_effect=swap_then_rewrite):
            HOOK.write_plan_artifact(state, payload, self.hard_body("new-mirror"))
        self.assertEqual(list(outside.iterdir()), [])
        restored_directory = target.parent if rename_blocked else parked
        restored = restored_directory / target.name
        self.assertEqual(restored.read_bytes(), old_document)
        self.assertEqual(
            [candidate.name for candidate in restored_directory.iterdir()],
            [target.name],
        )
        self.assertEqual(swapped, not rename_blocked)
        self.assertEqual(state["plan_artifact"]["write_status"], "write_failed")
        self.assertEqual(
            state["plan_artifact"]["warning_code"],
            "write_error" if rename_blocked else "unsafe_path",
        )

    def test_s13_retention_transactions_are_bounded_to_sixteen(self) -> None:
        directory = self.root / "bounded-retention"
        directory.mkdir()
        current: Path | None = None
        for generation in range(1, 25):
            digest = f"{generation:032x}"
            candidate = directory / f"hard-plan-g{generation:04d}-{digest}.md"
            candidate.write_bytes(
                (
                    f"{HOOK.PLAN_ARTIFACT_OWNER}\n"
                    f"generation: {generation}\n"
                    f"plan_digest: {digest}\n"
                    "-->\n"
                ).encode("utf-8")
            )
            if generation == 24:
                current = candidate
        self.assertIsNotNone(current)
        original = HOOK._unlink_plan_file_if_identity
        observed_quarantine_counts: list[int] = []

        def track_transaction_size(
            path: Path,
            identity: tuple[int, int, int],
            directory_fd: int | None,
            **kwargs: object,
        ) -> bool:
            names = (
                os.listdir(directory_fd)
                if directory_fd is not None
                else [candidate.name for candidate in path.parent.iterdir()]
            )
            observed_quarantine_counts.append(
                sum(".quarantine." in name for name in names)
            )
            return original(path, identity, directory_fd, **kwargs)

        with patch.object(
            HOOK,
            "_unlink_plan_file_if_identity",
            side_effect=track_transaction_size,
        ):
            HOOK._retain_plan_artifacts(directory, current)
        generations = sorted(
            metadata[0]
            for candidate in directory.iterdir()
            if (metadata := HOOK._owned_plan_artifact(candidate)) is not None
        )
        self.assertEqual(generations, [19, 20, 21, 22, 23, 24])
        self.assertEqual(
            observed_quarantine_counts,
            list(range(HOOK.MAX_RETENTION_TRANSACTION_ITEMS, 0, -1))
            + [2, 1],
        )
        self.assertFalse(
            any(
                ".quarantine." in candidate.name
                or ".restore." in candidate.name
                or candidate.name.endswith(".tmp")
                for candidate in directory.iterdir()
            )
        )

    @unittest.skipIf(os.name == "nt", "POSIX renameat2 fallback only")
    @unittest.skipUnless(hasattr(os, "link"), "hard-link fallback required")
    def test_s14_unsupported_renameat2_uses_no_clobber_link_fallback(self) -> None:
        class UnsupportedRename:
            argtypes = None
            restype = None

            def __call__(self, *args: object) -> int:
                ctypes.set_errno(errno.EINVAL)
                return -1

        class UnsupportedLibrary:
            renameat2 = UnsupportedRename()

        directory = self.root / "renameat2-fallback"
        directory.mkdir()
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.addCleanup(os.close, directory_fd)
        source = directory / "source.tmp"
        source.write_bytes(b"candidate")
        with patch("ctypes.CDLL", return_value=UnsupportedLibrary()):
            HOOK._plan_rename_if_absent(source, "published", directory_fd)
        published = directory / "published"
        self.assertFalse(source.exists())
        self.assertEqual(published.read_bytes(), b"candidate")
        self.assertEqual(published.stat().st_nlink, 1)

        guarded_source = directory / "guarded.tmp"
        guarded_source.write_bytes(b"new")
        guarded_target = directory / "guarded"
        guarded_target.write_bytes(b"old")
        with patch("ctypes.CDLL", return_value=UnsupportedLibrary()):
            with self.assertRaises(HOOK.PlanArtifactError) as observed:
                HOOK._plan_rename_if_absent(guarded_source, guarded_target.name, directory_fd)
        self.assertEqual(observed.exception.code, "unsafe_path")
        self.assertEqual(guarded_source.read_bytes(), b"new")
        self.assertEqual(guarded_target.read_bytes(), b"old")

    def test_s03a_long_non_token_text_redaction_is_bounded(self) -> None:
        value = "x" * (HOOK.MAX_PLAN_ARTIFACT_BODY_BYTES * 2)
        self.assertEqual(HOOK.redact_text(value), value)

    def test_s03b_custom_token_keys_remain_redacted(self) -> None:
        text = 'foo_token="first-secret" custom-service-token=second-secret'
        redacted = HOOK.redact_text(text)
        self.assertNotIn("first-secret", redacted)
        self.assertNotIn("second-secret", redacted)
        self.assertGreaterEqual(redacted.count("<redacted>"), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
