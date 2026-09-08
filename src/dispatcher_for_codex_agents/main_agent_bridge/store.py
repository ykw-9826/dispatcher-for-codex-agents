"""Private durable event files and append-only consumption, with OS wakeups."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path

from dispatcher_for_codex_agents.notifications.core import private_read

from .contracts import BridgeEvent


def canonical(value) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False) + "\n").encode()


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def safe_path(root: Path, relative: str) -> Path:
    path = root / relative
    if (
        Path(relative).is_absolute()
        or ".." in Path(relative).parts
        or path.resolve() != path
        or root not in path.parents
    ):
        raise ValueError("PATH_ALLOWLIST_VIOLATION")
    return path


def private_directory(path: Path, *, create=False):
    if path.resolve() != path:
        raise ValueError("SYMLINK_FORBIDDEN")
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=False)
    info = path.stat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or (stat.S_IMODE(info.st_mode) != 0o700)
    ):
        raise ValueError("EVENT_DIRECTORY_NOT_PRIVATE")


def exclusive(path: Path, content: bytes):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def lock(path: Path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("LOCK_NOT_PRIVATE")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("ACTIVE_THREAD_CONFLICT") from exc
        yield
    finally:
        os.close(fd)


class EventStore:
    def __init__(self, root: Path, *, create=False):
        self.root = root.absolute()
        private_directory(self.root, create=create)
        if create:
            private_directory(self.root / "events", create=True)
            private_directory(self.root / "artifacts", create=True)
            exclusive(self.root / "event.key", secrets.token_bytes(32))
        self.key = private_read(self.root / "event.key")
        if len(self.key) != 32:
            raise ValueError("EVENT_KEY_INVALID")
        self.changed = asyncio.Event()

    def append(self, record: dict):
        # Only the lock-holding controller writes this ledger; no receiver cache.
        path = self.root / "consumption.jsonl"
        fd = os.open(
            path, os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600
        )
        try:
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError("LEDGER_NOT_PRIVATE")
            content = canonical(record)
            if os.write(fd, content) != len(content):
                raise ValueError("LEDGER_PARTIAL_WRITE")
            os.fsync(fd)
        finally:
            os.close(fd)

    def records(self) -> list[dict]:
        path = self.root / "consumption.jsonl"
        if not path.exists():
            return []
        # Bounded task ledger, not an unbounded queue; torn records fail closed.
        content = private_read(path)
        if content and not content.endswith(b"\n"):
            raise ValueError("LEDGER_TORN_WRITE")
        return [json.loads(line) for line in content.splitlines()]

    def validate_manifest(self, event: BridgeEvent) -> dict:
        path = safe_path(self.root / "artifacts", event.manifest)
        content = private_read(path)
        if digest(content) != event.manifest_sha256:
            raise ValueError("MANIFEST_HASH_MISMATCH")
        value = json.loads(content)
        if set(value) != {"task_id", "status", "files"} or (
            value["task_id"] != event.task_id
            or value["status"] not in {"SUCCESS", "BLOCKED"}
            or not isinstance(value["files"], list)
            or not 1 <= len(value["files"]) <= 8
        ):
            raise ValueError("RESULT_MANIFEST_INVALID")
        if (event.kind == "batch_completed") != (value["status"] == "SUCCESS"):
            raise ValueError("EVENT_TERMINAL_MISMATCH")
        seen = set()
        for row in value["files"]:
            if set(row) != {"path", "sha256"} or row["path"] in seen:
                raise ValueError("MANIFEST_COVERAGE_INVALID")
            seen.add(row["path"])
            data = private_read(safe_path(self.root / "artifacts", row["path"]))
            if digest(data) != row["sha256"]:
                raise ValueError("RESULT_HASH_MISMATCH")
        return value

    def put(self, event: BridgeEvent) -> tuple[str, bool]:
        self.validate_manifest(event)
        # Delivery time/order are not identity: re-delivery cannot create a wake.
        identity = event.model_dump(exclude={"occurred_at", "sequence"})
        event_id = digest(canonical(identity))
        document = {"event_id": event_id, "event": event.model_dump()}
        document["signature"] = hmac.new(
            self.key, canonical(document), hashlib.sha256
        ).hexdigest()
        path = self.root / "events" / (event_id + ".json")
        if path.exists():
            self.read_event(path)
            return event_id, False
        exclusive(path, canonical(document))
        self.changed.set()
        return event_id, True

    def read_event(self, path: Path) -> tuple[str, BridgeEvent]:
        value = json.loads(private_read(path))
        if set(value) != {"event_id", "event", "signature"}:
            raise ValueError("EVENT_ENVELOPE_INVALID")
        signature = value.pop("signature")
        if not hmac.compare_digest(
            signature, hmac.new(self.key, canonical(value), hashlib.sha256).hexdigest()
        ):
            raise ValueError("EVENT_SOURCE_INVALID")
        event = BridgeEvent.model_validate(value["event"])
        identity = event.model_dump(exclude={"occurred_at", "sequence"})
        if value["event_id"] != digest(canonical(identity)) or (
            path.name != value["event_id"] + ".json"
        ):
            raise ValueError("EVENT_ID_MISMATCH")
        self.validate_manifest(event)
        return value["event_id"], event

    def pending(self) -> list[tuple[str, BridgeEvent]]:
        reserved = {
            event_id
            for row in self.records()
            if row.get("state") in {"RESERVED", "COMPLETED", "AMBIGUOUS"}
            for event_id in row.get("event_ids", [])
        }
        events = [
            self.read_event(p) for p in sorted((self.root / "events").glob("*.json"))
        ]
        return sorted(
            (item for item in events if item[0] not in reserved),
            key=lambda item: (item[1].sequence, item[0]),
        )

    async def wait(self) -> list[tuple[str, BridgeEvent]]:
        # Check BEFORE and AFTER clearing: early events and lost edges are safe.
        while True:
            pending = self.pending()
            if pending:
                return pending
            self.changed.clear()
            pending = self.pending()
            if pending:
                return pending
            await self.changed.wait()  # OS event loop blocks; no timer/status polling.
