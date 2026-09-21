"""Validate task grants and compile them to bounded Codex host permissions.

No policy is obtained from model prose or a ModelProfile capability hint. Remote
MCP grants authorize exact remote tools, not access to the local filesystem.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .contracts import CapabilityPolicy


class CapabilityError(ValueError):
    """A grant cannot be safely validated or enforced by this adapter."""


@dataclass(frozen=True)
class InternalRuntimeGrant:
    """Pinned self-exec dependency, not part of AgentTask authorization.

    Exact-file compatibility for openai/codex#29049. Never infer a parent grant.
    The trusted adapter selects this executable; models/task JSON cannot supply it.
    """

    path: str
    sha256: str
    identity: tuple[int, ...]

    @staticmethod
    def _identity(info: os.stat_result) -> tuple[int, ...]:
        return (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_nlink,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )

    @classmethod
    def capture(cls, selected: str) -> InternalRuntimeGrant:
        """Refuse aliases/nonregular files and sanitize filesystem failures."""
        try:
            path = canonical_path(selected)
            if str(path) != selected:
                raise CapabilityError("Noncanonical selected Codex executable")
            before = path.stat()
            if not stat.S_ISREG(before.st_mode) or not os.access(path, os.X_OK):
                raise CapabilityError("Selected Codex executable is not executable")
            identity = cls._identity(before)
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as handle:
                if cls._identity(os.fstat(handle.fileno())) != identity:
                    raise CapabilityError("Selected Codex executable changed")
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
                if cls._identity(os.fstat(handle.fileno())) != identity:
                    raise CapabilityError("Selected Codex executable changed")
            if (
                canonical_path(selected) != path
                or cls._identity(path.stat()) != identity
            ):
                raise CapabilityError("Selected Codex executable changed")
            return cls(str(path), digest, identity)
        except (OSError, RuntimeError) as exc:
            raise CapabilityError(
                "Selected Codex executable: identity I/O failure"
            ) from exc

    def verify(self) -> None:
        if self.capture(self.path) != self:
            raise CapabilityError("Selected Codex executable changed after preflight")

    def record(self, version: str) -> dict:
        return {
            "source": "CODEX_SELF_EXEC_COMPAT",
            "path": self.path,
            "path_type": "file",
            "access": "READ_EXEC_SUPPORT",
            "filesystem_access": "read",
            "sha256": self.sha256,
            "path_identity": dict(
                zip(
                    (
                        "device",
                        "inode",
                        "mode",
                        "nlink",
                        "size",
                        "mtime_ns",
                        "ctime_ns",
                    ),
                    self.identity,
                    strict=True,
                )
            ),
            "selected_codex_version": version,
            "injected_by_dca": True,
        }


@dataclass(frozen=True)
class SelectedRuntimeProtectionScope:
    """Adapter-owned deny boundary, never a filesystem grant or task field.

    The only recognized installation layout is the canonical standalone release
    suffix: standalone/releases/<release-id>/bin/codex. Selection belongs to the
    trusted caller, not model output. No generic parent-directory heuristic.
    """

    executable: str
    protected_root: str
    root_identity: tuple[int, ...]

    @classmethod
    def derive(cls, grant: InternalRuntimeGrant) -> SelectedRuntimeProtectionScope:
        path = Path(grant.path)
        if len(path.parents) < 4 or not (
            path.name == "codex"
            and path.parent.name == "bin"
            and path.parents[2].name == "releases"
            and path.parents[3].name == "standalone"
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", path.parents[1].name)
        ):
            raise CapabilityError("RUNTIME_PROTECTION_BOUNDARY_UNRESOLVED")
        try:
            root = canonical_path(str(path.parents[1]))
            info = root.stat()
            if not stat.S_ISDIR(info.st_mode):
                raise CapabilityError("RUNTIME_PROTECTION_BOUNDARY_UNRESOLVED")
            return cls(grant.path, str(root), (info.st_dev, info.st_ino, info.st_mode))
        except (OSError, RuntimeError) as exc:
            raise CapabilityError("RUNTIME_PROTECTION_BOUNDARY_UNRESOLVED") from exc

    def record(self, grant: InternalRuntimeGrant, version: str) -> dict:
        return {
            "selected_executable": self.executable,
            "canonical_executable": self.executable,
            "runtime_layout": "CODEX_STANDALONE_RELEASE",
            "protected_root": self.protected_root,
            "derivation_method": "canonical_standalone_release_suffix_v1",
            "root_identity": list(self.root_identity),
            "executable_identity": grant.record(version)["path_identity"],
            "executable_sha256": grant.sha256,
            "codex_version": version,
            "purpose": "USER_GRANT_DENY_BOUNDARY_NOT_A_GRANT",
            "user_grants_check": "PENDING",
        }


def canonical_path(value: str, *, must_exist: bool = True) -> Path:
    path = Path(value)
    if not path.is_absolute() or any(c in value for c in "*?[]\x00\r\n"):
        raise CapabilityError("Absolute, literal capability paths are required")
    if ".." in path.parts or path.resolve(strict=False) != path:
        raise CapabilityError("Noncanonical or symlink capability path")
    if path == Path(path.anchor) or path == Path.home():
        raise CapabilityError("Filesystem-root or home-wide grants are forbidden")
    if must_exist and not (path.is_file() or path.is_dir()):
        raise CapabilityError("Capability path must be an existing file/directory")
    if path.is_file() and path.stat().st_nlink != 1:
        raise CapabilityError("Hard-linked capability files are forbidden")
    return path


def permits(policy: CapabilityPolicy, path: str | Path, *, write: bool = False) -> bool:
    """Conservative event/fixture check, not a replacement for the OS sandbox."""
    try:
        target = canonical_path(str(path), must_exist=False)
        grants = policy.write_paths if write else policy.read_paths + policy.write_paths
        return any(
            target == (root := canonical_path(raw))
            or (root.is_dir() and target.is_relative_to(root))
            for raw in grants
        )
    except (OSError, RuntimeError, CapabilityError):
        return False


def validate_paths(
    policy: CapabilityPolicy, *, protected: tuple[Path, ...] = ()
) -> None:
    for raw in policy.read_paths + policy.write_paths:
        path = canonical_path(raw)
        for forbidden in protected:
            forbidden = forbidden.resolve()
            if (
                path == forbidden
                or path.is_relative_to(forbidden)
                or forbidden.is_relative_to(path)
            ):
                raise CapabilityError(
                    "Capability overlaps runtime/auth/artifact storage"
                )
        if path.is_dir():

            def denied_walk(error: OSError) -> None:
                raise CapabilityError(
                    "Capability directory cannot be fully checked"
                ) from error

            for parent, dirs, files in os.walk(
                path, followlinks=False, onerror=denied_walk
            ):
                for name in dirs + files:
                    canonical_path(str(Path(parent) / name))


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def policy_record(
    policy: CapabilityPolicy,
    *,
    applied: bool,
    compiled: dict | None = None,
    runtime_protection: dict | None = None,
) -> dict:
    value = policy.model_dump(mode="json")
    return {
        "requested": value,
        "requested_policy_sha256": _digest(value),
        "compiled_command_policy": copy.deepcopy(compiled),
        "compiled_command_policy_sha256": _digest(compiled) if compiled else None,
        "compiled_command_policy_emitted": applied and compiled is not None,
        "internal_runtime_support_grants": copy.deepcopy(
            (compiled or {}).get("internal_runtime_support_grants", [])
        ),
        "runtime_protection": copy.deepcopy(runtime_protection),
        "enforcement_confirmation": "NOT_VERIFIED",
        "grant_source": "AgentTask.capability_policy",
        "approval": "never",
        "agent_recursion": False,
        "shell_network": False,
        "filesystem_enforcement": (
            "codex_named_permissions" if not policy.restricted else "v1_restricted"
        ),
        "remote_mcp_scope": (
            "remote tools only; not governed by local filesystem grants"
        ),
        "host_os_isolation_verified": False,
    }


SYSTEM_CONFIG = Path("/etc/codex/config.toml")


def _table(value: object, field: str) -> dict:
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise CapabilityError(f"Host {field} must be a table")
    return value


def _strings(value: object, field: str) -> None:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise CapabilityError(f"Host {field} must be a string array")


def _string_table(value: object, field: str) -> None:
    if not all(isinstance(v, str) for v in _table(value, field).values()):
        raise CapabilityError(f"Host {field} must contain strings")


def _fields(table: dict, names: tuple[str, ...], kind: type, field: str) -> None:
    if any(name in table and type(table[name]) is not kind for name in names):
        raise CapabilityError(f"Host {field} has an invalid field type")


def _validate_layer(value: object, *, selected: bool = False) -> dict:
    """Validate every table consumed below, without echoing configuration values."""
    layer = _table(value, "configuration")
    if selected and "profiles" in layer:
        raise CapabilityError("Nested host profiles are unsupported")
    for profile in _table(layer.get("profiles", {}), "profiles").values():
        _validate_layer(profile, selected=True)
    for server in _table(layer.get("mcp_servers", {}), "mcp_servers").values():
        server = _table(server, "MCP server")
        _fields(server, ("url", "command", "cwd", "bearer_token_env_var"), str, "MCP")
        _fields(server, ("enabled", "required"), bool, "MCP")
        for name in ("enabled_tools", "disabled_tools", "args", "env_vars", "scopes"):
            if name in server:
                _strings(server[name], "MCP " + name)
        for name in ("env", "http_headers", "env_http_headers"):
            if name in server:
                _string_table(server[name], "MCP " + name)
        for name in ("startup_timeout_sec", "startup_timeout_ms", "tool_timeout_sec"):
            if name in server and type(server[name]) not in (int, float):
                raise CapabilityError("Host MCP timeout must be numeric")
    for permission in _table(layer.get("permissions", {}), "permissions").values():
        permission = _table(permission, "permission entry")
        if "filesystem" in permission:
            _string_table(permission["filesystem"], "permission filesystem")
        if "network" in permission:
            network = _table(permission["network"], "permission network")
            _fields(network, ("enabled",), bool, "permission network")
    environment = _table(layer.get("shell_environment_policy", {}), "shell environment")
    for name in ("set", "filter"):
        if name in environment:
            _string_table(environment[name], "shell environment " + name)
    for name in ("include_only", "exclude"):
        if name in environment:
            _strings(environment[name], "shell environment " + name)
    _fields(environment, ("inherit",), str, "shell environment")
    _fields(
        environment,
        ("ignore_default_excludes", "experimental_use_profile"),
        bool,
        "shell environment",
    )
    _table(layer.get("sandbox_workspace_write", {}), "sandbox_workspace_write")
    for project in _table(layer.get("projects", {}), "projects").values():
        project = _table(project, "project trust")
        if "trust_level" in project and project["trust_level"] not in (
            "trusted",
            "untrusted",
        ):
            raise CapabilityError("Invalid host project trust level")
    if "project_root_markers" in layer:
        markers = layer["project_root_markers"]
        _strings(markers, "project_root_markers")
        if not markers or any(
            not m or m in (".", "..") or any(c in m for c in "/\\*?[]") for m in markers
        ):
            raise CapabilityError("Unsupported host project root markers")
    return layer


def _merge(target: dict, source: dict) -> None:
    """Codex tables merge recursively; arrays/scalars replace, not concatenate."""
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def _read_layers(file: Path, profile_id: str) -> list[dict]:
    if not file.exists():
        return []
    if file.is_symlink() or file.resolve() != file.absolute():
        raise CapabilityError("Symlink host configuration is unsupported")
    try:
        with file.open("rb") as stream:
            value = _validate_layer(tomllib.load(stream))
    except (OSError, ValueError) as exc:
        if isinstance(exc, CapabilityError):
            raise
        raise CapabilityError("Host configuration cannot be safely parsed") from None
    selected = value.get("profiles", {}).get(profile_id)
    if selected is not None and set(selected) & {
        "mcp_servers",
        "permissions",
        "shell_environment_policy",
        "projects",
        "project_root_markers",
        "sandbox_mode",
        "sandbox_workspace_write",
    }:
        # Do not guess legacy named-profile application order relative to
        # sidecars/project layers. Require unambiguous task-runtime config.
        raise CapabilityError(
            "Capability settings in legacy named profiles are ambiguous"
        )
    return [value] + ([selected] if selected is not None else [])


def _canonical_trust_path(raw: str) -> Path:
    """Only lexical dot/separator normalization; never infer a symlink alias."""
    path = Path(raw)
    if (
        not path.is_absolute()
        or raw.startswith("//")
        or ".." in path.parts
        or any(c in raw for c in "\x00\r\n*?[]")
    ):
        raise CapabilityError("Trust record requires a canonical absolute path")
    try:
        if path.resolve(strict=False) != path:
            raise CapabilityError("Symlink trust mapping is unsupported")
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, CapabilityError):
            raise
        raise CapabilityError("Trust record path cannot be safely resolved") from None
    return path


def _check_project_trust(
    layers: list[dict], root: Path, cwd: Path, *, required: bool
) -> None:
    """Check the entire interval before reading even its first project config.

    Inspect original base layers, not a merged trust map: a later trusted record
    must not erase an earlier explicit deny. Distinct spellings for one path are
    ambiguous even when their values happen to agree.
    """
    spellings: dict[Path, str] = {}
    applicable: dict[Path, set[str]] = {}
    for layer in layers:
        for raw, record in layer.get("projects", {}).items():
            path = _canonical_trust_path(raw)
            if path in spellings and spellings[path] != raw:
                raise CapabilityError("Duplicate semantic trust path mapping")
            spellings[path] = raw
            if not (path.is_relative_to(root) and cwd.is_relative_to(path)):
                continue
            level = record.get("trust_level")
            if level not in ("trusted", "untrusted"):
                raise CapabilityError("Applicable project trust record is ambiguous")
            applicable.setdefault(path, set()).add(level)
    if required and applicable.get(root) != {"trusted"}:
        raise CapabilityError(
            "Project configuration requires an explicit trusted canonical root"
        )
    if any("untrusted" in levels for levels in applicable.values()):
        raise CapabilityError("Explicit project trust deny on root-to-cwd path")


def _config_layers(home: Path, profile_id: str, cwd: Path) -> list[dict]:
    result: list[dict] = []
    for file in dict.fromkeys(
        (SYSTEM_CONFIG, home / "config.toml", home / f"{profile_id}.config.toml")
    ):
        result.extend(_read_layers(file, profile_id))
    base: dict = {}
    for layer in result:
        _merge(base, layer)
    cwd = cwd.resolve()
    ancestors = (cwd, *cwd.parents)
    markers = base.get("project_root_markers", [".git"])
    root = next((p for p in ancestors if any((p / m).exists() for m in markers)), None)
    candidates = ancestors[: ancestors.index(root) + 1] if root else ancestors
    projects = [
        p / ".codex" / "config.toml"
        for p in reversed(candidates)
        if (p / ".codex" / "config.toml").exists()
    ]
    if root is not None:
        _check_project_trust(result, root, cwd, required=bool(projects))
    elif projects:
        raise CapabilityError(
            "Project configuration requires an explicit trusted canonical root"
        )
    if projects:
        for file in projects:
            layers = _read_layers(file, profile_id)
            if any("projects" in v or "project_root_markers" in v for v in layers):
                raise CapabilityError(
                    "Project configuration cannot redefine trust/root discovery"
                )
            result.extend(layers)
    return result


def endpoint_record(value: str) -> dict:
    """No path, query, userinfo, headers or credential values enter provenance."""
    try:
        url = urlsplit(value)
        return {
            "scheme": url.scheme,
            "hostname": url.hostname,
            "port": url.port,
            "identity_sha256": hashlib.sha256(value.encode()).hexdigest(),
        }
    except ValueError:
        raise CapabilityError("Invalid MCP endpoint") from None


def redacted_command(command: tuple[str, ...]) -> list[str]:
    preview = list(command)
    for index, part in enumerate(preview):
        if part.startswith("mcp_servers.") and ".url=" in part:
            key, value = part.split("=", 1)
            identity = endpoint_record(json.loads(value))["identity_sha256"]
            preview[index] = (
                key + "=" + json.dumps("REDACTED_ENDPOINT_SHA256:" + identity)
            )
    return preview


def host_overrides(
    policy: CapabilityPolicy,
    *,
    home: Path,
    profile_id: str,
    cwd: Path,
    cli_version: str | None = None,
    compiled: dict | None = None,
    internal_runtime_grant: InternalRuntimeGrant | None = None,
) -> list[str]:
    """No profile inheritance may add tools or broaden our permission table."""
    validate_paths(policy, protected=(home,))
    layers = _config_layers(home, profile_id, cwd)
    effective: dict = {}
    for layer in layers:
        _merge(effective, layer)
    servers = effective.get("mcp_servers", {})
    grants: dict[str, list[str]] = {}
    for tool in policy.tools:
        if tool.startswith("mcp:"):
            _, server, name = tool.split(":")
            grants.setdefault(server, []).append(name)
    overrides = []
    mcp_record = {}
    for server in sorted(servers.keys() | grants.keys()):
        key = "mcp_servers." + json.dumps(server)
        if server not in grants:
            overrides.append(f"{key}.enabled=false")
            mcp_record[server] = {"enabled": False}
            continue
        definition = servers.get(server, {})
        # Stdio MCP is outside Codex's command sandbox; do not grant it implicitly.
        endpoint = definition.get("url", "")
        details = endpoint_record(endpoint)
        url = urlsplit(endpoint)
        if (
            "command" in definition
            or details["scheme"] != "https"
            or not details["hostname"]
        ):
            raise CapabilityError(
                "Only explicitly configured HTTPS MCP servers are supported"
            )
        if url.username or url.password:
            raise CapabilityError("Credential-bearing MCP URL is forbidden")
        if definition.get("enabled", True) is False:
            raise CapabilityError("MCP grant conflicts with host disabled server")
        if "enabled_tools" in definition and not set(grants[server]).issubset(
            definition["enabled_tools"]
        ):
            raise CapabilityError("MCP grant exceeds host enabled_tools")
        if set(grants[server]) & set(definition.get("disabled_tools", [])):
            raise CapabilityError("MCP grant conflicts with host disabled_tools")
        tools = sorted(grants[server])
        disabled = definition.get("disabled_tools", [])
        # Pin the endpoint actually checked, not just the tool list. Never put
        # headers or environment credential values in argv or provenance.
        overrides.extend(
            [
                f"{key}.url={json.dumps(endpoint)}",
                f"{key}.enabled=true",
                f"{key}.enabled_tools={json.dumps(tools)}",
                f"{key}.disabled_tools={json.dumps(disabled)}",
            ]
        )
        mcp_record[server] = {
            "enabled": True,
            "transport": "https",
            "endpoint": details,
            "enabled_tools": tools,
            "disabled_tools": disabled,
        }
    if compiled is not None:
        compiled.update(
            {
                "working_directory": str(cwd.resolve()),
                "sandbox": "read-only" if policy.restricted else "named:dca_task",
                "filesystem_enforcement": (
                    "v1_restricted" if policy.restricted else "codex_named_permissions"
                ),
                "read_paths": sorted(set(policy.read_paths + policy.write_paths)),
                "write_paths": sorted(policy.write_paths),
                "mcp_servers": mcp_record,
                "internal_runtime_support_grants": [],
            }
        )
    if policy.restricted:
        if compiled is not None:
            compiled["filesystem"] = {":root": "read"}
        return overrides
    if cli_version is not None:
        version = re.search(r"(\d+)\.(\d+)\.(\d+)", cli_version)
        if not version or tuple(map(int, version.groups())) < (0, 153, 4):
            raise CapabilityError("Capability grants require Codex >= 0.153.4")
    # Legacy and named permission syntaxes cannot safely be combined. Refuse,
    # rather than rewriting a user's sidecar or pretending an override won.
    if any("sandbox_mode" in v or "sandbox_workspace_write" in v for v in layers):
        raise CapabilityError(
            "Use a task runtime profile without legacy "
            "sandbox_mode/sandbox_workspace_write"
        )
    if any("dca_task" in v.get("permissions", {}) for v in layers):
        raise CapabilityError("Reserved dca_task permissions already configured")
    if any(v.get("shell_environment_policy", {}).get("set") for v in layers):
        raise CapabilityError("Task runtime must not inject shell environment values")
    filesystem = {":minimal": "read", str(cwd): "read"}
    filesystem.update({p: "read" for p in policy.read_paths})
    filesystem.update({p: "write" for p in policy.write_paths})
    if internal_runtime_grant is not None:
        internal_runtime_grant.verify()
        # Caller writes cannot replace the runtime, even outside CODEX_HOME.
        validate_paths(
            CapabilityPolicy(write_paths=policy.write_paths),
            protected=(Path(internal_runtime_grant.path),),
        )
        filesystem[internal_runtime_grant.path] = "read"
        if compiled is not None:
            compiled["internal_runtime_support_grants"] = [
                internal_runtime_grant.record(cli_version or "NOT_REPORTED")
            ]
    if compiled is not None:
        compiled["filesystem"] = filesystem
    literal = (
        "{"
        + ",".join(f"{json.dumps(k)}={json.dumps(v)}" for k, v in filesystem.items())
        + "}"
    )
    overrides += [
        'default_permissions="dca_task"',
        f"permissions.dca_task.filesystem={literal}",
        "permissions.dca_task.network.enabled=false",
    ]
    return overrides


def event_violation(item: dict, policy: CapabilityPolicy) -> str | None:
    """Additional terminal audit. Prevention belongs to the compiled host policy."""
    kind = item.get("type", "")
    if kind == "command_execution":
        if not ({"shell", "unified_exec"} & set(policy.tools)):
            return "command tool not authorized"
        command = item.get("command", "")
        if (
            not isinstance(command, str)
            or not command.strip()
            or re.search(r"(?:^|[\s/;|])(?:codex|dca)(?:\s|$)", command, re.I)
        ):
            return "recursive or invalid command"
        return None
    if kind == "web_search":
        return None if "web_search" in policy.tools else "web_search not authorized"
    if kind == "mcp_tool_call":
        name = f"mcp:{item.get('server')}:{item.get('tool')}"
        return None if name in policy.tools else "MCP tool not authorized"
    # Native apply_patch cannot be enabled as an independent allowlisted tool in
    # this version. File writes must use the authorized command tool + OS roots.
    return f"tool not authorized: {kind}"
