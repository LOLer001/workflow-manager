#!/usr/bin/env python3
"""Read-only trust health check for Workflow Manager lifecycle hooks."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, TextIO


PLUGIN_ID = "workflow-manager@workflow-manager"
EXPECTED_HOOK_COUNT = 9
HEALTHY_TRUST_STATUSES = frozenset({"trusted", "managed"})
REVIEW_TRUST_STATUSES = frozenset({"untrusted", "modified"})
DEFAULT_TIMEOUT_SECONDS = 15.0
_EOF = object()
DISPATCH_RECEIPT_SCHEMA = 1
DISPATCH_RECEIPT_MAX_BYTES = 4096
DISPATCH_STALE_SECONDS = 15 * 60
DISPATCH_WRITER_VERSION = "1.0.49"
DISPATCH_STATE_SCHEMA = 29
DISPATCH_EXECUTION_PROFILE = "11"
DISPATCH_STABLE_SKILL_SCHEMA = 9
DISPATCH_RUNNER_KINDS = frozenset({"posix_direct", "posix_cached", "windows_py", "windows_python"})


class DoctorError(RuntimeError):
    """An operational or protocol error that makes the check inconclusive."""


class DoctorArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: invalid arguments\n")


class _StdoutReader:
    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self.lines: queue.Queue[str | object] = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            while True:
                line = self._stream.readline()
                if line == "":
                    break
                self.lines.put(line)
        finally:
            self.lines.put(_EOF)

    def close(self) -> None:
        try:
            self._stream.close()
        except OSError:
            pass
        self._thread.join(timeout=0.2)


def _resolve_cli(explicit: str | None) -> str:
    if explicit is None:
        resolved = shutil.which("codex")
        if resolved is None:
            raise DoctorError("Codex CLI not found on PATH; use --codex-cli PATH")
        return resolved

    candidate = Path(explicit).expanduser()
    if candidate.is_file():
        return str(candidate.resolve())
    if not any(separator in explicit for separator in (os.sep, os.altsep) if separator):
        resolved = shutil.which(explicit)
        if resolved is not None:
            return resolved
    raise DoctorError("Codex CLI executable not found")


def _resolve_cwd(value: str | None) -> Path:
    candidate = Path(value).expanduser() if value is not None else Path.cwd()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise DoctorError("working directory is unavailable") from exc
    if not resolved.is_dir():
        raise DoctorError("working directory is not a directory")
    return resolved


def _popen(cli: str, cwd: Path) -> subprocess.Popen[str]:
    options: dict[str, Any] = {}
    if os.name == "nt":
        options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        options["start_new_session"] = True
    try:
        return subprocess.Popen(
            [cli, "app-server", "--stdio"],
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="strict",
            bufsize=1,
            **options,
        )
    except (OSError, ValueError) as exc:
        raise DoctorError("failed to start Codex CLI") from exc


def _signal_process(process: subprocess.Popen[str], *, force: bool) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            # A configured Codex CLI may be a .cmd launcher. Terminating only the
            # wrapper leaves its Python/app-server child alive, which keeps pipes
            # and temporary directories open past the doctor's deadline. Kill the
            # exact Windows process tree without invoking a shell.
            system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
            taskkill = system_root / "System32" / "taskkill.exe"
            command = [str(taskkill), "/PID", str(process.pid), "/T"]
            if force:
                command.append("/F")
            try:
                subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=1.5,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except (OSError, subprocess.TimeoutExpired):
                (process.kill if force else process.terminate)()
        else:
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.stdin is not None:
        try:
            process.stdin.close()
        except OSError:
            pass
    if process.poll() is not None:
        return
    try:
        process.wait(timeout=0.4)
        return
    except subprocess.TimeoutExpired:
        pass
    _signal_process(process, force=False)
    try:
        process.wait(timeout=0.8)
        return
    except subprocess.TimeoutExpired:
        pass
    _signal_process(process, force=True)
    try:
        process.wait(timeout=0.8)
    except subprocess.TimeoutExpired:
        # Popen.kill() is the strongest portable primitive available on Windows.
        try:
            process.kill()
            process.wait(timeout=0.4)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _send(process: subprocess.Popen[str], message: Mapping[str, Any]) -> None:
    if process.stdin is None:
        raise DoctorError("Codex app-server stdin is unavailable")
    try:
        process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        process.stdin.flush()
    except (BrokenPipeError, OSError) as exc:
        code = process.poll()
        detail = f" (exit code {code})" if code is not None else ""
        raise DoctorError(f"Codex app-server closed its input{detail}") from exc


def _rpc_error(method: str, error: Any) -> DoctorError:
    if not isinstance(error, Mapping):
        return DoctorError(f"{method} failed with a malformed RPC error")
    code = error.get("code")
    if isinstance(code, int) and not isinstance(code, bool):
        return DoctorError(f"{method} failed (code {code})")
    return DoctorError(f"{method} failed")


def _receive_response(
    process: subprocess.Popen[str],
    reader: _StdoutReader,
    request_id: int,
    method: str,
    deadline: float,
) -> Any:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DoctorError(f"timed out waiting for {method}")
        try:
            line = reader.lines.get(timeout=remaining)
        except queue.Empty as exc:
            raise DoctorError(f"timed out waiting for {method}") from exc
        if line is _EOF:
            code = process.poll()
            detail = f" (exit code {code})" if code is not None else ""
            raise DoctorError(f"Codex app-server ended before {method} responded{detail}")
        assert isinstance(line, str)
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise DoctorError(f"invalid JSON received while waiting for {method}") from exc
        if not isinstance(message, Mapping):
            raise DoctorError(f"invalid RPC message received while waiting for {method}")
        if message.get("id") != request_id:
            if "id" not in message and isinstance(message.get("method"), str):
                continue
            raise DoctorError(f"unexpected RPC message received while waiting for {method}")
        if "error" in message:
            raise _rpc_error(method, message["error"])
        if "result" not in message:
            raise DoctorError(f"RPC response for {method} has no result")
        return message["result"]


def _run_rpc(cli: str, cwd: Path, timeout_seconds: float) -> Any:
    process = _popen(cli, cwd)
    if process.stdout is None:
        _stop_process(process)
        raise DoctorError("Codex app-server stdout is unavailable")
    reader = _StdoutReader(process.stdout)
    deadline = time.monotonic() + timeout_seconds
    try:
        _send(
            process,
            {
                "method": "initialize",
                "id": 1,
                "params": {
                    "clientInfo": {
                        "name": "workflow_manager_hook_trust_doctor",
                        "title": "Workflow Manager Hook Trust Doctor",
                        "version": "1",
                    }
                },
            },
        )
        _receive_response(process, reader, 1, "initialize", deadline)
        _send(process, {"method": "initialized"})
        _send(
            process,
            {
                "method": "hooks/list",
                "id": 2,
                "params": {"cwds": [str(cwd)]},
            },
        )
        return _receive_response(process, reader, 2, "hooks/list", deadline)
    finally:
        _stop_process(process)
        reader.close()


def _matching_cwd_entry(result: Any, cwd: Path) -> Mapping[str, Any]:
    if not isinstance(result, Mapping) or not isinstance(result.get("data"), list):
        raise DoctorError("hooks/list returned an invalid result")
    entries = result["data"]
    requested = os.path.normcase(os.path.abspath(str(cwd)))
    matching = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise DoctorError("hooks/list returned an invalid cwd entry")
        entry_cwd = entry.get("cwd")
        if isinstance(entry_cwd, str):
            normalized = os.path.normcase(os.path.abspath(entry_cwd))
            if normalized == requested:
                matching.append(entry)
    if len(matching) != 1:
        raise DoctorError("hooks/list did not return exactly one entry for the requested cwd")
    entry = matching[0]
    errors = entry.get("errors", [])
    if not isinstance(errors, list):
        raise DoctorError("hooks/list returned an invalid errors field")
    if errors:
        raise DoctorError("hook discovery reported one or more errors")
    if not isinstance(entry.get("hooks"), list):
        raise DoctorError("hooks/list returned an invalid hooks field")
    return entry


def _project_hooks(result: Any, cwd: Path) -> list[dict[str, Any]]:
    entry = _matching_cwd_entry(result, cwd)
    selected = []
    for hook in entry["hooks"]:
        if not isinstance(hook, Mapping):
            raise DoctorError("hooks/list returned an invalid hook entry")
        if hook.get("pluginId") != PLUGIN_ID:
            continue
        key = hook.get("key")
        event = hook.get("eventName")
        enabled = hook.get("enabled")
        trust_status = hook.get("trustStatus")
        current_hash = hook.get("currentHash")
        if not isinstance(key, str) or not key:
            raise DoctorError("Workflow Manager hook has an invalid key")
        if not isinstance(event, str) or not event:
            raise DoctorError("Workflow Manager hook has an invalid event")
        if not isinstance(enabled, bool):
            raise DoctorError("Workflow Manager hook has an invalid enabled state")
        if trust_status not in HEALTHY_TRUST_STATUSES | REVIEW_TRUST_STATUSES:
            raise DoctorError("Workflow Manager hook has an invalid trust status")
        if not isinstance(current_hash, str) or not current_hash:
            raise DoctorError("Workflow Manager hook has an invalid current hash")
        selected.append(
            {
                "key": key,
                "event": event,
                "enabled": enabled,
                "trustStatus": trust_status,
                "currentHash": current_hash,
            }
        )

    if len(selected) != EXPECTED_HOOK_COUNT:
        raise DoctorError(
            f"expected {EXPECTED_HOOK_COUNT} Workflow Manager hooks, found {len(selected)}"
        )
    keys = [hook["key"] for hook in selected]
    if len(set(keys)) != len(keys):
        raise DoctorError("Workflow Manager hook keys are not unique")
    return sorted(selected, key=lambda hook: (hook["event"], hook["key"]))


def _needs_review(hooks: list[dict[str, Any]]) -> bool:
    return any(
        not hook["enabled"] or hook["trustStatus"] in REVIEW_TRUST_STATUSES
        for hook in hooks
    )


def _print_report(hooks: list[dict[str, Any]], *, as_json: bool, dispatch: dict[str, Any] | None = None) -> int:
    review = _needs_review(hooks)
    status = "review_required" if review else "ok"
    if as_json:
        print(
            json.dumps(
                {
                    "pluginId": PLUGIN_ID,
                    "status": status,
                    "configuration_status": status,
                    "dispatch_status": dispatch or {"status": "not_requested"},
                    "count": len(hooks),
                    "hooks": hooks,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    else:
        label = "REVIEW REQUIRED" if review else "OK"
        print(f"Workflow Manager hook trust: {label} ({len(hooks)}/{EXPECTED_HOOK_COUNT})")
        print("key\tevent\tenabled\ttrustStatus\tcurrentHash")
        for hook in hooks:
            print(
                f"{hook['key']}\t{hook['event']}\t{str(hook['enabled']).lower()}\t"
                f"{hook['trustStatus']}\t{hook['currentHash']}"
            )
        dispatch_status = (dispatch or {"status": "not_requested"})["status"]
        print(f"configuration_status: {status}")
        print(f"dispatch_status: {dispatch_status}")
    return 2 if review else 0


def _receipt_path(plugin_data: str, session: str) -> Path:
    token = hashlib.sha256(("workflow-manager-dispatch-receipt-v1\0" + session).encode("utf-8")).hexdigest()[:32]
    root = Path(plugin_data).expanduser().resolve(strict=True)
    if not root.is_dir() or root.is_symlink():
        raise DoctorError("plugin data is unavailable")
    return root / "dispatch-receipts" / f"{token}.json"


def _parse_since(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DoctorError("--since must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise DoctorError("--since must include a timezone")
    return parsed.astimezone(timezone.utc)


def _dispatch_status(plugin_data: str | None, session: str | None, since: datetime | None, required: list[str]) -> dict[str, Any]:
    if session is None:
        return {"status": "current_session_unavailable"}
    if not plugin_data:
        raise DoctorError("--plugin-data is required with --session")
    path = _receipt_path(plugin_data, session)
    try:
        info = path.lstat()
        if path.is_symlink() or not path.is_file() or info.st_size > DISPATCH_RECEIPT_MAX_BYTES:
            return {"status": "receipt_invalid"}
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping) or raw.get("schema") != DISPATCH_RECEIPT_SCHEMA:
            return {"status": "receipt_invalid"}
        at = raw.get("at")
        events = raw.get("events")
        timeline = raw.get("timeline")
        if raw.get("source") != "hook":
            return {"status": "source_mismatch"}
        if (raw.get("writer_version") != DISPATCH_WRITER_VERSION
                or raw.get("state_schema") != DISPATCH_STATE_SCHEMA
                or raw.get("execution_profile") != DISPATCH_EXECUTION_PROFILE
                or raw.get("stable_skill_schema") != DISPATCH_STABLE_SKILL_SCHEMA):
            return {"status": "runtime_mismatch"}
        if raw.get("runner_kind") not in DISPATCH_RUNNER_KINDS:
            return {"status": "runtime_mismatch"}
        fingerprints = ("plugin_root_fingerprint", "source_fingerprint", "stable_skill_fingerprint")
        if any(not isinstance(raw.get(key), str) or len(raw[key]) not in {7, 32} for key in fingerprints):
            return {"status": "receipt_invalid"}
        if (not isinstance(at, str) or not isinstance(events, list)
                or not all(x in ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PreCompact", "PostCompact", "SubagentStart", "SubagentStop", "Stop") for x in events)
                or not isinstance(timeline, list) or len(timeline) != len(events)
                or raw.get("event_count") != len(events)):
            return {"status": "receipt_invalid"}
        for index, item in enumerate(timeline):
            if (not isinstance(item, Mapping) or item.get("event") != events[index]
                    or not isinstance(item.get("at"), str) or not isinstance(item.get("run"), str)
                    or len(item["run"]) != 32):
                return {"status": "receipt_invalid"}
            _parse_since(item["at"])
        observed = _parse_since(at)
        now = datetime.now(timezone.utc)
        if since is not None and observed < since:
            return {"status": "stale"}
        if (now - observed).total_seconds() > DISPATCH_STALE_SECONDS:
            return {"status": "stale"}
        cursor = 0
        for expected in required:
            try:
                cursor = events.index(expected, cursor) + 1
            except ValueError:
                return {"status": "event_missing"}
        return {"status": "ok", "schema": DISPATCH_RECEIPT_SCHEMA, "event_count": len(events), "runner_kind": raw["runner_kind"]}
    except FileNotFoundError:
        return {"status": "receipt_missing"}
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {"status": "receipt_invalid"}


def _print_error(message: str, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"status": "error", "error": message}, ensure_ascii=False))
    else:
        print(f"hook trust doctor: {message}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = DoctorArgumentParser(
        prog="hook_trust_doctor",
        description="Read Workflow Manager hook trust state from Codex app-server."
    )
    parser.add_argument("--codex-cli", help="Explicit path to the Codex CLI executable.")
    parser.add_argument("--cwd", help="Working directory whose effective hooks are checked.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    parser.add_argument("--session", help="Explicit session identifier to check; never inferred.")
    parser.add_argument("--plugin-data", help="Explicit plugin-data root required for --session.")
    parser.add_argument("--since", help="RFC3339 lower freshness bound for the dispatch receipt.")
    parser.add_argument("--require-event", action="append", default=[], choices=("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PreCompact", "PostCompact", "SubagentStart", "SubagentStop", "Stop"), help="Required receipt event; may be repeated in order.")
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        metavar="SECONDS",
        help=f"Overall RPC timeout (default: {DEFAULT_TIMEOUT_SECONDS:g} seconds).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if not (args.timeout > 0):
            raise DoctorError("--timeout must be greater than zero")
        cli = _resolve_cli(args.codex_cli)
        cwd = _resolve_cwd(args.cwd)
        hooks = _project_hooks(_run_rpc(cli, cwd, args.timeout), cwd)
        dispatch = _dispatch_status(args.plugin_data, args.session, _parse_since(args.since), args.require_event)
        configuration_exit = _print_report(hooks, as_json=args.json, dispatch=dispatch)
        return 3 if configuration_exit == 0 and dispatch["status"] != "ok" else configuration_exit
    except DoctorError as exc:
        _print_error(str(exc), as_json=args.json)
        return 1
    except KeyboardInterrupt:
        _print_error("interrupted", as_json=args.json)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
