"""Rate-limiter tests for the OneBot (QQ) platform plugin.

Two limiters with deliberately different semantics:

* :class:`TokenBucket` — a burst budget with linear refill (inbound gates).
* :class:`SlidingWindowCounter` — an exact hard cap (the group speech
  budget). The distinction matters: a token bucket would let the bot dump a
  full burst into a group after a quiet hour, which is exactly the behaviour
  "at most 5 messages per 3 minutes" is meant to prevent.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

import pytest

from plugins.platforms.onebot.rate_limit import (
    GC_STALE_AFTER,
    SlidingWindowCounter,
    TokenBucket,
)


class TestTokenBucket:

    def test_allows_within_capacity(self):
        b = TokenBucket.per_minute(20)
        assert all(b.check("g:1") for _ in range(20))

    def test_denies_when_empty(self):
        b = TokenBucket.per_minute(20)
        for _ in range(20):
            b.check("g:1")
        assert b.check("g:1") is False

    def test_isolates_keys(self):
        b = TokenBucket.per_minute(3)
        for _ in range(3):
            assert b.check("a")
        assert b.check("a") is False
        for _ in range(3):
            assert b.check("b")

    def test_capacity_caps_refill(self):
        """An idle key must not accumulate a giant burst."""
        b = TokenBucket.per_minute(5)
        assert b.check("k")
        state = b._state["k"]
        state.last_refill = time.monotonic() - 3600.0
        state.tokens = 0.0
        assert b.check("k")
        assert abs(b._state["k"].tokens - (b.capacity - 1.0)) < 1e-6

    def test_refills_over_time(self):
        b = TokenBucket.per_minute(60)  # 1 token/sec
        for _ in range(60):
            b.check("g:1")
        assert b.check("g:1") is False
        b._state["g:1"].last_refill = time.monotonic() - 1.1
        assert b.check("g:1")

    def test_clock_regression_does_not_leak_tokens(self):
        b = TokenBucket.per_minute(2)
        assert b.check("k")
        b._state["k"].last_refill = time.monotonic() + 60.0  # "future"
        assert b.check("k")
        assert b.check("k") is False


class TestTokenBucketSweep:

    def test_sweep_drops_stale_keys(self):
        b = TokenBucket.per_minute(5)
        b.check("idle")
        b._state["idle"].last_refill = time.monotonic() - GC_STALE_AFTER - 1.0
        b.sweep_stale()
        assert b.tracked_keys() == 0

    def test_sweep_keeps_fresh_keys(self):
        b = TokenBucket.per_minute(5)
        b.check("fresh")
        b.sweep_stale()
        assert b.tracked_keys() == 1

    @pytest.mark.asyncio
    async def test_start_gc_sweeps_periodically(self):
        b = TokenBucket.per_minute(5)
        b.check("idle")
        b._state["idle"].last_refill = time.monotonic() - GC_STALE_AFTER - 1.0
        cancel = asyncio.Event()
        task = b.start_gc(cancel=cancel, interval=0.02)
        try:
            for _ in range(50):
                await asyncio.sleep(0.02)
                if b.tracked_keys() == 0:
                    break
            assert b.tracked_keys() == 0
        finally:
            cancel.set()
            await asyncio.wait_for(task, timeout=1.0)

    @pytest.mark.asyncio
    async def test_cancel_event_stops_loop_cleanly(self):
        b = TokenBucket.per_minute(5)
        cancel = asyncio.Event()
        task = b.start_gc(cancel=cancel, interval=0.05)
        await asyncio.sleep(0.01)
        cancel.set()
        await asyncio.wait_for(task, timeout=1.0)
        assert task.done() and task.exception() is None

    @pytest.mark.asyncio
    async def test_task_cancel_also_stops_loop(self):
        b = TokenBucket.per_minute(5)
        task = b.start_gc(interval=0.05)
        await asyncio.sleep(0.01)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1.0)
        assert task.done()


class TestSlidingWindowCounter:

    def test_allows_up_to_max_then_blocks(self):
        c = SlidingWindowCounter()
        assert c.allow("g", 600.0, 2, now=100.0)
        assert c.allow("g", 600.0, 2, now=101.0)
        assert not c.allow("g", 600.0, 2, now=102.0)
        assert c.count("g", 600.0, now=102.0) == 2

    def test_window_actually_slides(self):
        c = SlidingWindowCounter()
        assert c.allow("g", 60.0, 1, now=0.0)
        assert not c.allow("g", 60.0, 1, now=59.0)
        assert c.allow("g", 60.0, 1, now=61.0)

    def test_production_shape_five_per_three_minutes(self):
        """The configured production cap, exercised end to end."""
        c = SlidingWindowCounter()
        window, cap = 180.0, 5
        for i in range(5):
            assert c.allow("inst:183287894", window, cap, now=float(i))
        assert not c.allow("inst:183287894", window, cap, now=5.0)
        # The first message ages out at t=180.
        assert c.allow("inst:183287894", window, cap, now=181.0)

    def test_zero_window_or_zero_max_disables(self):
        c = SlidingWindowCounter()
        for _ in range(10):
            assert c.allow("g", 0.0, 5)
            assert c.allow("g", 600.0, 0)
        assert c.tracked_keys() == 0  # a disabled cap records nothing

    def test_peek_without_record_then_record(self):
        c = SlidingWindowCounter()
        assert c.allow("g", 600.0, 1, record=False, now=0.0)
        assert c.count("g", 600.0, now=0.0) == 0
        c.record("g", now=0.0)
        assert not c.allow("g", 600.0, 1, record=False, now=1.0)

    def test_keys_are_independent(self):
        c = SlidingWindowCounter()
        assert c.allow("a", 600.0, 1, now=0.0)
        assert c.allow("b", 600.0, 1, now=0.0)
        assert not c.allow("a", 600.0, 1, now=1.0)

    def test_sweep_stale_prunes_old_keys(self):
        c = SlidingWindowCounter()
        c.record("old", now=time.monotonic() - 2 * GC_STALE_AFTER)
        c.record("new")
        c.sweep_stale()
        assert c.tracked_keys() == 1
