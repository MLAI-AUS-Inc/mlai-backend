"""Bounded import MX observations and a resumable domain cache fill; no preference writes."""

from collections import Counter
from datetime import timedelta
import logging
from uuid import uuid4

from django.core.cache import cache
from django.utils import timezone

from crm.models import Contact, CrmAuditEvent, MailDomainInfo

from . import hygiene, policy, volume

logger = logging.getLogger(__name__)


def report(addresses, *, now):
    """Count independent hygiene findings; unknown domains never count as clean or no-MX."""
    infos = hygiene.domain_infos(addresses, now, max_new=policy.value('CRM_IMPORT_DOMAIN_MAX_NEW'))
    hygiene.remember(infos)
    counts = Counter()
    for address in addresses:
        domain = address.rpartition('@')[2].lower()
        info = infos.get(domain)
        counts['domain_unverified'] += int(info is None or info.has_mx is None)
        counts['no_mx'] += int(info is not None and info.has_mx is False)
        counts['disposable'] += int(hygiene.disposable(domain))
        counts['likely_typo'] += int(domain in hygiene.TYPO)
    return counts


def run_domain_hygiene(*, now=None):
    """Fill a small domain batch per pass, advancing past unknown answers instead of starving."""
    if not policy.value('CRM_LIFECYCLE_ENABLED'):
        return {'status': 'disabled'}
    now = now or timezone.now()
    claim, acquired = uuid4().hex, False
    try:
        acquired = cache.add('crm:import-hygiene:lock', claim, timeout=policy.value('CRM_LIFECYCLE_PASS_LOCK_SECONDS'))
        if not acquired:
            return {'status': 'throttled'}
        day = now.astimezone(volume.MELBOURNE).date().isoformat()
        receipt = CrmAuditEvent.objects.filter(target_type='crm_domain_hygiene', target_id=day).order_by('-created_at', '-pk').first()
        if receipt and receipt.detail.get('status') == 'complete':
            return {'status': 'complete', 'checked': 0}
        cursor = receipt.detail.get('cursor', '') if receipt else ''
        size = min(policy.value('CRM_IMPORT_DOMAIN_MAX_NEW'), policy.value('CRM_DOMAIN_CHECK_WORKERS'))
        known = MailDomainInfo.objects.filter(checked_at__gte=now - timedelta(days=policy.value('CRM_DOMAIN_CACHE_DAYS')),
            checked_at__lte=now, has_mx__isnull=False).values('domain')
        pending = Contact.objects.exclude(email_domain='').exclude(email_domain__in=known)
        domains = list(pending.filter(email_domain__gt=cursor).order_by('email_domain').values_list('email_domain', flat=True).distinct()[:size])
        if not domains and cursor:
            domains = list(pending.order_by('email_domain').values_list('email_domain', flat=True).distinct()[:size])
        infos = hygiene.domain_infos(['probe@' + domain for domain in domains], now, max_new=policy.value('CRM_IMPORT_DOMAIN_MAX_NEW'))
        hygiene.remember(infos)
        result = {'status': 'partial' if pending.exists() else 'complete', 'checked': len(infos),
            'unknown': sum(info.has_mx is None for info in infos.values()), 'cursor': domains[-1] if domains else ''}
        CrmAuditEvent.objects.create(action='sync', target_type='crm_domain_hygiene', target_id=day, detail=result)
        return {key: value for key, value in result.items() if key != 'cursor'}
    except Exception as exc:
        logger.warning('CRM domain hygiene deferred: %s', type(exc).__name__)
        return {'status': 'deferred'}
    finally:
        try:
            if acquired and cache.get('crm:import-hygiene:lock') == claim:
                cache.delete('crm:import-hygiene:lock')
        except Exception:
            logger.warning('CRM domain hygiene lock cleanup deferred')
