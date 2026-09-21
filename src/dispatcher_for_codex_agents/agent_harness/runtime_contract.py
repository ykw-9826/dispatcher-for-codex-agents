"""Versioned, pure output interpretation and observable tool-activity evidence.

No runtime operations, credentials, permissions or model calls belong here.
CLI mappings follow OpenAI exec_events.rs at rust-v0.153.4/rust-v0.155.1.
The separately named synthetic protocol is test evidence, not a Codex protocol.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from .contracts import RuntimeContract
from .schema import OutputSchemaError, validate_json_schema

NORMALIZER_VERSION = "dca-output/1"
CLASSIFIER_VERSION = "dca-activity/2"
VALIDATOR_VERSION = "dca-schema/1"
SYNTHETIC_PROTOCOL = "dca.synthetic-runtime/1"
JSON_WHITESPACE = b" \t\r\n"
EXECUTED = "TOOL_EXECUTED"
REJECTED = "TOOL_ATTEMPT_REJECTED"
UNKNOWN = "UNKNOWN_OR_INCOMPLETE"
NONE = "NO_TOOL_ACTIVITY"


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def validate_coverage(
    output: Any, schema: dict, record_ids: tuple[str, ...] | None = None
) -> str:
    """The existing batch exact-once rule, also usable with frozen task enums."""
    if record_ids is None:
        try:
            record_ids = tuple(
                schema["properties"]["results"]["items"]["properties"]["record_id"][
                    "enum"
                ]
            )
        except (KeyError, TypeError):
            return "NOT_APPLICABLE"
    results = output.get("results") if isinstance(output, dict) else None
    if not isinstance(results, list):
        raise ValueError("results is not an array")
    observed = [
        row.get("record_id")
        for row in results
        if isinstance(row, dict) and isinstance(row.get("record_id"), str)
    ]
    counts = Counter(observed)
    missing = sorted(set(record_ids) - set(observed))
    extra = sorted(set(observed) - set(record_ids))
    duplicate = sorted(key for key, count in counts.items() if count != 1)
    if len(observed) != len(record_ids) or missing or extra or duplicate:
        raise ValueError(f"missing={missing}, extra={extra}, duplicate={duplicate}")
    return "PASS"


@dataclass(frozen=True)
class NormalizedOutput:
    value: Any
    validator_bytes: bytes
    schema_status: str
    error: str | None
    audit: dict[str, Any]


def normalize_output(
    raw: bytes, schema: dict, contract: RuntimeContract
) -> NormalizedOutput:
    """Try the old JSON validator first, then at most one exact wrapper removal."""
    selected = raw
    removed: list[list[int]] = []
    action = "raw"
    try:
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        if contract.structured_output == "json_or_single_fence":
            start = len(raw) - len(raw.lstrip(JSON_WHITESPACE))
            end = len(raw.rstrip(JSON_WHITESPACE))
            close = end - 3
            if (
                raw[start : start + 8] == b"```json\n"
                and close >= start + 8
                and raw[close:end] == b"```"
                and raw[close - 1 : close] == b"\n"
            ):
                # Only the two token ranges are deleted; all other bytes survive.
                candidate = raw[:start] + raw[start + 8 : close] + raw[end:]
                try:
                    value = json.loads(candidate.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    pass
                else:
                    selected = candidate
                    removed = [[start, start + 8], [close, end]]
                    action = "single_json_fence"
    error: str | None = None
    try:
        value = json.loads(selected.decode("utf-8"))
        validate_json_schema(value, schema)
        # Check encodability without replacing/re-serializing validator bytes.
        json.dumps(value, ensure_ascii=False).encode("utf-8")
        status = "PASS"
    except (ValueError, UnicodeDecodeError, OutputSchemaError) as exc:
        status = "FAIL"
        # Parsing diagnostics must not echo the response or credentials.
        error = "OUTPUT_SCHEMA_INVALID:" + type(exc).__name__
        if isinstance(exc, UnicodeEncodeError):
            value = raw.decode("utf-8", errors="replace")
        else:
            try:
                value = json.loads(selected.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                value = raw.decode("utf-8", errors="replace")
    return NormalizedOutput(
        value,
        selected,
        status,
        error,
        {
            "rule_version": NORMALIZER_VERSION,
            "validator_version": VALIDATOR_VERSION,
            "mode": contract.structured_output,
            "action": action,
            "raw_final_sha256": digest(raw),
            "normalized_sha256": digest(selected) if removed else None,
            "validator_input": "normalized" if removed else "raw",
            "validator_input_sha256": digest(selected),
            "removed_byte_ranges": removed,
            "schema_status": status,
            "coverage_status": "NOT_EVALUATED_HERE",
        },
    )


def protocol_for_version(version: str) -> str:
    if not isinstance(version, str):
        return "UNSUPPORTED"
    if version == SYNTHETIC_PROTOCOL:
        return version
    match = re.fullmatch(r"(?:codex-cli|fake-codex) (0\.153\.4|0\.155\.1)", version)
    return "codex-exec/" + match[1] if match else "UNSUPPORTED"


def _lifecycle_issues(events: tuple[dict, ...]) -> list[str]:
    """Validate one exec turn; completed-only items are part of CLI JSONL.

    Agent messages may be intermediate responses. Only the final selected message
    must precede the terminal event; do not invent an App Server phase field.
    """
    issues: list[str] = []
    phase = "before_thread"
    items: dict[str, tuple[str, str]] = {}
    for index, event in enumerate(events):
        kind = event.get("type")
        problem = None
        if kind == "thread.started":
            if phase != "before_thread" or index != 0:
                problem = "thread_start_out_of_order"
            else:
                phase = "before_turn"
        elif kind == "turn.started":
            if phase != "before_turn":
                problem = "turn_start_out_of_order"
            else:
                phase = "in_turn"
        elif kind in ("turn.completed", "turn.failed"):
            if phase != "in_turn" or index != len(events) - 1:
                problem = "terminal_out_of_order"
            phase = "terminal"
        elif kind in ("item.started", "item.updated", "item.completed"):
            if phase != "in_turn":
                problem = "item_outside_turn"
            item = event.get("item")
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("id"), str)
                or not item["id"]
                or not isinstance(item.get("type"), str)
            ):
                problem = problem or "invalid_item_identity"
            else:
                previous = items.get(item["id"])
                if previous is not None and (
                    previous[0] != item["type"]
                    or previous[1] == "item.completed"
                    or kind == "item.started"
                ):
                    problem = problem or "conflicting_item_lifecycle"
                if kind == "item.updated" and previous is None:
                    problem = problem or "item_update_without_start"
                items[item["id"]] = (item["type"], kind)
        elif phase == "terminal":
            problem = "event_after_terminal"
        if problem:
            issues.append(f"events[{index}]:{problem}")
    if phase != "terminal":
        issues.append("missing_thread_turn_terminal")
    if any(state != "item.completed" for _, state in items.values()):
        issues.append("unfinished_item_lifecycle")
    return issues


def classify_activity(
    events: tuple[dict, ...], *, version: str, complete: bool, stderr: str = ""
) -> dict[str, Any]:
    """Classify observable facts, never infer denials from prose or error text."""
    protocol = protocol_for_version(version)
    calls: dict[str, dict] = {}
    issues: list[str] = []
    synthetic = protocol == SYNTHETIC_PROTOCOL
    supported = protocol != "UNSUPPORTED"
    if not supported or not complete:
        issues.append("unsupported_protocol_or_incomplete_stream")
    lifecycle_issues = _lifecycle_issues(events)
    issues.extend(lifecycle_issues)
    benign = {"agent_message", "reasoning", "todo_list"}
    tools = {
        "command_execution",
        "mcp_tool_call",
        "web_search",
        "file_change",
        "collab_tool_call",
    }
    known_top = {
        "thread.started",
        "turn.started",
        "turn.completed",
        "turn.failed",
        "error",
        "item.started",
        "item.updated",
        "item.completed",
    }
    exempt_events: list[int] = []
    work_state: dict[str, list] = {}
    for index, event in enumerate(events):
        event_type = event.get("type")
        if not isinstance(event_type, str):
            issues.append(f"events[{index}]:invalid_event_type")
            continue
        if event_type not in known_top or event_type in {"turn.failed", "error"}:
            issues.append(f"events[{index}]:unsupported_or_error_event")
        if event_type not in {"item.started", "item.updated", "item.completed"}:
            continue
        item = event.get("item")
        if not isinstance(item, dict) or not isinstance(item.get("type"), str):
            issues.append(f"events[{index}]:invalid_item")
            continue
        kind = item["type"]
        if kind in benign:
            if kind in {"agent_message", "reasoning"} and not isinstance(
                item.get("text"), str
            ):
                issues.append(f"events[{index}]:invalid_text_item")
            if kind == "todo_list":
                if not isinstance(item.get("items"), list) or not isinstance(
                    item.get("id"), str
                ):
                    issues.append(f"events[{index}]:invalid_work_state")
                else:
                    work_state[item["id"]] = item["items"]
            continue
        call_id = item.get("id")
        if not isinstance(call_id, str) or not call_id:
            issues.append(f"events[{index}]:missing_call_identity")
            call_id = f"unidentified-{index}"
        call = calls.setdefault(
            call_id,
            {
                "call_id": call_id,
                "tool": kind,
                "evidence": [],
                "states": [],
                "outcome": None,
                "closed": False,
            },
        )
        call["evidence"].append(
            {"artifact": "events.jsonl", "event_index": index, "pointer": "/item"}
        )
        if call["tool"] != kind or call["closed"]:
            call["states"].append(UNKNOWN)
        if kind == "request_user_input_rejection" and synthetic:
            allowed = {
                "type",
                "id",
                "tool",
                "phase",
                "reason",
                "execution_started",
                "required_input",
                "required_work_satisfied",
            }
            if (
                set(item) == allowed
                and item.get("tool") == "request_user_input"
                and event_type == "item.completed"
                and item.get("phase") == "before_execution"
                and item.get("reason") == "noninteractive"
                and item.get("execution_started") is False
                and item.get("required_input") is False
                and item.get("required_work_satisfied") is True
                and not call["states"]
            ):
                call["states"].append(REJECTED)
                call["outcome"] = "noninteractive_preexecution_rejection"
                exempt_events.append(index)
            else:
                call["states"].append(UNKNOWN)
        elif kind not in tools or not supported:
            call["states"].append(UNKNOWN)
        elif kind == "command_execution":
            status = item.get("status")
            code = item.get("exit_code")
            if (
                status == "declined"
                and code is None
                and event_type == "item.completed"
                and not call["states"]
            ):
                call["states"].append(REJECTED)
                call["outcome"] = "declined"
            elif (
                event_type == "item.completed"
                and status in ("completed", "failed")
                and type(code) is int
            ):
                call["states"].append(EXECUTED)
                call["outcome"] = "success" if code == 0 else "execution_failed"
            elif status == "in_progress" and event_type in {
                "item.started",
                "item.updated",
            }:
                call["states"].append("STARTED")
            else:
                call["states"].append(UNKNOWN)
        elif kind in {"mcp_tool_call", "collab_tool_call"}:
            if event_type == "item.started" and item.get("status") == "in_progress":
                call["states"].append(EXECUTED)  # official item: invocation dispatched
            elif event_type == "item.completed" and item.get("status") in (
                "completed",
                "failed",
            ):
                call["states"].append(EXECUTED)
                call["outcome"] = item["status"]
            else:
                call["states"].append(UNKNOWN)
        elif kind == "web_search":
            call["states"].append(
                EXECUTED
                if isinstance(item.get("query"), str)
                and isinstance(item.get("action"), dict)
                else UNKNOWN
            )
        elif kind == "file_change":
            # Failed patch can mean denial or execution failure; never guess.
            call["states"].append(
                EXECUTED
                if event_type == "item.completed" and item.get("status") == "completed"
                else UNKNOWN
            )
        if event_type == "item.completed":
            call["closed"] = True
    aggregates: set[str] = set()
    if any(
        not isinstance(step, dict) or step.get("completed") is not True
        for steps in work_state.values()
        for step in steps
    ):
        issues.append("unfinished_required_work")
    for call in calls.values():
        states = set(call.pop("states"))
        if not call["closed"] or (
            REJECTED in states and (EXECUTED in states or "STARTED" in states)
        ):
            states.add(UNKNOWN)
        states.discard("STARTED")
        if not states:
            states.add(UNKNOWN)
        call["classifications"] = sorted(states)
        if lifecycle_issues:
            # Out-of-turn data is not normal execution/rejection evidence.
            call["classifications"] = [UNKNOWN]
            call["outcome"] = None
            states = {UNKNOWN}
        aggregates.update(states)
    # Stderr is not correlation evidence. Its input/tool-rejection signal can only
    # make interpretation more conservative, never turn a failure into success.
    if re.search(
        r"request_user_input|tool.{0,30}(?:reject|denied|unavailable)", stderr, re.I
    ):
        issues.append("uncorrelated_stderr_tool_signal")
    if issues:
        aggregates.add(UNKNOWN)
    if not aggregates:
        aggregates.add(NONE)
    if UNKNOWN in aggregates:
        exempt_events = []
    return {
        "classifier_version": CLASSIFIER_VERSION,
        "protocol": protocol,
        "synthetic": synthetic,
        "lifecycle": {
            "status": "PASS" if supported and not lifecycle_issues else UNKNOWN,
            "issues": lifecycle_issues,
        },
        "classifications": sorted(aggregates),
        "calls": list(calls.values()),
        "issues": issues,
        "confirmed_user_input_rejection_events": exempt_events,
        "scope": "observable_stream_only",
    }
