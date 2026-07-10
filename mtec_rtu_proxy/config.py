"""Runtime configuration, sourced from environment variables.

No real host addresses live in code. Defaults are deliberately generic
(loopback / documentation ranges); real values come from the environment
(see ``.env.example``).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import FrozenSet, Mapping, Optional


@dataclass(frozen=True)
class Config:
    listen_host: str = "0.0.0.0"
    listen_port: int = 502
    dongle_host: str = "127.0.0.1"
    dongle_port: int = 502
    hero_ips: FrozenSet[str] = frozenset()
    txn_timeout: float = 3.0        # per-transaction upstream timeout (seconds)
    cache_ttl: float = 30.0         # how long a cached register stays servable
    reconnect_backoff: float = 3.0  # wait before reconnecting a dead upstream
    connect_settle: float = 2.0     # pause after connect before first request
    dongle_unit: Optional[int] = None  # force this Modbus unit id upstream (None = pass client's through)
    log_level: str = "INFO"            # set DEBUG to log every request/reply with decoded values
    min_request_interval: float = 0.0  # min seconds between upstream requests (0 = no throttle)
    hero_cache_ttl: float = 0.0        # if >0, also serve hero reads from cache within this window (debounce)
    stats_interval: float = 60.0       # log a stats summary every N seconds (0 = off)
    cache_jitter: float = 0.0          # random 0..N s added per cache write to de-sync block expiries

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Config":
        heroes = env.get("HERO_IPS", "")
        du = env.get("DONGLE_UNIT", "").strip()
        return cls(
            listen_host=env.get("LISTEN_HOST", "0.0.0.0"),
            listen_port=int(env.get("LISTEN_PORT", "502")),
            dongle_host=env.get("DONGLE_HOST", "127.0.0.1"),
            dongle_port=int(env.get("DONGLE_PORT", "502")),
            hero_ips=frozenset(x.strip() for x in heroes.split(",") if x.strip()),
            txn_timeout=float(env.get("TXN_TIMEOUT", "3.0")),
            cache_ttl=float(env.get("CACHE_TTL", "30.0")),
            reconnect_backoff=float(env.get("RECONNECT_BACKOFF", "3.0")),
            connect_settle=float(env.get("CONNECT_SETTLE", "2.0")),
            dongle_unit=int(du) if du else None,
            log_level=env.get("LOG_LEVEL", "INFO").upper(),
            min_request_interval=float(env.get("MIN_REQUEST_INTERVAL", "0.0")),
            hero_cache_ttl=float(env.get("HERO_CACHE_TTL", "0.0")),
            stats_interval=float(env.get("STATS_INTERVAL", "60.0")),
            cache_jitter=float(env.get("CACHE_JITTER", "0.0")),
        )
