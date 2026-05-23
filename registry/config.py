import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


@lru_cache
def load_config() -> dict[str, Any]:
    path = Path(os.environ.get("FORGE_CONFIG", "config.yaml"))
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def cfg(path: str, default: Any = None) -> Any:
    cur: Any = load_config()
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur
