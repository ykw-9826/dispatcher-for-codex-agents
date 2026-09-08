#!/usr/bin/env python3
"""Deterministic stdio protocol fixture. No provider, HTTP or model dependency."""

import json
import sys
from pathlib import Path


def emit(value):
    print(json.dumps(value), flush=True)


if "--version" in sys.argv:
    print("fake-app-server 1.0")
    raise SystemExit(0)

if "generate-json-schema" in sys.argv:
    root = Path(sys.argv[sys.argv.index("--out") + 1])
    files = {
        "v2/ThreadStartParams.json": [
            "allowProviderModelFallback",
            "dynamicTools",
            "model",
            "modelProvider",
            "cwd",
        ],
        "v2/TurnStartParams.json": ["threadId", "toolOutput", "outputSchema"],
        "DynamicToolCallParams.json": ["threadId", "turnId", "tool", "arguments"],
        "DynamicToolCallResponse.json": ["success", "contentItems"],
    }
    for name, keys in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"properties": dict.fromkeys(keys, {})}))
    raise SystemExit(0)

turn_count = 0
for line in sys.stdin:
    value = json.loads(line)
    method = value.get("method")
    if method == "initialized":
        continue
    params = value.get("params", {})
    if method == "initialize":
        emit({"id": value["id"], "result": {"userAgent": "fake-app-server"}})
    elif method == "thread/start":
        assert params["allowProviderModelFallback"] is False
        assert params["sandbox"] == "read-only"
        emit(
            {
                "id": value["id"],
                "result": {
                    "thread": {"id": "thread-stdio"},
                    "model": params["model"],
                    "modelProvider": params["modelProvider"],
                    "cwd": params["cwd"],
                },
            }
        )
    elif method == "turn/start":
        turn_count += 1
        assert params["threadId"] == "thread-stdio"
        emit({"id": value["id"], "result": {"turn": {"id": f"turn-{turn_count}"}}})
        name = (
            "dca_read_verified_results"
            if "toolOutput" in params
            else "dca_start_approved_jobs"
        )
        emit(
            {
                "id": 1000 + turn_count,
                "method": "item/tool/call",
                "params": {
                    "threadId": "thread-stdio",
                    "turnId": f"turn-{turn_count}",
                    "callId": "call",
                    "tool": name,
                    "arguments": {},
                },
            }
        )
    elif method == "turn/interrupt":
        emit({"id": value["id"], "result": {}})
    elif method is None and "result" in value:
        content = json.loads(value["result"]["contentItems"][0]["text"])
        final = "WAITING"
        if "results" in content:
            final = json.dumps(
                {
                    "task_id": content["task_id"],
                    "confirmed": True,
                    "result_files": [r["file"] for r in content["results"]],
                    "summary": "Validated fixture results.",
                }
            )
        emit(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-stdio",
                    "item": {"type": "agentMessage", "text": final},
                },
            }
        )
        emit(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-stdio",
                    "turn": {"id": f"turn-{turn_count}", "status": "completed"},
                },
            }
        )
    else:
        raise ValueError("UNEXPECTED_FAKE_RPC")
