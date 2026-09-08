"""Aggregate, elapsed-time monitoring with PID identity and terminal wakeup."""

from __future__ import annotations

import ctypes
import json
import math
import os
import select
import time
from collections import Counter
from pathlib import Path
from typing import Any

from .batch import _all_attempt_specs, _classify_attempt, load_batch_plan
from .process_guard import execution_is_locked, process_identity


def check_interval(elapsed_seconds: float) -> float:
    if elapsed_seconds < 0 or not math.isfinite(elapsed_seconds):
        raise ValueError("elapsed time must be finite and nonnegative")
    for boundary, interval in ((900, 180), (1800, 300), (7200, 900)):
        if elapsed_seconds < boundary:
            return float(interval)
    return 3600.0


def _resources(identity: dict[str, Any]) -> dict[str, int]:
    values = {}
    try:
        proc = Path(f"/proc/{identity['pid']}")
        fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
        values["cpu_ticks"] = int(fields[11]) + int(fields[12])
        for line in (proc / "io").read_text().splitlines():
            key, value = line.split(":", 1)
            values[key] = int(value)
    except (OSError, ValueError, IndexError):
        pass
    return values


def aggregate_status(plan_root: str | Path) -> dict[str, Any]:
    """No cache is authoritative; plans and immutable results define state."""
    plan = load_batch_plan(plan_root)
    counts = Counter(
        _classify_attempt(plan, spec).value for spec in _all_attempt_specs(plan)
    )
    owner_path = plan.root / ".execution.owner.json"
    owner = json.loads(owner_path.read_text()) if owner_path.exists() else {}
    identity = owner.get("identity")
    alive = bool(
        identity
        and process_identity(identity["pid"]) == identity
        and execution_is_locked(plan.root)
    )
    resources = _resources(identity) if alive else {}
    children = []
    for marker in sorted((plan.root / ".processes").glob("*.json")):
        value = json.loads(marker.read_text())
        child = value.get("child")
        if child and process_identity(child["pid"]) == child:
            children.append({"identity": child, "resources": _resources(child)})
    stats = [p.stat() for p in (plan.root / "workers").rglob("*") if p.is_file()]
    elapsed = max(0.0, time.time() - owner.get("started_at", time.time()))
    incomplete = counts["RUNNING_OR_INCOMPLETE"]
    failures = sum(counts[x] for x in ("FAILED", "POLICY_VIOLATION", "SCHEMA_INVALID"))
    return {
        "batch_id": plan.snapshot.batch_id,
        "status_counts": dict(sorted(counts.items())),
        "parent_identity": identity,
        "parent_alive": alive,
        "owner_run_id": owner.get("run_id"),
        "elapsed_seconds": elapsed,
        "next_check_seconds": check_interval(elapsed),
        "long_run_diagnostic_required": elapsed > 43200,
        "file_count": len(stats),
        "file_bytes": sum(s.st_size for s in stats),
        "latest_mtime": max((s.st_mtime for s in stats), default=None),
        "resources": resources,
        "active_children": children,
        "events_buffered_until_terminal": True,
        "terminal": not alive and counts["PLANNED"] == 0 and incomplete == 0,
        "human_action_required": bool(failures or (incomplete and not alive)),
    }


def wait_for_check(
    identity: dict[str, Any] | None, seconds: float, *, plan_root: Path | None = None
) -> None:
    """Wake on parent exit when Linux pidfd is available, without minute polls."""
    if identity and process_identity(identity["pid"]) != identity:
        return
    if plan_root is not None:
        # Linux inotify wakes when the owning runner releases its writable lock,
        # including a still-alive library host; no short-interval polling.
        libc = ctypes.CDLL(None, use_errno=True)
        descriptor = libc.inotify_init1(os.O_CLOEXEC | os.O_NONBLOCK)
        if descriptor >= 0:
            try:
                watched = libc.inotify_add_watch(
                    descriptor, os.fsencode(plan_root / ".execution.lock"), 0x8
                )
                if watched >= 0:
                    if execution_is_locked(plan_root):
                        select.select([descriptor], [], [], seconds)
                    return
            finally:
                os.close(descriptor)
    if identity and hasattr(os, "pidfd_open"):
        try:
            if process_identity(identity["pid"]) != identity:
                return
            descriptor = os.pidfd_open(identity["pid"])
            try:
                if process_identity(identity["pid"]) == identity:
                    select.select([descriptor], [], [], seconds)
            finally:
                os.close(descriptor)
            return
        except (OSError, ProcessLookupError):
            return
    time.sleep(seconds)


def monitor_batch(plan_root: str | Path, *, watch: bool = False) -> dict[str, Any]:
    previous = None
    unchanged = 0
    previous_diagnostic = None
    while True:
        status = aggregate_status(plan_root)
        progress = (
            status["status_counts"],
            status["file_count"],
            status["file_bytes"],
            status["latest_mtime"],
            status["resources"],
            status["active_children"],
            status["parent_alive"],
        )
        unchanged = unchanged + 1 if progress == previous else 0
        status["consecutive_unchanged_checks"] = unchanged
        status["stalled_diagnostic_required"] = unchanged >= 2
        diagnostic = (
            status["stalled_diagnostic_required"],
            status["long_run_diagnostic_required"],
        )
        if progress != previous or diagnostic != previous_diagnostic:
            print(json.dumps(status, sort_keys=True), flush=True)
        previous_diagnostic = diagnostic
        if (
            not watch
            or status["terminal"]
            or status["human_action_required"]
            or not status["parent_alive"]
        ):
            return status
        previous = progress
        wait_for_check(
            status["parent_identity"],
            status["next_check_seconds"],
            plan_root=Path(plan_root),
        )
