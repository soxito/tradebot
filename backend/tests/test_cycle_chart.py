"""Fisher transform + the cycle chart payload's shape."""
from __future__ import annotations

import math

import pandas as pd
import pytest

from app.signals.technical import fisher


def _df(closes):
    return pd.DataFrame({
        "timestamp": pd.to_datetime([i * 60_000 for i in range(len(closes))], unit="ms", utc=True),
        "open": closes,
        "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes],
        "close": closes,
        "volume": [1.0] * len(closes),
    })


def test_fisher_is_finite_and_bounded_in_normal_ranges():
    closes = [100 + math.sin(i / 5) * 5 + i * 0.05 for i in range(120)]
    f, trigger = fisher(_df(closes))
    assert len(f) == len(trigger) == 120
    assert f.notna().all()
    # The transform maps to a near-Gaussian: |values| beyond ~4 are rare on a
    # sine input, and a hard clamp on the raw keeps it finite regardless.
    assert f.abs().max() < 10.0


def test_fisher_turns_at_price_turns():
    """A V-shaped series flips the fisher's slope at the bottom."""
    closes = [100 - i for i in range(30)] + [71 + i for i in range(30)]
    f, _ = fisher(_df(closes))
    vals = [float(v) for v in f]
    bottom = vals.index(min(vals))
    assert 25 <= bottom <= 35  # near the actual turn, within the smoothing lag


def test_fisher_handles_flat_series():
    closes = [100.0] * 40
    f, trigger = fisher(_df(closes))
    assert f.notna().all()
    assert float(f.abs().max()) == 0.0


def test_fisher_never_raises_on_tiny_input():
    f, trigger = fisher(_df([100.0, 101.0, 99.0]))
    assert len(f) == 3
