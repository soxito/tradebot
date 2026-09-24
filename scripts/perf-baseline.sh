#!/usr/bin/env bash
# perf-baseline.sh — capture perf snapshot before/after optimization
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/data/perf-baseline.json"
mkdir -p "$ROOT/data"
echo "=== TradeBot perf baseline ==="
echo "Date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Frontend bundle
FE_NEXT="$ROOT/frontend/.next"
FE_SIZE="n/a"
if [ -d "$FE_NEXT" ]; then
  FE_SIZE=$(du -sh "$FE_NEXT" 2>/dev/null | cut -f1)
  CHUNKS=$(ls -lh "$FE_NEXT/static/chunks/"*.js 2>/dev/null | awk '{print $9, $5}' | head -15 | tr '\n' ';' || echo "n/a")
else
  CHUNKS="no build yet"
fi

# Backend log stats
LOG="$ROOT/logs/backend.log"
LOG_SIZE=$(du -sh "$LOG" 2>/dev/null | cut -f1 || echo "n/a")
LOG_LINES=$(wc -l < "$LOG" 2>/dev/null | tr -d ' ' || echo "0")

# Docker (optional)
DOCKER_STATS=$(docker stats --no-stream --format "{{.Name}} {{.CPUPerc}} {{.MemUsage}}" 2>/dev/null | head -10 || echo "docker not running")

cat > "$OUT" <<JSON
{
  "date": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "frontend_next_size": "$FE_SIZE",
  "frontend_chunks": "$CHUNKS",
  "backend_log_size": "$LOG_SIZE",
  "backend_log_lines": $LOG_LINES,
  "docker_stats": "$(echo "$DOCKER_STATS" | tr '\n' ';' | sed 's/"/\\"/g')"
}
JSON
cat "$OUT"
echo "Written to $OUT"
