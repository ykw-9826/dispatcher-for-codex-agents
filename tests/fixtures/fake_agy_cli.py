"""Synthetic documented 1.2.12/1.2.13 headless subset. Never imports or runs agy."""

import argparse
import json
import os
import sys
import time

# Version probes never enter the synthetic task path.
if sys.argv[1:] == ["--version"]:
    print("1.2.13")
    raise SystemExit(0)

parser = argparse.ArgumentParser()
parser.add_argument("--output-format", choices=["stream-json"], required=True)
parser.add_argument("--json-schema", required=True)
parser.add_argument("--print-timeout", required=True)
parser.add_argument("--model")
parser.add_argument("--sandbox", action="store_true")
parser.add_argument("--agent")
parser.add_argument("--disable-slash-commands", action="store_true", required=True)
parser.add_argument("--print", required=True)
args = parser.parse_args()
mode = os.environ.get("FAKE_AGY_MODE", "success")
identity = "synthetic-conversation"
if mode == "auth":
    print('AGY_ERROR: {"code":"AUTH_REQUIRED"}', file=sys.stderr)
    raise SystemExit(3)


def emit(event):
    print(json.dumps(event), flush=True)


emit(
    {
        "event": "init",
        "conversation_id": identity,
        "init": {
            "cwd": os.getcwd() if args.sandbox else "/synthetic/workspace",
            "json_schema": json.loads(args.json_schema),
            **({"agent": args.agent} if args.agent else {}),
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
elif mode == "truncated":
    print('{"event":', flush=True)
    raise SystemExit(0)
elif mode == "partial":
    raise SystemExit(0)
else:
    emit(
        {
            "event": "step_update",
            "step_update": {
                "conversation_id": identity,
                "step_index": 0,
                "step_type": "user_input",
                "state": "DONE",
            },
        }
    )
    emit(
        {
            "event": "step_update",
            "step_update": {
                "conversation_id": identity,
                "step_index": 1,
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
    if (
        json.loads(args.json_schema).get("properties", {}).get("ok", {}).get("const")
        == "DCA_AGY_L1_OK"
    ):
        value = {"ok": "DCA_AGY_L1_OK"}
    final = {
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
    if mode in {"default_contract_blocker", "missing_structured"}:
        final["result"].pop("structured_output")
        final["result"]["response"] = ""
    if mode == "default_contract_blocker":
        # Synthetic typed attempt plus denial; no command is actually run.
        for state in ("ACTIVE", "DONE"):
            emit(
                {
                    "event": "step_update",
                    "step_update": {
                        "conversation_id": identity,
                        "step_index": 2,
                        "step_type": "tool",
                        "state": state,
                        "tool_info": {
                            "name": "run_command",
                            "parameters": {"command": "synthetic"},
                        },
                    },
                }
            )
        final["result"]["denied_actions"] = [
            {"action": "command(synthetic)", "display_name": "Synthetic command"}
        ]
    if mode == "after_error":
        emit({"event": "error", "error": {"code": "SYNTHETIC_FAILURE"}})
    emit(final)
    if mode == "duplicate":
        emit(final)
