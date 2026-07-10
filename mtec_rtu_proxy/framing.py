"""Modbus RTU-over-TCP framing helpers.

The M-TEC "espressif" dongle speaks Modbus **RTU framed over a raw TCP stream**
(unit-id + PDU + CRC16), *not* Modbus/TCP (which prepends a 7-byte MBAP header
and carries no CRC). Getting this wrong is the single biggest reason generic
Modbus-TCP proxies fail against this dongle.

Everything in this module is a pure function so it can be unit-tested without
any I/O.
"""
from __future__ import annotations

from typing import List, Tuple


def crc16(data: bytes) -> int:
    """Modbus CRC-16 (poly 0xA001). On the wire the low byte goes first."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def append_crc(pdu: bytes) -> bytes:
    """Return ``pdu`` with its little-endian CRC-16 appended."""
    c = crc16(pdu)
    return pdu + bytes([c & 0xFF, (c >> 8) & 0xFF])


def crc_ok(frame: bytes) -> bool:
    """True if the trailing 2 bytes are a valid CRC for the rest of ``frame``."""
    return len(frame) >= 4 and crc16(frame[:-2]) == (frame[-2] | (frame[-1] << 8))


def build_fc03_response(unit: int, values: List[int]) -> bytes:
    """Build a Read-Holding-Registers (FC03) response frame (incl. CRC)."""
    body = bytes([unit, 0x03, len(values) * 2])
    for v in values:
        body += bytes([(v >> 8) & 0xFF, v & 0xFF])
    return append_crc(body)


def build_exception(unit: int, fc: int, code: int) -> bytes:
    """Build a Modbus exception reply (function code | 0x80)."""
    return append_crc(bytes([unit, fc | 0x80, code]))


def parse_fc03_request(frame: bytes) -> Tuple[int, int, int]:
    """Return ``(unit, start_addr, quantity)`` from an 8-byte FC03 request."""
    return frame[0], (frame[2] << 8) | frame[3], (frame[4] << 8) | frame[5]


def parse_fc03_response(frame: bytes) -> Tuple[int, List[int]]:
    """Return ``(unit, [register values])`` from an FC03 response frame."""
    bc = frame[2]
    regs = [(frame[3 + 2 * i] << 8) | frame[4 + 2 * i] for i in range(bc // 2)]
    return frame[0], regs


def take_requests(buf: bytearray) -> List[bytes]:
    """Pull complete, CRC-valid RTU requests out of ``buf`` (mutated in place).

    Supports FC03 (read holding, 8 B), FC06 (write single, 8 B) and FC16
    (write multiple, 9+N B). Bad-CRC or unknown leading bytes are dropped one
    at a time so a corrupted stream can resynchronise instead of wedging.
    """
    out: List[bytes] = []
    while len(buf) >= 8:
        fc = buf[1]
        if fc in (0x03, 0x06):
            frame = bytes(buf[:8])
            if not crc_ok(frame):
                del buf[:1]
                continue
            del buf[:8]
            out.append(frame)
        elif fc == 0x10:  # write multiple: unit,fc,start(2),qty(2),bytecount,data,crc(2)
            byte_count = buf[6]
            total = 9 + byte_count
            if len(buf) < total:
                break  # wait for the rest of the frame
            frame = bytes(buf[:total])
            if not crc_ok(frame):
                del buf[:1]
                continue
            del buf[:total]
            out.append(frame)
        else:
            del buf[:1]  # unknown function code -> resync
    return out
