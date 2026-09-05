"""Durable user-level generation fence for Slack OAuth callbacks."""

from __future__ import annotations

from collections.abc import Iterable

from integrations.models import (
    ExternalServiceConnection,
    ExternalServiceConnectionStatus,
    ExternalServiceProvider,
)

SLACK_OAUTH_GENERATION_KEY = "mlai_slack_oauth_generation"
SLACK_OAUTH_FENCE_ACCOUNT_ID = "__mlai_slack_oauth_fence__"


def connection_slack_oauth_generation(
    connection: ExternalServiceConnection,
) -> int:
    """Return a connection's user-level Slack consent generation."""

    raw = (connection.provider_metadata or {}).get(SLACK_OAUTH_GENERATION_KEY, 0)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def current_slack_oauth_generation(user_id: int, *, for_update: bool = False) -> int:
    """Read the highest durable Slack disconnect generation for one user."""

    queryset = ExternalServiceConnection.objects.filter(
        user_id=user_id,
        provider=ExternalServiceProvider.SLACK,
    ).order_by("id")
    if for_update:
        queryset = queryset.select_for_update()
    return max(
        (connection_slack_oauth_generation(connection) for connection in queryset),
        default=0,
    )


def stamp_slack_oauth_generation(
    metadata: dict | None,
    generation: int,
) -> dict:
    """Copy provider metadata and attach the non-secret consent generation."""

    stamped = dict(metadata or {})
    stamped[SLACK_OAUTH_GENERATION_KEY] = max(0, int(generation))
    return stamped


def advance_slack_oauth_generation_locked(
    user,
    *,
    connections: Iterable[ExternalServiceConnection] | None = None,
) -> int:
    """Advance the disconnect fence while the caller holds the user row lock."""

    locked_connections = (
        list(connections)
        if connections is not None
        else list(
            ExternalServiceConnection.objects.select_for_update()
            .filter(user_id=user.pk, provider=ExternalServiceProvider.SLACK)
            .order_by("id")
        )
    )
    generation = (
        max(
            (
                connection_slack_oauth_generation(connection)
                for connection in locked_connections
            ),
            default=0,
        )
        + 1
    )
    if not locked_connections:
        ExternalServiceConnection.objects.create(
            user=user,
            provider=ExternalServiceProvider.SLACK,
            external_account_id=SLACK_OAUTH_FENCE_ACCOUNT_ID,
            account_label="Slack disconnect fence",
            status=ExternalServiceConnectionStatus.DISCONNECTED,
            provider_metadata=stamp_slack_oauth_generation({}, generation),
        )
        return generation
    for connection in locked_connections:
        connection.provider_metadata = stamp_slack_oauth_generation(
            connection.provider_metadata,
            generation,
        )
        connection.save(update_fields=("provider_metadata", "updated_at"))
    return generation
