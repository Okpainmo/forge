from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

from registry.config import cfg

subscribers: dict[str, set[asyncio.Queue[str]]] = {}


def log_dir() -> Path:
    path = Path(cfg("storage.log_dir", "/var/lib/forge/logs"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_path(run_id: str) -> Path:
    return log_dir() / f"{run_id}.jsonl"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def append(run_id: str, job: str, line: str) -> None:
    event = {"ts": now_iso(), "job": job, "line": line.rstrip("\n")}
    encoded = json.dumps(event, sort_keys=True)
    with log_path(run_id).open("a", encoding="utf-8") as handle:
        handle.write(encoded + "\n")
        handle.flush()
    for queue in list(subscribers.get(run_id, set())):
        await queue.put(encoded)


async def stream(run_id: str, follow: bool) -> AsyncIterator[str]:
    path = log_path(run_id)
    if path.exists():
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                yield f"data: {line.strip()}\n\n"
                await asyncio.sleep(0)
    if not follow:
        return
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1000)
    subscribers.setdefault(run_id, set()).add(queue)
    try:
        while True:
            line = await queue.get()
            yield f"data: {line}\n\n"
    finally:
        subscribers.get(run_id, set()).discard(queue)
