"""Initialize private paths in a new source checkout; no network or installs."""

from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path


def initialize(root: Path, *, dry_run: bool = False) -> dict:
    root = root.absolute()
    if root.resolve() != root or root == Path("/") or not root.is_dir():
        raise ValueError("CANONICAL_CHECKOUT_ROOT_REQUIRED")
    if not (root / "pyproject.toml").is_file():
        raise ValueError("SOURCE_CHECKOUT_REQUIRED")
    directories = [
        root,
        *(
            root / name
            for name in (
                "configs",
                "runtime",
                "runtime/tmp",
                "runtime/cache",
                "runtime/state",
                "runtime/logs",
                "runs",
            )
        ),
    ]
    configs = {
        root / "configs/workspace.json": {"version": 1, "workspace_root": str(root)},
        root
        / "configs/notifications.json": {
            "version": 1,
            "ledger_directory": str(root / "runtime/state/notifications"),
            "sinks": [],
        },
    }
    for path in [*directories, *configs]:
        if path.resolve() != path or path.is_symlink():
            raise ValueError("SYMLINK_PATH_FORBIDDEN")
        if path.exists():
            info = path.stat()
            if info.st_uid != os.getuid():
                raise ValueError("CURRENT_OWNER_REQUIRED")
            if path in configs:
                raise ValueError("EXISTING_CONFIG_NOT_OVERWRITTEN")
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("DIRECTORY_REQUIRED")
    if not dry_run:
        for path in directories:
            path.mkdir(mode=0o700, exist_ok=True)
            path.chmod(0o700)
        for path, value in configs.items():
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(value, stream, sort_keys=True)
                stream.write("\n")
    return {
        "status": "DRY_RUN" if dry_run else "INITIALIZED",
        "model_calls": 0,
        "notifications_enabled": False,
        "workspace": str(root),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        result = initialize(Path(__file__).resolve().parents[1], dry_run=args.dry_run)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Initialization refused: {exc}\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
