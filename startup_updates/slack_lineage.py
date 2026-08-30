"""Exact Slack connector lineage for startup-update runs.

Slack channel and timestamp identifiers are only unique inside one workspace
credential.  A run therefore pins the concrete connection and its durable
OAuth generation, and every worker-visible thread id carries that same
authority.  This keeps late classifier/extractor callbacks from being applied
to a replacement Slack identity that happens to reuse channel ids.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from integrations.models import ExternalServiceConnection
from integrations.services.slack_oauth_authority import (
    connection_slack_oauth_generation,
)

SLACK_LINEAGE_VERSION = 2
SLACK_RUN_AUTHORITY_KEY = "authority"


@dataclass(frozen=True)
class SlackRunAuthority:
    """Non-secret durable identity of one Slack connector authority."""

    user_id: int
    organization_id: int
    connection_id: int
    oauth_generation: int

    def as_dict(self) -> dict[str, int]:
        return {
            "version": SLACK_LINEAGE_VERSION,
            "user_id": self.user_id,
            "organization_id": self.organization_id,
            "connection_id": self.connection_id,
            "oauth_generation": self.oauth_generation,
        }

    @property
    def public_id_prefix(self) -> str:
        return (
            f"slack:v{SLACK_LINEAGE_VERSION}:"
            f"{self.connection_id}:{self.oauth_generation}:"
        )

    def thread_public_id(self, channel_id: str, thread_ts: str) -> str:
        return f"{self.public_id_prefix}{channel_id}:{thread_ts}"


def slack_run_authority_for_connection(
    connection: ExternalServiceConnection,
) -> SlackRunAuthority:
    if connection.organization_id is None:
        raise ValueError("Slack connection is not linked to an organization.")
    return SlackRunAuthority(
        user_id=connection.user_id,
        organization_id=connection.organization_id,
        connection_id=connection.pk,
        oauth_generation=connection_slack_oauth_generation(connection),
    )


def slack_run_authority_from_request(
    run_request: dict[str, Any] | None,
) -> Optional[SlackRunAuthority]:
    external_context = (run_request or {}).get("external_context") or {}
    slack_context = (
        external_context.get("slack") if isinstance(external_context, dict) else None
    )
    raw = (
        slack_context.get(SLACK_RUN_AUTHORITY_KEY)
        if isinstance(slack_context, dict)
        else None
    )
    if not isinstance(raw, dict) or raw.get("version") != SLACK_LINEAGE_VERSION:
        return None
    try:
        authority = SlackRunAuthority(
            user_id=int(raw["user_id"]),
            organization_id=int(raw["organization_id"]),
            connection_id=int(raw["connection_id"]),
            oauth_generation=int(raw["oauth_generation"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
    if min(
        authority.user_id,
        authority.organization_id,
        authority.connection_id,
    ) <= 0 or authority.oauth_generation < 0:
        return None
    return authority


def slack_run_authority_is_present(run_request: dict[str, Any] | None) -> bool:
    external_context = (run_request or {}).get("external_context") or {}
    slack_context = (
        external_context.get("slack") if isinstance(external_context, dict) else None
    )
    return isinstance(slack_context, dict) and SLACK_RUN_AUTHORITY_KEY in slack_context


def pin_slack_run_authority(
    run_request: dict[str, Any] | None,
    authority: SlackRunAuthority,
) -> dict[str, Any]:
    updated = dict(run_request or {})
    external_context = dict(updated.get("external_context") or {})
    slack_context = dict(external_context.get("slack") or {})
    slack_context[SLACK_RUN_AUTHORITY_KEY] = authority.as_dict()
    external_context["slack"] = slack_context
    updated["external_context"] = external_context
    return updated


def parse_slack_thread_public_id(
    value: str,
) -> Optional[tuple[int, int, str, str]]:
    parts = str(value or "").split(":", 5)
    if (
        len(parts) != 6
        or parts[0] != "slack"
        or parts[1] != f"v{SLACK_LINEAGE_VERSION}"
        or not parts[4]
        or not parts[5]
    ):
        return None
    try:
        connection_id = int(parts[2])
        oauth_generation = int(parts[3])
    except (TypeError, ValueError):
        return None
    if connection_id <= 0 or oauth_generation < 0:
        return None
    return connection_id, oauth_generation, parts[4], parts[5]


def slack_thread_public_id_matches_authority(
    value: str,
    authority: SlackRunAuthority,
) -> Optional[tuple[str, str]]:
    parsed = parse_slack_thread_public_id(value)
    if parsed is None:
        return None
    connection_id, oauth_generation, channel_id, thread_ts = parsed
    if (
        connection_id != authority.connection_id
        or oauth_generation != authority.oauth_generation
    ):
        return None
    return channel_id, thread_ts
