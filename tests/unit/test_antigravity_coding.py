"""Synthetic coding admission gate and immutable evidence tests."""

import json
import sys
from pathlib import Path

import pytest
from test_workspace_artifact import workspace  # noqa: F401

from dispatcher_for_codex_agents.agent_harness import antigravity_coding
from dispatcher_for_codex_agents.agent_harness.antigravity_live import (
    OfficialCliApproval,
)
from dispatcher_for_codex_agents.agent_harness.workspace_artifact import digest


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("success", "PASS"),
        ("noop", "FAIL_ARTIFACT"),
        ("tamper", "FAIL_ARTIFACT"),
        ("error", "ARTIFACT_VALID_RUNTIME_ABNORMAL"),
        ("validator_error", "FAIL_ARTIFACT"),
        ("validator_deleted", "FAIL_ARTIFACT"),
        ("whitespace", "FAIL_ARTIFACT"),
    ],
)
def test_one_call_sealed_post_audit_not_final_prose(
    workspace, tmp_path, monkeypatch, mode, expected  # noqa: F811
):
    task, _ = workspace
    fixture = Path(__file__).parents[1] / "fixtures/fake_agy_coding.py"
    exe = tmp_path / "fake-coding"
    fixture_text = f"#!{sys.executable}\n" + fixture.read_text().replace(
        'mode = "success"', f"mode = {mode!r}"
    )
    if mode == "whitespace":
        fixture_text = fixture_text.replace('"value = 1\\n"', '"value = 1\\n\\n"')
    exe.write_text(fixture_text)
    exe.chmod(0o700)
    config = tmp_path / "admission.json"
    config.write_text(
        json.dumps(
            {
                "execution_mode": "workspace_artifact",
                "task_sha256": digest(task.model_dump_json().encode()),
                "customization_baseline": "PASS",
                "state_ownership": "OFFICIAL_CLI",
                "dca_retries": 0,
                "fallback": False,
                "experimental_live": True,
            }
        )
    )
    config.chmod(0o600)
    approval = OfficialCliApproval(
        executable=exe,
        executable_sha256=digest(exe.read_bytes()),
        config_sha256=digest(config.read_bytes()),
    )
    actual_popen = antigravity_coding.subprocess.Popen
    calls = []

    def popen(argv, **kwargs):
        if str(argv[0]) == str(exe):
            assert kwargs["env"]["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"
            calls.append(argv)
        return actual_popen(argv, **kwargs)

    monkeypatch.setattr(antigravity_coding.subprocess, "Popen", popen)
    validator_calls = []
    hidden = tmp_path / "hidden-validator.py"
    hidden.write_text("# parent-held synthetic oracle\n")

    def validators(*args, **kwargs):
        validator_calls.append(True)
        if mode == "validator_error":
            raise OSError("SYNTHETIC_PRIVATE_CONTENT_DO_NOT_ECHO")
        if mode == "validator_deleted":
            hidden.unlink()
        return [{"name": "public", "status": "PASS", "exit_code": 0}]

    monkeypatch.setattr(antigravity_coding, "run_validators", validators)
    attempt = tmp_path / "attempt"
    result = antigravity_coding.invoke_coding_admission(
        task=task,
        approval=approval,
        admission_path=config,
        prompt="synthetic fix",
        attempt=attempt,
        validator_mounts=(hidden,),
    )
    assert result["status"] == expected
    assert result["dca_logical_invocations"] == 1
    assert result["dca_retries"] == 0
    assert result["final_text_authority"] is False
    if mode == "whitespace":
        assert result["artifact_audit"]["failure"] == "WORKSPACE_DIFF_CHECK_FAILED"
        assert result["artifact_audit"]["scope_integrity"] == "PASS"
        assert result["artifact_audit"]["diff_check"] == "FAIL"
        assert result["validators"][0]["status"] == "PASS"
        assert (task.workspace_root / "src/labels.py").read_bytes() == b"value = 1\n\n"
    assert "SYNTHETIC_PRIVATE_CONTENT_DO_NOT_ECHO" not in json.dumps(result)
    if mode == "validator_deleted":
        assert result["validator_support_unchanged"] is False
    assert len(calls) == 2  # no-model --version then exactly one fake invocation
    assert not any(
        a.startswith(("--json-schema", "--agent", "--model", "--continue"))
        for a in calls[1]
    )
    assert bool(validator_calls) is (mode != "tamper")
    manifest = json.loads((attempt / "sha256.json").read_text())
    assert all(digest((attempt / n).read_bytes()) == h for n, h in manifest.items())
    frozen = {p.name: p.read_bytes() for p in attempt.iterdir() if p.is_file()}
    with pytest.raises((ValueError, FileExistsError, OSError)):
        antigravity_coding.invoke_coding_admission(
            task=task,
            approval=approval,
            admission_path=config,
            prompt="no repeat",
            attempt=attempt,
            validator_mounts=(),
        )
    assert {p.name: p.read_bytes() for p in attempt.iterdir() if p.is_file()} == frozen
    assert len([c for c in calls if c[1:] != ["--version"]]) == 1
