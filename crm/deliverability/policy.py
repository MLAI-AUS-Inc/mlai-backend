"""Central defaults; every numeric delivery rule is configurable through settings."""

import math

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
    "CRM_WAVES_ENABLED": False,
    "CRM_RAMP_LADDER": (150, 300, 600, 1000, 1500, 2500, 4000, 6000, 9000, 13000, 20000, 30000, 45000),
    "CRM_STREAM_MAX_DAILY": 45000,
    "CRM_STREAM_ACTIVE_PEAK_FACTOR": 1.5,
    "CRM_STREAM_SLOW_FACTOR": 0.5,
    "CRM_RAMP_MIN_UTILIZATION": 0.6,
    "CRM_RAMP_HEALTHY_HARD_BOUNCE": 0.02,
    "CRM_RAMP_HEALTHY_COMPLAINT": 0.0005,
    "CRM_RAMP_HEALTHY_DEFERRAL": 0.03,
    "CRM_RAMP_PEAK_DAYS": 30,
    "CRM_IDLE_DECAY_DAYS": 30,
    "CRM_IDLE_DECAY_STAGES": 2,
    "CRM_IDLE_LONG_DECAY_DAYS": 90,
    "CRM_IDLE_LONG_DECAY_STAGES": 5,
    "CRM_IDLE_RESET_DAYS": 180,
    "CRM_SEND_WINDOW": "mon-fri 09:00-16:00 Australia/Melbourne",
    "CRM_EVENT_SEND_WINDOW": "mon-sun 09:00-18:00 Australia/Melbourne",
    "CRM_BLACKOUT_DATES": (),
    "CRM_TIME_CRITICAL_HOURS": 72,
    "CRM_CANARY_MIN": 50,
    "CRM_CANARY_MAX": 200,
    "CRM_CANARY_FRACTION": 0.1,
    "CRM_CANARY_WAIT_MINUTES": 45,
    "CRM_EVENT_CANARY_WAIT_MINUTES": 20,
    "CRM_CANARY_MIN_SAMPLE": 50,
    "CRM_CANARY_HARD_BOUNCE_COUNT": 8,
    "CRM_CANARY_MAX_DEFERRAL_RATE": 0.05,
    "CRM_MIN_WAVE": 25,
    "CRM_HOURLY_HEADROOM": 1.5,
    "CRM_PROVIDER_SHARE_HEADROOM": 1.25,
    "CRM_PROVIDER_THROTTLE_FACTOR": 0.25,
    "CRM_FREQ_MIN_GAP_DAYS": 3,
    "CRM_FREQ_MAX_PER_7D": 2,
    "CRM_FREQ_WINDOW_DAYS": 7,
    "CRM_FREQ_MAX_DEFER_DAYS": 7,
    "CRM_EVENT_FREQ_GAP_HOURS": 24,
    "CRM_TIER_A_DAYS": 30,
    "CRM_TIER_B_DAYS": 90,
    "CRM_DORMANT_DAYS": 365,
    "CRM_TIER_A_STARS": 4,
    "CRM_TIER_B_STARS": 3,
    "CRM_TIER_C_STARS": 2,
    "CRM_MAX_BLAST_RECIPIENTS": 50000,
    "CRM_DOMAIN_CACHE_DAYS": 30,
    "CRM_DOMAIN_ERROR_CACHE_SECONDS": 300,
    "CRM_DOMAIN_CHECK_WORKERS": 8,
    "CRM_FEEDBACK_MAX_AGE_MINUTES": 60,
    "CRM_EMAIL_EVENT_RETENTION_DAYS": 180,
    "CRM_ESTIMATE_MAX_DAYS": 365,
    "CRM_DAY_CLOSE_INTERVAL_SECONDS": 3600,
    "CRM_RAMP_AMBER_DAYS": 2,
    "CRM_RAMP_AMBER_DEMOTION": 1,
}


def value(name, stream=None):
    """Read at use time so overrides and tests share the same policy."""
    from django.conf import settings
    result = getattr(settings, name, DEFAULTS[name])
    overrides = getattr(stream, "policy_overrides", None) or {}
    if name in ("CRM_SEND_WINDOW", "CRM_EVENT_SEND_WINDOW") and name in overrides:
        if not isinstance(overrides[name], str):
            raise ValueError("Invalid stream window override")
        result = overrides[name]
    return result


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
            pieces = tuple(item.strip().lower() for item in raw.split(",") if item.strip())
            if default and all(type(item) is int for item in default):
                try:
                    pieces = tuple(int(item) for item in pieces)
                except ValueError:
                    raise ValueError(f"{name} must contain positive increasing integers") from None
                if not pieces or any(item <= 0 for item in pieces) or any(a >= b for a, b in zip(pieces, pieces[1:])):
                    raise ValueError(f"{name} must contain positive increasing integers")
            result[name] = pieces
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
            if not math.isfinite(result[name]) or result[name] <= 0 or (default <= 1 and result[name] > 1):
                raise ValueError(f"{name} must be a positive finite policy value")
        else:
            result[name] = raw.strip()
    return result
