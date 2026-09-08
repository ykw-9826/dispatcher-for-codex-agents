"""Fake filesystem boundary and network denial; all artifacts stay project-local."""

import os
import socket
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_filesystem(tmp_path, monkeypatch):
    from dispatcher_for_codex_agents.notifications import core

    # Existing tests model separate external-secret and repo directories. Ignore
    # only the outer test storage repo; nested fake .git boundaries stay real.
    original = core.repository_root

    def fake_repository_root(path):
        if path.is_relative_to(tmp_path):
            return next(
                (
                    parent
                    for parent in path.parents
                    if parent.is_relative_to(tmp_path) and (parent / ".git").exists()
                ),
                None,
            )
        return original(path)

    monkeypatch.setattr(core, "repository_root", fake_repository_root)
    # CLI tests must never inherit an enabled real phone sink.
    monkeypatch.setenv("DCA_NOTIFY_CONFIG", str(tmp_path / "disabled-missing.json"))
    monkeypatch.setattr(tempfile, "tempdir", tempfile.tempdir)
    monkeypatch.setattr(os, "environ", dict(os.environ))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("REAL_NETWORK_FORBIDDEN_IN_LOCAL_ACCEPTANCE")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def pytest_configure(config):
    root = Path(__file__).resolve().parents[1]
    # pytest temp directories must not default to /tmp; no live models are used.
    if config.option.basetemp is None:
        import time

        config.option.basetemp = str(root / "runtime/tmp" / f"pytest-{time.time_ns()}")
