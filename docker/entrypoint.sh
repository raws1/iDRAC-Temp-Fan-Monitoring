#!/bin/sh
set -eu

required_vars="IDRAC_HOST IDRAC_USER IDRAC_PASSWORD"
for var in $required_vars; do
  eval "value=\${$var:-}"
  if [ -z "$value" ]; then
    echo "Missing required environment variable: $var" >&2
    exit 1
  fi
done

OUTPUT_DIR="${OUTPUT_DIR:-/data}"
HTTP_BIND="${HTTP_BIND:-0.0.0.0}"
HTTP_PORT="${HTTP_PORT:-8580}"
CHECK_INTERVAL_SECONDS="${CHECK_INTERVAL_SECONDS:-180}"
INTERVAL_SECONDS="${INTERVAL_SECONDS:-$CHECK_INTERVAL_SECONDS}"
DURATION_SECONDS="${DURATION_SECONDS:-0}"
ENABLE_POWEREDGE_SHUTUP="${ENABLE_POWEREDGE_SHUTUP:-1}"
FANCONTROL_INTERVAL_SECONDS="${FANCONTROL_INTERVAL_SECONDS:-$CHECK_INTERVAL_SECONDS}"
export CHECK_INTERVAL_SECONDS INTERVAL_SECONDS FANCONTROL_INTERVAL_SECONDS

mkdir -p "$OUTPUT_DIR"
python3 /app/generate_fan_curve_panel.py \
  --source /app/PowerEdge-shutup/fancontrol.sh \
  --output "$OUTPUT_DIR/fan_curve_panel.html" \
  --alert-output "$OUTPUT_DIR/alert_status_panel.html"
cp /app/index.html "$OUTPUT_DIR/index.html"

python3 -u /app/monitor_idrac_temps_f.py \
  --host "$IDRAC_HOST" \
  --user "$IDRAC_USER" \
  --password "$IDRAC_PASSWORD" \
  --interval-seconds "$INTERVAL_SECONDS" \
  --duration-seconds "$DURATION_SECONDS" \
  --out-dir "$OUTPUT_DIR" &

monitor_pid="$!"
pids="$monitor_pid"

if [ "$ENABLE_POWEREDGE_SHUTUP" = "1" ]; then
  /app/fancontrol-loop.sh &
  fancontrol_pid="$!"
  pids="$pids $fancontrol_pid"
fi

cleanup() {
  for pid in $pids; do
    kill "$pid" 2>/dev/null || true
  done
  for pid in $pids; do
    wait "$pid" 2>/dev/null || true
  done
}

trap cleanup INT TERM

cd "$OUTPUT_DIR"
python3 -m http.server "$HTTP_PORT" --bind "$HTTP_BIND" &
http_pid="$!"
pids="$pids $http_pid"

status=0
while :; do
  for pid in $pids; do
    if ! kill -0 "$pid" 2>/dev/null; then
      wait "$pid" || status="$?"
      cleanup
      exit "$status"
    fi
  done
  sleep 1
done

cleanup

exit "$status"
