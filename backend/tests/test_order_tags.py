"""Every app order is tagged so it can be told apart from a manual one.

An MT5 position's comment is fixed at execution — ``OrderModifySafe`` and MT5's
own ``TRADE_ACTION_SLTP`` carry price/SL/TP and nothing else — so positions
already open cannot be re-labelled on the broker. They are classified from the
comment they already carry instead, which is why the legacy tag formats have to
keep matching.
"""
from __future__ import annotations

import pytest

from app.trading.order_tags import (
    MT5_COMMENT_MAX,
    SOURCE_ROOM,
    SOURCE_SCALP,
    SOURCE_SMC,
    SOURCE_TELEGRAM,
    build_comment,
    classify,
    describe,
    is_app_order,
)


# ── building ────────────────────────────────────────────────────────────────

def test_telegram_orders_carry_their_signal_id():
    assert build_comment(SOURCE_TELEGRAM, 154270) == "TG#154270"


def test_room_orders_carry_their_reference():
    assert build_comment(SOURCE_ROOM, 42) == "ROOM#42"


def test_comment_is_clipped_to_what_mt5_keeps():
    comment = build_comment(SOURCE_TELEGRAM, "9" * 60)
    assert len(comment) <= MT5_COMMENT_MAX
    assert comment.startswith("TG#")


def test_an_unknown_source_is_rejected_rather_than_silently_untagged():
    with pytest.raises(ValueError):
        build_comment("mystery", 1)


# ── classifying ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("comment,source,ref", [
    ("TG#154270", SOURCE_TELEGRAM, "154270"),
    ("ROOM#42", SOURCE_ROOM, "42"),
    ("SCALP#7", SOURCE_SCALP, "7"),
    ("SMC#3", SOURCE_SMC, "3"),
])
def test_app_orders_are_recognised(comment, source, ref):
    info = classify(comment)
    assert info["origin"] == "app"
    assert info["source"] == source
    assert info["ref"] == ref


@pytest.mark.parametrize("comment,source", [
    ("ScalpBot#334", SOURCE_SCALP),
    ("ScalpBot-R#334", SOURCE_SCALP),      # recovery variant
    ("room leader 82%", SOURCE_ROOM),
])
def test_legacy_tags_still_classify_as_app_orders(comment, source):
    """Positions opened before the tags were unified must not flip to manual."""
    info = classify(comment)
    assert info["origin"] == "app"
    assert info["source"] == source


@pytest.mark.parametrize("comment", [
    None, "", "   ", "my own trade", "gold long", "hedge", "[sl]", "tp1",
])
def test_anything_untagged_is_treated_as_manual(comment):
    info = classify(comment)
    assert info["origin"] == "manual"
    assert info["source"] == "manual"
    assert not is_app_order(comment)


def test_a_broker_suffix_does_not_break_recognition():
    """Brokers sometimes append their own text to the comment."""
    assert classify("TG#154270 [sl 4350]")["ref"] == "154270"
    assert is_app_order("TG#154270 [sl 4350]")


def test_round_trip_from_build_to_classify():
    for source in (SOURCE_TELEGRAM, SOURCE_ROOM, SOURCE_SCALP, SOURCE_SMC):
        info = classify(build_comment(source, 99))
        assert info["origin"] == "app"
        assert info["source"] == source
        assert info["ref"] == "99"


# ── labelling ───────────────────────────────────────────────────────────────

def test_labels_name_what_opened_the_position():
    assert describe("TG#154270") == "Telegram signal 154270"
    assert describe("ROOM#42") == "Trading room 42"
    assert describe("ScalpBot#334") == "Scalp bot 334"
    assert describe("") == "Manual order"
    assert describe("my own trade") == "Manual order"
