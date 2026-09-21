"""Digital-first spending with exact, non-convertible service refund allocation."""

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction

from .apple_purchases import apply_digital_credit
from .models import Ledger, PointsAccount

DIGITAL_SOURCES = frozenset(("CONTENT_FACTORY", "TOOLS"))


def _ledger(**values):
    from .services import IdempotencyConflictError
    try:
        with transaction.atomic():
            return Ledger.objects.create(**values)
    except IntegrityError:
        if Ledger.objects.filter(idempotency_key=values["idempotency_key"]).exists():
            raise IdempotencyConflictError("That transaction key belongs to another operation") from None
        raise


def spendable_balance(account):
    """Return digital-tool capacity, suspending spending on reversed used credit."""
    if account.digital_refund_debt_microroo:
        return 0
    return account.balance_microroo + account.digital_balance_microroo


@transaction.atomic
def spend(*, user, amount, source, description, actor, key, reference_type=None,
          reference_id=None, allow_reserved_turn_id=None):
    """Spend digital credits first, recording every source for a later reversal."""
    from .services import PointsService, InsufficientBalanceError
    if source not in DIGITAL_SOURCES or amount <= 0:
        raise ValueError("A positive digital-service spend is required")
    get_user_model().objects.select_for_update().get(pk=user.pk)
    existing = Ledger.objects.filter(idempotency_key=key).first()
    if existing:
        PointsService._validate_idempotent_ledger(
            existing, user=user, kind="SPEND", source=source, delta=None,
            delta_microroo=-amount, reference_type=reference_type, reference_id=reference_id,
        )
        return existing, False
    account, _ = PointsAccount.objects.get_or_create(user=user)
    account = PointsAccount.objects.select_for_update().get(pk=account.pk)
    PointsService._ensure_microroo_account(account)
    available = max(spendable_balance(account) - PointsService._reserved_microroo(
        user, exclude_turn_id=allow_reserved_turn_id,
    ), 0)
    if available < amount:
        raise InsufficientBalanceError("Insufficient available digital-service points")
    digital = min(amount, account.digital_balance_microroo)
    general = amount - digital
    purchased = min(general, account.purchased_topup_balance_microroo)
    account.digital_balance_microroo -= digital
    account.balance_microroo -= general
    PointsService._debit_account_microroo_balances(account, general)
    account.lifetime_spent_microroo += amount
    PointsService._sync_legacy_account(account)
    ledger = _ledger(
        user=user, kind="SPEND", source=source, delta=-(amount // 1_000_000),
        delta_microroo=-amount, digital_delta_microroo=-digital,
        purchased_delta_microroo=-purchased, description=description,
        created_by_slack_id=actor, idempotency_key=key,
        reference_type=reference_type, reference_id=reference_id,
    )
    account.save()
    return ledger, True


@transaction.atomic
def refund(*, user, original_key, amount, source, description, actor, key,
           reference_type=None, reference_id=None):
    """Reverse one original digital-service charge once, preserving all buckets."""
    from .services import PointsService, IdempotencyConflictError
    get_user_model().objects.select_for_update().get(pk=user.pk)
    original = Ledger.objects.select_for_update().filter(idempotency_key=original_key).first()
    if (original is None or original.user_id != user.pk or original.kind != "SPEND"
            or original.source != source or source not in DIGITAL_SOURCES
            or original.delta_microroo != -amount or amount <= 0):
        raise ValueError("Refund must match an original digital-service charge")
    existing = Ledger.objects.filter(idempotency_key=key).first()
    if existing:
        PointsService._validate_idempotent_ledger(
            existing, user=user, kind="REFUND", source=source, delta=None,
            delta_microroo=amount, reference_type=reference_type, reference_id=reference_id,
        )
        if existing.refund_of_id not in (None, original.pk):
            raise IdempotencyConflictError("Refund key belongs to another charge")
        return existing, False
    if original.service_refunds.exists():
        raise IdempotencyConflictError("This charge has already been refunded")
    account = PointsAccount.objects.select_for_update().get(user=user)
    PointsService._ensure_microroo_account(account)
    digital = -original.digital_delta_microroo
    purchased = -(original.purchased_delta_microroo or 0)
    general = amount - digital
    if digital < 0 or purchased < 0 or general < purchased:
        raise ValueError("Original point allocation is invalid")
    apply_digital_credit(account, digital)
    account.balance_microroo += general
    account.purchased_topup_balance_microroo += purchased
    account.earned_balance_microroo += general - purchased
    account.lifetime_spent_microroo = max(0, account.lifetime_spent_microroo - amount)
    PointsService._sync_legacy_account(account)
    result = _ledger(
        user=user, kind="REFUND", source=source, delta=amount // 1_000_000,
        delta_microroo=amount, digital_delta_microroo=digital,
        purchased_delta_microroo=purchased, refund_of=original, description=description,
        created_by_slack_id=actor, idempotency_key=key,
        reference_type=reference_type, reference_id=reference_id,
    )
    account.save()
    return result, True
