"""
Kronos Forecast Plugin — Configuration

Kronos is a decoder-only foundation model for financial K-lines (OHLCV).
Models live on the Hugging Face Hub under the NeoQuasar org:
  - Kronos-mini      + Kronos-Tokenizer-2k   (fastest, ctx 2048, 4.1M params)
  - Kronos-small     + Kronos-Tokenizer-base (balanced, ctx 512, 24.7M params)
  - Kronos-base      + Kronos-Tokenizer-base (most accurate, ctx 512, 102.3M params)
  - Kronos-large     + Kronos-Tokenizer-base (best — not yet public)

All values are overridable via KRONOS_* environment variables.
"""
import os
from dataclasses import dataclass


# ── Kronos paper knowledge (arXiv:2508.02739) ─────────────────────────────────
# A compact fact-sheet every agent / narrative that reasons over a Kronos
# forecast is given, so the desk understands the *model* it is reading — not
# just its numbers. Sourced from the paper and the model card.
KRONOS_PAPER_KNOWLEDGE: str = (
    "Kronos (arXiv:2508.02739, AAAI 2026) is the first open-source FOUNDATION MODEL "
    "for the language of financial markets — candlesticks (K-lines). It is a "
    "decoder-only autoregressive transformer (GPT-style, but for OHLCV instead of "
    "text), built in two stages: (1) a specialised TOKENIZER quantises continuous "
    "OHLCV candles into hierarchical discrete tokens, preserving both price dynamics "
    "and trade-activity patterns; (2) the transformer is pre-trained with a "
    "next-token objective on 12 BILLION K-line records from 45 global exchanges, so "
    "it has seen crypto, FX, equities, metals and indices and learns cross-asset "
    "and seasonal structure. Zero-shot it beats the leading time-series foundation "
    "model by 93% RankIC on price forecasting and cuts volatility-forecast MAE by 9%. "
    "How to read a Kronos forecast: it samples MANY stochastic paths (temperature + "
    "top_p nucleus sampling); the mean path is the forecast and the p10/p90 band is "
    "its uncertainty — a TIGHT band means model conviction, a WIDE band means high "
    "dispersion/low conviction. The confidence score (0-1) is the fraction of paths "
    "agreeing on direction, then adjusted by the volume gate and macro bias. Volume "
    "is a HARD precondition here: on a dead or unresolvable tape Kronos returns "
    "NO_TRADE (silence), it does not guess a direction from price alone. Model sizes: "
    "Kronos-mini (4.1M, ctx 2048), Kronos-small (24.7M, ctx 512), Kronos-base "
    "(102.3M, ctx 512). The model can be FINE-TUNED on your own OHLCV so it "
    "specialises to a specific instrument/timeframe."
)


# ── Published model catalogue (shown in the UI dropdown) ──────────────────────
KRONOS_MODELS: list[dict] = [
    {
        "id": "NeoQuasar/Kronos-mini",
        "label": "Kronos-mini (4.1M — fastest)",
        "tokenizer": "NeoQuasar/Kronos-Tokenizer-2k",
        "max_context": 2048,
        "params_m": 4.1,
    },
    {
        "id": "NeoQuasar/Kronos-small",
        "label": "Kronos-small (24.7M — balanced)",
        "tokenizer": "NeoQuasar/Kronos-Tokenizer-base",
        "max_context": 512,
        "params_m": 24.7,
    },
    {
        "id": "NeoQuasar/Kronos-base",
        "label": "Kronos-base (102.3M — most accurate)",
        "tokenizer": "NeoQuasar/Kronos-Tokenizer-base",
        "max_context": 512,
        "params_m": 102.3,
    },
]



def _auto_device() -> str:
    """Pick the best available torch device without importing torch eagerly."""
    forced = os.getenv("KRONOS_DEVICE", "").strip().lower()
    if forced:
        return forced
    try:
        import torch  # type: ignore

        if torch.cuda.is_available():
            return "cuda:0"
        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"


@dataclass(frozen=True)
class KronosConfig:
    # Hugging Face model + tokenizer identifiers.
    # Default to Kronos-base (102.3M — most accurate) everywhere.
    model_name: str = os.getenv("KRONOS_MODEL_NAME", "NeoQuasar/Kronos-base")
    tokenizer_name: str = os.getenv("KRONOS_TOKENIZER_NAME", "NeoQuasar/Kronos-Tokenizer-base")

    # Compute
    device: str = _auto_device()
    max_context: int = int(os.getenv("KRONOS_MAX_CONTEXT", "512"))

    # Forecast defaults
    # Feed the model as many closed candles as its context allows (max_context)
    # so the forecast weighs deep history, not a 24h slice. Override lower/higher
    # per request via ?lookback= or with KRONOS_DEFAULT_LOOKBACK.
    default_lookback: int = int(os.getenv("KRONOS_DEFAULT_LOOKBACK", os.getenv("KRONOS_MAX_CONTEXT", "512")))
    default_pred_len: int = int(os.getenv("KRONOS_DEFAULT_PRED_LEN", "24"))
    default_samples: int = int(os.getenv("KRONOS_DEFAULT_SAMPLES", "10"))

    # Sampling
    temperature: float = float(os.getenv("KRONOS_TEMPERATURE", "1.0"))
    top_p: float = float(os.getenv("KRONOS_TOP_P", "0.9"))

    # Where the vendored Kronos `model/` package is copied by scripts/setup_kronos.sh
    vendor_dir: str = os.getenv(
        "KRONOS_VENDOR_DIR",
        "plugins/KronosForecastPlugin/backend/vendor",
    )

    # Cap how much history we ever pull from the exchange for a single forecast
    max_lookback: int = int(os.getenv("KRONOS_MAX_LOOKBACK", "512"))

    # Cache forecasts for this many seconds (per symbol/timeframe/params).
    # 180s is safe for 1h/4h charts — prices don't shift meaningfully in 3 min.
    cache_ttl_s: int = int(os.getenv("KRONOS_CACHE_TTL_S", "180"))


kronos_config = KronosConfig()
