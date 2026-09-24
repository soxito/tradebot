#!/usr/bin/env bash
#
# Soup Plugin — installer (macOS / Linux)
#
# Installs MakazhanAlpamys/Soup (Apache-2.0) for LLM fine-tuning.
# - Idempotent: skips if already installed (pass --force to reinstall).
# - Creates isolated venv at ~/.tradebot/soup-venv (avoids backend dep conflicts).
# - Installs soup-cli[train,ui,serve,fast] + clones repo to integrations/soup
#   for recipe/version tracking.
# - Supports Python 3.10-3.12 (torch wheels unavailable on 3.13+ / 3.14).
#
# Usage:
#   bash plugins/SoupPlugin/scripts/setup_soup.sh
#   bash plugins/SoupPlugin/scripts/setup_soup.sh --force
#   TRADEBOT_SOUP_VENV=~/.tradebot/soup-venv bash plugins/SoupPlugin/scripts/setup_soup.sh
#
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$PLUGIN_DIR/../.." && pwd)"
SOUP_REPO="https://github.com/MakazhanAlpamys/Soup.git"
SOUP_DIR="${REPO_ROOT}/integrations/soup"
SOUP_VERSION_FILE="${REPO_ROOT}/integrations/soup.version.json"
SOUP_VENV="${TRADEBOT_SOUP_VENV:-$HOME/.tradebot/soup-venv}"
SOUP_VENV_BIN="$SOUP_VENV/bin"
SOUP_PY="$SOUP_VENV_BIN/python"
SOUP_PIP="$SOUP_VENV_BIN/pip"

FORCE=0
if [[ "${1:-}" == "--force" ]]; then FORCE=1; fi

echo "==> Soup setup — repo=$SOUP_REPO  dir=$SOUP_DIR  venv=$SOUP_VENV"

if [[ $FORCE -eq 0 ]] && [[ -f "$SOUP_VENV_BIN/soup" || -f "$SOUP_VENV_BIN/soup-cli" ]] && [[ -d "$SOUP_DIR/.git" ]]; then
  echo "==> soup already installed at $SOUP_VENV — skipping (use --force to reinstall)."
  # Still verify version file
  if [[ ! -f "$SOUP_VERSION_FILE" ]] && command -v git >/dev/null 2>&1; then
    SHA="$(git -C "$SOUP_DIR" rev-parse HEAD 2>/dev/null || echo unknown)"
    printf '{"commit":"%s","installed_at":"%s","repo":"%s"}\n' "$SHA" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$SOUP_REPO" > "$SOUP_VERSION_FILE" || true
  fi
  exit 0
fi

# ── Python selection (prefer 3.11/3.12) ──────────────────────────────────
pick_python() {
  for c in python3.12 python3.11 python3 python; do
    if command -v "$c" >/dev/null 2>&1; then
      # Check minor version is 10-12
      if "$c" -c "import sys; assert 10 <= sys.version_info.minor <= 12" 2>/dev/null; then
        echo "$(command -v "$c")"; return 0
      fi
    fi
  done
  # Fallback scan for homebrew
  for p in /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 /usr/local/bin/python3.12 /usr/local/bin/python3.11; do
    if [[ -x "$p" ]] && "$p" -c "import sys; assert 10 <= sys.version_info.minor <= 12" 2>/dev/null; then
      echo "$p"; return 0
    fi
  done
  command -v python3 || echo python3
}

BASE_PY="$(pick_python)"
echo "==> Using base Python: $BASE_PY ($($BASE_PY --version 2>&1 || echo unknown))"

# ── Clone repo ────────────────────────────────────────────────────────────
if ! command -v git >/dev/null 2>&1; then
  echo "!! git not found — install git to enable Soup. Skipping." >&2
  exit 0
fi

mkdir -p "$(dirname "$SOUP_DIR")"
if [[ -d "$SOUP_DIR/.git" ]]; then
  echo "==> Updating existing soup repo …"
  git -C "$SOUP_DIR" fetch --depth 1 origin 2>&1 | head -20 || true
  git -C "$SOUP_DIR" pull --ff-only 2>&1 | head -20 || true
else
  echo "==> Cloning Soup → $SOUP_DIR …"
  rm -rf "$SOUP_DIR"
  git clone --depth 1 "$SOUP_REPO" "$SOUP_DIR" || {
    echo "!! clone failed — check network / git. Soup unavailable." >&2
    exit 0
  }
fi

# ── Create isolated venv ──────────────────────────────────────────────────
if [[ ! -x "$SOUP_PY" ]]; then
  echo "==> Creating Soup venv at $SOUP_VENV with $BASE_PY …"
  mkdir -p "$(dirname "$SOUP_VENV")"
  "$BASE_PY" -m venv "$SOUP_VENV" || {
    echo "!! venv creation failed — try: $BASE_PY -m venv $SOUP_VENV" >&2
    exit 0
  }
  echo "    venv created."
else
  echo "==> Soup venv already exists at $SOUP_VENV — reusing."
fi

echo "==> Upgrading pip …"
"$SOUP_PIP" install --quiet --upgrade pip 2>&1 | tail -5 || true

echo "==> Installing soup-cli[train,ui,serve,fast] (may take several minutes) …"
# Try full extras, fallback to minimal if heavy deps fail (e.g., no torch wheel)
set +e
"$SOUP_PIP" install --quiet --prefer-binary --upgrade "soup-cli[train,ui,serve,fast]" 2>&1 | tail -20
EC=$?
if [[ $EC -ne 0 ]]; then
  echo "!! full extras failed (EC=$EC) — trying minimal soup-cli …"
  "$SOUP_PIP" install --quiet --prefer-binary --upgrade "soup-cli" 2>&1 | tail -20
  EC2=$?
  if [[ $EC2 -ne 0 ]]; then
    echo "!! soup-cli install failed entirely — check Python version (need 3.10-3.12) and network." >&2
  else
    echo "==> minimal soup-cli installed (retry with Python 3.11 for full features)."
  fi
else
  echo "==> soup-cli[train,ui,serve,fast] installed."
fi
set -e

# Verify
if [[ -f "$SOUP_VENV_BIN/soup" ]]; then
  echo "==> soup binary: $SOUP_VENV_BIN/soup"
  "$SOUP_VENV_BIN/soup" --version 2>&1 | head -5 || "$SOUP_VENV_BIN/soup" --help 2>&1 | head -10 || true
else
  echo "!! soup binary not found at $SOUP_VENV_BIN/soup — install may have failed, but repo is cloned." >&2
fi

# Quick health: soup doctor if available
if "$SOUP_VENV_BIN/soup" 2>&1 | grep -q doctor; then
  "$SOUP_VENV_BIN/soup" doctor 2>&1 | head -40 || true
fi

# Write version file
SHA="$(git -C "$SOUP_DIR" rev-parse HEAD 2>/dev/null || echo unknown)"
TS="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date)"
printf '{"commit":"%s","installed_at":"%s","repo":"%s","venv":"%s"}\n' "$SHA" "$TS" "$SOUP_REPO" "$SOUP_VENV" > "$SOUP_VERSION_FILE" || true
echo "==> Wrote $SOUP_VERSION_FILE"

echo "==> Done. Soup available via: $SOUP_VENV_BIN/soup"
echo "    UI: $SOUP_VENV_BIN/soup ui --host 127.0.0.1 --port 7860"
echo "    Serve: $SOUP_VENV_BIN/soup serve --model <path> --host 127.0.0.1 --port 8012"
