#!/bin/sh
set -eu

if [ "${RUN_MIGRATIONS_ON_START:-1}" = "1" ]; then
  python manage.py migrate --noinput
  python manage.py migrate --check --noinput
fi

exec gunicorn \
  --bind 0.0.0.0:8000 \
  --workers "${GUNICORN_WORKERS:-3}" \
  --worker-class sync \
  --keep-alive "${GUNICORN_KEEP_ALIVE:-2}" \
  --timeout "${GUNICORN_TIMEOUT:-30}" \
  --graceful-timeout "${GUNICORN_GRACEFUL_TIMEOUT:-30}" \
  --max-requests "${GUNICORN_MAX_REQUESTS:-300}" \
  --max-requests-jitter "${GUNICORN_MAX_REQUESTS_JITTER:-50}" \
  mlai.wsgi:application
