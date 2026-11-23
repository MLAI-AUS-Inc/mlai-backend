"""A durable, private Chat notification for a completed security report."""
import hashlib
import logging
import time
from datetime import timedelta
from uuid import UUID

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from agent_runtime import native_crypto
from community_chat.models import CommunityChatDevice, DeviceBindingStatus
from community_chat.onboarding import has_community_access
from content_factory import website_notification_transport as transport
from content_factory.models import ContentFactoryCallbackEvent
from founder_tools.models import VibeRaisingCompany
from founder_tools.services import user_may_use_organization

from . import cybertest_checkout as checkout
from .cybertest_delivery import assess_delivery
from .cybertest_models import CybertestCheckout, CybertestCompanyBinding
from .cybertest_policy import CheckoutError

logger = logging.getLogger(__name__)
EVENT_TYPE = 'cybertest_report_ready'
PREFIX = 'security-report-ready:'
LEASE = timedelta(minutes=10)
RETRY_WINDOW = timedelta(days=2)
MAX_DEVICES = 8


def staging_enabled():
    return (getattr(settings, 'APP_ENV', '') == 'chat-staging'
            and getattr(settings, 'CYBERTEST_ENTRY_STAGING_ENABLED', False)
            and getattr(settings, 'CYBERTEST_BILLING_MODE', '') == 'test'
            and getattr(settings, 'CYBERTEST_STAGING_COMPLETION_NOTICES_ENABLED', False)
            and getattr(settings, 'COMMUNITY_CHAT_FRONTEND_URL', '').rstrip('/') == 'https://chat-staging.mlai.au')


def enabled():
    if getattr(settings, 'APP_ENV', '') == 'chat-staging':
        return staging_enabled()
    return getattr(settings, 'CYBERTEST_COMPLETION_NOTICES_ENABLED', False)


def receipt_allowed(receipt):
    if not enabled():
        return False
    staging = staging_enabled()
    if receipt.test_mode != staging:
        return False
    return not staging or receipt.binding.get('mlai_company_id') in settings.CYBERTEST_STAGING_COMPANY_IDS


def enqueue(receipt):
    """Create one outbox row per root after trusted history acknowledged full delivery."""
    if not receipt_allowed(receipt):
        return {'status': 'disabled'}
    ContentFactoryCallbackEvent.objects.get_or_create(
        event_id=PREFIX + str(receipt.audit_id), defaults={'event_type': EVENT_TYPE})
    return {'status': 'queued'}


def completed(checkout_id, audit_id, evidence):
    """Accept cumulative completion on an existing receipt without touching its ledger."""
    receipt = checkout._get(checkout_id)
    checkout._audit(receipt, audit_id)
    if (receipt.dispatched_at is None or receipt.status not in ('started', 'refunded')
            or receipt.settlement is None or not receipt.phase_contract):
        raise CheckoutError('COMPLETION_UNAVAILABLE', 'A settled, dispatched check is required.')
    delivered = assess_delivery(receipt.phase_contract, evidence, str(receipt.audit_id))
    if delivered['status'] != 'full':
        raise CheckoutError('INCOMPLETE_REVIEW', 'A saved report and all completed checks are required.')
    return enqueue(receipt)


def recipient(audit_id):
    """Resolve the original payer and verified Chat devices against current access."""
    receipt = CybertestCheckout.objects.select_related('user').filter(audit_id=audit_id, test_mode=staging_enabled()).first()
    if not receipt or not receipt_allowed(receipt) or not receipt.user or str(receipt.user_id) != receipt.binding.get('mlai_user_id'):
        return None
    user = receipt.user
    if not has_community_access(user):
        return None
    company = VibeRaisingCompany.objects.select_related('organization').filter(
        pk=receipt.binding.get('mlai_company_id'), profile__user=user, closed_at__isnull=True).first()
    if not company or (company.organization_id and not user_may_use_organization(user, company.organization)):
        return None
    if not CybertestCompanyBinding.objects.filter(
            company=company, workspace_id=receipt.binding.get('workspace_id'),
            is_active=True, verified_at__isnull=False).exists():
        return None
    devices = tuple(CommunityChatDevice.objects.filter(
        user=user, status=DeviceBindingStatus.VERIFIED, revoked_at__isnull=True,
        verified_at__isnull=False).order_by('public_key').values_list('public_key', flat=True).distinct()[:MAX_DEVICES+1])
    if not 1 <= len(devices) <= MAX_DEVICES:
        return None
    return str(company.pk), devices


def send_claimed(row, claim):
    audit_id = UUID(row.event_id.removeprefix(PREFIX))
    target = recipient(audit_id)
    if target is None:
        return 'skipped'
    company_id, devices = target
    notice_id = hashlib.sha256(row.event_id.encode()).hexdigest()
    options = {'staging': True} if staging_enabled() else {}
    channel = transport.open_dm(devices, notice_id, **options)
    # Bind retries to exactly the same message/event ID before any publish.
    # A changed signer, set of devices or DM channel must not produce a second notice.
    from .cybertest_views import _origin
    link = f"{_origin('COMMUNITY_CHAT_FRONTEND_URL')}/my-startup/security/checks/{audit_id}?company_id={company_id}"
    event = native_crypto.sign_event(
        transport.notifier_key(**options), kind=9, created_at=int(row.created_at.timestamp()),
        tags=[['h', str(UUID(channel))], ['mlai-security-report', notice_id]] + [['p', key] for key in devices],
        content=f'Your security review is ready.\n\n[Open your report]({link})')
    if row.job_id and row.job_id != event['id']:
        return 'skipped'
    if not claim.filter(job_id=row.job_id).update(job_id=event['id']):
        return 'superseded'
    if target != recipient(audit_id):
        return 'skipped'
    transport.post_event(event, **options)
    return 'sent'


def drain():
    """Lease a bounded batch. A crash leaves the row eligible for replay."""
    if not enabled():
        return {'status': 'disabled'}
    now = timezone.now()
    pending = ContentFactoryCallbackEvent.objects.filter(event_type=EVENT_TYPE, processed_at__isnull=True)
    due = pending.filter(Q(claimed_at__isnull=True) | Q(claimed_at__lt=now-LEASE))
    deadline = time.monotonic() + 20
    rows = list(due.order_by('created_at', 'pk')[:10])
    counts = {'sent': 0, 'retry': 0, 'skipped': 0, 'superseded': 0}
    for row in rows:
        if time.monotonic() >= deadline:
            break
        token = timezone.now()
        if not due.filter(pk=row.pk).update(claimed_at=token):
            continue
        claim = pending.filter(pk=row.pk, claimed_at=token)
        try:
            outcome = 'skipped' if row.created_at < now-RETRY_WINDOW else send_claimed(row, claim)
        except (CheckoutError, transport.NotificationTransportError) as error:
            logger.warning('cybertest completion DM deferred audit=%s reason=%s', row.event_id,
                           getattr(error, 'code', None) or error.outcome)
            counts['retry'] += 1
            continue
        if outcome != 'superseded':
            claim.update(processed_at=timezone.now())
        counts[outcome] += 1
    return counts
