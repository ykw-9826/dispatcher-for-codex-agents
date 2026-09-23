"""Preview-first user hooks merge and guarded rollback; never edit providers."""

from __future__ import annotations

import ast
import configparser
import copy
import email.parser
import fcntl
import hashlib
import io
import json
import os
import re
import shlex
import stat
import time
import tokenize
import tomllib
from contextlib import nullcontext
from pathlib import Path

from dispatcher_for_codex_agents.workspace_paths import workspace_root

from .core import external_path, ledger, storage_path
from .sinks import HOOK_TIMEOUT_SECONDS

EVENTS = ("UserPromptSubmit", "Stop", "SessionEnd", "Interrupt", "PermissionRequest")
MARKER = "DCA — Dispatcher for Codex Agents notification"
# Explicit migration detection only; never a public command alias.
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
                "timeout": HOOK_TIMEOUT_SECONDS,
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


def _launcher_structure(body: str, namespace: str) -> None:
    """Accept only console-wrapper ASTs, never arbitrary code containing tokens."""
    try:
        nodes = ast.parse(body).body
    except (SyntaxError, ValueError):
        raise ValueError("RELEASE_LAUNCHER_INVALID") from None

    def tree(node):
        return ast.dump(node, include_attributes=False)

    def statements(source):
        return [tree(node) for node in ast.parse(source).body]

    allowed_imports = {
        statements("import sys")[0]: "sys",
        statements("import re")[0]: "re",
        statements(f"from {namespace}.notifications.cli import main")[0]: "main",
    }
    imports = set()
    while nodes and isinstance(nodes[0], (ast.Import, ast.ImportFrom)):
        name = allowed_imports.get(tree(nodes.pop(0)))
        if name is None or name in imports:
            raise ValueError("RELEASE_LAUNCHER_INVALID")
        imports.add(name)
    if not {"sys", "main"} <= imports:
        raise ValueError("RELEASE_LAUNCHER_INVALID")
    if len(nodes) == 1 and isinstance(nodes[0], ast.If):
        guard = nodes[0]
        if guard.orelse or tree(guard.test) != tree(
            ast.parse('__name__ == "__main__"', mode="eval").body
        ):
            raise ValueError("RELEASE_LAUNCHER_INVALID")
        nodes = guard.body
    if not nodes or tree(nodes[-1]) not in {
        statements("sys.exit(main())")[0],
        statements("raise SystemExit(main())")[0],
    }:
        raise ValueError("RELEASE_LAUNCHER_INVALID")
    # uv's suffix/slice normalization and pip/distlib's re.sub normalization.
    # Compare whole statements, including destinations, slices, arguments, guards
    # and branches: an otherwise-correct import/call cannot hide extra logic.
    prefix = [tree(node) for node in nodes[:-1]]
    uv = statements(
        'if sys.argv[0].endswith("-script.pyw"):\n'
        "    sys.argv[0] = sys.argv[0][:-11]\n"
        'elif sys.argv[0].endswith(".exe"):\n'
        "    sys.argv[0] = sys.argv[0][:-4]\n"
    )
    pip = [
        statements(f"sys.argv[0] = re.sub({pattern!r}, '', sys.argv[0])")
        for pattern in (r"(-script\.pyw|\.exe)?$", r"(-script\.pyw?|\.exe)?$")
    ]
    if prefix not in ([], uv) and not ("re" in imports and prefix in pip):
        raise ValueError("RELEASE_LAUNCHER_INVALID")


def _release_executable(value: str, *, destination: bool) -> dict:
    """Read-only identity check. Never run an unknown executable for validation."""
    path = Path(value)
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError("CANONICAL_RELEASE_EXECUTABLE_REQUIRED")
    info = path.stat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != os.getuid()
        or not os.access(path, os.X_OK)
    ):
        raise ValueError("RELEASE_EXECUTABLE_INVALID")
    # Retired names are recognized only for explicitly requested migration.
    legacy = path.name == "b2m-notify"
    if path.name not in {"dca-notify", "b2m-notify"} or (destination and legacy):
        raise ValueError("UNKNOWN_NOTIFICATION_EXECUTABLE")
    venv = path.parent.parent
    release = venv.parent
    if (
        path.parent.name != "bin"
        or venv.name != "venv"
        or release.parent.name != "releases"
        or not re.fullmatch(r"[0-9a-f]{7,40}", release.name)
    ):
        raise ValueError("COMMIT_ADDRESSED_RELEASE_REQUIRED")
    distribution = (
        "denovo-codex-agent-tool" if legacy else "dispatcher-for-codex-agents"
    )
    namespace = "denovo_codex_agent_tool" if legacy else "dispatcher_for_codex_agents"
    metadata = list(
        (venv / "lib").glob(
            "python*/site-packages/"
            + distribution.replace("-", "_")
            + "-*.dist-info/METADATA"
        )
    )
    if len(metadata) != 1 or metadata[0].resolve() != metadata[0]:
        raise ValueError("RELEASE_METADATA_INVALID")
    msg = email.parser.BytesParser().parsebytes(metadata[0].read_bytes())
    if msg["Name"] != distribution or msg["Version"] not in (
        {"1.0.3"} if destination else {"1.0.0", "1.0.1", "1.0.2", "1.0.3"}
    ):
        raise ValueError("RELEASE_VERSION_INVALID")
    entries = configparser.ConfigParser()
    entries.read_string((metadata[0].parent / "entry_points.txt").read_text())
    if (
        entries.get("console_scripts", path.name)
        != namespace + ".notifications.cli:main"
    ):
        raise ValueError("RELEASE_ENTRYPOINT_INVALID")
    raw = path.read_bytes()
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
        if encoding not in {"utf-8", "utf-8-sig"}:
            raise ValueError
        body = raw.decode(encoding)
    except (SyntaxError, UnicodeError, ValueError):
        raise ValueError("RELEASE_LAUNCHER_INVALID") from None
    interpreter = Path(body.split("\n", 1)[0].removeprefix("#!"))
    if (
        not raw.startswith(b"#!")
        or not body.startswith("#!" + str(interpreter) + "\n")
        or interpreter.parent != venv / "bin"
        or not re.fullmatch(r"python(?:3(?:\.[0-9]+)?)?", interpreter.name)
    ):
        raise ValueError("RELEASE_LAUNCHER_INVALID")
    _launcher_structure(body, namespace)
    if not interpreter.is_file():
        raise ValueError("RELEASE_PYTHON_MISSING")
    return {
        "path": str(path),
        "sha256": _digest(raw),
        "distribution": distribution,
        "version": msg["Version"],
    }


def migrate_hooks(
    codex_home: str,
    executable: str,
    config_path: str,
    *,
    from_executables: tuple[str, ...],
    expected_sha256: str | None = None,
    apply: bool = False,
) -> dict:
    """Explicit three-hook migration, preview first. No trust/secret/ledger reads."""
    target_identity = _release_executable(executable, destination=True)
    sources = {s: _release_executable(s, destination=False) for s in from_executables}
    if len(sources) != len(from_executables):
        raise ValueError("DUPLICATE_MIGRATION_SOURCE")
    accepted = {**sources, executable: target_identity}
    home = external_path(codex_home)
    target = external_path(home / "hooks.json")
    original = target.read_bytes()
    document = json.loads(original)
    config = storage_path(config_path)
    config_bytes = config.read_bytes()  # non-secret config only, never load sinks
    base = external_path(home / "config.toml")
    base_bytes = base.read_bytes() if base.exists() else None
    parsed = tomllib.loads(base_bytes.decode()) if base_bytes else {}
    if parsed.get("notify") or any(
        n in json.dumps(parsed.get("hooks", {}))
        for n in (MARKER, RETIRED_MARKER, "b2m-notify", "dca-notify")
    ):
        raise ValueError("CONFLICTING_HOST_NOTIFICATION_REGISTRATION")
    if not isinstance(document, dict) or not isinstance(document.get("hooks"), dict):
        raise ValueError("HOOK_CONFIG_INVALID")
    merged = copy.deepcopy(document)
    changes = []
    events = {"UserPromptSubmit", "Stop", "PermissionRequest"}
    for event, groups in merged["hooks"].items():
        if not isinstance(groups, list):
            raise ValueError("HOOK_CONFIG_INVALID")
        recognized = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise ValueError("HOOK_CONFIG_INVALID")
            for handler in group["hooks"]:
                if not isinstance(handler, dict):
                    raise ValueError("HOOK_CONFIG_INVALID")
                if any(
                    n in json.dumps(handler)
                    for n in (MARKER, RETIRED_MARKER, "b2m-notify", "dca-notify")
                ):
                    if (
                        event not in events
                        or set(group) != {"hooks"}
                        or len(group["hooks"]) != 1
                    ):
                        raise ValueError("UNKNOWN_LEGACY_HOOK_STATE")
                    args = shlex.split(handler.get("command", ""))
                    if (
                        not args
                        or args[0] not in accepted
                        or args
                        != [args[0], "hook", "--event", event, "--config", str(config)]
                        or handler["command"] != shlex.join(args)
                    ):
                        raise ValueError("UNKNOWN_LEGACY_EXECUTABLE_OR_ARGS")
                    if (
                        set(handler) != {"type", "command", "timeout", "statusMessage"}
                        or handler["type"] != "command"
                        or handler["statusMessage"] not in (MARKER, RETIRED_MARKER)
                        or type(handler["timeout"]) is not int
                        or handler["timeout"] not in (3, 4, HOOK_TIMEOUT_SECONDS)
                    ):
                        raise ValueError("UNKNOWN_LEGACY_HOOK_STATE")
                    recognized.append(handler)
        if event in events:
            if len(recognized) != 1:
                raise ValueError("MISSING_OR_DUPLICATE_DCA_HANDLER")
            handler = recognized[0]
            desired = {
                "type": "command",
                "command": shlex.join(
                    [executable, "hook", "--event", event, "--config", str(config)]
                ),
                "timeout": HOOK_TIMEOUT_SECONDS,
                "statusMessage": MARKER,
            }
            if handler != desired:
                changes.append(
                    {"event": event, "before": dict(handler), "after": desired}
                )
                handler.clear()
                handler.update(desired)
    if not events <= set(merged["hooks"]):
        raise ValueError("MISSING_DCA_HANDLER")
    preview = {
        "status": "HOOK_MIGRATION_PREVIEW",
        "changed": bool(changes),
        "previous_sha256": _digest(original),
        "changes": changes,
        "source_identities": list(sources.values()),
        "target_identity": target_identity,
        "trust_status": "USER_REVIEW_REQUIRED",
        "config_modified": False,
        "ledger_modified": False,
    }
    if not apply or not changes:
        return preview
    if expected_sha256 != _digest(original):
        raise ValueError("MIGRATION_PREVIEW_HASH_REQUIRED_OR_CHANGED")
    operation = _operation_root(home)
    operation.mkdir(mode=0o700, parents=True, exist_ok=True)
    with ledger(operation / ".denovo-hook-installer"):
        if (
            target.read_bytes() != original
            or config.read_bytes() != config_bytes
            or (base.read_bytes() if base.exists() else None) != base_bytes
        ):
            raise ValueError("CONCURRENT_MIGRATION_CHANGE")
        if any(
            _release_executable(p, destination=(p == executable)) != identity
            for p, identity in accepted.items()
        ):
            raise ValueError("RELEASE_EXECUTABLE_CHANGED")
        stamp = str(time.time_ns())
        backup = operation / ("hooks.json.dca-backup-" + stamp)
        _exclusive(backup, original)
        updated = (json.dumps(merged, sort_keys=True, indent=2) + "\n").encode()
        receipt = operation / ("dca-hooks-receipt-" + stamp + ".json")
        value = {
            **preview,
            "target": str(target),
            "backup": str(backup),
            "previous_exists": True,
            "installed_sha256": _digest(updated),
        }
        _exclusive(receipt, (json.dumps(value, sort_keys=True) + "\n").encode())
        _replace(target, updated, original)
    return {
        **preview,
        "status": "HOOKS_MIGRATED_PENDING_TRUST",
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
