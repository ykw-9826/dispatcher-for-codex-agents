"""Offline real-wait budget checks; synthetic HTTPS and isolated ledgers only."""

import io
import json
import subprocess
import time
from urllib.parse import urlsplit

import pytest

from dispatcher_for_codex_agents.notifications import core, sinks
from dispatcher_for_codex_agents.notifications.cli import main


@pytest.fixture(autouse=True)
def no_process_launch(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("budget tests must not launch a model or installed CLI")

    monkeypatch.setattr(subprocess, "Popen", forbidden)


def row(sc3=False, sid="phone", **overrides):
    return {
        "sink_id": sid,
        "kind": "serverchan",
        "enabled": True,
        "send_key": "sctp123tFAKESECRET1234" if sc3 else "SCTFAKESECRET1234",
        **overrides,
    }


def event():
    return core.NotificationEvent(
        source="codex",
        kind="turn_completed",
        status="COMPLETED",
        session_id="synthetic",
        turn_id="budget-test",
    )


def config(tmp_path, rows):
    p = tmp_path / "notifications.json"
    p.write_text(
        json.dumps(
            {"version": 1, "ledger_directory": str(tmp_path / "ledger"), "sinks": rows}
        )
    )
    p.chmod(0o600)
    return p


@pytest.mark.parametrize("sc3,expected", [(False, (2.0, 2.5)), (True, (4.0, 5.0))])
def test_budget_defaults_and_socket_forwarding(sc3, expected, monkeypatch):
    sink = sinks.configured_sink(row(sc3))
    seen = []

    class Response:
        status = 200

        def read(self, limit):
            return b'{"code":0}'

    class Connection:
        def __init__(self, host, port, *, timeout, context):
            seen.append(timeout)

        def request(self, *args):
            pass

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(sinks.http.client, "HTTPSConnection", Connection)
    assert sink.send(event())["delivery_status"] == "SENT"
    assert (sink.budget.socket_timeout, sink.budget.deadline) == expected
    assert seen == [expected[0]]


@pytest.mark.parametrize("sc3", [False, True])
@pytest.mark.parametrize(
    "overrides",
    [
        {"transport_timeout_seconds": 0.1, "sink_deadline_seconds": 0.2},
        {"transport_timeout_seconds": 5, "sink_deadline_seconds": 5},
        {"sink_deadline_seconds": 5},
        {"transport_timeout_seconds": 0.4},
    ],
)
def test_valid_overrides(sc3, overrides):
    sink = sinks.configured_sink(row(sc3, **overrides))
    assert 0.1 <= sink.budget.socket_timeout <= sink.budget.deadline <= 5
    for key, value in overrides.items():
        actual = (
            sink.budget.deadline
            if key == "sink_deadline_seconds"
            else sink.budget.socket_timeout
        )
        assert actual == value


@pytest.mark.parametrize(
    "field", ["transport_timeout_seconds", "sink_deadline_seconds"]
)
@pytest.mark.parametrize(
    "bad",
    [
        None,
        True,
        False,
        "2",
        [],
        {},
        -1,
        0,
        0.01,
        5.01,
        float("nan"),
        float("inf"),
        float("-inf"),
        10**400,
    ],
)
def test_invalid_override_fails_controlled(field, bad):
    with pytest.raises(ValueError, match="^TRANSPORT_BUDGET_INVALID$"):
        sinks.configured_sink(row(**{field: bad}))


def test_socket_larger_than_outer_rejected():
    with pytest.raises(ValueError, match="TRANSPORT_BUDGET_INVALID"):
        sinks.configured_sink(
            row(transport_timeout_seconds=3, sink_deadline_seconds=2.5)
        )


@pytest.mark.parametrize("timeout", [None, True, 0, -1, float("nan"), float("inf"), 6])
def test_http_timeout_cannot_be_unbounded(timeout):
    with pytest.raises(ValueError, match="TRANSPORT_BUDGET_INVALID"):
        sinks.https_post("https://example.invalid/test", b"", "text/plain", timeout)


@pytest.mark.parametrize(
    "sc3,delay,status",
    [
        (False, 0.6, "SENT"),
        (False, 0.85, "SENT"),
        (False, 0.95, "SENT"),
        (False, 2.8, "DELIVERY_UNKNOWN"),
        (True, 2.6, "SENT"),
        (True, 3.0, "SENT"),
        (True, 5.3, "DELIVERY_UNKNOWN"),
    ],
)
def test_real_waits_default_budgets(sc3, delay, status, tmp_path, monkeypatch):
    calls = tmp_path / "calls"

    def transport(*args):
        with calls.open("a") as f:
            f.write("once\n")
        time.sleep(delay)
        return 200, b'{"code":0}'

    monkeypatch.setattr(sinks, "https_post", transport)
    sink = sinks.configured_sink(row(sc3))
    start = time.monotonic()
    result = sinks.bounded_send(sink, event())
    elapsed = time.monotonic() - start
    assert result["delivery_status"] == status
    assert result["protocol"] == sink.protocol
    assert calls.read_text() == "once\n"
    if status == "SENT":
        assert elapsed >= delay and result["service_accepted"]
    else:
        assert result["failure_code"] == "DELIVERY_TIMEOUT"
        assert elapsed >= sink.budget.deadline
    assert elapsed < sink.budget.deadline + 0.8
    assert sink.send_key not in json.dumps(result)


@pytest.mark.parametrize("scenario", ["normal", "first-timeout", "all-max-timeout"])
def test_whole_stop_hook_sequential_budget(tmp_path, monkeypatch, scenario):
    rows = [row(False, "app"), row(False, "wechat"), row(True, "sc3")]
    if scenario == "all-max-timeout":
        for r in rows:
            r["sink_deadline_seconds"] = 5
    path = config(tmp_path, rows)
    calls = tmp_path / "calls"
    delays = {
        "normal": [0.6, 0.6, 2.6],
        "first-timeout": [3, 0.05, 0.05],
        "all-max-timeout": [6, 6, 6],
    }[scenario]

    def transport(url, body, content_type, timeout):
        index = len(calls.read_text().splitlines()) if calls.exists() else 0
        with calls.open("a") as f:
            f.write(str(index) + "\n")
        # No URL/credential is persisted; only a call index is retained.
        assert (urlsplit(url).hostname.endswith("push.ft07.com")) == (index == 2)
        time.sleep(delays[index])
        return 200, b'{"code":0}'

    monkeypatch.setattr(sinks, "https_post", transport)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"session_id": "synthetic", "turn_id": "budget-test"})),
    )
    start = time.monotonic()
    assert main(["hook", "--event", "Stop", "--config", str(path)]) == 0
    elapsed = time.monotonic() - start
    assert elapsed < sinks.HOOK_TIMEOUT_SECONDS
    assert calls.read_text() == "0\n1\n2\n"
    with core.ledger(tmp_path / "ledger") as (records, _):
        finals = [r for r in records if "elapsed_seconds" in r]
    assert [r["sink_id"] for r in finals] == [r["sink_id"] for r in rows]
    assert [r["protocol"] for r in finals] == [
        "serverchan_turbo",
        "serverchan_turbo",
        "serverchan_sc3",
    ]
    expected = {
        "normal": ["SENT"] * 3,
        "first-timeout": ["DELIVERY_UNKNOWN", "SENT", "SENT"],
        "all-max-timeout": ["DELIVERY_UNKNOWN"] * 3,
    }[scenario]
    assert [r["delivery_status"] for r in finals] == expected
    # Same identity stays deduped; changing a budget never grants a retry.
    for r in rows:
        r["transport_timeout_seconds"] = 0.1
    config(tmp_path, rows)
    again = core.notify(event(), path)
    assert all(
        r["delivery_status"] == "DUPLICATE_SUPPRESSED" for r in again["sinks"].values()
    )
    assert calls.read_text() == "0\n1\n2\n"
    assert "FAKESECRET" not in (tmp_path / "ledger/delivery.jsonl").read_text()


def test_invalid_budget_sink_does_not_block_next_sink(tmp_path, monkeypatch):
    path = config(
        tmp_path,
        [row(False, "bad", sink_deadline_seconds=float("nan")), row(True, "good")],
    )
    calls = []

    def sender(sink, item):
        calls.append(sink.sink_id)
        return {"delivery_status": "SENT"}

    result = core.notify(event(), path, sender=sender)
    assert calls == ["good"]
    assert result["sinks"]["bad"]["failure_code"] == "CONFIGURATION_ERROR"
    assert result["sinks"]["bad"]["attempted"] is False
    assert result["sinks"]["good"]["protocol"] == "serverchan_sc3"


def test_hook_budget_invariant_all_legal_configurations():
    assert sinks.HOOK_TIMEOUT_SECONDS == 17
    assert 2.5 + 2.5 + 5 == 10
    assert (
        sinks.MAX_SINKS * sinks.MAX_SINK_DEADLINE_SECONDS + sinks.LOCAL_OVERHEAD_SECONDS
        <= sinks.HOOK_TIMEOUT_SECONDS
    )
