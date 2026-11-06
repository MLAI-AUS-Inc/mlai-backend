"""Central defaults; every numeric delivery rule is configurable through settings."""

import math

DEFAULTS = {
    "CRM_STREAMS": "news:news.mlai.au:hello@news.mlai.au:marketing|event_update;notify:notify.mlai.au:notifications@notify.mlai.au:transactional",
    "CRM_PROTECTED_DOMAINS": ("mlai.au",),
    "CRM_DELIVERABILITY_OWNERS": ("tech@mlai.au",),
    "CRM_DELIVERABILITY_SLACK_CHANNEL": "",
    "CRM_GUARDIAN_ENABLED": False,
    "CRM_LIFECYCLE_ENABLED": False,
    "CRM_TRANSACTIONAL_MONITOR_ENABLED": False,
    "CRM_CUSTOMERIO_NOTIFY_DKIM_VERIFIED": False,
    "CRM_TRANSACTIONAL_INTERVAL_SECONDS": 55,
    "CRM_TRANSACTIONAL_WINDOW_MINUTES": 15,
    "CRM_TRANSACTIONAL_MIN_SAMPLE": 1,
    "CRM_TRANSACTIONAL_FAILURE_RED": 0.05,
    "CRM_TRANSACTIONAL_MAX_ROWS": 20000,
    "CRM_TRANSACTIONAL_LATENCY_SECONDS": 120,
    "CRM_TRANSACTIONAL_LATENCY_HOLD_MINUTES": 15,
    "CRM_TRANSACTIONAL_EVENT_LOOKBACK_MINUTES": 60,
    "CRM_LIFECYCLE_BATCH_SIZE": 500,
    "CRM_LIFECYCLE_PASS_LOCK_SECONDS": 90,
    "CRM_IMPORT_DOMAIN_MAX_NEW": 8,
    "CRM_RECONFIRMATION_RESPONSE_DAYS": 30,
    "CRM_RECONFIRMATION_CONSENT_MONTHS": 36,
    "CRM_RECONFIRMATION_DAILY_FRACTION": 0.1,
    "CRM_GUARD_COHORT_MIN_COUNT": 5,
    "CRM_GUARD_COHORT_FACTOR": 3,
    "CRM_GUARD_CLICK_MIN_SAMPLE": 500,
    "CRM_GUARD_CLICK_RATIO": 0.3,
    "CRM_GUARD_CLICK_BASELINE_DAYS": 3,
    "CRM_GUARD_CLICK_LOOKBACK_DAYS": 30,
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
    "CRM_PREFLIGHT_ENABLED": False,
    "CRM_PREFLIGHT_MAX_AGE_HOURS": 24,
    "CRM_SUBJECT_MIN_LENGTH": 10,
    "CRM_SUBJECT_MAX_LENGTH": 70,
    "CRM_SUBJECT_CAPS_RATE": 0.3,
    "CRM_ALLOWED_ACRONYMS": ('mlai', 'ai', 'ml', 'au'),
    "CRM_BODY_MIN_WORDS": 50,
    "CRM_BODY_MAX_WORDS": 1500,
    "CRM_HTML_WARN_KB": 80,
    "CRM_HTML_BLOCK_KB": 100,
    "CRM_LINKS_WARN": 10,
    "CRM_LINKS_BLOCK": 25,
    "CRM_LINK_TIMEOUT_SECONDS": 5,
    "CRM_LINK_WORKERS": 10,
    "CRM_LINK_MAX_REDIRECTS": 5,
    "CRM_LINK_MAX_LENGTH": 2048,
    "CRM_SPAM_WARN_SCORE": 3.0,
    "CRM_SPAM_BLOCK_SCORE": 5.0,
    "CRM_LONG_SEND_DAYS": 3,
    "CRM_DUPLICATE_CONTENT_DAYS": 14,
    "CRM_PREFLIGHT_FEEDBACK_HOURS": 24,
    "CRM_WEBHOOK_TEST_MAX_AGE_DAYS": 7,
    "CRM_PREFLIGHT_POSTMASTER_WARN": 0.001,
    "CRM_PREFLIGHT_POSTMASTER_BLOCK": 0.0015,
    "CRM_POSTMASTER_MAX_AGE_DAYS": 4,
    "CRM_TEMPLATE_CACHE_SECONDS": 300,
    "CRM_FOOTER_CONTACT_ADDRESS": 'hi@mlai.au',
    "RSPAMD_URL": 'http://rspamd:11333',
    "CRM_SPAMCHECK_TIMEOUT_SECONDS": 10,
    "CRM_SEED_TIMEOUT_MINUTES": 15,
    "CRM_SEED_POLL_SECONDS": 55,
    "CRM_SEED_IMAP_TIMEOUT_SECONDS": 5,
    "CRM_SEED_MAX_MESSAGE_BYTES": 262144,
    "CRM_SEED_MAX_MATCHES": 10,
    "CRM_SEED_CANARY_FACTOR": 0.5,
    "CRM_SEED_SPAM_BLOCK_PROVIDERS": 2,
    "CRM_SEED_MAILBOXES": '',
    "CRM_LISTMONK_MAX_RATE": 2,
    "CRM_LISTMONK_PUBLIC_URL": 'https://lists.mlai.au',
    "CRM_CONFIG_CHECK_SECONDS": 3600,
    "CRM_REPUTATION_MAX_AGE_HOURS": 24,
    "CRM_MONITORS_ENABLED": False,
    "CRM_SEED_HEARTBEAT_ENABLED": False,
    "CRM_DELIVERABILITY_EMAIL_ALERTS_ENABLED": False,
    "CRM_MONITOR_INTERVAL_SECONDS": 55,
    "CRM_MONITOR_PASS_SECONDS": 35,
    "CRM_DNS_CHECK_SECONDS": 600,
    "CRM_DNS_RECOVERY_PASSES": 2,
    "CRM_DNS_FAILURE_CHECKS": 2,
    "CRM_GUARD_UNSUBSCRIBE_MIN_SAMPLE": 200,
    "CRM_GUARD_UNSUBSCRIBE_AMBER": 0.01,
    "CRM_GUARD_UNSUBSCRIBE_RED": 0.025,
    "CRM_GUARD_DEFERRAL_MIN_SAMPLE": 50,
    "CRM_GUARD_DEFERRAL_AMBER": 0.05,
    "CRM_GUARD_DEFERRAL_RED": 0.2,
    "CRM_GUARD_PROVIDER_RATE_LIMIT_COUNT": 5,
    "CRM_GUARD_SLOW_CLEAN_HOURS": 24,
    "CRM_GUARD_PROVIDER_CLEAN_HOURS": 6,
    "CRM_GUARD_PROVIDER_HOLD_HOURS": 24,
    "CRM_GUARD_POSTMASTER_AMBER": 0.0008,
    "CRM_GUARD_POSTMASTER_RED": 0.0015,
    "CRM_GUARD_ROOT_SPAM_RATE": 0.001,
    "CRM_GUARD_POSTMASTER_SLOW_HOURS": 48,
    "CRM_GUARD_FEEDBACK_MIN_SAMPLE": 50,
    "CRM_GUARD_FEEDBACK_MIN_AGE_MINUTES": 30,
    "CRM_GUARD_FEEDBACK_LOOKBACK_HOURS": 2,
    "CRM_GUARD_RATE_LIMIT_HOURS": 2,
    "CRM_MONITOR_HISTORY_DAYS": 90,
    "CRM_MONITOR_HTTP_SECONDS": 10,
    "CRM_MONITOR_MAX_PAGES": 20,
    "CRM_REPUTATION_MAX_SUBJECTS": 100,
    "CRM_REPUTATION_DNS_SECONDS": 3,
    "CRM_REPUTATION_NAMESERVERS": (),
    "CRM_URIBL_ENABLED": False,
    "CRM_SURBL_ENABLED": False,
    "CRM_SAFE_BROWSING_NONCOMMERCIAL": False,
    "CRM_POSTMASTER_HOURS": (10, 16),
    "CRM_DIGEST_HOUR": 8,
    "CRM_DIGEST_MINUTE": 30,
    "CRM_SEED_HEARTBEAT_HOUR": 8,
    "CRM_SEED_HEARTBEAT_MINUTE": 45,
}

# Only numeric thresholds and sending windows can be overridden by an owner.
# Feature switches, protected identities and secret references remain deployment configuration.
OVERRIDABLE = frozenset(name for name in DEFAULTS if name.startswith('CRM_GUARD_')
    and name.endswith(('_SAMPLE', '_COUNT', '_AMBER', '_RED', '_HIGH'))
    and type(DEFAULTS[name]) in (int, float)) | {'CRM_SEND_WINDOW', 'CRM_EVENT_SEND_WINDOW'}
SECRET_KEYS = ('POSTMASTER_OAUTH_CLIENT_ID', 'POSTMASTER_OAUTH_CLIENT_SECRET',
    'POSTMASTER_OAUTH_REFRESH_TOKEN', 'SPAMHAUS_DQS_KEY', 'SAFE_BROWSING_API_KEY',
    'WEB_RISK_API_KEY', 'RESEND_READ_API_KEY', 'CRM_NOTIFY_API_KEY')

def value(name, stream=None):
    """Read at use time so overrides and tests share the same policy."""
    from django.conf import settings
    result = getattr(settings, name, DEFAULTS[name])
    overrides = getattr(stream, "policy_overrides", None) or {}
    if name in OVERRIDABLE and name in overrides:
        environment_settings({name: str(overrides[name])})
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
                if (not pieces or any(item < (0 if name == 'CRM_POSTMASTER_HOURS' else 1) for item in pieces)
                        or (name == 'CRM_POSTMASTER_HOURS' and any(item > 23 for item in pieces))
                        or any(a >= b for a, b in zip(pieces, pieces[1:]))):
                    raise ValueError(f"{name} must contain positive increasing integers")
            result[name] = pieces
        elif isinstance(default, int):
            try:
                result[name] = int(raw)
            except (ValueError, TypeError):
                raise ValueError(f"{name} must be an integer") from None
            if name in ('CRM_DIGEST_HOUR', 'CRM_SEED_HEARTBEAT_HOUR'):
                if not 0 <= result[name] <= 23:
                    raise ValueError(f"{name} must be a local hour")
            elif name in ('CRM_DIGEST_MINUTE', 'CRM_SEED_HEARTBEAT_MINUTE'):
                if not 0 <= result[name] <= 59:
                    raise ValueError(f"{name} must be a local minute")
            elif result[name] < 1:
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
