"""Zero-model synthetic history -> reinterpretation -> explicit collection.

The rejection event is dca.synthetic-runtime/1, not a captured Codex event.
Only the bundled fake executable runs. No provider or notification is contacted.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path

from dispatcher_for_codex_agents.agent_harness.batch import (
    load_batch_plan,
    plan_batch,
    run_batch,
)
from dispatcher_for_codex_agents.agent_harness.cli import main as cli
from dispatcher_for_codex_agents.agent_harness.revalidation import describe_source
from dispatcher_for_codex_agents.agent_harness.runtime_contract import digest
from dispatcher_for_codex_agents.workspace_paths import activate_workspace, output_path


def run_demo(destination: Path) -> dict:
    activate_workspace()
    destination = output_path(destination)
    destination.mkdir(parents=True, mode=0o700)  # Never overwrite prior evidence.
    example = Path(__file__).resolve().parent
    fake = example.parent / "tests/fixtures/fake_codex_cli.py"
    home = destination / "synthetic-home"
    home.mkdir(mode=0o700)
    (home / "config.toml").write_text(
        '[model_providers.fixture]\nname="Synthetic only"\n'
    )
    for profile in ("test-primary", "test-shadow"):
        (home / f"{profile}.config.toml").write_text(
            'model="fake-model"\nmodel_provider="fixture"\n'
        )
    plan_root = destination / "batch"
    plan_batch(
        source_tsv=example / "records.tsv",
        batch_id="DEMO",
        record_id_column="record_id",
        selected_columns=("record_id", "title", "description"),
        profile_role_config=example / "profile_roles.json",
        shard_size=2,
        prompt_template=example / "prompt.txt",
        expected_output_schema=example / "schema.json",
        output_root=plan_root,
        timeout=10,
    )
    saved = os.environ.copy()
    calls = destination / "fake_calls.log"
    try:
        for name in list(os.environ):
            if name.startswith("FAKE_"):
                os.environ.pop(name)
        os.environ.update(
            FAKE_RUNTIME_CONTRACT_DEMO="1",
            FAKE_CODEX_MODE="batch_success",
            FAKE_EXPECT_SCHEMA="0",
            FAKE_CODEX_CALL_LOG=str(calls),
        )
        report = run_batch(
            plan_root=plan_root,
            run_id="synthetic-history",
            executable=str(fake),
            codex_home=home,
        )
    finally:
        os.environ.clear()
        os.environ.update(saved)
    plan = load_batch_plan(plan_root)
    policy = destination / "runtime_contract.json"
    policy.write_text(
        json.dumps(
            {
                "structured_output": "json_or_single_fence",
                "rejected_user_input": "warn_if_runtime_rejected",
            }
        )
    )
    selections = []
    original_hashes = {}

    def invoke(*arguments):
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture):
            code = cli(list(arguments))
        if code:
            raise ValueError("Synthetic revalidation/collection CLI failed")
        return json.loads(capture.getvalue())

    for source in sorted(plan_root.glob("workers/*/*/*")):
        descriptor = describe_source(source)
        original_hashes[str(source)] = descriptor["files"]
        source_manifest = (
            destination / f"{source.parent.parent.name}-{source.parent.name}.json"
        )
        source_manifest.write_text(json.dumps(descriptor))
        original = json.loads((source / "invocation_result.json").read_bytes())
        assert original["status"] == "failure", "Strict original unexpectedly succeeded"
        identity = "r-" + str(len(selections) + 1)
        derived = invoke(
            "revalidate",
            "--source-manifest",
            str(source_manifest),
            "--runtime-contract",
            str(policy),
            "--revalidation-id",
            identity,
            "--output-root",
            str(destination / "derived"),
        )
        assert derived["status"] == "success" and derived["new_model_requests"] == 0
        shard = next(
            s for s in plan.shards if source.parent.parent.name.endswith(s.shard_id)
        )
        selections.append(
            {
                "profile_id": source.parent.name,
                "shard_id": shard.shard_id,
                "attempt_id": source.name,
                "revalidation_directory": derived["directory"],
            }
        )
    selection = destination / "selection.json"
    selection.write_text(json.dumps({"version": 1, "selections": selections}))
    collected = invoke(
        "batch",
        "collect",
        "--plan-root",
        str(plan_root),
        "--collection-id",
        "explicit-derived",
        "--selection",
        str(selection),
    )
    assert collected["status"] == "PASS"
    for source, hashes in original_hashes.items():
        assert describe_source(Path(source))["files"] == hashes
    result = {
        "status": "PASS",
        "synthetic_protocol": "dca.synthetic-runtime/1",
        "original_execution": report,
        "collection": collected,
        "virtual_invocations": len(calls.read_text().splitlines()),
        "real_model_requests": 0,
        "notification_requests": 0,
        "revalidation_new_usage": {},
        "source_hashes_unchanged": True,
        "exact_once_coverage": "PASS",
        "roles": "PLAN_OWNED_UNCHANGED",
        "selection_sha256": digest(selection.read_bytes()),
    }
    with (destination / "demo_result.json").open("x") as stream:
        json.dump(result, stream, indent=2, sort_keys=True)
        stream.write("\n")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run_demo(args.output_root), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
