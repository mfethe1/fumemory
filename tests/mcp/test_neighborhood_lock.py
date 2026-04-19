from __future__ import annotations

import asyncio

import pytest

from memu.mcp.neighborhood_lock import InProcessLockBackend, NeighborhoodLock


def test_single_slug_acquire_release():
    async def go():
        backend = InProcessLockBackend()
        async with NeighborhoodLock(backend, ["a"]):
            pass
        # Lock should be free for re-acquisition.
        async with NeighborhoodLock(backend, ["a"]):
            pass

    asyncio.run(go())


def test_serializes_concurrent_writers_on_same_slug():
    """Two tasks contending on 'a' must complete in strict sequence, not interleave."""

    async def go():
        backend = InProcessLockBackend()
        events: list[str] = []

        async def worker(tag: str):
            async with NeighborhoodLock(backend, ["a"]):
                events.append(f"{tag}:enter")
                # Yield so the scheduler would interleave if the lock didn't hold.
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                events.append(f"{tag}:exit")

        await asyncio.gather(worker("x"), worker("y"))
        # enter/exit pairs must be contiguous for each tag.
        assert events[0].endswith(":enter")
        assert events[1].endswith(":exit")
        assert events[0].split(":")[0] == events[1].split(":")[0]
        assert events[2].endswith(":enter")
        assert events[3].endswith(":exit")
        assert events[2].split(":")[0] == events[3].split(":")[0]

    asyncio.run(go())


def test_disjoint_neighborhoods_run_concurrently():
    async def go():
        backend = InProcessLockBackend()
        inside_a = asyncio.Event()
        inside_b = asyncio.Event()

        async def holder(slug: str, entered: asyncio.Event, other: asyncio.Event):
            async with NeighborhoodLock(backend, [slug]):
                entered.set()
                await asyncio.wait_for(other.wait(), timeout=1.0)

        # If locks on disjoint slugs didn't run concurrently, wait_for would time out.
        await asyncio.gather(
            holder("a", inside_a, inside_b),
            holder("b", inside_b, inside_a),
        )

    asyncio.run(go())


def test_sorted_acquisition_prevents_deadlock():
    """Two tasks requesting overlapping neighborhoods in reversed input order
    must still both complete — NeighborhoodLock sorts internally."""

    async def go():
        backend = InProcessLockBackend()

        async def w1():
            async with NeighborhoodLock(backend, ["a", "b"]):
                await asyncio.sleep(0)

        async def w2():
            async with NeighborhoodLock(backend, ["b", "a"]):
                await asyncio.sleep(0)

        await asyncio.wait_for(asyncio.gather(w1(), w2()), timeout=1.0)

    asyncio.run(go())


def test_releases_on_exception():
    async def go():
        backend = InProcessLockBackend()

        with pytest.raises(RuntimeError):
            async with NeighborhoodLock(backend, ["a", "b"]):
                raise RuntimeError("boom")

        # Both slugs must be free now.
        await asyncio.wait_for(
            NeighborhoodLock(backend, ["a", "b"]).__aenter__(), timeout=0.5
        )

    asyncio.run(go())


def test_empty_and_duplicate_slugs():
    async def go():
        backend = InProcessLockBackend()
        # Empty neighborhood is a no-op.
        async with NeighborhoodLock(backend, []):
            pass
        # Duplicates collapse to a single lock.
        async with NeighborhoodLock(backend, ["a", "a", "a"]):
            pass
        # Empty-string slugs are dropped.
        async with NeighborhoodLock(backend, ["", "a"]):
            pass

    asyncio.run(go())
