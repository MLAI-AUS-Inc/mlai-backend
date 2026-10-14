"""Loopback-only real backend for synthetic local development, never hosted config."""
import os
from urllib.parse import urlsplit
from django.core.exceptions import ImproperlyConfigured
if os.environ.get('APP_ENV')!='staging-local':raise ImproperlyConfigured('Local profile requires APP_ENV=staging-local.')
url=urlsplit(os.environ.get('DATABASE_URL',''))
if url.scheme not in ('postgres','postgresql') or url.hostname not in ('127.0.0.1','localhost','::1') or not url.path[1:].startswith('local_'):
    raise ImproperlyConfigured('Local database must be a loopback PostgreSQL local_* target.')
if len(os.environ.get('SECRET_KEY',''))<64:raise ImproperlyConfigured('Generate an independent private local SECRET_KEY (64+ characters).')
from .settings import *
STAGING_LOCAL_DB_NAME=url.path[1:]
ROOT_URLCONF='mlai.urls'
DEBUG=False
ALLOWED_HOSTS=['localhost','127.0.0.1','[::1]']
SECURE_SSL_REDIRECT=False
SESSION_COOKIE_SECURE=False
SESSION_COOKIE_DOMAIN=None
CORS_ALLOWED_ORIGINS=['http://localhost:5173','http://127.0.0.1:5173']
CSRF_TRUSTED_ORIGINS=CORS_ALLOWED_ORIGINS
SIMPLE_JWT={**SIMPLE_JWT,'SIGNING_KEY':SECRET_KEY,'ISSUER':'mlai-staging-local','ACCESS_TOKEN_LIFETIME':timedelta(minutes=15)}
EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend'
MEDIA_ROOT=os.environ.get('STAGING_LOCAL_MEDIA_ROOT',str(Path(BASE_DIR) / '.scratch/local-media'))
COMMUNITY_CHAT_FEDERATION_ISSUER_ENABLED=False
COMMUNITY_CHAT_FEDERATION_STAGING_ENABLED=False
COMMUNITY_CHAT_STAGING_PASSWORD_ENABLED=False
COMMUNITY_CHAT_EMAIL_CODE_AUTH_ENABLED=False
COMMUNITY_CHAT_SIGNUP_ENABLED=False
COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED=False
STAGING_CAPABILITIES={}
MIDDLEWARE=['mlai.staging_integrations.IntegrationMiddleware',*MIDDLEWARE]
REST_FRAMEWORK={**REST_FRAMEWORK,'EXCEPTION_HANDLER':'mlai.staging_integrations.exception_handler'}
for _name in list(globals()):
    if _name.isupper() and any(word in _name for word in ('API_KEY','BOT_TOKEN','WEBHOOK_SECRET','OAUTH_CLIENT_SECRET','SENTRY_DSN','OTEL_EXPORTER','SERVICE_ACCOUNT')):
        globals()[_name]=''
DEFAULT_FRONTEND_URL='http://localhost:5173'
COMMUNITY_CHAT_FRONTEND_URL=DEFAULT_FRONTEND_URL
COMMUNITY_CHAT_API_AUDIENCE='http://127.0.0.1:8000'

from .staging_integrations import pin_isolated_destinations
pin_isolated_destinations(globals(),local=True)
