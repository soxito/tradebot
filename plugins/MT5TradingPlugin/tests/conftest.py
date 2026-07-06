from __future__ import annotations

import sys
from pathlib import Path

# Ensure the repository root is importable so ``plugins.*`` resolves when tests
# are run from any working directory.
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
