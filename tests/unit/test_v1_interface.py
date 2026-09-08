"""Public naming and safe external-provider workflow regressions."""

import importlib.metadata
import importlib.util
import json
import sys
import tomllib
from pathlib import Path

import pytest

from dispatcher_for_codex_agents.agent_harness import AgentTask
from dispatcher_for_codex_agents.agent_harness.cli import build_parser
from dispatcher_for_codex_agents.agent_harness.payload import PayloadBuilder
from dispatcher_for_codex_agents.agent_harness.runtime import default_registry
from dispatcher_for_codex_agents.main_agent_bridge.contracts import JobSpec
from dispatcher_for_codex_agents.notifications.cli import default_config

ROOT = Path(__file__).resolve().parents[2]


def script(name):
    spec = importlib.util.spec_from_file_location("test_example", ROOT / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def workspace(tmp_path, monkeypatch):
    root = tmp_path / "safe checkout"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='test-workspace'\n")
    script("scripts/init-workspace.py").initialize(root)
    (root / ".git").mkdir()
    monkeypatch.setenv("DCA_WORKSPACE_CONFIG", str(root / "configs/workspace.json"))
    return root


def test_public_names_and_no_legacy_runtime_api(tmp_path):
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert set(project["scripts"]) == {"dca", "dca-notify"}
    assert project["name"] == "dispatcher-for-codex-agents"
    assert project["scripts"]["dca"] == (
        "dispatcher_for_codex_agents.agent_harness.cli:main"
    )
    assert project["version"] == "1.0.0"
    source = tmp_path / "records.tsv"
    source.write_text("record_id\tvalue\nSYN-001\t7\n")
    task = AgentTask(
        task_id="synthetic",
        role="external-agent",
        prompt_template="Return JSON.",
        approved_input_files=(str(source),),
        selected_columns=("record_id", "value"),
        timeout=10.0,
        call_limit=1,
        expected_output_schema={"type": "object", "additionalProperties": False},
    )
    payload = PayloadBuilder().build(task).content
    assert payload.startswith("DCA_AGENT_TASK_V1\n")
    assert payload.endswith("DCA_AGENT_TASK_END\n")
    # These retired strings occur only in negative tests, never as accepted aliases.
    for old in ("B2M_NOTIFY_", "b2m-notify", "B2M_REVIEWER_TASK", "ReviewerTask"):
        assert old not in payload
        for path in (ROOT / "src").rglob("*.py"):
            assert old not in path.read_text()
    assert "agent_executable" in JobSpec.model_fields
    assert "reviewer_executable" not in JobSpec.model_fields


def test_installed_identity_without_old_public_aliases():
    distribution = importlib.metadata.distribution("dispatcher-for-codex-agents")
    assert distribution.version == "1.0.0"
    assert {entry.name for entry in distribution.entry_points} == {"dca", "dca-notify"}
    assert build_parser().prog == "dca"
    assert "DCA — Dispatcher for Codex Agents" in build_parser().format_help()
    assert (Path(sys.prefix) / "bin/dca").is_file()
    assert not (Path(sys.prefix) / "bin/denovo-codex-agent-tool").exists()
    assert not (Path(sys.prefix) / "bin/dispatcher-for-codex-agents").exists()
    assert importlib.util.find_spec("denovo_codex_agent_tool") is None
    with pytest.raises(importlib.metadata.PackageNotFoundError):
        importlib.metadata.distribution("denovo-codex-agent-tool")


def test_controller_tools_use_only_current_names():
    from dispatcher_for_codex_agents.main_agent_bridge.controller import TOOLS

    assert {item["name"] for item in TOOLS} == {
        "dca_start_approved_jobs",
        "dca_read_verified_results",
    }


def test_notification_new_env_only(monkeypatch):
    monkeypatch.setenv("DCA_NOTIFY_CONFIG", "/explicit/new-notifications.json")
    monkeypatch.setenv("B2M_NOTIFY_CONFIG", "/obsolete/must-not-be-used.json")
    assert default_config() == "/explicit/new-notifications.json"


def test_batch_help_and_neutral_contract(capsys):
    with pytest.raises(SystemExit) as result:
        build_parser().parse_args(["batch", "plan", "--help"])
    assert result.value.code == 0
    text = capsys.readouterr().out
    assert "--record-id-column" in text
    assert "--article-id-column" not in text
    schema = json.loads((ROOT / "examples/schema.json").read_text())
    assert set(schema["properties"]) == {"results"}
    assert "record_id" in schema["properties"]["results"]["items"]["required"]


@pytest.mark.parametrize("dry_run", [True, False])
def test_cross_provider_demo_is_offline_and_immutable(tmp_path, monkeypatch, dry_run):
    root = workspace(tmp_path, monkeypatch)
    demo = script("examples/cross_provider_demo.py")
    destination = root / "runs/cross-provider"
    result = demo.run_demo(destination, dry_run=dry_run)
    assert result["real_model_calls"] == result["notification_requests"] == 0
    assert result["test_only"] and result["models_and_controller_are_simulated"]
    assert len({row["provider"] for row in result["routes"]}) == 2
    if dry_run:
        assert result["status"] == "DRY_RUN_VALIDATED"
        assert not (destination / "test-agent-calls.log").exists()
        assert not (destination / "controller-state").exists()
    else:
        assert result["status"] == "PASS"
        assert result["virtual_agent_invocations"] == 4
        assert result["exact_once_coverage"] == "PASS"
        assert result["controller_continuation"]["confirmation"]["confirmed"]
        assert result["controller_continuation"]["continuation_calls"] == 1
        assert result["logical_controller_turns"] == 2
        assert result["wait_logical_model_requests"] == 0
        assert all(
            p["provider_reported_served_model"] == "NOT_REPORTED"
            for p in result["agent_provenance"]
        )
    before = (destination / "demo_result.json").read_bytes()
    with pytest.raises(ValueError, match="ALREADY_EXISTS"):
        demo.run_demo(destination, dry_run=dry_run)
    assert (destination / "demo_result.json").read_bytes() == before


@pytest.mark.parametrize(
    "adapter_id", ["zcode_agent", "deepseek_harness", "deepcode_cli"]
)
def test_reserved_adapters_still_fail_closed(adapter_id):
    with pytest.raises(ValueError, match="NOT_IMPLEMENTED"):
        default_registry().require(adapter_id)


def test_no_brand_branch_in_core():
    import ast

    for path in (ROOT / "src/dispatcher_for_codex_agents/agent_harness").glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.If, ast.IfExp, ast.While)):
                for part in ast.walk(node.test):
                    if isinstance(part, ast.Constant) and isinstance(part.value, str):
                        assert not any(
                            brand in part.value.casefold()
                            for brand in ("glm", "deepseek", "kimi")
                        )
