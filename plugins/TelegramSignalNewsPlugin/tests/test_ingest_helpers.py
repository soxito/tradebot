from plugins.TelegramSignalNewsPlugin.backend.services.ingest_service import (
    compute_dedupe_hash,
    max_message_id,
    normalize_handle,
)


def test_normalize_handle_variants():
    assert normalize_handle("crypto_signals") == "@crypto_signals"
    assert normalize_handle("@macro_news") == "@macro_news"
    assert normalize_handle("https://t.me/marketflow") == "@marketflow"
    assert normalize_handle("-1001234567890") == "-1001234567890"


def test_max_message_id_numeric_precedence():
    assert max_message_id("100", "102") == "102"
    assert max_message_id(None, "50") == "50"


def test_dedupe_hash_is_stable_for_same_payload():
    one = compute_dedupe_hash(4, "900", "BUY BTCUSDT")
    two = compute_dedupe_hash(4, "900", "BUY BTCUSDT")
    three = compute_dedupe_hash(4, "901", "BUY BTCUSDT")

    assert one == two
    assert one != three
