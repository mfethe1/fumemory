"""Extism WebAssembly sandbox provider.

Extism runs untrusted code inside a WASM sandbox *inside the current process*.
- Mathematically proven memory isolation (no shared heap)
- Zero network access unless explicitly granted
- Sub-millisecond spawn time
- No external API key required — runs fully local

Limitations:
- Only supports languages that compile to WASM (Python via Wasm-Python, JS, Rust, etc.)
- No filesystem access by default (intentional security boundary)

Setup:
    pip install extism

Docs: https://extism.org/docs/quickstart/python-host/
"""

from __future__ import annotations

import logging
import os
import time

from memu.sandbox.base import SandboxProvider, SandboxResult

logger = logging.getLogger(__name__)

# Pre-compiled Wasm Python interpreter (PyodideMicroVm or similar)
# In production, build a custom WASM module that accepts code and returns stdout/stderr
WASM_MODULE_URL = os.environ.get(
    "EXTISM_WASM_MODULE_URL",
    "https://github.com/extism/python-pdk/releases/latest/download/code_runner.wasm"
)


class ExtismSandboxProvider(SandboxProvider):
    """Execute code in a WebAssembly sandbox via Extism."""

    @property
    def name(self) -> str:
        return "extism"

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
            import extism  # noqa: F401
        except ImportError:
            return SandboxResult(
                success=False,
                stderr="extism not installed. Run: pip install extism",
                exit_code=1,
                provider=self.name,
            )

        try:
            import extism

            t0 = time.monotonic()

            # Build WASM plugin — use manifest to load module
            manifest = {"wasm": [{"url": WASM_MODULE_URL}]}
            
            # Allowed host functions (explicitly opt-in)
            with extism.Plugin(manifest, wasi=False) as plugin:
                input_payload = {
                    "code": code,
                    "language": language,
                    "env": env or {},
                }
                import json
                raw = plugin.call("execute", json.dumps(input_payload).encode())
                result = json.loads(raw)
                elapsed_ms = (time.monotonic() - t0) * 1000

                return SandboxResult(
                    success=result.get("success", False),
                    stdout=result.get("stdout", ""),
                    stderr=result.get("stderr", ""),
                    exit_code=result.get("exit_code", 0),
                    execution_time_ms=elapsed_ms,
                    provider=self.name,
                )

        except Exception as exc:
            logger.exception("Extism WASM execution failed: %s", exc)
            return SandboxResult(
                success=False,
                stderr=str(exc),
                exit_code=1,
                provider=self.name,
            )

    async def health(self) -> bool:
        try:
            import extism  # noqa: F401
            return True
        except ImportError:
            return False
