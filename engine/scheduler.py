from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone
from pathlib import Path
import shutil
from typing import Any

from engine.logs import append
from engine.runner import IntegrityError, make_workspace, pull_dependencies, run_job
from engine.slack import notify
from registry import metadata
from registry.config import cfg
from registry.resolver import DependencyCycleError, ResolutionError, resolve
from registry.storage import ArtifactError, store_upload


class DagError(Exception):
    pass


def topo_validate(jobs: dict[str, Any]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str, stack: list[str]) -> None:
        if name in visiting:
            cycle = stack[stack.index(name):]
            raise DagError("job cycle: " + " -> ".join(cycle))
        if name in visited:
            return
        if name not in jobs:
            raise DagError(f"job '{name}' is listed in needs but does not exist")
        visiting.add(name)
        for dep in jobs[name].get("needs", []) or []:
            visit(dep, stack + [dep])
        visiting.remove(name)
        visited.add(name)

    for name in jobs:
        visit(name, [name])


def descendants(jobs: dict[str, Any], failed: str) -> set[str]:
    out: set[str] = set()
    changed = True
    while changed:
        changed = False
        for name, job in jobs.items():
            needs = set(job.get("needs", []) or [])
            if name not in out and (failed in needs or needs.intersection(out)):
                out.add(name)
                changed = True
    return out


async def publish_artifacts(run_id: str, pipeline: dict[str, Any], workspace: Path, publisher: str) -> None:
    deps = pipeline.get("dependencies") or []
    for artifact in pipeline.get("artifacts", []):
        path = (workspace / artifact["path"]).resolve()
        try:
            path.relative_to(workspace.resolve())
        except ValueError:
            raise ArtifactError(400, f"artifact path escapes workspace: {artifact['path']}")
        if not path.exists():
            raise ArtifactError(400, f"artifact path does not exist: {artifact['path']}")
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
        checksum = "sha256:" + hasher.hexdigest()
        with path.open("rb") as handle:
            store_upload(
                name=artifact["name"],
                version=artifact["version"],
                checksum=checksum,
                fileobj=handle,
                publisher=publisher,
                deps=deps,
            )
        await append(run_id, "publisher", f"published {artifact['name']}@{artifact['version']} {checksum}")


def duration_seconds(started: datetime) -> float:
    return round((datetime.now(timezone.utc) - started).total_seconds(), 3)


def reset_workspace(run_id: str, workspace: Path) -> None:
    for child in workspace.iterdir():
        if child.name == "deps":
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


async def execute_run(run_id: str, pipeline: dict[str, Any], token: str | None, publisher: str) -> None:
    started = datetime.now(timezone.utc)
    metadata.update_run(run_id, "running", started_at=started)
    await append(run_id, "system", f"pipeline {pipeline['name']} started")
    await notify("pipeline started", {"pipeline": pipeline["name"], "run_id": run_id, "duration": 0})
    try:
        try:
            lockfile = resolve(pipeline.get("dependencies") or [])
        except DependencyCycleError as exc:
            metadata.update_run(run_id, "cycle_failure", finished_at=datetime.now(timezone.utc), failure_reason=str(exc))
            await append(run_id, "resolver", str(exc))
            await notify("resolution failure", {"pipeline": pipeline["name"], "run_id": run_id, "duration": duration_seconds(started), "details": str(exc)})
            return
        except ResolutionError as exc:
            metadata.update_run(run_id, "conflict_failure", finished_at=datetime.now(timezone.utc), failure_reason=str(exc))
            await append(run_id, "resolver", str(exc))
            await notify("resolution failure", {"pipeline": pipeline["name"], "run_id": run_id, "duration": duration_seconds(started), "details": str(exc)})
            return

        metadata.update_run(run_id, "running", lockfile=lockfile)
        workspace = make_workspace(run_id)
        reset_workspace(run_id, workspace)
        try:
            await pull_dependencies(run_id, workspace, lockfile)
        except IntegrityError as exc:
            metadata.update_run(run_id, "integrity_failure", finished_at=datetime.now(timezone.utc), failure_reason=str(exc))
            await notify("integrity failure", {"run_id": run_id, "duration": duration_seconds(started), "artifact": f"{exc.name}@{exc.version}", "expected": exc.expected, "actual": exc.actual})
            return

        jobs = pipeline["jobs"]
        try:
            topo_validate(jobs)
        except DagError as exc:
            metadata.update_run(run_id, "cycle_failure", finished_at=datetime.now(timezone.utc), failure_reason=str(exc))
            await append(run_id, "scheduler", str(exc))
            await notify("pipeline failed", {"pipeline": pipeline["name"], "run_id": run_id, "duration": duration_seconds(started), "details": str(exc)})
            return

        for name in jobs:
            metadata.upsert_job(run_id, name, "queued")

        pending = set(jobs)
        running: dict[str, asyncio.Task[int]] = {}
        completed: set[str] = set()
        skipped: set[str] = set()
        failed_job: str | None = None
        limit = int(cfg("runner.concurrency", 4))

        while pending or running:
            ready = sorted(
                name for name in pending
                if all(dep in completed for dep in jobs[name].get("needs", []) or [])
            )
            for name in ready[: max(0, limit - len(running))]:
                pending.remove(name)
                metadata.upsert_job(run_id, name, "running", started_at=datetime.now(timezone.utc))
                running[name] = asyncio.create_task(run_job(run_id, name, jobs[name], workspace, token))

            if not running:
                break
            done, _ = await asyncio.wait(running.values(), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                name = next(k for k, v in running.items() if v is task)
                del running[name]
                code = task.result()
                if code == 0:
                    completed.add(name)
                    metadata.upsert_job(run_id, name, "succeeded", finished_at=datetime.now(timezone.utc), exit_code=code)
                else:
                    failed_job = name
                    metadata.upsert_job(run_id, name, "failed", finished_at=datetime.now(timezone.utc), exit_code=code)
                    for dep in sorted(descendants(jobs, name).intersection(pending)):
                        pending.remove(dep)
                        skipped.add(dep)
                        metadata.upsert_job(run_id, dep, "skipped", finished_at=datetime.now(timezone.utc), skipped_reason=f"dependency {name} failed")

        if failed_job:
            metadata.update_run(run_id, "failed", finished_at=datetime.now(timezone.utc), failure_reason=f"job {failed_job} failed")
            await append(run_id, "system", f"pipeline failed: job {failed_job} failed")
            await notify("pipeline failed", {"pipeline": pipeline["name"], "run_id": run_id, "duration": duration_seconds(started), "failing_job": failed_job})
            return

        await publish_artifacts(run_id, pipeline, workspace, publisher)
        metadata.update_run(run_id, "succeeded", finished_at=datetime.now(timezone.utc))
        await append(run_id, "system", "pipeline succeeded")
        await notify("pipeline succeeded", {"pipeline": pipeline["name"], "run_id": run_id, "duration": duration_seconds(started)})
    except Exception as exc:
        metadata.update_run(run_id, "failed", finished_at=datetime.now(timezone.utc), failure_reason=str(exc))
        await append(run_id, "system", f"pipeline failed: {exc}")
        await notify("pipeline failed", {"pipeline": pipeline["name"], "run_id": run_id, "duration": duration_seconds(started), "details": str(exc)})
