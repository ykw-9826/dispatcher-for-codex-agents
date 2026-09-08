"""Small runtime boundary; tasks and results remain runtime invariant."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any, Protocol, runtime_checkable

from .contracts import AgentTask, InvocationResult, ModelProfile
from .payload import PayloadBuilder

DEFAULT_ADAPTER_ID = "codex_cli"
RESERVED_ADAPTER_IDS = frozenset({"zcode_agent", "deepseek_harness", "deepcode_cli"})


@dataclass(frozen=True)
class AdapterSettings:
    executable: str | Sequence[str] = "codex"
    runtime_home: str | Path | None = None
    environment: dict[str, str] | None = None
    cancellation: Event | None = None


@runtime_checkable
class InvocationAdapter(Protocol):
    """Adapters validate configuration/capabilities and normalize terminal events."""

    adapter_id: str

    def preflight(self, *, task: AgentTask, profile: ModelProfile) -> dict[str, Any]:
        """Validate without model calls; return only redacted runtime provenance."""
        ...

    def invoke(
        self,
        *,
        task: AgentTask,
        profile: ModelProfile,
        attempt_id: str,
        workers_root: str | Path,
        payload_builder: PayloadBuilder | None = None,
    ) -> InvocationResult:
        """Write one immutable terminal shard using the shared result contract."""
        ...


class AdapterRegistry:
    """Explicit factories only: no dynamic imports or silent fallback."""

    def __init__(self) -> None:
        self._factories: dict[str, Callable[[AdapterSettings], InvocationAdapter]] = {}

    def register(
        self, adapter_id: str, factory: Callable[[AdapterSettings], InvocationAdapter]
    ) -> None:
        if adapter_id in self._factories or adapter_id in RESERVED_ADAPTER_IDS:
            raise ValueError(f"adapter is already registered or reserved: {adapter_id}")
        self._factories[adapter_id] = factory

    def require(self, adapter_id: str) -> None:
        if adapter_id not in self._factories:
            raise ValueError(f"unsupported adapter_id (NOT_IMPLEMENTED): {adapter_id}")

    def create(self, adapter_id: str, settings: AdapterSettings) -> InvocationAdapter:
        self.require(adapter_id)
        adapter = self._factories[adapter_id](settings)
        if (
            not isinstance(adapter, InvocationAdapter)
            or adapter.adapter_id != adapter_id
        ):
            raise ValueError(
                "Adapter factory does not implement its registered contract"
            )
        return adapter


def default_registry() -> AdapterRegistry:
    from .adapter import CodexCliAdapter

    registry = AdapterRegistry()
    registry.register(
        DEFAULT_ADAPTER_ID,
        lambda settings: CodexCliAdapter(
            executable=settings.executable,
            codex_home=settings.runtime_home,
            environment=settings.environment,
            cancellation=settings.cancellation,
        ),
    )
    return registry
