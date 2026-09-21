"""Immutable synthetic history and explicit collector selection tests."""

import hashlib
import json
from pathlib import Path

import pytest
from test_agent_harness_batch import _fake_home, _plan

from dispatcher_for_codex_agents.agent_harness.batch import (
    collect_batch,
    load_batch_plan,
    run_batch,
)
from dispatcher_for_codex_agents.agent_harness.contracts import RuntimeContract
from dispatcher_for_codex_agents.agent_harness.revalidation import (
    describe_source,
    revalidate,
    verify_revalidation,
)

POLICY = RuntimeContract(
    structured_output="json_or_single_fence",
    rejected_user_input="warn_if_runtime_rejected",
)


def hashes(directory):
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir()
    }


def synthetic_history(tmp_path, monkeypatch):
    from test_agent_harness_batch import FAKE_CODEX

    root = _plan(tmp_path, records=1, profiles=1)
    home = _fake_home(tmp_path, profiles=1)
    monkeypatch.setenv("FAKE_CODEX_MODE", "batch_success")
    monkeypatch.setenv("FAKE_EXPECT_SCHEMA", "0")
    monkeypatch.setenv("FAKE_RUNTIME_CONTRACT_DEMO", "1")
    run_batch(
        plan_root=root,
        run_id="synthetic-source",
        codex_home=home,
        executable=str(FAKE_CODEX),
    )
    source = next(root.glob("workers/*/*/*"))
    manifest = tmp_path / "source.json"
    manifest.write_text(json.dumps(describe_source(source)))
    return root, source, manifest


def test_explicit_derived_collection_and_immutable_source(tmp_path, monkeypatch):
    root, source, manifest = synthetic_history(tmp_path, monkeypatch)
    before = hashes(source)
    assert (
        json.loads((source / "invocation_result.json").read_text())["status"]
        == "failure"
    )
    report = revalidate(
        source_manifest=manifest,
        runtime_contract=POLICY,
        revalidation_id="r1",
        output_root=tmp_path / "derived",
    )
    derivative = Path(report["directory"])
    assert report["status"] == "success"
    assert report["new_model_requests"] == 0 and report["new_usage"] == {}
    assert hashes(source) == before
    assert (
        verify_revalidation(derivative, expected_source=source)["status"] == "success"
    )
    with pytest.raises(ValueError):
        revalidate(
            source_manifest=manifest,
            runtime_contract=POLICY,
            revalidation_id="r1",
            output_root=tmp_path / "derived",
        )
    assert (
        collect_batch(plan_root=root, collection_id="original-only")["status"] == "FAIL"
    )
    plan = load_batch_plan(root)
    selection = tmp_path / "selection.json"
    entry = {
        "profile_id": plan.snapshot.profiles[0].profile_id,
        "shard_id": plan.shards[0].shard_id,
        "attempt_id": source.name,
        "revalidation_directory": str(derivative),
    }
    selection.write_text(json.dumps({"version": 1, "selections": [entry]}))
    collected = collect_batch(
        plan_root=root, collection_id="reinterpreted", selection=selection
    )
    assert collected["status"] == "PASS"
    assert collected["warnings"]
    assert hashes(source) == before
    selection.write_text(
        json.dumps(
            {
                "version": 1,
                "selections": [entry, {**entry, "revalidation_directory": None}],
            }
        )
    )
    with pytest.raises(ValueError):
        collect_batch(plan_root=root, collection_id="duplicated", selection=selection)


@pytest.mark.parametrize(
    "mutation", ["hash", "traversal", "missing", "version", "capability"]
)
def test_bad_source_manifest_fails_closed(tmp_path, monkeypatch, mutation):
    _, source, manifest = synthetic_history(tmp_path, monkeypatch)
    descriptor = json.loads(manifest.read_text())
    if mutation == "hash":
        descriptor["files"]["events.jsonl"] = "0" * 64
    if mutation == "traversal":
        descriptor["files"]["../events.jsonl"] = "0" * 64
    if mutation == "missing":
        descriptor["files"].pop("events.jsonl")
    if mutation == "version":
        descriptor["format"] = "unsupported"
    if mutation == "capability":
        descriptor["capability_policy"] = {"tools": ["shell"]}
    manifest.write_text(json.dumps(descriptor))
    with pytest.raises(ValueError):
        revalidate(
            source_manifest=manifest,
            runtime_contract=POLICY,
            revalidation_id="bad",
            output_root=tmp_path / "derived",
        )
    assert not (tmp_path / "derived" / "bad").exists()


@pytest.mark.parametrize(
    "mutation", ["tamper", "missing", "symlink", "hardlink", "extra"]
)
def test_actual_source_files_fail_closed(tmp_path, monkeypatch, mutation):
    _, source, manifest = synthetic_history(tmp_path, monkeypatch)
    # Deliberately corrupt only this test's freshly generated synthetic history.
    source.chmod(0o700)
    target = source / "events.jsonl"
    if mutation == "tamper":
        target.chmod(0o600)
        target.write_bytes(target.read_bytes() + b"\n")
    if mutation == "missing":
        target.unlink()
    if mutation in {"symlink", "hardlink"}:
        import os

        outside = tmp_path / "outside.log"
        outside.write_bytes(target.read_bytes())
        target.unlink()
        if mutation == "symlink":
            target.symlink_to(outside)
        else:
            os.link(outside, target)
    if mutation == "extra":
        (source / "unexpected.txt").write_text("not allowlisted")
    with pytest.raises(ValueError):
        revalidate(
            source_manifest=manifest,
            runtime_contract=POLICY,
            revalidation_id="bad",
            output_root=tmp_path / "derived",
        )
    assert not (tmp_path / "derived").exists()


def test_deterministic_derivative_and_no_subprocess(tmp_path, monkeypatch):
    _, source, manifest = synthetic_history(tmp_path, monkeypatch)

    def forbidden(*args, **kwargs):
        raise AssertionError("No model or subprocess")

    monkeypatch.setattr("subprocess.Popen", forbidden)
    one = revalidate(
        source_manifest=manifest,
        runtime_contract=POLICY,
        revalidation_id="same",
        output_root=tmp_path / "one",
    )
    two = revalidate(
        source_manifest=manifest,
        runtime_contract=POLICY,
        revalidation_id="same",
        output_root=tmp_path / "two",
    )
    assert hashes(Path(one["directory"])) == hashes(Path(two["directory"]))
    evaluation = json.loads((Path(one["directory"]) / "evaluation.json").read_bytes())
    assert (
        evaluation["usage"] == {}
        and evaluation["provenance"]["new_agent_subprocess_count"] == 0
    )
    assert one["original_usage_reference"]["input_tokens"] == 17
    assert (
        verify_revalidation(one["directory"], expected_source=source)["status"]
        == "success"
    )
    path = Path(one["directory"])
    path.chmod(0o700)
    (path / "extra.py").write_text('raise AssertionError("do not execute")')
    with pytest.raises(ValueError):
        verify_revalidation(path, expected_source=source)


def test_out_of_turn_rejection_derivative_never_collected(tmp_path, monkeypatch):
    root, source, manifest = synthetic_history(tmp_path, monkeypatch)
    # Create adversarial synthetic evidence before pinning its review snapshot.
    source.chmod(0o700)
    capture = source / "events.jsonl"
    capture.chmod(0o600)
    lines = capture.read_bytes().splitlines(keepends=True)
    lines[1], lines[2] = lines[2], lines[1]
    capture.write_bytes(b"".join(lines))
    output_manifest = source / "output_sha256.tsv"
    output_manifest.chmod(0o600)
    output_manifest.write_text(
        "path\tsize\tsha256\n"
        + "".join(
            f"{p.name}\t{p.stat().st_size}\t{hashlib.sha256(p.read_bytes()).hexdigest()}\n"
            for p in sorted(source.iterdir())
            if p.name != "output_sha256.tsv"
        )
    )
    manifest.write_text(json.dumps(describe_source(source)))
    before = hashes(source)

    def forbidden(*args, **kwargs):
        raise AssertionError("Revalidation/collection must not launch any process")

    monkeypatch.setattr("subprocess.Popen", forbidden)
    report = revalidate(
        source_manifest=manifest,
        runtime_contract=POLICY,
        revalidation_id="bad-order",
        output_root=tmp_path / "derived",
    )
    assert report["status"] == "failure"
    assert report["failure_code"] == "EVENT_STREAM_INVALID"
    selection = tmp_path / "selection.json"
    selection.write_text(
        json.dumps(
            {
                "version": 1,
                "selections": [
                    {
                        "profile_id": "profile-1",
                        "shard_id": "shard-0001",
                        "attempt_id": "attempt-001",
                        "revalidation_directory": report["directory"],
                    }
                ],
            }
        )
    )
    with pytest.raises(ValueError, match="not successful"):
        collect_batch(plan_root=root, collection_id="blocked", selection=selection)
    assert hashes(source) == before
    assert not (root / "collections" / "blocked").exists()


@pytest.mark.parametrize("out_of_order", [False, True])
def test_legacy_eight_file_layout_not_rewritten(tmp_path, monkeypatch, out_of_order):
    import csv

    from dispatcher_for_codex_agents.agent_harness.contracts import (
        AgentTask,
        InvocationResult,
        ModelProfile,
    )
    from dispatcher_for_codex_agents.agent_harness.payload import InputRecord
    from dispatcher_for_codex_agents.agent_harness.shard import ImmutableShardWriter

    _, source, _ = synthetic_history(tmp_path, monkeypatch)
    writer = ImmutableShardWriter(
        workers_root=tmp_path / "legacy",
        task_id=source.parent.parent.name,
        profile_id=source.parent.name,
        attempt_id=source.name,
    )
    task = AgentTask.model_validate_json(
        (source / "agent_task.snapshot.json").read_bytes()
    )
    profile = ModelProfile.model_validate_json(
        (source / "model_profile.snapshot.redacted.json").read_bytes()
    )
    result = InvocationResult.model_validate_json(
        (source / "invocation_result.json").read_bytes()
    )
    events = (source / "events.jsonl").read_bytes()
    if out_of_order:
        lines = events.splitlines(keepends=True)
        # Synthetic refusal is now outside the turn; hash-valid is not order-valid.
        lines[1], lines[2] = lines[2], lines[1]
        events = b"".join(lines)
    writer.write(
        task=task,
        profile=profile,
        input_records=tuple(
            InputRecord(
                logical_name=row["logical_name"],
                source_name=row["source_name"],
                size=int(row["size"]),
                sha256=row["sha256"],
            )
            for row in csv.DictReader(
                (source / "input_sha256.tsv").read_text().splitlines(), delimiter="\t"
            )
        ),
        events_jsonl=events,
        stderr_log=b"",
        final_output=result.final_output,
        result=result,
    )
    assert len(list(writer.path.iterdir())) == 8
    before = hashes(writer.path)
    manifest = tmp_path / "legacy.json"
    manifest.write_text(json.dumps(describe_source(writer.path)))
    report = revalidate(
        source_manifest=manifest,
        runtime_contract=POLICY,
        revalidation_id="legacy-interpretation",
        output_root=tmp_path / "derived",
    )
    assert report["status"] == ("failure" if out_of_order else "success")
    assert hashes(writer.path) == before
    verified = verify_revalidation(report["directory"], expected_source=writer.path)
    assert verified["status"] == report["status"]
    if out_of_order:
        evaluation = json.loads(
            (Path(report["directory"]) / "evaluation.json").read_bytes()
        )
        assert evaluation["failure_code"] == "EVENT_STREAM_INVALID"
        assert evaluation["schema_validation_status"] == "PASS"
        assert evaluation["provenance"]["new_agent_subprocess_count"] == 0


def test_batch_cli_policy_and_explicit_original_choice(tmp_path, monkeypatch, capsys):
    from test_agent_harness_batch import FAKE_CODEX, _inputs

    from dispatcher_for_codex_agents.agent_harness.cli import main

    inputs = _inputs(tmp_path / "inputs", records=1, profiles=1)
    policy = tmp_path / "policy.json"
    policy.write_text(POLICY.model_dump_json())
    plan_root = tmp_path / "plan"
    args = [
        "batch",
        "plan",
        "--source-tsv",
        str(inputs["source"]),
        "--batch-id",
        "BATCH-A",
        "--record-id-column",
        "record_id",
        "--selected-column",
        "record_id",
        "--profile-role-config",
        str(inputs["profiles"]),
        "--shard-size",
        "1",
        "--prompt-template",
        str(inputs["prompt"]),
        "--expected-output-schema",
        str(inputs["schema"]),
        "--output-root",
        str(plan_root),
        "--runtime-contract",
        str(policy),
    ]
    assert main(args) == 0
    capsys.readouterr()
    plan = load_batch_plan(plan_root)
    assert plan.snapshot.runtime_contract == POLICY
    home = _fake_home(tmp_path, profiles=1)
    monkeypatch.setenv("FAKE_RUNTIME_CONTRACT_DEMO", "1")
    monkeypatch.setenv("FAKE_CODEX_MODE", "batch_success")
    monkeypatch.setenv("FAKE_EXPECT_SCHEMA", "0")
    report = run_batch(
        plan_root=plan_root, run_id="optin", codex_home=home, executable=str(FAKE_CODEX)
    )
    assert report["terminal_status_counts"] == {"SUCCESS": 1}
    source = next(plan_root.glob("workers/*/*/*"))
    assert json.loads((source / "agent_task.snapshot.json").read_bytes())[
        "runtime_contract"
    ] == POLICY.model_dump(mode="json")
    selected = tmp_path / "selection.json"
    selected.write_text(
        json.dumps(
            {
                "version": 1,
                "selections": [
                    {
                        "profile_id": "profile-1",
                        "shard_id": "shard-0001",
                        "attempt_id": "attempt-001",
                    }
                ],
            }
        )
    )
    collected = collect_batch(
        plan_root=plan_root, collection_id="explicit-original", selection=selected
    )
    assert collected["status"] == "PASS" and collected["warnings"]
    assert collected["selected_revalidation_count"] == 0
    with pytest.raises(ValueError):
        collect_batch(
            plan_root=plan_root, collection_id="explicit-original", selection=selected
        )


def test_cli_invalid_contract_no_reservation(tmp_path, monkeypatch, capsys):
    from dispatcher_for_codex_agents.agent_harness.cli import main

    policy = tmp_path / "invalid.json"
    policy.write_text('{"structured_output":"repair"}')

    def forbidden(*args, **kwargs):
        raise AssertionError("No subprocess")

    monkeypatch.setattr("subprocess.Popen", forbidden)
    code = main(
        [
            "revalidate",
            "--source-manifest",
            str(tmp_path / "missing.json"),
            "--runtime-contract",
            str(policy),
            "--revalidation-id",
            "invalid",
            "--output-root",
            str(tmp_path / "out"),
        ]
    )
    assert code == 2 and not (tmp_path / "out").exists()
    assert "Invalid runtime contract" in capsys.readouterr().err


@pytest.mark.parametrize("malformed", ["empty", "bad_digest", "wrong_name"])
def test_input_fingerprint_structure_is_checked_even_with_valid_hashes(
    tmp_path, monkeypatch, malformed
):
    import csv
    import io

    _, source, manifest = synthetic_history(tmp_path, monkeypatch)
    source.chmod(0o700)
    fingerprint = source / "input_sha256.tsv"
    rows = list(csv.DictReader(fingerprint.read_text().splitlines(), delimiter="\t"))
    header = ("logical_name", "source_name", "size", "sha256")
    if malformed == "empty":
        rows = []
    if malformed == "bad_digest":
        rows[0]["sha256"] = "not-a-hash"
    if malformed == "wrong_name":
        rows[0]["source_name"] = "unapproved.tsv"
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer, fieldnames=header, delimiter="\t", lineterminator="\n"
    )
    writer.writeheader()
    writer.writerows(rows)
    fingerprint.chmod(0o600)
    fingerprint.write_text(buffer.getvalue())
    output_manifest = source / "output_sha256.tsv"
    output_manifest.chmod(0o600)
    output_manifest.write_text(
        "path\tsize\tsha256\n"
        + "".join(
            f"{p.name}\t{p.stat().st_size}\t{hashlib.sha256(p.read_bytes()).hexdigest()}\n"
            for p in sorted(source.iterdir())
            if p.name != "output_sha256.tsv"
        )
    )
    manifest.write_text(json.dumps(describe_source(source)))
    with pytest.raises(ValueError):
        revalidate(
            source_manifest=manifest,
            runtime_contract=POLICY,
            revalidation_id="invalid-fingerprint",
            output_root=tmp_path / "derived",
        )
