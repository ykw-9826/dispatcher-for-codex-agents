"""Run-local contracts for bounded external agent invocations.

These models describe an execution envelope only.  They deliberately do not
extend the project's global Run/Candidate governance schemas.
"""

from __future__ import annotations

import json
import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def validate_local_identifier(value: str, *, field_name: str) -> str:
    """Validate a single path-safe run-local identifier."""
    if not _IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must match {_IDENTIFIER_PATTERN.pattern!r}.")
    return value


class HarnessModel(BaseModel):
    """Strict, immutable base model for the agent harness."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class InvocationStatus(StrEnum):
    """Terminal status of one adapter invocation."""

    SUCCESS = "success"
    FAILURE = "failure"


class SchemaValidationStatus(StrEnum):
    """Local validation status for the agent's final JSON output."""

    PASS = "PASS"
    FAIL = "FAIL"
    NOT_RUN = "NOT_RUN"


class FailureCode(StrEnum):
    """Stable failure taxonomy for one run-local invocation envelope."""

    PROVIDER_RATE_LIMIT_429 = "PROVIDER_RATE_LIMIT_429"
    PROVIDER_PREFILL_PARAMETER_ERROR = "PROVIDER_PREFILL_PARAMETER_ERROR"
    PROVIDER_PARTIAL_PARAMETER_ERROR = "PROVIDER_PARTIAL_PARAMETER_ERROR"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    CLI_NONZERO_EXIT = "CLI_NONZERO_EXIT"
    TURN_COMPLETED_MISSING = "TURN_COMPLETED_MISSING"
    EVENT_STREAM_INVALID = "EVENT_STREAM_INVALID"
    OUTPUT_SCHEMA_INVALID = "OUTPUT_SCHEMA_INVALID"
    POLICY_VIOLATION = "POLICY_VIOLATION"
    SERVED_MODEL_NOT_REPORTED = "SERVED_MODEL_NOT_REPORTED"
    SILENT_FALLBACK_DETECTED = "SILENT_FALLBACK_DETECTED"
    CALL_LIMIT_EXCEEDED = "CALL_LIMIT_EXCEEDED"
    PROFILE_CONFIGURATION_INVALID = "PROFILE_CONFIGURATION_INVALID"
    PAYLOAD_INVALID = "PAYLOAD_INVALID"
    FINAL_OUTPUT_MISSING = "FINAL_OUTPUT_MISSING"


class AgentTask(HarnessModel):
    """Model-agnostic description of one bounded external agent task."""

    task_id: str
    role: str
    prompt_template: str
    approved_input_files: tuple[str, ...]
    selected_columns: tuple[str, ...]
    timeout: float = Field(gt=0, le=3600)
    call_limit: int = Field(ge=1, le=100)
    expected_output_schema: dict[str, Any]

    @field_validator("task_id")
    @classmethod
    def _validate_task_id(cls, value: str) -> str:
        return validate_local_identifier(value, field_name="task_id")

    @field_validator("role", "prompt_template")
    @classmethod
    def _validate_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Text fields must be non-empty.")
        return stripped

    @field_validator("approved_input_files", "selected_columns")
    @classmethod
    def _validate_nonempty_unique_values(
        cls, values: tuple[str, ...]
    ) -> tuple[str, ...]:
        if not values:
            raise ValueError("At least one value is required.")
        stripped = tuple(value.strip() for value in values)
        if any(not value or "\x00" in value for value in stripped):
            raise ValueError("Values must be non-empty and contain no NUL bytes.")
        if len(stripped) != len(set(stripped)):
            raise ValueError("Values must not contain duplicates.")
        return stripped

    @field_validator("expected_output_schema")
    @classmethod
    def _validate_output_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not value:
            raise ValueError("expected_output_schema must be non-empty.")
        return value


class ModelProfile(HarnessModel):
    """Logical profile selection without provider, model, or credentials.

    Provider and model identifiers are resolved at invocation time from the
    effective Codex sidecar profile.
    """

    profile_id: str
    capabilities: dict[str, Any]
    adapter_id: str

    @field_validator("capabilities")
    @classmethod
    def _validate_capabilities(cls, value: dict[str, Any]) -> dict[str, Any]:
        forbidden_keys = {
            "api_key",
            "apikey",
            "access_token",
            "auth_token",
            "secret",
            "password",
            "credential",
            "provider",
            "model",
        }

        def walk(item: Any) -> None:
            if isinstance(item, dict):
                for key, child in item.items():
                    if key.casefold().replace("-", "_") in forbidden_keys:
                        raise ValueError(
                            f"Credential/authority key is forbidden: {key}"
                        )
                    walk(child)
            elif isinstance(item, (list, tuple)):
                for child in item:
                    walk(child)

        walk(value)
        try:
            json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("capabilities must be JSON serializable.") from exc
        return value

    @field_validator("profile_id")
    @classmethod
    def _validate_profile_id(cls, value: str) -> str:
        return validate_local_identifier(value, field_name="profile_id")

    @field_validator("adapter_id")
    @classmethod
    def _validate_adapter_id(cls, value: str) -> str:
        return validate_local_identifier(value, field_name="adapter_id")


class InvocationResult(HarnessModel):
    """Structured terminal record for one external agent invocation."""

    status: InvocationStatus
    exit_code: int | None
    turn_completed: bool
    final_output: Any | None
    schema_validation_status: SchemaValidationStatus
    usage: dict[str, int]
    provenance: dict[str, Any]
    warnings: tuple[str, ...]
    failure_code: FailureCode | None
    latency_seconds: float = Field(ge=0)

    @model_validator(mode="after")
    def _validate_terminal_invariants(self) -> InvocationResult:
        if self.status == InvocationStatus.SUCCESS and self.failure_code is not None:
            raise ValueError("Successful results must not have a failure_code.")
        if self.status == InvocationStatus.FAILURE and self.failure_code is None:
            raise ValueError("Failed results require a failure_code.")
        if self.status == InvocationStatus.SUCCESS:
            if not self.turn_completed:
                raise ValueError("Successful results require turn_completed=true.")
            if self.schema_validation_status != SchemaValidationStatus.PASS:
                raise ValueError("Successful results require schema validation PASS.")
        return self
