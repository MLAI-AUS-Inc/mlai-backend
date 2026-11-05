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
    value = f"{incident.value:.2%}" if incident.signal.endswith("_rate") or incident.signal in ('click_anomaly', 'cohort_quarantine') else str(int(incident.value))
    action = "Recorded for staging review" if policy.value("CRM_GUARDIAN_DRY_RUN") else {
        "stop_stream": "Campaign sending stopped; an owner must decide when it resumes",
        "pause_blast": "This campaign is paused; new campaign starts are on hold",
        "slow_stream": "Future campaign sending will slow down",
        'throttle_provider': 'Sending to the affected inbox provider has slowed',
    }.get(incident.action, "The email team should review this")
    if incident.signal == 'cohort_quarantine':
        action = 'Untouched contacts in that group need review before receiving community email'
    link = f"{settings.DEFAULT_BACKEND_URL.rstrip('/')}/api/v1/community-chat/crm/deliverability/incidents/{incident.pk}/"
    health = settings.COMMUNITY_CHAT_FRONTEND_URL.rstrip("/") + f"/admin/sending?incident={incident.pk}"
    return f"MLAI email: {reason}.\nObserved {value} across {incident.sample} messages.\n{action}.\n{health}\n{link}"


def alert_open_incidents(*, now):
    """Retry failed alerts and remind about unacknowledged red incidents."""
    try:
        alert_owner_emails(now=now)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning('CRM owner email alert deferred: %s', type(exc).__name__)
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


def alert_owner_emails(*, now):
    """Red alerts and four-hour reminders have stable per-owner idempotency receipts."""
    if not policy.value('CRM_DELIVERABILITY_EMAIL_ALERTS_ENABLED'):
        return
    from .owner_mail import send
    for incident in DeliverabilityIncident.objects.filter(state='open', severity='red').order_by('opened_at'):
        bucket = max(0, int((now - incident.opened_at).total_seconds() // (policy.value('CRM_GUARD_ALERT_REMINDER_HOURS') * 3600)))
        send('MLAI email needs an owner decision', alert_text(incident),
            key=f'incident:{incident.pk}:{bucket}', now=now)


def digest_text(stream, *, now):
    """Aggregate yesterday's outcomes and today's capacity; no recipient identifiers."""
    from . import observations, volume
    yesterday = now.astimezone(volume.MELBOURNE).date() - timedelta(days=1)
    day = stream.days.filter(day=yesterday).first()
    lines = [f'MLAI email health — {stream.label} — {yesterday}', f'State: {stream.state}. {stream.state_reason}.']
    if day:
        lines += [f'Sent {day.sent:,}; delivered {day.delivered:,}.',
            f'Hard bounces {day.bounced_hard / day.sent:.2%}; unsubscribes {day.unsubscribed / day.sent:.2%}.' if day.sent else 'No campaign handoffs recorded.',
            f'Complaints {day.complained / day.delivered:.2%}.' if day.delivered else 'No delivery denominator available.']
    else:
        lines.append('No sending-day observations available for yesterday.')
    if not stream.transactional:
        lines.append(f'Today’s campaign capacity: {volume.budget(stream):,}. Ramp stage: {stream.ramp_stage}.')
    lines.append(f'Open incidents: {stream.incidents.filter(state="open").count()}.')
    for kind in ('postmaster', 'dns', 'reputation', 'config', 'heartbeat'):
        result = observations.latest(stream, kind)
        lines.append(f'{kind}: {result.get("status", "not_checked")}.')
        if kind == 'postmaster':
            for day, values in result.get('days', {}).items():
                rate = values.get('spam_rate')
                lines.append(f'Google reported spam {day}: {rate:.3%}.' if type(rate) in (int, float) else f'Google reported spam {day}: no data.')
    lines.append(settings.COMMUNITY_CHAT_FRONTEND_URL.rstrip('/') + '/admin/sending')
    return '\n'.join(lines)


def run_daily_digest(*, now=None):
    """Retry the same daily receipt; the notify provider never receives a new retry key."""
    import logging
    from django.utils import timezone
    from crm.models import SendingStream
    from . import owner_mail, volume
    if not policy.value('CRM_MONITORS_ENABLED'):
        return {'status': 'disabled'}
    now = now or timezone.now()
    local = now.astimezone(volume.MELBOURNE)
    if (local.hour, local.minute) < (policy.value('CRM_DIGEST_HOUR'), policy.value('CRM_DIGEST_MINUTE')):
        return {'status': 'not_due'}
    try:
        sent = 0
        for stream in SendingStream.objects.order_by('key'):
            sent += owner_mail.send('MLAI daily email health — ' + stream.label, digest_text(stream, now=now),
                key=f'digest:{stream.key}:{local.date()}', now=now)
        return {'status': 'completed', 'streams_emailed': sent}
    except Exception as exc:
        logging.getLogger(__name__).warning('CRM digest deferred: %s', type(exc).__name__)
        return {'status': 'deferred'}
