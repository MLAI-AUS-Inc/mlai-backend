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

# One hashed resolution includes both the API and Watt engine requirements.
COPY requirements.txt requirements-engine.txt requirements.lock /app/
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
RUN python -m playwright install --with-deps chromium

COPY . /app/

# Collect static files with the same storage backend used at runtime. The
# build-only flag avoids loading production secrets while still creating the
# hashed manifest that Django Admin and DRF expect.
RUN DJANGO_STATIC_BUILD=True python manage.py collectstatic --noinput \
    && python -c "import json; manifest = json.load(open('/app/staticfiles/staticfiles.json')); paths = manifest.get('paths', {}); required = {'admin/css/base.css', 'rest_framework/css/bootstrap.min.css'}; missing = sorted(required - paths.keys()); assert not missing, f'Missing production static manifest entries: {missing}'"

EXPOSE 8000

CMD ["sh", "/app/scripts/start-web.sh"]
