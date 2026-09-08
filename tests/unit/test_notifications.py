"""Deterministic notification tests; fake transports cannot call any model."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs

import pytest

from dispatcher_for_codex_agents.notifications import NotificationEvent, notify
from dispatcher_for_codex_agents.notifications.cli import emit_batch, hook_event, main
from dispatcher_for_codex_agents.notifications.core import load_config, record_context
from dispatcher_for_codex_agents.notifications.hooks import (
    install_hooks,
    rollback_hooks,
)
from dispatcher_for_codex_agents.notifications.sinks import bounded_send, https_post


@pytest.fixture(autouse=True)
def no_model_calls(monkeypatch, tmp_path):
    from dispatcher_for_codex_agents.notifications import hooks

    # Fake installer backups must not select the developer's persistent state.
    monkeypatch.setattr(hooks, "workspace_root", lambda: tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("notification must never launch a model/subprocess")

    monkeypatch.setattr(subprocess, "Popen", forbidden)


def config(tmp_path, *, enabled=True):
    path = tmp_path / "notifications.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "ledger_directory": str(tmp_path / "ledger"),
                "sinks": [
                    {
                        "sink_id": "phone",
                        "kind": "serverchan",
                        "enabled": enabled,
                        "send_key": "SCTFAKESECRET1234",
                    },
                    {
                        "sink_id": "web",
                        "kind": "webhook",
                        "enabled": enabled,
                        "url": "https://example.invalid/PRIVATE",
                        "success_field": "ok",
                        "success_value": True,
                    },
                ],
            }
        )
    )
    path.chmod(0o600)
    return path


def event(
    kind="batch_completed",
    *,
    source="harness",
    status="COMPLETED",
    run_id="run1",
    **kwargs,
):
    return NotificationEvent(
        source=source,
        kind=kind,
        status=status,
        session_id="s1",
        turn_id="t1",
        run_id=run_id,
        **kwargs,
    )


def sent(sink, event):
    return {"delivery_status": "SENT", "http_status": 200, "application_status": "PASS"}


def test_fake_sinks_privacy_payload_and_application_ack(tmp_path, monkeypatch):
    path = config(tmp_path)
    calls = []

    def post(url, body, content_type):
        calls.append((body, content_type))
        if content_type == "application/json":
            return 200, b'{"ok":true}'
        return 200, b'{"code":0}'

    monkeypatch.setattr(
        "dispatcher_for_codex_agents.notifications.sinks.https_post", post
    )
    report = notify(event(), path, sender=lambda sink, item: sink.send(item))
    assert {r["delivery_status"] for r in report["sinks"].values()} == {"SENT"}
    webhook = json.loads(calls[1][0])
    phone = parse_qs(calls[0][0].decode())
    assert webhook["body"] == phone["desp"][0]
    assert webhook["title"] == phone["title"][0] == "[DCA] 子任务完成"
    assert "不代表结果已获批准" in webhook["body"]
    assert all(value not in webhook["body"] for value in ("run1", "event_id", "s1"))
    logged = (tmp_path / "ledger/delivery.jsonl").read_text()
    assert all(
        value not in logged
        for value in ("SCTFAKESECRET", "PRIVATE", "description", "prompt", "assistant")
    )


@pytest.mark.parametrize(
    "response,expected",
    [
        ((429, b"{}"), "FAILED"),
        ((500, b"{}"), "FAILED"),
        ((200, b'{"code":1,"ok":false}'), "FAILED"),
        ((200, b"not-json"), "FAILED"),
        ((302, b"{}"), "FAILED"),
    ],
)
def test_http_and_application_failures(tmp_path, monkeypatch, response, expected):
    path = config(tmp_path)
    monkeypatch.setattr(
        "dispatcher_for_codex_agents.notifications.sinks.https_post",
        lambda *_: response,
    )
    result = notify(event(), path, sender=lambda sink, item: sink.send(item))
    assert all(row["delivery_status"] == expected for row in result["sinks"].values())


def test_duplicate_per_sink_and_failure_not_success_suppressed(tmp_path):
    path = config(tmp_path)
    calls = []

    def sender(sink, item):
        calls.append((sink.sink_id, item.kind))
        return sent(sink, item)

    first = event()
    notify(first, path, sender=sender, clock=lambda: 100)
    duplicate = notify(first, path, sender=sender, clock=lambda: 101)
    assert all(
        r["delivery_status"] == "DUPLICATE_SUPPRESSED"
        for r in duplicate["sinks"].values()
    )
    stop = notify(
        event("turn_completed", source="codex"), path, sender=sender, clock=lambda: 102
    )
    assert all(
        r["delivery_status"] == "HARNESS_TERMINAL_SUPPRESSED"
        for r in stop["sinks"].values()
    )
    for kind in (
        "batch_failed",
        "shard_failed",
        "explicit_retry_required",
        "human_action_required",
    ):
        notify(event(kind, status="REQUIRED"), path, sender=sender, clock=lambda: 103)
    assert len(calls) == 10


@pytest.mark.parametrize("run,when", [("different", 101), ("run1", 401)])
def test_no_cross_run_or_expired_suppression(tmp_path, run, when):
    path = config(tmp_path)
    notify(event(), path, sender=sent, clock=lambda: 100)
    result = notify(
        event("turn_completed", source="codex", run_id=run),
        path,
        sender=sent,
        clock=lambda: when,
    )
    assert all(r["delivery_status"] == "SENT" for r in result["sinks"].values())


def test_failed_harness_delivery_does_not_suppress_main_stop(tmp_path):
    path = config(tmp_path)
    notify(event(), path, sender=lambda *_: {"delivery_status": "DELIVERY_UNKNOWN"})
    result = notify(event("turn_completed", source="codex"), path, sender=sent)
    assert all(r["delivery_status"] == "SENT" for r in result["sinks"].values())


def test_concurrent_duplicate_and_distinct_failure(tmp_path):
    path = config(tmp_path)
    calls = []

    def sender(sink, item):
        time.sleep(0.02)
        calls.append((sink.sink_id, item.kind))
        return sent(sink, item)

    with ThreadPoolExecutor(max_workers=3) as pool:
        values = list(
            pool.map(
                lambda item: notify(item, path, sender=sender),
                [event(), event(), event("human_action_required", status="REQUIRED")],
            )
        )
    assert all(row["status"] == "PROCESSED" for row in values)
    assert sorted(calls) == [
        ("phone", "batch_completed"),
        ("phone", "human_action_required"),
        ("web", "batch_completed"),
        ("web", "human_action_required"),
    ]


def test_bounded_timeout_and_no_automatic_resend(tmp_path):
    class SlowSink:
        def send(self, _):
            time.sleep(30)

    start = time.monotonic()
    result = bounded_send(SlowSink(), event(), timeout=0.05)
    assert time.monotonic() - start < 1
    assert result["delivery_status"] == "DELIVERY_UNKNOWN"
    path = config(tmp_path)
    notify(event(), path, sender=lambda *_: result)
    again = notify(event(), path, sender=lambda *_: pytest.fail("no automatic resend"))
    assert all(
        r["delivery_status"] == "DUPLICATE_SUPPRESSED" for r in again["sinks"].values()
    )


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o400])
def test_private_config_fail_closed(tmp_path, mode):
    path = config(tmp_path)
    path.chmod(mode)
    assert notify(event(), path)["status"] == "NOTIFICATION_NONBLOCKING_ERROR"


def test_missing_config_and_repository_config(tmp_path):
    assert (
        notify(event(), tmp_path / "missing")["status"] == "USER_CONFIGURATION_REQUIRED"
    )
    path = config(tmp_path)
    (tmp_path / ".git").write_text("gitdir: elsewhere")
    with pytest.raises(ValueError, match="OUTSIDE_REPOSITORY"):
        load_config(path)


def test_secure_config_symlink_and_unknown_fields(tmp_path):
    path = config(tmp_path)
    link = tmp_path / "alias"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="SYMLINK"):
        load_config(link)
    with pytest.raises(TypeError):
        NotificationEvent(
            source="test",
            kind="delivery_test",
            status="TEST",
            run_id="r1",
            description="PRIVATE",
        )
    with pytest.raises(ValueError):
        event(metrics={"description": 1})


@pytest.mark.parametrize(
    "name",
    [
        "Stop",
        "Interrupt",
        "SessionEnd",
        "UserPromptSubmit",
        "PermissionRequest",
        "SubagentStop",
    ],
)
def test_hook_cli_safe_output_no_continuation(tmp_path, monkeypatch, capsys, name):
    path = config(tmp_path, enabled=False)
    raw = {
        "session_id": "s1",
        "turn_id": "t1",
        "prompt": "PRIVATE_PROMPT",
        "last_assistant_message": "PRIVATE_TEXT",
        "transcript_path": "/private/transcript",
        "cwd": "/private/repo",
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(raw)))
    assert main(["hook", "--event", name, "--config", str(path)]) == 0
    output = capsys.readouterr()
    assert output.out == ("{}\n" if name in {"Stop", "SubagentStop"} else "")
    assert output.err == ""
    content = (tmp_path / "ledger/delivery.jsonl").read_text()
    assert "PRIVATE" not in content and "/private" not in content


def test_hook_failure_cannot_block_or_continue(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(sys, "stdin", io.StringIO("not-json"))
    assert main(["hook", "--event", "Stop", "--config", str(tmp_path / "absent")]) == 0
    assert capsys.readouterr().out == "{}\n"


def test_explicit_context_binding_and_turn_start(tmp_path, monkeypatch):
    path = config(tmp_path, enabled=False)
    hook_event("UserPromptSubmit", {"session_id": "s1", "turn_id": "t1"}, str(path))
    record_context(path, session_id="s1", turn_id="t1", run_id="r1")
    seen = []
    monkeypatch.setattr(
        "dispatcher_for_codex_agents.notifications.cli.notify",
        lambda item, _: seen.append(item),
    )
    hook_event("Stop", {"session_id": "s1", "turn_id": "t1"}, str(path))
    assert seen[0].run_id == "r1" and "elapsed_seconds" in seen[0].metrics
    hook_event("Stop", {"session_id": "s1", "turn_id": "t2"}, str(path))
    assert seen[1].run_id is None


def test_batch_success_failure_summary_only(monkeypatch, tmp_path):
    path = config(tmp_path, enabled=False)
    seen = []
    monkeypatch.setattr(
        "dispatcher_for_codex_agents.notifications.cli.notify",
        lambda item, _: seen.append(item.kind),
    )
    emit_batch(
        {"status": "PASS", "terminal_status_counts": {"SUCCESS": 39}},
        run_id="ok",
        config_path=str(path),
    )
    emit_batch(
        {
            "status": "PARTIAL_OR_BLOCKED",
            "terminal_status_counts": {"SUCCESS": 38, "FAILED": 1},
        },
        run_id="bad",
        config_path=str(path),
    )
    assert seen == ["batch_completed", "batch_failed", "explicit_retry_required"]


def test_installer_backup_merge_idempotence_rollback(tmp_path):
    home = tmp_path / "codex"
    home.mkdir()
    base = home / "config.toml"
    base.write_text('model="unchanged"\n')
    original = b'{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"true"}]}]}}'
    (home / "hooks.json").write_bytes(original)
    args = (str(home), sys.executable, str(tmp_path / "notify.json"))
    assert install_hooks(*args)["changed"] is True
    assert (home / "hooks.json").read_bytes() == original
    assert not (home / ".denovo-hook-installer").exists()
    result = install_hooks(*args, apply=True)
    assert base.read_text() == 'model="unchanged"\n'
    assert len(json.loads((home / "hooks.json").read_text())["hooks"]["Stop"]) == 2
    assert install_hooks(*args, apply=True)["changed"] is False
    assert (
        list(Path(result["receipt"]).parent.glob("config.toml.dca-backup-*"))[0]
        .stat()
        .st_mode
        & 0o777
        == 0o600
    )
    rollback_hooks(result["receipt"], apply=True)
    assert (home / "hooks.json").read_bytes() == original


def test_rollback_refuses_external_change(tmp_path):
    home = tmp_path / "codex"
    result = install_hooks(
        str(home), sys.executable, str(tmp_path / "notify.json"), apply=True
    )
    target = home / "hooks.json"
    target.write_text(target.read_text() + " ")
    with pytest.raises(ValueError, match="CHANGED"):
        rollback_hooks(result["receipt"], apply=True)


@pytest.mark.parametrize("inline", [False, True])
def test_retired_hook_registration_is_rejected_not_migrated(tmp_path, inline):
    home = tmp_path / "codex"
    home.mkdir()
    marker = "Denovo Codex Agent Tool notification"
    target = home / ("config.toml" if inline else "hooks.json")
    content = (
        f'[hooks]\nstatusMessage="{marker}"\n'
        if inline
        else json.dumps({"hooks": {"Stop": [{"hooks": [{"statusMessage": marker}]}]}})
    )
    target.write_text(content)
    before = {path.name: path.read_bytes() for path in home.iterdir()}
    with pytest.raises(ValueError, match="RETIRED_HOOK_REGISTRATION"):
        install_hooks(str(home), sys.executable, str(tmp_path / "notify.json"))
    assert {path.name: path.read_bytes() for path in home.iterdir()} == before


def test_installer_blocks_top_level_notify(tmp_path):
    home = tmp_path / "codex"
    home.mkdir()
    (home / "config.toml").write_text('notify=["existing-notifier"]\n')
    with pytest.raises(ValueError, match="TOP_LEVEL_NOTIFY_CONFLICT"):
        install_hooks(str(home), sys.executable, str(tmp_path / "config"), apply=True)
    assert not (home / "hooks.json").exists()


def test_https_verification_and_no_redirect(monkeypatch):
    calls = []

    class Response:
        status = 302

        def read(self, _):
            return b"{}"

    class Connection:
        def __init__(self, host, port, *, timeout, context):
            import ssl

            assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname

        def request(self, *args):
            calls.append(args)

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr("http.client.HTTPSConnection", Connection)
    assert (
        https_post("https://example.invalid/token", b"{}", "application/json")[0] == 302
    )
    assert len(calls) == 1
    with pytest.raises(ValueError):
        https_post("http://example.invalid", b"{}", "application/json")


def test_actual_bounded_sink_success_and_transport_error():
    class GoodSink:
        def send(self, _):
            return {"delivery_status": "SENT", "http_status": 200}

    class BrokenSink:
        def send(self, _):
            raise RuntimeError("SECRET_PRIVATE_URL")

    assert bounded_send(GoodSink(), event())["delivery_status"] == "SENT"
    result = bounded_send(BrokenSink(), event())
    assert result["delivery_status"] == "DELIVERY_UNKNOWN"
    assert "SECRET" not in json.dumps(result)


def test_per_sink_failure_does_not_hide_other_success(tmp_path):
    path = config(tmp_path)
    result = notify(
        event(),
        path,
        sender=lambda sink, item: (
            {"delivery_status": "FAILED"}
            if sink.sink_id == "phone"
            else sent(sink, item)
        ),
    )
    assert result["sinks"]["phone"]["delivery_status"] == "FAILED"
    assert result["sinks"]["web"]["delivery_status"] == "SENT"


def test_batch_notification_failure_does_not_change_cli_exit(
    monkeypatch, tmp_path, capsys
):
    from dispatcher_for_codex_agents.agent_harness.cli import main as harness_main

    monkeypatch.setattr(
        "dispatcher_for_codex_agents.agent_harness.cli.run_batch",
        lambda **_: {"status": "PASS", "terminal_status_counts": {"SUCCESS": 3}},
    )

    def broken(*_):
        raise OSError("PRIVATE")

    monkeypatch.setattr("dispatcher_for_codex_agents.notifications.cli.notify", broken)
    assert (
        harness_main(["batch", "run", "--plan-root", str(tmp_path), "--run-id", "safe"])
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "PASS"


def test_absent_previous_hooks_rollback_is_recoverable(tmp_path):
    home = tmp_path / "codex"
    result = install_hooks(
        str(home), sys.executable, str(tmp_path / "notify.json"), apply=True
    )
    restored = rollback_hooks(result["receipt"], apply=True)
    assert not (home / "hooks.json").exists()
    assert Path(restored["recoverable_copy"]).is_file()


def test_host_schema_intersection_and_unimplemented_events(tmp_path):
    old = tmp_path / "old.json"
    new = tmp_path / "new.json"
    old.write_text(
        json.dumps(
            {
                "definitions": {
                    "HookEventName": {
                        "enum": ["userPromptSubmit", "stop", "permissionRequest"]
                    }
                }
            }
        )
    )
    new.write_text(
        json.dumps(
            {
                "definitions": {
                    "HookEventName": {
                        "enum": [
                            "userPromptSubmit",
                            "stop",
                            "permissionRequest",
                            "interrupt",
                            "sessionEnd",
                        ]
                    }
                }
            }
        )
    )
    report = install_hooks(
        str(tmp_path / "home"),
        sys.executable,
        str(tmp_path / "config.json"),
        host_schemas=(str(old), str(new)),
    )
    assert report["unsupported_events"] == ["SessionEnd", "Interrupt"]
    assert report["host_capability_verified"] is True
    report = install_hooks(
        str(tmp_path / "home"),
        sys.executable,
        str(tmp_path / "config.json"),
        host_schemas=(str(new),),
    )
    assert report["unsupported_events"] == []
