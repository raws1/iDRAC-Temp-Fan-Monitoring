#!/bin/sh
set -eu

export TZ="${TZ:-America/New_York}"

CHECK_INTERVAL_SECONDS="${CHECK_INTERVAL_SECONDS:-180}"
INTERVAL_SECONDS="${FANCONTROL_INTERVAL_SECONDS:-$CHECK_INTERVAL_SECONDS}"
OUTPUT_DIR="${OUTPUT_DIR:-/data}"
LOG_FILE="${OUTPUT_DIR}/fancontrol.log"
TARGET_HOST="${POWEREDGE_SHUTUP_IPMIHOST:-${IDRAC_HOST:-unknown-host}}"

mkdir -p "$OUTPUT_DIR"
touch "$LOG_FILE"

while :; do
  ts="$(date '+%Y-%m-%d %I:%M:%S %p %Z')"
  echo "[fancontrol] $ts host=$TARGET_HOST" | tee -a "$LOG_FILE"
  tmp_log="$(mktemp)"
  if bash /app/PowerEdge-shutup/fancontrol.sh >"$tmp_log" 2>&1; then
    cat "$tmp_log" | tee -a "$LOG_FILE"
    rm -f "$tmp_log"
  else
    cat "$tmp_log" | tee -a "$LOG_FILE"
    rm -f "$tmp_log"
    echo "[fancontrol-error] $ts host=$TARGET_HOST fancontrol.sh failed" | tee -a "$LOG_FILE" >&2
  fi
  sleep "$INTERVAL_SECONDS"
done
