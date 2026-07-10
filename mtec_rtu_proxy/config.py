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
    write_ips: FrozenSet[str] = frozenset()  # IPs allowed to send FC06/FC16 writes (empty = allow all + warn)
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
        def _num(key: str, default: str, cast):
            raw = env.get(key, default)
            try:
                return cast(raw)
            except (TypeError, ValueError):
                raise ValueError(
                    f"invalid {key}={raw!r}: expected a {cast.__name__}"
                ) from None

        def _ips(key: str) -> "FrozenSet[str]":
            return frozenset(x.strip() for x in env.get(key, "").split(",") if x.strip())

        du = env.get("DONGLE_UNIT", "").strip()
        hero_ips = _ips("HERO_IPS")
        # Writes default to the hero(s) -- the controller is the natural writer. Add
        # other legitimate writers (e.g. Home Assistant switches) via WRITE_IPS.
        write_ips = _ips("WRITE_IPS") or hero_ips
        return cls(
            listen_host=env.get("LISTEN_HOST", "0.0.0.0"),
            listen_port=_num("LISTEN_PORT", "502", int),
            dongle_host=env.get("DONGLE_HOST", "127.0.0.1"),
            dongle_port=_num("DONGLE_PORT", "502", int),
            hero_ips=hero_ips,
            write_ips=write_ips,
            txn_timeout=_num("TXN_TIMEOUT", "3.0", float),
            cache_ttl=_num("CACHE_TTL", "30.0", float),
            reconnect_backoff=_num("RECONNECT_BACKOFF", "3.0", float),
            connect_settle=_num("CONNECT_SETTLE", "2.0", float),
            dongle_unit=int(du) if du else None,
            log_level=env.get("LOG_LEVEL", "INFO").upper(),
            min_request_interval=_num("MIN_REQUEST_INTERVAL", "0.0", float),
            hero_cache_ttl=_num("HERO_CACHE_TTL", "0.0", float),
            stats_interval=_num("STATS_INTERVAL", "60.0", float),
            cache_jitter=_num("CACHE_JITTER", "0.0", float),
        )
