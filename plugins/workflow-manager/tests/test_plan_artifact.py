from __future__ import annotations

import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
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
        )

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
                    "reasoning_effort": "ultra",
                    "fork_turns": "none",
                },
            },
            data=selected,
        )
        self.assertNotIn(
            "permissionDecision",
            json.loads(accepted.stdout or "{}").get("hookSpecificOutput", {}),
        )
        agent_id = f"{suffix}-assessor"
        self.run_hook(
            {
                "hook_event_name": "SubagentStart",
                "session_id": session,
                "hook_run_id": f"{suffix}-start",
                "agent_id": agent_id,
                "model": "gpt-5.6-sol",
                "reasoning_effort": "ultra",
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
                "prompt": "修复 Android 跨模块故障、编译验证，但不要使用任何子智能体",
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
        self.assertRegex(path.name, rf"^hard-plan-g0001-{state['plan_digest']}\.md$")

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
        self.assertEqual(state["plan_artifact"]["content_digest"], HOOK.plan_artifact_body_digest(text))

    def test_m04_header_edits_do_not_drift_but_body_edits_do_not_authorize(self) -> None:
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
        self.run_hook({"hook_event_name": "SessionStart", "session_id": "m04", "hook_run_id": "header", "source": "resume"})
        self.assertEqual(self.state()["plan_artifact"]["write_status"], "written")
        path.write_bytes(path.read_bytes() + b"\nunauthorized body edit\n")
        drift = self.run_hook({"hook_event_name": "SessionStart", "session_id": "m04", "hook_run_id": "body", "source": "resume"})
        observed = self.state()
        self.assertEqual(observed["plan_artifact"]["write_status"], "content_drift")
        self.assertEqual(observed["plan_digest"], original_plan)
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

    def test_m06_write_failure_is_typed_and_same_plan_can_retry(self) -> None:
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
        self.assertEqual(failed["plan_state"], "awaiting_confirmation")
        self.assertEqual(failed["plan_artifact"]["write_status"], "write_failed")
        generation = failed["plan_generation"]
        self.assertIn("write_failed", first.stdout)
        (self.data / "plans").unlink()
        payload.update({"hook_run_id": "retry-stop"})
        retry = self.run_hook(payload)
        recovered = self.state()
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(recovered["plan_artifact"]["write_status"], "written")
        self.assertEqual(recovered["plan_generation"], generation)

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
        self.assertEqual(state["plan_state"], "awaiting_confirmation")
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

    def test_m10_retention_keeps_current_plus_five_owned_old_files(self) -> None:
        session = "m10"
        self.begin_parent_plan(session)
        for generation in range(1, 8):
            if generation > 1:
                path = self.state_path()
                state = json.loads(path.read_text(encoding="utf-8"))
                state["plan_state"] = "analyzing"
                path.write_text(json.dumps(state), encoding="utf-8")
            state = self.accept_parent_plan(session, self.hard_body(f"g{generation}"), run_id=f"plan-{generation}")
            if generation == 1:
                session_dir = self.artifact_path(state).parent
                (session_dir / f"hard-plan-g0000-{'0' * 32}.md").write_text("user file", encoding="utf-8")
        owned = [path for path in session_dir.glob("*.md") if path.read_text(encoding="utf-8").startswith("<!-- workflow-manager-plan-artifact:v1")]
        self.assertEqual(len(owned), 6)
        self.assertTrue((session_dir / f"hard-plan-g0000-{'0' * 32}.md").exists())
        self.assertTrue(self.artifact_path(state).exists())

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
                "objective_fingerprint",
                "difficulty_decision_id",
                "plan_digest",
                "content_digest",
                "generation",
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
            rf"^plans/[A-Za-z0-9._-]+-[0-9a-f]{{16}}/hard-plan-g0001-{state['plan_digest']}\.md$",
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
            HOOK._atomic_write_plan_file(symbolic, b"changed")
        hard = directory / "hard.md"
        os.link(source, hard)
        with self.assertRaises(HOOK.PlanArtifactError):
            HOOK._atomic_write_plan_file(hard, b"changed")
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
        raw = "1. safe plan\n" + "\n".join(markers) + "\n\u202e\u2066hidden\u2069\n" + ("x" * (HOOK.MAX_PLAN_ARTIFACT_BODY_BYTES * 2))
        body = HOOK.sanitize_plan_artifact_body(raw)
        for marker in markers:
            self.assertNotIn(marker, body)
        for control in ("\u202e", "\u2066", "\u2069"):
            self.assertNotIn(control, body)
        self.assertLessEqual(len(body.encode("utf-8")), HOOK.MAX_PLAN_ARTIFACT_BODY_BYTES)

    def test_s04_replace_failure_preserves_old_file_and_removes_temporary(self) -> None:
        directory = self.root / "atomic"
        directory.mkdir()
        target = directory / "plan.md"
        target.write_text("old-content", encoding="utf-8")
        with patch.object(HOOK.os, "replace", side_effect=OSError("simulated")):
            with self.assertRaises(OSError):
                HOOK._atomic_write_plan_file(target, b"new-content")
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
        self.assertIn(state["plan_digest"], files[0].name)

    @unittest.skipUnless(hasattr(os, "link") and hasattr(os, "symlink"), "link support required")
    def test_s06_retention_uses_generation_and_preserves_forged_links(self) -> None:
        payload = {"session_id": "s06"}
        environment = patch.dict(os.environ, {"PLUGIN_DATA": str(self.data), "CODEX_HOME": str(self.codex_home)})
        environment.start()
        self.addCleanup(environment.stop)
        for generation in range(1, 9):
            state = HOOK.new_state(payload)
            state.update(
                {
                    "plan_state": "awaiting_confirmation",
                    "plan_generation": generation,
                    "plan_digest": f"{generation:032x}",
                    "plan_objective_fingerprint": "a" * 16,
                    "plan_difficulty_decision_id": "b" * 24,
                }
            )
            HOOK.write_plan_artifact(state, payload, self.hard_body(f"g{generation}"))
            if generation == 1:
                directory = self.data / "plans" / HOOK.plan_artifact_session_id("s06")
                first = next(directory.glob("*.md"))
                hard = directory / f"hard-plan-g9998-{'8' * 32}.md"
                os.link(first, hard)
                forged = directory / f"hard-plan-g9999-{'9' * 32}.md"
                forged.write_text("forged", encoding="utf-8")
                linked = directory / f"hard-plan-g9997-{'7' * 32}.md"
                self.symlink_or_skip(linked, forged)
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
        self.assertEqual(first["plan_artifact"]["lifecycle_status"], "executing")

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
                "plan_generation": 1,
                "plan_digest": "9" * 32,
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

        def swap_then_unlink(path: Path, identity: tuple[int, int, int], directory_fd: int | None) -> bool:
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
            removed = original(path, identity, directory_fd)
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
            HOOK.write_plan_artifact(state, payload, self.hard_body("swap-retention"))
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
                "plan_generation": 1,
                "plan_digest": "2" * 32,
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
        ) -> bool:
            names = (
                os.listdir(directory_fd)
                if directory_fd is not None
                else [candidate.name for candidate in path.parent.iterdir()]
            )
            observed_quarantine_counts.append(
                sum(".quarantine." in name for name in names)
            )
            return original(path, identity, directory_fd)

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
