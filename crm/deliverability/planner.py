"""Consent-preserving hygiene, activity tiers and stable provider-interleaved snapshots."""

import hashlib
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import timedelta

from django.db.models import Case, Count, DateTimeField, OuterRef, Q, Subquery, When
from django.db.models.functions import Coalesce

from crm.engagement import engagement_rating
from crm.models import BlastDelivery, Contact, EventRegistration, InboundMessage

from . import hygiene, policy, volume


@dataclass
class Recipient:
    contact: Contact
    tier: str
    provider: str
    active_at: object
    stars: int


@dataclass
class Plan:
    recipients: list
    excluded: dict
    infos: dict
    summary: dict


def active_at(contact, now):
    """Trust observed activity, never opens; Phase 6 will persist the daily roll-up."""
    timestamps = [getattr(contact, key, None) for key in ('last_active_at', 'last_attended_at',
        'latest_registration', 'latest_click', 'latest_reply')]
    timestamps.append(getattr(getattr(contact, 'user', None), 'last_login', None))
    return max((stamp for stamp in timestamps if stamp and stamp.tzinfo and stamp <= now), default=None)


def tier_for(contact, now, *, stars=None):
    observed = active_at(contact, now)
    if stars is None:
        stars = (engagement_rating(contact.attributes) or {}).get('stars', 0)
    consent = contact.marketing_consent_at if contact.marketing_consent == 'granted' else None
    if consent and (not consent.tzinfo or consent > now):
        consent = None
    def recent(stamp, days):
        return stamp is not None and stamp >= now - timedelta(days=days)
    if recent(observed, policy.value('CRM_TIER_A_DAYS')) or stars >= policy.value('CRM_TIER_A_STARS') or recent(consent, policy.value('CRM_TIER_A_DAYS')):
        return 'A'
    if recent(observed, policy.value('CRM_TIER_B_DAYS')) or stars >= policy.value('CRM_TIER_B_STARS'):
        return 'B'
    if recent(observed, policy.value('CRM_DORMANT_DAYS')) or stars >= policy.value('CRM_TIER_C_STARS') or recent(consent, policy.value('CRM_DORMANT_DAYS')):
        return 'C'
    return 'D'


def interleave(recipients):
    """Weighted deficit scheduling preserves the input order within each provider."""
    queues = defaultdict(deque)
    for recipient in recipients:
        queues[recipient.provider].append(recipient)
    shares = {key: len(rows) for key, rows in queues.items()}
    served = Counter()
    total = len(recipients)
    result = []
    for position in range(total):
        provider = min((key for key, rows in queues.items() if rows),
            key=lambda key: (-(position + 1) * shares[key] / total + served[key], key))
        result.append(queues[provider].popleft())
        served[provider] += 1
    return result


def _activity_queryset(queryset):
    registrations = EventRegistration.objects.filter(contact_id=OuterRef('pk'), removed_at__isnull=True, registered_at__isnull=False).order_by('-registered_at').values('registered_at')[:1]
    clicks = BlastDelivery.objects.filter(contact_id=OuterRef('pk'), first_clicked_at__isnull=False).order_by('-first_clicked_at').values('first_clicked_at')[:1]
    replies = InboundMessage.objects.filter(contact_id=OuterRef('pk')).order_by('-received_at').values('received_at')[:1]
    return queryset.select_related('user').annotate(latest_registration=Subquery(registrations, output_field=DateTimeField()),
        latest_click=Subquery(clicks, output_field=DateTimeField()), latest_reply=Subquery(replies, output_field=DateTimeField()))


def prepare(blast, now, *, audience=None, cached_only=False):
    """Compute a read-only plan from the existing purpose/consent/suppression resolver."""
    from crm.blasts import resolve_audience
    audience = audience or resolve_audience(blast, now=now)
    contacts = list(_activity_queryset(audience.querysets['email']).order_by('pk'))
    infos = hygiene.domain_infos([contact.email for contact in contacts], now, cached_only=cached_only)
    excluded = Counter(audience.excluded.get('email') or {})
    by_tier = defaultdict(list)
    warnings = Counter()
    for contact in contacts:
        info = infos.get(contact.email.rpartition('@')[2].lower())
        reason = hygiene.exclusion(contact, info, blast.purpose)
        stars = (engagement_rating(contact.attributes) or {}).get('stars', 0)
        tier = 'A' if blast.purpose == 'event_update' else tier_for(contact, now, stars=stars)
        if reason is None and tier == 'D':
            reason = 'dormant'
        if reason:
            excluded[reason] += 1
            continue
        observed = active_at(contact, now)
        by_tier[tier].append(Recipient(contact, tier, info.provider, observed, stars))
        if contact.email.partition('@')[0].lower() in hygiene.WARN_ROLES:
            warnings['role_addresses'] += 1
    recipients = []
    for tier in ('A', 'B', 'C'):
        ordered = sorted(by_tier[tier], key=lambda row: (-row.stars, -(row.active_at.timestamp() if row.active_at else 0),
            hashlib.sha256(str(row.contact.pk).encode()).hexdigest()))
        recipients.extend(interleave(ordered))
    stream = blast.stream
    used_today, used_hour = volume.usage(stream, now)
    estimate = volume.estimate(stream, len(recipients), now, used_today=used_today, used_hour=used_hour,
        critical=volume.time_critical(blast, now))
    summary = {'people': len(recipients), 'excluded': dict(excluded),
        'tiers': {tier: len(by_tier[tier]) for tier in ('A', 'B', 'C')},
        'providers': dict(Counter(row.provider for row in recipients)), 'estimate': estimate,
        'warnings': [{'key': key, 'people': count} for key, count in warnings.items()]}
    if excluded['domain_unverified']:
        summary['warnings'].append({'key': 'domain_checks_pending', 'people': excluded['domain_unverified']})
    return Plan(recipients, dict(excluded), infos, summary)


def plan_preview(blast, now):
    return prepare(blast, now).summary


def plan_snapshot(blast, now, *, prepared=None):
    """Write each ranked email row once inside queue_blast's existing transaction."""
    prepared = prepared or prepare(blast, now, cached_only=True)
    rows = [BlastDelivery(blast=blast, contact=row.contact, address=row.contact.email, channel='email',
        consent_basis='event_registration' if blast.purpose == 'event_update' else 'explicit' if row.contact.marketing_consent == 'granted' else 'inferred_attendance',
        send_rank=index, tier=row.tier, provider=row.provider) for index, row in enumerate(prepared.recipients)]
    BlastDelivery.objects.bulk_create(rows)
    hygiene.remember(prepared.infos)
    blast.plan = prepared.summary
    return prepared


def eligible_rows(rows, blast, now):
    """Compute blocked contacts once, rather than recounting history for every row."""
    other = BlastDelivery.objects.filter(channel='email').exclude(blast_id=blast.pk).filter(
        Q(status__in=('submitting', 'sent', 'delivered', 'read', 'bounced', 'undelivered', 'unknown'))
        | ~Q(provider_id='') | Q(status='queued', wave__status='loading')
        | Q(status='unsubscribed', error_code=''))
    other = other.exclude(status='cancelled').annotate(frequency_at=Coalesce('wave__started_at',
        Case(When(wave__status='loading', then='wave__reserved_at'), output_field=DateTimeField()),
        'delivered_at', 'blast__sent_at', 'created_at'))
    if blast.purpose == 'event_update':
        recent = other.filter(blast__purpose='event_update', blast__event_id=blast.event_id,
            frequency_at__gte=now - timedelta(hours=policy.value('CRM_EVENT_FREQ_GAP_HOURS')))
        return rows.exclude(contact_id__in=recent.values('contact_id'))
    recent = other.filter(blast__purpose='marketing', frequency_at__gte=now - timedelta(days=policy.value('CRM_FREQ_WINDOW_DAYS')))
    maximum = recent.order_by().values('contact_id').annotate(n=Count('pk')).filter(n__gte=policy.value('CRM_FREQ_MAX_PER_7D')).values('contact_id')
    gap = recent.filter(frequency_at__gte=now - timedelta(days=policy.value('CRM_FREQ_MIN_GAP_DAYS')))
    return rows.exclude(contact_id__in=gap.values('contact_id')).exclude(contact_id__in=maximum)


def frequency_block(delivery, now):
    """Provider handoffs and in-flight reservations count; unstarted cancelled waves do not."""
    return not eligible_rows(BlastDelivery.objects.filter(pk=delivery.pk), delivery.blast, now).exists()
