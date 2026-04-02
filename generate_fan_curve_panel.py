#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import os
import re
from pathlib import Path


STEP_ASSIGN_RE = re.compile(
    r"^(TEMP_STEP|FST|AMBTEMP_STEP|AMBTEMP_MOD_STEP|AMBTEMP_noCPU_FS_STEP)(\d+)=(.+)$"
)
SIMPLE_ASSIGN_RE = re.compile(r"^(TEMPgov|CPUdelta|DeltaR|EXHTEMP_MAX|MAX_MOD|E_value)=(.+)$")
ENV_DEFAULT_RE = re.compile(r"^\$\{([A-Z0-9_]+):-([^}]+)\}$")


def normalize_value(raw: str) -> str:
    value = raw.strip().strip('"').strip("'")
    match = ENV_DEFAULT_RE.match(value)
    if match:
        env_name, default_value = match.groups()
        return os.environ.get(env_name, default_value)
    return value


def parse_bool(raw: str | None, default: bool = False) -> bool:
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    return default


def parse_fancontrol(path: Path) -> dict[str, object]:
    cpu_steps: dict[int, int] = {}
    cpu_speeds: dict[int, int] = {}
    ambient_steps: dict[int, int] = {}
    ambient_mods: dict[int, int] = {}
    ambient_speeds: dict[int, int] = {}
    scalar: dict[str, str] = {}

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.rstrip()
        if raw_line[:1].isspace():
            continue
        if not line or line.startswith("#"):
            continue

        step_match = STEP_ASSIGN_RE.match(line)
        if step_match:
            prefix, index_text, value_raw = step_match.groups()
            index = int(index_text)
            value = int(normalize_value(value_raw))
            if prefix == "TEMP_STEP":
                cpu_steps[index] = value
            elif prefix == "FST":
                cpu_speeds[index] = value
            elif prefix == "AMBTEMP_STEP":
                ambient_steps[index] = value
            elif prefix == "AMBTEMP_MOD_STEP":
                ambient_mods[index] = value
            else:
                ambient_speeds[index] = value
            continue

        simple_match = SIMPLE_ASSIGN_RE.match(line)
        if simple_match:
            name, value_raw = simple_match.groups()
            scalar[name] = normalize_value(value_raw)

    return {
        "cpu_steps": cpu_steps,
        "cpu_speeds": cpu_speeds,
        "ambient_steps": ambient_steps,
        "ambient_mods": ambient_mods,
        "ambient_speeds": ambient_speeds,
        "scalar": scalar,
    }


def format_temp_f(value_c: int, *, delta: bool = False, signed: bool = False) -> str:
    value_f = value_c * 9.0 / 5.0
    if not delta:
        value_f += 32.0

    if value_f.is_integer():
        rendered = f"{int(value_f)}"
    else:
        rendered = f"{value_f:.1f}"

    if signed and not rendered.startswith("-"):
        rendered = f"+{rendered}"
    return f"{rendered} F"


def governor_label(tempgov: str) -> str:
    if tempgov == "1":
        return "Highest CPU temperature"
    return "Average CPU temperature"


def failsafe_label(raw_value: str | None) -> str:
    if raw_value is None:
        return "Auto"
    value = raw_value.strip()
    if value.lower() == "auto":
        return "Auto"
    if value.endswith("%"):
        return value
    return f"{value}%"


def numeric_failsafe_floor(raw_value: str | None) -> int | None:
    if raw_value is None:
        return None
    value = raw_value.strip()
    if not value or value.lower() == "auto":
        return None
    return int(value)


def build_panel_html(config: dict[str, object]) -> str:
    cpu_steps: dict[int, int] = config["cpu_steps"]  # type: ignore[assignment]
    cpu_speeds: dict[int, int] = config["cpu_speeds"]  # type: ignore[assignment]
    ambient_steps: dict[int, int] = config["ambient_steps"]  # type: ignore[assignment]
    ambient_mods: dict[int, int] = config["ambient_mods"]  # type: ignore[assignment]
    ambient_speeds: dict[int, int] = config["ambient_speeds"]  # type: ignore[assignment]
    scalar: dict[str, str] = config["scalar"]  # type: ignore[assignment]

    required_scalars = ("TEMPgov", "DeltaR", "EXHTEMP_MAX", "MAX_MOD")
    missing = [name for name in required_scalars if name not in scalar]
    if missing:
        raise ValueError(f"Missing scalar settings: {', '.join(missing)}")

    cpu_indexes = sorted(set(cpu_steps) & set(cpu_speeds))
    ambient_indexes = sorted(set(ambient_steps) & set(ambient_mods) & set(ambient_speeds))
    if not cpu_indexes:
        raise ValueError("No CPU fan curve steps found")
    if not ambient_indexes:
        raise ValueError("No ambient fan curve steps found")

    cpu_rows: list[str] = []
    for index in cpu_indexes:
        cpu_rows.append(
            "<tr>"
            f"<td>{html.escape(format_temp_f(cpu_steps[index]))} or lower</td>"
            f"<td>{cpu_speeds[index]}%</td>"
            "</tr>"
        )

    highest_cpu = cpu_steps[cpu_indexes[-1]]
    cpu_rows.append(
        "<tr>"
        f"<td>Above {html.escape(format_temp_f(highest_cpu))}</td>"
        f"<td>{html.escape(failsafe_label(scalar.get('E_value')))}</td>"
        "</tr>"
    )

    failsafe_floor = numeric_failsafe_floor(scalar.get("E_value"))
    ambient_rows: list[str] = []
    for index in ambient_indexes:
        effective_ambient_speed = ambient_speeds[index]
        if failsafe_floor is not None:
            effective_ambient_speed = max(effective_ambient_speed, failsafe_floor)
        ambient_rows.append(
            "<tr>"
            f"<td>{html.escape(format_temp_f(ambient_steps[index]))} or lower</td>"
            f"<td>{html.escape(format_temp_f(ambient_mods[index], delta=True, signed=True))}</td>"
            f"<td>{effective_ambient_speed}%</td>"
            "</tr>"
        )

    highest_ambient = ambient_steps[ambient_indexes[-1]]
    ambient_rows.append(
        "<tr>"
        f"<td>Above {html.escape(format_temp_f(highest_ambient))}</td>"
        f"<td>{html.escape(format_temp_f(int(scalar['MAX_MOD']), delta=True, signed=True))} max modifier</td>"
        f"<td>{html.escape(failsafe_label(scalar.get('E_value')))}</td>"
        "</tr>"
    )

    return f"""
<h2>Fan Curve Settings</h2>
<p>Configured thresholds from <code>PowerEdge-shutup/fancontrol.sh</code>, converted to Fahrenheit for display.</p>

<div class="curve-meta">
  <div class="metric">
    <span class="metric-label">CPU Governor</span>
    <span class="metric-value">{html.escape(governor_label(scalar["TEMPgov"]))}</span>
  </div>
  <div class="metric">
    <span class="metric-label">Delta A/E Ratio</span>
    <span class="metric-value">{html.escape(scalar["DeltaR"])}:1</span>
  </div>
  <div class="metric">
    <span class="metric-label">Exhaust Critical</span>
    <span class="metric-value">{html.escape(format_temp_f(int(scalar["EXHTEMP_MAX"])))}</span>
  </div>
</div>

<div class="curve-group">
  <h3>CPU Fan Curve</h3>
  <table>
    <thead>
      <tr>
        <th>CPU Temp</th>
        <th>Fan Speed</th>
      </tr>
    </thead>
    <tbody>
      {''.join(cpu_rows)}
    </tbody>
  </table>
</div>

<div class="curve-group">
  <h3>Ambient / Inlet Modifiers</h3>
  <table>
    <thead>
      <tr>
        <th>Inlet Temp</th>
        <th>CPU Offset</th>
        <th>Ambient-Only Fan</th>
      </tr>
    </thead>
    <tbody>
      {''.join(ambient_rows)}
    </tbody>
  </table>
</div>
""".strip()


def build_error_panel(message: str) -> str:
    return (
        "<h2>Fan Curve Settings</h2>"
        "<p>Unable to load fan curve settings from <code>PowerEdge-shutup/fancontrol.sh</code>.</p>"
        f"<pre>{html.escape(message)}</pre>"
    )


def build_alert_status_panel() -> str:
    enabled = parse_bool(os.environ.get("ALERT_EMAIL_ENABLED"), default=False)
    email_to = os.environ.get("ALERT_EMAIL_TO", "").strip()

    if enabled:
        rows = [
            "<div class='metric'>"
            "<span class='metric-label'>Email Alerts</span>"
            "<span class='metric-value status-active'>Active</span>"
            "</div>"
        ]
        if email_to:
            rows.append(
                "<div class='metric'>"
                "<span class='metric-label'>Send To</span>"
                f"<span class='metric-value'>{html.escape(email_to)}</span>"
                "</div>"
            )
    else:
        rows = [
            "<div class='metric'>"
            "<span class='metric-label'>Email Alerts</span>"
            "<span class='metric-value status-inactive'>Inactive</span>"
            "</div>"
        ]

    return (
        "<div class='alert-meta'>"
        + "".join(rows)
        + "</div>"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate dashboard HTML for fan curve settings.")
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--alert-output")
    args = parser.parse_args()

    source_path = Path(args.source)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        panel_html = build_panel_html(parse_fancontrol(source_path))
    except Exception as exc:
        panel_html = build_error_panel(str(exc))

    output_path.write_text(panel_html + "\n", encoding="utf-8")
    if args.alert_output:
        alert_output_path = Path(args.alert_output)
        alert_output_path.parent.mkdir(parents=True, exist_ok=True)
        alert_output_path.write_text(build_alert_status_panel() + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
