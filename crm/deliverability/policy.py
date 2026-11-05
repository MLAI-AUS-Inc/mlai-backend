"""Central defaults; every numeric delivery rule is configurable through settings."""

DEFAULTS = {
    "CRM_STREAMS": "news:news.mlai.au:hello@news.mlai.au:marketing|event_update;notify:notify.mlai.au:notifications@notify.mlai.au:transactional",
    "CRM_PROTECTED_DOMAINS": ("mlai.au",),
    "CRM_DELIVERABILITY_OWNERS": ("tech@mlai.au",),
    "CRM_DELIVERABILITY_SLACK_CHANNEL": "",
    "CRM_GUARDIAN_ENABLED": False,
    "CRM_GUARDIAN_DRY_RUN": False,
    "CRM_GUARD_INTERVAL_SECONDS": 55,
    "CRM_GUARD_HEARTBEAT_SECONDS": 180,
    "CRM_GUARD_LOOKBACK_DAYS": 7,
    "CRM_GUARD_HARD_BOUNCE_MIN_SAMPLE": 100,
    "CRM_GUARD_HARD_BOUNCE_AMBER": 0.02,
    "CRM_GUARD_HARD_BOUNCE_RED": 0.05,
    "CRM_GUARD_COMPLAINT_MIN_COUNT": 2,
    "CRM_GUARD_COMPLAINT_RED_MIN_COUNT": 3,
    "CRM_GUARD_COMPLAINT_MIN_SAMPLE": 300,
    "CRM_GUARD_COMPLAINT_AMBER": 0.0005,
    "CRM_GUARD_COMPLAINT_RED": 0.001,
    "CRM_GUARD_COMPLAINT_HIGH": 0.003,
    "CRM_GUARD_POLICY_BLOCK_MIN_COUNT": 3,
    "CRM_GUARD_SOFT_BOUNCE_LIMIT": 3,
    "CRM_GUARD_ALERT_REMINDER_HOURS": 4,
    "CRM_GUARD_RESUME_DEMOTION": 2,
    "CRM_GUARD_RESUME_SLOW_HOURS": 48,
    "CRM_GUARD_POLICY_WINDOW_HOURS": 1,
    "CRM_FEEDBACK_MATCH_DAYS": 7,
    "CRM_FEEDBACK_DIAGNOSTIC_LENGTH": 500,
}


def value(name):
    """Read at use time so overrides and tests share the same policy."""
    from django.conf import settings
    return getattr(settings, name, DEFAULTS[name])


def environment_settings(environ):
    """Parse the policy's environment inventory without logging configured values."""
    result = {}
    for name, default in DEFAULTS.items():
        raw = environ.get(name)
        if raw is None:
            result[name] = default
            continue
        if isinstance(default, bool):
            if raw.lower() not in ("true", "false", "1", "0"):
                raise ValueError(f"{name} must be a boolean")
            result[name] = raw.lower() in ("true", "1")
        elif isinstance(default, tuple):
            result[name] = tuple(item.strip().lower() for item in raw.split(",") if item.strip())
        elif isinstance(default, int):
            try:
                result[name] = int(raw)
            except (ValueError, TypeError):
                raise ValueError(f"{name} must be an integer") from None
            if result[name] < 1:
                raise ValueError(f"{name} must be positive")
        elif isinstance(default, float):
            try:
                result[name] = float(raw)
            except (ValueError, TypeError):
                raise ValueError(f"{name} must be a rate") from None
            if not 0 < result[name] <= 1:
                raise ValueError(f"{name} must be a rate between zero and one")
        else:
            result[name] = raw.strip()
    return result
