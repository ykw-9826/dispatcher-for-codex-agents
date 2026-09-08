"""Deterministic main-model replacement; no provider or phone requests."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from dispatcher_for_codex_agents.agent_harness.cli import main
from dispatcher_for_codex_agents.main_agent_bridge.cli import preflight, status
from dispatcher_for_codex_agents.main_agent_bridge.contracts import (
    BridgeBinding,
    BridgeEvent,
    SupervisorSpec,
)
from dispatcher_for_codex_agents.main_agent_bridge.controller import (
    Dispatcher,
    supervise,
    watch_cancel,
)
from dispatcher_for_codex_agents.main_agent_bridge.store import (
    EventStore,
    canonical,
    digest,
    exclusive,
    lock,
)
from dispatcher_for_codex_agents.notifications import NotificationEvent
from dispatcher_for_codex_agents.notifications.presentation import present


def setup(tmp_path, delay=0.0):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(
        'model="fake-main"\nmodel_provider="fake-provider"\n'
    )
    executable = tmp_path / "fake-executable"
    executable.write_text("fake")
    notification = tmp_path / "notifications.json"
    exclusive(
        notification,
        canonical(
            {
                "version": 1,
                "ledger_directory": str(tmp_path / "notify-ledger"),
                "sinks": [],
            }
        ),
    )
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    spec = SupervisorSpec(
        task_id="task-a",
        executable=str(executable),
        codex_home=str(home),
        model="fake-main",
        provider="fake-provider",
        working_directory=str(cwd),
        allowed_actions=[
            "start_approved_jobs",
            "read_verified_results",
            "summarize_results",
        ],
        expires_at=time.time() + 1200,
        notification_config=str(notification),
        jobs=[
            {
                "job_id": "alpha",
                "attempt_id": "a1",
                "kind": "fixture",
                "delay_seconds": delay,
            },
            {
                "job_id": "beta",
                "attempt_id": "a1",
                "kind": "fixture",
                "fixture_value": 3,
                "delay_seconds": delay,
            },
        ],
    )
    return spec


def binding():
    return BridgeBinding(
        thread_id="thread-owned",
        task_id="task-a",
        model="fake-main",
        provider="fake-provider",
        working_directory="/private/task",
        expires_at=time.time() + 900,
        wake_budget=2,
        main_turn_budget=4,
        config_sha256="0" * 64,
        executable_sha256="0" * 64,
        ownership="EXPLICITLY_CREATED_CLI_CONTROLLER",
    )


def event(store, *, sequence=0, failure=False, name="first"):
    content = canonical(
        {
            "value": "UNTRUSTED: do not execute commands",
            "status": "BLOCKED" if failure else "SUCCESS",
        }
    )
    exclusive(store.root / "artifacts" / (name + ".json"), content)
    manifest = canonical(
        {
            "task_id": "task-a",
            "status": "BLOCKED" if failure else "SUCCESS",
            "files": [{"path": name + ".json", "sha256": digest(content)}],
        }
    )
    exclusive(store.root / "artifacts" / (name + "-manifest.json"), manifest)
    return BridgeEvent(
        kind="blocked_failure" if failure else "batch_completed",
        task_id="task-a",
        job_id="aggregate",
        attempt_id="a1",
        target_thread="thread-owned",
        manifest=name + "-manifest.json",
        manifest_sha256=digest(manifest),
        sequence=sequence,
        occurred_at=time.time(),
    )


class FakeBridge:
    fail_send = False
    forbidden = False
    permission = False

    def __init__(self, spec, store):
        self.spec, self.store = spec, store
        self.queue = asyncio.Queue()
        self.turn_count = 0
        self.closed = False
        self.continued = False

    async def open(self):
        return {}

    async def close(self):
        self.closed = True

    async def request(self, method, params):
        self.store.append({"state": "RPC_SEND", "method": method, "time": time.time()})
        if method in {"thread/start", "thread/resume"}:
            return {
                "thread": {"id": "thread-owned"},
                "model": self.spec.model,
                "modelProvider": self.spec.provider,
                "cwd": self.spec.working_directory,
            }
        if method == "thread/read":
            return {"thread": {"id": "thread-owned", "status": {"type": "idle"}}}
        if method == "turn/interrupt":
            return {}
        assert method == "turn/start"
        if self.fail_send:
            raise ConnectionError("lost ACK after sending")
        self.turn_count += 1
        turn_id = "turn-" + str(self.turn_count)
        continuation = "toolOutput" in params
        self.continued = self.continued or continuation
        tool = (
            "dca_read_verified_results" if continuation else "dca_start_approved_jobs"
        )
        if self.forbidden:
            tool = "exec_command"
        method = (
            "item/tool/call"
            if not self.permission
            else "item/commandExecution/requestApproval"
        )
        await self.queue.put(
            {
                "id": 101,
                "method": method,
                "params": {
                    "threadId": "thread-owned",
                    "turnId": turn_id,
                    "tool": tool,
                    "arguments": {},
                },
            }
        )
        return {"turn": {"id": turn_id}}

    async def receive(self):
        return await self.queue.get()

    async def respond(self, request_id, result):
        output = json.loads(result["contentItems"][0]["text"])
        final = "WAITING"
        if "results" in output:
            final = json.dumps(
                {
                    "task_id": self.spec.task_id,
                    "confirmed": True,
                    "result_files": [r["file"] for r in output["results"]],
                    "summary": "Read and confirmed deterministic results.",
                }
            )
        await self.queue.put(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-owned",
                    "item": {"type": "agentMessage", "text": final},
                },
            }
        )
        await self.queue.put(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-owned",
                    "turn": {
                        "id": "turn-" + str(self.turn_count),
                        "status": "completed",
                    },
                },
            }
        )


def test_end_to_end_really_continues_same_thread_no_polling(tmp_path):
    spec = setup(tmp_path, delay=0.03)
    store = EventStore(tmp_path / "state", create=True)
    instances, notices = [], []

    def factory(spec, store):
        bridge = FakeBridge(spec, store)
        instances.append(bridge)
        return bridge

    def failed_phone(*args):
        notices.append(args[0].kind)
        raise TimeoutError("phone failed")

    result = asyncio.run(
        supervise(spec, store, bridge_factory=factory, notifier=failed_phone)
    )
    assert result["status"] == "COMPLETED"
    assert result["duplicate_event_suppressed"] is True
    assert (
        instances[0].continued and instances[0].turn_count == 2 and instances[0].closed
    )
    report = status(store)
    assert report["logical_main_turn_requests"] == 2
    assert report["wait_logical_model_requests"] == 0
    assert report["continuation_requests"] == 1
    assert notices == ["batch_completed"]
    assert result["physical_model_api_requests"] == "UNKNOWN"
    assert result["external_agent_invocations"] == 0


def test_blocking_wait_and_early_delivery(tmp_path):
    async def run():
        store = EventStore(tmp_path / "state", create=True)
        waiting = asyncio.create_task(store.wait())
        await asyncio.sleep(0.01)
        assert not waiting.done()
        item = event(store)
        key, _ = store.put(item)
        assert (await asyncio.wait_for(waiting, 1))[0][0] == key
        # Earlier than subscription and after process/store recreation.
        restarted = EventStore(store.root)
        assert (await asyncio.wait_for(restarted.wait(), 1))[0][0] == key

    asyncio.run(run())


def test_duplicate_and_out_of_order_failure_not_swallowed(tmp_path):
    store = EventStore(tmp_path / "state", create=True)
    later = event(store, sequence=7)
    earlier = event(store, sequence=1, failure=True, name="failure")
    key, new = store.put(later)
    assert new
    assert store.put(later.model_copy(update={"occurred_at": 1.0, "sequence": 8})) == (
        key,
        False,
    )
    store.put(earlier)
    assert [e.kind for _, e in store.pending()] == [
        "blocked_failure",
        "batch_completed",
    ]


@pytest.mark.parametrize("failure", ["ack_loss", "disconnect", "crash_after_reserve"])
def test_unknown_delivery_never_restarts_model(tmp_path, failure):
    spec = setup(tmp_path)
    store = EventStore(tmp_path / "state", create=True)
    key, _ = store.put(event(store))
    bridge = FakeBridge(spec, store)
    bridge.fail_send = True
    dispatcher = Dispatcher(store, binding(), bridge)
    if failure == "crash_after_reserve":
        store.append({"state": "RESERVED", "reservation": "old", "event_ids": [key]})
    else:
        with pytest.raises(ConnectionError):
            asyncio.run(dispatcher.deliver())
    restarted = EventStore(store.root)
    assert not restarted.pending()
    calls = len(store.records())
    if failure != "crash_after_reserve":
        with pytest.raises(ValueError, match="AMBIGUOUS"):
            asyncio.run(Dispatcher(restarted, binding(), bridge).deliver())
    else:
        assert asyncio.run(Dispatcher(restarted, binding(), bridge).deliver()) is None
    assert len(store.records()) == calls


@pytest.mark.parametrize("gate", ["active", "permission_pending", "cancelled"])
def test_activity_permission_and_cancel_win(tmp_path, gate):
    spec = setup(tmp_path)
    store = EventStore(tmp_path / "state", create=True)
    store.put(event(store))
    bridge = FakeBridge(spec, store)
    with pytest.raises(ValueError):
        asyncio.run(Dispatcher(store, binding(), bridge).deliver(**{gate: True}))
    assert bridge.turn_count == 0 and not store.records()


def test_budget_and_expiry_fail_closed(tmp_path):
    spec = setup(tmp_path)
    store = EventStore(tmp_path / "state", create=True)
    store.put(event(store))
    bridge = FakeBridge(spec, store)
    expired = binding().model_copy(update={"expires_at": 1.0})
    with pytest.raises(ValueError, match="EXPIRED"):
        asyncio.run(Dispatcher(store, expired, bridge).deliver())
    for number in range(2):
        store.append({"state": "RESERVED", "reservation": str(number), "event_ids": []})
        store.append(
            {"state": "COMPLETED", "reservation": str(number), "event_ids": []}
        )
    with pytest.raises(ValueError, match="WAKE_BUDGET"):
        asyncio.run(Dispatcher(store, binding(), bridge).deliver())
    assert bridge.turn_count == 0


@pytest.mark.parametrize(
    "attack",
    ["signature", "hash", "escape", "symlink", "source", "recursion", "foreign_thread"],
)
def test_untrusted_events_and_paths_rejected(tmp_path, attack):
    store = EventStore(tmp_path / "state", create=True)
    item = event(store)
    if attack == "signature":
        key, _ = store.put(item)
        path = store.root / "events" / (key + ".json")
        value = json.loads(path.read_text())
        value["event"]["kind"] = "human_action_required"
        path.write_text(json.dumps(value))
        with pytest.raises(ValueError, match="SOURCE"):
            store.pending()
    elif attack == "hash":
        (store.root / "artifacts/first.json").write_text("changed")
        with pytest.raises(ValueError, match="HASH"):
            store.put(item)
    elif attack == "symlink":
        (store.root / "artifacts/link").symlink_to(tmp_path)
        with pytest.raises(ValueError, match="PATH"):
            store.put(item.model_copy(update={"manifest": "link/file.json"}))
    elif attack in {"escape", "source", "recursion"}:
        update = {
            "escape": {"manifest": "../secret"},
            "source": {"source": "agent"},
            "recursion": {"kind": "turn_completed"},
        }[attack]
        with pytest.raises(ValueError):
            BridgeEvent.model_validate({**item.model_dump(), **update})
    else:
        spec = setup(tmp_path)
        store.put(item.model_copy(update={"target_thread": "wrong-thread"}))
        with pytest.raises(ValueError, match="THREAD_BINDING"):
            asyncio.run(Dispatcher(store, binding(), FakeBridge(spec, store)).deliver())


def test_private_permissions_immutable_and_controller_lock(tmp_path):
    store = EventStore(tmp_path / "state", create=True)
    assert store.root.stat().st_mode & 0o777 == 0o700
    assert (store.root / "event.key").stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        EventStore(store.root, create=True)
    with lock(store.root / "controller.lock"):
        with pytest.raises(ValueError, match="ACTIVE_THREAD"):
            with lock(store.root / "controller.lock"):
                pass
    (store.root / "event.key").chmod(0o644)
    with pytest.raises(ValueError, match="OWNER_OR_MODE"):
        EventStore(store.root)


def test_cancel_event_uses_os_wakeup(tmp_path):
    async def run():
        store = EventStore(tmp_path / "state", create=True)
        called = asyncio.Event()
        watcher = asyncio.create_task(watch_cancel(store, called.set))
        await asyncio.sleep(0.01)
        exclusive(store.root / "cancel.json", b"{}")
        await asyncio.wait_for(called.wait(), 1)
        await watcher

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["forbidden", "permission"])
def test_runtime_tool_or_permission_failure_never_approves(tmp_path, kind):
    spec = setup(tmp_path)
    store = EventStore(tmp_path / "state", create=True)

    def factory(spec, store):
        bridge = FakeBridge(spec, store)
        setattr(bridge, kind, True)
        return bridge

    with pytest.raises(ValueError, match="POLICY|PERMISSION"):
        asyncio.run(supervise(spec, store, bridge_factory=factory))
    assert not (store.root / "artifacts/alpha.json").exists()


def test_cli_dry_run_status_cancel_and_invalid_spec(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        "dispatcher_for_codex_agents.main_agent_bridge.capabilities.inspect_host",
        lambda *args: {"model_requests": 0},
    )
    spec = setup(tmp_path)
    path = tmp_path / "spec.json"
    exclusive(path, canonical(spec.model_dump()))
    root = tmp_path / "state"
    args = [
        "bridge",
        "start",
        "--spec",
        str(path),
        "--state-root",
        str(root),
        "--new-thread",
        "--dry-run",
    ]
    assert main(args) == 0 and not root.exists()
    assert json.loads(capsys.readouterr().out)["model_calls"] == 0
    EventStore(root, create=True)
    assert main(["bridge", "cancel", "--state-root", str(root)]) == 0
    assert main(["bridge", "status", "--state-root", str(root)]) == 0
    assert main(args) == 17
    path.write_text('{"api_key": "DO_NOT_PRINT_SECRET"}')
    assert main(args) == 17
    assert "DO_NOT_PRINT_SECRET" not in capsys.readouterr().out
    assert main(["bridge", "recover", "--state-root", str(root)]) == 17


def test_config_change_or_model_change_fails_before_model(tmp_path):
    spec = setup(tmp_path)
    preflight(spec)
    with pytest.raises(ValueError, match="MODEL"):
        preflight(spec.model_copy(update={"model": "different-main"}))


def test_notification_chinese_template_contains_no_ids_or_body():
    item = NotificationEvent(
        source="codex",
        kind="turn_completed",
        status="COMPLETED",
        session_id="PRIVATE_LONG_SESSION_ID",
    )
    output = present(item)
    assert output["title"] == "[DCA] 本轮回复结束"
    assert "PRIVATE" not in str(output)
    assert "不代表结果已获批准" in output["body"]
    closed = NotificationEvent(
        source="test",
        kind="event_acceptance_completed",
        status="TEST",
        task_id="private",
        metrics={
            "same_thread_verified": 1,
            "wait_model_requests": 0,
            "duplicate_suppression_verified": 1,
        },
    )
    assert present(closed)["title"] == "[DCA] 事件驱动验收完成"
    assert "IDE 原窗口：不支持" in present(closed)["body"]


def test_torn_ledger_cannot_be_treated_as_unsent(tmp_path):
    store = EventStore(tmp_path / "state", create=True)
    exclusive(store.root / "consumption.jsonl", b'{"state":"RESER')
    with pytest.raises(ValueError, match="TORN"):
        store.pending()


def test_no_brand_branch_or_shell_resume_or_host_port():
    root = (
        Path(__file__).parents[2] / "src/dispatcher_for_codex_agents/main_agent_bridge"
    )
    content = "\n".join(p.read_text() for p in root.glob("*.py"))
    for forbidden in (
        "--last",
        "shell=True",
        "sqlite3",
        "pyautogui",
        "deepseek",
        "glm",
        "kimi",
        "ws://",
        "thread/fork",
    ):
        assert forbidden not in content
    assert '"app-server", "--stdio"' in content


def test_real_stdio_transport_with_fake_executable(tmp_path):
    spec = setup(tmp_path, delay=0.04)
    executable = Path(spec.executable)
    fixture = Path(__file__).parents[1] / "fixtures/fake_main_appserver.py"
    executable.write_text(fixture.read_text())
    executable.chmod(0o700)
    store = EventStore(tmp_path / "state", create=True)
    result = asyncio.run(supervise(spec, store))
    assert result["status"] == "COMPLETED"
    assert result["thread_id"] == "thread-stdio"
    assert status(store)["wait_logical_model_requests"] == 0
    methods = [r["method"] for r in store.records() if r.get("state") == "RPC_SEND"]
    assert methods == ["initialize", "thread/start", "turn/start", "turn/start"]
    assert len(list(store.root.glob("host-capabilities-*.json"))) == 1


def test_recovery_proven_unsent_event_continues_without_relaunch(tmp_path):
    from dispatcher_for_codex_agents.main_agent_bridge.controller import Controller
    from dispatcher_for_codex_agents.main_agent_bridge.jobs import run_jobs, seal_event

    spec = setup(tmp_path)
    store = EventStore(tmp_path / "state", create=True)

    async def prepare():
        bridge = FakeBridge(spec, store)
        controller = Controller(spec, store, bridge)
        await controller.create_thread()
        files = await run_jobs(spec, store, controller.cancellation)
        seal_event(spec, store, controller.binding.thread_id, files)

    asyncio.run(prepare())
    before = sum(r.get("state") == "JOB_LAUNCH_INTENT" for r in store.records())
    result = asyncio.run(
        supervise(spec, EventStore(store.root), recover=True, bridge_factory=FakeBridge)
    )
    assert result["status"] == "COMPLETED"
    assert (
        sum(r.get("state") == "JOB_LAUNCH_INTENT" for r in store.records())
        == before
        == 2
    )
    assert not store.pending()
    with pytest.raises(ValueError, match="UNCERTAIN|NO_VERIFIED"):
        asyncio.run(supervise(spec, store, recover=True, bridge_factory=FakeBridge))


def test_restart_with_incomplete_jobs_never_relaunches(tmp_path):
    spec = setup(tmp_path)
    store = EventStore(tmp_path / "state", create=True)
    store.append({"state": "JOB_LAUNCH_INTENT", "job_id": "alpha"})
    with pytest.raises(ValueError, match="NO_VERIFIED"):
        asyncio.run(supervise(spec, store, recover=True, bridge_factory=FakeBridge))
    assert not any(r.get("method") == "turn/start" for r in store.records())


def test_cancel_running_controller_interrupts_without_continuation(tmp_path):
    spec = setup(tmp_path, delay=90.0)
    store = EventStore(tmp_path / "state", create=True)

    async def run():
        task = asyncio.create_task(supervise(spec, store, bridge_factory=FakeBridge))
        await asyncio.sleep(0.04)
        exclusive(store.root / "cancel.json", b"{}")
        with pytest.raises(ValueError, match="CANCEL"):
            await asyncio.wait_for(task, 1)

    asyncio.run(run())
    assert sum(r.get("method") == "turn/start" for r in store.records()) == 1
    assert not store.pending()


def test_long_wait_and_over_720_minutes_use_fake_clock_only(tmp_path, monkeypatch):
    from dispatcher_for_codex_agents.main_agent_bridge.controller import Controller

    spec = setup(tmp_path)
    store = EventStore(tmp_path / "state", create=True)
    ticks = iter([0.0, 0.0, 901.0, 1801.0, 7201.0, 43201.0, 43202.0, 43202.0])

    # Patch this module's clock reference, not asyncio's global monotonic clock.
    class Clock:
        @staticmethod
        def monotonic():
            return next(ticks)

        time = staticmethod(time.time)

    monkeypatch.setattr(
        "dispatcher_for_codex_agents.main_agent_bridge.controller.time", Clock
    )
    intervals = []

    async def run():
        controller = Controller(spec, store, FakeBridge(spec, store))
        controller.binding = binding()
        monkeypatch.setattr(controller, "guard", lambda: None)
        future = asyncio.get_running_loop().create_future()
        controller.jobs = future

        async def wait(tasks, *, timeout):
            intervals.append(timeout)
            if len(intervals) == 5:
                future.set_result([])
                return set(), set()
            return set(), set()

        monkeypatch.setattr(
            "dispatcher_for_codex_agents.main_agent_bridge.controller.asyncio.wait",
            wait,
        )
        await controller._wait_jobs()

    asyncio.run(run())
    assert intervals == [180, 300, 900, 3600, 3600]
    watchdog = [r for r in store.records() if r.get("state") == "MODEL_FREE_WATCHDOG"]
    assert any(r["elapsed"] > 43200 for r in watchdog)
    assert not any(r.get("method") == "turn/start" for r in store.records())


def test_event_batching_one_continuation_and_later_pending(tmp_path):
    spec = setup(tmp_path)
    store = EventStore(tmp_path / "state", create=True)
    store.put(event(store))
    store.put(event(store, failure=True, name="error", sequence=2))
    bridge = FakeBridge(spec, store)
    reservation, _, ids = asyncio.run(Dispatcher(store, binding(), bridge).deliver())
    assert len(ids) == 2 and bridge.turn_count == 1
    store.put(event(store, failure=True, name="late", sequence=3))
    with pytest.raises(ValueError, match="UNACKNOWLEDGED"):
        asyncio.run(Dispatcher(store, binding(), bridge).deliver())
    store.append({"state": "COMPLETED", "reservation": reservation, "event_ids": ids})
    assert asyncio.run(Dispatcher(store, binding(), bridge).deliver()) is not None
    assert bridge.turn_count == 2


@pytest.mark.parametrize("mode", ["batch_success", "rate_limit"])
def test_batch_reuses_harness_collector_and_notifier(tmp_path, monkeypatch, mode):
    from test_agent_harness_batch import (
        FAKE_CODEX,
        _fake_environment,
        _fake_home,
        _plan,
    )

    spec = setup(tmp_path)
    plan = _plan(tmp_path / "batch-fixture", records=2, profiles=1, shard_size=1)
    home = _fake_home(tmp_path, profiles=1)
    log = _fake_environment(monkeypatch, tmp_path, mode=mode)
    spec = SupervisorSpec.model_validate(
        {
            **spec.model_dump(),
            "jobs": [
                {
                    "job_id": "batch-job",
                    "attempt_id": "a1",
                    "kind": "batch",
                    "plan_root": str(plan),
                    "agent_home": str(home),
                    "agent_executable": str(FAKE_CODEX),
                }
            ],
        }
    )
    store = EventStore(tmp_path / "state", create=True)
    result = asyncio.run(supervise(spec, store, bridge_factory=FakeBridge))
    assert result["status"] == "COMPLETED"
    assert len(log.read_text().splitlines()) == 2
    expected = "SUCCESS" if mode == "batch_success" else "BLOCKED"
    assert result["result_status"] == expected
    assert (plan / "collections/a1/collection_manifest.tsv").is_file()
    assert (plan / "collections/a1/failed_shards.tsv").is_file()
    notices = [r for r in store.records() if r.get("state") == "NOTIFICATION_RESULT"]
    assert len(notices) == 1
