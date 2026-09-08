"""Preview-first user hooks merge and guarded rollback; never edit providers."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import shlex
import time
import tomllib
from contextlib import nullcontext
from pathlib import Path

from dispatcher_for_codex_agents.workspace_paths import workspace_root

from .core import external_path, ledger, storage_path

EVENTS = ("UserPromptSubmit", "Stop", "SessionEnd", "Interrupt", "PermissionRequest")
MARKER = "DCA — Dispatcher for Codex Agents notification"
# Detection only, never an accepted alias or a migration of an installed hook.
RETIRED_MARKER = "Denovo Codex Agent Tool notification"


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _exclusive(path: Path, content: bytes):
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _operation_root(home: Path) -> Path:
    root = workspace_root()
    if root is None:
        return home
    return root / "runtime/state/backups/hooks" / _digest(str(home).encode())[:16]


def _replace(path: Path, content: bytes, expected: bytes | None):
    # Cross-filesystem workspace backups cannot be atomically renamed into home.
    # Write only the explicitly authorized hooks.json, under a same-file lock;
    # callers have already durably saved backup and receipt in the workspace.
    flags = os.O_RDWR | os.O_NOFOLLOW
    if expected is None:
        flags |= os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "r+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if expected is not None and handle.read() != expected:
            raise ValueError("CONCURRENT_HOOK_CONFIGURATION_CHANGE")
        handle.seek(0)
        handle.write(content)
        handle.truncate()
        handle.flush()
        os.fsync(handle.fileno())


def install_hooks(
    codex_home: str,
    executable: str,
    config_path: str,
    *,
    apply: bool = False,
    host_schemas: tuple[str, ...] = (),
) -> dict:
    supported = set(EVENTS)
    for schema_path in host_schemas:
        schema = json.loads(Path(schema_path).read_text())
        names = schema["definitions"]["HookEventName"]["enum"]
        supported &= {name[0].upper() + name[1:] for name in names}
    if not host_schemas:
        # Portable common subset, not a claim that unprobed hosts support more.
        supported &= {"UserPromptSubmit", "Stop", "PermissionRequest"}
    if not {"UserPromptSubmit", "Stop"} <= supported:
        raise ValueError("HOST_CORE_HOOKS_NOT_SUPPORTED")
    home = external_path(codex_home)
    executable_path = Path(executable).absolute()
    if not executable_path.is_file() or not os.access(executable_path, os.X_OK):
        raise ValueError("NOTIFY_EXECUTABLE_UNAVAILABLE")
    config_path = str(storage_path(config_path))
    operation = _operation_root(home)
    if apply:
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        operation.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Share the internal disk mutex with older releases during hook operations.
    with ledger(operation / ".denovo-hook-installer") if apply else nullcontext():
        target = external_path(home / "hooks.json")
        existed = target.exists()
        original = target.read_bytes() if existed else b""
        document = json.loads(original) if original else {"hooks": {}}
        if not isinstance(document, dict) or not isinstance(
            document.get("hooks"), dict
        ):
            raise ValueError("HOOK_CONFIG_INVALID")
        base = home / "config.toml"
        if base.is_symlink():
            raise ValueError("CONFIG_SYMLINK_FORBIDDEN")
        base_content = base.read_bytes() if base.exists() else None
        parsed = tomllib.loads(base_content.decode()) if base_content else {}
        if any(
            RETIRED_MARKER in json.dumps(hooks)
            for hooks in (document["hooks"], parsed.get("hooks", {}))
        ):
            raise ValueError("RETIRED_HOOK_REGISTRATION_REQUIRES_MANUAL_REMOVAL")
        if parsed.get("notify"):
            raise ValueError("TOP_LEVEL_NOTIFY_CONFLICT_REQUIRES_USER_CHOICE")
        if MARKER in json.dumps(parsed.get("hooks", {})):
            raise ValueError("DUPLICATE_INLINE_REGISTRATION")
        merged = copy.deepcopy(document)
        for event in EVENTS:
            if event not in supported:
                continue
            groups = merged["hooks"].setdefault(event, [])
            command = shlex.join(
                [
                    str(executable_path),
                    "hook",
                    "--event",
                    event,
                    "--config",
                    config_path,
                ]
            )
            handler = {
                "type": "command",
                "command": command,
                "timeout": 3,
                "statusMessage": MARKER,
            }
            existing = [
                h
                for group in groups
                for h in group.get("hooks", [])
                if h.get("statusMessage") == MARKER
            ]
            if existing and existing != [handler]:
                raise ValueError("EXISTING_DCA_HOOK_DIFFERS_ROLLBACK_FIRST")
            if not existing:
                groups.append({"hooks": [handler]})
        updated = (json.dumps(merged, indent=2, sort_keys=True) + "\n").encode()
        changed = merged != document
        preview = {
            "status": "HOOK_INSTALL_PREVIEW",
            "changed": changed,
            "events": [event for event in EVENTS if event in supported],
            "unsupported_events": [event for event in EVENTS if event not in supported],
            "host_capability_verified": bool(host_schemas),
            "trust_status": "USER_REVIEW_REQUIRED",
            "provider_config_modified": False,
        }
        if not apply or not changed:
            return preview
        stamp = str(time.time_ns())
        backup = operation / ("hooks.json.dca-backup-" + stamp)
        if existed:
            _exclusive(backup, original)
        if base_content is not None:
            _exclusive(operation / ("config.toml.dca-backup-" + stamp), base_content)
        receipt = operation / ("dca-hooks-receipt-" + stamp + ".json")
        value = {
            "target": str(target),
            "backup": str(backup) if existed else None,
            "previous_exists": existed,
            "previous_sha256": _digest(original),
            "installed_sha256": _digest(updated),
        }
        _exclusive(receipt, (json.dumps(value, sort_keys=True) + "\n").encode())
        if target.exists() != existed or (existed and target.read_bytes() != original):
            raise ValueError("CONCURRENT_HOOK_CONFIGURATION_CHANGE")
        if base.exists() != (base_content is not None) or (
            base_content is not None and base.read_bytes() != base_content
        ):
            raise ValueError("CONCURRENT_BASE_CONFIGURATION_CHANGE")
        _replace(target, updated, original if existed else None)
        return {
            **preview,
            "status": "HOOKS_INSTALLED_PENDING_TRUST",
            "receipt": str(receipt),
        }


def rollback_hooks(receipt_path: str, *, apply: bool = False) -> dict:
    receipt = storage_path(receipt_path)
    value = json.loads(receipt.read_text())
    target = external_path(value["target"])
    operation = _operation_root(target.parent)
    if receipt.parent not in (target.parent, operation) or target.name != "hooks.json":
        raise ValueError("ROLLBACK_TARGET_INVALID")
    with ledger(operation / ".denovo-hook-installer") if apply else nullcontext():
        current = target.read_bytes()
        if _digest(current) != value["installed_sha256"]:
            raise ValueError("HOOK_CONFIG_CHANGED_REFUSE_ROLLBACK")
        backup = storage_path(value["backup"]) if value["backup"] else None
        if backup and backup.parent != receipt.parent:
            raise ValueError("BACKUP_PATH_INVALID")
        original = backup.read_bytes() if backup else b""
        if _digest(original) != value["previous_sha256"]:
            raise ValueError("BACKUP_HASH_MISMATCH")
        result = {
            "status": "ROLLBACK_PREVIEW",
            "previous_exists": value["previous_exists"],
        }
        if apply:
            saved = operation / ("hooks.json.dca-removed-" + str(time.time_ns()))
            _exclusive(saved, current)
            if value["previous_exists"]:
                _replace(target, original, current)
            else:
                target.unlink()
            result = {**result, "status": "ROLLED_BACK", "recoverable_copy": str(saved)}
        return result
