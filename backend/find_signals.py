import requests
import json

url = "http://127.0.0.1:1448/api/v1/signals/smc/generate"
symbols = [
    "BTC/USDT",
    "ETH/USDT",
    "XRP/USDT",
    "SOL/USDT",
    "BNB/USDT",
    "ADA/USDT",
    "DOGE/USDT",
    "AVAX/USDT",
    "LINK/USDT",
    "MATIC/USDT",
]
timeframes = ["5m", "15m", "1h", "4h"]

buy_signal = None
sell_signal = None
hold_samples = []
all_rows = []
action_counts = {"BUY": 0, "SELL": 0, "HOLD": 0, "WAIT": 0}

def get_signal(symbol, tf):
    payload = {
        "symbol": symbol,
        "timeframe": tf,
        "exchange": "bitget",
        "limit": 200,
        "use_ai_agents": False,
        "use_insights": False,
        "persist_signal": False
    }
    try:
        r = requests.post(url, json=payload, timeout=30).json()
        if not r or "data" not in r:
            # Handle cases where the structure might be different or error
            if isinstance(r, dict) and "action" in r:
                return r
            return None
        return r["data"] if "data" in r else r
    except Exception as e:
        return None

for tf in timeframes:
    for symbol in symbols:
        res = get_signal(symbol, tf)
        if not res:
            continue
        
        action = (res.get("action") or "WAIT").upper()
        entry_quality = res.get("entry_quality") if isinstance(res.get("entry_quality"), dict) else {}
        volume_context = res.get("volume_context") if isinstance(res.get("volume_context"), dict) else {}
        row = {
            "symbol": symbol,
            "timeframe": tf,
            "action": action,
            "confidence": res.get("confidence"),
            "score": res.get("score"),
            "entry_price": res.get("entry_price"),
            "volume_ratio": volume_context.get("volume_ratio"),
            "buy_ratio": volume_context.get("buy_ratio"),
            "directional_confirmed": volume_context.get("directional_confirmed"),
            "volume_confirmed": volume_context.get("volume_confirmed"),
            "entry_quality_label": entry_quality.get("label"),
            "entry_quality_reasons": entry_quality.get("reasons"),
        }
        all_rows.append(row)
        action_counts[action] = action_counts.get(action, 0) + 1
        
        if action == "BUY" and not buy_signal:
            buy_signal = row
        elif action == "SELL" and not sell_signal:
            sell_signal = row
        if action == "HOLD" and len(hold_samples) < 5:
            hold_samples.append(row)

print("ACTION COUNTS:")
print(json.dumps(action_counts, indent=2))

def as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def buy_direction_ok(row):
    if not row:
        return False
    br = as_float(row.get("buy_ratio"))
    return row.get("directional_confirmed") is True and br is not None and br >= 0.55

def sell_direction_ok(row):
    if not row:
        return False
    br = as_float(row.get("buy_ratio"))
    return row.get("directional_confirmed") is True and br is not None and br <= 0.45

print("SELECTED SIGNALS:")
if buy_signal:
    print(f"BUY: {json.dumps(buy_signal, indent=2)}")
else:
    print("BUY: not found")

if sell_signal:
    print(f"SELL: {json.dumps(sell_signal, indent=2)}")
else:
    print("SELL: not found")

print("\nDIRECTIONAL CHECKS:")
print(f"BUY directional PASS: {buy_direction_ok(buy_signal)}")
print(f"SELL directional PASS: {sell_direction_ok(sell_signal)}")

print("\nHOLD SAMPLES:")
for s in hold_samples[:5]:
    print(json.dumps(s, indent=2))

if not hold_samples:
    print("No HOLD samples found.")

print("\nVERDICT:")
if buy_signal and sell_signal and buy_direction_ok(buy_signal) and sell_direction_ok(sell_signal):
    print("PASS - Found BUY and SELL with directional confirmation aligned to thresholds.")
elif buy_signal and sell_signal:
    print("PARTIAL - Found BUY and SELL but one or both directional checks failed.")
else:
    print("INCONCLUSIVE - Could not find both BUY and SELL in this sweep.")
