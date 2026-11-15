"""The effective value of every founder Articles, My startup and Content Factory setting.

``python manage.py article_flags`` prints one line per setting so the owner can
read production configuration in one command. Secrets are never printed: a
setting whose name looks like a key, token, secret or password shows only
whether it is set. Nothing here reads the database or calls a provider.
"""
from __future__ import annotations

import os

from django.conf import settings

PREFIXES = ("ARTICLES_", "MY_STARTUP_", "CONTENT_FACTORY_", "WEBSITE_")
SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PRIVATE")
MAX_VALUE_LENGTH = 120


def deploy_keys() -> frozenset:
    """Settings the release pipeline can install (scripts/validate_articles_deploy_config.py)."""
    try:
        from scripts.validate_articles_deploy_config import KEYS
    except ImportError:
        return frozenset()
    return frozenset(KEYS)


def is_secret(name: str) -> bool:
    return any(marker in name for marker in SECRET_MARKERS)


def setting_names() -> list[str]:
    """Every founder-flow setting name: the prefixes above plus every key the release pipeline installs."""
    names = {name for name in dir(settings) if name.isupper() and name.startswith(PREFIXES)}
    names |= {name for name in deploy_keys() if hasattr(settings, name)}
    return sorted(names)


def display(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple, set, frozenset)):
        value = ",".join(str(item) for item in value)
    text = " ".join(str(value).split())
    return text if len(text) <= MAX_VALUE_LENGTH else text[:MAX_VALUE_LENGTH - 3] + "..."


def flag_rows() -> list[dict]:
    """One row per setting: effective value, whether the host environment sets it, and whether a release can."""
    wired = deploy_keys()
    rows = []
    for name in setting_names():
        value = getattr(settings, name)
        secret = is_secret(name)
        rows.append({
            "name": name,
            "value": ("set" if value else "unset") if secret else display(value),
            "source": "env" if name in os.environ else "default",
            "deploy": name in wired,
            "secret": secret,
        })
    return rows


def warnings() -> list[str]:
    """Plain warnings about settings that are on but cannot work as configured."""
    from .founder_journey_alerts import SLACK_CHANNEL_PATTERN
    channel = str(getattr(settings, "CONTENT_FACTORY_OPS_SLACK_CHANNEL_ID", "") or "").strip()
    if SLACK_CHANNEL_PATTERN.fullmatch(channel):
        return []
    on = [name for name in ("ARTICLES_FOUNDER_STUCK_ALERTS_ENABLED", "ARTICLES_FOUNDER_JOURNEY_ALERTS_ENABLED")
          if getattr(settings, name, name == "ARTICLES_FOUNDER_STUCK_ALERTS_ENABLED")]
    if not on:
        return []
    state = "is not set" if not channel else "is not a valid Slack channel id"
    return [f"WARNING: {', '.join(on)} is on, but CONTENT_FACTORY_OPS_SLACK_CHANNEL_ID {state}. "
            "Alerts are only logged and never reach Slack. Set the channel id on the host."]
