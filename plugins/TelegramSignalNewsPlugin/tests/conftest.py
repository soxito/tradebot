from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# The command handlers import KronosForecastPlugin, which imports the core
# `app.*` package (read-only) for the exchange manager, so the backend root has
# to be importable too — same as the Kronos plugin's own conftest.
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# Never cold-load the Kronos weights just to run unit tests.
import os  # noqa: E402

os.environ.setdefault("KRONOS_WARMUP", "0")

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_cross_test_singletons():
    """Undo module-level state that leaks between tests in this process.

    Two singletons outlive an individual test and made results depend on the
    random ordering:

    * ``ai_router._circuits`` holds a provider's breaker open for two minutes
      (thirty for a config fault), so a test that trips one silently changes
      the path every later test takes.

    * The ccxt client inside the Bitget connector caches ``markets_loading`` as
      a future bound to the event loop that created it. pytest-asyncio gives
      each test its own loop, so a later ``await`` on that stale future raises
      ``CancelledError`` — which is what intermittently broke
      ``test_a_failed_forecast_falls_through_to_chat``. Dropping the cached
      future makes the next caller reload markets in its own loop.
    """
    try:
        from plugins.AiMarketAnalyst.backend.services import ai_router
        ai_router._circuits.clear()
    except Exception:  # noqa: BLE001 — plugin-optional
        ai_router = None

    def _clear_ccxt_loop_state() -> None:
        try:
            from app.exchanges.manager import exchange_manager
        except Exception:  # noqa: BLE001
            return
        for connector in getattr(exchange_manager, "exchanges", {}).values():
            client = getattr(connector, "exchange", None)
            if client is None:
                continue
            # markets_loading is an asyncio.Future created by ensure_future in
            # whichever loop first called load_markets; the throttler owns a
            # long-running looper task on that same loop. Both must go.
            for attr in ("markets_loading", "markets_loaded"):
                if hasattr(client, attr):
                    try:
                        setattr(client, attr, None)
                    except Exception:  # noqa: BLE001
                        pass
            throttler = getattr(client, "throttler", None)
            if throttler is not None:
                for attr in ("running", "queue"):
                    try:
                        if attr == "running":
                            throttler.running = False
                        elif hasattr(throttler, "queue"):
                            throttler.queue.clear()
                    except Exception:  # noqa: BLE001
                        pass

    _clear_ccxt_loop_state()
    try:
        yield
    finally:
        if ai_router is not None:
            ai_router._circuits.clear()
        _clear_ccxt_loop_state()
