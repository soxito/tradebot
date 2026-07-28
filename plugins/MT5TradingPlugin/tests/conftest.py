from __future__ import annotations

import sys
from pathlib import Path

# Ensure the repository root is importable so ``plugins.*`` resolves when tests
# are run from any working directory.
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# ``backend/`` too, so plugin code that reaches into the core app (``app.core.*``)
# imports the same way it does at runtime, where both roots are on the path.
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
