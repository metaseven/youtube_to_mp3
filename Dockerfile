from python:3.12-slim

# Предотвращаем создание .pyc файлов и включаем небуферизованный вывод логов
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Устанавливаем ffmpeg (обязателен для конвертации в MP3 и встраивания обложек/метаданных)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Сначала копируем зависимости для кэширования слоёв Docker
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install -r requirements.txt && \
    pip install --upgrade yt-dlp

# Копируем исходный код проекта
COPY . .

# Создаём директорию для временных загрузок и непривилегированного пользователя
RUN mkdir -p /app/downloads && \
    useradd -m -u 1000 botuser && \
    chown -R botuser:botuser /app

USER botuser

# Встроенная проверка работоспособности контейнера для Docker / Coolify
HEALTHCHECK --interval=60s --timeout=10s --start-period=15s --retries=3 \
    CMD pgrep -f "python -m bot.main" > /dev/null || exit 1

CMD ["python", "-m", "bot.main"]
