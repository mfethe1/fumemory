"""Core symbols used by tiny_pkg consumers."""
from __future__ import annotations

from . import utils as u
from ._private import hidden_helper


class Greeter:
    """Produces greetings with configurable enthusiasm."""

    def __init__(self, name: str, *, loud: bool = False) -> None:
        self.name = name
        self.loud = loud

    def greet(self) -> str:
        """Return a greeting string."""
        raw = self._render()
        if self.loud:
            return raw.upper()
        return raw

    def _render(self) -> str:
        return u.format_hello(self.name)


def make_greeter(name: str) -> Greeter:
    """Factory: build a :class:`Greeter` with a normalized name."""
    normalized = hidden_helper(name)
    return Greeter(normalized)
