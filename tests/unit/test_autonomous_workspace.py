"""Autonomous workspace acceptance tests use only synthetic repositories."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from test_workspace_artifact import git, workspace  # noqa: F401

from dispatcher_for_codex_agents.agent_harness import autonomous_workspace as aw
from dispatcher_for_codex_agents.agent_harness.workspace_artifact import (
    digest,
)


@pytest.fixture
def task(workspace):  # noqa: F811
    original, _ = workspace
    return aw.AutonomousWorkspaceTask(
        workspace_root=original.workspace_root,
        baseline_commit=original.baseline_commit,
        protected_paths=("tests/test_labels.py",),
        validator_commands=original.validator_commands,
    )


def finalize(task, baseline, tmp_path, monkeypatch, *, runtime="PASS", valid=True):
    calls = []

    def validators(*args, **kwargs):
        calls.append(True)
        return [{"name": "public", "status": "PASS" if valid else "FAIL"}]

    monkeypatch.setattr(aw, "run_validators", validators)
    result = aw.finalize_autonomous_workspace(
        task=task,
        baseline=baseline,
        runtime_status=runtime,
        validator_support=aw.freeze_validator_support(task, ()),
        validator_mounts=(),
        output=tmp_path / "acceptance",
    )
    manifest = json.loads((tmp_path / "acceptance/sha256.json").read_text())
    assert all(
        digest((tmp_path / "acceptance" / n).read_bytes()) == h
        for n, h in manifest.items()
    )
    return result, calls


def test_full_workspace_new_source_and_build_are_not_exact_file_violation(
    task, tmp_path, monkeypatch
):
    before = aw.freeze_autonomous_workspace(task)
    root = task.workspace_root
    (root / "src/labels.py").write_text("value = 1\n")
    (root / "src/new_module.py").write_text("value = 2\n")
    (root / "build").mkdir()
    (root / "build/result.bin").write_bytes(b"\x00synthetic-build")
    result, calls = finalize(task, before, tmp_path, monkeypatch)
    assert (
        result["status"]
        == result["scope"]
        == result["functional"]
        == result["hygiene"]
        == "PASS"
    )
    assert result["new_paths"] == ["build/result.bin", "src/new_module.py"]
    assert calls == [True]


@pytest.mark.parametrize("kind", ["whitespace", "eof"])
def test_hygiene_does_not_masquerade_as_functional_failure(
    task, tmp_path, monkeypatch, kind
):
    before = aw.freeze_autonomous_workspace(task)
    text = "value = 1  \n" if kind == "whitespace" else "value = 1\n\n"
    target = task.workspace_root / "src/labels.py"
    target.write_text(text)
    result, _ = finalize(task, before, tmp_path, monkeypatch)
    assert result["functional"] == result["scope"] == "PASS"
    assert result["hygiene"] == "CLEANUP_REQUIRED"
    assert result["status"] == "FUNCTIONAL_PASS_CLEANUP_REQUIRED"
    assert target.read_text() == text


@pytest.mark.parametrize("kind", ["replace", "delete", "mode"])
def test_explicit_protected_restore_has_receipt_then_validates(
    task, tmp_path, monkeypatch, kind
):
    task = task.model_copy(update={"protected_policy": "restore"})
    before = aw.freeze_autonomous_workspace(task)
    protected = task.workspace_root / task.protected_paths[0]
    raw = protected.read_bytes()
    if kind == "replace":
        protected.write_text("# agent changed oracle\n")
    elif kind == "delete":
        protected.unlink()
    else:
        protected.chmod(0o700)
    result, calls = finalize(task, before, tmp_path, monkeypatch)
    assert result["status"] == "PASS"
    assert protected.read_bytes() == raw
    assert (
        protected.stat().st_mode & 0o777
        == before["files"][task.protected_paths[0]]["mode"]
    )
    assert result["recovery"][0]["event"] == "SUPERVISOR_RESTORE_APPLIED"
    assert (
        result["recovery"][0]["paths"][0]["before"]
        != result["recovery"][0]["paths"][0]["after"]
    )
    assert calls == [True]
    assert (tmp_path / "acceptance/restore-plan.json").is_file()
    assert (tmp_path / "acceptance/restore-receipt.json").is_file()


def test_protected_default_reject_skips_validation(task, tmp_path, monkeypatch):
    before = aw.freeze_autonomous_workspace(task)
    (task.workspace_root / task.protected_paths[0]).write_text("# changed\n")
    result, calls = finalize(task, before, tmp_path, monkeypatch)
    assert result["status"] == "FAIL"
    assert result["failure"] == "PROTECTED_PATH_CHANGED"
    assert result["recovery"] == [] and not calls


@pytest.mark.parametrize("kind", ["staged", "working_copy_restored", "restore_policy"])
def test_protected_index_change_cannot_survive_as_accepted_output(
    task, tmp_path, monkeypatch, kind
):
    if kind == "restore_policy":
        task = task.model_copy(update={"protected_policy": "restore"})
    before = aw.freeze_autonomous_workspace(task)
    name = task.protected_paths[0]
    protected = task.workspace_root / name
    original = protected.read_bytes()
    protected.write_text("# staged unwanted protected-test change\n")
    git(task.workspace_root, "add", "--", name)
    if kind == "working_copy_restored":
        protected.write_bytes(original)
    result, calls = finalize(task, before, tmp_path, monkeypatch)
    assert result["status"] == result["scope"] == "FAIL"
    assert result["failure"] == "PROTECTED_INDEX_CHANGED"
    assert not calls and not result["recovery"]
    assert git(task.workspace_root, "diff", "--cached", "--", name)


@pytest.mark.parametrize("kind", ["commit", "branch", "config", "symlink", "hardlink"])
def test_scope_not_downgraded_to_hygiene(task, tmp_path, monkeypatch, kind):
    before = aw.freeze_autonomous_workspace(task)
    root = task.workspace_root
    target = root / "src/labels.py"
    target.write_text("value = 1\n")
    if kind == "commit":
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
        assert not git(root, "diff")
    elif kind == "branch":
        git(root, "switch", "-c", "other")
    elif kind == "config":
        with (root / ".git/config").open("a") as f:
            f.write("[include]\npath=/synthetic/no-read\n")
    elif kind == "symlink":
        target.unlink()
        target.symlink_to(tmp_path / "outside")
    else:
        os.link(target, tmp_path / "outside")
    result, calls = finalize(task, before, tmp_path, monkeypatch)
    assert result["status"] == result["scope"] == "FAIL"
    assert result["functional"] == "NOT_RUN" and not calls


@pytest.mark.parametrize(
    "runtime,valid", [("ERROR", True), ("UNKNOWN", True), ("PASS", False)]
)
def test_runtime_or_functional_failure_stays_failed(
    task, tmp_path, monkeypatch, runtime, valid
):
    before = aw.freeze_autonomous_workspace(task)
    result, _ = finalize(
        task, before, tmp_path, monkeypatch, runtime=runtime, valid=valid
    )
    assert result["status"] == "FAIL"
    assert result["functional"] == ("PASS" if valid else "FAIL")


def test_parent_oracle_hash_drift_fails_before_validator(task, tmp_path, monkeypatch):
    before = aw.freeze_autonomous_workspace(task)
    hidden = tmp_path / "hidden.py"
    hidden.write_text("# original\n")
    pins = aw.freeze_validator_support(task, (hidden,))
    hidden.write_text("# changed\n")
    monkeypatch.setattr(
        aw, "run_validators", lambda *a, **k: pytest.fail("must not run")
    )
    result = aw.finalize_autonomous_workspace(
        task=task,
        baseline=before,
        runtime_status="PASS",
        validator_support=pins,
        validator_mounts=(hidden,),
        output=tmp_path / "out",
    )
    assert result["failure"] == "VALIDATOR_SUPPORT_CHANGED"


@pytest.mark.parametrize(
    "name", ["../escape", "/root", ".git/config", "src/../x", "src\\x", ""]
)
def test_protected_paths_are_exact_and_local(task, name):
    with pytest.raises(ValueError):
        aw.AutonomousWorkspaceTask(**{**task.model_dump(), "protected_paths": [name]})


def test_repeat_cannot_overwrite_audit(task, tmp_path, monkeypatch):
    before = aw.freeze_autonomous_workspace(task)
    finalize(task, before, tmp_path, monkeypatch)
    with pytest.raises(FileExistsError):
        finalize(task, before, tmp_path, monkeypatch)


def test_changed_task_cannot_borrow_baseline(task, tmp_path, monkeypatch):
    before = aw.freeze_autonomous_workspace(task)
    changed = task.model_copy(update={"protected_paths": ()})
    result, calls = finalize(changed, before, tmp_path, monkeypatch)
    assert result["failure"] == "TASK_CHANGED" and not calls


def test_no_live_execution_or_registry_admission_from_task():
    from dispatcher_for_codex_agents.agent_harness.runtime import default_registry

    assert not hasattr(aw, "invoke")
    with pytest.raises(ValueError, match="LIVE_NOT_ADMITTED"):
        default_registry().create("antigravity_cli", None)


def test_new_untracked_whitespace_requires_cleanup(task, tmp_path, monkeypatch):
    before = aw.freeze_autonomous_workspace(task)
    (task.workspace_root / "new.py").write_text("value = 1  \n")
    result, calls = finalize(task, before, tmp_path, monkeypatch)
    assert result["hygiene"] == "CLEANUP_REQUIRED"
    assert result["functional"] == result["scope"] == "PASS"
    assert calls == [True]


def test_malformed_baseline_git_config_is_controlled(task):
    (task.workspace_root / ".git/config").write_text("not a section\n")
    with pytest.raises(aw.WorkspaceContractError, match="AUTONOMOUS_BASELINE_INVALID"):
        aw.freeze_autonomous_workspace(task)


def test_validator_side_effect_invalidates_scope(task, tmp_path, monkeypatch):
    before = aw.freeze_autonomous_workspace(task)

    def mutate(*args, **kwargs):
        (task.workspace_root / "src/labels.py").write_text("value = 7\n")
        return [{"name": "public", "status": "PASS"}]

    monkeypatch.setattr(aw, "run_validators", mutate)
    result = aw.finalize_autonomous_workspace(
        task=task,
        baseline=before,
        runtime_status="PASS",
        validator_support=aw.freeze_validator_support(task, ()),
        validator_mounts=(),
        output=tmp_path / "out",
    )
    assert result["failure"] == "VALIDATOR_SIDE_EFFECT"
    assert result["scope"] == result["status"] == "FAIL"


def test_validator_sandbox_unavailable_defers_functional_not_scope(
    task, tmp_path, monkeypatch
):
    before = aw.freeze_autonomous_workspace(task)
    (task.workspace_root / "src/labels.py").write_text("value = 1\n")
    calls = []

    def unavailable(*args, **kwargs):
        calls.append(True)
        raise OSError("bwrap namespace denied")

    monkeypatch.setattr(aw, "run_validators", unavailable)
    result = aw.finalize_autonomous_workspace(
        task=task,
        baseline=before,
        runtime_status="PASS",
        validator_support=aw.freeze_validator_support(task, ()),
        validator_mounts=(),
        output=tmp_path / "acceptance",
        require_validator_sandbox=False,
    )
    assert result["scope"] == "PASS"
    assert result["functional"] == "FAIL"
    assert result["status"] == "FAIL"
    assert result["validator_sandbox_enforcement"] == "DEFERRED"
    assert result["validators"][0]["failure"] == "VALIDATOR_SANDBOX_UNAVAILABLE"
    assert calls == [True]


def test_corrupt_restore_snapshot_cannot_write_protected_file(
    task, tmp_path, monkeypatch
):
    task = task.model_copy(update={"protected_policy": "restore"})
    before = aw.freeze_autonomous_workspace(task)
    name = task.protected_paths[0]
    before["protected_bytes"][name] = "Y29ycnVwdA=="
    target = task.workspace_root / name
    target.write_text("# agent content\n")
    result, calls = finalize(task, before, tmp_path, monkeypatch)
    assert result["failure"] == "PROTECTED_BASELINE_HASH_INVALID" and not calls
    assert target.read_text() == "# agent content\n"
    assert not (tmp_path / "acceptance/restore-receipt.json").exists()


@pytest.mark.parametrize("reference", [False, True])
def test_multifile_benchmark_has_failing_seed_and_passing_oracle(tmp_path, reference):
    fixtures = Path(__file__).resolve().parents[1] / "fixtures/agy_workspace"
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir()
    for name in ("parser.py", "normalize.py", "pipeline.py"):
        source = "reference_" + name if reference and name != "pipeline.py" else name
        shutil.copyfile(fixtures / source, repo / "src" / name)
    shutil.copyfile(fixtures / "public_tests.py", repo / "tests/test_labels.py")
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
    results = []
    for argv in (
        (sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests"),
        (sys.executable, "-B", str(fixtures / "hidden_validator.py"), str(repo)),
    ):
        p = subprocess.run(argv, cwd=repo, env=env, capture_output=True, timeout=20)
        assert (p.returncode == 0) == reference, p.stderr.decode()
        results.append(p)
    if reference:
        assert b"cases=1464" in results[1].stdout
        assert not list(repo.rglob("reference*"))


@pytest.mark.parametrize("kind", ["valid", "wrong_bytes", "syntax_error"])
def test_parent_build_oracle_uses_scratch_and_checks_bytes(tmp_path, kind):
    fixtures = Path(__file__).resolve().parents[1] / "fixtures/agy_workspace"
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/parser.py").write_text(
        "value = 1\n" if kind != "syntax_error" else "invalid (\n"
    )
    shutil.copyfile(fixtures / "build_fixture.py", repo / "build.py")
    if kind == "wrong_bytes":
        (repo / "build.py").write_text(
            "from pathlib import Path\nimport zipfile\n"
            "Path('build').mkdir()\n"
            "with zipfile.ZipFile('build/labels.zip', 'w') as z:\n"
            "    z.writestr('src/parser.py', 'wrong')\n"
        )
    before = {p: p.read_bytes() for p in repo.rglob("*") if p.is_file()}
    result = subprocess.run(
        [sys.executable, "-B", str(fixtures / "build_validator.py"), str(repo)],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "TMPDIR": str(tmp_path)},
        capture_output=True,
        timeout=20,
    )
    assert (result.returncode == 0) == (kind == "valid")
    assert before == {p: p.read_bytes() for p in repo.rglob("*") if p.is_file()}
    assert not (repo / "build").exists()
