"""Documented 1.4.10 subset, deterministic streams and fake executable only."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_autonomous_workspace import task  # noqa: F401
from test_workspace_artifact import workspace  # noqa: F401

from dispatcher_for_codex_agents.agent_harness import antigravity_autonomous as aa
from dispatcher_for_codex_agents.agent_harness.antigravity import compile_headless
from dispatcher_for_codex_agents.agent_harness.workspace_artifact import digest


def capture(cwd):
    return [
        {
            "event": "init",
            "conversation_id": "synthetic",
            "init": {
                "cwd": str(cwd),
                "permission_mode": "proceed-in-sandbox",
                "tools": ["run_command"],
            },
        },
        {
            "event": "step_update",
            "step_update": {
                "conversation_id": "synthetic",
                "step_index": 0,
                "step_type": "user_input",
                "state": "DONE",
            },
        },
        {
            "event": "step_update",
            "step_update": {
                "conversation_id": "synthetic",
                "step_index": 1,
                "step_type": "tool",
                "state": "DONE",
                "tool_name": "run_command",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        },
        {
            "event": "result",
            "result": {
                "conversation_id": "synthetic",
                "status": "SUCCESS",
                "num_turns": 3,
                "usage": {
                    "input_tokens": 30,
                    "output_tokens": 15,
                    "thinking_tokens": 4,
                },
            },
        },
    ]


def interpret(rows, cwd, **kwargs):
    raw = b"\n".join(json.dumps(row).encode() for row in rows)
    return aa.interpret_autonomous_capture(raw, cwd=cwd, exit_code=0, **kwargs)


def plan(task, **kwargs):  # noqa: F811
    return aa.compile_autonomous_headless(
        task=task,
        **{
            "executable": Path("/synthetic/agy"),
            "project": "synthetic-task-project",
            "prompt": "Fix the synthetic workspace; run tests and build.",
            **kwargs,
        },
    )


def test_inert_plan_uses_finite_fresh_sandbox_not_permission_bypass(task):  # noqa: F811
    result = plan(task)
    assert result.version == "1.4.10"
    assert result.live_gate == "ANTIGRAVITY_AUTONOMOUS_LIVE_NOT_ADMITTED"
    assert result.auto_update_disabled
    assert result.timeout_seconds == task.timeout_seconds
    assert "--sandbox" in result.argv
    assert "--project=synthetic-task-project" in result.argv
    i = result.argv.index("--print-timeout")
    assert result.argv[i + 1] == "600s"
    assert not any(
        arg.startswith(
            (
                "--agent",
                "--model",
                "--json-schema",
                "--continue",
                "--conversation",
                "--dangerously",
                "--remote-control",
            )
        )
        for arg in result.argv
    )


def test_deferred_sandbox_plan_is_explicit_and_not_bypass_flags(task):  # noqa: F811
    result = plan(task, sandbox=False)
    assert result.live_gate == "ANTIGRAVITY_AUTONOMOUS_LIVE_NOT_ADMITTED"
    assert "--sandbox" not in result.argv
    assert result.sandbox is False
    assert not any(
        arg.startswith(
            (
                "--agent",
                "--model",
                "--json-schema",
                "--continue",
                "--conversation",
                "--dangerously",
                "--remote-control",
                "--yolo",
                "--no-sandbox",
            )
        )
        for arg in result.argv
    )


@pytest.mark.parametrize("version", ["1.3.5", "1.4.0", "1.4.9", "1.4.11", ""])
def test_new_plan_does_not_approve_other_versions(task, version):  # noqa: F811
    with pytest.raises(ValueError, match="VERSION_UNSUPPORTED"):
        plan(task, version=version)


@pytest.mark.parametrize(
    "changes",
    [
        {"project": "/synthetic/workspace"},
        {"project": "--dangerously-skip-permissions"},
        {"project": "x\n--other"},
        {"project": ""},
        {"executable": Path("relative")},
        {"executable": Path("/synthetic/../agy")},
        {"prompt": ""},
        {"prompt": "x\0y"},
    ],
)
def test_command_invalid_input_fails_closed(task, changes):  # noqa: F811
    with pytest.raises(ValueError, match="COMMAND_INVALID"):
        plan(task, **changes)


def test_multi_turn_tools_and_final_usage_not_double_counted(tmp_path):
    result = interpret(capture(tmp_path), tmp_path)
    assert result["status"] == "PASS"
    assert result["num_turns"] == 3
    assert result["usage"]["input_tokens"] == 30
    assert result["usage"]["output_tokens"] == 15
    assert result["physical_provider_requests"] == "UNKNOWN"
    assert result["agy_internal_retries"] == "UNKNOWN"
    assert result["sandbox_enforcement"] == "NOT_CERTIFIED_BY_CAPTURE"
    assert result["final_text_authority"] is False


def test_missing_usage_is_not_zero_cost(tmp_path):
    rows = capture(tmp_path)
    rows[-1]["result"].pop("usage")
    result = interpret(rows, tmp_path)
    assert result["status"] == "PASS"
    assert result["usage"] == {}
    assert result["usage_status"] == "NOT_REPORTED"


@pytest.mark.parametrize(
    "status", ["ERROR", "CANCELED", "INTERRUPTED", "INVALID", "WAITING", "RUNNING"]
)
def test_abnormal_or_waiting_final_cannot_be_success(tmp_path, status):
    rows = capture(tmp_path)
    rows[-1]["result"]["status"] = status
    assert interpret(rows, tmp_path)["status"] == "RUNTIME_ABNORMAL"


@pytest.mark.parametrize(
    "fault",
    [
        "no_final",
        "duplicate_final",
        "contradictory_final",
        "identity",
        "late_init",
        "incomplete_tool",
        "late_step",
        "duplicate_done",
        "index_regression",
        "unknown_tool",
        "recursion",
        "unknown_event",
        "unsafe_permission_mode",
        "schema",
        "negative_usage",
        "bad_turns",
    ],
)
def test_malformed_lifecycle_cannot_be_promoted(tmp_path, fault):
    rows = capture(tmp_path)
    if fault == "no_final":
        rows.pop()
    elif fault in {"duplicate_final", "contradictory_final"}:
        rows.append(json.loads(json.dumps(rows[-1])))
        if fault == "contradictory_final":
            rows[-1]["result"]["status"] = "ERROR"
    elif fault == "identity":
        rows[-1]["result"]["conversation_id"] = "other"
    elif fault == "late_init":
        rows[0], rows[1] = rows[1], rows[0]
    elif fault == "incomplete_tool":
        rows[2]["step_update"]["state"] = "ACTIVE"
    elif fault == "late_step":
        rows.append(rows[2])
    elif fault == "duplicate_done":
        rows.insert(3, rows[2])
    elif fault == "index_regression":
        rows[1]["step_update"]["step_index"] = 2
    elif fault == "unknown_tool":
        rows[2]["step_update"]["tool_name"] = "unadmitted_network_tool"
    elif fault == "recursion":
        rows[2]["step_update"]["subagent_info"] = {"id": "synthetic-child"}
    elif fault == "unknown_event":
        rows[2] = {"event": "new_unknown_event"}
    elif fault == "unsafe_permission_mode":
        rows[0]["init"]["permission_mode"] = "dangerously-skip"
    elif fault == "schema":
        rows[0]["init"]["json_schema"] = {"type": "object"}
    elif fault == "negative_usage":
        rows[-1]["result"]["usage"]["input_tokens"] = -1
    elif fault == "bad_turns":
        rows[-1]["result"]["num_turns"] = True
    result = interpret(rows, tmp_path)
    assert result["status"] == "UNKNOWN_OR_INCOMPLETE"
    assert result["failure"] == "AUTONOMOUS_STREAM_INVALID"


@pytest.mark.parametrize(
    "raw", [b"not-json", b"\xff", b'{"event":"init","event":"result"}']
)
def test_bad_bytes_and_duplicate_keys_controlled(tmp_path, raw):
    assert aa.interpret_autonomous_capture(raw, cwd=tmp_path, exit_code=0)["failure"]


@pytest.mark.parametrize("exit_code", [None, True, 1, -9])
def test_zero_exit_is_required_but_not_sufficient(tmp_path, exit_code):
    raw = b"\n".join(json.dumps(r).encode() for r in capture(tmp_path))
    assert (
        aa.interpret_autonomous_capture(raw, cwd=tmp_path, exit_code=exit_code)[
            "status"
        ]
        == "RUNTIME_ABNORMAL"
    )
    assert (
        aa.interpret_autonomous_capture(b"", cwd=tmp_path, exit_code=0)["status"]
        != "PASS"
    )


def test_deadline_overrides_success_and_denial_is_not_execution_proof(tmp_path):
    rows = capture(tmp_path)
    rows[-1]["result"]["denied_actions"] = [{"action": "synthetic-outside-probe"}]
    result = interpret(rows, tmp_path)
    assert result["status"] == "PASS" and result["permission_denials"] == 1
    assert interpret(rows, tmp_path, timed_out=True)["status"] == "TIMEOUT"


def test_fake_executable_accepts_plan_and_multiturn_stream(task):  # noqa: F811
    fixture = Path(__file__).parents[1] / "fixtures/fake_agy_autonomous.py"
    compiled = plan(task, executable=fixture.resolve())
    version = subprocess.run(
        [sys.executable, str(fixture), "--version"],
        capture_output=True,
        check=True,
        env={"PATH": "/usr/bin:/bin"},
        timeout=10,
    )
    assert version.stdout.strip() == b"1.4.10"
    proc = subprocess.run(
        [sys.executable, *compiled.argv],
        cwd=task.workspace_root,
        env={"PATH": "/usr/bin:/bin", "AGY_CLI_DISABLE_AUTO_UPDATE": "true"},
        capture_output=True,
        timeout=10,
        check=True,
    )
    result = aa.interpret_autonomous_capture(
        proc.stdout, cwd=task.workspace_root, exit_code=proc.returncode
    )
    assert result["status"] == "PASS" and result["num_turns"] == 2
    assert result["usage"]["output_tokens"] == 4


def test_child_environment_preserves_ssh_context_without_copying_parent_env(
    monkeypatch,
):
    monkeypatch.setenv("AGY_CLI_DISABLE_AUTO_UPDATE", "1")
    monkeypatch.setenv("HOME", "/synthetic/home")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/synthetic/runtime")
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "synthetic-dbus")
    monkeypatch.setenv("SSH_CLIENT", "synthetic-client")
    monkeypatch.setenv("SSH_CONNECTION", "synthetic-connection")
    monkeypatch.setenv("XDG_SESSION_TYPE", "tty")
    monkeypatch.setenv("FAKE_PARENT_CREDENTIAL", "do-not-copy")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)

    environment = aa.autonomous_child_environment()

    assert environment == {
        "AGY_CLI_DISABLE_AUTO_UPDATE": "true",
        "DBUS_SESSION_BUS_ADDRESS": "synthetic-dbus",
        "HOME": "/synthetic/home",
        "SSH_CLIENT": "synthetic-client",
        "SSH_CONNECTION": "synthetic-connection",
        "XDG_RUNTIME_DIR": "/synthetic/runtime",
        "XDG_SESSION_TYPE": "tty",
    }


def test_child_environment_fails_closed_when_required_context_is_missing(
    monkeypatch,
):
    for name in ("HOME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(KeyError):
        aa.autonomous_child_environment()


def test_real_launcher_executes_process_and_runs_parent_audit(
    task, tmp_path  # noqa: F811
):
    fixture = Path(__file__).parents[1] / "fixtures/fake_agy_autonomous.py"
    executable = tmp_path / "fake-agy"
    executable.write_text(f"#!{sys.executable}\n" + fixture.read_text())
    executable.chmod(0o700)
    approval = aa.AutonomousRuntimeApproval(
        executable=executable,
        executable_sha256=digest(executable.read_bytes()),
        project="synthetic-task-project",
        prompt=aa.AUTONOMOUS_PROMPT,
    )
    fixture.resolve().chmod(0o755)
    attempt = tmp_path / "attempt"
    task.workspace_root.chmod(0o775)
    result = aa.launch_autonomous_workspace(
        task=task,
        approval=approval,
        attempt=attempt,
        validator_mounts=(fixture.resolve(),),
    )
    assert result["status"] == "PASS"
    assert result["dca_logical_invocations"] == 1
    assert result["dca_retries"] == 0
    assert result["sandbox_requested"] is True
    assert result["sandbox_enforcement"] == "REQUESTED_NOT_CERTIFIED"
    assert result["ssh_context_inherited"]
    assert (task.workspace_root / "src/labels.py").read_text() == "value = 1\n"
    assert result["audit"]["status"] == "FAIL"
    assert result["audit"]["validators"][0]["status"] == "DEFERRED"
    assert result["audit"]["validator_sandbox_enforcement"] == "DEFERRED"
    assert result["audit"]["new_paths"] == []
    manifest = json.loads((attempt / "sha256.json").read_text())
    assert all(
        digest((attempt / name).read_bytes()) == expected
        for name, expected in manifest.items()
    )


def test_real_launcher_defers_native_sandbox_but_keeps_parent_audit(
    task, tmp_path  # noqa: F811
):
    fixture = Path(__file__).parents[1] / "fixtures/fake_agy_autonomous.py"
    executable = _fake_executable(tmp_path, fixture.read_text())
    attempt = tmp_path / "attempt"
    task.workspace_root.chmod(0o775)
    result = aa.launch_autonomous_workspace(
        task=task,
        approval=_approval(executable),
        attempt=attempt,
        sandbox=False,
    )
    assert result["sandbox_requested"] is False
    assert result["sandbox_enforcement"] == "UNSANDBOXED/DEFERRED"
    assert result["status"] == "PASS"
    assert result["audit"]["scope"] == "PASS"
    assert result["audit"]["functional"] == "FAIL"
    assert result["audit"]["validators"][0]["status"] == "DEFERRED"
    assert result["audit"]["validator_sandbox_enforcement"] == "DEFERRED"
    manifest = json.loads((attempt / "sha256.json").read_text())
    assert all(
        digest((attempt / name).read_bytes()) == expected
        for name, expected in manifest.items()
    )


def test_real_launcher_rejects_prompt_override(task, tmp_path):  # noqa: F811
    fixture = Path(__file__).parents[1] / "fixtures/fake_agy_autonomous.py"
    approval = aa.AutonomousRuntimeApproval(
        executable=fixture.resolve(),
        executable_sha256=fixture.read_bytes().hex(),
        project="synthetic-task-project",
        prompt="different prompt",
    )
    with pytest.raises(ValueError, match="RUNTIME_INPUT_INVALID"):
        aa.launch_autonomous_workspace(
            task=task,
            approval=approval,
            attempt=tmp_path / "attempt",
        )


def test_real_launcher_rejects_binary_drift(task, tmp_path):  # noqa: F811
    fixture = Path(__file__).parents[1] / "fixtures/fake_agy_autonomous.py"
    executable = tmp_path / "fake-agy"
    executable.write_text(fixture.read_text())
    executable.chmod(0o700)
    approval = aa.AutonomousRuntimeApproval(
        executable=executable,
        executable_sha256="0" * 64,
        project="synthetic-task-project",
        prompt=aa.AUTONOMOUS_PROMPT,
    )
    with pytest.raises(ValueError, match="BINARY_DRIFT"):
        aa.launch_autonomous_workspace(
            task=task,
            approval=approval,
            attempt=tmp_path / "attempt",
        )


def test_historical_strict_route_not_admitted_for_1410():
    from dispatcher_for_codex_agents.agent_harness.contracts import AgentTask

    with pytest.raises(ValueError, match="VERSION_UNSUPPORTED"):
        compile_headless(
            task=AgentTask(
                task_id="synthetic",
                role="diagnostic",
                prompt_template="synthetic",
                approved_input_files=("synthetic.tsv",),
                selected_columns=("id",),
                timeout=10,
                call_limit=1,
                expected_output_schema={"type": "object"},
            ),
            executable="/synthetic/agy",
            model=None,
            prompt="synthetic",
            version="1.4.10",
        )


def _fake_executable(tmp_path, body):
    executable = tmp_path / "fake-agy"
    executable.write_text(f"#!{sys.executable}\n" + body)
    executable.chmod(0o700)
    return executable


def _approval(executable):
    return aa.AutonomousRuntimeApproval(
        executable=executable,
        executable_sha256=digest(executable.read_bytes()),
        project="synthetic-task-project",
        prompt=aa.AUTONOMOUS_PROMPT,
    )


def test_runtime_version_probe_accepts_only_pinned_1410(tmp_path, monkeypatch):
    fixture = Path(__file__).parents[1] / "fixtures/fake_agy_autonomous.py"
    executable = _fake_executable(tmp_path, fixture.read_text())
    calls = []
    real_run = aa.subprocess.run

    def spy(argv, **kwargs):
        calls.append(list(argv))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(aa.subprocess, "run", spy)
    aa._probe_runtime_version(
        _approval(executable),
        cwd=tmp_path,
        environment=aa.autonomous_child_environment(),
    )
    # Exactly one no-prompt call: a --print here would be a model invocation.
    assert calls == [[str(executable), "--version"]]


@pytest.mark.parametrize("reported", ["1.3.5", "1.4.9", "1.4.11", "unknown"])
def test_runtime_version_probe_rejects_unpinned_report(tmp_path, reported):
    fixture = Path(__file__).parents[1] / "fixtures/fake_agy_autonomous.py"
    body = fixture.read_text().replace('print("1.4.10")', f'print("{reported}")')
    with pytest.raises(ValueError, match="VERSION_UNSUPPORTED"):
        aa._probe_runtime_version(
            _approval(_fake_executable(tmp_path, body)),
            cwd=tmp_path,
            environment=aa.autonomous_child_environment(),
        )


def test_runtime_version_probe_fails_closed_on_probe_error(tmp_path):
    executable = tmp_path / "not-executable"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o600)
    with pytest.raises(ValueError, match="VERSION_PROBE_FAILED"):
        aa._probe_runtime_version(
            _approval(executable),
            cwd=tmp_path,
            environment=aa.autonomous_child_environment(),
        )


def test_launcher_rejects_unpinned_runtime_before_attempt(task, tmp_path):  # noqa: F811
    fixture = Path(__file__).parents[1] / "fixtures/fake_agy_autonomous.py"
    body = fixture.read_text().replace('print("1.4.10")', 'print("1.4.9")')
    task.workspace_root.chmod(0o775)
    before = (task.workspace_root / "src/labels.py").read_bytes()
    attempt = tmp_path / "attempt"
    with pytest.raises(ValueError, match="VERSION_UNSUPPORTED"):
        aa.launch_autonomous_workspace(
            task=task,
            approval=_approval(_fake_executable(tmp_path, body)),
            attempt=attempt,
        )
    # Fail-closed: no attempt record and no prompt-bearing mutation happened.
    assert not attempt.exists()
    assert (task.workspace_root / "src/labels.py").read_bytes() == before


def test_launcher_rechecks_binary_after_probe_before_attempt(
    task, tmp_path, monkeypatch  # noqa: F811
):
    fixture = Path(__file__).parents[1] / "fixtures/fake_agy_autonomous.py"
    executable = _fake_executable(tmp_path, fixture.read_text())
    approval = _approval(executable)
    original_run = aa.subprocess.run

    def drift_after_probe(*args, **kwargs):
        result = original_run(*args, **kwargs)
        executable.write_text(executable.read_text() + "\n# drifted\n")
        return result

    monkeypatch.setattr(aa.subprocess, "run", drift_after_probe)
    with pytest.raises(ValueError, match="BINARY_DRIFT"):
        aa.launch_autonomous_workspace(
            task=task,
            approval=approval,
            attempt=tmp_path / "attempt",
        )
    assert not (tmp_path / "attempt").exists()
