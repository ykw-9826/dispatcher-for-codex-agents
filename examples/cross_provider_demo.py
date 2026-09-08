"""Test-safe external-provider workflow, including a simulated Codex controller.

Only the two bundled protocol fixtures can be executed. There is no live flag,
provider credential access, or real notification sink. Model/provider names in
the generated sidecars illustrate routing, not a new live capability claim.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import io
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from dispatcher_for_codex_agents.agent_harness.cli import main as cli
from dispatcher_for_codex_agents.workspace_paths import (
    activate_workspace,
    output_path,
    temporary_root,
)


def _json(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
    path.chmod(0o600)


def _invoke(*args: str) -> dict:
    capture = io.StringIO()
    with contextlib.redirect_stdout(capture):
        code = cli(list(args))
    if code:
        raise ValueError("TEST_DEMO_CLI_FAILED")
    return json.loads(capture.getvalue())


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream, delimiter="\t"))


def run_demo(destination: Path, *, dry_run: bool = False) -> dict:
    activate_workspace()
    destination = output_path(destination)
    if destination.exists():
        raise ValueError("DEMO_OUTPUT_ALREADY_EXISTS")
    example = Path(__file__).resolve().parent
    fixtures = example.parent / "tests/fixtures"
    agent_executable = fixtures / "fake_codex_cli.py"
    controller_executable = fixtures / "fake_main_appserver.py"
    for path in (agent_executable, controller_executable):
        if not path.is_file() or path.is_symlink():
            raise ValueError("BUNDLED_TEST_EXECUTABLE_REQUIRED")
    destination.mkdir(parents=True, mode=0o700)
    host = destination / "test-host"
    host.mkdir(mode=0o700)
    for path in (agent_executable, controller_executable):
        copied = host / path.name
        shutil.copyfile(path, copied)
        copied.chmod(0o700)
    agent_executable = host / agent_executable.name
    controller_executable = host / controller_executable.name
    (host / "config.toml").write_text(
        'model="gpt-6-astra"\nmodel_provider="openai"\n', encoding="utf-8"
    )
    routes = (
        ("demo-glm", "glm-5.3", "example-glm-provider"),
        ("demo-deepseek", "deepseek-v4-flash", "example-deepseek-provider"),
    )
    for profile_id, model, provider in routes:
        (host / f"{profile_id}.config.toml").write_text(
            f'model="{model}"\nmodel_provider="{provider}"\n', encoding="utf-8"
        )
    roles = destination / "profile_roles.json"
    _json(
        roles,
        {
            "profiles": [
                {
                    "profile_id": profile_id,
                    "role": "external-agent-comparison",
                    "authority_class": "diagnostic",
                    "execution_order": index,
                }
                for index, (profile_id, _, _) in enumerate(routes, start=1)
            ]
        },
    )
    private = Path(tempfile.mkdtemp(prefix="dca-cross-provider-", dir=temporary_root()))
    notifications = private / "notifications.disabled.json"
    _json(
        notifications,
        {"version": 1, "ledger_directory": str(private / "ledger"), "sinks": []},
    )
    plan = destination / "plan"
    calls = destination / "test-agent-calls.log"
    original = os.environ.copy()
    try:
        for key in list(os.environ):
            if key.startswith(("FAKE_", "DCA_NOTIFY_")):
                os.environ.pop(key)
        os.environ.update(
            FAKE_CODEX_MODE="batch_success",
            FAKE_EXPECT_SCHEMA="0",
            FAKE_OMIT_SERVED_MODEL="1",
            FAKE_CODEX_CALL_LOG=str(calls),
            DCA_NOTIFY_CONFIG=str(notifications),
        )
        _invoke(
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
            str(roles),
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
        spec_path = destination / "controller.spec.json"
        _json(
            spec_path,
            {
                "task_id": "cross-provider-test",
                "executable": str(controller_executable),
                "codex_home": str(host),
                "model": "gpt-6-astra",
                "provider": "openai",
                "working_directory": str(destination),
                "allowed_actions": [
                    "start_approved_jobs",
                    "read_verified_results",
                    "summarize_results",
                ],
                # Fresh finite test authorization, never a permanent live grant.
                "expires_at": time.time() + 300,
                "wake_budget": 1,
                "main_turn_budget": 2,
                "main_turn_timeout": 20.0,
                "jobs": [
                    {
                        "job_id": "external-models",
                        "attempt_id": "test-attempt",
                        "kind": "batch",
                        "plan_root": str(plan),
                        "max_workers": 2,
                        "agent_executable": str(agent_executable),
                        "agent_home": str(host),
                    }
                ],
                "notification_config": str(notifications),
            },
        )
        command = (
            "bridge",
            "start",
            "--spec",
            str(spec_path),
            "--state-root",
            str(destination / "controller-state"),
            "--new-thread",
        )
        preflight = _invoke(*command, "--dry-run")
        if calls.exists():
            raise ValueError("DRY_RUN_STARTED_AGENT")
        result = {
            "status": "DRY_RUN_VALIDATED",
            "test_only": True,
            "real_model_calls": 0,
            "notification_requests": 0,
            "models_and_controller_are_simulated": True,
            "routes": [
                dict(zip(("profile_id", "model", "provider"), row, strict=True))
                for row in routes
            ],
            "preflight": preflight,
        }
        if not dry_run:
            continuation = _invoke(*command)
            if continuation.get("status") != "COMPLETED":
                raise ValueError("TEST_CONTROLLER_NOT_COMPLETED")
            controller_status = _invoke(
                "bridge",
                "status",
                "--state-root",
                str(destination / "controller-state"),
            )
            if (
                controller_status["logical_main_turn_requests"] != 2
                or controller_status["wait_logical_model_requests"] != 0
            ):
                raise ValueError("TEST_CONTROLLER_REQUEST_COUNT_INVALID")
            count = len(calls.read_text().splitlines())
            if count != 4:
                raise ValueError("UNEXPECTED_TEST_INVOCATION_COUNT")
            collection = plan / "collections/test-attempt"
            coverage = _rows(collection / "coverage_audit.tsv")
            if len(coverage) != 2 or any(
                row["coverage_status"] != "PASS" or row["observed_count"] != "3"
                for row in coverage
            ):
                raise ValueError("TEST_COVERAGE_INVALID")
            if len(_rows(collection / "diagnostic_results.tsv")) != 6:
                raise ValueError("TEST_DIAGNOSTIC_RESULTS_INVALID")
            verified = 0
            manifests = [
                *plan.rglob("output_sha256.tsv"),
                collection / "collection_manifest.tsv",
            ]
            for manifest in manifests:
                for row in _rows(manifest):
                    content = (manifest.parent / row["path"]).read_bytes()
                    if hashlib.sha256(content).hexdigest() != row["sha256"]:
                        raise ValueError("TEST_HASH_MISMATCH")
                    verified += 1
            provenance = []
            for path in sorted(plan.rglob("invocation_result.json")):
                value = json.loads(path.read_text())
                if value["status"] != "success":
                    raise ValueError("TEST_AGENT_FAILURE")
                provenance.append(value["provenance"])
            expected = {(model, provider) for _, model, provider in routes}
            if {
                (p["configured_model"], p["configured_provider"]) for p in provenance
            } != expected:
                raise ValueError("TEST_ROUTE_MISMATCH")
            if any(
                p["provider_reported_served_model"] != "NOT_REPORTED"
                for p in provenance
            ):
                raise ValueError("TEST_SERVED_MODEL_INFERRED")
            result.update(
                status="PASS",
                virtual_agent_invocations=count,
                exact_once_coverage="PASS",
                hashes_verified=verified,
                controller_continuation=continuation,
                logical_controller_turns=controller_status[
                    "logical_main_turn_requests"
                ],
                wait_logical_model_requests=controller_status[
                    "wait_logical_model_requests"
                ],
                agent_provenance=provenance,
                collection=str(collection),
            )
        _json(destination / "demo_result.json", result)
        return result
    finally:
        os.environ.clear()
        os.environ.update(original)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--dry-run", action="store_true", help="Validate the test plan only."
    )
    args = parser.parse_args()
    try:
        result = run_demo(args.output_root, dry_run=args.dry_run)
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Test demo refused: {exc}\n")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
