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
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event
from typing import Any

from dispatcher_for_codex_agents.agent_harness.capabilities import (
    CapabilityError,
    InternalRuntimeGrant,
    SelectedRuntimeProtectionScope,
    event_violation,
    host_overrides,
    policy_record,
    redacted_command,
    validate_paths,
)
from dispatcher_for_codex_agents.agent_harness.contracts import (
    AgentTask,
    CapabilityPolicy,
    FailureCode,
    InvocationResult,
    InvocationStatus,
    ModelProfile,
    RuntimeContract,
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
    validate_schema_definition,
)
from dispatcher_for_codex_agents.agent_harness.shard import ImmutableShardWriter
from dispatcher_for_codex_agents.workspace_paths import temporary_root

from .process_guard import record_invocation_process
from .runtime_contract import (
    UNKNOWN,
    canonical_bytes,
    classify_activity,
    digest,
    normalize_output,
    validate_coverage,
)

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


def _redact_capture(content: bytes, environment: dict[str, str]) -> bytes:
    """Preserve capture bytes except the existing credential redaction rule."""
    for key, value in environment.items():
        if key.upper().endswith(_SECRET_ENV_SUFFIXES) and len(value) >= 8:
            content = content.replace(
                value.encode("utf-8"), f"<REDACTED:{key}>".encode()
            )
    return content


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


def _policy_violations(
    event: dict[str, Any], policy: CapabilityPolicy | None = None
) -> list[str]:
    policy = policy or CapabilityPolicy()
    violations: list[str] = []
    event_type = event.get("type")
    if not isinstance(event_type, str):
        return ["event type is missing or non-string"]
    folded_type = event_type.casefold()
    if event_type not in {
        "thread.started",
        "turn.started",
        "turn.completed",
        "turn.failed",
        "error",
        "item.started",
        "item.updated",
        "item.completed",
    }:
        violations.append(f"unrecognized event type: {event_type}")
    if any(
        token in folded_type for token in ("subagent", "spawn_agent", "collaboration")
    ):
        violations.append(f"forbidden event type: {event_type}")
    if event_type in _FORBIDDEN_ITEM_TYPES or event_type.endswith("_tool_call"):
        violation = event_violation(event, policy)
        if violation:
            violations.append(violation)

    item = event.get("item")
    if isinstance(item, dict):
        item_type = item.get("type")
        if not isinstance(item_type, str) or item_type not in {
            "agent_message",
            "reasoning",
            "todo_list",
            "error",
        }:
            violation = event_violation(item, policy)
            if violation:
                violations.append(violation)
        command = item.get("command")
        if isinstance(command, str) and re.search(
            r"(?:^|[\s/])codex(?:\s|$)", command, re.IGNORECASE
        ):
            violations.append("recursive Codex command detected")
    return violations


def _parse_event_stream(
    stdout: str, policy: CapabilityPolicy | None = None
) -> _ParsedEvents:
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
        try:
            json.dumps(event, ensure_ascii=False).encode("utf-8")
        except UnicodeEncodeError:
            parse_error = f"line {line_number}: invalid Unicode scalar in JSONL event"
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
        violations.extend(_policy_violations(event, policy))
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


def evaluate_capture(
    *,
    task: AgentTask,
    profile: ModelProfile,
    stdout: str | bytes,
    stderr: str | bytes,
    cli_version: str,
    exit_code: int | None,
    provenance: dict,
    latency: float = 0,
    timed_out: bool = False,
    cancelled: bool = False,
) -> tuple[InvocationResult, dict[str, bytes]]:
    """One interpretation path for invocation and immutable revalidation."""
    capture = stdout.encode("utf-8") if isinstance(stdout, str) else stdout
    invalid_encoding = False
    try:
        stdout = capture.decode("utf-8")
    except UnicodeDecodeError:
        stdout = capture.decode("utf-8", errors="replace")
        invalid_encoding = True
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    contract = RuntimeContract.model_validate(task.runtime_contract)
    parsed = _parse_event_stream(stdout, task.capability_policy)
    if invalid_encoding:
        parsed = replace(parsed, valid_jsonl=False, parse_error="invalid UTF-8 capture")
    activity = classify_activity(
        parsed.events,
        version=cli_version,
        complete=parsed.valid_jsonl
        and parsed.turn_completed_count == 1
        and parsed.terminal_is_last,
        stderr=stderr,
    )
    if (
        activity["protocol"] != "UNSUPPORTED"
        and activity["lifecycle"]["issues"]
        and parsed.turn_completed_count == 1
    ):
        # Invalid ordering cannot become success even with the default contract.
        # Keep the established missing-terminal failure path when no terminal exists.
        parsed = replace(
            parsed, valid_jsonl=False, parse_error="invalid thread/turn/item lifecycle"
        )
    exemptions = set(activity["confirmed_user_input_rejection_events"])
    warnings = _warning_lines(stderr)
    if contract.rejected_user_input == "warn_if_runtime_rejected" and exemptions:
        violations = [
            violation
            for index, event in enumerate(parsed.events)
            if index not in exemptions
            for violation in _policy_violations(event, task.capability_policy)
        ]
        parsed = replace(parsed, policy_violations=tuple(violations))
        warnings.extend(
            f"REQUEST_USER_INPUT_RUNTIME_REJECTED:events.jsonl#/events/{index}"
            for index in sorted(exemptions)
        )
    if UNKNOWN in activity["classifications"]:
        warnings.append(
            "TOOL_ACTIVITY_UNKNOWN_OR_INCOMPLETE:interpretation.json#/tool_activity"
        )
        # Opt-in never turns unknown execution into warning-success. Defaults
        # retain the pre-1.0.2 acceptance path and expose conservative diagnostics.
        if contract != RuntimeContract():
            parsed = replace(
                parsed,
                policy_violations=parsed.policy_violations
                + ("tool activity not conclusively classified",),
            )
    raw = (
        parsed.final_text.encode("utf-8", errors="surrogatepass")
        if parsed.final_text is not None
        else b""
    )
    normalized = (
        normalize_output(raw, task.expected_output_schema, contract)
        if parsed.final_text is not None
        else None
    )
    schema_status = (
        SchemaValidationStatus(normalized.schema_status)
        if normalized
        else SchemaValidationStatus.NOT_RUN
    )
    final_output = normalized.value if normalized else None
    if (
        normalized
        and schema_status == SchemaValidationStatus.PASS
        and contract != RuntimeContract()
    ):
        try:
            normalized.audit["coverage_status"] = validate_coverage(
                final_output, task.expected_output_schema
            )
        except ValueError:
            schema_status = SchemaValidationStatus.FAIL
            normalized.audit["coverage_status"] = "FAIL"
            warnings.append("EXACT_ONCE_COVERAGE_INVALID")
    served_model = (
        parsed.served_models[0] if len(parsed.served_models) == 1 else "NOT_REPORTED"
    )
    if served_model == "NOT_REPORTED":
        warnings.append(FailureCode.SERVED_MODEL_NOT_REPORTED.value)
    if parsed.parse_error:
        warnings.append(parsed.parse_error)
    warnings.extend(parsed.policy_violations)
    if normalized and normalized.error:
        warnings.append(normalized.error)
    combined_errors = (
        stderr
        + "\n"
        + "\n".join(
            json.dumps(event, ensure_ascii=False, sort_keys=True)
            for event in parsed.events
            if event.get("type") in ("error", "turn.failed")
        )
    )
    failure_code = None
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
    elif not provenance.get("agent_process_started"):
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
        allowed = profile.capabilities.get(
            "served_model_allowlist", [provenance.get("configured_model")]
        )
        if not isinstance(allowed, list) or not all(
            isinstance(value, str) for value in allowed
        ):
            failure_code = FailureCode.PROFILE_CONFIGURATION_INVALID
        elif served_model != "NOT_REPORTED" and served_model not in allowed:
            failure_code = FailureCode.SILENT_FALLBACK_DETECTED
        elif parsed.final_text is None:
            failure_code = FailureCode.FINAL_OUTPUT_MISSING
        elif schema_status != SchemaValidationStatus.PASS:
            failure_code = FailureCode.OUTPUT_SCHEMA_INVALID
    locator = next(
        (
            index
            for index in reversed(range(len(parsed.events)))
            if parsed.events[index].get("type") == "item.completed"
            and isinstance(parsed.events[index].get("item"), dict)
            and parsed.events[index]["item"].get("type") == "agent_message"
            and isinstance(parsed.events[index]["item"].get("text"), str)
        ),
        None,
    )
    audit = {
        "artifact_contract": "dca.invocation-interpretation/1",
        "runtime_contract": contract.model_dump(mode="json"),
        "runtime_contract_sha256": digest(
            canonical_bytes(contract.model_dump(mode="json"))
        ),
        "captured_events_sha256": digest(capture),
        "capture_boundary": "CLI stdout bytes after existing credential redaction",
        "raw_final_present": parsed.final_text is not None,
        "extraction": {
            "artifact": "events.jsonl",
            "event_index": locator,
            "pointer": "/item/text",
        },
        "normalization": normalized.audit if normalized else None,
        "tool_activity": activity,
        "normalized_present": bool(
            normalized and normalized.audit["removed_byte_ranges"]
        ),
    }
    result = InvocationResult(
        status=InvocationStatus.FAILURE if failure_code else InvocationStatus.SUCCESS,
        exit_code=exit_code,
        turn_completed=parsed.turn_completed_count == 1,
        final_output=final_output,
        schema_validation_status=schema_status,
        usage=parsed.usage,
        provenance={
            **provenance,
            "provider_reported_served_model": served_model,
            "runtime_interpretation": audit,
        },
        warnings=tuple(dict.fromkeys(warnings)),
        failure_code=failure_code,
        latency_seconds=latency,
    )
    artifacts = {
        "raw_final_output.bin": raw,
        "interpretation.json": canonical_bytes(audit) + b"\n",
    }
    if audit["normalized_present"]:
        artifacts["normalized_output.bin"] = normalized.validator_bytes
    return result, artifacts


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
        self._internal_runtime_grant: InternalRuntimeGrant | None = None
        self._runtime_scope: SelectedRuntimeProtectionScope | None = None
        self._runtime_protection: dict | None = None
        self._cancellation = cancellation or Event()

    def preflight(self, *, task: AgentTask, profile: ModelProfile) -> dict[str, Any]:
        RuntimeContract.model_validate(task.runtime_contract)
        self._runtime_protection = None
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
        # Normalize profile read/decode errors before capability compilation or
        # a local version probe, including for dry-run's pre-reservation path.
        resolved = self._resolver.resolve(profile.profile_id)
        command, compiled = self._build_command(
            profile=profile,
            isolated_working_directory=Path(temporary_root())
            / "EPHEMERAL_ISOLATED_DIRECTORY",
            schema_path=Path("output_schema.json") if native else None,
            capability_policy=task.capability_policy,
        )
        return {
            "runtime_contract": RuntimeContract.model_validate(
                task.runtime_contract
            ).model_dump(mode="json"),
            "configured_model": resolved.configured_model,
            "configured_provider": resolved.configured_provider,
            "profile_compatibility": resolved.provenance(
                self._cli_version or "NOT_PROBED"
            ),
            "capability_policy": policy_record(
                task.capability_policy,
                applied=False,
                compiled=compiled,
                runtime_protection=self._runtime_protection,
            ),
            "command": redacted_command(command),
            "command_preview_redacted": True,
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
                [
                    (
                        self._internal_runtime_grant.path
                        if self._internal_runtime_grant is not None
                        else self._executable[0]
                    ),
                    *self._executable[1:],
                    "--version",
                ],
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

    def _runtime_support(self) -> InternalRuntimeGrant:
        """Pin a canonical executable; never add a launcher symlink or parent."""
        selected = self._executable[0]
        if not Path(selected).is_absolute():
            if Path(selected).name != selected:
                raise CapabilityError("Selected Codex executable must be absolute")
            selected = (
                shutil.which(selected, path=self._environment().get("PATH")) or ""
            )
        grant = InternalRuntimeGrant.capture(selected)
        if self._internal_runtime_grant is None:
            self._internal_runtime_grant = grant
        elif self._internal_runtime_grant != grant:
            raise CapabilityError("Selected Codex executable changed after preflight")
        return self._internal_runtime_grant

    def _protect_runtime(
        self, policy: CapabilityPolicy, grant: InternalRuntimeGrant
    ) -> None:
        # Runs before even the version subprocess. Layout failure must not start
        # an unknown executable; failure provenance never becomes an applied grant.
        self._runtime_protection = {
            "selected_executable": grant.path,
            "canonical_executable": grant.path,
            "runtime_layout": "UNRESOLVED",
            "protected_root": None,
            "derivation_method": "canonical_standalone_release_suffix_v1",
            "codex_version": self._cli_version or "NOT_PROBED",
            "purpose": "USER_GRANT_DENY_BOUNDARY_NOT_A_GRANT",
            "user_grants_check": "NOT_PERFORMED",
        }
        scope = SelectedRuntimeProtectionScope.derive(grant)
        if self._runtime_scope is not None and self._runtime_scope != scope:
            raise CapabilityError("Selected runtime protection scope changed")
        self._runtime_scope = scope
        self._runtime_protection = scope.record(
            grant, self._cli_version or "NOT_PROBED"
        )
        try:
            validate_paths(
                policy,
                protected=(
                    self._resolver.codex_home,
                    Path(grant.path),
                    Path(scope.protected_root),
                ),
            )
        except (ValueError, OSError):
            self._runtime_protection["user_grants_check"] = "REJECTED"
            raise
        self._runtime_protection["user_grants_check"] = "PASS"

    def build_command(
        self,
        *,
        profile: ModelProfile,
        isolated_working_directory: Path,
        schema_path: Path | None,
        capability_policy: CapabilityPolicy | None = None,
    ) -> tuple[str, ...]:
        return self._build_command(
            profile=profile,
            isolated_working_directory=isolated_working_directory,
            schema_path=schema_path,
            capability_policy=capability_policy,
        )[0]

    def _build_command(
        self,
        *,
        profile: ModelProfile,
        isolated_working_directory: Path,
        schema_path: Path | None,
        capability_policy: CapabilityPolicy | None = None,
    ) -> tuple[tuple[str, ...], dict]:
        """Build the only permitted fresh, ephemeral `codex exec` command."""
        self._runtime_protection = None
        policy = capability_policy or CapabilityPolicy()
        # model_copy()/model_construct() must not bypass the authority validator.
        policy = CapabilityPolicy.model_validate(policy.model_dump(mode="json"))
        # Validate user authority before deriving a separate runtime dependency.
        validate_paths(policy, protected=(self._resolver.codex_home,))
        runtime = self._runtime_support() if not policy.restricted else None
        if runtime is not None:
            self._protect_runtime(policy, runtime)
        compiled: dict = {}
        overrides = host_overrides(
            policy,
            home=self._resolver.codex_home,
            profile_id=profile.profile_id,
            cwd=isolated_working_directory,
            compiled=compiled,
            internal_runtime_grant=runtime,
            cli_version=(
                self._read_cli_version(self._environment())
                if not policy.restricted
                else None
            ),
        )
        if self._runtime_protection is not None:
            self._runtime_protection["codex_version"] = (
                self._cli_version or "NOT_PROBED"
            )
        command = [
            runtime.path if runtime is not None else self._executable[0],
            *self._executable[1:],
            "--strict-config",
            "--profile",
            profile.profile_id,
            "--ask-for-approval",
            "never",
            "--cd",
            str(isolated_working_directory),
            "--disable",
            "multi_agent",
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
            "--disable",
            "shell_snapshot",
            "--config",
            "mcp_servers={}",
            "--config",
            "notify=[]",
        ]
        if policy.restricted:
            command.extend(["--sandbox", "read-only"])
        # Independent host features must not hitchhike on a command/web/MCP grant.
        for feature in (
            "multi_agent_v2",
            "request_permissions_tool",
            "shell_snapshot_v2",
            "skill_mcp_dependency_install",
            "skill_search",
            "workspace_dependencies",
            "view_image",
            "goals",
            "sleep_tool",
            "tool_suggest",
            "remote_plugin",
            "browser_use_external",
            "browser_use_full_cdp_access",
            "code_mode",
            "code_mode_only",
            "code_mode_host",
            "step_model_switching",
            "default_mode_request_user_input",
        ):
            command.extend(["--disable", feature])
        for feature, enabled in (
            ("shell_tool", bool({"shell", "unified_exec"} & set(policy.tools))),
            ("unified_exec", "unified_exec" in policy.tools),
        ):
            command.extend(["--enable" if enabled else "--disable", feature])
        overrides += [
            (
                'web_search="live"'
                if "web_search" in policy.tools
                else 'web_search="disabled"'
            ),
            'shell_environment_policy.inherit="none"',
            "shell_environment_policy.set={}",
            "shell_environment_policy.include_only=[]",
            "shell_environment_policy.ignore_default_excludes=false",
        ]
        for override in overrides:
            command.extend(["--config", override])
        command.extend(
            [
                "exec",
                "--ephemeral",
                "--ignore-rules",
                "--skip-git-repo-check",
                "--json",
                "--color",
                "never",
            ]
        )
        if schema_path is not None:
            command.extend(["--output-schema", str(schema_path)])
        command.append("-")
        # Derive feature states from the emitted argv rather than copying grants.
        compiled.update(
            {
                "features": {
                    command[i + 1]: flag == "--enable"
                    for i, flag in enumerate(command[:-1])
                    if flag in {"--enable", "--disable"}
                },
                "approval": "never",
                "web_search": "live" if "web_search" in policy.tools else "disabled",
                "network": {"shell": False},
                "agent_recursion": False,
                "shell_environment_policy": {
                    "inherit": "none",
                    "set": {},
                    "include_only": [],
                    "ignore_default_excludes": False,
                },
            }
        )
        return tuple(command), compiled

    def _terminate_process_group(
        self, process: subprocess.Popen[str]
    ) -> tuple[bytes, bytes]:
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

    def _provenance(
        self,
        *,
        profile: ModelProfile,
        resolved: ResolvedSidecarProfile | None,
        cli_version: str,
        served_model: str,
        native_output_schema: bool,
        agent_process_started: bool,
        capability_policy: CapabilityPolicy,
        compiled_policy: dict | None = None,
        profile_compatibility: dict | None = None,
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
            "profile_compatibility": (
                resolved.provenance(cli_version)
                if resolved is not None
                else {**(profile_compatibility or {}), "codex_cli_version": cli_version}
            ),
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
            "sandbox": (
                "read-only" if capability_policy.restricted else "named:dca_task"
            ),
            "capability_policy": policy_record(
                capability_policy,
                applied=agent_process_started,
                compiled=compiled_policy,
                runtime_protection=self._runtime_protection,
            ),
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
        profile_compatibility: dict | None = None,
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
                capability_policy=task.capability_policy,
                profile_compatibility=profile_compatibility,
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
        self._runtime_protection = None
        validate_local_identifier(attempt_id, field_name="attempt_id")
        writer = ImmutableShardWriter(
            workers_root=workers_root,
            task_id=task.task_id,
            profile_id=profile.profile_id,
            attempt_id=attempt_id,
        )
        try:
            RuntimeContract.model_validate(task.runtime_contract)
        except ValueError:
            return self._write_prelaunch_failure(
                writer=writer,
                task=task,
                profile=profile,
                payload=None,
                resolved=None,
                cli_version="NOT_PROBED",
                failure_code=FailureCode.PAYLOAD_INVALID,
                detail="Runtime contract validation failed",
            )
        environment = self._environment()
        cli_version = (
            self._read_cli_version(environment)
            if task.capability_policy.restricted
            else "NOT_PROBED"
        )
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
                profile_compatibility=exc.compatibility,
            )

        try:
            validate_paths(
                task.capability_policy,
                protected=(self._resolver.codex_home, Path(workers_root)),
            )
            self.build_command(
                profile=profile,
                isolated_working_directory=Path(temporary_root())
                / "EPHEMERAL_ISOLATED_DIRECTORY",
                schema_path=None,
                capability_policy=task.capability_policy,
            )
            cli_version = self._cli_version or cli_version
        except (ValueError, OSError) as exc:
            return self._write_prelaunch_failure(
                writer=writer,
                task=task,
                profile=profile,
                payload=payload,
                resolved=resolved,
                cli_version=cli_version,
                failure_code=FailureCode.POLICY_VIOLATION,
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
        stdout = b""
        stderr = b""
        exit_code: int | None = None
        timed_out = False
        cancelled = False
        process_started = False
        compiled_policy: dict | None = None
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
                command, compiled_policy = self._build_command(
                    profile=profile,
                    isolated_working_directory=isolated,
                    schema_path=schema_path,
                    capability_policy=task.capability_policy,
                )
                record_invocation_process(
                    Path(workers_root),
                    task.task_id,
                    profile.profile_id,
                    attempt_id,
                    child_pid=None,
                )
                if not task.capability_policy.restricted:
                    # Recheck immediately before Popen; no re-selection/fallback.
                    self._protect_runtime(
                        task.capability_policy, self._runtime_support()
                    )
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=False,
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
                    input_text: bytes | None = payload.content.encode("utf-8")
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
        except CapabilityError as exc:
            return self._write_prelaunch_failure(
                writer=writer,
                task=task,
                profile=profile,
                payload=payload,
                resolved=resolved,
                cli_version=cli_version,
                failure_code=FailureCode.POLICY_VIOLATION,
                detail=str(exc),
            )
        except OSError as exc:
            stderr = str(exc).encode("utf-8")
        latency = time.monotonic() - started

        redacted_stdout = _redact_capture(stdout or b"", environment)
        redacted_stderr = _redact_capture(stderr or b"", environment)
        result, runtime_artifacts = evaluate_capture(
            task=task,
            profile=profile,
            stdout=redacted_stdout,
            stderr=redacted_stderr,
            cli_version=cli_version,
            exit_code=exit_code,
            latency=latency,
            timed_out=timed_out,
            cancelled=cancelled,
            provenance=self._provenance(
                profile=profile,
                resolved=resolved,
                cli_version=cli_version,
                served_model="NOT_REPORTED",
                native_output_schema=native_output_schema,
                agent_process_started=process_started,
                capability_policy=task.capability_policy,
                compiled_policy=compiled_policy,
            ),
        )
        writer.write(
            task=task,
            profile=profile,
            input_records=payload.input_records,
            events_jsonl=redacted_stdout,
            stderr_log=redacted_stderr,
            final_output=result.final_output,
            result=result,
            runtime_artifacts=runtime_artifacts,
        )
        return result
