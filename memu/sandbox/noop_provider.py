"""No-op sandbox provider for testing / dry-run mode."""
from memu.sandbox.base import SandboxProvider, SandboxResult


class NoopSandboxProvider(SandboxProvider):
    @property
    def name(self) -> str:
        return "noop"

    async def execute(self, code: str, **kwargs) -> SandboxResult:
        return SandboxResult(
            success=True,
            stdout=f"[NOOP] Would execute {len(code)} chars of code",
            provider=self.name,
        )

    async def health(self) -> bool:
        return True
