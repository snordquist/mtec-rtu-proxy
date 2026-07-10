"""Liveness probe: ``python -m modbus_proxy.healthcheck`` -> exit 0 (healthy) / 1.

Opens a localhost connection to the proxy and sends one FC03 read, then checks that
*any* framed Modbus reply comes back (register data OR a gateway exception both prove
the proxy is responsive) within a bounded time. Wired up as the Docker HEALTHCHECK so
a wedged-but-alive process (event loop stuck, listener dead) is detected and restarted.
It does NOT assert the upstream is up -- an exception reply still means the proxy lives.
"""
from __future__ import annotations

import asyncio
import os
import sys

from . import framing
from .config import Config


async def _probe(cfg: Config, addr: int, timeout: float) -> bool:
    unit = cfg.upstream_unit if cfg.upstream_unit is not None else 252
    req = framing.append_crc(bytes([unit, 0x03, (addr >> 8) & 0xFF, addr & 0xFF, 0, 1]))
    reader, writer = await asyncio.open_connection("127.0.0.1", cfg.listen_port)
    try:
        writer.write(req)
        await writer.drain()
        head = await asyncio.wait_for(reader.readexactly(2), timeout)  # unit + fc
        return len(head) == 2
    finally:
        writer.close()


def main() -> None:
    cfg = Config.from_env()
    addr = int(os.environ.get("HEALTHCHECK_ADDR", "10000"))
    timeout = cfg.txn_timeout * 2 + 4
    try:
        ok = asyncio.run(_probe(cfg, addr, timeout))
    except Exception:  # noqa: BLE001 - any failure = unhealthy
        ok = False
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
