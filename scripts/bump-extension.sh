#!/usr/bin/env bash
#
# bump-extension.sh — bump the JARVIS extension version and rebuild the ZIPs.
#
# Every time you change the extension, run this so the version increments and
# the auto-update banner fires for users on the older version.
#
# Usage:
#   ./scripts/bump-extension.sh              # patch bump  3.0.0 -> 3.0.1
#   ./scripts/bump-extension.sh patch        # patch bump  3.0.0 -> 3.0.1
#   ./scripts/bump-extension.sh minor        # minor bump  3.0.0 -> 3.1.0
#   ./scripts/bump-extension.sh major        # major bump  3.0.0 -> 4.0.0
#   ./scripts/bump-extension.sh 3.2.5        # set exact version
#   ./scripts/bump-extension.sh patch "Fixed mic muting" "Faster popup"
#                                            # patch bump + changelog entries
#
# What it does (atomic, all-or-nothing):
#   1. Reads current version from jarvis-extension/manifest.json
#   2. Computes the new version
#   3. Updates manifest.json           (what gets installed)
#   4. Updates backend _EXT_VERSION    (what the backend reports as "latest")
#   5. Updates backend _EXT_RELEASED   (today's date)
#   6. Optionally prepends changelog entries
#   7. Rebuilds frontend/public/jarvis-extension.zip            (legacy name)
#      and  frontend/public/jarvis-extension-v<VERSION>.zip     (versioned)
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANIFEST="$ROOT/jarvis-extension/manifest.json"
BACKEND="$ROOT/backend/app/api/jarvis.py"

# ── Colors ────────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'

die() { echo -e "${RED}✗ $1${NC}" >&2; exit 1; }

[[ -f "$MANIFEST" ]] || die "manifest.json not found at $MANIFEST"
[[ -f "$BACKEND" ]]  || die "backend jarvis.py not found at $BACKEND"

# ── Read current version from the manifest (source of truth) ──────────────────
CURRENT="$(python3 -c "import json,sys; print(json.load(open('$MANIFEST'))['version'])")" \
  || die "could not read version from manifest.json"

# ── Compute the new version ───────────────────────────────────────────────────
ARG="${1:-patch}"
shift || true   # remaining args become changelog entries

IFS='.' read -r MAJ MIN PAT <<< "$CURRENT"

case "$ARG" in
  patch) PAT=$((PAT + 1)) ;;
  minor) MIN=$((MIN + 1)); PAT=0 ;;
  major) MAJ=$((MAJ + 1)); MIN=0; PAT=0 ;;
  [0-9]*.[0-9]*.[0-9]*)
    # Explicit version like 3.2.5
    NEW="$ARG"
    ;;
  *) die "Unknown bump type '$ARG'. Use: patch | minor | major | X.Y.Z" ;;
esac

NEW="${NEW:-$MAJ.$MIN.$PAT}"
TODAY="$(date +%Y-%m-%d)"

echo -e "${CYAN}▶ Bumping JARVIS extension: ${YELLOW}$CURRENT${CYAN} → ${GREEN}$NEW${NC}"

# ── 1. Update manifest.json (preserve formatting via Python) ──────────────────
python3 - "$MANIFEST" "$NEW" <<'PY'
import json, sys
path, new = sys.argv[1], sys.argv[2]
with open(path) as f:
    data = json.load(f)
data["version"] = new
with open(path, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
print(f"  ✓ manifest.json → {new}")
PY

# ── 2. Update backend _EXT_VERSION + _EXT_RELEASED ────────────────────────────
python3 - "$BACKEND" "$NEW" "$TODAY" "$@" <<'PY'
import re, sys
path, new, today, *changelog = sys.argv[1:]
with open(path) as f:
    src = f.read()

# _EXT_VERSION
src, n1 = re.subn(r'_EXT_VERSION\s*=\s*"[^"]*"', f'_EXT_VERSION = "{new}"', src, count=1)
# _EXT_RELEASED
src, n2 = re.subn(r'_EXT_RELEASED\s*=\s*"[^"]*"', f'_EXT_RELEASED = "{today}"', src, count=1)

if n1 == 0:
    print("  ✗ could not find _EXT_VERSION in backend", file=sys.stderr); sys.exit(1)

# Optionally prepend changelog entries to _EXT_CHANGELOG = [ ... ]
if changelog:
    m = re.search(r'(_EXT_CHANGELOG\s*=\s*\[)(.*?)(\])', src, re.DOTALL)
    if m:
        head, body, tail = m.group(1), m.group(2), m.group(3)
        new_entries = "".join(f'\n    "{c}",' for c in changelog)
        src = src[:m.start()] + head + new_entries + body + tail + src[m.end():]
        print(f"  ✓ added {len(changelog)} changelog entr{'y' if len(changelog)==1 else 'ies'}")

with open(path, "w") as f:
    f.write(src)
print(f"  ✓ backend _EXT_VERSION → {new} (released {today})")
PY

# ── 3. Rebuild the ZIPs (versioned + legacy), clean old versioned ZIPs ────────
python3 - "$ROOT" "$NEW" <<'PY'
import zipfile, sys
from pathlib import Path
root, version = sys.argv[1], sys.argv[2]
ext_dir = Path(root) / "jarvis-extension"
public = Path(root) / "frontend" / "public"

# Remove stale versioned ZIPs so only the current one remains
for old in public.glob("jarvis-extension-v*.zip"):
    if old.name != f"jarvis-extension-v{version}.zip":
        old.unlink()
        print(f"  ✓ removed stale {old.name}")

outputs = [
    public / f"jarvis-extension-v{version}.zip",
    public / "jarvis-extension.zip",
]
for out in outputs:
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in sorted(ext_dir.rglob("*")):
            if item.is_file() and not any(p.startswith(".") for p in item.parts):
                zf.write(item, item.relative_to(ext_dir))
    print(f"  ✓ {out.name} ({out.stat().st_size/1024:.1f} KB)")
PY

echo -e "${GREEN}✓ Extension bumped to v$NEW${NC}"
echo -e "${CYAN}  Next steps:${NC}"
echo "    1. Restart the backend so it serves v$NEW as latest"
echo "    2. Users on the old version will see the update banner automatically"
echo "    3. They reload the extension in chrome://extensions to get v$NEW"
