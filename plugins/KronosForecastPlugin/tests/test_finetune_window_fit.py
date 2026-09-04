"""Regression tests for the fine-tune window auto-fit.

Guards the bug where a short live history (one exchange page ~1000 candles) plus
the default 512-bar context left the 10% validation split with a negative sample
count, crashing the trainer with ``__len__() should return >= 0``.
"""
from __future__ import annotations

import pytest

from plugins.KronosForecastPlugin.backend.services.finetune_service import _fit_window


def _splits_ok(rows: int, lookback: int = 512, pred_len: int = 48, batch: int = 32):
    f = _fit_window(rows, lookback, pred_len, batch)
    window = f["lookback"] + f["pred_len"] + 1
    train_samples = f["train_rows"] - window + 1
    val_samples = f["val_rows"] - window + 1
    return f, window, train_samples, val_samples


@pytest.mark.parametrize("rows", [260, 300, 500, 800, 1000, 2000, 5000, 8000])
def test_every_split_has_samples(rows: int) -> None:
    f, window, train_samples, val_samples = _splits_ok(rows)
    # The train loader uses drop_last=True → needs at least batch_size samples.
    assert train_samples >= f["batch_size"] >= 1
    # The val loader keeps its final partial batch → needs at least one sample.
    assert val_samples >= 1
    assert f["lookback"] >= 1 and f["pred_len"] >= 1


def test_reported_crash_scenario_no_longer_negative() -> None:
    # 1000 rows, default 512 lookback → val was -532 samples before the fix.
    f, _window, _train, val_samples = _splits_ok(1000)
    assert val_samples >= 1
    assert f["adjusted"] is True  # window was shrunk to fit


def test_ample_history_keeps_full_context() -> None:
    # With plenty of candles the fit should not shrink below the requested window.
    f, _window, _train, _val = _splits_ok(8000, lookback=512, pred_len=48)
    assert f["lookback"] == 512
    assert f["pred_len"] == 48
    assert f["adjusted"] is False
