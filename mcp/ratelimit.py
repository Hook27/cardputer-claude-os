"""Per-agent minimum-interval rate limiter for the cardputer-mcp daemon.

Why this exists: many agents now share one daemon and one physical device.
The `cardputer-companion` skill asks agents to self-throttle notifications
("default to silence"), but self-restraint is etiquette, not enforcement — a
buggy or adversarial agent can still bury the banner. This is the backstop:
a simple per-agent minimum interval between non-critical `notify`s, so one
chatty agent can't drown the device or starve another agent's alerts.

It is intentionally tiny and dependency-free (a sibling to `auth.py`): a dict
of last-allowed timestamps keyed by agent label, compared against an
injectable monotonic clock so tests are deterministic. The policy decisions
that *don't* belong here live at the call site in `server.py`:

  - `crit` notifications bypass the limiter (a real emergency always rings).
  - the blocking tools (`ask`/`confirm`) are never rate-limited — they are
    deliberate, user-driven round-trips, not fire-and-forget banners.

This class only knows about opaque keys and one interval.
"""

from __future__ import annotations

import time
from typing import Callable, Dict, Optional


class MinIntervalLimiter:
    """Allow at most one event per ``min_interval_s`` per key.

    Usage::

        limiter = MinIntervalLimiter(60)
        if limiter.allow("claude-code"):
            ...  # do the rate-limited thing

    The first event for any key always passes. A *denied* call does NOT slide
    the window forward — the interval is always measured from the last
    *allowed* event, so an agent that keeps hammering can't push its own
    next-allowed time further and further out. ``min_interval_s <= 0``
    disables limiting entirely (every call allowed).
    """

    def __init__(
        self,
        min_interval_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._min = max(0.0, float(min_interval_s))
        self._clock = clock
        self._last: Dict[str, float] = {}

    @property
    def min_interval_s(self) -> float:
        return self._min

    def allow(self, key: str) -> bool:
        """Return True (and record the time) if an event for ``key`` is
        allowed now; False otherwise.

        Async-safety: callers are ``async`` tools sharing one daemon, but the
        read-check-write below has no ``await`` point, so asyncio never
        preempts mid-update and the plain dict is safe. Preserve that
        invariant (or add an ``asyncio.Lock``) if this ever gains an await.
        """
        if self._min <= 0:
            return True
        now = self._clock()
        last: Optional[float] = self._last.get(key)
        if last is not None and (now - last) < self._min:
            return False
        self._last[key] = now
        return True
