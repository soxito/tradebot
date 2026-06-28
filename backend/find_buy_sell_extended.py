import json
import os
import requests

URL = "http://127.0.0.1:1448/api/v1/signals/smc/generate"
EXCHANGES = [
    value.strip()
    for value in os.getenv("SMC_SWEEP_EXCHANGES", "bitget").split(",")
    if value.strip()
]
TIMEFRAMES = ["5m", "15m", "1h", "4h", "1d"]
SYMBOLS = [
    "BTC/USDT",
    "ETH/USDT",
    "SOL/USDT",
    "XRP/USDT",
    "BNB/USDT",
    "ADA/USDT",
    "DOGE/USDT",
    "AVAX/USDT",
    "LINK/USDT",
    "MATIC/USDT",
    "TRX/USDT",
    "TON/USDT",
    "DOT/USDT",
    "NEAR/USDT",
    "ATOM/USDT",
    "LTC/USDT",
    "BCH/USDT",
    "AAVE/USDT",
    "ARB/USDT",
    "OP/USDT",
    "SUI/USDT",
    "SEI/USDT",
    "RUNE/USDT",
    "FET/USDT",
    "INJ/USDT",
    "PEPE/USDT",
    "WIF/USDT",
    "BONK/USDT",
]


def safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def directional_buy_ok(row):
    if not row:
        return False
    br = safe_float(row.get("buy_ratio"))
    return row.get("directional_confirmed") is True and br is not None and br >= 0.55


def directional_sell_ok(row):
    if not row:
        return False
    br = safe_float(row.get("buy_ratio"))
    return row.get("directional_confirmed") is True and br is not None and br <= 0.45


def fetch(exchange, symbol, timeframe):
    payload = {
        "symbol": symbol,
        "timeframe": timeframe,
        "exchange": exchange,
        "limit": 200,
        "use_ai_agents": False,
        "use_insights": False,
        "persist_signal": False,
    }
    response = requests.post(URL, json=payload, timeout=30)
    response.raise_for_status()
    data = response.json()
    vc = data.get("volume_context") or {}
    eq = data.get("entry_quality") or {}
    return {
        "exchange": exchange,
        "symbol": symbol,
        "timeframe": timeframe,
        "action": (data.get("action") or "WAIT").upper(),
        "confidence": data.get("confidence"),
        "score": data.get("score"),
        "entry_price": data.get("entry_price"),
        "volume_ratio": vc.get("volume_ratio"),
        "buy_ratio": vc.get("buy_ratio"),
        "directional_confirmed": vc.get("directional_confirmed"),
        "volume_confirmed": vc.get("volume_confirmed"),
        "entry_quality_label": eq.get("label"),
        "entry_quality_reasons": eq.get("reasons"),
    }


def main():
    counts = {"BUY": 0, "SELL": 0, "HOLD": 0, "WAIT": 0}
    errors = []
    buy_pass = None
    sell_pass = None
    hold_samples = []
    best_score_row = None
    worst_score_row = None

    for exchange in EXCHANGES:
        for timeframe in TIMEFRAMES:
            for symbol in SYMBOLS:
                try:
                    row = fetch(exchange, symbol, timeframe)
                except Exception as exc:
                    errors.append({"exchange": exchange, "symbol": symbol, "timeframe": timeframe, "error": str(exc)})
                    continue

                action = row["action"]
                counts[action] = counts.get(action, 0) + 1

                score = safe_float(row.get("score"))
                if score is not None:
                    if best_score_row is None or score > safe_float(best_score_row.get("score")):
                        best_score_row = row
                    if worst_score_row is None or score < safe_float(worst_score_row.get("score")):
                        worst_score_row = row

                if action == "BUY" and buy_pass is None and directional_buy_ok(row):
                    buy_pass = row

                if action == "SELL" and sell_pass is None and directional_sell_ok(row):
                    sell_pass = row

                if action == "HOLD" and len(hold_samples) < 5:
                    hold_samples.append(row)

                if buy_pass and sell_pass and len(hold_samples) >= 5:
                    break

            if buy_pass and sell_pass and len(hold_samples) >= 5:
                break

        if buy_pass and sell_pass and len(hold_samples) >= 5:
            break

    print("ACTION COUNTS:")
    print(json.dumps(counts, indent=2))

    print("\nBUY PASS CANDIDATE:")
    print(json.dumps(buy_pass, indent=2) if buy_pass else "not found")

    print("\nSELL PASS CANDIDATE:")
    print(json.dumps(sell_pass, indent=2) if sell_pass else "not found")

    print("\nHOLD SAMPLES:")
    for row in hold_samples:
        print(json.dumps(row, indent=2))

    print("\nBEST SCORE SAMPLE:")
    print(json.dumps(best_score_row, indent=2) if best_score_row else "none")

    print("\nWORST SCORE SAMPLE:")
    print(json.dumps(worst_score_row, indent=2) if worst_score_row else "none")

    print("\nERROR COUNT:", len(errors))
    if errors:
        print("FIRST 5 ERRORS:")
        for err in errors[:5]:
            print(json.dumps(err, indent=2))

    print("\nVERDICT:")
    if buy_pass and sell_pass:
        print("PASS - Found directional-confirmed BUY and SELL samples.")
    elif sell_pass:
        print("PARTIAL - Found directional-confirmed SELL but no directional-confirmed BUY in current sweep.")
    else:
        print("INCONCLUSIVE - Did not find directional-confirmed BUY/SELL pair in current sweep.")


if __name__ == "__main__":
    main()
