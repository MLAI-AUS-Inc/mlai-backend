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
        from django.utils import timezone
        from crm.deliverability.health import stream_dto
        now = timezone.now()
        streams = SendingStream.objects.select_related("mailbox").order_by("key")
        return Response({"streams": [stream_dto(stream, now=now) for stream in streams],
            "incidents": [incident_dto(incident) for incident in DeliverabilityIncident.objects.filter(
                state="open").select_related("stream").order_by("-opened_at")[:30]],
            "guardian_enabled": policy.value("CRM_GUARDIAN_ENABLED"),
            "monitors_enabled": policy.value("CRM_MONITORS_ENABLED"),
            "seed_heartbeat_enabled": policy.value("CRM_SEED_HEARTBEAT_ENABLED"),
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


class StreamPolicyView(DeliverabilityOwnerView):
    """Audited owner overrides; no identity or feature-switch changes."""

    def patch(self, request, key):
        from django.utils import timezone
        from crm.deliverability.health import stream_dto, update_stream
        get_object_or_404(SendingStream, key=key)
        stream = update_stream(key, request.data, actor=request.user, now=timezone.now())
        return Response(stream_dto(stream, now=timezone.now()))


class CheckInput(StrictSerializer):
    stream = serializers.SlugField(max_length=40)
    kind = serializers.ChoiceField(choices=('dns', 'config', 'reputation', 'postmaster'), required=False, default='config')


class SelfCheckView(DeliverabilityOwnerView):
    """Explicit owner checks only; this endpoint never submits an inbox probe."""

    def post(self, request):
        from django.utils import timezone
        from crm.deliverability import observations
        from crm.deliverability.monitors import config_check, dns, postmaster, reputation
        from crm.deliverability.monitors.http import budget
        from crm.models import CrmAuditEvent
        data = validated(CheckInput, request)
        stream = get_object_or_404(SendingStream.objects.select_related('mailbox'), key=data['stream'])
        if not policy.value('CRM_MONITORS_ENABLED'):
            return Response({'status': 'disabled'})
        now = timezone.now()
        CrmAuditEvent.objects.create(actor=request.user, action='edit', target_type='sending_stream', target_id=str(stream.pk),
            detail={'operation': 'self_check_requested', 'kind': data['kind']})
        try:
            with budget(policy.value('CRM_MONITOR_PASS_SECONDS')):
                result = {'dns': dns.check, 'config': config_check.check, 'reputation': reputation.check,
                    'postmaster': postmaster.check}[data['kind']](stream, now=now)
        except Exception:
            result = observations.record(stream, data['kind'], {'status': 'unavailable'}, now=now)
        return Response(result)


class HeartbeatInput(StrictSerializer):
    stream = serializers.SlugField(max_length=40)


class SeedHeartbeatView(DeliverabilityOwnerView):
    """Submit at most one enrolled-seed heartbeat per stream/Melbourne day."""

    def post(self, request):
        from django.utils import timezone
        from crm.deliverability.monitors.heartbeat import start
        from crm.models import CrmAuditEvent
        data = validated(HeartbeatInput, request)
        stream = get_object_or_404(SendingStream.objects.select_related('mailbox'), key=data['stream'])
        if not policy.value('CRM_SEED_HEARTBEAT_ENABLED'):
            return Response({'status': 'disabled'})
        CrmAuditEvent.objects.create(actor=request.user, action='edit', target_type='sending_stream', target_id=str(stream.pk),
            detail={'operation': 'seed_heartbeat_requested'})
        return Response(start(stream, now=timezone.now(), actor=request.user))
