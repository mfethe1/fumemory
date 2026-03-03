"""Docker sandbox provider — existing behavior, refactored to the SandboxProvider interface.

This is the default fallback. Existing warden_runtime.py Docker calls
will migrate to this provider so the Warden stays provider-agnostic.
"""

from __future__ import annotations

import logging
import os
import time

from memu.sandbox.base import SandboxProvider, SandboxResult

logger = logging.getLogger(__name__)

SANDBOX_IMAGE = os.environ.get("WARDEN_SANDBOX_IMAGE", "python:3.11-alpine")
SANDBOX_TIMEOUT_S = int(os.environ.get("WARDEN_SANDBOX_TIMEOUT", "30"))


class DockerSandboxProvider(SandboxProvider):
    """Execute code in an air-gapped Docker container (existing behavior)."""

    @property
    def name(self) -> str:
        return "docker"

    async def execute(
        self,
        code: str,
        *,
        language: str = "python",
        timeout_s: int = 30,
        env: dict[str, str] | None = None,
        budget_usd: float = 0.0,
    ) -> SandboxResult:
        try:
            import docker

            t0 = time.monotonic()
            client = docker.from_env()

            # Air-gapped: --network none, no volumes
            container = client.containers.run(
                image=SANDBOX_IMAGE,
                command=["python3", "-c", code],
                detach=False,
                remove=True,
                network_mode="none",
                mem_limit="128m",
                cpu_period=100000,
                cpu_quota=50000,  # 50% CPU
                timeout=timeout_s,
                environment=env or {},
                stdout=True,
                stderr=True,
            )

            elapsed_ms = (time.monotonic() - t0) * 1000
            output = container.decode("utf-8") if isinstance(container, bytes) else str(container)

            return SandboxResult(
                success=True,
                stdout=output,
                exit_code=0,
                execution_time_ms=elapsed_ms,
                provider=self.name,
            )

        except ImportError:
            return SandboxResult(
                success=False,
                stderr="docker SDK not installed",
                exit_code=1,
                provider=self.name,
            )
        except Exception as exc:
            logger.exception("Docker sandbox execution failed: %s", exc)
            return SandboxResult(
                success=False,
                stderr=str(exc),
                exit_code=1,
                provider=self.name,
            )

    async def health(self) -> bool:
        try:
            import docker
            client = docker.from_env()
            client.ping()
            return True
        except Exception:
            return False
