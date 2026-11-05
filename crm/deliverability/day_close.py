"""Close observed sending days once; missing feedback can never promote a ramp."""

import logging
from datetime import datetime, time, timedelta

from django.core.cache import cache
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from crm.models import BlastDelivery, CrmAuditEvent, EmailEvent, SendingStream, StreamDay

from . import policy, volume
from .wave_sync import metrics

logger = logging.getLogger(__name__)


def refresh_day(row):
    start = datetime.combine(row.day, time.min, volume.MELBOURNE)
    end = start + timedelta(days=1)
    rows = BlastDelivery.objects.filter(wave__blast__stream=row.stream,
        wave__started_at__gte=start, wave__started_at__lt=end)
    counts = metrics(rows)
    events = EmailEvent.objects.filter(delivery__in=rows)
    counts['deferred'] = events.filter(event_type='email.delivery_delayed').values('delivery_id').distinct().count()
    for key, value in counts.items():
        setattr(row, key, value)
    row.by_provider = {provider: metrics(rows.filter(provider=provider))
        for provider in rows.values_list('provider', flat=True).distinct()}
    for provider, values in row.by_provider.items():
        values['deferred'] = events.filter(recipient_provider=provider,
            event_type='email.delivery_delayed').values('delivery_id').distinct().count()
    incident = row.stream.incidents.filter(opened_at__lt=end, last_detected_at__gte=start).exists()
    feedback = events.filter(source='resend').exists()
    row.healthy = volume.healthy_day(row, incident=incident, feedback=feedback) if feedback else None
    row.save()


def close_day(row_id, *, now):
    """The stream lock and durable audit prevent repeated promotions across retries."""
    stream_id = StreamDay.objects.values_list('stream_id', flat=True).get(pk=row_id)
    with transaction.atomic():
        stream = SendingStream.objects.select_for_update().get(pk=stream_id)
        row = StreamDay.objects.select_for_update().get(pk=row_id)
        row.stream = stream
        refresh_day(row)
        closed = CrmAuditEvent.objects.filter(target_type='stream_day', target_id=str(row.pk), detail__operation='day_closed').exists()
        if closed:
            return False
        before = stream.ramp_stage
        if stream.state not in ('held', 'stopped'):
            stream.ramp_stage = volume.promote_stage(stream.ramp_stage, row.sent, row.budget, row.healthy is True)
            amber = True
            for offset in range(policy.value('CRM_RAMP_AMBER_DAYS')):
                day = row.day - timedelta(days=offset)
                start = datetime.combine(day, time.min, volume.MELBOURNE)
                amber = amber and stream.incidents.filter(severity='amber', opened_at__lt=start + timedelta(days=1),
                    last_detected_at__gte=start).exists()
            if amber:
                stream.ramp_stage = max(0, stream.ramp_stage - policy.value('CRM_RAMP_AMBER_DEMOTION'))
            if stream.ramp_stage == len(policy.value('CRM_RAMP_LADDER')) - 1 and stream.state == 'warming':
                stream.state = 'active'
        stream.peak_healthy_daily_volume = StreamDay.objects.filter(stream=stream, healthy=True,
            day__gte=row.day - timedelta(days=policy.value('CRM_RAMP_PEAK_DAYS') - 1), day__lte=row.day).aggregate(n=Max('sent'))['n'] or 0
        if stream.ramp_stage != before:
            stream.ramp_updated_at = now
        stream.save(update_fields=['ramp_stage', 'ramp_updated_at', 'state', 'peak_healthy_daily_volume', 'updated_at'])
        CrmAuditEvent.objects.create(action='edit', target_type='stream_day', target_id=str(row.pk),
            detail={'operation': 'day_closed', 'day': row.day.isoformat(), 'sent': row.sent, 'budget': row.budget,
                'healthy': row.healthy, 'from_stage': before, 'stage': stream.ramp_stage})
        return True


def run_stream_day_close(*, now=None):
    """Never raises into the scheduler and is inert while wave sending is disabled."""
    if not policy.value('CRM_WAVES_ENABLED'):
        return {'status': 'disabled'}
    now = now or timezone.now()
    try:
        if not cache.add('crm:deliverability:day-close', 1, timeout=policy.value('CRM_DAY_CLOSE_INTERVAL_SECONDS')):
            return {'status': 'throttled'}
        today = now.astimezone(volume.MELBOURNE).date()
        closed = 0
        for row in StreamDay.objects.filter(day__lt=today).order_by('day', 'stream_id'):
            closed += close_day(row.pk, now=now)
        EmailEvent.objects.filter(received_at__lt=now - timedelta(days=policy.value('CRM_EMAIL_EVENT_RETENTION_DAYS'))).delete()
        return {'status': 'completed', 'closed': closed}
    except Exception as exc:
        logger.error('CRM stream day close deferred: %s', type(exc).__name__)
        return {'status': 'deferred'}
