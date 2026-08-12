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

    def test_removed_version_discovers_latest_installed_wrapper(self) -> None:
        cache_parent = self.root / "version cache"
        latest_root = cache_parent / HOOK.WRITER_VERSION
        scripts = latest_root / "scripts"
        scripts.mkdir(parents=True)
        shutil.copy2(PLUGIN_ROOT / "scripts" / "orchestrator_hook.py", scripts)
        shutil.copy2(PLUGIN_ROOT / "scripts" / "run_orchestrator_hook.ps1", scripts)
        removed_root = cache_parent / "1.0.16"
        recovered_data = self.root / "recovered data"
        result = self.run_command_windows(
            {
                "hook_event_name": "SessionStart",
                "session_id": "removed-version",
                "hook_run_id": "removed-version",
                "source": "startup",
            },
            env=self.environment(plugin_root=removed_root, data=recovered_data),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("hookSpecificOutput", json.loads(result.stdout))
        states = list((recovered_data / "sessions").glob("*.json"))
        self.assertEqual(len(states), 1)
        state = json.loads(states[0].read_text(encoding="utf-8"))
        self.assertEqual(state["writer_version"], HOOK.WRITER_VERSION)

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
        events = [
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": session,
                "hook_run_id": "executor-objective",
                "model": "gpt-5.6-sol",
                "prompt": "排查 Android 设备反复重启并修复、编译部署实机验证",
            },
            {
                "hook_event_name": "Stop",
                "session_id": session,
                "hook_run_id": "executor-plan",
                "last_assistant_message": (
                    "1. 收集日志并定位根因\n2. 修改对应模块并编译部署\n"
                    "3. 完成实机验证与回滚检查\n验收：问题不再复现。\n"
                    "计划已就绪，等待确认后执行"
                ),
            },
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": session,
                "hook_run_id": "executor-confirm",
                "prompt": "确认按这个计划执行",
            },
        ]
        for payload in events:
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
                        f"plan_generation={state['plan_generation']}. Exclusive execution ownership; "
                        "implement the full actionable plan and run verification acceptance tests."
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
