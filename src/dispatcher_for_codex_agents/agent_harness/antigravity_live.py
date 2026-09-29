"""Explicit experimental official-CLI admission, separate from default registry.

Approval is a caller trust boundary, not a capability granted by ModelProfile or
the environment. CLI authentication/state remain entirely CLI-owned. This module
does not inspect authentication or global customization files.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import signal
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .antigravity import (
    AGY_LIVE_TARGET_VERSION,
    compile_headless,
    evaluate_headless_capture,
    validate_schema_argument,
)
from .contracts import AgentTask, FailureCode, InvocationResult, ModelProfile
from .payload import PayloadBuilder
from .schema import validate_schema_definition
from .shard import ImmutableShardWriter

DEFAULT_ADMISSION_PROFILE = "antigravity_default_text_only"
AGENT_NAME = "dca-admission-text-only"
AGENT_RELATIVE = Path(".agents/agents") / (AGENT_NAME + ".md")
AGENT_DEFINITION = """---
name: dca-admission-text-only
description: Single-turn text-only DCA admission probe.
mainAgent: true
subagent: false
excludeDefaultComponents: true
tools: []
mcpServers: []
skills: []
plugins: []
commandExecutionPolicy: "off"
---
Return the requested JSON object. Do not use tools, files, network or subagents.
"""


@dataclass(frozen=True)
class OfficialCliApproval:
    """Trusted caller's reviewed identities, never inferred from user profiles.

    This is an explicit Python admission interface, not a general CLI switch. The
    operator separately verifies official binary origin/version and customizations.
    Hashes pin that review, not an assertion that arbitrary binaries are official.
    """

    executable: Path
    executable_sha256: str
    config_sha256: str
    version: str = AGY_LIVE_TARGET_VERSION


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _path(path: Path, *, directory: bool = False) -> None:
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError("ANTIGRAVITY_PATH_NOT_CANONICAL")
    info = path.lstat()
    if info.st_uid != os.getuid() or (
        not stat.S_ISDIR(info.st_mode)
        if directory
        else not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
    ):
        raise ValueError("ANTIGRAVITY_PATH_IDENTITY_INVALID")


class AntigravityCliAdapter:
    """One explicit admission attempt per disposable workspace; no retries.

    No factory is registered by default. A reviewed config digest AND a separately
    approved executable identity are required. Capability grants remain unsupported.
    Tool activity is audited; advertised inventory is not an effective grant.
    """

    adapter_id = "antigravity_cli"

    def __init__(
        self, *, admission_path: Path, approval: OfficialCliApproval | None = None
    ) -> None:
        if approval is None:
            raise ValueError("ANTIGRAVITY_LIVE_NOT_ADMITTED")
        self.admission_path = admission_path
        self.approval = approval

    def _configuration(self, task: AgentTask, profile: ModelProfile) -> dict:
        try:
            task = AgentTask.model_validate(task.model_dump(mode="json"))
            profile = ModelProfile.model_validate(profile.model_dump(mode="json"))
            if (
                profile.adapter_id != self.adapter_id
                or profile.profile_id
                not in {self.adapter_id, DEFAULT_ADMISSION_PROFILE}
                or task.call_limit != 1
                or not task.capability_policy.restricted
                or not math.isfinite(task.timeout)
                or self.approval.version != AGY_LIVE_TARGET_VERSION
            ):
                raise ValueError("task/profile not admitted")
            validate_schema_definition(task.expected_output_schema)
            _path(self.admission_path)
            if stat.S_IMODE(self.admission_path.stat().st_mode) != 0o600:
                raise ValueError("admission mode")
            raw = self.admission_path.read_bytes()
            if _sha(raw) != self.approval.config_sha256:
                raise ValueError("admission changed")
            config = json.loads(raw)
            required = {
                "ANTIGRAVITY_EXPERIMENTAL_LIVE": True,
                "provider": self.adapter_id,
                "profile": profile.profile_id,
                "session_mode": "fresh",
                "dca_retries": 0,
                "provider_fallback": False,
                "customization_baseline": "PASS",
                "state_ownership": "OFFICIAL_CLI",
            }
            if set(config) != set(required) | {"workspace", "model"}:
                raise ValueError("admission fields")
            for key, value in required.items():
                if type(config[key]) is not type(value) or config[key] != value:
                    raise ValueError("admission value")
            default_agent = profile.profile_id == DEFAULT_ADMISSION_PROFILE
            model = config["model"]
            if default_agent and model is not None:
                raise ValueError("default admission must not override model")
            if model is not None and (
                not isinstance(model, str) or not model or model.startswith("-")
            ):
                raise ValueError("model selection")
            executable = self.approval.executable
            _path(executable)
            if not os.access(executable, os.X_OK) or (
                _sha(executable.read_bytes()) != self.approval.executable_sha256
            ):
                raise ValueError("executable identity")
            workspace = Path(config["workspace"])
            _path(workspace, directory=True)
            if stat.S_IMODE(workspace.stat().st_mode) != 0o700:
                raise ValueError("workspace mode")
            # The workspace must not be in any source checkout or contain any
            # other customization/input. Inputs are read by the parent only.
            if any(
                (p / ".git").is_file() or (p / ".git/HEAD").exists()
                for p in (workspace, *workspace.parents)
            ):
                raise ValueError("workspace in repository")
            expected = (
                set()
                if default_agent
                else {
                    workspace / ".agents",
                    workspace / ".agents/agents",
                    workspace / AGENT_RELATIVE,
                }
            )
            if default_agent and os.path.lexists(self._marker(workspace, True)):
                raise ValueError("workspace already used")
            if set(workspace.rglob("*")) != expected:
                raise ValueError("workspace not fresh")
            for path in expected:
                _path(path, directory=path != workspace / AGENT_RELATIVE)
            if not default_agent and (
                (workspace / AGENT_RELATIVE).read_bytes() != AGENT_DEFINITION.encode()
            ):
                raise ValueError("agent definition changed")
            return config
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            raise ValueError("ANTIGRAVITY_EXPERIMENTAL_ADMISSION_INVALID") from None

    @staticmethod
    def _marker(workspace: Path, default_agent: bool) -> Path:
        # Keep the default-agent working directory genuinely empty at startup.
        # A sibling marker is still exclusive per canonical workspace path.
        return (
            workspace.with_name("." + workspace.name + ".dca-admission-used")
            if default_agent
            else workspace / ".dca-admission-used"
        )

    def _check_binary(self) -> None:
        """Recheck the selected path; do not auto-admit an updater's replacement."""
        try:
            executable = self.approval.executable
            _path(executable)
            if not os.access(executable, os.X_OK) or (
                _sha(executable.read_bytes()) != self.approval.executable_sha256
            ):
                raise ValueError("identity mismatch")
        except (OSError, ValueError, RuntimeError):
            raise ValueError("ANTIGRAVITY_BINARY_DRIFT") from None

    def _runtime_check(self, workspace: Path, env: dict[str, str]) -> None:
        """No prompt/auth discovery. Every invocation gets a fresh version probe."""
        self._check_binary()
        try:
            probe = subprocess.run(
                [str(self.approval.executable), "--version"],
                cwd=workspace,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            raise ValueError("ANTIGRAVITY_VERSION_PROBE_FAILED") from None
        # Do not emit probe output: even a failing executable may echo secrets.
        self._check_binary()
        if probe.returncode != 0 or probe.stdout.strip() != (
            AGY_LIVE_TARGET_VERSION.encode("ascii")
        ):
            raise ValueError("ANTIGRAVITY_VERSION_UNSUPPORTED")

    def preflight(self, *, task: AgentTask, profile: ModelProfile) -> dict:
        config = self._configuration(task, profile)
        return {
            "adapter_id": self.adapter_id,
            "experimental_admission": "EXPLICIT_REVIEWED_LOCAL_CONFIG",
            "admission_sha256": self.approval.config_sha256,
            "executable_sha256": self.approval.executable_sha256,
            "runtime_version": self.approval.version,
            "configured_provider": self.adapter_id,
            "admission_profile": profile.profile_id,
            "agent_selection": (
                "DEFAULT_NO_OVERRIDE"
                if profile.profile_id == DEFAULT_ADMISSION_PROFILE
                else "EXPLICIT_CUSTOM_TEXT_ONLY"
            ),
            "configured_model": config["model"],
            "state_ownership": "OFFICIAL_CLI",
            "credential_access_by_dca": False,
            "customization_baseline": config["customization_baseline"],
            "permission_enforcement": "NOT_VERIFIED",
        }

    def invoke(
        self,
        *,
        task: AgentTask,
        profile: ModelProfile,
        attempt_id: str,
        workers_root: str | Path,
        payload_builder: PayloadBuilder | None = None,
    ) -> InvocationResult:
        config = self._configuration(task, profile)
        preflight = self.preflight(task=task, profile=profile)
        payload = (payload_builder or PayloadBuilder()).build(task)
        plan = compile_headless(
            task=task,
            executable=str(self.approval.executable),
            model=config["model"],
            prompt=payload.content,
            version=self.approval.version,
        )
        default_agent = profile.profile_id == DEFAULT_ADMISSION_PROFILE
        argv = [
            *plan.argv[:-1],
            "--sandbox",
            *([] if default_agent else ["--agent=" + AGENT_NAME]),
            plan.argv[-1],
        ]
        validate_schema_argument(argv, task.expected_output_schema)
        workspace = Path(config["workspace"])
        # Copy only for child inheritance; never persist or log this environment.
        # This overrides a parent's "1"/"false" without changing the parent.
        child_env = dict(os.environ)
        child_env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "true"
        self._runtime_check(workspace, child_env)
        # Persistent single-use marker: no accidental second subprocess, even
        # if a caller supplies a different attempt ID after failure.
        marker = self._marker(workspace, default_agent)
        descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        writer = ImmutableShardWriter(
            workers_root=workers_root,
            task_id=task.task_id,
            profile_id=profile.profile_id,
            attempt_id=attempt_id,
        )
        start = time.monotonic()
        stdout, stderr = b"", b""
        exit_code = None
        timed_out = False
        launched = False
        binary_drift = False
        try:
            # Inherit normal official user environment. DCA does not read/copy
            # auth files, inspect keyring, set HOME, or select an API fallback.
            # Check again after reservation and immediately before prompt-bearing
            # spawn. This detects observed drift; it is not an atomic exec/hash
            # primitive against concurrent replacement by another process.
            self._check_binary()
            process = subprocess.Popen(
                argv,
                cwd=workspace,
                env=child_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            launched = True
            try:
                stdout, stderr = process.communicate(
                    timeout=plan.process_timeout_seconds
                )
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate()
            exit_code = process.returncode
        except ValueError:
            binary_drift = True
            stderr = b"ANTIGRAVITY_BINARY_DRIFT\n"
        except OSError:
            # Never copy a launch exception (potentially containing argv) into
            # public diagnostics. The reserved failure shard is still sealed.
            stderr = b"ANTIGRAVITY_PROCESS_START_FAILED\n"
        elapsed = time.monotonic() - start
        result = evaluate_headless_capture(
            task=task,
            stdout=stdout,
            exit_code=exit_code,
            elapsed=elapsed,
            timed_out=timed_out,
            version=self.approval.version,
        )
        observed = result.provenance.get("observed_init", {})
        if result.status == "success" and (
            observed.get("cwd") != str(workspace)
            or observed.get("agent") != (None if default_agent else AGENT_NAME)
        ):
            result = result.model_copy(
                update={
                    "status": "failure",
                    "final_output": None,
                    "failure_code": FailureCode.POLICY_VIOLATION,
                    "warnings": (*result.warnings, "AGENT_OR_WORKSPACE_MISMATCH"),
                }
            )
        provenance = dict(result.provenance)
        provenance.update(preflight)
        provenance.update(
            evidence_scope="EXPERIMENTAL_OFFICIAL_CLI_CAPTURE",
            live_execution_gate="EXPLICIT_EXPERIMENTAL_ADMISSION",
            accounting_scope="THIS_FRESH_PROCESS",
            dca_logical_invocations=int(launched),
            dca_retries=0,
            execution_route="OFFICIAL_CLI_SUBPROCESS",
            argv=argv,
            cwd=str(workspace),
            effective_capability=None,
            auto_update_environment={"AGY_CLI_DISABLE_AUTO_UPDATE": "true"},
            runtime_version_check="PASS",
            prespawn_binary_check="FAIL" if binary_drift else "PASS",
        )
        # Raw stderr stays in the private immutable capture. Never interpret a
        # prose denial as tool enforcement or publish it as a warning.
        result = result.model_copy(
            update={
                "provenance": provenance,
                **(
                    {
                        "failure_code": (
                            FailureCode.POLICY_VIOLATION
                            if binary_drift
                            else FailureCode.CLI_NONZERO_EXIT
                        )
                    }
                    if not launched
                    else {}
                ),
            }
        )
        writer.write(
            task=task,
            profile=profile,
            input_records=payload.input_records,
            events_jsonl=stdout,
            stderr_log=stderr,
            final_output=result.final_output,
            result=result,
        )
        return result
