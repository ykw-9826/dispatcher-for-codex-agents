"""Bounded, no-fallback adapter for non-interactive Codex CLI agents."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any

from dispatcher_for_codex_agents.agent_harness.contracts import (
    AgentTask,
    FailureCode,
    InvocationResult,
    InvocationStatus,
    ModelProfile,
    SchemaValidationStatus,
    validate_local_identifier,
)
from dispatcher_for_codex_agents.agent_harness.payload import (
    BuiltPayload,
    PayloadBuilder,
    PayloadBuildError,
)
from dispatcher_for_codex_agents.agent_harness.profile import (
    ProfileResolutionError,
    ResolvedSidecarProfile,
    SidecarProfileResolver,
)
from dispatcher_for_codex_agents.agent_harness.schema import (
    OutputSchemaError,
    validate_json_schema,
    validate_schema_definition,
)
from dispatcher_for_codex_agents.agent_harness.shard import ImmutableShardWriter
from dispatcher_for_codex_agents.workspace_paths import temporary_root

from .process_guard import record_invocation_process

_RATE_LIMIT_PATTERN = re.compile(r"(?:\b429\b|rate[ _-]?limit)", re.IGNORECASE)
_PREFILL_PATTERN = re.compile(
    r"prefill.{0,120}(?:parameter|unsupported|invalid)|"
    r"(?:parameter|unsupported|invalid).{0,120}prefill",
    re.IGNORECASE | re.DOTALL,
)
_PARTIAL_PATTERN = re.compile(
    r"partial.{0,120}(?:parameter|unsupported|invalid)|"
    r"(?:parameter|unsupported|invalid).{0,120}partial",
    re.IGNORECASE | re.DOTALL,
)
_SECRET_ENV_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD")
_FORBIDDEN_ITEM_TYPES = {
    "command_execution",
    "computer_use",
    "dynamic_tool_call",
    "file_change",
    "image_generation",
    "mcp_tool_call",
    "tool_call",
    "web_search",
}


@dataclass(frozen=True, slots=True)
class _ParsedEvents:
    events: tuple[dict[str, Any], ...]
    valid_jsonl: bool
    parse_error: str | None
    turn_completed_count: int
    terminal_is_last: bool
    final_text: str | None
    usage: dict[str, int]
    served_models: tuple[str, ...]
    policy_violations: tuple[str, ...]


def _redact_text(text: str, environment: dict[str, str]) -> str:
    redacted = text
    for key, value in environment.items():
        if key.upper().endswith(_SECRET_ENV_SUFFIXES) and len(value) >= 8:
            redacted = redacted.replace(value, f"<REDACTED:{key}>")
    return redacted


def _warning_lines(stderr: str) -> list[str]:
    warnings: list[str] = []
    for line in stderr.splitlines():
        folded = line.casefold()
        if any(token in folded for token in ("warning", "error", "orphan")):
            warnings.append(line[:500])
        if len(warnings) == 20:
            break
    return warnings


def _extract_served_models(event: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    keys = {
        "provider_model",
        "provider_reported_served_model",
        "served_model",
    }
    for source in (event, event.get("metadata")):
        if not isinstance(source, dict):
            continue
        for key in keys:
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                values.add(value.strip())
    return values


def _policy_violations(event: dict[str, Any]) -> list[str]:
    violations: list[str] = []
    event_type = event.get("type")
    if not isinstance(event_type, str):
        return ["event type is missing or non-string"]
    folded_type = event_type.casefold()
    if any(
        token in folded_type
        for token in ("subagent", "spawn_agent", "collaboration", "mcp_tool")
    ):
        violations.append(f"forbidden event type: {event_type}")

    item = event.get("item")
    if isinstance(item, dict):
        item_type = item.get("type")
        if isinstance(item_type, str) and (
            item_type in _FORBIDDEN_ITEM_TYPES or item_type.endswith("_tool_call")
        ):
            violations.append(f"forbidden item type: {item_type}")
        command = item.get("command")
        if isinstance(command, str) and re.search(
            r"(?:^|[\s/])codex(?:\s|$)", command, re.IGNORECASE
        ):
            violations.append("recursive Codex command detected")
    return violations


def _parse_event_stream(stdout: str) -> _ParsedEvents:
    events: list[dict[str, Any]] = []
    parse_error: str | None = None
    for line_number, raw_line in enumerate(stdout.splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            parse_error = f"line {line_number}: {exc.msg}"
            break
        if not isinstance(event, dict):
            parse_error = f"line {line_number}: JSONL event must be an object"
            break
        events.append(event)

    completed = [event for event in events if event.get("type") == "turn.completed"]
    terminal_is_last = bool(events) and events[-1].get("type") == "turn.completed"
    final_text: str | None = None
    usage: dict[str, int] = {}
    served_models: set[str] = set()
    violations: list[str] = []
    for event in events:
        served_models.update(_extract_served_models(event))
        violations.extend(_policy_violations(event))
        item = event.get("item")
        if (
            event.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "agent_message"
            and isinstance(item.get("text"), str)
        ):
            final_text = item["text"]
    if completed:
        raw_usage = completed[-1].get("usage")
        if isinstance(raw_usage, dict):
            usage = {
                key: value
                for key, value in raw_usage.items()
                if isinstance(key, str)
                and isinstance(value, int)
                and not isinstance(value, bool)
            }

    valid_jsonl = parse_error is None and bool(events)
    if len(completed) > 1:
        valid_jsonl = False
        parse_error = "multiple turn.completed events"
    if completed and not terminal_is_last:
        valid_jsonl = False
        parse_error = "turn.completed is not the final JSONL event"
    return _ParsedEvents(
        events=tuple(events),
        valid_jsonl=valid_jsonl,
        parse_error=parse_error,
        turn_completed_count=len(completed),
        terminal_is_last=terminal_is_last,
        final_text=final_text,
        usage=usage,
        served_models=tuple(sorted(served_models)),
        policy_violations=tuple(violations),
    )


class CodexCliAdapter:
    """Invoke one requested Codex sidecar profile with fail-closed controls."""

    adapter_id = "codex_cli"

    def __init__(
        self,
        *,
        executable: str | Sequence[str] = "codex",
        codex_home: str | Path | None = None,
        environment: dict[str, str] | None = None,
        termination_grace_seconds: float = 1.0,
        cancellation: Event | None = None,
    ) -> None:
        if isinstance(executable, str):
            self._executable = (executable,)
        else:
            self._executable = tuple(executable)
        if not self._executable:
            raise ValueError("executable must not be empty.")
        self._resolver = SidecarProfileResolver(codex_home)
        self._environment_overrides = dict(environment or {})
        self._termination_grace_seconds = termination_grace_seconds
        self._calls_started: dict[tuple[str, str], int] = {}
        self._cli_version: str | None = None
        self._cancellation = cancellation or Event()

    def preflight(self, *, task: AgentTask, profile: ModelProfile) -> dict[str, Any]:
        if profile.adapter_id != self.adapter_id:
            raise ValueError("Profile adapter mismatch")
        if shutil.which(self._executable[0]) is None:
            raise ValueError("Codex executable is not available")
        validate_schema_definition(task.expected_output_schema)
        native = profile.capabilities.get("native_output_schema", False)
        if not isinstance(native, bool):
            raise ValueError("native_output_schema must be boolean")
        served = profile.capabilities.get("served_model_allowlist")
        if served is not None and (
            not isinstance(served, list)
            or not served
            or not all(isinstance(item, str) and item for item in served)
        ):
            raise ValueError("served_model_allowlist must be a nonempty string array")
        resolved = self._resolver.resolve(profile.profile_id)
        return {
            "configured_model": resolved.configured_model,
            "configured_provider": resolved.configured_provider,
            "command": list(
                self.build_command(
                    profile=profile,
                    isolated_working_directory=Path("EPHEMERAL_ISOLATED_DIRECTORY"),
                    schema_path=Path("output_schema.json") if native else None,
                )
            ),
        }

    @property
    def calls_started(self) -> int:
        """Return the number of agent subprocesses started by this adapter."""
        return sum(self._calls_started.values())

    def _environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment.update(self._environment_overrides)
        for key in tuple(environment):
            if key in {"PWD", "OLDPWD"} or key.startswith("DCA_NOTIFY_"):
                environment.pop(key, None)
        environment["CODEX_HOME"] = str(self._resolver.codex_home)
        return environment

    def _read_cli_version(self, environment: dict[str, str]) -> str:
        if self._cli_version is not None:
            return self._cli_version
        try:
            completed = subprocess.run(
                [*self._executable, "--version"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                timeout=10,
            )
            version = (completed.stdout or completed.stderr).strip()
            self._cli_version = version if version else "NOT_REPORTED"
        except (OSError, subprocess.SubprocessError):
            self._cli_version = "NOT_REPORTED"
        return self._cli_version

    def build_command(
        self,
        *,
        profile: ModelProfile,
        isolated_working_directory: Path,
        schema_path: Path | None,
    ) -> tuple[str, ...]:
        """Build the only permitted fresh, ephemeral `codex exec` command."""
        command = [
            *self._executable,
            "--strict-config",
            "--profile",
            profile.profile_id,
            "--sandbox",
            "read-only",
            "--ask-for-approval",
            "never",
            "--cd",
            str(isolated_working_directory),
            "--disable",
            "multi_agent",
            "--disable",
            "shell_tool",
            "--disable",
            "unified_exec",
            "--disable",
            "plugins",
            "--disable",
            "apps",
            "--disable",
            "hooks",
            "--disable",
            "browser_use",
            "--disable",
            "computer_use",
            "--disable",
            "image_generation",
            "--config",
            "mcp_servers={}",
            "--config",
            "notify=[]",
            "exec",
            "--ephemeral",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--json",
            "--color",
            "never",
        ]
        if schema_path is not None:
            command.extend(["--output-schema", str(schema_path)])
        command.append("-")
        return tuple(command)

    def _terminate_process_group(
        self, process: subprocess.Popen[str]
    ) -> tuple[str, str]:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except ProcessLookupError:
            pass
        try:
            return process.communicate(timeout=self._termination_grace_seconds)
        except subprocess.TimeoutExpired:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
            return process.communicate()

    @staticmethod
    def _provenance(
        *,
        profile: ModelProfile,
        resolved: ResolvedSidecarProfile | None,
        cli_version: str,
        served_model: str,
        native_output_schema: bool,
        agent_process_started: bool,
    ) -> dict[str, Any]:
        return {
            "adapter_id": profile.adapter_id,
            "approval": "never",
            "configured_model": (
                resolved.configured_model if resolved is not None else "NOT_REPORTED"
            ),
            "configured_provider": (
                resolved.configured_provider if resolved is not None else "NOT_REPORTED"
            ),
            "codex_cli_version": cli_version,
            "ephemeral": True,
            "fallback_profiles": [],
            "multi_agent": False,
            "native_output_schema_requested": native_output_schema,
            "payload_transport": "stdin",
            "provider_reported_served_model": served_model,
            "requested_profile": profile.profile_id,
            "agent_process_started": agent_process_started,
            "agent_subprocess_count": int(agent_process_started),
            "resume": False,
            "agent_working_directory": "EPHEMERAL_ISOLATED_DIRECTORY",
            "sandbox": "read-only",
        }

    def _write_prelaunch_failure(
        self,
        *,
        writer: ImmutableShardWriter,
        task: AgentTask,
        profile: ModelProfile,
        payload: BuiltPayload | None,
        resolved: ResolvedSidecarProfile | None,
        cli_version: str,
        failure_code: FailureCode,
        detail: str,
    ) -> InvocationResult:
        environment = self._environment()
        redacted_detail = _redact_text(detail, environment)
        result = InvocationResult(
            status=InvocationStatus.FAILURE,
            exit_code=None,
            turn_completed=False,
            final_output=None,
            schema_validation_status=SchemaValidationStatus.NOT_RUN,
            usage={},
            provenance=self._provenance(
                profile=profile,
                resolved=resolved,
                cli_version=cli_version,
                served_model="NOT_REPORTED",
                native_output_schema=False,
                agent_process_started=False,
            ),
            warnings=(redacted_detail, FailureCode.SERVED_MODEL_NOT_REPORTED.value),
            failure_code=failure_code,
            latency_seconds=0.0,
        )
        writer.write(
            task=task,
            profile=profile,
            input_records=payload.input_records if payload is not None else (),
            events_jsonl="",
            stderr_log=redacted_detail + "\n",
            final_output=None,
            result=result,
        )
        return result

    def invoke(
        self,
        *,
        task: AgentTask,
        profile: ModelProfile,
        attempt_id: str,
        workers_root: str | Path,
        payload_builder: PayloadBuilder | None = None,
    ) -> InvocationResult:
        """Run one bounded agent call and always emit one terminal shard."""
        validate_local_identifier(attempt_id, field_name="attempt_id")
        writer = ImmutableShardWriter(
            workers_root=workers_root,
            task_id=task.task_id,
            profile_id=profile.profile_id,
            attempt_id=attempt_id,
        )
        environment = self._environment()
        cli_version = self._read_cli_version(environment)
        if self._cancellation.is_set():
            return self._write_prelaunch_failure(
                writer=writer,
                task=task,
                profile=profile,
                payload=None,
                resolved=None,
                cli_version=cli_version,
                failure_code=FailureCode.CANCELLED,
                detail="Cancelled before launch",
            )
        if profile.adapter_id != self.adapter_id:
            return self._write_prelaunch_failure(
                writer=writer,
                task=task,
                profile=profile,
                payload=None,
                resolved=None,
                cli_version=cli_version,
                failure_code=FailureCode.PROFILE_CONFIGURATION_INVALID,
                detail=(
                    f"Profile adapter_id={profile.adapter_id!r} does not match "
                    f"{self.adapter_id!r}."
                ),
            )
        try:
            validate_schema_definition(task.expected_output_schema)
        except OutputSchemaError as exc:
            return self._write_prelaunch_failure(
                writer=writer,
                task=task,
                profile=profile,
                payload=None,
                resolved=None,
                cli_version=cli_version,
                failure_code=FailureCode.OUTPUT_SCHEMA_INVALID,
                detail=str(exc),
            )

        builder = payload_builder or PayloadBuilder()
        try:
            payload = builder.build(task)
        except (OSError, PayloadBuildError) as exc:
            return self._write_prelaunch_failure(
                writer=writer,
                task=task,
                profile=profile,
                payload=None,
                resolved=None,
                cli_version=cli_version,
                failure_code=FailureCode.PAYLOAD_INVALID,
                detail=str(exc),
            )
        try:
            resolved = self._resolver.resolve(profile.profile_id)
        except ProfileResolutionError as exc:
            return self._write_prelaunch_failure(
                writer=writer,
                task=task,
                profile=profile,
                payload=payload,
                resolved=None,
                cli_version=cli_version,
                failure_code=FailureCode.PROFILE_CONFIGURATION_INVALID,
                detail=str(exc),
            )

        call_key = (task.task_id, profile.profile_id)
        prior_calls = self._calls_started.get(call_key, 0)
        if prior_calls >= task.call_limit:
            return self._write_prelaunch_failure(
                writer=writer,
                task=task,
                profile=profile,
                payload=payload,
                resolved=resolved,
                cli_version=cli_version,
                failure_code=FailureCode.CALL_LIMIT_EXCEEDED,
                detail=(
                    f"call_limit={task.call_limit} already reached for "
                    f"task/profile {call_key!r}."
                ),
            )

        native_output_schema = bool(
            profile.capabilities.get("native_output_schema", False)
        )
        started = time.monotonic()
        stdout = ""
        stderr = ""
        exit_code: int | None = None
        timed_out = False
        cancelled = False
        process_started = False
        try:
            with tempfile.TemporaryDirectory(
                prefix="dca-agent-", dir=temporary_root()
            ) as temporary:
                isolated = Path(temporary)
                schema_path: Path | None = None
                if native_output_schema:
                    schema_path = isolated / "output_schema.json"
                    schema_path.write_text(
                        json.dumps(
                            task.expected_output_schema,
                            ensure_ascii=False,
                            indent=2,
                            sort_keys=True,
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    schema_path.chmod(0o444)
                command = self.build_command(
                    profile=profile,
                    isolated_working_directory=isolated,
                    schema_path=schema_path,
                )
                record_invocation_process(
                    Path(workers_root),
                    task.task_id,
                    profile.profile_id,
                    attempt_id,
                    child_pid=None,
                )
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=environment,
                    cwd=isolated,
                    start_new_session=True,
                )
                process_started = True
                self._calls_started[call_key] = prior_calls + 1
                try:
                    record_invocation_process(
                        Path(workers_root),
                        task.task_id,
                        profile.profile_id,
                        attempt_id,
                        child_pid=process.pid,
                    )
                    input_text: str | None = payload.content
                    deadline = time.monotonic() + task.timeout
                    while True:
                        if self._cancellation.is_set():
                            cancelled = True
                            stdout, stderr = self._terminate_process_group(process)
                            break
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            timed_out = True
                            stdout, stderr = self._terminate_process_group(process)
                            break
                        try:
                            stdout, stderr = process.communicate(
                                input_text, timeout=min(remaining, 0.2)
                            )
                            break
                        except subprocess.TimeoutExpired:
                            input_text = None
                except KeyboardInterrupt:
                    self._cancellation.set()
                    cancelled = True
                    stdout, stderr = self._terminate_process_group(process)
                except Exception:
                    self._terminate_process_group(process)
                    raise
                exit_code = process.returncode
        except OSError as exc:
            stderr = str(exc)
        latency = time.monotonic() - started

        redacted_stdout = _redact_text(stdout or "", environment)
        redacted_stderr = _redact_text(stderr or "", environment)
        parsed = _parse_event_stream(redacted_stdout)

        final_output: Any | None = None
        schema_status = SchemaValidationStatus.NOT_RUN
        schema_error: str | None = None
        if parsed.final_text is not None:
            try:
                final_output = json.loads(parsed.final_text)
            except json.JSONDecodeError as exc:
                final_output = parsed.final_text
                schema_status = SchemaValidationStatus.FAIL
                schema_error = str(exc)
            else:
                try:
                    validate_json_schema(final_output, task.expected_output_schema)
                    schema_status = SchemaValidationStatus.PASS
                except OutputSchemaError as exc:
                    schema_status = SchemaValidationStatus.FAIL
                    schema_error = str(exc)

        served_model = (
            parsed.served_models[0]
            if len(parsed.served_models) == 1
            else "NOT_REPORTED"
        )
        warnings = _warning_lines(redacted_stderr)
        if served_model == "NOT_REPORTED":
            warnings.append(FailureCode.SERVED_MODEL_NOT_REPORTED.value)
        if parsed.parse_error:
            warnings.append(parsed.parse_error)
        warnings.extend(parsed.policy_violations)
        if schema_error:
            warnings.append(schema_error)

        error_events = "\n".join(
            json.dumps(event, ensure_ascii=False, sort_keys=True)
            for event in parsed.events
            if event.get("type") in {"error", "turn.failed"}
        )
        combined_errors = redacted_stderr + "\n" + error_events
        failure_code: FailureCode | None
        if cancelled:
            failure_code = FailureCode.CANCELLED
        elif timed_out:
            failure_code = FailureCode.TIMEOUT
        elif _RATE_LIMIT_PATTERN.search(combined_errors):
            failure_code = FailureCode.PROVIDER_RATE_LIMIT_429
        elif _PREFILL_PATTERN.search(combined_errors):
            failure_code = FailureCode.PROVIDER_PREFILL_PARAMETER_ERROR
        elif _PARTIAL_PATTERN.search(combined_errors):
            failure_code = FailureCode.PROVIDER_PARTIAL_PARAMETER_ERROR
        elif not process_started:
            failure_code = FailureCode.CLI_NONZERO_EXIT
        elif not parsed.valid_jsonl:
            failure_code = FailureCode.EVENT_STREAM_INVALID
        elif parsed.policy_violations:
            failure_code = FailureCode.POLICY_VIOLATION
        elif exit_code != 0:
            failure_code = FailureCode.CLI_NONZERO_EXIT
        elif parsed.turn_completed_count == 0:
            failure_code = FailureCode.TURN_COMPLETED_MISSING
        elif len(parsed.served_models) > 1:
            failure_code = FailureCode.SILENT_FALLBACK_DETECTED
        else:
            allowed_served = profile.capabilities.get(
                "served_model_allowlist", [resolved.configured_model]
            )
            if not isinstance(allowed_served, list) or not all(
                isinstance(value, str) for value in allowed_served
            ):
                failure_code = FailureCode.PROFILE_CONFIGURATION_INVALID
                warnings.append("served_model_allowlist must be a string array")
            elif served_model != "NOT_REPORTED" and served_model not in allowed_served:
                failure_code = FailureCode.SILENT_FALLBACK_DETECTED
                warnings.append(
                    f"served model {served_model!r} is not in the declared allowlist"
                )
            elif parsed.final_text is None:
                failure_code = FailureCode.FINAL_OUTPUT_MISSING
            elif schema_status != SchemaValidationStatus.PASS:
                failure_code = FailureCode.OUTPUT_SCHEMA_INVALID
            else:
                failure_code = None

        status = (
            InvocationStatus.SUCCESS
            if failure_code is None
            else InvocationStatus.FAILURE
        )
        result = InvocationResult(
            status=status,
            exit_code=exit_code,
            turn_completed=parsed.turn_completed_count == 1,
            final_output=final_output,
            schema_validation_status=schema_status,
            usage=parsed.usage,
            provenance=self._provenance(
                profile=profile,
                resolved=resolved,
                cli_version=cli_version,
                served_model=served_model,
                native_output_schema=native_output_schema,
                agent_process_started=process_started,
            ),
            warnings=tuple(dict.fromkeys(warnings)),
            failure_code=failure_code,
            latency_seconds=latency,
        )
        writer.write(
            task=task,
            profile=profile,
            input_records=payload.input_records,
            events_jsonl=redacted_stdout,
            stderr_log=redacted_stderr,
            final_output=final_output,
            result=result,
        )
        return result
