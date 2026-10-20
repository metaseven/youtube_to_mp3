from python:3.12-slim

# Предотвращаем создание .pyc файлов, интерактивные запросы apt и буферизацию
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive

# Устанавливаем минимальный ffmpeg (без документации и лишних пакетов) и procps (для pgrep)
RUN apt-get update -qq && \
    apt-get install -y -qq --no-install-recommends ffmpeg ca-certificates procps && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Устанавливаем зависимости напрямую без лишнего перекомпилирования pip
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем исходный код проекта
COPY . .

# Создаём директорию для временных загрузок и непривилегированного пользователя
RUN mkdir -p /app/downloads && \
    useradd -m -u 1000 botuser && \
    chown -R botuser:botuser /app

USER botuser

# Встроенная проверка работоспособности контейнера для Docker / Coolify
HEALTHCHECK --interval=60s --timeout=10s --start-period=15s --retries=3 \
    CMD pgrep -f "bot.main" > /dev/null || exit 1

CMD ["python", "-m", "bot.main"]
