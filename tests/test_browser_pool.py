"""Tests for core/browser_pool.py — BrowserPool singleton."""
import asyncio

import pytest

from core.browser_pool import BrowserPool, browser_pool


@pytest.fixture(autouse=True)
def _clear_pool():
    """Reset BrowserPool class state before each test."""
    BrowserPool._instances.clear()
    yield


@pytest.mark.asyncio
async def test_get_or_create_new_project():
    """get_or_create calls factory for a new project_id."""
    calls = []

    async def factory():
        calls.append(1)
        mgr = object()
        return mgr

    mgr = await BrowserPool.get_or_create(1, factory)
    assert mgr is not None
    assert len(calls) == 1
    assert await BrowserPool.is_active(1)


@pytest.mark.asyncio
async def test_get_or_create_reuses_existing():
    """get_or_create returns the same manager for consecutive calls."""
    mgr1 = object()

    async def factory():
        return mgr1

    first = await BrowserPool.get_or_create(1, factory)
    calls2 = []

    async def factory2():
        calls2.append(1)
        return object()

    second = await BrowserPool.get_or_create(1, factory2)
    assert first is second
    assert len(calls2) == 0  # factory not called


@pytest.mark.asyncio
async def test_get_or_create_different_projects():
    """get_or_create creates separate managers for different projects."""

    async def factory_a():
        return object()

    async def factory_b():
        return object()

    mgr_a = await BrowserPool.get_or_create(1, factory_a)
    mgr_b = await BrowserPool.get_or_create(2, factory_b)
    assert mgr_a is not mgr_b


@pytest.mark.asyncio
async def test_get_or_create_concurrent_lock():
    """get_or_create uses asyncio.Lock to prevent TOCTOU race."""
    started = []

    async def slow_factory():
        started.append(1)
        await asyncio.sleep(0.05)  # simulate slow creation
        return object()

    # Race two concurrent calls for the same project
    results = await asyncio.gather(
        BrowserPool.get_or_create(1, slow_factory),
        BrowserPool.get_or_create(1, slow_factory),
    )
    # Both should return the same instance (second waited for first)
    assert results[0] is results[1]
    assert len(started) == 1  # factory only called once


@pytest.mark.asyncio
async def test_register_new():
    """register stores a new manager."""
    mgr = object()
    await BrowserPool.register(1, mgr)
    assert await BrowserPool.is_active(1)


@pytest.mark.asyncio
async def test_register_overwrite():
    """register overwrites an existing entry."""
    mgr1 = object()
    mgr2 = object()
    await BrowserPool.register(1, mgr1)
    await BrowserPool.register(1, mgr2)
    assert BrowserPool._instances[1] is mgr2


@pytest.mark.asyncio
async def test_is_active_inactive():
    """is_active returns False for unknown project."""
    assert not await BrowserPool.is_active(99)


@pytest.mark.asyncio
async def test_is_active_active():
    """is_active returns True after registration."""
    BrowserPool._instances[1] = object()
    assert await BrowserPool.is_active(1)


@pytest.mark.asyncio
async def test_close_existing_manager():
    """close stops the manager and removes it from pool."""
    stopped = False

    class FakeMgr:
        async def stop(self):
            nonlocal stopped
            stopped = True

    BrowserPool._instances[1] = FakeMgr()
    await BrowserPool.close(1)
    assert stopped
    assert not await BrowserPool.is_active(1)


@pytest.mark.asyncio
async def test_close_nonexistent():
    """close on unknown project is a no-op (warning logged)."""
    await BrowserPool.close(99)  # should not raise


@pytest.mark.asyncio
async def test_close_stop_fails():
    """close logs a warning if stop raises, but still removes from pool."""

    class BrokenMgr:
        async def stop(self):
            raise RuntimeError("boom")

    BrowserPool._instances[1] = BrokenMgr()
    await BrowserPool.close(1)  # should not raise
    assert not await BrowserPool.is_active(1)


def test_module_level_alias():
    """browser_pool is an instance of BrowserPool."""
    assert browser_pool is BrowserPool


@pytest.mark.asyncio
async def test_get_or_create_factory_exception():
    """get_or_create propagates factory exceptions."""
    async def broken_factory():
        raise ValueError("factory failed")

    with pytest.raises(ValueError, match="factory failed"):
        await BrowserPool.get_or_create(1, broken_factory)
    assert not await BrowserPool.is_active(1)


@pytest.mark.asyncio
async def test_register_then_get_or_create():
    """register + get_or_create returns the registered instance."""
    mgr = object()
    await BrowserPool.register(5, mgr)
    result = await BrowserPool.get_or_create(5, lambda: object())
    assert result is mgr


@pytest.mark.asyncio
async def test_get_returns_none_for_unknown():
    """get returns None for a project not in the pool."""
    result = await BrowserPool.get(42)
    assert result is None


@pytest.mark.asyncio
async def test_get_returns_registered_manager():
    """get returns the manager after register."""
    mgr = object()
    await BrowserPool.register(7, mgr)
    result = await BrowserPool.get(7)
    assert result is mgr


@pytest.mark.asyncio
async def test_is_active_returns_false_after_close():
    """is_active returns False after close removes the manager."""
    async def factory():
        return object()
    await BrowserPool.get_or_create(1, factory)
    assert await BrowserPool.is_active(1)
    await BrowserPool.close(1)
    assert not await BrowserPool.is_active(1)
