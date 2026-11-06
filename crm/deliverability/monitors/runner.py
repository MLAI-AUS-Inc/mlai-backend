"""One bounded due monitor per minute, with independent retry and observation state."""

import logging
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

from crm.models import CrmAuditEvent, SendingStream

from .. import observations, policy, volume
from . import config_check, dns, heartbeat, postmaster, reputation, resend
from .http import budget

logger = logging.getLogger(__name__)


def run_deliverability_monitors(*, now=None, refresh=False, stream_key=None):
    """No failure escapes into the shared scheduler. Flags default to disabled."""
    if not policy.value('CRM_MONITORS_ENABLED'):
        return {'status': 'disabled'}
    now = now or timezone.now()
    try:
        if not refresh and not cache.add('crm:deliverability:monitors', 1, timeout=policy.value('CRM_MONITOR_INTERVAL_SECONDS')):
            return {'status': 'throttled'}
        streams = SendingStream.objects.select_related('mailbox').order_by('key')
        if stream_key:
            streams = streams.filter(key=stream_key)
        local = now.astimezone(volume.MELBOURNE)
        jobs = []
        for stream in streams:
            for name, fn, interval in (('dns', dns.check, policy.value('CRM_DNS_CHECK_SECONDS')),
                    ('config', config_check.check, policy.value('CRM_CONFIG_CHECK_SECONDS')),
                    ('reputation', reputation.check, policy.value('CRM_REPUTATION_MAX_AGE_HOURS') * 3600)):
                key = f'crm:deliverability:monitor:{name}:{stream.pk}'
                if refresh or not cache.get(key):
                    jobs.append((stream, name, fn, interval, key))
            if not stream.transactional:
                key = f'crm:deliverability:monitor:suppressions:{stream.pk}'
                if not cache.get(key):
                    jobs.append((stream, 'suppressions', resend.suppression_page, 86400, key))
            hours = [hour for hour in policy.value('CRM_POSTMASTER_HOURS') if hour <= local.hour]
            if hours:
                key = f'crm:deliverability:monitor:postmaster:{stream.pk}:{local.date()}:{max(hours)}'
                if not cache.get(key):
                    jobs.append((stream, 'postmaster', postmaster.check, 86400, key))
            if (local.hour, local.minute) >= (policy.value('CRM_SEED_HEARTBEAT_HOUR'), policy.value('CRM_SEED_HEARTBEAT_MINUTE')):
                key = f'crm:deliverability:monitor:heartbeat:{stream.pk}:{local.date()}'
                if policy.value('CRM_SEED_HEARTBEAT_ENABLED') and not cache.get(key):
                    jobs.append((stream, 'heartbeat', heartbeat.start, 86400, key))
        # Missing checks sort first. Afterwards the least recently observed due check wins,
        # so repeated DNS work cannot starve daily reputation or Postmaster jobs.
        jobs.sort(key=lambda job: (0 if job[1] == 'dns' and cache.get('crm:deliverability:urgent-dns:' + str(job[0].pk)) else 1, observations.latest(job[0], job[1]).get('checked_at', ''), job[0].key, job[1]))
        if not jobs:
            return {'status': 'idle'}
        stream, name, fn, interval, key = jobs[0]
        if not cache.add(key, 1, timeout=interval):
            return {'status': 'throttled'}
        try:
            with budget(policy.value('CRM_MONITOR_PASS_SECONDS')):
                result = fn(stream, now=now)
                if name == 'dns':
                    cache.delete('crm:deliverability:urgent-dns:' + str(stream.pk))
                if name == 'postmaster' and not stream.transactional:
                    try:
                        postmaster.root_check(stream, now=now)
                    except Exception:
                        observations.record(stream, 'root_postmaster', {'status': 'unavailable'}, now=now)
            if result.get('status') == 'partial':
                cache.set(key, 1, timeout=policy.value('CRM_MONITOR_INTERVAL_SECONDS'))
            return {'status': 'completed', 'check': name, 'stream': stream.key, 'result': result.get('status', 'complete')}
        except Exception as exc:
            logger.warning('CRM monitor deferred kind=%s error=%s', name, type(exc).__name__)
            observations.record(stream, name, {'status': 'unavailable'}, now=now)
            cache.set(key, 1, timeout=policy.value('CRM_MONITOR_INTERVAL_SECONDS'))
            return {'status': 'deferred', 'check': name, 'stream': stream.key}
    except Exception as exc:
        logger.warning('CRM monitor runner deferred error=%s', type(exc).__name__)
        return {'status': 'deferred'}
