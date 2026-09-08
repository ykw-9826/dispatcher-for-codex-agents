"""Independent runtime, recovery and monitoring tests; no model requests."""

from __future__ import annotations

import csv
import json
import subprocess
from pathlib import Path
from threading import Event, Timer

import pytest
from test_agent_harness import _adapter, _profile
from test_agent_harness_batch import _inputs, _read_tsv

from dispatcher_for_codex_agents.agent_harness.batch import (
    collect_batch,
    plan_batch,
    run_batch,
    status_batch,
)
from dispatcher_for_codex_agents.agent_harness.contracts import InvocationResult
from dispatcher_for_codex_agents.agent_harness.monitoring import (
    aggregate_status,
    check_interval,
    wait_for_check,
)
from dispatcher_for_codex_agents.agent_harness.process_guard import (
    execution_lock,
    process_identity,
)
from dispatcher_for_codex_agents.agent_harness.runtime import (
    RESERVED_ADAPTER_IDS,
    AdapterRegistry,
    AdapterSettings,
    default_registry,
)
from dispatcher_for_codex_agents.agent_harness.schema import validate_json_schema
from dispatcher_for_codex_agents.agent_harness.shard import ImmutableShardWriter


class FakeAlternateAdapter:
    adapter_id = "fake_alternate"

    def __init__(self, settings):
        self.settings = settings

    def preflight(self, *, task, profile):
        return {"runtime": "alternate", "profile_id": profile.profile_id}

    def invoke(self, *, task, profile, attempt_id, workers_root, payload_builder=None):
        payload = payload_builder.build(task)
        results = []
        for source in task.approved_input_files:
            with Path(source).open() as handle:
                results.extend(
                    {
                        "record_id": row["record_id"],
                        "decision": "TYPE_A",
                        "confidence": 0.5,
                    }
                    for row in csv.DictReader(handle, delimiter="\t")
                )
        output = {"results": results}
        validate_json_schema(output, task.expected_output_schema)
        result = InvocationResult(
            status="success",
            exit_code=0,
            turn_completed=True,
            final_output=output,
            schema_validation_status="PASS",
            usage={},
            provenance={"adapter_id": self.adapter_id, "model_calls": 0},
            warnings=(),
            failure_code=None,
            latency_seconds=0,
        )
        writer = ImmutableShardWriter(
            workers_root=workers_root,
            task_id=task.task_id,
            profile_id=profile.profile_id,
            attempt_id=attempt_id,
        )
        writer.write(
            task=task,
            profile=profile,
            input_records=payload.input_records,
            events_jsonl='{"alternate_runtime_event":"finished"}\n',
            stderr_log="",
            final_output=output,
            result=result,
        )
        if self.settings.environment and self.settings.environment.get(
            "CANCEL_AFTER_ONE"
        ):
            self.settings.cancellation.set()
        return result


def alternate_plan(tmp_path):
    inputs = _inputs(tmp_path / "inputs", records=5, profiles=3)
    roles = json.loads(inputs["profiles"].read_text())
    for row in roles["profiles"]:
        row["adapter_id"] = "fake_alternate"
    inputs["profiles"].write_text(json.dumps(roles))
    registry = AdapterRegistry()
    registry.register("fake_alternate", FakeAlternateAdapter)
    root = tmp_path / "run"
    plan_batch(
        source_tsv=inputs["source"],
        batch_id="BATCH-A",
        record_id_column="record_id",
        selected_columns=("record_id", "title"),
        profile_role_config=inputs["profiles"],
        shard_size=2,
        prompt_template=inputs["prompt"],
        expected_output_schema=inputs["schema"],
        output_root=root,
        adapter_registry=registry,
    )
    return root, registry


def test_alternate_runtime_full_cycle_without_codex(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No subprocess or model may be called")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    root, registry = alternate_plan(tmp_path)
    dry = run_batch(
        plan_root=root,
        run_id="dry",
        dry_run=True,
        adapter_registry=registry,
        executable="does-not-exist",
    )
    assert dry["scheduled_invocations"] == 9
    first = run_batch(
        plan_root=root, run_id="one", adapter_registry=registry, max_workers=2
    )
    assert first["executed_count"] == 9
    assert status_batch(plan_root=root, snapshot_id="s1")["status_counts"] == {
        "SUCCESS": 9
    }
    assert collect_batch(plan_root=root, collection_id="c1")["status"] == "PASS"
    directory = root / "collections/c1"
    assert len(_read_tsv(directory / "authoritative_results.tsv")) == 5
    assert len(_read_tsv(directory / "shadow_results.tsv")) == 5
    assert len(_read_tsv(directory / "diagnostic_results.tsv")) == 5
    again = run_batch(plan_root=root, run_id="two", adapter_registry=registry)
    assert again["executed_count"] == 0
    assert again["skipped_existing_success"] == 9
    # A library host still alive after run_batch is not a running batch.
    assert aggregate_status(root)["terminal"] is True


@pytest.mark.parametrize("adapter_id", sorted(RESERVED_ADAPTER_IDS) + ["unknown"])
def test_unimplemented_adapters_fail_closed(adapter_id):
    with pytest.raises(ValueError, match="NOT_IMPLEMENTED"):
        default_registry().create(adapter_id, AdapterSettings())


def test_cancel_stops_new_shards_and_continues_only_unstarted(tmp_path):
    root, registry = alternate_plan(tmp_path)
    first = run_batch(
        plan_root=root,
        run_id="cancel",
        adapter_registry=registry,
        environment={"CANCEL_AFTER_ONE": "1"},
    )
    assert first["executed_count"] == 1
    assert first["terminal_status_counts"] == {"PLANNED": 8, "SUCCESS": 1}
    second = run_batch(plan_root=root, run_id="continue", adapter_registry=registry)
    assert second["executed_count"] == 8
    assert second["skipped_existing_success"] == 1
    assert collect_batch(plan_root=root, collection_id="done")["status"] == "PASS"


def test_execution_lock_blocks_duplicate_parent(tmp_path):
    root, registry = alternate_plan(tmp_path)
    with execution_lock(root):
        with pytest.raises(ValueError, match="BATCH_EXECUTION_ACTIVE"):
            run_batch(plan_root=root, run_id="duplicate", adapter_registry=registry)
    assert not (root / "workers").exists()


def test_cancel_live_fake_child_records_terminal_failure(tmp_path):
    adapter, task = _adapter(tmp_path, "timeout")
    event = Event()
    adapter._cancellation = event
    timer = Timer(0.15, event.set)
    timer.start()
    try:
        result = adapter.invoke(
            task=task,
            profile=_profile(),
            attempt_id="cancelled",
            workers_root=tmp_path / "run",
        )
    finally:
        timer.join()
    assert result.failure_code == "CANCELLED"
    assert result.latency_seconds < 2
    assert result.provenance["agent_subprocess_count"] == 1
    assert list((tmp_path / "run").rglob("output_sha256.tsv"))


@pytest.mark.parametrize(
    "elapsed,expected",
    [
        (0, 180),
        (899.9, 180),
        (900, 300),
        (1799.9, 300),
        (1800, 900),
        (7199.9, 900),
        (7200, 3600),
        (43201, 3600),
    ],
)
def test_monitoring_boundaries(elapsed, expected):
    assert check_interval(elapsed) == expected


def test_pid_reuse_never_waits_on_unrelated_process(monkeypatch):
    import os

    identity = process_identity(os.getpid())
    identity["start_ticks"] = "wrong"
    monkeypatch.setattr("time.sleep", lambda _: pytest.fail("must return immediately"))
    wait_for_check(identity, 3600)


def test_retry_blocks_live_child_and_unknown_launch(tmp_path):
    import os

    from dispatcher_for_codex_agents.agent_harness.process_guard import (
        assert_attempt_not_active,
        record_invocation_process,
    )

    args = (tmp_path, "task", "profile", "attempt")
    record_invocation_process(*args, child_pid=os.getpid())
    with pytest.raises(ValueError, match="CHILD_STILL_ALIVE"):
        assert_attempt_not_active(*args, incomplete=True)
    record_invocation_process(*args, child_pid=None)
    with pytest.raises(ValueError, match="LAUNCH_STATE_UNKNOWN"):
        assert_attempt_not_active(*args, incomplete=True)


def test_monitor_wakes_when_library_runner_releases_lock(tmp_path):
    import os
    import time
    from threading import Thread

    acquired = Event()

    def hold():
        with execution_lock(tmp_path, run_id="r1"):
            acquired.set()
            time.sleep(0.1)

    thread = Thread(target=hold)
    thread.start()
    assert acquired.wait(1)
    started = time.monotonic()
    wait_for_check(process_identity(os.getpid()), 180, plan_root=tmp_path)
    thread.join()
    assert time.monotonic() - started < 1


def test_agent_notifications_disabled_at_both_layers(tmp_path, monkeypatch):
    monkeypatch.setenv("DCA_NOTIFY_CONFIG", "/private/notify.json")
    monkeypatch.setenv("DCA_NOTIFY_SESSION_ID", "private-session")
    monkeypatch.setenv("PWD", "/private/project")
    adapter, _ = _adapter(tmp_path, "success")
    command = adapter.build_command(
        profile=_profile(), isolated_working_directory=tmp_path, schema_path=None
    )
    assert "notify=[]" in command
    assert any(
        command[i : i + 2] == ("--disable", "hooks") for i in range(len(command) - 1)
    )
    environment = adapter._environment()
    assert "PWD" not in environment
    assert not any(key.startswith("DCA_NOTIFY_") for key in environment)


def test_monitor_elapsed_is_entire_batch_across_parent_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "dispatcher_for_codex_agents.agent_harness.process_guard.time.time",
        lambda: 100.0,
    )
    with execution_lock(tmp_path, run_id="first"):
        pass
    monkeypatch.setattr(
        "dispatcher_for_codex_agents.agent_harness.process_guard.time.time",
        lambda: 7300.0,
    )
    with execution_lock(tmp_path, run_id="second"):
        pass
    owner = json.loads((tmp_path / ".execution.owner.json").read_text())
    assert owner["started_at"] == 100.0
    assert owner["runner_started_at"] == 7300.0
    assert check_interval(owner["runner_started_at"] - owner["started_at"]) == 3600
