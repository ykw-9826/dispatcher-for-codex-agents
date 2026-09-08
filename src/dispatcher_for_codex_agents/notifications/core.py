"""Strict metadata envelope, secure local ledger and bounded best-effort delivery."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

KINDS = frozenset(
    {
        "turn_started",
        "turn_completed",
        "session_ended",
        "interrupted",
        "subagent_stopped",
        "batch_completed",
        "batch_failed",
        "shard_failed",
        "explicit_retry_required",
        "human_action_required",
        "delivery_test",
        "event_acceptance_completed",
    }
)
STATUSES = frozenset(
    {"STARTED", "COMPLETED", "FAILED", "INTERRUPTED", "REQUIRED", "TEST"}
)
METRICS = frozenset(
    {
        "elapsed_seconds",
        "planned",
        "success",
        "failed",
        "incomplete",
        "same_thread_verified",
        "wait_model_requests",
        "duplicate_suppression_verified",
    }
)
CORRELATIONS = ("session_id", "turn_id", "run_id", "task_id", "shard_id", "attempt_id")


def identifier(value: str | None) -> str | None:
    if value is not None and (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value)
        or value in {".", ".."}
    ):
        raise ValueError("INVALID_IDENTIFIER")
    return value


@dataclass(frozen=True)
class NotificationEvent:
    """Notification-only contract, separate from application result schemas."""

    source: str
    kind: str
    status: str
    session_id: str | None = None
    turn_id: str | None = None
    run_id: str | None = None
    task_id: str | None = None
    shard_id: str | None = None
    attempt_id: str | None = None
    failure_code: str | None = None
    metrics: dict[str, float | int] = field(default_factory=dict)
    occurred_at: float = field(default_factory=time.time)
    event_id: str = ""

    def __post_init__(self):
        import math

        if self.source not in {"codex", "harness", "test"}:
            raise ValueError("INVALID_SOURCE")
        if self.kind not in KINDS or self.status not in STATUSES:
            raise ValueError("INVALID_EVENT_TYPE")
        for name in CORRELATIONS:
            identifier(getattr(self, name))
        if not any(getattr(self, name) for name in CORRELATIONS):
            raise ValueError("CORRELATION_REQUIRED")
        if self.failure_code is not None and not re.fullmatch(
            r"[A-Z0-9_]{1,100}", self.failure_code
        ):
            raise ValueError("INVALID_FAILURE_CODE")
        if not math.isfinite(self.occurred_at) or self.occurred_at < 0:
            raise ValueError("INVALID_TIME")
        for key, value in self.metrics.items():
            if (
                key not in METRICS
                or type(value) not in {int, float}
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError("INVALID_METRIC")
        identity = [self.source, self.kind, self.status, self.failure_code]
        identity.extend(getattr(self, key) for key in CORRELATIONS)
        expected = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        if self.event_id and self.event_id != expected:
            raise ValueError("EVENT_ID_MISMATCH")
        object.__setattr__(self, "event_id", expected)

    def payload(self) -> dict[str, Any]:
        value = asdict(self)
        value["model_calls"] = 0
        value["result_approval_claimed"] = False
        return value


def repository_root(path: Path) -> Path | None:
    return next((parent for parent in path.parents if (parent / ".git").exists()), None)


def external_path(path: str | Path) -> Path:
    value = Path(path).expanduser().absolute()
    if value.resolve() != value:
        raise ValueError("SYMLINK_PATH_FORBIDDEN")
    if repository_root(value) is not None:
        raise ValueError("NOTIFICATION_STORAGE_MUST_BE_OUTSIDE_REPOSITORY")
    return value


def storage_path(path: str | Path) -> Path:
    """Non-secret config/state only; secret files still use external_path."""
    from dispatcher_for_codex_agents.workspace_paths import workspace_root

    value = Path(path).expanduser().absolute()
    if value.resolve() != value:
        raise ValueError("SYMLINK_PATH_FORBIDDEN")
    repository = repository_root(value)
    if repository is None:
        return value
    root = workspace_root()
    # A configured private snapshot can be nested under another Git checkout.
    # Its approved non-secret storage remains limited to its own two roots.
    if root is not None and any(
        value.is_relative_to(root / name) for name in ("configs", "runtime")
    ):
        return value
    raise ValueError("NOTIFICATION_STORAGE_MUST_BE_OUTSIDE_REPOSITORY")


def private_read(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (
            info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or not stat.S_ISREG(info.st_mode)
        ):
            raise ValueError("CONFIG_OWNER_OR_MODE_INVALID")
        content = handle.read(65537)
        if len(content) > 65536:
            raise ValueError("CONFIG_TOO_LARGE")
        return content


def load_config(path: str | Path) -> dict[str, Any]:
    resolved = storage_path(path)
    config = json.loads(private_read(resolved))
    if (
        not isinstance(config, dict)
        or set(config) != {"version", "ledger_directory", "sinks"}
        or config["version"] != 1
    ):
        raise ValueError("CONFIG_SCHEMA_INVALID")
    directory = storage_path(config["ledger_directory"])
    if not directory.is_absolute() or directory == Path("/"):
        raise ValueError("LEDGER_PATH_INVALID")
    if not isinstance(config["sinks"], list) or len(config["sinks"]) > 2:
        raise ValueError("AT_MOST_TWO_SINKS")
    from .sinks import configured_sink

    seen = set()
    for row in config["sinks"]:
        if (
            repository_root(resolved)
            and isinstance(row, dict)
            and (row.get("send_key") or row.get("url"))
        ):
            raise ValueError("INLINE_SECRET_MUST_BE_OUTSIDE_REPOSITORY")
        configured_sink(row)
        if row["sink_id"] in seen:
            raise ValueError("DUPLICATE_SINK_ID")
        seen.add(row["sink_id"])
    return config


@contextmanager
def ledger(directory: str | Path):
    """Nonblocking process lock; append-only rows are flushed before network I/O."""
    directory = storage_path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("LEDGER_DIRECTORY_NOT_PRIVATE")
    descriptor = os.open(
        directory / "delivery.jsonl", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "r+", encoding="utf-8") as handle:
        info = os.fstat(handle.fileno())
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("LEDGER_NOT_PRIVATE")
        deadline = time.monotonic() + 0.1
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.005)
        rows = [json.loads(line) for line in handle if line.strip()]

        def append(row):
            handle.seek(0, os.SEEK_END)
            handle.write(json.dumps(row, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            rows.append(row)

        yield rows, append


def notify(
    event: NotificationEvent, config_path: str | Path, *, sender=None, clock=time.time
) -> dict[str, Any]:
    """Never raise or retry; a delivery error cannot alter any business result."""
    from .sinks import bounded_send, configured_sink

    sender = sender or bounded_send
    try:
        config = load_config(config_path)
        results = {}
        with ledger(config["ledger_directory"]) as (_, append):
            append({"observed": True, "event": event.payload(), "recorded_at": clock()})
        for settings in config["sinks"]:
            sink_id = settings["sink_id"]
            if not settings["enabled"]:
                continue
            should_send = False
            with ledger(config["ledger_directory"]) as (rows, append):
                matches = [
                    row
                    for row in rows
                    if row.get("sink_id") == sink_id
                    and row.get("event_id") == event.event_id
                ]
                row = {
                    "event_id": event.event_id,
                    "sink_id": sink_id,
                    "event": event.payload(),
                    "recorded_at": clock(),
                }
                if matches:
                    outcome = {
                        "delivery_status": "DUPLICATE_SUPPRESSED",
                        "original_status": next(
                            (
                                r["delivery_status"]
                                for r in reversed(matches)
                                if r["delivery_status"] != "DUPLICATE_SUPPRESSED"
                            ),
                            "DELIVERY_UNKNOWN",
                        ),
                    }
                else:
                    # Only confirmed same session/turn/run and this sink qualify.
                    candidates = [
                        r
                        for r in rows
                        if r.get("sink_id") == sink_id
                        and r.get("delivery_status") == "SENT"
                        and r.get("event", {}).get("kind")
                        in {"batch_completed", "batch_failed"}
                        and all(
                            getattr(event, k) and r["event"].get(k) == getattr(event, k)
                            for k in ("session_id", "turn_id", "run_id")
                        )
                        and 0 <= clock() - r["recorded_at"] <= 300
                    ]
                    if event.kind == "turn_completed" and candidates:
                        outcome = {"delivery_status": "HARNESS_TERMINAL_SUPPRESSED"}
                    else:
                        # Crash/timeout before a confirmed response remains unknown.
                        append({**row, "delivery_status": "DELIVERY_UNKNOWN"})
                        should_send = True
                if not should_send:
                    append({**row, **outcome})
            if should_send:
                # Network is outside the ledger lock: failure/human-action events
                # remain independent while another event is being delivered.
                outcome = sender(configured_sink(settings), event)
                with ledger(config["ledger_directory"]) as (_, append):
                    append({**row, **outcome, "recorded_at": clock()})
            results[sink_id] = outcome
        return {
            "status": "PROCESSED" if results else "USER_CONFIGURATION_REQUIRED",
            "sinks": results,
            "model_calls": 0,
        }
    except FileNotFoundError:
        return {"status": "USER_CONFIGURATION_REQUIRED", "model_calls": 0}
    except Exception as exc:
        # Never stringify exceptions: URLs, keys and hook input can be embedded.
        return {
            "status": "NOTIFICATION_NONBLOCKING_ERROR",
            "error_class": type(exc).__name__,
            "model_calls": 0,
        }


def record_context(
    config_path: str | Path,
    *,
    session_id: str,
    turn_id: str | None,
    run_id: str | None = None,
    started: bool = False,
):
    """Explicit local turn/run binding; never infer identity from prompt content."""
    for value in (session_id, turn_id, run_id):
        identifier(value)
    config = load_config(config_path)
    with ledger(config["ledger_directory"]) as (rows, append):
        matches = [
            row
            for row in rows
            if row.get("context")
            and row.get("session_id") == session_id
            and row.get("turn_id") == turn_id
        ]
        prior = matches[-1] if matches else {}
        row = {
            "context": True,
            "session_id": session_id,
            "turn_id": turn_id,
            "run_id": run_id or prior.get("run_id"),
            "started_at": prior.get("started_at", time.time()),
            "turn_started": started or prior.get("turn_started", False),
        }
        if started or run_id:
            append(row)
        return row
