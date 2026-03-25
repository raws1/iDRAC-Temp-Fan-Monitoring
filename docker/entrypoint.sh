#!/bin/sh
set -eu

required_vars="IDRAC_USER IDRAC_PASSWORD"
for var in $required_vars; do
  eval "value=\${$var:-}"
  if [ -z "$value" ]; then
    echo "Missing required environment variable: $var" >&2
    exit 1
  fi
done

HOSTS_RAW="${IDRAC_HOSTS:-${IDRAC_HOST:-}}"
if [ -z "$HOSTS_RAW" ]; then
  echo "Missing required environment variable: IDRAC_HOSTS or IDRAC_HOST" >&2
  exit 1
fi

OUTPUT_DIR="${OUTPUT_DIR:-/data}"
HTTP_BIND="${HTTP_BIND:-0.0.0.0}"
HTTP_PORT="${HTTP_PORT:-8580}"
CHECK_INTERVAL_SECONDS="${CHECK_INTERVAL_SECONDS:-180}"
INTERVAL_SECONDS="${INTERVAL_SECONDS:-$CHECK_INTERVAL_SECONDS}"
DURATION_SECONDS="${DURATION_SECONDS:-0}"
ENABLE_POWEREDGE_SHUTUP="${ENABLE_POWEREDGE_SHUTUP:-1}"
FANCONTROL_INTERVAL_SECONDS="${FANCONTROL_INTERVAL_SECONDS:-$CHECK_INTERVAL_SECONDS}"
export CHECK_INTERVAL_SECONDS INTERVAL_SECONDS FANCONTROL_INTERVAL_SECONDS

slugify_host() {
  printf '%s' "$1" | tr -cs 'A-Za-z0-9._-' '-'
}

HOSTS_LIST="$(printf '%s' "$HOSTS_RAW" | tr ',\n' '  ' | xargs)"
if [ -z "$HOSTS_LIST" ]; then
  echo "No valid hosts found in IDRAC_HOSTS/IDRAC_HOST" >&2
  exit 1
fi

mkdir -p "$OUTPUT_DIR"
mkdir -p "$OUTPUT_DIR/hosts"

hosts_manifest_tmp="$OUTPUT_DIR/hosts.json.tmp"
printf '[' > "$hosts_manifest_tmp"
first_host=1
for host in $HOSTS_LIST; do
  host_slug="$(slugify_host "$host")"
  if [ "$first_host" -eq 1 ]; then
    first_host=0
  else
    printf ',' >> "$hosts_manifest_tmp"
  fi
  printf '\n  {"host":"%s","slug":"%s"}' "$host" "$host_slug" >> "$hosts_manifest_tmp"
done
printf '\n]\n' >> "$hosts_manifest_tmp"
mv "$hosts_manifest_tmp" "$OUTPUT_DIR/hosts.json"

python3 /app/generate_fan_curve_panel.py \
  --source /app/PowerEdge-shutup/fancontrol.sh \
  --output "$OUTPUT_DIR/fan_curve_panel.html" \
  --alert-output "$OUTPUT_DIR/alert_status_panel.html"
cp /app/index.html "$OUTPUT_DIR/index.html"

pids=""
for host in $HOSTS_LIST; do
  host_slug="$(slugify_host "$host")"
  host_dir="$OUTPUT_DIR/hosts/$host_slug"
  mkdir -p "$host_dir"

  python3 -u /app/monitor_idrac_temps_f.py \
    --host "$host" \
    --user "$IDRAC_USER" \
    --password "$IDRAC_PASSWORD" \
    --interval-seconds "$INTERVAL_SECONDS" \
    --duration-seconds "$DURATION_SECONDS" \
    --out-dir "$host_dir" &
  monitor_pid="$!"
  pids="$pids $monitor_pid"

  if [ "$ENABLE_POWEREDGE_SHUTUP" = "1" ]; then
    POWEREDGE_SHUTUP_IPMIHOST="$host" OUTPUT_DIR="$host_dir" /app/fancontrol-loop.sh &
    fancontrol_pid="$!"
    pids="$pids $fancontrol_pid"
  fi
done

echo "Monitoring hosts: $HOSTS_LIST"

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
