"""Permissioned audience corrections; audit field names without logging demographic values."""

from django.db import transaction
from django.shortcuts import get_object_or_404

from community_chat.admin_access import require_mlai_admin
from crm.audience_profiles import AudienceProfileError, correction_profile, with_profile
from crm.audience_sources import project_records
from crm.models import Contact, CrmAuditEvent

from .errors import CrmError


def correct_audience_profile(contact_id, data: dict, *, actor, now) -> dict:
    """Correct one evidence-backed facet under a row lock, preserving all consent metadata."""

    require_mlai_admin(actor)
    with transaction.atomic():
        contact = get_object_or_404(Contact.objects.select_for_update(), pk=contact_id)
        try:
            profile = correction_profile(contact.attributes, data, now=now)
            attributes = with_profile(contact.attributes, profile)
        except AudienceProfileError as exc:
            raise CrmError("invalid_audience_profile", pointer=exc.pointer, reason=exc.reason) from exc
        if attributes != contact.attributes:
            contact.attributes = attributes
            contact.save(update_fields=["attributes", "updated_at"])
            CrmAuditEvent.objects.create(
                actor=actor, action=CrmAuditEvent.Action.EDIT, target_type="contact", target_id=str(contact.pk),
                detail={"operation": "audience_profile_correction", "fields": [data["facet"]]},
            )
    return profile


def apply_audience_mapping(contact_id, data: dict, *, actor, now) -> tuple[dict, dict]:
    """Apply an explicitly reviewed source mapping without promoting it to admin-authored facts."""

    require_mlai_admin(actor)
    if not isinstance(data, dict) or set(data) != {"records", "mappings"}:
        raise CrmError("invalid_mapping_request")
    if not isinstance(data["records"], list) or not isinstance(data["mappings"], list):
        raise CrmError("invalid_mapping_request")
    with transaction.atomic():
        contact = get_object_or_404(Contact.objects.select_for_update(), pk=contact_id)
        try:
            profile, counts = project_records(contact.attributes, data["records"], data["mappings"], now=now)
            attributes = with_profile(contact.attributes, profile)
        except AudienceProfileError as exc:
            raise CrmError("invalid_mapping_request", pointer=exc.pointer, reason=exc.reason) from exc
        if attributes != contact.attributes:
            contact.attributes = attributes
            contact.save(update_fields=["attributes", "updated_at"])
            CrmAuditEvent.objects.create(
                actor=actor, action=CrmAuditEvent.Action.EDIT, target_type="contact", target_id=str(contact.pk),
                detail={"operation": "audience_source_mapping", "fields": sorted({item["facet"] for item in data["mappings"]})},
            )
    return profile, counts
