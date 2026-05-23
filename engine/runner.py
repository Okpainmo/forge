from __future__ import annotations

import asyncio
from pathlib import Path
import shlex
import tarfile
from typing import Any

import docker
import httpx

from engine.logs import append
from registry.config import cfg


def workspace_root() -> Path:
    root = Path(cfg("storage.workspace_dir", "/var/lib/forge/workspaces"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def memory_to_bytes(raw: str) -> int:
    raw = raw.strip()
    units = {"Mi": 1024**2, "Gi": 1024**3, "M": 1000**2, "G": 1000**3}
    for suffix, mult in units.items():
        if raw.endswith(suffix):
            return int(float(raw[: -len(suffix)]) * mult)
    return int(raw)


def make_workspace(run_id: str) -> Path:
    path = workspace_root() / run_id
    path.mkdir(parents=True, exist_ok=True)
    (path / "deps").mkdir(exist_ok=True)
    return path


def safe_extract_tar(archive: Path, dest: Path) -> None:
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            if member.issym() or member.islnk():
                raise IntegrityError(dest.name, "unknown", "no tar links", member.name)
            target = (dest / member.name).resolve()
            try:
                target.relative_to(dest.resolve())
            except ValueError:
                raise IntegrityError(dest.name, "unknown", "safe tar path", str(target)) from None
        tar.extractall(dest)


async def pull_dependencies(run_id: str, workspace: Path, lockfile: dict[str, Any]) -> None:
    base_url = cfg("runner.registry_url", cfg("server.public_url", "http://localhost:8080")).rstrip("/")
    async with httpx.AsyncClient(timeout=None) as client:
        for dep in lockfile.get("dependencies", []):
            url = f"{base_url}/artifacts/{dep['name']}/{dep['version']}"
            dest = workspace / "deps" / dep["name"]
            dest.mkdir(parents=True, exist_ok=True)
            archive = dest / "artifact.tar.gz"
            hasher = __import__("hashlib").sha256()
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                with archive.open("wb") as handle:
                    async for chunk in resp.aiter_bytes():
                        hasher.update(chunk)
                        handle.write(chunk)
            actual = hasher.hexdigest()
            if actual != dep["sha256"]:
                await append(run_id, "resolver", f"integrity failure for {dep['name']}@{dep['version']}: expected {dep['sha256']}, actual {actual}")
                raise IntegrityError(dep["name"], dep["version"], dep["sha256"], actual)
            try:
                await asyncio.to_thread(safe_extract_tar, archive, dest)
            except tarfile.TarError:
                await append(run_id, "resolver", f"pulled {dep['name']}@{dep['version']} as raw blob")
            else:
                await append(run_id, "resolver", f"extracted {dep['name']}@{dep['version']} into deps/{dep['name']}")
            await append(run_id, "resolver", f"pulled {dep['name']}@{dep['version']} sha256:{actual}")


class IntegrityError(Exception):
    def __init__(self, name: str, version: str, expected: str, actual: str):
        super().__init__(f"{name}@{version}: expected {expected}, actual {actual}")
        self.name = name
        self.version = version
        self.expected = expected
        self.actual = actual


def shell_script(steps: list[dict[str, str]]) -> str:
    lines = ["set -eu"]
    for step in steps:
        lines.append(f"echo {shlex.quote('--- step: ' + step['name'])}")
        lines.append(step["run"])
    return "\n".join(lines) + "\n"


async def run_job(run_id: str, job_name: str, job: dict[str, Any], workspace: Path, token: str | None) -> int:
    client = docker.from_env()
    timeout = int(job.get("timeout_seconds") or cfg("runner.default_timeout_seconds", 1800))
    resources = job["resources"]
    script_path = workspace / f".forge-{job_name}.sh"
    script_path.write_text(shell_script(job["steps"]), encoding="utf-8")
    env = {
        "FORGE_URL": cfg("runner.registry_url", cfg("server.public_url", "http://localhost:8080")),
        "FORGE_TOKEN": token or "",
    }
    await append(run_id, job_name, f"starting container {job['runtime']}")

    def create_container():
        return client.containers.run(
            job["runtime"],
            ["sh", f"/workspace/.forge-{job_name}.sh"],
            detach=True,
            working_dir="/workspace",
            volumes={str(workspace): {"bind": "/workspace", "mode": "rw"}},
            environment=env,
            network=cfg("runner.docker_network", "forge_internal"),
            mem_limit=memory_to_bytes(resources["memory"]),
            nano_cpus=int(float(resources["cpu"]) * 1_000_000_000),
            pids_limit=256,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
        )

    container = await asyncio.to_thread(create_container)
    try:
        async def pump_logs() -> None:
            queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=1000)

            def collect() -> None:
                try:
                    for chunk in container.logs(stream=True, stdout=True, stderr=True, follow=True):
                        asyncio.run_coroutine_threadsafe(queue.put(chunk), loop).result()
                finally:
                    asyncio.run_coroutine_threadsafe(queue.put(None), loop).result()

            loop = asyncio.get_running_loop()
            collector = asyncio.to_thread(collect)
            collector_task = asyncio.create_task(collector)
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                text = chunk.decode("utf-8", errors="replace")
                for line in text.splitlines():
                    await append(run_id, job_name, line)
            await collector_task

        log_task = asyncio.create_task(pump_logs())
        try:
            result = await asyncio.wait_for(asyncio.to_thread(container.wait), timeout=timeout)
        except asyncio.TimeoutError:
            await asyncio.to_thread(container.kill)
            await append(run_id, job_name, f"job timed out after {timeout}s")
            try:
                await asyncio.wait_for(log_task, timeout=5)
            except asyncio.TimeoutError:
                log_task.cancel()
            return 124
        await log_task
        await asyncio.to_thread(container.reload)
        state = container.attrs.get("State", {})
        if state.get("OOMKilled"):
            await append(run_id, job_name, "container was OOMKilled by memory limit")
        return int(result.get("StatusCode", 1))
    finally:
        try:
            await asyncio.to_thread(container.remove, force=True)
        except Exception:
            pass
