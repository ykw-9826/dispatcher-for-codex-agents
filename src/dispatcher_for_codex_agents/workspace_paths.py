"""Installation paths only; no scheduling, model, or notification behavior."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from pathlib import Path


def workspace_root() -> Path | None:
    """Require workspace configuration in release-layout installs.

    Ordinary library/legacy checkouts retain explicit-path behavior. Development
    may select the same config with DCA_WORKSPACE_CONFIG.
    """
    selected = os.environ.get("DCA_WORKSPACE_CONFIG")
    prefix = Path(sys.prefix)
    if selected is not None:
        config = Path(selected)
    elif prefix.name == "venv" and prefix.parent.parent.name == "releases":
        config = prefix.parent.parent.parent / "configs/workspace.json"
    elif prefix.name == ".venv":
        config = prefix.parent / "configs/workspace.json"
    else:
        return None
    if not config.is_absolute() or config.resolve() != config:
        raise ValueError("WORKSPACE_CONFIG_PATH_INVALID")
    info = config.stat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_size > 4096
    ):
        raise ValueError("WORKSPACE_CONFIG_NOT_PRIVATE")
    value = json.loads(config.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "workspace_root"}
        or type(value["version"]) is not int
        or value["version"] != 1
        or not isinstance(value["workspace_root"], str)
    ):
        raise ValueError("WORKSPACE_CONFIG_INVALID")
    root = Path(value["workspace_root"])
    if (
        not root.is_absolute()
        or root.resolve() != root
        or config != root / "configs/workspace.json"
    ):
        raise ValueError("WORKSPACE_ROOT_MISMATCH")
    for path in (root, root / "runtime/tmp", root / "runtime/cache", root / "runs"):
        info = path.stat()
        if (
            path.resolve() != path
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
            or not os.access(path, os.W_OK | os.X_OK)
        ):
            raise ValueError("WORKSPACE_DIRECTORY_INVALID")
    return root


def temporary_root() -> str:
    root = workspace_root()
    if root is not None:
        return str(root / "runtime/tmp")
    selected = os.environ.get("TMPDIR")
    if not selected:
        raise ValueError("EXPLICIT_TEMPORARY_DIRECTORY_REQUIRED")
    path = Path(selected)
    if not path.is_absolute() or path.resolve() != path or not path.is_dir():
        raise ValueError("EXPLICIT_TEMPORARY_DIRECTORY_INVALID")
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("EXPLICIT_TEMPORARY_DIRECTORY_NOT_PRIVATE")
    return str(path)


def output_path(path: str | Path) -> Path:
    """Installed CLI outputs stay in explicit project-local run/state roots."""
    value = Path(path).expanduser().absolute()
    root = workspace_root()
    if root is not None and (
        value.resolve() != value
        or not any(value.is_relative_to(root / name) for name in ("runs", "runtime"))
    ):
        raise ValueError("OUTPUT_OUTSIDE_WORKSPACE")
    return value


def activate_workspace() -> Path | None:
    """Bind tool caches/temp; Codex host storage is not relocated."""
    root = workspace_root()
    if root is None:
        raise ValueError("WORKSPACE_CONFIGURATION_REQUIRED")
    temporary = str(root / "runtime/tmp")
    for name in ("TMPDIR", "TMP", "TEMP"):
        os.environ[name] = temporary
    # Explicit choice: tempfile must not try /tmp when the selected path fails.
    tempfile.tempdir = temporary
    os.environ["XDG_CACHE_HOME"] = str(root / "runtime/cache")
    os.environ["PIP_CACHE_DIR"] = str(root / "runtime/cache/pip")
    os.environ["RUFF_CACHE_DIR"] = str(root / "runtime/cache/ruff")
    os.environ["BLACK_CACHE_DIR"] = str(root / "runtime/cache/black")
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    return root
