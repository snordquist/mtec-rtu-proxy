"""Single-master, caching RTU-over-TCP proxy.

Design goals (why generic Modbus-TCP proxies fail against the M-TEC dongle):

1. **Correct RTU framing.** The dongle speaks RTU-over-TCP; replies are framed
   here by function code (FC03 by byte-count, exceptions = 3 bytes after the
   header), never by an MBAP length field.

2. **Exactly one persistent upstream connection.** The dongle tolerates only a
   single Modbus master. All clients are multiplexed onto one serialized
   upstream worker, so the dongle never sees more than one connection.

3. **No reconnect churn.** On a *transient* per-transaction timeout the upstream
   socket is drained and kept alive (a fresh reconnect would be refused by the
   single-master dongle). It reconnects only on a real socket death, with
   backoff.

4. **Priority + cache.** Hero/EMS clients get priority and always read live, so
   control is never stale. Everyone else is answered from the register cache
   for FC03 reads (zero extra dongle load) and only falls through to a live read
   on a cache miss. Writes are always forwarded live.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from . import framing
from .cache import RegisterCache
from .config import Config

log = logging.getLogger("mtec_rtu_proxy")

PRIO_HERO = 0
PRIO_OTHER = 10


class Upstream:
    """Owns THE single persistent connection to the dongle + a serialized queue."""

    def __init__(self, config: Config, cache: RegisterCache):
        self.cfg = config
        self.cache = cache
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.q: "asyncio.PriorityQueue" = asyncio.PriorityQueue()
        self._seq = 0
        self._connections = 0  # total successful upstream connects (tests assert on this)

    @property
    def connection_count(self) -> int:
        return self._connections

    async def _connect(self) -> None:
        while True:
            try:
                log.info("connecting to dongle %s:%s", self.cfg.dongle_host, self.cfg.dongle_port)
                self.reader, self.writer = await asyncio.wait_for(
                    asyncio.open_connection(self.cfg.dongle_host, self.cfg.dongle_port),
                    self.cfg.txn_timeout + 2,
                )
                self._connections += 1
                if self.cfg.connect_settle:
                    await asyncio.sleep(self.cfg.connect_settle)
                log.info("connected to dongle (total connects=%d)", self._connections)
                return
            except Exception as e:  # noqa: BLE001 - retry any connect failure
                log.error("dongle connect failed: %r; backoff %.1fs", e, self.cfg.reconnect_backoff)
                await asyncio.sleep(self.cfg.reconnect_backoff)

    async def _read_reply(self) -> bytes:
        """Frame exactly one RTU reply (no MBAP)."""
        head = await self.reader.readexactly(2)  # unit, fc
        fc = head[1]
        if fc & 0x80:  # exception: +1 code, +2 crc
            return head + await self.reader.readexactly(3)
        if fc == 0x03:  # read holding: +bytecount, +data, +2 crc
            bc = (await self.reader.readexactly(1))[0]
            return head + bytes([bc]) + await self.reader.readexactly(bc + 2)
        if fc in (0x06, 0x10):  # write single/multiple: echo 4 + crc
            return head + await self.reader.readexactly(6)
        return head + await self.reader.readexactly(4)  # unknown: best effort

    async def _drain(self) -> None:
        """Discard any late/stale bytes after a timeout WITHOUT closing the socket."""
        try:
            while True:
                data = await asyncio.wait_for(self.reader.read(256), 0.2)
                if not data:
                    break
        except asyncio.TimeoutError:
            pass

    async def worker(self) -> None:
        await self._connect()
        while True:
            _prio, _seq, req, fut = await self.q.get()
            if self.writer is None or self.writer.is_closing():
                await self._connect()
            try:
                self.writer.write(req)
                await self.writer.drain()
                reply = await asyncio.wait_for(self._read_reply(), self.cfg.txn_timeout)
                if framing.crc_ok(reply):
                    self._cache_from(req, reply)
                    if not fut.done():
                        fut.set_result(reply)
                elif not fut.done():
                    fut.set_exception(IOError("bad CRC from dongle"))
            except asyncio.TimeoutError:
                # KEY: resync, do NOT tear down the single upstream on a transient miss
                log.warning("txn timeout -> draining (upstream kept alive)")
                await self._drain()
                if not fut.done():
                    fut.set_exception(asyncio.TimeoutError())
            except Exception as e:  # noqa: BLE001 - real socket death -> reconnect
                log.error("upstream error %r -> reconnect with backoff", e)
                try:
                    if self.writer:
                        self.writer.close()
                except Exception:
                    pass
                self.reader = self.writer = None
                if not fut.done():
                    fut.set_exception(e)
                await asyncio.sleep(self.cfg.reconnect_backoff)

    def _cache_from(self, req: bytes, reply: bytes) -> None:
        if len(req) >= 6 and req[1] == 0x03 and reply[1] == 0x03:
            start = (req[2] << 8) | req[3]
            _, regs = framing.parse_fc03_response(reply)
            self.cache.update(start, regs)

    async def submit(self, req: bytes, priority: int) -> bytes:
        fut: "asyncio.Future[bytes]" = asyncio.get_running_loop().create_future()
        self._seq += 1
        await self.q.put((priority, self._seq, req, fut))
        return await fut

    async def aclose(self) -> None:
        """Close the upstream socket so the dongle sees EOF (clean shutdown)."""
        w, self.writer, self.reader = self.writer, None, None
        if w is not None:
            try:
                w.close()
                await w.wait_closed()
            except Exception:
                pass


async def serve(frame: bytes, is_hero: bool, up: Upstream, cache: RegisterCache) -> bytes:
    fc = frame[1]
    # Non-priority FC03 reads are answered from cache -> zero dongle load.
    if fc == 0x03 and not is_hero:
        _, start, qty = framing.parse_fc03_request(frame)
        cached = cache.get_block(start, qty)
        if cached is not None:
            return framing.build_fc03_response(frame[0], cached)
        # cache miss -> fall through to a (low-priority) live read that warms it
    priority = PRIO_HERO if is_hero else PRIO_OTHER
    try:
        return await up.submit(frame, priority)
    except Exception:  # noqa: BLE001 - surface as a Modbus gateway exception
        return framing.build_exception(frame[0], fc, 0x0B)  # 0x0B = target failed to respond


async def serve_mbap(txn: int, unit: int, pdu: bytes, is_hero: bool,
                     up: Upstream, cache: RegisterCache, cfg: Config) -> bytes:
    """Serve a Modbus/TCP (MBAP) client by bridging to the RTU dongle.

    Translates MBAP -> RTU (adds CRC, optional unit override), submits on the
    single upstream, then wraps the RTU reply back into MBAP (echoing the txn).
    """
    fc = pdu[0]
    send_unit = cfg.dongle_unit if cfg.dongle_unit is not None else unit
    # non-priority FC03 reads may be served from cache (zero dongle load)
    if fc == 0x03 and not is_hero and len(pdu) >= 5:
        start = (pdu[1] << 8) | pdu[2]
        qty = (pdu[3] << 8) | pdu[4]
        cached = cache.get_block(start, qty)
        if cached is not None:
            body = bytes([0x03, qty * 2])
            for v in cached:
                body += bytes([(v >> 8) & 0xFF, v & 0xFF])
            return framing.build_mbap(txn, unit, body)
    priority = PRIO_HERO if is_hero else PRIO_OTHER
    rtu_req = framing.append_crc(bytes([send_unit]) + pdu)
    try:
        rtu_reply = await up.submit(rtu_req, priority)
        return framing.build_mbap(txn, unit, framing.rtu_pdu(rtu_reply))
    except Exception:  # noqa: BLE001 - surface as a Modbus gateway exception
        return framing.build_mbap(txn, unit, bytes([fc | 0x80, 0x0B]))


async def handle_client(reader, writer, up: Upstream, cache: RegisterCache, cfg: Config) -> None:
    peer = writer.get_extra_info("peername")
    peer_ip = peer[0] if peer else "?"
    is_hero = peer_ip in cfg.hero_ips
    buf = bytearray()
    dialect = None
    try:
        while True:
            data = await reader.read(256)
            if not data:
                break
            buf += data
            if dialect is None:
                dialect = framing.detect_dialect(buf)
                if dialect is None:
                    continue  # need more bytes to decide
                log.info("client %s (%s, %s)", peer_ip,
                         "HERO/live" if is_hero else "cache", dialect)
            if dialect == "mbap":
                for txn, unit, pdu in framing.take_mbap_requests(buf):
                    writer.write(await serve_mbap(txn, unit, pdu, is_hero, up, cache, cfg))
                    await writer.drain()
            else:
                for frame in framing.take_requests(buf):
                    reply = await serve(frame, is_hero, up, cache)
                    if reply:
                        writer.write(reply)
                        await writer.drain()
    except (ConnectionResetError, asyncio.IncompleteReadError, BrokenPipeError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


class ProxyServer:
    """Composes the cache, the single upstream worker and the client server."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.cache = RegisterCache(ttl=cfg.cache_ttl)
        self.up = Upstream(cfg, self.cache)
        self._server: Optional[asyncio.AbstractServer] = None
        self._worker: Optional[asyncio.Task] = None

    async def start(self) -> "ProxyServer":
        self._worker = asyncio.create_task(self.up.worker())
        self._server = await asyncio.start_server(
            lambda r, w: handle_client(r, w, self.up, self.cache, self.cfg),
            self.cfg.listen_host,
            self.cfg.listen_port,
        )
        return self

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]

    async def serve_forever(self) -> None:
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        # Stop the worker first, then close the upstream so the dongle sees EOF
        # (Server.wait_closed() blocks on live connections on Python 3.13+).
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
        await self.up.aclose()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


async def run(cfg: Config) -> None:
    srv = await ProxyServer(cfg).start()
    log.info(
        "RTU caching proxy on %s:%s -> dongle %s:%s (heroes=%s)",
        cfg.listen_host, cfg.listen_port, cfg.dongle_host, cfg.dongle_port, sorted(cfg.hero_ips),
    )
    await srv.serve_forever()
