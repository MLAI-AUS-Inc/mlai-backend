"""Feedback reconciliation and aggregate counts for independently owned waves."""

import hashlib
from collections import Counter
from datetime import timedelta

from django.db import transaction
from django.db.models import Count, Q

from crm import blast_email, listmonk
from crm.models import BlastDelivery, Contact, EmailEvent

from . import volume, waves


def metrics(rows):
    """One row per recipient, so repeated feedback never inflates a rate."""
    return rows.aggregate(
        sent=Count('pk', filter=~Q(status__in=('queued', 'cancelled', 'failed')) & ~(Q(status='unsubscribed') & ~Q(error_code=''))),
        delivered=Count('pk', filter=Q(delivered_at__isnull=False)),
        bounced_hard=Count('pk', filter=Q(bounce_kind='hard')), bounced_soft=Count('pk', filter=Q(bounce_kind='soft')),
        complained=Count('pk', filter=Q(error_code='resend_complained')),
        unsubscribed=Count('pk', filter=Q(status='unsubscribed', error_code='')),
        opened=Count('pk', filter=Q(first_opened_at__isnull=False)), clicked=Count('pk', filter=Q(first_clicked_at__isnull=False)))


def wave_counts(wave):
    rows = wave.deliveries.all()
    result = metrics(rows) if wave.started_at else {key: 0 for key in metrics(rows.none())}
    result['deferred'] = wave.email_events.filter(event_type='email.delivery_delayed').values('recipient').distinct().count()
    result['by_provider'] = {}
    for provider in rows.values_list('provider', flat=True).distinct():
        result['by_provider'][provider] = metrics(rows.filter(provider=provider))
        result['by_provider'][provider]['deferred'] = wave.email_events.filter(recipient_provider=provider,
            event_type='email.delivery_delayed').values('recipient').distinct().count()
    return result


def record_smtp_feedback(delivery, kind, now):
    """Store known listmonk bounce types; absent SMTP diagnostics remain absent."""
    from crm.resend_feedback import _apply_contact_feedback, next_delivery_state
    receipt = hashlib.sha256(f'listmonk:{delivery.wave_id}:{delivery.pk}:{kind}'.encode()).hexdigest()
    with transaction.atomic():
        Contact.objects.select_for_update().get(pk=delivery.contact_id)
        delivery = BlastDelivery.objects.select_for_update(of=('self',)).select_related('blast__mailbox', 'contact').get(pk=delivery.pk)
        event, created = EmailEvent.objects.get_or_create(receipt=receipt, defaults={
            'source': 'listmonk', 'event_type': 'email.bounced', 'provider_message_id': delivery.provider_id,
            'recipient': delivery.address, 'sender': delivery.blast.mailbox.email, 'recipient_provider': delivery.provider,
            'stream_id': delivery.blast.stream_id, 'blast': delivery.blast, 'wave_id': delivery.wave_id, 'delivery': delivery,
            'bounce_type': 'Permanent' if kind == 'hard' else 'Transient', 'occurred_at': now})
        if created:
            parsed = {'type': 'email.bounced', 'recipient': delivery.address, 'email_id': delivery.provider_id,
                'bounce_type': event.bounce_type}
            delivery.status, delivery.error_code = next_delivery_state(delivery.status, delivery.error_code,
                'email.bounced', bounce_type=event.bounce_type)
            if delivery.error_code != 'resend_complained':
                delivery.bounce_kind = 'hard' if delivery.status == 'bounced' else 'soft'
            delivery.save(update_fields=['status', 'error_code', 'bounce_kind', 'updated_at'])
            _apply_contact_feedback(parsed, event, delivery, now)
    contact = Contact.objects.get(pk=delivery.contact_id)
    if contact.email_status in ('bounced', 'complained', 'unsubscribed') and contact.listmonk_subscriber_id:
        listmonk.blocklist_subscribers([contact.listmonk_subscriber_id])


def sync_blast(blast, *, since, now):
    """Each campaign maps only to its own rows, including late engagement."""
    from crm.listmonk_sync import ENGAGEMENT_DAYS, OVERLAP, sync_engagement
    from crm.blasts import refresh_status
    counts = Counter()
    recent = now - timedelta(days=ENGAGEMENT_DAYS)
    for wave in blast.waves.filter(Q(status__in=waves.ACTIVE) | Q(started_at__gte=recent)
            | Q(stats__legacy_adopted=True, started_at__isnull=True)).order_by('index'):
        if wave.listmonk_campaign_id or wave.status in waves.ACTIVE:
            waves.advance(wave.pk, now=now)
            wave.refresh_from_db()
        if wave.listmonk_campaign_id and wave.started_at:
            counts['campaigns'] += 1
            counts.update(sync_engagement(blast, wave=wave, since=max(since, wave.started_at - OVERLAP), now=now))
        wave.stats = {**wave.stats, **wave_counts(wave)}
        wave.save(update_fields=['stats', 'updated_at'])
    totals = Counter()
    for wave in blast.waves.all():
        for key in ('sent', 'delivered', 'bounced_hard', 'bounced_soft', 'deferred', 'complained', 'unsubscribed', 'opened', 'clicked'):
            totals[key] += (wave.stats or {}).get(key, 0)
    totals['to_send'] = blast.deliveries.filter(channel='email').count()
    values = dict(totals)
    values['cancel_pending'] = blast.status == 'cancelled' and blast.waves.filter(status__in=waves.ACTIVE).exists()
    blast_email.merge_email_stats(blast.pk, **values)
    if blast.stream_id:
        from .day_close import refresh_day
        from crm.models import StreamDay
        dates = {wave.started_at.astimezone(volume.MELBOURNE).date() for wave in blast.waves.all() if wave.started_at}
        for row in StreamDay.objects.filter(stream=blast.stream, day__in=dates):
            refresh_day(row)
        remaining = blast.deliveries.filter(channel='email', status='queued', wave__isnull=True).count()
        used, used_hour = volume.usage(blast.stream, now)
        first = blast.waves.filter(started_at__isnull=False).order_by('index').first()
        plan = dict(blast.plan or {})
        plan['estimate'] = volume.estimate(blast.stream, remaining, now, used_today=used, used_hour=used_hour,
            canary_started_at=first.started_at if first else None, critical=volume.time_critical(blast, now))
        type(blast).objects.filter(pk=blast.pk).update(plan=plan)
    refresh_status(blast.pk, now=now)
    return counts
