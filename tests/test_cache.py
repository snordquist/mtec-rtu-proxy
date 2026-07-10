from modbus_proxy.cache import RegisterCache


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
    # Back-dating each write by a random 0..jitter staggers expiry: the block that
    # was back-dated more expires EARLIER, so blocks cached together don't all
    # refresh at once.
    now = [1000.0]
    offsets = iter([1.0, 0.0])  # block100 back-dated 4s (exp 1006); block200 not (exp 1010)
    c = RegisterCache(ttl=10, clock=lambda: now[0], jitter=4.0, rand=lambda: next(offsets))
    c.update(100, [1])
    c.update(200, [2])
    now[0] = 1008.0
    assert c.get_block(100, 1) is None      # staggered earlier -> already expired
    assert c.get_block(200, 1) == [2]       # still fresh


def test_jitter_never_extends_ttl():
    # Back-dating means a value is never served OLDER than the configured ttl
    # (max staleness = ttl, not ttl+jitter).
    now = [1000.0]
    c = RegisterCache(ttl=10, clock=lambda: now[0], jitter=4.0, rand=lambda: 1.0)
    c.update(100, [1])          # stored timestamp back-dated to 996
    now[0] = 1006.1            # only 6.1s since the real write, but stored age = 10.1s
    assert c.get_block(100, 1) is None  # expired early -> never exceeds ttl


def test_qty_zero_is_not_a_vacuous_hit():
    c = RegisterCache(ttl=30, clock=lambda: 1000.0)
    c.update(100, [11])
    assert c.get_block(100, 0) is None  # must fall through to a live request, not "hit"
    assert c.hits == 0


def test_invalidate_drops_written_range():
    c = RegisterCache(ttl=30, clock=lambda: 1000.0)
    c.update(100, [1, 2, 3])
    c.invalidate(101, 1)
    assert c.get_block(100, 3) is None   # 101 evicted -> partial miss -> live read
    assert c.get_block(100, 1) == [1]    # untouched entries remain
