FROM python:3.11-slim-bookworm

LABEL maintainer="tamasha-extractor" version="2.1.0"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DEBIAN_FRONTEND=noninteractive

# Chromium system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libdbus-1-3 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    libatspi2.0-0 libwayland-client0 libxshmfence1 \
    libglib2.0-0 libexpat1 \
    fonts-liberation fonts-noto-color-emoji fonts-freefont-ttf \
    wget curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN playwright install chromium && \
    playwright install-deps chromium 2>/dev/null || true && \
    python -c "from playwright.sync_api import sync_playwright; print('Playwright OK')"

COPY app.py .

RUN groupadd -r appuser && \
    useradd -r -g appuser -d /app -s /sbin/nologin appuser && \
    chown -R appuser:appuser /app /ms-playwright && \
    mkdir -p /tmp/.chromium && chown -R appuser:appuser /tmp/.chromium

USER appuser

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD wget -q --spider http://localhost:${PORT:-5000}/api/health || exit 1

CMD ["sh", "-c", \
    "gunicorn app:app \
     --bind 0.0.0.0:${PORT:-5000} \
     --workers 1 \
     --threads 4 \
     --timeout 120 \
     --graceful-timeout 30 \
     --keep-alive 5 \
     --access-logfile - \
     --error-logfile - \
     --log-level info"]
