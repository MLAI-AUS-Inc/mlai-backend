"""Observed activity and dormant-list maintenance, without granting permission."""

import copy
import logging
from datetime import date, datetime, time, timedelta
from uuid import uuid4

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from crm.engagement_sources import source_time
from crm.models import Contact, CrmAuditEvent

from . import policy, volume

logger = logging.getLogger(__name__)
TARGET = 'crm_lifecycle_refresh'


def dormant_suppressed(attributes):
    """Unknown flag values hold marketing; only JSON false explicitly clears it."""
    return not isinstance(attributes, dict) or ('dormant_suppressed' in attributes and attributes['dormant_suppressed'] is not False)


def instant(value):
    """An explicit date is conservative midnight Melbourne; no missing date becomes now."""
    value = source_time(value)
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min, volume.MELBOURNE)
    return None


def newsletter_click(contact, now):
    """Use dated Substack export evidence; aggregate click/open counts are not activity."""
    value = getattr(contact, 'attributes', None)
    attrs = value if isinstance(value, dict) else {}
    stamps = []
    newsletter = attrs.get('engagement_newsletter')
    if isinstance(newsletter, dict) and newsletter.get('source') == 'substack_export':
        exported, clicked = instant(newsletter.get('exported_at')), instant(newsletter.get('last_clicked_at'))
        if exported and clicked and clicked <= exported <= now:
            stamps.append(clicked)
    imported = attrs.get('engagement_import')
    columns = imported.get('source_columns') if isinstance(imported, dict) else None
    if isinstance(columns, dict):
        clicked = instant(columns.get('Last clicked at'))
        if clicked and clicked <= now:
            stamps.append(clicked)
    return max(stamps, default=None)


def observed_activity(contact, now):
    """Latest login, attendance, registration, click or reply; never an open."""
    stamps = [getattr(contact, key, None) for key in ('last_active_at', 'consent_reconfirmed_at',
        'last_attended_at', 'latest_registration', 'latest_click', 'latest_reply')]
    stamps += [getattr(getattr(contact, 'user', None), 'last_login', None), newsletter_click(contact, now)]
    return max((stamp for stamp in stamps if isinstance(stamp, datetime) and stamp.tzinfo and stamp <= now), default=None)


def refreshed_attributes(contact, active, *, now):
    """Only a verified action lifts a dormant flag; every other hold stays intact."""
    if not isinstance(contact.attributes, dict):
        return contact.attributes
    attrs = copy.deepcopy(contact.attributes)
    lifecycle = attrs.get('crm_lifecycle')
    sent = instant(lifecycle.get('reconfirmation_sent_at')) if isinstance(lifecycle, dict) else None
    if sent and sent <= now:
        acted = bool((active and active >= sent) or (contact.consent_reconfirmed_at and contact.consent_reconfirmed_at >= sent))
        if acted:
            attrs.pop('dormant_suppressed', None)
        elif now >= sent + timedelta(days=policy.value('CRM_RECONFIRMATION_RESPONSE_DAYS')):
            attrs['dormant_suppressed'] = True
    return attrs


def run_lifecycle_refresh(*, now=None):
    """One resumable daily batch, with contact locks and aggregate-only receipts."""
    if not policy.value('CRM_LIFECYCLE_ENABLED'):
        return {'status': 'disabled'}
    now = now or timezone.now()
    day = now.astimezone(volume.MELBOURNE).date().isoformat()
    claim, acquired = uuid4().hex, False
    try:
        acquired = cache.add('crm:lifecycle:lock', claim, timeout=policy.value('CRM_LIFECYCLE_PASS_LOCK_SECONDS'))
        if not acquired:
            return {'status': 'throttled'}
        receipt = CrmAuditEvent.objects.filter(target_type=TARGET, target_id=day).order_by('-created_at', '-pk').first()
        if receipt and receipt.detail.get('status') == 'complete':
            return {'status': 'complete', 'changed': 0}
        cursor = receipt.detail.get('cursor') if receipt else None
        from .planner import _activity_queryset
        with transaction.atomic():
            query = Contact.objects.all()
            if cursor:
                query = query.filter(pk__gt=cursor)
            contacts = list(_activity_queryset(query, now=now).select_for_update(of=('self',)).order_by('pk')[:policy.value('CRM_LIFECYCLE_BATCH_SIZE')])
            activity_changes, attribute_changes, changed_ids = [], [], set()
            for contact in contacts:
                active = observed_activity(contact, now)
                attrs = refreshed_attributes(contact, active, now=now)
                domain = contact.email.rpartition('@')[2].lower()
                if (active, domain) != (contact.last_active_at, contact.email_domain):
                    contact.last_active_at, contact.email_domain = active, domain
                    contact.updated_at = now
                    activity_changes.append(contact); changed_ids.add(contact.pk)
                if attrs != contact.attributes:
                    contact.attributes, contact.updated_at = attrs, now
                    attribute_changes.append(contact); changed_ids.add(contact.pk)
            if activity_changes:
                Contact.objects.bulk_update(activity_changes, ['last_active_at', 'email_domain', 'updated_at'], batch_size=250)
            if attribute_changes:
                Contact.objects.bulk_update(attribute_changes, ['attributes', 'updated_at'], batch_size=250)
            cursor = str(contacts[-1].pk) if contacts else cursor
            remaining = bool(cursor and Contact.objects.filter(pk__gt=cursor).exists())
            result = {'status': 'partial' if remaining else 'complete', 'processed': len(contacts), 'changed': len(changed_ids), 'cursor': cursor}
            CrmAuditEvent.objects.create(action='sync', target_type=TARGET, target_id=day, detail=result)
        return {k: v for k, v in result.items() if k != 'cursor'}
    except Exception as exc:
        logger.warning('CRM lifecycle refresh deferred: %s', type(exc).__name__)
        return {'status': 'deferred'}
    finally:
        try:
            if acquired and cache.get('crm:lifecycle:lock') == claim:
                cache.delete('crm:lifecycle:lock')
        except Exception:
            logger.warning('CRM lifecycle lock cleanup deferred')
