"""
Desktop sidecar entrypoint
==========================

Started by the Electron shell (``desktop/main.js``) as the app's only child
process. Everything it needs is passed through the environment:

    TRADEBOT_PORT        port to listen on (Electron picks a free one)
    TRADEBOT_DATA_DIR    per-user directory for the SQLite DB, logs and config
    TRADEBOT_STATIC_DIR  the exported Next.js frontend, served at "/"

Differs from the development launcher (``start.py``) in three ways that matter:

* **Loopback only.** ``start.py`` binds ``0.0.0.0`` so a phone on the same
  Wi-Fi can reach the dashboard. That is a deliberate choice for a machine the
  developer controls. Shipping it would put an unauthenticated API that can
  place live trades on every network the user's laptop joins, so the desktop
  app binds ``127.0.0.1`` and nothing else.
* **No reload, one worker.** Reload watches the source tree, which is read-only
  inside an app bundle. Multiple workers would multiply the trading loops.
* **Config lives outside the bundle**, under the user's data directory.
"""
# The Windows event-loop policy fix in app.main must run before asyncio is
# touched, so import order matters: app.main first, uvicorn second.
import os
import sys
from pathlib import Path


def _bootstrap_paths() -> None:
    """Put the backend and the project root on sys.path.

    The plugin loader imports by string (``plugins.<name>.backend.router``), so
    the directory holding ``plugins/`` has to be importable. In development
    ``start.py`` gets this for free by running uvicorn with ``cwd=backend/``;
    inside the packaged app there is no such cwd guarantee.
    """
    backend_dir = Path(__file__).resolve().parent.parent  # …/backend
    app_root = backend_dir.parent                          # …/ (holds plugins/)
    for entry in (str(backend_dir), str(app_root)):
        if entry not in sys.path:
            sys.path.insert(0, entry)


def main() -> int:
    _bootstrap_paths()

    data_dir = os.environ.get("TRADEBOT_DATA_DIR", "").strip()
    if not data_dir:
        print(
            "TRADEBOT_DATA_DIR is not set — refusing to start.\n"
            "This entrypoint is for the packaged desktop app; "
            "use `python start.py` for development.",
            file=sys.stderr,
        )
        return 2

    Path(data_dir).mkdir(parents=True, exist_ok=True)

    try:
        port = int(os.environ.get("TRADEBOT_PORT", "1448"))
    except ValueError:
        print("TRADEBOT_PORT is not a number", file=sys.stderr)
        return 2

    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",       # never 0.0.0.0 — see module docstring
        port=port,
        loop="asyncio",         # explicit: avoids the uvloop probe crashing on Windows
        workers=1,
        reload=False,
        log_level=os.environ.get("TRADEBOT_LOG_LEVEL", "info"),
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
