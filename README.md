# iDRAC Temp/Fan Monitoring

Containerized iDRAC temperature and fan monitoring with a local dashboard, email alerts, and optional PowerEdge fan control integration based on https://github.com/White-Raven/PowerEdge-shutup

## What It Does

- Captures inlet and CPU temperature data from iDRAC
- Captures fan RPM data
- Renders daily, weekly, monthly, and yearly SVG graphs
- Serves a browser dashboard over HTTP
- Optionally runs the bundled `PowerEdge-shutup` fan-control script
- Optionally sends email alerts when temperatures cross a threshold

## Configuration

The container reads its settings from environment variables. A publish-safe example is included in `.env.example`.

Important settings:

- `IDRAC_HOSTS`
- `IDRAC_HOST`
- `IDRAC_USER`
- `IDRAC_PASSWORD`
- `CHECK_INTERVAL_SECONDS`
- `DURATION_SECONDS`
- `ALERT_EMAIL_ENABLED`

Use `IDRAC_HOSTS` for multiple targets with shared credentials and shared monitoring settings. `IDRAC_HOST` remains available for the single-host case.

`DURATION_SECONDS=0` means run indefinitely.

When multiple hosts are configured, the container launches one monitor per host and the dashboard adds a host selector so you can switch between per-host graphs and logs.

## Run

```bash
docker compose up -d --build
```

The dashboard is served on port `8580` by default.

## Notes

- Runtime data and captured dashboards are intentionally ignored by Git.
- The `PowerEdge-shutup` directory is vendored in this repository so local fan-curve edits are tracked with the project.


