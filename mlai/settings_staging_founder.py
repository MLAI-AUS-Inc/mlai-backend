"""Separate founder acceptance profile; no provisioning or implicit migrations.

The existing password pilot never imports this module. The environment must be
explicitly complete before base settings or applications can load. The profile
alone does not start a worker, expose ingress or qualify the actual journey.
"""
import os as _os
from copy import deepcopy as _deepcopy

from django.core.exceptions import ImproperlyConfigured

from .founder_staging_environment import checked_founder_environment, InvalidFounderEnvironment

try:
    _fixture = checked_founder_environment(_os.environ)
except InvalidFounderEnvironment as _failure:
    raise ImproperlyConfigured(str(_failure)) from None

from .settings import *
from .staging_integrations import pin_isolated_destinations

DEBUG = False
ALLOWED_HOSTS = ["api-founder-staging.mlai.au"]
ROOT_URLCONF = "mlai.urls"
DATABASES = _deepcopy(DATABASES)
# Ordinary services own user→credential locks. Request-wide transactions would
# retain authentication's session last-used write before onboarding's user lock.
DATABASES["default"]["ATOMIC_REQUESTS"] = False
CACHES = _deepcopy(CACHES)
for _cache in CACHES.values():
    _cache["KEY_PREFIX"] = "founder_acceptance"

FOUNDER_STAGING_ENABLED = True
FOUNDER_STAGING_INBOX_PROVIDER_ENABLED = True
FOUNDER_STAGING_EMAIL_DOMAIN = _fixture.email_domain
FOUNDER_STAGING_INBOX_WRITE_TOKEN = _fixture.inbox_writer
FOUNDER_STAGING_INBOX_PROVIDER_URL = "http://founder-ci-inbox:8990/deliver"

# Base integration defaults must not escape to the pilot or production. The
# container's separate reviewed network remains the authoritative backstop.
pin_isolated_destinations(globals(), founder=True)
for _name in list(globals()):
    if _name.isupper() and any(_word in _name for _word in (
            "API_KEY", "BOT_TOKEN", "WEBHOOK_SECRET", "OAUTH_CLIENT_SECRET",
            "SENTRY_DSN", "OTEL_EXPORTER", "SERVICE_ACCOUNT")):
        globals()[_name] = ""

COMMUNITY_CHAT_API_AUDIENCE = "https://api-founder-staging.mlai.au"
COMMUNITY_CHAT_FRONTEND_URL = "https://founder-staging.mlai.au"
COMMUNITY_CHAT_ALLOWED_ORIGINS = [COMMUNITY_CHAT_FRONTEND_URL]
COMMUNITY_CHAT_RELAY_URL = "wss://founder-staging.mlai.au"
COMMUNITY_CHAT_ADAPTER_URL = "http://founder-chat-adapter:3100"
COMMUNITY_CHAT_ADAPTER_TOKEN = _fixture.adapter_token
COMMUNITY_CHAT_MEMBER_ACCOUNTS_ENABLED = True
MLAI_CHAT_ACCOUNT_KEY_SECRET = _fixture.account_key_secret
COMMUNITY_CHAT_EMAIL_CODE_PEPPER = _fixture.email_pepper
COMMUNITY_CHAT_EMAIL_CODE_DELIVERY_SECRET = _fixture.delivery_secret
COMMUNITY_CHAT_SIGNUP_ENABLED = True
COMMUNITY_CHAT_EMAIL_CODE_AUTH_ENABLED = True
COMMUNITY_CHAT_EMAIL_CODE_TTL_SECONDS = 300
COMMUNITY_CHAT_ONE_STEP_JOIN_ENABLED = True
COMMUNITY_CHAT_STAGING_PASSWORD_ENABLED = False
COMMUNITY_CHAT_PASSWORD_AUTH_ENABLED = False
COMMUNITY_CHAT_DEVICE_AUTH_ENABLED = False
COMMUNITY_CHAT_DEVICE_AUTH_V2_ENABLED = False
COMMUNITY_CHAT_FEDERATION_ISSUER_ENABLED = False
COMMUNITY_CHAT_FEDERATION_STAGING_ENABLED = False
COMMUNITY_CHAT_AI_PROVIDERS = []
CONTENT_FACTORY_URL = "http://founder-content-factory:8000"
CONTENT_FACTORY_PREVIEW_BASE_URL = "https://preview-founder-staging.mlai.au"

COMMUNITY_CHAT_HOST_ONLY_COOKIES = True
COMMUNITY_CHAT_CLEAR_LEGACY_COOKIES = False
COMMUNITY_CHAT_ACCESS_COOKIE_NAME = "__Host-mlai_founder_access"
COMMUNITY_CHAT_REFRESH_COOKIE_NAME = "__Host-mlai_founder_refresh"
CORS_ALLOWED_ORIGINS = [COMMUNITY_CHAT_FRONTEND_URL]
CSRF_TRUSTED_ORIGINS = [COMMUNITY_CHAT_FRONTEND_URL]
SESSION_COOKIE_NAME = "__Host-mlai_founder_django"
CSRF_COOKIE_NAME = "__Host-mlai_founder_csrf"
SESSION_COOKIE_DOMAIN = None
CSRF_COOKIE_DOMAIN = None
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_REDIRECT_EXEMPT = ["^healthz/live$"]
MEDIA_ROOT = "/app/founder-staging-media"
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# Only the real leased email outbox is wired in this component. Generation and
# website/provider capabilities stay unavailable until their isolated stack is
# implemented and qualified. No production SDK or worker fallback is permitted.
STAGING_CAPABILITIES = {"email": "local", "account_creation": "local"}
MIDDLEWARE = ["mlai.founder_staging_admission.FounderAdmissionMiddleware",
              "mlai.staging_integrations.IntegrationMiddleware", *MIDDLEWARE]
REST_FRAMEWORK = {**REST_FRAMEWORK, "EXCEPTION_HANDLER": "mlai.staging_integrations.exception_handler"}
ARTICLES_DRAFT_READY_NOTIFICATIONS_ENABLED = False
ARTICLES_GOAL_NOTIFICATIONS_ENABLED = False
MY_STARTUP_TRANSITION_NOTIFICATIONS_ENABLED = False
MY_STARTUP_TURN_REMINDERS_ENABLED = False
ARTICLES_STAFF_ALERT_RULES_ENABLED = False
ARTICLES_FOUNDER_JOURNEY_ALERTS_ENABLED = False
ARTICLES_OPERATIONAL_ALERTS_ENABLED = False
MENTORING_GOOGLE_ENABLED = False
COMMUNITY_CHAT_VOLUNTEER_ENABLED = False
MY_STARTUP_TEAM_ENABLED = False
