"""Fixed-version synthetic Codex exec events, not live provider evidence."""

import copy
import json

import pytest
from test_agent_capabilities import config_overrides
from test_agent_harness import _adapter, _profile
from test_runtime_contract import stream

from dispatcher_for_codex_agents.agent_harness.adapter import evaluate_capture
from dispatcher_for_codex_agents.agent_harness.runtime_contract import (
    CLASSIFIER_VERSION,
    classify_activity,
    protocol_for_version,
)

FEATURES_158 = (
    "daemon_auto_start",
    "agent_message_board",
    "send_message_to_user_async",
)


@pytest.mark.parametrize("version", ["0.153.4", "0.155.1", "0.158.0"])
def test_explicit_version_admission_and_historical_lifecycle(version):
    host = "codex-cli " + version
    assert protocol_for_version(host) == "codex-exec/" + version
    assert CLASSIFIER_VERSION == "dca-activity/3"
    assert classify_activity(stream(), version=host, complete=True)[
        "classifications"
    ] == ["NO_TOOL_ACTIVITY"]


@pytest.mark.parametrize("version", ["0.158.1", "0.159.0", "0.158.0-alpha", "UNKNOWN"])
def test_no_future_wildcards(version):
    assert protocol_for_version("codex-cli " + version) == "UNSUPPORTED"


@pytest.mark.parametrize(
    "results", [None, [], [{"title": "synthetic", "snippet": "text"}]]
)
def test_optional_search_results_do_not_change_tool_authority(results):
    item = {
        "type": "item.completed",
        "item": {
            "id": "search",
            "type": "web_search",
            "query": "synthetic",
            "action": {"type": "search", "query": "synthetic"},
        },
    }
    baseline = classify_activity(
        stream(item), version="codex-cli 0.158.0", complete=True
    )
    enriched = copy.deepcopy(item)
    enriched["item"]["results"] = results
    observed = classify_activity(
        stream(enriched), version="codex-cli 0.158.0", complete=True
    )
    assert observed == baseline
    assert "TOOL_EXECUTED" in observed["classifications"]


@pytest.mark.parametrize("version", ["0.153.4", "0.155.1", "0.158.0"])
def test_standalone_and_override_precedence(tmp_path, version):
    adapter, task = _adapter(tmp_path, "success")
    home = tmp_path / "codex-home"
    (home / "config.toml").write_text(
        'model="base-must-not-win"\nmodel_provider="base"\nweb_search="live"\n'
        "[features]\nshell_tool=true\nmulti_agent=true\n"
        "daemon_auto_start=true\nagent_message_board=true\n"
        "send_message_to_user_async=true\n"
        '[model_providers.fake]\nname="Synthetic"\n'
    )
    adapter._cli_version = "codex-cli " + version
    preview = adapter.preflight(task=task, profile=_profile())
    args = preview["command"]
    overrides = config_overrides(args)
    assert args[args.index("--profile") + 1] == "fake-profile"
    assert "--strict-config" in args
    assert args.index("--profile") < args.index("exec")
    assert "--ephemeral" in args and args[-1] == "-"
    assert overrides["web_search"] == "disabled"
    assert overrides["mcp_servers"] == {}
    assert overrides["notify"] == []
    assert overrides["shell_environment_policy"]["inherit"] == "none"
    disabled = {args[i + 1] for i, x in enumerate(args[:-1]) if x == "--disable"}
    assert {
        "multi_agent",
        "shell_tool",
        "unified_exec",
        "hooks",
        "shell_snapshot",
    } <= disabled
    assert all((f in disabled) == (version == "0.158.0") for f in FEATURES_158)
    resolved = adapter._resolver.resolve("fake-profile")
    assert resolved.configured_model == "fake-model"
    assert resolved.configured_provider == "fake"
    capture = evaluate_capture(
        task=task,
        profile=_profile(),
        stdout="\n".join(
            json.dumps(e) for e in stream(text='{"decision":"TYPE_A","reason":"done"}')
        ),
        stderr="",
        cli_version="codex-cli " + version,
        exit_code=0,
        provenance={"agent_process_started": True, "configured_model": "fake-model"},
    )[0]
    assert capture.status == "success"


def _capture_for_version(tmp_path, version, *, extra=(), policy=None):
    from test_agent_harness import _task

    from dispatcher_for_codex_agents.agent_harness.contracts import CapabilityPolicy

    return evaluate_capture(
        task=_task(
            tmp_path / "not-read", capability_policy=policy or CapabilityPolicy()
        ),
        profile=_profile(),
        stdout="\n".join(
            json.dumps(e)
            for e in stream(*extra, text='{"decision":"TYPE_A","reason":"done"}')
        ),
        stderr="",
        cli_version=version,
        exit_code=0,
        provenance={"agent_process_started": True, "configured_model": "fake-model"},
    )[0]


@pytest.mark.parametrize(
    "version",
    [
        "codex-cli 0.158.1",
        "codex-cli 0.159.0",
        "codex-cli 0.158.0-alpha",
        "codex-app-server 0.158.0",
        "0.158.0",
        "UNKNOWN",
        "codex-cli 0.158.0\n",
        "codex-cli 00.158.0",
    ],
)
def test_unsupported_version_cannot_accept_or_invoke(tmp_path, monkeypatch, version):
    import subprocess

    result = _capture_for_version(tmp_path, version)
    assert result.failure_code == "EVENT_STREAM_INVALID"
    adapter, task = _adapter(tmp_path, "success")
    adapter._cli_version = version

    def forbidden(*args, **kwargs):
        pytest.fail("Unadmitted version must not launch any subprocess")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    result = adapter.invoke(
        task=task,
        profile=_profile(),
        attempt_id="blocked",
        workers_root=tmp_path / "run",
    )
    assert result.failure_code == "PROFILE_CONFIGURATION_INVALID"
    assert adapter.calls_started == 0
    assert "CODEX_EXEC_VERSION_NOT_ADMITTED" in result.warnings


@pytest.mark.parametrize(
    "results", ["invalid", {}, True, 7, [float("nan")], [float("inf")]]
)
def test_malformed_search_results_fail_even_with_authorized_web(tmp_path, results):
    from dispatcher_for_codex_agents.agent_harness.contracts import CapabilityPolicy

    item = {
        "type": "item.completed",
        "item": {
            "id": "search",
            "type": "web_search",
            "query": "synthetic",
            "action": {"type": "search"},
            "results": results,
        },
    }
    result = _capture_for_version(
        tmp_path,
        "codex-cli 0.158.0",
        extra=(item,),
        policy=CapabilityPolicy(tools=("web_search",)),
    )
    assert result.failure_code == "EVENT_STREAM_INVALID"


@pytest.mark.parametrize(
    "results", [None, [], [{"title": "text"}], [1, "text", True, None, [2]]]
)
def test_well_typed_results_follow_existing_authorization(tmp_path, results):
    from dispatcher_for_codex_agents.agent_harness.contracts import CapabilityPolicy

    item = {
        "type": "item.completed",
        "item": {
            "id": "search",
            "type": "web_search",
            "query": "synthetic",
            "action": {"type": "search"},
            "results": results,
        },
    }
    assert (
        _capture_for_version(tmp_path, "codex-cli 0.158.0", extra=(item,)).status
        == "failure"
    )
    assert (
        _capture_for_version(
            tmp_path,
            "codex-cli 0.158.0",
            extra=(item,),
            policy=CapabilityPolicy(tools=("web_search",)),
        ).status
        == "success"
    )


def test_failed_version_probe_is_not_a_version_certificate(tmp_path, monkeypatch):
    import subprocess

    adapter, _ = _adapter(tmp_path, "success")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "codex-cli 0.158.0", ""),
    )
    assert adapter._read_cli_version({}) == "NOT_REPORTED"
