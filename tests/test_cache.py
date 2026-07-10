from mtec_rtu_proxy.cache import RegisterCache


def test_update_and_get_block():
    c = RegisterCache(ttl=30, clock=lambda: 1000.0)
    c.update(100, [11, 22, 33])
    assert c.get_block(100, 3) == [11, 22, 33]
    assert c.get_block(100, 2) == [11, 22]
    assert c.get_block(101, 2) == [22, 33]


def test_partial_miss_returns_none():
    c = RegisterCache(ttl=30, clock=lambda: 1000.0)
    c.update(100, [11, 22])
    assert c.get_block(100, 3) is None  # register 102 was never cached


def test_ttl_expiry():
    now = [1000.0]
    c = RegisterCache(ttl=30, clock=lambda: now[0])
    c.update(100, [11, 22, 33])
    now[0] = 1029.9
    assert c.get_block(100, 3) == [11, 22, 33]  # still fresh
    now[0] = 1031.0
    assert c.get_block(100, 3) is None          # expired
