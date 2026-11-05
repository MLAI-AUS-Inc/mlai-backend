"""Shared system sender and best-effort, payload-free transport observations."""

import logging
import re
import uuid
from contextlib import contextmanager

from django.conf import settings
from django.db import transaction
from django.utils import timezone

DEFAULT_FROM = 'MLAI <notifications@notify.mlai.au>'
logger = logging.getLogger(__name__)


def sender():
    """Return the single configured identity for newly prepared system messages."""
    return str(getattr(settings, 'MLAI_SYSTEM_FROM_EMAIL', '') or DEFAULT_FROM).strip()


def resend_key():
    """Prefer the notify-restricted credential; retain the legacy rollout fallback."""
    return str(getattr(settings, 'CRM_NOTIFY_API_KEY', '') or getattr(settings, 'RESEND_API_KEY', '') or '').strip()


def provider_id(response):
    """Only retain an opaque provider receipt, never arbitrary response text."""
    if not isinstance(response, dict):
        return ''
    value = response.get('delivery_id') or response.get('id') or response.get('message_id') or ''
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value) else ''


class Attempt:
    """A transport explicitly marks acceptance after validating its response."""

    def __init__(self):
        self.ok = False
        self.receipt = ''

    def accepted(self, response=None):
        """Record API acceptance; this does not claim inbox delivery."""
        self.ok, self.receipt = True, provider_id(response)


def _record(identity, detail):
    try:
        from crm.models import CrmAuditEvent
        # A telemetry write failure must not poison a caller's transaction.
        with transaction.atomic():
            CrmAuditEvent.objects.create(action='send', target_type='transactional_mail', target_id=identity, detail=detail)
    except Exception as exc:
        logger.warning('System email telemetry unavailable error_type=%s', type(exc).__name__)


@contextmanager
def observe(provider, kind='notification'):
    """Observe an actual provider call without changing its result or exception."""
    attempt = Attempt()
    enabled = getattr(settings, 'CRM_TRANSACTIONAL_MONITOR_ENABLED', False) is True
    enabled = enabled and provider in ('resend', 'customerio') and kind in ('signin', 'notification')
    identity, started = uuid.uuid4().hex, timezone.now()
    detail = {'provider': provider, 'kind': kind, 'started_at': started.isoformat()}
    if enabled:
        _record(identity, {**detail, 'phase': 'attempt'})
    try:
        yield attempt
    finally:
        if enabled:
            _record(identity, {**detail, 'phase': 'result', 'ok': attempt.ok,
                'provider_message_id': attempt.receipt, 'finished_at': timezone.now().isoformat()})
