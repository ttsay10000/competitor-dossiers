# Use Debian so playwright install --with-deps can install Chromium system libraries.
FROM python:3.12-bookworm

WORKDIR /app

# Install Playwright and Chromium (with system deps). Required for Lark/AvantStay talent (JS-rendered career pages).
RUN pip install --no-cache-dir playwright==1.42.0 && \
    playwright install --with-deps chromium

# App dependencies (lean list; playwright already installed above).
COPY requirements-render.txt .
RUN pip install --no-cache-dir -r requirements-render.txt

# App code. Alembic migrations run at start if needed (see render.yaml or override startCommand).
COPY . .

# Default: run web server. Cron overrides via dockerCommand in render.yaml.
ENV PORT=10000
EXPOSE 10000
CMD ["sh", "-c", "python -m alembic upgrade head 2>/dev/null || true && exec python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-10000}"]
