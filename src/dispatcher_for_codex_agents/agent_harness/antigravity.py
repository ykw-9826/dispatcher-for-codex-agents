"""Offline Antigravity 1.2.12/1.2.13 protocol subset; never launches an executable.

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

AGY_VERSION = "1.2.12"  # Historical capture/fixture default, not live authority.
AGY_LIVE_TARGET_VERSION = "1.2.13"
AGY_CAPTURE_VERSIONS = (AGY_VERSION, AGY_LIVE_TARGET_VERSION)
AGY_PROTOCOL = "dca-agy-headless/1"
ADMISSION_CONTRACT = "dca-agy-text-only/2"
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
    model: str | None,
    prompt: str,
    version: str = AGY_VERSION,
) -> HeadlessPlan:
    task = _task(task)
    if version not in AGY_CAPTURE_VERSIONS:
        raise ValueError("ANTIGRAVITY_VERSION_UNSUPPORTED")
    policy = task.capability_policy
    if not policy.restricted:
        raise ValueError("ANTIGRAVITY_CAPABILITY_MAPPING_UNSUPPORTED")
    if (
        not isinstance(executable, str)
        or not PurePosixPath(executable).is_absolute()
        or ".." in PurePosixPath(executable).parts
        or (
            model is not None
            and (not isinstance(model, str) or not model or model.startswith("-"))
        )
        or not isinstance(prompt, str)
        or not prompt
        or any("\0" in v for v in (executable, model or "", prompt))
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
            *(("--model=" + model,) if model is not None else ()),
            "--disable-slash-commands",
            "--print=" + prompt,
        ),
        policy_hash,
        task.timeout,
    )


def validate_schema_argument(argv: list[str], schema: dict) -> None:
    """Guard the generated schema argument before any live process can start."""
    try:
        positions = [
            i
            for i, arg in enumerate(argv)
            if arg == "--json-schema" or arg.startswith("--json-schema=")
        ]
        if len(positions) != 1:
            raise ValueError("schema flag count")
        i = positions[0]
        raw = argv[i + 1] if argv[i] == "--json-schema" else argv[i].split("=", 1)[1]
        if _canonical(_json(raw)) != _canonical(schema):
            raise ValueError("schema flag mismatch")
    except (ValueError, IndexError, TypeError, RecursionError, OverflowError):
        raise ValueError("ANTIGRAVITY_SCHEMA_ARGUMENT_INVALID") from None


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
    init = None
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
            _require(
                set(body)
                <= {"cwd", "tools", "permission_mode", "model", "agent", "json_schema"},
                "INIT_FIELDS",
            )
            for key in ("model", "agent"):
                _require(key not in body or isinstance(body[key], str), "INIT_FIELDS")
            _require(
                "json_schema" not in body or isinstance(body["json_schema"], dict),
                "INIT_FIELDS",
            )
            _require(
                isinstance(body.get("cwd"), str)
                and isinstance(body.get("permission_mode"), str),
                "INIT_INVALID",
            )
            _require(
                isinstance(body.get("tools"), list)
                and all(isinstance(name, str) and name for name in body["tools"]),
                "ADVERTISED_TOOL_INVENTORY_INVALID",
            )
            init = body
            conversation = identity
            continue
        _require(conversation is not None, "INIT_MISSING")
        _require(kind in {"step_update", "result"}, "EVENT_UNKNOWN")
        _require(set(event) == {"event", kind}, "EVENT_FIELDS")
        body = event[kind]
        _require(isinstance(body, dict), "EVENT_BODY_INVALID")
        _require(body.get("conversation_id") == conversation, "IDENTITY_CONFLICT")
        if kind == "step_update":
            # Typed activity, not init inventory or prose, determines admission.
            _require(
                "subagent_info" not in body and body.get("step_type") != "subagent",
                "TEXT_ONLY_SUBAGENT_ACTIVITY",
            )
            _require(
                not ({"tool_info", "tool_name"} & set(body))
                and body.get("step_type") != "tool",
                "TEXT_ONLY_TOOL_ACTIVITY",
            )
            _require(
                set(body)
                <= {
                    "conversation_id",
                    "step_index",
                    "state",
                    "step_type",
                    "text_delta",
                    "usage",
                    "duration_seconds",
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
            if "duration_seconds" in body:
                duration = body["duration_seconds"]
                _require(
                    type(duration) in (int, float)
                    and math.isfinite(duration)
                    and duration >= 0,
                    "STEP_DURATION_INVALID",
                )
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
                    "denied_actions",
                },
                "RESULT_FIELDS",
            )
            _require(
                "denied_actions" not in body
                or isinstance(body["denied_actions"], list),
                "DENIED_ACTIONS_INVALID",
            )
            _require(not body.get("denied_actions"), "TEXT_ONLY_DENIED_ACTIONS")
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
                type(body.get("num_turns")) is int and body["num_turns"] >= 0,
                "NUM_TURNS_INVALID",
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
    return {**terminal, "_init": init, "_steps": steps}


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
        "admission_contract": ADMISSION_CONTRACT,
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
        _require(version in AGY_CAPTURE_VERSIONS, "VERSION_UNSUPPORTED")
        _require(
            task.capability_policy == CapabilityPolicy(),
            "CAPABILITY_MAPPING_UNSUPPORTED",
        )
        terminal = _parse(stdout)
        init = terminal.pop("_init")
        steps = terminal.pop("_steps")
        input_indices = [i for i, (kind, _) in steps.items() if kind == "user_input"]
        provenance["observed_init"] = init
        provenance["advertised_tool_inventory"] = init["tools"]
        provenance["tool_inventory_semantics"] = "ADVERTISED_TOOL_INVENTORY"
        provenance["observed_tool_steps"] = 0
        provenance["observed_subagent_steps"] = 0
        provenance["terminal_status"] = terminal["status"]
        provenance["result_num_turns"] = terminal["num_turns"]
        provenance["user_input_step_count"] = len(input_indices)
        provenance["single_input_rule"] = (
            f"AGY_{version.replace('.', '_')}_ONE_INPUT_AND_NUM_TURNS_ONE"
        )
        provenance["conversation_identity_sha256"] = hashlib.sha256(
            terminal["conversation_id"].encode()
        ).hexdigest()
        # Schema echo is an independent requirement, not a claim about the
        # actual output. A CLI advertising a schema can still violate it.
        _require("json_schema" in init, "AGY_SCHEMA_NOT_EFFECTIVE")
        _require(
            _canonical(init["json_schema"]) == _canonical(task.expected_output_schema),
            "INIT_SCHEMA_CONFLICT",
        )
        if terminal["status"] != "SUCCESS":
            failure = (
                FailureCode.CANCELLED
                if terminal["status"] in {"CANCELED", "INTERRUPTED"}
                else FailureCode.TURN_COMPLETED_MISSING
            )
        else:
            _require(
                "structured_output" in terminal,
                "AGY_STRUCTURED_OUTPUT_CONTRACT_VIOLATION",
            )
            _require(
                "json_schema" in terminal
                and _canonical(terminal["json_schema"])
                == _canonical(task.expected_output_schema),
                "TERMINAL_SCHEMA_CONFLICT",
            )
            _require(len(input_indices) == 1, "ONE_STRUCTURED_INPUT_REQUIRED")
            _require(
                input_indices[0] == next(iter(steps)),
                "INPUT_MUST_PRECEDE_RESPONSE",
            )
            # No one-input telemetry mismatch is established for either version.
            # Do not relax this check using synthetic evidence alone.
            _require(terminal["num_turns"] == 1, "ONE_FRESH_TURN_REQUIRED")
            value = terminal["structured_output"]
            normalized = normalize_output(
                _canonical(value).encode("utf-8"),
                task.expected_output_schema,
                task.runtime_contract,
            )
            provenance["output_interpretation"] = normalized.audit
            provenance["structured_output_source"] = "TERMINAL_RESULT_FIELD"
            schema_status = normalized.schema_status
            if schema_status != "PASS":
                failure = FailureCode.OUTPUT_SCHEMA_INVALID
            else:
                # response is a consistency check only; it never supplies a
                # missing structured_output and is never fence-recovered here.
                _require(
                    _canonical(_json(terminal["response"])) == _canonical(value),
                    "RESPONSE_STRUCTURED_OUTPUT_CONFLICT",
                )
                provenance["coverage_status"] = validate_coverage(
                    value, task.expected_output_schema
                )
                provenance["stream_validation"] = "PASS"
                completed = True
        usage = terminal.get("usage", {})
        provenance["usage_status"] = (
            "REPORTED"
            if {"input_tokens", "output_tokens"} <= set(usage)
            else "PARTIAL" if usage else "UNKNOWN"
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
