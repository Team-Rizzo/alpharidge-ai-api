from app.utils.rate_limit import TokenBuckets, client_key


def test_client_key():
    assert client_key("203.0.113.7") == "203.0.113.7"
    assert client_key("::ffff:203.0.113.7") == "203.0.113.7"
    assert client_key("2001:db8::1") == "2001:db8::/64"
    assert client_key(None) == "-"


def test_buckets_refuse_past_burst():
    t = [0.0]
    b = TokenBuckets(1, 2, clock=lambda: t[0])
    assert b.take("a") == 0 and b.take("a") == 0
    assert b.take("a") > 0
    t[0] += 1
    assert b.take("a") == 0
