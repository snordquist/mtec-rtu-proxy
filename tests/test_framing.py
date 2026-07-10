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
