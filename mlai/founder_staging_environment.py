"""Validate a proposed, separate founder fixture runtime before Django loads.

This module does not provision, connect to, or enable any service. Existing
profiles never import it. A future dedicated profile must call this guard
before importing base settings or initializing applications.
"""
from dataclasses import dataclass, field
import re
from urllib.parse import unquote, urlsplit


PROFILE = "mlai.settings_staging_founder"
DATABASE = ("founder-postgres", 5432, "founder_runtime", "/founder_acceptance")
CACHE = ("founder-redis", 6379, "/0")
EMAIL_DOMAIN = "staging.founder.mlai.au"
TOKEN = re.compile(r"[0-9a-f]{64,128}")
PRIVATE_INPUTS = {"SECRET_KEY", "COMMUNITY_CHAT_EMAIL_CODE_PEPPER", "FOUNDER_STAGING_INBOX_WRITE_TOKEN", "COMMUNITY_CHAT_ADAPTER_TOKEN",
                  "COMMUNITY_CHAT_EMAIL_CODE_DELIVERY_SECRET", "MLAI_CHAT_ACCOUNT_KEY_SECRET"}
SECRET_NAME = re.compile(r"(?:^|_)(?:KEYS?|SECRET|TOKEN|PASSWORD|CREDENTIALS|SERVICE_ACCOUNT)(?:_(?:B64|BASE64|JSON))?$")
NOTIFICATIONS = (
    "ARTICLES_DRAFT_READY_NOTIFICATIONS_ENABLED", "ARTICLES_GOAL_NOTIFICATIONS_ENABLED",
    "MY_STARTUP_TRANSITION_NOTIFICATIONS_ENABLED", "MY_STARTUP_TURN_REMINDERS_ENABLED",
)


class InvalidFounderEnvironment(ValueError):
    """The disabled fixture cannot start with this environment."""


@dataclass(frozen=True)
class FounderEnvironment:
    """Validated private inputs; their values never appear in repr or errors."""
    database_url: str = field(repr=False)
    redis_url: str = field(repr=False)
    secret_key: str = field(repr=False)
    email_pepper: str = field(repr=False)
    inbox_writer: str = field(repr=False)
    adapter_token: str = field(repr=False)
    delivery_secret: str = field(repr=False)
    account_key_secret: str = field(repr=False)
    email_domain: str = EMAIL_DOMAIN


def _refuse(condition):
    if not condition:
        raise InvalidFounderEnvironment("Dedicated founder staging configuration is incomplete or outside its fixed scope")


def _url(value, schemes):
    _refuse(isinstance(value, str) and value == value.strip())
    try:
        parsed = urlsplit(value)
        _refuse(parsed.scheme in schemes and not parsed.query and not parsed.fragment)
        # Accessing port here rejects malformed and out-of-range numeric ports.
        parsed.port
    except (ValueError, TypeError):
        raise InvalidFounderEnvironment("Dedicated founder staging URL is invalid") from None
    return parsed


def checked_founder_environment(environ):
    """Refuse production/old-pilot routes, ambient SDK keys and startup DDL.

    The runtime role must also be restricted on the separate PostgreSQL service;
    validating a URL cannot prove server-side privileges or network isolation.
    """
    _refuse(environ.get("DJANGO_SETTINGS_MODULE") == PROFILE
            and environ.get("APP_ENV") == "chat-staging"
            and environ.get("FOUNDER_STAGING_ENABLED") == "true"
            and environ.get("FOUNDER_STAGING_INBOX_PROVIDER_ENABLED") == "true"
            and environ.get("RUN_MIGRATIONS_ON_START") == "0")
    _refuse(not any(key.startswith("PG") for key in environ))
    for name in (*NOTIFICATIONS, "COMMUNITY_CHAT_STAGING_PASSWORD_ENABLED",
                 "COMMUNITY_CHAT_FEDERATION_STAGING_ENABLED", "TOKENMAXER_FEDERATION_ENABLED"):
        _refuse(environ.get(name, "false") == "false")
    _refuse(not environ.get("FOUNDER_STAGING_INBOX_READ_TOKEN"))
    forbidden = {"GOOGLE_APPLICATION_CREDENTIALS", "CUSTOMERIO_SITE_ID", "CUSTOMERIO_API_KEY",
                 "STRIPE_SECRET_KEY", "SLACK_BOT_TOKEN", "CONTENT_FACTORY_API_KEY", "CONTENT_FACTORY_URL",
                 "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "RESEND_API_KEY",
                 "SENTRY_DSN", "FIREBASE_SERVICE_ACCOUNT"}
    _refuse(not any(value and name not in PRIVATE_INPUTS
                    and (name in forbidden or name.startswith(("AWS_", "OTEL_", "FIREBASE_")) or SECRET_NAME.search(name))
                    for name, value in environ.items()))
    database_url, redis_url = environ.get("DATABASE_URL", ""), environ.get("REDIS_URL", "")
    database, cache = _url(database_url, {"postgres", "postgresql"}), _url(redis_url, {"redis"})
    _refuse((database.hostname, database.port, unquote(database.username or ""), database.path) == DATABASE)
    _refuse((cache.hostname, cache.port, cache.path) == CACHE and cache.username in (None, "", "default"))
    credentials = [unquote(database.password or ""), unquote(cache.password or "")]
    names = ("SECRET_KEY", "COMMUNITY_CHAT_EMAIL_CODE_PEPPER", "FOUNDER_STAGING_INBOX_WRITE_TOKEN", "COMMUNITY_CHAT_ADAPTER_TOKEN",
             "COMMUNITY_CHAT_EMAIL_CODE_DELIVERY_SECRET", "MLAI_CHAT_ACCOUNT_KEY_SECRET")
    values = [environ.get(name, "") for name in names]
    _refuse(all(isinstance(value, str) and TOKEN.fullmatch(value) for value in [*credentials, *values]))
    _refuse(len(set([*credentials, *values])) == 8)
    _refuse(environ.get("FOUNDER_STAGING_EMAIL_DOMAIN") == EMAIL_DOMAIN)
    return FounderEnvironment(database_url, redis_url, *values)
