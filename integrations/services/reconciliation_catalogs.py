"""Stable, PII-minimised fingerprints for reconciliation entity catalogs."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from integrations.models import HumanitixEvent
from startup_updates.models import (
    LinearProjectArtifact,
    LinearProjectSelection,
    LumaEventSelection,
)


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_reconciliation_catalog_status(
    *,
    organization,
    expected_source_hashes: dict[str, str] | None = None,
) -> dict[str, Any]:
    luma = [
        {
            "source_id": item.event_id,
            "name": item.event_name,
            "start_at": item.start_at.isoformat() if item.start_at else None,
            "last_synced_at": (
                item.last_synced_at.isoformat() if item.last_synced_at else None
            ),
        }
        for item in LumaEventSelection.objects.filter(
            organization=organization
        ).order_by("event_id", "id")
    ]
    humanitix = [
        {
            "source_id": item.external_event_id,
            "name": item.event_name,
            "start_at": item.start_at.isoformat() if item.start_at else None,
            "end_at": item.end_at.isoformat() if item.end_at else None,
            "last_synced_at": (
                item.last_synced_at.isoformat() if item.last_synced_at else None
            ),
        }
        for item in HumanitixEvent.objects.filter(
            organization=organization
        ).order_by("external_event_id", "id")
    ]
    linear_by_id: dict[str, dict[str, Any]] = {}
    for item in LinearProjectArtifact.objects.filter(
        organization=organization
    ).prefetch_related("members").order_by("linear_project_id", "id"):
        linear_by_id[item.linear_project_id] = {
            "source_id": item.linear_project_id,
            "name": item.name,
            "status": item.status_name or item.status_type,
            "start_date": item.start_date.isoformat() if item.start_date else None,
            "target_date": item.target_date.isoformat() if item.target_date else None,
            "members": sorted(
                {
                    (member.linear_user_id, member.membership_source)
                    for member in item.members.all()
                    if member.active
                }
            ),
        }
    for item in LinearProjectSelection.objects.filter(
        organization=organization
    ).order_by("linear_project_id", "id"):
        linear_by_id.setdefault(
            item.linear_project_id,
            {
                "source_id": item.linear_project_id,
                "name": item.project_name or item.linear_project_id,
                "status": item.project_status,
                "start_date": None,
                "target_date": None,
                "members": [],
            },
        )
    collections = {
        "luma_events": luma,
        "humanitix_events": humanitix,
        "linear_projects": list(linear_by_id.values()),
    }
    source_hashes = {
        name: _stable_hash(records) for name, records in collections.items()
    }
    expected = {
        str(name): str(value)
        for name, value in (expected_source_hashes or {}).items()
        if name in source_hashes and value
    }
    changed = [
        name for name, value in expected.items() if source_hashes.get(name) != value
    ]
    action_items = [
        {
            "kind": "catalog_sync_required",
            "catalog": name,
            "reason": "catalog_empty",
            "action": f"Sync or select at least one {name.replace('_', ' ')} record.",
        }
        for name, records in collections.items()
        if not records
    ]
    if changed:
        action_items.append(
            {
                "kind": "catalog_refresh_required",
                "catalogs": changed,
                "reason": "catalog_changed_after_run_started",
                "action": "Start a fresh reconciliation run before approving or promoting.",
            }
        )
    return {
        "schema_version": 1,
        "counts": {name: len(records) for name, records in collections.items()},
        "source_hashes": source_hashes,
        "combined_hash": _stable_hash(source_hashes),
        "expected_source_hashes": expected,
        "drift_detected": bool(changed),
        "changed_catalogs": changed,
        "action_items": action_items,
    }
