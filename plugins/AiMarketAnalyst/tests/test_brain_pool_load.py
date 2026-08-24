"""An unkeyed brain role must borrow the *least loaded* shared key.

The failure this prevents: a brain role with no key of its own reaching for
whichever provider the pool happened to list first — which, ordered by priority,
is the busiest one. The roles that do have keys then queue behind a rate limit
they never caused.
"""

from __future__ import annotations

import sys

sys.path.insert(0, "/Users/sakhilematsimela/Sites/tradebot/backend")

from app.api.jarvis import _brain_load  # noqa: E402


class _P:
    def __init__(self, pid, daily=0, monthly=0, daily_limit=None,
                 monthly_limit=None, status="ok"):
        self.id = pid
        self.daily_calls = daily
        self.monthly_calls = monthly
        self.daily_limit = daily_limit
        self.monthly_limit = monthly_limit
        self.status = status

    def __repr__(self):
        return f"P{self.id}"


def _order(providers):
    return [p.id for p in sorted(providers, key=_brain_load)]


def test_least_used_uncapped_provider_comes_first():
    assert _order([_P(1, daily=800), _P(2, daily=10), _P(3, daily=300)]) == [2, 3, 1]


def test_headroom_beats_raw_call_count():
    """200/10000 has far more left than 200/250 — raw counts would tie them."""
    roomy = _P(1, daily=200, daily_limit=10_000)
    nearly_spent = _P(2, daily=200, daily_limit=250)
    assert _order([nearly_spent, roomy]) == [1, 2]


def test_monthly_cap_is_used_when_there_is_no_daily_cap():
    assert _order([
        _P(1, monthly=9_000, monthly_limit=10_000),
        _P(2, monthly=100, monthly_limit=10_000),
    ]) == [2, 1]


def test_a_busy_uncapped_key_does_not_always_look_cheapest():
    """Uncapped providers are scaled onto the same 0-1 axis as capped ones."""
    busy_uncapped = _P(1, daily=900)                       # ~0.90 of a nominal day
    fresh_capped = _P(2, daily=10, daily_limit=1000)       # 0.01 of its cap
    assert _order([busy_uncapped, fresh_capped]) == [2, 1]


def test_a_failing_provider_does_not_win_by_being_idle():
    """The trap: a provider that rejects everything accrues no calls.

    Ranked on load alone it looks like the emptiest key available, so every
    unkeyed brain role would be sent straight at the one thing known to be down.
    """
    broken_but_idle = _P(1, daily=0, status="error")
    healthy_but_busy = _P(2, daily=400, status="ok")
    assert _order([broken_but_idle, healthy_but_busy]) == [2, 1]


def test_failing_providers_are_still_ordered_among_themselves():
    """Last resort is still the least-loaded of a bad set, not an arbitrary one."""
    assert _order([
        _P(1, daily=900, status="error"),
        _P(2, daily=5, status="error"),
    ]) == [2, 1]


def test_ordering_is_stable_for_equally_loaded_providers():
    """Concurrent roles must not see the pool reshuffle between them."""
    pool = [_P(3), _P(1), _P(2)]
    assert _order(pool) == [1, 2, 3] == _order(list(reversed(pool)))


def test_slot_spread_still_gives_distinct_roles_distinct_providers():
    """Least-used ordering must not collapse every role onto one key."""
    pool = sorted([_P(1, daily=500), _P(2, daily=5), _P(3, daily=50)], key=_brain_load)
    picks = {slot: pool[slot % len(pool)].id for slot in range(5)}
    # Three providers, five roles: the first three roles are all distinct.
    assert len({picks[0], picks[1], picks[2]}) == 3
    # And the least loaded is what slot 0 reaches for.
    assert picks[0] == 2
