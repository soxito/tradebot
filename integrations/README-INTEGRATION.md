# TradingAgents — TradeBot Integration

Full integration of [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents)
(the multi-agent LLM trading framework: analyst team → bull/bear debate →
trader → risk debate → portfolio manager) into the tradebot app.

## Architecture

```
┌──────────────┐  HTTP/SSE   ┌─────────────────┐  in-process   ┌────────────────────────┐
│ Trading Room │ ──────────► │ FastAPI backend  │ ────────────► │ TradingAgents sidecar  │
│ (Next.js)    │             │ /api/v1/         │               │ :8010                  │
│              │             │ tradingagents/*  │               │ integrations/…/.venv   │
└──────────────┘             └─────────────────┘               │ tradingagents==0.7.0   │
                                                              └────────────────────────┘
```

The framework's heavy dependency stack (langchain 1.x, langgraph, pandas 3.x,
textual, backtrader…) **never loads into the backend process** — it runs in an
isolated venv as a child process (or a docker-compose service). The upstream
source is cloned at `integrations/TradingAgents/` for reference; runtime uses
the PyPI release `tradingagents==0.7.0`, which is newer than GitHub main and
exposes native `on_message` / `on_state` streaming callbacks.

## Components

| Piece | Path | Role |
|---|---|---|
| Sidecar service | `backend/tradingagents_service/` | FastAPI wrapper: start runs, SSE progress, final dossier |
| Proxy router | `backend/app/api/tradingagents.py` | `/api/v1/tradingagents/*`, durable persistence to DB |
| HTTP client | `backend/app/services/tradingagents_client.py` | blocking run helper used by pre-trade validation |
| DB model | `tradingagents_runs` table | every run + full reports, survives restarts |
| Orchestrator hook | `backend/app/agents/orchestrator.py` | when AI Provider = TradingAgents, validates via sidecar & persists |
| UI desk | `frontend/src/components/tradingagents/` | live pipeline, report viewer, history — embedded in the Trading Room |
| Setup | `start.py` (`setup_tradingagents_sidecar`) | one-off clone + isolated venv bootstrap |

## Using it

1. **Setup** (one-off): `python3 start.py` creates `integrations/TradingAgents/.venv`
   with full deps, or do it manually:
   ```bash
   git clone https://github.com/TauricResearch/TradingAgents.git integrations/TradingAgents
   cd integrations/TradingAgents && python3.13 -m venv .venv
   .venv/bin/pip install "tradingagents==0.7.0" fastapi "uvicorn[standard]" sse-starlette loguru python-dotenv httpx
   ```
2. **Keys**: set at least one provider key in `.env` (`OPENAI_API_KEY`,
   `OPENROUTER_API_KEY`, …). The sidecar loads the workspace `.env` itself.
3. **Run**: start the app normally — the backend spawns the sidecar on boot
   (`TRADINGAGENTS_SERVICE_AUTOSTART=true`). Manual start:
   ```bash
   cd backend && PYTHONPATH=. ../integrations/TradingAgents/.venv/bin/python -m tradingagents_service.main
   ```
4. **Trade Room**: open `/trading-room` → “TradingAgents desk” button on the
   right rail → enter ticker (`AAPL`, `BTC/USDT`, `0700.HK`) → *Convene
   TradingAgents*. Watch phases light up, read each report, revisit history.
5. **Auto-trading**: Settings → AI Provider = `TradingAgents` routes pre-trade
   validation through the same pipeline; every validation is persisted with its
   complete dossier (`source = trade_validation`).

## API surface

Sidecar (`http://127.0.0.1:8010`):

- `GET  /health`
- `POST /api/runs {ticker, trade_date?, llm_provider?, deep_think_llm?, quick_think_llm?, reasoning_effort?, max_debate_rounds?, max_risk_discuss_rounds?}`
- `GET  /api/runs/{id}` · `GET /api/runs` · `GET /api/runs/{id}/stream` (SSE)

Backend proxy (`/api/v1/tradingagents/*`): `status`, `analyze`, `runs`,
`runs/{id}`, `runs/{id}/stream`.

## Notes & limits

- One full run ≈ dozens of LLM calls; round counts multiply cost. Max 2
  concurrent runs per sidecar.
- Crypto symbols map to Yahoo tickers (`BTC/USDT → BTC-USD`); equities keep
  exchange suffixes. Upstream data vendors are Yahoo/StockTwits/Reddit based —
  crypto coverage is thinner than equities.
- The upstream repo also ships a CLI/TUI (`cli/`) usable from the sidecar venv:
  `integrations/TradingAgents/.venv/bin/python -m cli.main`.

## Verified working (2026-08-26)

A complete `BTC-USD` run on `google_genai` / `gemini-3-flash-preview` finished
end-to-end: **HOLD @ 85% confidence** with entry ref $78,404.98, all four
analyst reports (≈4KB each), full bull/bear transcripts, a 3-turn risk debate,
trader plan and structured recommendation — streamed live over SSE the whole way.

Model notes for this deployment:

| Provider | Status here | Notes |
|---|---|---|
| `google_genai` | ✅ works | Use **`gemini-3-flash-preview`** for both deep+quick. Flash-lite models reject the thinking-level config (400). Free tier ≈ 20 req/day. |
| `openrouter` | key present | Works; your key currently has its spend limit reached. |
| `openai` | ⚠️ misconfigured | Your `OPENAI_API_KEY` holds an `nvapi-…` NVIDIA key, which OpenAI rejects. Set a real OpenAI key or pick another provider in the desk. |
| `litellm`/Groq | ❌ | Your Groq key has no chat-model access. |

