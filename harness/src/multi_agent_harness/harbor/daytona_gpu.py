"""GPU compatibility for Bespoke SDK a96416d, whose Daytona adapter ignores config.gpu.

Only GPU creation is overridden. Parameter building, networking, TTL, create tokens, orphan
cleanup and error classification stay with the pinned adapter. Remove this module when that
SDK maps Daytona GPU resources itself. No process-global SDK monkeypatch is installed.
"""

from __future__ import annotations

from copy import copy

from bespokelabs.sandbox import AsyncSandboxClient
from bespokelabs.sandbox.backends import daytona as sdk
from bespokelabs.sandbox.exceptions import SandboxConfigurationError
from bespokelabs.sandbox.types import SandboxConfig

DEFAULT_GPU = "any"  # internal SDK request: one GPU with provider-default NVIDIA type


def validate_gpu_type(gpu_type: str | None) -> None:
    from daytona import GpuType, Resources

    if not {"gpu", "gpu_type"} <= Resources.__dataclass_fields__.keys():
        raise ValueError("Daytona SDK lacks GPU resources; install the project's harbor dependencies")
    # The runtime probe currently supports NVIDIA devices only.
    supported = {t.value for t in GpuType if t.value in {
        "H100", "H200", "B200", "B300", "RTX-PRO-6000", "RTX-4090", "RTX-5090"}}
    if gpu_type is not None and gpu_type not in supported:
        raise ValueError(f"unsupported gpu_type {gpu_type!r}; supported NVIDIA types: {', '.join(sorted(supported))}")


class _GpuProvider:
    """Per-create provider view; all lifecycle operations stay in the SDK."""

    def __init__(self, client, gpu_type):
        self.client, self.gpu_type = client, gpu_type

    def __getattr__(self, name):
        return getattr(self.client, name)

    def create(self, params, **kwargs):
        from daytona import GpuType, Resources

        params.resources = params.resources or Resources()
        params.resources.gpu = 1
        params.resources.gpu_type = GpuType(self.gpu_type) if self.gpu_type else None
        return self.client.create(params, **kwargs)


class DaytonaGpuClient(sdk.DaytonaClient):
    def create(self, config: SandboxConfig) -> sdk.DaytonaSession:
        if config.gpu is None:
            return super().create(config)
        gpu_type = None if config.gpu == DEFAULT_GPU else config.gpu
        validate_gpu_type(gpu_type)
        if not (config.image or config.dockerfile) or "resources" in config.backend_options:
            raise SandboxConfigurationError("GPU creation requires an image/Dockerfile and no resources override",
                                            backend="daytona", op="create")
        self._ensure_client()
        # Do not swap the shared client's provider: concurrent CPU/GPU creates must be isolated.
        local = copy(self)
        local._client = _GpuProvider(self._client, gpu_type)
        return sdk.DaytonaClient.create(local, config)


class DaytonaGpuAsyncClient(AsyncSandboxClient):
    async def _get_backend_client(self) -> DaytonaGpuClient:
        # Match the pinned SDK's lazy, thread-safe backend initialization.
        import asyncio

        if self._backend_client is None:
            async with self._init_lock:
                if self._backend_client is None:
                    self._backend_client = await asyncio.to_thread(DaytonaGpuClient)
        return self._backend_client
