"""Offline protocol, permission, three-target and explicit migration acceptance."""

import hashlib
import io
import json
import os
import shlex
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from dispatcher_for_codex_agents.notifications import core, hooks, sinks
from dispatcher_for_codex_agents.notifications.cli import hook_event, main
from dispatcher_for_codex_agents.notifications.presentation import present


@pytest.fixture(autouse=True)
def no_model(monkeypatch, tmp_path):
    import subprocess

    def forbidden(*args, **kwargs):
        pytest.fail("notification must not start a model or CLI")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(hooks, "workspace_root", lambda: tmp_path)


def secret(tmp_path, name, value):
    path = tmp_path / (name + ".env")
    path.write_text(name + "=" + value + "\n")
    path.chmod(0o600)
    return str(path)


def settings(tmp_path):
    return [
        {
            "sink_id": "turbo",
            "kind": "serverchan",
            "enabled": True,
            "send_key_env_file": secret(
                tmp_path, "SERVERCHAN_SENDKEY", "SCTFAKESECRET1234"
            ),
        },
        {
            "sink_id": "sc3",
            "kind": "serverchan",
            "enabled": True,
            "send_key": "sctp123tFAKESECRET1234",
        },
        {
            "sink_id": "ding",
            "kind": "dingtalk",
            "enabled": True,
            "webhook_env_file": secret(
                tmp_path,
                "DINGTALK_WEBHOOK_URL",
                "https://oapi.dingtalk.com/robot/send?access_token=FAKE1234567890123456",
            ),
            "signing": "HMAC_SHA256",
            "signing_secret_env_file": secret(
                tmp_path, "DINGTALK_SIGNING_SECRET", "SECFAKE12345678"
            ),
        },
    ]


def config(tmp_path, rows=None, **extra):
    path = tmp_path / "notifications.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "ledger_directory": str(tmp_path / "ledger"),
                "sinks": settings(tmp_path) if rows is None else rows,
                **extra,
            }
        )
    )
    path.chmod(0o600)
    return path


def event():
    return core.NotificationEvent(
        source="codex",
        kind="turn_completed",
        status="COMPLETED",
        session_id="synthetic",
        turn_id="turn",
    )


def sent(*_):
    return {"delivery_status": "SENT", "http_status": 200, "application_status": "PASS"}


@pytest.mark.parametrize(
    "key,protocol,endpoint",
    [
        (
            "SCTFAKESECRET1234",
            "serverchan_turbo",
            "https://sctapi.ftqq.com/SCTFAKESECRET1234.send",
        ),
        (
            "sctp123tFAKESECRET1234",
            "serverchan_sc3",
            "https://123.push.ft07.com/send/sctp123tFAKESECRET1234.send",
        ),
    ],
)
def test_serverchan_routes(key, protocol, endpoint, monkeypatch):
    calls = []
    monkeypatch.setattr(
        sinks, "https_post", lambda *a: (calls.append(a) or (200, b'{"code":0}'))
    )
    result = sinks.ServerChanSink("test", key).send(event())
    assert calls[0][0] == endpoint
    assert parse_qs(calls[0][1].decode())["title"] == ["[DCA] Codex 本轮结束"]
    assert result["service_accepted"] and result["protocol"] == protocol
    assert key not in json.dumps(result)
    assert key not in repr(sinks.ServerChanSink("test", key))


@pytest.mark.parametrize(
    "key",
    [
        "sctp",
        "sctpttoken",
        "sctp123x12345678",
        "sctp123t",
        "sctp123tabc/path",
        "sctp123t" + "ABC12345?secret",
        "SCT",
        "unknown12345678",
        "sctp١٢٣tFAKE12345678",
    ],
)
def test_unknown_keys_rejected(key):
    with pytest.raises(ValueError, match="KEY_FORMAT"):
        sinks.serverchan_endpoint(key)


@pytest.mark.parametrize(
    "prefix,protocol,host,path_prefix",
    [
        ("SCT", "serverchan_turbo", "sctapi.ftqq.com", "/"),
        ("sctp123t", "serverchan_sc3", "123.push.ft07.com", "/send/"),
    ],
)
@pytest.mark.parametrize(
    "token",
    [
        "Plain123",
        "a",
        "safe_token-with-punctuation",
        "x" * 1024,
        "!$&'()*+,-.:;=@[]^_`{|}~%",
        "%2F%3F%23%0D%0A",
    ],
)
def test_serverchan_safe_tokens_preserve_identity(
    prefix, protocol, host, path_prefix, token, monkeypatch
):
    key = prefix + token
    sink = sinks.configured_sink(
        {"sink_id": "synthetic", "kind": "serverchan", "enabled": True, "send_key": key}
    )
    actual_protocol, endpoint = sinks.serverchan_endpoint(key)
    parsed = urlsplit(endpoint)
    assert actual_protocol == sink.protocol == protocol
    assert parsed.scheme == "https" and parsed.netloc == host
    assert not parsed.query and not parsed.fragment and not parsed.username
    assert parsed.path.startswith(path_prefix) and parsed.path.endswith(".send")
    assert unquote(parsed.path[len(path_prefix) : -5]) == key
    assert sink.send_key == key  # no trimming, case folding, or token rewriting
    assert key not in repr(sink)
    calls = []
    monkeypatch.setattr(
        sinks, "https_post", lambda *a: (calls.append(a) or (200, b'{"code":0}'))
    )
    result = sink.send(event())
    assert calls[0][0] == endpoint
    assert result["protocol"] == protocol and result["service_accepted"]
    assert key not in json.dumps(result) and endpoint not in json.dumps(result)
    assert sinks.protocol_metadata(sink) == {"protocol": protocol}


@pytest.mark.parametrize("uid", ["0", "00123", "123", "123456789012345678901"])
def test_serverchan_uid_is_ascii_digits_without_local_numeric_assumptions(uid):
    protocol, endpoint = sinks.serverchan_endpoint("sctp" + uid + "t_")
    assert protocol == "serverchan_sc3"
    assert urlsplit(endpoint).netloc == uid + ".push.ft07.com"


@pytest.mark.parametrize("prefix", ["SCT", "sctp123t"])
@pytest.mark.parametrize(
    "unsafe",
    [
        " ",
        "\t",
        "\r",
        "\n",
        "\x00",
        "\x1f",
        "\x7f",
        "\x85",
        "\u00a0",
        "/",
        "?",
        "#",
        "\\",
    ],
)
def test_serverchan_unsafe_tokens_rejected_without_echo(prefix, unsafe):
    key = prefix + "synthetic" + unsafe + "token"
    with pytest.raises(ValueError) as failure:
        sinks.serverchan_endpoint(key)
    assert str(failure.value) == "SERVERCHAN_KEY_FORMAT_INVALID"
    assert key not in str(failure.value)


@pytest.mark.parametrize(
    "key",
    [
        "",
        None,
        123,
        "sctToken",
        "Sctp123tToken",
        "sctp-1tToken",
        "sctp123Token",
        "sctp123TToken",
        "sctp123",
        "sctp١٢٣tToken",
    ],
)
def test_serverchan_prefix_and_uid_fail_closed(key):
    with pytest.raises(ValueError, match="^SERVERCHAN_KEY_FORMAT_INVALID$"):
        sinks.serverchan_endpoint(key)


@pytest.mark.parametrize(
    "status,body,passed,code",
    [
        (200, b'{"code":0}', True, None),
        (200, b'{"code":7,"message":"PRIVATE_URL"}', False, "APPLICATION_REJECTED"),
        (200, b'{"code":false}', False, "APPLICATION_REJECTED"),
        (200, b'{"code":"0"}', False, "APPLICATION_REJECTED"),
        (200, b"not json PRIVATE", False, "APPLICATION_REJECTED"),
        (429, b"PRIVATE", False, "HTTP_ERROR"),
        (302, b"PRIVATE", False, "HTTP_ERROR"),
    ],
)
def test_business_response_whitelist(status, body, passed, code):
    result = sinks.validate_response(status, body, "code", 0)
    assert (result["delivery_status"] == "SENT") == passed
    assert result.get("failure_code") == code
    assert "PRIVATE" not in json.dumps(result)


def test_dingtalk_signature_and_payload(tmp_path, monkeypatch):
    import base64
    import hmac

    row = settings(tmp_path)[2]
    sink = sinks.configured_sink(row)
    stamp = 1700000000000
    monkeypatch.setattr(sinks.time, "time", lambda: stamp / 1000)
    calls = []
    monkeypatch.setattr(
        sinks, "https_post", lambda *a: (calls.append(a) or (200, b'{"errcode":0}'))
    )
    result = sink.send(event())
    query = parse_qs(urlsplit(calls[0][0]).query)
    expected = base64.b64encode(
        hmac.digest(b"SECFAKE12345678", b"1700000000000\nSECFAKE12345678", "sha256")
    ).decode()
    assert query["sign"] == [expected] and query["timestamp"] == [str(stamp)]
    payload = json.loads(calls[0][1])
    assert payload == {
        "msgtype": "markdown",
        "markdown": {"title": "[DCA] Codex 本轮结束", "text": present(event())["body"]},
    }
    assert result["service_accepted"] and result["protocol"] == "dingtalk_robot"
    assert "FAKE" not in repr(sink) + json.dumps(result)


def test_dingtalk_unsigned_explicit_and_business_failure(tmp_path, monkeypatch):
    row = settings(tmp_path)[2]
    row["signing"] = "NONE"
    del row["signing_secret_env_file"]
    calls = []
    monkeypatch.setattr(
        sinks,
        "https_post",
        lambda *a: (
            calls.append(a) or (200, b'{"errcode":310000,"errmsg":"PRIVATE_TOKEN"}')
        ),
    )
    result = sinks.configured_sink(row).send(event())
    assert "sign=" not in calls[0][0] and "timestamp=" not in calls[0][0]
    assert result["delivery_status"] == "FAILED" and result["business_code"] == 310000
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize(
    "fault", ["missing", "format", "mode", "fifo", "url", "signing", "unknown"]
)
def test_sink_local_config_faults_continue(tmp_path, fault):
    rows = settings(tmp_path)
    if fault == "missing":
        Path(rows[0]["send_key_env_file"]).unlink()
    elif fault == "format":
        Path(rows[0]["send_key_env_file"]).write_text("WRONG=PRIVATE\n")
    elif fault == "mode":
        Path(rows[0]["send_key_env_file"]).chmod(0o644)
    elif fault == "fifo":
        path = Path(rows[0]["send_key_env_file"])
        path.unlink()
        os.mkfifo(path, mode=0o600)
    elif fault == "url":
        Path(rows[2]["webhook_env_file"]).write_text(
            "DINGTALK_WEBHOOK_URL=https://evil.invalid/PRIVATE\n"
        )
    elif fault == "signing":
        rows[2]["signing"] = "UNKNOWN"
    else:
        rows[0]["kind"] = "unknown"
    path = config(tmp_path, rows)
    calls = []
    result = core.notify(
        event(), path, sender=lambda s, e: (calls.append(s.sink_id) or sent())
    )
    failed = "ding" if fault in {"url", "signing"} else "turbo"
    assert result["sinks"][failed]["delivery_status"] == "NOT_ATTEMPTED"
    assert result["sinks"][failed]["attempted"] is False
    assert len(calls) == 2 and failed not in calls
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize(
    "fault", ["success", "first_failed", "failed", "timeout", "exception"]
)
def test_three_target_independence_and_dedupe(tmp_path, fault):
    path = config(tmp_path)
    calls = []

    def send(sink, item):
        calls.append(sink.sink_id)
        if sink.sink_id == "turbo" and fault == "first_failed":
            return {"delivery_status": "FAILED"}
        if sink.sink_id == "sc3":
            if fault == "exception":
                raise OSError("PRIVATE_URL")
            if fault not in {"success", "first_failed"}:
                return {
                    "delivery_status": (
                        "FAILED" if fault == "failed" else "DELIVERY_UNKNOWN"
                    )
                }
        return sent()

    result = core.notify(event(), path, sender=send)
    assert calls == ["turbo", "sc3", "ding"]
    assert result["sinks"]["ding"]["delivery_status"] == "SENT"
    if fault == "first_failed":
        assert result["sinks"]["turbo"]["delivery_status"] == "FAILED"
        assert result["sinks"]["sc3"]["delivery_status"] == "SENT"
    else:
        assert result["sinks"]["turbo"]["delivery_status"] == "SENT"
    duplicate = core.notify(event(), path, sender=lambda *_: pytest.fail("replay"))
    assert all(
        r["delivery_status"] == "DUPLICATE_SUPPRESSED"
        for r in duplicate["sinks"].values()
    )
    logged = (tmp_path / "ledger/delivery.jsonl").read_text()
    assert all(
        x not in logged for x in ("FAKE", "PRIVATE_URL", "access_token=", "sign=")
    )


@pytest.mark.parametrize(
    "fault", ["duplicate", "four", "bad_root", "policy", "version"]
)
def test_global_fail_closed(tmp_path, fault):
    rows = settings(tmp_path)
    if fault == "duplicate":
        rows[1]["sink_id"] = rows[0]["sink_id"]
    elif fault == "four":
        rows.append(dict(rows[0], sink_id="four"))
    path = config(tmp_path, rows)
    if fault == "bad_root":
        path.write_text("[]")
    elif fault == "policy":
        cfg = json.loads(path.read_text())
        cfg["permission_notification_policy"] = "HUMAN_ACTION_ONLY"
        path.write_text(json.dumps(cfg))
    elif fault == "version":
        cfg = json.loads(path.read_text())
        cfg["version"] = True
        path.write_text(json.dumps(cfg))
    result = core.notify(
        event(), path, sender=lambda *_: pytest.fail("must fail closed")
    )
    assert result["status"] == "NOTIFICATION_NONBLOCKING_ERROR"
    assert not (tmp_path / "ledger").exists()


def test_old_ledger_same_identity_and_integrity(tmp_path):
    path = config(tmp_path)
    with core.ledger(tmp_path / "ledger") as (_, append):
        payload = event().payload()
        del payload["result_approval_claimed"]
        payload["scientific_success_claimed"] = False
        append(
            {
                "event_id": event().event_id,
                "sink_id": "turbo",
                "event": payload,
                "delivery_status": "SENT",
                "recorded_at": 1,
            }
        )
    original = (tmp_path / "ledger/delivery.jsonl").read_bytes()
    calls = []
    result = core.notify(
        event(), path, sender=lambda s, e: (calls.append(s.sink_id) or sent())
    )
    assert calls == ["sc3", "ding"]
    assert result["sinks"]["turbo"]["delivery_status"] == "DUPLICATE_SUPPRESSED"
    assert (tmp_path / "ledger/delivery.jsonl").read_bytes().startswith(original)
    with (tmp_path / "ledger/delivery.jsonl").open("a") as f:
        f.write("[]\n")
    assert (
        core.notify(event(), path, sender=lambda *_: pytest.fail("corrupt ledger"))[
            "status"
        ]
        == "NOTIFICATION_NONBLOCKING_ERROR"
    )


@pytest.mark.parametrize("policy", [None, "OFF", "REQUEST_OBSERVED"])
def test_permission_neutral_off_and_no_raw_content(
    tmp_path, monkeypatch, capsys, policy
):
    path = config(
        tmp_path,
        **({} if policy is None else {"permission_notification_policy": policy}),
    )
    calls = []
    monkeypatch.setattr(
        sinks, "bounded_send", lambda sink, e: (calls.append(e) or sent())
    )
    raw = {
        "session_id": "synthetic",
        "turn_id": "turn",
        "tool_name": "Bash",
        "tool_input": {
            "command": "PRIVATE_COMMAND",
            "description": "PRIVATE_DESCRIPTION",
        },
        "transcript_path": "/PRIVATE_TRANSCRIPT",
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(raw)))
    assert main(["hook", "--event", "PermissionRequest", "--config", str(path)]) == 0
    assert capsys.readouterr().out == ""  # no allow/deny override
    assert len(calls) == (3 if policy == "REQUEST_OBSERVED" else 0)
    text = (tmp_path / "ledger/delivery.jsonl").read_text()
    assert "PRIVATE" not in text and "NOT_ESTABLISHED" in text and "shell" in text
    for e in calls:
        shown = present(e)
        assert shown["title"] == "[DCA] 权限请求"
        assert all(
            x not in json.dumps(shown, ensure_ascii=False)
            for x in ("需要人工操作", "等待用户批准", "必须处理", "PRIVATE")
        )


def test_two_permission_observations_not_collapsed(tmp_path, monkeypatch):
    path = config(tmp_path, permission_notification_policy="REQUEST_OBSERVED")
    seen = []
    monkeypatch.setattr(
        "dispatcher_for_codex_agents.notifications.cli.notify",
        lambda e, _: seen.append(e),
    )
    for _ in range(2):
        hook_event("PermissionRequest", {"session_id": "s", "turn_id": "t"}, str(path))
    assert seen[0].event_id != seen[1].event_id
    assert all(e.kind == "permission_request_observed" for e in seen)


def test_permission_off_does_not_load_sink_secrets(tmp_path, monkeypatch):
    path = config(tmp_path)
    monkeypatch.setattr(
        sinks, "secret_value", lambda *_: pytest.fail("credential read")
    )
    monkeypatch.setattr(
        sinks, "bounded_send", lambda *_: pytest.fail("external request")
    )
    result = hook_event("PermissionRequest", {"session_id": "synthetic"}, str(path))
    assert result["status"] == "PERMISSION_NOTIFICATION_OFF"


def test_all_presentations_current_branding():
    from dispatcher_for_codex_agents.notifications.presentation import TITLES

    for kind in TITLES:
        e = core.NotificationEvent(
            source="test",
            kind=kind,
            status="OBSERVED" if kind == "permission_request_observed" else "TEST",
            run_id="synthetic",
        )
        shown = present(e)
        assert shown["title"].startswith("[DCA]")
        assert "denovo" not in json.dumps(shown, ensure_ascii=False).lower()


class SlowSink:
    def __init__(self, delay, failed=False):
        self.delay, self.failed = delay, failed

    def send(self, _):
        time.sleep(self.delay)
        return {"delivery_status": "FAILED" if self.failed else "SENT"}


@pytest.mark.parametrize(
    "error,code",
    [(ConnectionRefusedError, "CONNECTION_ERROR"), (TimeoutError, "TRANSPORT_ERROR")],
)
def test_bounded_transport_error_categories(error, code):
    class BrokenSink:
        def send(self, _):
            raise error("PRIVATE_URL_AND_TOKEN")

    result = sinks.bounded_send(BrokenSink(), event())
    assert result["delivery_status"] == "DELIVERY_UNKNOWN"
    assert result["failure_code"] == code
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize(
    "delays,failed",
    [([0], []), ([0, 0, 0], []), ([2, 0, 0], []), ([0, 0, 0], [0, 1]), ([2, 2, 2], [])],
)
def test_sequential_budget_including_config_ledger(
    tmp_path, monkeypatch, delays, failed
):
    rows = settings(tmp_path)[: len(delays)]
    path = config(tmp_path, rows)
    # Include a synthetic existing ledger without reading production history.
    with core.ledger(tmp_path / "ledger") as (_, append):
        for i in range(100):
            append({"context": True, "session_id": "old", "turn_id": str(i)})
    mapping = {
        r["sink_id"]: SlowSink(delays[i], i in failed) for i, r in enumerate(rows)
    }
    monkeypatch.setattr(sinks, "configured_sink", lambda row: mapping[row["sink_id"]])
    start = time.monotonic()
    core.load_config(path)
    config_elapsed = time.monotonic() - start
    start = time.monotonic()
    with core.ledger(tmp_path / "ledger") as (logged, _):
        assert len(logged) == 100
    ledger_elapsed = time.monotonic() - start
    start = time.monotonic()
    result = core.notify(event(), path)
    elapsed = time.monotonic() - start
    assert elapsed + config_elapsed < sinks.HOOK_TIMEOUT_SECONDS
    expected = [
        "DELIVERY_UNKNOWN" if d else "FAILED" if i in failed else "SENT"
        for i, d in enumerate(delays)
    ]
    assert [r["delivery_status"] for r in result["sinks"].values()] == expected
    print(
        json.dumps(
            {
                "config_seconds": config_elapsed,
                "ledger_seconds": ledger_elapsed,
                "fanout_seconds": elapsed,
                "outcomes": expected,
            }
        )
    )


def release(tmp_path, commit, *, legacy=False, version="1.0.3"):
    venv = tmp_path / "releases" / commit / "venv"
    bin_path = venv / "bin"
    bin_path.mkdir(parents=True)
    (bin_path / "python").write_text("test-only, never executed")
    name = "b2m-notify" if legacy else "dca-notify"
    ns = "denovo_codex_agent_tool" if legacy else "dispatcher_for_codex_agents"
    distribution = ns.replace("_", "-")
    exe = bin_path / name
    exe.write_text(
        f"#!{bin_path}/python\nimport sys\n"
        f"from {ns}.notifications.cli import main\nsys.exit(main())\n"
    )
    exe.chmod(0o700)
    meta = venv / f"lib/python3.11/site-packages/{ns}-{version}.dist-info"
    meta.mkdir(parents=True)
    (meta / "METADATA").write_text(f"Name: {distribution}\nVersion: {version}\n")
    (meta / "entry_points.txt").write_text(
        f"[console_scripts]\n{name} = {ns}.notifications.cli:main\n"
    )
    return str(exe)


def migration_fixture(tmp_path):
    old = release(tmp_path, "a" * 40, legacy=True, version="1.0.0")
    current = release(tmp_path, "b" * 40, version="1.0.2")
    new = release(tmp_path, "c" * 40)
    home = tmp_path / "codex"
    home.mkdir()
    base = home / "config.toml"
    base.write_text('model="unchanged"\n[trust]\nsynthetic="unchanged"\n')
    cfg = config(tmp_path)
    with core.ledger(tmp_path / "ledger") as (_, append):
        append({"context": True, "session_id": "historical"})
    doc = {"hooks": {}}
    for event_name in ("UserPromptSubmit", "Stop", "PermissionRequest"):
        doc["hooks"][event_name] = [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": shlex.join(
                            [
                                current if event_name == "Stop" else old,
                                "hook",
                                "--event",
                                event_name,
                                "--config",
                                str(cfg),
                            ]
                        ),
                        "timeout": 3,
                        "statusMessage": hooks.RETIRED_MARKER,
                    }
                ]
            }
        ]
    doc["hooks"]["Stop"].append({"hooks": [{"type": "command", "command": "true"}]})
    target = home / "hooks.json"
    target.write_text(json.dumps(doc))
    target.chmod(0o600)
    return home, cfg, target, (old, current), new


def test_explicit_migration_backup_noop_rollback(tmp_path):
    home, cfg, target, old, new = migration_fixture(tmp_path)
    protected = [cfg, home / "config.toml", tmp_path / "ledger/delivery.jsonl"]
    before = [p.read_bytes() for p in protected]
    original = target.read_bytes()
    args = (str(home), new, str(cfg))
    preview = hooks.migrate_hooks(*args, from_executables=old)
    assert len(preview["changes"]) == 3 and target.read_bytes() == original
    with pytest.raises(ValueError, match="PREVIEW_HASH"):
        hooks.migrate_hooks(*args, from_executables=old, apply=True)
    result = hooks.migrate_hooks(
        *args,
        from_executables=old,
        expected_sha256=preview["previous_sha256"],
        apply=True,
    )
    receipt = json.loads(Path(result["receipt"]).read_text())
    assert Path(receipt["backup"]).read_bytes() == original
    assert [p.read_bytes() for p in protected] == before
    assert (
        json.loads(target.read_text())["hooks"]["Stop"][1]["hooks"][0]["command"]
        == "true"
    )
    after = target.read_bytes()
    assert not hooks.migrate_hooks(*args, from_executables=old, apply=True)["changed"]
    assert target.read_bytes() == after
    hooks.rollback_hooks(result["receipt"], apply=True)
    assert target.read_bytes() == original


def test_migration_cli_preview_only(tmp_path, capsys):
    home, cfg, target, old, new = migration_fixture(tmp_path)
    original = target.read_bytes()
    args = [
        "hooks-migrate",
        "--codex-home",
        str(home),
        "--config",
        str(cfg),
        "--executable",
        new,
    ]
    for source in old:
        args += ["--from-executable", source]
    assert main(args) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["status"] == "HOOK_MIGRATION_PREVIEW"
    assert len(preview["changes"]) == 3
    assert target.read_bytes() == original


@pytest.mark.parametrize(
    "fault",
    [
        "unknown_executable",
        "duplicate",
        "matcher",
        "extra_arg",
        "target_version",
        "metadata",
        "symlink",
        "stale_hash",
    ],
)
def test_migration_fail_closed(tmp_path, fault):
    home, cfg, target, old, new = migration_fixture(tmp_path)
    doc = json.loads(target.read_text())
    stop = doc["hooks"]["Stop"][0]["hooks"][0]
    if fault == "unknown_executable":
        stop["command"] = stop["command"].replace(old[1], "/unknown/dca-notify")
    elif fault == "duplicate":
        doc["hooks"]["Stop"].append(doc["hooks"]["Stop"][0])
    elif fault == "matcher":
        doc["hooks"]["Stop"][0]["matcher"] = "*"
    elif fault == "extra_arg":
        stop["command"] += " --unknown"
    elif fault in ("target_version", "metadata"):
        p = next(
            (Path(new).parents[1] / "lib").glob(
                "python*/site-packages/*.dist-info/METADATA"
            )
        )
        p.write_text(
            "Name: dispatcher-for-codex-agents\nVersion: 1.0.2\n"
            if fault == "target_version"
            else "Name: malicious\nVersion: 1.0.3\n"
        )
    elif fault == "symlink":
        link = tmp_path / "alias"
        link.symlink_to(new)
        new = str(link)
    target.write_text(json.dumps(doc))
    original = target.read_bytes()
    with pytest.raises(ValueError):
        hooks.migrate_hooks(
            str(home),
            new,
            str(cfg),
            from_executables=old,
            apply=True,
            expected_sha256=(
                "0" * 64
                if fault == "stale_hash"
                else hashlib.sha256(original).hexdigest()
            ),
        )
    assert target.read_bytes() == original


def test_hook_budget_and_permission_invariant():
    assert sinks.MAX_SINKS == 3
    assert (
        sinks.HOOK_TIMEOUT_SECONDS - sinks.MAX_SINKS * sinks.SINK_DEADLINE_SECONDS >= 1
    )
    with pytest.raises(ValueError, match="OBSERVATION_ONLY"):
        core.NotificationEvent(
            source="codex",
            kind="permission_request_observed",
            status="REQUIRED",
            session_id="test",
        )


def historical_delivery(item, sink_id="turbo"):
    return {
        "event_id": item.event_id,
        "event": item.payload(),
        "sink_id": sink_id,
        "delivery_status": "SENT",
        "recorded_at": 1,
    }


@pytest.mark.parametrize("duplicate", [False, True], ids=["fresh-3-sink", "duplicate"])
@pytest.mark.parametrize(
    "damage",
    [
        "malformed",
        "wrong-hash",
        "uppercase",
        "nested-mismatch",
        "equal-but-wrong-identity",
        "missing-event",
        "invalid-event",
        "missing-source",
        "missing-outer",
        "invalid-identity-field",
    ],
)
def test_f1_corrupt_history_globally_blocks_before_construction(
    tmp_path, monkeypatch, duplicate, damage
):
    cfg = config(tmp_path)
    item = event()
    bad = historical_delivery(item)
    if damage == "malformed":
        bad["event_id"] = "not-a-valid-hash"
    elif damage == "wrong-hash":
        bad["event_id"] = "0" * 64
        del bad["event"]["event_id"]
    elif damage == "uppercase":
        bad["event_id"] = bad["event_id"].upper()
    elif damage == "nested-mismatch":
        bad["event"]["event_id"] = "0" * 64
    elif damage == "equal-but-wrong-identity":
        bad["event"]["turn_id"] = "another-turn"
    elif damage == "missing-event":
        del bad["event"]
    elif damage == "invalid-event":
        bad["event"] = None
    elif damage == "missing-source":
        del bad["event"]["source"]
    elif damage == "missing-outer":
        del bad["event_id"]
    elif damage == "invalid-identity-field":
        bad["event"]["session_id"] = {"PRIVATE": "content"}
    rows = [bad]
    if duplicate:
        rows.extend(historical_delivery(item, s) for s in ("turbo", "sc3", "ding"))
    directory = tmp_path / "ledger"
    directory.mkdir(mode=0o700)
    path = directory / "delivery.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    path.chmod(0o600)
    original = path.read_bytes()
    constructions, sends = [], []
    monkeypatch.setattr(sinks, "configured_sink", lambda row: constructions.append(row))
    result = core.notify(item, cfg, sender=lambda *args: sends.append(args))
    assert result == {
        "status": "NOTIFICATION_NONBLOCKING_ERROR",
        "failure_code": "LEDGER_IDENTITY_CORRUPTION",
        "model_calls": 0,
    }
    assert constructions == sends == []
    assert path.read_bytes() == original  # Not even an observation/reservation.
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize("version", ["1.0.0", "1.0.1", "1.0.2", "1.0.3"])
@pytest.mark.parametrize("nested_id", [True, False])
def test_f1_supported_history_and_per_sink_dedupe(tmp_path, version, nested_id):
    cfg = config(tmp_path)
    item = event()
    delivery = historical_delivery(item)
    if version == "1.0.0":
        del delivery["event"]["result_approval_claimed"]
        delivery["event"]["scientific_success_claimed"] = False
    if not nested_id:
        del delivery["event"]["event_id"]
    # Context and observations never enter dedupe; do not invent event identities.
    with core.ledger(tmp_path / "ledger") as (_, append):
        append({"context": True, "session_id": "historical"})
        append({"observed": True, "event": item.payload(), "recorded_at": 1})
        append({"permission_observation": True, "observation_id": "synthetic"})
        append(delivery)
    original = (tmp_path / "ledger/delivery.jsonl").read_bytes()
    calls = []

    def send(sink, _):
        calls.append(sink.sink_id)
        return sent()

    result = core.notify(item, cfg, sender=send)
    assert result["sinks"]["turbo"]["delivery_status"] == "DUPLICATE_SUPPRESSED"
    assert calls == ["sc3", "ding"]
    again = core.notify(item, cfg, sender=send)
    assert calls == ["sc3", "ding"]
    assert all(
        r["delivery_status"] == "DUPLICATE_SUPPRESSED" for r in again["sinks"].values()
    )
    assert (tmp_path / "ledger/delivery.jsonl").read_bytes().startswith(original)


WRAPPER_IMPORT = "from dispatcher_for_codex_agents.notifications.cli import main\n"
UV_WRAPPER = (
    'if __name__ == "__main__":\n'
    '    if sys.argv[0].endswith("-script.pyw"):\n'
    "        sys.argv[0] = sys.argv[0][:-11]\n"
    '    elif sys.argv[0].endswith(".exe"):\n'
    "        sys.argv[0] = sys.argv[0][:-4]\n"
    "    sys.exit(main())\n"
)


@pytest.mark.parametrize("wrapper", ["uv", "pip", "pip-pyw", "plain", "system-exit"])
def test_f2_supported_wrapper_structures(tmp_path, wrapper):
    exe = Path(release(tmp_path, "c" * 40))
    prefix = "import sys\n"
    if wrapper == "uv":
        body = WRAPPER_IMPORT + UV_WRAPPER
    elif wrapper.startswith("pip"):
        pattern = (
            r"(-script\.pyw?|\.exe)?$"
            if wrapper == "pip"
            else r"(-script\.pyw|\.exe)?$"
        )
        body = (
            "import re\n" + WRAPPER_IMPORT + 'if __name__ == "__main__":\n'
            f"    sys.argv[0] = re.sub({pattern!r}, '', sys.argv[0])\n"
            "    sys.exit(main())\n"
        )
    else:
        body = WRAPPER_IMPORT + (
            "raise SystemExit(main())\n"
            if wrapper == "system-exit"
            else "sys.exit(main())\n"
        )
    # Standard uv uses the venv's python3 spelling; no candidate is executed.
    (exe.parent / "python3").write_text("never executed")
    exe.write_text(f"#!{exe.parent}/python3\n# -*- coding: utf-8 -*-\n" + prefix + body)
    identity = hooks._release_executable(str(exe), destination=True)
    assert identity["version"] == "1.0.3"
    assert identity["sha256"] == hashlib.sha256(exe.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "fault",
    [
        "comments",
        "literal",
        "exit-zero",
        "extra-call",
        "wrong-module",
        "shadow-main",
        "extra-import",
        "extra-branch",
        "wrong-slice",
        "wrong-guard",
        "dead-call",
        "syntax",
        "encoding",
        "bom",
        "shebang",
        "distribution",
        "version",
        "entrypoint",
        "canonical-path",
    ],
)
def test_f2_launcher_rejection_has_no_migration_side_effects(tmp_path, fault):
    home, cfg, target, old, new = migration_fixture(tmp_path)
    exe = Path(new)
    header = f"#!{exe.parent}/python\n"
    body = "import sys\n" + WRAPPER_IMPORT + "sys.exit(main())\n"
    if fault == "comments":
        body = "# " + WRAPPER_IMPORT + "# sys.exit(main())\nraise SystemExit(0)\n"
    elif fault == "literal":
        body = repr(WRAPPER_IMPORT + "sys.exit(main())") + "\nraise SystemExit(0)\n"
    elif fault == "exit-zero":
        body = body.replace("sys.exit(main())", "raise SystemExit(0)")
    elif fault == "extra-call":
        body += 'print("unapproved")\n'
    elif fault == "wrong-module":
        body = body.replace("notifications.cli", "other.cli")
    elif fault == "shadow-main":
        body = body.replace("sys.exit", "main = lambda: 0\nsys.exit")
    elif fault == "extra-import":
        body = "import os\n" + body
    elif fault == "extra-branch":
        body = (
            "import sys\n" + WRAPPER_IMPORT + UV_WRAPPER + 'else:\n    print("extra")\n'
        )
    elif fault == "wrong-slice":
        body = "import sys\n" + WRAPPER_IMPORT + UV_WRAPPER.replace(":-11", ":-1")
    elif fault == "wrong-guard":
        body = (
            "import sys\n"
            + WRAPPER_IMPORT
            + UV_WRAPPER.replace('"__main__"', '"never"')
        )
    elif fault == "dead-call":
        body = "import sys\n" + WRAPPER_IMPORT + "if False:\n    sys.exit(main())\n"
    elif fault == "syntax":
        body += "invalid!\n"
    elif fault == "encoding":
        body = "# coding: utf-7\n" + body
    elif fault == "shebang":
        header = "#!/usr/bin/python3\n"
    elif fault in {"distribution", "version", "entrypoint"}:
        meta = next((exe.parents[1] / "lib").glob("python*/site-packages/*.dist-info"))
        if fault == "entrypoint":
            (meta / "entry_points.txt").write_text(
                "[console_scripts]\ndca-notify = wrong:main\n"
            )
        else:
            (meta / "METADATA").write_text(
                "Name: "
                + (
                    "wrong"
                    if fault == "distribution"
                    else "dispatcher-for-codex-agents"
                )
                + "\nVersion: "
                + ("1.0.2" if fault == "version" else "1.0.3")
                + "\n"
            )
    elif fault == "canonical-path":
        alias = tmp_path / "alias"
        alias.symlink_to(exe)
        new = str(alias)
    exe.write_text(header + body)
    if fault == "bom":
        exe.write_bytes(b"\xef\xbb\xbf" + exe.read_bytes())
    before = {
        str(p.relative_to(tmp_path)): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    with pytest.raises(ValueError):
        hooks.migrate_hooks(
            str(home),
            new,
            str(cfg),
            from_executables=old,
            apply=True,
            expected_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
        )
    after = {
        str(p.relative_to(tmp_path)): p.read_bytes()
        for p in tmp_path.rglob("*")
        if p.is_file()
    }
    assert before == after  # Includes hooks, trust/base config, config, ledger.
    assert not (tmp_path / "runtime").exists()  # No backup/receipt/installer lock.


@pytest.mark.parametrize(
    "key,protocol",
    [
        ("SCTFAKESECRET1234", "serverchan_turbo"),
        ("sctp123tFAKESECRET1234", "serverchan_sc3"),
    ],
)
@pytest.mark.parametrize(
    "outcome,code",
    [
        ("success", None),
        ("application", "APPLICATION_REJECTED"),
        ("connection", "CONNECTION_ERROR"),
        ("transport", "TRANSPORT_ERROR"),
        ("timeout", "DELIVERY_TIMEOUT"),
        ("child-exit", "CHILD_RESULT_INVALID"),
        ("child-result", "CHILD_RESULT_INVALID"),
    ],
)
def test_f3_parent_protocol_all_bounded_outcomes(
    key, protocol, outcome, code, monkeypatch
):
    def transport(*_):
        if outcome == "connection":
            raise ConnectionRefusedError(key)
        if outcome == "transport":
            raise RuntimeError("https://secret.invalid/" + key)
        if outcome == "timeout":
            time.sleep(1)
        if outcome == "child-exit":
            os._exit(9)
        return 200, (b'{"code":7}' if outcome == "application" else b'{"code":0}')

    monkeypatch.setattr(sinks, "https_post", transport)
    if outcome == "child-result":
        monkeypatch.setattr(sinks.ServerChanSink, "send", lambda *_: [key])
    sink = sinks.ServerChanSink("phone", key)
    assert sink.protocol == protocol  # Determined before fork or network.
    result = sinks.bounded_send(sink, event(), timeout=0.05)
    assert result["protocol"] == protocol
    assert result.get("failure_code") == code
    assert result["delivery_status"] == (
        "SENT"
        if outcome == "success"
        else "FAILED" if outcome == "application" else "DELIVERY_UNKNOWN"
    )
    assert key not in json.dumps(result)
    assert "https://" not in json.dumps(result)


@pytest.mark.parametrize("outcome", ["success", "exception", "child-exit", "timeout"])
def test_f3_reservation_and_final_keep_parent_protocol(tmp_path, monkeypatch, outcome):
    cfg = config(tmp_path)

    def transport(*_):
        if outcome == "timeout":
            time.sleep(1)
        if outcome == "child-exit":
            os._exit(8)
        return 200, b'{"code":0,"errcode":0}'

    monkeypatch.setattr(sinks, "https_post", transport)

    def sender(sink, item):
        if outcome == "exception":
            raise RuntimeError("PRIVATE_URL_AND_TOKEN")
        return sinks.bounded_send(sink, item, timeout=0.05)

    result = core.notify(event(), cfg, sender=sender)
    protocols = {
        "turbo": "serverchan_turbo",
        "sc3": "serverchan_sc3",
        "ding": "dingtalk_robot",
    }
    with core.ledger(tmp_path / "ledger") as (rows, _):
        for name, protocol in protocols.items():
            delivery = [row for row in rows if row.get("sink_id") == name]
            assert len(delivery) == 3
            assert delivery[0]["failure_code"] == "RESERVED"
            assert "protocol" not in delivery[0]  # Sink not constructed yet.
            assert delivery[1]["protocol"] == delivery[2]["protocol"] == protocol
            assert result["sinks"][name]["protocol"] == protocol
        serialized = json.dumps([result, rows])
    assert all(s not in serialized for s in ("FAKE", "https://", "PRIVATE_URL"))


@pytest.mark.parametrize("failure", [False, True])
def test_f3_other_protocols_and_parent_authority(tmp_path, monkeypatch, failure):
    ding = sinks.configured_sink(settings(tmp_path)[2])
    web = sinks.GenericWebhookSink("web", "https://example.invalid/hook")

    def send(*_):
        if failure:
            raise ConnectionRefusedError("PRIVATE_URL")
        return 200, b'{"ok":true,"errcode":0}'

    monkeypatch.setattr(sinks, "https_post", send)
    for sink, protocol in ((ding, "dingtalk_robot"), (web, "generic_webhook")):
        result = sinks.bounded_send(sink, event())
        assert result["protocol"] == protocol
        assert result["delivery_status"] == ("DELIVERY_UNKNOWN" if failure else "SENT")
    monkeypatch.setattr(
        sinks.GenericWebhookSink, "send", lambda *_: {**sent(), "protocol": "wrong"}
    )
    assert sinks.bounded_send(web, event())["protocol"] == "generic_webhook"
