"""Parametrized tests for the neighborhood lock registries.

Covers the seven scenarios from the design doc. The multi-process SQLite
case is intentionally skipped here — the test harness is flaky across OSes
because SQLite's lock reporting depends on filesystem semantics. A
dedicated follow-up PR will bring that coverage in with an OS matrix.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from memu.lane_lock import LaneContestedError
from memu.neighborhood_lock import (
    MarkdownLockRegistry,
    NeighborhoodConflict,
    NeighborhoodLock,
    SqliteLockRegistry,
)
from memu.storage import get_backend


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


async def _make_sqlite_registry(tmp_path: Path):
    backend = get_backend(f"sqlite:///{tmp_path / 'index.db'}")
    await backend.init()
    registry = backend.get_lock_registry()
    await registry.init()
    return registry, backend.close


async def _make_markdown_registry(tmp_path: Path):
    backend = get_backend(f"file://{tmp_path}")
    await backend.init()
    registry = backend.get_lock_registry()
    await registry.init()
    return registry, backend.close


REGISTRY_FACTORIES = {
    "sqlite": _make_sqlite_registry,
    "markdown": _make_markdown_registry,
}


@pytest.fixture(params=sorted(REGISTRY_FACTORIES))
def registry_factory(request):
    return REGISTRY_FACTORIES[request.param]


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def test_scenario_1_basic_acquire_and_release(registry_factory, tmp_path):
    """Single holder can acquire and release cleanly; fence starts at 1."""

    async def go():
        registry, close = await registry_factory(tmp_path)
        try:
            rec = await registry.try_acquire("foo", "holder-a", ttl_s=30)
            assert rec.slug == "foo"
            assert rec.holder_id == "holder-a"
            assert rec.fencing_token == 1

            released = await registry.release("foo", "holder-a", rec.fencing_token)
            assert released is True
            assert await registry.peek("foo") is None
        finally:
            await close()

    _run(go())


def test_scenario_2_concurrent_acquire_conflicts(registry_factory, tmp_path):
    """Second holder gets NeighborhoodConflict while the lease is live."""

    async def go():
        registry, close = await registry_factory(tmp_path)
        try:
            await registry.try_acquire("foo", "holder-a", ttl_s=30)
            with pytest.raises(NeighborhoodConflict):
                await registry.try_acquire("foo", "holder-b", ttl_s=30)
        finally:
            await close()

    _run(go())


def test_scenario_3_conflict_subclasses_lane_contested(
    registry_factory, tmp_path
):
    """Existing lane-lock callers catch neighborhood conflicts too."""

    async def go():
        registry, close = await registry_factory(tmp_path)
        try:
            await registry.try_acquire("foo", "holder-a", ttl_s=30)
            with pytest.raises(LaneContestedError):
                await registry.try_acquire("foo", "holder-b", ttl_s=30)
        finally:
            await close()

    _run(go())


def test_scenario_4_release_enables_reacquire_with_monotonic_fence(
    registry_factory, tmp_path
):
    """After release, re-acquire succeeds and fence advances strictly."""

    async def go():
        registry, close = await registry_factory(tmp_path)
        try:
            first = await registry.try_acquire("foo", "holder-a", ttl_s=30)
            await registry.release("foo", "holder-a", first.fencing_token)

            second = await registry.try_acquire("foo", "holder-b", ttl_s=30)
            assert second.fencing_token > first.fencing_token
            assert second.fencing_token == first.fencing_token + 1
        finally:
            await close()

    _run(go())


def test_scenario_5_expired_lease_is_reclaimable(registry_factory, tmp_path):
    """A lease past its TTL can be reclaimed by a new holder."""

    async def go():
        registry, close = await registry_factory(tmp_path)
        try:
            # Very short TTL, then wait it out.
            rec = await registry.try_acquire("foo", "holder-a", ttl_s=0.05)
            await asyncio.sleep(0.08)
            reclaimed = await registry.try_acquire(
                "foo", "holder-b", ttl_s=30
            )
            assert reclaimed.holder_id == "holder-b"
            assert reclaimed.fencing_token > rec.fencing_token
            # Original holder's stale release must now fail.
            assert (
                await registry.release("foo", "holder-a", rec.fencing_token)
                is False
            )
        finally:
            await close()

    _run(go())


def test_scenario_6_wrong_owner_cannot_release_or_renew(
    registry_factory, tmp_path
):
    """Release/renew are owner-scoped; wrong holder is a no-op / error."""

    async def go():
        registry, close = await registry_factory(tmp_path)
        try:
            rec = await registry.try_acquire("foo", "holder-a", ttl_s=30)
            # Wrong holder id.
            assert (
                await registry.release("foo", "holder-b", rec.fencing_token)
                is False
            )
            # Wrong fencing token.
            assert (
                await registry.release("foo", "holder-a", rec.fencing_token + 99)
                is False
            )
            # Renew by impostor should raise.
            with pytest.raises(NeighborhoodConflict):
                await registry.renew(
                    "foo", "holder-b", rec.fencing_token, ttl_s=30
                )
            # Real owner can still release.
            assert (
                await registry.release("foo", "holder-a", rec.fencing_token)
                is True
            )
        finally:
            await close()

    _run(go())


def test_scenario_7_context_manager_renews_and_multi_lock(
    registry_factory, tmp_path
):
    """NeighborhoodLock acquires root+neighbors, renews in-flight, releases
    everything on exit."""

    async def go():
        registry, close = await registry_factory(tmp_path)
        try:
            holder_lock = NeighborhoodLock(
                registry,
                "root",
                neighbors=["neighbor-a", "neighbor-b"],
                holder_id="worker-1",
                ttl_s=0.4,
                renew_interval_s=0.1,
            )
            async with holder_lock as nl:
                assert nl.fencing_token >= 1
                # All three slugs should be live.
                for slug in ("root", "neighbor-a", "neighbor-b"):
                    rec = await registry.peek(slug)
                    assert rec is not None
                    assert rec.holder_id == "worker-1"

                # A peer attempting any one of the three should be blocked.
                with pytest.raises(NeighborhoodConflict):
                    await registry.try_acquire(
                        "neighbor-a", "worker-2", ttl_s=10
                    )

                # Sleep past the original TTL so renewal must have fired.
                await asyncio.sleep(0.55)
                rec_after = await registry.peek("root")
                assert rec_after is not None
                assert rec_after.holder_id == "worker-1"

            # Exit -> every slug released.
            for slug in ("root", "neighbor-a", "neighbor-b"):
                assert await registry.peek(slug) is None
        finally:
            await close()

    _run(go())


# ---------------------------------------------------------------------------
# Markdown-specific: the fencing_token sidecar is actually honored.
# ---------------------------------------------------------------------------


def test_markdown_honors_fence_sidecar_on_release(tmp_path):
    """Corrupting ``.memu/locks/_fence/<slug>`` should invalidate release."""

    async def go():
        registry, close = await _make_markdown_registry(tmp_path)
        try:
            rec = await registry.try_acquire("paper/attention", "holder-a", ttl_s=30)
            fence_file = tmp_path / ".memu" / "locks" / "_fence" / "paper__attention"
            assert fence_file.exists()
            # Fast-forward the fence counter as if another claim had bumped it.
            fence_file.write_text(str(rec.fencing_token + 5))
            # Release now sees a mismatched fence and refuses to delete.
            assert (
                await registry.release(
                    "paper/attention", "holder-a", rec.fencing_token
                )
                is False
            )
            # Renew with the stale token hits the same check.
            with pytest.raises(NeighborhoodConflict):
                await registry.renew(
                    "paper/attention",
                    "holder-a",
                    rec.fencing_token,
                    ttl_s=30,
                )
        finally:
            await close()

    _run(go())


def test_sqlite_registry_shares_backend_connection(tmp_path):
    """Sanity: the SQLite registry reuses the existing backend connection
    (tables land in the same database file as PR #21's node tables)."""

    async def go():
        backend = get_backend(f"sqlite:///{tmp_path / 'index.db'}")
        await backend.init()
        registry = backend.get_lock_registry()
        assert isinstance(registry, SqliteLockRegistry)
        await registry.init()
        await registry.try_acquire("foo", "holder-a", ttl_s=30)
        row = backend.conn.execute(
            "SELECT slug, holder_id FROM neighborhood_locks"
        ).fetchone()
        assert row["slug"] == "foo"
        assert row["holder_id"] == "holder-a"
        await backend.close()

    _run(go())


def test_markdown_registry_type(tmp_path):
    async def go():
        backend = get_backend(f"file://{tmp_path}")
        await backend.init()
        registry = backend.get_lock_registry()
        assert isinstance(registry, MarkdownLockRegistry)
        await backend.close()

    _run(go())
