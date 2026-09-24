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
- **Paper-aware agents** — the Kronos paper fact-sheet (arXiv:2508.02739) is injected
  into the JARVIS analysis prompt and into every trading-room seat's forecast context,
  so all agents read the p10/p90 band as conviction and treat `NO_TRADE` as "no volume"
  rather than bearish.
- **Fine-tune on your own data** — a tokenizer → predictor pipeline (mirroring the
  upstream share-market example) exposed on the `/kronos-forecast` page, via REST, and
  through the Telegram `/finetune` command. Exports page through history and the
  context window auto-fits the data, so short histories train cleanly; every job is
  remembered in the JARVIS brains.
- **Auto-updates on launch** — `start.py` checks the upstream Kronos commit on every run
  and re-vendors the `model/` package when it changes.

## Setup (enable the real model)

**macOS / Linux**
```bash
bash plugins/KronosForecastPlugin/scripts/setup_kronos.sh
# optional: pre-download weights so the first forecast is instant
bash plugins/KronosForecastPlugin/scripts/setup_kronos.sh --predownload
```

**Windows (PowerShell)**
```powershell
powershell -ExecutionPolicy Bypass -File plugins\KronosForecastPlugin\scripts\setup_kronos.ps1
# optional: pre-download weights so the first forecast is instant
powershell -ExecutionPolicy Bypass -File plugins\KronosForecastPlugin\scripts\setup_kronos.ps1 -Predownload
```

> `setup_kronos.sh` is a bash script and will not run in PowerShell — use the
> `.ps1` version on Windows. Flags map as `--predownload` → `-Predownload`,
> `--all` → `-All`, `--test` → `-Test`, `--no-test` → `-NoTest`.

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
- `GET /api/v1/plugins/kronos/finetune/setup-status`
- `POST /api/v1/plugins/kronos/finetune/export`
- `POST /api/v1/plugins/kronos/finetune/start`
- `GET /api/v1/plugins/kronos/finetune/jobs`
- `GET /api/v1/plugins/kronos/finetune/jobs/{job_id}`
- `DELETE /api/v1/plugins/kronos/finetune/jobs/{job_id}`
- `POST /api/v1/plugins/kronos/finetune/load/{job_id}`

## Fine-tuning on your own data

Fine-tune Kronos on live OHLCV for a specific instrument, exactly like the
[share-market example](https://github.com/shiyu-coder/Kronos#-finetuning-on-your-own-data-a-share-market-example)
in the upstream repo (tokenizer, then predictor).

**Install the pipeline once** (vendors `finetune_csv/` from upstream):

```bash
bash plugins/KronosForecastPlugin/scripts/setup_kronos_finetune.sh
# Windows: powershell -ExecutionPolicy Bypass -File plugins\KronosForecastPlugin\scripts\setup_kronos_finetune.ps1
```

Then start a job from any surface:

- **Web** — the **Fine-tune on your own data** panel on `/kronos-forecast`
  (export/preview, hyper-parameters, Start, job history, live log, Load Model).
- **Telegram** — `/finetune BTCUSDT 1h` to start, `/finetune status [id]` to watch
  the training log, `/finetune load <id>` to use a finished model, `/finetune setup`
  to install the pipeline.
- **REST** — `POST /finetune/start`, poll `GET /finetune/jobs/{id}`, then
  `POST /finetune/load/{id}`.

Jobs export data → train the tokenizer → train the predictor as a background
process; a completed model can be hot-swapped into the engine so every subsequent
`/forecast` runs through it.

**Data & auto-fit (no more `__len__() should return >= 0` crashes).** A single
exchange OHLCV request caps at ~1000 candles, which is too few for a 512-bar
context once the train/val split is taken. The exporter now pages **backwards**
through history (crypto) to gather thousands of candles, and the trainer
**auto-fits** `lookback`/`pred_len`/`batch_size` to whatever history is available
so both the train and validation splits always yield samples. The fitted values
are saved on the job, so `/finetune load` uses the exact context the model was
trained with. More history ⇒ a larger context (up to the full 512).

**Remembered in the brains.** Every fine-tune milestone (started, completed,
failed, loaded) is written to the JARVIS knowledge brains (vault + AI-analyst +
PaulKnowledge), so training history persists across restarts and shows up in
`/intelligence`.

## Attribution

Kronos © its authors, MIT License. Paper: [arXiv:2508.02739](https://arxiv.org/abs/2508.02739).
The `model/` package is vendored (not redistributed in this repo) via `setup_kronos.sh`.
