"""Local executable schema gate, before thread creation or model requests."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from dispatcher_for_codex_agents.workspace_paths import temporary_root

from .store import digest


def inspect_host(executable: str, codex_home: str, cwd: str) -> dict:
    environment = os.environ.copy()
    environment["CODEX_HOME"] = codex_home
    version = subprocess.run(
        [executable, "--version"],
        capture_output=True,
        text=True,
        timeout=15,
        cwd=cwd,
        env=environment,
        check=True,
    ).stdout.strip()
    with tempfile.TemporaryDirectory(
        prefix="dca-host-schema-", dir=temporary_root()
    ) as temporary:
        subprocess.run(
            [
                executable,
                "app-server",
                "generate-json-schema",
                "--out",
                temporary,
                "--experimental",
            ],
            capture_output=True,
            timeout=30,
            cwd=cwd,
            env=environment,
            check=True,
        )
        expected = {
            "v2/ThreadStartParams.json": {
                "allowProviderModelFallback",
                "dynamicTools",
                "model",
                "modelProvider",
                "cwd",
            },
            "v2/TurnStartParams.json": {"threadId", "toolOutput", "outputSchema"},
            "DynamicToolCallParams.json": {"threadId", "turnId", "tool", "arguments"},
            "DynamicToolCallResponse.json": {"success", "contentItems"},
        }
        hashes = {}
        for name, fields in expected.items():
            content = (Path(temporary) / name).read_bytes()
            if not fields <= json.loads(content).get("properties", {}).keys():
                raise ValueError("HOST_CONTINUATION_INTERFACE_UNSUPPORTED")
            hashes[name] = digest(content)
    return {"version": version, "schema_sha256": hashes, "model_requests": 0}
