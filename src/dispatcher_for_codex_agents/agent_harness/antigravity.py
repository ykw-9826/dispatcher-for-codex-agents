"""Offline Antigravity 1.2.12 protocol subset; never launches an executable.

Synthetic conformance is not authentication, permission or live-host admission.
The production registry deliberately refuses this runtime.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from .contracts import AgentTask, CapabilityPolicy, FailureCode, InvocationResult
from .runtime_contract import normalize_output, validate_coverage

AGY_VERSION = "1.2.12"
AGY_PROTOCOL = "dca-agy-headless/1"
LIVE_GATE = "ANTIGRAVITY_LIVE_NOT_ADMITTED"


@dataclass(frozen=True)
class HeadlessPlan:
    """Inert argv preview, not a runnable adapter or an authority grant."""

    argv: tuple[str, ...]
    requested_policy_sha256: str
    process_timeout_seconds: float
    live_gate: str = LIVE_GATE


def _task(task: AgentTask) -> AgentTask:
    return AgentTask.model_validate(task.model_dump(mode="json"))


def compile_headless(
    *,
    task: AgentTask,
    executable: str,
    model: str,
    prompt: str,
    version: str = AGY_VERSION,
) -> HeadlessPlan:
    task = _task(task)
    if version != AGY_VERSION:
        raise ValueError("ANTIGRAVITY_VERSION_UNSUPPORTED")
    policy = task.capability_policy
    if not policy.restricted:
        raise ValueError("ANTIGRAVITY_CAPABILITY_MAPPING_UNSUPPORTED")
    if (
        not isinstance(executable, str)
        or not PurePosixPath(executable).is_absolute()
        or ".." in PurePosixPath(executable).parts
        or not isinstance(model, str)
        or not model
        or model.startswith("-")
        or not isinstance(prompt, str)
        or not prompt
        or any("\0" in v for v in (executable, model, prompt))
        or not math.isfinite(task.timeout)
        or task.timeout < 0.001
    ):
        raise ValueError("ANTIGRAVITY_COMMAND_INVALID")
    policy_hash = hashlib.sha256(
        json.dumps(policy.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()
    return HeadlessPlan(
        (
            executable,
            "--output-format",
            "stream-json",
            "--json-schema",
            json.dumps(task.expected_output_schema, allow_nan=False),
            "--print-timeout",
            format(task.timeout, ".9f").rstrip("0").rstrip(".") + "s",
            "--model=" + model,
            "--disable-slash-commands",
            "--print=" + prompt,
        ),
        policy_hash,
        task.timeout,
    )


class _Invalid(ValueError):
    pass


def _require(condition: bool, category: str) -> None:
    if not condition:
        raise _Invalid(category)


def _object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        _require(key not in result, "DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _constant(_: str) -> None:
    raise _Invalid("NONFINITE_JSON")


def _finite_float(raw: str) -> float:
    value = float(raw)
    _require(math.isfinite(value), "NONFINITE_JSON")
    return value


def _json(raw: str) -> Any:
    return json.loads(
        raw,
        object_pairs_hook=_object,
        parse_constant=_constant,
        parse_float=_finite_float,
    )


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _usage(value: Any) -> dict[str, int]:
    _require(isinstance(value, dict), "USAGE_INVALID")
    allowed = {
        "input_tokens",
        "output_tokens",
        "thinking_tokens",
        "cache_read_tokens",
        "total_tokens",
    }
    _require(set(value) <= allowed, "USAGE_UNSUPPORTED")
    _require(all(type(v) is int and v >= 0 for v in value.values()), "USAGE_INVALID")
    return value


def _parse(raw: bytes) -> dict:
    conversation = None
    terminal = None
    steps: dict[int, tuple[str, str]] = {}
    for line in raw.decode("utf-8").split("\n"):
        if not line.strip():
            continue
        event = _json(line)
        _require(isinstance(event, dict), "EVENT_NOT_OBJECT")
        # Reject lone surrogates without echoing content.
        json.dumps(event, ensure_ascii=False).encode("utf-8")
        _require(terminal is None, "EVENT_AFTER_RESULT")
        kind = event.get("event")
        _require(isinstance(kind, str), "EVENT_KIND_INVALID")
        if kind == "init":
            _require(conversation is None, "DUPLICATE_INIT")
            _require(set(event) <= {"event", "conversation_id", "init"}, "INIT_FIELDS")
            identity = event.get("conversation_id")
            body = event.get("init")
            _require(isinstance(identity, str) and bool(identity), "IDENTITY_INVALID")
            _require(isinstance(body, dict), "INIT_INVALID")
            _require(set(body) <= {"cwd", "tools", "permission_mode"}, "INIT_FIELDS")
            _require(
                isinstance(body.get("cwd"), str)
                and isinstance(body.get("permission_mode"), str),
                "INIT_INVALID",
            )
            _require(body.get("tools") == [], "TOOLS_NOT_ADMITTED")
            conversation = identity
            continue
        _require(conversation is not None, "INIT_MISSING")
        _require(kind in {"step_update", "result"}, "EVENT_UNKNOWN")
        _require(set(event) == {"event", kind}, "EVENT_FIELDS")
        body = event[kind]
        _require(isinstance(body, dict), "EVENT_BODY_INVALID")
        _require(body.get("conversation_id") == conversation, "IDENTITY_CONFLICT")
        if kind == "step_update":
            _require(
                set(body)
                <= {
                    "conversation_id",
                    "step_index",
                    "state",
                    "step_type",
                    "text_delta",
                    "usage",
                },
                "STEP_FIELDS_OR_TOOL_NOT_ADMITTED",
            )
            index, state, step_type = (
                body.get("step_index"),
                body.get("state"),
                body.get("step_type"),
            )
            _require(type(index) is int and index >= 0, "STEP_INDEX_INVALID")
            _require(
                isinstance(state, str) and state in {"ACTIVE", "DONE"},
                "STEP_STATE_INVALID",
            )
            _require(
                isinstance(step_type, str)
                and step_type in {"user_input", "agent_response", "checkpoint"},
                "STEP_TYPE_OR_TOOL_NOT_ADMITTED",
            )
            if "text_delta" in body:
                _require(isinstance(body["text_delta"], str), "STEP_TEXT_INVALID")
            if "usage" in body:
                _usage(body["usage"])  # Validate, never sum into final usage.
            previous = steps.get(index)
            _require(
                previous is None
                or (previous[0] == step_type and previous[1] != "DONE"),
                "STEP_ORDER_INVALID",
            )
            steps[index] = (step_type, state)
        else:
            _require(
                set(body)
                <= {
                    "conversation_id",
                    "status",
                    "response",
                    "duration_seconds",
                    "num_turns",
                    "usage",
                    "structured_output",
                    "json_schema",
                },
                "RESULT_FIELDS",
            )
            status = body.get("status")
            _require(
                isinstance(status, str)
                and status
                in {
                    "SUCCESS",
                    "ERROR",
                    "CANCELED",
                    "INTERRUPTED",
                    "INVALID",
                    "WAITING",
                    "RUNNING",
                },
                "RESULT_STATUS_INVALID",
            )
            _require(
                type(body.get("num_turns")) is int and body["num_turns"] == 1,
                "ONE_FRESH_TURN_REQUIRED",
            )
            duration = body.get("duration_seconds")
            _require(
                type(duration) in (int, float)
                and math.isfinite(duration)
                and duration >= 0,
                "DURATION_INVALID",
            )
            _require(isinstance(body.get("response"), str), "RESPONSE_INVALID")
            if "usage" in body:
                _usage(body["usage"])
            _require(
                all(state == "DONE" for _, state in steps.values()), "STEP_INCOMPLETE"
            )
            terminal = body
    _require(terminal is not None, "FINAL_RESULT_MISSING")
    return terminal


def evaluate_headless_capture(
    *,
    task: AgentTask,
    stdout: bytes,
    exit_code: int | None,
    elapsed: float,
    version: str = AGY_VERSION,
    timed_out: bool = False,
    cancelled: bool = False,
) -> InvocationResult:
    """Interpret one capture. No executable, auth, environment or stderr access.

    A successful synthetic result certifies this protocol subset only.
    Unknown events and unsupported authority fail closed.
    """
    task = _task(task)
    provenance = {
        "adapter_id": "antigravity_cli",
        "runtime_protocol": AGY_PROTOCOL,
        "runtime_version": version,
        "evidence_scope": "OFFLINE_DOCUMENTED_SUBSET",
        "live_execution_gate": LIVE_GATE,
        "served_model": "NOT_REPORTED",
        "capability_policy": {
            "requested": task.capability_policy.model_dump(mode="json"),
            "effective": None,
            "enforcement": "NOT_VERIFIED",
        },
        "capture_sha256": hashlib.sha256(stdout).hexdigest(),
        "logical_invocation_scope": "ONE_FRESH_SESSION",
        "accounting_scope": "OFFLINE_COMPONENT_NOT_SOURCE_HISTORY",
        "dca_logical_invocations": 0,
        "stream_validation": "UNKNOWN_OR_INCOMPLETE",
        "process_outcome": (
            "TIMEOUT"
            if timed_out
            else (
                "CANCELLED"
                if cancelled
                else (
                    "EXIT_ZERO"
                    if type(exit_code) is int and exit_code == 0
                    else "NONZERO_EXIT" if type(exit_code) is int else "UNKNOWN"
                )
            )
        ),
        "dca_retries": 0,
        "agy_builtin_retry_policy": "NOT_CONTROLLED",
        "agy_builtin_retries_observed": "UNKNOWN",
        "provider_requests": "UNKNOWN",
        "cost_hard_cap": "NOT_ESTABLISHED",
        "usage_source": "FINAL_RESULT_ONLY",
        "usage_status": "UNKNOWN",
    }
    failure = None
    warnings: list[str] = []
    value = None
    schema_status = "NOT_RUN"
    completed = False
    usage: dict[str, int] = {}
    try:
        _require(version == AGY_VERSION, "VERSION_UNSUPPORTED")
        _require(
            task.capability_policy == CapabilityPolicy(),
            "CAPABILITY_MAPPING_UNSUPPORTED",
        )
        terminal = _parse(stdout)
        provenance["stream_validation"] = "PASS"
        provenance["terminal_status"] = terminal["status"]
        provenance["conversation_identity_sha256"] = hashlib.sha256(
            terminal["conversation_id"].encode()
        ).hexdigest()
        usage = terminal.get("usage", {})
        provenance["usage_status"] = (
            "REPORTED"
            if {"input_tokens", "output_tokens"} <= set(usage)
            else "PARTIAL" if usage else "UNKNOWN"
        )
        completed = terminal["status"] == "SUCCESS"
        if not completed:
            failure = (
                FailureCode.CANCELLED
                if terminal["status"] in {"CANCELED", "INTERRUPTED"}
                else FailureCode.TURN_COMPLETED_MISSING
            )
        else:
            normalized = normalize_output(
                terminal["response"].encode(),
                task.expected_output_schema,
                task.runtime_contract,
            )
            provenance["output_interpretation"] = normalized.audit
            schema_status = normalized.schema_status
            if schema_status != "PASS":
                failure = FailureCode.OUTPUT_SCHEMA_INVALID
            else:
                # Strict JSON and type-sensitive equality: True must not equal 1.
                value = _json(normalized.validator_bytes.decode("utf-8"))
                _require(
                    "structured_output" not in terminal
                    or _canonical(terminal["structured_output"]) == _canonical(value),
                    "STRUCTURED_OUTPUT_CONFLICT",
                )
                _require(
                    "json_schema" not in terminal
                    or _canonical(terminal["json_schema"])
                    == _canonical(task.expected_output_schema),
                    "SCHEMA_CONFLICT",
                )
                provenance["coverage_status"] = validate_coverage(
                    value, task.expected_output_schema
                )
    except (_Invalid, ValueError, TypeError, RecursionError, OverflowError) as exc:
        # Fixed categories only: never persist parser text or runtime output.
        warnings.append(str(exc) if isinstance(exc, _Invalid) else "CAPTURE_INVALID")
        failure = FailureCode.EVENT_STREAM_INVALID
        value, schema_status, completed = None, "NOT_RUN", False
    if timed_out:
        failure = FailureCode.TIMEOUT
    elif cancelled:
        failure = FailureCode.CANCELLED
    elif type(exit_code) is not int or exit_code != 0:
        failure = FailureCode.CLI_NONZERO_EXIT
    if failure is not None:
        value = None
    return InvocationResult(
        status="failure" if failure else "success",
        exit_code=exit_code,
        turn_completed=completed,
        final_output=value,
        schema_validation_status=schema_status,
        usage=usage,
        provenance=provenance,
        warnings=tuple(warnings),
        failure_code=failure,
        latency_seconds=elapsed,
    )
