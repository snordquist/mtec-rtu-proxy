from mtec_rtu_proxy import framing


def test_crc16_known_vector():
    # Textbook Modbus example: 01 03 00 00 00 01 -> CRC 84 0A (low byte first).
    assert framing.crc16(bytes.fromhex("010300000001")) == 0x0A84
    assert framing.append_crc(bytes.fromhex("010300000001")) == bytes.fromhex("010300000001840A")


def test_crc_ok_roundtrip():
    for pdu in (b"\xfc\x03\x00\x64\x00\x03", b"\x01\x06\x00\x01\x00\x2a"):
        assert framing.crc_ok(framing.append_crc(pdu))


def test_crc_ok_rejects_corruption():
    good = bytearray(framing.append_crc(b"\xfc\x03\x00\x64\x00\x03"))
    good[3] ^= 0xFF
    assert not framing.crc_ok(bytes(good))


def test_fc03_response_roundtrip():
    resp = framing.build_fc03_response(252, [11, 22, 4095])
    assert framing.crc_ok(resp)
    unit, regs = framing.parse_fc03_response(resp)
    assert unit == 252
    assert regs == [11, 22, 4095]


def test_fc03_request_parse():
    req = framing.append_crc(bytes([252, 0x03, 0x00, 0x64, 0x00, 0x03]))
    assert framing.parse_fc03_request(req) == (252, 100, 3)


def test_build_exception():
    exc = framing.build_exception(252, 0x03, 0x0B)
    assert framing.crc_ok(exc)
    assert exc[1] == 0x83  # FC | 0x80
    assert exc[2] == 0x0B


def test_take_requests_splits_concatenated_frames():
    a = framing.append_crc(bytes([252, 0x03, 0, 100, 0, 3]))
    b = framing.append_crc(bytes([252, 0x06, 0, 5, 0, 42]))
    buf = bytearray(a + b)
    assert framing.take_requests(buf) == [a, b]
    assert len(buf) == 0


def test_take_requests_waits_for_partial_frame():
    a = framing.append_crc(bytes([252, 0x03, 0, 100, 0, 3]))  # 8 bytes
    buf = bytearray(a[:-1])                                    # 7 bytes -> incomplete
    assert framing.take_requests(buf) == []
    assert len(buf) == 7


def test_take_requests_fc16_write_multiple():
    body = bytes([252, 0x10, 0x00, 0x0A, 0x00, 0x02, 0x04, 0x00, 0x01, 0x00, 0x02])
    frame = framing.append_crc(body)
    buf = bytearray(frame)
    assert framing.take_requests(buf) == [frame]
    assert len(buf) == 0


def test_take_requests_resyncs_after_garbage():
    good = framing.append_crc(bytes([252, 0x03, 0, 100, 0, 1]))
    buf = bytearray(bytes([252, 0x03, 0, 100, 0, 1, 0x00, 0x00]) + good)  # bad-CRC frame + good
    frames = framing.take_requests(buf)
    assert good in frames


# --- MBAP / dialect detection (EnergyHero speaks Modbus/TCP) -------------------

def test_mbap_build_matches_real_hero_frame():
    # Exactly the frame captured from the EnergyHero: FC06 write reg 0x61B3=1000, unit 255
    frame = framing.build_mbap(0x0009, 0xFF, bytes([0x06, 0x61, 0xB3, 0x03, 0xE8]))
    assert frame == bytes.fromhex("000900000006ff0661b303e8")


def test_take_mbap_requests_roundtrip():
    frame = bytes.fromhex("000900000006ff0661b303e8")
    buf = bytearray(frame)
    assert framing.take_mbap_requests(buf) == [(0x0009, 0xFF, bytes([0x06, 0x61, 0xB3, 0x03, 0xE8]))]
    assert len(buf) == 0


def test_detect_dialect_rtu_vs_mbap():
    rtu = framing.append_crc(bytes([252, 0x03, 0x00, 0x64, 0x00, 0x03]))
    assert framing.detect_dialect(rtu) == "rtu"
    assert framing.detect_dialect(bytes.fromhex("000900000006ff0661b303e8")) == "mbap"


def test_detect_dialect_rtu_read_of_addr_zero_is_not_mbap():
    # bytes 2:4 are 00 00 (like MBAP) but the CRC is valid -> must be classified RTU
    rtu = framing.append_crc(bytes([252, 0x03, 0x00, 0x00, 0x00, 0x03]))
    assert framing.detect_dialect(rtu) == "rtu"


def test_detect_dialect_partial_rtu_fc16_addr0_not_misread_as_mbap():
    # RTU FC16 write 2 regs @0: start(0,0) collides with MBAP's zero proto-id. A
    # partially-arrived frame (header only) must NOT latch to MBAP -> return None.
    full = framing.append_crc(bytes([252, 0x10, 0x00, 0x00, 0x00, 0x02, 0x04, 0, 1, 0, 2]))
    assert framing.detect_dialect(bytes(full[:8])) is None   # wait for the rest
    assert framing.detect_dialect(full) == "rtu"             # complete + valid CRC


def test_detect_dialect_mbap_with_txn_low_byte_0x10():
    # A real MBAP frame whose txn low byte is 0x10 must still be detected as MBAP
    # (not stalled waiting for a bogus RTU FC16 length).
    frame = framing.build_mbap(0x0010, 0xFF, bytes([0x03, 0x00, 0x64, 0x00, 0x02]))
    assert frame[1] == 0x10
    assert framing.detect_dialect(frame) == "mbap"


def test_take_mbap_requests_resyncs_past_garbage():
    good = bytes.fromhex("000900000006ff0661b303e8")
    buf = bytearray(bytes([0xAB, 0x00, 0x07]) + good)  # leading garbage (bad proto-id)
    assert framing.take_mbap_requests(buf) == [(0x0009, 0xFF, bytes([0x06, 0x61, 0xB3, 0x03, 0xE8]))]


def test_take_mbap_requests_drops_implausible_length():
    # length field 0/1 must not wedge the parser (it used to break forever).
    buf = bytearray(bytes([0x00, 0x00, 0x00, 0x00, 0x00, 0x01, 0xFF]))
    framing.take_mbap_requests(buf)  # must make progress, not loop
    assert len(buf) < 7


def test_take_requests_drops_bogus_fc16_bytecount():
    # A stray 0x10 with byte_count != 2*qty is garbage -> drop one byte, don't stall
    # waiting for a huge bogus frame; a following valid frame still parses.
    good = framing.append_crc(bytes([252, 0x03, 0, 100, 0, 1]))
    buf = bytearray(bytes([0x00, 0x10, 0x00, 0x00, 0x00, 0x02, 0xFA]) + good)
    frames = framing.take_requests(buf)
    assert good in frames


def test_describe_request_and_reply():
    assert framing.describe_request(bytes([0x03, 0x80, 0xE8, 0x00, 0x01])) == "FC03 read @33000 x1"
    assert framing.describe_request(bytes([0x06, 0x61, 0xB3, 0x03, 0xE8])) == "FC06 write @25011=1000"
    assert framing.describe_reply(bytes([0x03, 0x02, 0x24, 0xFE])) == "FC03 -> [9470]"
    assert framing.describe_reply(bytes([0x83, 0x0B])) == "EXCEPTION fc03 code=0x0B"


def test_reply_matches_detects_desync():
    req2 = framing.append_crc(bytes([252, 0x03, 0, 100, 0, 2]))  # read qty 2
    assert framing.reply_matches(req2, framing.build_fc03_response(252, [11, 22]))
    # a valid frame for the WRONG quantity (off-by-one desync) must be rejected
    assert not framing.reply_matches(req2, framing.build_fc03_response(252, [11, 22, 33]))
    # an exception is a valid response to the request
    assert framing.reply_matches(req2, framing.build_exception(252, 0x03, 0x0B))
    # write reply must echo the address
    wreq = framing.append_crc(bytes([252, 0x06, 0x61, 0xB3, 0x03, 0xE8]))
    assert framing.reply_matches(wreq, wreq)
    assert not framing.reply_matches(wreq, framing.append_crc(bytes([252, 0x06, 0x00, 0x01, 0x03, 0xE8])))
