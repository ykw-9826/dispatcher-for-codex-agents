"""Synthetic post-audit: never launches an official runtime."""

import json
import os
import subprocess

import pytest

from dispatcher_for_codex_agents.agent_harness.antigravity_coding import (
    interpret_coding_capture,
)
from dispatcher_for_codex_agents.agent_harness.workspace_artifact import (
    ValidatorCommand,
    WorkspaceArtifactTask,
    WorkspaceContractError,
    audit_workspace,
    freeze_workspace,
)


def git(root, *args):
    env = {
        "PATH": "/usr/bin:/bin",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }
    return subprocess.check_output(
        ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", *args], cwd=root, env=env
    )


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "benchmark"
    root.mkdir()
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / "src/labels.py").write_text("value = 0\n")
    (root / "tests/test_labels.py").write_text("# immutable test\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "--", "src/labels.py", "tests/test_labels.py")
    git(
        root,
        "-c",
        "user.name=Synthetic",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "Synthetic baseline",
    )
    task = WorkspaceArtifactTask(
        workspace_root=root,
        baseline_commit=git(root, "rev-parse", "HEAD").decode().strip(),
        allowed_write_paths=("src/labels.py",),
        validator_commands=(ValidatorCommand(name="public", argv=("/bin/true",)),),
    )
    return task, freeze_workspace(task)


def test_actual_bytes_not_index_or_final_text(workspace):
    task, baseline = workspace
    assert audit_workspace(task, baseline)["failure"] == "NO_IMPLEMENTATION_CHANGE"
    (task.workspace_root / "src/labels.py").write_text("value = 1\n")
    result = audit_workspace(task, baseline)
    assert result["status"] == "PASS"
    assert result["changed_paths"] == ["src/labels.py"]
    git(task.workspace_root, "add", "--", "src/labels.py")
    assert audit_workspace(task, baseline)["status"] == "PASS"


@pytest.mark.parametrize(
    "kind",
    [
        "test",
        "untracked",
        "ignored",
        "delete",
        "symlink",
        "hardlink",
        "mode",
        "branch",
        "commit",
        "remote",
        "config",
        "dirty_diff",
    ],
)
def test_scope_and_git_tampering_fail_closed(workspace, kind, tmp_path):
    task, before = workspace
    root = task.workspace_root
    target = root / "src/labels.py"
    target.write_text("value = 1\n")
    if kind == "test":
        (root / "tests/test_labels.py").write_text("# fake success\n")
    elif kind == "untracked":
        (root / "extra").write_text("x")
    elif kind == "ignored":
        (root / ".git/info/exclude").write_text("hidden\n")
        (root / "hidden").write_text("x")
    elif kind == "delete":
        target.unlink()
    elif kind == "symlink":
        target.unlink()
        target.symlink_to(root / "tests/test_labels.py")
    elif kind == "hardlink":
        os.link(target, tmp_path / "alias")
    elif kind == "mode":
        target.chmod(0o755)
    elif kind == "branch":
        git(root, "branch", "other")
        git(root, "symbolic-ref", "HEAD", "refs/heads/other")
    elif kind == "commit":
        git(root, "add", "--", "src/labels.py")
        git(
            root,
            "-c",
            "user.name=Synthetic",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "Unauthorized",
        )
    elif kind == "remote":
        git(root, "remote", "add", "origin", "https://example.invalid/repo")
    elif kind == "config":
        with (root / ".git/config").open("a") as f:
            f.write("[include]\npath = /synthetic/not-read\n")
    else:
        target.write_text("value = 1  \n")
    assert audit_workspace(task, before)["status"] == "FAIL"


@pytest.mark.parametrize(
    "name",
    [
        "/tmp/x",
        "../x",
        "src/../x",
        "tests/test_labels.py",
        ".git/config",
        "src\\escape",
        "src/./labels.py",
        "",
    ],
)
def test_allowed_paths_are_exact_sources(workspace, name):
    with pytest.raises(ValueError):
        WorkspaceArtifactTask(
            **{**workspace[0].model_dump(), "allowed_write_paths": [name]}
        )


def test_dirty_baseline_rejected(workspace):
    task, _ = workspace
    (task.workspace_root / "extra").write_text("x")
    with pytest.raises(WorkspaceContractError):
        freeze_workspace(task)


def capture(root, *, status="SUCCESS", tool="write_to_file"):
    return [
        {
            "event": "init",
            "conversation_id": "synthetic",
            "init": {
                "cwd": str(root),
                "tools": ["write_to_file", "run_command"],
                "permission_mode": "request-review",
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
                "tool_info": {"name": tool},
            },
        },
        {
            "event": "result",
            "result": {
                "conversation_id": "synthetic",
                "status": status,
                "response": "diagnostic, not JSON or success authority",
                "num_turns": 1,
                "denied_actions": [{"action": "command(synthetic)"}],
                "usage": {"input_tokens": 10, "output_tokens": 5, "thinking_tokens": 2},
            },
        },
    ]


def test_coding_allows_tools_and_no_schema_without_changing_strict_mode(tmp_path):
    from test_agent_harness import _task

    from dispatcher_for_codex_agents.agent_harness.antigravity import (
        evaluate_headless_capture,
    )

    raw = "\n".join(map(json.dumps, capture(tmp_path))).encode()
    result = interpret_coding_capture(raw, cwd=tmp_path, exit_code=0)
    assert result["status"] == "PASS"
    assert result["permission_denials"] == 1
    assert result["usage"]["output_tokens"] == 5
    assert result["physical_provider_requests"] == "UNKNOWN"
    strict = evaluate_headless_capture(
        task=_task(tmp_path / "not-read"),
        stdout=raw,
        exit_code=0,
        elapsed=0.1,
        version="1.2.13",
    )
    assert strict.status == "failure"


@pytest.mark.parametrize(
    "change",
    [
        "partial",
        "duplicate",
        "error",
        "unknown",
        "identity",
        "subagent",
        "network",
        "input_order",
        "tool_error",
        "timeout",
        "nonzero",
    ],
)
def test_coding_runtime_abnormal_not_accepted(tmp_path, change):
    seq = capture(tmp_path)
    if change == "partial":
        seq.pop()
    elif change == "duplicate":
        seq.append(seq[-1])
    elif change == "error":
        seq[-1]["result"]["status"] = "ERROR"
    elif change == "unknown":
        seq.insert(-1, {"event": "unknown"})
    elif change == "identity":
        seq[-1]["result"]["conversation_id"] = "different"
    elif change == "subagent":
        seq[2]["step_update"]["subagent_info"] = {}
    elif change == "network":
        seq[2]["step_update"]["tool_info"]["name"] = "read_url_content"
    elif change == "input_order":
        seq[1], seq[2] = seq[2], seq[1]
    elif change == "tool_error":
        seq[-1]["result"]["error"] = "synthetic hard error"
    raw = "\n".join(map(json.dumps, seq)).encode()
    result = interpret_coding_capture(
        raw,
        cwd=tmp_path,
        exit_code=1 if change == "nonzero" else 0,
        timed_out=change == "timeout",
    )
    assert result["status"] != "PASS"


def test_no_change_is_artifact_failure_not_scope_violation(workspace):
    task, baseline = workspace
    outcome = audit_workspace(task, baseline)
    assert outcome["status"] == "FAIL"
    assert outcome["scope_integrity"] == "PASS"
    assert outcome["failure"] == "NO_IMPLEMENTATION_CHANGE"


def test_malformed_baseline_git_config_error_is_controlled(workspace):
    task, _ = workspace
    (task.workspace_root / ".git/config").write_text("SYNTHETIC_PRIVATE_TEXT")
    with pytest.raises(WorkspaceContractError, match="^WORKSPACE_BASELINE_INVALID$"):
        freeze_workspace(task)


def test_preinit_transport_error_is_not_coding_success(tmp_path):
    raw = json.dumps(
        {
            "event": "result",
            "result": {
                "conversation_id": "",
                "status": "ERROR",
                "num_turns": 0,
                "error": "synthetic TLS handshake timeout",
                "response": "",
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        }
    ).encode()
    result = interpret_coding_capture(raw, cwd=tmp_path, exit_code=1)
    assert result["status"] == "RUNTIME_ABNORMAL"
    assert result["failure"] == "PROCESS_EXIT"


def test_validator_sandbox_does_not_inherit_credentials(
    workspace, tmp_path, monkeypatch
):
    from dispatcher_for_codex_agents.agent_harness import workspace_artifact

    calls = []
    monkeypatch.setenv("SYNTHETIC_MODEL_CREDENTIAL", "DO_NOT_INHERIT")

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout=b"PASS", stderr=b"")

    monkeypatch.setattr(workspace_artifact.subprocess, "run", run)
    result = workspace_artifact.run_validators(
        workspace[0], mounts=(), output=tmp_path / "validation"
    )
    argv, options = calls[0]
    assert "--unshare-all" in argv and "--clearenv" in argv
    assert "--ro-bind" in argv and "--bind" not in argv
    assert options["env"] == {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    assert not any("DO_NOT_INHERIT" in v for v in argv)
    assert result[0]["status"] == "PASS"
