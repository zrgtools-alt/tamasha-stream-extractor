# ===========================================================================
# Tamasha Free Channel HLS Extractor — Production Dockerfile v2
# Optimized for Render.com (Docker runtime, 512MB-2GB RAM)
# ===========================================================================

FROM python:3.11-slim-bookworm AS base

LABEL maintainer="tamasha-extractor"
LABEL version="2.0.0"

# Environment
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DEBIAN_FRONTEND=noninteractive

# ---- System dependencies for Playwright Chromium ----
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Chromium runtime dependencies
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libatspi2.0-0 \
    libwayland-client0 \
    libxshmfence1 \
    libglib2.0-0 \
    libexpat1 \
    # Fonts
    fonts-liberation \
    fonts-noto-color-emoji \
    fonts-freefont-ttf \
    # Networking / diagnostics
    wget \
    curl \
    ca-certificates \
    # Clean up
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

WORKDIR /app

# ---- Python dependencies ----
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---- Playwright Chromium ----
RUN playwright install chromium && \
    # Install any remaining OS deps playwright needs
    playwright install-deps chromium 2>/dev/null || true && \
    # Verify installation
    python -c "from playwright.sync_api import sync_playwright; print('Playwright OK')"

# ---- Application code ----
COPY app.py .

# ---- Non-root user ----
RUN groupadd -r appuser && \
    useradd -r -g appuser -d /app -s /sbin/nologin appuser && \
    chown -R appuser:appuser /app && \
    chown -R appuser:appuser /ms-playwright && \
    # Create tmp dir for Chromium
    mkdir -p /tmp/.chromium && chown -R appuser:appuser /tmp/.chromium

USER appuser

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD wget --quiet --tries=1 --spider http://localhost:${PORT:-5000}/api/health || exit 1

# Gunicorn with:
#   --workers 1     : Single worker (Chromium is heavy, one at a time)
#   --threads 4     : Handle concurrent HTTP requests via threads
#   --timeout 120   : Extraction can take 30-60s
#   --graceful-timeout 30 : Allow clean shutdown
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
