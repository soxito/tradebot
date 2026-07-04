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
    default_lookback: int = int(os.getenv("KRONOS_DEFAULT_LOOKBACK", "400"))
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

    # Cache forecasts for this many seconds (per symbol/timeframe/params)
    cache_ttl_s: int = int(os.getenv("KRONOS_CACHE_TTL_S", "60"))


kronos_config = KronosConfig()
