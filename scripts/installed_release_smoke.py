"""Installed-wheel-only checks using synthetic config and no network transport."""

from __future__ import annotations

import argparse
import importlib.metadata as metadata
import json
import os
import shlex
import socket
import sys
import sysconfig
import zipfile
from pathlib import Path
from unittest.mock import patch


def forbidden_network(*args, **kwargs):
    raise AssertionError("NETWORK_FORBIDDEN_IN_RELEASE_SMOKE")


def smoke(wheel: Path, version: str, output: Path) -> dict:
    import dispatcher_for_codex_agents as package
    from dispatcher_for_codex_agents.notifications import core, hooks, sinks

    os.umask(0o077)
    if output.resolve() != output.absolute() or output.exists():
        raise ValueError("NEW_CANONICAL_SMOKE_DIRECTORY_REQUIRED")
    output.mkdir(mode=0o700)
    site = Path(sysconfig.get_path("purelib"))
    if not site.is_relative_to(Path(sys.prefix)) or not Path(
        package.__file__
    ).is_relative_to(site):
        raise ValueError("IMPORT_NOT_FROM_FRESH_VENV")
    distribution = metadata.distribution("dispatcher-for-codex-agents")
    if package.__version__ != version or distribution.version != version:
        raise ValueError("INSTALLED_VERSION_MISMATCH")
    direct = json.loads(distribution.read_text("direct_url.json") or "{}")
    if (
        direct.get("dir_info", {}).get("editable")
        or direct.get("url") != wheel.as_uri()
    ):
        raise ValueError("NOT_INSTALLED_FROM_SELECTED_WHEEL")
    with zipfile.ZipFile(wheel) as archive:
        modules = [
            n
            for n in archive.namelist()
            if n.startswith("dispatcher_for_codex_agents/") and n.endswith(".py")
        ]
        if not modules or any(
            (site / n).read_bytes() != archive.read(n) for n in modules
        ):
            raise ValueError("INSTALLED_WHEEL_BYTES_MISMATCH")

    with (
        patch.object(socket.socket, "connect", forbidden_network),
        patch.object(socket, "create_connection", forbidden_network),
        patch.object(socket, "getaddrinfo", forbidden_network),
    ):
        config = output / "notifications.json"
        config.write_text(
            json.dumps(
                {
                    "version": 1,
                    "ledger_directory": str(output / "unused-ledger"),
                    "sinks": [],
                }
            )
        )
        cfg = core.load_config(config)
        assert cfg.get("permission_notification_policy", "OFF") == "OFF"
        for prefix, protocol, defaults in [
            ("SCT", "serverchan_turbo", (2.0, 2.5)),
            ("sctp123t", "serverchan_sc3", (4.0, 5.0)),
        ]:
            # Generated placeholder only; never load a secret or call send().
            sink = sinks.ServerChanSink("synthetic", prefix + "FAKE" + "SECRET1234")
            assert sink.protocol == protocol
            assert (sink.budget.socket_timeout, sink.budget.deadline) == defaults
        assert hooks.HOOK_TIMEOUT_SECONDS == sinks.HOOK_TIMEOUT_SECONDS == 17
        home = output / "synthetic-home"
        home.mkdir(mode=0o700)
        executable = str(Path(sys.prefix) / "bin/dca-notify")
        events = ("UserPromptSubmit", "Stop", "PermissionRequest")
        document = {
            "hooks": {
                event: [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": shlex.join(
                                    [
                                        executable,
                                        "hook",
                                        "--event",
                                        event,
                                        "--config",
                                        str(config),
                                    ]
                                ),
                                "timeout": 4,
                                "statusMessage": hooks.MARKER,
                            }
                        ]
                    }
                ]
                for event in events
            }
        }
        target = home / "hooks.json"
        target.write_text(json.dumps(document))
        before = target.read_bytes()
        original = core.repository_root
        # Parser-only fixture: no production home/release. Long-path or spaced
        # venvs may use shell launchers, so release identity is synthetic here.
        # Installed byte/import/CLI checks above remain real and unpatched.
        with (
            patch.object(
                core,
                "repository_root",
                lambda p: None if p.is_relative_to(home) else original(p),
            ),
            patch.object(
                hooks, "_release_executable", lambda value, **kw: {"path": value}
            ),
        ):
            preview = hooks.migrate_hooks(
                str(home), executable, str(config), from_executables=(executable,)
            )
        assert preview["status"] == "HOOK_MIGRATION_PREVIEW"
        assert len(preview["changes"]) == 3
        assert all(c["after"]["timeout"] == 17 for c in preview["changes"])
        assert target.read_bytes() == before
        assert not (output / "unused-ledger").exists()
    return {
        "status": "PASS",
        "version": version,
        "import_path": package.__file__,
        "modules_matched": len(modules),
        "config_parse": "PASS",
        "protocol_construction": "PASS",
        "permission_default": "OFF",
        "turbo_budget": [2.0, 2.5],
        "sc3_budget": [4.0, 5.0],
        "hook_timeout": 17,
        "migration_preview": "PASS",
        "migration_identity": "SYNTHETIC_PARSER_FIXTURE",
        "network_requests": 0,
        "model_requests": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            smoke(args.wheel.resolve(), args.version, args.output_root.absolute()),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
