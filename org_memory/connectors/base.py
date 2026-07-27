from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable


class ConnectorExecutionDeferred(RuntimeError):
    """Raised when a provider's production ingestion adapter is not installed."""


@dataclass(frozen=True)
class ScopeDescriptor:
    scope_type: str
    external_id: str
    name: str
    canonical_url: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScopePage:
    scopes: Sequence[ScopeDescriptor]
    next_cursor: Optional[str] = None
    warnings: Sequence[str] = field(default_factory=tuple)


@dataclass(frozen=True)
class SourcePreview:
    summary: Mapping[str, Any]
    warnings: Sequence[str] = field(default_factory=tuple)


@dataclass(frozen=True)
class DryRunResult:
    summary: Mapping[str, Any]
    warnings: Sequence[str] = field(default_factory=tuple)


@dataclass(frozen=True)
class SyncPage:
    records: Sequence[Mapping[str, Any]]
    removals: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    next_cursor: Optional[str] = None
    checkpoint: Mapping[str, Any] = field(default_factory=dict)
    rate_limit: Mapping[str, Any] = field(default_factory=dict)
    retryable: bool = False
    has_more: bool = False


@dataclass(frozen=True)
class SourceVersionPayload:
    external_id: str
    canonical_url: str
    version_key: str
    source_times: Mapping[str, Any]
    metadata: Mapping[str, Any]
    acl: Mapping[str, Any]
    content: Any


@dataclass(frozen=True)
class TombstoneResult:
    tombstoned_external_ids: Sequence[str]


@dataclass(frozen=True)
class ConnectorHealth:
    status: str
    credential_status: str
    last_successful_sync_at: Optional[str]
    source_lag_seconds: Optional[int]
    details: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class MemoryConnector(Protocol):
    provider: str

    def discover_scopes(self, configuration, cursor=None) -> ScopePage: ...

    def preview(self, configuration, selected_scopes, policy) -> SourcePreview: ...

    def dry_run(self, configuration, selected_scopes, policy) -> DryRunResult: ...

    def backfill(self, configuration, selected_scopes, checkpoint) -> SyncPage: ...

    def incremental_sync(self, configuration, cursor) -> SyncPage: ...

    def refresh_permissions(self, configuration, checkpoint) -> SyncPage: ...

    def fetch_version(self, configuration, external_id) -> SourceVersionPayload: ...

    def tombstone_missing(self, configuration, sync_run) -> TombstoneResult: ...

    def health(self, configuration) -> ConnectorHealth: ...
