from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PLUGIN_ROOT / "scripts" / "hook_trust_doctor.py"
PLUGIN_ID = "workflow-manager@workflow-manager"
EVENTS = (
    "session_start",
    "user_prompt_submit",
    "pre_tool_use",
    "post_tool_use",
    "pre_compact",
    "post_compact",
    "subagent_start",
    "subagent_stop",
    "stop",
)


FAKE_SERVER = r'''
import json
import os
from pathlib import Path
import sys
import time

mode = os.environ.get("FAKE_MODE", "trusted")
record_path = Path(os.environ["FAKE_RECORD"])
events = json.loads(os.environ["FAKE_EVENTS"])
plugin_id = os.environ["FAKE_PLUGIN_ID"]
received = []

def record():
    record_path.write_text(
        json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "received": received}),
        encoding="utf-8",
    )

def emit(value):
    print(json.dumps(value), flush=True)

for line in sys.stdin:
    message = json.loads(line)
    received.append(message)
    record()
    method = message.get("method")
    if method == "initialize":
        if mode == "timeout":
            time.sleep(30)
        elif mode == "bad_json":
            print("this is not json", flush=True)
            time.sleep(30)
        elif mode == "rpc_error":
            emit({
                "id": message["id"],
                "error": {
                    "code": -32603,
                    "message": "SECRET_RPC_COMMAND /secret/rpc/source/hooks.json",
                },
            })
        else:
            emit({"id": message["id"], "result": {"serverInfo": {"name": "fake"}}})
    elif method == "initialized":
        continue
    elif method == "hooks/list":
        cwd = message["params"]["cwds"][0]
        hooks = []
        for index, event in enumerate(events):
            trust = "modified" if mode == "modified" and index == 4 else "trusted"
            owner = "someone-else@example" if mode == "no_plugin" else plugin_id
            hooks.append({
                "key": f"workflow-manager:{event}:0:0",
                "eventName": event,
                "enabled": True,
                "trustStatus": trust,
                "currentHash": f"sha256:{index:02d}",
                "pluginId": owner,
                "command": "SECRET_COMMAND_MUST_NOT_LEAK",
                "sourcePath": "/secret/source/path/hooks.json",
            })
        emit({
            "id": message["id"],
            "result": {
                "data": [{"cwd": cwd, "hooks": hooks, "warnings": [], "errors": []}]
            },
        })
        record()
        break
'''


class HookTrustDoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.record = self.root / "record.json"
        fake_python = self.root / "fake_codex.py"
        fake_python.write_text(textwrap.dedent(FAKE_SERVER), encoding="utf-8")
        if os.name == "nt":
            self.fake_cli = self.root / "fake_codex.cmd"
            self.fake_cli.write_text(
                f'@"{sys.executable}" "{fake_python}" %*\r\n', encoding="utf-8"
            )
        else:
            self.fake_cli = self.root / "fake_codex"
            self.fake_cli.write_text(
                f"#!{sys.executable}\n" + fake_python.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            self.fake_cli.chmod(self.fake_cli.stat().st_mode | stat.S_IXUSR)

    def run_doctor(
        self,
        mode: str,
        *,
        timeout: float = 2.0,
        cwd: Path | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            {
                "FAKE_MODE": mode,
                "FAKE_RECORD": str(self.record),
                "FAKE_EVENTS": json.dumps(EVENTS),
                "FAKE_PLUGIN_ID": PLUGIN_ID,
            }
        )
        if extra_env:
            env.update(extra_env)
        target_cwd = cwd or self.root
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--codex-cli",
                str(self.fake_cli),
                "--cwd",
                str(target_cwd),
                "--timeout",
                str(timeout),
                "--json",
            ],
            cwd=str(PLUGIN_ROOT),
            env=env,
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )

    def read_record(self) -> dict[str, object]:
        return json.loads(self.record.read_text(encoding="utf-8"))

    def test_nine_trusted_hooks_exit_zero_and_output_is_redacted(self) -> None:
        result = self.run_doctor("trusted")

        self.assertEqual(result.returncode, 3, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["pluginId"], PLUGIN_ID)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["configuration_status"], "ok")
        self.assertEqual(payload["dispatch_status"]["status"], "current_session_unavailable")
        self.assertEqual(payload["count"], 9)
        self.assertEqual(len(payload["hooks"]), 9)
        for hook in payload["hooks"]:
            self.assertEqual(
                set(hook), {"key", "event", "enabled", "trustStatus", "currentHash"}
            )
            self.assertTrue(hook["enabled"])
            self.assertEqual(hook["trustStatus"], "trusted")
        combined_output = result.stdout + result.stderr
        self.assertNotIn("SECRET_COMMAND_MUST_NOT_LEAK", combined_output)
        self.assertNotIn("/secret/source/path", combined_output)

    def test_modified_hook_requires_review_and_exits_two(self) -> None:
        result = self.run_doctor("modified")

        self.assertEqual(result.returncode, 2, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "review_required")
        self.assertEqual(
            [hook["trustStatus"] for hook in payload["hooks"]].count("modified"), 1
        )

    def test_absent_workflow_manager_plugin_is_an_error(self) -> None:
        result = self.run_doctor("no_plugin")

        self.assertEqual(result.returncode, 1, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "error")
        self.assertIn("expected 9", payload["error"])
        self.assertIn("found 0", payload["error"])

    def test_timeout_and_bad_json_are_bounded_protocol_errors(self) -> None:
        for mode, expected in (("timeout", "timed out"), ("bad_json", "invalid JSON")):
            with self.subTest(mode=mode):
                started = time.monotonic()
                result = self.run_doctor(mode, timeout=0.2)
                elapsed = time.monotonic() - started

                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertLess(elapsed, 3.0)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["status"], "error")
                self.assertIn(expected, payload["error"])

    def test_wire_protocol_and_process_arguments_are_strictly_read_only(self) -> None:
        target_cwd = self.root / "workspace"
        target_cwd.mkdir()
        result = self.run_doctor("trusted", cwd=target_cwd)

        self.assertEqual(result.returncode, 3, result.stderr)
        record = self.read_record()
        self.assertEqual(record["argv"], ["app-server", "--stdio"])
        self.assertEqual(Path(str(record["cwd"])).resolve(), target_cwd.resolve())
        received = record["received"]
        self.assertEqual(
            [message["method"] for message in received],
            ["initialize", "initialized", "hooks/list"],
        )
        self.assertEqual(received[2]["params"], {"cwds": [str(target_cwd.resolve())]})
        wire = json.dumps(received).lower()
        self.assertNotIn("config/", wire)
        self.assertNotIn("bypass", wire)
        source = SCRIPT.read_text(encoding="utf-8").lower()
        self.assertNotIn("config/" + "batchwrite", source)
        self.assertNotIn("dangerously-" + "bypass-hook-trust", source)

    def test_missing_default_cli_fails_clearly(self) -> None:
        env = os.environ.copy()
        env["PATH"] = ""
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--json"],
            cwd=str(PLUGIN_ROOT),
            env=env,
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )

        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "error")
        self.assertIn("not found on PATH", payload["error"])
        self.assertIn("--codex-cli", payload["error"])

    def test_errors_do_not_leak_cli_cwd_rpc_or_argument_secrets(self) -> None:
        secret_cli = str(self.root / "SECRET_CLI_PATH" / "codex")
        secret_cwd = self.root / "SECRET_CWD_PATH"
        cases = [
            subprocess.run(
                [sys.executable, str(SCRIPT), "--codex-cli", secret_cli, "--json"],
                cwd=str(PLUGIN_ROOT),
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            ),
            self.run_doctor("trusted", cwd=secret_cwd),
            self.run_doctor("rpc_error"),
            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--timeout",
                    "/secret/argument/path",
                ],
                cwd=str(PLUGIN_ROOT),
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            ),
        ]
        secrets = (
            secret_cli,
            str(secret_cwd),
            "SECRET_RPC_COMMAND",
            "/secret/rpc/source/hooks.json",
            "/secret/argument/path",
            str(SCRIPT),
        )
        for result in cases:
            with self.subTest(output=result.stdout + result.stderr):
                self.assertEqual(result.returncode, 1)
                combined = result.stdout + result.stderr
                for secret in secrets:
                    self.assertNotIn(secret, combined)


if __name__ == "__main__":
    unittest.main()
