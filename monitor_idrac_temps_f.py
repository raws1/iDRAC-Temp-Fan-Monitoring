#!/usr/bin/env python3
"""
Capture iDRAC temperatures in Fahrenheit plus all fan RPM readings, and redraw live SVG graphs.

Usage:
  python3 monitor_idrac_temps_f.py \
    --host IDRAC_HOST --user IDRAC_USER --password 'IDRAC_PASSWORD' \
    --encryption-key "$IDRAC_ENCRYPTION_KEY" \
    --interval-seconds 60 --duration-seconds 0 \
    --out-dir /path/to/output
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import re
import smtplib
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from html import escape
from pathlib import Path
from typing import Dict, List, Tuple
from zoneinfo import ZoneInfo


TEMP_SENSOR_RE = re.compile(r"^\s*([^|]+)\s*\|.*\|\s*(-?\d+)\s+degrees C\s*$")
FAN_SENSOR_RE = re.compile(r"^\s*([^|]+)\s*\|.*\|\s*(\d+)\s+RPM\s*$")
PREFERRED_INLET_SENSORS = ("Inlet Temp", "System Board Inlet Temp")
PREFERRED_CPU1_SENSORS = ("CPU1 Temp", "Temp")
DISPLAY_TZ = ZoneInfo(os.environ.get("TZ", "America/New_York"))
LONG_RANGE_REFRESH_SECONDS = 12 * 60 * 60
SUMMARY_WINDOW_SECONDS = 24 * 60 * 60
GRAPH_WINDOWS = (
    ("daily", "Daily", SUMMARY_WINDOW_SECONDS, 0),
    ("weekly", "Weekly", 7 * 24 * 60 * 60, LONG_RANGE_REFRESH_SECONDS),
    ("monthly", "Monthly", 30 * 24 * 60 * 60, LONG_RANGE_REFRESH_SECONDS),
    ("yearly", "Yearly", 365 * 24 * 60 * 60, LONG_RANGE_REFRESH_SECONDS),
)


@dataclass
class TempSample:
    ts_epoch: float
    ts_text: str
    inlet_f: float
    cpu1_f: float


@dataclass
class FanSample:
    ts_epoch: float
    ts_text: str
    fan_rpm: Dict[str, int]


@dataclass
class EmailAlertConfig:
    enabled: bool
    threshold_f: float
    test_on_start: bool
    smtp_host: str
    smtp_port: int
    smtp_username: str | None
    smtp_password: str | None
    smtp_starttls: bool
    smtp_ssl: bool
    email_from: str
    email_to: str
    subject_prefix: str


def c_to_f(c: int) -> float:
    return (c * 9.0 / 5.0) + 32.0


def display_dt_from_epoch(ts_epoch: float) -> datetime:
    return datetime.fromtimestamp(ts_epoch, tz=DISPLAY_TZ)


def display_now() -> datetime:
    return datetime.now(tz=DISPLAY_TZ)


def format_display_timestamp(ts_epoch: float) -> str:
    return display_dt_from_epoch(ts_epoch).strftime("%Y-%m-%d %I:%M:%S %p %Z")


def format_axis_timestamp(ts_epoch: float) -> str:
    return display_dt_from_epoch(ts_epoch).strftime("%I:%M %p")


def format_temp_f(value_f: float) -> str:
    return f"{value_f:.1f} F"


def format_rpm(value: float) -> str:
    return f"{value:.0f} RPM"


def parse_bool(raw: str | None, default: bool) -> bool:
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    return default


def load_email_alert_config() -> EmailAlertConfig:
    enabled = parse_bool(os.environ.get("ALERT_EMAIL_ENABLED"), default=False)
    threshold_f = float(os.environ.get("ALERT_TEMP_THRESHOLD_F", "120"))
    test_on_start = parse_bool(os.environ.get("ALERT_EMAIL_TEST_ON_START"), default=True)
    smtp_host = os.environ.get("ALERT_SMTP_HOST", "").strip()
    smtp_port = int(os.environ.get("ALERT_SMTP_PORT", "587"))
    smtp_username = os.environ.get("ALERT_SMTP_USERNAME", "").strip() or None
    smtp_password = os.environ.get("ALERT_SMTP_PASSWORD", "") or None
    smtp_ssl = parse_bool(os.environ.get("ALERT_SMTP_SSL"), default=False)
    smtp_starttls = parse_bool(os.environ.get("ALERT_SMTP_STARTTLS"), default=(not smtp_ssl))
    email_to = os.environ.get("ALERT_EMAIL_TO", "").strip()
    email_from = os.environ.get("ALERT_EMAIL_FROM", email_to).strip()
    subject_prefix = os.environ.get("ALERT_EMAIL_SUBJECT_PREFIX", "delltemps").strip() or "delltemps"

    if enabled:
        missing: List[str] = []
        if not smtp_host:
            missing.append("ALERT_SMTP_HOST")
        if not email_to:
            missing.append("ALERT_EMAIL_TO")
        if not email_from:
            missing.append("ALERT_EMAIL_FROM (or ALERT_EMAIL_TO)")
        if missing:
            print(
                "Email alerts disabled due to missing required settings: "
                + ", ".join(missing),
                file=sys.stderr,
            )
            enabled = False

    return EmailAlertConfig(
        enabled=enabled,
        threshold_f=threshold_f,
        test_on_start=test_on_start,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_username=smtp_username,
        smtp_password=smtp_password,
        smtp_starttls=smtp_starttls,
        smtp_ssl=smtp_ssl,
        email_from=email_from,
        email_to=email_to,
        subject_prefix=subject_prefix,
    )


def send_email_alert(config: EmailAlertConfig, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"] = config.email_from
    msg["To"] = config.email_to
    msg["Subject"] = subject
    msg.set_content(body)

    if config.smtp_ssl:
        with smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, timeout=20) as smtp:
            if config.smtp_username:
                smtp.login(config.smtp_username, config.smtp_password or "")
            smtp.send_message(msg)
        return

    with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=20) as smtp:
        smtp.ehlo()
        if config.smtp_starttls:
            smtp.starttls()
            smtp.ehlo()
        if config.smtp_username:
            smtp.login(config.smtp_username, config.smtp_password or "")
        smtp.send_message(msg)


def natural_key(name: str) -> List[object]:
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", name)]


def estimate_monospace_text_width_px(text: str, font_size: int = 12) -> int:
    return math.ceil(len(text) * font_size * 0.62)


def chart_left_padding(y_labels: List[str], minimum: int = 70) -> int:
    if not y_labels:
        return minimum
    return max(minimum, max(estimate_monospace_text_width_px(label) for label in y_labels) + 18)


def x_tick_anchor(idx: int, total_ticks: int) -> str:
    if total_ticks <= 1:
        return "middle"
    if idx == 0:
        return "start"
    if idx == total_ticks - 1:
        return "end"
    return "middle"


def run_ipmitool(host: str, user: str, password: str, encryption_key: str, sensor_type: str) -> str:
    cmd = [
        "ipmitool",
        "-I",
        "lanplus",
        "-H",
        host,
        "-U",
        user,
        "-P",
        password,
    ]
    if encryption_key:
        cmd.extend(["-y", encryption_key])
    cmd.extend(
        [
            "sdr",
            "type",
            sensor_type,
        ]
    )
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "ipmitool returned non-zero status")
    return proc.stdout


def get_temp_sensor_map(host: str, user: str, password: str, encryption_key: str) -> Dict[str, int]:
    out = run_ipmitool(host, user, password, encryption_key, sensor_type="temperature")
    temp_map: Dict[str, int] = {}
    for line in out.splitlines():
        m = TEMP_SENSOR_RE.match(line)
        if not m:
            continue
        temp_map[m.group(1).strip()] = int(m.group(2))
    return temp_map


def get_fan_sensor_map(host: str, user: str, password: str, encryption_key: str) -> Dict[str, int]:
    out = run_ipmitool(host, user, password, encryption_key, sensor_type="fan")
    fan_map: Dict[str, int] = {}
    for line in out.splitlines():
        m = FAN_SENSOR_RE.match(line)
        if not m:
            continue
        fan_map[m.group(1).strip()] = int(m.group(2))
    return fan_map


def select_sensor_name(sensor_map: Dict[str, int], preferred_names: Tuple[str, ...], role: str) -> str:
    lowered = {name.lower(): name for name in sensor_map.keys()}
    for preferred in preferred_names:
        found = lowered.get(preferred.lower())
        if found is not None:
            return found
    raise RuntimeError(f"Could not locate {role} sensor. Available sensors: {', '.join(sorted(sensor_map.keys()))}")


def discover_initial_sensor_config(
    host: str,
    user: str,
    password: str,
    encryption_key: str,
    status_path: Path,
) -> Tuple[str, str, List[str]]:
    retry_seconds = max(1, int(os.environ.get("INITIAL_SENSOR_DISCOVERY_RETRY_SECONDS", "10")))

    while True:
        try:
            initial_temp_map = get_temp_sensor_map(host, user, password, encryption_key)
            inlet_sensor_name = select_sensor_name(initial_temp_map, PREFERRED_INLET_SENSORS, role="inlet")
            cpu1_sensor_name = select_sensor_name(initial_temp_map, PREFERRED_CPU1_SENSORS, role="cpu1")

            initial_fan_map = get_fan_sensor_map(host, user, password, encryption_key)
            fan_sensor_names = sorted(initial_fan_map.keys(), key=natural_key)
            if not fan_sensor_names:
                raise RuntimeError("No fan sensors found via IPMI")

            return inlet_sensor_name, cpu1_sensor_name, fan_sensor_names
        except Exception as exc:
            ts_text = display_now().strftime("%Y-%m-%d %I:%M:%S %p %Z")
            message = (
                f"{ts_text} waiting for stable sensor discovery on {host}: {exc} "
                f"(retrying in {retry_seconds}s)"
            )
            status_path.write_text(message + "\n", encoding="utf-8")
            print(message, file=sys.stderr)
            time.sleep(retry_seconds)


def get_temp_values_c(
    host: str,
    user: str,
    password: str,
    encryption_key: str,
    inlet_sensor_name: str,
    cpu1_sensor_name: str,
) -> Tuple[int, int]:
    sensor_map = get_temp_sensor_map(host, user, password, encryption_key)
    inlet_c = sensor_map.get(inlet_sensor_name)
    cpu1_c = sensor_map.get(cpu1_sensor_name)
    if inlet_c is None or cpu1_c is None:
        raise RuntimeError(
            "Could not parse required temperature sensors. "
            f"needed_inlet={inlet_sensor_name}, needed_cpu1={cpu1_sensor_name}, "
            f"available={', '.join(sorted(sensor_map.keys()))}"
        )
    return inlet_c, cpu1_c


def get_fan_values_rpm(
    host: str,
    user: str,
    password: str,
    encryption_key: str,
    fan_sensor_names: List[str],
) -> Dict[str, int]:
    sensor_map = get_fan_sensor_map(host, user, password, encryption_key)
    values: Dict[str, int] = {}
    missing: List[str] = []
    for name in fan_sensor_names:
        val = sensor_map.get(name)
        if val is None:
            missing.append(name)
            continue
        values[name] = val
    if missing:
        raise RuntimeError(
            "Missing expected fan sensors from reading: "
            f"{', '.join(missing)}; available={', '.join(sorted(sensor_map.keys()))}"
        )
    return values


def read_temp_samples(csv_path: Path) -> List[TempSample]:
    if not csv_path.exists():
        return []
    rows: List[TempSample] = []
    with csv_path.open("r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(
                TempSample(
                    ts_epoch=float(row["ts_epoch"]),
                    ts_text=row["timestamp"],
                    inlet_f=float(row["inlet_f"]),
                    cpu1_f=float(row["cpu_f"]),
                )
            )
    return rows


def append_temp_sample(csv_path: Path, sample: TempSample) -> None:
    need_header = not csv_path.exists()
    with csv_path.open("a", newline="") as f:
        writer = csv.writer(f)
        if need_header:
            writer.writerow(["timestamp", "ts_epoch", "inlet_f", "cpu_f"])
        writer.writerow(
            [
                sample.ts_text,
                f"{sample.ts_epoch:.3f}",
                f"{sample.inlet_f:.2f}",
                f"{sample.cpu1_f:.2f}",
            ]
        )


def ensure_fan_csv_header(csv_path: Path, fan_sensor_names: List[str]) -> None:
    expected_header = ["timestamp", "ts_epoch"] + fan_sensor_names
    if not csv_path.exists():
        return
    with csv_path.open("r", newline="") as f:
        reader = csv.reader(f)
        current_header = next(reader, [])
    if current_header == expected_header:
        return

    ts = display_now().strftime("%Y%m%d-%I%M%S%p-%Z")
    backup_path = csv_path.with_name(f"{csv_path.stem}.backup-{ts}{csv_path.suffix}")
    csv_path.rename(backup_path)
    print(f"Fan CSV header changed. Backed up previous file to {backup_path}")


def append_fan_sample(csv_path: Path, sample: FanSample, fan_sensor_names: List[str]) -> None:
    ensure_fan_csv_header(csv_path, fan_sensor_names)
    need_header = not csv_path.exists()
    with csv_path.open("a", newline="") as f:
        writer = csv.writer(f)
        if need_header:
            writer.writerow(["timestamp", "ts_epoch"] + fan_sensor_names)
        writer.writerow(
            [sample.ts_text, f"{sample.ts_epoch:.3f}"] + [str(sample.fan_rpm[name]) for name in fan_sensor_names]
        )


def read_fan_samples(csv_path: Path, fan_sensor_names: List[str]) -> List[FanSample]:
    if not csv_path.exists():
        return []

    samples: List[FanSample] = []
    with csv_path.open("r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            fan_values: Dict[str, int] = {}
            ok = True
            for name in fan_sensor_names:
                val = row.get(name)
                if val is None or val == "":
                    ok = False
                    break
                fan_values[name] = int(float(val))
            if not ok:
                continue
            samples.append(
                FanSample(
                    ts_epoch=float(row["ts_epoch"]),
                    ts_text=row["timestamp"],
                    fan_rpm=fan_values,
                )
            )
    return samples


def select_time_window_temp(samples: List[TempSample], window_seconds: int) -> List[TempSample]:
    if not samples:
        return []
    cutoff = samples[-1].ts_epoch - window_seconds
    return [sample for sample in samples if sample.ts_epoch >= cutoff]


def select_time_window_fan(samples: List[FanSample], window_seconds: int) -> List[FanSample]:
    if not samples:
        return []
    cutoff = samples[-1].ts_epoch - window_seconds
    return [sample for sample in samples if sample.ts_epoch >= cutoff]


def should_refresh_svg(svg_path: Path, refresh_seconds: int, now_epoch: float) -> bool:
    if refresh_seconds <= 0:
        return True
    if not svg_path.exists():
        return True
    return (now_epoch - svg_path.stat().st_mtime) >= refresh_seconds


def format_axis_timestamp_for_span(ts_epoch: float, span_seconds: float) -> str:
    if span_seconds <= 2 * 24 * 60 * 60:
        return display_dt_from_epoch(ts_epoch).strftime("%I:%M %p")
    if span_seconds <= 14 * 24 * 60 * 60:
        return display_dt_from_epoch(ts_epoch).strftime("%b %d %I:%M %p")
    if span_seconds <= 45 * 24 * 60 * 60:
        return display_dt_from_epoch(ts_epoch).strftime("%b %d")
    return display_dt_from_epoch(ts_epoch).strftime("%b %Y")


def _map(v: float, in_min: float, in_max: float, out_min: float, out_max: float) -> float:
    if in_max == in_min:
        return (out_min + out_max) / 2.0
    return out_min + (v - in_min) * (out_max - out_min) / (in_max - in_min)


def _write_no_data_svg(svg_path: Path, message: str) -> None:
    content = (
        "<svg xmlns='http://www.w3.org/2000/svg' width='1100' height='560'>"
        "<rect x='0' y='0' width='100%' height='100%' fill='#0d1117'/>"
        f"<text x='50%' y='50%' text-anchor='middle' fill='#e6edf3' font-size='24'>{escape(message)}</text>"
        "</svg>"
    )
    svg_path.write_text(content, encoding="utf-8")


def write_text_atomic(path: Path, content: str) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(path)


def build_temp_summary_panel_html(samples: List[TempSample]) -> str:
    window_samples = select_time_window_temp(samples, SUMMARY_WINDOW_SECONDS)
    if not window_samples:
        return (
            "<h2>24-Hour Temperature Summary</h2>"
            "<p>Waiting for temperature samples to build the rolling 24-hour summary.</p>"
        )

    inlet_values = [sample.inlet_f for sample in window_samples]
    cpu_values = [sample.cpu1_f for sample in window_samples]
    latest_stamp = display_dt_from_epoch(window_samples[-1].ts_epoch).strftime("%Y-%m-%d %I:%M:%S %p %Z")

    def sensor_block(title: str, values: List[float]) -> str:
        avg_value = sum(values) / len(values)
        low_value = min(values)
        high_value = max(values)
        return (
            "<section class='sensor-summary'>"
            f"<h3>{escape(title)}</h3>"
            "<div class='summary-grid'>"
            "<div class='metric'>"
            "<span class='metric-label'>24h Average</span>"
            f"<span class='metric-value'>{escape(format_temp_f(avg_value))}</span>"
            "</div>"
            "<div class='metric'>"
            "<span class='metric-label'>Lowest</span>"
            f"<span class='metric-value'>{escape(format_temp_f(low_value))}</span>"
            "</div>"
            "<div class='metric'>"
            "<span class='metric-label'>Highest</span>"
            f"<span class='metric-value'>{escape(format_temp_f(high_value))}</span>"
            "</div>"
            "</div>"
            "</section>"
        )

    return (
        "<h2>24-Hour Temperature Summary</h2>"
        "<p>Rolling summary from the last 24 hours of captured temperature data.</p>"
        f"<p class='summary-note'>Updated through {escape(latest_stamp)} from {len(window_samples)} samples.</p>"
        "<div class='summary-sensors'>"
        f"{sensor_block('Inlet Temp', inlet_values)}"
        f"{sensor_block('CPU1 Temp', cpu_values)}"
        "</div>"
    )


def write_temp_summary_panel(panel_path: Path, samples: List[TempSample]) -> None:
    write_text_atomic(panel_path, build_temp_summary_panel_html(samples) + "\n")


def build_fan_summary_panel_html(samples: List[FanSample], fan_sensor_names: List[str]) -> str:
    window_samples = select_time_window_fan(samples, SUMMARY_WINDOW_SECONDS)
    if not window_samples or not fan_sensor_names:
        return (
            "<h2>24-Hour Fan Summary</h2>"
            "<p>Waiting for fan samples to build the rolling 24-hour summary.</p>"
        )

    latest_stamp = display_dt_from_epoch(window_samples[-1].ts_epoch).strftime("%Y-%m-%d %I:%M:%S %p %Z")

    def sensor_block(title: str) -> str:
        values = [sample.fan_rpm[title] for sample in window_samples if title in sample.fan_rpm]
        if not values:
            return ""

        avg_value = sum(values) / len(values)
        low_value = min(values)
        high_value = max(values)
        return (
            "<section class='sensor-summary'>"
            f"<h3>{escape(title)}</h3>"
            "<div class='summary-grid'>"
            "<div class='metric'>"
            "<span class='metric-label'>24h Average</span>"
            f"<span class='metric-value'>{escape(format_rpm(avg_value))}</span>"
            "</div>"
            "<div class='metric'>"
            "<span class='metric-label'>Lowest</span>"
            f"<span class='metric-value'>{escape(format_rpm(low_value))}</span>"
            "</div>"
            "<div class='metric'>"
            "<span class='metric-label'>Highest</span>"
            f"<span class='metric-value'>{escape(format_rpm(high_value))}</span>"
            "</div>"
            "</div>"
            "</section>"
        )

    sensor_blocks = "".join(sensor_block(name) for name in fan_sensor_names)
    return (
        "<h2>24-Hour Fan Summary</h2>"
        "<p>Rolling summary from the last 24 hours of captured fan RPM data.</p>"
        f"<p class='summary-note'>Updated through {escape(latest_stamp)} from {len(window_samples)} samples.</p>"
        "<div class='summary-sensors'>"
        f"{sensor_blocks}"
        "</div>"
    )


def write_fan_summary_panel(panel_path: Path, samples: List[FanSample], fan_sensor_names: List[str]) -> None:
    write_text_atomic(panel_path, build_fan_summary_panel_html(samples, fan_sensor_names) + "\n")


def render_temp_svg(samples: List[TempSample], svg_path: Path, title: str, span_seconds: float) -> None:
    width = 1100
    height = 560
    right = 20
    top = 50
    bottom = 90

    if not samples:
        _write_no_data_svg(svg_path, "No temperature data yet")
        return

    xs = [s.ts_epoch for s in samples]
    inlet_vals = [s.inlet_f for s in samples]
    cpu_vals = [s.cpu1_f for s in samples]
    ys = inlet_vals + cpu_vals

    x_min = min(xs)
    x_max = max(xs)
    if x_min == x_max:
        x_max = x_min + 1.0

    y_min_raw = min(ys)
    y_max_raw = max(ys)
    y_min = math.floor((y_min_raw - 2.0) / 5.0) * 5.0
    y_max = math.ceil((y_max_raw + 2.0) / 5.0) * 5.0
    if y_min == y_max:
        y_min -= 5
        y_max += 5

    y_grid_vals = [y_min + i * (y_max - y_min) / 8.0 for i in range(9)]
    y_labels = [f"{yv:.1f}F" for yv in y_grid_vals]
    left = chart_left_padding(y_labels, minimum=70)
    plot_w = width - left - right
    plot_h = height - top - bottom

    def x_px(v: float) -> float:
        return _map(v, x_min, x_max, left, left + plot_w)

    def y_px(v: float) -> float:
        return _map(v, y_min, y_max, top + plot_h, top)

    inlet_pts = " ".join(f"{x_px(s.ts_epoch):.2f},{y_px(s.inlet_f):.2f}" for s in samples)
    cpu_pts = " ".join(f"{x_px(s.ts_epoch):.2f},{y_px(s.cpu1_f):.2f}" for s in samples)

    x_tick_count = min(8, len(samples))
    x_tick_indices = [
        round(i * (len(samples) - 1) / max(1, x_tick_count - 1)) for i in range(x_tick_count)
    ]

    now_text = display_now().strftime("%Y-%m-%d %I:%M:%S %p %Z")
    last = samples[-1]

    lines: List[str] = []
    lines.append(f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}'>")
    lines.append("<defs>")
    lines.append(
        "<linearGradient id='bg' x1='0' y1='0' x2='0' y2='1'>"
        "<stop offset='0%' stop-color='#0d1117'/>"
        "<stop offset='100%' stop-color='#161b22'/>"
        "</linearGradient>"
    )
    lines.append("</defs>")
    lines.append("<rect x='0' y='0' width='100%' height='100%' fill='url(#bg)'/>")

    lines.append(
        f"<text x='{left}' y='30' fill='#e6edf3' font-size='22' "
        f"font-family='Menlo, Monaco, monospace'>{escape(title)}</text>"
    )
    lines.append(
        f"<text x='{left}' y='48' fill='#8b949e' font-size='13' "
        f"font-family='Menlo, Monaco, monospace'>Updated: {escape(now_text)}  |  Samples: {len(samples)}</text>"
    )

    lines.append(
        f"<rect x='{left}' y='{top}' width='{plot_w}' height='{plot_h}' "
        "fill='none' stroke='#30363d' stroke-width='1'/>"
    )

    for yv in y_grid_vals:
        yp = y_px(yv)
        lines.append(
            f"<line x1='{left}' y1='{yp:.2f}' x2='{left + plot_w}' y2='{yp:.2f}' "
            "stroke='#21262d' stroke-width='1'/>"
        )
        lines.append(
            f"<text x='{left - 10}' y='{yp + 4:.2f}' text-anchor='end' fill='#8b949e' "
            "font-size='12' font-family='Menlo, Monaco, monospace'>"
            f"{yv:.1f}F</text>"
        )

    for tick_pos, idx in enumerate(x_tick_indices):
        s = samples[idx]
        xp = x_px(s.ts_epoch)
        lines.append(
            f"<line x1='{xp:.2f}' y1='{top + plot_h}' x2='{xp:.2f}' y2='{top + plot_h + 6}' "
            "stroke='#30363d' stroke-width='1'/>"
        )
        label = format_axis_timestamp_for_span(s.ts_epoch, span_seconds)
        anchor = x_tick_anchor(tick_pos, len(x_tick_indices))
        lines.append(
            f"<text x='{xp:.2f}' y='{top + plot_h + 24}' text-anchor='{anchor}' fill='#8b949e' "
            f"font-size='12' font-family='Menlo, Monaco, monospace'>{label}</text>"
        )

    lines.append(
        f"<polyline points='{inlet_pts}' fill='none' stroke='#58a6ff' "
        "stroke-width='3' stroke-linecap='round' stroke-linejoin='round'/>"
    )
    lines.append(
        f"<polyline points='{cpu_pts}' fill='none' stroke='#ff7b72' "
        "stroke-width='3' stroke-linecap='round' stroke-linejoin='round'/>"
    )

    lx = left + plot_w - 220
    ly = top + 18
    lines.append(
        f"<line x1='{lx}' y1='{ly}' x2='{lx + 32}' y2='{ly}' stroke='#58a6ff' stroke-width='3'/>"
        f"<text x='{lx + 40}' y='{ly + 4}' fill='#c9d1d9' font-size='13' font-family='Menlo, Monaco, monospace'>"
        "Inlet Temp (F)</text>"
    )
    lines.append(
        f"<line x1='{lx}' y1='{ly + 20}' x2='{lx + 32}' y2='{ly + 20}' stroke='#ff7b72' stroke-width='3'/>"
        f"<text x='{lx + 40}' y='{ly + 24}' fill='#c9d1d9' font-size='13' font-family='Menlo, Monaco, monospace'>"
        "CPU1 Temp (F)</text>"
    )

    last_x = x_px(last.ts_epoch)
    lines.append(f"<circle cx='{last_x:.2f}' cy='{y_px(last.inlet_f):.2f}' r='4' fill='#58a6ff'/>")
    lines.append(f"<circle cx='{last_x:.2f}' cy='{y_px(last.cpu1_f):.2f}' r='4' fill='#ff7b72'/>")
    lines.append(
        f"<text x='{left}' y='{height - 28}' fill='#e6edf3' font-size='13' font-family='Menlo, Monaco, monospace'>"
        f"Last sample ({escape(last.ts_text)}): Inlet={last.inlet_f:.1f}F, CPU1={last.cpu1_f:.1f}F</text>"
    )
    lines.append("</svg>")

    tmp = svg_path.with_suffix(".svg.tmp")
    tmp.write_text("".join(lines), encoding="utf-8")
    os.replace(tmp, svg_path)


def render_fan_svg(
    samples: List[FanSample],
    fan_sensor_names: List[str],
    svg_path: Path,
    title: str,
    span_seconds: float,
) -> None:
    width = 1100
    height = 620
    right = 20
    top = 50
    bottom = 105

    if not samples or not fan_sensor_names:
        _write_no_data_svg(svg_path, "No fan data yet")
        return

    xs = [s.ts_epoch for s in samples]
    all_rpm = [s.fan_rpm[name] for s in samples for name in fan_sensor_names]

    x_min = min(xs)
    x_max = max(xs)
    if x_min == x_max:
        x_max = x_min + 1.0

    y_min_raw = min(all_rpm)
    y_max_raw = max(all_rpm)
    y_min = max(0.0, math.floor((y_min_raw - 200.0) / 200.0) * 200.0)
    y_max = math.ceil((y_max_raw + 200.0) / 200.0) * 200.0
    if y_min == y_max:
        y_max += 200.0

    y_grid_vals = [y_min + i * (y_max - y_min) / 8.0 for i in range(9)]
    y_labels = [f"{yv:.0f} RPM" for yv in y_grid_vals]
    left = chart_left_padding(y_labels, minimum=70)
    plot_w = width - left - right
    plot_h = height - top - bottom

    def x_px(v: float) -> float:
        return _map(v, x_min, x_max, left, left + plot_w)

    def y_px(v: float) -> float:
        return _map(v, y_min, y_max, top + plot_h, top)

    palette = ["#58a6ff", "#ff7b72", "#3fb950", "#d2a8ff", "#f2cc60", "#79c0ff", "#ffa657", "#a5d6ff"]

    x_tick_count = min(8, len(samples))
    x_tick_indices = [
        round(i * (len(samples) - 1) / max(1, x_tick_count - 1)) for i in range(x_tick_count)
    ]

    now_text = display_now().strftime("%Y-%m-%d %I:%M:%S %p %Z")
    last = samples[-1]

    lines: List[str] = []
    lines.append(f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}'>")
    lines.append("<defs>")
    lines.append(
        "<linearGradient id='bg' x1='0' y1='0' x2='0' y2='1'>"
        "<stop offset='0%' stop-color='#0d1117'/>"
        "<stop offset='100%' stop-color='#161b22'/>"
        "</linearGradient>"
    )
    lines.append("</defs>")
    lines.append("<rect x='0' y='0' width='100%' height='100%' fill='url(#bg)'/>")

    lines.append(
        f"<text x='{left}' y='30' fill='#e6edf3' font-size='22' "
        f"font-family='Menlo, Monaco, monospace'>{escape(title)}</text>"
    )
    lines.append(
        f"<text x='{left}' y='48' fill='#8b949e' font-size='13' "
        f"font-family='Menlo, Monaco, monospace'>Updated: {escape(now_text)}  |  Samples: {len(samples)}</text>"
    )

    lines.append(
        f"<rect x='{left}' y='{top}' width='{plot_w}' height='{plot_h}' "
        "fill='none' stroke='#30363d' stroke-width='1'/>"
    )

    for yv in y_grid_vals:
        yp = y_px(yv)
        lines.append(
            f"<line x1='{left}' y1='{yp:.2f}' x2='{left + plot_w}' y2='{yp:.2f}' "
            "stroke='#21262d' stroke-width='1'/>"
        )
        lines.append(
            f"<text x='{left - 10}' y='{yp + 4:.2f}' text-anchor='end' fill='#8b949e' "
            "font-size='12' font-family='Menlo, Monaco, monospace'>"
            f"{yv:.0f} RPM</text>"
        )

    for tick_pos, idx in enumerate(x_tick_indices):
        s = samples[idx]
        xp = x_px(s.ts_epoch)
        lines.append(
            f"<line x1='{xp:.2f}' y1='{top + plot_h}' x2='{xp:.2f}' y2='{top + plot_h + 6}' "
            "stroke='#30363d' stroke-width='1'/>"
        )
        label = format_axis_timestamp_for_span(s.ts_epoch, span_seconds)
        anchor = x_tick_anchor(tick_pos, len(x_tick_indices))
        lines.append(
            f"<text x='{xp:.2f}' y='{top + plot_h + 24}' text-anchor='{anchor}' fill='#8b949e' "
            f"font-size='12' font-family='Menlo, Monaco, monospace'>{label}</text>"
        )

    legend_x = left + plot_w - 230
    legend_y = top + 18
    for idx, fan_name in enumerate(fan_sensor_names):
        color = palette[idx % len(palette)]
        points = " ".join(f"{x_px(s.ts_epoch):.2f},{y_px(s.fan_rpm[fan_name]):.2f}" for s in samples)
        lines.append(
            f"<polyline points='{points}' fill='none' stroke='{color}' "
            "stroke-width='3' stroke-linecap='round' stroke-linejoin='round'/>"
        )
        ly = legend_y + (idx * 18)
        lines.append(f"<line x1='{legend_x}' y1='{ly}' x2='{legend_x + 32}' y2='{ly}' stroke='{color}' stroke-width='3'/>")
        lines.append(
            f"<text x='{legend_x + 40}' y='{ly + 4}' fill='#c9d1d9' font-size='13' "
            f"font-family='Menlo, Monaco, monospace'>{escape(fan_name)} (RPM)</text>"
        )

    last_x = x_px(last.ts_epoch)
    last_parts: List[str] = []
    for idx, fan_name in enumerate(fan_sensor_names):
        color = palette[idx % len(palette)]
        rpm = last.fan_rpm[fan_name]
        lines.append(f"<circle cx='{last_x:.2f}' cy='{y_px(rpm):.2f}' r='4' fill='{color}'/>")
        last_parts.append(f"{fan_name}={rpm}")

    lines.append(
        f"<text x='{left}' y='{height - 30}' fill='#e6edf3' font-size='13' font-family='Menlo, Monaco, monospace'>"
        f"Last sample ({escape(last.ts_text)}): {escape(' | '.join(last_parts))}</text>"
    )
    lines.append("</svg>")

    tmp = svg_path.with_suffix(".svg.tmp")
    tmp.write_text("".join(lines), encoding="utf-8")
    os.replace(tmp, svg_path)


def render_windowed_svgs(
    temp_samples: List[TempSample],
    fan_samples: List[FanSample],
    fan_sensor_names: List[str],
    out_dir: Path,
    host: str,
    now_epoch: float,
) -> None:
    for slug, label, window_seconds, refresh_seconds in GRAPH_WINDOWS:
        temp_path = out_dir / f"temps_f_{slug}.svg"
        fan_path = out_dir / f"fans_rpm_{slug}.svg"

        if not (
            should_refresh_svg(temp_path, refresh_seconds, now_epoch)
            or should_refresh_svg(fan_path, refresh_seconds, now_epoch)
        ):
            continue

        windowed_temp_samples = select_time_window_temp(temp_samples, window_seconds)
        windowed_fan_samples = select_time_window_fan(fan_samples, window_seconds)

        render_temp_svg(
            windowed_temp_samples,
            temp_path,
            title=f"iDRAC Temperature Monitor ({host}) - {label}",
            span_seconds=window_seconds,
        )
        render_fan_svg(
            windowed_fan_samples,
            fan_sensor_names,
            fan_path,
            title=f"iDRAC Fan Speed Monitor ({host}) - {label}",
            span_seconds=window_seconds,
        )

        # Preserve the legacy "live" filenames by keeping them pointed at the daily view.
        if slug == "daily":
            (out_dir / "temps_f_live.svg").write_text(temp_path.read_text(encoding="utf-8"), encoding="utf-8")
            (out_dir / "fans_rpm_live.svg").write_text(fan_path.read_text(encoding="utf-8"), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor iDRAC temperatures/fans and render live SVG graphs.")
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--encryption-key", default=os.environ.get("IDRAC_ENCRYPTION_KEY", ""))
    parser.add_argument("--interval-seconds", type=int, default=300)
    parser.add_argument("--duration-seconds", type=int, default=10800)
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args()

    if args.interval_seconds <= 0:
        print("interval-seconds must be > 0", file=sys.stderr)
        return 2
    if args.duration_seconds < 0:
        print("duration-seconds must be >= 0", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    temp_csv_path = out_dir / "temps_f.csv"
    temp_svg_path = out_dir / "temps_f_live.svg"
    temp_summary_panel_path = out_dir / "temp_summary_panel.html"
    fan_csv_path = out_dir / "fans_rpm.csv"
    fan_svg_path = out_dir / "fans_rpm_live.svg"
    fan_summary_panel_path = out_dir / "fan_summary_panel.html"
    status_path = out_dir / "status.txt"

    run_forever = args.duration_seconds == 0
    total_samples = None if run_forever else ((args.duration_seconds // args.interval_seconds) + 1)
    start_epoch = time.time()
    duration_label = "indefinite" if run_forever else f"{args.duration_seconds}s"
    sample_count_label = "continuous" if total_samples is None else str(total_samples)

    print(f"Starting monitor: {sample_count_label} samples, every {args.interval_seconds}s, duration {duration_label}")
    print(f"Output directory: {out_dir}")
    print(f"Temp CSV: {temp_csv_path}")
    print(f"Temp Graph (daily/live): {temp_svg_path}")
    print(f"Temp Summary Panel: {temp_summary_panel_path}")
    print(f"Fan CSV: {fan_csv_path}")
    print(f"Fan Graph (daily/live): {fan_svg_path}")
    print(f"Fan Summary Panel: {fan_summary_panel_path}")

    write_temp_summary_panel(temp_summary_panel_path, [])
    write_fan_summary_panel(fan_summary_panel_path, [], [])

    inlet_sensor_name, cpu1_sensor_name, fan_sensor_names = discover_initial_sensor_config(
        args.host,
        args.user,
        args.password,
        args.encryption_key,
        status_path,
    )

    alert_config = load_email_alert_config()
    alert_active = False

    print(f"Using temperature sensors: inlet='{inlet_sensor_name}', cpu1='{cpu1_sensor_name}'")
    print(f"Using fan sensors: {', '.join(fan_sensor_names)}")
    if alert_config.enabled:
        print(
            "Email alerts enabled: "
            f"threshold>{alert_config.threshold_f:.1f}F, to={alert_config.email_to}, smtp={alert_config.smtp_host}:{alert_config.smtp_port}"
        )
        if alert_config.test_on_start:
            test_subject = f"{alert_config.subject_prefix} startup test on {args.host}"
            test_body = (
                "This is a startup test email from delltemps.\n\n"
                f"Time: {display_now().strftime('%Y-%m-%d %I:%M:%S %p %Z')}\n"
                f"iDRAC Host: {args.host}\n"
                f"Threshold: {alert_config.threshold_f:.1f}F\n"
                f"Check interval: {args.interval_seconds}s\n"
            )
            try:
                send_email_alert(alert_config, test_subject, test_body)
                print(f"Startup test email sent to {alert_config.email_to}")
            except Exception as exc:
                print(f"ERROR sending startup test email: {exc}", file=sys.stderr)
    else:
        print("Email alerts disabled")

    sample_idx = 0
    while True:
        now = time.time()
        ts_text = format_display_timestamp(now)
        sample_label = (
            f"sample {sample_idx + 1}"
            if total_samples is None
            else f"sample {sample_idx + 1}/{total_samples}"
        )
        try:
            inlet_c, cpu1_c = get_temp_values_c(
                args.host,
                args.user,
                args.password,
                args.encryption_key,
                inlet_sensor_name=inlet_sensor_name,
                cpu1_sensor_name=cpu1_sensor_name,
            )
            fan_values = get_fan_values_rpm(
                args.host,
                args.user,
                args.password,
                args.encryption_key,
                fan_sensor_names=fan_sensor_names,
            )

            temp_sample = TempSample(
                ts_epoch=now,
                ts_text=ts_text,
                inlet_f=c_to_f(inlet_c),
                cpu1_f=c_to_f(cpu1_c),
            )
            fan_sample = FanSample(ts_epoch=now, ts_text=ts_text, fan_rpm=fan_values)

            append_temp_sample(temp_csv_path, temp_sample)
            append_fan_sample(fan_csv_path, fan_sample, fan_sensor_names)

            temp_samples = read_temp_samples(temp_csv_path)
            fan_samples = read_fan_samples(fan_csv_path, fan_sensor_names)

            render_windowed_svgs(
                temp_samples,
                fan_samples,
                fan_sensor_names,
                out_dir,
                args.host,
                now,
            )
            write_temp_summary_panel(temp_summary_panel_path, temp_samples)
            write_fan_summary_panel(fan_summary_panel_path, fan_samples, fan_sensor_names)

            fan_summary = ", ".join(f"{name}={fan_values[name]}RPM" for name in fan_sensor_names)
            status = (
                f"{ts_text} {sample_label} "
                f"inlet={temp_sample.inlet_f:.1f}F cpu1={temp_sample.cpu1_f:.1f}F "
                f"fans: {fan_summary} "
                f"(temp sensors: {inlet_sensor_name}, {cpu1_sensor_name})"
            )
            status_path.write_text(status + "\n", encoding="utf-8")
            print(status)

            if alert_config.enabled:
                peak_sensor = "CPU1" if temp_sample.cpu1_f >= temp_sample.inlet_f else "Inlet"
                peak_temp = max(temp_sample.cpu1_f, temp_sample.inlet_f)
                if peak_temp > alert_config.threshold_f:
                    if not alert_active:
                        subject = (
                            f"{alert_config.subject_prefix} temperature alert on {args.host}: "
                            f"{peak_sensor} {peak_temp:.1f}F"
                        )
                        body = (
                            f"Temperature threshold exceeded on iDRAC {args.host}.\n\n"
                            f"Time: {ts_text}\n"
                            f"Threshold: {alert_config.threshold_f:.1f}F\n"
                            f"Inlet: {temp_sample.inlet_f:.1f}F\n"
                            f"CPU1: {temp_sample.cpu1_f:.1f}F\n"
                            f"Trigger: {peak_sensor} {peak_temp:.1f}F\n"
                            f"Fan speeds: {fan_summary}\n"
                        )
                        try:
                            send_email_alert(alert_config, subject, body)
                            alert_active = True
                            print(
                                f"Email alert sent to {alert_config.email_to}: "
                                f"{peak_sensor} {peak_temp:.1f}F > {alert_config.threshold_f:.1f}F"
                            )
                        except Exception as exc:
                            print(f"ERROR sending alert email: {exc}", file=sys.stderr)
                else:
                    alert_active = False
        except Exception as exc:
            err = f"{ts_text} {sample_label} ERROR: {exc}"
            status_path.write_text(err + "\n", encoding="utf-8")
            print(err, file=sys.stderr)

        if total_samples is not None and sample_idx == total_samples - 1:
            break
        sample_idx += 1
        next_epoch = start_epoch + (sample_idx * args.interval_seconds)
        sleep_seconds = max(0.0, next_epoch - time.time())
        time.sleep(sleep_seconds)

    done_text = f"{display_now().strftime('%Y-%m-%d %I:%M:%S %p %Z')} completed {total_samples} samples"
    status_path.write_text(done_text + "\n", encoding="utf-8")
    print(done_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
