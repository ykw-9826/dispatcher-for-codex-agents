"""CLI tests for the bounded agent harness."""

from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path

import pytest

from dispatcher_for_codex_agents.agent_harness import AgentTask, FailureCode
from dispatcher_for_codex_agents.agent_harness.cli import (
    CliExitCode,
    exit_code_for_result,
    main,
)
from dispatcher_for_codex_agents.agent_harness.contracts import (
    InvocationResult,
    InvocationStatus,
    SchemaValidationStatus,
)

FAKE_CODEX = Path(__file__).parents[1] / "fixtures" / "fake_codex_cli.py"


def _schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "decision": {"type": "string", "enum": ["TYPE_A", "TYPE_B"]},
            "reason": {"type": "string", "minLength": 1},
        },
        "required": ["decision", "reason"],
        "additionalProperties": False,
    }


def _write_task(tmp_path: Path, schema: dict[str, object] | None = None) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    input_path = tmp_path / "fictional-input.tsv"
    input_path.write_text(
        "record_id\tdescription\tprivate_note\n"
        "FIC-002\tSecond fictional description.\tsecret-two\n"
        "FIC-001\tFirst fictional description.\tsecret-one\n",
        encoding="utf-8",
    )
    task = AgentTask(
        task_id="cli-task",
        role="acceptance-agent",
        prompt_template="Process every fictional row without tools.",
        approved_input_files=(str(input_path),),
        selected_columns=("record_id", "description"),
        timeout=2,
        call_limit=1,
        expected_output_schema=schema or _schema(),
    )
    path = tmp_path / "agent-task.json"
    path.write_text(task.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def _write_home(path: Path, profiles: tuple[str, ...]) -> None:
    path.mkdir()
    (path / "config.toml").write_text(
        '[model_providers.fake]\nname = "Fake Provider"\n', encoding="utf-8"
    )
    for profile in profiles:
        (path / f"{profile}.config.toml").write_text(
            'model = "fake-model"\nmodel_provider = "fake"\n',
            encoding="utf-8",
        )


def _args(
    task: Path,
    home: Path,
    root: Path,
    profile: str,
    attempt: str,
    *,
    dry: bool = False,
) -> list[str]:
    args = [
        "invoke",
        "--task",
        str(task),
        "--profile",
        profile,
        "--attempt-id",
        attempt,
        "--shard-root",
        str(root),
        "--codex-home",
        str(home),
        "--codex-executable",
        str(FAKE_CODEX),
    ]
    if dry:
        args.append("--dry-run")
    return args


def _result(code: FailureCode | None) -> InvocationResult:
    success = code is None
    return InvocationResult(
        status=InvocationStatus.SUCCESS if success else InvocationStatus.FAILURE,
        exit_code=0 if success else 1,
        turn_completed=success,
        final_output={"decision": "TYPE_A", "reason": "ok"} if success else None,
        schema_validation_status=(
            SchemaValidationStatus.PASS if success else SchemaValidationStatus.NOT_RUN
        ),
        usage={},
        provenance={},
        warnings=(),
        failure_code=code,
        latency_seconds=0,
    )


def _verify_hashes(shard: Path) -> None:
    with (shard / "output_sha256.tsv").open(
        "r", encoding="utf-8", newline=""
    ) as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    for row in rows:
        content = (shard / row["path"]).read_bytes()
        assert int(row["size"]) == len(content)
        assert row["sha256"] == hashlib.sha256(content).hexdigest()


def test_dry_run_has_no_process_shard_or_absolute_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    task = _write_task(tmp_path)
    home = tmp_path / "home"
    _write_home(home, ("profile-a",))
    call_log = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_CODEX_CALL_LOG", str(call_log))

    code = main(_args(task, home, tmp_path / "run", "profile-a", "dry", dry=True))
    report = json.loads(capsys.readouterr().out)

    assert code == CliExitCode.OK
    assert report["status"] == "DRY_RUN_VALIDATED"
    assert report["agent_process_started"] is False
    assert "--profile" in report["command"] and "resume" not in report["command"]
    assert not call_log.exists() and not (tmp_path / "run").exists()
    assert str((tmp_path / "fictional-input.tsv").resolve()) not in json.dumps(report)


def test_success_is_once_immutable_hashed_and_path_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    task = _write_task(tmp_path)
    home = tmp_path / "home"
    _write_home(home, ("profile-a",))
    log = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_CODEX_MODE", "success")
    monkeypatch.setenv("FAKE_EXPECT_SCHEMA", "0")
    monkeypatch.setenv("FAKE_EXPECT_PROFILE", "profile-a")
    monkeypatch.setenv("FAKE_CODEX_CALL_LOG", str(log))
    args = _args(task, home, tmp_path / "run", "profile-a", "one")

    assert main(args) == CliExitCode.OK
    result = json.loads(capsys.readouterr().out)
    assert result["provenance"]["agent_subprocess_count"] == 1
    shard = tmp_path / "run/workers/cli-task/profile-a/one"
    assert not os.access(shard, os.W_OK)
    _verify_hashes(shard)
    input_path = str((tmp_path / "fictional-input.tsv").resolve())
    assert all(
        input_path not in path.read_text(encoding="utf-8") for path in shard.iterdir()
    )
    snapshot = json.loads(
        (shard / "agent_task.snapshot.json").read_text(encoding="utf-8")
    )
    assert snapshot["approved_input_files"] == ["input_001:fictional-input.tsv"]
    assert main(args) == CliExitCode.SHARD_EXISTS
    capsys.readouterr()
    assert log.read_text(encoding="utf-8").splitlines() == ["agent_subprocess"]


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (None, CliExitCode.OK),
        (FailureCode.PROVIDER_RATE_LIMIT_429, CliExitCode.PROVIDER_FAILURE),
        (FailureCode.PROVIDER_PREFILL_PARAMETER_ERROR, CliExitCode.PROVIDER_FAILURE),
        (FailureCode.PROVIDER_PARTIAL_PARAMETER_ERROR, CliExitCode.PROVIDER_FAILURE),
        (FailureCode.TIMEOUT, CliExitCode.TIMEOUT),
        (FailureCode.CLI_NONZERO_EXIT, CliExitCode.CLI_FAILURE),
        (FailureCode.TURN_COMPLETED_MISSING, CliExitCode.TERMINAL_FAILURE),
        (FailureCode.EVENT_STREAM_INVALID, CliExitCode.TERMINAL_FAILURE),
        (FailureCode.FINAL_OUTPUT_MISSING, CliExitCode.TERMINAL_FAILURE),
        (FailureCode.OUTPUT_SCHEMA_INVALID, CliExitCode.OUTPUT_INVALID),
        (FailureCode.POLICY_VIOLATION, CliExitCode.POLICY_FAILURE),
        (FailureCode.SILENT_FALLBACK_DETECTED, CliExitCode.POLICY_FAILURE),
        (FailureCode.CALL_LIMIT_EXCEEDED, CliExitCode.POLICY_FAILURE),
        (FailureCode.PROFILE_CONFIGURATION_INVALID, CliExitCode.CLI_FAILURE),
        (FailureCode.PAYLOAD_INVALID, CliExitCode.CLI_FAILURE),
    ],
)
def test_exit_code_mapping(failure: FailureCode | None, expected: CliExitCode) -> None:
    assert exit_code_for_result(_result(failure)) == expected


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("rate_limit", CliExitCode.PROVIDER_FAILURE),
        ("missing_turn_completed", CliExitCode.TERMINAL_FAILURE),
        ("invalid_jsonl", CliExitCode.TERMINAL_FAILURE),
        ("policy", CliExitCode.POLICY_FAILURE),
    ],
)
def test_failure_exit_and_shard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
    expected: CliExitCode,
) -> None:
    task = _write_task(tmp_path)
    home = tmp_path / "home"
    _write_home(home, ("profile-a",))
    monkeypatch.setenv("FAKE_CODEX_MODE", mode)
    monkeypatch.setenv("FAKE_EXPECT_SCHEMA", "0")
    code = main(_args(task, home, tmp_path / "run", "profile-a", mode))
    capsys.readouterr()
    assert code == expected
    shard = tmp_path / f"run/workers/cli-task/profile-a/{mode}"
    _verify_hashes(shard)


def test_bad_task_schema_and_profile_start_no_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    _write_home(home, ("profile-a",))
    log = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_CODEX_CALL_LOG", str(log))
    bad = tmp_path / "bad.json"
    bad.write_text("{not-json", encoding="utf-8")
    cases = [(bad, "profile-a", "bad-task")]
    schema = _schema()
    schema["oneOf"] = []
    cases.append((_write_task(tmp_path / "schema", schema), "profile-a", "bad-schema"))
    cases.append((_write_task(tmp_path / "profile"), "missing", "bad-profile"))
    for task, profile, attempt in cases:
        assert (
            main(_args(task, home, tmp_path / attempt, profile, attempt, dry=True))
            == CliExitCode.INPUT_INVALID
        )
        capsys.readouterr()
    assert not log.exists()


def test_three_profiles_make_disjoint_shards_and_exactly_three_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    task = _write_task(tmp_path)
    profiles = ("profile-a", "profile-b", "profile-c")
    home = tmp_path / "home"
    _write_home(home, profiles)
    log = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_CODEX_MODE", "success")
    monkeypatch.setenv("FAKE_EXPECT_SCHEMA", "0")
    monkeypatch.delenv("FAKE_EXPECT_PROFILE", raising=False)
    monkeypatch.setenv("FAKE_CODEX_CALL_LOG", str(log))
    root = tmp_path / "run"
    for profile in profiles:
        assert main(_args(task, home, root, profile, "acceptance")) == CliExitCode.OK
        capsys.readouterr()
    assert log.read_text(encoding="utf-8").splitlines() == ["agent_subprocess"] * 3
    for profile in profiles:
        _verify_hashes(root / f"workers/cli-task/{profile}/acceptance")
