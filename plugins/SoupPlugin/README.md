# Soup LLM Finetuning Plugin

Fine-tune and post-train **any** Hugging Face causal LM in one command via [MakazhanAlpamys/Soup](https://github.com/MakazhanAlpamys/Soup) (`trysoup.dev`), now integrated into TradeBot.

## What it provides
- **Isolated install**: clones `https://github.com/MakazhanAlpamys/Soup.git` → `integrations/soup` + venv `~/.tradebot/soup-venv` (never touches `backend/.venv`). Auto-updates on every `python start.py` (like Kronos), with `TRADEBOT_SKIP_SOUP_SETUP=1` / `TRADEBOT_SKIP_SOUP_UPDATE=1` opt-outs.
- **Hardware-adaptive**: `start.py:_soup_recommend_resources()` + plugin `recommendation()` probe RAM/VRAM + MPS/CUDA, then auto-pick `quantization=4bit, stream_layers=True (8B on 4GB), batch=auto` on weak hosts, `bf16` on 16GB+ VRAM.
- **Backend plugin** (`/api/v1/plugins/soup/*`): `status`, `models` (unified Kronos catalogue + finetuned + HF cache + Soup adapters), `recipes`, `finetune/start`, `finetune/jobs`, `data/upload`, `ohlcv/export` (experimental), `ui/proxy` (proxied auth — token never leaks), `health`, `serve/start`, `check-update`, `kronos/overview`.
- **Frontend**:
  - `/soup-finetune` — full Soup Lab (status, recommendation, unified models, training form, dataset upload, OHLCV→JSONL bridge, job table, log, **embedded Soup UI via `/api/v1/plugins/soup/ui/proxy`** on :7860).
  - `/kronos-finetune` — now tabbed: **Kronos (OHLCV)** | **Soup (LLM)**. Kronos tab unchanged; Soup tab is a condensed quick-train + embedded UI + link to full lab. Both satisfy "both" (6.3).
  - Sidebar: `Kronos Forecast → Fine-tune Lab` + `Soup Lab` (also top-level).
- **Experimental OHLCV→LLM bridge**: `soup_ohlcv_exporter.export_ohlcv_to_jsonl()` reuses Kronos `finetune_data_exporter` cascade (MT5→Yahoo→ccxt) to window OHLCV into ShareGPT JSONL for Soup SFT. Train a small causal LM (e.g. `Qwen2.5-0.5B`) and compare forecast side-by-side with Kronos as a dashed band on `/kronos-forecast` (future overlay; currently preview via `ohlcv/preview`).

## Install & run
```bash
# One-time (auto-runs on start.py anyway)
bash plugins/SoupPlugin/scripts/setup_soup.sh          # macOS/Linux
# or
powershell -ExecutionPolicy Bypass -File plugins\SoupPlugin\scripts\setup_soup.ps1  # Windows

# Verify
~/.tradebot/soup-venv/bin/soup doctor
~/.tradebot/soup-venv/bin/soup ui --host 127.0.0.1 --port 7860 --no-browser  # http://localhost:7860

# Docker (optional sidecar, like hermes)
docker compose --profile soup up -d soup  # :8012 health, :7860 UI
```

## API examples
```bash
curl http://127.0.0.1:1448/api/v1/plugins/soup/status | jq
curl http://127.0.0.1:1448/api/v1/plugins/soup/models | jq
curl -X POST http://127.0.0.1:1448/api/v1/plugins/soup/finetune/start \
  -H 'Content-Type: application/json' \
  -d '{"base":"Qwen/Qwen2.5-0.5B-Instruct","task":"sft","epochs":1,"dataset_inline":[{"messages":[{"role":"user","content":"hi"},{"role":"assistant","content":"hello"}]}]}'

# OHLCV bridge (experimental)
curl -X POST http://127.0.0.1:1448/api/v1/plugins/soup/ohlcv/export \
  -d '{"symbol":"BTC/USD","timeframe":"1h","limit":2000}' -H 'Content-Type: application/json' | jq
```

## Decision notes (6.1–6.5)
- 6.1: agents-only primary + OHLCV experimental (side-by-side, not replacement)
- 6.2: Kronos models = `Kronos-mini/small/base` (4.1/24.7/102.3M) + finetuned `finetuned:<id>` + HF cache + Soup adapters → one `models` aggregation
- 6.3: both = tab in `/kronos-finetune` + standalone `/soup-finetune`
- 6.4: adaptive RAM/VRAM → best quality for this hardware
- 6.5: proxied auth (`SOUP_UI_TOKEN` server-injected, never in browser)

## Files
- `plugin.json` — slug `soup`, `service_provider: backend.router`
- `backend/models.py` — `soup_finetune_jobs`
- `backend/router.py` — all routes
- `backend/services/soup_utils.py` — paths, version, recommendation, unified models
- `backend/services/soup_service.py` — manager (jobs, YAML, subprocess `soup train`)
- `backend/services/soup_ohlcv_exporter.py` — OHLCV→JSONL
- `scripts/setup_soup.sh|.ps1` — isolated venv + clone
- `frontend/src/pages/soup-finetune.tsx` — full lab
- `frontend/src/pages/kronos-finetune.tsx` — tabbed
