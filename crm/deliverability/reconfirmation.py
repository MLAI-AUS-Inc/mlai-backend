"""Owner-started dormant preference checks and explicit, signed confirmations."""

import calendar
import copy
import hashlib
import json
import math
from uuid import UUID
from datetime import datetime, time, timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.core import signing
from django.db import transaction
from django.db.models import Exists, OuterRef, Q, Sum
from django.utils import timezone

from crm.blasts import BlastError
from crm.models import Blast, BlastDelivery, BlastWave, Contact, ContactConsentEvent, CrmAuditEvent, SendingStream
from crm.queries import banned

from . import lifecycle, policy, volume
from .streams import is_owner, stream_configured

PATH = '/api/v1/crm/preferences/reconfirm/'
SALT = 'crm.dormant-reconfirmation.v1'
ATTRIBUTE = 'crm_reconfirmation_url'
GO_URL = '{{ .Subscriber.Attribs.crm_reconfirmation_url }}'
BUTTON = '\n\n[Yes, keep me subscribed](' + GO_URL + ')'


def is_reconfirmation(blast):
    value = (getattr(blast, 'stats', None) or {}).get('crm_lifecycle')
    return isinstance(value, dict) and value.get('kind') == 'reconfirmation'


def basis(contact):
    """Bind an opaque link to this address and current affirmative permission."""
    def stamp(value):
        return value.timestamp() if isinstance(value, datetime) and value.tzinfo else None
    values = [contact.email.lower(), contact.marketing_consent, contact.marketing_consent_source,
        stamp(contact.marketing_consent_at), stamp(contact.consent_reconfirmed_at)]
    return hashlib.sha256(json.dumps(values, separators=(',', ':')).encode()).hexdigest()


def consent_cutoff(now):
    """A calendar-month cutoff, including leap-day adjustment."""
    months = now.year * 12 + now.month - 1 - policy.value('CRM_RECONFIRMATION_CONSENT_MONTHS')
    year, month = divmod(months, 12); month += 1
    return now.replace(year=year, month=month, day=min(now.day, calendar.monthrange(year, month)[1]))


def proof_time(contact, now):
    return max((value for value in (contact.marketing_consent_at, contact.consent_reconfirmed_at)
        if isinstance(value, datetime) and value.tzinfo and value <= now), default=None)


def with_bounce_history(query):
    return query.annotate(reconfirmation_bounced=Exists(ContactConsentEvent.objects.filter(
        contact_id=OuterRef('pk'), channel='email', kind='bounced')) | Exists(
        BlastDelivery.objects.filter(contact_id=OuterRef('pk'), channel='email', bounce_kind='hard')))


def exclusion(contact, now, *, history=None):
    """No inferred permission, stale proof, hard bounce or repeated reconfirmation."""
    if contact.email_status != 'active' or contact.marketing_consent != 'granted':
        return 'reconfirmation_not_eligible'
    proof = proof_time(contact, now)
    if proof is None or proof < consent_cutoff(now):
        return 'consent_too_old'
    if history is None:
        history = getattr(contact, 'reconfirmation_bounced', None)
    if history is None:
        history = with_bounce_history(Contact.objects.filter(pk=contact.pk)).values_list('reconfirmation_bounced', flat=True).get()
    if history:
        return 'previous_hard_bounce'
    attrs = contact.attributes if isinstance(contact.attributes, dict) else {}
    previous = attrs.get('crm_lifecycle')
    if isinstance(previous, dict) and previous.get('reconfirmation_basis') == basis(contact) and previous.get('reconfirmation_sent_at'):
        return 'reconfirmation_already_sent'
    return None


def handoff_exclusion(contact, now):
    """Re-read all activity and bounce evidence just before loading the provider list."""
    from .planner import _activity_queryset, tier_for
    current = with_bounce_history(_activity_queryset(Contact.objects.filter(pk=contact.pk), now=now)).get()
    if tier_for(current, now) != 'D':
        return 'not_dormant'
    return exclusion(current, now)


def stream_block(stream):
    """Preference checks begin only on an active marketing stream."""
    if not policy.value('CRM_LIFECYCLE_ENABLED'):
        return 'lifecycle_disabled'
    if stream.state != 'active':
        return 'reconfirmation_stream_not_ready'
    if math.floor(volume.budget(stream) * policy.value('CRM_RECONFIRMATION_DAILY_FRACTION')) < policy.value('CRM_CANARY_MIN'):
        return 'reconfirmation_capacity_low'
    return None


def admission(blast):
    return stream_block(blast.stream)


def daily_used(stream, now):
    start = datetime.combine(now.astimezone(volume.MELBOURNE).date(), time.min, volume.MELBOURNE)
    rows = BlastWave.objects.filter(blast__stream=stream, blast__stats__crm_lifecycle__kind='reconfirmation').filter(
        Q(started_at__gte=start, started_at__lt=start + timedelta(days=1)) |
        Q(started_at__isnull=True, status='loading', reserved_at__gte=start, reserved_at__lt=start + timedelta(days=1)))
    return rows.aggregate(n=Sum('size'))['n'] or 0


def allowance(blast, now, available):
    cap = math.floor(volume.budget(blast.stream) * policy.value('CRM_RECONFIRMATION_DAILY_FRACTION'))
    return min(available, max(0, cap - daily_used(blast.stream, now)))


def prepare_draft(key, *, actor, now=None):
    """Prepare a reviewable draft; creation cannot queue or send it."""
    if not is_owner(actor):
        raise BlastError('owner_required', 403)
    if not policy.value('CRM_LIFECYCLE_ENABLED'):
        raise BlastError('lifecycle_disabled', 404)
    now = now or timezone.now()
    with transaction.atomic():
        stream = SendingStream.objects.select_for_update(of=('self',)).select_related('mailbox').get(key=key)
        if stream.transactional or 'marketing' not in stream.purposes or not stream_configured(stream):
            raise BlastError('stream_not_for_reconfirmation')
        if stream.state != 'active':
            raise BlastError('reconfirmation_stream_not_ready', 409)
        existing = Blast.objects.filter(stream=stream, status='draft', stats__crm_lifecycle__kind='reconfirmation').first()
        if existing:
            return existing
        draft = Blast.objects.create(created_by=actor, stream=stream, mailbox=stream.mailbox,
            purpose='marketing', channels=['email'], subject='Do you still want emails from MLAI?',
            body_markdown="It's been a while since we heard from you. Would you like to keep receiving MLAI community emails?\n\n"
                "Choose Yes below if you'd like to stay subscribed. If you don't respond within 30 days, we'll leave you out of community emails. "
                "You can unsubscribe at any time using the link at the end of this message.",
            segment_definition={'match': 'all', 'rules': [], 'exclude': []},
            stats={'crm_lifecycle': {'kind': 'reconfirmation', 'prepared_at': now.isoformat()}})
        CrmAuditEvent.objects.create(actor=actor, action='edit', target_type='blast', target_id=str(draft.pk),
            detail={'operation': 'reconfirmation_draft_prepared'})
        return draft


def url_for(delivery, *, now):
    payload = {'contact': str(delivery.contact_id), 'delivery': str(delivery.pk), 'basis': basis(delivery.contact),
        'expires': int((now + timedelta(days=policy.value('CRM_RECONFIRMATION_RESPONSE_DAYS'))).timestamp())}
    token = signing.Signer(salt=SALT).sign_object(payload, compress=True)
    return settings.DEFAULT_BACKEND_URL.rstrip('/') + PATH + '?' + urlencode({'token': token})


def handed_off(wave, *, now):
    """Record the actual handoff once, without treating a queued draft as activity."""
    ids = wave.deliveries.filter(status__in=('submitting', 'sent', 'delivered', 'read', 'unknown')).values_list('contact_id', 'pk')
    deliveries = dict(ids)
    for contact in Contact.objects.select_for_update().filter(pk__in=deliveries).order_by('pk'):
        if not isinstance(contact.attributes, dict):
            continue
        attrs = copy.deepcopy(contact.attributes)
        state = attrs.get('crm_lifecycle') if isinstance(attrs.get('crm_lifecycle'), dict) else {}
        delivery_id = str(deliveries[contact.pk])
        if state.get('reconfirmation_delivery') == delivery_id:
            continue
        attrs['crm_lifecycle'] = {**state, 'reconfirmation_sent_at': now.isoformat(),
            'reconfirmation_basis': basis(contact), 'reconfirmation_delivery': delivery_id}
        contact.attributes = attrs
        contact.save(update_fields=['attributes', 'updated_at'])


def inspect_token(token, *, now):
    """Validate a bounded opaque token, never a recipient identity supplied by a client."""
    try:
        if not isinstance(token, str) or len(token) > 2048:
            raise ValueError
        payload = signing.Signer(salt=SALT).unsign_object(token)
        if (not isinstance(payload, dict) or set(payload) != {'contact', 'delivery', 'basis', 'expires'}
                or type(payload.get('expires')) is not int or payload['expires'] <= now.timestamp()
                or not isinstance(payload.get('basis'), str) or len(payload['basis']) != 64
                or not isinstance(payload.get('contact'), str) or not isinstance(payload.get('delivery'), str)
                or not payload['delivery'].isascii() or not payload['delivery'].isdecimal()
                or not 0 < int(payload['delivery']) <= 9223372036854775807):
            raise ValueError
        UUID(payload['contact'])
        return payload
    except (signing.BadSignature, ValueError, TypeError, KeyError):
        raise BlastError('reconfirmation_link_unavailable', 400) from None


def confirm(token, *, now=None, apply=False):
    """GET can inspect; only a CSRF-protected, explicit POST calls apply=True."""
    if not policy.value('CRM_LIFECYCLE_ENABLED'):
        raise BlastError('reconfirmation_link_unavailable', 400)
    now = now or timezone.now()
    payload = inspect_token(token, now=now)
    try:
        with transaction.atomic():
            person = Contact.objects.select_for_update().annotate(is_banned=banned()).get(pk=payload['contact'])
            delivery = BlastDelivery.objects.select_related('blast', 'wave').get(pk=payload['delivery'], contact=person, channel='email')
            if (person.is_banned or person.email_status != 'active' or person.marketing_consent != 'granted'
                    or not is_reconfirmation(delivery.blast) or not delivery.wave_id or not delivery.wave.started_at or delivery.wave.started_at > now
                    or with_bounce_history(Contact.objects.filter(pk=person.pk)).get().reconfirmation_bounced):
                raise ValueError
            state = person.attributes.get('crm_lifecycle') if isinstance(person.attributes, dict) else None
            if not isinstance(state, dict):
                raise ValueError
            if state.get('confirmed_delivery') == str(delivery.pk):
                return 'already_confirmed'
            if payload.get('basis') != basis(person) or state.get('reconfirmation_delivery') != str(delivery.pk):
                raise ValueError
            if not apply:
                return 'ready'
            attrs = copy.deepcopy(person.attributes)
            attrs.pop('dormant_suppressed', None)
            attrs['crm_lifecycle'] = {**state, 'confirmed_delivery': str(delivery.pk), 'confirmed_at': now.isoformat()}
            person.consent_reconfirmed_at, person.last_active_at, person.attributes = now, now, attrs
            person.save(update_fields=['consent_reconfirmed_at', 'last_active_at', 'attributes', 'updated_at'])
            ContactConsentEvent.objects.create(contact=person, channel='email', kind='granted', source='signed_reconfirmation',
                occurred_at=now, detail={'delivery': str(delivery.pk), 'previous_permission': 'granted'})
            return 'confirmed'
    except (Contact.DoesNotExist, BlastDelivery.DoesNotExist, ValueError, KeyError, TypeError):
        raise BlastError('reconfirmation_link_unavailable', 400) from None
