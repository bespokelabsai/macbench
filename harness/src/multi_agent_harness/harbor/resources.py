"""The team's fixed GPU allocation; independent of provider and logical agent identities."""

from __future__ import annotations

import re
from typing import Any


def integer(value: Any, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not re.fullmatch(r"[0-9]+", str(value)):
        raise ValueError(f"{name} must be an integer >= {minimum}")
    result = int(value)
    if result < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return result


def gpu_plan(n_agents: int, n_gpus: int = 0, gpu_type: str | None = None) -> dict[str, Any]:
    n_agents, n_gpus = integer(n_agents, "n_agents", 1), integer(n_gpus, "n_gpus")
    if n_gpus > n_agents:
        raise ValueError("n_gpus must be <= n_agents (one GPU per selected agent)")
    if gpu_type is not None:
        if not isinstance(gpu_type, str) or not gpu_type.strip():
            raise ValueError("gpu_type must be a nonempty GPU model name")
        gpu_type = gpu_type.strip().upper()
        if not n_gpus:
            raise ValueError("gpu_type requires n_gpus > 0")
    return {"version": 1, "n_gpus": n_gpus, "gpu_type": gpu_type,
            "slots": {f"agent-{i}": int(i > n_agents - n_gpus) for i in range(1, n_agents + 1)}}
