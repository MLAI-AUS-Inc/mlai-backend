"""Idempotent Apple delivery and ordered refunds, separate from physical points."""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .apple_models import AppleIapNotification, AppleIapTransaction
from .apple_verification import InvalidApplePurchase, VerifiedApplePurchase
from .models import Ledger, PointsAccount

SANDBOX_LIFETIME_LIMIT = 100_000_000


def sandbox_account(user):
    """Only explicitly configured synthetic accounts receive sandbox credits."""
    return str(user.community_chat_profile_id) in getattr(settings, "APPLE_IAP_SANDBOX_ACCOUNT_TOKENS", ())


def _validate_owner(user, purchase):
    if not user.is_active or user.community_chat_profile_id != purchase.account_token:
        raise InvalidApplePurchase("apple_account_mismatch")
    expected = "Sandbox" if sandbox_account(user) else "Production"
    if purchase.environment != expected:
        raise InvalidApplePurchase("apple_environment_mismatch")


def apply_digital_credit(account, amount):
    """Cancel nonmonetary refund debt before making digital credit spendable."""
    offset = min(account.digital_refund_debt_microroo, amount)
    account.digital_refund_debt_microroo -= offset
    account.digital_balance_microroo += amount - offset


@transaction.atomic
def deliver_purchase(*, user, purchase: VerifiedApplePurchase, event_kind="DELIVERY", event_signed_at=None):
    """Apply a verified transaction under the global user-first lock order.

    Delivery cannot undo a refund. Only a later signed REFUND_REVERSED event
    restores it. Stale events are acknowledged without replaying their effect.
    """
    user = get_user_model().objects.select_for_update().get(pk=user.pk)
    _validate_owner(user, purchase)
    signed_at = event_signed_at or purchase.signed_at
    row = AppleIapTransaction.objects.select_for_update().filter(
        environment=purchase.environment, transaction_id=purchase.transaction_id,
    ).first()
    if row:
        if (row.user_id != user.pk or row.account_token != purchase.account_token
                or row.product_id != purchase.product_id or row.quantity != purchase.quantity
                or row.original_transaction_id != purchase.original_transaction_id):
            raise InvalidApplePurchase("apple_transaction_conflict")
        if signed_at < row.latest_signed_at:
            return row
        if row.status == "refunded" and event_kind != "REFUND_REVERSED" and purchase.revoked_at is None and event_kind not in ("REFUND", "REVOKE"):
            return row
        if row.status == "refunded" and signed_at == row.latest_signed_at and event_kind == "REFUND_REVERSED":
            return row
    else:
        row = AppleIapTransaction.objects.create(
            user=user, environment=purchase.environment, transaction_id=purchase.transaction_id,
            original_transaction_id=purchase.original_transaction_id, account_token=purchase.account_token,
            product_id=purchase.product_id, quantity=purchase.quantity,
            price_milliunits=purchase.price_milliunits, currency=purchase.currency,
            purchased_at=purchase.purchased_at, latest_signed_at=signed_at,
            payload_digest=purchase.payload_digest,
        )
    account, _ = PointsAccount.objects.get_or_create(user=user)
    account = PointsAccount.objects.select_for_update().get(pk=account.pk)
    revoked = event_kind in ("REFUND", "REVOKE") or (
        purchase.revoked_at is not None and event_kind != "REFUND_REVERSED"
    )
    desired = "refunded" if revoked else "credited"
    if row.status != desired:
        amount = purchase.microroo
        if revoked:
            if row.granted_microroo:
                removed = min(account.digital_balance_microroo, amount)
                account.digital_balance_microroo -= removed
                account.digital_refund_debt_microroo += amount - removed
                row.reversal_ledger = Ledger.objects.create(
                    user=user, kind="ADJUST", source="purchased_topup", delta=0, delta_microroo=0,
                    digital_delta_microroo=-amount, description="Apple digital purchase refunded",
                    reference_type="APPLE_IAP", reference_id=str(row.pk),
                )
        else:
            if purchase.environment == "Sandbox" and not row.granted_microroo:
                granted = AppleIapTransaction.objects.filter(user=user, environment="Sandbox").aggregate(
                    total=Sum("granted_microroo"),
                )["total"] or 0
                if granted + amount > SANDBOX_LIFETIME_LIMIT:
                    raise InvalidApplePurchase("sandbox_purchase_limit")
            apply_digital_credit(account, amount)
            row.grant_ledger = Ledger.objects.create(
                user=user, kind="ADJUST", source="purchased_topup", delta=0, delta_microroo=0,
                digital_delta_microroo=amount, description="Apple digital purchase delivered",
                reference_type="APPLE_IAP", reference_id=str(row.pk),
            )
            row.granted_microroo = amount
        row.status = desired
        account.save(update_fields=("digital_balance_microroo", "digital_refund_debt_microroo", "updated_at"))
    row.revoked_at = (purchase.revoked_at or signed_at) if revoked else None
    row.latest_signed_at = signed_at
    row.payload_digest = purchase.payload_digest
    row.save()
    return row


@transaction.atomic
def process_notification(notification):
    """Persist replay receipts and independently verified state changes."""
    purchase = notification.purchase
    user = None
    if purchase:
        user = get_user_model().objects.select_for_update().filter(
            community_chat_profile_id=purchase.account_token,
        ).first()
    receipt, _ = AppleIapNotification.objects.get_or_create(
        pk=notification.notification_id,
        defaults={"environment": notification.environment, "notification_type": notification.notification_type,
                  "subtype": notification.subtype, "signed_at": notification.signed_at,
                  "payload_digest": notification.payload_digest},
    )
    receipt = AppleIapNotification.objects.select_for_update().get(pk=receipt.pk)
    if receipt.payload_digest != notification.payload_digest:
        raise InvalidApplePurchase("apple_notification_conflict")
    if receipt.status == "completed":
        return receipt
    if purchase and user and user.is_active and notification.notification_type in ("ONE_TIME_CHARGE", "REFUND", "REFUND_REVERSED", "REVOKE"):
        receipt.transaction = deliver_purchase(
            user=user, purchase=purchase, event_kind=notification.notification_type,
            event_signed_at=notification.signed_at,
        )
    elif purchase and not user:
        # Never resurrect an erased account or reassign its purchase.
        receipt.error_code = "owner_unavailable"
    receipt.status = "completed"
    receipt.completed_at = timezone.now()
    receipt.save()
    return receipt
