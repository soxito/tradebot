import requests
import json
import sys

BASE_URL = "http://127.0.0.1:1448/api/v1"

def get_signal(symbol, timeframe):
    payload = {
        "symbol": symbol,
        "timeframe": timeframe,
        "exchange": "bitget",
        "limit": 200,
        "use_ai_agents": False,
        "use_insights": False,
        "persist_signal": False
    }
    try:
        response = requests.post(f"{BASE_URL}/signals/smc/generate", json=payload)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"Error for {symbol} {timeframe}: {e}")
        return None

def print_result(data):
    if not data: return
    # Extract fields as requested
    res = {
        "symbol": data.get("symbol"),
        "action": data.get("action"),
        "confidence": data.get("confidence"),
        "score": data.get("score"),
        "entry_price": data.get("entry_price"),
        "volume_context": {
            "volume_ratio": data.get("volume_context", {}).get("volume_ratio"),
            "buy_ratio": data.get("volume_context", {}).get("buy_ratio"),
            "directional_confirmed": data.get("volume_context", {}).get("directional_confirmed"),
            "volume_confirmed": data.get("volume_context", {}).get("volume_confirmed")
        },
        "entry_quality": {
            "label": data.get("entry_quality", {}).get("label"),
            "reasons": data.get("entry_quality", {}).get("reasons")
        }
    }
    print(json.dumps(res, indent=2))
    return res

results = []
for pair in ["BTC/USDT", "ETH/USDT"]:
    data = get_signal(pair, "15m")
    if data and data.get("action") == "HOLD":
        data = get_signal(pair, "1h")
    if data:
        results.append(data)
        print_result(data)

