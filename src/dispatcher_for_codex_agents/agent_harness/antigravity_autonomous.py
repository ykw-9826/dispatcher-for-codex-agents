"""AGY 1.4.10 autonomous-workspace runtime and capture interpretation.

The launcher is explicit, one attempt, and requires a separately reviewed
approval. Official project permissions and actual sandbox enforcement remain
separate gates.
Unlike the historical structured/exact-file routes, coding may use multiple
turns and local tools. Final prose is never an artifact acceptance oracle.
"""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .antigravity import _json, _usage
from .antigravity_coding import CODING_TOOLS, _support_matches
from .antigravity_live import _path
from .autonomous_workspace import (
    AutonomousWorkspaceTask,
    _save,
    finalize_autonomous_workspace,
    freeze_autonomous_workspace,
    freeze_validator_support,
)
from .workspace_artifact import digest

AUTONOMOUS_AGY_VERSION = "1.4.10"
AUTONOMOUS_AGY_PROTOCOL = "dca-agy-autonomous/1"
AUTONOMOUS_PROMPT = "Fix the synthetic workspace; run tests and build."
AUTONOMOUS_LIVE_GATE = "ANTIGRAVITY_AUTONOMOUS_LIVE_NOT_ADMITTED"


@dataclass(frozen=True)
class AutonomousHeadlessPlan:
    """Preview only; neither a project selector nor --sandbox grants admission."""

    argv: tuple[str, ...]
    task_sha256: str
    timeout_seconds: float
    version: str = AUTONOMOUS_AGY_VERSION
    live_gate: str = AUTONOMOUS_LIVE_GATE
    auto_update_disabled: bool = True
    sandbox: bool = True


def compile_autonomous_headless(
    *,
    task: AutonomousWorkspaceTask,
    executable: Path,
    project: str,
    prompt: str,
    version: str = AUTONOMOUS_AGY_VERSION,
    sandbox: bool = True,
) -> AutonomousHeadlessPlan:
    """Documented flags only, fresh headless conversation, explicit deadline.

    --project selects an official project ID/name, not a filesystem path. The
    caller must independently verify its folders and project permission rules.
    sandbox=False is explicit deferred enforcement for a controlled development
    benchmark; it must be recorded as UNSANDBOXED/DEFERRED, never sandbox PASS.
    No model, agent, auth, API, schema or resume override is emitted.
    """
    task = AutonomousWorkspaceTask.model_validate(task.model_dump())
    if version != AUTONOMOUS_AGY_VERSION:
        raise ValueError("ANTIGRAVITY_AUTONOMOUS_VERSION_UNSUPPORTED")
    if (
        not isinstance(executable, Path)
        or not executable.is_absolute()
        or ".." in executable.parts
        or not isinstance(project, str)
        or not project
        or project.startswith(("-", "/"))
        or not isinstance(prompt, str)
        or not prompt
        or any(c in str(executable) + project for c in "\0\r\n")
        or "\0" in prompt
    ):
        raise ValueError("ANTIGRAVITY_AUTONOMOUS_COMMAND_INVALID")
    argv = [str(executable)]
    if sandbox:
        argv.append("--sandbox")
    argv.extend(
        (
            "--project=" + project,
            "--output-format",
            "stream-json",
            "--print-timeout",
            f"{task.timeout_seconds:g}s",
            "--disable-slash-commands",
            "--print=" + prompt,
        )
    )
    return AutonomousHeadlessPlan(
        argv=tuple(argv),
        task_sha256=digest(task.model_dump_json().encode()),
        timeout_seconds=task.timeout_seconds,
        sandbox=sandbox,
    )


def autonomous_child_environment() -> dict[str, str]:
    """Build a minimal, non-secret child environment for the official CLI.

    AGY's headless login context can reject subprocesses whose SSH session
    indicators were stripped by historical DCA allowlists. Preserve observed
    real parent context values without copying authentication state. Required
    base keys are explicitly pinned; optional desktop/config context is included
    only when already present in the trusted parent.
    """
    environment = {
        "AGY_CLI_DISABLE_AUTO_UPDATE": "true",
        "HOME": os.environ["HOME"],
        "XDG_RUNTIME_DIR": os.environ["XDG_RUNTIME_DIR"],
        "DBUS_SESSION_BUS_ADDRESS": os.environ["DBUS_SESSION_BUS_ADDRESS"],
    }
    for name in ("SSH_CLIENT", "SSH_CONNECTION"):
        if name in os.environ:
            environment[name] = os.environ[name]
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_SESSION_TYPE"):
        if name in os.environ:
            environment[name] = os.environ[name]
    return environment


@dataclass(frozen=True)
class AutonomousRuntimeApproval:
    """Reviewed executable digest and prompt identity for one autonomous attempt."""

    executable: Path
    executable_sha256: str
    project: str
    prompt: str
    version: str = AUTONOMOUS_AGY_VERSION


def interpret_autonomous_capture(
    raw: bytes, *, cwd: Path, exit_code: int | None, timed_out: bool = False
) -> dict:
    """Protocol completion, not proof of sandbox or functional correctness.

    Only final cumulative usage is recorded; per-step usage is not added again.
    Unobservable internal retries and physical provider requests stay UNKNOWN.
    Tool inventory is not a grant; unsupported used tools/recursion fail closed.
    """
    record = {
        "contract": AUTONOMOUS_AGY_PROTOCOL,
        "status": "UNKNOWN_OR_INCOMPLETE",
        "terminal_status": None,
        "tool_steps": [],
        "permission_denials": 0,
        "usage": {},
        "usage_status": "NOT_REPORTED",
        "agy_internal_retries": "UNKNOWN",
        "physical_provider_requests": "UNKNOWN",
        "sandbox_enforcement": "NOT_CERTIFIED_BY_CAPTURE",
        "final_text_authority": False,
    }
    try:
        init = None
        final = None
        steps = {}
        tools = {}
        for line in raw.decode("utf-8").splitlines():
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
                    or body.get("cwd") != str(cwd)
                    or body.get("permission_mode")
                    not in {"request-review", "proceed-in-sandbox"}
                    or "json_schema" in body
                    or "agent" in body
                    or "model" in body
                    or not isinstance(body.get("tools"), list)
                    or not all(isinstance(t, str) for t in body["tools"])
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
                or "subagent_info" in body
            ):
                raise ValueError
            if kind == "result":
                if not steps or any(state != "DONE" for _, state in steps.values()):
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
                or not steps
                and category != "user_input"
                or i in steps
                and (steps[i][0] != category or steps[i][1] == "DONE")
                or i not in steps
                and steps
                and i <= max(steps)
            ):
                raise ValueError
            steps[i] = (category, state)
            if category == "tool":
                info = body.get("tool_info", {})
                if not isinstance(info, dict):
                    raise ValueError
                name = body.get("tool_name", info.get("name"))
                if (
                    not isinstance(name, str)
                    or name not in CODING_TOOLS
                    or info.get("name", name) != name
                    or i in tools
                    and tools[i] != name
                ):
                    raise ValueError
                tools[i] = name
            elif "tool_info" in body or "tool_name" in body:
                raise ValueError
        if final is None or type(final.get("num_turns")) is not int:
            raise ValueError
        if final["num_turns"] < 1:
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
        denials = final.get("denied_actions", [])
        if not isinstance(denials, list):
            raise ValueError
        usage = _usage(final["usage"]) if "usage" in final else {}
        record.update(
            status=(
                "PASS"
                if status == "SUCCESS" and not final.get("error")
                else "RUNTIME_ABNORMAL"
            ),
            terminal_status=status,
            tool_steps=[{"index": i, "name": name} for i, name in tools.items()],
            permission_denials=len(denials),
            num_turns=final["num_turns"],
            user_input_steps=sum(t == "user_input" for t, _ in steps.values()),
            conversation_sha256=digest(init["conversation_id"].encode()),
            permission_mode_reported=init["init"]["permission_mode"],
            usage=usage,
            usage_status="REPORTED" if "usage" in final else "NOT_REPORTED",
        )
    except (
        ValueError,
        TypeError,
        KeyError,
        UnicodeError,
        RecursionError,
        OverflowError,
    ):
        record["failure"] = "AUTONOMOUS_STREAM_INVALID"
    if timed_out:
        record.update(status="TIMEOUT", failure="PROCESS_DEADLINE")
    elif type(exit_code) is not int or exit_code != 0:
        record.update(status="RUNTIME_ABNORMAL", failure="PROCESS_EXIT")
    return record


def _check_runtime_binary(approval: AutonomousRuntimeApproval) -> None:
    executable = approval.executable
    try:
        _path(executable)
        if (
            not stat.S_ISREG(executable.lstat().st_mode)
            or digest(executable.read_bytes()) != approval.executable_sha256
        ):
            raise ValueError
    except (OSError, ValueError, RuntimeError):
        raise ValueError("ANTIGRAVITY_BINARY_DRIFT") from None


def _probe_runtime_version(
    approval: AutonomousRuntimeApproval,
    *,
    cwd: Path,
    environment: dict[str, str],
) -> None:
    """Bounded no-prompt version probe before any prompt-bearing spawn.

    A pinned hash proves bytes, not behaviour: the selected binary must also
    self-report the pinned 1.4.10. The probe never carries a prompt, reuses the
    attempt's minimal child environment and rechecks the hash afterwards. Its
    output is never recorded, since even a failing executable may echo secrets.
    """
    _check_runtime_binary(approval)
    try:
        probe = subprocess.run(
            [str(approval.executable), "--version"],
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise ValueError("ANTIGRAVITY_AUTONOMOUS_VERSION_PROBE_FAILED") from None
    _check_runtime_binary(approval)
    if probe.returncode != 0 or probe.stdout.strip() != (
        AUTONOMOUS_AGY_VERSION.encode("ascii")
    ):
        raise ValueError("ANTIGRAVITY_AUTONOMOUS_VERSION_UNSUPPORTED")


def launch_autonomous_workspace(
    *,
    task: AutonomousWorkspaceTask,
    approval: AutonomousRuntimeApproval,
    attempt: Path,
    validator_mounts: tuple[Path, ...] = (),
    sandbox: bool = True,
) -> dict:
    """Execute one AGY attempt, then perform the trusted parent post-audit.

    Native AGY sandbox enforcement is not currently required for this controlled
    development benchmark. The launcher records requested versus deferred state,
    never retries or changes permissions, and still runs the full parent audit.
    """
    task = AutonomousWorkspaceTask.model_validate(task.model_dump())
    if (
        not attempt.is_absolute()
        or attempt.resolve() != attempt
        or attempt.is_relative_to(task.workspace_root)
        or approval.prompt != AUTONOMOUS_PROMPT
    ):
        raise ValueError("AUTONOMOUS_RUNTIME_INPUT_INVALID")
    _path(task.workspace_root, directory=True)
    if stat.S_IMODE(task.workspace_root.stat().st_mode) & 0o002 or not os.access(
        task.workspace_root, os.W_OK
    ):
        raise ValueError("AUTONOMOUS_WORKSPACE_MODE_INVALID")
    _check_runtime_binary(approval)
    baseline = freeze_autonomous_workspace(task)
    support = freeze_validator_support(task, validator_mounts)
    plan = compile_autonomous_headless(
        task=task,
        executable=approval.executable,
        project=approval.project,
        prompt=approval.prompt,
        version=approval.version,
        sandbox=sandbox,
    )
    environment = autonomous_child_environment()
    _probe_runtime_version(approval, cwd=task.workspace_root, environment=environment)
    _check_runtime_binary(approval)
    attempt.mkdir(mode=0o700)
    _save(attempt, "task.json", task.model_dump(mode="json"))
    _save(attempt, "baseline.json", baseline)
    _save(attempt, "validator-support.json", support)
    _save(
        attempt,
        "plan.json",
        {"argv": plan.argv, "task_sha256": plan.task_sha256},
    )
    _save(
        attempt,
        "environment-presence.json",
        {
            key: (value == os.environ.get(key))
            for key, value in environment.items()
            if key in os.environ
        },
    )
    start = time.monotonic()
    launched = False
    timed_out = False
    exit_code = None
    stdout = stderr = b""
    try:
        process = subprocess.Popen(
            plan.argv,
            cwd=task.workspace_root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        launched = True
        try:
            stdout, stderr = process.communicate(timeout=task.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        exit_code = process.returncode
    except (OSError, ValueError):
        stderr = b"AUTONOMOUS_PROCESS_START_FAILED\n"
    elapsed = time.monotonic() - start
    _save(attempt, "events.jsonl", stdout)
    _save(attempt, "stderr.log", stderr)
    runtime = interpret_autonomous_capture(
        stdout,
        cwd=task.workspace_root,
        exit_code=exit_code,
        timed_out=timed_out,
    )
    runtime.update(
        argv=plan.argv,
        version=approval.version,
        binary_sha256=approval.executable_sha256,
        exit_code=exit_code,
        elapsed_seconds=elapsed,
        dca_logical_invocations=int(launched),
        dca_retries=0,
        child_environment_keys=sorted(environment),
        ssh_context_inherited={
            key: environment.get(key) == os.environ.get(key)
            for key in ("SSH_CLIENT", "SSH_CONNECTION")
            if key in os.environ
        },
        runtime_version_check="PASS",
        sandbox_requested="--sandbox" in plan.argv,
        sandbox_enforcement=(
            "REQUESTED_NOT_CERTIFIED" if sandbox else "UNSANDBOXED/DEFERRED"
        ),
    )
    if runtime["status"] == "PASS":
        support_unchanged = _support_matches(support, task, validator_mounts)
        if support_unchanged:
            try:
                audit = finalize_autonomous_workspace(
                    task=task,
                    baseline=baseline,
                    runtime_status=runtime["status"],
                    validator_support=support,
                    validator_mounts=validator_mounts,
                    output=attempt / "audit",
                    require_validator_sandbox=False,
                )
            except OSError:
                audit = {
                    "contract": "dca-autonomous-workspace/1",
                    "status": "FAIL",
                    "scope": "FAIL",
                    "functional": "NOT_RUN",
                    "hygiene": "NOT_RUN",
                    "runtime_status": runtime["status"],
                    "recovery": [],
                    "validators": [],
                    "permission_enforcement": "NOT_CERTIFIED_BY_POST_AUDIT",
                    "failure": "VALIDATOR_SANDBOX_UNAVAILABLE",
                }
        else:
            audit = {
                "contract": "dca-autonomous-workspace/1",
                "status": "FAIL",
                "scope": "FAIL",
                "functional": "NOT_RUN",
                "hygiene": "NOT_RUN",
                "runtime_status": runtime["status"],
                "recovery": [],
                "validators": [],
                "permission_enforcement": "NOT_CERTIFIED_BY_POST_AUDIT",
                "failure": "VALIDATOR_SUPPORT_CHANGED",
            }
    else:
        audit = {
            "contract": "dca-autonomous-workspace/1",
            "status": "FAIL",
            "scope": "NOT_RUN",
            "functional": "NOT_RUN",
            "hygiene": "NOT_RUN",
            "runtime_status": runtime["status"],
            "recovery": [],
            "validators": [],
            "permission_enforcement": "NOT_CERTIFIED_BY_POST_AUDIT",
        }
    runtime["audit"] = audit
    _save(attempt, "result.json", runtime)
    _save(
        attempt,
        "sha256.json",
        {
            path.relative_to(attempt).as_posix(): digest(path.read_bytes())
            for path in attempt.rglob("*")
            if path.is_file()
        },
    )
    return runtime
