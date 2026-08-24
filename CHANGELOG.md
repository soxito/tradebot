# Changelog

All notable changes to TradeBot are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project uses a rolling `main` branch (no semantic version tags yet), so
entries are grouped under `[Unreleased]` and dated milestone headings.

## [Unreleased]

### Fixed
- **The board no longer answers "hold" into a trending market.** Four of the
  seven live seats were still running the repo's *original crypto-only* system
  prompts: agent instructions are seeded into `agents.system_prompt` once and
  nothing ever revisited them, so every later improvement to
  `backend/app/agents/specialists.py` never reached a running install. The
  market analyst still carried "when in doubt, lean toward neutral" and the
  signal generator still enforced "confidence >= 0.70" — it quoted that
  threshold verbatim while declining a gold move. `upgrade_stock_prompts()` now
  runs at startup and rewrites any prompt whose SHA-256 matches a known stock
  version (current or superseded); anything a user edited is left untouched.
- **"No candles available for XAUUSD" (and every other pair).** Four separate
  OHLCV paths existed and each returned nothing on its own; whichever one a
  surface happened to call decided whether the market "had candles".
  `app/services/candles.py` is now the single resolver — Yahoo with CME volume
  and Swissquote anchoring, the forex provider, credentialed exchanges, keyless
  public exchanges — and folds a finer timeframe up when the requested one is
  unserved. Verified live across 10 instruments × 3 timeframes with no misses.
- **Agent answers cut off mid-sentence.** Three causes, all closed: the seeded
  2000-token ceiling (now a 4000 floor for every seat, 8000 for reasoning
  models), a single widening retry (now two), and a truncated JSON response
  being discarded whole (now repaired — the decision and its levels are kept,
  and any dangling string is trimmed back to its last complete sentence, so a
  half-word is never published). Recovered answers are flagged
  `reasoning_trimmed` rather than passed off as complete.

### Added
- **The trading room reads the forecast.** `/forecast`'s Kronos projection —
  direction, expected % change, p10/p90 band, volume gate and macro bias — is
  now part of every agent's context and is carried on the meeting result, so
  the room and the forecast page can no longer disagree about which way a
  market points.
- **A measured momentum read in the agents' context.** EMA stack, position in
  the 60-bar range, ATR expansion, net travel and Kaufman path efficiency,
  computed rather than asked for. A market that is moving is reported as
  moving; the prompts require a seat calling neutral into a `strong` reading to
  name the level that stops it. Chop still reads weak — efficiency below 0.12
  is never a trend.
- **Agreed signals are published to Telegram in full.** The room used to
  announce its conclusions through the generic alert line (a title, one
  sentence, three key/value pairs). `app/services/room_publisher.py` now sends
  the verdict and vote, every seat's reasoning, the forecast, the structural
  read, the plan's levels, the copyable signal card and the drawn chart.
- **Room cadence and timeframe are one setting each, and they govern.**
  "Re-analyse the focused pair every …" is now the room's clock in every case,
  including with "keep the board meeting 24/7" on — the rotation interval used
  to silently override it whenever nothing was pinned. A new
  `room_settings.focus_timeframe` chooses the timeframe the agents analyse on
  *and* the one the room's wall chart draws, so the argument and the picture
  are the same market.
- **Desk brief in the web trading room** (`POST /agents/room/brief`, rendered by
  `components/room/DeskBrief.tsx`) — the verdict, forecast, momentum, plan
  levels, market read, signal card and chart that the `/room` Telegram command
  already sent. The two surfaces now deliver the same analysis.
- **Email (SMTP) alert channel** for the monitoring system, alongside the
  existing Telegram and Discord alerts. `AlertService.notify()` now also emails
  on qualifying events; delivery is non-blocking (SMTP runs in a worker thread
  via `asyncio.to_thread`) and supports STARTTLS (port 587), implicit SSL
  (port 465) and plain SMTP. Configured via `EMAIL_ALERTS_ENABLED`, `SMTP_HOST`,
  `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_TO` and
  `SMTP_USE_TLS` (documented in `.env.example`). `GET /api/v1/monitoring/status`
  now reports `email_configured`, and email successes/failures are recorded in
  the alert metrics.
- **Crypto market-cap monitor page** (`/market-cap`) for tracking overall
  market capitalization.
- **JARVIS browser extension**: the popup now shows real exchange account
  balances fetched from the backend.
- **Windows + low-end hardware compatibility** (target: Intel Core i5-4300U,
  2-core/4-thread, Intel HD 4400 iGPU, 16 GB RAM).
  - `start.bat` — one-click Windows launcher wrapping the cross-platform
    `start.py` (auto-selects the `py -3` launcher, falls back to `python`).
  - Frontend 3D/WebGL kill-switch: `NEXT_PUBLIC_DISABLE_3D` (auto-enabled on the
    `low` UI tier) plus a per-browser `localStorage` override
    (`tradebot.disable3d`). Skips the Three.js JARVIS robot and the 3D
    force-graph on weak GPUs; the 2D graph stays fully functional.
  - `start.py` now detects real system RAM on Windows (`GlobalMemoryStatusEx`)
    and true physical core count (PowerShell/`wmic`/`psutil`).
  - `start.py --stop` gained a PID-file fallback (`taskkill` / `os.kill`) so
    services can be stopped on Windows where `pgrep`/`pkill` don't exist.

### Changed
- **Execution gates lowered to 40% and made to move together.** `min_consensus`
  and `min_confidence` both default to 0.40 (were 0.70). The seats rarely align
  above 70% on a ranging pair, and holding out for that was skipping setups that
  then ran; 40% still bars a genuinely split board. The two must match — consensus
  without the matching confidence just relocates the block message to the next
  gate. Nothing goes live from this alone: `dry_run` stays on and MT5 live mode
  off, so orders are logged, not sent.
- **Every gold room signal has a take-profit worth holding for.** The furthest
  target is floored at 110 pips (11.0 in price, since 1 gold pip = 0.10) from
  entry; a too-near final target is what has a trade sit open for hours to bank a
  few dollars and then hand them back on the next swing. Only the last rung is
  stretched — the near rungs stay where the seat sized them, as partial-takes —
  and the floor scales per instrument (silver's is 1.1, not 11). The card and the
  chart carry the same floored ladder.
- **The MT5 scalp bot targets 80-110 pips on gold, positioned by flow.** The
  take-profit is placed inside that band by live tape — a strong, one-sided volume
  spike reaches for 110, a quiet balanced tape banks near 80 — which stops it both
  from scalping a 30-pip target into chop and from over-holding a winner past its
  move. The reward:risk floor stays authoritative: a wide stop that needs more
  than 110 pips to keep R:R ≥ min still gets it. Other instruments are unchanged.
- **App-placed and room MT5 positions now trail, not just break even.**
  Break-even secured a working trade at entry and then stopped moving, so a trade
  that ran on gave the extra back on a reversal. `app/trading/trailing.py` adds a
  forward-only profit trail that locks half the excursion once a trade is clearly
  in profit, and the auto-manage cycle applies whichever of break-even / trail is
  more protective. (Requires the MT5 auto-manage loop to be running — a user
  toggle, currently off.)
- `start.py` resource tuning: on machines with ≤4 physical cores, the ML/BLAS
  thread cap now leaves headroom for the async event loop (e.g. a 2-core/4-thread
  box gets 2 ML threads, not 4). The backend also skips uvicorn `--reload`
  (and its file watchers) on ≤2 physical cores — override with `TRADEBOT_RELOAD=1`.
- The face-vision dependencies (`mediapipe`, `opencv-python-headless`,
  `face_recognition`) are now **optional** and commented out of
  `backend/requirements.txt`. `face_recognition` pulls in `dlib`, which has no
  prebuilt Windows wheel, so shipping it in the core requirements broke
  `pip install` on Windows. The vision endpoints import these libraries lazily,
  so the backend starts fine without them. Enable face-vision explicitly with
  `bash scripts/setup-face-vision.sh`.

### Added
- **The trading room publishes a tradeable signal card**, alongside the board of
  agent opinions it already posted. The Signal Generator now quotes absolute
  prices instead of percentages — an entry band, a ladder of take profits and a
  stop — and `signal_card.py` renders them in the format a desk publishes:

      #XAUUSD Sell 4403/4405

      TP 4400 … TP 4385

      SL 4411

      Analysis 9 💫 AUGUST 13 - XAUUSD
      🟢 BUY ZONE 4371 - 4374 …

  The agent may also name a *reaction zone* — the level it would trade from if
  price returns to it — which is published under its own heading with its stop,
  three targets and the note explaining what confirms it. Nothing is published
  unchecked: every target must clear the entry band in the trade's direction and
  the stop must sit the other side of it, an entry "zone" too wide to fill as one
  order is narrowed, and a plan that fails those tests is dropped rather than
  tidied into something that looks tradeable but never was. A card that
  contradicts the room's own verdict is withheld. The chart overlay now draws the
  same ladder, so the picture and the message carry one plan rather than two.

### Fixed
- **A structured agent answer is budgeted for the model that serves it.** The
  token ceiling was chosen from the model the seat *asked* for, but a seat set to
  a model the provider does not carry is served by that provider's default
  instead — so a Signal Generator configured for `open-mistral-nemo` was answered
  by `nemotron-3.5-lightning` on a budget sized for neither, and was cut off
  mid-thought every time. Every structured agent call now gets the same generous
  ceiling whatever ends up serving it, rather than one conditioned on recognising
  the model name. Measured on a real room prompt: the same model that never
  finished at the old ceiling answers in 1599 tokens and 10s at this one, 3/3.
- **`/room` always calls the models now.** Two shortcuts keep the background
  scanner off the token budget — a decision recalled from past outcomes, and a
  per-symbol cache that replays an answer for an hour — and both were also
  applying to runs a person typed. On one `/room XAUUSD` three of four seats
  answered from cache, one of them 3313 seconds old, and the board reported
  "AI calls: 0" under text a model had written 55 minutes earlier about a
  different price. User-initiated triggers (`telegram`, `manual`, `api`, `user`)
  now skip both shortcuts and step over the providers' backoff as well: a
  cooldown protects a budget from the scanner, and should not be what decides
  that someone's question goes unanswered. The scheduled triggers keep every
  saving they had.
- **`/room` publishes a signal even when the providers are all rate-limited.**
  Three faults stacked into one silent reply. Every provider was inside its own
  circuit-breaker cooldown at the same time — SambaNova 429-ing into 900s while
  NVIDIA timed out into 240s — so the pool was locked out and every seat fell
  back to a local read with no levels; a breaker is a backoff hint, and all of
  them together is now treated as a lockout worth one pass through anyway. The
  truncation retry was itself causing some of those timeouts, so it is skipped
  once half the deadline is gone: a merely verbose provider was being recorded
  as one that times out, which took it out for every other seat too. And a seat
  that has a direction but no prices — the normal shape of a local read — now
  publishes the levels the chart is already drawing, instead of the message
  saying nothing while the picture says plenty.
- **The room chart shows the exit, not just the entry.** A HOLD verdict skipped
  the ATR frame entirely, so the image went out with an entry line and nothing
  else: where to get in, never where to get out. The seats' own lean now frames
  the levels when the room itself declines, the stop and targets are filled in
  independently (a plan with targets but no stop used to get neither, drawing the
  reward and hiding the risk), and the ladder runs 3×/4.5×/6× ATR against the
  1.5× stop.
- **The follow-up is told once.** The room journals a plan every run, so the same
  untriggered scenario was read back and rendered three times — three identical
  paragraphs that read like a stuck process. The plan's levels also no longer
  publish under "Key Levels", which the structural read above them already uses.
- **`/room` answers with real analysis again.** Every seat was falling back to a
  local read ("AI calls: 0", HOLD at 20%) because the models' JSON decisions were
  arriving truncated. Three causes, all fixed: a 4xx of *any* kind — including a
  routine 429 — stripped `response_format` from a payload the retry layer shares,
  so later attempts asked for free text and got prose; the per-agent token
  ceiling (1200) cut off models that narrate before emitting the object; and
  those same models were being judged by the 40s default deadline, timing out a
  provider that was answering normally and tripping its breaker for everything
  behind it. A JSON answer cut off at the budget is now retried once with double
  the room (capped at 8000), `response_format` is dropped only when the model
  actually rejects it, structured agent calls floor at 2048 tokens, and reasoning
  models get the 120s deadline. Unparsable output is logged with its model,
  provider and tail, so "could not parse" says which of the two it was.
- **Chart overlays land on the price they claim.** Level and plan lines were
  positioned from the vision model's estimate of where a price sits as a
  percentage of image height — routinely several percent out, which on a phone
  screenshot of gold is tens of dollars, drawn with full confidence. The scale is
  now *measured*: `chart_axis.py` finds the axis label rows in the image itself
  and pairs them with the label values the model read, which is the half a vision
  model does reliably. Partial label lists (measured: llama-3.2-11b reports the
  top four and stops) are matched by rung and disambiguated against the live-quote
  chip, whose row is measured rather than estimated. Values arriving on the wrong
  scale (`4,460.000` read as `4460000`) are rescaled against the live price, and
  the quote chip is used as a final check — a scale it contradicts draws nothing
  rather than drawing everything in the wrong place.
- **Agents no longer send a connected provider's key to OpenAI.**
  `OPENAI_API_KEY` normally holds an NVIDIA NIM / Groq / Cerebras / OpenRouter
  key, and the headroom proxy picks `api.openai.com` as its own upstream
  regardless of `HEADROOM_OPENAI_BASE_URL` — so routing those keys through it
  returned `401 Incorrect API key provided: nvapi-…`, tripped the agents' shared
  circuit breaker, and dropped every seat on the trading room board (and the
  Telegram `/room` reply) to a local read for five minutes. Key→endpoint routing
  now lives in `app/core/ai_key_routing.py` and is applied by the base agent,
  the LLM gateway, the analyst's OpenAI client and the voice STT/TTS fallback;
  only a genuine `sk-` key reaches OpenAI or the compression proxy.
- **A reasoning model is no longer clamped to the per-agent token ceiling.**
  Models that emit `reasoning_content` before their answer spend ~1000 tokens
  thinking; at the 1200-token agent clamp the JSON decision was truncated
  mid-sentence, so a healthy provider was recorded as returning nothing. The
  router now floors reasoning models at 2048 tokens (measured: 1200 truncates,
  2048 completes).
- **A seat with no provider answer reports its local read** instead of falling
  through to the raw `OPENAI_API_KEY` — a vendor outside every usage cap and
  task dedication the provider pool enforces, and one the user may never have
  connected. The direct-key path, when it is legitimately used, now asks for the
  role's routed model rather than the `o3` seed default that no connected
  provider serves.
- `python start.py --status` no longer crashes on Windows (the Obsidian
  process check now uses a guarded, cross-platform helper).

## 2026-08-02 — Macro context, analysis journal, Electron desktop & research UI

### Added
- **Dedicated spot metals provider** (`exchanges/metals_provider.py`): fetches
  live XAUUSD, XAGUSD, XPTUSD, XPDUSD from Swissquote and gold-api in
  cascade — actual spot prices, not COMEX futures (GC=F was $58/oz above spot at
  the time of writing). Used by JARVIS analysis and the Paul prompt builder.
- **Yahoo Finance OHLCV universal fallback** (`exchanges/yahoo_provider.py`):
  FX pairs, indices and commodities now render chart history even when the MT5
  bridge is offline; all MT5-style symbols map to Yahoo tickers automatically.
- **Universal market data service** (`services/market_data.py`): single
  `resolve_price(symbol)` call for any asset class — crypto, FX, indices, metals.
  Eliminates the `/USDT`-only filter that prevented AI models from quoting gold,
  EUR/USD, or index prices.
- **Macro context engine** (`services/macro_context.py`): fetches DXY and VIX,
  computes a signed confidence modifier (trend slope + vol-normalised z-score),
  and emits plain-English regime sentences attached to every trade proposal.
  Never blocks a trade; only modulates model confidence. Front-end
  `MacroContextPanel` displays DXY/VIX area charts with regime tags.
- **Analysis journal** (`services/analysis_journal.py`): JARVIS captures every
  trade proposal (side, entry, stop, target, confidence, model) and later
  records what price actually did — building a self-calibrating accuracy
  track-record that both JARVIS and Paul read back before composing new proposals.
- **Agent-Reach research client** (`services/agent_reach_client.py`): thin
  async wrapper for Jina Reader (web read), Exa via mcporter (semantic search),
  yt-dlp (YouTube transcripts) and the `gh` CLI (GitHub) — used in JARVIS
  `/analyze` deep-research cycles.
- **Research UI components**: `ResearchEntries`, `ResearchJobCard`, and
  `SignalResearchBoard` on the `/research` page expose economic-calendar
  findings, MT5 background-research loop results and signal-research jobs.
- **Vault page** (`/vault`): Obsidian Knowledge Browser — full-text search,
  filterable note list (signal / decision / strategy / community / daily),
  markdown detail panel, trigger-sync button, live SSE-backed feed.
- **Electron desktop app** (`desktop/`): fully packaged cross-platform
  desktop app (macOS arm64 + x64, Windows, Linux). Includes splash screen,
  native menus, single-instance lock and local-server lifecycle management
  (`desktop_main.py`). No Python/Node/Postgres/Redis install required.
- **GitHub Actions desktop-release CI/CD** (`.github/workflows/desktop-release.yml`):
  multi-platform Electron builds triggered by `v*` tags; publishes installers
  to GitHub Releases.
- Test suite additions: `test_analysis_journal`, `test_macro_context`,
  `test_market_data`, `test_metals_spot`, `test_text_format`,
  `test_price_tick_routing`, `test_jarvis_task_models`,
  `test_jarvis_symbol_routing`, plus `AgentPaulPlugin` news-research tests.

## 2026-07-29 — SMC background research loop

### Added
- **SMC background research loop** registered in the main app scheduler:
  runs the economic-calendar, news and sentiment cycle automatically every
  15 minutes so the `/research` board stays current without manual triggers.

## 2026-07-28 — Research API, Yahoo Finance OHLCV & Docker improvements

### Added
- **Research API** (`api/research.py`): exposes MT5 research-loop findings,
  economic-calendar events, signal-research jobs, upcoming event reminders, loop
  status, and a `POST /research/run-cycle` trigger. Plugin imports are lazy so
  a deploy without the MT5 plugin still boots.
- **Headroom proxy Docker image** (`docker/Dockerfile.headroom`,
  `docker/headroom_proxy.py`): lightweight MITM proxy that compresses LLM API
  request/response bodies, reducing context costs by 60–95%.
- **OpenWA Docker image** (`docker/Dockerfile.openwa`): containerised OpenWA
  gateway for the WhatsAppSignalNewsPlugin.
- **Codebase documentation** (`docs/codebase/`): machine-readable
  ARCHITECTURE, CONVENTIONS, INTEGRATIONS, STACK, STRUCTURE, TESTING, and
  CONCERNS docs generated for AI-assisted development.
- `docker-compose.yml` extended with headroom-proxy and openwa services.

### Changed
- Yahoo Finance OHLCV provider fully rewritten (454 → ~900 lines): added
  universal ticker mapping (MT5 → Yahoo), multi-source fallback, OHLCV
  normalisation, caching, and a symbol auto-detection helper used by the
  MT5 chart fallback.

## 2026-07-15 — S.O.X Command Room, ngrok tunnels, OpenHuman Hub & Vibe Trading

### Added
- **Ngrok tunnel service** (`services/ngrok_service.py` + `api/ngrok.py`):
  managed ngrok SDK session/listener lifecycle for backend and frontend tunnels.
  Google OAuth is always enforced on every managed tunnel and cannot be
  disabled. Config persisted to DB (`NgrokConfig` model).
- **Ngrok management page** (`/ngrok`): start/stop/configure tunnels, copy
  public URLs, view OAuth enforcement status.
- **S.O.X Command Room** (`/jarvis-room`): complete redesign of the JARVIS Room.
  Enhanced 960-particle orb with sonar-ring (LISTENING), spark-ejection
  (THINKING), L+R EQ bar visualisers (TALKING) and electromagnetic jitter;
  futuristic hex-grid SVG background; adaptive quality scaling.
- **OpenHuman Hub** (`/openhuman-hub`): 10-tab integration dashboard —
  Brain (force-directed memory-tree graph), Tiny Place (3D world), Subconscious,
  Research, Agents (JARVIS + OpenHuman joint), Integrations, Workflows, Kronos,
  SMC, Signals and Settings — with `OpenHumanMascot` and `TinyPlaceWorld`
  components.
- **MemoryGraph component** (`components/MemoryGraph.tsx`): force-directed
  memory-tree canvas replicating the OpenHuman Brain → Graph view (source /
  L1 / L2 / document node types, physics layout, pan + hover).
- **Vibe Trading page** (`/vibe-trading`): UI for VibeTradingPlugin —
  natural-language strategy entry, 460-alpha Alpha Zoo browser, multi-agent
  swarm research controls, strategy run history.
- `AiMarketAnalyst` provider presets expanded with additional model options
  across all supported providers.
- `AgentPaulPlugin` — paul_chat service improvements: richer context building,
  better signal parsing, improved response formatting.

## 2026-07-09 — Kronos engine enhancements & MT5 improvements (Patch 2.012)

### Added
- Kronos engine (`kronos_engine.py`) expanded: improved multi-model fallback
  logic, better OHLCV pre-processing for short series, confidence band
  post-processing, and explicit status reporting for each model variant.
- MT5 plugin router: additional helper endpoints and improved error handling.
- TradingView chart component: visual improvements and symbol-change stability.
- Exchanges API: incremental improvements to balance + position normalisation.

## 2026-07-05–07 — MT5 autonomous scalp bot, Redis health-checks & device performance

### Added
- **MT5 autonomous scalp bot** (MT5TradingPlugin):
  - `scalp_bot_service.py`: per-account asyncio background loops; each
    10-second cycle pulls M1/M5/H1/H4/D1 candles, optionally runs Kronos + AI
    gate, uses the `ScalpStrategyEngine` for entry decision, places market orders
    with SL/TP, closes on the profit target and opens an SMC-guided recovery leg
    when the trade is offside. All state persisted to `mt5_scalp_sessions` /
    `mt5_scalp_trades`.
  - `scalp_strategy.py`: multi-timeframe SMC strategy engine (order blocks,
    FVGs, trend bias, entry refinement).
  - `candle_feed.py`: unified candle-feed abstraction (MT5 bridge → exchange
    fallback) used by the scalp bot and the broader MT5 plugin.
  - `MT5ScalpBotPanel.tsx`: per-account autonomous scalp-bot control surface
    on `/mt5-live` — symbol search, lot size + risk settings, live phase/bias/
    PnL view; hot-config (settings editable while running); session state
    pre-fills on account switch.
- **MT5 auto-detection** in `start.py`: probes whether MetaTrader 5 is
  installed and adjusts startup behaviour accordingly.
- **Redis health-check and auto-provision** in `start.py`: verifies Redis is
  reachable at startup, starts it via Homebrew or Docker if absent, and injects
  `REDIS_URL` into the environment.
- **Device-performance enhancements** (`utils/devicePerformance.ts`): better
  CPU/GPU tier detection, wider browser compatibility, and finer-grained
  quality-level breakpoints.

### Fixed
- MT5 REST client: account-detection logic made more robust for edge-case
  server responses.
- Kronos "No OHLCV data available": forecaster falls back to a keyless public
  `ccxt` fetch; a setup self-test runs automatically so forecasts work without
  exchange API keys.

## 2026-07-05 — Realtime streaming, deep AI analysis & Kronos models

### Added
- **Server-Sent Events (SSE) realtime backbone** replacing UI polling.
  - `EventBus` (Redis pub/sub across workers with an in-memory fallback) and a
    `GET /api/v1/stream/events` endpoint (topics: signals, trades, sniper,
    sentiment, monitor status, price ticks, system alerts).
  - Frontend shares a single `EventSource` across browser tabs via Web Locks
    leader election + `BroadcastChannel`; consumers keep polling only as a
    fallback when the stream is disconnected. Live/Poll badge in the header.
  - Opt-in Web Notifications + Vibration and a Screen Wake Lock for live trading.
- **Position-aware, AI-composed deep analysis** in JARVIS: `/analyze` now
  enriches crypto pairs with volume pressure, live news/sentiment, a Kronos
  forecast, your open position (side, size, entry, live PnL) and a natural-language
  narrative. Every request is captured to all three "brains" for learning.
- **Kronos forecasting models**: all three published Kronos models (mini/small/base)
  are supported with per-model install status and an "Install all" action.
- `start.py` now auto-installs and health-checks Redis (Homebrew or Docker) and
  injects `REDIS_URL` so the EventBus fans out across workers.

### Fixed
- Kronos "No OHLCV data available" — the forecaster now falls back to a keyless
  public `ccxt` fetch and runs a setup self-test, so forecasts work without
  exchange API keys.

## 2026-07-04 — Off-thread rendering & Bitget unified accounts

### Added
- **Web Worker rendering** for the JARVIS UI to keep the main thread responsive:
  the ~980-particle S.O.X orb (OffscreenCanvas worker) and the Three.js robot
  avatar now render off the main thread, with graceful main-thread fallbacks.
- **Adaptive graphics auto-scaling**: a device performance tier (low → ultra)
  derived from CPU cores + memory, with a runtime FPS monitor that downgrades
  quality to stay smooth on weak/thermally-throttled machines.
- **Bitget multi-account + unified-account support**: aggregates USDT/USDC/COIN
  futures balances and positions across product types, main + sub-accounts, with
  a read-only diagnostic script.

## 2026-07-03 — Bitget futures margin & leverage fix

### Fixed
- Bitget error 45117 ("margin mode cannot be adjusted"): the futures order path
  now detects a symbol's existing margin mode from open positions/orders and
  matches it instead of forcing a change while a position or pending order exists.

## Baseline — Core platform (Phases 1–6) + plugins + JARVIS

### Added
- **Backend**: FastAPI (Python 3.13), SQLAlchemy (async) + asyncpg, Redis,
  multi-exchange connectors via `ccxt` (Binance, Bitget, Bybit, OKX, KuCoin,
  Coinbase) plus a native Bitget v2 SDK for spot + futures.
- **TradingView webhook receiver** with signature validation and a signal +
  decision engine (position sizing, stop-loss / take-profit, risk limits).
- **News & sentiment pipeline**: RSS feeds + CryptoPanic scored with VADER +
  TextBlob, written to `sentiment_scores` and surfaced on the dashboard.
- **Frontend**: Next.js 16 + React 19 + TypeScript + Tailwind, TradingView
  Lightweight Charts, multi-exchange wallet balances, trade history and an
  auto-testing connection banner.
- **Standalone plugin architecture** (`plugins/`, never modifies core):
  - `MT5TradingPlugin` — MetaTrader 5 REST bridge integration.
  - `AiMarketAnalyst` — multi-provider LLM router (Groq, OpenRouter, Gemini,
    Mistral, Cerebras, DeepSeek, Together, OpenAI, and custom endpoints) with
    automatic failover.
  - `KronosForecastPlugin` — open-source Kronos ML candle forecasting with a
    heuristic fallback when the model isn't installed.
  - `TelegramSignalNewsPlugin` — Telegram channel ingestion, signal parsing,
    news-to-sentiment, and a sniper auto-trade lifecycle.
  - `ObsidianKnowledgePlugin` — knowledge-base integration.
  - `AgentPaulPlugin` — background "subconscious" agent.
- **JARVIS / Paul assistant**: global voice + chat widget with hands-free
  navigation and click, cost-aware Deepgram STT fallback (free Web Speech
  primary), speaker-ID voice matching, and a WebGL S.O.X room.
- **Monitoring & alerting**: Prometheus metrics, structured logging, and a
  configurable alert service (Telegram + Discord) with a minimum-severity gate
  and a `GET /api/v1/monitoring/status` endpoint.
- **Cross-platform launcher** `start.py` (+ `run-local.sh`) that provisions
  Postgres/Redis (Homebrew or Docker), builds the Python venv, installs
  dependencies, and starts the backend and frontend with resource-aware tuning.
