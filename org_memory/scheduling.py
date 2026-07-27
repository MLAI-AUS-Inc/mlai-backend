from __future__ import annotations

import json
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.utils import timezone


class MemoryScheduleConfigurationError(ValueError):
    pass


def _positive_integer(value, *, name: str, default: int) -> int:
    raw = default if value in (None, "") else value
    try:
        normalized = int(raw)
    except (TypeError, ValueError) as exc:
        raise MemoryScheduleConfigurationError(f"{name} must be an integer.") from exc
    if normalized < 1:
        raise MemoryScheduleConfigurationError(f"{name} must be positive.")
    return normalized


def _provider_mapping(setting_name: str) -> dict:
    value = getattr(settings, setting_name, {})
    if isinstance(value, str):
        try:
            value = json.loads(value or "{}")
        except json.JSONDecodeError as exc:
            raise MemoryScheduleConfigurationError(
                f"{setting_name} must be a JSON object."
            ) from exc
    if not isinstance(value, dict):
        raise MemoryScheduleConfigurationError(f"{setting_name} must be a mapping.")
    return value


def provider_sync_interval_seconds(provider: str, *, configuration=None) -> int:
    configuration_value = None
    if configuration is not None and isinstance(configuration.configuration, dict):
        configuration_value = configuration.configuration.get("sync_interval_seconds")
    provider_value = _provider_mapping(
        "ORG_MEMORY_PROVIDER_SYNC_INTERVAL_SECONDS"
    ).get(str(provider))
    default = _positive_integer(
        getattr(settings, "ORG_MEMORY_SYNC_INTERVAL_SECONDS", 86400),
        name="ORG_MEMORY_SYNC_INTERVAL_SECONDS",
        default=86400,
    )
    return _positive_integer(
        configuration_value if configuration_value not in (None, "") else provider_value,
        name=f"sync interval for {provider}",
        default=default,
    )


def provider_freshness_slo_seconds(provider: str, *, configuration=None) -> int:
    configuration_value = None
    if configuration is not None and isinstance(configuration.configuration, dict):
        configuration_value = configuration.configuration.get("freshness_slo_seconds")
    provider_value = _provider_mapping(
        "ORG_MEMORY_PROVIDER_FRESHNESS_SLO_SECONDS"
    ).get(str(provider))
    default = _positive_integer(
        getattr(settings, "ORG_MEMORY_FRESHNESS_SLO_SECONDS", 86400),
        name="ORG_MEMORY_FRESHNESS_SLO_SECONDS",
        default=86400,
    )
    return _positive_integer(
        configuration_value if configuration_value not in (None, "") else provider_value,
        name=f"freshness SLO for {provider}",
        default=default,
    )


def reconciliation_timezone() -> ZoneInfo:
    name = str(
        getattr(
            settings,
            "ORG_MEMORY_DAILY_RECONCILIATION_TIME_ZONE",
            "Australia/Sydney",
        )
        or ""
    ).strip()
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise MemoryScheduleConfigurationError(
            "ORG_MEMORY_DAILY_RECONCILIATION_TIME_ZONE is invalid."
        ) from exc


def reconciliation_window(now=None):
    now = now or timezone.now()
    if timezone.is_naive(now):
        now = timezone.make_aware(now, timezone.utc)
    target_timezone = reconciliation_timezone()
    local_now = now.astimezone(target_timezone)
    hour = _positive_integer(
        int(getattr(settings, "ORG_MEMORY_DAILY_RECONCILIATION_HOUR", 5)) + 1,
        name="ORG_MEMORY_DAILY_RECONCILIATION_HOUR plus one",
        default=6,
    ) - 1
    if hour > 23:
        raise MemoryScheduleConfigurationError(
            "ORG_MEMORY_DAILY_RECONCILIATION_HOUR must be between 0 and 23."
        )
    local_start = datetime.combine(local_now.date(), time(hour=hour), target_timezone)
    return {
        "due": local_now >= local_start,
        "report_date": local_now.date(),
        "time_zone": target_timezone.key,
        "window_started_at": local_start.astimezone(timezone.utc),
        "next_window_at": (local_start + timedelta(days=1)).astimezone(timezone.utc),
    }
