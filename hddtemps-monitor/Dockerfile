FROM alpine:3.20

RUN apk add --no-cache python3 openssh-client tzdata

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY app/monitor.py /app/monitor.py

EXPOSE 8789

CMD ["python3", "/app/monitor.py"]
