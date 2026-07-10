"""Read-through register cache with a TTL.

The clock is injectable so TTL expiry can be tested deterministically without
``sleep``.
"""
from __future__ import annotations

import time
from typing import Callable, Dict, List, Optional, Tuple


class RegisterCache:
    """Maps holding-register address -> (value, timestamp).

    Populated as a side effect of every *live* FC03 response the proxy relays
    (including the EMS/Hero's own polls). Non-priority clients can then be
    answered from here, adding zero load to the single-master dongle.
    """

    def __init__(self, ttl: float = 30.0, clock: Callable[[], float] = time.monotonic):
        self._ttl = ttl
        self._clock = clock
        self._d: Dict[int, Tuple[int, float]] = {}
        self.hits = 0  # stats: number of reads served from cache

    def update(self, start: int, regs: List[int]) -> None:
        now = self._clock()
        for i, v in enumerate(regs):
            self._d[start + i] = (v, now)

    def get_block(self, start: int, qty: int, max_age: Optional[float] = None) -> Optional[List[int]]:
        """Return the values if *all* requested registers are cached and fresh.

        Returns ``None`` on any miss or stale entry, so the caller falls back to
        a live read. ``max_age`` overrides the default TTL (used for the shorter
        hero-debounce window).
        """
        ttl = self._ttl if max_age is None else max_age
        now = self._clock()
        out: List[int] = []
        for addr in range(start, start + qty):
            entry = self._d.get(addr)
            if entry is None or (now - entry[1]) > ttl:
                return None
            out.append(entry[0])
        self.hits += 1
        return out

    def __len__(self) -> int:
        return len(self._d)
