"""Parent-side autonomous workspace acceptance, not a runtime permission grant.

The official runtime must independently enforce its reviewed permission envelope.
This module has no agent-launch path. Its audit cannot certify transient actions
or turn unverified shell/network capabilities into admitted capabilities.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
from configparser import Error as ConfigError
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .antigravity_coding import _support_matches, _validator_identity
from .workspace_artifact import (
    ValidatorCommand,
    WorkspaceContractError,
    _canonical,
    _DiffCheckFailed,
    _files,
    _git,
    _metadata,
    digest,
    run_validators,
)


class AutonomousWorkspaceTask(BaseModel):
    """An entire disposable repository is writable; protected bytes are audited.

    This is a result/recovery contract, not a second capability_policy. A caller
    must not use it as proof of official-runtime permission enforcement.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    execution_mode: Literal["autonomous_workspace"] = "autonomous_workspace"
    workspace_root: Path
    baseline_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    protected_paths: tuple[str, ...] = ()
    protected_policy: Literal["reject", "restore"] = "reject"
    validator_commands: tuple[ValidatorCommand, ...] = Field(min_length=1)
    timeout_seconds: float = Field(default=600, ge=1, le=1800, allow_inf_nan=False)

    @field_validator("protected_paths")
    @classmethod
    def exact_protected_files(cls, names):
        if len(names) != len(set(names)):
            raise ValueError("PROTECTED_PATH_DUPLICATE")
        for name in names:
            path = PurePosixPath(name)
            if (
                not name
                or path.is_absolute()
                or path.as_posix() != name
                or any(part in {"..", ".git"} for part in path.parts)
                or any(c in name for c in "\\\0\r\n")
            ):
                raise ValueError("PROTECTED_PATH_INVALID")
        return names

    @model_validator(mode="after")
    def distinct_validators(self):
        if len({v.name for v in self.validator_commands}) != len(
            self.validator_commands
        ):
            raise ValueError("VALIDATOR_NAME_DUPLICATE")
        return self


def _save(root: Path, name: str, value: dict | bytes) -> None:
    raw = value if isinstance(value, bytes) else json.dumps(value, indent=2).encode()
    fd = os.open(root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def freeze_autonomous_workspace(task: AutonomousWorkspaceTask) -> dict:
    """Freeze a clean standalone Git baseline; shared worktrees not admitted."""
    try:
        task = AutonomousWorkspaceTask.model_validate(task.model_dump())
        root = task.workspace_root
        _canonical(root, directory=True)
        metadata = _metadata(root)
        head = _git(root, "rev-parse", "HEAD").decode().strip()
        if (
            head != task.baseline_commit
            or Path(_git(root, "rev-parse", "--show-toplevel").decode().strip()) != root
            or _git(root, "status", "--porcelain")
            or _git(root, "remote", "-v")
        ):
            raise WorkspaceContractError("BASELINE_NOT_CLEAN_OR_REMOTE_FREE")
        files = _files(root)
        tracked = set(_git(root, "ls-files", "-z").decode().strip("\0").split("\0"))
        if tracked != set(files) or not set(task.protected_paths) <= tracked:
            raise WorkspaceContractError("BASELINE_FILE_SET_INVALID")
        return {
            "contract": "dca-autonomous-workspace/1",
            "task_sha256": digest(task.model_dump_json().encode()),
            "head": head,
            "branch": _git(root, "symbolic-ref", "HEAD").decode().strip(),
            "remote_sha256": digest(_git(root, "remote", "-v")),
            "files": files,
            "protected_bytes": {
                n: base64.b64encode((root / n).read_bytes()).decode()
                for n in task.protected_paths
            },
            **metadata,
        }
    except (
        OSError,
        ValueError,
        ConfigError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as exc:
        if isinstance(exc, WorkspaceContractError):
            raise
        raise WorkspaceContractError("AUTONOMOUS_BASELINE_INVALID") from None


def _inspect(task: AutonomousWorkspaceTask, baseline: dict) -> dict:
    root = task.workspace_root
    _canonical(root, directory=True)
    _canonical(root / ".git/config")
    if digest((root / ".git/config").read_bytes()) != baseline["config_sha256"]:
        raise WorkspaceContractError("GIT_CONFIG_CHANGED")
    if _metadata(root) != {
        k: baseline[k] for k in ("config_sha256", "git_dir_identity")
    }:
        raise WorkspaceContractError("GIT_STORAGE_CHANGED")
    head = _git(root, "rev-parse", "HEAD").decode().strip()
    branch = _git(root, "symbolic-ref", "HEAD").decode().strip()
    remote = digest(_git(root, "remote", "-v"))
    after = _files(root)
    old = baseline["files"]
    changed = sorted(n for n in old.keys() | after.keys() if old.get(n) != after.get(n))
    protected_index_changed = []
    if task.protected_paths:
        protected_index_changed = sorted(
            name
            for name in _git(
                root,
                "diff",
                "--cached",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                "--name-only",
                "-z",
                baseline["head"],
                "--",
                *(":(literal)" + name for name in task.protected_paths),
            )
            .decode()
            .split("\0")
            if name
        )
    state = {
        "head": head,
        "branch": branch,
        "remote_sha256": remote,
        "files": after,
        "changed_paths": changed,
        "new_paths": sorted(after.keys() - old.keys()),
        "deleted_paths": sorted(old.keys() - after.keys()),
        "protected_changed": sorted(set(changed) & set(task.protected_paths)),
        "protected_index_changed": protected_index_changed,
    }
    if (
        head != baseline["head"]
        or branch != baseline["branch"]
        or remote != baseline["remote_sha256"]
    ):
        # A commit must not make an empty current diff hide baseline changes.
        raise WorkspaceContractError("GIT_BASELINE_DRIFT")
    return state


def _restore(task, baseline, before, output):
    rows = []
    for name in before["protected_changed"]:
        original = baseline["files"][name]
        raw = base64.b64decode(baseline["protected_bytes"][name], validate=True)
        if digest(raw) != original["sha256"]:
            raise WorkspaceContractError("PROTECTED_BASELINE_HASH_INVALID")
        path = task.workspace_root / name
        _canonical(path.parent, directory=True)
        if path.exists():
            _canonical(path)
        rows.append(
            {"path": name, "before": before["files"].get(name), "after": original}
        )
    # Record the intent before any mutation. Interrupted recovery is never PASS.
    _save(
        output,
        "restore-plan.json",
        {"event": "SUPERVISOR_RESTORE_PLANNED", "paths": rows},
    )
    for item in rows:
        path = task.workspace_root / item["path"]
        raw = base64.b64decode(baseline["protected_bytes"][item["path"]], validate=True)
        fd, temporary = tempfile.mkstemp(prefix=".dca-restore-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
                os.fchmod(stream.fileno(), item["after"]["mode"])
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    receipt = {"event": "SUPERVISOR_RESTORE_APPLIED", "paths": rows}
    _save(output, "restore-receipt.json", receipt)
    return receipt


def _new_file_hygiene(root: Path, name: str) -> None:
    # --no-index exit bits: 1 = different, 2 = whitespace errors.
    # A clean new file normally returns 1, not 0.
    p = subprocess.run(
        [
            "/usr/bin/git",
            "--no-pager",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-index",
            "--check",
            "--",
            "/dev/null",
            name,
        ],
        cwd=root,
        env={
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=10,
    )
    if p.returncode in (2, 3):
        raise _DiffCheckFailed("WORKSPACE_DIFF_CHECK_FAILED")
    if p.returncode not in (0, 1):
        raise WorkspaceContractError("GIT_INSPECTION_FAILED")


def finalize_autonomous_workspace(
    *,
    task: AutonomousWorkspaceTask,
    baseline: dict,
    runtime_status: str,
    validator_support: dict,
    validator_mounts: tuple[Path, ...],
    output: Path,
    require_validator_sandbox: bool = False,
) -> dict:
    """Called by the trusted parent only after runtime/children are quiescent.

    runtime_status comes from the reviewed backend, never final agent prose.
    This post-audit-only interface grants no permission to launch a live backend.
    """
    task = AutonomousWorkspaceTask.model_validate(task.model_dump())
    if (
        not output.is_absolute()
        or output.resolve() != output
        or output.is_relative_to(task.workspace_root)
    ):
        raise WorkspaceContractError("AUDIT_OUTPUT_PATH_INVALID")
    output.mkdir(mode=0o700)
    result = {
        "contract": "dca-autonomous-workspace/1",
        "status": "FAIL",
        "scope": "FAIL",
        "functional": "NOT_RUN",
        "hygiene": "NOT_RUN",
        "runtime_status": runtime_status,
        "recovery": [],
        "validators": [],
        "permission_enforcement": "NOT_CERTIFIED_BY_POST_AUDIT",
    }
    _save(output, "baseline.json", baseline)
    try:
        if baseline["task_sha256"] != digest(task.model_dump_json().encode()):
            raise WorkspaceContractError("TASK_CHANGED")
        before = _inspect(task, baseline)
        _save(output, "agent-final-state.json", before)
        if before["protected_index_changed"]:
            # File restoration must not leave an accepted artifact with poisoned
            # protected bytes still staged for a later commit. Never reset Git.
            raise WorkspaceContractError("PROTECTED_INDEX_CHANGED")
        if before["protected_changed"]:
            if task.protected_policy != "restore":
                raise WorkspaceContractError("PROTECTED_PATH_CHANGED")
            result["recovery"].append(_restore(task, baseline, before, output))
        final = _inspect(task, baseline)
        if final["protected_changed"]:
            raise WorkspaceContractError("PROTECTED_PATH_NOT_RESTORED")
        result["scope"] = "PASS"
        result["changed_paths"] = final["changed_paths"]
        result["new_paths"] = final["new_paths"]
        result["deleted_paths"] = final["deleted_paths"]
        for label, args in {
            "status": ("status", "--short"),
            "baseline-diff": (
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                task.baseline_commit,
                "--",
            ),
            "cached-diff": ("diff", "--no-ext-diff", "--no-textconv", "--cached", "--"),
        }.items():
            _save(output, label + ".txt", _git(task.workspace_root, *args))
        try:
            _git(
                task.workspace_root,
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--check",
                task.baseline_commit,
                "--",
            )
            result["hygiene"] = "PASS"
            for name in final["new_paths"]:
                _new_file_hygiene(task.workspace_root, name)
        except _DiffCheckFailed:
            result["hygiene"] = "CLEANUP_REQUIRED"
        if not _support_matches(validator_support, task, validator_mounts):
            raise WorkspaceContractError("VALIDATOR_SUPPORT_CHANGED")
        try:
            validators = run_validators(
                task, mounts=validator_mounts, output=output / "validators"
            )
        except OSError:
            if require_validator_sandbox:
                raise
            result["validators"] = [
                {
                    "name": "sandbox",
                    "status": "DEFERRED",
                    "exit_code": None,
                    "failure": "VALIDATOR_SANDBOX_UNAVAILABLE",
                }
            ]
            result["validator_sandbox_enforcement"] = "DEFERRED"
        else:
            result["validators"] = validators
            if any(validator.get("status") == "DEFERRED" for validator in validators):
                result["validator_sandbox_enforcement"] = "DEFERRED"
        if _inspect(task, baseline) != final or not _support_matches(
            validator_support, task, validator_mounts
        ):
            raise WorkspaceContractError("VALIDATOR_SIDE_EFFECT")
        result["functional"] = (
            "PASS"
            if result["validators"]
            and all(v["status"] == "PASS" for v in result["validators"])
            else "FAIL"
        )
        if result["functional"] == "PASS" and runtime_status == "PASS":
            result["status"] = (
                "PASS"
                if result["hygiene"] == "PASS"
                else "FUNCTIONAL_PASS_CLEANUP_REQUIRED"
            )
    except (
        OSError,
        ValueError,
        KeyError,
        ConfigError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as exc:
        result["scope"] = "FAIL"
        result["failure"] = (
            str(exc)
            if isinstance(exc, WorkspaceContractError)
            else "AUTONOMOUS_AUDIT_INVALID"
        )
    _save(output, "result.json", result)
    _save(
        output,
        "sha256.json",
        {
            p.relative_to(output).as_posix(): digest(p.read_bytes())
            for p in output.rglob("*")
            if p.is_file()
        },
    )
    return result


def freeze_validator_support(task, mounts):
    """Expose the existing parent-held validator pinning; no new validator runner."""
    return _validator_identity(task, mounts)
