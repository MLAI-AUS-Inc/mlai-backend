"""Atomic linked-phase operations. No model calls and no browser authority."""
import hmac
from django.db import transaction
from django.utils import timezone
from .cybertest_models import CybertestCheckout as Receipt
from .cybertest_policy import CheckoutError, binding_digest
from .cybertest_delivery import delivery_settlement

ACTIVE = ['quoted', 'authorized', 'charged', 'started']


def lock_upgrade_parent(intent, *, test_mode, checkout_id):
    """Called inside create_quote's transaction; parent lock serializes offers."""
    from . import cybertest_checkout as checkout
    contract = intent['purchase']
    parent = checkout._get(contract['parentCheckoutId'], lock=True)
    snapshot = parent.phase_contract
    if (parent.id == checkout._uuid(checkout_id) or not snapshot or snapshot.get('phase') != 'basic'
            or parent.status != 'started' or parent.test_mode != test_mode
            or not parent.settlement or parent.settlement.get('delivery', {}).get('minimumMet') is not True
            or parent.settlement.get('charge_microroo') != 24_000_000
            or parent.user_id is None or str(parent.user_id) != intent.get('mlai_user_id')):
        raise CheckoutError('UPGRADE_UNAVAILABLE', 'A delivered basic scan from this payer is required.')
    original = {k: v for k, v in parent.binding.items() if k != 'purchase'}
    selected = {k: v for k, v in intent.items() if k != 'purchase'}
    if (not hmac.compare_digest(binding_digest(original), binding_digest(selected))
            or any(snapshot.get(k) != contract.get(k) for k in ('commitSha', 'sourceManifestSha256'))):
        raise CheckoutError('SCOPE_CHANGED', 'Upgrade the same company, source version and scan scope.')
    # Expiry releases only unpaid offers. A charged/dispatched child may still
    # be working or reconciling and must never be silently replaced.
    parent.upgrade_checkouts.filter(status__in=['quoted', 'authorized'], expires_at__lte=timezone.now()).exclude(
        pk=checkout._uuid(checkout_id)).update(status='cancelled', updated_at=timezone.now())
    if parent.upgrade_checkouts.filter(status__in=ACTIVE).exclude(pk=checkout._uuid(checkout_id)).exists():
        raise CheckoutError('UPGRADE_EXISTS', 'This basic scan already has an upgrade. Open its existing result or checkout.')
    return parent


@transaction.atomic
def settle_delivery(checkout_id, audit_id, evidence, costs):
    from . import cybertest_checkout as checkout
    row = checkout._get(checkout_id, lock=True)
    checkout._audit(row, audit_id)
    if not row.phase_contract or row.dispatched_at is None or row.status not in ('started', 'refunded'):
        raise CheckoutError('SETTLEMENT_REVIEW_REQUIRED', 'A dispatched phased scan is required.')
    result = delivery_settlement(row.phase_contract, evidence, str(row.audit_id),
                                 row.pricing_snapshot, costs, existing=row.settlement)
    if row.settlement is not None:
        return checkout.receipt_status(row)
    if not row.test_mode and result['refund_microroo']:
        if row.user_id is None:
            raise CheckoutError('SETTLEMENT_REVIEW_REQUIRED', 'The payer account is unavailable.')
        row.refund_ledger, _ = checkout.PointsService.refund(
            user=row.user, delta=row.points, source='TOOLS',
            description='Vibe Security phase did not meet its delivery minimum',
            created_by_slack_id=row.user.slack_id or '',
            idempotency_key=f'cybertest:delivery:refund:{row.id}',
            original_spend_key=f'cybertest:charge:{row.id}',
            reference_type='CYBERTEST_AUDIT', reference_id=str(row.audit_id))
        if result['charge_microroo']:
            retained, _ = checkout.PointsService.spend_microroo(
                user=row.user, delta_microroo=result['charge_microroo'], source='TOOLS',
                description='Vibe Security basic scan: actual model usage only',
                created_by_slack_id=row.user.slack_id or '',
                idempotency_key=f'cybertest:delivery:retained:{row.id}',
                reference_type='CYBERTEST_AUDIT', reference_id=str(row.audit_id))
            result = {**result, 'retained_ledger_id': str(retained.pk)}
    row.settlement = result
    row.delivery_evidence = evidence
    row.settled_at = timezone.now()
    row.failure_code = '' if result['delivery']['minimumMet'] else 'DELIVERY_MINIMUM_NOT_MET'
    if result['charge_microroo'] == 0:
        row.status = 'refunded'
    row.save(update_fields=['settlement', 'delivery_evidence', 'settled_at', 'failure_code', 'status', 'refund_ledger', 'updated_at'])
    return checkout.receipt_status(row)
