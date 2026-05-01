from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.checks import Error, register

from jobs.conf import settings


def _scheduler_config_errors() -> list[str]:
    errors: list[str] = []

    try:
        ZoneInfo(settings.jobs_schedule_timezone.strip() or "Australia/Melbourne")
    except ZoneInfoNotFoundError:
        errors.append(f"Invalid jobs schedule timezone: {settings.jobs_schedule_timezone}")

    if not 0 <= settings.jobs_schedule_hour <= 23:
        errors.append("JOBS_SCHEDULE_HOUR must be between 0 and 23")
    if not 0 <= settings.jobs_schedule_minute <= 59:
        errors.append("JOBS_SCHEDULE_MINUTE must be between 0 and 59")
    if settings.jobs_retry_attempts < 1:
        errors.append("JOBS_RETRY_ATTEMPTS must be at least 1")
    if settings.jobs_retry_delay_seconds < 0:
        errors.append("JOBS_RETRY_DELAY_SECONDS must be 0 or greater")
    if settings.jobs_failure_stop_after_days < 1:
        errors.append("JOBS_FAILURE_STOP_AFTER_DAYS must be at least 1")
    if settings.jobs_scheduler_max_pages < 1:
        errors.append("JOBS_SCHEDULER_MAX_PAGES must be at least 1")
    if settings.jobs_scheduler_per_keyword_limit < 1:
        errors.append("JOBS_SCHEDULER_PER_KEYWORD_LIMIT must be at least 1")

    return errors


@register()
def jobs_scheduler_configuration_check(app_configs, **kwargs):
    del app_configs, kwargs

    if not settings.jobs_scheduler_enabled:
        return []

    return [
        Error(
            message,
            id="jobs.E001",
        )
        for message in _scheduler_config_errors()
    ]
