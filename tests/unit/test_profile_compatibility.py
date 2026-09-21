"""Isolated standalone discovery, legacy diagnostics and failure containment."""

import hashlib
import json
import os
from pathlib import Path

import pytest
from test_agent_capabilities import config_overrides
from test_agent_harness import _adapter, _profile, _shard
from test_agent_harness_cli import _verify_hashes

from dispatcher_for_codex_agents.agent_harness import FailureCode, ShardExistsError
from dispatcher_for_codex_agents.agent_harness.profile import (
    ProfileResolutionError,
    SidecarProfileResolver,
)
from dispatcher_for_codex_agents.agent_harness.shard import ImmutableShardWriter


def write_home(tmp_path, base, sidecar=None, name="fake-profile"):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(base, encoding="utf-8")
    if sidecar is not None:
        (home / f"{name}.config.toml").write_text(sidecar, encoding="utf-8")
    return home


BASE = '[model_providers.fake]\nname="Test"\nwire_api="responses"\n'
SIDECAR = 'model="fake-model"\nmodel_provider="fake"\n'


@pytest.mark.parametrize(
    "name",
    [
        "volc-glm53-max",
        "volc-glm53-flash-max",
        "scnet-glm53-flash-max",
        "scnet-dsv4-flash-0731-max",
        "deepseek-flash-max",
    ],
)
def test_standalone_route_identity_and_profile_local_values(tmp_path, name):
    home = write_home(
        tmp_path,
        'model="base-not-selected"\nmodel_provider="other"\n'
        'model_reasoning_effort="low"\nmodel_verbosity="low"\n' + BASE,
        SIDECAR + 'model_reasoning_effort="max"\nmodel_verbosity="high"\n',
        name,
    )
    resolved = SidecarProfileResolver(home).resolve(name)
    proof = resolved.provenance("codex-cli 0.154.0")
    assert resolved.profile_id == name
    assert resolved.configured_model == "fake-model"
    assert resolved.configured_provider == "fake"
    assert proof["requested_profile"] == name
    assert proof["profile_layout"] == "standalone"
    assert proof["model_reasoning_effort"] == "max"
    assert proof["model_verbosity"] == "high"
    assert proof["sidecar_basename"] == f"{name}.config.toml"
    assert (
        proof["selected_sidecar_sha256"]
        == hashlib.sha256(resolved.sidecar_path.read_bytes()).hexdigest()
    )
    assert proof["base_provider_identity"] == "config.toml:model_providers.fake"
    assert len(proof["base_provider_safe_sha256"]) == 64
    assert proof["codex_cli_version"] == "codex-cli 0.154.0"
    assert proof["compatibility_warnings"] == []
    assert str(home) not in json.dumps(proof)


@pytest.mark.parametrize(
    "sidecar", ['model="fake-model"\n', 'model_provider="fake"\n', ""]
)
def test_base_defaults_cannot_fill_missing_sidecar_identity(tmp_path, sidecar):
    home = write_home(tmp_path, SIDECAR + BASE, sidecar)
    with pytest.raises(
        ProfileResolutionError, match="SIDECAR_MODEL_AND_PROVIDER_REQUIRED"
    ):
        SidecarProfileResolver(home).resolve("fake-profile")


def test_unset_effort_is_not_reported_as_base_default(tmp_path):
    home = write_home(tmp_path, 'model_reasoning_effort="high"\n' + BASE, SIDECAR)
    record = SidecarProfileResolver(home).resolve("fake-profile").provenance()
    assert record["model_reasoning_effort"] is None
    assert record["model_verbosity"] is None
    assert record["codex_cli_version"] == "NOT_PROBED"


@pytest.mark.parametrize(
    "base,sidecar,layout,code",
    [
        (
            '[profiles.fake-profile]\nmodel="old"\n',
            None,
            "legacy",
            "LEGACY_MIGRATION_REQUIRED",
        ),
        (
            '[profiles.fake-profile]\nmodel="old"\n',
            SIDECAR,
            "conflict",
            "PROFILE_LAYOUT_CONFLICT",
        ),
        (
            '[profiles.fake-profile]\nmodel="old"\n',
            "broken [",
            "conflict",
            "PROFILE_LAYOUT_CONFLICT",
        ),
        (BASE, None, "missing", "MISSING"),
        (
            'profile="fake-profile"\n' + BASE,
            SIDECAR,
            "standalone",
            "MIGRATION_REQUIRED",
        ),
        (
            BASE + '[profiles.unselected]\nmodel="old"\n',
            SIDECAR,
            "standalone",
            "MIGRATION_REQUIRED",
        ),
        ("", SIDECAR, "standalone", "BASE_PROVIDER_DEFINITION_MISSING"),
        (
            '[model_providers.other]\nname="Other"\n',
            SIDECAR,
            "standalone",
            "BASE_PROVIDER_DEFINITION_MISSING",
        ),
        ("model_providers=[]", SIDECAR, "standalone", "INVALID_STRUCTURE"),
        (
            '[model_providers]\nfake="invalid"',
            SIDECAR,
            "standalone",
            "INVALID_STRUCTURE",
        ),
        (
            "[model_providers.fake]\nwire_api=[]",
            SIDECAR,
            "standalone",
            "INVALID_STRUCTURE",
        ),
        (
            BASE,
            SIDECAR + '[model_providers.fake]\nname="Sidecar"\n',
            "conflict",
            "SIDECAR_AUTHORITY_CONFLICT",
        ),
        (BASE, SIDECAR + 'profile="other"\n', "conflict", "SIDECAR_AUTHORITY_CONFLICT"),
    ],
)
def test_layout_errors_are_structured(tmp_path, base, sidecar, layout, code):
    home = write_home(tmp_path, base, sidecar)
    with pytest.raises(ProfileResolutionError, match=code) as result:
        SidecarProfileResolver(home).resolve("fake-profile")
    assert result.value.compatibility["profile_layout"] == layout
    assert str(home) not in str(result.value)


@pytest.mark.parametrize(
    "name", ["../escape", "/absolute", "nested/name", "foo.config.toml", "", ".", ".."]
)
def test_ambiguous_selector_rejected_before_read(tmp_path, monkeypatch, name):
    def forbidden(*args, **kwargs):
        pytest.fail("Ambiguous selector reached config loading")

    monkeypatch.setattr(SidecarProfileResolver, "_load_toml", forbidden)
    with pytest.raises(ProfileResolutionError, match="AMBIGUOUS_PROFILE_IDENTIFIER"):
        SidecarProfileResolver(tmp_path).resolve(name)


@pytest.mark.parametrize("target", ["config.toml", "fake-profile.config.toml"])
@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_config_aliases_fail_closed(tmp_path, target, kind):
    home = write_home(tmp_path, BASE, SIDECAR)
    path = home / target
    original = home / "original"
    path.rename(original)
    if kind == "symlink":
        path.symlink_to(original)
    else:
        os.link(original, path)
    with pytest.raises(ProfileResolutionError, match="AMBIGUOUS_CONFIG_PATH"):
        SidecarProfileResolver(home).resolve("fake-profile")


def test_safe_provider_fingerprint_excludes_secrets_and_environment(
    tmp_path, monkeypatch
):
    home = write_home(tmp_path, BASE, SIDECAR)
    resolver = SidecarProfileResolver(home)

    class NoEnvironmentValues(dict):
        def get(self, *args, **kwargs):
            pytest.fail("Resolver read environment values")

        def __getitem__(self, key):
            pytest.fail("Resolver read environment values")

    monkeypatch.setattr(os, "environ", NoEnvironmentValues())
    fingerprints = []
    for sentinel in ("SYNTHETIC_ONE", "SYNTHETIC_TWO"):
        (home / "config.toml").write_text(
            BASE + f'base_url="https://example.invalid/v1?token={sentinel}"\n'
            f'env_key="{sentinel}"\nexperimental_bearer_token="{sentinel}"\n'
            f'[model_providers.fake.http_headers]\nAuthorization="{sentinel}"\n'
            f'[model_providers.fake.env_http_headers]\nPrivate="{sentinel}"\n'
        )
        proof = resolver.resolve("fake-profile").provenance()
        assert sentinel not in json.dumps(proof)
        fingerprints.append(proof["base_provider_safe_sha256"])
    assert fingerprints[0] == fingerprints[1]
    (home / "config.toml").write_text(BASE + 'base_url="https://other.invalid/v1"\n')
    assert (
        resolver.resolve("fake-profile").provenance()["base_provider_safe_sha256"]
        != fingerprints[0]
    )


@pytest.mark.parametrize("target", ["config.toml", "fake-profile.config.toml"])
@pytest.mark.parametrize(
    "content", [b"# SYNTHETIC_SECRET\n\xff", b'model="unterminated', b"model=[]"]
)
def test_decode_errors_reserved_failure_is_complete_and_immutable(
    tmp_path, monkeypatch, target, content
):
    adapter, task = _adapter(tmp_path, "success")
    adapter._cli_version = "codex-cli 0.154.0"
    (tmp_path / "codex-home" / target).write_bytes(content)

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid config must not launch an agent")

    monkeypatch.setattr("subprocess.Popen", forbidden)
    with pytest.raises(ProfileResolutionError):
        adapter.preflight(task=task, profile=_profile())
    assert not (tmp_path / "run").exists()
    original = adapter._resolver.resolve

    def after_reservation(name):
        assert _shard(tmp_path, "one").is_dir()
        return original(name)

    monkeypatch.setattr(adapter._resolver, "resolve", after_reservation)
    arguments = dict(
        task=task, profile=_profile(), attempt_id="one", workers_root=tmp_path / "run"
    )
    result = adapter.invoke(**arguments)
    assert result.failure_code == FailureCode.PROFILE_CONFIGURATION_INVALID
    assert result.provenance["agent_subprocess_count"] == adapter.calls_started == 0
    assert (
        result.provenance["profile_compatibility"]["codex_cli_version"]
        == "codex-cli 0.154.0"
    )
    shard = _shard(tmp_path, "one")
    assert set(p.name for p in shard.iterdir()) == set(
        ImmutableShardWriter.REQUIRED_FILES
    )
    _verify_hashes(shard)
    before = {p.name: p.read_bytes() for p in shard.iterdir()}
    assert all(
        b"SYNTHETIC_SECRET" not in value and b"Traceback" not in value
        for value in before.values()
    )
    with pytest.raises(ShardExistsError):
        adapter.invoke(**arguments)
    assert before == {p.name: p.read_bytes() for p in shard.iterdir()}


def test_profile_fields_do_not_grant_task_capabilities(tmp_path):
    adapter, task = _adapter(tmp_path, "success")
    home = tmp_path / "codex-home"
    (home / "fake-profile.config.toml").write_text(
        SIDECAR + 'web_search="live"\n[features]\nshell_tool=true\nmulti_agent=true\n'
    )
    preview = adapter.preflight(task=task, profile=_profile())
    assert preview["capability_policy"]["requested"] == {
        "read_paths": [],
        "write_paths": [],
        "tools": [],
    }
    assert config_overrides(preview["command"])["web_search"] == "disabled"
    assert "--enable" not in preview["command"]
    result = adapter.invoke(
        task=task, profile=_profile(), attempt_id="one", workers_root=tmp_path / "run"
    )
    assert result.status == "success"
    proof = result.provenance["profile_compatibility"]
    assert proof["profile_layout"] == "standalone"
    assert proof["codex_cli_version"] == result.provenance["codex_cli_version"]
    assert proof["codex_cli_version"] != "NOT_PROBED"
    assert proof["codex_cli_version"] == "fake-codex 0.153.4"
    _verify_hashes(_shard(tmp_path, "one"))


def test_legacy_selector_without_sidecar_is_not_reported_missing(tmp_path):
    home = write_home(tmp_path, 'profile="fake-profile"\n')
    with pytest.raises(ProfileResolutionError, match="MIGRATION_REQUIRED") as error:
        SidecarProfileResolver(home).resolve("fake-profile")
    assert error.value.compatibility["profile_layout"] == "legacy"


@pytest.mark.parametrize(
    "base,sidecar,layout",
    [
        ('[profiles.fake-profile]\nmodel="legacy"\n', None, "legacy"),
        ('[profiles.fake-profile]\nmodel="legacy"\n', SIDECAR, "conflict"),
        ('[model_providers.other]\nname="Other"\n', SIDECAR, "standalone"),
        (BASE, None, "missing"),
        ("[model_providers.fake]\nbase_url=123\n", SIDECAR, "standalone"),
        (BASE, SIDECAR + "model_reasoning_effort=[]\n", "standalone"),
    ],
)
def test_compatibility_rejection_cli_dry_run_and_reserved_shard(
    tmp_path, monkeypatch, capsys, base, sidecar, layout
):
    from test_agent_harness_cli import _args, _write_task

    from dispatcher_for_codex_agents.agent_harness import CodexCliAdapter
    from dispatcher_for_codex_agents.agent_harness.cli import main

    home = write_home(tmp_path, base, sidecar)
    task = _write_task(tmp_path / "inputs")

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid profile started a subprocess")

    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr(
        CodexCliAdapter, "_read_cli_version", lambda self, env: "fake-version"
    )
    root = tmp_path / "run"
    args = _args(task, home, root, "fake-profile", "one")
    assert main(args + ["--dry-run"]) == 2
    dry = json.loads(capsys.readouterr().err)
    assert dry["profile_compatibility"]["profile_layout"] == layout
    assert not root.exists()
    assert main(args) == 12
    result = json.loads(capsys.readouterr().out)
    assert result["failure_code"] == "PROFILE_CONFIGURATION_INVALID"
    assert result["provenance"]["agent_subprocess_count"] == 0
    assert result["provenance"]["profile_compatibility"]["profile_layout"] == layout
    shard = root / "workers" / "cli-task" / "fake-profile" / "one"
    assert set(p.name for p in shard.iterdir()) == set(
        ImmutableShardWriter.REQUIRED_FILES
    )
    _verify_hashes(shard)
    before = {p.name: p.read_bytes() for p in shard.iterdir()}
    assert main(args) == 16
    capsys.readouterr()
    assert before == {p.name: p.read_bytes() for p in shard.iterdir()}


@pytest.mark.parametrize(
    "configured",
    [
        {},
        {"model_reasoning_effort": "max"},
        {"model_verbosity": "medium"},
        {"model_reasoning_effort": "provider-custom", "model_verbosity": "low"},
    ],
)
def test_optional_overrides_are_not_required_or_backfilled(tmp_path, configured):
    home = write_home(
        tmp_path,
        'model_reasoning_effort="high"\nmodel_verbosity="high"\n' + BASE,
        SIDECAR
        + "".join(f"{key}={json.dumps(value)}\n" for key, value in configured.items()),
    )
    proof = SidecarProfileResolver(home).resolve("fake-profile").provenance()
    for key in ("model_reasoning_effort", "model_verbosity"):
        assert proof[key] == configured.get(key)


@pytest.mark.parametrize("key", ["model_reasoning_effort", "model_verbosity"])
@pytest.mark.parametrize(
    "value", ["", " ", " high", "high ", "hi\ngh", "hi\x00gh", 1, True, [], {}]
)
def test_invalid_optional_overrides_are_controlled(tmp_path, key, value):
    home = write_home(tmp_path, BASE, SIDECAR + f"{key}={json.dumps(value)}\n")
    with pytest.raises(ProfileResolutionError, match="INVALID_STRUCTURE"):
        SidecarProfileResolver(home).resolve("fake-profile")


def test_verbosity_unsupported_value_is_controlled(tmp_path):
    home = write_home(
        tmp_path, BASE, SIDECAR + 'model_verbosity="SYNTHETIC_INVALID_VALUE"\n'
    )
    with pytest.raises(ProfileResolutionError, match="INVALID_STRUCTURE") as result:
        SidecarProfileResolver(home).resolve("fake-profile")
    assert "SYNTHETIC" not in str(result.value)


@pytest.mark.parametrize("key", ["model", "model_provider"])
@pytest.mark.parametrize("value", [None, "", 2, [], {}])
def test_required_identity_fields_remain_required(tmp_path, key, value):
    values = {"model": "fake-model", "model_provider": "fake"}
    if value is None:
        del values[key]
    else:
        values[key] = value
    sidecar = "".join(f"{key}={json.dumps(value)}\n" for key, value in values.items())
    home = write_home(tmp_path, SIDECAR + BASE, sidecar)
    with pytest.raises(ProfileResolutionError):
        SidecarProfileResolver(home).resolve("fake-profile")


def inject_config_io_error(monkeypatch, target, operation, error_type):
    """Fail only a fake config path; preserve shard/input filesystem operations."""
    state = {"hits": 0, "successful_stats": 0}
    original_stat = Path.stat

    def observed_stat(path, *args, **kwargs):
        result = original_stat(path, *args, **kwargs)
        if path == target:
            state["successful_stats"] += 1
        return result

    monkeypatch.setattr(Path, "stat", observed_stat)
    if operation == "exists":
        original_is_file = Path.is_file
        # Base exists() is reached only when is_file() is false.
        monkeypatch.setattr(
            Path,
            "is_file",
            lambda path: False if path == target else original_is_file(path),
        )
    if operation == "hardlink_stat":
        # Reach the explicit nlink stat, past discovery/type/link predicates.
        for name, value in (("exists", True), ("is_file", True), ("is_symlink", False)):
            original = getattr(Path, name)

            def fixed(path, _original=original, _value=value):
                return _value if path == target else _original(path)

            monkeypatch.setattr(Path, name, fixed)
        operation = "stat"
    original_operation = getattr(Path, operation)

    def fail(path, *args, **kwargs):
        if path == target:
            state["hits"] += 1
            errno = 13 if error_type is PermissionError else 5
            raise error_type(errno, "SYNTHETIC_IO_SECRET", str(target))
        return original_operation(path, *args, **kwargs)

    monkeypatch.setattr(Path, operation, fail)
    return state


@pytest.mark.parametrize("filename", ["config.toml", "fake-profile.config.toml"])
@pytest.mark.parametrize(
    "operation",
    ["exists", "stat", "is_file", "is_symlink", "hardlink_stat", "read_bytes"],
)
@pytest.mark.parametrize("error_type", [PermissionError, OSError])
def test_discovery_and_load_io_failure_after_reservation(
    tmp_path, monkeypatch, filename, operation, error_type
):
    adapter, task = _adapter(tmp_path, "success")
    adapter._cli_version = "offline-version"
    state = inject_config_io_error(
        monkeypatch, tmp_path / "codex-home" / filename, operation, error_type
    )

    def forbidden(*args, **kwargs):
        pytest.fail("Profile I/O failure must not start any subprocess")

    monkeypatch.setattr("subprocess.Popen", forbidden)
    with pytest.raises(ProfileResolutionError, match="IO_ERROR") as error:
        adapter.preflight(task=task, profile=_profile())
    assert filename in str(error.value)
    assert "SYNTHETIC" not in str(error.value) and str(tmp_path) not in str(error.value)
    assert not (tmp_path / "run").exists()
    original = adapter._resolver.resolve

    def after_reservation(name):
        assert _shard(tmp_path, "one").is_dir()
        return original(name)

    monkeypatch.setattr(adapter._resolver, "resolve", after_reservation)
    arguments = dict(
        task=task, profile=_profile(), attempt_id="one", workers_root=tmp_path / "run"
    )
    result = adapter.invoke(**arguments)
    assert result.failure_code == FailureCode.PROFILE_CONFIGURATION_INVALID
    assert result.provenance["agent_subprocess_count"] == adapter.calls_started == 0
    shard = _shard(tmp_path, "one")
    assert set(p.name for p in shard.iterdir()) == set(
        ImmutableShardWriter.REQUIRED_FILES
    )
    _verify_hashes(shard)
    before = {p.name: p.read_bytes() for p in shard.iterdir()}
    for content in before.values():
        for forbidden_text in (
            b"SYNTHETIC_IO_SECRET",
            b"PermissionError",
            b"Traceback",
            str(tmp_path).encode(),
        ):
            assert forbidden_text not in content
    with pytest.raises(ShardExistsError):
        adapter.invoke(**arguments)
    assert before == {p.name: p.read_bytes() for p in shard.iterdir()}
    assert state["hits"] >= 2
    if operation == "read_bytes":
        assert state["successful_stats"] > 0


@pytest.mark.parametrize(
    "filename,operation",
    [
        ("fake-profile.config.toml", "exists"),
        ("fake-profile.config.toml", "stat"),
        ("fake-profile.config.toml", "read_bytes"),
        ("config.toml", "stat"),
        ("config.toml", "read_bytes"),
    ],
)
def test_cli_io_failure_dry_run_and_immutable_invoke(
    tmp_path, monkeypatch, capsys, filename, operation
):
    from test_agent_harness_cli import _args, _write_home, _write_task

    from dispatcher_for_codex_agents.agent_harness import CodexCliAdapter
    from dispatcher_for_codex_agents.agent_harness.cli import main

    home = tmp_path / "cli-home"
    _write_home(home, ("fake-profile",))
    task = _write_task(tmp_path / "inputs")
    inject_config_io_error(monkeypatch, home / filename, operation, PermissionError)

    def forbidden(*args, **kwargs):
        pytest.fail("No subprocess allowed")

    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr(
        CodexCliAdapter, "_read_cli_version", lambda self, env: "offline-version"
    )
    root = tmp_path / "run"
    args = _args(task, home, root, "fake-profile", "one")
    assert main(args + ["--dry-run"]) == 2
    dry = capsys.readouterr()
    assert json.loads(dry.err)["failure_code"] == "PROFILE_CONFIGURATION_INVALID"
    assert not root.exists()
    assert main(args) == 12
    actual = capsys.readouterr()
    result = json.loads(actual.out)
    assert result["failure_code"] == "PROFILE_CONFIGURATION_INVALID"
    assert result["provenance"]["agent_subprocess_count"] == 0
    assert "Traceback" not in actual.err + actual.out + dry.err
    assert "SYNTHETIC_IO_SECRET" not in actual.err + actual.out + dry.err
    shard = root / "workers" / "cli-task" / "fake-profile" / "one"
    assert set(p.name for p in shard.iterdir()) == set(
        ImmutableShardWriter.REQUIRED_FILES
    )
    _verify_hashes(shard)
    before = {p.name: p.read_bytes() for p in shard.iterdir()}
    assert main(args) == 16
    capsys.readouterr()
    assert before == {p.name: p.read_bytes() for p in shard.iterdir()}


@pytest.mark.parametrize(
    "error_type", [PermissionError, OSError, ValueError, RuntimeError]
)
def test_home_canonicalization_failures_are_controlled(
    tmp_path, monkeypatch, error_type
):
    home = tmp_path / "codex-home"
    original = Path.resolve

    def fail(path, *args, **kwargs):
        if path == home:
            raise error_type("SYNTHETIC_PATH_SECRET")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fail)
    with pytest.raises(ProfileResolutionError) as error:
        SidecarProfileResolver(home)
    assert "CODEX_HOME" in str(error.value)
    assert "SYNTHETIC" not in str(error.value) and str(home) not in str(error.value)


@pytest.mark.parametrize("control", [KeyboardInterrupt, SystemExit, BaseException])
@pytest.mark.parametrize("stage", ["canonicalization", "discovery"])
def test_control_exceptions_are_not_normalized(tmp_path, monkeypatch, control, stage):
    home = tmp_path / "home"
    resolver = SidecarProfileResolver(home)
    target = home if stage == "canonicalization" else home / "fake-profile.config.toml"
    method = "resolve" if stage == "canonicalization" else "exists"
    original = getattr(Path, method)

    def stop(path, *args, **kwargs):
        if path == target:
            raise control("synthetic control")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, method, stop)
    with pytest.raises(control, match="synthetic control"):
        if stage == "canonicalization":
            SidecarProfileResolver(home)
        else:
            resolver.resolve("fake-profile")
