"""Deterministic tests for the model-agnostic Codex CLI agent harness."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from dispatcher_for_codex_agents.agent_harness import (
    AgentTask,
    CodexCliAdapter,
    FailureCode,
    ModelProfile,
    PayloadBuilder,
    SchemaValidationStatus,
    ShardExistsError,
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


def _write_input(path: Path) -> None:
    path.write_text(
        "record_id\tdescription\tprivate_note\n"
        "FIC-002\tSecond fictional description.\tsecret-2\n"
        "FIC-001\tFirst fictional description.\tsecret-1\n",
        encoding="utf-8",
    )


def _write_codex_home(path: Path) -> None:
    path.mkdir()
    (path / "config.toml").write_text(
        '[model_providers.fake]\nname = "Fake Provider"\n',
        encoding="utf-8",
    )
    (path / "fake-profile.config.toml").write_text(
        'model = "fake-model"\nmodel_provider = "fake"\n',
        encoding="utf-8",
    )


def _task(input_path: Path, **updates: object) -> AgentTask:
    values: dict[str, object] = {
        "task_id": "fictional-task",
        "role": "title_description_agent",
        "prompt_template": "Classify the fictional record; use no tools.",
        "approved_input_files": [str(input_path)],
        "selected_columns": ["record_id", "description"],
        "timeout": 2.0,
        "call_limit": 1,
        "expected_output_schema": _schema(),
    }
    values.update(updates)
    return AgentTask.model_validate(values)


def _profile(**updates: object) -> ModelProfile:
    values: dict[str, object] = {
        "profile_id": "fake-profile",
        "capabilities": {
            "native_output_schema": True,
            "served_model_allowlist": ["fake-model"],
        },
        "adapter_id": "codex_cli",
    }
    values.update(updates)
    return ModelProfile.model_validate(values)


def _adapter(tmp_path: Path, mode: str) -> tuple[CodexCliAdapter, AgentTask]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    input_path = tmp_path / "fictional.tsv"
    codex_home = tmp_path / "codex-home"
    _write_input(input_path)
    _write_codex_home(codex_home)
    adapter = CodexCliAdapter(
        executable=(sys.executable, str(FAKE_CODEX)),
        codex_home=codex_home,
        environment={"FAKE_CODEX_MODE": mode, "FAKE_EXPECT_SCHEMA": "1"},
        termination_grace_seconds=0.1,
    )
    return adapter, _task(input_path)


def _shard(tmp_path: Path, attempt_id: str) -> Path:
    return tmp_path / "run" / "workers" / "fictional-task" / "fake-profile" / attempt_id


def test_contracts_keep_model_profile_model_and_credentials_out() -> None:
    assert set(ModelProfile.model_fields) == {
        "profile_id",
        "capabilities",
        "adapter_id",
    }
    for forbidden in ("provider", "model", "api_key"):
        with pytest.raises(ValidationError):
            ModelProfile.model_validate(
                {
                    "profile_id": "fake-profile",
                    "capabilities": {},
                    "adapter_id": "codex_cli",
                    forbidden: "forbidden",
                }
            )


def test_payload_is_allowlisted_column_selected_sorted_and_path_redacted(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "fictional.tsv"
    _write_input(input_path)

    payload = PayloadBuilder(allowed_roots=(tmp_path,)).build(_task(input_path))

    assert str(input_path.resolve()) not in payload.content
    assert "private_note" not in payload.content
    assert "secret-1" not in payload.content
    assert payload.content.index("FIC-001") < payload.content.index("FIC-002")
    assert payload.content.startswith("DCA_AGENT_TASK_V1\n")
    assert payload.content.endswith("DCA_AGENT_TASK_END\n")
    assert (
        payload.input_records[0].sha256
        == hashlib.sha256(input_path.read_bytes()).hexdigest()
    )


def test_success_captures_terminal_usage_and_immutable_hashed_shard(
    tmp_path: Path,
) -> None:
    adapter, task = _adapter(tmp_path, "success")

    result = adapter.invoke(
        task=task,
        profile=_profile(),
        attempt_id="attempt-success",
        workers_root=tmp_path / "run",
        payload_builder=PayloadBuilder(allowed_roots=(tmp_path,)),
    )

    assert result.status == "success"
    assert result.exit_code == 0
    assert result.turn_completed
    assert result.schema_validation_status == SchemaValidationStatus.PASS
    assert result.final_output["decision"] == "TYPE_A"
    assert result.usage == {
        "cached_input_tokens": 3,
        "input_tokens": 17,
        "output_tokens": 9,
        "reasoning_output_tokens": 2,
    }
    assert result.provenance["configured_model"] == "fake-model"
    assert result.provenance["configured_provider"] == "fake"
    assert result.provenance["provider_reported_served_model"] == "fake-model"
    assert result.provenance["fallback_profiles"] == []
    assert adapter.calls_started == 1

    shard = _shard(tmp_path, "attempt-success")
    expected_files = {
        "events.jsonl",
        "final_output.json",
        "input_sha256.tsv",
        "invocation_result.json",
        "model_profile.snapshot.redacted.json",
        "output_sha256.tsv",
        "agent_task.snapshot.json",
        "stderr.log",
    }
    assert {path.name for path in shard.iterdir()} == expected_files
    assert not os.access(shard, os.W_OK)

    with (shard / "output_sha256.tsv").open(
        "r", encoding="utf-8", newline=""
    ) as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    assert {row["path"] for row in rows} == expected_files - {"output_sha256.tsv"}
    for row in rows:
        content = (shard / row["path"]).read_bytes()
        assert int(row["size"]) == len(content)
        assert row["sha256"] == hashlib.sha256(content).hexdigest()

    profile_snapshot = json.loads(
        (shard / "model_profile.snapshot.redacted.json").read_text(encoding="utf-8")
    )
    assert set(profile_snapshot) == {"profile_id", "capabilities", "adapter_id"}
    assert {"provider", "model", "api_key"}.isdisjoint(profile_snapshot)

    second_adapter, _ = _adapter(tmp_path / "second", "success")
    with pytest.raises(ShardExistsError):
        second_adapter.invoke(
            task=task,
            profile=_profile(),
            attempt_id="attempt-success",
            workers_root=tmp_path / "run",
        )


@pytest.mark.parametrize(
    ("mode", "expected_failure"),
    [
        ("nonzero", FailureCode.CLI_NONZERO_EXIT),
        ("missing_turn_completed", FailureCode.TURN_COMPLETED_MISSING),
        ("invalid_jsonl", FailureCode.EVENT_STREAM_INVALID),
        ("rate_limit", FailureCode.PROVIDER_RATE_LIMIT_429),
        ("prefill", FailureCode.PROVIDER_PREFILL_PARAMETER_ERROR),
        ("partial", FailureCode.PROVIDER_PARTIAL_PARAMETER_ERROR),
        ("schema_invalid", FailureCode.OUTPUT_SCHEMA_INVALID),
        ("policy", FailureCode.POLICY_VIOLATION),
        ("silent_fallback", FailureCode.SILENT_FALLBACK_DETECTED),
    ],
)
def test_failure_injection_produces_structured_record(
    tmp_path: Path, mode: str, expected_failure: FailureCode
) -> None:
    adapter, task = _adapter(tmp_path, mode)
    attempt_id = f"attempt-{mode.replace('_', '-')}"

    result = adapter.invoke(
        task=task,
        profile=_profile(),
        attempt_id=attempt_id,
        workers_root=tmp_path / "run",
        payload_builder=PayloadBuilder(allowed_roots=(tmp_path,)),
    )

    assert result.status == "failure"
    assert result.failure_code == expected_failure
    assert (_shard(tmp_path, attempt_id) / "invocation_result.json").is_file()
    assert adapter.calls_started == 1


def test_hard_timeout_terminates_process_group_and_records_failure(
    tmp_path: Path,
) -> None:
    adapter, task = _adapter(tmp_path, "timeout")
    short_task = task.model_copy(update={"timeout": 0.1})

    result = adapter.invoke(
        task=short_task,
        profile=_profile(),
        attempt_id="attempt-timeout",
        workers_root=tmp_path / "run",
    )

    assert result.status == "failure"
    assert result.failure_code == FailureCode.TIMEOUT
    assert result.latency_seconds < 3
    assert adapter.calls_started == 1


def test_missing_served_model_is_not_inferred(tmp_path: Path) -> None:
    adapter, task = _adapter(tmp_path, "success_no_served")

    result = adapter.invoke(
        task=task,
        profile=_profile(),
        attempt_id="attempt-no-served-model",
        workers_root=tmp_path / "run",
    )

    assert result.status == "success"
    assert result.provenance["provider_reported_served_model"] == "NOT_REPORTED"
    assert FailureCode.SERVED_MODEL_NOT_REPORTED.value in result.warnings


def test_call_limit_fails_without_starting_a_fallback_call(tmp_path: Path) -> None:
    adapter, task = _adapter(tmp_path, "success")
    profile = _profile()
    first = adapter.invoke(
        task=task,
        profile=profile,
        attempt_id="attempt-one",
        workers_root=tmp_path / "run",
    )
    second = adapter.invoke(
        task=task,
        profile=profile,
        attempt_id="attempt-two",
        workers_root=tmp_path / "run",
    )

    assert first.status == "success"
    assert second.failure_code == FailureCode.CALL_LIMIT_EXCEEDED
    assert adapter.calls_started == 1
    assert second.provenance["fallback_profiles"] == []


def test_model_profile_rejects_nested_credentials() -> None:
    with pytest.raises(ValidationError, match="forbidden"):
        ModelProfile.model_validate(
            {
                "profile_id": "fake-profile",
                "capabilities": {"transport": {"api_key": "must-not-persist"}},
                "adapter_id": "codex_cli",
            }
        )


def test_provider_terms_in_scientific_output_are_not_failure_signals(
    tmp_path: Path,
) -> None:
    adapter, task = _adapter(tmp_path, "success_science_terms")

    result = adapter.invoke(
        task=task,
        profile=_profile(),
        attempt_id="attempt-science-terms",
        workers_root=tmp_path / "run",
    )

    assert result.status == "success"
    assert result.final_output["reason"] == "Item 429 used partial prefill."


def test_schema_invalid_json_is_preserved_as_structured_output(tmp_path: Path) -> None:
    adapter, task = _adapter(tmp_path, "schema_invalid")

    result = adapter.invoke(
        task=task,
        profile=_profile(),
        attempt_id="attempt-schema-preserved",
        workers_root=tmp_path / "run",
    )

    assert result.failure_code == FailureCode.OUTPUT_SCHEMA_INVALID
    assert result.final_output == {"decision": "MAYBE", "reason": "invalid enum"}


def test_business_constraint_in_declared_schema_remains_a_hard_gate(
    tmp_path: Path,
) -> None:
    adapter, task = _adapter(tmp_path, "success")
    schema = _schema()
    schema["properties"]["decision"]["enum"] = ["TYPE_B"]
    task = task.model_copy(update={"expected_output_schema": schema})
    result = adapter.invoke(
        task=task,
        profile=_profile(),
        attempt_id="explicit-business-schema",
        workers_root=tmp_path / "run",
    )
    assert result.failure_code == FailureCode.OUTPUT_SCHEMA_INVALID
    assert result.status == "failure"
    assert result.final_output["decision"] == "TYPE_A"


def test_core_has_no_model_brand_branching() -> None:
    import ast

    source_root = (
        Path(__file__).parents[2]
        / "src"
        / "dispatcher_for_codex_agents"
        / "agent_harness"
    )
    # Declaring reserved adapter ids is allowed; scientific/control decisions
    # must not branch on a model brand, including conditional expressions.
    for path in sorted(source_root.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.If, ast.IfExp, ast.While)):
                expression = ast.dump(node.test).casefold()
                assert not any(
                    brand in expression for brand in ("glm", "deepseek", "kimi")
                )
