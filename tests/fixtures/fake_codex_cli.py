#!/usr/bin/env python3
"""Deterministic fake executable for agent-harness tests."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def emit(value: dict[str, object]) -> None:
    print(json.dumps(value, sort_keys=True), flush=True)


if "--version" in sys.argv:
    print("fake-codex 0.1.0")
    raise SystemExit(0)

arguments = sys.argv[1:]
call_log = os.environ.get("FAKE_CODEX_CALL_LOG")
if call_log:
    with Path(call_log).open("a", encoding="utf-8") as handle:
        handle.write("agent_subprocess\n")
required_pairs = {
    "--sandbox": "read-only",
    "--ask-for-approval": "never",
    "--color": "never",
}
expected_profile = os.environ.get("FAKE_EXPECT_PROFILE")
if expected_profile is not None:
    required_pairs["--profile"] = expected_profile
for flag, expected in required_pairs.items():
    if flag not in arguments or arguments[arguments.index(flag) + 1] != expected:
        print(f"missing required pair: {flag} {expected}", file=sys.stderr)
        raise SystemExit(91)

required_flags = {
    "--strict-config",
    "--ephemeral",
    "--ignore-rules",
    "--skip-git-repo-check",
    "--json",
}
if not required_flags.issubset(arguments):
    print("missing required bounded-exec flags", file=sys.stderr)
    raise SystemExit(92)
if "resume" in arguments:
    print("resume is forbidden", file=sys.stderr)
    raise SystemExit(93)
if arguments.count("--profile") != 1:
    print("exactly one profile is required", file=sys.stderr)
    raise SystemExit(94)
if not arguments[arguments.index("--profile") + 1]:
    raise SystemExit(94)

disabled = {
    arguments[index + 1]
    for index, value in enumerate(arguments[:-1])
    if value == "--disable"
}
if not {
    "apps",
    "browser_use",
    "computer_use",
    "hooks",
    "image_generation",
    "multi_agent",
    "plugins",
    "shell_tool",
    "unified_exec",
}.issubset(disabled):
    print("required no-tool feature flags were not disabled", file=sys.stderr)
    raise SystemExit(95)

isolated = Path(arguments[arguments.index("--cd") + 1])
if not isolated.is_dir() or "dca-agent-" not in isolated.name:
    print("working directory is not ephemeral and isolated", file=sys.stderr)
    raise SystemExit(96)
if os.environ.get("FAKE_EXPECT_SCHEMA", "1") == "1":
    if "--output-schema" not in arguments:
        print("native output schema was expected", file=sys.stderr)
        raise SystemExit(97)

payload = sys.stdin.read()
if (
    "DCA_AGENT_TASK_V1" not in payload
    or "DCA_AGENT_TASK_END" not in payload
    or "INPUT_BEGIN" not in payload
):
    print("canonical stdin payload missing", file=sys.stderr)
    raise SystemExit(98)


def schema_value(schema: dict[str, object], *, record_id: str | None = None) -> object:
    if "enum" in schema:
        values = schema["enum"]
        if record_id is not None and record_id in values:
            return record_id
        return values[0]
    value_type = schema.get("type")
    if value_type == "object":
        properties = schema.get("properties", {})
        return {
            name: schema_value(
                properties[name],
                record_id=record_id if name == "record_id" else None,
            )
            for name in schema.get("required", [])
        }
    if value_type == "array":
        count = max(int(schema.get("minItems", 1)), 1)
        return [
            schema_value(schema["items"], record_id=record_id) for _ in range(count)
        ]
    if value_type == "string":
        return record_id if record_id is not None else "fixture"
    if value_type == "number":
        return float(schema.get("minimum", 0.5))
    if value_type == "integer":
        return int(schema.get("minimum", 0))
    if value_type == "boolean":
        return False
    if value_type == "null":
        return None
    raise ValueError(f"unsupported fake schema type: {value_type!r}")


def batch_output(payload_text: str, mode_name: str) -> dict[str, object]:
    schema_line = next(
        line
        for line in payload_text.splitlines()
        if line.startswith("EXPECTED_OUTPUT_SCHEMA ")
    )
    schema = json.loads(schema_line.removeprefix("EXPECTED_OUTPUT_SCHEMA "))
    result_schema = schema["properties"]["results"]["items"]
    record_ids = list(result_schema["properties"]["record_id"]["enum"])
    if mode_name == "batch_missing":
        record_ids = record_ids[:-1]
    elif mode_name == "batch_duplicate" and len(record_ids) > 1:
        record_ids[-1] = record_ids[0]
    elif mode_name == "batch_extra":
        record_ids[-1] = "OUT-OF-BATCH"
    return {
        "results": [
            schema_value(result_schema, record_id=record_id) for record_id in record_ids
        ]
    }


mode = os.environ.get("FAKE_CODEX_MODE", "success")
served_model = "other-model" if mode == "silent_fallback" else "fake-model"
thread: dict[str, object] = {"type": "thread.started", "thread_id": "fake-thread"}
if mode != "success_no_served" and os.environ.get("FAKE_OMIT_SERVED_MODEL") != "1":
    thread["served_model"] = served_model
emit(thread)

if mode == "timeout":
    time.sleep(30)
    raise SystemExit(0)
if mode == "invalid_jsonl":
    print("{this is not json", flush=True)
    raise SystemExit(0)
if mode == "nonzero":
    emit({"type": "error", "message": "synthetic CLI failure"})
    print("synthetic nonzero error", file=sys.stderr)
    raise SystemExit(7)
if mode == "rate_limit":
    emit({"type": "error", "message": "HTTP 429 rate limit"})
    print("provider error: HTTP 429 rate limit exceeded", file=sys.stderr)
    raise SystemExit(1)
if mode == "prefill":
    emit({"type": "error", "message": "prefill parameter unsupported"})
    print("provider error: unsupported prefill parameter", file=sys.stderr)
    raise SystemExit(1)
if mode == "partial":
    emit({"type": "error", "message": "partial parameter invalid"})
    print("provider error: invalid partial parameter", file=sys.stderr)
    raise SystemExit(1)

if mode == "policy":
    emit(
        {
            "type": "item.completed",
            "item": {
                "id": "tool-1",
                "type": "command_execution",
                "command": "codex exec resume --last",
                "status": "completed",
            },
        }
    )

if mode == "schema_invalid":
    output = {"decision": "MAYBE", "reason": "invalid enum"}
elif mode == "success_science_terms":
    output = {"decision": "TYPE_A", "reason": "Item 429 used partial prefill."}
elif mode.startswith("batch_"):
    output = batch_output(payload, mode)
else:
    output = {"decision": "TYPE_A", "reason": "fictional fixture accepted"}
emit(
    {
        "type": "item.completed",
        "item": {
            "id": "message-1",
            "type": "agent_message",
            "text": json.dumps(output, sort_keys=True),
        },
    }
)
if mode != "missing_turn_completed":
    emit(
        {
            "type": "turn.completed",
            "usage": {
                "cached_input_tokens": 3,
                "input_tokens": 17,
                "output_tokens": 9,
                "reasoning_output_tokens": 2,
            },
        }
    )
