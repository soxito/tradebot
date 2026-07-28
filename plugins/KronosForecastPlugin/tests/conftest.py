from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# forecast_service imports the core `app.*` package (read-only) for the exchange
# manager, so the backend root has to be importable too.
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# Never cold-load the Kronos weights just to run unit tests.
import os  # noqa: E402

os.environ.setdefault("KRONOS_WARMUP", "0")
