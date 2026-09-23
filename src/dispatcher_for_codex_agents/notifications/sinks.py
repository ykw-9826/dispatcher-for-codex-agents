"""HTTPS only, fixed operational templates, bounded one-shot sink processes."""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import math
import os
import re
import select
import signal
import socket
import ssl
import time
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import quote, urlencode, urlsplit

SOCKET_TIMEOUT_SECONDS = 0.5
SINK_DEADLINE_SECONDS = 0.9
MAX_SINKS = 3
MAX_SINK_DEADLINE_SECONDS = 5.0
LOCAL_OVERHEAD_SECONDS = 2
# Covers any three allowed budgets, not just two Turbo targets plus one SC3.
# Local overhead is a tested allowance, not an OS/large-ledger scheduling SLA.
HOOK_TIMEOUT_SECONDS = math.ceil(
    MAX_SINKS * MAX_SINK_DEADLINE_SECONDS + LOCAL_OVERHEAD_SECONDS
)


def _seconds(value, minimum: float, maximum: float) -> float:
    if type(value) not in (int, float) or not minimum <= value <= maximum:
        raise ValueError("TRANSPORT_BUDGET_INVALID")
    return float(value)


@dataclass(frozen=True)
class TransportBudget:
    socket_timeout: float
    deadline: float

    def __post_init__(self):
        _seconds(self.socket_timeout, 0.1, MAX_SINK_DEADLINE_SECONDS)
        _seconds(self.deadline, 0.2, MAX_SINK_DEADLINE_SECONDS)
        if self.socket_timeout > self.deadline:
            raise ValueError("TRANSPORT_BUDGET_INVALID")


def serverchan_budget(protocol: str, overrides: dict) -> TransportBudget:
    socket_timeout, deadline = (
        (4.0, 5.0) if protocol == "serverchan_sc3" else (2.0, 2.5)
    )
    return TransportBudget(
        overrides.get("transport_timeout_seconds", socket_timeout),
        overrides.get("sink_deadline_seconds", deadline),
    )


def serverchan_endpoint(key: str) -> tuple[str, str]:
    # Keep the loader's printable-ASCII envelope, not a vendor token alphabet.
    # Reject path/query/fragment separators (including proxy-normalized backslash).
    if not isinstance(key, str) or any(
        not 33 <= ord(c) <= 126 or c in "/?#\\" for c in key
    ):
        raise ValueError("SERVERCHAN_KEY_FORMAT_INVALID")
    # Encode one path segment, including literal '%' characters. This preserves
    # the credential on URL decoding without interpreting embedded URL escapes.
    segment = quote(key, safe="")
    if key.startswith("SCT") and len(key) > 3:
        return "serverchan_turbo", "https://sctapi.ftqq.com/" + segment + ".send"
    match = re.fullmatch(r"sctp([0-9]+)t(.+)", key)
    if match:
        return "serverchan_sc3", f"https://{match[1]}.push.ft07.com/send/{segment}.send"
    raise ValueError("SERVERCHAN_KEY_FORMAT_INVALID")


def secret_value(path: str, name: str) -> str:
    from .core import external_path, private_read

    content = private_read(external_path(path)).decode("ascii")
    lines = [
        s.strip()
        for s in content.splitlines()
        if s.strip() and not s.lstrip().startswith("#")
    ]
    if len(lines) != 1 or not lines[0].startswith(name + "="):
        raise ValueError("SECRET_FILE_INVALID")
    value = lines[0].split("=", 1)[1]
    if not value or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        raise ValueError("SECRET_VALUE_INVALID")
    return value


def dingtalk_signature(secret: str, timestamp: int) -> str:
    message = f"{timestamp}\n{secret}".encode()
    return base64.b64encode(
        hmac.new(secret.encode(), message, hashlib.sha256).digest()
    ).decode()


class NotificationSink(Protocol):
    sink_id: str

    def send(self, event) -> dict: ...


def https_post(
    url: str,
    body: bytes,
    content_type: str,
    timeout: float = SOCKET_TIMEOUT_SECONDS,
) -> tuple[int, bytes]:
    timeout = _seconds(timeout, 0.1, MAX_SINK_DEADLINE_SECONDS)
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
        parts.hostname,
        parts.port,
        timeout=timeout,
        context=ssl.create_default_context(),
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
    protocol: str = field(init=False)
    budget: TransportBudget | None = None

    def __post_init__(self):
        protocol, _ = serverchan_endpoint(self.send_key)
        object.__setattr__(self, "protocol", protocol)
        if self.budget is None:
            object.__setattr__(self, "budget", serverchan_budget(protocol, {}))
        elif not isinstance(self.budget, TransportBudget):
            raise ValueError("TRANSPORT_BUDGET_INVALID")

    def send(self, event) -> dict:
        from .presentation import present

        display = present(event)
        body = urlencode(
            {
                "title": display["title"],
                "desp": display["body"],
            }
        ).encode()
        _, endpoint = serverchan_endpoint(self.send_key)
        status, content = https_post(
            endpoint,
            body,
            "application/x-www-form-urlencoded",
            self.budget.socket_timeout,
        )
        return {
            **validate_response(status, content, "code", 0),
            "protocol": self.protocol,
        }


@dataclass(frozen=True)
class DingTalkSink:
    sink_id: str
    url: str = field(repr=False)
    signing_secret: str | None = field(default=None, repr=False)
    protocol: str = field(default="dingtalk_robot", init=False)

    def send(self, event) -> dict:
        from .presentation import present

        display = present(event)
        url = self.url
        if self.signing_secret is not None:
            timestamp = int(time.time() * 1000)
            url += "&" + urlencode(
                {
                    "timestamp": timestamp,
                    "sign": dingtalk_signature(self.signing_secret, timestamp),
                }
            )
        payload = {
            "msgtype": "markdown",
            "markdown": {"title": display["title"], "text": display["body"]},
        }
        status, content = https_post(
            url, json.dumps(payload, ensure_ascii=False).encode(), "application/json"
        )
        return {
            **validate_response(status, content, "errcode", 0),
            "protocol": self.protocol,
        }


@dataclass(frozen=True)
class GenericWebhookSink:
    sink_id: str
    url: str = field(repr=False)
    success_field: str = "ok"
    success_value: str | int | bool = True
    protocol: str = field(default="generic_webhook", init=False)

    def send(self, event) -> dict:
        from .presentation import present

        status, content = https_post(
            self.url,
            json.dumps(present(event), ensure_ascii=False, sort_keys=True).encode(),
            "application/json",
        )
        return {
            **validate_response(
                status, content, self.success_field, self.success_value
            ),
            "protocol": self.protocol,
        }


def validate_response(status: int, content: bytes, field: str, expected) -> dict:
    if not 200 <= status < 300:
        return {
            "delivery_status": "FAILED",
            "failure_code": "HTTP_ERROR",
            "http_status": status,
            "service_accepted": False,
        }
    value = None
    try:
        value = json.loads(content)
        passed = (
            isinstance(value, dict)
            and type(value.get(field)) is type(expected)
            and value[field] == expected
        )
    except (ValueError, UnicodeError):
        passed = False
    result = {
        "delivery_status": "SENT" if passed else "FAILED",
        "http_status": status,
        "application_status": "PASS" if passed else "FAIL",
        "service_accepted": passed,
    }
    if not passed:
        result["failure_code"] = "APPLICATION_REJECTED"
    # Never retain provider messages/URLs; integer business codes only.
    if isinstance(value, dict) and type(value.get(field)) is int:
        result["business_code"] = value[field]
    return result


def configured_sink(row: dict) -> NotificationSink:
    from .core import identifier

    if not isinstance(row, dict) or type(row.get("enabled")) is not bool:
        raise ValueError("SINK_CONFIG_INVALID")
    if not identifier(row.get("sink_id")):
        raise ValueError("SINK_ID_REQUIRED")
    common = {"sink_id", "kind", "enabled"}
    if row.get("kind") == "serverchan":
        budget_fields = {"transport_timeout_seconds", "sink_deadline_seconds"}
        fields = set(row) - budget_fields
        if fields == common | {"send_key_env_file"}:
            # Read the existing credential, never source/eval shell text or
            # include secret values in diagnostics.
            send_key = secret_value(row["send_key_env_file"], "SERVERCHAN_SENDKEY")
        elif fields == common | {"send_key"}:
            send_key = row["send_key"]
        else:
            raise ValueError("SINK_CONFIG_INVALID")
        if not isinstance(send_key, str):
            raise ValueError("SERVERCHAN_KEY_FORMAT_INVALID")
        protocol, _ = serverchan_endpoint(send_key)
        budget = serverchan_budget(protocol, row)
        return ServerChanSink(row["sink_id"], send_key, budget)
    if row.get("kind") == "dingtalk":
        fields = common | {"webhook_env_file", "signing"}
        signing = row.get("signing")
        if signing == "HMAC_SHA256":
            fields |= {"signing_secret_env_file"}
        if signing not in {"HMAC_SHA256", "NONE"} or set(row) != fields:
            raise ValueError("DINGTALK_CONFIG_INVALID")
        url = secret_value(row["webhook_env_file"], "DINGTALK_WEBHOOK_URL")
        # Fixed service and a single token; no redirects or precomputed signatures.
        if not re.fullmatch(
            r"https://oapi\.dingtalk\.com/robot/send\?access_token=[A-Za-z0-9_-]{16,256}",
            url,
        ):
            raise ValueError("DINGTALK_WEBHOOK_INVALID")
        secret = (
            secret_value(row["signing_secret_env_file"], "DINGTALK_SIGNING_SECRET")
            if signing == "HMAC_SHA256"
            else None
        )
        if secret is not None and not re.fullmatch(r"SEC[A-Za-z0-9]{8,256}", secret):
            raise ValueError("DINGTALK_SIGNING_SECRET_INVALID")
        return DingTalkSink(row["sink_id"], url, secret)
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


def protocol_metadata(sink: NotificationSink) -> dict[str, str]:
    """Parent-owned, bounded labels only; custom test sinks may have no protocol."""
    protocol = getattr(sink, "protocol", None)
    if isinstance(protocol, str) and protocol in {
        "serverchan_turbo",
        "serverchan_sc3",
        "dingtalk_robot",
        "generic_webhook",
    }:
        return {"protocol": protocol}
    return {}


def bounded_send(
    sink: NotificationSink, event, *, timeout: float | None = None
) -> dict:
    """One short-lived child bounds DNS/TLS/read time; no daemon or retry queue."""
    if timeout is None:
        budget = getattr(sink, "budget", None)
        timeout = (
            budget.deadline
            if isinstance(budget, TransportBudget)
            else SINK_DEADLINE_SECONDS
        )
    # Explicit shorter timeouts remain available to offline tests/library callers.
    timeout = _seconds(timeout, 0.001, MAX_SINK_DEADLINE_SECONDS)
    protocol = protocol_metadata(sink)
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            try:
                outcome = sink.send(event)
            except Exception as exc:
                outcome = {
                    "delivery_status": "DELIVERY_UNKNOWN",
                    "failure_code": (
                        "CONNECTION_ERROR"
                        if isinstance(exc, (ConnectionError, socket.gaierror))
                        else "TRANSPORT_ERROR"
                    ),
                }
            os.write(write_fd, json.dumps(outcome).encode())
        finally:
            os.close(write_fd)
            os._exit(0)
    os.close(write_fd)
    try:
        failure = "DELIVERY_TIMEOUT"
        if select.select([read_fd], [], [], timeout)[0]:
            content = os.read(read_fd, 4096)
            failure = "CHILD_RESULT_INVALID"
            try:
                outcome = json.loads(content)
            except (ValueError, UnicodeError):
                outcome = None
            if isinstance(outcome, dict) and outcome.get("delivery_status") in (
                "SENT",
                "FAILED",
                "DELIVERY_UNKNOWN",
            ):
                return {**outcome, **protocol}
        return {
            "delivery_status": "DELIVERY_UNKNOWN",
            "failure_code": failure,
            **protocol,
        }
    finally:
        os.close(read_fd)
        # Only our own unreaped child; PID cannot be reused before waitpid.
        if os.waitpid(pid, os.WNOHANG)[0] == 0:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
