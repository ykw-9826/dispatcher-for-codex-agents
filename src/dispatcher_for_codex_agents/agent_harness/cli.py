"""Fail-closed CLI for agent invocations and batch-local orchestration."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Sequence
from enum import IntEnum
from pathlib import Path
from threading import Event
from typing import Any

from pydantic import ValidationError

from dispatcher_for_codex_agents.agent_harness.batch import (
    BatchError,
    collect_batch,
    create_retry_plan,
    plan_batch,
    run_batch,
    status_batch,
)
from dispatcher_for_codex_agents.agent_harness.contracts import (
    AgentTask,
    FailureCode,
    InvocationResult,
    ModelProfile,
)
from dispatcher_for_codex_agents.agent_harness.payload import (
    PayloadBuilder,
    PayloadBuildError,
)
from dispatcher_for_codex_agents.agent_harness.process_guard import cancellation_signals
from dispatcher_for_codex_agents.agent_harness.profile import ProfileResolutionError
from dispatcher_for_codex_agents.agent_harness.runtime import (
    DEFAULT_ADAPTER_ID,
    AdapterSettings,
    default_registry,
)
from dispatcher_for_codex_agents.agent_harness.schema import (
    OutputSchemaError,
    validate_schema_definition,
)
from dispatcher_for_codex_agents.agent_harness.shard import ShardExistsError
from dispatcher_for_codex_agents.workspace_paths import activate_workspace, output_path


class CliExitCode(IntEnum):
    """Stable parent-process exit codes for the minimum CLI."""

    OK = 0
    INPUT_INVALID = 2
    PROVIDER_FAILURE = 10
    TIMEOUT = 11
    CLI_FAILURE = 12
    TERMINAL_FAILURE = 13
    OUTPUT_INVALID = 14
    POLICY_FAILURE = 15
    SHARD_EXISTS = 16
    CANCELLED = 130


_PROVIDER_FAILURES = {
    FailureCode.PROVIDER_RATE_LIMIT_429,
    FailureCode.PROVIDER_PREFILL_PARAMETER_ERROR,
    FailureCode.PROVIDER_PARTIAL_PARAMETER_ERROR,
}
_TERMINAL_FAILURES = {
    FailureCode.EVENT_STREAM_INVALID,
    FailureCode.FINAL_OUTPUT_MISSING,
    FailureCode.TURN_COMPLETED_MISSING,
}
_POLICY_FAILURES = {
    FailureCode.CALL_LIMIT_EXCEEDED,
    FailureCode.POLICY_VIOLATION,
    FailureCode.SILENT_FALLBACK_DETECTED,
}


def exit_code_for_result(result: InvocationResult) -> CliExitCode:
    """Map an InvocationResult to a stable shell exit code."""
    if result.status == "success":
        return CliExitCode.OK
    if result.failure_code == FailureCode.CANCELLED:
        return CliExitCode.CANCELLED
    if result.failure_code in _PROVIDER_FAILURES:
        return CliExitCode.PROVIDER_FAILURE
    if result.failure_code == FailureCode.TIMEOUT:
        return CliExitCode.TIMEOUT
    if result.failure_code in _TERMINAL_FAILURES:
        return CliExitCode.TERMINAL_FAILURE
    if result.failure_code == FailureCode.OUTPUT_SCHEMA_INVALID:
        return CliExitCode.OUTPUT_INVALID
    if result.failure_code in _POLICY_FAILURES:
        return CliExitCode.POLICY_FAILURE
    return CliExitCode.CLI_FAILURE


def build_parser() -> argparse.ArgumentParser:
    """Build the deliberately small command-line interface."""
    parser = argparse.ArgumentParser(
        prog="dca", description="DCA — Dispatcher for Codex Agents"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    from dispatcher_for_codex_agents.main_agent_bridge.cli import add_parser

    add_parser(subparsers)
    invoke = subparsers.add_parser(
        "invoke",
        help="Run one fresh, bounded external agent through its registered adapter.",
    )
    invoke.add_argument("--task", required=True, help="AgentTask JSON file.")
    invoke.add_argument(
        "--profile", required=True, help="External agent sidecar profile id."
    )
    invoke.add_argument("--adapter", default=DEFAULT_ADAPTER_ID)
    invoke.add_argument("--attempt-id", required=True, help="Immutable attempt id.")
    invoke.add_argument("--shard-root", required=True, help="Run-local shard root.")
    invoke.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate without starting Codex or reserving the attempt shard.",
    )
    invoke.add_argument(
        "--runtime-home",
        "--codex-home",
        dest="codex_home",
        default=None,
        help=argparse.SUPPRESS,
    )
    invoke.add_argument(
        "--executable",
        "--codex-executable",
        dest="codex_executable",
        default="codex",
        help=argparse.SUPPRESS,
    )

    batch = subparsers.add_parser(
        "batch", help="Plan, run, recover, inspect, or collect external agent shards."
    )
    batch_commands = batch.add_subparsers(dest="batch_command", required=True)

    plan = batch_commands.add_parser(
        "plan", help="Create an immutable batch-local execution plan."
    )
    plan.add_argument("--source-tsv", required=True)
    plan.add_argument("--batch-id", required=True)
    plan.add_argument("--batch-id-column", default="batch_id")
    plan.add_argument("--record-id-column", required=True)
    plan.add_argument(
        "--selected-column", action="append", required=True, dest="selected_columns"
    )
    plan.add_argument("--profile-role-config", required=True)
    plan.add_argument("--shard-size", required=True, type=int)
    plan.add_argument("--prompt-template", required=True)
    plan.add_argument("--expected-output-schema", required=True)
    plan.add_argument("--output-root", required=True)
    plan.add_argument("--membership-tsv", default=None)
    plan.add_argument("--timeout", default=600.0, type=float)

    run = batch_commands.add_parser(
        "run", help="Execute only never-started planned attempts."
    )
    run.add_argument("--plan-root", required=True)
    run.add_argument("--run-id", required=True)
    run.add_argument("--max-workers", type=int, choices=(1, 2), default=1)
    run.add_argument("--retry-plan", default=None)
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--notify-config", default=None)
    run.add_argument("--session-id", default=None)
    run.add_argument("--turn-id", default=None)
    run.add_argument("--runtime-home", "--codex-home", dest="codex_home", default=None)
    run.add_argument(
        "--executable", "--codex-executable", dest="codex_executable", default="codex"
    )

    status = batch_commands.add_parser(
        "status", help="Rebuild an immutable status snapshot from artifacts."
    )
    status.add_argument("--plan-root", required=True)
    status.add_argument("--snapshot-id", required=True)

    collect = batch_commands.add_parser(
        "collect", help="Collect schema-valid successful attempt shards."
    )
    collect.add_argument("--plan-root", required=True)
    collect.add_argument("--collection-id", required=True)

    retry = batch_commands.add_parser(
        "retry-plan", help="Freeze an explicit human-approved retry plan."
    )
    retry.add_argument("--plan-root", required=True)
    retry.add_argument("--retry-request", required=True)
    monitor = batch_commands.add_parser(
        "monitor", help="Aggregate status; --watch uses bounded cadence."
    )
    monitor.add_argument("--plan-root", required=True)
    monitor.add_argument("--watch", action="store_true")
    return parser


def _safe_validation_detail(exc: ValidationError) -> str:
    errors = exc.errors(include_url=False, include_context=False, include_input=False)
    return json.dumps(errors, ensure_ascii=False, sort_keys=True)


def _load_task(raw_path: str) -> AgentTask:
    path = Path(raw_path).expanduser()
    display_name = path.name or "<unnamed-task>"
    if path.is_symlink():
        raise ValueError(f"Task file must not be a symlink: {display_name}")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"Task file is unavailable: {display_name}") from exc
    if not resolved.is_file():
        raise ValueError(f"Task path is not a regular file: {display_name}")
    try:
        document = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Task file is not valid UTF-8 JSON: {display_name}") from exc
    if not isinstance(document, dict):
        raise ValueError(f"Task JSON root must be an object: {display_name}")

    raw_inputs = document.get("approved_input_files")
    if isinstance(raw_inputs, list):
        normalized: list[str] = []
        for raw_input in raw_inputs:
            if not isinstance(raw_input, str):
                normalized.append(raw_input)
                continue
            candidate = Path(raw_input).expanduser()
            if not candidate.is_absolute():
                candidate = resolved.parent / candidate
            normalized.append(str(candidate.resolve(strict=False)))
        document["approved_input_files"] = normalized
    try:
        return AgentTask.model_validate(document)
    except ValidationError as exc:
        raise ValueError(
            f"AgentTask validation failed: {_safe_validation_detail(exc)}"
        ) from exc


def _model_profile(
    profile_id: str, adapter_id: str = DEFAULT_ADAPTER_ID
) -> ModelProfile:
    try:
        return ModelProfile(
            profile_id=profile_id,
            capabilities={"native_output_schema": False},
            adapter_id=adapter_id,
        )
    except ValidationError as exc:
        raise ValueError(
            f"ModelProfile validation failed: {_safe_validation_detail(exc)}"
        ) from exc


def _payload_builder(task: AgentTask) -> PayloadBuilder:
    roots = tuple(
        dict.fromkeys(
            Path(raw_path).resolve(strict=False).parent
            for raw_path in task.approved_input_files
        )
    )
    return PayloadBuilder(allowed_roots=roots)


def _validate_shard_destination(
    raw_root: str,
    *,
    task: AgentTask,
    profile: ModelProfile,
    attempt_id: str,
) -> Path:
    root = output_path(raw_root).resolve(strict=False)
    target = root / "workers" / task.task_id / profile.profile_id / attempt_id
    if target.exists():
        raise ShardExistsError(
            "Attempt shard already exists: "
            f"workers/{task.task_id}/{profile.profile_id}/{attempt_id}"
        )
    ancestor = root
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if not ancestor.is_dir() or not os.access(ancestor, os.W_OK | os.X_OK):
        raise ValueError("Shard root has no writable existing ancestor.")
    if root.exists() and (root.is_symlink() or not root.is_dir()):
        raise ValueError("Shard root must be a real directory when it exists.")
    return root


def _dry_run(
    *,
    task: AgentTask,
    profile: ModelProfile,
    attempt_id: str,
    shard_root: Path,
    codex_home: str | None,
    executable: str,
) -> dict[str, Any]:
    del shard_root
    validate_schema_definition(task.expected_output_schema)
    payload = _payload_builder(task).build(task)
    adapter = default_registry().create(
        profile.adapter_id, AdapterSettings(executable, codex_home)
    )
    resolved = adapter.preflight(task=task, profile=profile)
    return {
        **resolved,
        "input_sha256": [
            {
                "logical_name": item.logical_name,
                "sha256": item.sha256,
                "size": item.size,
                "source_name": item.source_name,
            }
            for item in payload.input_records
        ],
        "payload_sha256": hashlib.sha256(payload.content.encode("utf-8")).hexdigest(),
        "profile_id": profile.profile_id,
        "agent_process_started": False,
        "schema_validation_status": "PASS",
        "shard_relative_path": (
            f"workers/{task.task_id}/{profile.profile_id}/{attempt_id}"
        ),
        "status": "DRY_RUN_VALIDATED",
        "task_id": task.task_id,
    }


def _print_json(value: Any, *, stream: Any | None = None) -> None:
    if stream is None:
        stream = sys.stdout
    print(
        json.dumps(value, ensure_ascii=False, sort_keys=True),
        file=stream,
        flush=True,
    )


def _run_batch_command(args: argparse.Namespace, cancellation: Event) -> int:
    if args.batch_command == "plan":
        output_path(args.output_root)
    elif args.batch_command != "monitor":
        output_path(args.plan_root)
    if args.batch_command == "monitor":
        from .monitoring import monitor_batch

        report = monitor_batch(args.plan_root, watch=args.watch)
        return int(
            CliExitCode.CLI_FAILURE
            if report["human_action_required"]
            else CliExitCode.OK
        )
    if args.batch_command == "plan":
        report = plan_batch(
            source_tsv=args.source_tsv,
            batch_id=args.batch_id,
            batch_id_column=args.batch_id_column,
            record_id_column=args.record_id_column,
            selected_columns=args.selected_columns,
            profile_role_config=args.profile_role_config,
            shard_size=args.shard_size,
            prompt_template=args.prompt_template,
            expected_output_schema=args.expected_output_schema,
            output_root=args.output_root,
            membership_tsv=args.membership_tsv,
            timeout=args.timeout,
        )
        _print_json(report)
        return int(CliExitCode.OK)
    if args.batch_command == "run":
        report = run_batch(
            plan_root=args.plan_root,
            run_id=args.run_id,
            max_workers=args.max_workers,
            retry_plan=args.retry_plan,
            dry_run=args.dry_run,
            codex_home=args.codex_home,
            executable=args.codex_executable,
            cancellation=cancellation,
        )
        _print_json(report)
        if cancellation.is_set():
            from dispatcher_for_codex_agents.notifications.cli import emit_batch

            emit_batch(
                report,
                run_id=args.run_id,
                config_path=args.notify_config,
                session_id=args.session_id,
                turn_id=args.turn_id,
            )
            return int(CliExitCode.CANCELLED)
        if not args.dry_run:
            from dispatcher_for_codex_agents.notifications.cli import emit_batch

            emit_batch(
                report,
                run_id=args.run_id,
                config_path=args.notify_config,
                session_id=args.session_id,
                turn_id=args.turn_id,
            )
        return int(
            CliExitCode.OK
            if report["status"] in {"PASS", "DRY_RUN_VALIDATED"}
            else CliExitCode.CLI_FAILURE
        )
    if args.batch_command == "status":
        _print_json(
            status_batch(
                plan_root=args.plan_root,
                snapshot_id=args.snapshot_id,
            )
        )
        return int(CliExitCode.OK)
    if args.batch_command == "collect":
        report = collect_batch(
            plan_root=args.plan_root,
            collection_id=args.collection_id,
        )
        _print_json(report)
        return int(
            CliExitCode.OK if report["status"] == "PASS" else CliExitCode.OUTPUT_INVALID
        )
    if args.batch_command == "retry-plan":
        _print_json(
            create_retry_plan(
                plan_root=args.plan_root,
                retry_request=args.retry_request,
            )
        )
        return int(CliExitCode.OK)
    raise BatchError("Unknown batch command")


def main(argv: Sequence[str] | None = None) -> int:
    try:
        activate_workspace()
    except (OSError, ValueError):
        _print_json(
            {"failure_code": "WORKSPACE_CONFIGURATION_INVALID", "status": "failure"},
            stream=sys.stderr,
        )
        return int(CliExitCode.INPUT_INVALID)
    cancellation = Event()
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["bridge"]:
        from dispatcher_for_codex_agents.main_agent_bridge.cli import main_command

        return main_command(build_parser().parse_args(arguments))
    if arguments[:2] == ["batch", "monitor"]:
        try:
            return _main(arguments, cancellation)
        except KeyboardInterrupt:
            return int(CliExitCode.CANCELLED)
    with cancellation_signals(cancellation):
        return _main(arguments, cancellation)


def _main(argv: Sequence[str] | None, cancellation: Event) -> int:
    """Run one CLI command and return a deterministic process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "batch":
            return _run_batch_command(args, cancellation)
        task = _load_task(args.task)
        profile = _model_profile(args.profile, args.adapter)
        default_registry().require(profile.adapter_id)
        validate_schema_definition(task.expected_output_schema)
        executable = args.codex_executable
        shard_root = _validate_shard_destination(
            args.shard_root,
            task=task,
            profile=profile,
            attempt_id=args.attempt_id,
        )
        if args.dry_run:
            _print_json(
                _dry_run(
                    task=task,
                    profile=profile,
                    attempt_id=args.attempt_id,
                    shard_root=shard_root,
                    codex_home=args.codex_home,
                    executable=executable,
                )
            )
            return int(CliExitCode.OK)

        adapter = default_registry().create(
            profile.adapter_id,
            AdapterSettings(executable, args.codex_home, cancellation=cancellation),
        )
        adapter.preflight(task=task, profile=profile)
        result = adapter.invoke(
            task=task,
            profile=profile,
            attempt_id=args.attempt_id,
            workers_root=shard_root,
            payload_builder=_payload_builder(task),
        )
        _print_json(result.model_dump(mode="json"))
        return int(exit_code_for_result(result))
    except ShardExistsError as exc:
        _print_json(
            {"failure_code": "SHARD_EXISTS", "status": "failure", "warning": str(exc)},
            stream=sys.stderr,
        )
        return int(CliExitCode.SHARD_EXISTS)
    except (
        OutputSchemaError,
        PayloadBuildError,
        ProfileResolutionError,
        BatchError,
        ValueError,
    ) as exc:
        _print_json(
            {
                "failure_code": "CLI_INPUT_INVALID",
                "status": "failure",
                "warning": str(exc),
            },
            stream=sys.stderr,
        )
        return int(CliExitCode.INPUT_INVALID)


__all__ = ["CliExitCode", "build_parser", "exit_code_for_result", "main"]
