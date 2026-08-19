"""Two rate limiters with deliberately different semantics.

Both are per-process, stdlib-only, and keyed by an opaque string so the
caller controls cardinality.

* :class:`TokenBucket` — a **burst budget** with continuous linear refill.
  Used for the per-group and per-sender inbound gates: it absorbs a normal
  conversational burst and only bites when someone is hammering.
* :class:`SlidingWindowCounter` — an **exact hard cap** ("at most N in the
  last M minutes").  Used for the group speech cap, which is a promise to
  the humans in the room about how chatty the bot may be.  A token bucket
  would leak that promise: after a quiet hour it would allow a full burst.

The window and limit are passed *per call* to :class:`SlidingWindowCounter`
rather than stored, so a config change takes effect without rebuilding the
counter — and a module-level instance keeps its recorded timestamps across
an adapter restart, which is the point (a reconnect must not hand the bot a
fresh speech budget).
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional

__all__ = [
    "GC_INTERVAL",
    "GC_STALE_AFTER",
    "SlidingWindowCounter",
    "TokenBucket",
]


#: Bucket entries untouched for this long are pruned by the GC sweeper.
GC_STALE_AFTER: float = 3600.0

#: How often the background sweeper walks the map.
GC_INTERVAL: float = 300.0


@dataclass
class _BucketState:
    """Per-key token-bucket state."""

    tokens: float
    last_refill: float


class TokenBucket:
    """Per-key token bucket with linear refill.

    Prefer :meth:`per_minute` over the raw constructor so the refill
    arithmetic stays in one place.

    >>> b = TokenBucket.per_minute(60)
    >>> b.check("group:1")
    True
    """

    __slots__ = ("_capacity", "_refill_per_sec", "_state")

    def __init__(self, capacity: float, refill_per_sec: float) -> None:
        self._capacity = float(capacity)
        self._refill_per_sec = float(refill_per_sec)
        self._state: Dict[str, _BucketState] = {}

    @classmethod
    def per_minute(cls, per_min: int) -> "TokenBucket":
        """Allow ``per_min`` events per minute per key (burst = per_min)."""
        capacity = float(per_min)
        return cls(capacity=capacity, refill_per_sec=capacity / 60.0)

    @property
    def capacity(self) -> float:
        """Per-key capacity."""
        return self._capacity

    def check(self, key: str) -> bool:
        """Try to consume one token; ``True`` means "allowed".

        A brand-new key starts full.
        """
        now = time.monotonic()
        entry = self._state.get(key)
        if entry is None:
            entry = _BucketState(tokens=self._capacity, last_refill=now)
            self._state[key] = entry
        elapsed = now - entry.last_refill
        if elapsed < 0:
            # The monotonic clock should not regress, but clamp defensively
            # so one weird tick cannot leak negative tokens.
            elapsed = 0.0
        entry.tokens = min(
            entry.tokens + elapsed * self._refill_per_sec, self._capacity
        )
        entry.last_refill = now
        if entry.tokens >= 1.0:
            entry.tokens -= 1.0
            return True
        return False

    def tracked_keys(self) -> int:
        """Number of live keys (tests / metrics)."""
        return len(self._state)

    def sweep_stale(self) -> None:
        """Drop entries idle for longer than :data:`GC_STALE_AFTER`.

        Semantically free: a key that has not spoken in an hour is not
        mid-burst, so it comes back at full capacity.
        """
        cutoff = time.monotonic() - GC_STALE_AFTER
        stale = [k for k, v in self._state.items() if v.last_refill < cutoff]
        for k in stale:
            self._state.pop(k, None)

    def start_gc(
        self,
        cancel: Optional[asyncio.Event] = None,
        interval: float = GC_INTERVAL,
    ) -> "asyncio.Task":
        """Spawn a background task that periodically sweeps stale keys.

        Set ``cancel`` to stop the loop cleanly, or cancel the returned task.
        """
        cancel_event = cancel if cancel is not None else asyncio.Event()

        async def _loop() -> None:
            try:
                # Skip the immediate tick — nothing to sweep yet.
                await self._wait_for_tick(cancel_event, interval)
                while not cancel_event.is_set():
                    self.sweep_stale()
                    await self._wait_for_tick(cancel_event, interval)
            except asyncio.CancelledError:
                return

        return asyncio.create_task(_loop(), name="onebot-token-bucket-gc")

    @staticmethod
    async def _wait_for_tick(cancel: asyncio.Event, interval: float) -> None:
        """Wait ``interval`` seconds, or until ``cancel`` is set."""
        try:
            await asyncio.wait_for(cancel.wait(), timeout=interval)
        except (asyncio.TimeoutError, TimeoutError):
            return


class SlidingWindowCounter:
    """Per-key sliding-window event counter — an exact hard cap.

    Unlike :class:`TokenBucket`, an event recorded at ``t`` stops counting at
    ``t + window_secs``, so "5 per 3 minutes" means literally that.
    """

    __slots__ = ("_events",)

    def __init__(self) -> None:
        self._events: Dict[str, Deque[float]] = {}

    def count(self, key: str, window_secs: float, now: Optional[float] = None) -> int:
        """Live event count for ``key`` inside the trailing window."""
        ts = time.monotonic() if now is None else now
        events = self._events.get(key)
        if not events:
            return 0
        cutoff = ts - window_secs
        while events and events[0] <= cutoff:
            events.popleft()
        if not events:
            self._events.pop(key, None)
            return 0
        return len(events)

    def allow(
        self,
        key: str,
        window_secs: float,
        max_count: int,
        *,
        record: bool = True,
        now: Optional[float] = None,
    ) -> bool:
        """``True`` when ``key`` is under ``max_count`` events in the window.

        ``window_secs <= 0`` or ``max_count <= 0`` disables the cap (always
        allowed, nothing recorded).  ``record=False`` peeks without counting —
        use it to pre-check before expensive work, then :meth:`record` once
        the message actually goes out.
        """
        if window_secs <= 0 or max_count <= 0:
            return True
        ts = time.monotonic() if now is None else now
        if self.count(key, window_secs, now=ts) >= max_count:
            return False
        if record:
            self._events.setdefault(key, deque()).append(ts)
        return True

    def record(self, key: str, now: Optional[float] = None) -> None:
        """Record one event (pairs with ``allow(..., record=False)``)."""
        ts = time.monotonic() if now is None else now
        self._events.setdefault(key, deque()).append(ts)

    def tracked_keys(self) -> int:
        return len(self._events)

    def sweep_stale(self, max_window_secs: float = GC_STALE_AFTER) -> None:
        """Drop keys whose newest event is older than ``max_window_secs``."""
        cutoff = time.monotonic() - max_window_secs
        stale = [k for k, v in self._events.items() if not v or v[-1] < cutoff]
        for k in stale:
            self._events.pop(k, None)
