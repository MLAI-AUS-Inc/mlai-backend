"""Small owner-facing monitor observations, with no provider payloads or credentials."""

from django.core.cache import cache

from crm.models import CrmAuditEvent


def record(stream, kind, result, *, now):
    """Callers must supply a bounded, redacted result, never an upstream response."""
    payload = {**result, 'checked_at': now.isoformat()}
    identity = f'{stream.pk}:{kind}'
    CrmAuditEvent.objects.create(action='sync', target_type='deliverability_check', target_id=identity,
        detail={'kind': kind, 'stream': stream.key, 'result': payload})
    cache.set('crm:deliverability:observation:' + identity, payload, timeout=86400)
    return payload


def latest(stream, kind):
    identity = f'{stream.pk}:{kind}'
    cached = cache.get('crm:deliverability:observation:' + identity)
    if isinstance(cached, dict):
        return cached
    row = CrmAuditEvent.objects.filter(target_type='deliverability_check', target_id=identity).order_by('-created_at', '-pk').first()
    return row.detail.get('result', {}) if row else {'status': 'not_checked'}
