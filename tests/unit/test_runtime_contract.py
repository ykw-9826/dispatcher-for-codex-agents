"""Adversarial interpretation tests. All events are synthetic, no model calls."""

import hashlib
import json
from itertools import permutations

import pytest
from pydantic import ValidationError

from dispatcher_for_codex_agents.agent_harness.contracts import RuntimeContract
from dispatcher_for_codex_agents.agent_harness.runtime_contract import (
    SYNTHETIC_PROTOCOL,
    classify_activity,
    normalize_output,
)


def rejection(**updates):
    item = {
        "id": "input-1",
        "type": "request_user_input_rejection",
        "tool": "request_user_input",
        "phase": "before_execution",
        "reason": "noninteractive",
        "execution_started": False,
        "required_input": False,
        "required_work_satisfied": True,
    }
    item.update(updates)
    return {"type": "item.completed", "item": item}


def stream(*items, text='{"x":"ok"}'):
    return (
        {"type": "thread.started", "thread_id": "synthetic"},
        {"type": "turn.started"},
        *items,
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "id": "final", "text": text},
        },
        {"type": "turn.completed", "usage": {}},
    )


def activity(*items, version=SYNTHETIC_PROTOCOL, complete=True, stderr=""):
    return classify_activity(
        stream(*items), version=version, complete=complete, stderr=stderr
    )


def test_tool_evidence_and_quoted_errors():
    assert activity()["classifications"] == ["NO_TOOL_ACTIVITY"]
    command = {
        "type": "item.completed",
        "item": {
            "id": "cmd",
            "type": "command_execution",
            "command": "false",
            "status": "failed",
            "exit_code": 1,
        },
    }
    result = activity(command, rejection())
    assert set(result["classifications"]) == {"TOOL_EXECUTED", "TOOL_ATTEMPT_REJECTED"}
    assert result["calls"][0]["outcome"] == "execution_failed"
    assert activity(rejection(), version="codex-cli 0.155.1")["classifications"] == [
        "UNKNOWN_OR_INCOMPLETE"
    ]
    result = classify_activity(
        stream(text='{"x":"request_user_input was rejected"}'),
        version="codex-cli 0.155.1",
        complete=True,
    )
    assert result["confirmed_user_input_rejection_events"] == []
    assert activity(stderr="request_user_input unavailable")["classifications"] == [
        "UNKNOWN_OR_INCOMPLETE"
    ]


@pytest.mark.parametrize(
    "change",
    [
        {"execution_started": True},
        {"required_input": True},
        {"required_work_satisfied": False},
        {"phase": "after_execution"},
        {"extra": "bad"},
    ],
)
def test_rejection_requires_all_preconditions(change):
    result = activity(rejection(**change))
    assert result["classifications"] == ["UNKNOWN_OR_INCOMPLETE"]
    assert result["confirmed_user_input_rejection_events"] == []


def test_conflicting_and_duplicate_lifecycle():
    assert (
        "UNKNOWN_OR_INCOMPLETE" in activity(rejection(), rejection())["classifications"]
    )
    assert (
        activity(rejection(), complete=False)["confirmed_user_input_rejection_events"]
        == []
    )
    assert classify_activity((), version="codex-cli 0.155.1", complete=False)[
        "classifications"
    ] == ["UNKNOWN_OR_INCOMPLETE"]


def test_authorization_and_warning_gate(tmp_path):
    from test_agent_harness import _profile, _task

    from dispatcher_for_codex_agents.agent_harness.adapter import evaluate_capture

    task = _task(tmp_path / "not-read.tsv")

    def check(events, selected):
        return evaluate_capture(
            task=selected,
            profile=_profile(),
            stdout="\n".join(json.dumps(e) for e in events),
            stderr="",
            cli_version=SYNTHETIC_PROTOCOL,
            exit_code=0,
            provenance={
                "agent_process_started": True,
                "configured_model": "fake-model",
            },
        )[0]

    text = '{"decision":"TYPE_A","reason":"done"}'
    assert (
        check(stream(rejection(), text=text), task).failure_code == "POLICY_VIOLATION"
    )
    optin = task.model_copy(
        update={
            "runtime_contract": RuntimeContract(
                rejected_user_input="warn_if_runtime_rejected"
            )
        }
    )
    assert check(stream(rejection(), text=text), optin).status == "success"
    executed = {
        "type": "item.completed",
        "item": {
            "id": "cmd",
            "type": "command_execution",
            "command": "true",
            "exit_code": 0,
            "status": "completed",
        },
    }
    assert (
        check(stream(executed, rejection(), text=text), optin).failure_code
        == "POLICY_VIOLATION"
    )
    assert (
        check(stream(rejection(required_work_satisfied=False), text=text), optin).status
        == "failure"
    )
    assert (
        check(stream(rejection(), text="{}"), optin).failure_code
        == "OUTPUT_SCHEMA_INVALID"
    )


SCHEMA = {
    "type": "object",
    "properties": {"x": {"type": "string"}},
    "required": ["x"],
    "additionalProperties": False,
}


@pytest.mark.parametrize(
    "bad",
    [
        {"structured_output": "repair"},
        {"rejected_user_input": True},
        {"unrestricted": True},
        {"structured_output": None},
    ],
)
def test_policy_fail_closed(bad):
    with pytest.raises(ValidationError):
        RuntimeContract.model_validate(bad)


def test_exact_byte_slicing_and_idempotence():
    raw = b' \t```json\n \n{"x":"Unicode: \\u03bb and ``` and \\n"}\n```\r\n '
    output = normalize_output(
        raw, SCHEMA, RuntimeContract(structured_output="json_or_single_fence")
    )
    assert output.schema_status == "PASS"
    assert (
        output.validator_bytes
        == b' \t \n{"x":"Unicode: \\u03bb and ``` and \\n"}\n\r\n '
    )
    assert output.audit["raw_final_sha256"] == hashlib.sha256(raw).hexdigest()
    again = normalize_output(
        output.validator_bytes,
        SCHEMA,
        RuntimeContract(structured_output="json_or_single_fence"),
    )
    assert again.validator_bytes == output.validator_bytes
    assert again.audit["action"] == "raw"


@pytest.mark.parametrize(
    "raw",
    [
        b'```json\r\n{"x":"a"}\n```',
        b'```JSON\n{"x":"a"}\n```',
        b'```\n{"x":"a"}\n```',
        b'prose\n```json\n{"x":"a"}\n```',
        b'```json\n{"x":"a"}\n``` tail',
        b'```json\n{"x":"a"}',
        b'```json\n{"x":"a"}```',
        b"```json\n{}\n```\n```json\n{}\n```",
        '\u00a0```json\n{"x":"a"}\n```'.encode(),
    ],
)
def test_invalid_wrappers_never_repaired(raw):
    result = normalize_output(
        raw, SCHEMA, RuntimeContract(structured_output="json_or_single_fence")
    )
    assert result.schema_status == "FAIL"


def test_defaults_and_schema_failure_do_not_widen_acceptance():
    assert (
        normalize_output(
            b'```json\n{"x":"a"}\n```', SCHEMA, RuntimeContract()
        ).schema_status
        == "FAIL"
    )
    result = normalize_output(
        b' {"x":1} ', SCHEMA, RuntimeContract(structured_output="json_or_single_fence")
    )
    assert result.schema_status == "FAIL"
    assert result.audit["action"] == "raw"
    assert result.validator_bytes == b' {"x":1} '


def test_number_spelling_and_unicode_preserved():
    raw = '```json\n {"x":1.00e+02,"u":"λ"}\n```'.encode()
    result = normalize_output(
        raw,
        {"type": "object"},
        RuntimeContract(structured_output="json_or_single_fence"),
    )
    assert result.validator_bytes == raw[8:-3]
    assert json.loads(result.validator_bytes)["x"] == 100


@pytest.mark.parametrize(
    "mode,expected", [("strict_json", "failure"), ("json_or_single_fence", "success")]
)
def test_real_invoke_path_capture_and_manifest(tmp_path, monkeypatch, mode, expected):
    from test_agent_harness import _adapter, _profile, _shard

    from dispatcher_for_codex_agents.agent_harness.shard import read_verified_shard

    monkeypatch.setenv("FAKE_FENCE_OUTPUT", "1")
    monkeypatch.setenv("FAKE_JSONL_CRLF", "1")
    adapter, task = _adapter(tmp_path, "success")
    task = task.model_copy(
        update={"runtime_contract": RuntimeContract(structured_output=mode)}
    )
    result = adapter.invoke(
        task=task,
        profile=_profile(),
        attempt_id="format",
        workers_root=tmp_path / "run",
    )
    assert result.status == expected and adapter.calls_started == 1
    files = read_verified_shard(_shard(tmp_path, "format"))
    assert b"\r\n" in files["events.jsonl"]
    audit = json.loads(files["interpretation.json"])
    assert (
        audit["captured_events_sha256"]
        == hashlib.sha256(files["events.jsonl"]).hexdigest()
    )
    raw = files["raw_final_output.bin"]
    assert raw.startswith(b"```json\n")
    if mode != "strict_json":
        ranges = audit["normalization"]["removed_byte_ranges"]
        expected_bytes = (
            raw[: ranges[0][0]] + raw[ranges[0][1] : ranges[1][0]] + raw[ranges[1][1] :]
        )
        assert files["normalized_output.bin"] == expected_bytes
        assert (
            hashlib.sha256(expected_bytes).hexdigest()
            == audit["normalization"]["normalized_sha256"]
        )


def test_malformed_policy_after_reservation_has_immutable_failure(
    tmp_path, monkeypatch
):
    from test_agent_harness import _adapter, _profile, _shard

    from dispatcher_for_codex_agents.agent_harness.shard import (
        ImmutableShardWriter,
        ShardExistsError,
    )

    adapter, task = _adapter(tmp_path, "success")
    task = task.model_copy(update={"runtime_contract": {"structured_output": "repair"}})

    def forbidden(*args, **kwargs):
        raise AssertionError("No subprocess for invalid interpretation contract")

    monkeypatch.setattr("subprocess.Popen", forbidden)
    with pytest.raises(ValueError):
        adapter.preflight(task=task, profile=_profile())
    assert not (tmp_path / "run").exists()
    with pytest.warns(UserWarning, match="Pydantic serializer warnings"):
        result = adapter.invoke(
            task=task,
            profile=_profile(),
            attempt_id="invalid-policy",
            workers_root=tmp_path / "run",
        )
    assert result.failure_code == "PAYLOAD_INVALID" and adapter.calls_started == 0
    assert {p.name for p in _shard(tmp_path, "invalid-policy").iterdir()} == set(
        ImmutableShardWriter.REQUIRED_FILES
    )
    with pytest.raises(ShardExistsError):
        adapter.invoke(
            task=task,
            profile=_profile(),
            attempt_id="invalid-policy",
            workers_root=tmp_path / "run",
        )


@pytest.mark.parametrize(
    "events",
    [
        (),
        ({"type": "turn.completed"},),
        ({"type": "alien"},),
        stream({"type": "item.completed", "item": []}),
        stream({"type": "item.completed", "item": {"type": "unknown_tool", "id": "u"}}),
        stream(
            {
                "type": "item.started",
                "item": {
                    "type": "command_execution",
                    "id": "u",
                    "status": "in_progress",
                },
            }
        ),
    ],
)
def test_unsupported_or_truncated_never_no_activity(events):
    evidence = classify_activity(events, version="codex-cli 0.155.1", complete=False)
    assert "NO_TOOL_ACTIVITY" not in evidence["classifications"]
    assert "UNKNOWN_OR_INCOMPLETE" in evidence["classifications"]


@pytest.mark.parametrize(
    "status,code,expected",
    [
        ("completed", 0, "TOOL_EXECUTED"),
        ("failed", 7, "TOOL_EXECUTED"),
        ("declined", None, "TOOL_ATTEMPT_REJECTED"),
        ("failed", None, "UNKNOWN_OR_INCOMPLETE"),
    ],
)
def test_command_execution_distinct_from_denial(status, code, expected):
    event = {
        "type": "item.completed",
        "item": {
            "id": "c",
            "type": "command_execution",
            "status": status,
            "exit_code": code,
            "command": "fixture",
        },
    }
    assert activity(event, version="codex-cli 0.155.1")["classifications"] == [expected]


def test_authorized_command_failure_is_execution_not_denial(tmp_path):
    from test_agent_harness import _profile, _task

    from dispatcher_for_codex_agents.agent_harness.adapter import evaluate_capture
    from dispatcher_for_codex_agents.agent_harness.contracts import CapabilityPolicy

    task = _task(
        tmp_path / "input.tsv", capability_policy=CapabilityPolicy(tools=("shell",))
    )
    event = {
        "type": "item.completed",
        "item": {
            "id": "c",
            "type": "command_execution",
            "status": "failed",
            "exit_code": 7,
            "command": "false",
            "aggregated_output": "request_user_input rejected",
        },
    }
    result, _ = evaluate_capture(
        task=task,
        profile=_profile(),
        stdout="\n".join(
            json.dumps(e)
            for e in stream(event, text='{"decision":"TYPE_A","reason":"fixture"}')
        ),
        stderr="",
        cli_version="codex-cli 0.155.1",
        exit_code=0,
        provenance={"agent_process_started": True, "configured_model": "fake-model"},
    )
    assert result.status == "success"
    evidence = result.provenance["runtime_interpretation"]["tool_activity"]
    assert evidence["classifications"] == ["TOOL_EXECUTED"]
    assert not evidence["confirmed_user_input_rejection_events"]


def test_raw_json_coverage_failure_not_repaired():
    from test_agent_harness_batch import _schema

    from dispatcher_for_codex_agents.agent_harness.runtime_contract import (
        validate_coverage,
    )

    schema = _schema()
    schema["properties"]["results"]["items"]["properties"]["record_id"]["enum"] = [
        "R1",
        "R2",
    ]
    raw = json.dumps(
        {"results": [{"record_id": "R1", "decision": "TYPE_A", "confidence": 1}] * 2}
    ).encode()
    result = normalize_output(
        raw, schema, RuntimeContract(structured_output="json_or_single_fence")
    )
    assert result.schema_status == "PASS" and result.audit["action"] == "raw"
    with pytest.raises(ValueError):
        validate_coverage(result.value, schema)


def test_model_profile_cannot_grant_interpretation():
    from test_agent_harness import _profile

    with pytest.raises(ValueError):
        _profile(
            capabilities={
                "runtime_contract": {"structured_output": "json_or_single_fence"}
            }
        )


@pytest.mark.parametrize(
    "bad",
    [
        {"type": []},
        {"type": {}},
        {"type": None},
        {"type": "item.completed", "item": {"type": [], "id": "x"}},
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "id": "x",
                "command": "true",
                "status": [],
                "exit_code": 0,
            },
        },
        {
            "type": "item.completed",
            "item": {"type": "mcp_tool_call", "id": "x", "status": {}},
        },
        {"type": "item.completed", "item": {"type": "request_user_input", "id": "x"}},
    ],
)
def test_wrong_event_types_contained(tmp_path, bad):
    from test_agent_harness import _profile, _task

    from dispatcher_for_codex_agents.agent_harness.adapter import evaluate_capture

    task = _task(
        tmp_path / "not-read.tsv",
        runtime_contract=RuntimeContract(
            rejected_user_input="warn_if_runtime_rejected"
        ),
    )
    result, artifacts = evaluate_capture(
        task=task,
        profile=_profile(),
        stdout="\n".join(
            json.dumps(e)
            for e in stream(bad, text='{"decision":"TYPE_A","reason":"done"}')
        ),
        stderr="",
        cli_version="codex-cli 0.155.1",
        exit_code=0,
        provenance={"agent_process_started": True, "configured_model": "fake-model"},
    )
    assert result.status == "failure"
    assert (
        "UNKNOWN_OR_INCOMPLETE"
        in json.loads(artifacts["interpretation.json"])["tool_activity"][
            "classifications"
        ]
    )


@pytest.mark.parametrize(
    "suffix",
    [
        b"\xff",
        b'\n{"type":"item.completed","item":{"type":"agent_message","text":"\\ud800"}}',
    ],
)
def test_invalid_capture_encoding_contained(tmp_path, suffix):
    from test_agent_harness import _profile, _task

    from dispatcher_for_codex_agents.agent_harness.adapter import evaluate_capture

    capture = "\n".join(json.dumps(e) for e in stream()).encode() + suffix
    result, _ = evaluate_capture(
        task=_task(tmp_path / "not-read.tsv"),
        profile=_profile(),
        stdout=capture,
        stderr=b"",
        cli_version="codex-cli 0.155.1",
        exit_code=0,
        provenance={"agent_process_started": True, "configured_model": "fake-model"},
    )
    assert result.failure_code == "EVENT_STREAM_INVALID"
    assert (
        result.provenance["runtime_interpretation"]["captured_events_sha256"]
        == hashlib.sha256(capture).hexdigest()
    )


def test_final_selection_not_reasoning_or_tool_output(tmp_path):
    from test_agent_harness import _profile, _task

    from dispatcher_for_codex_agents.agent_harness.adapter import evaluate_capture

    evidence = stream(
        {
            "type": "item.completed",
            "item": {
                "type": "reasoning",
                "id": "r",
                "text": '{"decision":"TYPE_A","reason":"valid but unselected"}',
            },
        },
        text="not JSON",
    )
    result, _ = evaluate_capture(
        task=_task(tmp_path / "not-read.tsv"),
        profile=_profile(),
        stdout="\n".join(json.dumps(e) for e in evidence),
        stderr="",
        cli_version="codex-cli 0.155.1",
        exit_code=0,
        provenance={"agent_process_started": True, "configured_model": "fake-model"},
    )
    assert (
        result.failure_code == "OUTPUT_SCHEMA_INVALID"
        and result.final_output == "not JSON"
    )


@pytest.mark.parametrize(
    "version", [SYNTHETIC_PROTOCOL, "codex-cli 0.153.4", "codex-cli 0.155.1"]
)
@pytest.mark.parametrize("optin", [False, True])
def test_all_thread_turn_item_terminal_permutations_fail_closed(
    tmp_path, version, optin
):
    from test_agent_harness import _profile, _task

    from dispatcher_for_codex_agents.agent_harness.adapter import evaluate_capture

    contract = (
        RuntimeContract(rejected_user_input="warn_if_runtime_rejected")
        if optin
        else RuntimeContract()
    )
    task = _task(tmp_path / "not-read.tsv", runtime_contract=contract)
    ordered = stream(rejection(), text='{"decision":"TYPE_A","reason":"done"}')
    for permutation in permutations(range(len(ordered))):
        events = tuple(ordered[i] for i in permutation)
        legal_order = permutation in ((0, 1, 2, 3, 4), (0, 1, 3, 2, 4))
        evidence = classify_activity(events, version=version, complete=True)
        assert (evidence["lifecycle"]["status"] == "PASS") is legal_order
        if legal_order:
            continue
        assert "UNKNOWN_OR_INCOMPLETE" in evidence["classifications"]
        assert "NO_TOOL_ACTIVITY" not in evidence["classifications"]
        assert not evidence["confirmed_user_input_rejection_events"]
        assert all(
            c["classifications"] == ["UNKNOWN_OR_INCOMPLETE"] for c in evidence["calls"]
        )
        result, _ = evaluate_capture(
            task=task,
            profile=_profile(),
            stdout="\n".join(json.dumps(e) for e in events),
            stderr="",
            cli_version=version,
            exit_code=0,
            provenance={
                "agent_process_started": True,
                "configured_model": "fake-model",
            },
        )
        assert result.status == "failure"
        assert result.failure_code == "EVENT_STREAM_INVALID"
        assert not any(
            w.startswith("REQUEST_USER_INPUT_RUNTIME_REJECTED:")
            for w in result.warnings
        )


@pytest.mark.parametrize("optin", [False, True])
@pytest.mark.parametrize(
    "stderr", ["", "request_user_input unavailable in Default mode"]
)
def test_final_before_turn_cannot_be_schema_only_success(tmp_path, optin, stderr):
    from test_agent_harness import _profile, _task

    from dispatcher_for_codex_agents.agent_harness.adapter import evaluate_capture

    contract = (
        RuntimeContract(structured_output="json_or_single_fence")
        if optin
        else RuntimeContract()
    )
    events = stream(text='{"decision":"TYPE_A","reason":"done"}')
    result, _ = evaluate_capture(
        task=_task(tmp_path / "not-read.tsv", runtime_contract=contract),
        profile=_profile(),
        stdout="\n".join(json.dumps(events[i]) for i in (0, 2, 1, 3)),
        stderr=stderr,
        cli_version="codex-cli 0.155.1",
        exit_code=0,
        provenance={"agent_process_started": True, "configured_model": "fake-model"},
    )
    assert result.schema_validation_status == "PASS"
    assert result.failure_code == "EVENT_STREAM_INVALID"
    assert result.provenance["runtime_interpretation"]["tool_activity"][
        "classifications"
    ] == ["UNKNOWN_OR_INCOMPLETE"]


@pytest.mark.parametrize(
    "sequence",
    [
        ("item.updated", "item.completed"),
        ("item.started", "item.started", "item.completed"),
        ("item.completed", "item.updated"),
        ("item.completed", "item.started", "item.completed"),
        ("item.completed", "item.completed"),
        ("item.started",),
    ],
)
def test_bad_item_lifecycle_revokes_other_rejection_exemptions(sequence):
    items = tuple(
        {"type": state, "item": {"type": "reasoning", "id": "r", "text": "fixture"}}
        for state in sequence
    )
    evidence = activity(rejection(), *items)
    assert evidence["lifecycle"]["status"] == "UNKNOWN_OR_INCOMPLETE"
    assert evidence["confirmed_user_input_rejection_events"] == []


def test_valid_item_lifecycle_and_intermediate_messages_preserved():
    items = tuple(
        {
            "type": state,
            "item": {
                "type": "todo_list",
                "id": "todo",
                "items": [{"text": "done", "completed": True}],
            },
        }
        for state in ("item.started", "item.updated", "item.completed")
    )
    earlier = {
        "type": "item.completed",
        "item": {"type": "agent_message", "id": "earlier", "text": "progress"},
    }
    evidence = activity(earlier, *items, rejection())
    assert evidence["lifecycle"]["status"] == "PASS"
    assert evidence["classifications"] == ["TOOL_ATTEMPT_REJECTED"]
    assert evidence["confirmed_user_input_rejection_events"]
