"""Workspace-only migration regressions. All credentials and sends are fake."""

import json
import os
import shutil
import sys
import tempfile

import pytest

from dispatcher_for_codex_agents.agent_harness.cli import main
from dispatcher_for_codex_agents.notifications import NotificationEvent, notify
from dispatcher_for_codex_agents.notifications.cli import default_config
from dispatcher_for_codex_agents.notifications.cli import main as notify_main
from dispatcher_for_codex_agents.notifications.sinks import configured_sink
from dispatcher_for_codex_agents.workspace_paths import (
    activate_workspace,
    temporary_root,
    workspace_root,
)


@pytest.fixture(autouse=True)
def isolated_defaults(monkeypatch):
    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.delenv("DCA_WORKSPACE_CONFIG", raising=False)
    monkeypatch.delenv("DCA_NOTIFY_CONFIG", raising=False)
    monkeypatch.setattr(sys, "prefix", "/standalone-python")
    monkeypatch.setattr(sys, "dont_write_bytecode", sys.dont_write_bytecode)
    monkeypatch.setattr(tempfile, "tempdir", tempfile.tempdir)


def workspace(tmp_path, monkeypatch):
    root = tmp_path / "engineering"
    root.mkdir(mode=0o700)
    for relative in ("configs", "runtime/tmp", "runtime/cache", "runs"):
        (root / relative).mkdir(mode=0o700, parents=True)
    config = root / "configs/workspace.json"
    config.write_text(json.dumps({"version": 1, "workspace_root": str(root)}))
    config.chmod(0o600)
    monkeypatch.setenv("DCA_WORKSPACE_CONFIG", str(config))
    return root, config


def test_workspace_temp_cache_and_notification_defaults(tmp_path, monkeypatch):
    root, _ = workspace(tmp_path, monkeypatch)
    assert activate_workspace() == root
    assert temporary_root() == str(root / "runtime/tmp")
    assert tempfile.gettempdir() == str(root / "runtime/tmp")
    assert default_config() == str(root / "configs/notifications.json")
    assert os.environ["XDG_CACHE_HOME"] == str(root / "runtime/cache")
    with tempfile.TemporaryDirectory(dir=temporary_root()) as directory:
        assert directory.startswith(str(root / "runtime/tmp") + "/")


def test_installed_layout_detected_and_missing_config_blocks(tmp_path, monkeypatch):
    root, config = workspace(tmp_path, monkeypatch)
    monkeypatch.delenv("DCA_WORKSPACE_CONFIG")
    monkeypatch.setattr(sys, "prefix", str(root / "releases/revision/venv"))
    assert workspace_root() == root
    config.unlink()
    assert main(["--help"]) == 2
    assert notify_main(["emit"]) == 2
    assert notify_main(["hook", "--event", "Stop"]) == 0


@pytest.mark.parametrize("problem", ["mode", "schema", "root", "symlink", "tmp"])
def test_invalid_workspace_never_falls_back(tmp_path, monkeypatch, problem):
    root, config = workspace(tmp_path, monkeypatch)
    if problem == "mode":
        config.chmod(0o644)
    elif problem == "schema":
        config.write_text('{"version":1,"unknown":true}')
    elif problem == "root":
        config.write_text(json.dumps({"version": 1, "workspace_root": "/tmp"}))
    elif problem == "symlink":
        link = root / "alias.json"
        link.symlink_to(config)
        monkeypatch.setenv("DCA_WORKSPACE_CONFIG", str(link))
    else:
        (root / "runtime/tmp").chmod(0o755)
    assert main(["batch", "status", "--plan-root", str(root)]) == 2


def test_legacy_library_and_explicit_notification_config(monkeypatch):
    assert workspace_root() is None
    monkeypatch.delenv("TMPDIR", raising=False)
    with pytest.raises(ValueError, match="EXPLICIT_TEMPORARY_DIRECTORY_REQUIRED"):
        temporary_root()
    with pytest.raises(ValueError, match="EXPLICIT_NOTIFICATION_CONFIGURATION"):
        default_config()
    with pytest.raises(ValueError, match="WORKSPACE_CONFIGURATION_REQUIRED"):
        activate_workspace()
    monkeypatch.setenv("DCA_NOTIFY_CONFIG", "/explicit/notifications.json")
    assert default_config() == "/explicit/notifications.json"


def test_retired_workspace_env_is_not_an_alias(tmp_path, monkeypatch):
    monkeypatch.setenv("DENOVO_WORKSPACE_CONFIG", str(tmp_path / "obsolete.json"))
    assert workspace_root() is None
    root, _ = workspace(tmp_path, monkeypatch)
    assert workspace_root() == root


def secret_settings(tmp_path):
    secret = tmp_path / "serverchan.env"
    secret.write_text("SERVERCHAN_SENDKEY=SCTFAKESECRET12345\n")
    secret.chmod(0o600)
    return secret, {
        "sink_id": "phone",
        "kind": "serverchan",
        "enabled": True,
        "send_key_env_file": str(secret),
    }


def test_external_secret_reference_and_legacy_value(tmp_path):
    secret, settings = secret_settings(tmp_path)
    before = secret.read_bytes()
    sink = configured_sink(settings)
    assert sink.send_key == "SCTFAKESECRET12345"
    assert "SCTFAKESECRET" not in repr(sink)
    assert secret.read_bytes() == before
    legacy = {k: v for k, v in settings.items() if k != "send_key_env_file"}
    legacy["send_key"] = sink.send_key
    assert configured_sink(legacy) == sink


@pytest.mark.parametrize("problem", ["mode", "symlink", "shell", "duplicate", "both"])
def test_secret_file_fail_closed(tmp_path, problem):
    secret, settings = secret_settings(tmp_path)
    if problem == "mode":
        secret.chmod(0o644)
    elif problem == "symlink":
        link = tmp_path / "alias.env"
        link.symlink_to(secret)
        settings["send_key_env_file"] = str(link)
    elif problem == "shell":
        secret.write_text("SERVERCHAN_SENDKEY=$(touch forbidden)\n")
    elif problem == "duplicate":
        secret.write_text(secret.read_text() * 2)
    else:
        settings["send_key"] = "SCTFAKESECRET12345"
    with pytest.raises(ValueError):
        configured_sink(settings)
    assert not (tmp_path / "forbidden").exists()


def test_ledger_copy_preserves_event_identity_and_no_resend(tmp_path):
    _, settings = secret_settings(tmp_path)
    config = tmp_path / "notifications.json"
    old = tmp_path / "old-ledger"
    new = tmp_path / "new-ledger"
    value = {"version": 1, "ledger_directory": str(old), "sinks": [settings]}
    config.write_text(json.dumps(value))
    config.chmod(0o600)
    event = NotificationEvent(
        source="harness", kind="batch_completed", status="COMPLETED", run_id="fake"
    )
    calls = []

    def send(sink, item):
        calls.append(item.event_id)
        return {"delivery_status": "SENT"}

    notify(event, config, sender=send)
    original = (old / "delivery.jsonl").read_bytes()
    shutil.copytree(old, new)
    assert (new / "delivery.jsonl").read_bytes() == original
    value["ledger_directory"] = str(new)
    config.write_text(json.dumps(value))
    result = notify(event, config, sender=send)
    assert result["sinks"]["phone"]["delivery_status"] == "DUPLICATE_SUPPRESSED"
    assert calls == [event.event_id]
    assert (old / "delivery.jsonl").read_bytes() == original
