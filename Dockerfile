FROM python:3.11-slim AS dependencies

WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt requirements-engine.txt requirements.lock ./
RUN pip install --no-cache-dir --prefix=/install --require-hashes -r requirements.lock

FROM python:3.11-slim AS application

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY --from=dependencies /install /usr/local

COPY . /app/

# Collect static files with the same storage backend used at runtime. The
# build-only flag avoids loading production secrets while still creating the
# hashed manifest that Django Admin and DRF expect.
RUN DJANGO_STATIC_BUILD=True python manage.py collectstatic --noinput \
    && python -c "import json; manifest = json.load(open('/app/staticfiles/staticfiles.json')); paths = manifest.get('paths', {}); required = {'admin/css/base.css', 'rest_framework/css/bootstrap.min.css'}; missing = sorted(required - paths.keys()); assert not missing, f'Missing production static manifest entries: {missing}'"

EXPOSE 8000

CMD ["sh", "/app/scripts/start-web.sh"]

# Only the Jobs worker launches browsers. Other services reuse the common image.
FROM application AS jobs
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN python -m playwright install --with-deps chromium

# Keep an ordinary `docker build .` lean too.
FROM application AS runtime
