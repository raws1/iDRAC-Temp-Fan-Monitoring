#!/bin/sh
set -eu

export TZ="${TZ:-America/New_York}"

CHECK_INTERVAL_SECONDS="${CHECK_INTERVAL_SECONDS:-180}"
INTERVAL_SECONDS="${FANCONTROL_INTERVAL_SECONDS:-$CHECK_INTERVAL_SECONDS}"
OUTPUT_DIR="${OUTPUT_DIR:-/data}"
LOG_FILE="${OUTPUT_DIR}/fancontrol.log"

mkdir -p "$OUTPUT_DIR"
touch "$LOG_FILE"

while :; do
  ts="$(date '+%Y-%m-%d %I:%M:%S %p %Z')"
  echo "[fancontrol] $ts running PowerEdge-shutup/fancontrol.sh" | tee -a "$LOG_FILE"
  tmp_log="$(mktemp)"
  if bash /app/PowerEdge-shutup/fancontrol.sh >"$tmp_log" 2>&1; then
    cat "$tmp_log" | tee -a "$LOG_FILE"
    rm -f "$tmp_log"
  else
    cat "$tmp_log" | tee -a "$LOG_FILE"
    rm -f "$tmp_log"
    echo "[fancontrol] $ts fancontrol.sh failed" | tee -a "$LOG_FILE" >&2
  fi
  sleep "$INTERVAL_SECONDS"
done
