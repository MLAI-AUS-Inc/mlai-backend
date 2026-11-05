"""Verified MLAI admin checks, polling and bounded message fixes."""

from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.response import Response

from crm.blasts import BlastError
from crm.deliverability import policy
from crm.deliverability.preflight import service
from crm.deliverability.preflight.presentation import result_dto
from crm.deliverability.streams import is_owner
from crm.models import Blast

from .serializers import StrictSerializer
from .views import BLAST_RELATIONS, CrmView, blast_detail, validated


def found(blast_id):
    if not policy.value('CRM_PREFLIGHT_ENABLED'):
        raise BlastError('preflight_disabled', 404)
    blast = get_object_or_404(Blast.objects.select_related(*BLAST_RELATIONS), pk=blast_id)
    if 'email' not in (blast.channels or []) or not blast.stream_id:
        raise BlastError('email_not_selected', 409)
    if blast.status not in ('draft', 'queued', 'sending'):
        raise BlastError('blast_not_live', 409)
    return blast


class PreflightView(CrmView):
    def post(self, request, blast_id):
        result = service.run_preflight(found(blast_id))
        return Response(result_dto(result, owner=is_owner(request.user)))

    def get(self, request, blast_id):
        result = service.current_result(found(blast_id))
        return Response(result_dto(result, owner=is_owner(request.user)))


class FixInput(StrictSerializer):
    kind = serializers.ChoiceField(choices=('expand_short_links', 'upgrade_https', 'add_preview_text', 'remove_subject_prefix'))


class PreflightFixView(CrmView):
    def post(self, request, blast_id):
        found(blast_id)
        data = validated(FixInput, request)
        service.fix_draft(blast_id, data['kind'], actor=request.user)
        result = service.run_preflight(found(blast_id))
        return Response({'blast': blast_detail(blast_id), 'preflight': result_dto(result, owner=is_owner(request.user))})
