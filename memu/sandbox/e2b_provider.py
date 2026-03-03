"""E2B Firecracker MicroVM sandbox provider.

E2B wraps AWS Firecracker for:
- 50ms cold boot (vs 2-5s Docker)
- Hardware-level VM isolation (no container escape vectors)
- Per-execution billing ($0.000024/second compute)

Setup:
    pip install e2b-code-interpreter
    export E2B_API_KEY=e2b_...

Docs: https://e2b.dev/docs
"""

from __future__ import annotations

import logging
import os
import time

from memu.sandbox.base import SandboxProvider, SandboxResult

logger = logging.getLogger(__name__)

E2B_API_KEY = os.environ.get("E2B_API_KEY", "")


class E2BSandboxProvider(SandboxProvider):
    """Execute code in E2B Firecracker MicroVMs."""

    @property
    def name(self) -> str:
        return "e2b"

    async def execute(
        self,
        code: str,
        *,
        language: str = "python",
        timeout_s: int = 30,
        env: dict[str, str] | None = None,
        budget_usd: float = 0.0,
    ) -> SandboxResult:
        if not E2B_API_KEY:
            return SandboxResult(
                success=False,
                stderr="E2B_API_KEY not configured",
                exit_code=1,
                provider=self.name,
            )

        try:
            # Import here so the rest of the system works without e2b installed
            from e2b_code_interpreter import Sandbox

            t0 = time.monotonic()
            sbx = Sandbox(api_key=E2B_API_KEY, timeout=timeout_s)

            # Inject env vars
            if env:
                env_exports = "\n".join(f'import os; os.environ["{k}"] = "{v}"' for k, v in env.items())
                code = env_exports + "\n" + code

            execution = sbx.run_code(code)
            elapsed_ms = (time.monotonic() - t0) * 1000
            sbx.kill()

            stdout = "\n".join(str(r) for r in (execution.results or []))
            stderr = "\n".join(execution.error.traceback if execution.error else [])
            success = execution.error is None

            logger.info("E2B execution done in %.0fms success=%s", elapsed_ms, success)

            return SandboxResult(
                success=success,
                stdout=stdout,
                stderr=stderr,
                exit_code=0 if success else 1,
                execution_time_ms=elapsed_ms,
                provider=self.name,
                sandbox_id=sbx.sandbox_id if hasattr(sbx, "sandbox_id") else None,
            )

        except ImportError:
            return SandboxResult(
                success=False,
                stderr="e2b-code-interpreter not installed. Run: pip install e2b-code-interpreter",
                exit_code=1,
                provider=self.name,
            )
        except Exception as exc:
            logger.exception("E2B execution failed: %s", exc)
            return SandboxResult(
                success=False,
                stderr=str(exc),
                exit_code=1,
                provider=self.name,
            )

    async def health(self) -> bool:
        if not E2B_API_KEY:
            return False
        try:
            from e2b_code_interpreter import Sandbox  # noqa: F401
            return True
        except ImportError:
            return False
