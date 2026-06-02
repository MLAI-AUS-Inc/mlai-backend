FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    libpq-dev \
    gcc \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt
RUN python -m playwright install --with-deps chromium

# Install the WTH engine LAST, in its own layer. Bumping the engine tag in
# requirements-engine.txt invalidates only this small layer (not the expensive
# pip/playwright layers above), so scenario-release redeploys stay fast.
COPY requirements-engine.txt /app/
RUN pip install --no-cache-dir -r requirements-engine.txt

COPY . /app/

# Collect static files
RUN python manage.py collectstatic --noinput

EXPOSE 8000

CMD ["sh", "/app/scripts/start-web.sh"]
