"""Runtime configuration, sourced from environment variables.

No real host addresses live in code. Defaults are deliberately generic
(loopback / documentation ranges); real values come from the environment
(see ``.env.example``).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import FrozenSet, Mapping


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

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "Config":
        heroes = env.get("HERO_IPS", "")
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
        )
