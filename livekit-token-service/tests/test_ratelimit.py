import pytest

from livekit_tokens.errors import RateLimited
from livekit_tokens.ratelimit import SlidingWindow


def test_limit_is_inclusive_and_the_next_request_is_refused():
    w = SlidingWindow(3, 60)
    for _ in range(3):
        w.hit("a", 100)
    with pytest.raises(RateLimited) as e:
        w.hit("a", 100)
    assert e.value.status == 429 and e.value.extra["retry_after"] == 60


def test_window_slides_at_the_exact_boundary():
    w = SlidingWindow(2, 60)
    w.hit("a", 100)
    w.hit("a", 130)
    with pytest.raises(RateLimited) as e:
        w.hit("a", 159.5)
    assert e.value.extra["retry_after"] == 1  # the first hit leaves at t=160
    with pytest.raises(RateLimited):
        w.hit("a", 159.99)
    w.hit("a", 160)  # exactly window seconds later the first hit no longer counts


def test_retry_after_is_rounded_up_and_never_zero():
    w = SlidingWindow(1, 60)
    w.hit("a", 0)
    with pytest.raises(RateLimited) as e:
        w.hit("a", 59.2)
    assert e.value.extra["retry_after"] == 1
    with pytest.raises(RateLimited) as e:
        w.hit("a", 10)
    assert e.value.extra["retry_after"] == 50


def test_keys_are_independent():
    w = SlidingWindow(1, 60)
    w.hit("a", 0)
    w.hit("b", 0)
    with pytest.raises(RateLimited):
        w.hit("a", 1)


def test_refused_requests_do_not_extend_the_block():
    w = SlidingWindow(1, 60)
    w.hit("a", 0)
    for t in (10, 20, 30, 40, 50):
        with pytest.raises(RateLimited):
            w.hit("a", t)
    w.hit("a", 60)


def test_idle_keys_are_pruned():
    w = SlidingWindow(1, 60)
    for i in range(10_001):
        w.hit(f"user{i}", 0)
    w.hit("late", 1000)
    assert len(w._hits) < 10  # only the fresh key is left


def test_retry_after_rounds_up_fractions():
    w = SlidingWindow(1, 60)
    w.hit("a", 0)
    with pytest.raises(RateLimited) as e:
        w.hit("a", 10.5)  # 49.5 s left
    assert e.value.extra["retry_after"] == 50
