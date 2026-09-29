"""Synthetic-only fixtures: no real executable, authentication or provider."""

import copy
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from test_agent_harness import _task

from dispatcher_for_codex_agents.agent_harness.antigravity import (
    LIVE_GATE,
    compile_headless,
    evaluate_headless_capture,
)
from dispatcher_for_codex_agents.agent_harness.contracts import CapabilityPolicy
from dispatcher_for_codex_agents.agent_harness.runtime import (
    AdapterRegistry,
    default_registry,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "fake_agy_cli.py"


def events():
    return [
        {
            "event": "init",
            "conversation_id": "synthetic",
            "init": {
                "cwd": "/synthetic",
                "tools": [],
                "permission_mode": "request-review",
            },
        },
        {
            "event": "step_update",
            "step_update": {
                "conversation_id": "synthetic",
                "step_index": 0,
                "step_type": "agent_response",
                "state": "DONE",
                "usage": {"input_tokens": 900, "output_tokens": 900},
            },
        },
        {
            "event": "result",
            "result": {
                "conversation_id": "synthetic",
                "status": "SUCCESS",
                "response": '{"decision":"TYPE_A","reason":"done"}',
                "duration_seconds": 0.1,
                "num_turns": 1,
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "thinking_tokens": 2,
                    "total_tokens": 15,
                },
            },
        },
    ]


def capture(tmp_path, sequence=None, **kwargs):
    raw = kwargs.pop("stdout", None)
    if raw is None:
        raw = "\n".join(
            json.dumps(e) for e in (events() if sequence is None else sequence)
        ).encode()
    return evaluate_headless_capture(
        task=_task(tmp_path / "not-read"),
        stdout=raw,
        exit_code=kwargs.pop("exit_code", 0),
        elapsed=0.1,
        **kwargs,
    )


def test_success_final_usage_only_and_unknown_accounting(tmp_path):
    result = capture(tmp_path)
    assert result.status == "success"
    assert result.final_output["decision"] == "TYPE_A"
    assert result.usage == {
        "input_tokens": 10,
        "output_tokens": 5,
        "thinking_tokens": 2,
        "total_tokens": 15,
    }
    assert result.provenance["provider_requests"] == "UNKNOWN"
    assert result.provenance["agy_builtin_retries_observed"] == "UNKNOWN"
    assert result.provenance["served_model"] == "NOT_REPORTED"
    assert result.provenance["capability_policy"]["effective"] is None
    assert result.provenance["live_execution_gate"] == LIVE_GATE


@pytest.mark.parametrize(
    "grant",
    [
        {"tools": ("shell",)},
        {"tools": ("web_search",)},
        {"tools": ("mcp:synthetic:read",)},
        {"read_paths": ("/synthetic/read",)},
        {"write_paths": ("/synthetic/write",)},
    ],
)
def test_authority_never_silently_reduced(tmp_path, grant):
    task = _task(tmp_path / "not-read", capability_policy=CapabilityPolicy(**grant))
    with pytest.raises(ValueError, match="CAPABILITY_MAPPING_UNSUPPORTED"):
        compile_headless(
            task=task, executable="/synthetic/agy", model="synthetic", prompt="hi"
        )
    result = evaluate_headless_capture(
        task=task,
        stdout=b"",
        exit_code=0,
        elapsed=0,
    )
    assert result.status == "failure"
    assert result.provenance["capability_policy"]["effective"] is None


def test_no_production_adapter_even_if_fake_protocol_passes(tmp_path):
    assert capture(tmp_path).status == "success"
    with pytest.raises(ValueError, match=LIVE_GATE):
        default_registry().require("antigravity_cli")
    with pytest.raises(ValueError, match="reserved"):
        AdapterRegistry().register("antigravity_cli", lambda _: None)


@pytest.mark.parametrize("version", ["1.2.11", "1.2.13", "UNKNOWN"])
def test_version_fail_closed(tmp_path, version):
    assert capture(tmp_path, version=version).status == "failure"
    with pytest.raises(ValueError, match="VERSION_UNSUPPORTED"):
        compile_headless(
            task=_task(tmp_path / "not-read"),
            executable="/synthetic/agy",
            model="synthetic",
            prompt="hi",
            version=version,
        )


@pytest.mark.parametrize(
    "status", ["ERROR", "CANCELED", "INTERRUPTED", "INVALID", "WAITING", "RUNNING"]
)
def test_non_success_statuses(tmp_path, status):
    seq = events()
    seq[-1]["result"]["status"] = status
    result = capture(tmp_path, seq)
    assert result.status == "failure"
    assert result.provenance["terminal_status"] == status


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_result",
        "duplicate_result",
        "conflicting_result",
        "identity",
        "before_init",
        "unknown",
        "not_object",
        "open_step",
        "done_then_active",
        "missing_init",
        "duplicate_init",
        "tool",
        "tool_metadata",
        "tool_inventory",
        "bad_schema",
        "structured_conflict",
        "schema_conflict",
        "multi_turn",
        "bad_usage",
        "unknown_usage",
        "bad_index",
        "bad_state",
        "bad_type",
        "bad_duration",
        "unknown_result_field",
        "trailing_event",
    ],
)
def test_adversarial_failures(tmp_path, mutation):
    seq = events()
    step, result = seq[1]["step_update"], seq[-1]["result"]
    if mutation == "missing_result":
        seq.pop()
    elif mutation == "duplicate_result":
        seq.append(copy.deepcopy(seq[-1]))
    elif mutation == "conflicting_result":
        second = copy.deepcopy(seq[-1])
        second["result"]["status"] = "ERROR"
        seq.append(second)
    elif mutation == "identity":
        step["conversation_id"] = "other"
    elif mutation == "before_init":
        seq[0], seq[1] = seq[1], seq[0]
    elif mutation == "unknown":
        seq.insert(1, {"event": "unknown"})
    elif mutation == "not_object":
        seq.insert(1, [])
    elif mutation == "open_step":
        step["state"] = "ACTIVE"
    elif mutation == "done_then_active":
        second = copy.deepcopy(seq[1])
        second["step_update"]["state"] = "ACTIVE"
        seq.insert(2, second)
    elif mutation == "missing_init":
        seq.pop(0)
    elif mutation == "duplicate_init":
        seq.insert(1, copy.deepcopy(seq[0]))
    elif mutation == "tool":
        step["step_type"] = "tool"
    elif mutation == "tool_metadata":
        step["tool_info"] = {"status": "rejected"}
    elif mutation == "tool_inventory":
        seq[0]["init"]["tools"] = ["shell"]
    elif mutation == "bad_schema":
        result["response"] = '{"wrong":true}'
    elif mutation == "structured_conflict":
        result["structured_output"] = {"different": True}
    elif mutation == "schema_conflict":
        result["json_schema"] = {"type": "integer"}
    elif mutation == "multi_turn":
        result["num_turns"] = 2
    elif mutation == "bad_usage":
        result["usage"]["input_tokens"] = True
    elif mutation == "unknown_usage":
        result["usage"]["unknown_counter"] = 42
    elif mutation == "bad_index":
        step["step_index"] = True
    elif mutation == "bad_state":
        step["state"] = []
    elif mutation == "bad_type":
        step["step_type"] = {}
    elif mutation == "bad_duration":
        result["duration_seconds"] = -1
    elif mutation == "unknown_result_field":
        result["error"] = "SYNTHETIC_DO_NOT_LOG"
    elif mutation == "trailing_event":
        seq.append(copy.deepcopy(seq[1]))
    outcome = capture(tmp_path, seq)
    assert outcome.status == "failure"
    assert outcome.final_output is None
    assert "SYNTHETIC_DO_NOT_LOG" not in outcome.model_dump_json()


@pytest.mark.parametrize(
    "raw",
    [
        b"not-json",
        b"\xff",
        b'{"event":"init","event":"result"}',
        b'{"event":NaN}',
        b'{"event":"\\ud800"}',
    ],
)
def test_malformed_redacted(tmp_path, raw):
    result = capture(tmp_path, stdout=raw)
    assert result.failure_code == "EVENT_STREAM_INVALID"
    assert result.warnings


def test_missing_usage_never_zero_and_no_step_substitution(tmp_path):
    seq = events()
    del seq[-1]["result"]["usage"]
    result = capture(tmp_path, seq)
    assert result.status == "success"
    assert result.usage == {}
    assert result.provenance["usage_status"] == "UNKNOWN"


def test_active_done_and_matching_structured_output(tmp_path):
    seq = events()
    active = copy.deepcopy(seq[1])
    active["step_update"]["state"] = "ACTIVE"
    seq.insert(1, active)
    seq[-1]["result"]["structured_output"] = {"decision": "TYPE_A", "reason": "done"}
    assert capture(tmp_path, seq).status == "success"


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("success", None),
        ("partial", "EVENT_STREAM_INVALID"),
        ("nonzero", "CLI_NONZERO_EXIT"),
        ("hang", "TIMEOUT"),
        ("cancel", "CANCELLED"),
    ],
)
def test_fake_process_lifecycle(tmp_path, mode, expected):
    task = _task(tmp_path / "not-read", timeout=0.3 if mode == "hang" else 2)
    plan = compile_headless(
        task=task,
        executable="/synthetic/agy",
        model="synthetic",
        prompt="short",
    )
    assert plan.live_gate == LIVE_GATE
    assert "--resume" not in plan.argv and "--sandbox" not in plan.argv
    assert plan.argv[plan.argv.index("--print-timeout") + 1] == (
        "0.3s" if mode == "hang" else "2s"
    )
    assert plan.process_timeout_seconds == task.timeout
    # Only this test harness substitutes Python + fixture; library has no launcher.
    process = subprocess.Popen(
        [sys.executable, "-I", "-B", str(FIXTURE), *plan.argv[1:]],
        cwd=tmp_path,
        env={"FAKE_AGY_MODE": "hang" if mode == "cancel" else mode},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    timed_out = cancelled = False
    prefix = b""
    try:
        if mode == "cancel":
            prefix = process.stdout.readline()
            os.killpg(process.pid, signal.SIGTERM)
            cancelled = True
        stdout, _ = process.communicate(timeout=plan.process_timeout_seconds)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        stdout, _ = process.communicate(timeout=5)
        timed_out = True
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
    result = evaluate_headless_capture(
        task=task,
        stdout=prefix + stdout,
        exit_code=process.returncode,
        elapsed=0.3,
        timed_out=timed_out,
        cancelled=cancelled,
    )
    assert result.failure_code == expected
    assert result.provenance["dca_retries"] == 0
    assert result.provenance["provider_requests"] == "UNKNOWN"


def _forbid_processes(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Gate crossed subprocess boundary")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)


@pytest.mark.parametrize(
    "name",
    [
        "agy",
        "antigravity",
        "antigravity_cli",
        "Antigravity-CLI",
        "unknown",
        "antigravity_cli_extra",
    ],
)
def test_registry_aliases_and_cli_task_deserialization_gate(
    tmp_path, monkeypatch, capsys, name
):
    from test_agent_harness_cli import _args, _write_task

    from dispatcher_for_codex_agents.agent_harness.cli import main
    from dispatcher_for_codex_agents.agent_harness.runtime import AdapterSettings

    task_path = _write_task(tmp_path)
    _forbid_processes(monkeypatch)
    with pytest.raises(ValueError, match="NOT_IMPLEMENTED"):
        default_registry().create(name, AdapterSettings(executable="agy"))
    args = _args(
        task_path, tmp_path / "missing-home", tmp_path / "run", "synthetic", "one"
    )
    assert main([*args, "--adapter", name]) != 0
    assert "NOT_IMPLEMENTED" in capsys.readouterr().err
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize("selection", ["profile", "provider", "adapter"])
def test_direct_codex_invocation_and_preflight_reject_agy_before_probe(
    tmp_path, monkeypatch, selection
):
    from test_agent_harness import _adapter, _profile

    adapter, task = _adapter(tmp_path, "success")
    profile = _profile()
    if selection == "profile":
        profile = _profile(profile_id="agy")
    elif selection == "adapter":
        profile = _profile(adapter_id="antigravity_cli")
    else:
        (tmp_path / "codex-home/config.toml").write_text(
            '[model_providers.antigravity]\nname="synthetic"\n'
        )
        (tmp_path / "codex-home/fake-profile.config.toml").write_text(
            'model="synthetic"\nmodel_provider="antigravity"\n'
        )
    _forbid_processes(monkeypatch)
    with pytest.raises(ValueError, match=LIVE_GATE):
        adapter.preflight(task=task, profile=profile)
    result = adapter.invoke(
        task=task, profile=profile, attempt_id="one", workers_root=tmp_path / "run"
    )
    assert result.failure_code == "PROFILE_CONFIGURATION_INVALID"
    assert any(LIVE_GATE in w for w in result.warnings)
    assert result.provenance["agent_subprocess_count"] == adapter.calls_started == 0


@pytest.mark.parametrize(
    "kind", ["basename", "absolute", "symlink", "wrapper", "mutated"]
)
def test_executable_selection_gate_precedes_any_subprocess(tmp_path, monkeypatch, kind):
    from dispatcher_for_codex_agents.agent_harness.adapter import CodexCliAdapter

    agy = tmp_path / "agy"
    agy.write_text("#!/bin/sh\nexit 99\n")
    agy.chmod(0o700)
    alias = tmp_path / "codex"
    alias.symlink_to(agy)
    _forbid_processes(monkeypatch)
    options = {
        "basename": "agy",
        "absolute": str(agy),
        "symlink": str(alias),
        "wrapper": (sys.executable, str(agy)),
    }
    if kind == "mutated":
        adapter = CodexCliAdapter(executable=sys.executable, codex_home=tmp_path)
        adapter._executable = (str(agy),)
        with pytest.raises(ValueError, match=LIVE_GATE):
            adapter._read_cli_version({})
    else:
        with pytest.raises(ValueError, match=LIVE_GATE):
            CodexCliAdapter(executable=options[kind], codex_home=tmp_path)


def test_batch_config_cannot_select_agy(tmp_path, monkeypatch):
    from test_agent_harness_batch import _inputs

    from dispatcher_for_codex_agents.agent_harness.batch import plan_batch

    inputs = _inputs(tmp_path / "inputs", records=1, profiles=1)
    roles = json.loads(inputs["profiles"].read_text())
    roles["profiles"][0]["adapter_id"] = "antigravity_cli"
    inputs["profiles"].write_text(json.dumps(roles))
    _forbid_processes(monkeypatch)
    with pytest.raises(ValueError, match=LIVE_GATE):
        plan_batch(
            source_tsv=inputs["source"],
            batch_id="BATCH-A",
            record_id_column="record_id",
            selected_columns=("record_id", "title", "description"),
            profile_role_config=inputs["profiles"],
            shard_size=1,
            prompt_template=inputs["prompt"],
            expected_output_schema=inputs["schema"],
            output_root=tmp_path / "plan",
        )


def test_task_cannot_override_runtime_or_enable_fallback(tmp_path, monkeypatch):
    from pydantic import ValidationError

    from dispatcher_for_codex_agents.agent_harness.contracts import (
        AgentTask,
        ModelProfile,
    )

    _forbid_processes(monkeypatch)
    task = _task(tmp_path / "not-read").model_dump(mode="json")
    for extra in ("adapter_id", "fallback_provider", "resume"):
        with pytest.raises(ValidationError):
            AgentTask.model_validate({**task, extra: "antigravity_cli"})
    with pytest.raises(ValidationError):
        ModelProfile(
            profile_id="safe",
            adapter_id="codex_cli",
            capabilities={},
            fallback_provider="antigravity_cli",
        )


@pytest.mark.parametrize(
    "prompt",
    [
        "--disable-slash-commands",
        "--model=hostile",
        "a b",
        "'quoted' \"double\"",
        "line one\n--resume latest",
        "$(do-not-run); text",
    ],
)
def test_hostile_prompt_is_data_across_fake_process(tmp_path, prompt):
    task = _task(tmp_path / "not-read")
    model = 'synthetic model "quoted"\nnot-a-flag'
    plan = compile_headless(
        task=task, executable="/synthetic/agy", model=model, prompt=prompt
    )
    assert plan.argv[-1] == "--print=" + prompt
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(FIXTURE), *plan.argv[1:]],
        cwd=tmp_path,
        env={"FAKE_AGY_MODE": "echo", "FAKE_AGY_EXPECT_MODEL": model},
        capture_output=True,
        timeout=plan.process_timeout_seconds,
        check=True,
    )
    outcome = evaluate_headless_capture(
        task=task, stdout=result.stdout, exit_code=result.returncode, elapsed=0.1
    )
    assert outcome.status == "success"
    assert outcome.final_output["reason"] == prompt


@pytest.mark.parametrize("timeout", [0.000000001, 0.0001])
def test_duration_must_not_round_to_unlimited_zero(tmp_path, timeout):
    with pytest.raises(ValueError, match="COMMAND_INVALID"):
        compile_headless(
            task=_task(tmp_path / "not-read", timeout=timeout),
            executable="/synthetic/agy",
            model="fake",
            prompt="data",
        )


def test_finite_process_deadline_and_exact_timeout(tmp_path):
    plan = compile_headless(
        task=_task(tmp_path / "not-read", timeout=0.001),
        executable="/synthetic/agy",
        model="fake",
        prompt="data",
    )
    assert plan.process_timeout_seconds == 0.001
    assert plan.argv[plan.argv.index("--print-timeout") + 1] == "0.001s"


@pytest.mark.parametrize(
    "body",
    [
        '{"x":1,"x":2}',
        '{"x":NaN}',
        '{"x":Infinity}',
        '{"x":1e999}',
    ],
)
def test_nested_response_is_strict_json(tmp_path, body):
    task = _task(tmp_path / "not-read", expected_output_schema={"type": "object"})
    seq = events()
    seq[-1]["result"]["response"] = body
    outcome = evaluate_headless_capture(
        task=task,
        stdout="\n".join(map(json.dumps, seq)).encode(),
        exit_code=0,
        elapsed=0.1,
    )
    assert outcome.status == "failure"


def test_structured_conflict_is_type_sensitive(tmp_path):
    task = _task(tmp_path / "not-read", expected_output_schema={"type": "object"})
    seq = events()
    seq[-1]["result"]["response"] = '{"x":1}'
    seq[-1]["result"]["structured_output"] = {"x": True}
    outcome = evaluate_headless_capture(
        task=task,
        stdout="\n".join(map(json.dumps, seq)).encode(),
        exit_code=0,
        elapsed=0.1,
    )
    assert outcome.status == "failure"


@pytest.mark.parametrize(
    "mutation",
    [
        "result_identity",
        "init_conflict",
        "error_then_success",
        "killed",
        "timed_out_complete",
        "nonzero_complete",
    ],
)
def test_terminal_independent_axes(tmp_path, mutation):
    seq = events()
    kwargs = {}
    if mutation == "result_identity":
        seq[-1]["result"]["conversation_id"] = "different"
    elif mutation == "init_conflict":
        extra = copy.deepcopy(seq[0])
        extra["conversation_id"] = "different"
        seq.insert(1, extra)
    elif mutation == "error_then_success":
        extra = copy.deepcopy(seq[-1])
        extra["result"]["status"] = "ERROR"
        seq.insert(-1, extra)
    elif mutation == "killed":
        kwargs["exit_code"] = -9
    elif mutation == "timed_out_complete":
        kwargs["timed_out"] = True
    elif mutation == "nonzero_complete":
        kwargs["exit_code"] = 3
    result = capture(tmp_path, seq, **kwargs)
    assert result.status == "failure"
    assert result.final_output is None


def test_usage_and_offline_invocation_accounting_are_separate(tmp_path):
    result = capture(tmp_path)
    assert result.provenance["dca_logical_invocations"] == 0
    assert result.provenance["dca_retries"] == 0
    assert (
        result.provenance["accounting_scope"] == "OFFLINE_COMPONENT_NOT_SOURCE_HISTORY"
    )
    assert result.provenance["usage_status"] == "REPORTED"
    assert result.provenance["provider_requests"] == "UNKNOWN"


def test_unicode_line_separator_is_not_a_jsonl_record_boundary(tmp_path):
    seq = events()
    seq[-1]["result"]["response"] = json.dumps(
        {"decision": "TYPE_A", "reason": "one\u2028two"}, ensure_ascii=False
    )
    outcome = capture(
        tmp_path,
        stdout="\n".join(json.dumps(e, ensure_ascii=False) for e in seq).encode(),
    )
    assert outcome.status == "success"


def test_revalidation_cannot_promote_or_launch_unadmitted_adapter(
    tmp_path, monkeypatch
):
    from dispatcher_for_codex_agents.agent_harness.contracts import (
        InvocationResult,
        ModelProfile,
        RuntimeContract,
    )
    from dispatcher_for_codex_agents.agent_harness.revalidation import (
        describe_source,
        revalidate,
    )
    from dispatcher_for_codex_agents.agent_harness.shard import ImmutableShardWriter

    task = _task(tmp_path / "not-read")
    profile = ModelProfile(
        profile_id="synthetic", adapter_id="antigravity_cli", capabilities={}
    )
    writer = ImmutableShardWriter(
        workers_root=tmp_path / "run",
        task_id=task.task_id,
        profile_id=profile.profile_id,
        attempt_id="one",
    )
    writer.write(
        task=task,
        profile=profile,
        input_records=(),
        events_jsonl="",
        stderr_log="",
        final_output=None,
        result=InvocationResult(
            status="failure",
            exit_code=1,
            turn_completed=False,
            final_output=None,
            schema_validation_status="NOT_RUN",
            usage={},
            provenance={
                "adapter_id": "antigravity_cli",
                "agent_process_started": False,
            },
            warnings=(),
            failure_code="CLI_NONZERO_EXIT",
            latency_seconds=0,
        ),
    )
    source = next((tmp_path / "run").rglob("invocation_result.json")).parent
    descriptor = tmp_path / "descriptor.json"
    descriptor.write_text(json.dumps(describe_source(source)))
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    _forbid_processes(monkeypatch)
    with pytest.raises(ValueError, match="Revalidation failed closed: ValueError"):
        revalidate(
            source_manifest=descriptor,
            runtime_contract=RuntimeContract(),
            revalidation_id="no-resume",
            output_root=tmp_path / "derived",
        )
    assert before == {p.name: p.read_bytes() for p in source.iterdir()}


@pytest.mark.parametrize("route", ["profile", "executable"])
def test_cli_alias_cannot_bypass_default_codex_adapter(
    tmp_path, monkeypatch, capsys, route
):
    from test_agent_harness_cli import _args, _write_task

    from dispatcher_for_codex_agents.agent_harness.cli import main

    task_path = _write_task(tmp_path)
    args = _args(
        task_path,
        tmp_path / "missing-home",
        tmp_path / "run",
        "agy" if route == "profile" else "synthetic",
        "one",
    )
    if route == "executable":
        args[args.index("--codex-executable") + 1] = "/synthetic/agy"
    _forbid_processes(monkeypatch)
    assert main(args) != 0
    assert LIVE_GATE in capsys.readouterr().err
