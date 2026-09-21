"""Task capability compiler, offline host emulation and immutable result tests.

The fake executable verifies compiled grants; it is not a kernel sandbox test.
"""

from __future__ import annotations

import hashlib
import json
import os
import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_agent_harness import _adapter, _profile, _shard
from test_agent_harness_cli import _verify_hashes

from dispatcher_for_codex_agents.agent_harness import CapabilityPolicy, FailureCode
from dispatcher_for_codex_agents.agent_harness import capabilities as cap
from dispatcher_for_codex_agents.agent_harness.adapter import _parse_event_stream
from dispatcher_for_codex_agents.agent_harness.capabilities import (
    CapabilityError,
    canonical_path,
    host_overrides,
    permits,
    policy_record,
    validate_paths,
)
from dispatcher_for_codex_agents.agent_harness.cli import _load_task, build_parser

FAKE_PROVIDER = '\n[model_providers.fake]\nname="Fake Provider"\n'


def policy_task(task, **grants):
    return task.model_copy(update={"capability_policy": CapabilityPolicy(**grants)})


def invoke(adapter, task, tmp_path, attempt="one"):
    return adapter.invoke(
        task=task, profile=_profile(), attempt_id=attempt, workers_root=tmp_path / "run"
    )


def config_overrides(command):
    def merge(target, update):
        for key, value in update.items():
            if isinstance(value, dict):
                merge(target.setdefault(key, {}), value)
            else:
                target[key] = value

    result = {}
    for i, flag in enumerate(command[:-1]):
        if flag == "--config":
            merge(result, tomllib.loads(command[i + 1]))
    return result


def test_default_restricted_and_payload_unchanged(tmp_path):
    adapter, task = _adapter(tmp_path, "success")
    preview = adapter.preflight(task=task, profile=_profile())
    args = preview["command"]
    assert args[args.index("--sandbox") + 1] == "read-only"
    assert "--enable" not in args
    overrides = config_overrides(args)
    assert overrides["web_search"] == "disabled"
    assert task.capability_policy.restricted
    result = invoke(adapter, task, tmp_path)
    assert result.status == "success"
    assert result.provenance["capability_policy"]["requested"] == {
        "read_paths": [],
        "write_paths": [],
        "tools": [],
    }


@pytest.mark.parametrize(
    "tools",
    [
        ("browser",),
        ("apps",),
        ("apply_patch",),
        ("unrestricted",),
        ("shell", "unified_exec"),
        ("shell", "shell"),
        ("mcp:a:spawn_agent",),
    ],
)
def test_unsupported_or_recursive_grants_rejected(tools):
    with pytest.raises(ValidationError):
        CapabilityPolicy(tools=tools)


@pytest.mark.parametrize(
    "key", ["tools", "read_paths", "write_paths", "capability_policy"]
)
def test_profile_support_hints_do_not_grant_authority(key):
    with pytest.raises(ValidationError):
        _profile(capabilities={key: []})


@pytest.mark.parametrize("tool", ["shell", "unified_exec"])
def test_explicit_file_read_and_provenance(tmp_path, tool):
    adapter, task = _adapter(tmp_path, "capability")
    target = tmp_path / "approved.txt"
    target.write_text("read me", encoding="utf-8")
    task = policy_task(task, read_paths=(str(target),), tools=(tool,))
    adapter._environment_overrides["FAKE_CAP_PATH"] = str(target)
    result = invoke(adapter, task, tmp_path)
    assert result.status == "success"
    assert result.final_output["reason"] == "read me"
    assert adapter.calls_started == 1
    proof = result.provenance["capability_policy"]
    assert proof["compiled_command_policy"]["read_paths"] == [str(target)]
    assert proof["compiled_command_policy_emitted"] is True
    assert (
        proof["requested_policy_sha256"]
        == hashlib.sha256(
            json.dumps(proof["requested"], sort_keys=True).encode()
        ).hexdigest()
    )
    assert proof["host_os_isolation_verified"] is False
    saved = json.loads(
        (_shard(tmp_path, "one") / "agent_task.snapshot.json").read_text()
    )
    assert saved["capability_policy"] == proof["requested"]


def test_ungranted_read_denied_by_fake_host(tmp_path):
    adapter, task = _adapter(tmp_path, "capability")
    target = tmp_path / "not-approved.txt"
    target.write_text("DO NOT EXPOSE", encoding="utf-8")
    task = policy_task(task, tools=("shell",))
    adapter._environment_overrides["FAKE_CAP_PATH"] = str(target)
    result = invoke(adapter, task, tmp_path)
    assert result.status == "success"  # reporting a denied access is a valid result
    assert result.final_output["reason"] == "PERMISSION_DENIED"
    assert not permits(task.capability_policy, target)


@pytest.mark.parametrize("writable", [False, True])
def test_read_and_write_ranges_are_separate(tmp_path, writable):
    adapter, task = _adapter(tmp_path, "capability")
    target = tmp_path / "document.txt"
    target.write_text("unchanged", encoding="utf-8")
    task = policy_task(
        task,
        tools=("shell",),
        **{"write_paths" if writable else "read_paths": (str(target),)},
    )
    adapter._environment_overrides.update(
        FAKE_CAP_PATH=str(target), FAKE_CAP_OPERATION="write"
    )
    result = invoke(adapter, task, tmp_path)
    assert result.status == "success"
    assert target.read_text() == ("fixture write" if writable else "unchanged")
    assert permits(task.capability_policy, target, write=True) == writable


def test_directory_grant_does_not_expand_to_parent_or_sibling(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    (root / "file").touch()
    (tmp_path / "sibling").touch()
    policy = CapabilityPolicy(read_paths=(str(root),))
    validate_paths(policy)
    assert permits(policy, root / "file")
    assert not permits(policy, tmp_path / "sibling")
    assert not permits(policy, root / ".." / "sibling")
    assert not permits(policy, root / "file", write=True)


@pytest.mark.parametrize(
    "kind", ["direct", "nested", "parent", "missing", "root", "relative"]
)
def test_bad_or_symlink_paths_fail_before_agent_and_keep_shard(tmp_path, kind):
    adapter, task = _adapter(tmp_path, "success")
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("outside")
    link = allowed / "link"
    link.symlink_to(outside)
    aliases = {
        "direct": str(link),
        "nested": str(allowed),
        "parent": str(allowed / ".." / "outside"),
        "missing": str(tmp_path / "missing"),
        "root": "/",
        "relative": "relative.txt",
    }
    task = policy_task(task, read_paths=(aliases[kind],), tools=("shell",))
    result = invoke(adapter, task, tmp_path)
    assert result.failure_code == FailureCode.POLICY_VIOLATION
    assert adapter.calls_started == 0
    assert result.provenance["capability_policy"]["compiled_command_policy"] is None
    assert (_shard(tmp_path, "one") / "output_sha256.tsv").is_file()


@pytest.mark.parametrize("protected", ["codex-home", "run"])
def test_auth_and_result_artifacts_cannot_be_granted(tmp_path, protected):
    adapter, task = _adapter(tmp_path, "success")
    task = policy_task(task, write_paths=(str(tmp_path / protected),), tools=("shell",))
    result = invoke(adapter, task, tmp_path)
    assert result.failure_code == FailureCode.POLICY_VIOLATION
    assert adapter.calls_started == 0


@pytest.mark.parametrize(
    "tools,item,success",
    [
        (("web_search",), {"type": "web_search", "query": "fixture"}, True),
        (("shell",), {"type": "web_search"}, False),
        (("web_search",), {"type": "command_execution", "command": "true"}, False),
        (("shell",), {"type": "command_execution", "command": "codex exec -"}, False),
        (("shell",), {"type": "file_change"}, False),
        (("shell",), {"type": "collab_agent_tool_call"}, False),
    ],
)
def test_allowlist_not_all_tools(tmp_path, tools, item, success):
    adapter, task = _adapter(tmp_path, "allowed_event")
    adapter._environment_overrides["FAKE_CAP_EVENT"] = json.dumps(item)
    task = policy_task(task, tools=tools)
    result = invoke(adapter, task, tmp_path)
    assert (result.status == "success") == success
    if not success:
        assert result.failure_code == FailureCode.POLICY_VIOLATION


def test_mcp_exact_allowlist_and_inherited_servers_disabled(tmp_path):
    adapter, task = _adapter(tmp_path, "success")
    base = tmp_path / "codex-home" / "config.toml"
    base.write_text(
        base.read_text()
        + '\n[mcp_servers.docs]\nurl="https://tools.invalid/mcp"\n[mcp_servers.other]\ncommand="blocked"\n'
    )
    task = policy_task(task, tools=("mcp:docs:lookup",))
    preview = adapter.preflight(task=task, profile=_profile())
    config = config_overrides(preview["command"])
    assert config["mcp_servers"]["docs"] == {
        "enabled": True,
        "enabled_tools": ["lookup"],
        "disabled_tools": [],
        "url": "REDACTED_ENDPOINT_SHA256:"
        + hashlib.sha256(b"https://tools.invalid/mcp").hexdigest(),
    }
    assert config["mcp_servers"]["other"]["enabled"] is False
    assert config["web_search"] == "disabled"
    for tool, expected in [("lookup", False), ("delete", True)]:
        events = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "mcp_tool_call", "server": "docs", "tool": tool},
            }
        )
        assert (
            bool(_parse_event_stream(events, task.capability_policy).policy_violations)
            == expected
        )


@pytest.mark.parametrize(
    "config",
    [
        'sandbox_mode="read-only"',
        '[permissions.dca_task.filesystem]\n"/"="write"',
        '[shell_environment_policy.set]\nPRIVATE_VALUE="must-not-inherit"',
        '[mcp_servers.docs]\ncommand="stdio-command"',
        '[mcp_servers.docs]\nurl="https://tools.invalid/mcp"\ndisabled_tools=["lookup"]',
    ],
)
def test_conflicting_host_configs_fail_closed(tmp_path, config):
    adapter, task = _adapter(tmp_path, "success")
    base = tmp_path / "codex-home" / "config.toml"
    base.write_text(config + FAKE_PROVIDER)
    task = policy_task(
        task, tools=("mcp:docs:lookup",) if "mcp_servers" in config else ("shell",)
    )
    result = invoke(adapter, task, tmp_path)
    assert result.failure_code == FailureCode.POLICY_VIOLATION
    assert adapter.calls_started == 0
    assert base.read_text() == config + FAKE_PROVIDER


def test_old_host_rejected_without_inference(tmp_path):
    adapter, task = _adapter(tmp_path, "success")
    adapter._cli_version = "codex-cli 0.144.6"
    result = invoke(adapter, policy_task(task, tools=("shell",)), tmp_path)
    assert result.failure_code == FailureCode.POLICY_VIOLATION
    assert adapter.calls_started == 0


def test_cli_task_json_is_the_only_authority_entry(tmp_path):
    adapter, task = _adapter(tmp_path, "success")
    task = policy_task(task, tools=("web_search",))
    task_file = tmp_path / "task.json"
    task_file.write_text(task.model_dump_json())
    loaded = _load_task(task_file)
    assert loaded.capability_policy == task.capability_policy
    preview = adapter.preflight(task=loaded, profile=_profile())
    assert config_overrides(preview["command"])["web_search"] == "live"
    assert adapter.calls_started == 0
    assert "invoke" in build_parser().format_help()


def test_malformed_canonical_paths_and_unknown_fields():
    for value in ("relative", "/", "/literal*", "../escape"):
        with pytest.raises(CapabilityError):
            canonical_path(value)
    with pytest.raises(ValidationError):
        CapabilityPolicy(unrestricted=True)


def test_compiler_is_deterministic(tmp_path):
    adapter, task = _adapter(tmp_path, "success")
    policy = CapabilityPolicy(tools=("shell",))
    args = dict(
        home=tmp_path / "codex-home",
        profile_id="fake-profile",
        cwd=tmp_path,
        cli_version="codex-cli 0.153.4",
    )
    assert host_overrides(policy, **args) == host_overrides(policy, **args)


def test_link_and_nonregular_root_are_rejected(tmp_path):
    first = tmp_path / "first"
    first.touch()
    os.link(first, tmp_path / "alias")
    with pytest.raises(CapabilityError, match="Hard-linked"):
        validate_paths(CapabilityPolicy(read_paths=(str(first),)))
    pipe = tmp_path / "pipe"
    os.mkfifo(pipe)
    with pytest.raises(CapabilityError):
        validate_paths(CapabilityPolicy(read_paths=(str(pipe),)))


def test_top_level_unapproved_mcp_event_is_not_ignored():
    event = json.dumps({"type": "mcp_tool_call", "server": "unexpected", "tool": "run"})
    assert _parse_event_stream(event).policy_violations


def test_unknown_host_tool_and_event_fail_closed():
    item = json.dumps({"type": "item.completed", "item": {"type": "future_tool"}})
    assert _parse_event_stream(
        item, CapabilityPolicy(tools=("shell",))
    ).policy_violations
    assert _parse_event_stream('{"type":"future.execution"}').policy_violations
    malformed = json.dumps({"type": "item.completed", "item": {"type": {}}})
    assert _parse_event_stream(malformed).policy_violations


@pytest.mark.parametrize("dry", [True, False])
def test_cli_capability_json_dry_run_and_invoke(tmp_path, monkeypatch, capsys, dry):
    from test_agent_harness_cli import _args, _verify_hashes, _write_home, _write_task

    from dispatcher_for_codex_agents.agent_harness.cli import main

    home = tmp_path / "cli-home"
    _write_home(home, ("fake-profile",))
    path = _write_task(tmp_path / "inputs")
    task = _load_task(path)
    task = policy_task(task, tools=("web_search",))
    path.write_text(task.model_dump_json())
    monkeypatch.setenv("FAKE_EXPECT_SCHEMA", "0")
    monkeypatch.setenv("FAKE_CODEX_MODE", "allowed_event")
    monkeypatch.setenv("FAKE_CAP_EVENT", json.dumps({"type": "web_search"}))
    root = tmp_path / "cli-run"
    args = _args(path, home, root, "fake-profile", "cli-one", dry=dry)
    index = args.index("--codex-executable") + 1
    binary = tmp_path / "standalone/releases/0.test-arch/bin/codex"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(Path(args[index]).read_bytes())
    binary.chmod(0o700)
    args[index] = str(binary)
    status = main(args)
    assert status == 0
    result = json.loads(capsys.readouterr().out)
    assert (
        result["agent_process_started"] is False
        if dry
        else result["status"] == "success"
    )
    if dry:
        assert not root.exists()
    else:
        shard = root / "workers" / "cli-task" / "fake-profile" / "cli-one"
        _verify_hashes(shard)
        before = (shard / "invocation_result.json").read_bytes()
        assert main(_args(path, home, root, "fake-profile", "cli-one")) == 16
        assert (shard / "invocation_result.json").read_bytes() == before


def project_configs(tmp_path, monkeypatch, outer, inner, middle=""):
    """An isolated, explicitly trusted project; no maintainer config is needed."""
    home = tmp_path / "codex-home"
    home.mkdir(exist_ok=True)
    root = tmp_path / "project"
    cwd = root / "nested" / "inner"
    cwd.mkdir(parents=True)
    (root / ".git").mkdir()
    (home / "config.toml").write_text(
        f'[projects.{json.dumps(str(root))}]\ntrust_level="trusted"\n'
    )
    for directory, content in ((root, outer), (root / "nested", middle), (cwd, inner)):
        (directory / ".codex").mkdir()
        (directory / ".codex" / "config.toml").write_text(content)
    monkeypatch.setattr(cap, "SYSTEM_CONFIG", tmp_path / "no-system-config")
    return home, cwd


def compile_mcp(home, cwd):
    compiled = {}
    overrides = host_overrides(
        CapabilityPolicy(tools=("mcp:docs:lookup",)),
        home=home,
        profile_id="fake-profile",
        cwd=cwd,
        cli_version="codex-cli 0.153.4",
        compiled=compiled,
    )
    command = [token for item in overrides for token in ("--config", item)]
    return config_overrides(command), compiled


@pytest.mark.parametrize(
    "outer,inner,accepted",
    [
        ("https://outer.invalid/mcp", "http://inner.invalid/mcp", False),
        ("http://outer.invalid/mcp", "https://inner.invalid/mcp", True),
    ],
)
def test_effective_project_endpoint_nearest_wins(
    tmp_path, monkeypatch, outer, inner, accepted
):
    home, cwd = project_configs(
        tmp_path,
        monkeypatch,
        f'[mcp_servers.docs]\nurl="{outer}"',
        f'[mcp_servers.docs]\nurl="{inner}"',
        '[mcp_servers.docs]\nurl="https://middle.invalid/mcp"',
    )
    if not accepted:
        with pytest.raises(CapabilityError, match="HTTPS"):
            compile_mcp(home, cwd)
        return
    command, proof = compile_mcp(home, cwd)
    assert command["mcp_servers"]["docs"]["url"] == inner
    assert proof["mcp_servers"]["docs"]["endpoint"] == cap.endpoint_record(inner)
    layers = cap._config_layers(home, "fake-profile", cwd)
    assert [v["mcp_servers"]["docs"]["url"] for v in layers if "mcp_servers" in v] == [
        outer,
        "https://middle.invalid/mcp",
        inner,
    ]


@pytest.mark.parametrize(
    "outer,inner",
    [
        ('url="https://outer.invalid/mcp"', 'command="stdio"'),
        ('command="stdio"', 'url="https://inner.invalid/mcp"'),
        ('command=""', 'url="https://inner.invalid/mcp"'),
        ('command="stdio"', 'command="another-stdio"'),
    ],
)
def test_mixed_or_stdio_effective_transport_rejected(
    tmp_path, monkeypatch, outer, inner
):
    home, cwd = project_configs(
        tmp_path,
        monkeypatch,
        "[mcp_servers.docs]\n" + outer,
        "[mcp_servers.docs]\n" + inner,
    )
    with pytest.raises(CapabilityError, match="HTTPS"):
        compile_mcp(home, cwd)


@pytest.mark.parametrize(
    "outer,inner,accepted",
    [
        ("enabled=true", "enabled=false", False),
        ("enabled=false", "enabled=true", True),
        ('enabled_tools=["lookup","delete"]', 'enabled_tools=["delete"]', False),
        ('enabled_tools=["delete"]', 'enabled_tools=["lookup"]', True),
        ('enabled_tools=["lookup"]', "enabled_tools=[]", False),
        ("disabled_tools=[]", 'disabled_tools=["lookup"]', False),
        ('disabled_tools=["lookup"]', 'disabled_tools=["delete"]', True),
        ('enabled_tools=["lookup"]', 'disabled_tools=["lookup"]', False),
    ],
)
def test_effective_host_server_and_tool_limits(
    tmp_path, monkeypatch, outer, inner, accepted
):
    home, cwd = project_configs(
        tmp_path,
        monkeypatch,
        '[mcp_servers.docs]\nurl="https://tools.invalid/mcp"\n' + outer,
        "[mcp_servers.docs]\n" + inner,
    )
    if not accepted:
        with pytest.raises(CapabilityError, match="MCP grant"):
            compile_mcp(home, cwd)
    else:
        config, proof = compile_mcp(home, cwd)
        assert config["mcp_servers"]["docs"]["enabled_tools"] == ["lookup"]
        assert proof["mcp_servers"]["docs"]["enabled_tools"] == ["lookup"]


def test_nested_tables_merge_and_project_boundary(tmp_path, monkeypatch):
    home, cwd = project_configs(
        tmp_path,
        monkeypatch,
        '[mcp_servers.docs]\nurl="https://tools.invalid/mcp"\n[mcp_servers.docs.http_headers]\nX-Outer="outer"\nX-Common="outer"',
        '[mcp_servers.docs.http_headers]\nX-Inner="inner"\nX-Common="inner"',
    )
    # This ancestor is outside the detected repository; it must not be read.
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text("mcp_servers=[]")
    merged = {}
    for layer in cap._config_layers(home, "fake-profile", cwd):
        cap._merge(merged, layer)
    assert merged["mcp_servers"]["docs"]["http_headers"] == {
        "X-Outer": "outer",
        "X-Inner": "inner",
        "X-Common": "inner",
    }
    compile_mcp(home, cwd)


@pytest.mark.parametrize("trust", ["untrusted", "missing"])
def test_project_trust_not_inferred_or_expanded(tmp_path, monkeypatch, trust):
    home, cwd = project_configs(tmp_path, monkeypatch, "", "")
    (home / "config.toml").write_text(
        ""
        if trust == "missing"
        else f'[projects.{json.dumps(str(cwd.parents[1]))}]\ntrust_level="untrusted"'
    )
    with pytest.raises(CapabilityError, match="trusted canonical root"):
        compile_mcp(home, cwd)


MALFORMED_CONFIGS = [
    "mcp_servers=[]",
    'mcp_servers="not a table"',
    '[mcp_servers]\ndocs="not a table"',
    "[mcp_servers]\ndocs=[]",
    "[mcp_servers]\ndocs=null",
    "[mcp_servers.docs]\nurl=[]",
    "[mcp_servers.docs]\ncommand=123",
    '[mcp_servers.docs]\nenabled="false"',
    '[mcp_servers.docs]\nenabled_tools="lookup"',
    "[mcp_servers.docs]\ndisabled_tools={lookup=true}",
    "[mcp_servers.docs]\nhttp_headers=[]",
    "[mcp_servers.docs.env_http_headers]\nAuthorization=[]",
    "[mcp_servers.docs]\nenv=[]",
    "[mcp_servers.docs]\nenv_vars={}",
    '[mcp_servers.docs]\nargs="bad"',
    "[mcp_servers.docs]\ntool_timeout_sec=[]",
    "profiles=[]",
    '[profiles]\n"fake-profile"=[]',
    "permissions=[]",
    "[permissions]\ncustom=[]",
    "[permissions.custom]\nfilesystem=[]",
    "[permissions.custom]\nnetwork=[]",
    "shell_environment_policy=[]",
    "[shell_environment_policy]\nset=[]",
    "[shell_environment_policy]\ninclude_only={}",
    "projects=[]",
    '[projects]\n"/example"=[]',
    "project_root_markers=[[]]",
]


def assert_prelaunch_failure(adapter, result, tmp_path):
    from dispatcher_for_codex_agents.agent_harness.shard import (
        ImmutableShardWriter,
        ShardExistsError,
    )

    assert result.failure_code in (
        FailureCode.POLICY_VIOLATION,
        FailureCode.PROFILE_CONFIGURATION_INVALID,
    )
    assert adapter.calls_started == 0
    assert result.provenance["agent_subprocess_count"] == 0
    assert result.provenance["capability_policy"]["compiled_command_policy"] is None
    assert not (tmp_path / "agent-calls.log").exists()
    shard = _shard(tmp_path, "one")
    assert set(p.name for p in shard.iterdir()) == set(
        ImmutableShardWriter.REQUIRED_FILES
    )
    _verify_hashes(shard)
    before = (shard / "invocation_result.json").read_bytes()
    with pytest.raises(ShardExistsError):
        ImmutableShardWriter(
            workers_root=tmp_path / "run",
            task_id="fictional-task",
            profile_id="fake-profile",
            attempt_id="one",
        )
    assert (shard / "invocation_result.json").read_bytes() == before
    assert "Traceback" not in (shard / "stderr.log").read_text()


@pytest.mark.parametrize("config", MALFORMED_CONFIGS)
def test_malformed_global_config_has_complete_failure_shard(tmp_path, config):
    adapter, task = _adapter(tmp_path, "success")
    adapter._environment_overrides["FAKE_CODEX_CALL_LOG"] = str(
        tmp_path / "agent-calls.log"
    )
    (tmp_path / "codex-home" / "config.toml").write_text(config)
    result = invoke(adapter, policy_task(task, tools=("shell",)), tmp_path)
    assert_prelaunch_failure(adapter, result, tmp_path)


@pytest.mark.parametrize("location", ["system", "sidecar", "project"])
def test_malformed_actual_config_layers_fail_cleanly(tmp_path, monkeypatch, location):
    from dispatcher_for_codex_agents.agent_harness import adapter as adapter_module

    adapter, task = _adapter(tmp_path, "success")
    adapter._environment_overrides["FAKE_CODEX_CALL_LOG"] = str(
        tmp_path / "agent-calls.log"
    )
    if location == "system":
        target = tmp_path / "system.toml"
        target.write_text("mcp_servers=[]")
        monkeypatch.setattr(cap, "SYSTEM_CONFIG", target)
    elif location == "sidecar":
        target = tmp_path / "codex-home" / "fake-profile.config.toml"
        target.write_text('model="fake-model"\nmodel_provider="fake"\nmcp_servers=[]')
    else:
        _, cwd = project_configs(tmp_path, monkeypatch, "", "mcp_servers=[]")
        monkeypatch.setattr(adapter_module, "temporary_root", lambda: cwd)
    result = invoke(adapter, policy_task(task, tools=("shell",)), tmp_path)
    assert_prelaunch_failure(adapter, result, tmp_path)


@pytest.mark.parametrize(
    "value", [None, [], "not a table", 0, {"mcp_servers": {"docs": None}}]
)
def test_non_table_roots_or_null_server_are_capability_errors(value):
    with pytest.raises(CapabilityError):
        cap._validate_layer(value)


@pytest.mark.parametrize(
    "config,exit_code,failure_code",
    [
        ("mcp_servers=[]", 15, "POLICY_VIOLATION"),
        ("[mcp_servers]\ndocs=null", 12, "PROFILE_CONFIGURATION_INVALID"),
    ],
)
def test_cli_malformed_host_returns_failure_not_traceback(
    tmp_path, monkeypatch, capsys, config, exit_code, failure_code
):
    from test_agent_harness_cli import _args, _write_home, _write_task

    from dispatcher_for_codex_agents.agent_harness.cli import main

    home = tmp_path / "cli-home"
    _write_home(home, ("fake-profile",))
    (home / "config.toml").write_text(config + FAKE_PROVIDER)
    task_file = _write_task(tmp_path / "inputs")
    monkeypatch.setenv("FAKE_CODEX_CALL_LOG", str(tmp_path / "agent-calls.log"))
    code = main(_args(task_file, home, tmp_path / "run", "fake-profile", "one"))
    captured = capsys.readouterr()
    assert code == exit_code
    assert json.loads(captured.out)["failure_code"] == failure_code
    assert "Traceback" not in captured.err
    assert not (tmp_path / "agent-calls.log").exists()
    _verify_hashes(tmp_path / "run" / "workers" / "cli-task" / "fake-profile" / "one")


def test_compiled_policy_tracks_actual_command_and_cwd(tmp_path):
    adapter, task = _adapter(tmp_path, "success")
    target = tmp_path / "write-target"
    target.mkdir()
    task = policy_task(
        task, tools=("unified_exec", "web_search"), write_paths=(str(target),)
    )
    preview = adapter.preflight(task=task, profile=_profile())
    assert preview["capability_policy"]["compiled_command_policy_emitted"] is False
    assert preview["capability_policy"]["compiled_command_policy"] is not None
    result = invoke(adapter, task, tmp_path)
    proof = result.provenance["capability_policy"]
    compiled = proof["compiled_command_policy"]
    assert (
        compiled["working_directory"]
        != preview["capability_policy"]["compiled_command_policy"]["working_directory"]
    )
    cwd = Path(compiled["working_directory"])
    assert cwd.is_absolute() and cwd.name.startswith("dca-agent-")
    assert compiled["filesystem"] == {
        ":minimal": "read",
        str(cwd): "read",
        str(target): "write",
        adapter._executable[0]: "read",
    }
    assert compiled["read_paths"] == compiled["write_paths"] == [str(target)]
    assert compiled["features"]["shell_tool"] is True
    assert compiled["features"]["unified_exec"] is True
    assert compiled["features"]["multi_agent"] is False
    assert compiled["features"]["hooks"] is False
    assert compiled["network"] == {"shell": False}
    assert compiled["web_search"] == "live"
    assert compiled["approval"] == "never"
    assert compiled["agent_recursion"] is False
    assert (
        proof["compiled_command_policy_sha256"]
        == hashlib.sha256(json.dumps(compiled, sort_keys=True).encode()).hexdigest()
    )
    assert proof["compiled_command_policy_emitted"] is True
    assert proof["host_os_isolation_verified"] is False
    assert proof["enforcement_confirmation"] == "NOT_VERIFIED"
    _verify_hashes(_shard(tmp_path, "one"))


def test_endpoint_changes_compiled_hash_without_disclosing_credentials(tmp_path):
    adapter, task = _adapter(tmp_path, "success")
    policy = CapabilityPolicy(tools=("mcp:docs:lookup",))
    base = tmp_path / "codex-home" / "config.toml"
    proofs = []
    for suffix in ("first", "second"):
        endpoint = (
            f"https://tools.invalid/private-path-{suffix}?token=SYNTHETIC_QUERY_SECRET"
        )
        base.write_text(
            f'[mcp_servers.docs]\nurl={json.dumps(endpoint)}\nbearer_token_env_var="SYNTHETIC_ENV_NAME"\n[mcp_servers.docs.http_headers]\nAuthorization="SYNTHETIC_HEADER_SECRET"\n[mcp_servers.docs.env]\nSAMPLE="SYNTHETIC_ENV_SECRET"'
            + FAKE_PROVIDER
        )
        command, compiled = adapter._build_command(
            profile=_profile(),
            isolated_working_directory=tmp_path,
            schema_path=None,
            capability_policy=policy,
        )
        assert config_overrides(command)["mcp_servers"]["docs"]["url"] == endpoint
        proofs.append(policy_record(policy, applied=False, compiled=compiled))
        preview = adapter.preflight(
            task=policy_task(task, tools=policy.tools), profile=_profile()
        )
        serialized = json.dumps(preview)
        for private in (
            "SYNTHETIC_QUERY_SECRET",
            "SYNTHETIC_HEADER_SECRET",
            "SYNTHETIC_ENV_SECRET",
            "SYNTHETIC_ENV_NAME",
            "private-path-",
        ):
            assert private not in serialized
    assert proofs[0]["requested_policy_sha256"] == proofs[1]["requested_policy_sha256"]
    assert (
        proofs[0]["compiled_command_policy_sha256"]
        != proofs[1]["compiled_command_policy_sha256"]
    )
    result = invoke(adapter, policy_task(task, tools=policy.tools), tmp_path)
    assert result.status == "success"
    shard_content = "".join(p.read_text() for p in _shard(tmp_path, "one").iterdir())
    assert "SYNTHETIC_" not in shard_content and "private-path-" not in shard_content


def test_credential_userinfo_is_rejected_without_echo(tmp_path):
    adapter, task = _adapter(tmp_path, "success")
    # Construct a synthetic credential-bearing URL only inside the negative test;
    # no literal credential URL belongs in the distributable source snapshot.
    userinfo = ":".join(("SYNTHETIC_USER", "SYNTHETIC_PASSWORD"))
    endpoint = "https://" + userinfo + "@tools.invalid/mcp"
    (tmp_path / "codex-home" / "config.toml").write_text(
        "[mcp_servers.docs]\nurl=" + json.dumps(endpoint) + FAKE_PROVIDER
    )
    result = invoke(adapter, policy_task(task, tools=("mcp:docs:lookup",)), tmp_path)
    assert_prelaunch_failure(adapter, result, tmp_path)
    assert "SYNTHETIC" not in result.model_dump_json()


def test_ambiguous_legacy_profile_capabilities_fail_closed(tmp_path):
    adapter, task = _adapter(tmp_path, "success")
    (tmp_path / "codex-home" / "config.toml").write_text(
        '[profiles."fake-profile".mcp_servers.docs]\nenabled=false\n'
        '[mcp_servers.docs]\nurl="https://tools.invalid/mcp"\nenabled=true'
    )
    result = invoke(adapter, policy_task(task, tools=("mcp:docs:lookup",)), tmp_path)
    assert_prelaunch_failure(adapter, result, tmp_path)
    assert result.failure_code == FailureCode.PROFILE_CONFIGURATION_INVALID
    assert "PROFILE_LAYOUT_CONFLICT" in " ".join(result.warnings)
    assert result.provenance["profile_compatibility"]["profile_layout"] == "conflict"


def trust_records(*records):
    return "".join(
        f"[projects.{json.dumps(str(path))}]\ntrust_level={json.dumps(level)}\n"
        for path, level in records
    )


def spy_project_reads(monkeypatch, root):
    reads = []
    original = cap._read_layers

    def read(file, profile_id):
        if file.is_relative_to(root):
            reads.append(file)
        return original(file, profile_id)

    monkeypatch.setattr(cap, "_read_layers", read)
    return reads


TRUST_MCP_CONFIG = (
    '[mcp_servers.docs]\nurl="https://synthetic.invalid/mcp"\n'
    'enabled=true\nenabled_tools=["lookup"]\n'
)


@pytest.mark.parametrize(
    "levels",
    [
        ("trusted", "untrusted", None),
        ("trusted", None, "untrusted"),
        ("untrusted", "trusted", "trusted"),
        ("trusted", "untrusted", "trusted"),
    ],
)
def test_trust_interval_deny_before_any_project_consumption(
    tmp_path, monkeypatch, levels
):
    home, cwd = project_configs(
        tmp_path, monkeypatch, TRUST_MCP_CONFIG, TRUST_MCP_CONFIG, TRUST_MCP_CONFIG
    )
    root = cwd.parents[1]
    (home / "config.toml").write_text(
        trust_records(
            *(
                (p, level)
                for p, level in zip((root, cwd.parent, cwd), levels, strict=True)
                if level
            )
        )
    )
    reads = spy_project_reads(monkeypatch, root)
    compiled = {}
    with pytest.raises(CapabilityError, match="trust"):
        host_overrides(
            CapabilityPolicy(tools=("shell", "mcp:docs:lookup")),
            home=home,
            profile_id="fake-profile",
            cwd=cwd,
            cli_version="codex-cli 0.153.4",
            compiled=compiled,
        )
    assert reads == []  # Not even the trusted root config was consumed.
    assert compiled == {}  # No override/proof could promote an untrusted layer.


def test_trust_interval_nested_trusted_retains_nearest_precedence(
    tmp_path, monkeypatch
):
    home, cwd = project_configs(
        tmp_path,
        monkeypatch,
        TRUST_MCP_CONFIG,
        TRUST_MCP_CONFIG.replace("synthetic.invalid", "inner.invalid"),
    )
    root = cwd.parents[1]
    (home / "config.toml").write_text(
        trust_records((root, "trusted"), (cwd, "trusted"), (tmp_path, "untrusted"))
    )
    # A config outside the root must not be read, even though it is present.
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text("not valid TOML")
    reads = spy_project_reads(monkeypatch, tmp_path)
    config, proof = compile_mcp(home, cwd)
    project_reads = [p for p in reads if p.parent.name == ".codex"]
    assert project_reads == [
        p / ".codex" / "config.toml" for p in (root, cwd.parent, cwd)
    ]
    assert config["mcp_servers"]["docs"]["url"] == "https://inner.invalid/mcp"
    assert proof["mcp_servers"]["docs"]["endpoint"] == cap.endpoint_record(
        config["mcp_servers"]["docs"]["url"]
    )


@pytest.mark.parametrize("denied_layer", ["system", "base", "sidecar"])
def test_trust_interval_conflict_cannot_be_erased_by_later_layer(
    tmp_path, monkeypatch, denied_layer
):
    home, cwd = project_configs(
        tmp_path, monkeypatch, TRUST_MCP_CONFIG, TRUST_MCP_CONFIG
    )
    root = cwd.parents[1]
    system = tmp_path / "system.toml"
    monkeypatch.setattr(cap, "SYSTEM_CONFIG", system)
    for name, path in (
        ("system", system),
        ("base", home / "config.toml"),
        ("sidecar", home / "fake-profile.config.toml"),
    ):
        path.write_text(
            trust_records(
                (root, "trusted"),
                (cwd, "untrusted" if name == denied_layer else "trusted"),
            )
        )
    reads = spy_project_reads(monkeypatch, root)
    with pytest.raises(CapabilityError, match="deny"):
        compile_mcp(home, cwd)
    assert reads == []


@pytest.mark.parametrize("second_level", ["trusted", "untrusted"])
def test_trust_interval_duplicate_semantic_mapping_rejected(
    tmp_path, monkeypatch, second_level
):
    home, cwd = project_configs(
        tmp_path, monkeypatch, TRUST_MCP_CONFIG, TRUST_MCP_CONFIG
    )
    root = cwd.parents[1]
    (home / "config.toml").write_text(
        trust_records((root, "trusted"), (str(root) + "/.", second_level))
    )
    reads = spy_project_reads(monkeypatch, root)
    with pytest.raises(CapabilityError, match="Duplicate semantic"):
        compile_mcp(home, cwd)
    assert reads == []


@pytest.mark.parametrize("suffix", ["/", "/.", "//./"])
def test_trust_interval_canonical_equivalent_path_normalization(
    tmp_path, monkeypatch, suffix
):
    home, cwd = project_configs(
        tmp_path, monkeypatch, TRUST_MCP_CONFIG, TRUST_MCP_CONFIG
    )
    (home / "config.toml").write_text(
        trust_records((str(cwd.parents[1]) + suffix, "trusted"))
    )
    config, _ = compile_mcp(home, cwd)
    assert config["mcp_servers"]["docs"]["enabled_tools"] == ["lookup"]


@pytest.mark.parametrize(
    "kind", ["relative", "parent", "symlink", "double-root", "glob", "missing-level"]
)
def test_trust_interval_unsafe_or_ambiguous_mapping_rejected(
    tmp_path, monkeypatch, kind
):
    home, cwd = project_configs(
        tmp_path, monkeypatch, TRUST_MCP_CONFIG, TRUST_MCP_CONFIG
    )
    root = cwd.parents[1]
    alias = tmp_path / "alias"
    alias.symlink_to(cwd, target_is_directory=True)
    raw = {
        "relative": "project/nested/inner",
        "parent": str(cwd / ".."),
        "symlink": str(alias),
        "double-root": "/" + str(cwd),
        "glob": str(cwd) + "*",
        "missing-level": str(cwd),
    }[kind]
    extra = (
        f"[projects.{json.dumps(raw)}]\n"
        if kind == "missing-level"
        else trust_records((raw, "untrusted"))
    )
    (home / "config.toml").write_text(trust_records((root, "trusted")) + extra)
    reads = spy_project_reads(monkeypatch, root)
    with pytest.raises(CapabilityError):
        compile_mcp(home, cwd)
    assert reads == []


def test_trust_interval_deny_does_not_parse_malformed_project(tmp_path, monkeypatch):
    home, cwd = project_configs(
        tmp_path, monkeypatch, "malformed root", "mcp_servers=null"
    )
    (home / "config.toml").write_text(
        trust_records((cwd.parents[1], "trusted"), (cwd, "untrusted"))
    )
    reads = spy_project_reads(monkeypatch, cwd.parents[1])
    with pytest.raises(CapabilityError, match="trust deny"):
        compile_mcp(home, cwd)
    assert reads == []


def test_trust_interval_invoke_deny_produces_failure_without_agent(
    tmp_path, monkeypatch
):
    from dispatcher_for_codex_agents.agent_harness import adapter as adapter_module

    adapter, task = _adapter(tmp_path, "success")
    adapter._cli_version = "codex-cli 0.153.4"
    home, cwd = project_configs(
        tmp_path, monkeypatch, TRUST_MCP_CONFIG, TRUST_MCP_CONFIG
    )
    (home / "config.toml").write_text(
        trust_records((cwd.parents[1], "trusted"), (cwd, "untrusted")) + FAKE_PROVIDER
    )
    monkeypatch.setattr(adapter_module, "temporary_root", lambda: cwd)
    reads = spy_project_reads(monkeypatch, cwd.parents[1])
    result = invoke(adapter, policy_task(task, tools=("mcp:docs:lookup",)), tmp_path)
    assert result.failure_code == FailureCode.POLICY_VIOLATION
    assert reads == []
    assert_prelaunch_failure(adapter, result, tmp_path)


PROFILE_LOAD_ERRORS = [
    pytest.param(b"# SYNTHETIC_CONFIG_SECRET\n\xff", "INVALID_UTF8", id="utf8"),
    pytest.param(b'model = "SYNTHETIC_CONFIG_SECRET', "INVALID_TOML", id="toml"),
    pytest.param(
        b'model = ["SYNTHETIC_CONFIG_SECRET"]', "INVALID_STRUCTURE", id="model-type"
    ),
    pytest.param(
        b'model_provider = {v="SYNTHETIC_CONFIG_SECRET"}',
        "INVALID_STRUCTURE",
        id="provider-type",
    ),
    pytest.param(
        b"profiles = [] # SYNTHETIC_CONFIG_SECRET",
        "INVALID_STRUCTURE",
        id="profiles-type",
    ),
    pytest.param(
        b'[profiles]\nbad="SYNTHETIC_CONFIG_SECRET"',
        "INVALID_STRUCTURE",
        id="profile-entry",
    ),
    pytest.param(
        b"model_providers=[] # SYNTHETIC_CONFIG_SECRET",
        "INVALID_STRUCTURE",
        id="providers-type",
    ),
    pytest.param(
        b'[model_providers]\nbad="SYNTHETIC_CONFIG_SECRET"',
        "INVALID_STRUCTURE",
        id="provider-entry",
    ),
]


def profile_error_fixture(home, location, content):
    name = "config.toml" if location == "base" else "fake-profile.config.toml"
    target = home / name
    target.write_bytes(content)
    return target


def forbid_subprocess(monkeypatch, adapter):
    from dispatcher_for_codex_agents.agent_harness import adapter as adapter_module

    adapter._cli_version = "codex-cli 0.153.4"

    def forbidden(*args, **kwargs):
        pytest.fail("Configuration rejection must not start a subprocess")

    monkeypatch.setattr(adapter_module.subprocess, "Popen", forbidden)


@pytest.mark.parametrize("location", ["base", "sidecar"])
@pytest.mark.parametrize("content,category", PROFILE_LOAD_ERRORS)
def test_profile_load_errors_resolver_preflight_and_reserved_shard(
    tmp_path, monkeypatch, location, content, category
):
    from dispatcher_for_codex_agents.agent_harness.profile import ProfileResolutionError
    from dispatcher_for_codex_agents.agent_harness.shard import ShardExistsError

    adapter, task = _adapter(tmp_path, "success")
    forbid_subprocess(monkeypatch, adapter)
    target = profile_error_fixture(tmp_path / "codex-home", location, content)
    with pytest.raises(ProfileResolutionError) as error:
        adapter._resolver.resolve("fake-profile")
    message = str(error.value)
    assert category in message and target.name in message
    assert ("base config" if location == "base" else "sidecar profile") in message
    assert (
        "SYNTHETIC_CONFIG_SECRET" not in message and str(target.parent) not in message
    )
    with pytest.raises(ProfileResolutionError):
        adapter.preflight(task=policy_task(task, tools=("shell",)), profile=_profile())
    assert not (tmp_path / "run").exists()

    original = adapter._resolver.resolve

    def resolve_after_reservation(profile_id):
        assert _shard(tmp_path, "one").is_dir()
        return original(profile_id)

    monkeypatch.setattr(adapter._resolver, "resolve", resolve_after_reservation)
    result = invoke(adapter, task, tmp_path)
    assert result.failure_code == FailureCode.PROFILE_CONFIGURATION_INVALID
    assert_prelaunch_failure(adapter, result, tmp_path)
    before = {p.name: p.read_bytes() for p in _shard(tmp_path, "one").iterdir()}
    for content in before.values():
        assert b"SYNTHETIC_CONFIG_SECRET" not in content
        assert b"UnicodeDecodeError" not in content and b"Traceback" not in content
    with pytest.raises(ShardExistsError):
        invoke(adapter, task, tmp_path)
    assert before == {p.name: p.read_bytes() for p in _shard(tmp_path, "one").iterdir()}


@pytest.mark.parametrize("location", ["base", "sidecar"])
@pytest.mark.parametrize("content,category", PROFILE_LOAD_ERRORS[:3])
def test_profile_load_errors_cli_dry_run_and_immutable_invoke(
    tmp_path, monkeypatch, capsys, location, content, category
):
    from test_agent_harness_cli import _args, _write_home, _write_task

    from dispatcher_for_codex_agents.agent_harness.adapter import CodexCliAdapter
    from dispatcher_for_codex_agents.agent_harness.cli import main
    from dispatcher_for_codex_agents.agent_harness.shard import ImmutableShardWriter

    home = tmp_path / "cli-home"
    _write_home(home, ("fake-profile",))
    profile_error_fixture(home, location, content)
    task_file = _write_task(tmp_path / "inputs")
    adapter, _ = _adapter(tmp_path, "success")
    forbid_subprocess(monkeypatch, adapter)
    monkeypatch.setattr(
        CodexCliAdapter, "_read_cli_version", lambda self, env: "codex-cli 0.153.4"
    )
    root = tmp_path / "run"
    args = _args(task_file, home, root, "fake-profile", "one")
    assert main(args + ["--dry-run"]) == 2
    dry = capsys.readouterr()
    assert not root.exists()
    assert json.loads(dry.err)["failure_code"] == "PROFILE_CONFIGURATION_INVALID"
    assert category in dry.err and "SYNTHETIC_CONFIG_SECRET" not in dry.err
    assert main(args) == 12
    output = capsys.readouterr()
    assert "Traceback" not in output.err + output.out
    assert "SYNTHETIC_CONFIG_SECRET" not in output.err + output.out
    result = json.loads(output.out)
    assert result["failure_code"] == "PROFILE_CONFIGURATION_INVALID"
    assert result["provenance"]["agent_subprocess_count"] == 0
    shard = root / "workers" / "cli-task" / "fake-profile" / "one"
    assert set(p.name for p in shard.iterdir()) == set(
        ImmutableShardWriter.REQUIRED_FILES
    )
    _verify_hashes(shard)
    before = {p.name: p.read_bytes() for p in shard.iterdir()}
    assert main(args) == 16
    assert before == {p.name: p.read_bytes() for p in shard.iterdir()}


@pytest.mark.parametrize("value", [[], None, "bad root", {1: "bad key"}])
def test_profile_load_errors_unexpected_root_is_controlled(
    tmp_path, monkeypatch, value
):
    from dispatcher_for_codex_agents.agent_harness import profile as profile_module

    adapter, _ = _adapter(tmp_path, "success")
    monkeypatch.setattr(profile_module.tomllib, "load", lambda stream: value)
    with pytest.raises(
        profile_module.ProfileResolutionError, match="INVALID_STRUCTURE"
    ):
        adapter._resolver.resolve("fake-profile")


@pytest.mark.parametrize("control", [KeyboardInterrupt, SystemExit, BaseException])
def test_profile_load_errors_do_not_swallow_control_exceptions(
    tmp_path, monkeypatch, control
):
    from dispatcher_for_codex_agents.agent_harness import profile as profile_module

    adapter, _ = _adapter(tmp_path, "success")

    def stop(stream):
        raise control("synthetic control")

    monkeypatch.setattr(profile_module.tomllib, "load", stop)
    with pytest.raises(control, match="synthetic control"):
        adapter._resolver.resolve("fake-profile")
