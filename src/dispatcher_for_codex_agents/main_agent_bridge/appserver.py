"""Official stdio App Server transport. Never attach to an IDE's internal port."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import time
from typing import Protocol

from dispatcher_for_codex_agents.agent_harness.adapter import _redact_text
from dispatcher_for_codex_agents.agent_harness.process_guard import process_identity

from .contracts import SupervisorSpec
from .store import EventStore, canonical, exclusive


class MainAgentBridge(Protocol):
    """A task controller owns the transport and one explicitly bound thread."""

    async def request(self, method: str, params: dict) -> dict: ...

    async def receive(self) -> dict: ...

    async def respond(self, request_id, result: dict) -> None: ...

    async def close(self) -> None: ...


class AppServerBridge:
    def __init__(self, spec: SupervisorSpec, store: EventStore):
        self.spec, self.store = spec, store
        self.process = None
        self.pending = {}
        self.incoming = asyncio.Queue()
        self.serial = 0
        self.tasks = []
        self.closed = False
        self.environment = os.environ.copy()
        for key in tuple(self.environment):
            if key.startswith("DCA_NOTIFY_") or key in {
                "CODEX_THREAD_ID",
                "CODEX_TURN_ID",
                "CODEX_SESSION_ID",
                "CODEX_INTERNAL_ORIGINATOR_OVERRIDE",
            }:
                self.environment.pop(key, None)
        self.environment["CODEX_HOME"] = spec.codex_home

    def command(self) -> list[str]:
        command = [self.spec.executable, "--strict-config"]
        for feature in (
            "multi_agent",
            "shell_tool",
            "unified_exec",
            "plugins",
            "apps",
            "hooks",
            "browser_use",
            "computer_use",
            "image_generation",
        ):
            command += ["--disable", feature]
        for setting in (
            "mcp_servers={}",
            "notify=[]",
            'web_search="disabled"',
            "project_doc_max_bytes=0",
        ):
            command += ["--config", setting]
        return [*command, "app-server", "--stdio"]

    async def open(self):
        from .capabilities import inspect_host

        capabilities = await asyncio.to_thread(
            inspect_host,
            self.spec.executable,
            self.spec.codex_home,
            self.spec.working_directory,
        )
        exclusive(
            self.store.root / ("host-capabilities-" + str(time.time_ns()) + ".json"),
            canonical(capabilities),
        )
        # This process has no public listener. Its lifetime is the supervisor's.
        self.process = await asyncio.create_subprocess_exec(
            *self.command(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.environment,
            cwd=self.spec.working_directory,
            start_new_session=True,
            limit=4 * 1024 * 1024,
        )
        self.store.append(
            {
                "state": "HOST_STARTED",
                "identity": process_identity(self.process.pid),
                "time": time.time(),
            }
        )
        self.tasks = [
            asyncio.create_task(self._read()),
            asyncio.create_task(self._stderr()),
        ]
        result = await self.request(
            "initialize",
            {
                "clientInfo": {"name": "dca_event_controller", "version": "1.0.0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        await self._send({"method": "initialized"})
        return result

    async def _send(self, value):
        if self.closed or self.process is None or self.process.returncode is not None:
            raise ConnectionError("APP_SERVER_DISCONNECTED")
        self.process.stdin.write(canonical(value))
        await self.process.stdin.drain()

    async def request(self, method, params):
        self.serial += 1
        request_id = self.serial
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        self.store.append(
            {
                "state": "RPC_SEND",
                "method": method,
                "request_id": request_id,
                "thread_id": params.get("threadId"),
                "time": time.time(),
            }
        )
        try:
            await self._send({"id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout=45)
        finally:
            self.pending.pop(request_id, None)

    async def respond(self, request_id, result):
        await self._send({"id": request_id, "result": result})

    async def receive(self):
        message = await self.incoming.get()
        if isinstance(message, Exception):
            raise message
        return message

    async def _read(self):
        try:
            async for line in self.process.stdout:
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("APP_SERVER_INVALID_EVENT")
                if "id" in message and "method" not in message:
                    future = self.pending.get(message["id"])
                    if future is not None and not future.done():
                        if "error" in message:
                            future.set_exception(
                                RuntimeError("APP_SERVER_RPC_REJECTED")
                            )
                            self.store.append(
                                {
                                    "state": "RPC_ERROR",
                                    "code": message["error"].get("code"),
                                    "request_id": message["id"],
                                    "time": time.time(),
                                }
                            )
                        else:
                            future.set_result(message["result"])
                else:
                    await self.incoming.put(message)
        except Exception as exc:
            await self.incoming.put(exc)
        finally:
            error = ConnectionError("APP_SERVER_DISCONNECTED")
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(error)
            await self.incoming.put(error)

    async def _stderr(self):
        chunks = []
        size = 0
        async for line in self.process.stderr:
            text = _redact_text(line.decode(errors="replace"), self.environment)
            size += len(text)
            if size <= 65536:
                chunks.append(text)
        path = self.store.root / ("host-stderr-" + str(time.time_ns()) + ".log")
        exclusive(path, "".join(chunks).encode())

    async def close(self):
        if self.closed:
            return
        self.closed = True
        if self.process is not None:
            if self.process.returncode is None:
                self.process.stdin.close()
                try:
                    await asyncio.wait_for(self.process.wait(), 3)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(self.process.pid, signal.SIGTERM)
                    try:
                        await asyncio.wait_for(self.process.wait(), 3)
                    except TimeoutError:
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(self.process.pid, signal.SIGKILL)
                        await self.process.wait()
            for task in self.tasks:
                with contextlib.suppress(Exception):
                    await task
