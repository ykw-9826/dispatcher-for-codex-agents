"""Exclusive writer for one immutable run-local agent shard."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import stat
from pathlib import Path
from typing import Any

from dispatcher_for_codex_agents.agent_harness.contracts import (
    AgentTask,
    InvocationResult,
    ModelProfile,
    validate_local_identifier,
)
from dispatcher_for_codex_agents.agent_harness.payload import InputRecord


class ShardExistsError(FileExistsError):
    """Raised before invocation when an attempt shard already exists."""


def read_verified_shard(path: Path) -> dict[str, bytes]:
    """Read only known regular artifacts; verify the exact versioned manifest."""
    try:
        if (
            not path.is_absolute()
            or path.resolve(strict=True) != path
            or not path.is_dir()
        ):
            raise ValueError("Non-canonical shard directory")
        names = {item.name for item in path.iterdir()}
        base = set(ImmutableShardWriter.REQUIRED_FILES)
        extension = {"interpretation.json", "raw_final_output.bin"}
        if names not in (
            base,
            base | extension,
            base | extension | {"normalized_output.bin"},
        ):
            raise ValueError("Invocation shard file set is incomplete or unexpected")
        data = {}
        for name in sorted(names):
            item = path / name
            metadata = item.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ValueError("Artifact must be a regular non-linked file")
            data[name] = item.read_bytes()
        rows = list(
            csv.DictReader(
                io.StringIO(data["output_sha256.tsv"].decode("utf-8")), delimiter="\t"
            )
        )
        if len(rows) != len(names) - 1 or {row["path"] for row in rows} != names - {
            "output_sha256.tsv"
        }:
            raise ValueError("Invocation manifest coverage mismatch")
        for row in rows:
            content = data[row["path"]]
            if (
                len(content) != int(row["size"])
                or hashlib.sha256(content).hexdigest() != row["sha256"]
            ):
                raise ValueError("Invocation artifact hash mismatch")
        if "interpretation.json" in data:
            audit = json.loads(data["interpretation.json"])
            if not isinstance(audit, dict):
                raise ValueError("Invalid interpretation record")
            if audit.get(
                "artifact_contract"
            ) != "dca.invocation-interpretation/1" or audit.get(
                "normalized_present"
            ) is not (
                "normalized_output.bin" in data
            ):
                raise ValueError("Unsupported interpretation artifact layout")
        return data
    except (OSError, UnicodeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            "Invalid or unsafe invocation shard: " + type(exc).__name__
        ) from exc


class ImmutableShardWriter:
    """Reserve and write exactly one application-immutable attempt directory."""

    REQUIRED_FILES = (
        "agent_task.snapshot.json",
        "model_profile.snapshot.redacted.json",
        "input_sha256.tsv",
        "events.jsonl",
        "stderr.log",
        "final_output.json",
        "invocation_result.json",
        "output_sha256.tsv",
    )

    def __init__(
        self,
        *,
        workers_root: str | Path,
        task_id: str,
        profile_id: str,
        attempt_id: str,
    ) -> None:
        for name, value in (
            ("task_id", task_id),
            ("profile_id", profile_id),
            ("attempt_id", attempt_id),
        ):
            validate_local_identifier(value, field_name=name)
        root = Path(workers_root)
        target = root / "workers" / task_id / profile_id / attempt_id
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.mkdir()
        except FileExistsError as exc:
            raise ShardExistsError(f"Attempt shard already exists: {target}") from exc
        self.path = target
        self._sealed = False

    def _write_exclusive(self, name: str, content: bytes) -> Path:
        if self._sealed:
            raise RuntimeError("Shard is already sealed.")
        target = self.path / name
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return target

    @staticmethod
    def _json_bytes(value: Any) -> bytes:
        return (
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")

    def write(
        self,
        *,
        task: AgentTask,
        profile: ModelProfile,
        input_records: tuple[InputRecord, ...],
        events_jsonl: str | bytes,
        stderr_log: str | bytes,
        final_output: Any | None,
        result: InvocationResult,
        runtime_artifacts: dict[str, bytes] | None = None,
    ) -> Path:
        """Write all required files, hash them, and seal the attempt directory."""
        task_snapshot = task.model_dump(mode="json")
        profile_snapshot = profile.model_dump(mode="json")
        redacted_inputs = [
            f"{item.logical_name}:{item.source_name}" for item in input_records
        ]
        if not redacted_inputs:
            redacted_inputs = [
                f"input_{index:03d}:{Path(raw_path).name}"
                for index, raw_path in enumerate(task.approved_input_files, start=1)
            ]
        task_snapshot["approved_input_files"] = redacted_inputs

        self._write_exclusive(
            "agent_task.snapshot.json", self._json_bytes(task_snapshot)
        )
        self._write_exclusive(
            "model_profile.snapshot.redacted.json",
            self._json_bytes(profile_snapshot),
        )

        input_lines = ["logical_name\tsource_name\tsize\tsha256"]
        input_lines.extend(
            f"{item.logical_name}\t{item.source_name}\t{item.size}\t{item.sha256}"
            for item in input_records
        )
        self._write_exclusive(
            "input_sha256.tsv", ("\n".join(input_lines) + "\n").encode("utf-8")
        )
        self._write_exclusive(
            "events.jsonl",
            (
                events_jsonl.encode("utf-8")
                if isinstance(events_jsonl, str)
                else events_jsonl
            ),
        )
        self._write_exclusive(
            "stderr.log",
            stderr_log.encode("utf-8") if isinstance(stderr_log, str) else stderr_log,
        )
        self._write_exclusive("final_output.json", self._json_bytes(final_output))
        self._write_exclusive(
            "invocation_result.json",
            (result.model_dump_json(indent=2) + "\n").encode("utf-8"),
        )
        if runtime_artifacts is not None:
            if set(runtime_artifacts) not in (
                {"interpretation.json", "raw_final_output.bin"},
                {
                    "interpretation.json",
                    "raw_final_output.bin",
                    "normalized_output.bin",
                },
            ):
                raise ValueError("Unexpected runtime artifact set")
            for name, content in sorted(runtime_artifacts.items()):
                self._write_exclusive(name, content)

        output_lines = ["path\tsize\tsha256"]
        for path in sorted(self.path.iterdir(), key=lambda item: item.name):
            content = path.read_bytes()
            output_lines.append(
                f"{path.name}\t{len(content)}\t{hashlib.sha256(content).hexdigest()}"
            )
        self._write_exclusive(
            "output_sha256.tsv", ("\n".join(output_lines) + "\n").encode("utf-8")
        )

        missing = set(self.REQUIRED_FILES) - {path.name for path in self.path.iterdir()}
        if missing:
            raise RuntimeError(f"Cannot seal incomplete shard: {sorted(missing)!r}")
        for path in self.path.iterdir():
            path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        self.path.chmod(
            stat.S_IRUSR
            | stat.S_IXUSR
            | stat.S_IRGRP
            | stat.S_IXGRP
            | stat.S_IROTH
            | stat.S_IXOTH
        )
        self._sealed = True
        return self.path
