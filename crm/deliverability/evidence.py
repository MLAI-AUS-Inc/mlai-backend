"""Bounded aggregate evidence from the delivery ledgers, with no message content."""

from collections import Counter
from datetime import datetime, time, timedelta

from django.db.models import Count, Exists, Max, OuterRef, Q, Sum
from django.db.models.functions import TruncDate

from crm.models import BlastDelivery, BlastWave, CrmAuditEvent, EmailEvent, SeedTest
from system_email.models import SystemEmail, SystemEmailWebhookEvent

from . import policy, volume

MAX_DAYS = 31


def window(start, end, now):
    today = now.astimezone(volume.MELBOURNE).date()
    if start > end or end > today or (end - start).days >= MAX_DAYS:
        raise ValueError('Use at most 31 Melbourne dates ending today or earlier.')
    return (datetime.combine(start, time.min, volume.MELBOURNE),
        datetime.combine(end + timedelta(days=1), time.min, volume.MELBOURNE))


def campaign(blast, now):
    """Show acceptance and subsequent risk separately on the campaign detail."""
    from .preflight import placement, service
    rows = blast.deliveries.filter(channel='email')
    events = EmailEvent.objects.filter(blast=blast, source='resend')
    preflight = blast.preflight or {}
    source = service.seed_result(blast, preflight) if blast.stream_id and preflight else {}
    observed = placement.assess(blast, source, now)
    return {'source': 'crm_delivery_and_resend_events', 'checked_at': now.isoformat(),
        'sender': blast.mailbox.email if blast.mailbox_id else None, 'purpose': blast.purpose,
        'counts': rows.aggregate(eligible=Count('pk'), reserved=Count('pk', filter=Q(wave__isnull=False)),
            submitted_or_uncertain=Count('pk', filter=Q(wave__started_at__isnull=False) & ~Q(status='cancelled')),
            excluded=Count('pk', filter=Q(status='cancelled')),
            already_registered=Count('pk', filter=Q(status='cancelled', error_code='already_registered')),
            accepted=Count('pk', filter=Q(delivered_at__isnull=False)),
            later_transient_bounce=Count('pk', filter=Q(Exists(later_soft_bounces()))),
            unsubscribed=Count('pk', filter=Q(status='unsubscribed', error_code=''))),
        'feedback': event_counts(events), 'placement': observed}


def later_soft_bounces():
    """A later transient bounce remains visible even for pre-recovery deliveries."""
    return EmailEvent.objects.filter(delivery_id=OuterRef('pk'), event_type='email.bounced',
        bounce_type='Transient', occurred_at__gte=OuterRef('delivered_at'))


def event_counts(events, *groups):
    """Count messages, independently of how many receipts each one generated."""
    return list(events.values(*groups, 'recipient_provider').annotate(
        provider_accepted=Count('provider_message_id', filter=Q(event_type='email.delivered'), distinct=True),
        deferred=Count('provider_message_id', filter=Q(event_type='email.delivery_delayed'), distinct=True),
        bounced_soft=Count('provider_message_id', filter=Q(event_type='email.bounced', bounce_type='Transient'), distinct=True),
        bounced_hard=Count('provider_message_id', filter=Q(event_type='email.bounced') & ~Q(bounce_type='Transient'), distinct=True),
        complained=Count('provider_message_id', filter=Q(event_type='email.complained'), distinct=True),
        latest_feedback=Max('received_at')).order_by(*groups, 'recipient_provider'))


def report(start, end, now):
    """Return a read-only view of CRM handoffs and provider feedback by Melbourne date."""
    lower, upper = window(start, end, now)
    events = EmailEvent.objects.filter(source='resend', occurred_at__gte=lower, occurred_at__lt=upper)
    campaign_events = events.filter(blast__isnull=False).annotate(day=TruncDate('occurred_at', tzinfo=volume.MELBOURNE))
    reported = event_counts(campaign_events, 'day', 'sender', 'blast_id', 'blast__purpose')
    waves = BlastWave.objects.filter(reserved_at__gte=lower, reserved_at__lt=upper).annotate(
        day=TruncDate('reserved_at', tzinfo=volume.MELBOURNE)).values(
        'day', 'blast_id', 'blast__purpose', 'blast__mailbox__email').annotate(
        reserved=Sum('size'), campaign_handoffs=Sum('size', filter=Q(started_at__isnull=False))).order_by('day', 'blast_id')
    tests = CrmAuditEvent.objects.filter(target_type='crm_email_attempt', created_at__gte=lower, created_at__lt=upper
        ).annotate(day=TruncDate('created_at', tzinfo=volume.MELBOURNE)).values_list('day', 'detail')
    test_counts = Counter()
    for day, detail in tests:
        test_counts[(day, detail['kind'])] += detail['attempts']
    probes = SeedTest.objects.filter(created_at__gte=lower, created_at__lt=upper).select_related('stream').order_by('created_at')
    placements = []
    for probe in probes.iterator():
        mailbox_results = (probe.results or {}).get('mailboxes') or {}
        folders = Counter((row.get('provider') or 'unknown', row.get('folder') or 'unknown')
            for row in mailbox_results.values())
        placements.append({'day': probe.created_at.astimezone(volume.MELBOURNE).date(),
            'campaign': probe.blast_id, 'stream': probe.stream.key, 'checked_at': probe.sent_at,
            'status': probe.status, 'sample': len(mailbox_results), 'observations': [
                {'provider': provider, 'folder': folder, 'count': count}
                for (provider, folder), count in sorted(folders.items())]})
    system = SystemEmail.objects.filter(created_at__gte=lower, created_at__lt=upper)
    system_statuses = system.annotate(day=TruncDate('created_at', tzinfo=volume.MELBOURNE)).values(
        'day', 'message_key', 'status').annotate(count=Count('pk')).order_by('day', 'message_key', 'status')
    system_events = SystemEmailWebhookEvent.objects.filter(received_at__gte=lower, received_at__lt=upper).values(
        'event_type').annotate(messages=Count('provider_message_id', distinct=True)).order_by('event_type')
    return {'time_zone': 'Australia/Melbourne', 'checked_at': now, 'from_day': start, 'through_day': end,
        'crm_feedback_retention_days': policy.value('CRM_EMAIL_EVENT_RETENTION_DAYS'),
        'crm_wave_reservations': list(waves), 'crm_provider_feedback': reported,
        'crm_unmatched_provider_messages': events.filter(blast__isnull=True).values('provider_message_id').distinct().count(),
        'crm_newly_recorded_test_attempts': [{'day': day, 'kind': kind, 'count': count}
            for (day, kind), count in sorted(test_counts.items())],
        'system_email_statuses': list(system_statuses), 'system_email_feedback': list(system_events),
        'latest_signed_crm_feedback': events.aggregate(latest=Max('received_at'))['latest'],
        'placement_source': 'controlled_mailboxes', 'crm_probe_observations': placements}
