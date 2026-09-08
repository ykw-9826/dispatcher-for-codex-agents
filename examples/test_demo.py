"""Offline CLI demonstration using only the included fake executable."""

from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import io
import json
import os
import tempfile
from pathlib import Path

from dispatcher_for_codex_agents.agent_harness.cli import main as cli
from dispatcher_for_codex_agents.workspace_paths import (
    activate_workspace,
    output_path,
    temporary_root,
)


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream, delimiter="\t"))


def run_demo(destination: Path) -> dict:
    activate_workspace()
    destination = output_path(destination)
    if destination.exists():
        raise ValueError("DEMO_OUTPUT_ALREADY_EXISTS")
    destination.mkdir(mode=0o700, parents=True)
    example = Path(__file__).resolve().parent
    fake = example.parent / "tests/fixtures/fake_codex_cli.py"
    if not fake.is_file() or not os.access(fake, os.X_OK):
        raise ValueError("BUNDLED_FAKE_EXECUTABLE_REQUIRED")
    commands = []

    def invoke(*arguments: str) -> dict:
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture):
            code = cli(list(arguments))
        value = json.loads(capture.getvalue())
        commands.append({"arguments": arguments, "exit_code": code, "result": value})
        if code:
            raise ValueError("TEST_DEMO_CLI_FAILED")
        return value

    original_environment = os.environ.copy()
    with tempfile.TemporaryDirectory(
        prefix="dca-test-demo-", dir=temporary_root()
    ) as tmp:
        temporary = Path(tmp)
        runtime_home = temporary / "fake-runtime"
        runtime_home.mkdir(mode=0o700)
        for profile in ("test-primary", "test-shadow"):
            (runtime_home / f"{profile}.config.toml").write_text(
                'model="fake-model"\nmodel_provider="fake-provider"\n', encoding="utf-8"
            )
        notifications = temporary / "notifications.json"
        notifications.write_text(
            json.dumps(
                {
                    "version": 1,
                    "ledger_directory": str(temporary / "disabled-ledger"),
                    "sinks": [],
                }
            ),
            encoding="utf-8",
        )
        notifications.chmod(0o600)
        calls = destination / "test-agent-calls.log"
        for key in list(os.environ):
            if key.startswith("FAKE_") or key.startswith("DCA_NOTIFY_"):
                os.environ.pop(key)
        os.environ.update(
            {
                "FAKE_CODEX_MODE": "batch_success",
                "FAKE_EXPECT_SCHEMA": "0",
                "FAKE_CODEX_CALL_LOG": str(calls),
                "DCA_NOTIFY_CONFIG": str(notifications),
            }
        )
        plan = destination / "plan"
        try:
            invoke(
                "batch",
                "plan",
                "--source-tsv",
                str(example / "records.tsv"),
                "--batch-id",
                "DEMO",
                "--record-id-column",
                "record_id",
                "--selected-column",
                "record_id",
                "--selected-column",
                "title",
                "--selected-column",
                "description",
                "--profile-role-config",
                str(example / "profile_roles.json"),
                "--shard-size",
                "2",
                "--prompt-template",
                str(example / "prompt.txt"),
                "--expected-output-schema",
                str(example / "schema.json"),
                "--timeout",
                "10",
                "--output-root",
                str(plan),
            )
            common = (
                "batch",
                "run",
                "--plan-root",
                str(plan),
                "--runtime-home",
                str(runtime_home),
                "--executable",
                str(fake),
                "--notify-config",
                str(notifications),
            )
            invoke(*common, "--run-id", "demo-preflight", "--dry-run")
            assert not calls.exists(), "DRY_RUN_STARTED_A_AGENT"
            invoke(*common, "--run-id", "demo-execution")
            assert len(calls.read_text().splitlines()) == 4
            invoke(*common, "--run-id", "demo-skip-success")
            assert len(calls.read_text().splitlines()) == 4
            invoke("batch", "status", "--plan-root", str(plan), "--snapshot-id", "demo")
            invoke(
                "batch", "collect", "--plan-root", str(plan), "--collection-id", "demo"
            )
        finally:
            os.environ.clear()
            os.environ.update(original_environment)
    coverage_files = list(plan.glob("collections/*/coverage_audit.tsv"))
    assert len(coverage_files) == 1
    coverage = read_tsv(coverage_files[0])
    assert len(coverage) == 2
    assert all(
        row["coverage_status"] == "PASS" and row["observed_count"] == "3"
        for row in coverage
    )
    collection = coverage_files[0].parent
    assert len(read_tsv(collection / "authoritative_results.tsv")) == 3
    assert len(read_tsv(collection / "shadow_results.tsv")) == 3
    verified = 0
    for manifest in plan.rglob("output_sha256.tsv"):
        for row in read_tsv(manifest):
            content = (manifest.parent / row["path"]).read_bytes()
            assert len(content) == int(row["size"])
            assert hashlib.sha256(content).hexdigest() == row["sha256"]
            verified += 1
    result = {
        "status": "PASS",
        "records_per_profile": 3,
        "profiles": 2,
        "shards": 2,
        "virtual_agent_invocations": 4,
        "real_model_calls": 0,
        "notification_requests": 0,
        "exact_once_coverage": "PASS",
        "success_skip_no_reexecution": True,
        "shard_hashes_verified": verified,
        "collection": str(collection),
    }
    with (destination / "demo_result.json").open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2, sort_keys=True)
        stream.write("\n")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = run_demo(args.output_root)
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Test demo refused: {exc}\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
