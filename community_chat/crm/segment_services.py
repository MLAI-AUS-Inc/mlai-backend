"""Atomic segment editing and durable retries using the existing segment UUID.

No source relationship is required for a saved copy: its embedded rules remain
usable after the original is changed or deleted.
"""

from __future__ import annotations

import hashlib
import uuid

from django.db import IntegrityError, transaction

from community_chat.admin_access import require_mlai_admin
from crm.models import CrmAuditEvent, Segment
from crm.segment_trees import as_v2, builder_enabled, canonical_json, definition_etag, definition_hash, snapshot_nodes
from crm.segments import validate_definition

from .errors import CrmError


def _hash(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _top_snapshots(definition):
    if definition.get("version") != 2:
        return

    def walk(node):
        if node is None:
            return
        if node["kind"] == "saved_segment_snapshot":
            yield node
        elif node["kind"] == "group":
            for child in node["children"]:
                yield from walk(child)

    yield from walk(definition["include"])
    yield from walk(definition["exclude"])


def verify_snapshot_sources(definition, previous=None):
    """Verify newly added provenance; previously saved copies need no source."""

    retained = {_hash(node) for node in snapshot_nodes(previous or {})}
    nodes = [node for node in _top_snapshots(definition) if _hash(node) not in retained]
    if not nodes:
        return
    sources = {
        str(source.pk): source
        for source in Segment.objects.filter(pk__in={node["source_segment_id"] for node in nodes})
    }
    for node in nodes:
        source = sources.get(node["source_segment_id"])
        if source is None:
            raise CrmError("segment_source_unavailable", 409, source_segment_id=node["source_segment_id"])
        if (
            node["source_definition_hash"] != definition_hash(source.definition)
            or node["definition"] != as_v2(source.definition)
            or node["source_name"] != source.name
        ):
            raise CrmError("segment_source_changed", 409, source_segment_id=node["source_segment_id"])


def _precondition(segment, data, if_match):
    from .serializers import segment_dto

    stored_v2 = segment.definition.get("version") == 2
    proposed = data.get("definition", segment.definition)
    if stored_v2 and proposed.get("version") != 2:
        raise CrmError("definition_version_required", 409, supported_definition_versions=[2])
    if (stored_v2 or proposed.get("version") == 2) and not if_match:
        raise CrmError("segment_precondition_required", 428)
    if if_match is not None and if_match != definition_etag(segment):
        raise CrmError("segment_changed", 409, segment=segment_dto(segment))


def _replay(segment, actor, request_hash):
    receipt = CrmAuditEvent.objects.filter(
        target_type="segment", target_id=str(segment.pk), actor=actor,
        detail__operation="create",
    ).order_by("created_at").first()
    if receipt is None or receipt.detail.get("request_hash") != request_hash:
        raise CrmError("idempotency_key_reused", 409)
    return segment


def save_segment_atomic(segment, data, *, actor, now, if_match=None, idempotency_key=None):
    """Validate, lock, compare and save; legacy v1 writes remain compatible."""

    from .services import audit, segment_count

    require_mlai_admin(actor)
    data = dict(data)
    if "definition" in data:
        data["definition"] = validate_definition(data["definition"])
    if "name" in data:
        data["name"] = " ".join(data["name"].split())
    creating = segment is None
    request_hash = None
    create_id = None
    if creating:
        if "definition" not in data:
            raise CrmError("invalid_definition", pointer="/", reason="required")
        if idempotency_key is not None:
            if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 64:
                raise CrmError("invalid_idempotency_key")
            create_id = uuid.uuid5(uuid.NAMESPACE_URL, f"mlai-crm-segment:{actor.pk}:{idempotency_key}")
            request_hash = _hash({
                "name": data.get("name", ""), "description": data.get("description", ""),
                "definition": data["definition"],
            })
    try:
        with transaction.atomic():
            if creating:
                if create_id is not None:
                    existing = Segment.objects.select_for_update().filter(pk=create_id).first()
                    if existing is not None:
                        return _replay(existing, actor, request_hash)
                    if CrmAuditEvent.objects.filter(
                        target_type="segment", target_id=str(create_id), actor=actor,
                        detail__operation="create",
                    ).exists():
                        raise CrmError("segment_deleted", 409)
                segment = Segment(created_by=actor)
                if create_id is not None:
                    segment.pk = create_id
            else:
                segment = Segment.objects.select_for_update().filter(pk=segment.pk).first()
                if segment is None:
                    raise CrmError("not_found", 404)
                _precondition(segment, data, if_match)
            if not builder_enabled() and (
                data.get("definition", {}).get("version") == 2
                or segment.definition.get("version") == 2
            ):
                raise CrmError("segment_builder_disabled", 409)
            if "definition" in data:
                if any(node["source_segment_id"] == str(segment.pk)
                       for node in snapshot_nodes(data["definition"])):
                    raise CrmError("segment_self_reference", 400)
                verify_snapshot_sources(data["definition"], None if creating else segment.definition)
                segment.definition = data["definition"]
                segment.last_count = segment_count(segment.definition, now=now)
                segment.last_counted_at = now if segment.last_count is not None else None
            for name in ("name", "description"):
                if name in data:
                    setattr(segment, name, data[name])
            if not segment.name:
                raise CrmError("invalid_request", fields={"name": ["blank"]})
            segment.save(force_insert=creating)
            detail = {"operation": "create" if creating else "update", "fields": sorted(data)}
            if request_hash is not None:
                detail["request_hash"] = request_hash
            audit(actor, CrmAuditEvent.Action.EDIT, "segment", segment.pk, detail)
            return segment
    except IntegrityError:
        if not creating or create_id is None:
            raise
        existing = Segment.objects.filter(pk=create_id).first()
        if existing is None:
            raise
        return _replay(existing, actor, request_hash)


def delete_segment_atomic(segment, *, actor, if_match=None):
    """Delete under the same edit precondition used by updates."""

    from .services import audit

    require_mlai_admin(actor)
    with transaction.atomic():
        current = Segment.objects.select_for_update().filter(pk=segment.pk).first()
        if current is None:
            raise CrmError("not_found", 404)
        _precondition(current, {}, if_match)
        segment_id = current.pk
        current.delete()
        audit(actor, CrmAuditEvent.Action.EDIT, "segment", segment_id, {"operation": "delete"})
