"""Repo/env migration regressions; no added agent/controller semantics."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from dispatcher_for_codex_agents.notifications.core import (
    external_path,
    load_config,
    storage_path,
)
from dispatcher_for_codex_agents.workspace_paths import output_path, workspace_root


def repository(tmp_path, monkeypatch):
    root = tmp_path / "independent"
    root.mkdir(mode=0o700)
    (root / ".git").mkdir()
    for name in ("configs", "runtime/tmp", "runtime/cache", "runs"):
        (root / name).mkdir(mode=0o700, parents=True)
    cfg = root / "configs/workspace.json"
    cfg.write_text(json.dumps({"version": 1, "workspace_root": str(root)}))
    cfg.chmod(0o600)
    monkeypatch.setenv("DCA_WORKSPACE_CONFIG", str(cfg))
    return root


def test_project_venv_auto_detect(tmp_path, monkeypatch):
    root = repository(tmp_path, monkeypatch)
    monkeypatch.delenv("DCA_WORKSPACE_CONFIG")
    monkeypatch.setattr(sys, "prefix", str(root / ".venv"))
    assert workspace_root() == root


def test_repo_storage_allowed_secret_still_external(tmp_path, monkeypatch):
    root = repository(tmp_path, monkeypatch)
    assert storage_path(root / "runtime/state/events") == root / "runtime/state/events"
    assert (
        storage_path(root / "configs/notifications.json")
        == root / "configs/notifications.json"
    )
    with pytest.raises(ValueError, match="OUTSIDE_REPOSITORY"):
        external_path(root / "runtime/private.env")
    with pytest.raises(ValueError, match="OUTSIDE_REPOSITORY"):
        storage_path(root / "src/ledger")
    with pytest.raises(ValueError, match="OUTSIDE_REPOSITORY"):
        storage_path(root / "configs-evil/ledger")


@pytest.mark.parametrize("field", ["send_key", "url"])
def test_inline_credentials_rejected_in_repo(tmp_path, monkeypatch, field):
    root = repository(tmp_path, monkeypatch)
    cfg = root / "configs/notifications.json"
    cfg.write_text(
        json.dumps(
            {
                "version": 1,
                "ledger_directory": str(root / "runtime/state/test"),
                "sinks": [{field: "FAKE_NOT_A_CREDENTIAL"}],
            }
        )
    )
    cfg.chmod(0o600)
    with pytest.raises(ValueError, match="INLINE_SECRET"):
        load_config(cfg)


def test_installed_process_project_config_and_canonical_import():
    root = Path(__file__).resolve().parents[2]
    code = """
import json, sys
from pathlib import Path
import dispatcher_for_codex_agents
from dispatcher_for_codex_agents.workspace_paths import workspace_root
from dispatcher_for_codex_agents.notifications.core import load_config
r = workspace_root()
assert r is not None
assert Path(dispatcher_for_codex_agents.__file__).resolve().is_relative_to(r)
print(json.dumps({'root':str(r),'base':sys.base_prefix}))
"""
    value = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code],
        cwd=root,
        text=True,
        capture_output=True,
    )
    assert value.returncode == 0, value.stderr
    assert json.loads(value.stdout)["root"] == str(root)


def test_no_other_source_import_or_environment_dependency():
    root = Path(__file__).resolve().parents[2]
    for path in (root / "src").rglob("*.py"):
        text = path.read_text()
        assert "sys.path.insert" not in text
        assert "sys.path.append" not in text


def test_output_root_no_external_or_symlink(tmp_path, monkeypatch):
    root = repository(tmp_path, monkeypatch)
    assert output_path(root / "runs/new") == root / "runs/new"
    with pytest.raises(ValueError, match="OUTPUT_OUTSIDE"):
        output_path(tmp_path / "other-project")
    link = root / "runs/link"
    link.symlink_to(tmp_path)
    with pytest.raises(ValueError, match="OUTPUT_OUTSIDE"):
        output_path(link / "output")
