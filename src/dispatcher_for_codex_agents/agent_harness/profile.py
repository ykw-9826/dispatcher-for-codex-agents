"""Read-only resolution of effective model/provider from Codex sidecars."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


class ProfileResolutionError(ValueError):
    """Raised when a requested sidecar profile cannot be resolved safely."""


@dataclass(frozen=True, slots=True)
class ResolvedSidecarProfile:
    """Non-secret subset of the effective Codex profile."""

    profile_id: str
    configured_model: str
    configured_provider: str
    sidecar_path: Path


class SidecarProfileResolver:
    """Resolve model/provider while deliberately ignoring credential values."""

    def __init__(self, codex_home: str | Path | None = None) -> None:
        configured_home = codex_home or os.environ.get("CODEX_HOME")
        self.codex_home = (
            Path(configured_home).expanduser()
            if configured_home is not None
            else Path.home() / ".codex"
        ).resolve()

    @staticmethod
    def _load_toml(path: Path, *, required: bool) -> dict[str, object]:
        if not path.is_file():
            if required:
                raise ProfileResolutionError(f"Codex config file not found: {path}")
            return {}
        try:
            with path.open("rb") as handle:
                return tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ProfileResolutionError(f"Invalid Codex config file: {path}") from exc

    def resolve(self, profile_id: str) -> ResolvedSidecarProfile:
        """Resolve only model/provider fields from base plus requested sidecar."""
        base_path = self.codex_home / "config.toml"
        sidecar_path = self.codex_home / f"{profile_id}.config.toml"
        base = self._load_toml(base_path, required=False)
        sidecar = self._load_toml(sidecar_path, required=True)

        model = sidecar.get("model", base.get("model"))
        provider = sidecar.get("model_provider", base.get("model_provider"))
        if not isinstance(model, str) or not model.strip():
            raise ProfileResolutionError(
                f"Effective model is not reported by profile {profile_id!r}."
            )
        if not isinstance(provider, str) or not provider.strip():
            raise ProfileResolutionError(
                f"Effective provider is not reported by profile {profile_id!r}."
            )
        return ResolvedSidecarProfile(
            profile_id=profile_id,
            configured_model=model.strip(),
            configured_provider=provider.strip(),
            sidecar_path=sidecar_path,
        )
