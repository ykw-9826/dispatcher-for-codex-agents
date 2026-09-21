"""Exact self-exec support is internal, never a caller filesystem grant."""

import hashlib
import json
import os
from pathlib import Path

import pytest
from test_agent_capabilities import config_overrides, invoke, policy_task
from test_agent_harness import _adapter, _profile, _shard
from test_agent_harness_cli import _verify_hashes

from dispatcher_for_codex_agents.agent_harness import CapabilityPolicy, FailureCode
from dispatcher_for_codex_agents.agent_harness.capabilities import CapabilityError
from dispatcher_for_codex_agents.agent_harness.shard import (
    ImmutableShardWriter,
    ShardExistsError,
)


def prepared(tmp_path):
    adapter, task = _adapter(tmp_path, "success")
    # Fixture interpreter is explicitly canonical, just as a production binary.
    adapter._executable = (
        str(Path(adapter._executable[0]).resolve()),
        *adapter._executable[1:],
    )
    return adapter, policy_task(task, tools=("shell",))


def test_exact_internal_runtime_grant_and_unchanged_user_authority(tmp_path):
    adapter, task = prepared(tmp_path)
    preview = adapter.preflight(task=task, profile=_profile())
    proof = preview["capability_policy"]
    compiled = proof["compiled_command_policy"]
    (grant,) = compiled["internal_runtime_support_grants"]
    binary = Path(adapter._executable[0])
    assert grant["source"] == "CODEX_SELF_EXEC_COMPAT"
    assert grant["path"] == str(binary)
    assert grant["path_type"] == "file"
    assert grant["access"] == "READ_EXEC_SUPPORT"
    assert grant["filesystem_access"] == "read"
    assert grant["sha256"] == hashlib.sha256(binary.read_bytes()).hexdigest()
    assert grant["injected_by_dca"] is True
    assert "0.153.4" in grant["selected_codex_version"]
    assert proof["requested"] == task.capability_policy.model_dump(mode="json")
    assert proof["requested"]["read_paths"] == []
    assert (
        proof["requested_policy_sha256"]
        == hashlib.sha256(
            json.dumps(proof["requested"], sort_keys=True).encode()
        ).hexdigest()
    )
    fs = compiled["filesystem"]
    assert fs[str(binary)] == "read"
    assert str(binary.parent) not in fs
    assert str(adapter._resolver.codex_home) not in fs
    assert (
        fs
        == config_overrides(preview["command"])["permissions"]["dca_task"]["filesystem"]
    )
    assert preview["command"][0] == str(binary)
    result = invoke(adapter, task, tmp_path)
    assert result.status == "success"
    _verify_hashes(_shard(tmp_path, "one"))
    assert result.provenance["capability_policy"][
        "internal_runtime_support_grants"
    ] == [grant]


@pytest.mark.parametrize("scope", ["binary", "release", "home"])
@pytest.mark.parametrize("access", ["read_paths", "write_paths"])
def test_codex_home_user_requests_still_rejected(tmp_path, scope, access):
    adapter, task = prepared(tmp_path)
    home = adapter._resolver.codex_home
    release = home / "packages" / "releases" / "selected"
    release.mkdir(parents=True)
    binary = release / "codex"
    binary.write_text("synthetic executable")
    binary.chmod(0o700)
    target = {"binary": binary, "release": release, "home": home}[scope]
    task = policy_task(task, tools=("shell",), **{access: (str(target),)})
    result = invoke(adapter, task, tmp_path)
    assert result.failure_code == FailureCode.POLICY_VIOLATION
    assert adapter.calls_started == 0
    _verify_hashes(_shard(tmp_path, "one"))


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "directory",
        "nonexec",
        "symlink",
        "parent_symlink",
        "hardlink",
        "relative",
    ],
)
def test_bad_binary_fails_controlled_without_agent(tmp_path, kind):
    adapter, task = prepared(tmp_path)
    binary = tmp_path / "bin" / "codex"
    binary.parent.mkdir()
    binary.write_text("synthetic executable, never run")
    binary.chmod(0o700)
    selected = binary
    if kind == "missing":
        selected = binary.parent / "missing"
    elif kind == "directory":
        selected = binary.parent
    elif kind == "nonexec":
        binary.chmod(0o600)
    elif kind == "symlink":
        selected = tmp_path / "alias"
        selected.symlink_to(binary)
    elif kind == "parent_symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(binary.parent, target_is_directory=True)
        selected = alias / "codex"
    elif kind == "hardlink":
        os.link(binary, tmp_path / "hardlink")
    else:
        selected = Path("relative/codex")
    adapter._executable = (str(selected),)
    adapter._cli_version = "codex-cli 0.155.1"  # No diagnostic subprocess either.
    with pytest.raises(ValueError):
        adapter.preflight(task=task, profile=_profile())
    assert not (tmp_path / "run").exists()
    result = invoke(adapter, task, tmp_path)
    assert result.failure_code == FailureCode.POLICY_VIOLATION
    assert adapter.calls_started == 0
    shard = _shard(tmp_path, "one")
    assert set(p.name for p in shard.iterdir()) == set(
        ImmutableShardWriter.REQUIRED_FILES
    )
    _verify_hashes(shard)
    assert not result.provenance["capability_policy"]["compiled_command_policy_emitted"]
    before = {p.name: p.read_bytes() for p in shard.iterdir()}
    with pytest.raises(ShardExistsError):
        invoke(adapter, task, tmp_path)
    assert before == {p.name: p.read_bytes() for p in shard.iterdir()}


@pytest.mark.parametrize(
    "mutation", ["content", "replace", "symlink", "hardlink", "chmod", "selection"]
)
def test_binary_change_after_preflight_fails_before_agent(tmp_path, mutation):
    adapter, task = prepared(tmp_path)
    binary = Path(adapter._executable[0])
    binary.write_text("pinned fake binary")
    binary.chmod(0o700)
    adapter._executable = (str(binary),)
    adapter._cli_version = "codex-cli 0.155.1"
    adapter.preflight(task=task, profile=_profile())
    if mutation == "content":
        binary.write_text("modified fake binary")
    elif mutation in {"replace", "symlink", "selection"}:
        other = tmp_path / "other"
        other.write_bytes(binary.read_bytes())
        other.chmod(0o700)
        if mutation == "replace":
            other.replace(binary)
        elif mutation == "symlink":
            binary.unlink()
            binary.symlink_to(other)
        else:
            adapter._executable = (str(other),)
    elif mutation == "hardlink":
        os.link(binary, tmp_path / "alias")
    else:
        binary.chmod(0o600)
    result = invoke(adapter, task, tmp_path)
    assert result.failure_code == FailureCode.POLICY_VIOLATION
    assert adapter.calls_started == 0
    _verify_hashes(_shard(tmp_path, "one"))


def test_default_policy_does_not_add_runtime_grant(tmp_path):
    adapter, task = _adapter(tmp_path, "success")
    preview = adapter.preflight(task=task, profile=_profile())
    proof = preview["capability_policy"]
    assert proof["internal_runtime_support_grants"] == []
    assert proof["compiled_command_policy"]["filesystem"] == {":root": "read"}
    assert "--enable" not in preview["command"]
    assert task.capability_policy == CapabilityPolicy()


def test_change_at_final_launch_checkpoint_has_complete_failure_shard(
    tmp_path, monkeypatch
):
    from dispatcher_for_codex_agents.agent_harness import adapter as module

    adapter, task = prepared(tmp_path)
    binary = Path(adapter._executable[0])
    binary.write_text("synthetic executable, never run")
    binary.chmod(0o700)
    adapter._executable = (str(binary),)
    adapter._cli_version = "codex-cli 0.155.1"

    def mutate_before_popen(*args, **kwargs):
        binary.write_text("changed at launch")

    def forbidden_popen(*args, **kwargs):
        pytest.fail("agent subprocess must not start")

    monkeypatch.setattr(module, "record_invocation_process", mutate_before_popen)
    monkeypatch.setattr(module.subprocess, "Popen", forbidden_popen)
    result = invoke(adapter, task, tmp_path)
    assert result.failure_code == FailureCode.POLICY_VIOLATION
    assert result.provenance["agent_subprocess_count"] == 0
    _verify_hashes(_shard(tmp_path, "one"))


@pytest.mark.parametrize("parent", [False, True])
def test_write_grant_cannot_cover_selected_runtime_outside_codex_home(tmp_path, parent):
    adapter, task = prepared(tmp_path)
    directory = tmp_path / "runtime-binary"
    directory.mkdir()
    binary = directory / "codex"
    binary.write_text("not executed")
    binary.chmod(0o700)
    adapter._executable = (str(binary),)
    adapter._cli_version = "codex-cli 0.155.1"
    task = policy_task(
        task, write_paths=(str(directory if parent else binary),), tools=("shell",)
    )
    result = invoke(adapter, task, tmp_path)
    assert result.failure_code == FailureCode.POLICY_VIOLATION
    assert result.provenance["agent_subprocess_count"] == 0


def test_path_resolution_is_pinned_in_generated_command(tmp_path, monkeypatch):
    adapter, task = prepared(tmp_path)
    binary = Path(adapter._executable[0])
    binary.write_text("not executed")
    binary.chmod(0o700)
    monkeypatch.setenv("PATH", str(binary.parent))
    adapter._executable = ("codex",)
    adapter._cli_version = "codex-cli 0.155.1"
    preview = adapter.preflight(task=task, profile=_profile())
    assert preview["command"][0] == str(binary)
    assert preview["capability_policy"]["internal_runtime_support_grants"][0][
        "path"
    ] == str(binary)


def test_runtime_identity_io_error_is_sanitized(tmp_path, monkeypatch):
    from dispatcher_for_codex_agents.agent_harness.capabilities import (
        InternalRuntimeGrant,
    )

    binary = tmp_path / "codex"
    binary.write_text("not a model")
    binary.chmod(0o700)

    def denied(*args, **kwargs):
        raise PermissionError("SYNTHETIC_SECRET_DO_NOT_ECHO")

    monkeypatch.setattr(os, "open", denied)
    with pytest.raises(CapabilityError) as error:
        InternalRuntimeGrant.capture(str(binary))
    assert "SYNTHETIC_SECRET" not in str(error.value)
