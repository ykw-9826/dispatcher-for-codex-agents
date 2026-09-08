"""HTTPS only, fixed operational templates, bounded one-shot sink processes."""

from __future__ import annotations

import http.client
import json
import os
import re
import select
import signal
import ssl
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlencode, urlsplit


class NotificationSink(Protocol):
    sink_id: str

    def send(self, event) -> dict: ...


def https_post(url: str, body: bytes, content_type: str) -> tuple[int, bytes]:
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.fragment
    ):
        raise ValueError("HTTPS_TARGET_REQUIRED")
    connection = http.client.HTTPSConnection(
        parts.hostname, parts.port, timeout=0.5, context=ssl.create_default_context()
    )
    try:
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        connection.request("POST", target, body, {"Content-Type": content_type})
        response = connection.getresponse()
        return response.status, response.read(65537)
    finally:
        connection.close()


@dataclass(frozen=True)
class ServerChanSink:
    sink_id: str
    send_key: str = field(repr=False)

    def send(self, event) -> dict:
        from .presentation import present

        display = present(event)
        body = urlencode(
            {
                "title": display["title"],
                "desp": display["body"],
            }
        ).encode()
        status, content = https_post(
            "https://sctapi.ftqq.com/" + self.send_key + ".send",
            body,
            "application/x-www-form-urlencoded",
        )
        return validate_response(status, content, "code", 0)


@dataclass(frozen=True)
class GenericWebhookSink:
    sink_id: str
    url: str = field(repr=False)
    success_field: str = "ok"
    success_value: str | int | bool = True

    def send(self, event) -> dict:
        from .presentation import present

        status, content = https_post(
            self.url,
            json.dumps(present(event), ensure_ascii=False, sort_keys=True).encode(),
            "application/json",
        )
        return validate_response(
            status, content, self.success_field, self.success_value
        )


def validate_response(status: int, content: bytes, field: str, expected) -> dict:
    if not 200 <= status < 300:
        return {
            "delivery_status": "FAILED",
            "failure_code": "HTTP_ERROR",
            "http_status": status,
        }
    try:
        value = json.loads(content)
        passed = (
            isinstance(value, dict)
            and type(value.get(field)) is type(expected)
            and value[field] == expected
        )
    except (ValueError, UnicodeError):
        passed = False
    return {
        "delivery_status": "SENT" if passed else "FAILED",
        "http_status": status,
        "application_status": "PASS" if passed else "FAIL",
    }


def configured_sink(row: dict) -> NotificationSink:
    from .core import external_path, identifier, private_read

    if not isinstance(row, dict) or type(row.get("enabled")) is not bool:
        raise ValueError("SINK_CONFIG_INVALID")
    if not identifier(row.get("sink_id")):
        raise ValueError("SINK_ID_REQUIRED")
    common = {"sink_id", "kind", "enabled"}
    if row.get("kind") == "serverchan":
        if set(row) == common | {"send_key_env_file"}:
            # Read the existing credential, never source/eval shell text or
            # include secret values in diagnostics.
            content = private_read(external_path(row["send_key_env_file"]))
            lines = [
                line.strip()
                for line in content.decode("ascii").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
            if len(lines) != 1 or not lines[0].startswith("SERVERCHAN_SENDKEY="):
                raise ValueError("SERVERCHAN_SECRET_FILE_INVALID")
            send_key = lines[0].split("=", 1)[1]
        elif set(row) == common | {"send_key"}:
            send_key = row["send_key"]
        else:
            raise ValueError("SINK_CONFIG_INVALID")
        # Turbo API only; other ServerChan protocols require explicit support.
        if not isinstance(send_key, str) or (
            row["enabled"] and not re.fullmatch(r"SCT[A-Za-z0-9]{8,256}", send_key)
        ):
            raise ValueError("SERVERCHAN_TURBO_KEY_REQUIRED")
        return ServerChanSink(row["sink_id"], send_key)
    if row.get("kind") == "webhook":
        if set(row) != common | {"url", "success_field", "success_value"}:
            raise ValueError("SINK_CONFIG_INVALID")
        if not isinstance(row["url"], str):
            raise ValueError("WEBHOOK_URL_INVALID")
        parts = urlsplit(row["url"])
        if row["enabled"] and (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.fragment
        ):
            raise ValueError("HTTPS_TARGET_REQUIRED")
        identifier(row["success_field"])
        if not row["success_field"] or type(row["success_value"]) not in {
            str,
            int,
            bool,
        }:
            raise ValueError("APPLICATION_ACK_REQUIRED")
        return GenericWebhookSink(
            row["sink_id"], row["url"], row["success_field"], row["success_value"]
        )
    raise ValueError("SINK_NOT_IMPLEMENTED")


def bounded_send(sink: NotificationSink, event, *, timeout: float = 0.9) -> dict:
    """One short-lived child bounds DNS/TLS/read time; no daemon or retry queue."""
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            try:
                outcome = sink.send(event)
            except Exception:
                outcome = {
                    "delivery_status": "DELIVERY_UNKNOWN",
                    "failure_code": "TRANSPORT_ERROR",
                }
            os.write(write_fd, json.dumps(outcome).encode())
        finally:
            os.close(write_fd)
            os._exit(0)
    os.close(write_fd)
    try:
        if select.select([read_fd], [], [], timeout)[0]:
            content = os.read(read_fd, 4096)
            if content:
                return json.loads(content)
        return {
            "delivery_status": "DELIVERY_UNKNOWN",
            "failure_code": "DELIVERY_TIMEOUT",
        }
    finally:
        os.close(read_fd)
        # Only our own unreaped child; PID cannot be reused before waitpid.
        if os.waitpid(pid, os.WNOHANG)[0] == 0:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
