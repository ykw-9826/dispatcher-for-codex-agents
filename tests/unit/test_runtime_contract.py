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


def diagnostic(identity="diagnostic", message="Synthetic diagnostic — no subtype"):
    return {
        "type": "item.completed",
        "item": {"id": identity, "type": "error", "message": message},
    }


def preturn(*items, text='{"decision":"TYPE_A","reason":"done"}'):
    ordered = stream(text=text)
    return (ordered[0], *items, *ordered[1:])


def evaluate_preturn(tmp_path, events, *, stderr="", exit_code=0, optin=True):
    from test_agent_harness import _profile, _task

    from dispatcher_for_codex_agents.agent_harness.adapter import evaluate_capture

    policy = (
        RuntimeContract(
            structured_output="json_or_single_fence",
            rejected_user_input="warn_if_runtime_rejected",
        )
        if optin
        else RuntimeContract()
    )
    return evaluate_capture(
        task=_task(tmp_path / "not-read.tsv", runtime_contract=policy),
        profile=_profile(),
        stdout="\n".join(json.dumps(e) for e in events).encode(),
        stderr=stderr,
        cli_version="codex-cli 0.153.4",
        exit_code=exit_code,
        provenance={"agent_process_started": True, "configured_model": "fake-model"},
    )


@pytest.mark.parametrize("count", [0, 1, 3])
@pytest.mark.parametrize("version", ["codex-cli 0.153.4", "codex-cli 0.155.1"])
def test_preturn_completed_error_is_audited_without_message_heuristics(count, version):
    from dispatcher_for_codex_agents.agent_harness.runtime_contract import (
        CLASSIFIER_VERSION,
        PRETURN_DIAGNOSTIC_RULE,
    )

    events = preturn(*(diagnostic(str(i), str(i)) for i in range(count)))
    raw = json.dumps(events).encode()
    result = classify_activity(events, version=version, complete=True)
    assert json.dumps(events).encode() == raw
    assert result["classifier_version"] == CLASSIFIER_VERSION == "dca-activity/3"
    assert result["lifecycle"] == {"status": "PASS", "issues": []}
    assert result["classifications"] == ["NO_TOOL_ACTIVITY"]
    assert result["calls"] == result["confirmed_user_input_rejection_events"] == []
    assert result["applied_rule_ids"] == ([PRETURN_DIAGNOSTIC_RULE] if count else [])
    assert result["diagnostics"] == [
        {
            "classification": "PRETURN_NON_FATAL_DIAGNOSTIC",
            "rule_id": PRETURN_DIAGNOSTIC_RULE,
            "artifact": "events.jsonl",
            "event_index": i + 1,
            "pointer": "/item",
            "item_id": str(i),
        }
        for i in range(count)
    ]


@pytest.mark.parametrize("optin", [False, True])
@pytest.mark.parametrize(
    "kind",
    [
        "agent_message",
        "reasoning",
        "command_execution",
        "file_change",
        "mcp_tool_call",
        "collab_tool_call",
        "web_search",
        "todo_list",
        "unknown",
    ],
)
def test_other_preturn_completed_items_remain_hard_failure(tmp_path, kind, optin):
    item = diagnostic()
    item["item"]["type"] = kind
    result, _ = evaluate_preturn(tmp_path, preturn(item), optin=optin)
    evidence = result.provenance["runtime_interpretation"]["tool_activity"]
    assert result.failure_code == "EVENT_STREAM_INVALID"
    assert evidence["lifecycle"]["issues"] == ["events[1]:item_outside_turn"]
    assert not evidence["diagnostics"]
    assert not evidence["confirmed_user_input_rejection_events"]


@pytest.mark.parametrize("event_type", ["item.started", "item.updated"])
@pytest.mark.parametrize("item_type", ["error", "command_execution"])
def test_no_started_or_updated_preturn_exception(tmp_path, event_type, item_type):
    item = diagnostic()
    item["type"], item["item"]["type"] = event_type, item_type
    result, _ = evaluate_preturn(tmp_path, preturn(item))
    evidence = result.provenance["runtime_interpretation"]["tool_activity"]
    assert result.failure_code == "EVENT_STREAM_INVALID"
    assert "events[1]:item_outside_turn" in evidence["lifecycle"]["issues"]
    assert not evidence["diagnostics"]


@pytest.mark.parametrize("message", [None, 1, [], {}])
def test_malformed_diagnostic_message_fails_closed(tmp_path, message):
    result, _ = evaluate_preturn(tmp_path, preturn(diagnostic(message=message)))
    assert result.failure_code == "EVENT_STREAM_INVALID"


def test_diagnostic_identity_and_position_remain_strict(tmp_path):
    normal = stream(text='{"decision":"TYPE_A","reason":"done"}')
    invalid = [
        (diagnostic(), *normal),
        (*normal, diagnostic()),
        preturn(diagnostic(), diagnostic()),
        preturn(diagnostic(identity="final")),
        preturn(diagnostic(identity="")),
        preturn({"type": "unknown.event"}),
    ]
    for events in invalid:
        result, _ = evaluate_preturn(tmp_path, events)
        assert result.failure_code == "EVENT_STREAM_INVALID"
    unknown = classify_activity(
        preturn(diagnostic()), version="codex-cli 9.9.9", complete=True
    )
    assert unknown["classifications"] == ["UNKNOWN_OR_INCOMPLETE"]
    assert not unknown["applied_rule_ids"]


@pytest.mark.parametrize("optin", [False, True])
def test_diagnostic_does_not_override_fatal_or_other_acceptance_gates(tmp_path, optin):
    events = preturn(diagnostic())
    # A top-level fatal is not an allowed diagnostic, even with a contradictory
    # successful terminal, or when it is the final captured event.
    for fatal in (
        (*events[:2], {"type": "error", "message": "synthetic fatal"}),
        (*events[:2], {"type": "error", "message": "synthetic fatal"}, *events[2:]),
        (*events[:-1], {"type": "turn.failed", "error": {"message": "failed"}}),
        events[:-1],
    ):
        result, _ = evaluate_preturn(tmp_path, fatal, optin=optin)
        assert result.status == "failure"
        evidence = result.provenance["runtime_interpretation"]["tool_activity"]
        assert "UNKNOWN_OR_INCOMPLETE" in evidence["classifications"]
        assert not evidence["confirmed_user_input_rejection_events"]
    assert (
        evaluate_preturn(tmp_path, events, exit_code=1, optin=optin)[0].status
        == "failure"
    )
    assert (
        evaluate_preturn(tmp_path, preturn(diagnostic(), text="{}"), optin=optin)[
            0
        ].failure_code
        == "OUTPUT_SCHEMA_INVALID"
    )


def test_preturn_normalization_and_evidence_bytes_are_preserved(tmp_path):
    raw_final = b'```json\n{"decision":"TYPE_A","reason":"done"}\n```'
    events = preturn(diagnostic(), text=raw_final.decode())
    before = json.dumps(events)
    result, artifacts = evaluate_preturn(tmp_path, events)
    assert result.status == "success"
    assert json.dumps(events) == before
    assert artifacts["raw_final_output.bin"] == raw_final
    audit = json.loads(artifacts["interpretation.json"])
    assert (
        audit["captured_events_sha256"]
        == hashlib.sha256("\n".join(json.dumps(e) for e in events).encode()).hexdigest()
    )
    assert audit["normalization"]["action"] == "single_json_fence"
    assert audit["tool_activity"]["diagnostics"][0]["event_index"] == 1


def test_in_turn_error_behavior_unchanged_and_stderr_not_confirmed_rejection(tmp_path):
    before = activity(diagnostic(), version="codex-cli 0.153.4")
    assert before["lifecycle"]["status"] == "PASS"
    assert before["classifications"] == ["UNKNOWN_OR_INCOMPLETE"]
    assert not before["diagnostics"]
    events = preturn(diagnostic())
    result, _ = evaluate_preturn(
        tmp_path, events, stderr="request_user_input is unavailable in Default mode"
    )
    evidence = result.provenance["runtime_interpretation"]["tool_activity"]
    assert result.failure_code == "POLICY_VIOLATION"
    assert result.schema_validation_status == "PASS"
    assert evidence["lifecycle"]["status"] == "PASS"
    assert evidence["classifications"] == ["UNKNOWN_OR_INCOMPLETE"]
    assert evidence["confirmed_user_input_rejection_events"] == []
    assert evidence["calls"] == []
    assert "uncorrelated_stderr_tool_signal" in evidence["issues"]
    ordinary, _ = evaluate_preturn(tmp_path, events, stderr="synthetic cache notice")
    assert ordinary.status == "success"


@pytest.mark.parametrize(
    "kind", ["command_execution", "mcp_tool_call", "collab_tool_call"]
)
def test_diagnostic_does_not_reclassify_executed_tool_failures(kind):
    item = {
        "type": "item.completed",
        "item": {
            "id": "call",
            "type": kind,
            "status": "failed",
            "exit_code": 1,
        },
    }
    events = stream(item)
    normal = classify_activity(events, version="codex-cli 0.153.4", complete=True)
    with_diagnostic = classify_activity(
        (events[0], diagnostic(), *events[1:]),
        version="codex-cli 0.153.4",
        complete=True,
    )
    assert (
        normal["classifications"]
        == with_diagnostic["classifications"]
        == ["TOOL_EXECUTED"]
    )
    assert normal["calls"][0]["outcome"] == with_diagnostic["calls"][0]["outcome"]


@pytest.mark.parametrize("thread_id", [None, "", 0, False, [], {}])
def test_preturn_window_requires_valid_thread_identity(tmp_path, thread_id):
    events = preturn(diagnostic())
    events[0]["thread_id"] = thread_id
    for missing in (False, True):
        if missing:
            events[0].pop("thread_id", None)
        result, _ = evaluate_preturn(tmp_path, events)
        audit = result.provenance["runtime_interpretation"]["tool_activity"]
        assert result.failure_code == "EVENT_STREAM_INVALID"
        assert "events[0]:invalid_thread_identity" in audit["lifecycle"]["issues"]
        assert audit["diagnostics"] == audit["applied_rule_ids"] == []


@pytest.mark.parametrize("version", ["codex-cli 0.153.4", "codex-cli 0.155.1"])
def test_preturn_window_never_reopens_after_fatal_or_turn_terminal(tmp_path, version):
    ordered = stream(text='{"decision":"TYPE_A","reason":"done"}')
    starts = (
        (*ordered[:1], {"type": "error", "message": "warning-looking text"}),
        (*ordered[:2], {"type": "error", "message": "synthetic fatal"}),
        (*ordered[:2], {"type": "turn.failed", "error": {"message": "failed"}}),
        ordered,
    )
    for prefix in starts:
        for reopen in ((), ({"type": "turn.started"},)):
            events = (*prefix, *reopen, diagnostic(), *ordered[-2:])
            audit = classify_activity(events, version=version, complete=True)
            assert audit["lifecycle"]["status"] == "UNKNOWN_OR_INCOMPLETE"
            assert audit["diagnostics"] == audit["applied_rule_ids"] == []
            assert audit["confirmed_user_input_rejection_events"] == []
            assert evaluate_preturn(tmp_path, events)[0].status == "failure"


@pytest.mark.parametrize(
    "malformed",
    [
        None,
        [],
        "error",
        {},
        {"type": "error", "message": "synthetic"},
        {"type": "error", "id": "notice"},
        {"type": "error", "id": 1, "message": "synthetic"},
    ],
)
def test_preturn_malformed_item_cannot_use_diagnostic_rule(tmp_path, malformed):
    result, _ = evaluate_preturn(
        tmp_path, preturn({"type": "item.completed", "item": malformed})
    )
    audit = result.provenance["runtime_interpretation"]["tool_activity"]
    assert result.failure_code == "EVENT_STREAM_INVALID"
    assert audit["diagnostics"] == audit["applied_rule_ids"] == []


@pytest.mark.parametrize("optin", [False, True])
def test_no_tools_required_preserves_acceptance_policy(tmp_path, optin):
    for events in (preturn(), preturn(diagnostic())):
        result, _ = evaluate_preturn(tmp_path, events, optin=optin)
        audit = result.provenance["runtime_interpretation"]["tool_activity"]
        assert result.status == "success"
        assert result.schema_validation_status == "PASS"
        assert audit["classifications"] == ["NO_TOOL_ACTIVITY"]
        assert audit["calls"] == audit["confirmed_user_input_rejection_events"] == []
