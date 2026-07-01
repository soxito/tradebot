#!/usr/bin/env bash
# =============================================================================
#  JARVIS Face Vision Setup Script
#  Installs Python deps (mediapipe + face_recognition) and verifies GPU access.
#  Run once from the repo root:  bash scripts/setup-face-vision.sh
# =============================================================================
set -euo pipefail

CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${CYAN}[face-vision]${NC} $*"; }
ok()    { echo -e "${GREEN}[face-vision]${NC} ✓ $*"; }
warn()  { echo -e "${YELLOW}[face-vision]${NC} ⚠ $*"; }
err()   { echo -e "${RED}[face-vision]${NC} ✗ $*"; }

# ── Resolve Python interpreter ─────────────────────────────────────────────
PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" &>/dev/null; then
  err "python3 not found. Install Python 3.10+ first."
  exit 1
fi
PY_VER=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
info "Using Python $PY_VER at $("$PYTHON" -c 'import sys; print(sys.executable)')"

# Prefer the venv if it exists (same pattern as start.py uses)
if [[ -f backend/.venv/bin/python ]]; then
  PYTHON="backend/.venv/bin/python"
  PIP="backend/.venv/bin/pip"
  info "Using existing backend venv"
elif [[ -f backend/venv/bin/python ]]; then
  PYTHON="backend/venv/bin/python"
  PIP="backend/venv/bin/pip"
  info "Using existing backend venv"
else
  PIP="$PYTHON -m pip"
fi

# ── 1. Core deps ────────────────────────────────────────────────────────────
info "Installing mediapipe (GPU-capable, includes face mesh)…"
$PIP install --quiet --upgrade "mediapipe>=0.10.9"
ok "mediapipe installed"

info "Installing opencv-python-headless (no GUI window needed)…"
$PIP install --quiet --upgrade "opencv-python-headless>=4.9.0"
ok "opencv-python-headless installed"

# ── 2. face_recognition (dlib-based) ────────────────────────────────────────
info "Installing face_recognition (requires cmake + C++ build tools)…"

# cmake is required to build dlib
if ! command -v cmake &>/dev/null; then
  warn "cmake not found. Attempting install…"
  if command -v brew &>/dev/null; then
    brew install cmake --quiet && ok "cmake installed via Homebrew"
  elif command -v apt-get &>/dev/null; then
    sudo apt-get install -y cmake --quiet && ok "cmake installed via apt"
  else
    err "Please install cmake manually: https://cmake.org/download/"
    err "Then re-run this script."
    exit 1
  fi
fi

if $PYTHON -c "import face_recognition" &>/dev/null 2>&1; then
  ok "face_recognition already installed"
else
  $PIP install --quiet "dlib>=19.24.0" "face_recognition>=1.3.0"
  ok "face_recognition installed"
fi

# ── 3. Verify GPU / backend ─────────────────────────────────────────────────
echo ""
info "Verifying installation…"
$PYTHON - <<'PYEOF'
import sys

print("  Python:", sys.version.split()[0])

try:
    import cv2
    print(f"  opencv:           {cv2.__version__}  ✓")
except ImportError:
    print("  opencv:           NOT INSTALLED  ✗")

try:
    import mediapipe as mp
    print(f"  mediapipe:        {mp.__version__}  ✓")
    # Quick GPU check
    from mediapipe.tasks import python as mp_python
    print("  mediapipe tasks:  available  ✓")
except ImportError:
    print("  mediapipe:        NOT INSTALLED  ✗")

try:
    import face_recognition
    print(f"  face_recognition: installed  ✓")
except ImportError:
    print("  face_recognition: NOT INSTALLED  (identity check disabled)")

# Check if CUDA is available (optional)
try:
    import torch
    cuda_ok = torch.cuda.is_available()
    print(f"  torch CUDA:       {'available  ✓' if cuda_ok else 'not found (CPU/Metal will be used)'}")
except ImportError:
    print("  torch:            not installed (not required)")

print("")
print("  Backend GPU notes:")
print("  • macOS:          Metal/CoreML used automatically by mediapipe")
print("  • Linux (CUDA):   ensure CUDA 11.x+ and cuDNN are installed")
print("  • Windows (CUDA): ensure CUDA toolkit and cuDNN match")
PYEOF

echo ""
ok "Setup complete! Restart the backend to activate the Vision API."
echo ""
echo -e "  ${CYAN}Usage:${NC}"
echo -e "    1. Start JARVIS: ${GREEN}python start.py${NC}"
echo -e "    2. Open Chrome extension popup"
echo -e "    3. Toggle '👁 Face Vision' to ON"
echo -e "    4. Click 'Enroll Face' to register your identity"
echo ""
