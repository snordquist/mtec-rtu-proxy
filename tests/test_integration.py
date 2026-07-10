"""End-to-end tests: real localhost sockets, a mock single-master RTU dongle,
and clients driven through the proxy. Each test runs its own event loop via
``asyncio.run`` (no pytest-asyncio dependency)."""
import asyncio

from mtec_rtu_proxy import framing
from mtec_rtu_proxy.config import Config
from mtec_rtu_proxy.proxy import ProxyServer

from mock_dongle import MockDongle, client_request, read_one_rtu


def _cfg(dongle_port, **kw):
    base = dict(
        listen_host="127.0.0.1", listen_port=0,
        dongle_host="127.0.0.1", dongle_port=dongle_port,
        hero_ips=frozenset(), txn_timeout=1.0, cache_ttl=30.0,
        reconnect_backoff=0.2, connect_settle=0.0,
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


# --- a transient timeout drains, it does NOT tear down + reconnect ---------

def test_transient_timeout_keeps_upstream_alive():
    asyncio.run(_scenario_drain())


async def _scenario_drain():
    dongle = await MockDongle({100: 55}).start()
    proxy = await ProxyServer(
        _cfg(dongle.port, hero_ips=frozenset({"127.0.0.1"}), txn_timeout=0.5)
    ).start()
    try:
        r0 = await client_request(proxy.port, _fc03(252, 100, 1))
        assert framing.parse_fc03_response(r0)[1] == [55]
        assert dongle.total_connections == 1

        dongle.drop_next = 1  # next transaction gets no reply -> proxy times out
        r1 = await client_request(proxy.port, _fc03(252, 100, 1), timeout=3)
        assert r1[1] == 0x83 and r1[2] == 0x0B           # gateway "target failed" exception
        assert dongle.total_connections == 1             # upstream drained, NOT reconnected

        r2 = await client_request(proxy.port, _fc03(252, 100, 1))  # recovers on same socket
        assert framing.parse_fc03_response(r2)[1] == [55]
        assert dongle.total_connections == 1
    finally:
        await proxy.stop()
        await dongle.stop()


# --- writes (FC06) are forwarded live and take effect ----------------------

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
