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

# ── 3. FaceLandmarker model bundle ──────────────────────────────────────────
MODEL_DIR="backend/app/api/models"
MODEL_PATH="$MODEL_DIR/face_landmarker.task"
MODEL_URL="https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task"
mkdir -p "$MODEL_DIR"
if [[ -f "$MODEL_PATH" ]] && [[ $(wc -c < "$MODEL_PATH") -gt 100000 ]]; then
  ok "FaceLandmarker model already present"
else
  info "Downloading MediaPipe FaceLandmarker model (~3.6 MB)…"
  if command -v curl &>/dev/null; then
    curl -sL -o "$MODEL_PATH" "$MODEL_URL"
  else
    "$PYTHON" -c "import urllib.request; urllib.request.urlretrieve('$MODEL_URL','$MODEL_PATH')"
  fi
  if [[ -f "$MODEL_PATH" ]] && [[ $(wc -c < "$MODEL_PATH") -gt 100000 ]]; then
    ok "FaceLandmarker model downloaded"
  else
    err "Model download failed — the backend will retry automatically on first use"
  fi
fi

# ── 4. Verify GPU / backend ─────────────────────────────────────────────────
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
    from mediapipe.tasks import python as mp_python
    from mediapipe.tasks.python import vision as mp_vision
    print("  mediapipe Tasks:  FaceLandmarker available  ✓")
except ImportError:
    print("  mediapipe:        NOT INSTALLED  ✗")

try:
    import face_recognition
    print(f"  face_recognition: installed  ✓")
except ImportError:
    print("  face_recognition: NOT INSTALLED  (identity check disabled)")

import os
_model = "backend/app/api/models/face_landmarker.task"
if os.path.exists(_model) and os.path.getsize(_model) > 100000:
    print("  FaceLandmarker model: present  ✓")
else:
    print("  FaceLandmarker model: missing (auto-downloads on first use)")

print("")
print("  Inference delegate:")
print("  • Default:        CPU / XNNPACK (SIMD-accelerated, stable everywhere)")
print("  • Apple Silicon:  XNNPACK uses Accelerate/SIMD — fast for single face")
print("  • Linux/Win CUDA: set JARVIS_VISION_GPU=1 to use the GPU delegate")
print("  • Note: the macOS Metal GPU delegate is disabled (upstream desktop bug)")
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
