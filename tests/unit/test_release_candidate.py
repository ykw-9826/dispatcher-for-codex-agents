"""RC removal, terminology and model-free preflight gates."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from dispatcher_for_codex_agents import agent_harness
from dispatcher_for_codex_agents.agent_harness.adapter import _parse_event_stream
from dispatcher_for_codex_agents.agent_harness.cli import build_parser, main
from dispatcher_for_codex_agents.notifications.core import NotificationEvent

ROOT = Path(__file__).resolve().parents[2]


def test_metadata_warning_is_not_served_identity_or_semantic_approval():
    events = [
        {"type": "thread.started", "model": "configured-example"},
        {
            "type": "item.completed",
            "item": {
                "type": "error",
                "message": "Model metadata missing. Defaulting to fallback metadata.",
            },
        },
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": '{"answer":"incorrect"}'},
        },
        {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
    ]
    parsed = _parse_event_stream("\n".join(json.dumps(event) for event in events))
    assert parsed.valid_jsonl and parsed.terminal_is_last
    assert parsed.turn_completed_count == 1
    assert not parsed.served_models and not parsed.policy_violations
    assert json.loads(parsed.final_text) == {"answer": "incorrect"}


def test_domain_helpers_are_removed_not_aliased():
    assert importlib.util.find_spec(agent_harness.__name__ + ".acceptance") is None
    assert not any("acceptance" in name.casefold() for name in agent_harness.__all__)
    whitelist = (ROOT / "configs/public_files.txt").read_text().splitlines()
    assert not any("harness/acceptance.py" in name for name in whitelist)
    for path in (ROOT / "src").rglob("*.py"):
        for domain_token in (
            "INCLUDE_P0",
            "screening_readiness",
            "fulltext_recommended",
        ):
            assert domain_token not in path.read_text()


def test_external_agent_help_and_native_protocol_names(capsys):
    with pytest.raises(SystemExit) as result:
        build_parser().parse_args(["--help"])
    assert result.value.code == 0
    text = capsys.readouterr().out
    assert "external agent" in text
    assert "reviewer" not in text.casefold()
    doc = (ROOT / "docs/native_capabilities.md").read_text()
    assert "Follow-up after successful native child" in doc
    assert "successful external child" not in doc
    event = NotificationEvent(
        source="test", kind="turn_completed", status="COMPLETED", task_id="rc-test"
    )
    assert event.payload()["result_approval_claimed"] is False
    assert "scientific_success_claimed" not in event.payload()


def test_two_profile_dry_run_never_launches_a_process(tmp_path, monkeypatch, capsys):
    source = tmp_path / "values.tsv"
    source.write_text("record_id\tleft\tright\nSYN-001\t17\t25\n")
    task = agent_harness.AgentTask(
        task_id="rc-preflight",
        role="external-agent-smoke",
        prompt_template="Return the sum as JSON; no tools.",
        approved_input_files=(str(source),),
        selected_columns=("record_id", "left", "right"),
        timeout=180.0,
        call_limit=1,
        expected_output_schema={
            "type": "object",
            "properties": {"sum": {"type": "integer"}},
            "required": ["sum"],
            "additionalProperties": False,
        },
    )
    task_path = tmp_path / "task.json"
    task_path.write_text(task.model_dump_json())
    home = tmp_path / "host"
    home.mkdir()
    for name in ("alpha", "beta"):
        (home / f"{name}.config.toml").write_text(
            f'model="test-{name}"\nmodel_provider="external-{name}"\n'
        )

    def forbidden(*args, **kwargs):
        raise AssertionError("DRY_RUN_MUST_NOT_START_ANY_SUBPROCESS")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    for name in ("alpha", "beta"):
        code = main(
            [
                "invoke",
                "--task",
                str(task_path),
                "--profile",
                name,
                "--attempt-id",
                "once",
                "--shard-root",
                str(tmp_path / "results"),
                "--runtime-home",
                str(home),
                "--executable",
                sys.executable,
                "--dry-run",
            ]
        )
        report = json.loads(capsys.readouterr().out)
        assert code == 0 and report["status"] == "DRY_RUN_VALIDATED"
        assert report["agent_process_started"] is False
        assert report["configured_provider"] == f"external-{name}"
    assert not (tmp_path / "results").exists()


def test_public_demo_uses_neutral_records_and_no_live_switch():
    header = (ROOT / "examples/records.tsv").read_text().splitlines()[0].split("\t")
    assert "record_id" in header and "description" in header
    assert "abstract" not in header
    demo = (ROOT / "examples/cross_provider_demo.py").read_text()
    assert 'add_argument("--live"' not in demo
    assert '"models_and_controller_are_simulated": True' in demo
