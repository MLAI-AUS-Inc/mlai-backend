"""Owner-only deliverability records, behind the verified MLAI admin installation gate."""

from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.response import Response

from crm.deliverability import guardian, policy
from crm.deliverability.streams import is_owner
from crm.models import DeliverabilityIncident, SendingStream

from .errors import CrmError
from .serializers import StrictSerializer, iso
from .views import CrmView, pagination_window, validated


def incident_dto(incident):
    """No recipient identities or raw provider payloads are exposed here."""
    return {"id": str(incident.pk), "stream": incident.stream.key,
            "blast_id": str(incident.blast_id) if incident.blast_id else None,
            "signal": incident.signal, "severity": incident.severity, "value": incident.value,
            "threshold": incident.threshold, "sample": incident.sample, "action": incident.action,
            "state": incident.state, "opened_at": iso(incident.opened_at),
            "acknowledged_at": iso(incident.acknowledged_at), "resolution": incident.resolution,
            "notes": incident.notes}


class DeliverabilityOwnerView(CrmView):
    """An owner must also be an active MLAI admin on their own verified installation."""

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not is_owner(request.user):
            raise CrmError("owner_required", 403)


class DeliverabilityView(DeliverabilityOwnerView):
    def get(self, request):
        streams = SendingStream.objects.select_related("mailbox").order_by("key")
        return Response({"streams": [{"key": stream.key, "label": stream.label, "domain": stream.domain,
            "from": stream.mailbox.email, "reply_to": stream.mailbox.reply_to, "state": stream.state,
            "state_reason": stream.state_reason, "ramp_stage": stream.ramp_stage,
            "transactional": stream.transactional, "enabled": stream.mailbox.enabled} for stream in streams],
            "incidents": [incident_dto(incident) for incident in DeliverabilityIncident.objects.filter(
                state="open").select_related("stream").order_by("-opened_at")[:30]],
            "guardian_enabled": policy.value("CRM_GUARDIAN_ENABLED"),
            "dry_run": policy.value("CRM_GUARDIAN_DRY_RUN")})


class IncidentsView(DeliverabilityOwnerView):
    def get(self, request, incident_id=None):
        incidents = DeliverabilityIncident.objects.select_related("stream").order_by("-opened_at", "pk")
        if incident_id is not None:
            return Response(incident_dto(get_object_or_404(incidents, pk=incident_id)))
        state = request.query_params.get("state", "open")
        if state not in ("open", "resolved", "acknowledged", "all"):
            raise CrmError("invalid_state")
        if state != "all":
            incidents = incidents.filter(state=state)
        offset, limit = pagination_window(request)
        rows = list(incidents[offset:offset + limit + 1])
        return Response({"results": [incident_dto(row) for row in rows[:limit]],
                         "next": f"?state={state}&offset={offset + limit}&limit={limit}" if len(rows) > limit else None})


class AcknowledgeInput(StrictSerializer):
    resolution = serializers.ChoiceField(choices=("resume_slow", "resume", "stop_blast", "keep_stopped"))
    note = serializers.CharField(max_length=2000, allow_blank=True, required=False, default="")


class IncidentAcknowledgeView(DeliverabilityOwnerView):
    def post(self, request, incident_id):
        data = validated(AcknowledgeInput, request)
        return Response(incident_dto(guardian.acknowledge(incident_id, actor=request.user, **data)))
