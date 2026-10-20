from python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# В Alpine пакетный менеджер apk устанавливает ffmpeg в 10 раз быстрее и легче, чем apt-get в Debian
RUN apk add --no-cache ffmpeg ca-certificates

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/downloads && \
    adduser -D -u 1000 botuser && \
    chown -R botuser:botuser /app

USER botuser

HEALTHCHECK --interval=60s --timeout=10s --start-period=15s --retries=3 \
    CMD pgrep -f "bot.main" > /dev/null || exit 1

CMD ["python", "-m", "bot.main"]
