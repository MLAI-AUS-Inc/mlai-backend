from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime
from typing import Iterable, Mapping, Optional

from django.db.models import Q
from django.utils.dateparse import parse_datetime

from org_memory.models import (
    MemoryConnectionState,
    MemoryScopeStatus,
    MemorySource,
)


ARTIFACT_ADAPTER_SCHEMA_VERSION = "artifact-adapter-v1"


def canonical_hash(value) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def content_hash(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def version_key(payload) -> str:
    return f"{ARTIFACT_ADAPTER_SCHEMA_VERSION}:{canonical_hash(payload)}"


def estimate_tokens(text: str) -> int:
    return max((len(str(text or "")) + 3) // 4, 1)


def bounded_text(value, limit: int = 60000) -> str:
    return str(value or "").strip()[: max(int(limit), 1)]


def parse_source_datetime(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    return parse_datetime(str(value))


def encode_cursor(payload: Mapping) -> str:
    raw = json.dumps(
        dict(payload),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(value: Optional[str], *, kinds: Iterable[str]) -> dict:
    empty = {kind: {"updated_at": "", "pk": 0} for kind in kinds}
    if not value:
        return empty
    try:
        padding = "=" * (-len(value) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode((value + padding).encode("ascii")).decode("utf-8")
        )
    except Exception as exc:
        raise ValueError("Artifact adapter cursor is invalid.") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("Artifact adapter cursor is invalid.")
    positions = payload.get("positions")
    if not isinstance(positions, dict):
        raise ValueError("Artifact adapter cursor is invalid.")
    result = dict(empty)
    for kind in result:
        position = positions.get(kind)
        if not isinstance(position, dict):
            continue
        updated_at = str(position.get("updated_at") or "")
        try:
            pk = max(int(position.get("pk") or 0), 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("Artifact adapter cursor is invalid.") from exc
        if updated_at and parse_datetime(updated_at) is None:
            raise ValueError("Artifact adapter cursor is invalid.")
        result[kind] = {"updated_at": updated_at, "pk": pk}
    return result


def encoded_positions(positions: Mapping[str, Mapping]) -> str:
    return encode_cursor({"version": 1, "positions": positions})


def changed_after(queryset, position: Mapping):
    updated_at = parse_datetime(str(position.get("updated_at") or ""))
    try:
        pk = max(int(position.get("pk") or 0), 0)
    except (TypeError, ValueError):
        pk = 0
    if updated_at is None:
        return queryset
    return queryset.filter(
        Q(updated_at__gt=updated_at) | Q(updated_at=updated_at, pk__gt=pk)
    )


def cursor_position(instance) -> dict:
    return {
        "updated_at": instance.updated_at.isoformat(),
        "pk": int(instance.pk),
    }


def connection_is_accessible(configuration) -> bool:
    connection = configuration.connection
    status = str(getattr(connection, "status", "connected") or "connected")
    return (
        configuration.lifecycle_state
        not in {MemoryConnectionState.DELETE_PENDING, MemoryConnectionState.DELETED}
        and status != "disconnected"
    )


def source_acl(configuration, scope, *, revision_payload: Mapping) -> dict:
    connection = configuration.connection
    accessible = bool(
        connection_is_accessible(configuration)
        and scope.selected
        and scope.status == MemoryScopeStatus.SELECTED
    )
    metadata = dict(scope.metadata or {})
    approved_principals = metadata.get("approved_principal_refs") or []
    approved_groups = metadata.get("approved_group_refs") or []
    if not isinstance(approved_principals, list):
        approved_principals = []
    if not isinstance(approved_groups, list):
        approved_groups = []
    revision = canonical_hash(
        {
            "accessible": accessible,
            "connection_status": str(getattr(connection, "status", "connected") or "connected"),
            "connection_updated_at": getattr(connection, "updated_at", None),
            "configuration_state": configuration.lifecycle_state,
            "scope_id": scope.pk,
            "scope_updated_at": scope.updated_at,
            "classification": scope.default_classification,
            "approved_principal_refs": approved_principals,
            "approved_group_refs": approved_groups,
            "revision": revision_payload,
        }
    )
    return {
        "is_accessible": accessible,
        "provider_revision": revision,
        "principal_refs": [str(value)[:512] for value in approved_principals[:250]],
        "group_refs": [str(value)[:512] for value in approved_groups[:250]],
        "link_sharing": {},
        "metadata": {
            "scope_type": scope.scope_type,
            "scope_external_id": scope.external_id,
            "selected": bool(scope.selected),
        },
    }


def source_removals(configuration, *, expected: set[tuple[str, str]]) -> tuple[dict, ...]:
    removals = []
    sources = MemorySource.objects.filter(
        configuration=configuration,
        provider=configuration.provider,
    ).exclude(lifecycle_state="tombstoned")
    for source_type, external_id in sources.values_list("source_type", "external_id"):
        if (str(source_type), str(external_id)) not in expected:
            removals.append(
                {
                    "source_type": str(source_type),
                    "external_id": str(external_id),
                    "reason": "missing_or_outside_selected_scope",
                }
            )
    return tuple(removals)


def current_positions(kind_querysets: Mapping[str, object]) -> dict:
    positions = {}
    for kind, queryset in kind_querysets.items():
        latest = queryset.order_by("-updated_at", "-pk").first()
        positions[kind] = cursor_position(latest) if latest else {"updated_at": "", "pk": 0}
    return positions
