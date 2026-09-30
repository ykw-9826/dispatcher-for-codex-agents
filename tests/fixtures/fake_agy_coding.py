"""Synthetic coding subprocess; never imports or calls official agy."""

import argparse
import json
import os
import sys

if sys.argv[1:] == ["--version"]:
    print("1.2.13")
    raise SystemExit(0)

parser = argparse.ArgumentParser()
parser.add_argument("--sandbox", action="store_true", required=True)
parser.add_argument("--output-format", choices=["stream-json"], required=True)
parser.add_argument("--print-timeout", required=True)
parser.add_argument("--disable-slash-commands", action="store_true", required=True)
parser.add_argument("--print", required=True)
parser.parse_args()
mode = "success"  # Tests pin fixture bytes, never a real runtime/environment.


def emit(value):
    print(json.dumps(value), flush=True)


emit(
    {
        "event": "init",
        "conversation_id": "synthetic",
        "init": {
            "cwd": os.getcwd(),
            "tools": ["write_to_file"],
            "permission_mode": "request-review",
        },
    }
)
emit(
    {
        "event": "step_update",
        "step_update": {
            "conversation_id": "synthetic",
            "step_index": 0,
            "step_type": "user_input",
            "state": "DONE",
        },
    }
)
if mode != "noop":
    with open("src/labels.py", "w") as stream:
        stream.write("value = 1\n")
if mode == "tamper":
    with open("tests/test_labels.py", "w") as stream:
        stream.write("# agent pretends this is valid\n")
emit(
    {
        "event": "result",
        "result": {
            "conversation_id": "synthetic",
            "status": "ERROR" if mode == "error" else "SUCCESS",
            "num_turns": 1,
            "response": "Agent prose is not acceptance",
        },
    }
)
