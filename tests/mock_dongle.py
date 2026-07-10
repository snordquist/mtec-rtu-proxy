"""A mock M-TEC 'espressif' dongle for integration tests.

It reproduces the behaviours that break naive proxies:

* speaks **RTU-over-TCP** (no MBAP header, CRC-checked),
* is **single-master**: while one client is connected, further connections are
  refused (closed immediately) - just like the real dongle,
* can **drop** the next N replies to simulate the transient timeouts that make
  a churning proxy tear down and get refused.

It also records ``request_count``, ``total_connections`` and ``max_concurrent``
so tests can assert on the proxy's behaviour.
"""
from __future__ import annotations

import asyncio
from typing import Dict, Optional

from mtec_rtu_proxy import framing


class MockDongle:
    def __init__(self, registers: Optional[Dict[int, int]] = None, single_master: bool = True):
        self.registers: Dict[int, int] = dict(registers or {})
        self.single_master = single_master
        self.request_count = 0
        self.total_connections = 0
        self.concurrent = 0
        self.max_concurrent = 0
        self.drop_next = 0    # simulate this many missing replies (timeouts)
        self.delay_next = 0.0  # delay the next reply by this many seconds (late reply)
        self._server: Optional[asyncio.AbstractServer] = None

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> "MockDongle":
        self._server = await asyncio.start_server(self._handle, host, port)
        return self

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.single_master and self.concurrent >= 1:
            # emulate the espressif refusing a second master
            writer.close()
            return
        self.total_connections += 1
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        buf = bytearray()
        try:
            while True:
                data = await reader.read(256)
                if not data:
                    break
                buf += data
                for frame in framing.take_requests(buf):
                    await self._respond(frame, writer)
        except (ConnectionResetError, asyncio.IncompleteReadError, BrokenPipeError, OSError):
            pass
        finally:
            self.concurrent -= 1
            try:
                writer.close()
            except Exception:
                pass

    async def _respond(self, frame: bytes, writer: asyncio.StreamWriter) -> None:
        self.request_count += 1
        if self.drop_next > 0:
            self.drop_next -= 1
            return  # no reply -> the proxy will time out on this transaction
        if self.delay_next > 0:
            delay, self.delay_next = self.delay_next, 0.0
            await asyncio.sleep(delay)  # late reply: arrives after the proxy timed out
            if writer.is_closing():
                return  # proxy already dropped the socket -> late reply discarded
        unit = frame[0]
        fc = frame[1]
        if fc == 0x03:
            _, start, qty = framing.parse_fc03_request(frame)
            vals = [self.registers.get(start + i, 0) for i in range(qty)]
            writer.write(framing.build_fc03_response(unit, vals))
        elif fc == 0x06:
            addr = (frame[2] << 8) | frame[3]
            self.registers[addr] = (frame[4] << 8) | frame[5]
            writer.write(frame)  # FC06 reply echoes the request
        elif fc == 0x10:
            start = (frame[2] << 8) | frame[3]
            qty = (frame[4] << 8) | frame[5]
            byte_count = frame[6]
            data = frame[7 : 7 + byte_count]
            for i in range(qty):
                self.registers[start + i] = (data[2 * i] << 8) | data[2 * i + 1]
            writer.write(framing.append_crc(bytes([unit, 0x10, frame[2], frame[3], frame[4], frame[5]])))
        else:
            writer.write(framing.build_exception(unit, fc, 0x01))  # illegal function
        await writer.drain()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


async def read_one_rtu(reader: asyncio.StreamReader) -> bytes:
    """Read exactly one RTU reply frame from a stream (test client helper)."""
    head = await reader.readexactly(2)
    fc = head[1]
    if fc & 0x80:
        return head + await reader.readexactly(3)
    if fc == 0x03:
        bc = (await reader.readexactly(1))[0]
        return head + bytes([bc]) + await reader.readexactly(bc + 2)
    if fc in (0x06, 0x10):
        return head + await reader.readexactly(6)
    return head + await reader.readexactly(4)


async def client_request(port: int, frame: bytes, host: str = "127.0.0.1", timeout: float = 5.0) -> bytes:
    """Open a connection, send one RTU request, read one reply, close."""
    reader, writer = await asyncio.open_connection(host, port)
    try:
        writer.write(frame)
        await writer.drain()
        return await asyncio.wait_for(read_one_rtu(reader), timeout)
    finally:
        writer.close()


# --- Modbus/TCP (MBAP) client, mimicking the EnergyHero -----------------------

def build_mbap(txn: int, unit: int, pdu: bytes) -> bytes:
    length = 1 + len(pdu)
    return bytes([(txn >> 8) & 0xFF, txn & 0xFF, 0, 0,
                  (length >> 8) & 0xFF, length & 0xFF, unit]) + pdu


async def read_one_mbap(reader: asyncio.StreamReader) -> bytes:
    head = await reader.readexactly(6)            # txn(2) proto(2) length(2)
    length = (head[4] << 8) | head[5]
    return head + await reader.readexactly(length)


async def mbap_client_request(port: int, txn: int, unit: int, pdu: bytes,
                              host: str = "127.0.0.1", timeout: float = 5.0) -> bytes:
    reader, writer = await asyncio.open_connection(host, port)
    try:
        writer.write(build_mbap(txn, unit, pdu))
        await writer.drain()
        return await asyncio.wait_for(read_one_mbap(reader), timeout)
    finally:
        writer.close()
