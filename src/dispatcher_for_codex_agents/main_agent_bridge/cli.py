"""One explicit startup command; no separately launched monitor is necessary."""

from __future__ import annotations

import asyncio
import json
import time
import tomllib
from pathlib import Path

from dispatcher_for_codex_agents.notifications.core import load_config, private_read
from dispatcher_for_codex_agents.workspace_paths import output_path

from .contracts import SupervisorSpec
from .controller import supervise
from .jobs import preflight_jobs
from .store import EventStore, canonical, exclusive


def add_parser(subparsers):
    parser = subparsers.add_parser(
        "bridge",
        help="Explicit main-thread event-driven task controller (not IDE attachment).",
    )
    commands = parser.add_subparsers(dest="bridge_command", required=True)
    start = commands.add_parser(
        "start",
        help="Create an authorized dedicated thread; run, block and continue.",
    )
    start.add_argument("--spec", required=True)
    start.add_argument("--state-root", required=True)
    start.add_argument("--new-thread", action="store_true", required=True)
    start.add_argument("--dry-run", action="store_true")
    for name in ("status", "cancel", "recover"):
        command = commands.add_parser(name)
        command.add_argument("--state-root", required=True)
    return parser


def preflight(spec: SupervisorSpec):
    if spec.expires_at <= time.time() or spec.expires_at - time.time() > 259200:
        raise ValueError("AUTHORIZATION_MUST_EXPIRE_WITHIN_72_HOURS")
    if not Path(spec.executable).is_file() or not Path(spec.working_directory).is_dir():
        raise ValueError("HOST_OR_CWD_UNAVAILABLE")
    config = tomllib.loads((Path(spec.codex_home) / "config.toml").read_text())
    if (
        config.get("profile")
        or config.get("model") != spec.model
        or config.get("model_provider", "openai") != spec.provider
    ):
        raise ValueError("PINNED_MAIN_MODEL_MUST_MATCH_EXISTING_CONFIG")
    load_config(spec.notification_config)
    preflight_jobs(spec)


def status(store):
    rows = store.records()
    binding = store.root / "binding.json"
    starts = [r for r in rows if r.get("method") == "turn/start"]
    waits = {
        r["state"]: r
        for r in rows
        if r.get("state") in {"IDLE_WAIT_STARTED", "IDLE_WAIT_ENDED"}
    }
    quiet = None
    if len(waits) == 2:
        a, b = waits["IDLE_WAIT_STARTED"]["time"], waits["IDLE_WAIT_ENDED"]["time"]
        quiet = sum(a <= r["time"] <= b for r in starts)
    return {
        "thread_id": (
            json.loads(private_read(binding))["thread_id"] if binding.exists() else None
        ),
        "cancelled": (store.root / "cancel.json").exists(),
        "pending_events": len(store.pending()),
        "logical_main_turn_requests": len(starts),
        "continuation_requests": sum(r.get("state") == "RESERVED" for r in rows),
        "physical_model_api_requests": "UNKNOWN",
        "wait_logical_model_requests": quiet,
        "ambiguous": any(r.get("state") == "AMBIGUOUS" for r in rows),
        "last_state": rows[-1].get("state") if rows else "NEW",
        "completion_exists": (store.root / "completion.json").exists(),
    }


def run_command(args):
    root = Path(args.state_root).absolute()
    if args.bridge_command != "status":
        output_path(root)
    if args.bridge_command == "start":
        spec = SupervisorSpec.model_validate(json.loads(private_read(Path(args.spec))))
        preflight(spec)
        if root.exists() or root.resolve() != root:
            raise ValueError("NEW_PRIVATE_STATE_ROOT_REQUIRED")
        if args.dry_run:
            from .capabilities import inspect_host

            capabilities = inspect_host(
                spec.executable, spec.codex_home, spec.working_directory
            )
            return {
                "status": "DRY_RUN_VALIDATED",
                "model_calls": 0,
                "host": "OWNED_STDIO_APP_SERVER",
                "jobs": len(spec.jobs),
                "host_capabilities": capabilities,
            }, 0
        store = EventStore(root, create=True)
        exclusive(root / "spec.json", canonical(spec.model_dump()))
        result = asyncio.run(supervise(spec, store))
        return result, 0
    store = EventStore(root)
    if args.bridge_command == "status":
        return status(store), 0
    if args.bridge_command == "cancel":
        path = root / "cancel.json"
        if not path.exists():
            exclusive(
                path,
                canonical({"status": "AUTHORIZATION_REVOKED", "time": time.time()}),
            )
        return {"status": "CANCEL_REQUESTED", "model_calls": 0}, 0
    spec = SupervisorSpec.model_validate(json.loads(private_read(root / "spec.json")))
    result = asyncio.run(supervise(spec, store, recover=True))
    return result, 0


def main_command(args):
    try:
        result, code = run_command(args)
    except (Exception, KeyboardInterrupt) as exc:
        # Never expose raw validation input, auth errors or remote exception text.
        message = str(exc)
        safe = (
            message
            if message
            and len(message) < 100
            and all(c.isupper() or c == "_" for c in message)
            else "BRIDGE_BLOCKED"
        )
        result = {
            "status": "BLOCKED",
            "failure_code": safe,
            "error_class": type(exc).__name__,
        }
        code = 130 if isinstance(exc, KeyboardInterrupt) or "CANCEL" in safe else 17
    print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
    return code
