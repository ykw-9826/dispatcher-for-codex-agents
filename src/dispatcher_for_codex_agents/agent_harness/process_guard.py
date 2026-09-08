"""Local execution ownership and cooperative cancellation, without a service."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import signal
import tempfile
import threading
import time
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any


def process_identity(pid: int) -> dict[str, Any] | None:
    """Linux boot + start ticks disambiguate PID reuse; no process is killed here."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return {
            "pid": pid,
            "start_ticks": fields[19],
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        }
    except (OSError, IndexError, ValueError):
        return None


def execution_is_locked(root: Path) -> bool:
    """Inspect advisory ownership without creating or changing an artifact."""
    try:
        descriptor = os.open(root / ".execution.lock", os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True
    finally:
        os.close(descriptor)


def invocation_process_path(
    root: Path, task_id: str, profile_id: str, attempt_id: str
) -> Path:
    key = hashlib.sha256(
        json.dumps([task_id, profile_id, attempt_id]).encode()
    ).hexdigest()
    return root / ".processes" / (key + ".json")


def record_invocation_process(
    root: Path, task_id: str, profile_id: str, attempt_id: str, *, child_pid: int | None
):
    """Operational ownership, separate from the sealed result envelope."""
    path = invocation_process_path(root, task_id, profile_id, attempt_id)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    value = {
        "task_id": task_id,
        "profile_id": profile_id,
        "attempt_id": attempt_id,
        "parent": process_identity(os.getpid()),
        "child": process_identity(child_pid) if child_pid else None,
        "started_at": time.time(),
        "phase": "STARTED" if child_pid else "LAUNCH_INTENT",
    }
    descriptor, temporary = tempfile.mkstemp(prefix=".process-", dir=path.parent)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(value, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def assert_attempt_not_active(
    root: Path, task_id: str, profile_id: str, attempt_id: str, *, incomplete: bool
):
    path = invocation_process_path(root, task_id, profile_id, attempt_id)
    if not path.exists():
        return  # Legacy attempt: human retry approval must confirm termination.
    value = json.loads(path.read_text())
    child = value.get("child")
    if child and process_identity(child["pid"]) == child:
        raise ValueError("RETRY_BLOCKED_CHILD_STILL_ALIVE")
    if incomplete and not child:
        raise ValueError("RETRY_BLOCKED_LAUNCH_STATE_UNKNOWN")


@contextmanager
def cancellation_signals(event: threading.Event):
    """Signal handlers request cancellation; adapters reap their own children."""
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, lambda *_: event.set())
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


@contextmanager
def execution_lock(root: str | Path, *, run_id: str | None = None):
    """Only one parent may run or create retries in a batch at a time."""
    root = Path(root).resolve(strict=True)
    descriptor = os.open(
        root / ".execution.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError(
                "BATCH_EXECUTION_ACTIVE: another parent owns this batch"
            ) from exc
        if run_id is not None:
            now = time.time()
            previous_path = root / ".execution.owner.json"
            previous_owner = (
                json.loads(previous_path.read_text()) if previous_path.exists() else {}
            )
            owner = {
                "identity": process_identity(os.getpid()),
                # Cadence uses the whole batch, including explicit continuation.
                "started_at": previous_owner.get("started_at", now),
                "runner_started_at": now,
                "run_id": run_id,
            }
            fd, temporary = tempfile.mkstemp(prefix=".owner-", dir=root)
            with os.fdopen(fd, "w") as handle:
                json.dump(owner, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, root / ".execution.owner.json")
        yield
    finally:
        os.close(descriptor)


def exclusive_execution(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        if kwargs.get("dry_run", False):
            return function(*args, **kwargs)
        with execution_lock(kwargs["plan_root"], run_id=kwargs.get("run_id")):
            return function(*args, **kwargs)

    return wrapped
