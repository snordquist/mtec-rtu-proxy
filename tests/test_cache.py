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


def test_jitter_staggers_block_expiry():
    # Two blocks cached at the same instant get different random offsets, so they
    # expire at staggered times (no synchronized refresh burst).
    now = [1000.0]
    offsets = iter([0.0, 1.0])  # block100 off=0 -> exp 1010; block200 off=4 -> exp 1014
    c = RegisterCache(ttl=10, clock=lambda: now[0], jitter=4.0, rand=lambda: next(offsets))
    c.update(100, [1])
    c.update(200, [2])
    now[0] = 1012.0
    assert c.get_block(100, 1) is None      # block100 already expired (10s)
    assert c.get_block(200, 1) == [2]       # block200 still fresh (jittered later)
