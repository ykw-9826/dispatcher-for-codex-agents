"""Explicit admission tests use synthetic executable bytes, never official agy."""

import hashlib
import json
import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest
from test_agent_harness import _task

from dispatcher_for_codex_agents.agent_harness.antigravity import compile_headless
from dispatcher_for_codex_agents.agent_harness.antigravity_live import (
    AGENT_DEFINITION,
    AGENT_RELATIVE,
    DEFAULT_ADMISSION_PROFILE,
    AntigravityCliAdapter,
    OfficialCliApproval,
)
from dispatcher_for_codex_agents.agent_harness.contracts import (
    CapabilityPolicy,
    ModelProfile,
)
from dispatcher_for_codex_agents.agent_harness.runtime import default_registry
from dispatcher_for_codex_agents.agent_harness.shard import read_verified_shard


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def setup(tmp_path):
    # Explicit disposable root outside all project checkouts. Never fall back
    # to user home or use a real project as an agent workspace.
    with tempfile.TemporaryDirectory(prefix="dca-agy-fixture-", dir="/tmp") as raw:
        workspace = Path(raw)
        definition = workspace / AGENT_RELATIVE
        definition.parent.mkdir(parents=True)
        definition.write_text(AGENT_DEFINITION)
        fixture = Path(__file__).parents[1] / "fixtures/fake_agy_cli.py"
        executable = tmp_path / "fake-agy"
        executable.write_text(f"#!{sys.executable}\n" + fixture.read_text())
        executable.chmod(0o700)
        config = {
            "ANTIGRAVITY_EXPERIMENTAL_LIVE": True,
            "provider": "antigravity_cli",
            "profile": "antigravity_cli",
            "session_mode": "fresh",
            "dca_retries": 0,
            "provider_fallback": False,
            "customization_baseline": "PASS",
            "state_ownership": "OFFICIAL_CLI",
            "workspace": str(workspace),
            "model": None,
        }
        config_path = tmp_path / "admission.json"
        config_path.write_text(json.dumps(config))
        config_path.chmod(0o600)
        approval = OfficialCliApproval(
            executable=executable,
            executable_sha256=sha(executable.read_bytes()),
            config_sha256=sha(config_path.read_bytes()),
        )
        source = tmp_path / "input.tsv"
        source.write_text("record_id\tdescription\nsynthetic\tfixture\n")
        task = _task(source, timeout=2, call_limit=1)
        profile = ModelProfile(
            profile_id="antigravity_cli", adapter_id="antigravity_cli", capabilities={}
        )
        yield config, config_path, approval, workspace, task, profile


def adapter(setup):
    return AntigravityCliAdapter(admission_path=setup[1], approval=setup[2])


def invoke(setup, tmp_path):
    return adapter(setup).invoke(
        task=setup[4], profile=setup[5], attempt_id="one", workers_root=tmp_path
    )


def test_no_implicit_admission(setup, monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_EXPERIMENTAL_LIVE", "true")
    with pytest.raises(ValueError, match="LIVE_NOT_ADMITTED"):
        AntigravityCliAdapter(admission_path=setup[1])
    with pytest.raises(ValueError, match="LIVE_NOT_ADMITTED"):
        default_registry().create("antigravity_cli", None)


@pytest.mark.parametrize(
    "key,value",
    [
        ("ANTIGRAVITY_EXPERIMENTAL_LIVE", False),
        ("ANTIGRAVITY_EXPERIMENTAL_LIVE", "true"),
        ("ANTIGRAVITY_EXPERIMENTAL_LIVE", 1),
        ("provider", "agy"),
        ("profile", "antigravity"),
        ("session_mode", "latest"),
        ("dca_retries", True),
        ("dca_retries", 1),
        ("provider_fallback", True),
        ("customization_baseline", "UNKNOWN"),
        ("state_ownership", "DCA"),
        ("model", "--continue"),
        ("model", ""),
        ("unexpected", "x"),
    ],
)
def test_config_invalid_even_when_digest_reviewed(setup, key, value):
    config, path, approval, _, task, profile = setup
    config[key] = value
    path.write_text(json.dumps(config))
    approval = replace(approval, config_sha256=sha(path.read_bytes()))
    with pytest.raises(ValueError, match="ADMISSION_INVALID"):
        AntigravityCliAdapter(admission_path=path, approval=approval).preflight(
            task=task, profile=profile
        )


@pytest.mark.parametrize("change", ["config", "binary", "agent", "extra", "mode"])
def test_identity_drift_before_launch(setup, change):
    _, path, approval, workspace, task, profile = setup
    if change == "config":
        path.write_text(path.read_text() + " ")
    elif change == "binary":
        approval.executable.write_text("#!/bin/false\n")
    elif change == "agent":
        (workspace / AGENT_RELATIVE).write_text("tools: [run_command]\n")
    elif change == "extra":
        (workspace / ".agents/hooks.json").write_text("{}")
    else:
        path.chmod(0o644)
    with pytest.raises(ValueError, match="ADMISSION_INVALID"):
        adapter(setup).preflight(task=task, profile=profile)


@pytest.mark.parametrize(
    "policy",
    [
        {"tools": ["shell"]},
        {"read_paths": ["/synthetic"]},
        {"write_paths": ["/synthetic"]},
    ],
)
def test_no_silent_permission_widening(setup, policy):
    task = setup[4].model_copy(update={"capability_policy": CapabilityPolicy(**policy)})
    with pytest.raises(ValueError, match="ADMISSION_INVALID"):
        adapter(setup).preflight(task=task, profile=setup[5])


def test_success_capture_shard_and_no_repeat(setup, tmp_path):
    result = invoke(setup, tmp_path)
    assert result.status == "success"
    assert result.usage["input_tokens"] == 10
    assert result.provenance["dca_logical_invocations"] == 1
    assert result.provenance["dca_retries"] == 0
    assert result.provenance["provider_requests"] == "UNKNOWN"
    assert result.provenance["configured_model"] is None
    assert result.provenance["observed_init"]["tools"] == []
    assert result.provenance["permission_enforcement"] == "NOT_VERIFIED"
    assert "--sandbox" in result.provenance["argv"]
    assert all(not s.startswith("--model") for s in result.provenance["argv"])
    shard = tmp_path / "workers" / setup[4].task_id / "antigravity_cli/one"
    files = read_verified_shard(shard)
    assert len(files) == 8
    with pytest.raises(ValueError, match="ADMISSION_INVALID"):
        invoke(setup, tmp_path)
    assert read_verified_shard(shard) == files


@pytest.mark.parametrize(
    "mode,code",
    [
        ("auth", "CLI_NONZERO_EXIT"),
        ("hang", "TIMEOUT"),
        ("truncated", "EVENT_STREAM_INVALID"),
        ("partial", "EVENT_STREAM_INVALID"),
        ("after_error", "EVENT_STREAM_INVALID"),
        ("duplicate", "EVENT_STREAM_INVALID"),
    ],
)
def test_failure_injection_no_retry(setup, tmp_path, monkeypatch, mode, code):
    # The suite replaces os.environ with a plain dict; inherited execve env
    # intentionally ignores it. Pin the fake mode in reviewed fixture bytes.
    executable = setup[2].executable
    executable.write_text(
        executable.read_text().replace(
            'os.environ.get("FAKE_AGY_MODE", "success")', repr(mode)
        )
    )
    approval = replace(setup[2], executable_sha256=sha(executable.read_bytes()))
    setup = (setup[0], setup[1], approval, *setup[3:])
    setup = (*setup[:4], setup[4].model_copy(update={"timeout": 0.3}), setup[5])
    result = invoke(setup, tmp_path)
    assert result.status == "failure"
    assert result.failure_code == code
    assert result.provenance["dca_retries"] == 0
    assert (
        len(
            read_verified_shard(
                tmp_path / "workers" / setup[4].task_id / "antigravity_cli/one"
            )
        )
        == 8
    )


def test_unset_model_is_not_inferred(setup):
    plan = compile_headless(
        task=setup[4], executable="/synthetic/agy", model=None, prompt="fixed"
    )
    assert not any(arg.startswith("--model") for arg in plan.argv)


def test_missing_executable_never_probes(setup, monkeypatch):
    _, path, approval, _, task, profile = setup
    approval = replace(approval, executable=Path("/nonexistent/synthetic-agy"))
    monkeypatch.setattr(os, "system", lambda *_: pytest.fail("must not execute"))
    with pytest.raises(ValueError, match="ADMISSION_INVALID"):
        AntigravityCliAdapter(admission_path=path, approval=approval).preflight(
            task=task, profile=profile
        )


def test_text_only_agent_frontmatter_pins_documented_configuration():
    # Inspect the generated definition, not init's advertised inventory.
    frontmatter = dict(
        line.split(": ", 1)
        for line in AGENT_DEFINITION.split("---", 2)[1].strip().splitlines()
    )
    assert frontmatter == {
        "name": "dca-admission-text-only",
        "description": "Single-turn text-only DCA admission probe.",
        "mainAgent": "true",
        "subagent": "false",
        "excludeDefaultComponents": "true",
        "tools": "[]",
        "mcpServers": "[]",
        "skills": "[]",
        "plugins": "[]",
        "commandExecutionPolicy": '"off"',
    }


def test_missing_schema_flag_rejected_before_subprocess(setup, tmp_path, monkeypatch):
    from dataclasses import replace

    from dispatcher_for_codex_agents.agent_harness import antigravity_live

    original = antigravity_live.compile_headless

    def missing_schema(**kwargs):
        plan = original(**kwargs)
        argv = list(plan.argv)
        i = argv.index("--json-schema")
        del argv[i : i + 2]
        return replace(plan, argv=tuple(argv))

    monkeypatch.setattr(antigravity_live, "compile_headless", missing_schema)
    monkeypatch.setattr(
        antigravity_live.subprocess,
        "Popen",
        lambda *a, **k: pytest.fail("subprocess must not launch"),
    )
    with pytest.raises(ValueError, match="SCHEMA_ARGUMENT_INVALID"):
        invoke(setup, tmp_path)
    assert not (tmp_path / "workers").exists()
    assert not (setup[3] / ".dca-admission-used").exists()


@pytest.mark.parametrize("version", ["1.2.12", "1.2.14", "1.2.13-rc.1", "UNKNOWN"])
def test_live_version_is_exact_not_historical_or_future(setup, version):
    approved = replace(setup[2], version=version)
    instance = AntigravityCliAdapter(admission_path=setup[1], approval=approved)
    with pytest.raises(ValueError, match="ADMISSION_INVALID"):
        instance.preflight(task=setup[4], profile=setup[5])


def test_version_probe_and_task_both_freeze_child_environment(
    setup, tmp_path, monkeypatch
):
    from dispatcher_for_codex_agents.agent_harness import antigravity_live

    monkeypatch.setenv("AGY_CLI_DISABLE_AUTO_UPDATE", "1")
    original = antigravity_live.subprocess.Popen
    calls = []

    def observe(argv, **kwargs):
        assert kwargs["env"]["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"
        calls.append(list(argv))
        return original(argv, **kwargs)

    monkeypatch.setattr(antigravity_live.subprocess, "Popen", observe)
    result = invoke(setup, tmp_path)
    assert result.status == "success"
    assert len(calls) == 2
    assert calls[0] == [str(setup[2].executable), "--version"]
    assert any(arg.startswith("--print=") for arg in calls[1])
    assert os.environ["AGY_CLI_DISABLE_AUTO_UPDATE"] == "1"
    assert result.provenance["runtime_version"] == "1.2.13"
    assert result.provenance["runtime_version_check"] == "PASS"
    assert result.provenance["prespawn_binary_check"] == "PASS"
    assert result.provenance["auto_update_environment"] == {
        "AGY_CLI_DISABLE_AUTO_UPDATE": "true"
    }


@pytest.mark.parametrize("version", ["1.2.12", "1.2.14", "1.2.13-rc.1"])
def test_actual_version_mismatch_aborts_before_prompt(setup, tmp_path, version):
    executable = setup[2].executable
    executable.write_text(
        executable.read_text().replace(
            'print("1.2.13")', repr("unused") + "\n    print(" + repr(version) + ")"
        )
    )
    approved = replace(setup[2], executable_sha256=sha(executable.read_bytes()))
    altered = (setup[0], setup[1], approved, *setup[3:])
    with pytest.raises(ValueError, match="VERSION_UNSUPPORTED"):
        invoke(altered, tmp_path)
    assert not (setup[3] / ".dca-admission-used").exists()
    assert not (tmp_path / "workers").exists()


@pytest.mark.parametrize("stage", ["during_version", "after_reservation"])
def test_binary_drift_between_preflight_and_spawn_fails_closed(
    setup, tmp_path, monkeypatch, stage
):
    from dispatcher_for_codex_agents.agent_harness import antigravity_live

    instance = adapter(setup)
    instance.preflight(task=setup[4], profile=setup[5])
    executable = setup[2].executable
    if stage == "during_version":
        original = antigravity_live.subprocess.run

        def mutate(*args, **kwargs):
            result = original(*args, **kwargs)
            executable.write_text(executable.read_text() + "\n# updated\n")
            return result

        monkeypatch.setattr(antigravity_live.subprocess, "run", mutate)
    else:
        original_writer = antigravity_live.ImmutableShardWriter

        def mutate(**kwargs):
            writer = original_writer(**kwargs)
            executable.write_text(executable.read_text() + "\n# updated\n")
            return writer

        monkeypatch.setattr(antigravity_live, "ImmutableShardWriter", mutate)
    original_popen = antigravity_live.subprocess.Popen
    launches = []

    def no_prompt(argv, **kwargs):
        assert argv[1:] == ["--version"]
        launches.append(argv)
        return original_popen(argv, **kwargs)

    monkeypatch.setattr(antigravity_live.subprocess, "Popen", no_prompt)
    if stage == "during_version":
        with pytest.raises(ValueError, match="BINARY_DRIFT"):
            instance.invoke(
                task=setup[4],
                profile=setup[5],
                attempt_id="drift",
                workers_root=tmp_path,
            )
        assert not (tmp_path / "workers").exists()
    else:
        result = instance.invoke(
            task=setup[4], profile=setup[5], attempt_id="drift", workers_root=tmp_path
        )
        assert result.failure_code == "POLICY_VIOLATION"
        assert result.provenance["dca_logical_invocations"] == 0
        assert result.provenance["prespawn_binary_check"] == "FAIL"
        shard = tmp_path / "workers" / setup[4].task_id / "antigravity_cli/drift"
        assert len(read_verified_shard(shard)) == 8
    assert len(launches) == 1


@pytest.fixture
def default_setup(setup):
    with tempfile.TemporaryDirectory(prefix="dca-default-fixture-", dir="/tmp") as raw:
        workspace = Path(raw) / "workspace"
        workspace.mkdir(mode=0o700)
        config, path, approval, _, task, profile = setup
        config = {
            **config,
            "profile": DEFAULT_ADMISSION_PROFILE,
            "workspace": str(workspace),
        }
        path.write_text(json.dumps(config))
        approval = replace(approval, config_sha256=sha(path.read_bytes()))
        profile = profile.model_copy(update={"profile_id": DEFAULT_ADMISSION_PROFILE})
        task = task.model_copy(
            update={
                "prompt_template": 'Return {"ok":"DCA_AGY_L1_OK"}.',
                "expected_output_schema": {
                    "type": "object",
                    "properties": {"ok": {"const": "DCA_AGY_L1_OK"}},
                    "required": ["ok"],
                    "additionalProperties": False,
                },
            }
        )
        yield config, path, approval, workspace, task, profile


def test_default_fresh_l1_exact_command_and_empty_cwd(
    default_setup, tmp_path, monkeypatch
):
    from dispatcher_for_codex_agents.agent_harness import antigravity_live

    original = antigravity_live.subprocess.Popen
    calls = []

    def observe(argv, **kwargs):
        assert not list(default_setup[3].iterdir())
        assert kwargs["env"]["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"
        calls.append(argv)
        return original(argv, **kwargs)

    monkeypatch.setattr(antigravity_live.subprocess, "Popen", observe)
    result = invoke(default_setup, tmp_path)
    assert result.status == "success"
    assert result.final_output == {"ok": "DCA_AGY_L1_OK"}
    assert result.provenance["agent_selection"] == "DEFAULT_NO_OVERRIDE"
    assert result.provenance["user_input_step_count"] == 1
    assert result.provenance["result_num_turns"] == 1
    assert result.provenance["configured_model"] is None
    argv = calls[-1]
    assert "--sandbox" in argv
    assert argv.count("--json-schema") == 1
    assert len([a for a in argv if a.startswith("--print=")]) == 1
    assert all(
        not a.startswith(("--agent", "--model", "--continue", "--conversation"))
        for a in argv
    )
    assert len(calls) == 2  # version probe plus one synthetic task
    assert not list(default_setup[3].iterdir())
    with pytest.raises(ValueError, match="ADMISSION_INVALID"):
        invoke(default_setup, tmp_path)
    assert len(calls) == 2


@pytest.mark.parametrize("kind", ["custom_agent", "file", "mcp", "symlink", "model"])
def test_default_profile_cannot_inherit_local_customization(default_setup, kind):
    config, path, approval, workspace, task, profile = default_setup
    if kind == "model":
        config["model"] = "synthetic"
        path.write_text(json.dumps(config))
        approval = replace(approval, config_sha256=sha(path.read_bytes()))
    elif kind == "symlink":
        (workspace / "escape").symlink_to(path)
    elif kind == "custom_agent":
        agent = workspace / AGENT_RELATIVE
        agent.parent.mkdir(parents=True)
        agent.write_text(AGENT_DEFINITION)
    else:
        (workspace / (".mcp.json" if kind == "mcp" else "file.txt")).write_text("{}")
    with pytest.raises(ValueError, match="ADMISSION_INVALID"):
        AntigravityCliAdapter(admission_path=path, approval=approval).preflight(
            task=task, profile=profile
        )


@pytest.mark.parametrize(
    "policy",
    [
        {"read_paths": ["/synthetic"]},
        {"write_paths": ["/synthetic"]},
        {"tools": ["shell"]},
        {"tools": ["web_search"]},
        {"tools": ["mcp:synthetic:tool"]},
    ],
)
def test_default_profile_does_not_grant_permissions(default_setup, policy):
    task = default_setup[4].model_copy(
        update={"capability_policy": CapabilityPolicy(**policy)}
    )
    with pytest.raises(ValueError, match="ADMISSION_INVALID"):
        adapter(default_setup).preflight(task=task, profile=default_setup[5])


def test_const_only_string_schema_is_exact_and_not_rewritten():
    from dispatcher_for_codex_agents.agent_harness.schema import (
        OutputSchemaError,
        validate_json_schema,
        validate_schema_definition,
    )

    schema = {
        "type": "object",
        "properties": {"ok": {"const": "DCA_AGY_L1_OK"}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    before = json.dumps(schema)
    validate_schema_definition(schema)
    validate_json_schema({"ok": "DCA_AGY_L1_OK"}, schema)
    assert json.dumps(schema) == before
    for value in (
        {},
        {"ok": "wrong"},
        {"ok": None},
        {"ok": 1},
        {"ok": True},
        {"ok": "DCA_AGY_L1_OK", "extra": 1},
    ):
        with pytest.raises(OutputSchemaError):
            validate_json_schema(value, schema)
    for invalid in (
        {"const": 1},
        {"const": True},
        {"const": None},
        {"const": []},
        {"const": "x", "unknown": 1},
        {},
    ):
        with pytest.raises(OutputSchemaError):
            validate_schema_definition(invalid)


@pytest.mark.parametrize(
    "mode,warning",
    [
        ("default_contract_blocker", "TEXT_ONLY_TOOL_ACTIVITY"),
        ("missing_structured", "AGY_STRUCTURED_OUTPUT_CONTRACT_VIOLATION"),
    ],
)
def test_default_terminal_success_does_not_recover_invalid_capture(
    default_setup, tmp_path, monkeypatch, mode, warning
):
    from dispatcher_for_codex_agents.agent_harness import antigravity_live

    executable = default_setup[2].executable
    executable.write_text(
        executable.read_text().replace(
            'os.environ.get("FAKE_AGY_MODE", "success")', repr(mode)
        )
    )
    approved = replace(default_setup[2], executable_sha256=sha(executable.read_bytes()))
    selected = (default_setup[0], default_setup[1], approved, *default_setup[3:])
    original = antigravity_live.subprocess.Popen
    launches = []

    def observe(argv, **kwargs):
        launches.append(argv)
        return original(argv, **kwargs)

    monkeypatch.setattr(antigravity_live.subprocess, "Popen", observe)
    result = invoke(selected, tmp_path)
    assert result.status == "failure"
    assert result.exit_code == 0
    assert result.failure_code == "EVENT_STREAM_INVALID"
    assert warning in result.warnings
    assert result.final_output is None
    assert result.provenance["dca_logical_invocations"] == 1
    assert result.provenance["dca_retries"] == 0
    assert len(launches) == 2  # version plus one synthetic invocation, no retry
    assert not list(selected[3].iterdir())
    shard = (
        tmp_path / "workers" / selected[4].task_id / DEFAULT_ADMISSION_PROFILE / "one"
    )
    files = read_verified_shard(shard)
    assert len(files) == 8
    events = [
        json.loads(line) for line in (shard / "events.jsonl").read_text().splitlines()
    ]
    terminal = events[-1]["result"]
    assert terminal["status"] == "SUCCESS"
    assert terminal["num_turns"] == 1
    assert terminal["json_schema"] == selected[4].expected_output_schema
    assert "structured_output" not in terminal
    if mode == "default_contract_blocker":
        assert len(terminal["denied_actions"]) == 1
        tool_updates = [
            row["step_update"]
            for row in events
            if row.get("step_update", {}).get("step_type") == "tool"
        ]
        assert [row["state"] for row in tool_updates] == ["ACTIVE", "DONE"]
        assert {row["step_index"] for row in tool_updates} == {2}
    with pytest.raises(ValueError, match="ADMISSION_INVALID"):
        invoke(selected, tmp_path)
    assert len(launches) == 2
    assert read_verified_shard(shard) == files
