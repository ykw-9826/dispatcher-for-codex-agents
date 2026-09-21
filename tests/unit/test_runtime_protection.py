"""Offline selected-installation deny boundary, separate from runtime grants."""

import hashlib
import json
from pathlib import Path

import pytest
from test_agent_capabilities import config_overrides, invoke, policy_task
from test_agent_harness import _adapter, _profile, _shard
from test_agent_harness_cli import _verify_hashes

from dispatcher_for_codex_agents.agent_harness import FailureCode
from dispatcher_for_codex_agents.agent_harness.capabilities import CapabilityError
from dispatcher_for_codex_agents.agent_harness.shard import (
    ImmutableShardWriter,
    ShardExistsError,
)


def prepared(tmp_path, monkeypatch):
    adapter, task = _adapter(tmp_path, "success")
    binary = Path(adapter._executable[0])
    release = binary.parents[1]
    (release / "helper.dat").write_text("synthetic runtime material")
    (release / "data").mkdir()
    assert not release.is_relative_to(adapter._resolver.codex_home)
    adapter._cli_version = "codex-cli 0.155.1"

    def forbidden(*args, **kwargs):
        pytest.fail("No subprocess, including version checks, is permitted")

    monkeypatch.setattr("subprocess.run", forbidden)
    monkeypatch.setattr("subprocess.Popen", forbidden)
    return adapter, task, binary, release


def verify_failure(adapter, task, tmp_path):
    result = invoke(adapter, task, tmp_path)
    assert result.failure_code == FailureCode.POLICY_VIOLATION
    assert adapter.calls_started == result.provenance["agent_subprocess_count"] == 0
    proof = result.provenance["capability_policy"]
    assert proof["compiled_command_policy"] is None
    assert proof["internal_runtime_support_grants"] == []
    shard = _shard(tmp_path, "one")
    assert {p.name for p in shard.iterdir()} == set(ImmutableShardWriter.REQUIRED_FILES)
    _verify_hashes(shard)
    before = {p.name: p.read_bytes() for p in shard.iterdir()}
    with pytest.raises(ShardExistsError):
        invoke(adapter, task, tmp_path)
    assert before == {p.name: p.read_bytes() for p in shard.iterdir()}
    return proof


@pytest.mark.parametrize("access", ["read_paths", "write_paths"])
@pytest.mark.parametrize("target", ["binary", "root", "sibling", "subdir", "parent"])
def test_release_boundary_rejects_all_user_overlaps(
    tmp_path, monkeypatch, access, target
):
    adapter, task, binary, release = prepared(tmp_path, monkeypatch)
    path = {
        "binary": binary,
        "root": release,
        "sibling": release / "helper.dat",
        "subdir": release / "data",
        "parent": release.parent,
    }[target]
    task = policy_task(task, tools=("shell",), **{access: (str(path),)})
    with pytest.raises(CapabilityError, match="overlaps"):
        adapter.preflight(task=task, profile=_profile())
    assert not (tmp_path / "run").exists()
    proof = verify_failure(adapter, task, tmp_path)
    scope = proof["runtime_protection"]
    assert scope["user_grants_check"] == "REJECTED"
    assert scope["protected_root"] == str(release)
    assert scope["runtime_layout"] == "CODEX_STANDALONE_RELEASE"


@pytest.mark.parametrize("access", ["read_paths", "write_paths"])
def test_adjacent_grants_and_three_authorities_stay_separate(
    tmp_path, monkeypatch, access
):
    adapter, task, binary, release = prepared(tmp_path, monkeypatch)
    adjacent = release.with_name(release.name + "-adjacent-data")
    adjacent.mkdir()
    task = policy_task(task, tools=("shell",), **{access: (str(adjacent),)})
    preview = adapter.preflight(task=task, profile=_profile())
    proof = preview["capability_policy"]
    scope = proof["runtime_protection"]
    assert scope["protected_root"] == str(release)
    assert scope["user_grants_check"] == "PASS"
    assert scope["selected_executable"] == str(binary)
    assert scope["executable_sha256"] == hashlib.sha256(binary.read_bytes()).hexdigest()
    requested = task.capability_policy.model_dump(mode="json")
    assert proof["requested"] == requested
    assert (
        proof["requested_policy_sha256"]
        == hashlib.sha256(json.dumps(requested, sort_keys=True).encode()).hexdigest()
    )
    compiled = proof["compiled_command_policy"]
    assert "runtime_protection" not in compiled
    assert (
        proof["compiled_command_policy_sha256"]
        == hashlib.sha256(json.dumps(compiled, sort_keys=True).encode()).hexdigest()
    )
    assert [g["path"] for g in proof["internal_runtime_support_grants"]] == [
        str(binary)
    ]
    fs = config_overrides(preview["command"])["permissions"]["dca_task"]["filesystem"]
    assert fs == {
        ":minimal": "read",
        compiled["working_directory"]: "read",
        str(adjacent): "read" if access == "read_paths" else "write",
        str(binary): "read",
    }
    assert str(release) not in fs and str(binary.parent) not in fs


@pytest.mark.parametrize(
    "layout",
    [
        "codex",
        "bin/codex",
        "releases/0.test-arch/bin/codex",
        "standalone/versions/0.test-arch/bin/codex",
        "standalone/releases/0.test-arch/codex",
        "standalone/releases/0.test-arch/bin/other",
        "standalone/releases/.hidden/bin/codex",
    ],
)
def test_unknown_layout_rejected_before_even_version_subprocess(
    tmp_path, monkeypatch, layout
):
    adapter, task, _, _ = prepared(tmp_path, monkeypatch)
    binary = tmp_path / "unrecognized" / layout
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("never executed")
    binary.chmod(0o700)
    adapter._executable = (str(binary),)
    adapter._cli_version = None
    task = policy_task(task, tools=("shell",))
    with pytest.raises(CapabilityError, match="RUNTIME_PROTECTION_BOUNDARY_UNRESOLVED"):
        adapter.preflight(task=task, profile=_profile())
    proof = verify_failure(adapter, task, tmp_path)
    assert proof["runtime_protection"]["runtime_layout"] == "UNRESOLVED"
    assert proof["runtime_protection"]["protected_root"] is None
    assert proof["runtime_protection"]["user_grants_check"] == "NOT_PERFORMED"


def test_absent_policy_does_not_need_layout_resolution(tmp_path):
    import sys

    adapter, task = _adapter(tmp_path, "success")
    adapter._executable = (
        str(Path(sys.executable).resolve()),
        *adapter._executable[1:],
    )
    preview = adapter.preflight(task=task, profile=_profile())
    assert preview["capability_policy"]["runtime_protection"] is None
    assert preview["capability_policy"]["internal_runtime_support_grants"] == []
    result = invoke(adapter, task, tmp_path)
    assert result.status == "success"
    assert result.provenance["capability_policy"]["runtime_protection"] is None


@pytest.mark.parametrize(
    "kind", ["relative", "dotdot", "symlink", "hardlink", "double_separator"]
)
def test_selected_runtime_aliases_fail_closed(tmp_path, monkeypatch, kind):
    import os

    adapter, task, binary, _ = prepared(tmp_path, monkeypatch)
    selected = str(binary)
    if kind == "relative":
        selected = "runtime/packages/standalone/releases/0.test-arch/bin/codex"
    elif kind == "dotdot":
        selected = str(binary.parent / ".." / "bin" / "codex")
    elif kind == "double_separator":
        selected = str(binary).replace("/bin/codex", "/bin//codex")
    elif kind == "symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(binary.parents[1], target_is_directory=True)
        selected = str(alias / "bin/codex")
    else:
        os.link(binary, tmp_path / "hardlink")
    adapter._executable = (selected,)
    task = policy_task(task, tools=("shell",))
    with pytest.raises(ValueError):
        adapter.preflight(task=task, profile=_profile())
    verify_failure(adapter, task, tmp_path)


def test_protection_metadata_is_not_a_user_task_field(tmp_path):
    from pydantic import ValidationError

    from dispatcher_for_codex_agents.agent_harness import CapabilityPolicy

    for name in ("runtime_protection", "internal_runtime_support_grants"):
        with pytest.raises(ValidationError):
            CapabilityPolicy.model_validate({name: {"protected_root": str(tmp_path)}})


def test_root_identity_replacement_after_preflight_fails_closed(tmp_path, monkeypatch):
    adapter, task, _, release = prepared(tmp_path, monkeypatch)
    task = policy_task(task, tools=("shell",))
    adapter.preflight(task=task, profile=_profile())
    moved = release.with_name("moved-release")
    release.rename(moved)
    release.mkdir()
    # Preserve exact binary inode/hash while replacing only its release boundary.
    (moved / "bin").rename(release / "bin")
    with pytest.raises(CapabilityError, match="protection scope changed"):
        adapter.preflight(task=task, profile=_profile())
    verify_failure(adapter, task, tmp_path)
