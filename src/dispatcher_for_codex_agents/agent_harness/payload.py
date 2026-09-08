"""Deterministic, allowlist-only payload construction for agents."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass
from pathlib import Path

from dispatcher_for_codex_agents.agent_harness.contracts import AgentTask


class PayloadBuildError(ValueError):
    """Raised when approved input cannot be safely canonicalized."""


@dataclass(frozen=True, slots=True)
class InputRecord:
    """Redacted input provenance for one approved file."""

    logical_name: str
    source_name: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class BuiltPayload:
    """Canonical stdin payload and its input hash records."""

    content: str
    input_records: tuple[InputRecord, ...]


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class PayloadBuilder:
    """Read only task-approved tabular files and emit a stable stdin payload."""

    def __init__(self, *, allowed_roots: tuple[str | Path, ...] | None = None) -> None:
        self._allowed_roots = (
            tuple(Path(root).resolve(strict=True) for root in allowed_roots)
            if allowed_roots is not None
            else None
        )

    def _resolve_approved_file(self, raw_path: str) -> Path:
        path = Path(raw_path)
        display_name = path.name or "<unnamed-input>"
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise PayloadBuildError(
                f"Approved input is unavailable: {display_name}"
            ) from exc
        if not resolved.is_file():
            raise PayloadBuildError(
                f"Approved input is not a regular file: {display_name}"
            )
        if path.is_symlink():
            raise PayloadBuildError(f"Symlink inputs are forbidden: {display_name}")
        if self._allowed_roots is not None and not any(
            resolved == root or root in resolved.parents for root in self._allowed_roots
        ):
            raise PayloadBuildError(
                f"Approved input is outside configured allowed roots: {display_name}"
            )
        return resolved

    @staticmethod
    def _read_selected_rows(
        path: Path, raw_bytes: bytes, selected_columns: tuple[str, ...]
    ) -> list[dict[str, str]]:
        delimiter = "\t" if path.suffix.casefold() == ".tsv" else ","
        if path.suffix.casefold() not in {".tsv", ".csv"}:
            raise PayloadBuildError(
                f"Only UTF-8 TSV/CSV inputs are supported: {path.name}"
            )
        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PayloadBuildError(f"Input is not valid UTF-8: {path.name}") from exc
        reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=delimiter)
        if reader.fieldnames is None:
            raise PayloadBuildError(f"Input has no header: {path.name}")
        missing = [name for name in selected_columns if name not in reader.fieldnames]
        if missing:
            raise PayloadBuildError(
                f"Input {path.name} is missing selected columns: {missing!r}"
            )
        rows = [
            {column: row.get(column, "") for column in selected_columns}
            for row in reader
        ]

        rows.sort(key=lambda row: tuple(row[column] for column in selected_columns))
        return rows

    @staticmethod
    def _render_selected_tsv(
        rows: list[dict[str, str]], selected_columns: tuple[str, ...]
    ) -> str:
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(
            buffer,
            fieldnames=list(selected_columns),
            delimiter="\t",
            lineterminator="\n",
            extrasaction="raise",
        )
        writer.writeheader()
        writer.writerows(rows)
        return buffer.getvalue()

    def build(self, task: AgentTask) -> BuiltPayload:
        """Build a deterministic payload without exposing source paths to agent."""
        resolved_paths = [
            self._resolve_approved_file(path) for path in task.approved_input_files
        ]
        if len(resolved_paths) != len(set(resolved_paths)):
            raise PayloadBuildError("Approved inputs resolve to duplicate files.")
        resolved_paths.sort(key=lambda path: (path.name, str(path)))

        lines = [
            "DCA_AGENT_TASK_V1",
            "TASK "
            + _canonical_json(
                {
                    "role": task.role,
                    "selected_columns": list(task.selected_columns),
                    "task_id": task.task_id,
                }
            ),
            "PROMPT_BEGIN",
            task.prompt_template,
            "PROMPT_END",
            "EXPECTED_OUTPUT_SCHEMA " + _canonical_json(task.expected_output_schema),
            "POLICY "
            + _canonical_json(
                {
                    "agent_recursion": "forbidden",
                    "filesystem_access": "forbidden",
                    "output": "single_json_object_only",
                    "tools": "forbidden",
                }
            ),
        ]
        records: list[InputRecord] = []

        for index, path in enumerate(resolved_paths, start=1):
            raw_bytes = path.read_bytes()
            record = InputRecord(
                logical_name=f"input_{index:03d}",
                source_name=path.name,
                size=len(raw_bytes),
                sha256=hashlib.sha256(raw_bytes).hexdigest(),
            )
            rows = self._read_selected_rows(path, raw_bytes, task.selected_columns)
            lines.extend(
                [
                    "INPUT_BEGIN "
                    + _canonical_json(
                        {
                            "logical_name": record.logical_name,
                            "sha256": record.sha256,
                            "source_name": record.source_name,
                        }
                    ),
                    self._render_selected_tsv(rows, task.selected_columns).rstrip("\n"),
                    "INPUT_END",
                ]
            )
            records.append(record)

        lines.extend(
            [
                "INSTRUCTION Return exactly one JSON object matching "
                "EXPECTED_OUTPUT_SCHEMA. Do not use tools or access files.",
                "DCA_AGENT_TASK_END",
                "",
            ]
        )
        return BuiltPayload(content="\n".join(lines), input_records=tuple(records))
