from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from django.utils import timezone

from org_memory.governance import SUPPORTED_PROVIDERS, configured_enabled_providers
from org_memory.models import MemoryProviderEnablement

from .base import (
    ConnectorExecutionDeferred,
    ConnectorHealth,
    DryRunResult,
    MemoryConnector,
    ScopeDescriptor,
    ScopePage,
    SourcePreview,
)


@dataclass(frozen=True)
class ProviderDefinition:
    key: str
    label: str
    default_scope_type: str
    supports_webhooks: bool
    structured_aggregates_only: bool = False


PROVIDER_DEFINITIONS = {
    "google_drive": ProviderDefinition("google_drive", "Google Drive", "folder", True),
    "slack": ProviderDefinition("slack", "Slack", "channel", True),
    "linear": ProviderDefinition("linear", "Linear", "project", True),
    "notion": ProviderDefinition("notion", "Notion", "page_root", True),
    "gmail": ProviderDefinition("gmail", "Gmail", "label", True),
    "stripe": ProviderDefinition("stripe", "Stripe", "aggregate", True, True),
    "xero": ProviderDefinition("xero", "Xero", "aggregate", True, True),
    "luma": ProviderDefinition("luma", "Luma", "event", False, True),
}


class MetadataOnlyMemoryConnector:
    """Safe control-plane adapter until provider ingestion lands in later PRs."""

    def __init__(self, definition: ProviderDefinition):
        self.definition = definition
        self.provider = definition.key

    def discover_scopes(self, configuration, cursor=None) -> ScopePage:
        connection = configuration.connection
        external_id = str(
            getattr(connection, "external_account_id", "")
            or getattr(connection, "google_email", "")
            or f"connection:{configuration.pk}"
        )
        label = str(
            getattr(connection, "account_label", "")
            or getattr(connection, "google_email", "")
            or self.definition.label
        )
        return ScopePage(
            scopes=(
                ScopeDescriptor(
                    scope_type=self.definition.default_scope_type,
                    external_id=external_id,
                    name=label,
                    metadata={"discovery_mode": "account_metadata_only"},
                ),
            ),
            warnings=(
                "Provider-specific child-scope discovery is deferred to its ingestion adapter.",
            ),
        )

    def preview(self, configuration, selected_scopes, policy) -> SourcePreview:
        classifications = sorted(
            {scope.default_classification for scope in selected_scopes}
        )
        return SourcePreview(
            summary={
                "scope_count": len(selected_scopes),
                "classifications": classifications,
                "record_count": None,
                "unsupported_count": None,
                "estimated_tokens": None,
                "estimated_cost_aud": None,
                "review_volume": None,
                "content_activated": False,
            },
            warnings=(
                "This metadata-only preview does not fetch or activate source content.",
            ),
        )

    def dry_run(self, configuration, selected_scopes, policy) -> DryRunResult:
        return DryRunResult(
            summary={
                "scope_count": len(selected_scopes),
                "sample_artifacts": 0,
                "sample_chunks": 0,
                "sample_claims": 0,
                "active_memory_created": False,
            },
            warnings=(
                "Provider sample extraction is deferred until the provider adapter is installed.",
            ),
        )

    def _deferred(self):
        raise ConnectorExecutionDeferred(
            f"{self.provider} execution requires its reviewed ingestion adapter."
        )

    def backfill(self, configuration, selected_scopes, checkpoint):
        return self._deferred()

    def incremental_sync(self, configuration, cursor):
        return self._deferred()

    def refresh_permissions(self, configuration, checkpoint):
        return self._deferred()

    def fetch_version(self, configuration, external_id):
        return self._deferred()

    def tombstone_missing(self, configuration, sync_run):
        return self._deferred()

    def health(self, configuration) -> ConnectorHealth:
        connection = configuration.connection
        connection_status = str(getattr(connection, "status", "connected") or "connected")
        last_sync = configuration.last_successful_sync_at or getattr(
            connection, "last_synced_at", None
        )
        lag = None
        if last_sync:
            lag = max(int((timezone.now() - last_sync).total_seconds()), 0)
        credential_status = "connected"
        if connection_status in {"error", "disconnected"}:
            credential_status = connection_status
        return ConnectorHealth(
            status=configuration.lifecycle_state,
            credential_status=credential_status,
            last_successful_sync_at=last_sync.isoformat() if last_sync else None,
            source_lag_seconds=lag,
            details={"connection_status": connection_status},
        )


class MemoryConnectorRegistry:
    def __init__(self):
        self._connectors: dict[str, MemoryConnector] = {}
        for definition in PROVIDER_DEFINITIONS.values():
            if definition.key == "google_drive":
                from .google_drive import GoogleDriveMemoryConnector

                self.register(GoogleDriveMemoryConnector())
            elif definition.key == "linear":
                from .linear import LinearArtifactMemoryConnector

                self.register(LinearArtifactMemoryConnector())
            elif definition.key == "slack":
                from .slack import SlackArtifactMemoryConnector

                self.register(SlackArtifactMemoryConnector())
            elif definition.key == "notion":
                from .notion import NotionMemoryConnector

                self.register(NotionMemoryConnector())
            elif definition.key == "gmail":
                from .gmail import GmailMemoryConnector

                self.register(GmailMemoryConnector())
            elif definition.key in {"stripe", "xero", "luma"}:
                from .structured_aggregates import StructuredAggregateMemoryConnector

                self.register(StructuredAggregateMemoryConnector(definition.key))
            else:
                self.register(MetadataOnlyMemoryConnector(definition))

    def register(self, connector: MemoryConnector, *, replace: bool = False):
        provider = str(connector.provider).strip().lower()
        if provider not in SUPPORTED_PROVIDERS:
            raise ValueError(f"Unsupported organisational-memory provider: {provider}")
        if provider in self._connectors and not replace:
            raise ValueError(f"Connector is already registered: {provider}")
        self._connectors[provider] = connector

    def get(self, provider: str) -> MemoryConnector:
        provider = str(provider).strip().lower()
        try:
            return self._connectors[provider]
        except KeyError as exc:
            raise ValueError(f"Connector is not registered: {provider}") from exc

    def providers(self) -> Iterable[str]:
        return tuple(sorted(self._connectors))

    def definition(self, provider: str) -> ProviderDefinition:
        try:
            return PROVIDER_DEFINITIONS[str(provider).strip().lower()]
        except KeyError as exc:
            raise ValueError(f"Provider is not registered: {provider}") from exc

    def validate_conformance(self, provider: str) -> list[str]:
        connector = self.get(provider)
        required = (
            "discover_scopes",
            "preview",
            "dry_run",
            "backfill",
            "incremental_sync",
            "refresh_permissions",
            "fetch_version",
            "tombstone_missing",
            "health",
        )
        return [name for name in required if not callable(getattr(connector, name, None))]

    def enablement(self, organization, provider: str) -> dict:
        provider = str(provider).strip().lower()
        registered = provider in self._connectors
        deployment_enabled = provider in configured_enabled_providers()
        row = MemoryProviderEnablement.objects.filter(
            organization=organization,
            provider=provider,
        ).first()
        organization_enabled = bool(
            row and row.is_enabled and row.approved_by_id and row.approved_at
        )
        enabled = registered and deployment_enabled and organization_enabled
        reasons = []
        if not registered:
            reasons.append("connector_not_registered")
        if not deployment_enabled:
            reasons.append("deployment_feature_flag_disabled")
        if not organization_enabled:
            reasons.append("organization_feature_flag_disabled")
        return {
            "registered": registered,
            "deployment_enabled": deployment_enabled,
            "organization_enabled": organization_enabled,
            "enabled": enabled,
            "reasons": reasons,
        }


connector_registry = MemoryConnectorRegistry()
