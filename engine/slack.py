from __future__ import annotations

from typing import Any

import httpx

from registry.config import cfg


async def notify(event: str, payload: dict[str, Any]) -> None:
    webhook = cfg("slack.webhook_url", "")
    if not webhook:
        return
    tags = cfg("slack.tags", {})
    text = f"*Forge {event}*\n" + "\n".join(f"- {k}: {v}" for k, v in payload.items())
    if tags:
        text += "\n" + " ".join(str(v) for v in tags.values())
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(webhook, json={"text": text})
    except Exception:
        return
