"""Pluggable sandbox providers for the Warden.

Provider selection via SANDBOX_PROVIDER env var:
  - "docker"       — Docker containers (default, existing behavior)
  - "e2b"          — E2B Firecracker MicroVMs (50ms boot, hardware isolation)
  - "extism"       — WebAssembly via Extism (in-process, mathematically proven sandbox)
  - "none"         — No-op (dry run / testing)
"""

from memu.sandbox.base import SandboxProvider, SandboxResult
from memu.sandbox.docker_provider import DockerSandboxProvider
from memu.sandbox.e2b_provider import E2BSandboxProvider
from memu.sandbox.extism_provider import ExtismSandboxProvider

import os

def get_sandbox_provider() -> SandboxProvider:
    provider = os.environ.get("SANDBOX_PROVIDER", "docker").lower()
    if provider == "e2b":
        return E2BSandboxProvider()
    elif provider == "extism":
        return ExtismSandboxProvider()
    elif provider == "none":
        from memu.sandbox.noop_provider import NoopSandboxProvider
        return NoopSandboxProvider()
    else:
        return DockerSandboxProvider()

__all__ = ["SandboxProvider", "SandboxResult", "get_sandbox_provider"]
