"""Abstract base class for all sandbox providers."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SandboxResult:
    success: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    execution_time_ms: float = 0.0
    provider: str = "unknown"
    sandbox_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class SandboxProvider(abc.ABC):
    """All sandbox providers implement this interface.
    
    The Warden calls execute() and never touches Docker/E2B/WASM directly.
    """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Provider name for logging/telemetry."""

    @abc.abstractmethod
    async def execute(
        self,
        code: str,
        *,
        language: str = "python",
        timeout_s: int = 30,
        env: dict[str, str] | None = None,
        budget_usd: float = 0.0,
    ) -> SandboxResult:
        """Execute untrusted code and return the result.

        Args:
            code: The code string to execute.
            language: Target language (python, bash, node, etc.)
            timeout_s: Hard kill timeout in seconds.
            env: Environment variables to inject.
            budget_usd: Max compute spend (provider-enforced where supported).

        Returns:
            SandboxResult with stdout, stderr, exit_code, timing.
        """

    @abc.abstractmethod
    async def health(self) -> bool:
        """Return True if the provider is available and healthy."""

    async def close(self) -> None:
        """Optional cleanup hook."""
