#!/usr/bin/env python3
from __future__ import annotations

import csv
import datetime as dt
import html
import http.server
import math
import os
import pathlib
import re
import socketserver
import smtplib
import ssl
import subprocess
import threading
import time
from email.message import EmailMessage
from typing import Dict, List, Tuple


OUT_DIR = pathlib.Path("/data")
TEMP_CSV_PATH = OUT_DIR / "temps_f.csv"
FAN_CSV_PATH = OUT_DIR / "fans_rpm.csv"
COMBINED_SVG_PATH = OUT_DIR / "thermal_dashboard_live.svg"
HTML_PATH = OUT_DIR / "index.html"

NAS_HOST = os.environ.get("NAS_HOST", "nas.example.internal")
NAS_USER = os.environ.get("NAS_USER", "admin")
SSH_KEY_PATH = os.environ.get("SSH_KEY_PATH", "/run/ssh/id_ed25519_hddtemps")
SSH_KNOWN_HOSTS = os.environ.get("SSH_KNOWN_HOSTS", "/run/ssh/known_hosts")
PORT = int(os.environ.get("PORT", "8789"))
INTERVAL_SECONDS = int(os.environ.get("INTERVAL_SECONDS", "60"))
WINDOW_HOURS = int(os.environ.get("WINDOW_HOURS", "24"))
TEMP_ALERT_THRESHOLD_F = float(os.environ.get("NOTIFICATION_TEMP_THRESHOLD_F", "125"))
EMAIL_ENABLED = os.environ.get("NOTIFICATION_EMAIL_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
EMAIL_FROM = os.environ.get("NOTIFICATION_EMAIL_FROM", "").strip()
EMAIL_TO_RAW = os.environ.get("NOTIFICATION_EMAIL_TO", "").strip()
EMAIL_SERVER = os.environ.get("NOTIFICATION_EMAIL_SERVER", "").strip()
EMAIL_SERVER_PORT = int(os.environ.get("NOTIFICATION_EMAIL_SERVER_PORT", "587"))
EMAIL_SERVER_USER = os.environ.get("NOTIFICATION_EMAIL_SERVER_USER", "").strip()
EMAIL_SERVER_PASSWORD = os.environ.get("NOTIFICATION_EMAIL_SERVER_PASSWORD", "")
EMAIL_STARTTLS = os.environ.get("NOTIFICATION_EMAIL_STARTTLS", "true").lower() in {"1", "true", "yes", "on"}
EMAIL_TIMEOUT_SECONDS = int(os.environ.get("NOTIFICATION_EMAIL_TIMEOUT_SECONDS", "20"))
SEND_TEST_EMAIL_ON_START = os.environ.get("NOTIFICATION_SEND_TEST_EMAIL_ON_START", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}

DEFAULT_DISK_MAP = "3:HDD 1,4:HDD 2,5:HDD 3,6:HDD 4,7:HDD 5,8:HDD 6,9:HDD 7,10:HDD 8"
DEFAULT_FAN_MAP = "0:Fan 1,1:Fan 2,2:Fan 3"
DISK_MAP_RAW = os.environ.get("DISK_MAP", DEFAULT_DISK_MAP)
FAN_MAP_RAW = os.environ.get("FAN_MAP", DEFAULT_FAN_MAP)

COLORS = [
    "#1f77b4",
    "#d62728",
    "#2ca02c",
    "#ff7f0e",
    "#9467bd",
    "#8c564b",
    "#17becf",
    "#bcbd22",
    "#e11d48",
    "#14b8a6",
]
FAN_COLORS = ["#0f766e", "#0891b2", "#7c3aed"]
TEMP_LINE_STYLES = {
    "CPU Temp": "10 6",
    "System Temp": "3 5",
}

FAN_RE = re.compile(r"fan index = (\d+),ret = (-?\d+),fan = (\d+) rpm,fan_fail = (\d+)")
GETSYSINFO_TEMP_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*C\s*/\s*(-?\d+(?:\.\d+)?)\s*F")
ACTIVE_TEMP_ALERTS: set[str] = set()


def parse_index_map(raw: str) -> List[Tuple[int, str]]:
    result: List[Tuple[int, str]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        index_raw, label = part.split(":", 1)
        result.append((int(index_raw.strip()), label.strip()))
    return result


DISK_MAP = parse_index_map(DISK_MAP_RAW)
FAN_MAP = parse_index_map(FAN_MAP_RAW)
DISK_LABELS = [label for _, label in DISK_MAP]
EXTRA_TEMP_LABELS = ["CPU Temp", "System Temp"]
TEMP_LABELS = [*DISK_LABELS, *EXTRA_TEMP_LABELS]
FAN_LABELS = [label for _, label in FAN_MAP]
EMAIL_TO = [part.strip() for part in re.split(r"[;,]", EMAIL_TO_RAW) if part.strip()]


class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"http: {fmt % args}", flush=True)


def start_http_server() -> None:
    os.chdir(OUT_DIR)
    with socketserver.ThreadingTCPServer(("", PORT), NoCacheHandler) as httpd:
        httpd.serve_forever()


def ssh_command() -> List[str]:
    return [
        "ssh",
        "-x",
        "-i",
        SSH_KEY_PATH,
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={SSH_KNOWN_HOSTS}",
        f"{NAS_USER}@{NAS_HOST}",
    ]


def run_remote_command(remote_command: str, timeout: int = 45) -> str:
    return subprocess.check_output(ssh_command() + [remote_command], text=True, timeout=timeout)


def parse_getsysinfo_temp_f(sensor_name: str, raw_value: str) -> float:
    match = GETSYSINFO_TEMP_RE.search(raw_value.strip())
    if not match:
        raise RuntimeError(f"Failed to parse {sensor_name} temperature from {raw_value!r}")
    return round(float(match.group(2)))


def email_is_configured() -> bool:
    return EMAIL_ENABLED and bool(EMAIL_FROM and EMAIL_TO and EMAIL_SERVER)


def send_email(subject: str, body: str) -> None:
    if not email_is_configured():
        raise RuntimeError("email notifications are enabled but SMTP settings are incomplete")

    message = EmailMessage()
    message["From"] = EMAIL_FROM
    message["To"] = ", ".join(EMAIL_TO)
    message["Subject"] = subject
    message.set_content(body)

    with smtplib.SMTP(EMAIL_SERVER, EMAIL_SERVER_PORT, timeout=EMAIL_TIMEOUT_SECONDS) as smtp:
        smtp.ehlo()
        if EMAIL_STARTTLS:
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
        if EMAIL_SERVER_USER:
            smtp.login(EMAIL_SERVER_USER, EMAIL_SERVER_PASSWORD)
        smtp.send_message(message)


def maybe_send_startup_test_email(timestamp_local: dt.datetime) -> None:
    if not SEND_TEST_EMAIL_ON_START:
        return
    if not email_is_configured():
        print("Startup test email skipped: email notifications are disabled or incomplete", flush=True)
        return

    subject = f"[hddtemps] Startup test email for {NAS_HOST}"
    body = "\n".join(
        [
            "The HDD temp monitor container has started.",
            "",
            f"NAS host: {NAS_HOST}",
            f"Checked by: {NAS_USER}",
            f"Time: {timestamp_local.strftime('%Y-%m-%d %H:%M:%S %Z')}",
            f"Alert threshold: {TEMP_ALERT_THRESHOLD_F:.1f} F",
        ]
    )
    send_email(subject, body)
    print("Startup test email sent", flush=True)


def check_temp_alerts(timestamp_local: dt.datetime, temps_f: Dict[str, float]) -> None:
    global ACTIVE_TEMP_ALERTS

    if not EMAIL_ENABLED:
        return

    current_hot = {label for label, value in temps_f.items() if value > TEMP_ALERT_THRESHOLD_F}
    if not current_hot:
        ACTIVE_TEMP_ALERTS = set()
        return

    newly_hot = current_hot - ACTIVE_TEMP_ALERTS
    if not newly_hot:
        ACTIVE_TEMP_ALERTS = current_hot
        return

    ordered_hot = [(label, temps_f[label]) for label in DISK_LABELS if label in current_hot]
    ordered_new = [(label, temps_f[label]) for label in DISK_LABELS if label in newly_hot]
    subject = f"[hddtemps] ALERT {NAS_HOST} HDD temp over {TEMP_ALERT_THRESHOLD_F:.0f}F"
    body_lines = [
        "One or more HDD temperatures crossed the configured threshold.",
        "",
        f"NAS host: {NAS_HOST}",
        f"Checked by: {NAS_USER}",
        f"Time: {timestamp_local.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"Threshold: {TEMP_ALERT_THRESHOLD_F:.0f} F",
        "",
        "Newly triggered:",
    ]
    body_lines.extend(f"- {label}: {value:.0f} F" for label, value in ordered_new)
    body_lines.append("")
    body_lines.append("Currently above threshold:")
    body_lines.extend(f"- {label}: {value:.0f} F" for label, value in ordered_hot)
    send_email(subject, "\n".join(body_lines))
    ACTIVE_TEMP_ALERTS = current_hot
    print(f"Temperature alert email sent for {', '.join(label for label, _ in ordered_new)}", flush=True)


def fetch_temps_f() -> Dict[str, float]:
    disk_numbers = [str(disk_num) for disk_num, _ in DISK_MAP]
    remote_loop = (
        f'for i in {" ".join(disk_numbers)}; do '
        'printf "DISK_%s " "$i"; '
        '/sbin/get_hd_temp "$i" 2>/dev/null || echo ERR; '
        "done; "
        'cpu_temp=$(/sbin/getsysinfo cputmp 2>/dev/null || echo ERR); '
        'printf "CPU_TEMP %s\\n" "$cpu_temp"; '
        'system_temp=$(/sbin/getsysinfo systmp 2>/dev/null || echo ERR); '
        'printf "SYSTEM_TEMP %s\\n" "$system_temp"'
    )
    output = run_remote_command(remote_loop)

    temps_c: Dict[int, float] = {}
    extra_temps_f: Dict[str, float] = {}
    for line in output.splitlines():
        parts = line.strip().split()
        if len(parts) < 2:
            continue
        sensor_name = parts[0]
        if sensor_name.startswith("DISK_"):
            disk_num = int(sensor_name.split("_", 1)[1])
            temp_raw = parts[1]
            if temp_raw == "ERR":
                raise RuntimeError(f"Failed to read disk temperature for disk {disk_num}")
            temps_c[disk_num] = float(temp_raw)
            continue

        sensor_raw = " ".join(parts[1:])
        if sensor_raw == "ERR":
            raise RuntimeError(f"Failed to read {sensor_name.lower()} from getsysinfo")
        if sensor_name == "CPU_TEMP":
            extra_temps_f["CPU Temp"] = parse_getsysinfo_temp_f("CPU temp", sensor_raw)
        elif sensor_name == "SYSTEM_TEMP":
            extra_temps_f["System Temp"] = parse_getsysinfo_temp_f("system temp", sensor_raw)

    temps_f: Dict[str, float] = {}
    for disk_num, label in DISK_MAP:
        if disk_num not in temps_c:
            raise RuntimeError(f"Missing temperature for disk {disk_num}")
        temps_f[label] = round((temps_c[disk_num] * 9.0 / 5.0) + 32.0)
    for label in EXTRA_TEMP_LABELS:
        if label not in extra_temps_f:
            raise RuntimeError(f"Missing temperature for {label.lower()}")
        temps_f[label] = extra_temps_f[label]
    return temps_f


def fetch_fans_rpm() -> Dict[str, float]:
    fan_indexes = [str(fan_index) for fan_index, _ in FAN_MAP]
    remote_loop = (
        f'for i in {" ".join(fan_indexes)}; do '
        '/sbin/hal_app --se_sys_get_fan enc_sys_id=root,obj_index="$i" 2>&1; '
        "done"
    )
    output = run_remote_command(remote_loop)

    fans_rpm: Dict[int, float] = {}
    for line in output.splitlines():
        match = FAN_RE.search(line)
        if not match:
            continue
        fan_index = int(match.group(1))
        ret_code = int(match.group(2))
        fan_rpm = float(match.group(3))
        fan_fail = int(match.group(4))
        if ret_code != 0:
            raise RuntimeError(f"Fan query failed for fan {fan_index} with ret={ret_code}")
        if fan_fail != 0:
            raise RuntimeError(f"Fan {fan_index} reports fan_fail={fan_fail}")
        fans_rpm[fan_index] = fan_rpm

    result: Dict[str, float] = {}
    for fan_index, label in FAN_MAP:
        if fan_index not in fans_rpm:
            raise RuntimeError(f"Missing RPM for fan {fan_index}")
        result[label] = fans_rpm[fan_index]
    return result


def ensure_csv(path: pathlib.Path, labels: List[str]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    expected_header = ["timestamp_local", "timestamp_utc", *labels]

    if path.exists():
        with path.open(newline="") as fh:
            reader = csv.DictReader(fh)
            header = reader.fieldnames
            rows = list(reader) if header and header != expected_header else []
        if header == expected_header:
            return
        if header and header[:2] == expected_header[:2] and set(header).issubset(set(expected_header)):
            with path.open("w", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=expected_header)
                writer.writeheader()
                for row in rows:
                    writer.writerow({name: row.get(name, "") for name in expected_header})
            return
        backup_name = f"{path.stem}.bak-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}{path.suffix}"
        path.replace(path.with_name(backup_name))

    with path.open("w", newline="") as fh:
        csv.writer(fh).writerow(expected_header)


def append_sample(
    path: pathlib.Path,
    labels: List[str],
    timestamp_local: dt.datetime,
    timestamp_utc: dt.datetime,
    values: Dict[str, float],
) -> None:
    with path.open("a", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                timestamp_local.isoformat(timespec="seconds"),
                timestamp_utc.isoformat(timespec="seconds"),
                *[values[label] for label in labels],
            ]
        )


def read_samples(path: pathlib.Path, labels: List[str]) -> List[Dict[str, object]]:
    ensure_csv(path, labels)
    rows: List[Dict[str, object]] = []
    with path.open(newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            item: Dict[str, object] = {
                "timestamp_local": dt.datetime.fromisoformat(row["timestamp_local"]),
                "timestamp_utc": dt.datetime.fromisoformat(row["timestamp_utc"]),
            }
            for label in labels:
                value = row.get(label, "")
                item[label] = float(value) if value else math.nan
            rows.append(item)
    return rows


def filter_window(rows: List[Dict[str, object]]) -> List[Dict[str, object]]:
    if not rows:
        return rows
    cutoff = rows[-1]["timestamp_local"] - dt.timedelta(hours=WINDOW_HOURS)  # type: ignore[operator]
    return [row for row in rows if row["timestamp_local"] >= cutoff]


def svg_escape(text: str) -> str:
    return html.escape(text, quote=True)


def format_metric_value(value: float, unit: str) -> str:
    if unit == "RPM":
        return f"{value:.0f} RPM"
    return f"{value:.0f} F"


def format_axis_value(value: float, step: float) -> str:
    if step >= 1:
        return f"{value:.0f}"
    return f"{value:.1f}"


def nice_tick_step(span: float, target_ticks: int = 7) -> float:
    if span <= 0:
        return 1.0
    rough_step = span / target_ticks
    magnitude = 10 ** math.floor(math.log10(rough_step))
    normalized = rough_step / magnitude
    if normalized <= 1:
        nice = 1
    elif normalized <= 2:
        nice = 2
    elif normalized <= 5:
        nice = 5
    else:
        nice = 10
    return nice * magnitude


def render_empty_svg(title: str, subtitle: str) -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1400" height="760" viewBox="0 0 1400 760">'
        '<rect width="100%" height="100%" fill="#f8fafc"/>'
        f'<text x="85" y="35" font-family="ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,&quot;Segoe UI&quot;,sans-serif" font-size="30" font-weight="700" fill="#0f172a">{svg_escape(title)}</text>'
        f'<text x="85" y="58" font-family="ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,&quot;Segoe UI&quot;,sans-serif" font-size="15" fill="#475569">{svg_escape(subtitle)}</text>'
        '<text x="50%" y="50%" text-anchor="middle" font-family="ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,&quot;Segoe UI&quot;,sans-serif" font-size="28" fill="#334155">No data yet</text>'
        "</svg>"
    )


def calc_axis(values: List[float], unit: str) -> Tuple[float, float, float]:
    base_padding = 2.0 if unit == "F" else 100.0
    tick_step = nice_tick_step((max(values) - min(values)) + (base_padding * 2.0))
    axis_min = math.floor((min(values) - base_padding) / tick_step) * tick_step
    axis_max = math.ceil((max(values) + base_padding) / tick_step) * tick_step
    if axis_min == axis_max:
        axis_max = axis_min + tick_step
    return axis_min, axis_max, tick_step


def render_combined_svg(temp_rows: List[Dict[str, object]], fan_rows: List[Dict[str, object]]) -> str:
    title = "NAS Temps + Fan RPM"
    subtitle = f"Last {WINDOW_HOURS} hours, updated every minute"
    width = 1500
    height = 820
    left = 90
    right = 280
    title_y = 38
    subtitle_y = 66
    top = 96
    bottom = 100
    plot_w = width - left - right
    plot_h = height - top - bottom

    temp_rows = filter_window(temp_rows)
    fan_rows = filter_window(fan_rows)
    if not temp_rows and not fan_rows:
        return render_empty_svg(title, subtitle)

    temp_values = [
        float(row[label])
        for row in temp_rows
        for label in TEMP_LABELS
        if not math.isnan(float(row[label]))
    ]
    fan_values = [
        float(row[label])
        for row in fan_rows
        for label in FAN_LABELS
        if not math.isnan(float(row[label]))
    ]
    if not temp_values and not fan_values:
        return render_empty_svg(title, subtitle)

    row_groups = [rows for rows in (temp_rows, fan_rows) if rows]
    x0 = min(rows[0]["timestamp_local"] for rows in row_groups)  # type: ignore[index,arg-type]
    x1 = max(rows[-1]["timestamp_local"] for rows in row_groups)  # type: ignore[index,arg-type]
    if x0 == x1:
        x1 = x0 + dt.timedelta(minutes=1)

    temp_axis_min, temp_axis_max, temp_tick_step = calc_axis(temp_values or [0.0], "F")
    fan_axis_min, fan_axis_max, fan_tick_step = calc_axis(fan_values or [0.0], "RPM")

    def x_pos(ts: dt.datetime) -> float:
        span = (x1 - x0).total_seconds()
        return left + (((ts - x0).total_seconds() / span) * plot_w)

    def temp_y_pos(value: float) -> float:
        return top + ((temp_axis_max - value) / (temp_axis_max - temp_axis_min) * plot_h)

    def fan_y_pos(value: float) -> float:
        return top + ((fan_axis_max - value) / (fan_axis_max - fan_axis_min) * plot_h)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        "<style>",
        'text{font-family:ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;}',
        ".title{font-size:30px;font-weight:700;fill:#0f172a;}",
        ".sub{font-size:15px;fill:#475569;}",
        ".axis{font-size:13px;fill:#475569;}",
        ".axis-temp{font-size:13px;fill:#334155;}",
        ".axis-fan{font-size:13px;fill:#0f766e;}",
        ".legend{font-size:14px;fill:#0f172a;}",
        ".legend-head{font-size:14px;font-weight:700;fill:#334155;}",
        ".grid{stroke:#dbe3ef;stroke-width:1;}",
        ".frame{stroke:#94a3b8;stroke-width:1.2;fill:none;}",
        "</style>",
        '<rect width="100%" height="100%" fill="#f8fafc"/>',
        f'<text class="title" x="{left}" y="{title_y}">{svg_escape(title)}</text>',
        f'<text class="sub" x="{left}" y="{subtitle_y}">{svg_escape(subtitle)}</text>',
        f'<text class="axis-temp" x="{left}" y="{top - 14}">Fahrenheit</text>',
        f'<text class="axis-fan" x="{left + plot_w}" y="{top - 14}" text-anchor="end">RPM</text>',
    ]

    tick_value = temp_axis_min
    while tick_value <= temp_axis_max + (temp_tick_step / 2):
        y = temp_y_pos(tick_value)
        parts.append(f'<line class="grid" x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}"/>')
        parts.append(
            f'<text class="axis-temp" x="{left - 12}" y="{y + 4:.1f}" text-anchor="end">{format_axis_value(tick_value, temp_tick_step)}</text>'
        )
        if fan_values:
            fan_tick_value = fan_axis_min + ((tick_value - temp_axis_min) / (temp_axis_max - temp_axis_min) * (fan_axis_max - fan_axis_min))
            parts.append(
                f'<text class="axis-fan" x="{left + plot_w + 12}" y="{y + 4:.1f}" text-anchor="start">{format_axis_value(fan_tick_value, fan_tick_step)}</text>'
            )
        tick_value += temp_tick_step

    total_hours = max(1, int(math.ceil((x1 - x0).total_seconds() / 3600)))
    tick_count = min(total_hours + 1, 7)
    for idx in range(tick_count):
        ratio = idx / (tick_count - 1) if tick_count > 1 else 0
        ts = x0 + (x1 - x0) * ratio
        x = x_pos(ts)
        parts.append(f'<line class="grid" x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top + plot_h}"/>')
        parts.append(
            f'<text class="axis" x="{x:.1f}" y="{top + plot_h + 26}" text-anchor="middle">{svg_escape(ts.strftime("%m-%d %H:%M"))}</text>'
        )

    parts.append(f'<rect class="frame" x="{left}" y="{top}" width="{plot_w}" height="{plot_h}"/>')

    legend_x = left + plot_w + 72
    legend_y = top + 20
    parts.append(f'<text class="legend-head" x="{legend_x}" y="{legend_y}">Temps (F)</text>')
    legend_y += 18

    for idx, label in enumerate(TEMP_LABELS):
        points: List[str] = []
        last_value = None
        last_x = 0.0
        last_y = 0.0
        for row in temp_rows:
            value = float(row[label])
            if math.isnan(value):
                continue
            x = x_pos(row["timestamp_local"])  # type: ignore[arg-type]
            y = temp_y_pos(value)
            points.append(f"{x:.1f},{y:.1f}")
            last_value = value
            last_x = x
            last_y = y
        if not points:
            continue
        color = COLORS[idx % len(COLORS)]
        dash = TEMP_LINE_STYLES.get(label)
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        parts.append(
            f'<polyline fill="none" stroke="{color}" stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round"{dash_attr} points="{" ".join(points)}"/>'
        )
        if last_value is not None:
            parts.append(f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="3.4" fill="{color}"/>')
            parts.append(
                f'<line x1="{legend_x}" y1="{legend_y - 5}" x2="{legend_x + 22}" y2="{legend_y - 5}" stroke="{color}" stroke-width="4"{dash_attr}/>'
            )
            parts.append(
                f'<text class="legend" x="{legend_x + 30}" y="{legend_y}">{svg_escape(label)}: {format_metric_value(last_value, "F")}</text>'
            )
            legend_y += 24

    legend_y += 10
    parts.append(f'<text class="legend-head" x="{legend_x}" y="{legend_y}">Fans (RPM)</text>')
    legend_y += 18

    for idx, label in enumerate(FAN_LABELS):
        points = []
        last_value = None
        last_x = 0.0
        last_y = 0.0
        for row in fan_rows:
            value = float(row[label])
            if math.isnan(value):
                continue
            x = x_pos(row["timestamp_local"])  # type: ignore[arg-type]
            y = fan_y_pos(value)
            points.append(f"{x:.1f},{y:.1f}")
            last_value = value
            last_x = x
            last_y = y
        if not points:
            continue
        color = FAN_COLORS[idx % len(FAN_COLORS)]
        parts.append(
            f'<polyline fill="none" stroke="{color}" stroke-width="2.8" stroke-dasharray="7 5" stroke-linejoin="round" stroke-linecap="round" points="{" ".join(points)}"/>'
        )
        if last_value is not None:
            parts.append(f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="3.8" fill="{color}"/>')
            parts.append(
                f'<line x1="{legend_x}" y1="{legend_y - 5}" x2="{legend_x + 22}" y2="{legend_y - 5}" stroke="{color}" stroke-width="4" stroke-dasharray="7 5"/>'
            )
            parts.append(
                f'<text class="legend" x="{legend_x + 30}" y="{legend_y}">{svg_escape(label)}: {format_metric_value(last_value, "RPM")}</text>'
            )
            legend_y += 24

    latest_text = latest_timestamp_text([temp_rows, fan_rows])
    parts.append(
        f'<text class="sub" x="{left}" y="{height - 24}">Latest sample: {svg_escape(latest_text)}</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


def latest_table_rows(
    latest: Dict[str, object] | None,
    labels: List[str],
    unit: str,
    palette: List[str] | None = None,
) -> str:
    if latest is None:
        return (
            '<tr><td colspan="2" style="padding:10px 12px;color:#64748b;">No data yet</td></tr>'
        )

    colors = palette or COLORS
    rows = []
    for idx, label in enumerate(labels):
        color = colors[idx % len(colors)]
        value = float(latest[label])
        value_text = "n/a" if math.isnan(value) else format_metric_value(value, unit)
        rows.append(
            "<tr>"
            f'<td style="padding:8px 12px;border-bottom:1px solid #e2e8f0;"><span style="display:inline-block;width:12px;height:12px;background:{color};border-radius:999px;margin-right:8px;vertical-align:middle;"></span>{html.escape(label)}</td>'
            f'<td style="padding:8px 12px;border-bottom:1px solid #e2e8f0;text-align:right;">{html.escape(value_text)}</td>'
            "</tr>"
        )
    return "".join(rows)


def latest_timestamp_text(collections: List[List[Dict[str, object]]]) -> str:
    timestamps = [rows[-1]["timestamp_local"] for rows in collections if rows]
    if not timestamps:
        return "No data yet"
    latest = max(timestamps)  # type: ignore[arg-type]
    return latest.strftime("%Y-%m-%d %H:%M:%S %Z")


def render_html(temp_rows: List[Dict[str, object]], fan_rows: List[Dict[str, object]], status: str) -> str:
    temp_latest = temp_rows[-1] if temp_rows else None
    fan_latest = fan_rows[-1] if fan_rows else None
    updated = latest_timestamp_text([temp_rows, fan_rows])
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="60">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>NAS Thermal Dashboard</title>
  <style>
    :root {{
      --bg: #eef4fb;
      --panel: rgba(255,255,255,0.92);
      --line: #d7e2ef;
      --text: #0f172a;
      --muted: #475569;
      --accent: #0f766e;
    }}
    html {{
      height: 100%;
    }}
    body {{
      margin: 0;
      min-height: 100vh;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at top left, rgba(14,116,144,0.12), transparent 35%),
        linear-gradient(180deg, #f8fbff 0%, var(--bg) 100%);
      color: var(--text);
    }}
    .wrap {{
      min-height: 100vh;
      width: 100%;
      box-sizing: border-box;
      padding: clamp(12px, 2vw, 24px);
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      gap: clamp(12px, 1.6vw, 20px);
    }}
    .hero {{
      display: flex;
      justify-content: space-between;
      align-items: end;
      gap: clamp(12px, 2vw, 24px);
    }}
    h1 {{
      margin: 0;
      font-size: clamp(30px, 3.2vw, 44px);
      line-height: 1.05;
    }}
    .meta {{
      color: var(--muted);
      font-size: clamp(14px, 1vw, 16px);
    }}
    .status {{
      display: inline-block;
      margin-top: 10px;
      padding: 8px 12px;
      border-radius: 999px;
      background: #ecfeff;
      color: var(--accent);
      font-weight: 600;
      font-size: clamp(13px, 0.95vw, 15px);
      border: 1px solid #bae6fd;
    }}
    .section {{
      min-height: 0;
    }}
    .section-grid {{
      display: grid;
      grid-template-columns: minmax(300px, 360px) minmax(0, 1fr);
      gap: clamp(12px, 1.6vw, 20px);
      min-height: 0;
      height: 100%;
      align-items: stretch;
    }}
    .stack {{
      display: grid;
      gap: clamp(12px, 1.6vw, 20px);
      min-height: 0;
      grid-template-rows: minmax(0, 1fr) minmax(0, 1fr);
    }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 18px;
      box-shadow: 0 10px 30px rgba(15,23,42,0.05);
      overflow: hidden;
      display: flex;
      flex-direction: column;
      min-height: 0;
    }}
    .card h3 {{
      margin: 0;
      padding: clamp(14px, 1.1vw, 18px) clamp(16px, 1.2vw, 20px) 10px;
      font-size: clamp(17px, 1.2vw, 20px);
    }}
    .card .body {{
      padding: 0 clamp(16px, 1.2vw, 20px) clamp(16px, 1.2vw, 20px);
      overflow: auto;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: clamp(14px, 1vw, 16px);
    }}
    .graph {{
      padding: clamp(10px, 1vw, 14px);
      background: #fff;
      min-height: 0;
    }}
    img {{
      width: 100%;
      height: 100%;
      display: block;
      border-radius: 14px;
      border: 1px solid var(--line);
      background: #f8fafc;
      object-fit: contain;
    }}
    a {{
      color: #0f4c81;
      text-decoration: none;
      font-weight: 600;
    }}
    @media (max-width: 980px) {{
      .wrap {{
        min-height: auto;
        grid-template-rows: auto auto;
      }}
      .hero,
      .section-grid {{
        grid-template-columns: 1fr;
        display: grid;
      }}
      .stack {{
        grid-template-rows: auto auto;
      }}
      .graph img {{
        height: auto;
      }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="hero">
      <div>
        <h1>NAS Thermal Dashboard</h1>
        <div class="meta">HDD, CPU, and system temperatures in Fahrenheit with system fan speed in RPM</div>
        <div class="status">{html.escape(status)}</div>
      </div>
      <div class="meta">
        Last update<br>
        <strong>{html.escape(updated)}</strong><br>
        Refreshes every {INTERVAL_SECONDS} seconds
      </div>
    </div>

    <div class="section">
      <div class="section-grid">
        <div class="stack">
          <div class="card">
            <h3>Latest Temps (F)</h3>
            <div class="body">
              <table>{latest_table_rows(temp_latest, TEMP_LABELS, "F", COLORS)}</table>
            </div>
          </div>
          <div class="card">
            <h3>Latest Fan Speed (RPM)</h3>
            <div class="body">
              <table>{latest_table_rows(fan_latest, FAN_LABELS, "RPM", FAN_COLORS)}</table>
              <p class="meta" style="margin:16px 0 0;">Data: <a href="temps_f.csv">temps_f.csv</a> and <a href="fans_rpm.csv">fans_rpm.csv</a></p>
            </div>
          </div>
        </div>
        <div class="card graph">
          <img src="thermal_dashboard_live.svg" alt="Combined HDD temperature and fan speed chart">
        </div>
      </div>
    </div>
  </div>
</body>
</html>
"""


def write_outputs(status: str) -> None:
    temp_rows = read_samples(TEMP_CSV_PATH, TEMP_LABELS)
    fan_rows = read_samples(FAN_CSV_PATH, FAN_LABELS)
    COMBINED_SVG_PATH.write_text(render_combined_svg(temp_rows, fan_rows), encoding="utf-8")
    HTML_PATH.write_text(render_html(temp_rows, fan_rows, status), encoding="utf-8")


def main() -> None:
    ensure_csv(TEMP_CSV_PATH, TEMP_LABELS)
    ensure_csv(FAN_CSV_PATH, FAN_LABELS)
    write_outputs("Starting up")
    startup_timestamp = dt.datetime.now().astimezone()
    try:
        maybe_send_startup_test_email(startup_timestamp)
    except Exception as exc:  # noqa: BLE001
        print(f"Startup test email failed: {exc}", flush=True)

    server_thread = threading.Thread(target=start_http_server, daemon=True)
    server_thread.start()

    while True:
        timestamp_local = dt.datetime.now().astimezone()
        timestamp_utc = dt.datetime.now(dt.timezone.utc)
        ok_parts: List[str] = []
        error_parts: List[str] = []

        try:
            temps_f = fetch_temps_f()
            append_sample(TEMP_CSV_PATH, TEMP_LABELS, timestamp_local, timestamp_utc, temps_f)
            ok_parts.append("temps OK")
        except Exception as exc:  # noqa: BLE001
            error_parts.append(f"temps failed: {exc}")
            temps_f = None

        if temps_f is not None:
            try:
                check_temp_alerts(timestamp_local, temps_f)
            except Exception as exc:  # noqa: BLE001
                error_parts.append(f"alerts failed: {exc}")

        try:
            fans_rpm = fetch_fans_rpm()
            append_sample(FAN_CSV_PATH, FAN_LABELS, timestamp_local, timestamp_utc, fans_rpm)
            ok_parts.append("fans OK")
        except Exception as exc:  # noqa: BLE001
            error_parts.append(f"fans failed: {exc}")

        if error_parts and ok_parts:
            status = (
                f'Partial check at {timestamp_local.strftime("%Y-%m-%d %H:%M:%S %Z")}: '
                f'{"; ".join(ok_parts)}; {"; ".join(error_parts)}'
            )
            print(status, flush=True)
        elif error_parts:
            status = f'Last check failed at {timestamp_local.strftime("%Y-%m-%d %H:%M:%S %Z")}: {"; ".join(error_parts)}'
            print(status, flush=True)
        else:
            status = f'Last check OK at {timestamp_local.strftime("%Y-%m-%d %H:%M:%S %Z")}'

        write_outputs(status)
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
