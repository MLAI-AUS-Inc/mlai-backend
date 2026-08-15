"""Cached, read-only catalog of active Xero tracking options."""

from __future__ import annotations

from typing import Any

from django.core.cache import cache

from integrations import http_client
from integrations.models import ReconciliationProfile
from integrations.services.xero_reconciliation import XERO_API_URL, _xero_headers


def active_xero_project_options(*, organization, force_refresh: bool = False) -> list[dict[str, str]]:
    profile = ReconciliationProfile.objects.select_related("xero_connection").filter(
        organization=organization
    ).first()
    if not profile or not profile.xero_connection or not profile.project_tracking_category_id:
        return []
    cache_key = f"reconciliation:xero-project-options:{organization.id}:{profile.updated_at.timestamp()}"
    fallback_key = f"reconciliation:xero-project-options:{organization.id}:last-good"
    if not force_refresh:
        cached = cache.get(cache_key)
        if isinstance(cached, list):
            return cached

    try:
        response = http_client.get(
            f"{XERO_API_URL}/TrackingCategories",
            headers=_xero_headers(profile.xero_connection),
            timeout=(3, 10),
        )
        response.raise_for_status()
    except Exception:
        fallback = cache.get(fallback_key)
        if isinstance(fallback, list):
            return fallback
        raise
    categories = response.json().get("TrackingCategories", [])
    category = next((
        item for item in categories
        if str(item.get("TrackingCategoryID") or "") == profile.project_tracking_category_id
        and str(item.get("Status") or "ACTIVE").upper() == "ACTIVE"
    ), None)
    options: list[dict[str, str]] = []
    for option in (category or {}).get("Options") or []:
        if str(option.get("Status") or "ACTIVE").upper() != "ACTIVE":
            continue
        option_id = str(option.get("TrackingOptionID") or option.get("OptionID") or "").strip()
        name = str(option.get("Name") or "").strip()
        if option_id and name:
            options.append({
                "source_type": "xero_tracking",
                "source_id": option_id,
                "tracking_option_id": option_id,
                "name": name,
            })
    options.sort(key=lambda item: (item["name"].casefold(), item["source_id"]))
    cache.set(cache_key, options, timeout=900)
    cache.set(fallback_key, options, timeout=86400)
    return options
