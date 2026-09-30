"""Experimental one-shot workspace-artifact admission, never JSON-result recovery.

The default registry stays closed until a separately reviewed product route is
admitted. Official CLI owns auth; parent Git/tests, not final prose, decide success.
"""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import time
from pathlib import Path

from .antigravity import AGY_LIVE_TARGET_VERSION, _json, _usage
from .antigravity_live import AntigravityCliAdapter, OfficialCliApproval, _path
from .workspace_artifact import (
    WorkspaceArtifactTask,
    WorkspaceContractError,
    audit_workspace,
    digest,
    freeze_workspace,
    run_validators,
)

CODING_TOOLS = frozenset(
    {
        "run_command",
        "command_status",
        "send_command_input",
        "view_file",
        "read_file",
        "list_dir",
        "grep_search",
        "find_by_name",
        "codebase_search",
        "view_code_item",
        "view_file_outline",
        "write_to_file",
        "write_file",
        "replace_file_content",
        "multi_replace_file_content",
        "task_boundary",
        "notify_user",
        "ask_permission",
    }
)


def interpret_coding_capture(
    raw: bytes, *, cwd: Path, exit_code: int | None, timed_out=False
) -> dict:
    """Known typed coding events are evidence, not automatic tool violations."""
    record = {
        "status": "UNKNOWN_OR_INCOMPLETE",
        "terminal_status": None,
        "tool_steps": [],
        "permission_denials": 0,
        "usage": {},
        "agy_internal_retries": "UNKNOWN",
        "physical_provider_requests": "UNKNOWN",
    }
    try:
        init = None
        final = None
        steps = {}
        tools = {}
        for line in raw.decode("utf-8").split("\n"):
            if not line.strip():
                continue
            row = _json(line)
            if not isinstance(row, dict) or final is not None:
                raise ValueError
            kind = row.get("event")
            if kind == "init":
                if init is not None or set(row) != {"event", "conversation_id", "init"}:
                    raise ValueError
                body = row["init"]
                if (
                    not isinstance(row["conversation_id"], str)
                    or not row["conversation_id"]
                    or not isinstance(body, dict)
                ):
                    raise ValueError
                if (
                    body.get("cwd") != str(cwd)
                    or body.get("permission_mode") != "request-review"
                    or "agent" in body
                    or "model" in body
                    or "json_schema" in body
                ):
                    raise ValueError
                if not isinstance(body.get("tools"), list) or not all(
                    isinstance(t, str) for t in body["tools"]
                ):
                    raise ValueError
                init = row
                continue
            if (
                init is None
                or kind not in {"step_update", "result"}
                or set(row) != {"event", kind}
            ):
                raise ValueError
            body = row[kind]
            if (
                not isinstance(body, dict)
                or body.get("conversation_id") != init["conversation_id"]
            ):
                raise ValueError
            if kind == "result":
                if any(state != "DONE" for _, state in steps.values()):
                    raise ValueError
                final = body
                continue
            i, category, state = (
                body.get("step_index"),
                body.get("step_type"),
                body.get("state"),
            )
            if (
                type(i) is not int
                or i < 0
                or state not in {"ACTIVE", "DONE"}
                or category
                not in {"user_input", "agent_response", "checkpoint", "tool"}
            ):
                raise ValueError
            if "subagent_info" in body or not steps and category != "user_input":
                raise ValueError
            if i in steps and (steps[i][0] != category or steps[i][1] == "DONE"):
                raise ValueError
            steps[i] = (category, state)
            if category == "tool":
                info = body.get("tool_info", {})
                if not isinstance(info, dict):
                    raise ValueError
                name = body.get("tool_name", info.get("name"))
                if (
                    name not in CODING_TOOLS
                    or info.get("name", name) != name
                    or i in tools
                    and tools[i] != name
                ):
                    raise ValueError
                tools[i] = name
            elif "tool_info" in body or "tool_name" in body:
                raise ValueError
        if final is None or sum(t == "user_input" for t, _ in steps.values()) != 1:
            raise ValueError
        if type(final.get("num_turns")) is not int or final["num_turns"] != 1:
            raise ValueError
        denials = final.get("denied_actions", [])
        if not isinstance(denials, list):
            raise ValueError
        status = final.get("status")
        if status not in {
            "SUCCESS",
            "ERROR",
            "CANCELED",
            "INTERRUPTED",
            "INVALID",
            "WAITING",
            "RUNNING",
        }:
            raise ValueError
        record.update(
            terminal_status=status,
            tool_steps=[{"index": i, "name": name} for i, name in tools.items()],
            permission_denials=len(denials),
            user_input_steps=1,
            num_turns=1,
            conversation_sha256=digest(init["conversation_id"].encode()),
            usage=_usage(final["usage"]) if "usage" in final else {},
            status=(
                "PASS"
                if status == "SUCCESS" and not final.get("error")
                else "RUNTIME_ABNORMAL"
            ),
        )
    except (
        ValueError,
        TypeError,
        KeyError,
        UnicodeError,
        RecursionError,
        OverflowError,
    ):
        record["failure"] = "CODING_STREAM_INVALID"
    if timed_out:
        record.update(status="TIMEOUT", failure="PROCESS_DEADLINE")
    elif type(exit_code) is not int or exit_code != 0:
        record.update(status="RUNTIME_ABNORMAL", failure="PROCESS_EXIT")
    return record


def _save(directory: Path, name: str, value: bytes | dict) -> None:
    raw = (
        value
        if isinstance(value, bytes)
        else (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    )
    fd = os.open(directory / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _validator_identity(task: WorkspaceArtifactTask, mounts: tuple[Path, ...]) -> dict:
    """Pin parent-held validator files and executable bytes outside agent output."""
    paths = {Path(v.argv[0]).resolve(strict=True) for v in task.validator_commands}
    for path in mounts:
        if (
            not path.is_absolute()
            or path.resolve(strict=True) != path
            or path == Path("/")
        ):
            raise WorkspaceContractError("VALIDATOR_SUPPORT_INVALID")
        if path.is_file():
            paths.add(path)
    result = {}
    for path in paths:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or path.is_relative_to(task.workspace_root):
            raise WorkspaceContractError("VALIDATOR_SUPPORT_INVALID")
        result[str(path)] = digest(path.read_bytes())
    return result


def _support_matches(
    expected: dict, task: WorkspaceArtifactTask, mounts: tuple[Path, ...]
) -> bool:
    try:
        return expected == _validator_identity(task, mounts)
    except (OSError, ValueError, RuntimeError):
        return False


def invoke_coding_admission(
    *,
    task: WorkspaceArtifactTask,
    approval: OfficialCliApproval,
    admission_path: Path,
    prompt: str,
    attempt: Path,
    validator_mounts: tuple[Path, ...],
) -> dict:
    """Explicit approval; one process, no retry or same-workspace fallback."""
    task = WorkspaceArtifactTask.model_validate(task.model_dump())
    if approval.version != AGY_LIVE_TARGET_VERSION or not prompt or "\0" in prompt:
        raise WorkspaceContractError("CODING_ADMISSION_INVALID")
    _path(admission_path)
    if stat.S_IMODE(admission_path.stat().st_mode) != 0o600:
        raise WorkspaceContractError("CODING_ADMISSION_MODE_INVALID")
    raw = admission_path.read_bytes()
    expected = {
        "execution_mode": "workspace_artifact",
        "task_sha256": digest(task.model_dump_json().encode()),
        "customization_baseline": "PASS",
        "state_ownership": "OFFICIAL_CLI",
        "dca_retries": 0,
        "fallback": False,
        "experimental_live": True,
    }
    if digest(raw) != approval.config_sha256 or json.dumps(
        _json(raw.decode()), sort_keys=True
    ) != json.dumps(expected, sort_keys=True):
        raise WorkspaceContractError("CODING_ADMISSION_INVALID")
    before = freeze_workspace(task)
    support = _validator_identity(task, validator_mounts)
    if (
        not attempt.is_absolute()
        or attempt.resolve() != attempt
        or attempt.is_relative_to(task.workspace_root)
    ):
        raise WorkspaceContractError("ATTEMPT_PATH_INVALID")
    gate = AntigravityCliAdapter(admission_path=admission_path, approval=approval)
    env = dict(os.environ)
    env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "true"
    gate._runtime_check(task.workspace_root, env)
    argv = [
        str(approval.executable),
        "--sandbox",
        "--output-format",
        "stream-json",
        "--print-timeout",
        f"{task.timeout_seconds:g}s",
        "--disable-slash-commands",
        "--print=" + prompt,
    ]
    attempt.mkdir(mode=0o700)
    _save(attempt, "task.json", task.model_dump(mode="json"))
    _save(attempt, "before.json", before)
    _save(attempt, "validator-support.json", support)
    start = time.monotonic()
    launched = False
    timeout = False
    code = None
    stdout, stderr = b"", b""
    try:
        gate._check_binary()
        process = subprocess.Popen(
            argv,
            cwd=task.workspace_root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        launched = True
        try:
            stdout, stderr = process.communicate(timeout=task.timeout_seconds)
        except subprocess.TimeoutExpired:
            timeout = True
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        code = process.returncode
    except (OSError, ValueError):
        stderr = b"CODING_PROCESS_START_OR_BINARY_CHECK_FAILED\n"
    elapsed = time.monotonic() - start
    _save(attempt, "events.jsonl", stdout)
    _save(attempt, "stderr.log", stderr)
    runtime = interpret_coding_capture(
        stdout, cwd=task.workspace_root, exit_code=code, timed_out=timeout
    )
    audit = audit_workspace(task, before)
    validators = []
    support_unchanged = _support_matches(support, task, validator_mounts)
    if not support_unchanged:
        audit = {**audit, "status": "FAIL", "failure": "VALIDATOR_SUPPORT_CHANGED"}
    if audit.get("scope_integrity") == "PASS" and support_unchanged:
        try:
            validators = run_validators(
                task, mounts=validator_mounts, output=attempt / "validators"
            )
            second = audit_workspace(task, before)
            support_unchanged = _support_matches(support, task, validator_mounts)
            if second != audit or not support_unchanged:
                audit = {**second, "status": "FAIL", "failure": "VALIDATOR_SIDE_EFFECT"}
        except (OSError, ValueError, subprocess.SubprocessError):
            audit = {
                **audit,
                "status": "FAIL",
                "failure": "VALIDATOR_CONTROLLED_FAILURE",
            }
    artifacts_pass = (
        audit["status"] == "PASS"
        and bool(validators)
        and all(v["status"] == "PASS" for v in validators)
    )
    status = (
        ("PASS" if runtime["status"] == "PASS" else "ARTIFACT_VALID_RUNTIME_ABNORMAL")
        if artifacts_pass
        else "FAIL_ARTIFACT"
    )
    result = {
        "contract": "dca-workspace-artifact/1",
        "status": status,
        "runtime": runtime,
        "artifact_audit": audit,
        "validator_support_unchanged": support_unchanged,
        "validators": validators,
        "argv": argv,
        "version": approval.version,
        "binary_sha256": approval.executable_sha256,
        "auto_update_environment": {"AGY_CLI_DISABLE_AUTO_UPDATE": "true"},
        "exit_code": code,
        "elapsed_seconds": elapsed,
        "dca_logical_invocations": int(launched),
        "dca_retries": 0,
        "physical_provider_requests": "UNKNOWN",
        "agy_internal_retries": "UNKNOWN",
        "exact_file_os_enforcement": "NOT_VERIFIED",
        "final_text_authority": False,
    }
    _save(attempt, "result.json", result)
    files = {
        p.relative_to(attempt).as_posix(): digest(p.read_bytes())
        for p in attempt.rglob("*")
        if p.is_file()
    }
    _save(attempt, "sha256.json", files)
    return result
