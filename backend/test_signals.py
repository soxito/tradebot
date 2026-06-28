import requests
import json

base_url = "http://127.0.0.1:1448/api/v1/signals/smc/generate"
# Standard symbols usually don't have '/' in many backends, try just 'BTCUSDT' if 'BTC/USDT' fails
pairs = ["BTCUSDT", "ETHUSDT", "BTC/USDT", "ETH/USDT"]
timeframes = ["1h"]
exchange = "bitget" 

payload_base = {
    "use_ai_agents": False,
    "use_insights": False,
    "persist_signal": False,
    "limit": 200,
    "exchange": exchange
}

for symbol in pairs:
    for tf in timeframes:
        payload = payload_base.copy()
        payload.update({"symbol": symbol, "timeframe": tf})
        print(f"Testing {symbol} {tf}...")
        try:
            response = requests.post(base_url, json=payload, timeout=30)
            print(f"Status: {response.status_code}")
            if response.status_code == 200:
                print(f"Success! Response: {response.text[:200]}")
            else:
                print(f"Body: {response.text}")
        except Exception as e:
            print(f"Failed: {e}")
