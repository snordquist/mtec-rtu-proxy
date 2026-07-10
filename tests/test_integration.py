"""End-to-end tests: real localhost sockets, a mock single-master RTU dongle,
and clients driven through the proxy. Each test runs its own event loop via
``asyncio.run`` (no pytest-asyncio dependency)."""
import asyncio

from mtec_rtu_proxy import framing
from mtec_rtu_proxy.config import Config
from mtec_rtu_proxy.proxy import ProxyServer

from mock_dongle import MockDongle, client_request, read_one_rtu, mbap_client_request


def _cfg(dongle_port, **kw):
    base = dict(
        listen_host="127.0.0.1", listen_port=0,
        dongle_host="127.0.0.1", dongle_port=dongle_port,
        hero_ips=frozenset(), txn_timeout=1.0, cache_ttl=30.0,
        reconnect_backoff=0.2, connect_settle=0.0, stats_interval=0.0,
    )
    base.update(kw)
    return Config(**base)


def _fc03(unit, start, qty):
    return framing.append_crc(bytes([unit, 0x03, (start >> 8) & 0xFF, start & 0xFF,
                                     (qty >> 8) & 0xFF, qty & 0xFF]))


def _fc06(unit, addr, val):
    return framing.append_crc(bytes([unit, 0x06, (addr >> 8) & 0xFF, addr & 0xFF,
                                     (val >> 8) & 0xFF, val & 0xFF]))


# --- non-priority (HA) reads are served from cache -------------------------

def test_non_priority_read_is_served_from_cache():
    asyncio.run(_scenario_cache())


async def _scenario_cache():
    dongle = await MockDongle({100: 11, 101: 22, 102: 33}).start()
    proxy = await ProxyServer(_cfg(dongle.port)).start()  # hero_ips empty -> cache client
    try:
        req = _fc03(252, 100, 3)
        r1 = await client_request(proxy.port, req)
        assert framing.parse_fc03_response(r1)[1] == [11, 22, 33]
        assert dongle.request_count == 1  # first read went live

        r2 = await client_request(proxy.port, req)
        assert framing.parse_fc03_response(r2)[1] == [11, 22, 33]
        assert dongle.request_count == 1  # second read served from cache, no dongle hit
    finally:
        await proxy.stop()
        await dongle.stop()


# --- priority (Hero/EMS) reads are always live and warm the cache ----------

def test_priority_reads_are_live_and_warm_cache():
    asyncio.run(_scenario_hero_live())


async def _scenario_hero_live():
    dongle = await MockDongle({100: 7, 101: 8}).start()
    proxy = await ProxyServer(_cfg(dongle.port, hero_ips=frozenset({"127.0.0.1"}))).start()
    try:
        req = _fc03(252, 100, 2)
        await client_request(proxy.port, req)
        await client_request(proxy.port, req)
        assert dongle.request_count == 2                    # hero is never cache-served
        assert proxy.cache.get_block(100, 2) == [7, 8]      # ...but live reads warm the cache
    finally:
        await proxy.stop()
        await dongle.stop()


# --- the dongle only ever sees ONE connection (single master) --------------

def test_single_upstream_under_many_clients():
    asyncio.run(_scenario_single_master())


async def _scenario_single_master():
    dongle = await MockDongle({i: i for i in range(100, 110)}, single_master=True).start()
    proxy = await ProxyServer(_cfg(dongle.port)).start()
    try:
        reqs = [client_request(proxy.port, _fc03(252, 100 + (i % 5), 1)) for i in range(20)]
        await asyncio.gather(*reqs)
        assert dongle.max_concurrent == 1
        assert dongle.total_connections == 1
    finally:
        await proxy.stop()
        await dongle.stop()


# --- a LATE reply after a timeout must NOT desync subsequent reads ---------
# This is the real-world failure: RTU has no transaction ids, so a reply that
# arrives after the proxy gave up would shift every following read by one frame.
# The proxy must drop+reconnect the upstream to guarantee a clean resync.

def test_late_reply_does_not_desync():
    asyncio.run(_scenario_late_reply())


async def _scenario_late_reply():
    dongle = await MockDongle({100: 55, 200: 4242}).start()
    proxy = await ProxyServer(
        _cfg(dongle.port, hero_ips=frozenset({"127.0.0.1"}),
             txn_timeout=0.4, reconnect_backoff=0.1)
    ).start()
    async def read_until(addr, expect, tries=20):
        # after a reconnect the single-master dongle may briefly refuse; retry.
        # CRUCIAL: this only ever succeeds if the register returns its OWN value
        # (a desynced/shifted late reply would return the wrong value forever).
        for _ in range(tries):
            r = await client_request(proxy.port, _fc03(252, addr, 1), timeout=4)
            if len(r) >= 5 and r[1] == 0x03 and framing.parse_fc03_response(r)[1] == [expect]:
                return True
            await asyncio.sleep(0.2)
        return False

    try:
        assert framing.parse_fc03_response(await client_request(proxy.port, _fc03(252, 100, 1)))[1] == [55]
        conns_before = dongle.total_connections

        dongle.delay_next = 0.8  # reply for the next read arrives after the 0.4s timeout
        r1 = await client_request(proxy.port, _fc03(252, 200, 1), timeout=4)
        assert r1[1] == 0x83  # timed-out txn -> gateway exception to the client

        # subsequent reads must return the CORRECT registers, not a shifted late reply
        assert await read_until(100, 55)
        assert await read_until(200, 4242)
        assert dongle.total_connections == conns_before  # resynced by DRAINING, no reconnect
    finally:
        await proxy.stop()
        await dongle.stop()


# --- writes (FC06) are forwarded live and take effect ----------------------

def test_offbyone_desync_is_detected_and_resynced():
    asyncio.run(_scenario_desync())


async def _scenario_desync():
    dongle = await MockDongle({200: 5, 201: 6, 202: 7, 203: 8}).start()
    proxy = await ProxyServer(
        _cfg(dongle.port, hero_ips=frozenset({"127.0.0.1"}), reconnect_backoff=0.1)
    ).start()
    try:
        assert framing.parse_fc03_response(await client_request(proxy.port, _fc03(252, 200, 2)))[1] == [5, 6]
        conns = dongle.total_connections

        # duplicate the reply -> a leftover [5,6] frame shifts the stream by one
        dongle.duplicate_next = 1
        await client_request(proxy.port, _fc03(252, 200, 2))  # own reply ok; a copy is left over

        # SAME-shaped read of a DIFFERENT block: the leftover [5,6] has the right length,
        # so ONLY the pre-request resync guard (not reply_matches) can catch this off-by-one.
        async def read_until(addr, expect, tries=15):
            for _ in range(tries):
                r = await client_request(proxy.port, _fc03(252, addr, 2), timeout=4)
                if len(r) >= 5 and r[1] == 0x03 and framing.parse_fc03_response(r)[1] == expect:
                    return True
                await asyncio.sleep(0.1)
            return False

        assert await read_until(202, [7, 8])          # must return 202's OWN values, not [5,6]
        assert dongle.total_connections == conns       # resynced by DRAINING, no reconnect churn
    finally:
        await proxy.stop()
        await dongle.stop()


def test_write_is_forwarded_live():
    asyncio.run(_scenario_write())


async def _scenario_write():
    dongle = await MockDongle({100: 0}).start()
    proxy = await ProxyServer(_cfg(dongle.port, hero_ips=frozenset({"127.0.0.1"}))).start()
    try:
        echo = await client_request(proxy.port, _fc06(252, 100, 1234))
        assert echo == _fc06(252, 100, 1234)             # FC06 reply echoes the request
        read = await client_request(proxy.port, _fc03(252, 100, 1))
        assert framing.parse_fc03_response(read)[1] == [1234]
    finally:
        await proxy.stop()
        await dongle.stop()


# --- a malformed client frame is dropped; the connection survives ----------

def test_bad_crc_frame_is_ignored():
    asyncio.run(_scenario_bad_crc())


async def _scenario_bad_crc():
    dongle = await MockDongle({100: 99}).start()
    proxy = await ProxyServer(_cfg(dongle.port, hero_ips=frozenset({"127.0.0.1"}))).start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
        writer.write(bytes([252, 0x03, 0, 100, 0, 1, 0x00, 0x00]))  # broken CRC -> dropped
        await writer.drain()
        writer.write(_fc03(252, 100, 1))                            # valid, same connection
        await writer.drain()
        reply = await asyncio.wait_for(read_one_rtu(reader), 3)
        assert framing.parse_fc03_response(reply)[1] == [99]
        writer.close()
    finally:
        await proxy.stop()
        await dongle.stop()


# --- an MBAP client (EnergyHero) is bridged to the RTU dongle --------------

def test_mbap_client_read_bridged_to_rtu_dongle():
    asyncio.run(_scenario_mbap_read())


async def _scenario_mbap_read():
    dongle = await MockDongle({200: 4711, 201: 42}).start()
    # loopback treated as hero (live); force unit 252 upstream like the real setup
    cfg = _cfg(dongle.port, hero_ips=frozenset({"127.0.0.1"}), dongle_unit=252)
    proxy = await ProxyServer(cfg).start()
    try:
        pdu = bytes([0x03, 0x00, 0xC8, 0x00, 0x02])  # FC03 read addr 200 (0x00C8), qty 2
        reply = await mbap_client_request(proxy.port, txn=0x0009, unit=255, pdu=pdu)
        assert reply[0:2] == bytes([0x00, 0x09])     # txn echoed
        assert reply[6] == 255                       # client's unit echoed back
        assert reply[7] == 0x03                      # FC03
        bc = reply[8]
        vals = [(reply[9 + 2 * i] << 8) | reply[10 + 2 * i] for i in range(bc // 2)]
        assert vals == [4711, 42]                    # real data bridged through
    finally:
        await proxy.stop()
        await dongle.stop()


def test_mbap_client_write_bridged_to_rtu_dongle():
    asyncio.run(_scenario_mbap_write())


async def _scenario_mbap_write():
    dongle = await MockDongle({}).start()
    cfg = _cfg(dongle.port, hero_ips=frozenset({"127.0.0.1"}), dongle_unit=252)
    proxy = await ProxyServer(cfg).start()
    try:
        # exactly the Hero's captured write: FC06 reg 0x61B3=25011 value 1000, unit 255
        wpdu = bytes([0x06, 0x61, 0xB3, 0x03, 0xE8])
        reply = await mbap_client_request(proxy.port, txn=0x000A, unit=255, pdu=wpdu)
        assert reply[7] == 0x06                        # FC06 echoed
        assert reply[8:12] == bytes([0x61, 0xB3, 0x03, 0xE8])
        assert dongle.registers.get(25011) == 1000     # write reached the dongle
    finally:
        await proxy.stop()
        await dongle.stop()
