"""Synthetic documented 1.2.12 headless subset. Never imports or runs agy."""

import argparse
import json
import os
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument("--output-format", choices=["stream-json"], required=True)
parser.add_argument("--json-schema", required=True)
parser.add_argument("--print-timeout", required=True)
parser.add_argument("--model", required=True)
parser.add_argument("--disable-slash-commands", action="store_true", required=True)
parser.add_argument("--print", required=True)
args = parser.parse_args()
mode = os.environ.get("FAKE_AGY_MODE", "success")
identity = "synthetic-conversation"


def emit(event):
    print(json.dumps(event), flush=True)


emit(
    {
        "event": "init",
        "conversation_id": identity,
        "init": {
            "cwd": "/synthetic/workspace",
            "tools": [],
            "permission_mode": "request-review",
        },
    }
)
if mode == "hang":
    time.sleep(30)
elif mode == "nonzero":
    print('AGY_ERROR: {"code":"SYNTHETIC_ERROR"}', file=sys.stderr)
    raise SystemExit(3)
elif mode == "partial":
    raise SystemExit(0)
else:
    emit(
        {
            "event": "step_update",
            "step_update": {
                "conversation_id": identity,
                "step_index": 0,
                "step_type": "agent_response",
                "state": "DONE",
                "text_delta": "synthetic",
                "usage": {"input_tokens": 90, "output_tokens": 90},
            },
        }
    )
    if mode == "echo":
        assert args.model == os.environ["FAKE_AGY_EXPECT_MODEL"]
    value = {
        "decision": "TYPE_A",
        "reason": args.print if mode == "echo" else "synthetic",
    }
    emit(
        {
            "event": "result",
            "result": {
                "conversation_id": identity,
                "status": "SUCCESS",
                "num_turns": 1,
                "response": json.dumps(value),
                "duration_seconds": 0.01,
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "thinking_tokens": 2,
                    "cache_read_tokens": 3,
                    "total_tokens": 15,
                },
                "structured_output": value,
                "json_schema": json.loads(args.json_schema),
            },
        }
    )
