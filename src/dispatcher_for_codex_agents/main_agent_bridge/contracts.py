"""Small, strict operational contracts. Model output never authorizes an action."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from dispatcher_for_codex_agents.notifications.core import identifier


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class JobSpec(Strict):
    job_id: str
    attempt_id: str
    kind: Literal["fixture", "batch"]
    plan_root: str | None = None
    delay_seconds: float = 0.0
    fixture_value: int = 1
    max_workers: Literal[1, 2] = 1
    agent_executable: str = "codex"
    agent_home: str | None = None

    @model_validator(mode="after")
    def check(self):
        for value in (self.job_id, self.attempt_id):
            if not identifier(value):
                raise ValueError("JOB_ID_REQUIRED")
        if not math.isfinite(self.delay_seconds) or not 0 <= self.delay_seconds <= 7200:
            raise ValueError("INVALID_FIXTURE_DELAY")
        if (self.kind == "batch") != (self.plan_root is not None):
            raise ValueError("PLAN_REQUIRED_ONLY_FOR_BATCH")
        if self.kind == "batch" and self.delay_seconds:
            raise ValueError("BATCH_DELAY_FORBIDDEN")
        return self


class SupervisorSpec(Strict):
    """User-authored, frozen authorization; no shell commands or arbitrary imports."""

    version: Literal[1] = 1
    task_id: str
    executable: str
    codex_home: str
    model: str
    provider: str
    working_directory: str
    allowed_actions: list[
        Literal["start_approved_jobs", "read_verified_results", "summarize_results"]
    ]
    expires_at: float
    wake_budget: int = Field(default=1, ge=1, le=3)
    main_turn_budget: int = Field(default=2, ge=2, le=4)
    main_turn_timeout: float = Field(default=600.0, gt=0, le=1800)
    jobs: list[JobSpec] = Field(min_length=1, max_length=2)
    notification_config: str
    minimum_idle_seconds: float = Field(default=0.0, ge=0, le=120)

    @field_validator("expires_at", "main_turn_timeout", "minimum_idle_seconds")
    @classmethod
    def finite(cls, value):
        if not math.isfinite(value):
            raise ValueError("NONFINITE_TIME")
        return value

    @model_validator(mode="after")
    def check(self):
        for value in (self.task_id, self.model, self.provider):
            if not identifier(value):
                raise ValueError("IDENTITY_REQUIRED")
        if (
            set(self.allowed_actions)
            != {"start_approved_jobs", "read_verified_results", "summarize_results"}
            or len(self.allowed_actions) != 3
        ):
            raise ValueError("ACTION_ALLOWLIST_INVALID")
        if len({j.job_id for j in self.jobs}) != len(self.jobs):
            raise ValueError("DUPLICATE_JOB")
        roots = [j.plan_root for j in self.jobs if j.plan_root]
        if len(roots) != len(set(roots)):
            raise ValueError("MULTIPLE_WRITERS_FOR_PLAN_FORBIDDEN")
        for value in (
            self.executable,
            self.codex_home,
            self.working_directory,
            self.notification_config,
            *roots,
        ):
            path = Path(value)
            if not path.is_absolute() or path.resolve() != path or path == Path("/"):
                raise ValueError("ABSOLUTE_CANONICAL_PATH_REQUIRED")
        return self


class BridgeBinding(Strict):
    thread_id: str
    task_id: str
    model: str
    provider: str
    working_directory: str
    expires_at: float
    wake_budget: int
    main_turn_budget: int
    config_sha256: str
    executable_sha256: str
    ownership: Literal["EXPLICITLY_CREATED_CLI_CONTROLLER"]

    @model_validator(mode="after")
    def check(self):
        for value in (self.thread_id, self.task_id, self.model, self.provider):
            if not identifier(value) or ":" in value:
                raise ValueError("BINDING_ID_INVALID")
        if not 1 <= self.wake_budget <= 3 or not 2 <= self.main_turn_budget <= 4:
            raise ValueError("BINDING_BUDGET_INVALID")
        return self


class BridgeEvent(Strict):
    """Trusted parent metadata only. Never accept a agent-supplied event."""

    version: Literal[1] = 1
    source: Literal["trusted_parent"] = "trusted_parent"
    kind: Literal[
        "batch_completed",
        "blocked_failure",
        "human_action_required",
        "confirmed_anomaly",
    ]
    task_id: str
    job_id: str
    attempt_id: str
    target_thread: str
    manifest: str
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sequence: int = Field(ge=0)
    occurred_at: float

    @model_validator(mode="after")
    def check(self):
        for name in ("task_id", "job_id", "attempt_id", "target_thread"):
            if not identifier(getattr(self, name)):
                raise ValueError("EVENT_IDENTITY_REQUIRED")
        path = Path(self.manifest)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ValueError("MANIFEST_MUST_BE_RELATIVE")
        if not math.isfinite(self.occurred_at) or self.occurred_at < 0:
            raise ValueError("INVALID_TIME")
        return self
