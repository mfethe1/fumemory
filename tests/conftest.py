from __future__ import annotations

"""Shared pytest fixtures and test-runtime bootstrap for memU tests.

This file intentionally keeps import-path setup local to the test suite so
callers do not need to manually export PYTHONPATH before running pytest.
"""

from collections import deque
from pathlib import Path
from typing import Callable

import httpx
import pytest
import sys

# Ensure local project imports work from any working directory.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class DeterministicHttpResponseInjector:
    """Deterministic HTTP response injector with bounded sequence semantics."""

    def __init__(self, sequence: list[tuple[int, dict[str, object] | list[object] | str]]):
        if not sequence:
            raise ValueError("sequence must contain at least one response")
        self._responses = deque(sequence)
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, body = self._responses.popleft()
        headers = {"content-type": "application/json"}
        return httpx.Response(status_code=status, json=body, headers=headers, request=request)

    @property
    def request_count(self) -> int:
        return len(self.requests)


@pytest.fixture
def deterministic_429_injector() -> Callable[[list[tuple[int, dict[str, object] | list[object] | str]]], tuple[httpx.MockTransport, DeterministicHttpResponseInjector]]:
    """Build a deterministic mock transport that replays a bounded status sequence."""

    def _build(sequence: list[tuple[int, dict[str, object] | list[object] | str]]) -> tuple[httpx.MockTransport, DeterministicHttpResponseInjector]:
        injector = DeterministicHttpResponseInjector(sequence)
        return httpx.MockTransport(injector.handler), injector

    return _build
