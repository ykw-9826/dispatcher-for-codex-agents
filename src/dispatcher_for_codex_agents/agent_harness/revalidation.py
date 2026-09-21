"""Explicit immutable reinterpretation of hash-pinned invocation evidence.

This module never executes historical commands, resolves providers or calls an
adapter. It uses the same pure interpretation path as CodexCliAdapter.invoke.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import stat
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from .adapter import evaluate_capture
from .contracts import (
    AgentTask,
    FailureCode,
    HarnessModel,
    InvocationResult,
    ModelProfile,
    RuntimeContract,
    validate_local_identifier,
)
from .runtime_contract import canonical_bytes, digest, validate_coverage
from .shard import read_verified_shard


class SourceDescriptor(HarnessModel):
    format: Literal["dca.invocation-shard/1"]
    source_directory: str
    task_id: str
    profile_id: str
    attempt_id: str
    files: dict[str, str]

    @field_validator("task_id", "profile_id", "attempt_id")
    @classmethod
    def _identifier(cls, value):
        return validate_local_identifier(value, field_name="source identity")

    @field_validator("files")
    @classmethod
    def _files(cls, value):
        if not value or any(
            Path(name).name != name or not re.fullmatch(r"[0-9a-f]{64}", sha)
            for name, sha in value.items()
        ):
            raise ValueError("Source file allowlist must contain basenames and SHA256")
        return value


class SelectionEntry(HarnessModel):
    profile_id: str
    shard_id: str
    attempt_id: str
    revalidation_directory: str | None = None


class CollectionSelection(HarnessModel):
    version: Literal[1]
    selections: tuple[SelectionEntry, ...] = Field(min_length=1)


def _read_regular(path: Path) -> bytes:
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError("Evidence paths must be absolute and canonical")
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ValueError("Evidence must be a regular non-linked file")
    return path.read_bytes()


def describe_source(source_directory: str | Path) -> dict:
    """Build a caller-reviewable exact file allowlist; does not write anything."""
    path = Path(source_directory)
    files = read_verified_shard(path)
    return SourceDescriptor(
        format="dca.invocation-shard/1",
        source_directory=str(path),
        task_id=path.parent.parent.name,
        profile_id=path.parent.name,
        attempt_id=path.name,
        files={name: digest(content) for name, content in sorted(files.items())},
    ).model_dump(mode="json")


def _evaluate(
    source_bytes: bytes, contract: RuntimeContract, identity: str
) -> tuple[dict, dict[str, bytes]]:
    descriptor = SourceDescriptor.model_validate_json(source_bytes)
    path = Path(descriptor.source_directory)
    files = read_verified_shard(path)
    if {name: digest(content) for name, content in files.items()} != descriptor.files:
        raise ValueError("Source descriptor hash or file coverage mismatch")
    if (path.parent.parent.name, path.parent.name, path.name) != (
        descriptor.task_id,
        descriptor.profile_id,
        descriptor.attempt_id,
    ):
        raise ValueError("Source path/attempt identity mismatch")
    task = AgentTask.model_validate_json(files["agent_task.snapshot.json"])
    profile = ModelProfile.model_validate_json(
        files["model_profile.snapshot.redacted.json"]
    )
    old = InvocationResult.model_validate_json(files["invocation_result.json"])
    if (
        task.task_id != descriptor.task_id
        or profile.profile_id != descriptor.profile_id
    ):
        raise ValueError("Source task/profile identity mismatch")
    if (
        profile.adapter_id != "codex_cli"
        or old.provenance.get("adapter_id") != "codex_cli"
    ):
        raise ValueError("Unsupported historical adapter")
    # Hashing an empty/malformed input manifest is not validation of input
    # provenance. Completed executions must retain one fingerprint per input.
    if type(old.provenance.get("agent_process_started")) is not bool:
        raise ValueError("Missing or malformed source execution provenance")
    if old.provenance["agent_process_started"]:
        reader = csv.DictReader(
            io.StringIO(files["input_sha256.tsv"].decode("utf-8")), delimiter="\t"
        )
        fingerprints = list(reader)
        if reader.fieldnames != ["logical_name", "source_name", "size", "sha256"]:
            raise ValueError("Unsupported source input fingerprint format")
        if len(fingerprints) != len(task.approved_input_files) or [
            row["logical_name"] + ":" + row["source_name"] for row in fingerprints
        ] != list(task.approved_input_files):
            raise ValueError("Input fingerprint/task coverage mismatch")
        if any(
            int(row["size"]) < 0
            or not re.fullmatch(r"[0-9a-f]{64}", row["sha256"])
            or Path(row["source_name"]).name != row["source_name"]
            for row in fingerprints
        ):
            raise ValueError("Invalid source input fingerprint")
    # Only interpretation changes. Original capability, schema, role and payload
    # fingerprints are not replaced, extended or supplied by the new caller.
    updated = task.model_copy(update={"runtime_contract": contract})
    evaluated, artifacts = evaluate_capture(
        task=updated,
        profile=profile,
        stdout=files["events.jsonl"],
        stderr=files["stderr.log"],
        cli_version=old.provenance.get("codex_cli_version", "NOT_REPORTED"),
        exit_code=old.exit_code,
        provenance=old.provenance,
        timed_out=old.failure_code == FailureCode.TIMEOUT,
        cancelled=old.failure_code == FailureCode.CANCELLED,
    )
    if (
        "raw_final_output.bin" in files
        and artifacts["raw_final_output.bin"] != files["raw_final_output.bin"]
    ):
        raise ValueError("Source raw response disagrees with captured final event")
    coverage = "NOT_APPLICABLE"
    if evaluated.status == "success":
        try:
            coverage = validate_coverage(
                evaluated.final_output, task.expected_output_schema
            )
        except ValueError:
            coverage = "FAIL"
            evaluated = evaluated.model_copy(
                update={
                    "status": "failure",
                    "failure_code": FailureCode.OUTPUT_SCHEMA_INVALID,
                    "schema_validation_status": "FAIL",
                    "warnings": evaluated.warnings + ("EXACT_ONCE_COVERAGE_INVALID",),
                }
            )
    # Timeouts, cancellation, resource/transport failures, unavailable prerequisites
    # and execution failures cannot be fixed by changing output interpretation.
    if old.failure_code not in {
        None,
        FailureCode.POLICY_VIOLATION,
        FailureCode.OUTPUT_SCHEMA_INVALID,
    }:
        evaluated = evaluated.model_copy(
            update={
                "status": "failure",
                "failure_code": old.failure_code,
                "warnings": evaluated.warnings
                + ("SOURCE_FAILURE_NOT_REINTERPRETABLE",),
            }
        )
    evaluation = evaluated.model_dump(mode="json")
    original_usage = old.usage
    evaluation["usage"] = {}
    evaluation["provenance"] = {
        "revalidation_only": True,
        "new_model_requests": 0,
        "new_agent_subprocess_count": 0,
        "source_provenance_sha256": digest(canonical_bytes(old.provenance)),
        "runtime_interpretation": evaluation["provenance"]["runtime_interpretation"],
    }
    audit = evaluation["provenance"]["runtime_interpretation"]
    if audit["normalization"]:
        audit["normalization"]["coverage_status"] = coverage
    artifacts["interpretation.json"] = canonical_bytes(audit) + b"\n"
    artifacts["source_manifest.json"] = source_bytes
    artifacts["evaluation.json"] = canonical_bytes(evaluation) + b"\n"
    record = {
        "format": "dca.revalidation/1",
        "revalidation_id": identity,
        "source_attempt": {
            k: getattr(descriptor, k) for k in ("task_id", "profile_id", "attempt_id")
        },
        "source_directory": str(path),
        "source_manifest_sha256": digest(source_bytes),
        "source_artifact_hashes": descriptor.files,
        "source_input_manifest_sha256": digest(files["input_sha256.tsv"]),
        "source_schema_sha256": digest(canonical_bytes(task.expected_output_schema)),
        "source_capability_policy_sha256": digest(
            canonical_bytes(task.capability_policy.model_dump(mode="json"))
        ),
        "original_outcome": old.status.value,
        "original_failure_code": old.failure_code.value if old.failure_code else None,
        "status": evaluation["status"],
        "failure_code": evaluation["failure_code"],
        "runtime_contract": contract.model_dump(mode="json"),
        "runtime_interpretation": audit,
        "coverage_status": coverage,
        "warnings": evaluation["warnings"],
        "new_model_requests": 0,
        "new_usage": {},
        "new_usage_total_tokens": 0,
        "original_usage_reference": original_usage,
        "original_latency_reference": old.latency_seconds,
    }
    artifacts["revalidation_record.json"] = canonical_bytes(record) + b"\n"
    return record, artifacts


def revalidate(
    *,
    source_manifest: str | Path,
    runtime_contract: RuntimeContract | dict,
    revalidation_id: str,
    output_root: str | Path,
) -> dict:
    """Write one independent sealed result. Never mutate or retry the source."""
    validate_local_identifier(revalidation_id, field_name="revalidation_id")
    contract = RuntimeContract.model_validate(runtime_contract)
    try:
        source_bytes = _read_regular(Path(source_manifest))
        record, artifacts = _evaluate(source_bytes, contract, revalidation_id)
        root = Path(output_root)
        if not root.is_absolute() or root.resolve() != root:
            raise ValueError("Output root must be canonical")
        source = Path(record["source_directory"])
        if root == source or source in root.parents:
            raise ValueError("Revalidation destination cannot be inside the source")
        root.mkdir(parents=True, exist_ok=True)
        destination = root / revalidation_id
        destination.mkdir(mode=0o700)  # exclusive, including incomplete outputs
        artifacts["revalidation_sha256.json"] = (
            canonical_bytes(
                {
                    "format": "dca.revalidation-files/1",
                    "files": {
                        name: digest(content)
                        for name, content in sorted(artifacts.items())
                    },
                }
            )
            + b"\n"
        )
        for name, content in sorted(artifacts.items()):
            with (destination / name).open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            (destination / name).chmod(0o444)
        destination.chmod(0o555)
        return {**record, "directory": str(destination)}
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
        raise ValueError("Revalidation failed closed: " + type(exc).__name__) from exc


def verify_revalidation(directory: str | Path, *, expected_source: Path) -> dict:
    """Recompute the shared evaluation; hashes alone are not interpretation trust."""
    try:
        path = Path(directory)
        if (
            not path.is_absolute()
            or path.resolve(strict=True) != path
            or not path.is_dir()
        ):
            raise ValueError("Unsafe revalidation directory")
        required = {
            "source_manifest.json",
            "evaluation.json",
            "raw_final_output.bin",
            "interpretation.json",
            "revalidation_record.json",
            "revalidation_sha256.json",
        }
        names = {item.name for item in path.iterdir()}
        if names not in (required, required | {"normalized_output.bin"}):
            raise ValueError("Unexpected derivative file set")
        data = {item.name: _read_regular(item) for item in path.iterdir()}
        manifest = json.loads(data["revalidation_sha256.json"])
        record = json.loads(data["revalidation_record.json"])
        if not isinstance(manifest, dict) or not isinstance(record, dict):
            raise ValueError("Invalid derivative structure")
        if (
            manifest.get("format") != "dca.revalidation-files/1"
            or record.get("format") != "dca.revalidation/1"
        ):
            raise ValueError("Unsupported derivative version")
        if record["source_directory"] != str(expected_source):
            raise ValueError("Selected derivative belongs to a different source")
        if record["revalidation_id"] != path.name:
            raise ValueError("Derivative identity mismatch")
        fresh_record, fresh_artifacts = _evaluate(
            data["source_manifest.json"],
            RuntimeContract.model_validate(record["runtime_contract"]),
            path.name,
        )
        if (
            record != fresh_record
            or {
                name: content
                for name, content in data.items()
                if name != "revalidation_sha256.json"
            }
            != fresh_artifacts
        ):
            raise ValueError("Derivative content or evaluation mismatch")
        if manifest != {
            "format": "dca.revalidation-files/1",
            "files": {
                name: digest(content) for name, content in fresh_artifacts.items()
            },
        }:
            raise ValueError("Derivative manifest mismatch")
        return {
            **fresh_record,
            "final_output": json.loads(fresh_artifacts["evaluation.json"])[
                "final_output"
            ],
            "record_sha256": digest(data["revalidation_record.json"]),
        }
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
        raise ValueError(
            "Invalid revalidation evidence: " + type(exc).__name__
        ) from exc


def load_selection(path: str | Path) -> CollectionSelection:
    try:
        return CollectionSelection.model_validate_json(_read_regular(Path(path)))
    except (OSError, ValueError) as exc:
        raise ValueError("Invalid explicit collection selection") from exc
