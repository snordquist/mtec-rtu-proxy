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
    upstream_host: str = "127.0.0.1"
    upstream_port: int = 502
    priority_ips: FrozenSet[str] = frozenset()
    write_ips: FrozenSet[str] = frozenset()  # IPs allowed to send FC06/FC16 writes (empty = allow all + warn)
    txn_timeout: float = 3.0        # per-transaction upstream timeout (seconds)
    cache_ttl: float = 30.0         # how long a cached register stays servable
    reconnect_backoff: float = 3.0  # wait before reconnecting a dead upstream
    connect_settle: float = 2.0     # pause after connect before first request
    upstream_unit: Optional[int] = None  # force this Modbus unit id upstream (None = pass client's through)
    log_level: str = "INFO"            # set DEBUG to log every request/reply with decoded values
    min_request_interval: float = 0.0  # min seconds between upstream requests (0 = no throttle)
    priority_cache_ttl: float = 0.0        # if >0, also serve priority reads from cache within this window (debounce)
    stats_interval: float = 60.0       # log a stats summary every N seconds (0 = off)
    cache_jitter: float = 0.0          # random 0..N s added per cache write to de-sync block expiries

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Config":
        # Generic names are primary; the DONGLE_*/HERO_* names are accepted as
        # backward-compatible aliases (first non-empty wins).
        def _get(keys, default=""):
            for k in keys:
                v = env.get(k)
                if v is not None and v != "":
                    return v
            return default

        def _num(keys, default: str, cast):
            raw = _get(keys, default)
            try:
                return cast(raw)
            except (TypeError, ValueError):
                raise ValueError(
                    f"invalid {keys[0]}={raw!r}: expected a {cast.__name__}"
                ) from None

        def _ips(keys) -> "FrozenSet[str]":
            return frozenset(x.strip() for x in _get(keys).split(",") if x.strip())

        du = _get(["UPSTREAM_UNIT", "DONGLE_UNIT"]).strip()
        priority_ips = _ips(["PRIORITY_IPS", "HERO_IPS"])
        # Writes default to the priority client(s) -- the controller is the natural
        # writer. Add other legitimate writers (e.g. a dashboard) via WRITE_IPS.
        write_ips = _ips(["WRITE_IPS"]) or priority_ips
        return cls(
            listen_host=env.get("LISTEN_HOST", "0.0.0.0"),
            listen_port=_num(["LISTEN_PORT"], "502", int),
            upstream_host=_get(["UPSTREAM_HOST", "DONGLE_HOST"], "127.0.0.1"),
            upstream_port=_num(["UPSTREAM_PORT", "DONGLE_PORT"], "502", int),
            priority_ips=priority_ips,
            write_ips=write_ips,
            txn_timeout=_num(["TXN_TIMEOUT"], "3.0", float),
            cache_ttl=_num(["CACHE_TTL"], "30.0", float),
            reconnect_backoff=_num(["RECONNECT_BACKOFF"], "3.0", float),
            connect_settle=_num(["CONNECT_SETTLE"], "2.0", float),
            upstream_unit=int(du) if du else None,
            log_level=env.get("LOG_LEVEL", "INFO").upper(),
            min_request_interval=_num(["MIN_REQUEST_INTERVAL"], "0.0", float),
            priority_cache_ttl=_num(["PRIORITY_CACHE_TTL", "HERO_CACHE_TTL"], "0.0", float),
            stats_interval=_num(["STATS_INTERVAL"], "60.0", float),
            cache_jitter=_num(["CACHE_JITTER"], "0.0", float),
        )
