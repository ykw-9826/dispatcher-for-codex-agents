from __future__ import annotations

import csv
import json
import os
from pathlib import Path

import pytest

from dispatcher_for_codex_agents.agent_harness.batch import (
    BatchError,
    collect_batch,
    create_retry_plan,
    load_batch_plan,
    plan_batch,
    run_batch,
    status_batch,
)
from dispatcher_for_codex_agents.agent_harness.cli import CliExitCode, main

FAKE_CODEX = Path(__file__).parents[1] / "fixtures" / "fake_codex_cli.py"


def _write_json(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _write_tsv(path: Path, fields: tuple[str, ...], rows: list[dict[str, str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=list(fields),
            delimiter="\t",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)
    return path


def _schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "record_id": {"type": "string"},
                        "decision": {
                            "type": "string",
                            "enum": ["TYPE_A", "TYPE_B"],
                        },
                        "confidence": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                        },
                    },
                    "required": ["record_id", "decision", "confidence"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["results"],
        "additionalProperties": False,
    }


def _profiles(count: int = 3) -> list[dict[str, object]]:
    authority = ("authoritative", "shadow", "diagnostic")
    return [
        {
            "profile_id": f"profile-{index + 1}",
            "role": f"agent-role-{index + 1}",
            "authority_class": authority[index],
            "execution_order": index + 1,
        }
        for index in range(count)
    ]


def _inputs(root: Path, *, records: int = 5, profiles: int = 3) -> dict[str, Path]:
    source = _write_tsv(
        root / "source.tsv",
        ("batch_id", "record_id", "title", "description"),
        [
            {
                "batch_id": "BATCH-A" if index <= records else "BATCH-B",
                "record_id": f"REC-{index:03d}",
                "title": f"Fictional title {index}",
                "description": f"Fictional description {index}",
            }
            for index in range(1, records + 2)
        ],
    )
    profile_config = _write_json(
        root / "profiles.json", {"profiles": _profiles(profiles)}
    )
    prompt = root / "prompt.txt"
    prompt.write_text(
        "Process each fictional row and return only the required JSON.",
        encoding="utf-8",
    )
    schema = _write_json(root / "schema.json", _schema())
    return {
        "source": source,
        "profiles": profile_config,
        "prompt": prompt,
        "schema": schema,
    }


def _plan(
    root: Path,
    *,
    records: int = 5,
    profiles: int = 3,
    shard_size: int = 2,
    timeout: float = 1.0,
) -> Path:
    inputs = _inputs(root / "configuration", records=records, profiles=profiles)
    output = root / "batch"
    report = plan_batch(
        source_tsv=inputs["source"],
        batch_id="BATCH-A",
        record_id_column="record_id",
        selected_columns=("record_id", "title", "description"),
        profile_role_config=inputs["profiles"],
        shard_size=shard_size,
        prompt_template=inputs["prompt"],
        expected_output_schema=inputs["schema"],
        output_root=output,
        timeout=timeout,
    )
    assert report["model_calls_started"] == 0
    return output


def _fake_home(root: Path, profiles: int = 3) -> Path:
    home = root / "codex-home"
    home.mkdir()
    for profile in _profiles(profiles):
        (home / f"{profile['profile_id']}.config.toml").write_text(
            'model = "fake-model"\nmodel_provider = "fake-provider"\n',
            encoding="utf-8",
        )
    return home


def _fake_environment(
    monkeypatch: pytest.MonkeyPatch, root: Path, mode: str = "batch_success"
) -> Path:
    log = root / "agent-calls.log"
    monkeypatch.setenv("FAKE_CODEX_MODE", mode)
    monkeypatch.setenv("FAKE_EXPECT_SCHEMA", "0")
    monkeypatch.setenv("FAKE_CODEX_CALL_LOG", str(log))
    return log


def _read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def _assert_hash_manifest(directory: Path, name: str) -> None:
    rows = _read_tsv(directory / name)
    for row in rows:
        content = (directory / row["path"]).read_bytes()
        assert len(content) == int(row["size"])
        import hashlib

        assert hashlib.sha256(content).hexdigest() == row["sha256"]


def test_semantic_oracle_mismatch_is_diagnostic_collector_preserves_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _plan(tmp_path, records=1, profiles=1)
    home = _fake_home(tmp_path, profiles=1)
    calls = _fake_environment(monkeypatch, tmp_path)
    report = run_batch(
        plan_root=root,
        run_id="execution",
        executable=str(FAKE_CODEX),
        codex_home=home,
    )
    assert report["status"] == "PASS"
    attempt = next(root.glob("workers/*/*/*/invocation_result.json"))
    original = attempt.read_bytes()
    result = json.loads(original)
    returned = result["final_output"]["results"][0]
    business_oracle = "TYPE_B"
    assert returned["decision"] == "TYPE_A" != business_oracle
    assert result["status"] == "success"
    assert result["schema_validation_status"] == "PASS"
    assert result["provenance"]["configured_provider"] == "fake-provider"
    collection = collect_batch(plan_root=root, collection_id="execution")
    assert collection["status"] == "PASS"
    collected = _read_tsv(root / "collections/execution/agent_results_by_profile.tsv")
    assert json.loads(collected[0]["result_json"]) == returned
    assert attempt.read_bytes() == original
    assert len(calls.read_text().splitlines()) == 1
    _assert_hash_manifest(attempt.parent, "output_sha256.tsv")


def test_plan_is_deterministic_shared_membership_immutable_and_no_overwrite(
    tmp_path: Path,
) -> None:
    first = _plan(tmp_path / "first")
    second = _plan(tmp_path / "second")
    first_plan = first / "plan"
    second_plan = second / "plan"
    for name in (
        "batch_plan.snapshot.json",
        "shard_plan.tsv",
        "input_sha256.tsv",
        "profile_role_ledger.tsv",
        "plan_sha256.tsv",
    ):
        assert (first_plan / name).read_bytes() == (second_plan / name).read_bytes()
    loaded = load_batch_plan(first)
    assert [item.record_ids for item in loaded.shards] == [
        ("REC-001", "REC-002"),
        ("REC-003", "REC-004"),
        ("REC-005",),
    ]
    for shard in loaded.shards:
        task_inputs = {
            json.loads(
                (
                    first_plan / "tasks" / profile.profile_id / f"{shard.shard_id}.json"
                ).read_text(encoding="utf-8")
            )["approved_input_files"][0]
            for profile in loaded.snapshot.profiles
        }
        assert task_inputs == {f"../../inputs/{shard.shard_id}.tsv"}
    assert not os.access(first_plan, os.W_OK)
    _assert_hash_manifest(first_plan, "plan_sha256.tsv")
    with pytest.raises(BatchError, match="already exists"):
        plan_batch(
            source_tsv=tmp_path / "first/configuration/source.tsv",
            batch_id="BATCH-A",
            record_id_column="record_id",
            selected_columns=("record_id", "title", "description"),
            profile_role_config=tmp_path / "first/configuration/profiles.json",
            shard_size=2,
            prompt_template=tmp_path / "first/configuration/prompt.txt",
            expected_output_schema=tmp_path / "first/configuration/schema.json",
            output_root=first,
        )


def test_plan_rejects_unsupported_adapter_before_creating_output(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path / "configuration")
    profiles = json.loads(inputs["profiles"].read_text(encoding="utf-8"))
    profiles["profiles"][0]["adapter_id"] = "unsupported-adapter"
    _write_json(inputs["profiles"], profiles)
    output = tmp_path / "batch"
    with pytest.raises(BatchError, match="Profile-role config validation failed"):
        plan_batch(
            source_tsv=inputs["source"],
            batch_id="BATCH-A",
            record_id_column="record_id",
            selected_columns=("record_id", "title", "description"),
            profile_role_config=inputs["profiles"],
            shard_size=2,
            prompt_template=inputs["prompt"],
            expected_output_schema=inputs["schema"],
            output_root=output,
        )
    assert not output.exists()


def test_batch_dry_run_starts_no_process_and_max_workers_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _plan(tmp_path)
    home = _fake_home(tmp_path)
    log = _fake_environment(monkeypatch, tmp_path)
    report = run_batch(
        plan_root=root,
        run_id="dry-run",
        max_workers=2,
        dry_run=True,
        codex_home=home,
        executable=str(FAKE_CODEX),
    )
    assert report["status"] == "DRY_RUN_VALIDATED"
    assert report["scheduled_invocations"] == 9
    assert report["model_calls_started"] == 0
    assert not log.exists() and not (root / "workers").exists()
    with pytest.raises(BatchError, match="max_workers"):
        run_batch(
            plan_root=root,
            run_id="invalid-workers",
            max_workers=3,
            codex_home=home,
            executable=str(FAKE_CODEX),
        )


def test_success_resume_skip_status_collect_and_authority_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _plan(tmp_path)
    home = _fake_home(tmp_path)
    log = _fake_environment(monkeypatch, tmp_path)
    first = run_batch(
        plan_root=root,
        run_id="run-001",
        max_workers=2,
        codex_home=home,
        executable=str(FAKE_CODEX),
    )
    assert first["status"] == "PASS" and first["executed_count"] == 9
    assert len(log.read_text(encoding="utf-8").splitlines()) == 9
    second = run_batch(
        plan_root=root,
        run_id="run-002",
        codex_home=home,
        executable=str(FAKE_CODEX),
    )
    assert second["executed_count"] == 0
    assert second["skipped_existing_success"] == 9
    assert len(log.read_text(encoding="utf-8").splitlines()) == 9
    status = status_batch(plan_root=root, snapshot_id="after-resume")
    assert status["status_counts"] == {"SKIPPED_EXISTING_SUCCESS": 9}
    collection = collect_batch(plan_root=root, collection_id="complete")
    assert collection["status"] == "PASS"
    directory = root / "collections/complete"
    assert len(_read_tsv(directory / "agent_results_by_profile.tsv")) == 15
    assert len(_read_tsv(directory / "authoritative_results.tsv")) == 5
    assert len(_read_tsv(directory / "shadow_results.tsv")) == 5
    assert len(_read_tsv(directory / "diagnostic_results.tsv")) == 5
    assert all(
        row["authority_class"] == "authoritative"
        for row in _read_tsv(directory / "authoritative_results.tsv")
    )
    assert {
        row["coverage_status"] for row in _read_tsv(directory / "coverage_audit.tsv")
    } == {"PASS"}
    _assert_hash_manifest(directory, "collection_manifest.tsv")
    assert not os.access(directory, os.W_OK)


def test_failed_shard_is_blocked_until_explicit_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _plan(tmp_path, records=2, profiles=1, shard_size=2)
    home = _fake_home(tmp_path, profiles=1)
    log = _fake_environment(monkeypatch, tmp_path, "rate_limit")
    failed = run_batch(
        plan_root=root,
        run_id="failed",
        codex_home=home,
        executable=str(FAKE_CODEX),
    )
    assert failed["status"] == "PARTIAL_OR_BLOCKED"
    assert len(log.read_text(encoding="utf-8").splitlines()) == 1
    blocked = run_batch(
        plan_root=root,
        run_id="blocked",
        codex_home=home,
        executable=str(FAKE_CODEX),
    )
    assert blocked["executed_count"] == 0
    assert len(log.read_text(encoding="utf-8").splitlines()) == 1

    request = _write_json(
        tmp_path / "retry-request.json",
        {
            "retry_plan_id": "retry-001",
            "entries": [
                {
                    "profile_id": "profile-1",
                    "shard_id": "shard-0001",
                    "parent_attempt_id": "attempt-001",
                    "new_attempt_id": "attempt-002",
                    "human_approval_reason": "Human approved one bounded retry.",
                }
            ],
        },
    )
    retry = create_retry_plan(plan_root=root, retry_request=request)
    assert retry["model_calls_started"] == 0
    retry_rows = _read_tsv(root / "retry_plans/retry-001/retry_shards.tsv")
    assert retry_rows[0]["parent_failure_code"] == "PROVIDER_RATE_LIMIT_429"
    assert retry_rows[0]["attempt_id"] == "attempt-002"
    monkeypatch.setenv("FAKE_CODEX_MODE", "batch_success")
    recovered = run_batch(
        plan_root=root,
        run_id="retry-run",
        retry_plan="retry-001",
        codex_home=home,
        executable=str(FAKE_CODEX),
    )
    assert recovered["status"] == "PASS" and recovered["executed_count"] == 1
    assert len(log.read_text(encoding="utf-8").splitlines()) == 2
    assert collect_batch(plan_root=root, collection_id="recovered")["status"] == "PASS"
    with pytest.raises(BatchError, match="Successful profile/shard attempts"):
        create_retry_plan(
            plan_root=root,
            retry_request=_write_json(
                tmp_path / "bad-retry.json",
                {
                    "retry_plan_id": "retry-002",
                    "entries": [
                        {
                            "profile_id": "profile-1",
                            "shard_id": "shard-0001",
                            "parent_attempt_id": "attempt-001",
                            "new_attempt_id": "attempt-003",
                            "human_approval_reason": "Invalid branch retry.",
                        }
                    ],
                },
            ),
        )


def test_retry_attempt_id_is_scoped_to_each_profile_shard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _plan(tmp_path, records=3, profiles=1, shard_size=2)
    home = _fake_home(tmp_path, profiles=1)
    _fake_environment(monkeypatch, tmp_path, "rate_limit")
    run_batch(
        plan_root=root,
        run_id="failed-two-shards",
        codex_home=home,
        executable=str(FAKE_CODEX),
    )
    request = _write_json(
        tmp_path / "retry-two-shards.json",
        {
            "retry_plan_id": "retry-two-shards",
            "entries": [
                {
                    "profile_id": "profile-1",
                    "shard_id": shard_id,
                    "parent_attempt_id": "attempt-001",
                    "new_attempt_id": "attempt-002",
                    "human_approval_reason": "Human approved bounded retry.",
                }
                for shard_id in ("shard-0001", "shard-0002")
            ],
        },
    )
    report = create_retry_plan(plan_root=root, retry_request=request)
    assert report["entry_count"] == 2
    rows = _read_tsv(root / "retry_plans/retry-two-shards/retry_shards.tsv")
    assert {row["attempt_id"] for row in rows} == {"attempt-002"}
    assert {row["shard_id"] for row in rows} == {"shard-0001", "shard-0002"}


def test_incomplete_attempt_is_never_overwritten_and_can_be_planned_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _plan(tmp_path, records=2, profiles=1, shard_size=2)
    loaded = load_batch_plan(root)
    task_id = f"{loaded.snapshot.task_id_prefix}-shard-0001"
    incomplete = root / f"workers/{task_id}/profile-1/attempt-001"
    incomplete.mkdir(parents=True)
    (incomplete / "events.jsonl").write_text("", encoding="utf-8")
    home = _fake_home(tmp_path, profiles=1)
    log = _fake_environment(monkeypatch, tmp_path)
    result = run_batch(
        plan_root=root,
        run_id="incomplete",
        codex_home=home,
        executable=str(FAKE_CODEX),
    )
    assert result["executed_count"] == 0
    assert not log.exists()
    ledger = _read_tsv(root / "batch_runs/incomplete/batch_run_ledger.tsv")
    assert ledger[0]["action"] == "INCOMPLETE_ATTEMPT"
    assert status_batch(plan_root=root, snapshot_id="incomplete-status")[
        "status_counts"
    ] == {"RUNNING_OR_INCOMPLETE": 1}
    request = _write_json(
        tmp_path / "retry-incomplete.json",
        {
            "retry_plan_id": "retry-incomplete",
            "entries": [
                {
                    "profile_id": "profile-1",
                    "shard_id": "shard-0001",
                    "parent_attempt_id": "attempt-001",
                    "new_attempt_id": "attempt-002",
                    "human_approval_reason": "Human confirmed interrupted process.",
                }
            ],
        },
    )
    create_retry_plan(plan_root=root, retry_request=request)
    row = _read_tsv(root / "retry_plans/retry-incomplete/retry_shards.tsv")[0]
    assert row["parent_failure_code"] == "INCOMPLETE_ATTEMPT"


def test_interruption_recovery_runs_only_never_started_shards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _plan(tmp_path, records=4, profiles=1, shard_size=2)
    home = _fake_home(tmp_path, profiles=1)
    log = _fake_environment(monkeypatch, tmp_path)
    loaded = load_batch_plan(root)
    task_path = root / "plan/tasks/profile-1/shard-0001.json"
    assert (
        main(
            [
                "invoke",
                "--task",
                str(task_path),
                "--profile",
                "profile-1",
                "--attempt-id",
                "attempt-001",
                "--shard-root",
                str(root),
                "--codex-home",
                str(home),
                "--codex-executable",
                str(FAKE_CODEX),
            ]
        )
        == CliExitCode.OK
    )
    capsys.readouterr()
    assert (root / f"workers/{loaded.snapshot.task_id_prefix}-shard-0001").exists()
    recovered = run_batch(
        plan_root=root,
        run_id="after-interruption",
        codex_home=home,
        executable=str(FAKE_CODEX),
    )
    assert recovered["executed_count"] == 1
    assert recovered["skipped_existing_success"] == 1
    assert len(log.read_text(encoding="utf-8").splitlines()) == 2


@pytest.mark.parametrize("mode", ["batch_missing", "batch_duplicate", "batch_extra"])
def test_collection_fails_closed_for_invalid_id_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    root = _plan(tmp_path, records=2, profiles=1, shard_size=2)
    home = _fake_home(tmp_path, profiles=1)
    _fake_environment(monkeypatch, tmp_path, mode)
    run_batch(
        plan_root=root,
        run_id="coverage-run",
        codex_home=home,
        executable=str(FAKE_CODEX),
    )
    collection = collect_batch(plan_root=root, collection_id="coverage")
    assert collection["status"] == "FAIL"
    audit = _read_tsv(root / "collections/coverage/coverage_audit.tsv")
    assert audit[0]["coverage_status"] == "FAIL"


def test_batch_cli_plan_status_and_dry_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inputs = _inputs(tmp_path / "configuration", records=3, profiles=1)
    root = tmp_path / "cli-batch"
    code = main(
        [
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
            "--selected-column",
            "title",
            "--selected-column",
            "description",
            "--profile-role-config",
            str(inputs["profiles"]),
            "--shard-size",
            "2",
            "--prompt-template",
            str(inputs["prompt"]),
            "--expected-output-schema",
            str(inputs["schema"]),
            "--output-root",
            str(root),
            "--timeout",
            "1",
        ]
    )
    assert code == CliExitCode.OK
    assert json.loads(capsys.readouterr().out)["status"] == "PLAN_CREATED"
    home = _fake_home(tmp_path, profiles=1)
    log = _fake_environment(monkeypatch, tmp_path)
    code = main(
        [
            "batch",
            "run",
            "--plan-root",
            str(root),
            "--run-id",
            "cli-dry",
            "--dry-run",
            "--codex-home",
            str(home),
            "--codex-executable",
            str(FAKE_CODEX),
        ]
    )
    assert code == CliExitCode.OK
    assert json.loads(capsys.readouterr().out)["status"] == "DRY_RUN_VALIDATED"
    assert not log.exists()
    assert (
        main(
            [
                "batch",
                "status",
                "--plan-root",
                str(root),
                "--snapshot-id",
                "cli-status",
            ]
        )
        == CliExitCode.OK
    )
    assert json.loads(capsys.readouterr().out)["status_counts"] == {"PLANNED": 2}


def test_batch_core_has_no_model_brand_branches() -> None:
    package = (
        Path(__file__).parents[2]
        / "src"
        / "dispatcher_for_codex_agents"
        / "agent_harness"
    )
    source = "\n".join(
        (package / name).read_text(encoding="utf-8") for name in ("batch.py", "cli.py")
    ).casefold()
    assert not any(brand in source for brand in ("glm", "deepseek", "kimi"))
