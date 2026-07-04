# Kronos Forecast Plugin

K-line (OHLCV) price forecasting for TradeBot, powered by the open-source
**Kronos** foundation model ([shiyu-coder/Kronos](https://github.com/shiyu-coder/Kronos), MIT).

Kronos is a decoder-only transformer pre-trained on candlestick data from 45+
exchanges. It tokenizes OHLCV candles and autoregressively predicts future
candles, giving a forward-looking price path with probabilistic confidence bands.

## What it adds

- **`/kronos-forecast` page** — pick a pair/timeframe/horizon and see the predicted
  candles drawn as an overlay on the live chart, with a direction + confidence signal.
- **Chart overlays everywhere** — `/plugins/kronos/overlay/{exchange}/{symbol}` returns
  `overlays` + `markers` in the exact shape `TradingViewChart` consumes, so a forecast
  can be layered onto any chart in the app.
- **JARVIS integration** — "Jarvis, forecast BTC" (voice/chat) and the `analyze SYMBOL`
  command now include a Kronos ML forecast alongside EMA/RSI. The backend hook is
  graceful: if this plugin is removed, JARVIS keeps working unchanged.
- **Heuristic fallback** — until the model is installed, forecasts use a drift+volatility
  random walk so the UI and JARVIS work out of the box.

## Setup (enable the real model)

```bash
bash plugins/KronosForecastPlugin/scripts/setup_kronos.sh
# optional: pre-download weights so the first forecast is instant
bash plugins/KronosForecastPlugin/scripts/setup_kronos.sh --predownload
```

This installs `torch` + friends into `backend/.venv`, vendors the MIT-licensed
Kronos `model/` package into `backend/vendor/model`, then you restart the backend:

```bash
lsof -ti :1448 | xargs kill -9; ./run-local.sh backend --brew
curl http://localhost:1448/api/v1/plugins/kronos/status
```

## Configuration (env vars)

| Var | Default | Notes |
|-----|---------|-------|
| `KRONOS_MODEL_NAME` | `NeoQuasar/Kronos-small` | `-mini` fastest, `-base` most accurate |
| `KRONOS_TOKENIZER_NAME` | `NeoQuasar/Kronos-Tokenizer-base` | `-2k` for `-mini` |
| `KRONOS_DEVICE` | auto (mps/cuda/cpu) | force with e.g. `cpu` |
| `KRONOS_DEFAULT_PRED_LEN` | `24` | future candles |
| `KRONOS_DEFAULT_SAMPLES` | `10` | sampled paths → band width |

## Endpoints

- `GET /api/v1/plugins/kronos/status`
- `GET /api/v1/plugins/kronos/forecast/{exchange}/{symbol}?timeframe=&pred_len=&samples=`
- `GET /api/v1/plugins/kronos/overlay/{exchange}/{symbol}`
- `GET /api/v1/plugins/kronos/jarvis/{symbol}`
- `POST /api/v1/plugins/kronos/batch`

## Attribution

Kronos © its authors, MIT License. Paper: [arXiv:2508.02739](https://arxiv.org/abs/2508.02739).
The `model/` package is vendored (not redistributed in this repo) via `setup_kronos.sh`.
