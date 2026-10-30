"""Parent-owned Git/artifact acceptance; deliberately separate from JSON results.

An allowed diff is an acceptance boundary, not an exact-file OS sandbox claim.
Validators are trusted operator commands, run without credentials/network and
with read-only mounts. Never execute validators after a scope-integrity failure.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from configparser import Error as ConfigError
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class WorkspaceContractError(ValueError):
    """Fixed, content-free diagnostic; never raw Git/config/process errors."""


class _DiffCheckFailed(WorkspaceContractError):
    """Git inspected the diff and rejected its whitespace, not its file scope."""


class ValidatorCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    argv: tuple[str, ...] = Field(min_length=1)
    timeout_seconds: float = Field(default=30, gt=0, le=120, allow_inf_nan=False)

    @field_validator("argv")
    @classmethod
    def executable_is_absolute(cls, value):
        if not Path(value[0]).is_absolute() or any("\0" in arg for arg in value):
            raise ValueError("VALIDATOR_ARGV_INVALID")
        return value


class WorkspaceArtifactTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    execution_mode: Literal["workspace_artifact"] = "workspace_artifact"
    workspace_root: Path
    baseline_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    allowed_write_paths: tuple[str, ...] = Field(min_length=1)
    validator_commands: tuple[ValidatorCommand, ...] = Field(min_length=1)
    timeout_seconds: float = Field(default=180, ge=1, le=600, allow_inf_nan=False)

    @field_validator("allowed_write_paths")
    @classmethod
    def exact_source_paths(cls, paths):
        if len(set(paths)) != len(paths):
            raise ValueError("WRITE_PATH_DUPLICATE")
        for name in paths:
            path = PurePosixPath(name)
            if (
                path.is_absolute()
                or path.as_posix() != name
                or ".." in path.parts
                or not path.parts
                or any(part.startswith(".") for part in path.parts)
                or any(part in {"tests", "test", "conftest.py"} for part in path.parts)
                or any(char in name for char in "\\\0\r\n")
            ):
                raise ValueError("WRITE_PATH_INVALID_OR_TEST_PROTECTED")
        return paths

    @model_validator(mode="after")
    def distinct_validators(self):
        if len({v.name for v in self.validator_commands}) != len(
            self.validator_commands
        ):
            raise ValueError("VALIDATOR_NAME_DUPLICATE")
        return self


def _canonical(path: Path, *, directory=False) -> None:
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise WorkspaceContractError("PATH_NOT_CANONICAL")
    info = path.lstat()
    if info.st_uid != os.getuid() or (
        not stat.S_ISDIR(info.st_mode)
        if directory
        else not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
    ):
        raise WorkspaceContractError("PATH_IDENTITY_INVALID")


def _git(root: Path, *args: str) -> bytes:
    env = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
    }
    p = subprocess.run(
        [
            "/usr/bin/git",
            "--no-pager",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            *args,
        ],
        cwd=root,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=10,
    )
    if p.returncode == 2 and args[0] == "diff" and "--check" in args:
        raise _DiffCheckFailed("WORKSPACE_DIFF_CHECK_FAILED")
    if p.returncode:
        raise WorkspaceContractError("GIT_INSPECTION_FAILED")
    return p.stdout


def _walk_error(error: OSError) -> None:
    raise error


def _files(root: Path) -> dict:
    """Enumerate ignored files too; never follow links or read special files."""
    result = {}
    for current, dirs, files in os.walk(root, followlinks=False, onerror=_walk_error):
        directory = Path(current)
        if directory == root:
            dirs[:] = [name for name in dirs if name != ".git"]
        for name in dirs:
            _canonical(directory / name, directory=True)
        for name in files:
            path = directory / name
            _canonical(path)
            result[path.relative_to(root).as_posix()] = {
                "sha256": digest(path.read_bytes()),
                "mode": stat.S_IMODE(path.stat().st_mode),
            }
    return result


def _metadata(root: Path) -> dict:
    for name in (".git", ".git/objects", ".git/refs"):
        _canonical(root / name, directory=True)
    for name in (".git/config", ".git/HEAD", ".git/index"):
        _canonical(root / name)
    if (root / ".git/commondir").exists() or (
        root / ".git/objects/info/alternates"
    ).exists():
        raise WorkspaceContractError("SHARED_GIT_STORAGE_UNSUPPORTED")
    # No includes, filters, aliases, fsmonitor or external commands are admitted.
    from configparser import RawConfigParser

    config = RawConfigParser(strict=True)
    config.read_string((root / ".git/config").read_text())
    if set(config.sections()) != {"core"} or not set(config["core"]) <= {
        "repositoryformatversion",
        "filemode",
        "bare",
        "logallrefupdates",
        "ignorecase",
    }:
        raise WorkspaceContractError("GIT_CONFIG_NOT_MINIMAL")
    return {
        "config_sha256": digest((root / ".git/config").read_bytes()),
        "git_dir_identity": (root / ".git").stat().st_ino,
    }


def freeze_workspace(task: WorkspaceArtifactTask) -> dict:
    """Freeze a clean, standalone, remote-free disposable source repository."""
    try:
        task = WorkspaceArtifactTask.model_validate(task.model_dump())
        root = task.workspace_root
        _canonical(root, directory=True)
        meta = _metadata(root)
        if Path(_git(root, "rev-parse", "--show-toplevel").decode().strip()) != root:
            raise WorkspaceContractError("WORKSPACE_IS_NOT_REPO_ROOT")
        head = _git(root, "rev-parse", "HEAD").decode().strip()
        branch = _git(root, "symbolic-ref", "HEAD").decode().strip()
        remote = _git(root, "remote", "-v")
        if (
            head != task.baseline_commit
            or remote
            or _git(root, "status", "--porcelain")
        ):
            raise WorkspaceContractError("BASELINE_NOT_CLEAN_OR_REMOTE_FREE")
        entries = _git(root, "ls-files", "--stage", "-z").split(b"\0")
        tracked = []
        for entry in filter(None, entries):
            record, path = entry.split(b"\t", 1)
            mode, _, stage = record.split()
            if mode not in {b"100644", b"100755"} or stage != b"0":
                raise WorkspaceContractError("BASELINE_ENTRY_UNSUPPORTED")
            tracked.append(path.decode())
        files = _files(root)
        if set(files) != set(tracked) or not set(task.allowed_write_paths) <= set(
            files
        ):
            raise WorkspaceContractError("BASELINE_FILE_SET_INVALID")
        return {
            "contract": "dca-workspace-artifact/1",
            "head": head,
            "branch": branch,
            "remote_sha256": digest(remote),
            "files": files,
            **meta,
        }
    except (
        OSError,
        ValueError,
        UnicodeError,
        ConfigError,
        subprocess.SubprocessError,
    ) as exc:
        if isinstance(exc, WorkspaceContractError):
            raise
        raise WorkspaceContractError("WORKSPACE_BASELINE_INVALID") from None


def audit_workspace(task: WorkspaceArtifactTask, baseline: dict) -> dict:
    """Audit disk, not agent prose or a potentially manipulated Git index."""
    result = {
        "status": "FAIL",
        "head_unchanged": False,
        "branch_unchanged": False,
        "remote_unchanged": False,
        "tests_unchanged": False,
        "changed_paths": [],
        "unexpected_tracked": [],
        "unexpected_untracked": [],
        "diff_check": "NOT_RUN",
        "scope_integrity": "FAIL",
    }
    try:
        root = task.workspace_root
        _canonical(root, directory=True)
        # Check config before allowing Git to parse it; never execute mutated config.
        _canonical(root / ".git/config")
        if digest((root / ".git/config").read_bytes()) != baseline["config_sha256"]:
            raise WorkspaceContractError("GIT_CONFIG_CHANGED")
        if _metadata(root) != {
            k: baseline[k] for k in ("config_sha256", "git_dir_identity")
        }:
            raise WorkspaceContractError("GIT_STORAGE_CHANGED")
        result["head_unchanged"] = (
            _git(root, "rev-parse", "HEAD").decode().strip()
            == baseline["head"]
            == task.baseline_commit
        )
        result["branch_unchanged"] = (
            _git(root, "symbolic-ref", "HEAD").decode().strip() == baseline["branch"]
        )
        result["remote_unchanged"] = (
            digest(_git(root, "remote", "-v")) == baseline["remote_sha256"]
        )
        after = _files(root)
        old = baseline["files"]
        changed = sorted(n for n in old if after.get(n) != old[n])
        result["changed_paths"] = changed
        result["unexpected_tracked"] = [
            n for n in changed if n not in task.allowed_write_paths
        ]
        result["unexpected_untracked"] = sorted(set(after) - set(old))
        result["tests_unchanged"] = all(
            after.get(n) == value
            for n, value in old.items()
            if n not in task.allowed_write_paths
        )
        if any(
            n not in after or after[n]["mode"] != old[n]["mode"]
            for n in task.allowed_write_paths
        ):
            raise WorkspaceContractError("ALLOWED_FILE_DELETED_OR_MODE_CHANGED")
        if (
            not all(
                result[k]
                for k in (
                    "head_unchanged",
                    "branch_unchanged",
                    "remote_unchanged",
                    "tests_unchanged",
                )
            )
            or result["unexpected_tracked"]
            or result["unexpected_untracked"]
        ):
            raise WorkspaceContractError("WORKSPACE_SCOPE_VIOLATION")
        try:
            _git(
                root,
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--check",
                task.baseline_commit,
                "--",
            )
        except _DiffCheckFailed:
            result.update(diff_check="FAIL", scope_integrity="PASS")
            raise
        result["diff_check"] = "PASS"
        result["scope_integrity"] = "PASS"
        if not changed:
            raise WorkspaceContractError("NO_IMPLEMENTATION_CHANGE")
        result["status"] = "PASS"
        result["after_files"] = after
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError) as exc:
        result["failure"] = (
            str(exc)
            if isinstance(exc, WorkspaceContractError)
            else "WORKSPACE_AUDIT_INVALID"
        )
    return result


def run_validators(
    task: WorkspaceArtifactTask, *, mounts: tuple[Path, ...], output: Path
) -> list[dict]:
    """Explicit trusted validators; isolated from auth, network and host writes.

    mounts is operator-supplied immutable support (Python runtime and validators),
    never selected by the agent. No shell parsing, implicit home or env inheritance.
    """
    output.mkdir(mode=0o700)
    base = [
        "/usr/bin/bwrap",
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
    ]
    for raw in (Path("/usr"), Path("/lib"), Path("/lib64")):
        if raw.exists():
            base.extend(("--ro-bind", str(raw.resolve()), str(raw)))
    support_paths = [task.workspace_root]
    for command in task.validator_commands:
        support_paths.append(Path(command.argv[0]))
    for path in (*support_paths, *mounts):
        if (
            not path.is_absolute()
            or ".." in path.parts
            or path == Path("/")
            or not path.exists()
        ):
            raise WorkspaceContractError("VALIDATOR_MOUNT_INVALID")
        base.extend(("--ro-bind", str(path), str(path)))
    base.extend(
        (
            "--chdir",
            str(task.workspace_root),
            "--clearenv",
            "--setenv",
            "PATH",
            "/usr/bin:/bin",
            "--setenv",
            "LANG",
            "C.UTF-8",
            "--setenv",
            "PYTHONDONTWRITEBYTECODE",
            "1",
            "--",
        )
    )
    records = []
    for command in task.validator_commands:
        try:
            p = subprocess.run(
                [*base, *command.argv],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=command.timeout_seconds,
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            )
            raw = p.stdout + p.stderr
            sandbox_blocked = b"No permissions to create new namespace" in raw
            record = {
                "name": command.name,
                "status": (
                    "DEFERRED"
                    if sandbox_blocked
                    else "PASS" if p.returncode == 0 else "FAIL"
                ),
                "exit_code": p.returncode,
                "log_sha256": digest(raw),
            }
            if sandbox_blocked:
                record["failure"] = "VALIDATOR_SANDBOX_UNAVAILABLE"
                record["validator_sandbox_enforcement"] = "DEFERRED"
        except subprocess.TimeoutExpired as exc:
            raw = (exc.stdout or b"") + (exc.stderr or b"")
            record = {
                "name": command.name,
                "status": "TIMEOUT",
                "exit_code": None,
                "log_sha256": digest(raw),
            }
        except OSError:
            raw = b"VALIDATOR_SANDBOX_START_FAILED\n"
            record = {
                "name": command.name,
                "status": "HOST_INFRA_BLOCKED",
                "exit_code": None,
                "log_sha256": digest(raw),
            }
        with (output / (command.name + ".log")).open("xb") as f:
            f.write(raw)
        records.append(record)
    return records
