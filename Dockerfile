# =============================================================================
# Tamasha Free Channel HLS Extractor — Production Dockerfile
# =============================================================================
# Multi-stage isn't needed here; we need Chromium runtime deps in the final image.
# Based on python:3.11-slim-bookworm for minimal size with glibc.
# =============================================================================

FROM python:3.11-slim-bookworm

# Metadata
LABEL maintainer="tamasha-extractor"
LABEL description="Flask + Playwright API for extracting free HLS streams from Tamashaweb.com"

# Prevent Python from writing .pyc files and enable unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DEBIAN_FRONTEND=noninteractive

# -------------------------------------------------------------------------
# Install system dependencies required by Playwright Chromium
# -------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Core libs needed by Chromium
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
    # Font support (needed for page rendering)
    fonts-liberation \
    fonts-noto-color-emoji \
    # Misc utils
    wget \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# -------------------------------------------------------------------------
# Set up app directory
# -------------------------------------------------------------------------
WORKDIR /app

# -------------------------------------------------------------------------
# Install Python dependencies
# -------------------------------------------------------------------------
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# -------------------------------------------------------------------------
# Install Playwright Chromium browser
# -------------------------------------------------------------------------
RUN playwright install chromium \
    && playwright install-deps chromium 2>/dev/null || true

# -------------------------------------------------------------------------
# Copy application code
# -------------------------------------------------------------------------
COPY app.py .

# -------------------------------------------------------------------------
# Create non-root user for security
# -------------------------------------------------------------------------
RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser \
    && chown -R appuser:appuser /app \
    && chown -R appuser:appuser /ms-playwright

USER appuser

# -------------------------------------------------------------------------
# Expose port (Render.com uses $PORT env var, default 5000)
# -------------------------------------------------------------------------
EXPOSE 5000

# -------------------------------------------------------------------------
# Health check
# -------------------------------------------------------------------------
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD wget --no-verbose --tries=1 --spider http://localhost:${PORT:-5000}/api/health || exit 1

# -------------------------------------------------------------------------
# Start with gunicorn (production WSGI server)
# Use --timeout 120 because stream extraction can take 30-60s
# -------------------------------------------------------------------------
CMD ["sh", "-c", "gunicorn app:app --bind 0.0.0.0:${PORT:-5000} --workers 2 --threads 4 --timeout 120 --access-logfile - --error-logfile -"]
