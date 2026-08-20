from __future__ import annotations

import base64
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PLUGIN_ROOT / "scripts" / "orchestrator_hook.py"
SPEC = importlib.util.spec_from_file_location("windows_orchestrator_hook", SCRIPT)
assert SPEC and SPEC.loader
HOOK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HOOK)
HOOKS = PLUGIN_ROOT / "hooks" / "hooks.json"
WINDOWS_RESOLVER = PLUGIN_ROOT / "scripts" / "resolve_orchestrator_hook.ps1"
RESOLVER_TEXT = WINDOWS_RESOLVER.read_text(encoding="utf-8").replace("\r\n", "\n")
POWERSHELL_COMMAND = (
    "powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass "
    "-EncodedCommand "
    + base64.b64encode(RESOLVER_TEXT.encode("utf-16le")).decode("ascii")
)
EXPECTED_WINDOWS_COMMAND = (
    'cmd.exe /d /c "if defined TOKEN_FRUGAL_DEBUG ('
    + POWERSHELL_COMMAND
    + ") else ("
    + POWERSHELL_COMMAND
    + ' 2>NUL)"'
)


@unittest.skipUnless(os.name == "nt", "native Windows test")
class WindowsHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="token-frugal-windows-")
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        self.driver = self.root / "invoke-command-windows.cmd"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def command(self) -> list[str]:
        comspec = os.environ.get("COMSPEC", str(Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe"))
        return [comspec, "/d", "/c", str(self.driver)]

    def resolved_command(self, env: dict[str, str]) -> str:
        return EXPECTED_WINDOWS_COMMAND.replace("${PLUGIN_ROOT}", env["PLUGIN_ROOT"])

    def prepare_driver(self) -> None:
        command = EXPECTED_WINDOWS_COMMAND.replace("${PLUGIN_ROOT}", "%PLUGIN_ROOT%")
        self.driver.write_text(f"@echo off\n{command}\n", encoding="ascii")

    def environment(
        self,
        *,
        plugin_root: str | Path = PLUGIN_ROOT,
        path: str | None = None,
        data: Path | None = None,
    ) -> dict[str, str]:
        env = os.environ.copy()
        env["PLUGIN_ROOT"] = str(plugin_root)
        env["PLUGIN_DATA"] = str(data or self.data)
        env["CODEX_HOME"] = str(self.root / ".codex")
        env["PYTHONUTF8"] = "1"
        if path is not None:
            env["PATH"] = path
        return env

    def run_command_windows(
        self,
        payload: dict,
        *,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        resolved_env = env or self.environment()
        self.prepare_driver()
        return subprocess.run(
            self.command(),
            input=json.dumps(payload, ensure_ascii=True),
            text=True,
            capture_output=True,
            env=resolved_env,
            # These tests exercise the complete cmd -> PowerShell -> launcher chain. Hosted
            # runners can cold-start any link slowly, including the intentional no-Python
            # fail-open path, so the timeout is a harness allowance rather than a product SLA.
            timeout=45,
        )

    def test_plan_directory_guard_blocks_rename_until_handles_close(self) -> None:
        root = self.data
        session = HOOK.plan_artifact_session_id("windows-handle-guard")
        directory = root / "plans" / session
        renamed = self.root / "renamed-plan-session"
        with HOOK.plan_session_directory_guard(root, session) as guard:
            guard["verify"]()
            with self.assertRaises(OSError):
                directory.rename(renamed)
            self.assertTrue(directory.is_dir())
        directory.rename(renamed)
        self.assertTrue(renamed.is_dir())

    def test_all_declared_events_use_same_windows_command(self) -> None:
        hooks = json.loads(HOOKS.read_text(encoding="utf-8"))["hooks"]
        commands = [
            hook["commandWindows"]
            for matchers in hooks.values()
            for matcher in matchers
            for hook in matcher["hooks"]
        ]
        self.assertEqual(len(commands), 9)
        self.assertEqual(set(commands), {EXPECTED_WINDOWS_COMMAND})

    def test_hook_stdout_is_ascii_safe_for_non_utf8_windows_code_pages(self) -> None:
        source = (PLUGIN_ROOT / "scripts" / "orchestrator_hook.py").read_text(encoding="utf-8")
        for function in ("emit_pretool_deny", "emit_posttool_advisory", "emit_context"):
            start = source.index(f"def {function}")
            end = source.find("\ndef ", start + 4)
            body = source[start : end if end >= 0 else None]
            self.assertIn("ensure_ascii=True", body, function)

    def test_missing_wrapper_fails_open(self) -> None:
        missing_root = self.root / "removed plugin cache"
        missing_root.mkdir()
        result = self.run_command_windows(
            {"hook_event_name": "Stop", "session_id": "missing-wrapper"},
            env=self.environment(plugin_root=missing_root),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_exact_plugin_root_runs_and_missing_root_never_scans_siblings(self) -> None:
        exact_data = self.root / "exact data"
        exact = self.run_command_windows(
            {
                "hook_event_name": "SessionStart",
                "session_id": "exact-root",
                "hook_run_id": "exact-root",
                "source": "startup",
            },
            env=self.environment(plugin_root=PLUGIN_ROOT, data=exact_data),
        )
        self.assertEqual(exact.returncode, 0, exact.stderr)
        self.assertIn("hookSpecificOutput", json.loads(exact.stdout))
        exact_states = list((exact_data / "sessions").glob("*.json"))
        self.assertEqual(len(exact_states), 1)
        exact_state = json.loads(exact_states[0].read_text(encoding="utf-8"))
        self.assertEqual(exact_state["writer_version"], HOOK.WRITER_VERSION)

        cache_parent = self.root / "version cache"
        sibling_runner = cache_parent / HOOK.WRITER_VERSION / "scripts" / "run_orchestrator_hook.ps1"
        sibling_runner.parent.mkdir(parents=True)
        sibling_runner.write_text(
            '$marker = $env:WORKFLOW_MANAGER_FAKE_MARKER\n'
            'if ($marker) { Set-Content -LiteralPath $marker -Value "executed" -Encoding ascii }\n',
            encoding="utf-8",
        )
        missing_root = cache_parent / "1.0.16"
        missing_data = self.root / "missing data"
        marker = self.root / "fake-runner-executed.txt"
        env = self.environment(plugin_root=missing_root, data=missing_data)
        env["WORKFLOW_MANAGER_FAKE_MARKER"] = str(marker)
        env.pop("TOKEN_FRUGAL_DEBUG", None)
        missing = self.run_command_windows(
            {
                "hook_event_name": "Stop",
                "session_id": "missing-exact-root",
                "hook_run_id": "missing-exact-root",
            },
            env=env,
        )
        self.assertEqual(missing.returncode, 0, missing.stderr)
        self.assertEqual((missing.stdout, missing.stderr), ("", ""))
        self.assertFalse(marker.exists())
        self.assertFalse((missing_data / "sessions").exists())

        debug_env = env.copy()
        debug_env["TOKEN_FRUGAL_DEBUG"] = "1"
        debug = self.run_command_windows(
            {
                "hook_event_name": "Stop",
                "session_id": "missing-exact-root-debug",
                "hook_run_id": "missing-exact-root-debug",
            },
            env=debug_env,
        )
        self.assertEqual(debug.returncode, 0)
        self.assertEqual(debug.stdout, "")
        self.assertEqual(debug.stderr, "workflow_manager_hook: runner_missing\n")
        self.assertNotIn(str(missing_root), debug.stderr)
        self.assertNotIn(str(sibling_runner), debug.stderr)
        self.assertFalse(marker.exists())

        wrapper_only_root = self.root / "wrapper without source"
        scripts = wrapper_only_root / "scripts"
        scripts.mkdir(parents=True)
        shutil.copy2(PLUGIN_ROOT / "scripts" / "run_orchestrator_hook.ps1", scripts)
        result = self.run_command_windows(
            {"hook_event_name": "Stop", "session_id": "missing-source"},
            env=self.environment(plugin_root=wrapper_only_root),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_py_launcher_runs_all_nine_events(self) -> None:
        self.assertIsNotNone(shutil.which("py.exe"))
        cases = [
            ("SessionStart", {"source": "startup"}),
            ("UserPromptSubmit", {"prompt": "全面测试并优化多个模块"}),
            ("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "pwd"}}),
            (
                "PostToolUse",
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": "pwd"},
                    "tool_response": {"exit_code": 0},
                },
            ),
            ("PreCompact", {"trigger": "auto"}),
            ("PostCompact", {"trigger": "auto"}),
            ("SubagentStart", {"agent_id": "a1", "agent_type": "default"}),
            ("SubagentStop", {"agent_id": "a1", "last_assistant_message": "done"}),
            ("Stop", {"last_assistant_message": "done"}),
        ]
        for index, (event, extra) in enumerate(cases):
            with self.subTest(event=event):
                result = self.run_command_windows(
                    {
                        "hook_event_name": event,
                        "session_id": "windows-nine-events",
                        "hook_run_id": f"windows-{index}",
                        **extra,
                    }
                )
                self.assertEqual(result.returncode, 0, result.stderr)
        states = list((self.data / "sessions").glob("*.json"))
        self.assertEqual(len(states), 1)
        state = json.loads(states[0].read_text(encoding="utf-8"))
        self.assertEqual(state["schema_version"], HOOK.SCHEMA_VERSION)
        # PreToolUse without a duplicate is intentionally read-only.
        self.assertEqual(len(state["processed_hook_runs"]), 8)
        self.assertEqual(sum(state["event_counts"].values()), 8)
        self.assertEqual(list((self.data / "sessions").glob("*.tmp")), [])
        stable_skill = self.root / ".codex" / "skills" / "workflow-manager" / "SKILL.md"
        self.assertTrue(stable_skill.is_file())
        self.assertEqual(
            stable_skill.read_bytes(),
            (
                PLUGIN_ROOT
                / "assets"
                / "stable-skill"
                / "workflow-manager"
                / "SKILL.md"
            ).read_bytes(),
        )

    def test_python_fallback_handles_spaces_and_unicode(self) -> None:
        fake_root = self.root / "插件 root with spaces"
        scripts = fake_root / "scripts"
        scripts.mkdir(parents=True)
        shutil.copy2(PLUGIN_ROOT / "scripts" / "orchestrator_hook.py", scripts)
        shutil.copy2(PLUGIN_ROOT / "scripts" / "run_orchestrator_hook.ps1", scripts)

        python_dir = str(Path(sys.executable).parent)
        system32 = str(Path(os.environ["SystemRoot"]) / "System32")
        powershell_dir = str(Path(system32) / "WindowsPowerShell" / "v1.0")
        env = self.environment(
            plugin_root=fake_root,
            path=os.pathsep.join((python_dir, powershell_dir, system32)),
        )
        self.assertIsNone(shutil.which("py.exe", path=env["PATH"]))
        self.assertIsNotNone(shutil.which("python.exe", path=env["PATH"]))
        result = self.run_command_windows(
            {
                "hook_event_name": "SessionStart",
                "session_id": "中文 session with spaces",
                "hook_run_id": "fallback",
                "source": "startup",
            },
            env=env,
        )
        driver_bytes = self.driver.read_bytes()
        self.assertTrue(driver_bytes.isascii())
        driver_text = driver_bytes.decode("ascii")
        self.assertIn("-EncodedCommand", driver_text)
        self.assertNotIn(str(fake_root), driver_text)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("hookSpecificOutput", json.loads(result.stdout))
        self.assertEqual(len(list((self.data / "sessions").glob("*.json"))), 1)

    def test_no_python_fails_open(self) -> None:
        system32 = str(Path(os.environ["SystemRoot"]) / "System32")
        powershell_dir = str(Path(system32) / "WindowsPowerShell" / "v1.0")
        restricted_path = os.pathsep.join((powershell_dir, system32))
        self.assertIsNone(shutil.which("py.exe", path=restricted_path))
        self.assertIsNone(shutil.which("python.exe", path=restricted_path))
        result = self.run_command_windows(
            {"hook_event_name": "Stop", "session_id": "no-python"},
            env=self.environment(path=restricted_path),
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_extended_length_plugin_root_is_supported(self) -> None:
        extended_root = "\\\\?\\" + str(PLUGIN_ROOT)
        extended_data = self.root / "extended-data"
        result = self.run_command_windows(
            {
                "hook_event_name": "SessionStart",
                "session_id": "extended-root",
                "hook_run_id": "extended-root",
                "source": "startup",
            },
            env=self.environment(plugin_root=extended_root, data=extended_data),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("hookSpecificOutput", json.loads(result.stdout))
        self.assertEqual(len(list((extended_data / "sessions").glob("*.json"))), 1)

    def test_active_powershell_shell_executes_windows_command(self) -> None:
        powershell = Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        extended_root = "\\\\?\\" + str(PLUGIN_ROOT)
        powershell_data = self.root / "powershell-data"
        env = self.environment(plugin_root=extended_root, data=powershell_data)
        payload = {
            "hook_event_name": "SessionStart",
            "session_id": "powershell-shell",
            "hook_run_id": "powershell-shell",
            "source": "startup",
        }
        result = subprocess.run(
            [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", self.resolved_command(env)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env=env,
            # Hosted Windows runners may cold-start nested Windows PowerShell and the Python
            # launcher much more slowly than a developer workstation. This is an end-to-end
            # compatibility probe, not a responsiveness SLA.
            timeout=45,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("hookSpecificOutput", json.loads(result.stdout))
        self.assertEqual(len(list((powershell_data / "sessions").glob("*.json"))), 1)

    def test_concurrent_windows_commands_preserve_operations(self) -> None:
        env = self.environment(data=self.root / "concurrent-data")
        env["TOKEN_FRUGAL_DEBUG"] = "1"
        self.prepare_driver()
        processes: list[subprocess.Popen[str]] = []
        for index in range(12):
            payload = {
                "hook_event_name": "PostToolUse",
                "session_id": "windows-concurrent",
                "hook_run_id": f"concurrent-{index}",
                "tool_name": "Bash",
                "tool_input": {"command": f"echo {index}"},
                "tool_response": {"exit_code": 0},
            }
            process = subprocess.Popen(
                self.command(),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            assert process.stdin
            process.stdin.write(json.dumps(payload))
            process.stdin.close()
            processes.append(process)
        diagnostics: list[str] = []
        for process in processes:
            stderr = process.stderr.read() if process.stderr else ""
            diagnostics.append(stderr)
            process.wait(timeout=45)
            if process.stderr:
                process.stderr.close()
            self.assertEqual(process.returncode, 0, stderr)
        debug_text = "\n".join(diagnostics)
        self.assertNotIn("persist=lock_timeout", debug_text, debug_text)
        self.assertNotIn("persist=write_error", debug_text, debug_text)
        self.assertNotIn("persist=read_error", debug_text, debug_text)
        self.assertEqual(debug_text.count("persist=written"), 12, debug_text)
        states = list((Path(env["PLUGIN_DATA"]) / "sessions").glob("*.json"))
        self.assertEqual(len(states), 1)
        state = json.loads(states[0].read_text(encoding="utf-8"))
        self.assertEqual(len(state["operations"]), 12)
        self.assertEqual(len({item["fingerprint"] for item in state["operations"]}), 12)
        self.assertEqual(state["event_counts"]["PostToolUse"], 12)

    def test_windows_guard_denies_unc_git_and_preserves_large_output(self) -> None:
        denied = self.run_command_windows(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "windows-guard",
                "hook_run_id": "unc-git",
                "cwd": r"\\server\share\repo",
                "tool_name": "Bash",
                "tool_input": {"cmd": "git.exe status"},
            }
        )
        self.assertEqual(denied.returncode, 0, denied.stderr)
        denied_output = json.loads(denied.stdout)
        self.assertEqual(denied_output["hookSpecificOutput"]["permissionDecision"], "deny")

        compacted = self.run_command_windows(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "windows-large-output",
                "hook_run_id": "large-output",
                "tool_name": "Bash",
                "tool_input": {"command": "gradlew.bat test > build.log 2>&1"},
                "tool_response": {"exit_code": 1, "output": "failure line\n" * 320},
            }
        )
        self.assertEqual(compacted.returncode, 0, compacted.stderr)
        preserved_output = json.loads(compacted.stdout)
        self.assertTrue(preserved_output["continue"])
        self.assertNotIn("decision", preserved_output)
        context = preserved_output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("preserved the original", context)
        self.assertIn("Correctness and evidence completeness take priority", context)
        state_files = list((self.data / "sessions").glob("*.json"))
        states = [json.loads(path.read_text(encoding="utf-8")) for path in state_files]
        operations = [item for state in states for item in state.get("operations", [])]
        self.assertEqual(len(operations), 1)
        self.assertTrue(operations[0]["oversized"])
        self.assertFalse(operations[0]["compacted"])
        self.assertNotIn("failure line", json.dumps(states))

    def test_confirmed_executor_contract_roundtrip_is_ascii_safe(self) -> None:
        session = "windows-executor-contract"
        events = [{
            "hook_event_name": "UserPromptSubmit",
            "session_id": session,
            "hook_run_id": "executor-objective",
            "model": "gpt-5.6-sol",
            "prompt": "排查 Android 设备反复重启并修复、编译部署实机验证",
        }]
        for payload in events:
            result = self.run_command_windows(payload)
            self.assertEqual(result.returncode, 0, result.stderr)
            if result.stdout:
                self.assertTrue(result.stdout.isascii())
                json.loads(result.stdout)
        state = json.loads(next((self.data / "sessions").glob("*.json")).read_text(encoding="utf-8"))
        binding = state["assessor_binding_id"]
        assessor_request = self.run_command_windows({
            "hook_event_name": "PreToolUse", "session_id": session,
            "hook_run_id": "executor-assessor-request", "tool_name": "collaboration.spawn_agent",
            "tool_input": {"task_name": "high_assessor", "model": "gpt-5.6-sol", "reasoning_effort": "ultra", "fork_turns": "none", "message": (
                f"assessor_binding_id={binding} objective_fingerprint={state['objective']['fingerprint']} "
                "profile_resolution=highest_available assess Simple directly solve and verify; Hard read-only plan then confirmation"
            )},
        })
        self.assertEqual(assessor_request.returncode, 0, assessor_request.stderr)
        self.assertNotIn(
            "permissionDecision",
            json.loads(assessor_request.stdout or "{}").get("hookSpecificOutput", {}),
        )
        requested_state = json.loads(next((self.data / "sessions").glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(requested_state["assessor_state"], "spawn_pending")
        self.assertEqual(requested_state["subagents"][-1]["role"], "high_assessor")
        for payload in (
            {"hook_event_name": "SubagentStart", "session_id": session, "hook_run_id": "executor-assessor-start", "agent_id": "windows-executor-assessor", "model": "gpt-5.6-sol"},
            {"hook_event_name": "SubagentStop", "session_id": session, "hook_run_id": "executor-assessor-stop", "agent_id": "windows-executor-assessor", "status": "completed", "last_assistant_message": (
                "1. 收集日志并定位根因\n2. 修改对应模块并编译部署\n3. 完成实机验证与回滚检查\n"
                "验收：问题不再复现。\n"
                f"WORK_ASSESSMENT binding_id={binding} outcome=hard evidence_digest={'a' * 32}\n"
                "计划已就绪，等待确认后执行"
            )},
            {"hook_event_name": "UserPromptSubmit", "session_id": session, "hook_run_id": "executor-confirm", "prompt": "确认按这个计划执行"},
        ):
            result = self.run_command_windows(payload)
            self.assertEqual(result.returncode, 0, result.stderr)
            if result.stdout:
                self.assertTrue(result.stdout.isascii())
                json.loads(result.stdout)
        state = json.loads(next((self.data / "sessions").glob("*.json")).read_text(encoding="utf-8"))
        contract_id = state["execution_contract_id"]
        request = self.run_command_windows(
            {
                "hook_event_name": "PreToolUse",
                "session_id": session,
                "hook_run_id": "executor-request",
                "tool_name": "collaboration.spawn_agent",
                "tool_input": {
                    "task_name": "execute_confirmed_plan",
                    "model": "gpt-5.6-terra",
                    "reasoning_effort": "medium",
                    "fork_turns": "none",
                    "message": (
                        "Unique exclusive executor. "
                        f"execution_contract_id={contract_id} plan_digest={state['plan_digest']} "
                        f"plan_generation={state['plan_generation']}. "
                        "Reread the canonical journal before execution: "
                        f"relative_path={state['plan_artifact']['relative_path']} "
                        f"current_revision_digest={state['plan_artifact']['current_revision_digest']} "
                        f"journal_digest={state['plan_artifact']['journal_digest']}. "
                        "Exclusive execution ownership; "
                        "implement the full actionable plan and run verification acceptance tests.\n"
                        f"EXECUTION_RESULT execution_contract_id={contract_id} outcome=succeeded|failed evidence_digest=<32hex>"
                    ),
                },
            }
        )
        self.assertEqual(request.returncode, 0, request.stderr)
        self.assertTrue(request.stdout.isascii())
        request_output = json.loads(request.stdout)["hookSpecificOutput"]
        self.assertNotIn("permissionDecision", request_output)
        final_state = json.loads(next((self.data / "sessions").glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(final_state["executor_state"], "spawn_pending")
        self.assertEqual(final_state["executor_model"], "gpt-5.6-terra")

    def test_causal_review_roundtrip_is_fingerprint_only_and_ascii_safe(self) -> None:
        session = "windows-causal-review"
        initial_events = [
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": session,
                "hook_run_id": "causal-objective",
                "model": "gpt-5.6-sol",
                "prompt": "排查 Android 设备反复重启并修复、编译部署实机验证",
            },
        ]
        for payload in initial_events:
            result = self.run_command_windows(payload)
            self.assertEqual(result.returncode, 0, result.stderr)
            if result.stdout:
                self.assertTrue(result.stdout.isascii())
                json.loads(result.stdout)

        state_path = next((self.data / "sessions").glob("*.json"))
        state = json.loads(state_path.read_text(encoding="utf-8"))
        binding = state["assessor_binding_id"]
        assessor = self.run_command_windows({
            "hook_event_name": "PreToolUse", "session_id": session,
            "hook_run_id": "causal-assessor-request", "tool_name": "collaboration.spawn_agent",
            "tool_input": {"task_name": "high_assessor", "model": "gpt-5.6-sol", "reasoning_effort": "ultra", "fork_turns": "none", "message": (
                f"assessor_binding_id={binding} objective_fingerprint={state['objective']['fingerprint']} "
                "profile_resolution=highest_available assess Simple directly solve and verify; Hard read-only plan then confirmation"
            )},
        })
        self.assertEqual(assessor.returncode, 0, assessor.stderr)
        self.assertNotIn(
            "permissionDecision",
            json.loads(assessor.stdout or "{}").get("hookSpecificOutput", {}),
        )
        requested_state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(requested_state["assessor_state"], "spawn_pending")
        self.assertEqual(requested_state["subagents"][-1]["role"], "high_assessor")
        for payload in (
            {"hook_event_name": "SubagentStart", "session_id": session, "hook_run_id": "causal-assessor-start", "agent_id": "windows-causal-assessor", "model": "gpt-5.6-sol"},
            {"hook_event_name": "SubagentStop", "session_id": session, "hook_run_id": "causal-assessor-stop", "agent_id": "windows-causal-assessor", "status": "completed", "last_assistant_message": (
                "1. 收集日志并定位根因\n2. 修改对应模块并编译部署\n3. 完成实机验证与回滚检查\n"
                "验收：问题不再复现。\n"
                f"WORK_ASSESSMENT binding_id={binding} outcome=hard evidence_digest={'b' * 32}\n"
                "计划已就绪，等待确认后执行"
            )},
            {"hook_event_name": "UserPromptSubmit", "session_id": session, "hook_run_id": "causal-confirm", "prompt": "确认按这个计划执行"},
        ):
            result = self.run_command_windows(payload)
            self.assertEqual(result.returncode, 0, result.stderr)
            if result.stdout:
                self.assertTrue(result.stdout.isascii())
                json.loads(result.stdout)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        contract_id = state["execution_contract_id"]
        execution_events = [
            {
                "hook_event_name": "PreToolUse",
                "session_id": session,
                "hook_run_id": "causal-executor-request",
                "tool_name": "collaboration.spawn_agent",
                "tool_input": {
                    "task_name": "execute_confirmed_plan",
                    "model": "gpt-5.6-terra",
                    "reasoning_effort": "medium",
                    "fork_turns": "none",
                    "message": (
                        "Unique exclusive executor. "
                        f"execution_contract_id={contract_id} plan_digest={state['plan_digest']} "
                        f"plan_generation={state['plan_generation']}. "
                        "Reread the canonical journal before execution: "
                        f"relative_path={state['plan_artifact']['relative_path']} "
                        f"current_revision_digest={state['plan_artifact']['current_revision_digest']} "
                        f"journal_digest={state['plan_artifact']['journal_digest']}. "
                        "Exclusive execution ownership; "
                        "implement the full actionable plan and run verification acceptance tests.\n"
                        f"EXECUTION_RESULT execution_contract_id={contract_id} outcome=succeeded|failed evidence_digest=<32hex>"
                    ),
                },
            },
            {
                "hook_event_name": "SubagentStart",
                "session_id": session,
                "hook_run_id": "causal-executor-start",
                "agent_id": "windows-causal-executor",
            },
            {
                "hook_event_name": "PostToolUse",
                "session_id": session,
                "hook_run_id": "causal-change",
                "agent_id": "windows-causal-executor",
                "tool_name": "apply_patch",
                "tool_input": {"patch": "*** Begin Patch\n*** End Patch"},
                "tool_response": {"status": "completed"},
            },
            {
                "hook_event_name": "PostToolUse",
                "session_id": session,
                "hook_run_id": "causal-verification",
                "agent_id": "windows-causal-executor",
                "tool_name": "Bash",
                "tool_input": {"command": "py -3 -m unittest tests.test_reboot"},
                "tool_response": {"exit_code": 0, "output": "1 test passed"},
            },
            {
                "hook_event_name": "SubagentStop",
                "session_id": session,
                "hook_run_id": "causal-executor-stop",
                "agent_id": "windows-causal-executor",
                "status": "completed",
                "last_assistant_message": "implementation and verification complete",
            },
        ]
        for payload in execution_events:
            result = self.run_command_windows(payload)
            self.assertEqual(result.returncode, 0, result.stderr)
            if result.stdout:
                self.assertTrue(result.stdout.isascii())
                json.loads(result.stdout)

        feedback = "验收发现修复后新增黑屏，请检查是不是刚才改动导致"
        submitted = self.run_command_windows(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": session,
                "hook_run_id": "causal-feedback",
                "prompt": feedback,
            }
        )
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        self.assertTrue(submitted.stdout.isascii())
        json.loads(submitted.stdout)
        triage = json.loads(state_path.read_text(encoding="utf-8"))
        baseline = triage["last_execution_baseline"]
        review = triage["causal_review"]
        self.assertEqual(review["state"], "triage_required")
        self.assertEqual(review["baseline_id"], baseline["baseline_id"])
        self.assertRegex(review["review_id"], r"^[0-9a-f]{32}$")
        self.assertNotIn(feedback, json.dumps(triage, ensure_ascii=False))

        resolved_result = self.run_command_windows(
            {
                "hook_event_name": "Stop",
                "session_id": session,
                "hook_run_id": "causal-conclusion",
                "last_assistant_message": (
                    "CAUSAL_REVIEW "
                    f"baseline_id={baseline['baseline_id']} review_id={review['review_id']} "
                    f"outcome=introduced evidence_digest={'c' * 32}"
                ),
            }
        )
        self.assertEqual(resolved_result.returncode, 0, resolved_result.stderr)
        self.assertTrue(resolved_result.stdout.isascii())
        json.loads(resolved_result.stdout)
        resolved = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(resolved["causal_review"]["state"], "resolved")
        self.assertEqual(resolved["causal_review"]["outcome"], "introduced")
        self.assertEqual(resolved["plan_state"], "analyzing")
        self.assertIsNone(resolved["execution_contract_id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
