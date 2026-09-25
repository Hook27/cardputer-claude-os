"""Unit tests for mcp/ratelimit.py — the per-agent notify backstop.

Pure: a fake clock drives time, so there is no real sleeping and the
behaviour is fully deterministic.
"""

from ratelimit import MinIntervalLimiter


class _Clock:
    """Manually-advanced monotonic clock stand-in."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def test_first_event_always_allowed():
    lim = MinIntervalLimiter(60, clock=_Clock())
    assert lim.allow("claude-code") is True


def test_second_immediate_event_denied():
    lim = MinIntervalLimiter(60, clock=_Clock())
    assert lim.allow("a") is True
    assert lim.allow("a") is False


def test_allowed_again_after_interval():
    clk = _Clock()
    lim = MinIntervalLimiter(60, clock=clk)
    assert lim.allow("a") is True
    clk.advance(59.9)
    assert lim.allow("a") is False
    clk.advance(0.2)  # 60.1s since the allowed event
    assert lim.allow("a") is True


def test_independent_buckets_per_key():
    lim = MinIntervalLimiter(60, clock=_Clock())
    assert lim.allow("a") is True
    assert lim.allow("b") is True  # different agent -> own bucket
    assert lim.allow("a") is False


def test_zero_interval_disables_limiting():
    lim = MinIntervalLimiter(0, clock=_Clock())
    for _ in range(5):
        assert lim.allow("a") is True


def test_negative_interval_treated_as_disabled():
    lim = MinIntervalLimiter(-5)
    assert lim.allow("a") is True
    assert lim.allow("a") is True


def test_denied_attempt_does_not_slide_window():
    # A denied attempt must NOT reset the clock — otherwise a spamming agent
    # that keeps trying would push its own next-allowed time forward forever.
    # The window is always measured from the last *allowed* event.
    clk = _Clock()
    lim = MinIntervalLimiter(60, clock=clk)
    assert lim.allow("a") is True       # t=0   allowed
    clk.advance(30)
    assert lim.allow("a") is False      # t=30  denied (no slide)
    clk.advance(30)
    assert lim.allow("a") is True       # t=60  60s since the allowed event


def test_min_interval_s_property():
    assert MinIntervalLimiter(45).min_interval_s == 45.0
    assert MinIntervalLimiter(-1).min_interval_s == 0.0
