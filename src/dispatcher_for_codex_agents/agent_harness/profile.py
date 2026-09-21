"""Read-only resolution of effective model/provider from Codex sidecars."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .contracts import validate_local_identifier


class ProfileResolutionError(ValueError):
    """Raised when a requested sidecar profile cannot be resolved safely."""

    def __init__(self, message: str, *, compatibility: dict | None = None) -> None:
        super().__init__(message)
        self.compatibility = dict(compatibility or {})


def _configuration_error(
    error: Exception, *, role: str, basename: str
) -> ProfileResolutionError:
    """Normalize expected failures without copying OS/parser error contents."""
    if isinstance(error, ProfileResolutionError):
        return error
    if isinstance(error, UnicodeDecodeError):
        category = "INVALID_UTF8"
    elif isinstance(error, tomllib.TOMLDecodeError):
        category = "INVALID_TOML"
    elif isinstance(error, OSError):
        category = "IO_ERROR"
    else:
        # Used only for ValueError/RuntimeError from home path normalization.
        category = "INVALID_PATH_IDENTITY"
    return ProfileResolutionError(f"{role} ({basename!r}): {category}")


@dataclass(frozen=True, slots=True)
class ResolvedSidecarProfile:
    """Non-secret subset of the effective Codex profile."""

    profile_id: str
    configured_model: str
    configured_provider: str
    sidecar_path: Path
    compatibility: dict

    def provenance(self, cli_version: str = "NOT_PROBED") -> dict:
        return {**self.compatibility, "codex_cli_version": cli_version}


class SidecarProfileResolver:
    """Resolve model/provider while deliberately ignoring credential values."""

    def __init__(self, codex_home: str | Path | None = None) -> None:
        configured_home = codex_home or os.environ.get("CODEX_HOME")
        try:
            self.codex_home = (
                Path(configured_home).expanduser()
                if configured_home is not None
                else Path.home() / ".codex"
            ).resolve()
        except (OSError, ValueError, RuntimeError) as exc:
            raise _configuration_error(
                exc, role="config home", basename="CODEX_HOME"
            ) from None

    @staticmethod
    def _load_toml(
        path: Path,
        *,
        required: bool,
        role: str = "config",
        fingerprints: dict | None = None,
    ) -> dict[str, object]:
        def invalid(category: str) -> ProfileResolutionError:
            # Never copy decoder/parser messages, config content or full paths.
            return ProfileResolutionError(f"{role} ({path.name!r}): {category}")

        try:
            if path.is_symlink():
                raise invalid("AMBIGUOUS_CONFIG_PATH")
            if not path.is_file():
                if path.exists():
                    raise invalid("NOT_REGULAR_FILE")
                if required:
                    raise invalid("MISSING")
                return {}
            if path.stat().st_nlink != 1:
                raise invalid("AMBIGUOUS_CONFIG_PATH")
            raw = path.read_bytes()
            if fingerprints is not None:
                fingerprints[path.name] = hashlib.sha256(raw).hexdigest()
            value = tomllib.load(io.BytesIO(raw))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise _configuration_error(exc, role=role, basename=path.name) from None
        if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
            raise invalid("INVALID_STRUCTURE")
        for name in (
            "model",
            "model_provider",
            "model_reasoning_effort",
            "model_verbosity",
        ):
            if name in value and (
                not isinstance(value[name], str)
                or not value[name].strip()
                or value[name] != value[name].strip()
            ):
                raise invalid("INVALID_STRUCTURE")
        for name in ("model_reasoning_effort", "model_verbosity"):
            if name in value and any(ord(c) < 32 or ord(c) == 127 for c in value[name]):
                raise invalid("INVALID_STRUCTURE")
        if "model_verbosity" in value and value["model_verbosity"] not in {
            "low",
            "medium",
            "high",
        }:
            raise invalid("INVALID_STRUCTURE")
        for name in ("profiles", "model_providers"):
            if name in value and (
                not isinstance(value[name], dict)
                or not all(isinstance(v, dict) for v in value[name].values())
            ):
                raise invalid("INVALID_STRUCTURE")
        return value

    def resolve(self, profile_id: str) -> ResolvedSidecarProfile:
        """Resolve one standalone identity; never merge legacy or base defaults."""
        try:
            validate_local_identifier(profile_id, field_name="profile_id")
            if profile_id.endswith(".config.toml"):
                raise ValueError("filename is not a profile identifier")
        except (ValueError, TypeError):
            raise ProfileResolutionError("AMBIGUOUS_PROFILE_IDENTIFIER") from None
        base_path = self.codex_home / "config.toml"
        sidecar_path = self.codex_home / f"{profile_id}.config.toml"
        record = {
            "requested_profile": profile_id,
            "profile_layout": "missing",
            "sidecar_basename": sidecar_path.name,
            "sidecar_path_identity": f"CODEX_HOME/{sidecar_path.name}",
            "selected_sidecar_sha256": "NOT_REPORTED",
            "compatibility_warnings": [],
        }
        fingerprints: dict[str, str] = {}
        role, path = "sidecar profile", sidecar_path
        try:
            present = sidecar_path.exists() or sidecar_path.is_symlink()
            record["profile_layout"] = "standalone" if present else "missing"
            role, path = "base config", base_path
            base = self._load_toml(base_path, required=False, role="base config")
            legacy = base.get("profiles", {})
            if profile_id in legacy:
                record["profile_layout"] = "conflict" if present else "legacy"
                raise ProfileResolutionError(
                    "PROFILE_LAYOUT_CONFLICT"
                    if present
                    else "LEGACY_MIGRATION_REQUIRED"
                )
            if "profile" in base or legacy:
                # Conservative DCA policy, not a claim about every older CLI.
                if not present:
                    record["profile_layout"] = "legacy"
                raise ProfileResolutionError(
                    "LEGACY_SELECTOR_OR_TABLE_MIGRATION_REQUIRED"
                )
            role, path = "sidecar profile", sidecar_path
            sidecar = self._load_toml(
                sidecar_path,
                required=True,
                role="sidecar profile",
                fingerprints=fingerprints,
            )
            if any(
                key in sidecar for key in ("profile", "profiles", "model_providers")
            ):
                record["profile_layout"] = "conflict"
                raise ProfileResolutionError("SIDECAR_AUTHORITY_CONFLICT")
            model = sidecar.get("model")
            provider = sidecar.get("model_provider")
            if model is None or provider is None:
                raise ProfileResolutionError("SIDECAR_MODEL_AND_PROVIDER_REQUIRED")
            role, path = "base config", base_path
            definition = base.get("model_providers", {}).get(provider)
            if definition is None:
                raise ProfileResolutionError("BASE_PROVIDER_DEFINITION_MISSING")
            safe = self._provider_fingerprint(definition)
            record.update(
                {
                    "configured_model": model,
                    "configured_provider": provider,
                    "model_reasoning_effort": sidecar.get("model_reasoning_effort"),
                    "model_verbosity": sidecar.get("model_verbosity"),
                    "base_provider_identity": f"config.toml:model_providers.{provider}",
                    "base_provider_safe_sha256": safe,
                }
            )
        except (
            ProfileResolutionError,
            OSError,
            UnicodeDecodeError,
            tomllib.TOMLDecodeError,
        ) as exc:
            error = _configuration_error(exc, role=role, basename=path.name)
            record["selected_sidecar_sha256"] = fingerprints.get(
                sidecar_path.name, "NOT_REPORTED"
            )
            record["compatibility_warnings"] = [str(error)]
            raise ProfileResolutionError(str(error), compatibility=record) from None
        record["selected_sidecar_sha256"] = fingerprints[sidecar_path.name]
        return ResolvedSidecarProfile(
            profile_id=profile_id,
            configured_model=model,
            configured_provider=provider,
            sidecar_path=sidecar_path,
            compatibility=record,
        )

    @staticmethod
    def _provider_fingerprint(definition: dict) -> str:
        """Fingerprint only connection metadata, never auth/header/env values."""
        safe: dict = {}
        for name in ("name", "base_url", "wire_api", "env_key"):
            if name in definition and (
                not isinstance(definition[name], str) or not definition[name].strip()
            ):
                raise ProfileResolutionError("BASE_PROVIDER_INVALID_STRUCTURE")
        wire = definition.get("wire_api")
        if wire is not None and wire not in {"responses", "chat"}:
            raise ProfileResolutionError("BASE_PROVIDER_WIRE_API_INVALID")
        safe["wire_api"] = wire
        endpoint = definition.get("base_url")
        if endpoint is not None:
            try:
                parsed = urlsplit(endpoint)
                if not parsed.hostname or parsed.scheme not in {"http", "https"}:
                    raise ValueError
                # Auth in URL/header/query is neither emitted nor fingerprinted.
                safe["endpoint"] = [
                    parsed.scheme,
                    parsed.hostname,
                    parsed.port,
                    parsed.path,
                ]
            except ValueError:
                raise ProfileResolutionError("BASE_PROVIDER_ENDPOINT_INVALID") from None
        safe["env_key_configured"] = "env_key" in definition
        for name in ("requires_openai_auth", "supports_websockets"):
            if name in definition:
                if not isinstance(definition[name], bool):
                    raise ProfileResolutionError("BASE_PROVIDER_INVALID_STRUCTURE")
                safe[name] = definition[name]
        return hashlib.sha256(
            json.dumps(safe, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
