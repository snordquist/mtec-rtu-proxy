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
import struct
import time
from collections import deque
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
        self._last_txn = 0.0   # monotonic time of the last upstream write (for throttling)
        self.n_live = 0        # stats: successful live reads/writes
        self.n_timeout = 0     # stats: transaction timeouts
        self.n_drain = 0       # stats: total stale bytes drained (resyncs)
        # --- mute diagnostics: ring buffer of recent transactions so we can dump
        # the lead-in (load spike? latency creep? a write?) at the FIRST timeout of
        # a mute streak, and summarise on recovery. Only transitions log -> quiet.
        self._journal: "deque" = deque(maxlen=80)  # (t0, desc, outcome, latency_s)
        self._to_streak = 0    # consecutive timeouts (0 = healthy)
        self._mute_start = 0.0
        self._needs_resync = False  # set after a timeout/anomaly -> drain before next request

    @property
    def connection_count(self) -> int:
        return self._connections

    @staticmethod
    def _req_desc(req: bytes) -> str:
        """Compact request label: R<addr>:<qty> read, W<addr>=<val> write."""
        if len(req) < 6:
            return "??"
        fc, a, v = req[1], (req[2] << 8) | req[3], (req[4] << 8) | req[5]
        if fc == 0x03:
            return f"R{a}:{v}"
        if fc == 0x06:
            return f"W{a}={v}"
        if fc == 0x10:
            return f"W{a}:{v}n"
        return f"fc{fc:#x}@{a}"

    def _note_ok(self, desc: str) -> None:
        if self._to_streak:
            log.warning("MUTE RECOVERED after %.1fs / %d timeouts; first-ok=%s",
                        time.monotonic() - self._mute_start, self._to_streak, desc)
            self._to_streak = 0

    def _note_timeout(self, desc: str, t0: float) -> None:
        if self._to_streak == 0:  # onset: dump the lead-in ONCE per mute streak
            self._mute_start = t0
            r1 = sum(1 for e in self._journal if t0 - e[0] <= 1.0)
            r5 = sum(1 for e in self._journal if t0 - e[0] <= 5.0)
            w3 = [e[1] for e in self._journal if t0 - e[0] <= 3.0 and e[1].startswith("W")]
            def _fmt(e):
                age = t0 - e[0]
                return (f"-{age:.1f}s {e[1]} {e[3] * 1000:.0f}ms" if e[2] == "ok"
                        else f"-{age:.1f}s {e[1]} {e[2]}")
            trail = " | ".join(_fmt(e) for e in list(self._journal)[-12:])
            log.warning(
                "MUTE ONSET hung=%s qdepth=%d livereq_1s=%d livereq_5s=%d writes_3s=%s | trail: %s",
                desc, self.q.qsize(), r1, r5, (",".join(w3) or "none"), trail,
            )
        self._to_streak += 1

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

    def _drop(self) -> None:
        """Close the upstream socket; the next request reconnects a clean stream."""
        try:
            if self.writer:
                self.writer.close()
        except Exception:
            pass
        self.reader = self.writer = None

    def _pending_bytes(self) -> int:
        """Bytes already delivered to the reader but unread (a stray/duplicate reply).

        A zero-cost, non-blocking peek at the StreamReader's internal buffer, so the
        healthy path can skip the blocking drain entirely when the stream is aligned.
        """
        r = self.reader
        return len(getattr(r, "_buffer", b"")) if r is not None else 0

    async def _drain_stale(self, quiet: float = 0.03) -> int:
        """Discard bytes waiting on the upstream (a stale/late/duplicate reply).

        Reads until the stream has been silent for ``quiet`` seconds. In an aligned
        stream there are none. Draining (instead of reconnecting) realigns the stream
        WITHOUT the connection churn that agitates the fragile dongle. A larger
        ``quiet`` (used after a timeout) gives an in-flight late reply time to arrive
        so it can be discarded rather than mispaired with the next request.
        """
        if self.reader is None:
            return 0
        drained = 0
        try:
            while True:
                data = await asyncio.wait_for(self.reader.read(256), quiet)
                if not data:  # EOF: peer closed -> next write triggers a reconnect
                    break
                drained += len(data)
        except asyncio.TimeoutError:
            pass
        except Exception:  # noqa: BLE001
            pass
        if drained:
            log.warning("drained %d stale bytes to resync (no reconnect)", drained)
        return drained

    async def worker(self) -> None:
        await self._connect()
        while True:
            _prio, _seq, req, fut = await self.q.get()
            if fut.done():
                # The client already gave up (its wait_for elapsed) -> don't spend one
                # of the single-master dongle's scarce transaction slots on it.
                continue
            if self.writer is None or self.writer.is_closing():
                await self._connect()
            # Resync guard. Healthy path: an aligned stream has no pending bytes, so a
            # non-blocking peek lets us skip the drain (no per-request latency tax).
            # After a timeout/anomaly, drain until quiet so an in-flight late reply is
            # discarded here instead of being mispaired with THIS request -- without a
            # reconnect (churn agitates the fragile dongle).
            if self._needs_resync:
                self.n_drain += await self._drain_stale(quiet=min(self.cfg.txn_timeout, 0.5))
                self._needs_resync = False
            elif self._pending_bytes():
                self.n_drain += await self._drain_stale()
            if self.cfg.min_request_interval > 0:  # throttle: pace upstream requests
                wait = self.cfg.min_request_interval - (time.monotonic() - self._last_txn)
                if wait > 0:
                    await asyncio.sleep(wait)
            t0 = self._last_txn = time.monotonic()
            desc = self._req_desc(req)
            try:
                self.writer.write(req)
                await self.writer.drain()
                reply = await asyncio.wait_for(self._read_reply(), self.cfg.txn_timeout)
                dt = time.monotonic() - t0
                if not framing.crc_ok(reply):
                    self._journal.append((t0, desc, "badcrc", dt))
                    self._needs_resync = True  # drain before the next request
                    if not fut.done():
                        fut.set_exception(IOError("bad CRC from dongle"))
                elif not framing.reply_matches(req, reply):
                    # off-by-one desync: valid frame, wrong request. Realign by draining
                    # before the next request instead of reconnecting (no churn).
                    log.warning("reply/request mismatch (RTU desync) -> resync before next request")
                    self._journal.append((t0, desc, "desync", dt))
                    self._needs_resync = True
                    if not fut.done():
                        fut.set_exception(IOError("desync"))
                else:
                    self.n_live += 1
                    self._journal.append((t0, desc, "ok", dt))
                    self._note_ok(desc)
                    self._cache_from(req, reply)
                    if not fut.done():
                        fut.set_result(reply)
            except asyncio.TimeoutError:
                # No reply in time. Keep the connection -- reconnect churn agitates the
                # fragile dongle. Mark the stream dirty so the next request drains any
                # in-flight late reply (quiet-window) before trusting a reply again.
                self.n_timeout += 1
                self._needs_resync = True
                self._journal.append((t0, desc, "TIMEOUT", self.cfg.txn_timeout))
                self._note_timeout(desc, t0)
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
        if len(req) < 2:
            return
        fc = req[1]
        if fc == 0x03 and len(reply) >= 3 and reply[1] == 0x03:
            start = (req[2] << 8) | req[3]
            _, regs = framing.parse_fc03_response(reply)
            self.cache.update(start, regs)
        elif fc == 0x06 and len(req) >= 6:
            # write single: the echoed write confirms the dongle now holds this value
            addr = (req[2] << 8) | req[3]
            self.cache.update(addr, [(req[4] << 8) | req[5]])
        elif fc == 0x10 and len(req) >= 7:
            # write multiple: update the written range from the request's own values
            start = (req[2] << 8) | req[3]
            qty = (req[4] << 8) | req[5]
            bc = req[6]
            if bc == 2 * qty and len(req) >= 7 + bc:
                self.cache.update(start, [(req[7 + 2 * i] << 8) | req[8 + 2 * i] for i in range(qty)])
            else:
                self.cache.invalidate(start, qty)

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


def _write_allowed(cfg: Config, ip: str) -> bool:
    """FC06/FC16 writes are allowed from write_ips; empty write_ips = allow all."""
    return (not cfg.write_ips) or (ip in cfg.write_ips)


def _client_timeout(cfg: Config) -> float:
    """Upper bound a client waits for a reply so a stalled/reconnecting worker
    fast-fails to a gateway exception instead of hanging the client forever."""
    return cfg.txn_timeout * 2 + 2.0


async def serve(frame: bytes, is_hero: bool, up: Upstream, cache: RegisterCache,
                cfg: Config, client: str = "") -> bytes:
    fc = frame[1]
    req = framing.rtu_pdu(frame)
    if fc == 0x03:
        _, start, qty = framing.parse_fc03_request(frame)
        if not (1 <= qty <= 125):  # FC03 quantity limit -> illegal data value
            return framing.build_exception(frame[0], fc, 0x03)
        # FC03 reads: HA from cache always; hero only within the debounce window (if enabled).
        if not is_hero or cfg.hero_cache_ttl > 0:
            cached = cache.get_block(start, qty, max_age=(cfg.hero_cache_ttl if is_hero else None))
            if cached is not None:
                log.debug("%s rtu %s -> CACHE %s", client, framing.describe_request(req), cached)
                return framing.build_fc03_response(frame[0], cached)
    elif fc in (0x06, 0x10) and not _write_allowed(cfg, client):
        log.warning("write from %s rejected: not in WRITE_IPS", client)
        return framing.build_exception(frame[0], fc, 0x01)  # illegal function (unauthorized)
    priority = PRIO_HERO if is_hero else PRIO_OTHER
    try:
        reply = await asyncio.wait_for(up.submit(frame, priority), _client_timeout(cfg))
        log.debug("%s rtu %s -> %s", client, framing.describe_request(req),
                  framing.describe_reply(framing.rtu_pdu(reply)))
        return reply
    except Exception:  # noqa: BLE001 - surface as a Modbus gateway exception
        log.debug("%s rtu %s -> TIMEOUT/ERR", client, framing.describe_request(req))
        return framing.build_exception(frame[0], fc, 0x0B)  # 0x0B = target failed to respond


async def serve_mbap(txn: int, unit: int, pdu: bytes, is_hero: bool,
                     up: Upstream, cache: RegisterCache, cfg: Config, client: str = "") -> bytes:
    """Serve a Modbus/TCP (MBAP) client by bridging to the RTU dongle.

    Translates MBAP -> RTU (adds CRC, optional unit override), submits on the
    single upstream, then wraps the RTU reply back into MBAP (echoing the txn).
    """
    fc = pdu[0]
    send_unit = cfg.dongle_unit if cfg.dongle_unit is not None else unit
    if fc == 0x03 and len(pdu) >= 5:
        start = (pdu[1] << 8) | pdu[2]
        qty = (pdu[3] << 8) | pdu[4]
        if not (1 <= qty <= 125):  # FC03 quantity limit -> illegal data value
            return framing.build_mbap(txn, unit, bytes([fc | 0x80, 0x03]))
        # FC03 reads: HA from cache always; hero only within the debounce window (if enabled).
        if not is_hero or cfg.hero_cache_ttl > 0:
            cached = cache.get_block(start, qty, max_age=(cfg.hero_cache_ttl if is_hero else None))
            if cached is not None:
                body = bytes([0x03, qty * 2]) + struct.pack(">%dH" % len(cached), *cached)
                log.debug("%s mbap %s -> CACHE %s", client, framing.describe_request(pdu), cached)
                return framing.build_mbap(txn, unit, body)
    elif fc in (0x06, 0x10) and not _write_allowed(cfg, client):
        log.warning("write from %s rejected: not in WRITE_IPS", client)
        return framing.build_mbap(txn, unit, bytes([fc | 0x80, 0x01]))  # illegal function
    priority = PRIO_HERO if is_hero else PRIO_OTHER
    rtu_req = framing.append_crc(bytes([send_unit]) + pdu)
    try:
        rtu_reply = await asyncio.wait_for(up.submit(rtu_req, priority), _client_timeout(cfg))
        log.debug("%s mbap %s -> %s", client, framing.describe_request(pdu),
                  framing.describe_reply(framing.rtu_pdu(rtu_reply)))
        return framing.build_mbap(txn, unit, framing.rtu_pdu(rtu_reply))
    except Exception:  # noqa: BLE001 - surface as a Modbus gateway exception
        log.debug("%s mbap %s -> TIMEOUT/ERR", client, framing.describe_request(pdu))
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
                    writer.write(await serve_mbap(txn, unit, pdu, is_hero, up, cache, cfg, peer_ip))
                    await writer.drain()
            else:
                for frame in framing.take_requests(buf):
                    reply = await serve(frame, is_hero, up, cache, cfg, peer_ip)
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
        self.cache = RegisterCache(ttl=cfg.cache_ttl, jitter=cfg.cache_jitter)
        self.up = Upstream(cfg, self.cache)
        self._server: Optional[asyncio.AbstractServer] = None
        self._worker: Optional[asyncio.Task] = None
        self._stats_task: Optional[asyncio.Task] = None
        self._restart_task: Optional[asyncio.Task] = None

    async def start(self) -> "ProxyServer":
        self._spawn_worker()
        if self.cfg.stats_interval > 0:
            self._stats_task = asyncio.create_task(self._stats_loop())
        self._server = await asyncio.start_server(
            lambda r, w: handle_client(r, w, self.up, self.cache, self.cfg),
            self.cfg.listen_host,
            self.cfg.listen_port,
        )
        return self

    def _spawn_worker(self) -> None:
        self._worker = asyncio.create_task(self.up.worker())
        self._worker.add_done_callback(self._on_worker_done)

    def _on_worker_done(self, task: "asyncio.Task") -> None:
        # The worker is the SINGLE queue consumer; if it dies unexpectedly the proxy
        # would accept clients but answer nothing (a self-inflicted mute). Restart it
        # after a short delay (the delay avoids a tight crash-loop).
        if task.cancelled():
            return  # deliberate shutdown via stop()
        exc = task.exception()
        if exc is not None:
            log.critical("upstream worker died: %r -> restarting in 1s", exc)
            self._restart_task = asyncio.create_task(self._delayed_respawn())

    async def _delayed_respawn(self) -> None:
        await asyncio.sleep(1.0)
        self._spawn_worker()

    async def _stats_loop(self) -> None:
        last = (0, 0, 0, 0, 0)
        while True:
            await asyncio.sleep(self.cfg.stats_interval)
            cur = (self.cache.hits, self.up.n_live, self.up.n_timeout,
                   self.up.n_drain, self.up.connection_count)
            d = [c - p for c, p in zip(cur, last)]
            last = cur
            log.info("stats(%.0fs): cache_hits=%d live=%d timeouts=%d drained_bytes=%d reconnects=%d",
                     self.cfg.stats_interval, d[0], d[1], d[2], d[3], d[4])

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
        # Drop the supervisor callback so a cancel here is not treated as a crash.
        if self._worker is not None:
            self._worker.remove_done_callback(self._on_worker_done)
        for task in (self._worker, self._stats_task, self._restart_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        await self.up.aclose()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


async def run(cfg: Config) -> None:
    if not cfg.hero_ips:
        log.warning("HERO_IPS is empty: NO client gets live/priority reads -- every read "
                    "is served from cache (a controller would act on stale data)")
    if not cfg.write_ips:
        log.warning("WRITE_IPS is empty: FC06/FC16 writes are accepted from ANY client -- "
                    "set WRITE_IPS to restrict who may command the inverter")
    srv = await ProxyServer(cfg).start()
    log.info(
        "RTU caching proxy on %s:%s -> dongle %s:%s (heroes=%s, writers=%s)",
        cfg.listen_host, cfg.listen_port, cfg.dongle_host, cfg.dongle_port,
        sorted(cfg.hero_ips), sorted(cfg.write_ips) or "ALL",
    )
    await srv.serve_forever()
