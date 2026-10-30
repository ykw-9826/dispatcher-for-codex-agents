"""Synthetic AGY 1.4.10 only; no official CLI, auth or provider is accessed."""

import argparse
import json
import os
import sys
from pathlib import Path

if sys.argv[1:] == ["--version"]:
    print("1.4.10")
    raise SystemExit(0)

parser = argparse.ArgumentParser()
parser.add_argument("--sandbox", action="store_true")
parser.add_argument("--project", required=True)
parser.add_argument("--output-format", choices=["stream-json"], required=True)
parser.add_argument("--print-timeout", required=True)
parser.add_argument("--disable-slash-commands", action="store_true", required=True)
parser.add_argument("--print", required=True)
args = parser.parse_args()


def emit(value):
    print(json.dumps(value), flush=True)


def step(index, category, state, **extra):
    emit(
        {
            "event": "step_update",
            "step_update": {
                "conversation_id": "synthetic-autonomous",
                "step_index": index,
                "step_type": category,
                "state": state,
                **extra,
            },
        }
    )


emit(
    {
        "event": "init",
        "conversation_id": "synthetic-autonomous",
        "init": {
            "cwd": os.getcwd(),
            "tools": ["run_command", "write_to_file"],
            "permission_mode": "proceed-in-sandbox",
        },
    }
)
step(0, "user_input", "DONE")
step(1, "tool", "ACTIVE", tool_name="run_command")
step(1, "tool", "DONE", tool_name="run_command", usage={"output_tokens": 2})
step(2, "agent_response", "DONE")
step(3, "user_input", "DONE")
step(4, "tool", "DONE", tool_name="write_to_file")
workspace = Path(os.getcwd())
(workspace / "src/labels.py").write_text("value = 1\n")
emit(
    {
        "event": "result",
        "result": {
            "conversation_id": "synthetic-autonomous",
            "status": "SUCCESS",
            "num_turns": 2,
            "response": "Synthetic final prose is not acceptance",
            "usage": {"input_tokens": 8, "output_tokens": 4, "thinking_tokens": 2},
        },
    }
)
