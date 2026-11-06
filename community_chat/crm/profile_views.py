"""Dedicated typed audience-profile operations behind the existing CRM admin gate."""

from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.response import Response

from crm.audience_profiles import AudienceProfileError, audience_profile
from crm.audience_sources import project_records
from crm.models import Contact

from .errors import CrmError
from .profile_services import apply_audience_mapping, correct_audience_profile
from .views import CrmView


class AudienceProfileView(CrmView):
    """Read the typed profile and apply explicitly attributed single-facet corrections."""

    def get(self, request, contact_id):
        contact = get_object_or_404(Contact, pk=contact_id)
        return Response({"audience_profile": audience_profile(contact.attributes)})

    def post(self, request, contact_id):
        return Response({"audience_profile": correct_audience_profile(
            contact_id, request.data, actor=request.user, now=timezone.now(),
        )})


class AudienceProfileMappingPreviewView(CrmView):
    """Preview an explicit reviewed mapping without modifying profile, consent or source data."""

    def post(self, request, contact_id):
        contact = get_object_or_404(Contact, pk=contact_id)
        body = request.data
        if not isinstance(body, dict) or set(body) != {"records", "mappings"}:
            raise CrmError("invalid_mapping_preview")
        if not isinstance(body["records"], list) or not isinstance(body["mappings"], list):
            raise CrmError("invalid_mapping_preview")
        try:
            profile, counts = project_records(contact.attributes, body["records"], body["mappings"], now=timezone.now())
        except AudienceProfileError as exc:
            raise CrmError("invalid_mapping_preview", reason=exc.reason, pointer=exc.pointer) from exc
        return Response({"audience_profile": profile, "counts": counts, "applied": False})


class AudienceProfileMappingApplyView(CrmView):
    """Apply reviewed source facts separately from the generic scalar CSV import path."""

    def post(self, request, contact_id):
        profile, counts = apply_audience_mapping(contact_id, request.data, actor=request.user, now=timezone.now())
        return Response({"audience_profile": profile, "counts": counts, "applied": True})
