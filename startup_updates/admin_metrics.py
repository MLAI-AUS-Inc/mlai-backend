"""Aggregate adoption metrics for AI-assisted monthly startup updates.

This module intentionally reads only the startup-update connector models and
workflow. Vibe Marketing's GitHub/content-factory connections are separate and
must not be included in these counts.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from django.contrib.auth import get_user_model
from django.db.models import Exists, OuterRef
from django.utils import timezone

from integrations.models import (
    ExternalServiceConnection,
    ExternalServiceConnectionStatus,
    GoogleConnection,
)
from integrations.services.external_connectors import (
    CONNECTOR_DEFINITIONS,
    EXTERNAL_PROVIDER_ORDER,
)
from integrations.services.gmail_scopes import has_gmail_read_scope
from startup_updates.models import MonthlyUpdateDraft, UserStartupBinding
from startup_updates.services import STARTUP_UPDATE_WORKFLOW


CONNECTED_SOURCE_STATUSES = (
    ExternalServiceConnectionStatus.CONNECTED,
    ExternalServiceConnectionStatus.SYNCING,
)


def _startup_bound_google_connections():
    matching_binding = UserStartupBinding.objects.filter(
        user_id=OuterRef("user_id"),
        organization_id=OuterRef("organization_id"),
    )
    return (
        GoogleConnection.objects.filter(organization__isnull=False)
        .annotate(has_startup_binding=Exists(matching_binding))
        .filter(has_startup_binding=True)
    )


def _startup_bound_external_connections():
    matching_binding = UserStartupBinding.objects.filter(
        user_id=OuterRef("user_id"),
        organization_id=OuterRef("organization_id"),
    )
    return (
        ExternalServiceConnection.objects.filter(
            organization__isnull=False,
            provider__in=EXTERNAL_PROVIDER_ORDER,
            status__in=CONNECTED_SOURCE_STATUSES,
        )
        .annotate(has_startup_binding=Exists(matching_binding))
        .filter(has_startup_binding=True)
    )


def _connected_user_ids_by_source() -> dict[str, set[int]]:
    user_ids_by_source: dict[str, set[int]] = defaultdict(set)

    # GoogleConnection is shared with Vibe Marketing's website-baseline flow.
    # Requiring the exact Gmail read scope keeps Search Console / GA-only
    # marketing connections out of the monthly-update adoption metric.
    for user_id, scope in _startup_bound_google_connections().values_list(
        "user_id", "scope"
    ):
        if has_gmail_read_scope(scope):
            user_ids_by_source["gmail"].add(user_id)

    for provider, user_id in _startup_bound_external_connections().values_list(
        "provider", "user_id"
    ).distinct():
        user_ids_by_source[provider].add(user_id)

    return user_ids_by_source


def _ai_assisted_update_user_ids() -> set[int]:
    """Users with at least one draft produced by the startup-update workflow."""

    run_rows = (
        MonthlyUpdateDraft.objects.filter(run__workflow=STARTUP_UPDATE_WORKFLOW)
        .values_list("run__run_request__binding_id", "run__slack_user_id")
        .distinct()
    )

    binding_ids: set[int] = set()
    legacy_user_ids: set[int] = set()
    for raw_binding_id, raw_user_id in run_rows:
        try:
            if raw_binding_id is not None:
                binding_ids.add(int(raw_binding_id))
                continue
        except (TypeError, ValueError):
            pass

        # startup_monthly_update runs historically stored the owning Django
        # user id in slack_user_id. Keep that as a fallback for runs created
        # before run_request.binding_id was recorded.
        try:
            legacy_user_ids.add(int(str(raw_user_id or "").strip()))
        except (TypeError, ValueError):
            continue

    binding_user_ids = set(
        UserStartupBinding.objects.filter(id__in=binding_ids).values_list(
            "user_id", flat=True
        )
    )
    existing_legacy_user_ids = set(
        get_user_model().objects.filter(id__in=legacy_user_ids).values_list(
            "id", flat=True
        )
    )
    return binding_user_ids | existing_legacy_user_ids


def build_monthly_update_usage_payload() -> dict[str, Any]:
    user_ids_by_source = _connected_user_ids_by_source()
    connected_user_ids: set[int] = (
        set().union(*user_ids_by_source.values()) if user_ids_by_source else set()
    )
    ai_assisted_user_ids = _ai_assisted_update_user_ids()

    provider_order = ("gmail", *EXTERNAL_PROVIDER_ORDER)
    sources = [
        {
            "provider": provider,
            "label": CONNECTOR_DEFINITIONS[provider].label,
            "users": len(user_ids_by_source.get(provider, set())),
        }
        for provider in provider_order
    ]

    return {
        "connectedSourceUsers": len(connected_user_ids),
        "aiAssistedUpdateUsers": len(ai_assisted_user_ids),
        "connectedAndAiAssistedUsers": len(connected_user_ids & ai_assisted_user_ids),
        "sources": sources,
        "asOf": timezone.now().isoformat(),
    }
