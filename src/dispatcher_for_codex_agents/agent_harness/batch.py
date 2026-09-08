"""Deterministic batch-local orchestration for agent invocation shards.

The objects in this module are disposable execution-plan/result envelopes.  They
do not define, replace, or migrate any project-wide Run, Candidate, TaskBundle,
or ResultBundle schema.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import os
import stat
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from threading import Event
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from dispatcher_for_codex_agents.agent_harness.contracts import (
    AgentTask,
    FailureCode,
    InvocationResult,
    ModelProfile,
    validate_local_identifier,
)
from dispatcher_for_codex_agents.agent_harness.payload import PayloadBuilder
from dispatcher_for_codex_agents.agent_harness.process_guard import (
    assert_attempt_not_active,
    exclusive_execution,
)
from dispatcher_for_codex_agents.agent_harness.runtime import (
    DEFAULT_ADAPTER_ID,
    AdapterRegistry,
    AdapterSettings,
    default_registry,
)
from dispatcher_for_codex_agents.agent_harness.schema import (
    OutputSchemaError,
    validate_json_schema,
    validate_schema_definition,
)
from dispatcher_for_codex_agents.agent_harness.shard import ImmutableShardWriter

PLAN_CONTRACT = "BATCH_LOCAL_EXECUTION_PLAN_V1"
RETRY_CONTRACT = "BATCH_LOCAL_RETRY_PLAN_V1"
RESULT_CONTRACT = "BATCH_LOCAL_RESULT_ENVELOPE_V1"
BASE_ATTEMPT_ID = "attempt-001"


class BatchError(ValueError):
    """Raised when batch-local inputs or artifacts fail closed."""


class AuthorityClass(StrEnum):
    """Separation class for agent results."""

    AUTHORITATIVE = "authoritative"
    SHADOW = "shadow"
    DIAGNOSTIC = "diagnostic"


class BatchArtifactStatus(StrEnum):
    """Status reconstructed from immutable plan and attempt artifacts."""

    PLANNED = "PLANNED"
    RUNNING_OR_INCOMPLETE = "RUNNING_OR_INCOMPLETE"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    POLICY_VIOLATION = "POLICY_VIOLATION"
    SCHEMA_INVALID = "SCHEMA_INVALID"
    SKIPPED_EXISTING_SUCCESS = "SKIPPED_EXISTING_SUCCESS"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProfileRole(_StrictModel):
    """One profile's batch-local role without provider credentials."""

    profile_id: str
    role: str
    authority_class: AuthorityClass
    adapter_id: str = DEFAULT_ADAPTER_ID
    execution_order: int = Field(ge=1)

    @field_validator("profile_id")
    @classmethod
    def _identifier(cls, value: str, info: Any) -> str:
        return validate_local_identifier(value, field_name=info.field_name)

    @field_validator("adapter_id")
    @classmethod
    def _adapter_id(cls, value: str) -> str:
        return validate_local_identifier(value, field_name="adapter_id")

    @field_validator("role")
    @classmethod
    def _role(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("role must not be empty")
        return stripped


class SourceFingerprint(_StrictModel):
    """Path-redacted source fingerprint stored in a plan snapshot."""

    logical_name: str
    source_name: str
    size: int = Field(ge=0)
    sha256: str

    @field_validator("logical_name")
    @classmethod
    def _logical_name(cls, value: str) -> str:
        return validate_local_identifier(value, field_name="logical_name")

    @field_validator("sha256")
    @classmethod
    def _sha256(cls, value: str) -> str:
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("sha256 must be lowercase hexadecimal")
        return value


class BatchPlanSnapshot(_StrictModel):
    """Immutable configuration snapshot for one batch-local execution plan."""

    contract: str
    batch_id: str
    task_id_prefix: str
    record_id_column: str
    batch_id_column: str
    selected_columns: tuple[str, ...]
    shard_size: int = Field(ge=1)
    record_count: int = Field(ge=1)
    shard_count: int = Field(ge=1)
    base_attempt_id: str
    timeout: float = Field(gt=0, le=3600)
    prompt_template: str
    expected_output_schema: dict[str, Any]
    profiles: tuple[ProfileRole, ...]
    inputs: tuple[SourceFingerprint, ...]

    @field_validator("batch_id", "task_id_prefix", "base_attempt_id")
    @classmethod
    def _identifier(cls, value: str, info: Any) -> str:
        return validate_local_identifier(value, field_name=info.field_name)

    @field_validator("record_id_column", "batch_id_column")
    @classmethod
    def _column(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped or any(char in stripped for char in "\t\r\n\x00"):
            raise ValueError("column names must be non-empty single-line text")
        return stripped

    @field_validator("selected_columns")
    @classmethod
    def _columns(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values or len(values) != len(set(values)):
            raise ValueError("selected_columns must be non-empty and unique")
        if any(
            not item.strip() or any(c in item for c in "\t\r\n\x00") for item in values
        ):
            raise ValueError("selected_columns contains an invalid name")
        return values


class RetryRequestEntry(_StrictModel):
    """Human-authorized request to create one new retry attempt."""

    profile_id: str
    shard_id: str
    parent_attempt_id: str
    new_attempt_id: str
    human_approval_reason: str

    @field_validator("profile_id", "shard_id", "parent_attempt_id", "new_attempt_id")
    @classmethod
    def _identifier(cls, value: str, info: Any) -> str:
        return validate_local_identifier(value, field_name=info.field_name)

    @field_validator("human_approval_reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("human_approval_reason must not be empty")
        return stripped


class RetryRequest(_StrictModel):
    """Input document for an explicit retry-plan command."""

    retry_plan_id: str
    entries: tuple[RetryRequestEntry, ...]

    @field_validator("retry_plan_id")
    @classmethod
    def _identifier(cls, value: str) -> str:
        return validate_local_identifier(value, field_name="retry_plan_id")

    @field_validator("entries")
    @classmethod
    def _entries(
        cls, values: tuple[RetryRequestEntry, ...]
    ) -> tuple[RetryRequestEntry, ...]:
        if not values:
            raise ValueError("retry request must contain at least one entry")
        keys = [(item.profile_id, item.shard_id) for item in values]
        if len(keys) != len(set(keys)):
            raise ValueError("retry request repeats a profile/shard")
        return values


class RetryPlanEntry(_StrictModel):
    """Frozen retry attempt derived from a failed or incomplete parent."""

    profile_id: str
    shard_id: str
    task_id: str
    parent_attempt_id: str
    parent_failure_code: str
    attempt_id: str
    human_approval_reason: str


class RetryPlanSnapshot(_StrictModel):
    """Immutable batch-local retry plan; never generated automatically."""

    contract: str
    retry_plan_id: str
    batch_id: str
    entries: tuple[RetryPlanEntry, ...]


@dataclass(frozen=True, slots=True)
class PlannedShard:
    shard_id: str
    shard_index: int
    record_ids: tuple[str, ...]
    input_relative_path: str


@dataclass(frozen=True, slots=True)
class LoadedPlan:
    root: Path
    plan_directory: Path
    snapshot: BatchPlanSnapshot
    shards: tuple[PlannedShard, ...]

    @property
    def profile_by_id(self) -> dict[str, ProfileRole]:
        return {profile.profile_id: profile for profile in self.snapshot.profiles}


@dataclass(frozen=True, slots=True)
class AttemptSpec:
    profile: ProfileRole
    shard: PlannedShard
    task_id: str
    attempt_id: str
    parent_attempt_id: str | None = None
    parent_failure_code: str | None = None
    human_approval_reason: str | None = None
    retry_plan_id: str | None = None


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_exclusive(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _write_json(path: Path, value: Any) -> None:
    _write_exclusive(path, _json_bytes(value))


def _write_tsv(
    path: Path, fieldnames: Sequence[str], rows: Iterable[Mapping[str, Any]]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=list(fieldnames),
            delimiter="\t",
            lineterminator="\n",
            extrasaction="raise",
        )
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())


def _read_json(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise BatchError(f"Required JSON artifact is unavailable: {path.name}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BatchError(f"Invalid UTF-8 JSON artifact: {path.name}") from exc


def _read_tsv(
    path: Path, required: Sequence[str] | None = None
) -> list[dict[str, str]]:
    if path.is_symlink() or not path.is_file():
        raise BatchError(f"Required TSV artifact is unavailable: {path.name}")
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            if reader.fieldnames is None:
                raise BatchError(f"TSV has no header: {path.name}")
            if required is not None and tuple(reader.fieldnames) != tuple(required):
                raise BatchError(f"TSV header mismatch: {path.name}")
            return [dict(row) for row in reader]
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise BatchError(f"Invalid UTF-8 TSV artifact: {path.name}") from exc


def _fingerprint(path: Path, logical_name: str) -> SourceFingerprint:
    content = path.read_bytes()
    return SourceFingerprint(
        logical_name=logical_name,
        source_name=path.name,
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _relative_files(directory: Path) -> list[Path]:
    return sorted(
        (path for path in directory.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(directory).as_posix(),
    )


def _write_hash_manifest(directory: Path, name: str = "output_sha256.tsv") -> Path:
    manifest = directory / name
    rows = []
    for path in _relative_files(directory):
        if path == manifest:
            continue
        content = path.read_bytes()
        rows.append(
            {
                "path": path.relative_to(directory).as_posix(),
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    _write_tsv(manifest, ("path", "size", "sha256"), rows)
    return manifest


def _seal_directory(directory: Path) -> None:
    for path in _relative_files(directory):
        path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    directories = sorted(
        (path for path in directory.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    mode = (
        stat.S_IRUSR
        | stat.S_IXUSR
        | stat.S_IRGRP
        | stat.S_IXGRP
        | stat.S_IROTH
        | stat.S_IXOTH
    )
    for path in directories:
        path.chmod(mode)
    directory.chmod(mode)


def _verify_hashed_directory(directory: Path, manifest_name: str) -> None:
    if directory.is_symlink() or not directory.is_dir():
        raise BatchError(f"Artifact directory is unavailable: {directory.name}")
    manifest = directory / manifest_name
    rows = _read_tsv(manifest, ("path", "size", "sha256"))
    actual_files = {
        path.relative_to(directory).as_posix()
        for path in _relative_files(directory)
        if path != manifest
    }
    declared = {row["path"] for row in rows}
    if len(rows) != len(declared) or actual_files != declared:
        raise BatchError(f"Artifact manifest coverage mismatch: {manifest_name}")
    for row in rows:
        relative = Path(row["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise BatchError(f"Unsafe path in artifact manifest: {manifest_name}")
        path = directory / relative
        if path.is_symlink() or not path.is_file():
            raise BatchError(f"Manifest target is unavailable: {relative.as_posix()}")
        content = path.read_bytes()
        try:
            expected_size = int(row["size"])
        except ValueError as exc:
            raise BatchError(f"Invalid size in {manifest_name}") from exc
        if expected_size != len(content):
            raise BatchError(f"Artifact size mismatch: {relative.as_posix()}")
        if hashlib.sha256(content).hexdigest() != row["sha256"]:
            raise BatchError(f"Artifact SHA256 mismatch: {relative.as_posix()}")


def _resolve_input_file(raw_path: str | Path, label: str) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_symlink():
        raise BatchError(f"{label} must not be a symlink")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise BatchError(f"{label} is unavailable: {path.name}") from exc
    if not resolved.is_file():
        raise BatchError(f"{label} must be a regular file")
    return resolved


def _load_profile_roles(path: Path) -> tuple[ProfileRole, ...]:
    value = _read_json(path)
    if not isinstance(value, dict) or set(value) != {"profiles"}:
        raise BatchError("Profile-role config must contain only a profiles array")
    raw_profiles = value["profiles"]
    if not isinstance(raw_profiles, list) or not raw_profiles:
        raise BatchError("Profile-role config requires at least one profile")
    try:
        profiles = tuple(ProfileRole.model_validate(item) for item in raw_profiles)
    except ValidationError as exc:
        raise BatchError("Profile-role config validation failed") from exc
    ids = [item.profile_id for item in profiles]
    orders = [item.execution_order for item in profiles]
    if len(ids) != len(set(ids)) or len(orders) != len(set(orders)):
        raise BatchError("Profile ids and execution_order values must be unique")
    return tuple(sorted(profiles, key=lambda item: item.execution_order))


def _load_schema(path: Path) -> dict[str, Any]:
    value = _read_json(path)
    if not isinstance(value, dict):
        raise BatchError("Expected output schema root must be an object")
    try:
        validate_schema_definition(value)
    except OutputSchemaError as exc:
        raise BatchError(str(exc)) from exc
    _result_item_schema(value)
    return value


def _result_item_schema(schema: dict[str, Any]) -> dict[str, Any]:
    try:
        results = schema["properties"]["results"]
        item = results["items"]
        properties = item["properties"]
        record_schema = properties["record_id"]
    except (KeyError, TypeError) as exc:
        raise BatchError(
            "Output schema must define object.properties.results as an array of "
            "objects containing record_id"
        ) from exc
    if (
        schema.get("type") != "object"
        or results.get("type") != "array"
        or item.get("type") != "object"
        or record_schema.get("type") != "string"
        or "record_id" not in item.get("required", [])
        or item.get("additionalProperties") is not False
    ):
        raise BatchError("Output schema does not enforce strict result objects")
    return item


def _specialize_schema(
    schema: dict[str, Any], record_ids: Sequence[str]
) -> dict[str, Any]:
    specialized = copy.deepcopy(schema)
    item = _result_item_schema(specialized)
    item["properties"]["record_id"]["enum"] = list(record_ids)
    results = specialized["properties"]["results"]
    results["minItems"] = len(record_ids)
    results["maxItems"] = len(record_ids)
    validate_schema_definition(specialized)
    return specialized


def _task_id(prefix: str, shard_id: str) -> str:
    return validate_local_identifier(f"{prefix}-{shard_id}", field_name="task_id")


def _task_relative_path(profile_id: str, shard_id: str) -> str:
    return f"tasks/{profile_id}/{shard_id}.json"


def _task_path(plan: LoadedPlan, profile_id: str, shard_id: str) -> Path:
    return plan.plan_directory / _task_relative_path(profile_id, shard_id)


def _load_task(plan: LoadedPlan, profile_id: str, shard_id: str) -> AgentTask:
    path = _task_path(plan, profile_id, shard_id)
    value = _read_json(path)
    if not isinstance(value, dict):
        raise BatchError(f"AgentTask root must be an object: {path.name}")
    raw_inputs = value.get("approved_input_files")
    if isinstance(raw_inputs, list):
        value["approved_input_files"] = [
            (
                str((path.parent / item).resolve(strict=False))
                if isinstance(item, str) and not Path(item).is_absolute()
                else item
            )
            for item in raw_inputs
        ]
    try:
        return AgentTask.model_validate(value)
    except ValidationError as exc:
        raise BatchError(f"AgentTask validation failed: {path.name}") from exc


def plan_batch(
    *,
    source_tsv: str | Path,
    batch_id: str,
    record_id_column: str,
    selected_columns: Sequence[str],
    profile_role_config: str | Path,
    shard_size: int,
    prompt_template: str | Path,
    expected_output_schema: str | Path,
    output_root: str | Path,
    batch_id_column: str = "batch_id",
    membership_tsv: str | Path | None = None,
    timeout: float = 600.0,
    adapter_registry: AdapterRegistry | None = None,
) -> dict[str, Any]:
    """Create a sealed deterministic batch execution plan without model calls."""
    validate_local_identifier(batch_id, field_name="batch_id")
    if shard_size < 1:
        raise BatchError("shard_size must be positive")
    if timeout <= 0 or timeout > 3600:
        raise BatchError("timeout must be greater than zero and at most 3600")
    columns = tuple(selected_columns)
    if not columns or len(columns) != len(set(columns)):
        raise BatchError("selected columns must be non-empty and unique")
    if record_id_column not in columns:
        raise BatchError("selected columns must include the record-id column")

    source = _resolve_input_file(source_tsv, "source TSV")
    profiles_path = _resolve_input_file(profile_role_config, "profile-role config")
    prompt_path = _resolve_input_file(prompt_template, "prompt template")
    schema_path = _resolve_input_file(expected_output_schema, "output schema")
    membership = (
        _resolve_input_file(membership_tsv, "membership TSV")
        if membership_tsv is not None
        else None
    )
    profiles = _load_profile_roles(profiles_path)
    registry = adapter_registry or default_registry()
    for profile in profiles:
        try:
            registry.require(profile.adapter_id)
        except ValueError as exc:
            raise BatchError(f"Profile-role config validation failed: {exc}") from exc
    schema = _load_schema(schema_path)
    try:
        prompt = prompt_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise BatchError("Prompt template is not readable UTF-8 text") from exc
    if not prompt:
        raise BatchError("Prompt template must not be empty")

    source_rows = _read_tsv(source)
    if not source_rows:
        raise BatchError("Source TSV contains no rows")
    source_header = tuple(source_rows[0])
    missing_columns = set(columns) - set(source_header)
    if record_id_column not in source_header or missing_columns:
        raise BatchError("Source TSV is missing required selected columns")
    source_by_id: dict[str, dict[str, str]] = {}
    for row in source_rows:
        record_id = row.get(record_id_column, "").strip()
        if not record_id:
            raise BatchError("Source TSV contains an empty record id")
        if record_id in source_by_id:
            raise BatchError("Source TSV record ids must be unique")
        source_by_id[record_id] = row

    if membership is None:
        if batch_id_column not in source_header:
            raise BatchError(
                "Source TSV lacks the batch-id column; provide a membership TSV"
            )
        member_ids = [
            row[record_id_column]
            for row in source_rows
            if row.get(batch_id_column) == batch_id
        ]
    else:
        membership_rows = _read_tsv(membership)
        if not membership_rows:
            raise BatchError("Membership TSV contains no rows")
        membership_header = set(membership_rows[0])
        if {batch_id_column, record_id_column} - membership_header:
            raise BatchError("Membership TSV lacks batch/record id columns")
        member_ids = [
            row[record_id_column]
            for row in membership_rows
            if row.get(batch_id_column) == batch_id
        ]
    if not member_ids:
        raise BatchError(f"No records found for batch {batch_id!r}")
    if any(not value for value in member_ids) or len(member_ids) != len(
        set(member_ids)
    ):
        raise BatchError("Batch membership record ids must be non-empty and unique")
    missing_source = set(member_ids) - set(source_by_id)
    if missing_source:
        raise BatchError("Batch membership contains record ids absent from source TSV")
    record_ids = tuple(sorted(member_ids))

    output = Path(output_root).expanduser().resolve(strict=False)
    if output.exists():
        raise BatchError("Batch output root already exists; overwrite is forbidden")
    parent = output.parent
    if not parent.is_dir() or not os.access(parent, os.W_OK | os.X_OK):
        raise BatchError("Batch output root has no writable existing parent")
    output.mkdir()
    plan_directory = output / "plan"
    plan_directory.mkdir()

    inputs: list[SourceFingerprint] = [
        _fingerprint(source, "source_tsv"),
        _fingerprint(profiles_path, "profile_role_config"),
        _fingerprint(prompt_path, "prompt_template"),
        _fingerprint(schema_path, "expected_output_schema"),
    ]
    if membership is not None:
        inputs.append(_fingerprint(membership, "membership_tsv"))
    task_prefix = f"batch-{hashlib.sha256(batch_id.encode('utf-8')).hexdigest()[:12]}"
    shard_count = (len(record_ids) + shard_size - 1) // shard_size
    snapshot = BatchPlanSnapshot(
        contract=PLAN_CONTRACT,
        batch_id=batch_id,
        task_id_prefix=task_prefix,
        record_id_column=record_id_column,
        batch_id_column=batch_id_column,
        selected_columns=columns,
        shard_size=shard_size,
        record_count=len(record_ids),
        shard_count=shard_count,
        base_attempt_id=BASE_ATTEMPT_ID,
        timeout=timeout,
        prompt_template=prompt,
        expected_output_schema=schema,
        profiles=profiles,
        inputs=tuple(inputs),
    )
    _write_json(
        plan_directory / "batch_plan.snapshot.json",
        snapshot.model_dump(mode="json"),
    )
    _write_tsv(
        plan_directory / "input_sha256.tsv",
        ("logical_name", "source_name", "size", "sha256"),
        (item.model_dump(mode="json") for item in snapshot.inputs),
    )
    _write_tsv(
        plan_directory / "profile_role_ledger.tsv",
        (
            "profile_id",
            "role",
            "authority_class",
            "adapter_id",
            "execution_order",
        ),
        (profile.model_dump(mode="json") for profile in profiles),
    )

    shard_rows: list[dict[str, Any]] = []
    for shard_index in range(shard_count):
        shard_id = f"shard-{shard_index + 1:04d}"
        ids = record_ids[shard_index * shard_size : (shard_index + 1) * shard_size]
        input_relative = f"inputs/{shard_id}.tsv"
        selected_rows = [
            {column: source_by_id[record_id].get(column, "") for column in columns}
            for record_id in ids
        ]
        _write_tsv(plan_directory / input_relative, columns, selected_rows)
        specialized_schema = _specialize_schema(schema, ids)
        for profile in profiles:
            task = AgentTask(
                task_id=_task_id(task_prefix, shard_id),
                role=profile.role,
                prompt_template=prompt,
                approved_input_files=(f"../../{input_relative}",),
                selected_columns=columns,
                timeout=timeout,
                call_limit=1,
                expected_output_schema=specialized_schema,
            )
            _write_json(
                plan_directory / _task_relative_path(profile.profile_id, shard_id),
                task.model_dump(mode="json"),
            )
        for record_index, record_id in enumerate(ids, start=1):
            shard_rows.append(
                {
                    "batch_id": batch_id,
                    "shard_id": shard_id,
                    "shard_index": shard_index + 1,
                    "record_index": record_index,
                    "record_id": record_id,
                    "input_file": input_relative,
                    "task_id": _task_id(task_prefix, shard_id),
                }
            )
    _write_tsv(
        plan_directory / "shard_plan.tsv",
        (
            "batch_id",
            "shard_id",
            "shard_index",
            "record_index",
            "record_id",
            "input_file",
            "task_id",
        ),
        shard_rows,
    )
    _write_hash_manifest(plan_directory, "plan_sha256.tsv")
    _seal_directory(plan_directory)
    return {
        "record_count": len(record_ids),
        "batch_id": batch_id,
        "contract": PLAN_CONTRACT,
        "model_calls_started": 0,
        "output_root": str(output),
        "profile_count": len(profiles),
        "shard_count": shard_count,
        "status": "PLAN_CREATED",
    }


def load_batch_plan(raw_root: str | Path) -> LoadedPlan:
    """Load and fully verify an immutable batch-local execution plan."""
    root = Path(raw_root).expanduser()
    if root.is_symlink():
        raise BatchError("Batch root must not be a symlink")
    try:
        root = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise BatchError("Batch root is unavailable") from exc
    plan_directory = root / "plan"
    _verify_hashed_directory(plan_directory, "plan_sha256.tsv")
    try:
        snapshot = BatchPlanSnapshot.model_validate(
            _read_json(plan_directory / "batch_plan.snapshot.json")
        )
    except ValidationError as exc:
        raise BatchError("Batch plan snapshot validation failed") from exc
    if snapshot.contract != PLAN_CONTRACT:
        raise BatchError("Unsupported batch plan contract")
    validate_schema_definition(snapshot.expected_output_schema)
    profile_rows = _read_tsv(
        plan_directory / "profile_role_ledger.tsv",
        (
            "profile_id",
            "role",
            "authority_class",
            "adapter_id",
            "execution_order",
        ),
    )
    try:
        ledger_profiles = tuple(
            sorted(
                (ProfileRole.model_validate(row) for row in profile_rows),
                key=lambda item: item.execution_order,
            )
        )
    except ValidationError as exc:
        raise BatchError("Profile-role ledger validation failed") from exc
    if ledger_profiles != snapshot.profiles:
        raise BatchError("Profile-role ledger differs from batch plan snapshot")

    shard_rows = _read_tsv(
        plan_directory / "shard_plan.tsv",
        (
            "batch_id",
            "shard_id",
            "shard_index",
            "record_index",
            "record_id",
            "input_file",
            "task_id",
        ),
    )
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in shard_rows:
        if row["batch_id"] != snapshot.batch_id:
            raise BatchError("Shard plan contains a cross-batch row")
        grouped[row["shard_id"]].append(row)
    if len(grouped) != snapshot.shard_count:
        raise BatchError("Shard count differs from snapshot")
    planned_shards: list[PlannedShard] = []
    all_ids: list[str] = []
    for expected_index, shard_id in enumerate(sorted(grouped), start=1):
        rows = sorted(grouped[shard_id], key=lambda row: int(row["record_index"]))
        if any(int(row["shard_index"]) != expected_index for row in rows):
            raise BatchError("Shard indices are not stable and contiguous")
        if [int(row["record_index"]) for row in rows] != list(range(1, len(rows) + 1)):
            raise BatchError("Record indices are not stable and contiguous")
        record_ids = tuple(row["record_id"] for row in rows)
        if tuple(sorted(record_ids)) != record_ids:
            raise BatchError("Record ids are not sorted within a shard")
        if len(record_ids) > snapshot.shard_size:
            raise BatchError("A shard exceeds the frozen shard size")
        expected_input = f"inputs/{shard_id}.tsv"
        expected_task = _task_id(snapshot.task_id_prefix, shard_id)
        if any(
            row["input_file"] != expected_input or row["task_id"] != expected_task
            for row in rows
        ):
            raise BatchError("Shard plan contains inconsistent file/task mappings")
        input_rows = _read_tsv(
            plan_directory / expected_input, snapshot.selected_columns
        )
        input_ids = tuple(row[snapshot.record_id_column] for row in input_rows)
        if input_ids != record_ids:
            raise BatchError("Shard input membership differs from shard plan")
        planned_shards.append(
            PlannedShard(
                shard_id=shard_id,
                shard_index=expected_index,
                record_ids=record_ids,
                input_relative_path=expected_input,
            )
        )
        all_ids.extend(record_ids)
    if (
        len(all_ids) != snapshot.record_count
        or len(all_ids) != len(set(all_ids))
        or tuple(sorted(all_ids)) != tuple(all_ids)
    ):
        raise BatchError("Global record membership is missing, duplicated, or unsorted")

    plan = LoadedPlan(
        root=root,
        plan_directory=plan_directory,
        snapshot=snapshot,
        shards=tuple(planned_shards),
    )
    expected_tasks = {
        _task_relative_path(profile.profile_id, shard.shard_id)
        for profile in snapshot.profiles
        for shard in plan.shards
    }
    actual_tasks = {
        path.relative_to(plan_directory).as_posix()
        for path in (plan_directory / "tasks").rglob("*.json")
    }
    if expected_tasks != actual_tasks:
        raise BatchError("AgentTask snapshot coverage differs from plan")
    for profile in snapshot.profiles:
        for shard in plan.shards:
            task = _load_task(plan, profile.profile_id, shard.shard_id)
            if (
                task.task_id != _task_id(snapshot.task_id_prefix, shard.shard_id)
                or task.role != profile.role
                or task.selected_columns != snapshot.selected_columns
                or task.call_limit != 1
            ):
                raise BatchError("AgentTask snapshot differs from batch plan")
            expected_schema = _specialize_schema(
                snapshot.expected_output_schema, shard.record_ids
            )
            if task.expected_output_schema != expected_schema:
                raise BatchError(
                    "AgentTask output schema differs from shard membership"
                )
            expected_input = (plan_directory / shard.input_relative_path).resolve()
            if tuple(task.approved_input_files) != (str(expected_input),):
                raise BatchError("AgentTask approved input differs from shard input")
    return plan


def _base_attempt_specs(plan: LoadedPlan) -> tuple[AttemptSpec, ...]:
    return tuple(
        AttemptSpec(
            profile=profile,
            shard=shard,
            task_id=_task_id(plan.snapshot.task_id_prefix, shard.shard_id),
            attempt_id=plan.snapshot.base_attempt_id,
        )
        for profile in plan.snapshot.profiles
        for shard in plan.shards
    )


def _load_retry_plan_directory(directory: Path) -> RetryPlanSnapshot:
    _verify_hashed_directory(directory, "output_sha256.tsv")
    try:
        snapshot = RetryPlanSnapshot.model_validate(
            _read_json(directory / "retry_plan.snapshot.json")
        )
    except ValidationError as exc:
        raise BatchError("Retry plan snapshot validation failed") from exc
    if snapshot.contract != RETRY_CONTRACT or snapshot.retry_plan_id != directory.name:
        raise BatchError("Retry plan identity or contract mismatch")
    rows = _read_tsv(
        directory / "retry_shards.tsv",
        (
            "profile_id",
            "shard_id",
            "task_id",
            "parent_attempt_id",
            "parent_failure_code",
            "attempt_id",
            "human_approval_reason",
        ),
    )
    if [entry.model_dump(mode="json") for entry in snapshot.entries] != rows:
        raise BatchError("Retry shard ledger differs from retry plan snapshot")
    return snapshot


def _retry_snapshots(plan: LoadedPlan) -> tuple[RetryPlanSnapshot, ...]:
    root = plan.root / "retry_plans"
    if not root.exists():
        return ()
    if root.is_symlink() or not root.is_dir():
        raise BatchError("retry_plans must be a real directory")
    return tuple(
        _load_retry_plan_directory(path)
        for path in sorted(root.iterdir(), key=lambda item: item.name)
        if path.is_dir()
    )


def _all_attempt_specs(plan: LoadedPlan) -> tuple[AttemptSpec, ...]:
    profiles = plan.profile_by_id
    shards = {shard.shard_id: shard for shard in plan.shards}
    specs = list(_base_attempt_specs(plan))
    seen = {
        (spec.profile.profile_id, spec.shard.shard_id, spec.attempt_id)
        for spec in specs
    }
    for retry in _retry_snapshots(plan):
        if retry.batch_id != plan.snapshot.batch_id:
            raise BatchError("Retry plan references a different batch")
        for entry in retry.entries:
            if entry.profile_id not in profiles or entry.shard_id not in shards:
                raise BatchError("Retry plan references an unknown profile or shard")
            expected_task = _task_id(plan.snapshot.task_id_prefix, entry.shard_id)
            if entry.task_id != expected_task:
                raise BatchError("Retry plan task_id mismatch")
            key = (entry.profile_id, entry.shard_id, entry.attempt_id)
            if key in seen:
                raise BatchError("Retry attempt id is not unique for profile/shard")
            seen.add(key)
            specs.append(
                AttemptSpec(
                    profile=profiles[entry.profile_id],
                    shard=shards[entry.shard_id],
                    task_id=entry.task_id,
                    attempt_id=entry.attempt_id,
                    parent_attempt_id=entry.parent_attempt_id,
                    parent_failure_code=entry.parent_failure_code,
                    human_approval_reason=entry.human_approval_reason,
                    retry_plan_id=retry.retry_plan_id,
                )
            )
    return tuple(specs)


def _attempt_path(plan: LoadedPlan, spec: AttemptSpec) -> Path:
    return (
        plan.root / "workers" / spec.task_id / spec.profile.profile_id / spec.attempt_id
    )


def _verify_invocation_shard(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise BatchError("Invocation shard is not a real directory")
    names = {item.name for item in path.iterdir()}
    expected = set(ImmutableShardWriter.REQUIRED_FILES)
    if names != expected:
        raise BatchError("Invocation shard file set is incomplete or unexpected")
    rows = _read_tsv(path / "output_sha256.tsv", ("path", "size", "sha256"))
    if {row["path"] for row in rows} != expected - {"output_sha256.tsv"}:
        raise BatchError("Invocation output hash manifest coverage mismatch")
    for row in rows:
        artifact = path / row["path"]
        content = artifact.read_bytes()
        if len(content) != int(row["size"]):
            raise BatchError("Invocation artifact size mismatch")
        if hashlib.sha256(content).hexdigest() != row["sha256"]:
            raise BatchError("Invocation artifact SHA256 mismatch")


def _invocation_result(path: Path) -> InvocationResult:
    try:
        return InvocationResult.model_validate(
            _read_json(path / "invocation_result.json")
        )
    except ValidationError as exc:
        raise BatchError("InvocationResult validation failed") from exc


def _classify_attempt(plan: LoadedPlan, spec: AttemptSpec) -> BatchArtifactStatus:
    path = _attempt_path(plan, spec)
    if not path.exists():
        return BatchArtifactStatus.PLANNED
    if path.is_symlink() or not path.is_dir():
        return BatchArtifactStatus.RUNNING_OR_INCOMPLETE
    if {item.name for item in path.iterdir()} != set(
        ImmutableShardWriter.REQUIRED_FILES
    ):
        return BatchArtifactStatus.RUNNING_OR_INCOMPLETE
    try:
        _verify_invocation_shard(path)
        result = _invocation_result(path)
    except BatchError:
        return BatchArtifactStatus.RUNNING_OR_INCOMPLETE
    if result.status == "success" and result.schema_validation_status == "PASS":
        return BatchArtifactStatus.SUCCESS
    if result.failure_code in {
        FailureCode.POLICY_VIOLATION,
        FailureCode.SILENT_FALLBACK_DETECTED,
        FailureCode.CALL_LIMIT_EXCEEDED,
    }:
        return BatchArtifactStatus.POLICY_VIOLATION
    if result.failure_code == FailureCode.OUTPUT_SCHEMA_INVALID:
        return BatchArtifactStatus.SCHEMA_INVALID
    return BatchArtifactStatus.FAILED


@exclusive_execution
def create_retry_plan(
    *, plan_root: str | Path, retry_request: str | Path
) -> dict[str, Any]:
    """Freeze an explicit human-reasoned retry plan; never infer one."""
    plan = load_batch_plan(plan_root)
    request_path = _resolve_input_file(retry_request, "retry request")
    try:
        request = RetryRequest.model_validate(_read_json(request_path))
    except ValidationError as exc:
        raise BatchError("Retry request validation failed") from exc
    destination = plan.root / "retry_plans" / request.retry_plan_id
    if destination.exists():
        raise BatchError("Retry plan already exists; overwrite is forbidden")
    all_specs = list(_all_attempt_specs(plan))
    by_key = {
        (spec.profile.profile_id, spec.shard.shard_id, spec.attempt_id): spec
        for spec in all_specs
    }
    output_entries: list[RetryPlanEntry] = []
    for entry in request.entries:
        parent_key = (entry.profile_id, entry.shard_id, entry.parent_attempt_id)
        parent = by_key.get(parent_key)
        if parent is None:
            raise BatchError("Retry parent attempt is not present in existing plans")
        siblings = [
            spec
            for spec in all_specs
            if spec.profile.profile_id == entry.profile_id
            and spec.shard.shard_id == entry.shard_id
        ]
        if any(
            _classify_attempt(plan, spec) == BatchArtifactStatus.SUCCESS
            for spec in siblings
        ):
            raise BatchError("Successful profile/shard attempts cannot be retried")
        latest = siblings[-1]
        if latest.attempt_id != entry.parent_attempt_id:
            raise BatchError("Retry parent must be the latest planned attempt")
        parent_status = _classify_attempt(plan, parent)
        assert_attempt_not_active(
            plan.root,
            parent.task_id,
            entry.profile_id,
            parent.attempt_id,
            incomplete=parent_status == BatchArtifactStatus.RUNNING_OR_INCOMPLETE,
        )
        if parent_status not in {
            BatchArtifactStatus.FAILED,
            BatchArtifactStatus.POLICY_VIOLATION,
            BatchArtifactStatus.SCHEMA_INVALID,
            BatchArtifactStatus.RUNNING_OR_INCOMPLETE,
        }:
            raise BatchError("Retry parent is not failed or incomplete")
        if entry.new_attempt_id == entry.parent_attempt_id:
            raise BatchError("Retry attempt_id must differ from parent attempt_id")
        if any(spec.attempt_id == entry.new_attempt_id for spec in siblings):
            raise BatchError("Retry attempt_id already exists for profile/shard")
        parent_failure = "INCOMPLETE_ATTEMPT"
        if parent_status != BatchArtifactStatus.RUNNING_OR_INCOMPLETE:
            result = _invocation_result(_attempt_path(plan, parent))
            parent_failure = (
                result.failure_code.value
                if result.failure_code is not None
                else parent_status.value
            )
        output_entries.append(
            RetryPlanEntry(
                profile_id=entry.profile_id,
                shard_id=entry.shard_id,
                task_id=parent.task_id,
                parent_attempt_id=entry.parent_attempt_id,
                parent_failure_code=parent_failure,
                attempt_id=entry.new_attempt_id,
                human_approval_reason=entry.human_approval_reason,
            )
        )
    snapshot = RetryPlanSnapshot(
        contract=RETRY_CONTRACT,
        retry_plan_id=request.retry_plan_id,
        batch_id=plan.snapshot.batch_id,
        entries=tuple(output_entries),
    )
    destination.mkdir(parents=True)
    _write_json(
        destination / "retry_plan.snapshot.json", snapshot.model_dump(mode="json")
    )
    _write_tsv(
        destination / "retry_shards.tsv",
        (
            "profile_id",
            "shard_id",
            "task_id",
            "parent_attempt_id",
            "parent_failure_code",
            "attempt_id",
            "human_approval_reason",
        ),
        (entry.model_dump(mode="json") for entry in snapshot.entries),
    )
    _write_hash_manifest(destination)
    _seal_directory(destination)
    return {
        "entry_count": len(output_entries),
        "model_calls_started": 0,
        "retry_plan_id": request.retry_plan_id,
        "status": "RETRY_PLAN_CREATED",
    }


def _specs_for_retry(
    plan: LoadedPlan, retry_plan: str | Path
) -> tuple[AttemptSpec, ...]:
    raw = Path(retry_plan)
    destination = (
        raw
        if raw.is_absolute() or len(raw.parts) > 1
        else plan.root / "retry_plans" / raw
    )
    destination = destination.resolve(strict=True)
    if plan.root not in destination.parents:
        raise BatchError("Retry plan is outside the batch root")
    retry = _load_retry_plan_directory(destination)
    return tuple(
        spec
        for spec in _all_attempt_specs(plan)
        if spec.retry_plan_id == retry.retry_plan_id
    )


def _validate_run_spec(plan: LoadedPlan, spec: AttemptSpec) -> AgentTask:
    if spec.parent_attempt_id is not None:
        siblings = [
            item
            for item in _all_attempt_specs(plan)
            if item.profile.profile_id == spec.profile.profile_id
            and item.shard.shard_id == spec.shard.shard_id
        ]
        parent = next(
            (item for item in siblings if item.attempt_id == spec.parent_attempt_id),
            None,
        )
        if parent is None or _classify_attempt(plan, parent) not in {
            BatchArtifactStatus.FAILED,
            BatchArtifactStatus.POLICY_VIOLATION,
            BatchArtifactStatus.SCHEMA_INVALID,
            BatchArtifactStatus.RUNNING_OR_INCOMPLETE,
        }:
            raise BatchError("Retry parent no longer has a retry-eligible status")
        assert_attempt_not_active(
            plan.root,
            parent.task_id,
            parent.profile.profile_id,
            parent.attempt_id,
            incomplete=_classify_attempt(plan, parent)
            == BatchArtifactStatus.RUNNING_OR_INCOMPLETE,
        )
        if any(
            _classify_attempt(plan, item) == BatchArtifactStatus.SUCCESS
            for item in siblings
        ):
            raise BatchError(
                "Retry is blocked because the profile/shard already succeeded"
            )
    task = _load_task(plan, spec.profile.profile_id, spec.shard.shard_id)
    PayloadBuilder(allowed_roots=(plan.plan_directory / "inputs",)).build(task)
    return task


def _skip_attempts(plan: LoadedPlan) -> set[tuple[str, str, str]]:
    skipped: set[tuple[str, str, str]] = set()
    root = plan.root / "batch_runs"
    if not root.exists():
        return skipped
    for directory in sorted(root.iterdir(), key=lambda item: item.name):
        if not directory.is_dir():
            continue
        try:
            _verify_hashed_directory(directory, "output_sha256.tsv")
            rows = _read_tsv(directory / "batch_run_ledger.tsv")
        except BatchError:
            continue
        for row in rows:
            if row.get("action") == BatchArtifactStatus.SKIPPED_EXISTING_SUCCESS.value:
                skipped.add((row["profile_id"], row["shard_id"], row["attempt_id"]))
    return skipped


@exclusive_execution
def run_batch(
    *,
    plan_root: str | Path,
    run_id: str,
    max_workers: int = 1,
    retry_plan: str | Path | None = None,
    dry_run: bool = False,
    codex_home: str | Path | None = None,
    executable: str | Sequence[str] = "codex",
    environment: dict[str, str] | None = None,
    adapter_registry: AdapterRegistry | None = None,
    cancellation: Event | None = None,
) -> dict[str, Any]:
    """Execute only never-started attempts, preserving every terminal shard."""
    validate_local_identifier(run_id, field_name="run_id")
    if max_workers not in {1, 2}:
        raise BatchError("max_workers must be 1 or 2")
    plan = load_batch_plan(plan_root)
    specs = (
        _specs_for_retry(plan, retry_plan)
        if retry_plan is not None
        else _base_attempt_specs(plan)
    )
    if not specs:
        raise BatchError("Selected execution plan contains no attempts")
    run_directory = plan.root / "batch_runs" / run_id
    if run_directory.exists():
        raise BatchError("Batch run id already exists; overwrite is forbidden")

    registry = adapter_registry or default_registry()
    cancellation = cancellation or Event()
    settings = AdapterSettings(executable, codex_home, environment, cancellation)
    initial = {spec: _classify_attempt(plan, spec) for spec in specs}
    runnable = [spec for spec in specs if initial[spec] == BatchArtifactStatus.PLANNED]
    adapters = {}
    resolved_profiles = {}
    for spec in runnable:
        task = _validate_run_spec(plan, spec)
        profile_id = spec.profile.profile_id
        if profile_id not in adapters:
            adapters[profile_id] = registry.create(spec.profile.adapter_id, settings)
        resolved_profiles[profile_id] = adapters[profile_id].preflight(
            task=task,
            profile=ModelProfile(
                profile_id=profile_id,
                capabilities={"native_output_schema": False},
                adapter_id=spec.profile.adapter_id,
            ),
        )
    if dry_run:
        return {
            "blocked_existing_attempts": sum(
                status not in {BatchArtifactStatus.PLANNED, BatchArtifactStatus.SUCCESS}
                for status in initial.values()
            ),
            "dry_run": True,
            "max_workers": max_workers,
            "model_calls_started": 0,
            "planned_invocations": len(specs),
            "resolved_profiles": resolved_profiles,
            "retry_plan": (
                str(retry_plan) if retry_plan is not None else "NOT_APPLICABLE"
            ),
            "scheduled_invocations": len(runnable),
            "skipped_existing_success": sum(
                status == BatchArtifactStatus.SUCCESS for status in initial.values()
            ),
            "status": "DRY_RUN_VALIDATED",
        }

    profile_groups: dict[str, list[AttemptSpec]] = defaultdict(list)
    for spec in runnable:
        profile_groups[spec.profile.profile_id].append(spec)
    for values in profile_groups.values():
        values.sort(key=lambda item: item.shard.shard_id)

    def execute_profile(profile_id: str) -> list[dict[str, Any]]:
        adapter = adapters[profile_id]
        rows = []
        for spec in profile_groups[profile_id]:
            if cancellation.is_set():
                break
            task = _load_task(plan, spec.profile.profile_id, spec.shard.shard_id)
            model_profile = ModelProfile(
                profile_id=spec.profile.profile_id,
                capabilities={"native_output_schema": False},
                adapter_id=spec.profile.adapter_id,
            )
            result = adapter.invoke(
                task=task,
                profile=model_profile,
                attempt_id=spec.attempt_id,
                workers_root=plan.root,
                payload_builder=PayloadBuilder(
                    allowed_roots=(plan.plan_directory / "inputs",)
                ),
            )
            rows.append(
                {
                    "profile_id": spec.profile.profile_id,
                    "role": spec.profile.role,
                    "authority_class": spec.profile.authority_class.value,
                    "shard_id": spec.shard.shard_id,
                    "task_id": spec.task_id,
                    "attempt_id": spec.attempt_id,
                    "prior_status": BatchArtifactStatus.PLANNED.value,
                    "action": "EXECUTED",
                    "terminal_status": _classify_attempt(plan, spec).value,
                    "exit_code": (
                        result.exit_code
                        if result.exit_code is not None
                        else "NOT_REPORTED"
                    ),
                    "failure_code": (
                        result.failure_code.value
                        if result.failure_code is not None
                        else "NOT_APPLICABLE"
                    ),
                    "schema_validation_status": result.schema_validation_status.value,
                    "retry_plan_id": spec.retry_plan_id or "NOT_APPLICABLE",
                }
            )
        return rows

    executed_rows: list[dict[str, Any]] = []
    ordered_profiles = sorted(
        profile_groups,
        key=lambda value: plan.profile_by_id[value].execution_order,
    )
    if max_workers == 1:
        for profile_id in ordered_profiles:
            executed_rows.extend(execute_profile(profile_id))
    else:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {
                profile_id: pool.submit(execute_profile, profile_id)
                for profile_id in ordered_profiles
            }
            for profile_id in ordered_profiles:
                executed_rows.extend(futures[profile_id].result())

    rows_by_key = {
        (row["profile_id"], row["shard_id"], row["attempt_id"]): row
        for row in executed_rows
    }
    ledger_rows: list[dict[str, Any]] = []
    for spec in specs:
        key = (spec.profile.profile_id, spec.shard.shard_id, spec.attempt_id)
        if key in rows_by_key:
            ledger_rows.append(rows_by_key[key])
            continue
        prior = initial[spec]
        if prior == BatchArtifactStatus.SUCCESS:
            action = BatchArtifactStatus.SKIPPED_EXISTING_SUCCESS.value
        elif prior == BatchArtifactStatus.RUNNING_OR_INCOMPLETE:
            action = "INCOMPLETE_ATTEMPT"
        elif prior == BatchArtifactStatus.PLANNED:
            action = "CANCELLED_BEFORE_START"
        else:
            action = "BLOCKED_EXISTING_FAILURE"
        result: InvocationResult | None = None
        if prior not in {
            BatchArtifactStatus.PLANNED,
            BatchArtifactStatus.RUNNING_OR_INCOMPLETE,
        }:
            result = _invocation_result(_attempt_path(plan, spec))
        ledger_rows.append(
            {
                "profile_id": spec.profile.profile_id,
                "role": spec.profile.role,
                "authority_class": spec.profile.authority_class.value,
                "shard_id": spec.shard.shard_id,
                "task_id": spec.task_id,
                "attempt_id": spec.attempt_id,
                "prior_status": prior.value,
                "action": action,
                "terminal_status": prior.value,
                "exit_code": (
                    result.exit_code
                    if result is not None and result.exit_code is not None
                    else "NOT_REPORTED"
                ),
                "failure_code": (
                    result.failure_code.value
                    if result is not None and result.failure_code is not None
                    else (
                        "INCOMPLETE_ATTEMPT"
                        if prior == BatchArtifactStatus.RUNNING_OR_INCOMPLETE
                        else "NOT_APPLICABLE"
                    )
                ),
                "schema_validation_status": (
                    result.schema_validation_status.value
                    if result is not None
                    else "NOT_RUN"
                ),
                "retry_plan_id": spec.retry_plan_id or "NOT_APPLICABLE",
            }
        )
    ledger_rows.sort(
        key=lambda row: (
            plan.profile_by_id[row["profile_id"]].execution_order,
            row["shard_id"],
            row["attempt_id"],
        )
    )
    final_statuses = Counter(row["terminal_status"] for row in ledger_rows)
    blocked_actions = {
        "BLOCKED_EXISTING_FAILURE",
        "INCOMPLETE_ATTEMPT",
        "CANCELLED_BEFORE_START",
    }
    batch_status = (
        "PASS"
        if all(
            row["terminal_status"]
            in {
                BatchArtifactStatus.SUCCESS.value,
                BatchArtifactStatus.SKIPPED_EXISTING_SUCCESS.value,
            }
            and row["action"] not in blocked_actions
            for row in ledger_rows
        )
        else "PARTIAL_OR_BLOCKED"
    )
    run_directory.mkdir(parents=True)
    ledger_fields = (
        "profile_id",
        "role",
        "authority_class",
        "shard_id",
        "task_id",
        "attempt_id",
        "prior_status",
        "action",
        "terminal_status",
        "exit_code",
        "failure_code",
        "schema_validation_status",
        "retry_plan_id",
    )
    _write_tsv(run_directory / "batch_run_ledger.tsv", ledger_fields, ledger_rows)
    summary = {
        "attempt_count": len(specs),
        "contract": RESULT_CONTRACT,
        "executed_count": len(executed_rows),
        "max_workers": max_workers,
        "retry_plan": str(retry_plan) if retry_plan is not None else "NOT_APPLICABLE",
        "run_id": run_id,
        "skipped_existing_success": sum(
            row["action"] == BatchArtifactStatus.SKIPPED_EXISTING_SUCCESS.value
            for row in ledger_rows
        ),
        "status": batch_status,
        "terminal_status_counts": dict(sorted(final_statuses.items())),
    }
    _write_json(run_directory / "batch_run_summary.json", summary)
    _write_hash_manifest(run_directory)
    _seal_directory(run_directory)
    return summary


def status_batch(*, plan_root: str | Path, snapshot_id: str) -> dict[str, Any]:
    """Rebuild status solely from immutable plans, attempts, and run ledgers."""
    validate_local_identifier(snapshot_id, field_name="snapshot_id")
    plan = load_batch_plan(plan_root)
    destination = plan.root / "status" / snapshot_id
    if destination.exists():
        raise BatchError("Status snapshot already exists; overwrite is forbidden")
    skipped = _skip_attempts(plan)
    rows = []
    for spec in _all_attempt_specs(plan):
        status = _classify_attempt(plan, spec)
        key = (spec.profile.profile_id, spec.shard.shard_id, spec.attempt_id)
        display = (
            BatchArtifactStatus.SKIPPED_EXISTING_SUCCESS
            if status == BatchArtifactStatus.SUCCESS and key in skipped
            else status
        )
        rows.append(
            {
                "profile_id": spec.profile.profile_id,
                "role": spec.profile.role,
                "authority_class": spec.profile.authority_class.value,
                "shard_id": spec.shard.shard_id,
                "task_id": spec.task_id,
                "attempt_id": spec.attempt_id,
                "parent_attempt_id": spec.parent_attempt_id or "NOT_APPLICABLE",
                "retry_plan_id": spec.retry_plan_id or "NOT_APPLICABLE",
                "status": display.value,
            }
        )
    rows.sort(
        key=lambda row: (
            plan.profile_by_id[row["profile_id"]].execution_order,
            row["shard_id"],
            row["attempt_id"],
        )
    )
    counts = Counter(row["status"] for row in rows)
    summary = {
        "attempt_count": len(rows),
        "batch_id": plan.snapshot.batch_id,
        "contract": RESULT_CONTRACT,
        "snapshot_id": snapshot_id,
        "status_counts": dict(sorted(counts.items())),
    }
    destination.mkdir(parents=True)
    _write_tsv(
        destination / "batch_status.tsv",
        (
            "profile_id",
            "role",
            "authority_class",
            "shard_id",
            "task_id",
            "attempt_id",
            "parent_attempt_id",
            "retry_plan_id",
            "status",
        ),
        rows,
    )
    _write_json(destination / "batch_status_summary.json", summary)
    _write_hash_manifest(destination)
    _seal_directory(destination)
    return summary


def _successful_attempts_by_shard(
    plan: LoadedPlan,
) -> dict[tuple[str, str], list[AttemptSpec]]:
    grouped: dict[tuple[str, str], list[AttemptSpec]] = defaultdict(list)
    for spec in _all_attempt_specs(plan):
        if _classify_attempt(plan, spec) == BatchArtifactStatus.SUCCESS:
            grouped[(spec.profile.profile_id, spec.shard.shard_id)].append(spec)
    return grouped


def collect_batch(*, plan_root: str | Path, collection_id: str) -> dict[str, Any]:
    """Collect only successful schema-valid shards and fail closed on coverage."""
    validate_local_identifier(collection_id, field_name="collection_id")
    plan = load_batch_plan(plan_root)
    destination = plan.root / "collections" / collection_id
    if destination.exists():
        raise BatchError("Collection id already exists; overwrite is forbidden")
    successful = _successful_attempts_by_shard(plan)
    all_specs = _all_attempt_specs(plan)
    long_rows: list[dict[str, Any]] = []
    profile_rows: list[dict[str, Any]] = []
    coverage_rows: list[dict[str, Any]] = []
    completion_rows: list[dict[str, Any]] = []
    failed_rows: list[dict[str, Any]] = []
    global_planned_ids = {
        record_id for shard in plan.shards for record_id in shard.record_ids
    }
    collected_by_profile: dict[str, list[str]] = defaultdict(list)
    profile_errors: dict[str, list[str]] = defaultdict(list)

    for profile in plan.snapshot.profiles:
        successful_shards = 0
        for shard in plan.shards:
            key = (profile.profile_id, shard.shard_id)
            candidates = successful.get(key, [])
            if len(candidates) != 1:
                profile_errors[profile.profile_id].append(
                    f"{shard.shard_id}: expected one successful attempt, "
                    f"found {len(candidates)}"
                )
                continue
            spec = candidates[0]
            attempt_path = _attempt_path(plan, spec)
            _verify_invocation_shard(attempt_path)
            task = _load_task(plan, profile.profile_id, shard.shard_id)
            output = _read_json(attempt_path / "final_output.json")
            try:
                validate_json_schema(output, task.expected_output_schema)
            except OutputSchemaError as exc:
                profile_errors[profile.profile_id].append(f"{shard.shard_id}: {exc}")
                continue
            results = output.get("results") if isinstance(output, dict) else None
            if not isinstance(results, list):
                profile_errors[profile.profile_id].append(
                    f"{shard.shard_id}: results is not an array"
                )
                continue
            observed = [
                row.get("record_id")
                for row in results
                if isinstance(row, dict) and isinstance(row.get("record_id"), str)
            ]
            counts = Counter(observed)
            missing = sorted(set(shard.record_ids) - set(observed))
            extra = sorted(set(observed) - set(shard.record_ids))
            duplicate = sorted(key for key, count in counts.items() if count != 1)
            if len(observed) != len(shard.record_ids) or missing or extra or duplicate:
                profile_errors[profile.profile_id].append(
                    f"{shard.shard_id}: missing={missing}, extra={extra}, "
                    f"duplicate={duplicate}"
                )
                continue
            successful_shards += 1
            for record in sorted(results, key=lambda row: row["record_id"]):
                record_id = record["record_id"]
                collected_by_profile[profile.profile_id].append(record_id)
                result_json = _canonical_json(record)
                profile_rows.append(
                    {
                        "batch_id": plan.snapshot.batch_id,
                        "profile_id": profile.profile_id,
                        "role": profile.role,
                        "authority_class": profile.authority_class.value,
                        "shard_id": shard.shard_id,
                        "attempt_id": spec.attempt_id,
                        "record_id": record_id,
                        "result_json": result_json,
                    }
                )
                for field_name in sorted(set(record) - {"record_id"}):
                    long_rows.append(
                        {
                            "batch_id": plan.snapshot.batch_id,
                            "profile_id": profile.profile_id,
                            "role": profile.role,
                            "authority_class": profile.authority_class.value,
                            "shard_id": shard.shard_id,
                            "attempt_id": spec.attempt_id,
                            "record_id": record_id,
                            "field_name": field_name,
                            "value_json": _canonical_json(record[field_name]),
                        }
                    )
        observed = collected_by_profile[profile.profile_id]
        counts = Counter(observed)
        missing = sorted(global_planned_ids - set(observed))
        extra = sorted(set(observed) - global_planned_ids)
        duplicate = sorted(key for key, count in counts.items() if count != 1)
        errors = profile_errors[profile.profile_id]
        coverage_status = (
            "PASS"
            if len(observed) == len(global_planned_ids)
            and not missing
            and not extra
            and not duplicate
            and not errors
            else "FAIL"
        )
        coverage_rows.append(
            {
                "profile_id": profile.profile_id,
                "role": profile.role,
                "authority_class": profile.authority_class.value,
                "planned_count": len(global_planned_ids),
                "observed_count": len(observed),
                "unique_count": len(set(observed)),
                "missing_ids": ";".join(missing) or "NOT_APPLICABLE",
                "extra_or_cross_batch_ids": ";".join(extra) or "NOT_APPLICABLE",
                "duplicate_ids": ";".join(duplicate) or "NOT_APPLICABLE",
                "validation_errors": " | ".join(errors) or "NOT_APPLICABLE",
                "coverage_status": coverage_status,
            }
        )
        completion_rows.append(
            {
                "profile_id": profile.profile_id,
                "role": profile.role,
                "authority_class": profile.authority_class.value,
                "planned_shards": len(plan.shards),
                "successful_shards": successful_shards,
                "collected_records": len(observed),
                "coverage_status": coverage_status,
            }
        )

    for spec in all_specs:
        status = _classify_attempt(plan, spec)
        if status != BatchArtifactStatus.SUCCESS:
            failure_code = "INCOMPLETE_ATTEMPT"
            if status not in {
                BatchArtifactStatus.PLANNED,
                BatchArtifactStatus.RUNNING_OR_INCOMPLETE,
            }:
                result = _invocation_result(_attempt_path(plan, spec))
                failure_code = (
                    result.failure_code.value
                    if result.failure_code is not None
                    else status.value
                )
            elif status == BatchArtifactStatus.PLANNED:
                failure_code = "NOT_STARTED"
            failed_rows.append(
                {
                    "profile_id": spec.profile.profile_id,
                    "role": spec.profile.role,
                    "authority_class": spec.profile.authority_class.value,
                    "shard_id": spec.shard.shard_id,
                    "attempt_id": spec.attempt_id,
                    "status": status.value,
                    "failure_code": failure_code,
                    "retry_plan_id": spec.retry_plan_id or "NOT_APPLICABLE",
                }
            )

    profile_rows.sort(
        key=lambda row: (
            plan.profile_by_id[row["profile_id"]].execution_order,
            row["record_id"],
        )
    )
    long_rows.sort(
        key=lambda row: (
            plan.profile_by_id[row["profile_id"]].execution_order,
            row["record_id"],
            row["field_name"],
        )
    )
    failed_rows.sort(
        key=lambda row: (
            plan.profile_by_id[row["profile_id"]].execution_order,
            row["shard_id"],
            row["attempt_id"],
        )
    )
    destination.mkdir(parents=True)
    long_fields = (
        "batch_id",
        "profile_id",
        "role",
        "authority_class",
        "shard_id",
        "attempt_id",
        "record_id",
        "field_name",
        "value_json",
    )
    profile_fields = (
        "batch_id",
        "profile_id",
        "role",
        "authority_class",
        "shard_id",
        "attempt_id",
        "record_id",
        "result_json",
    )
    _write_tsv(destination / "agent_results_long.tsv", long_fields, long_rows)
    _write_tsv(
        destination / "agent_results_by_profile.tsv", profile_fields, profile_rows
    )
    for authority in AuthorityClass:
        _write_tsv(
            destination / f"{authority.value}_results.tsv",
            profile_fields,
            (row for row in profile_rows if row["authority_class"] == authority.value),
        )
    _write_tsv(
        destination / "coverage_audit.tsv",
        (
            "profile_id",
            "role",
            "authority_class",
            "planned_count",
            "observed_count",
            "unique_count",
            "missing_ids",
            "extra_or_cross_batch_ids",
            "duplicate_ids",
            "validation_errors",
            "coverage_status",
        ),
        coverage_rows,
    )
    _write_tsv(
        destination / "profile_completion_summary.tsv",
        (
            "profile_id",
            "role",
            "authority_class",
            "planned_shards",
            "successful_shards",
            "collected_records",
            "coverage_status",
        ),
        completion_rows,
    )
    _write_tsv(
        destination / "failed_shards.tsv",
        (
            "profile_id",
            "role",
            "authority_class",
            "shard_id",
            "attempt_id",
            "status",
            "failure_code",
            "retry_plan_id",
        ),
        failed_rows,
    )
    collection_status = (
        "PASS"
        if all(row["coverage_status"] == "PASS" for row in coverage_rows)
        else "FAIL"
    )
    summary = {
        "record_count": len(global_planned_ids),
        "collection_id": collection_id,
        "contract": RESULT_CONTRACT,
        "failed_attempt_count": len(failed_rows),
        "profile_count": len(plan.snapshot.profiles),
        "status": collection_status,
    }
    _write_json(destination / "collection_summary.json", summary)
    _write_hash_manifest(destination, "collection_manifest.tsv")
    _seal_directory(destination)
    return summary


__all__ = [
    "AuthorityClass",
    "BatchArtifactStatus",
    "BatchError",
    "BatchPlanSnapshot",
    "LoadedPlan",
    "ProfileRole",
    "collect_batch",
    "create_retry_plan",
    "load_batch_plan",
    "plan_batch",
    "run_batch",
    "status_batch",
]
