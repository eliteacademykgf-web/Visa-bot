FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip && pip install -e ".[dev]"
# Chromium и системные библиотеки для него. Обычный HTTP-клиент до страниц
# VFS не доходит — сайт отдаёт 403 без настоящего браузера.
RUN playwright install --with-deps chromium

COPY alembic.ini ./
COPY alembic ./alembic
COPY tests ./tests

# Процесс выбирается командой в docker-compose: bot / scheduler / web.
CMD ["python", "-m", "app.bot.main"]
