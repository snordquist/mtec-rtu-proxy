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


# --- Modbus/TCP (MBAP) support -------------------------------------------------
#
# Some clients (e.g. the M-TEC EnergyHero EMS) speak Modbus/TCP (MBAP header,
# no CRC), while the espressif dongle speaks RTU-over-TCP. The proxy bridges the
# two: MBAP request -> RTU request to the dongle -> RTU reply -> MBAP reply.
#
# MBAP frame layout: txn(2) proto(2, =0) length(2) unit(1) pdu(length-1)
#   where pdu = function code + data (no CRC).


def looks_like_mbap(buf: bytes) -> bool:
    """Heuristic: MBAP has protocol-id 0x0000 at bytes 2:4 and a plausible length.

    Only trust this AFTER ruling out a valid-CRC RTU frame (see ``detect_dialect``);
    it disambiguates the rare RTU-read-of-address-0 case.
    """
    if len(buf) < 8:
        return False
    if buf[2] != 0 or buf[3] != 0:
        return False
    length = (buf[4] << 8) | buf[5]
    return 2 <= length <= 253


def detect_dialect(buf: bytes):
    """Return 'rtu', 'mbap' or None (need more bytes) for a client's first frame."""
    if len(buf) < 8:
        return None
    fc = buf[1]
    if fc in (0x03, 0x06) and crc_ok(bytes(buf[:8])):
        return "rtu"
    if fc == 0x10 and len(buf) >= 9:
        total = 9 + buf[6]
        if len(buf) >= total and crc_ok(bytes(buf[:total])):
            return "rtu"
    if looks_like_mbap(buf):
        return "mbap"
    # Not a valid RTU frame and not MBAP-shaped: default to RTU so take_requests
    # can resync past leading garbage/bad-CRC bytes (never latches on nothing).
    return "rtu"


def take_mbap_requests(buf: bytearray):
    """Pull complete MBAP requests out of ``buf`` -> list of (txn, unit, pdu)."""
    out = []
    while len(buf) >= 6:
        length = (buf[4] << 8) | buf[5]
        total = 6 + length
        if length < 2 or len(buf) < total:
            break
        txn = (buf[0] << 8) | buf[1]
        unit = buf[6]
        pdu = bytes(buf[7:total])
        del buf[:total]
        out.append((txn, unit, pdu))
    return out


def build_mbap(txn: int, unit: int, pdu: bytes) -> bytes:
    """Wrap a PDU (function code + data, no CRC) in an MBAP header."""
    length = 1 + len(pdu)
    return bytes([(txn >> 8) & 0xFF, txn & 0xFF, 0, 0,
                  (length >> 8) & 0xFF, length & 0xFF, unit]) + pdu


def rtu_pdu(frame: bytes) -> bytes:
    """Extract the PDU (function code + data) from an RTU frame (unit+pdu+crc)."""
    return frame[1:-2]


# --- human-readable decoding for debug logging --------------------------------

def describe_request(pdu: bytes) -> str:
    """One-line summary of a request PDU (function code + data)."""
    fc = pdu[0] if pdu else 0
    if fc == 0x03 and len(pdu) >= 5:
        return f"FC03 read @{(pdu[1] << 8) | pdu[2]} x{(pdu[3] << 8) | pdu[4]}"
    if fc == 0x06 and len(pdu) >= 5:
        return f"FC06 write @{(pdu[1] << 8) | pdu[2]}={(pdu[3] << 8) | pdu[4]}"
    if fc == 0x10 and len(pdu) >= 5:
        return f"FC16 write @{(pdu[1] << 8) | pdu[2]} x{(pdu[3] << 8) | pdu[4]}"
    return f"FC{fc:02X}({pdu.hex()})"


def reply_matches(req: bytes, reply: bytes) -> bool:
    """Best-effort check that an RTU reply belongs to its request.

    RTU-over-TCP has no transaction id, so a stream that ever shifts by one
    frame stays silently mis-paired (every reply belongs to the *previous*
    request) with valid CRCs and no timeout. Detect that here: an FC03 reply's
    byte count must equal 2x the requested quantity, and a write reply must echo
    the request's start address. Mismatch => desync => caller should reconnect.
    """
    if len(req) < 4 or len(reply) < 4:
        return False
    rfc, pfc = req[1], reply[1]
    if pfc & 0x80:  # an exception is a valid response to a request of the same fc
        return (pfc & 0x7F) == rfc
    if pfc != rfc:
        return False
    if rfc == 0x03:  # read holding: byte count must be 2 * requested quantity
        qty = (req[4] << 8) | req[5]
        return len(reply) >= 3 and reply[2] == 2 * qty
    if rfc in (0x06, 0x10):  # write single/multiple: reply echoes the start address
        return reply[2:4] == req[2:4]
    return True


def describe_reply(pdu: bytes) -> str:
    """One-line summary of a reply PDU (function code + data)."""
    fc = pdu[0] if pdu else 0
    if fc & 0x80:
        return f"EXCEPTION fc{fc & 0x7F:02X} code=0x{pdu[1]:02X}" if len(pdu) > 1 else "EXCEPTION"
    if fc == 0x03 and len(pdu) >= 2:
        bc = pdu[1]
        regs = [(pdu[2 + 2 * i] << 8) | pdu[3 + 2 * i] for i in range(bc // 2)]
        return f"FC03 -> {regs}"
    if fc in (0x06, 0x10):
        return f"FC{fc:02X} ok"
    return f"FC{fc:02X}({pdu.hex()})"
