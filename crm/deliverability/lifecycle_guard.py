"""Cohort quarantine and mature provider click comparisons, without using opens."""

import copy
import hashlib
from collections import defaultdict
from datetime import timedelta
from statistics import median
from uuid import UUID

from django.db import transaction
from django.db.models import Q

from crm.engagement import marketing_review_hold
from crm.models import Blast, BlastDelivery, Contact, CrmAuditEvent, SendingStream

from . import guardian, policy, scopes, volume


def cohort_keys(contact):
    """Only validated internal import IDs and the bounded source enum identify cohorts."""
    result = [('source', contact.source)] if contact.source in Contact.Source.values else []
    value = contact.attributes.get('source_import') if isinstance(contact.attributes, dict) else None
    if isinstance(value, str):
        try:
            result.append(('source_import', str(UUID(value))))
        except ValueError:
            pass
    return result


def quarantine(blast, *, now):
    """Hold only untouched cohort members; existing holds and consent are preserved."""
    handed = Q(status__in=scopes.ATTEMPTED) | Q(error_code='resend_complained') | ~Q(provider_id='')
    rows = list(blast.deliveries.filter(handed, channel='email')
        .select_related('contact').order_by('pk'))
    if not rows:
        return []
    groups = defaultdict(list)
    for row in rows:
        for key in cohort_keys(row.contact):
            groups[key].append(row)
    total = {'bounce': sum(row.bounce_kind == 'hard' for row in rows),
        'complaint': sum(row.error_code == 'resend_complained' for row in rows)}
    delivered = lambda values: sum(bool(row.delivered_at) or row.status in ('delivered', 'read') or row.error_code == 'resend_complained' for row in values)
    all_delivered = delivered(rows)
    results = []
    for (kind, value), members in groups.items():
        for signal, bad in (('bounce', [row.pk for row in members if row.bounce_kind == 'hard']),
                ('complaint', [row.pk for row in members if row.error_code == 'resend_complained'])):
            denominator = len(members) if signal == 'bounce' else delivered(members)
            average_n = len(rows) if signal == 'bounce' else all_delivered
            average = total[signal] / average_n if average_n else 0
            rate = len(bad) / denominator if denominator else 0
            if len(bad) < policy.value('CRM_GUARD_COHORT_MIN_COUNT') or not average or rate < average * policy.value('CRM_GUARD_COHORT_FACTOR'):
                continue
            identity = hashlib.sha256(f'{kind}:{value}'.encode()).hexdigest()[:24]
            protected = blast.deliveries.filter(handed).values('contact_id')
            candidates = Contact.objects.filter(**({'source': value} if kind == 'source' else {'attributes__source_import': value}))
            candidates = candidates.filter(email_status='active', attributes__contains={}).exclude(marketing_consent='withdrawn').filter(
                Q(attributes__marketing_review_hold__isnull=True) | Q(attributes__marketing_review_hold=False)).exclude(pk__in=protected)
            changed = []
            with transaction.atomic():
                for person in candidates.select_for_update(skip_locked=True).order_by('pk')[:policy.value('CRM_LIFECYCLE_BATCH_SIZE')]:
                    if marketing_review_hold(person.attributes):
                        continue
                    person.attributes = copy.deepcopy(person.attributes)
                    person.attributes['marketing_review_hold'] = {'reason': 'cohort_deliverability',
                        'blast': str(blast.pk), 'cohort': identity, 'at': now.isoformat()}
                    person.updated_at = now
                    changed.append(person)
                if changed:
                    Contact.objects.bulk_update(changed, ['attributes', 'updated_at'], batch_size=250)
                    CrmAuditEvent.objects.create(action='edit', target_type='blast', target_id=str(blast.pk),
                        detail={'operation': 'cohort_quarantine', 'cohort': identity, 'kind': kind,
                            'signal': signal, 'sample': denominator, 'events': len(bad), 'held': len(changed)})
            result = guardian.detect(blast, guardian.Signal('cohort_quarantine', 'amber', rate,
                average * policy.value('CRM_GUARD_COHORT_FACTOR'), denominator, 'alert_only',
                evidence_key=scopes.evidence_key(bad), scope='cohort:' + identity + ':' + signal), now=now)
            if result:
                results.append(result)
    return results


def click_anomalies(stream, *, now):
    """Compare a mature 24-hour handoff cohort with observed provider-day medians."""
    first, last = now - timedelta(hours=48), now - timedelta(hours=24)
    rows = BlastDelivery.objects.filter(Q(status__in=scopes.ATTEMPTED) | Q(error_code='resend_complained') | ~Q(provider_id=''),
        blast__stream=stream, channel='email', wave__started_at__gte=first, wave__started_at__lt=last)
    today = now.astimezone(volume.MELBOURNE).date()
    history = list(stream.days.filter(day__gte=today - timedelta(days=policy.value('CRM_GUARD_CLICK_LOOKBACK_DAYS')),
        day__lt=first.astimezone(volume.MELBOURNE).date()))
    results = []
    for provider in rows.values_list('provider', flat=True).distinct():
        if provider not in ('google', 'microsoft', 'yahoo', 'apple', 'other'):
            continue
        cohort = rows.filter(provider=provider)
        count = cohort.count()
        if count < policy.value('CRM_GUARD_CLICK_MIN_SAMPLE'):
            continue
        baseline = []
        for day in history:
            values = day.by_provider.get(provider, {}) if isinstance(day.by_provider, dict) else {}
            if not isinstance(values, dict):
                continue
            sent, clicked = values.get('sent'), values.get('clicked')
            if type(sent) is int and sent >= policy.value('CRM_GUARD_CLICK_MIN_SAMPLE') and type(clicked) is int and 0 <= clicked <= sent:
                baseline.append(clicked / sent)
        if len(baseline) < policy.value('CRM_GUARD_CLICK_BASELINE_DAYS'):
            continue
        typical = median(baseline)
        rate = cohort.filter(first_clicked_at__isnull=False, first_clicked_at__lte=now).count() / count
        threshold = typical * policy.value('CRM_GUARD_CLICK_RATIO')
        if typical <= 0 or rate >= threshold:
            continue
        result = guardian.detect(None, guardian.Signal('click_anomaly', 'amber', rate, threshold, count,
            'throttle_provider', evidence_key=scopes.evidence_key(cohort.values_list('pk', flat=True)),
            scope='mature_clicks', provider=provider), stream=stream, now=now)
        if result:
            results.append(result)
    return results


def evaluate(*, now):
    """Return firing lifecycle incidents to the existing amber recovery loop."""
    if not policy.value('CRM_LIFECYCLE_ENABLED'):
        return []
    results = []
    for blast in Blast.objects.filter(stream__transactional=False).filter(
            Q(status__in=guardian.LIVE) | Q(sent_at__gte=now - timedelta(days=policy.value('CRM_GUARD_LOOKBACK_DAYS')))):
        results.extend(quarantine(blast, now=now))
    for stream in SendingStream.objects.filter(transactional=False):
        results.extend(click_anomalies(stream, now=now))
    return results
