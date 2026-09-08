"""Fixed parent-authored jobs; reuse batch execution, validation and collector."""

from __future__ import annotations

import asyncio
import json
import time
from threading import Event

from dispatcher_for_codex_agents.agent_harness.batch import (
    _verify_hashed_directory,
    collect_batch,
    load_batch_plan,
    run_batch,
)

from .contracts import BridgeEvent, JobSpec, SupervisorSpec
from .store import EventStore, canonical, digest, exclusive


def preflight_jobs(spec: SupervisorSpec):
    for job in spec.jobs:
        if job.kind == "batch":
            plan = load_batch_plan(job.plan_root)
            # A bridge start is not result approval or permission to retry.
            report = run_batch(
                plan_root=plan.root,
                run_id=job.attempt_id,
                max_workers=job.max_workers,
                dry_run=True,
                executable=job.agent_executable,
                codex_home=job.agent_home,
            )
            if report["blocked_existing_attempts"]:
                raise ValueError("EXPLICIT_RETRY_REQUIRED")


def _batch_job(job: JobSpec, cancellation: Event) -> dict:
    from pathlib import Path

    plan_root = Path(job.plan_root)
    report = run_batch(
        plan_root=plan_root,
        run_id=job.attempt_id,
        max_workers=job.max_workers,
        executable=job.agent_executable,
        codex_home=job.agent_home,
        cancellation=cancellation,
    )
    _verify_hashed_directory(
        plan_root / "batch_runs" / job.attempt_id, "output_sha256.tsv"
    )
    collection = collect_batch(plan_root=plan_root, collection_id=job.attempt_id)
    location = plan_root / "collections" / job.attempt_id
    _verify_hashed_directory(location, "collection_manifest.tsv")
    return {
        "job_id": job.job_id,
        "status": (
            "SUCCESS"
            if report["status"] == collection["status"] == "PASS"
            else "BLOCKED"
        ),
        "executed_invocations": report["executed_count"],
        "collection": str(location),
        "collection_status": collection["status"],
        "manifest_sha256": digest((location / "collection_manifest.tsv").read_bytes()),
        "result_acceptance": "NOT_GRANTED",
    }


async def run_jobs(
    spec: SupervisorSpec, store: EventStore, cancellation: Event
) -> list[dict]:
    async def one(job):
        relative = job.job_id + ".json"
        path = store.root / "artifacts" / relative
        # Launch intent makes a crashed job non-relaunchable on recovery.
        store.append(
            {
                "state": "JOB_LAUNCH_INTENT",
                "job_id": job.job_id,
                "attempt_id": job.attempt_id,
                "time": time.time(),
            }
        )
        try:
            if job.kind == "fixture":
                await asyncio.sleep(job.delay_seconds)
                result = {
                    "job_id": job.job_id,
                    "status": "SUCCESS",
                    "fixture_value": job.fixture_value,
                    "doubled": job.fixture_value * 2,
                    "external_agent_invocations": 0,
                }
            else:
                result = await asyncio.to_thread(_batch_job, job, cancellation)
        except Exception as exc:
            result = {
                "job_id": job.job_id,
                "status": "BLOCKED",
                "failure_code": "JOB_FAILED",
                "error_class": type(exc).__name__,
            }
        if cancellation.is_set():
            result = {
                "job_id": job.job_id,
                "status": "BLOCKED",
                "failure_code": "CANCELLED",
            }
        exclusive(path, canonical(result))
        # Parent re-parses the durable result before producing a wakeup manifest.
        if json.loads(path.read_text()) != result:
            raise ValueError("RESULT_PERSISTENCE_INVALID")
        store.append(
            {"state": "JOB_TERMINAL", "job_id": job.job_id, "time": time.time()}
        )
        return {"path": relative, "sha256": digest(path.read_bytes())}

    return await asyncio.gather(*(one(job) for job in spec.jobs))


def seal_event(spec, store, thread_id, files) -> BridgeEvent:
    success = all(
        json.loads((store.root / "artifacts" / row["path"]).read_text())["status"]
        == "SUCCESS"
        for row in files
    )
    manifest = {
        "task_id": spec.task_id,
        "status": "SUCCESS" if success else "BLOCKED",
        "files": files,
    }
    relative = "completion-manifest.json"
    content = canonical(manifest)
    exclusive(store.root / "artifacts" / relative, content)
    event = BridgeEvent(
        kind="batch_completed" if success else "blocked_failure",
        task_id=spec.task_id,
        job_id="aggregate",
        attempt_id="aggregate-001",
        target_thread=thread_id,
        manifest=relative,
        manifest_sha256=digest(content),
        sequence=0,
        occurred_at=time.time(),
    )
    store.put(event)
    return event
