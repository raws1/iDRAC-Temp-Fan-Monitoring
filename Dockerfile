FROM alpine:3.21

RUN apk add --no-cache bash python3 ipmitool tzdata

WORKDIR /app

COPY monitor_idrac_temps_f.py /app/monitor_idrac_temps_f.py
COPY generate_fan_curve_panel.py /app/generate_fan_curve_panel.py
COPY PowerEdge-shutup /app/PowerEdge-shutup
COPY config/index.html /app/index.html
COPY config/entrypoint.sh /app/entrypoint.sh
COPY config/fancontrol-loop.sh /app/fancontrol-loop.sh

RUN chmod +x /app/entrypoint.sh /app/fancontrol-loop.sh /app/PowerEdge-shutup/fancontrol.sh /app/PowerEdge-shutup/temppull.sh

ENV TZ=America/New_York \
    OUTPUT_DIR=/data \
    HTTP_BIND=0.0.0.0 \
    HTTP_PORT=8580 \
    CHECK_INTERVAL_SECONDS=180 \
    IDRAC_ENCRYPTION_KEY=0000000000000000000000000000000000000000 \
    INTERVAL_SECONDS=180 \
    DURATION_SECONDS=86400 \
    ENABLE_POWEREDGE_SHUTUP=1 \
    FANCONTROL_INTERVAL_SECONDS=180 \
    ALERT_EMAIL_ENABLED=0 \
    ALERT_TEMP_THRESHOLD_F=120 \
    ALERT_EMAIL_TEST_ON_START=1 \
    ALERT_SMTP_PORT=587 \
    ALERT_SMTP_STARTTLS=1 \
    ALERT_SMTP_SSL=0 \
    ALERT_EMAIL_SUBJECT_PREFIX=delltemps

EXPOSE 8580

ENTRYPOINT ["/app/entrypoint.sh"]
