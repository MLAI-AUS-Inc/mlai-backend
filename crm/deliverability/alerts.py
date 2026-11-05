"""Owner alerts contain aggregate evidence, never recipient addresses."""

from datetime import timedelta

from django.conf import settings
from django.db import transaction

from crm.models import DeliverabilityIncident

from . import policy
from .copy import REASONS


def alert_text(incident):
    """A bounded explanation with evidence and a link to the owner-only record."""
    reason = REASONS.get(incident.signal, "the email system needs attention")
    value = f"{incident.value:.2%}" if incident.signal.endswith("_rate") else str(int(incident.value))
    action = "Recorded for staging review" if policy.value("CRM_GUARDIAN_DRY_RUN") else {
        "stop_stream": "Campaign sending stopped; an owner must decide when it resumes",
        "pause_blast": "This campaign is paused; new campaign starts are on hold",
        "slow_stream": "Future campaign sending will slow down",
    }.get(incident.action, "The email team should review this")
    link = f"{settings.DEFAULT_BACKEND_URL.rstrip('/')}/api/v1/community-chat/crm/deliverability/incidents/{incident.pk}/"
    return f"MLAI email: {reason}.\nObserved {value} across {incident.sample} messages.\n{action}.\n{link}"


def alert_open_incidents(*, now):
    """Retry failed alerts and remind about unacknowledged red incidents."""
    channel = policy.value("CRM_DELIVERABILITY_SLACK_CHANNEL")
    if not channel:
        return
    from jobs.services.slack import post_slack_message
    for incident_id in DeliverabilityIncident.objects.filter(state="open").values_list("pk", flat=True):
        with transaction.atomic():
            incident = DeliverabilityIncident.objects.select_for_update().get(pk=incident_id)
            if incident.state != "open":
                continue
            if incident.last_alerted_at and (incident.severity != "red" or
                    incident.last_alerted_at > now - timedelta(hours=policy.value("CRM_GUARD_ALERT_REMINDER_HOURS"))):
                continue
            success, _ = post_slack_message({"channel": channel, "text": alert_text(incident)})
            if success:
                incident.last_alerted_at = now
                incident.save(update_fields=["last_alerted_at", "updated_at"])
