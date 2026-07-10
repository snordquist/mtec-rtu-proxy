"""Read-through register cache with a TTL.

The clock is injectable so TTL expiry can be tested deterministically without
``sleep``.
"""
from __future__ import annotations

import random
import time
from typing import Callable, Dict, List, Optional, Tuple


class RegisterCache:
    """Maps holding-register address -> (value, timestamp).

    Populated as a side effect of every *live* FC03 response the proxy relays
    (including the EMS/priority's own polls). Non-priority clients can then be
    answered from here, adding zero load to the single-master upstream.

    ``jitter`` back-dates each write's timestamp by a random 0..jitter seconds so
    that blocks cached together in the priority's synchronized poll burst expire at
    *staggered* times -- turning the sawtooth "all blocks refresh at once" load
    spike into a steady trickle (gentler on the fragile upstream). Back-dating
    (not forward-dating) keeps the effective TTL in ``ttl-jitter .. ttl`` so a
    cached value is never served *older* than the configured ``ttl``. ``rand`` is
    injectable for deterministic tests.
    """

    _MAX_ENTRIES = 8192  # guard against unbounded growth from a scanning/hostile client

    def __init__(self, ttl: float = 30.0, clock: Callable[[], float] = time.monotonic,
                 jitter: float = 0.0, rand: Callable[[], float] = random.random):
        self._ttl = ttl
        self._clock = clock
        self._jitter = jitter
        self._rand = rand
        self._d: Dict[int, Tuple[int, float]] = {}
        self.hits = 0  # stats: number of reads served from cache

    def update(self, start: int, regs: List[int]) -> None:
        now = self._clock()
        if self._jitter:
            now -= self._jitter * self._rand()  # back-date to stagger expiry (never extends TTL)
        for i, v in enumerate(regs):
            self._d[start + i] = (v, now)
        if len(self._d) > self._MAX_ENTRIES:
            self._evict_oldest()

    def invalidate(self, start: int, qty: int) -> None:
        """Drop cached entries for a written register range (post-write coherence)."""
        for addr in range(start, start + qty):
            self._d.pop(addr, None)

    def get_block(self, start: int, qty: int, max_age: Optional[float] = None) -> Optional[List[int]]:
        """Return the values if *all* requested registers are cached and fresh.

        Returns ``None`` on any miss, stale entry, or non-positive ``qty`` (a
        qty<=0 read must fall through to a live request/exception, not become a
        vacuous empty "hit"), so the caller falls back to a live read. ``max_age``
        overrides the default TTL (used for the shorter priority-debounce window).
        """
        if qty <= 0:
            return None
        ttl = self._ttl if max_age is None else max_age
        if ttl <= 0:
            return None  # caching disabled (CACHE_TTL=0) -> always a live read
        now = self._clock()
        out: List[int] = []
        for addr in range(start, start + qty):
            entry = self._d.get(addr)
            if entry is None:
                return None
            if (now - entry[1]) > ttl:
                self._d.pop(addr, None)  # purge stale entry on miss
                return None
            out.append(entry[0])
        self.hits += 1
        return out

    def _evict_oldest(self) -> None:
        # Drop the oldest ~10% by timestamp; cheap amortised cap on dict size.
        drop = max(1, len(self._d) // 10)
        for addr in sorted(self._d, key=lambda a: self._d[a][1])[:drop]:
            del self._d[addr]

    def __len__(self) -> int:
        return len(self._d)
