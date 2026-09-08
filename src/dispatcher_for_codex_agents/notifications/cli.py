"""dca-notify: shared deterministic main-hook and harness entry point."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from dispatcher_for_codex_agents.workspace_paths import (
    activate_workspace,
    workspace_root,
)

from .core import NotificationEvent, identifier, notify, record_context

HOOK_KINDS = {
    "UserPromptSubmit": ("turn_started", "STARTED"),
    "Stop": ("turn_completed", "COMPLETED"),
    "SessionEnd": ("session_ended", "COMPLETED"),
    "Interrupt": ("interrupted", "INTERRUPTED"),
    "PermissionRequest": ("human_action_required", "REQUIRED"),
    "SubagentStop": ("subagent_stopped", "COMPLETED"),
}


def default_config() -> str:
    selected = os.environ.get("DCA_NOTIFY_CONFIG")
    if selected:
        return selected
    root = workspace_root()
    if root:
        return str(root / "configs/notifications.json")
    raise ValueError("EXPLICIT_NOTIFICATION_CONFIGURATION_REQUIRED")


def hook_event(name: str, raw: dict, config_path: str):
    """Ignore prompt/transcript/cwd/assistant/tool input, even in diagnostics."""
    session = identifier(raw.get("session_id"))
    turn = identifier(raw.get("turn_id"))
    if not session:
        raise ValueError("HOST_SESSION_ID_REQUIRED")
    context = record_context(
        config_path,
        session_id=session,
        turn_id=turn,
        started=name == "UserPromptSubmit",
    )
    if name == "UserPromptSubmit":
        return {"status": "TURN_START_RECORDED", "model_calls": 0}
    kind, status = HOOK_KINDS[name]
    metrics = {}
    if turn and context["turn_started"]:
        metrics["elapsed_seconds"] = max(0, time.time() - context["started_at"])
    event = NotificationEvent(
        source="codex",
        kind=kind,
        status=status,
        session_id=session,
        turn_id=turn,
        run_id=context["run_id"],
        attempt_id=identifier(raw.get("agent_id")) if name == "SubagentStop" else None,
        metrics=metrics,
    )
    return notify(event, config_path)


def emit_batch(
    report: dict,
    *,
    run_id: str,
    config_path: str | None = None,
    session_id: str | None = None,
    turn_id: str | None = None,
) -> dict:
    """One batch terminal summary; no successful-shard pushes."""
    try:
        config_path = config_path or os.environ.get(
            "DCA_NOTIFY_CONFIG", default_config()
        )
        session_id = session_id or os.environ.get("DCA_NOTIFY_SESSION_ID")
        turn_id = turn_id or os.environ.get("DCA_NOTIFY_TURN_ID")
        if session_id and turn_id:
            record_context(
                config_path, session_id=session_id, turn_id=turn_id, run_id=run_id
            )
        passed = report.get("status") == "PASS"
        counts = report.get("terminal_status_counts", {})
        failed = sum(
            counts.get(key, 0)
            for key in ("FAILED", "SCHEMA_INVALID", "POLICY_VIOLATION")
        )
        metrics = {
            "success": counts.get("SUCCESS", 0),
            "failed": failed,
            "incomplete": counts.get("RUNNING_OR_INCOMPLETE", 0),
            "planned": sum(counts.values()),
        }
        event = NotificationEvent(
            source="harness",
            kind="batch_completed" if passed else "batch_failed",
            status="COMPLETED" if passed else "FAILED",
            session_id=session_id,
            turn_id=turn_id,
            run_id=run_id,
            metrics=metrics,
        )
        result = {"terminal": notify(event, config_path)}
        if not passed:
            kind = (
                "explicit_retry_required"
                if failed or metrics["incomplete"]
                else "human_action_required"
            )
            result["action"] = notify(
                NotificationEvent(
                    source="harness",
                    kind=kind,
                    status="REQUIRED",
                    session_id=session_id,
                    turn_id=turn_id,
                    run_id=run_id,
                ),
                config_path,
            )
        return result
    except Exception as exc:
        return {
            "status": "NOTIFICATION_NONBLOCKING_ERROR",
            "error_class": type(exc).__name__,
            "model_calls": 0,
        }


def main(argv=None) -> int:
    try:
        activate_workspace()
    except (OSError, ValueError):
        # A broken installation may not block the user's main turn.
        arguments = list(sys.argv[1:] if argv is None else argv)
        if arguments[:1] == ["hook"]:
            print("{}")
            print("WORKSPACE_CONFIGURATION_INVALID", file=sys.stderr)
            return 0
        print("WORKSPACE_CONFIGURATION_INVALID", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(
        prog="dca-notify",
        description=(
            "DCA — Dispatcher for Codex Agents notifications (zero model calls)"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    hook = sub.add_parser("hook")
    hook.add_argument("--event", choices=sorted(HOOK_KINDS), required=True)
    hook.add_argument("--config", default=default_config())
    emit = sub.add_parser("emit")
    emit.add_argument("--config", default=default_config())
    bind = sub.add_parser("bind")
    bind.add_argument("--config", default=default_config())
    bind.add_argument("--session-id", required=True)
    bind.add_argument("--turn-id", required=True)
    bind.add_argument("--run-id", required=True)
    install = sub.add_parser("hooks-install")
    install.add_argument(
        "--codex-home",
        default=os.environ.get("CODEX_HOME", str(Path.home() / ".codex")),
    )
    install.add_argument("--config", default=default_config())
    install.add_argument("--executable", required=True)
    install.add_argument("--apply", action="store_true")
    install.add_argument("--host-schema", action="append", default=[])
    restore = sub.add_parser("hooks-rollback")
    restore.add_argument("--receipt", required=True)
    restore.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "hooks-install":
            from .hooks import install_hooks

            result = install_hooks(
                args.codex_home,
                args.executable,
                args.config,
                apply=args.apply,
                host_schemas=tuple(args.host_schema),
            )
        elif args.command == "hooks-rollback":
            from .hooks import rollback_hooks

            result = rollback_hooks(args.receipt, apply=args.apply)
        elif args.command == "bind":
            record_context(
                args.config,
                session_id=args.session_id,
                turn_id=args.turn_id,
                run_id=args.run_id,
            )
            result = {"status": "BOUND", "model_calls": 0}
        else:
            content = sys.stdin.read(1048577)
            if len(content) > 1048576:
                raise ValueError("INPUT_TOO_LARGE")
            raw = json.loads(content)
            result = (
                hook_event(args.event, raw, args.config)
                if args.command == "hook"
                else notify(NotificationEvent(**raw), args.config)
            )
    except Exception as exc:
        result = {
            "status": "NOTIFICATION_NONBLOCKING_ERROR",
            "error_class": type(exc).__name__,
            "model_calls": 0,
        }
    if args.command == "hook":
        if result.get("status") == "NOTIFICATION_NONBLOCKING_ERROR":
            print(json.dumps(result, sort_keys=True), file=sys.stderr)
        if args.event in {"Stop", "SubagentStop"}:
            print("{}")
        return 0
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") != "NOTIFICATION_NONBLOCKING_ERROR" else 1


if __name__ == "__main__":
    raise SystemExit(main())
