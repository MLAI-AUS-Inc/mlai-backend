"""
Points System Services - Business Logic Layer

This module provides safe, idempotent operations for the points system.
All write operations use database transactions and idempotency keys to prevent
race conditions and duplicate transactions.
"""
import calendar
import logging
import re
import time
import uuid
from collections import OrderedDict
from datetime import date, datetime, timedelta
from typing import Optional, Tuple
import requests
from django.conf import settings
from django.db import (
    IntegrityError,
    OperationalError,
    connection,
    models,
    transaction,
)
from django.db.models import Count, Q
from django.utils import timezone

from .models import (
    PointsAccount, Ledger, BoostPostAdmission, Task, TaskAssignment, TaskSubmission, TaskActivity,
    CoworkingBooking, CoworkingDayCapacity,
    OfficeManagerAssignment, OfficeManagerDay,
    RewardsCatalog, RewardRedemption, PointsAdmin, PointsPurchase
)
from .permissions import (
    is_points_admin, require_admin, 
    PermissionDeniedError, InsufficientBalanceError, IdempotencyConflictError
)
from core.models import User

logger = logging.getLogger(__name__)


DATABASE_TRANSACTION_RETRY_ATTEMPTS = 3


def _retryable_transaction_error(exc: OperationalError) -> bool:
    """Return true only for PostgreSQL deadlock/serialization aborts."""
    cause = getattr(exc, "__cause__", None)
    code = (
        getattr(exc, "pgcode", None)
        or getattr(exc, "sqlstate", None)
        or getattr(cause, "pgcode", None)
        or getattr(cause, "sqlstate", None)
    )
    return code in {"40P01", "40001"}


class CoworkingBatchBookingError(ValueError):
    """Raised when an admin coworking batch fails preflight."""

    def __init__(self, message: str, errors: Optional[list[dict]] = None):
        super().__init__(message)
        self.errors = errors or []


class _CancellationOwnerChanged(RuntimeError):
    """Internal signal to restart after a concurrent identity merge."""


class PointsPurchaseService:
    """Business rules for Top-up Roo Points purchases."""

    DEFAULT_TERMS_VERSION = 'roo-points-terms-2026-05-04'
    DEFAULT_PRIVACY_VERSION = 'privacy-2026-05-04'
    DEFAULT_TERMS_ACCEPTANCE_TEXT = (
        'I understand that Roo Points are not money, have no cash value, are not '
        'refundable except where required by law, and cannot be transferred or sold.'
    )

    # NOTE: pack ids are opaque (kept stable across the points-doubling change so
    # in-flight purchases, the Slack reverse-map, and the frontend "popular" key
    # keep working); the id number no longer matches the points it grants.
    ROO_TOPUP_PACKS = {
        'topup_5': {
            'points': 10,
            'amount_cents': 1999,
            'currency': 'aud',
            'label': '10 Top-up Roo Points',
        },
        'topup_10': {
            'points': 20,
            'amount_cents': 3699,
            'currency': 'aud',
            'label': '20 Top-up Roo Points',
        },
        'topup_25': {
            'points': 50,
            'amount_cents': 6399,
            'currency': 'aud',
            'label': '50 Top-up Roo Points',
        },
    }
    MAX_POINTS_PER_PURCHASE = 25
    MAX_POINTS_PER_ROLLING_YEAR = 50
    MAX_SPENDABLE_BALANCE = 100
    MIN_ACCOUNT_AGE_DAYS = 7
    LOOKBACK_DAYS = 365

    @staticmethod
    def get_pack_config(pack_id: str) -> dict:
        cleaned_pack_id = (pack_id or '').strip()
        if cleaned_pack_id not in PointsPurchaseService.ROO_TOPUP_PACKS:
            allowed = ', '.join(PointsPurchaseService.ROO_TOPUP_PACKS.keys())
            raise ValueError(f"Unsupported top-up pack. Allowed packs: {allowed}")
        return PointsPurchaseService.ROO_TOPUP_PACKS[cleaned_pack_id]

    @staticmethod
    def frontend_checkout_page_url(purchase: PointsPurchase) -> str:
        frontend_base_url = getattr(settings, 'DEFAULT_FRONTEND_URL', 'https://mlai.au').rstrip('/')
        return f"{frontend_base_url}/roo/topup/{purchase.id}"

    @staticmethod
    def stripe_return_url(purchase: PointsPurchase, outcome: str) -> str:
        return f"{PointsPurchaseService.frontend_checkout_page_url(purchase)}?checkout={outcome}"

    @staticmethod
    @transaction.atomic
    def create_purchase(
        slack_user_id: str,
        pack_id: str,
        *,
        purchase_from: Optional[dict] = None,
        manual_balance_approval: bool = False,
        checkout_request_id: Optional[str] = None,
    ) -> PointsPurchase:
        cleaned_slack_user_id = (slack_user_id or '').strip()
        if not cleaned_slack_user_id:
            raise ValueError("slack_user_id is required")

        cleaned_pack_id = (pack_id or '').strip()
        pack = PointsPurchaseService.get_pack_config(cleaned_pack_id)

        user = PointsService.get_user_by_slack_id(cleaned_slack_user_id)
        if not user:
            raise PermissionDeniedError("A linked user account is required for top-up purchases")

        PointsPurchaseService.validate_purchase_limits(
            user,
            pack['points'],
            manual_balance_approval=manual_balance_approval,
        )

        origin = dict(purchase_from or {})
        origin.setdefault('source', 'slack')
        origin['slack_user_id'] = cleaned_slack_user_id

        cleaned_request_id = (checkout_request_id or '').strip() or None
        if cleaned_request_id and len(cleaned_request_id) > 255:
            raise ValueError("checkout_request_id must be 255 characters or fewer")

        purchase_defaults = {
            'user': user,
            'slack_user_id': cleaned_slack_user_id,
            'points_amount': pack['points'],
            'amount_cents': pack['amount_cents'],
            'currency': pack['currency'],
            'purchase_from': origin,
        }
        if cleaned_request_id:
            purchase, created = PointsPurchase.objects.get_or_create(
                checkout_request_id=cleaned_request_id,
                pack_id=cleaned_pack_id,
                defaults=purchase_defaults,
            )
            if not created and purchase.user_id != user.id:
                raise PermissionDeniedError(
                    "That top-up checkout request belongs to a different user"
                )
            return purchase

        return PointsPurchase.objects.create(
            pack_id=cleaned_pack_id,
            checkout_request_id=None,
            **purchase_defaults,
        )

    @staticmethod
    @transaction.atomic
    def create_purchase_for_user(
        user: User,
        pack_id: str,
        *,
        purchase_from: Optional[dict] = None,
    ) -> PointsPurchase:
        """Create a pending purchase for an already-authenticated web user.

        Unlike ``create_purchase`` (which resolves a user from a Slack id for the
        Roo bot), this is the entry point for the dashboard checkout flow: the
        user is taken directly from the authenticated request and a linked Slack
        account is not required.
        """
        if user is None or not getattr(user, "is_authenticated", False):
            raise PermissionDeniedError("Authentication is required for top-up purchases")

        cleaned_pack_id = (pack_id or '').strip()
        pack = PointsPurchaseService.get_pack_config(cleaned_pack_id)

        PointsPurchaseService.validate_purchase_limits(user, pack['points'])

        origin = dict(purchase_from or {})
        origin.setdefault('source', 'web')

        return PointsPurchase.objects.create(
            user=user,
            slack_user_id=(getattr(user, 'slack_id', '') or ''),
            pack_id=cleaned_pack_id,
            points_amount=pack['points'],
            amount_cents=pack['amount_cents'],
            currency=pack['currency'],
            purchase_from=origin,
        )

    @staticmethod
    @transaction.atomic
    def create_checkout_session(
        purchase: PointsPurchase,
        terms_version_accepted: Optional[str] = None,
        privacy_version_accepted: Optional[str] = None,
        *,
        collect_terms_in_checkout: bool = False,
    ) -> dict:
        if collect_terms_in_checkout:
            cleaned_terms_version = (
                terms_version_accepted
                or getattr(
                    settings,
                    'ROO_POINTS_TERMS_VERSION',
                    PointsPurchaseService.DEFAULT_TERMS_VERSION,
                )
                or ''
            ).strip()
            cleaned_privacy_version = (
                privacy_version_accepted
                or getattr(
                    settings,
                    'ROO_POINTS_PRIVACY_VERSION',
                    PointsPurchaseService.DEFAULT_PRIVACY_VERSION,
                )
                or ''
            ).strip()
        else:
            cleaned_terms_version = (terms_version_accepted or '').strip()
            cleaned_privacy_version = (privacy_version_accepted or '').strip()
        if not cleaned_terms_version or not cleaned_privacy_version:
            raise ValueError("terms_version_accepted and privacy_version_accepted are required")

        purchase = PointsPurchase.objects.select_for_update().get(pk=purchase.pk)
        if purchase.status != 'pending':
            raise ValueError(f"Points purchase is {purchase.status} and cannot start Checkout")
        if purchase.expires_at <= timezone.now():
            raise ValueError("Points purchase has expired")
        if purchase.stripe_checkout_session_id and purchase.stripe_checkout_session_url:
            return {
                'purchase': purchase,
                'checkout_session_id': purchase.stripe_checkout_session_id,
                'checkout_session_url': purchase.stripe_checkout_session_url,
            }

        PointsPurchaseService.validate_purchase_limits(purchase.user, purchase.points_amount)
        pack = PointsPurchaseService.get_pack_config(purchase.pack_id)

        stripe_secret_key = (getattr(settings, 'STRIPE_SECRET_KEY', '') or '').strip()
        if not stripe_secret_key:
            raise RuntimeError("Stripe is not configured for Roo Points top-ups")

        metadata = {
            'points_purchase_id': str(purchase.id),
            'mlai_user_id': str(purchase.user_id),
            'slack_user_id': purchase.slack_user_id,
            'pack_id': purchase.pack_id,
            'points_amount': str(purchase.points_amount),
            'terms_version': cleaned_terms_version,
            'privacy_version': cleaned_privacy_version,
            'checkout_consent_mode': (
                'stripe' if collect_terms_in_checkout else 'frontend'
            ),
        }
        data = {
            'mode': 'payment',
            'client_reference_id': str(purchase.id),
            'success_url': PointsPurchaseService.stripe_return_url(purchase, 'success'),
            'cancel_url': PointsPurchaseService.stripe_return_url(purchase, 'cancelled'),
            'expires_at': str(int(purchase.expires_at.timestamp())),
            'line_items[0][quantity]': '1',
            'line_items[0][price_data][currency]': purchase.currency,
            'line_items[0][price_data][unit_amount]': str(purchase.amount_cents),
            'line_items[0][price_data][product_data][name]': pack['label'],
        }
        if collect_terms_in_checkout:
            data['consent_collection[terms_of_service]'] = 'required'
            data['custom_text[terms_of_service_acceptance][message]'] = str(
                getattr(
                    settings,
                    'ROO_POINTS_TERMS_ACCEPTANCE_TEXT',
                    PointsPurchaseService.DEFAULT_TERMS_ACCEPTANCE_TEXT,
                )
                or PointsPurchaseService.DEFAULT_TERMS_ACCEPTANCE_TEXT
            ).strip()
        for key, value in metadata.items():
            data[f'metadata[{key}]'] = value
            data[f'payment_intent_data[metadata][{key}]'] = value

        try:
            response = requests.post(
                'https://api.stripe.com/v1/checkout/sessions',
                auth=(stripe_secret_key, ''),
                headers={'Idempotency-Key': f'roo-points-purchase:{purchase.id}'},
                data=data,
                timeout=20,
            )
        except requests.RequestException as exc:
            logger.warning(
                "Stripe Checkout Session request failed for PointsPurchase %s: %s",
                purchase.id,
                exc.__class__.__name__,
            )
            raise RuntimeError("Stripe Checkout Session request failed") from exc
        if response.status_code >= 400:
            try:
                error_data = response.json()
                message = error_data.get('error', {}).get('message') or response.text
            except ValueError:
                message = response.text
            raise RuntimeError(f"Stripe Checkout Session creation failed: {message}")

        session = response.json()
        checkout_session_id = session.get('id')
        checkout_session_url = session.get('url')
        if not checkout_session_id or not checkout_session_url:
            raise RuntimeError("Stripe Checkout Session response was missing id or url")

        purchase.stripe_checkout_session_id = checkout_session_id
        purchase.stripe_checkout_session_url = checkout_session_url
        update_fields = [
            'stripe_checkout_session_id',
            'stripe_checkout_session_url',
            'updated_at',
        ]
        if collect_terms_in_checkout:
            purchase_metadata = dict(purchase.metadata or {})
            purchase_metadata.update(
                {
                    'checkout_consent_mode': 'stripe',
                    'terms_version_presented': cleaned_terms_version,
                    'privacy_version_presented': cleaned_privacy_version,
                }
            )
            purchase.metadata = purchase_metadata
            update_fields.append('metadata')
        else:
            purchase.terms_version_accepted = cleaned_terms_version
            purchase.terms_accepted_at = timezone.now()
            purchase.privacy_version_accepted = cleaned_privacy_version
            purchase.privacy_accepted_at = purchase.terms_accepted_at
            update_fields.extend(
                [
                    'terms_version_accepted',
                    'terms_accepted_at',
                    'privacy_version_accepted',
                    'privacy_accepted_at',
                ]
            )
        purchase.save(update_fields=update_fields)

        return {
            'purchase': purchase,
            'checkout_session_id': checkout_session_id,
            'checkout_session_url': checkout_session_url,
        }

    @staticmethod
    def validate_purchase_limits(
        user: Optional[User],
        points_amount: int,
        *,
        now: Optional[datetime] = None,
        manual_balance_approval: bool = False,
    ) -> None:
        """
        Validate a Top-up Roo Points purchase.

        The previous policy guardrails (Slack linkage, 25-point per-purchase cap,
        7-day account age, 50-point rolling-year limit, and 100-point spendable
        cap) have intentionally been removed: any authenticated user may buy any
        available pack as often as they like. Only baseline correctness checks
        remain. The ``now`` and ``manual_balance_approval`` parameters are kept
        for call-site compatibility but are no longer used.

        Raises:
            PermissionDeniedError: when the caller is anonymous.
            ValueError: when points_amount is not positive.
        """
        if user is None or not getattr(user, "is_authenticated", False):
            raise PermissionDeniedError("A linked user account is required for top-up purchases")

        if points_amount <= 0:
            raise ValueError("points_amount must be positive")


class PointsService:
    """
    Service class for points operations.
    All methods are idempotent and transaction-safe.
    """

    MICROROO_PER_ROO = 1_000_000

    @staticmethod
    def roo_to_microroo(value: int) -> int:
        return int(value) * PointsService.MICROROO_PER_ROO

    @staticmethod
    def microroo_to_legacy_whole(value: int) -> int:
        """Return whole spendable Roo for compatibility APIs."""
        return max(int(value), 0) // PointsService.MICROROO_PER_ROO

    @staticmethod
    def _validate_idempotent_ledger(
        existing: Ledger,
        *,
        user: User,
        kind: str,
        source: str,
        delta: Optional[int],
        delta_microroo: int,
        reference_type: Optional[str],
        reference_id: Optional[str],
    ) -> None:
        """Reject an idempotency key that belongs to another operation.

        An idempotency key identifies the complete balance mutation, not merely
        a ledger row.  Returning a colliding row for another user, amount, kind,
        source, or reference can make callers believe their requested mutation
        succeeded even though a different mutation won the key.
        """
        if (
            existing.user_id != user.id
            or existing.kind != kind
            or existing.source != source
            or (delta is not None and existing.delta != delta)
            or existing.delta_microroo != delta_microroo
            or existing.reference_type != reference_type
            or existing.reference_id != reference_id
        ):
            raise IdempotencyConflictError(
                "That idempotency key belongs to a different points operation"
            )

    @staticmethod
    def _ensure_microroo_account(account: PointsAccount) -> bool:
        """Hydrate microroo fields for rows built by legacy code/tests.

        Production rows are populated by the precision migration.  This lazy
        bridge keeps old call sites that instantiate ``PointsAccount`` using
        only integer Roo fields safe during the compatibility window.
        """
        if not account.microroo_initialized:
            account.balance_microroo = PointsService.roo_to_microroo(account.balance)
            account.earned_balance_microroo = PointsService.roo_to_microroo(account.earned_balance)
            account.purchased_topup_balance_microroo = PointsService.roo_to_microroo(account.purchased_topup_balance)
            account.lifetime_earned_microroo = PointsService.roo_to_microroo(account.lifetime_earned)
            account.lifetime_purchased_topup_microroo = PointsService.roo_to_microroo(account.lifetime_purchased_topup)
            account.lifetime_spent_microroo = PointsService.roo_to_microroo(account.lifetime_spent)
            account.expired_or_reversed_microroo = PointsService.roo_to_microroo(account.expired_or_reversed_points)
            account.microroo_initialized = True
            return True
        return False

    @staticmethod
    def _sync_legacy_account(account: PointsAccount) -> None:
        account.microroo_initialized = True
        account.balance = PointsService.microroo_to_legacy_whole(account.balance_microroo)
        account.earned_balance = PointsService.microroo_to_legacy_whole(account.earned_balance_microroo)
        account.purchased_topup_balance = PointsService.microroo_to_legacy_whole(account.purchased_topup_balance_microroo)
        account.lifetime_earned = PointsService.microroo_to_legacy_whole(account.lifetime_earned_microroo)
        account.lifetime_purchased_topup = PointsService.microroo_to_legacy_whole(account.lifetime_purchased_topup_microroo)
        account.lifetime_spent = PointsService.microroo_to_legacy_whole(account.lifetime_spent_microroo)
        account.expired_or_reversed_points = PointsService.microroo_to_legacy_whole(account.expired_or_reversed_microroo)

    @staticmethod
    def _reserved_microroo(user: User, *, exclude_turn_id=None) -> int:
        # Imported lazily so the foundational points service remains usable
        # while migrations are being applied.
        from .models import CodingTurn

        query = CodingTurn.objects.filter(
            user=user,
            status__in=(CodingTurn.Status.ACTIVE, CodingTurn.Status.RECONCILING),
        )
        if exclude_turn_id is not None:
            query = query.exclude(id=exclude_turn_id)
        return sum(
            max(turn.reserved_microroo - turn.settled_microroo - turn.released_microroo, 0)
            for turn in query.only(
                "reserved_microroo", "settled_microroo", "released_microroo"
            )
        )

    @staticmethod
    def get_available_microroo(user: User, *, exclude_turn_id=None) -> int:
        account = PointsService.get_or_create_account(user)
        initialized = PointsService._ensure_microroo_account(account)
        if initialized:
            account.save(
                update_fields=(
                    "balance_microroo",
                    "earned_balance_microroo",
                    "purchased_topup_balance_microroo",
                    "lifetime_earned_microroo",
                    "lifetime_purchased_topup_microroo",
                    "lifetime_spent_microroo",
                    "expired_or_reversed_microroo",
                    "microroo_initialized",
                    "updated_at",
                )
            )
        return max(
            account.balance_microroo
            - PointsService._reserved_microroo(user, exclude_turn_id=exclude_turn_id),
            0,
        )
    
    @staticmethod
    def get_or_create_account(user: User) -> PointsAccount:
        """
        Get or lazily create a PointsAccount for a user.
        
        Args:
            user: The User instance
            
        Returns:
            PointsAccount instance
        """
        account, _ = PointsAccount.objects.get_or_create(user=user)
        return account
    
    @staticmethod
    def get_balance(user: User) -> dict:
        """
        Get the current balance and lifetime stats for a user.
        
        Args:
            user: The User instance
            
        Returns:
            Dict with balance, lifetime_earned, lifetime_spent
        """
        account = PointsService.get_or_create_account(user)
        PointsService._ensure_microroo_account(account)
        return {
            'balance': account.balance,
            'earned_balance': account.earned_balance,
            'purchased_topup_balance': account.purchased_topup_balance,
            'lifetime_earned': account.lifetime_earned,
            'lifetime_purchased_topup': account.lifetime_purchased_topup,
            'lifetime_spent': account.lifetime_spent,
            'expired_or_reversed_points': account.expired_or_reversed_points,
            'balance_microroo': account.balance_microroo,
            'earned_balance_microroo': account.earned_balance_microroo,
            'purchased_topup_balance_microroo': account.purchased_topup_balance_microroo,
            'lifetime_earned_microroo': account.lifetime_earned_microroo,
            'lifetime_purchased_topup_microroo': account.lifetime_purchased_topup_microroo,
            'lifetime_spent_microroo': account.lifetime_spent_microroo,
            'expired_or_reversed_microroo': account.expired_or_reversed_microroo,
        }

    @staticmethod
    def _debit_account_balances(account: PointsAccount, delta: int) -> None:
        """
        Deduct spendable points while keeping earned/top-up sub-balances coherent.

        A full point-lot allocator can replace this when expiry-aware redemption
        lands. For now, spend purchased top-up points first, then earned points.
        """
        remaining = delta
        purchased_debit = min(account.purchased_topup_balance, remaining)
        account.purchased_topup_balance -= purchased_debit
        remaining -= purchased_debit

        earned_debit = min(account.earned_balance, remaining)
        account.earned_balance -= earned_debit

    @staticmethod
    def _debit_account_microroo_balances(
        account: PointsAccount,
        delta_microroo: int,
        *,
        purchased_delta_microroo: Optional[int] = None,
    ) -> None:
        if purchased_delta_microroo is not None:
            earned_delta_microroo = (
                delta_microroo - purchased_delta_microroo
            )
            if (
                purchased_delta_microroo < 0
                or earned_delta_microroo < 0
                or account.purchased_topup_balance_microroo
                < purchased_delta_microroo
                or account.earned_balance_microroo < earned_delta_microroo
            ):
                raise InsufficientBalanceError(
                    "The original purchased/earned point allocation is no "
                    "longer available"
                )
            account.purchased_topup_balance_microroo -= (
                purchased_delta_microroo
            )
            account.earned_balance_microroo -= earned_delta_microroo
            return
        remaining = delta_microroo
        purchased_debit = min(account.purchased_topup_balance_microroo, remaining)
        account.purchased_topup_balance_microroo -= purchased_debit
        remaining -= purchased_debit
        earned_debit = min(account.earned_balance_microroo, remaining)
        account.earned_balance_microroo -= earned_debit

    @staticmethod
    @transaction.atomic
    def spend_microroo(
        *,
        user: User,
        delta_microroo: int,
        source: str,
        description: str,
        created_by_slack_id: str,
        idempotency_key: str,
        reference_type: Optional[str] = None,
        reference_id: Optional[str] = None,
        allow_reserved_turn_id=None,
    ) -> Tuple[Ledger, bool]:
        """Spend an exact microroo amount without allowing a negative balance."""
        if delta_microroo <= 0:
            raise ValueError("Microroo spend delta must be positive")
        def validate_existing(existing: Ledger) -> None:
            PointsService._validate_idempotent_ledger(
                existing,
                user=user,
                kind="SPEND",
                source=source,
                # The legacy whole-Roo projection of a fractional debit depends
                # on the balance boundary crossed by the original call.  The
                # exact microroo delta below is the authoritative amount.
                delta=None,
                delta_microroo=-delta_microroo,
                reference_type=reference_type,
                reference_id=reference_id,
            )

        existing = Ledger.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            validate_existing(existing)
            return existing, False

        # Global mutation lock order starts with the user row. Identity merges
        # take the same lock before bookings/accounts, preventing FK/deadlock
        # cycles with concurrent ledger writes.
        User.objects.select_for_update().get(pk=user.pk)

        account = PointsAccount.objects.select_for_update().filter(user=user).first()
        if not account:
            account = PointsAccount.objects.create(user=user)
            account = PointsAccount.objects.select_for_update().get(user=user)
        PointsService._ensure_microroo_account(account)

        reserved_elsewhere = PointsService._reserved_microroo(
            user,
            exclude_turn_id=allow_reserved_turn_id,
        )
        available = max(account.balance_microroo - reserved_elsewhere, 0)
        if available < delta_microroo:
            raise InsufficientBalanceError(
                f"Insufficient microroo balance: {available} < {delta_microroo} required"
            )

        before_whole = PointsService.microroo_to_legacy_whole(account.balance_microroo)
        account.balance_microroo -= delta_microroo
        PointsService._debit_account_microroo_balances(account, delta_microroo)
        account.lifetime_spent_microroo += delta_microroo
        PointsService._sync_legacy_account(account)
        after_whole = account.balance
        legacy_delta = after_whole - before_whole

        try:
            # Use a savepoint: an idempotency race must not poison the outer
            # transaction before we can safely load the winning row.
            with transaction.atomic():
                ledger = Ledger.objects.create(
                    user=user,
                    delta=legacy_delta,
                    delta_microroo=-delta_microroo,
                    kind='SPEND',
                    source=source,
                    reference_type=reference_type,
                    reference_id=reference_id,
                    description=description,
                    created_by_slack_id=created_by_slack_id,
                    idempotency_key=idempotency_key,
                )
        except IntegrityError:
            existing = Ledger.objects.get(idempotency_key=idempotency_key)
            validate_existing(existing)
            return existing, False
        account.save()
        return ledger, True
    
    @staticmethod
    def get_user_by_slack_id(slack_id: str) -> Optional[User]:
        """
        Get a User by their Slack ID.
        
        Args:
            slack_id: The Slack user ID
            
        Returns:
            User instance or None if not found
        """
        try:
            return User.objects.get(slack_id=slack_id)
        except User.DoesNotExist:
            return None
    
    @staticmethod
    def get_admin_allowance_status(slack_id: str) -> dict:
        """
        Get the admin's weekly allowance status.
        
        Week resets on Monday (ISO week).
        
        Args:
            slack_id: Admin's Slack ID
            
        Returns:
            Dict with allowance, used, remaining, or error
        """
        from django.db import models as db_models
        
        try:
            admin = PointsAdmin.objects.get(slack_user_id=slack_id, is_active=True)
        except PointsAdmin.DoesNotExist:
            return {'error': 'Not a points admin'}
        if not is_points_admin(slack_id):
            return {'error': 'Not a points admin'}
        
        # Calculate start of current ISO week (Monday)
        today = timezone.now().date()
        start_of_week = today - timedelta(days=today.weekday())
        start_of_week_dt = timezone.make_aware(
            datetime.combine(start_of_week, datetime.min.time())
        )

        # Sum points awarded by this admin this week
        used = Ledger.objects.filter(
            kind='EARN',
            created_by_slack_id=slack_id,
            created_at__gte=start_of_week_dt
        ).aggregate(total=db_models.Sum('delta'))['total'] or 0
        
        return {
            'allowance': admin.weekly_allowance,
            'used': used,
            'remaining': admin.weekly_allowance - used,
        }
    
    @staticmethod
    @transaction.atomic
    def award(
        user: User,
        delta: int,
        source: str,
        description: str,
        created_by_slack_id: str,
        idempotency_key: str,
        reference_type: Optional[str] = None,
        reference_id: Optional[str] = None,
    ) -> Tuple[Ledger, bool]:
        """
        Award points to a user with idempotency protection.
        
        Args:
            user: The User to award points to
            delta: Points to award (must be positive)
            source: Source of points (TASK, EVENT, DONATION, etc.)
            description: Human-readable description
            created_by_slack_id: Slack ID of who initiated the award
            idempotency_key: Unique key to prevent duplicate awards
            reference_type: Type of referenced object (e.g., TASK_SUBMISSION)
            reference_id: ID of referenced object
            
        Returns:
            Tuple of (Ledger entry, created: bool)
            If idempotency_key already exists, returns existing entry with created=False
            
        Raises:
            ValueError: If delta is not positive
        """
        if delta <= 0:
            raise ValueError("Award delta must be positive")
        delta_microroo = PointsService.roo_to_microroo(delta)

        def validate_existing(existing: Ledger) -> None:
            PointsService._validate_idempotent_ledger(
                existing,
                user=user,
                kind="EARN",
                source=source,
                delta=delta,
                delta_microroo=delta_microroo,
                reference_type=reference_type,
                reference_id=reference_id,
            )

        # Check for existing entry with same idempotency key
        existing = Ledger.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            validate_existing(existing)
            return existing, False

        User.objects.select_for_update().get(pk=user.pk)
        
        # Lock the account row for update
        account = PointsAccount.objects.select_for_update().filter(user=user).first()
        if not account:
            account = PointsAccount.objects.create(user=user)
            # Re-fetch with lock
            account = PointsAccount.objects.select_for_update().get(user=user)
        PointsService._ensure_microroo_account(account)
        
        # Create ledger entry
        try:
            # The savepoint keeps the outer account transaction usable after a
            # concurrent request wins the ledger key's unique constraint.
            with transaction.atomic():
                ledger = Ledger.objects.create(
                    user=user,
                    delta=delta,
                    delta_microroo=delta_microroo,
                    kind='EARN',
                    source=source,
                    reference_type=reference_type,
                    reference_id=reference_id,
                    description=description,
                    created_by_slack_id=created_by_slack_id,
                    idempotency_key=idempotency_key,
                )
        except IntegrityError:
            # Race condition - another process created the entry
            existing = Ledger.objects.get(idempotency_key=idempotency_key)
            validate_existing(existing)
            return existing, False
        
        # Update account
        account.balance_microroo += delta_microroo
        account.earned_balance_microroo += delta_microroo
        account.lifetime_earned_microroo += delta_microroo
        PointsService._sync_legacy_account(account)
        account.save()
        
        return ledger, True

    @staticmethod
    @transaction.atomic
    def credit_purchased_topup(
        user: User,
        delta: int,
        description: str,
        idempotency_key: str,
        reference_type: Optional[str] = None,
        reference_id: Optional[str] = None,
        created_by_slack_id: str = "STRIPE",
    ) -> Tuple[Ledger, bool]:
        """
        Credit purchased top-up points without increasing contribution metrics.
        """
        if delta <= 0:
            raise ValueError("Top-up credit delta must be positive")
        delta_microroo = PointsService.roo_to_microroo(delta)

        def validate_existing(existing: Ledger) -> None:
            PointsService._validate_idempotent_ledger(
                existing,
                user=user,
                kind="EARN",
                source="purchased_topup",
                delta=delta,
                delta_microroo=delta_microroo,
                reference_type=reference_type,
                reference_id=reference_id,
            )

        existing = Ledger.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            validate_existing(existing)
            return existing, False

        User.objects.select_for_update().get(pk=user.pk)

        account = PointsAccount.objects.select_for_update().filter(user=user).first()
        if not account:
            account = PointsAccount.objects.create(user=user)
            account = PointsAccount.objects.select_for_update().get(user=user)
        PointsService._ensure_microroo_account(account)

        try:
            with transaction.atomic():
                ledger = Ledger.objects.create(
                    user=user,
                    delta=delta,
                    delta_microroo=delta_microroo,
                    kind='EARN',
                    source='purchased_topup',
                    reference_type=reference_type,
                    reference_id=reference_id,
                    description=description,
                    created_by_slack_id=created_by_slack_id,
                    idempotency_key=idempotency_key,
                )
        except IntegrityError:
            existing = Ledger.objects.get(idempotency_key=idempotency_key)
            validate_existing(existing)
            return existing, False

        account.balance_microroo += delta_microroo
        account.purchased_topup_balance_microroo += delta_microroo
        account.lifetime_purchased_topup_microroo += delta_microroo
        PointsService._sync_legacy_account(account)
        account.save()

        return ledger, True
    
    @staticmethod
    @transaction.atomic
    def spend(
        user: User,
        delta: int,
        source: str,
        description: str,
        created_by_slack_id: str,
        idempotency_key: str,
        reference_type: Optional[str] = None,
        reference_id: Optional[str] = None,
        purchased_delta_microroo: Optional[int] = None,
    ) -> Tuple[Ledger, bool]:
        """
        Spend points (deduct from balance) with balance check and idempotency.
        
        Args:
            user: The User spending points
            delta: Points to spend (must be positive, will be stored as negative)
            source: Source of spend (COWORKING, MERCH, etc.)
            description: Human-readable description
            created_by_slack_id: Slack ID of who initiated
            idempotency_key: Unique key to prevent duplicate spends
            reference_type: Type of referenced object
            reference_id: ID of referenced object
            
        Returns:
            Tuple of (Ledger entry, created: bool)
            
        Raises:
            ValueError: If delta is not positive
            InsufficientBalanceError: If user doesn't have enough points
        """
        if delta <= 0:
            raise ValueError("Spend delta must be positive")
        delta_microroo = PointsService.roo_to_microroo(delta)
        if (
            purchased_delta_microroo is not None
            and not 0 <= purchased_delta_microroo <= delta_microroo
        ):
            raise ValueError(
                "Purchased spend delta must be between zero and delta"
            )

        def validate_existing(existing: Ledger) -> None:
            PointsService._validate_idempotent_ledger(
                existing,
                user=user,
                kind="SPEND",
                source=source,
                delta=-delta,
                delta_microroo=-delta_microroo,
                reference_type=reference_type,
                reference_id=reference_id,
            )

        # Check for existing entry with same idempotency key
        existing = Ledger.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            validate_existing(existing)
            return existing, False

        User.objects.select_for_update().get(pk=user.pk)
        
        # Lock the account row for update
        account = PointsAccount.objects.select_for_update().filter(user=user).first()
        if not account:
            account = PointsAccount.objects.create(user=user)
            account = PointsAccount.objects.select_for_update().get(user=user)
        PointsService._ensure_microroo_account(account)
        
        # Check balance
        available_microroo = max(
            account.balance_microroo - PointsService._reserved_microroo(user),
            0,
        )
        if available_microroo < delta_microroo:
            raise InsufficientBalanceError(
                f"Insufficient balance: {PointsService.microroo_to_legacy_whole(available_microroo)} < {delta} required"
            )
        
        # Create ledger entry (negative delta)
        try:
            with transaction.atomic():
                ledger = Ledger.objects.create(
                    user=user,
                    delta=-delta,
                    delta_microroo=-delta_microroo,
                    kind='SPEND',
                    source=source,
                    reference_type=reference_type,
                    reference_id=reference_id,
                    description=description,
                    created_by_slack_id=created_by_slack_id,
                    idempotency_key=idempotency_key,
                )
        except IntegrityError:
            existing = Ledger.objects.get(idempotency_key=idempotency_key)
            validate_existing(existing)
            return existing, False
        
        # Update account
        account.balance_microroo -= delta_microroo
        PointsService._debit_account_microroo_balances(
            account,
            delta_microroo,
            purchased_delta_microroo=purchased_delta_microroo,
        )
        account.lifetime_spent_microroo += delta_microroo
        PointsService._sync_legacy_account(account)
        account.save()
        
        return ledger, True
    
    @staticmethod
    @transaction.atomic
    def refund(
        user: User,
        delta: int,
        source: str,
        description: str,
        created_by_slack_id: str,
        idempotency_key: str,
        reference_type: Optional[str] = None,
        reference_id: Optional[str] = None,
        purchased_delta: int = 0,
        purchased_delta_microroo: Optional[int] = None,
        reverse_lifetime_spent: bool = False,
    ) -> Tuple[Ledger, bool]:
        """
        Refund points to a user (restore previously spent points).

        ``purchased_delta_microroo`` preserves the exact original point buckets
        when the caller recorded the spend allocation. ``purchased_delta`` is
        retained for whole-Roo callers. ``reverse_lifetime_spent`` is reserved
        for true reversals such as a pre-start booking cancellation.
        Similar to award but uses REFUND kind for audit trail clarity.
        """
        if delta <= 0:
            raise ValueError("Refund delta must be positive")
        delta_microroo = PointsService.roo_to_microroo(delta)

        if purchased_delta_microroo is not None and purchased_delta:
            raise ValueError(
                "Specify either purchased_delta or purchased_delta_microroo, not both"
            )
        if purchased_delta_microroo is None:
            purchased_delta_microroo = PointsService.roo_to_microroo(
                purchased_delta
            )
        if (
            purchased_delta_microroo < 0
            or purchased_delta_microroo > delta_microroo
        ):
            raise ValueError(
                "Purchased refund delta must be between zero and delta"
            )

        def validate_existing(existing: Ledger) -> None:
            PointsService._validate_idempotent_ledger(
                existing,
                user=user,
                kind="REFUND",
                source=source,
                delta=delta,
                delta_microroo=delta_microroo,
                reference_type=reference_type,
                reference_id=reference_id,
            )

        existing = Ledger.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            validate_existing(existing)
            return existing, False

        User.objects.select_for_update().get(pk=user.pk)

        account = PointsAccount.objects.select_for_update().filter(user=user).first()
        if not account:
            account = PointsAccount.objects.create(user=user)
            account = PointsAccount.objects.select_for_update().get(user=user)
        PointsService._ensure_microroo_account(account)
        
        try:
            with transaction.atomic():
                ledger = Ledger.objects.create(
                    user=user,
                    delta=delta,
                    delta_microroo=delta_microroo,
                    kind='REFUND',
                    source=source,
                    reference_type=reference_type,
                    reference_id=reference_id,
                    description=description,
                    created_by_slack_id=created_by_slack_id,
                    idempotency_key=idempotency_key,
                )
        except IntegrityError:
            existing = Ledger.objects.get(idempotency_key=idempotency_key)
            validate_existing(existing)
            return existing, False
        
        account.balance_microroo += delta_microroo
        account.purchased_topup_balance_microroo += purchased_delta_microroo
        account.earned_balance_microroo += (
            delta_microroo - purchased_delta_microroo
        )
        if reverse_lifetime_spent:
            account.lifetime_spent_microroo = max(
                0,
                account.lifetime_spent_microroo - delta_microroo,
            )
        PointsService._sync_legacy_account(account)
        account.save()
        
        return ledger, True


class CoworkingService:
    """
    Service class for coworking bookings.
    """

    MAX_REPORT_DAYS = 366

    @staticmethod
    def validated_booking_debit_provenance(
        booking: CoworkingBooking,
    ) -> int:
        """Return the purchased allocation only for a verified booking debit."""
        ledger = booking.ledger_entry
        expected_microroo = PointsService.roo_to_microroo(
            max(0, int(booking.points_cost))
        )
        if (
            ledger is None
            or ledger.user_id != booking.user_id
            or ledger.kind != 'SPEND'
            or ledger.source != 'COWORKING'
            or ledger.delta_microroo != -expected_microroo
            or ledger.reference_type != 'COWORKING_BOOKING'
            or ledger.reference_id != str(booking.date)
        ):
            raise ValueError(
                'The original coworking charge could not be verified'
            )
        purchased_microroo = booking.purchased_points_cost_microroo
        if (
            purchased_microroo is None
            or not 0 <= purchased_microroo <= expected_microroo
        ):
            raise ValueError(
                'The original coworking charge allocation is unavailable'
            )
        return purchased_microroo

    @staticmethod
    @transaction.atomic
    def transfer_user_ownership_for_merge(
        *,
        source: User,
        target: User,
    ) -> tuple[int, int]:
        """Move bookings and their Office Manager ownership as one unit.

        An active target booking on the same date is an ambiguous authority,
        so the entire surrounding account merge must fail rather than deleting
        the source-owned booking or assignment through ``CASCADE``.
        """
        if source.pk == target.pk:
            return 0, 0
        locked_users = {
            user.pk: user
            for user in User.objects.select_for_update()
            .filter(pk__in=[source.pk, target.pk])
            .order_by('pk')
        }
        if len(locked_users) != 2:
            raise ValueError('Both merge principals must still exist')

        # Booking mutations use user -> date -> booking -> account. Lock all
        # involved date namespaces in a stable order before taking row locks.
        merge_dates = sorted(set(
            CoworkingBooking.objects.filter(
                user_id__in=[source.pk, target.pk]
            ).values_list('date', flat=True)
        ))
        for booking_date in merge_dates:
            CoworkingService._lock_booking_date(booking_date)

        bookings = list(
            CoworkingBooking.objects.select_for_update()
            .filter(user_id=source.pk)
            .order_by('date', 'pk')
        )
        booking_ids = [booking.pk for booking in bookings]
        assignments = list(
            OfficeManagerAssignment.objects.select_for_update()
            .filter(Q(user_id=source.pk) | Q(booking_id__in=booking_ids))
            .order_by('day_id', 'pk')
        )
        booking_owner_by_id = {
            booking.pk: booking.user_id for booking in bookings
        }
        for assignment in assignments:
            if (
                assignment.user_id != source.pk
                or booking_owner_by_id.get(assignment.booking_id)
                != source.pk
            ):
                raise ValueError(
                    'Office Manager assignment and booking ownership disagree; '
                    'the account merge was refused'
                )

        active_dates = [
            booking.date for booking in bookings if booking.status == 'booked'
        ]
        conflicting_dates = list(
            CoworkingBooking.objects.select_for_update()
            .filter(
                user_id=target.pk,
                date__in=active_dates,
                status='booked',
            )
            .order_by('date')
            .values_list('date', flat=True)
        )
        if conflicting_dates:
            rendered = ', '.join(day.isoformat() for day in conflicting_dates)
            raise ValueError(
                'Cannot merge independently active coworking bookings on: '
                f'{rendered}'
            )

        assignment_count = OfficeManagerAssignment.objects.filter(
            pk__in=[assignment.pk for assignment in assignments]
        ).update(user_id=target.pk)
        booking_count = CoworkingBooking.objects.filter(
            pk__in=booking_ids
        ).update(user_id=target.pk)
        return booking_count, assignment_count
    
    @staticmethod
    def get_standard_coworking_cost() -> int:
        """
        Get the standard (undiscounted) cost for a coworking day.

        Prioritizes 'COWORKING_DAY' reward from catalog.
        Falls back to settings or default of 8 points.
        """
        try:
            reward = RewardsCatalog.objects.get(code='COWORKING_DAY', is_active=True)
            return reward.cost_points
        except RewardsCatalog.DoesNotExist:
            return getattr(settings, 'COWORKING_DAY_COST_POINTS', 8)

    # A 'ready' monthly update grants the coworking discount for this many days
    # from the moment it first became ready.
    MONTHLY_UPDATE_DISCOUNT_WINDOW_DAYS = 28
    BOOKING_DATE_LOCK_NAMESPACE = 1380929347

    @staticmethod
    def _has_ready_monthly_update(user: User, booking_date: date) -> bool:
        """
        Return whether any ABR-verified Australian company the user is bound to
        has a monthly update that is 'ready' and became ready within the last
        28 days (relative to ``booking_date``).

        The coworking discount rewards founders who run a verified registered
        Australian company and keep their monthly update current. Company
        eligibility mirrors ``vibe_raising.registration.company_is_verified``:
        ``registered`` with an ACN and an ABR-verified stamp.

        The window is time-based rather than calendar-month based: an update
        that reaches 'ready' grants the discount for the next 28 days
        regardless of month boundaries, so there is no start-of-month cliff.
        ``ready_at`` is stamped once, the first time a draft becomes ready, so
        re-approving an old draft cannot renew the window.
        """
        # Imported lazily to avoid a hard import dependency between the roo and
        # startup_updates / founder_tools apps at module load time.
        from founder_tools.models import VibeRaisingCompany
        from startup_updates.models import (
            MonthlyUpdateDraft,
            MonthlyUpdateDraftStatus,
            UserStartupBinding,
        )

        org_ids = list(
            UserStartupBinding.objects.filter(
                user=user,
                coworking_discount_eligible=True,
            ).values_list('organization_id', flat=True)
        )
        if not org_ids:
            return False

        # Only organisations backed by an ABR-verified company qualify. This is
        # the ORM form of vibe_raising.registration.company_is_verified().
        eligible_org_ids = set(
            VibeRaisingCompany.objects.filter(
                organization_id__in=org_ids,
                registered=True,
                abr_verified_at__isnull=False,
            )
            .exclude(acn__isnull=True)
            .exclude(acn='')
            .values_list('organization_id', flat=True)
        )
        if not eligible_org_ids:
            return False

        window_start = booking_date - timedelta(
            days=CoworkingService.MONTHLY_UPDATE_DISCOUNT_WINDOW_DAYS
        )
        return MonthlyUpdateDraft.objects.filter(
            organization_id__in=eligible_org_ids,
            status=MonthlyUpdateDraftStatus.READY,
            ready_at__isnull=False,
            ready_at__date__gte=window_start,
        ).exists()

    @staticmethod
    def get_coworking_cost(
        user: Optional[User] = None,
        booking_date: Optional[date] = None,
    ) -> int:
        """
        Get the cost for a coworking day.

        Defaults to the standard cost (from catalog/settings). When both
        ``user`` and ``booking_date`` are supplied and the user's ABR-verified
        startup has a monthly update that became 'ready' within the last 28
        days, the discounted cost applies instead — rewarding founders who keep
        their monthly update current.
        """
        standard = CoworkingService.get_standard_coworking_cost()
        if user is not None and booking_date is not None:
            if CoworkingService._has_ready_monthly_update(user, booking_date):
                return getattr(settings, 'COWORKING_DAY_DISCOUNT_COST_POINTS', 4)
        return standard

    @staticmethod
    def monthly_update_discount_applied(booking: CoworkingBooking) -> bool:
        """Return true only for a points-priced monthly-update discount."""
        if booking.booking_source != 'points':
            return False
        return booking.points_cost < CoworkingService.get_standard_coworking_cost()

    @staticmethod
    def _lock_booking_date(booking_date: date) -> None:
        """Serialize capacity checks for one date on PostgreSQL.

        Capacity normally comes from settings, so there may be no row available
        for ``select_for_update``. A transaction-scoped advisory lock protects
        both single and batch booking paths without creating fake overrides.
        """
        if connection.vendor != 'postgresql':
            return
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(%s, %s)",
                [CoworkingService.BOOKING_DATE_LOCK_NAMESPACE, booking_date.toordinal()],
            )
    
    @staticmethod
    def get_capacity(booking_date: date) -> int:
        """
        Get capacity for a specific date.
        
        Returns capacity from CoworkingDayCapacity if exists,
        otherwise returns DEFAULT_COWORKING_CAPACITY from settings.
        """
        try:
            day_capacity = CoworkingDayCapacity.objects.get(date=booking_date)
            return day_capacity.capacity
        except CoworkingDayCapacity.DoesNotExist:
            return getattr(settings, 'DEFAULT_COWORKING_CAPACITY', 24)
    
    @staticmethod
    def get_booked_count(booking_date: date) -> int:
        """Get active points-priced bookings that consume normal capacity."""
        return CoworkingBooking.objects.filter(
            date=booking_date,
            status='booked',
            booking_source='points',
        ).count()
    
    @staticmethod
    def check_availability(booking_date: date) -> Tuple[int, int]:
        """
        Check availability for a specific date.
        
        Returns:
            Tuple of (available_slots, total_capacity)
        """
        capacity = CoworkingService.get_capacity(booking_date)
        booked = CoworkingService.get_booked_count(booking_date)
        available = max(0, capacity - booked)
        return available, capacity

    @staticmethod
    def build_report(start_date: date, end_date: date) -> dict:
        """Build an inclusive active-booking report for a date range."""
        range_days = (end_date - start_date).days + 1
        if range_days <= 0:
            raise ValueError("end_date must be on or after start_date")
        if range_days > CoworkingService.MAX_REPORT_DAYS:
            raise ValueError(
                f"Coworking reports are limited to {CoworkingService.MAX_REPORT_DAYS} days"
            )

        daily_counts = OrderedDict(
            (start_date + timedelta(days=offset), 0)
            for offset in range(range_days)
        )

        bookings = CoworkingBooking.objects.filter(
            date__range=(start_date, end_date),
            status='booked',
        )
        for row in (
            bookings.values('date')
            .annotate(booked_users=Count('user_id', distinct=True))
            .order_by('date')
        ):
            daily_counts[row['date']] = row['booked_users']

        daily = [
            {
                'date': day.isoformat(),
                'booked_users': count,
            }
            for day, count in daily_counts.items()
        ]

        weekly_buckets = OrderedDict()
        monthly_buckets = OrderedDict()

        for day, count in daily_counts.items():
            week_start = day - timedelta(days=day.weekday())
            week_end = week_start + timedelta(days=6)
            if week_start not in weekly_buckets:
                weekly_buckets[week_start] = {
                    'week_start': week_start.isoformat(),
                    'week_end': week_end.isoformat(),
                    'booked_user_days': 0,
                    'active_days': 0,
                }
            weekly_buckets[week_start]['booked_user_days'] += count
            if count > 0:
                weekly_buckets[week_start]['active_days'] += 1

            month_key = (day.year, day.month)
            if month_key not in monthly_buckets:
                _, last_day = calendar.monthrange(day.year, day.month)
                monthly_buckets[month_key] = {
                    'month': f"{day.year:04d}-{day.month:02d}",
                    'month_start': date(day.year, day.month, 1).isoformat(),
                    'month_end': date(day.year, day.month, last_day).isoformat(),
                    'booked_user_days': 0,
                    'active_days': 0,
                }
            monthly_buckets[month_key]['booked_user_days'] += count
            if count > 0:
                monthly_buckets[month_key]['active_days'] += 1

        total_user_days = sum(daily_counts.values())
        max_daily = max(daily_counts.values()) if daily_counts else 0
        busiest_days = [
            {
                'date': day.isoformat(),
                'booked_users': count,
            }
            for day, count in daily_counts.items()
            if count == max_daily and count > 0
        ]

        return {
            'range': {
                'start_date': start_date.isoformat(),
                'end_date': end_date.isoformat(),
                'source': 'active_coworking_bookings',
            },
            'totals': {
                'booked_user_days': total_user_days,
                'unique_users': bookings.values('user_id').distinct().count(),
                'active_days': sum(1 for count in daily_counts.values() if count > 0),
                'range_days': range_days,
                'average_per_day': round(total_user_days / range_days, 2),
                'busiest_days': busiest_days,
            },
            'daily': daily,
            'weekly': list(weekly_buckets.values()),
            'monthly': list(monthly_buckets.values()),
        }
    
    @staticmethod
    def is_refundable(booking_date: date) -> bool:
        """
        Check if a booking for this date is refundable.
        
        A booking is refundable if cancelled before the cutoff time
        on the previous day.
        """
        cutoff_hours = getattr(settings, 'COWORKING_REFUND_CUTOFF_HOURS', 18)
        now = timezone.now()
        
        # Cutoff is at CUTOFF_HOURS on the day before the booking
        cutoff_datetime = timezone.make_aware(
            datetime.combine(
                booking_date - timedelta(days=1),
                datetime.min.time().replace(hour=cutoff_hours)
            )
        )
        
        return now < cutoff_datetime
    
    @staticmethod
    @transaction.atomic
    def book(
        user: User,
        booking_date: date,
        created_by_slack_id: str,
        slack_channel_id: Optional[str] = None,
    ) -> Tuple[CoworkingBooking, bool]:
        """
        Book a coworking spot for a specific date.
        
        Args:
            user: The User making the booking
            booking_date: The date to book
            created_by_slack_id: Slack ID of requester
            slack_channel_id: Optional Slack channel for notifications
            
        Returns:
            CoworkingBooking instance
            
        Raises:
            ValueError: If date is invalid or no availability
            InsufficientBalanceError: If user doesn't have enough points
        """
        # Validate date range
        today = timezone.now().date()
        max_advance_days = getattr(settings, 'COWORKING_BOOKING_ADVANCE_DAYS', 30)
        
        if booking_date < today:
            raise ValueError("Cannot book dates in the past")
        
        if booking_date > today + timedelta(days=max_advance_days):
            raise ValueError(f"Cannot book more than {max_advance_days} days in advance")

        User.objects.select_for_update().get(pk=user.pk)
        CoworkingService._lock_booking_date(booking_date)
        
        # Check if user already has a booking for this date
        existing = CoworkingBooking.objects.filter(
            user=user,
            date=booking_date,
            status='booked'
        ).first()
        if existing:
            return existing, False
        
        # Check availability
        available, capacity = CoworkingService.check_availability(booking_date)
        if available <= 0:
            raise ValueError(f"No availability for {booking_date} (capacity: {capacity})")
        
        # Get cost (discounted when the user's startup has a 'ready' monthly
        # update for the booking's month)
        cost = CoworkingService.get_coworking_cost(user=user, booking_date=booking_date)

        account = PointsAccount.objects.select_for_update().filter(
            user=user
        ).first()
        if account is not None:
            PointsService._ensure_microroo_account(account)
        purchased_points_cost_microroo = min(
            account.purchased_topup_balance_microroo if account else 0,
            PointsService.roo_to_microroo(cost),
        )
        
        # A new booking after cancellation is a new charge. The active-booking
        # check above still makes retries idempotent while the booking is live.
        booking_id = uuid.uuid4()
        idempotency_key = f"coworking_book:{booking_id}"
        
        # Spend points (this also validates balance)
        ledger, _ = PointsService.spend(
            user=user,
            delta=cost,
            source='COWORKING',
            description=f"Coworking booking for {booking_date}",
            created_by_slack_id=created_by_slack_id,
            idempotency_key=idempotency_key,
            reference_type='COWORKING_BOOKING',
            reference_id=str(booking_date),
        )
        
        # Create booking
        booking = CoworkingBooking.objects.create(
            id=booking_id,
            user=user,
            date=booking_date,
            status='booked',
            points_cost=cost,
            purchased_points_cost_microroo=(
                purchased_points_cost_microroo
            ),
            ledger_entry=ledger,
            slack_channel_id=slack_channel_id,
        )
        
        return booking, True

    @staticmethod
    @transaction.atomic
    def book_many(
        *,
        target_users: list[User],
        booking_date: date,
        created_by_slack_id: str,
        slack_channel_id: Optional[str] = None,
    ) -> list[Tuple[CoworkingBooking, bool]]:
        """Book multiple target users atomically for an admin-initiated check-in."""
        unique_users: list[User] = []
        seen_user_ids = set()
        for user in target_users:
            if user.id in seen_user_ids:
                continue
            unique_users.append(user)
            seen_user_ids.add(user.id)

        if not unique_users:
            raise CoworkingBatchBookingError("At least one target user is required")
        unique_users.sort(key=lambda user: str(user.pk))

        today = timezone.now().date()
        max_advance_days = getattr(settings, 'COWORKING_BOOKING_ADVANCE_DAYS', 30)

        if booking_date < today:
            raise CoworkingBatchBookingError("Cannot book dates in the past")

        if booking_date > today + timedelta(days=max_advance_days):
            raise CoworkingBatchBookingError(
                f"Cannot book more than {max_advance_days} days in advance"
            )

        list(
            User.objects.select_for_update()
            .filter(pk__in=[user.pk for user in unique_users])
            .order_by('pk')
        )
        CoworkingService._lock_booking_date(booking_date)

        existing_bookings = {
            booking.user_id: booking
            for booking in CoworkingBooking.objects.select_for_update().filter(
                user__in=unique_users,
                date=booking_date,
                status='booked',
            )
        }
        users_to_book = [
            user for user in unique_users if user.id not in existing_bookings
        ]

        available, capacity = CoworkingService.check_availability(booking_date)
        if available < len(users_to_book):
            raise CoworkingBatchBookingError(
                f"Not enough availability for {booking_date}",
                errors=[
                    {
                        'slack_user_id': user.slack_id,
                        'error': (
                            f"No availability for {booking_date}: "
                            f"{available} slots available, {len(users_to_book)} requested"
                        ),
                        'available_slots': available,
                        'requested_new_bookings': len(users_to_book),
                        'capacity': capacity,
                    }
                    for user in users_to_book
                ],
            )

        costs_by_user_id = {
            user.id: CoworkingService.get_coworking_cost(
                user=user,
                booking_date=booking_date,
            )
            for user in users_to_book
        }
        accounts_by_user_id = {
            account.user_id: account
            for account in PointsAccount.objects.select_for_update().filter(
                user__in=users_to_book
            ).order_by('user_id')
        }
        balance_errors = []
        for user in users_to_book:
            account = accounts_by_user_id.get(user.id)
            balance = account.balance if account else 0
            cost = costs_by_user_id[user.id]
            if balance < cost:
                balance_errors.append({
                    'slack_user_id': user.slack_id,
                    'error': f"Insufficient balance: {balance} < {cost} required",
                    'balance': balance,
                    'required_points': cost,
                })
        if balance_errors:
            raise CoworkingBatchBookingError(
                "One or more users have insufficient Roo Points",
                errors=balance_errors,
            )

        results: list[Tuple[CoworkingBooking, bool]] = []
        for user in unique_users:
            existing = existing_bookings.get(user.id)
            if existing:
                results.append((existing, False))
                continue
            results.append(
                CoworkingService.book(
                    user=user,
                    booking_date=booking_date,
                    created_by_slack_id=created_by_slack_id,
                    slack_channel_id=slack_channel_id,
                )
            )

        return results
    
    @staticmethod
    def cancel(
        booking_id: str,
        requester_slack_id: str,
        *,
        office_manager_authorized: bool = False,
    ) -> Tuple[CoworkingBooking, bool]:
        """Cancel with a bounded retry for transaction-level aborts."""
        for attempt in range(DATABASE_TRANSACTION_RETRY_ATTEMPTS):
            try:
                return CoworkingService._cancel_once(
                    booking_id,
                    requester_slack_id,
                    office_manager_authorized=office_manager_authorized,
                )
            except _CancellationOwnerChanged:
                if attempt + 1 >= DATABASE_TRANSACTION_RETRY_ATTEMPTS:
                    raise OperationalError(
                        "Booking owner kept changing during cancellation"
                    )
                continue
            except OperationalError as exc:
                if (
                    not _retryable_transaction_error(exc)
                    or attempt + 1 >= DATABASE_TRANSACTION_RETRY_ATTEMPTS
                ):
                    raise
                time.sleep(0.05 * (2 ** attempt))
        raise AssertionError("unreachable")

    @staticmethod
    @transaction.atomic
    def _cancel_once(
        booking_id: str,
        requester_slack_id: str,
        *,
        office_manager_authorized: bool = False,
    ) -> Tuple[CoworkingBooking, bool]:
        """
        Cancel a booking with conditional refund.
        
        Args:
            booking_id: UUID of the booking
            requester_slack_id: Slack ID of who is cancelling
            
        Returns:
            Tuple of (booking, refunded: bool)
            
        Raises:
            ValueError: If booking not found
        """
        try:
            booking_snapshot = CoworkingBooking.objects.values(
                'date',
                'user_id',
            ).get(id=booking_id)
        except CoworkingBooking.DoesNotExist:
            raise ValueError(f"Booking {booking_id} not found")

        booking_date = booking_snapshot['date']
        requester_id = (
            User.objects
            .filter(slack_id=str(requester_slack_id or '').strip())
            .values_list('pk', flat=True)
            .first()
        )
        locked_users = {
            user.pk: user
            for user in User.objects.select_for_update()
            .filter(
                pk__in=sorted(
                    {
                        booking_snapshot['user_id'],
                        *([requester_id] if requester_id is not None else []),
                    }
                )
            )
            .order_by('pk')
        }
        requester = locked_users.get(requester_id)
        requester_is_admin = is_points_admin(requester_slack_id)
        CoworkingService._lock_booking_date(booking_date)

        from .office_manager import OfficeManagerService

        office_manager_assignment_ref = (
            OfficeManagerAssignment.objects.filter(
                booking_id=booking_id,
            )
            .order_by('-claimed_at', '-pk')
            .values('id', 'day_id')
            .first()
        )
        locked_office_manager_day = None
        if office_manager_assignment_ref is not None:
            locked_office_manager_day = (
                OfficeManagerDay.objects.select_for_update().get(
                    pk=office_manager_assignment_ref['day_id']
                )
            )

        booking = CoworkingBooking.objects.select_for_update().get(id=booking_id)
        if booking.user_id != booking_snapshot['user_id']:
            # The owner changed between discovery and the ordered user locks.
            # Roll back and retry so the new owner is locked before the date.
            raise _CancellationOwnerChanged

        # Revalidate authority only after locking/reloading the mutable booking.
        # This closes both points->Office-Manager conversion and identity-merge
        # races between the view's lookup and the transactional mutation.
        if (
            booking.user_id != getattr(requester, 'pk', None)
            and not requester_is_admin
        ):
            raise PermissionDeniedError(
                'Not authorized to cancel this booking'
            )
        if booking.booking_source == 'office_manager' and not office_manager_authorized:
            raise PermissionDeniedError(
                'Office Manager cancellation requires Roo authorization'
            )

        booking._already_cancelled = booking.status == 'cancelled'
        booking._office_manager_day_reopened = False
        booking._office_manager_day_id = None
        booking._office_manager_assignment_id = None
        if booking.status == 'cancelled':
            if office_manager_assignment_ref is not None:
                assignment = (
                    OfficeManagerAssignment.objects.select_for_update()
                    .filter(pk=office_manager_assignment_ref['id'])
                    .first()
                )
                if assignment is not None:
                    booking._office_manager_day_id = assignment.day_id
                    booking._office_manager_assignment_id = assignment.id
                    booking._office_manager_day_reopened = bool(
                        locked_office_manager_day
                        and locked_office_manager_day.status == 'open'
                    )
            cancellation_refunded = bool(
                booking.refund_ledger_entry_id
                and booking.refund_ledger_entry.reference_type
                == 'COWORKING_REFUND'
            )
            return booking, cancellation_refunded

        refunded = False

        if booking.booking_source == 'office_manager':
            reopened, day_id, assignment_id = (
                OfficeManagerService.relinquish_for_booking(
                    booking,
                    requester_slack_id=requester_slack_id,
                    locked_day=locked_office_manager_day,
                )
            )
            booking._office_manager_day_reopened = reopened
            booking._office_manager_day_id = day_id
            booking._office_manager_assignment_id = assignment_id

        booking.status = 'cancelled'
        booking.cancelled_at = timezone.now()
        
        # Check if refund is applicable
        if booking.points_cost > 0 and CoworkingService.is_refundable(booking.date):
            purchased_points_cost_microroo = (
                CoworkingService.validated_booking_debit_provenance(booking)
            )
            idempotency_key = f"coworking_refund:{booking.id}"
            
            ledger, created = PointsService.refund(
                user=booking.user,
                delta=booking.points_cost,
                source='COWORKING',
                description=f"Refund for cancelled coworking booking on {booking.date}",
                created_by_slack_id=requester_slack_id,
                idempotency_key=idempotency_key,
                reference_type='COWORKING_REFUND',
                reference_id=str(booking.id),
                purchased_delta_microroo=purchased_points_cost_microroo,
                reverse_lifetime_spent=True,
            )
            
            booking.refund_ledger_entry = ledger
            refunded = True
        
        booking.save()
        
        return booking, refunded
    
    @staticmethod
    @require_admin
    def set_capacity(
        capacity_date: date,
        capacity: int,
        requester_slack_id: str,
        notes: Optional[str] = None,
    ) -> CoworkingDayCapacity:
        """
        Set or update capacity for a specific date (admin only).
        
        Args:
            capacity_date: The date to set capacity for
            capacity: Number of slots available
            requester_slack_id: Slack ID of admin making the change
            notes: Optional notes for the capacity change
            
        Returns:
            CoworkingDayCapacity instance
        """
        day_capacity, _ = CoworkingDayCapacity.objects.update_or_create(
            date=capacity_date,
            defaults={
                'capacity': capacity,
                'notes': notes,
            }
        )
        return day_capacity


class InvalidBoostPostError(ValueError):
    """The submitted Slack root is not a valid boostable social post."""


class BoostPostPayloadConflictError(ValueError):
    """An idempotency key was reused for a different Slack root payload."""


class BoostPostAdmissionService:
    """Atomically price and debit direct Slack boost-channel root posts."""

    BASE_COST_POINTS = 8
    DISCOUNT_COST_POINTS = 4

    @classmethod
    def _validate_payload(
        cls,
        *,
        submission_key: str,
        workspace_id: str,
        channel_id: str,
        root_message_ts: str,
        poster_slack_id: str,
    ) -> None:
        if not re.fullmatch(r'T[A-Z0-9]+', workspace_id):
            raise InvalidBoostPostError('workspace_id is invalid')
        if not re.fullmatch(r'C[A-Z0-9]+', channel_id):
            raise InvalidBoostPostError('channel_id is invalid')
        if not re.fullmatch(r'\d{8,}\.\d+', root_message_ts):
            raise InvalidBoostPostError('root_message_ts is invalid')
        if not re.fullmatch(r'U[A-Z0-9]+', poster_slack_id):
            raise InvalidBoostPostError('poster_slack_id is invalid')
        expected_key = f'boost-post:{workspace_id}:{channel_id}:{root_message_ts}'
        if submission_key != expected_key:
            raise InvalidBoostPostError('submission_key does not match the Slack root')

    @staticmethod
    def _payload_matches(
        admission: BoostPostAdmission,
        *,
        workspace_id: str,
        channel_id: str,
        root_message_ts: str,
        poster_slack_id: str,
    ) -> bool:
        return (
            admission.workspace_id == workspace_id
            and admission.channel_id == channel_id
            and admission.root_message_ts == root_message_ts
            and admission.poster_slack_id == poster_slack_id
        )

    @classmethod
    @transaction.atomic
    def admit(
        cls,
        *,
        submission_key: str,
        workspace_id: str,
        channel_id: str,
        root_message_ts: str,
        poster_slack_id: str,
        root_text: str,
        social_post_url: str,
        recheck_insufficient_points: bool = False,
    ) -> tuple[BoostPostAdmission, bool]:
        # Campaign content is not an admission rule. Keep the first URL only as
        # optional metadata while points balance remains the sole business gate.
        social_post_url = str(social_post_url or '').strip()[:2048]
        cls._validate_payload(
            submission_key=submission_key,
            workspace_id=workspace_id,
            channel_id=channel_id,
            root_message_ts=root_message_ts,
            poster_slack_id=poster_slack_id,
        )

        # Resolve without a lock first so an unlinked member still receives
        # the durable terminal admission result. When linked, lock the
        # principal before the admission/account rows to match identity merge.
        user = PointsService.get_user_by_slack_id(poster_slack_id)
        if user is not None:
            user = User.objects.select_for_update().get(pk=user.pk)

        admission, created = BoostPostAdmission.objects.select_for_update().get_or_create(
            submission_key=submission_key,
            defaults={
                'workspace_id': workspace_id,
                'channel_id': channel_id,
                'root_message_ts': root_message_ts,
                'poster_slack_id': poster_slack_id,
                'root_text': root_text,
                'social_post_url': social_post_url,
                'status': 'processing',
                'base_cost_points': cls.BASE_COST_POINTS,
            },
        )
        if not cls._payload_matches(
            admission,
            workspace_id=workspace_id,
            channel_id=channel_id,
            root_message_ts=root_message_ts,
            poster_slack_id=poster_slack_id,
        ):
            raise BoostPostPayloadConflictError(
                'submission_key is already bound to a different Slack root'
            )
        if admission.status == 'insufficient_points' and recheck_insufficient_points:
            admission.status = 'processing'
            admission.rejection_message = ''
            admission.save(update_fields=['status', 'rejection_message', 'updated_at'])
        if admission.status != 'processing':
            return admission, False

        if user is None:
            admission.status = 'member_unlinked'
            admission.rejection_message = 'Slack member is not linked to a Roo Points account'
            admission.save(update_fields=['status', 'rejection_message', 'updated_at'])
            return admission, created

        discount_applied = CoworkingService._has_ready_monthly_update(
            user,
            timezone.localdate(),
        )
        charged_points = (
            cls.DISCOUNT_COST_POINTS if discount_applied else cls.BASE_COST_POINTS
        )
        account = PointsAccount.objects.select_for_update().filter(user=user).first()
        if account is None:
            account = PointsAccount.objects.create(user=user)
            account = PointsAccount.objects.select_for_update().get(user=user)
        balance_before = account.balance
        if balance_before < charged_points:
            admission.user = user
            admission.status = 'insufficient_points'
            admission.charged_points = charged_points
            admission.discount_applied = discount_applied
            admission.balance_before = balance_before
            admission.new_balance = balance_before
            admission.rejection_message = (
                f'Insufficient Roo Points: {balance_before} available, '
                f'{charged_points} required'
            )
            admission.save(
                update_fields=[
                    'user',
                    'status',
                    'charged_points',
                    'discount_applied',
                    'balance_before',
                    'new_balance',
                    'rejection_message',
                    'updated_at',
                ]
            )
            return admission, created

        ledger, _ = PointsService.spend(
            user=user,
            delta=charged_points,
            source='TOOLS',
            description='Boost My Startup post approval',
            created_by_slack_id='ROO',
            idempotency_key=f'boost_post:{submission_key}',
            reference_type='BOOST_POST',
            reference_id=submission_key,
        )
        account.refresh_from_db(fields=['balance'])
        admission.user = user
        admission.status = 'approved'
        admission.charged_points = charged_points
        admission.discount_applied = discount_applied
        admission.balance_before = balance_before
        admission.new_balance = account.balance
        admission.ledger_entry = ledger
        admission.rejection_message = ''
        admission.save(
            update_fields=[
                'user',
                'status',
                'charged_points',
                'discount_applied',
                'balance_before',
                'new_balance',
                'ledger_entry',
                'rejection_message',
                'updated_at',
            ]
        )
        return admission, created


class StartupUpdateRewardService:
    """Rewards founders of verified registered companies for keeping their monthly
    update current."""

    REWARD_SOURCE = 'STARTUP_UPDATE'

    @staticmethod
    def reward_amount() -> int:
        return int(getattr(settings, 'ROO_POINTS_MONTHLY_UPDATE_REWARD', 20))

    @staticmethod
    def award_monthly_update_completion(user, company, month_bucket, draft=None) -> bool:
        """Award points the first time a *verified registered company* (valid ACN)
        completes a monthly update for ``month_bucket`` (a date on the first of the
        month).

        Idempotent per company + month — re-saving the same month's update never awards
        twice. Best-effort: returns False (and never raises) if the company isn't
        eligible or the award can't be made, so it can't break the update flow.
        """
        # Imported lazily to avoid a load-order dependency between roo and vibe_raising.
        from vibe_raising.registration import company_is_verified

        if user is None or company is None or month_bucket is None:
            return False
        if not company_is_verified(company):
            return False

        amount = StartupUpdateRewardService.reward_amount()
        if amount <= 0:
            return False

        month_key = month_bucket.strftime('%Y-%m')
        try:
            _ledger, created = PointsService.award(
                user=user,
                delta=amount,
                source=StartupUpdateRewardService.REWARD_SOURCE,
                description=f"Monthly update completed — {month_bucket.strftime('%B %Y')}",
                created_by_slack_id=getattr(user, 'slack_id', '') or 'system',
                idempotency_key=f"monthly_update_reward:{company.id}:{month_key}",
                reference_type='MONTHLY_UPDATE_DRAFT',
                reference_id=str(draft.id) if draft is not None else None,
            )
            return created
        except Exception:
            logger.exception("Failed to award monthly-update points to user %s", getattr(user, 'id', None))
            return False


class TaskService:
    """
    Service class for task operations.
    """
    
    @staticmethod
    def ensure_task_code(task: Task) -> str:
        """Ensure a volunteer-facing ROO task code exists."""
        if task.task_code:
            return task.task_code

        if not task.id:
            task.save()

        task.task_code = f"ROO-{task.id:04d}"
        task.save(update_fields=['task_code'])
        return task.task_code

    @staticmethod
    def can_review(task: Task, slack_user_id: str) -> bool:
        """Return whether the given Slack user can review the task."""
        if not slack_user_id:
            return False
        if is_points_admin(slack_user_id):
            return True

        allowed_reviewers = {
            task.reviewer_slack_id,
            task.fallback_reviewer_slack_id,
        }
        return slack_user_id in {reviewer for reviewer in allowed_reviewers if reviewer}

    @staticmethod
    def create_activity(
        *,
        task: Task,
        event_type: str,
        actor_slack_id: Optional[str] = None,
        assignment: Optional[TaskAssignment] = None,
        submission: Optional[TaskSubmission] = None,
        summary: str = "",
        metadata: Optional[dict] = None,
    ) -> TaskActivity:
        """Persist a raw audit trail event."""
        return TaskActivity.objects.create(
            task=task,
            assignment=assignment,
            submission=submission,
            event_type=event_type,
            actor_slack_id=actor_slack_id,
            summary=summary,
            metadata=metadata or {},
        )

    @staticmethod
    def sync_task_projection(task: Task) -> Task:
        """Mirror assignment state back to the legacy Task row."""
        task.refresh_from_db(fields=None)
        current_assignment = task.get_current_assignment()
        projected = task.sync_status_projection()

        update_fields = []
        if task.status != projected:
            task.status = projected
            update_fields.append('status')

        assigned_user = current_assignment.assigned_user if current_assignment else None
        assigned_slack_id = current_assignment.assigned_to_slack_id if current_assignment else None
        approved_by = current_assignment.approved_by_slack_id if current_assignment else None
        approved_at = current_assignment.approved_at if current_assignment else None

        if task.assigned_user_id != (assigned_user.id if assigned_user else None):
            task.assigned_user = assigned_user
            update_fields.append('assigned_user')
        if task.assigned_to_user_id != assigned_slack_id:
            task.assigned_to_user_id = assigned_slack_id
            update_fields.append('assigned_to_user_id')

        if projected == 'approved':
            if task.closed_by_user_id != approved_by:
                task.closed_by_user_id = approved_by
                update_fields.append('closed_by_user_id')
            if task.closed_at != approved_at:
                task.closed_at = approved_at
                update_fields.append('closed_at')
        elif task.status != 'cancelled':
            if task.closed_by_user_id is not None:
                task.closed_by_user_id = None
                update_fields.append('closed_by_user_id')
            if task.closed_at is not None:
                task.closed_at = None
                update_fields.append('closed_at')

        if update_fields:
            task.save(update_fields=update_fields)
        return task

    @staticmethod
    def _ensure_assignment_for_legacy_task(task: Task, user: User, slack_user_id: str) -> TaskAssignment:
        """Backfill an active assignment for legacy tasks that predate TaskAssignment."""
        assignment = task.get_active_assignment()
        if assignment:
            return assignment

        assignment_status = 'submitted' if task.status == 'submitted' else 'claimed'
        assignment = TaskAssignment.objects.create(
            task=task,
            assigned_user=user,
            assigned_to_slack_id=slack_user_id,
            claimed_points_snapshot=task.points_estimate or task.points,
            status=assignment_status,
            claimed_at=task.updated_at or timezone.now(),
            submitted_at=task.updated_at if assignment_status == 'submitted' else None,
        )
        return assignment

    @staticmethod
    @transaction.atomic
    def claim_task(task: Task, user: User, slack_user_id: str) -> TaskAssignment:
        """Claim a task by creating the active assignment."""
        task = Task.objects.select_for_update().get(pk=task.pk)
        TaskService.ensure_task_code(task)

        if task.status == 'cancelled':
            raise ValueError('Task is cancelled')
        if task.status == 'approved':
            raise ValueError('Task is already approved')
        if task.get_active_assignment():
            raise ValueError('Task already has an active assignment')

        assignment = TaskAssignment.objects.create(
            task=task,
            assigned_user=user,
            assigned_to_slack_id=slack_user_id,
            claimed_points_snapshot=task.points_estimate or task.points,
            status='claimed',
            claimed_at=timezone.now(),
        )
        TaskService.create_activity(
            task=task,
            assignment=assignment,
            event_type='claimed',
            actor_slack_id=slack_user_id,
            summary=f'Claimed by {slack_user_id}',
        )
        TaskService.sync_task_projection(task)
        return assignment

    @staticmethod
    def _resolve_assignment_for_submission(task: Task, user: User, slack_user_id: str) -> TaskAssignment:
        """Resolve or backfill the assignment that owns this submission."""
        assignment = task.get_active_assignment()
        if assignment:
            if assignment.status == 'submitted':
                raise ValueError('Task already has submitted work pending review')
            if assignment.assigned_to_slack_id and assignment.assigned_to_slack_id != slack_user_id:
                raise PermissionDeniedError('Only the assigned user can submit')
            if assignment.assigned_user and assignment.assigned_user != user:
                raise PermissionDeniedError('Only the assigned user can submit')
            updates = []
            if assignment.assigned_user_id != user.id:
                assignment.assigned_user = user
                updates.append('assigned_user')
            if assignment.assigned_to_slack_id != slack_user_id:
                assignment.assigned_to_slack_id = slack_user_id
                updates.append('assigned_to_slack_id')
            if updates:
                assignment.save(update_fields=updates)
            return assignment

        if task.status == 'cancelled':
            raise ValueError('Task is cancelled')
        if task.status == 'approved':
            raise ValueError('Task is already approved')
        if task.assigned_to_user_id and task.assigned_to_user_id != slack_user_id:
            raise PermissionDeniedError('Only the assigned user can submit')
        if task.assigned_user and task.assigned_user != user:
            raise PermissionDeniedError('Only the assigned user can submit')

        return TaskAssignment.objects.create(
            task=task,
            assigned_user=user,
            assigned_to_slack_id=slack_user_id,
            claimed_points_snapshot=task.points_estimate or task.points,
            status='claimed',
            claimed_at=timezone.now(),
        )

    @staticmethod
    @transaction.atomic
    def submit_task(
        task: Task,
        user: User,
        slack_user_id: str,
        submission_text: str,
        submission_url: Optional[str] = None,
        evidence_kind: str = 'text',
        evidence_payload: Optional[dict] = None,
    ) -> Tuple[TaskAssignment, TaskSubmission]:
        """Create a submission for the task's active assignment."""
        task = Task.objects.select_for_update().get(pk=task.pk)
        TaskService.ensure_task_code(task)
        assignment = TaskService._resolve_assignment_for_submission(task, user, slack_user_id)

        submission = TaskSubmission.objects.create(
            task=task,
            assignment=assignment,
            user=user,
            submission_text=submission_text,
            submission_url=submission_url,
            status='submitted',
            evidence_kind=evidence_kind or 'text',
            evidence_payload=evidence_payload or {},
        )
        assignment.status = 'submitted'
        assignment.submitted_at = timezone.now()
        assignment.save(update_fields=['status', 'submitted_at'])
        TaskService.create_activity(
            task=task,
            assignment=assignment,
            submission=submission,
            event_type='submitted',
            actor_slack_id=slack_user_id,
            summary='Work submitted for review',
        )
        TaskService.sync_task_projection(task)
        return assignment, submission

    @staticmethod
    def get_latest_submitted_submission(task: Task) -> Optional[TaskSubmission]:
        """Return the latest pending submission for a task."""
        return task.submissions.filter(status='submitted').order_by('-created_at').first()

    @staticmethod
    @transaction.atomic
    def reject_submission(
        task: Task,
        rejector_slack_id: str,
        *,
        reason: str = "",
        submission_id: Optional[str] = None,
    ) -> Tuple[TaskSubmission, Optional[TaskAssignment]]:
        """Reject the targeted or latest submitted attempt and return task to claimed."""
        task = Task.objects.select_for_update().get(pk=task.pk)
        if not TaskService.can_review(task, rejector_slack_id):
            raise PermissionDeniedError(f"{rejector_slack_id} is not authorized to reject submissions")

        if submission_id:
            try:
                submission = task.submissions.get(id=submission_id)
            except TaskSubmission.DoesNotExist as exc:
                raise ValueError('Submission not found') from exc
        else:
            submission = TaskService.get_latest_submitted_submission(task)
            if not submission:
                raise ValueError('No submitted work found to reject')

        if submission.status != 'submitted':
            raise ValueError(f"Submission is not in submitted status (current: {submission.status})")

        assignment = submission.assignment
        if not assignment:
            assignment = TaskService._ensure_assignment_for_legacy_task(
                task=task,
                user=submission.user,
                slack_user_id=submission.user.slack_id or task.assigned_to_user_id or '',
            )
            submission.assignment = assignment

        now = timezone.now()
        submission.status = 'rejected'
        submission.rejection_reason = reason
        submission.review_notes = reason
        submission.reviewed_by_slack_id = rejector_slack_id
        submission.reviewed_at = now
        submission.save(
            update_fields=[
                'assignment',
                'status',
                'rejection_reason',
                'review_notes',
                'reviewed_by_slack_id',
                'reviewed_at',
            ]
        )

        if assignment.status == 'submitted':
            assignment.status = 'claimed'
            assignment.save(update_fields=['status'])

        TaskService.create_activity(
            task=task,
            assignment=assignment,
            submission=submission,
            event_type='changes_requested',
            actor_slack_id=rejector_slack_id,
            summary='Changes requested',
            metadata={'reason': reason} if reason else {},
        )
        TaskService.sync_task_projection(task)
        return submission, assignment

    @staticmethod
    @transaction.atomic
    def approve_submission(
        submission: TaskSubmission,
        approver_slack_id: str,
        *,
        awarded_points: Optional[int] = None,
        review_notes: str = "",
    ) -> Tuple[TaskSubmission, Ledger, TaskAssignment]:
        """
        Approve a task submission and award points.
        
        Args:
            submission: The TaskSubmission to approve
            approver_slack_id: Slack ID of the approver
            
        Returns:
            Tuple of (submission, ledger entry)
            
        Raises:
            PermissionDeniedError: If approver is not an admin
            ValueError: If submission is not in submitted status
        """
        task = submission.task
        if not TaskService.can_review(task, approver_slack_id):
            raise PermissionDeniedError(f"{approver_slack_id} is not authorized to approve submissions")
        
        if submission.status != 'submitted':
            raise ValueError(f"Submission is not in submitted status (current: {submission.status})")

        assignment = submission.assignment
        if not assignment:
            assignment = TaskService._ensure_assignment_for_legacy_task(
                task=task,
                user=submission.user,
                slack_user_id=submission.user.slack_id or task.assigned_to_user_id or '',
            )

        user = assignment.assigned_user or submission.user
        if not user:
            raise ValueError('No user is associated with this submission')

        if awarded_points is None:
            awarded_points = assignment.claimed_points_snapshot or task.points_estimate or task.points
        elif awarded_points < task.points_min or awarded_points > task.points_max:
            raise ValueError(
                f"Approved points must be between {task.points_min} and {task.points_max}"
            )

        idempotency_key = f"task_award:{task.id}"
        ledger, created = PointsService.award(
            user=user,
            delta=awarded_points,
            source='TASK',
            description=f"Completed task: {task.title}",
            created_by_slack_id=approver_slack_id,
            idempotency_key=idempotency_key,
            reference_type='TASK_ASSIGNMENT',
            reference_id=str(assignment.id),
        )
        
        now = timezone.now()
        submission.status = 'approved'
        submission.assignment = assignment
        submission.review_notes = review_notes
        submission.reviewed_by_slack_id = approver_slack_id
        submission.reviewed_at = now
        submission.approved_by_slack_id = approver_slack_id
        submission.approved_at = now
        submission.ledger_entry = ledger
        submission.save(
            update_fields=[
                'assignment',
                'status',
                'review_notes',
                'reviewed_by_slack_id',
                'reviewed_at',
                'approved_by_slack_id',
                'approved_at',
                'ledger_entry',
            ]
        )

        assignment.assigned_user = user
        assignment.assigned_to_slack_id = assignment.assigned_to_slack_id or user.slack_id
        assignment.status = 'approved'
        assignment.approved_at = now
        assignment.approved_by_slack_id = approver_slack_id
        assignment.claimed_points_snapshot = assignment.claimed_points_snapshot or awarded_points
        assignment.awarded_points = awarded_points
        assignment.save(
            update_fields=[
                'assigned_user',
                'assigned_to_slack_id',
                'status',
                'approved_at',
                'approved_by_slack_id',
                'claimed_points_snapshot',
                'awarded_points',
            ]
        )

        TaskService.create_activity(
            task=task,
            assignment=assignment,
            submission=submission,
            event_type='approved',
            actor_slack_id=approver_slack_id,
            summary='Submission approved',
            metadata={'points_awarded': awarded_points, 'created': created},
        )
        TaskService.sync_task_projection(task)
        
        return submission, ledger, assignment

    @staticmethod
    @transaction.atomic
    def approve_assignment_without_submission(
        task: Task,
        user: User,
        approver_slack_id: str,
        *,
        slack_user_id: Optional[str] = None,
        awarded_points: Optional[int] = None,
        review_notes: str = "",
    ) -> Tuple[TaskAssignment, Ledger]:
        """Legacy/direct-award path where approval does not require a normal submission."""
        user = User.objects.select_for_update().get(pk=user.pk)
        task = Task.objects.select_for_update().get(pk=task.pk)
        if not TaskService.can_review(task, approver_slack_id):
            raise PermissionDeniedError(f"{approver_slack_id} is not authorized to approve tasks")

        slack_user_id = slack_user_id or user.slack_id or task.assigned_to_user_id or ''
        assignment = task.get_active_assignment()
        if not assignment:
            assignment = TaskService._ensure_assignment_for_legacy_task(task, user, slack_user_id)

        if awarded_points is None:
            awarded_points = assignment.claimed_points_snapshot or task.points_estimate or task.points
        elif awarded_points < task.points_min or awarded_points > task.points_max:
            raise ValueError(
                f"Approved points must be between {task.points_min} and {task.points_max}"
            )

        ledger, created = PointsService.award(
            user=user,
            delta=awarded_points,
            source='TASK',
            description=f"Completed task: {task.title}",
            created_by_slack_id=approver_slack_id,
            idempotency_key=f"task_award:{task.id}",
            reference_type='TASK_ASSIGNMENT',
            reference_id=str(assignment.id),
        )

        now = timezone.now()
        assignment.assigned_user = user
        assignment.assigned_to_slack_id = slack_user_id
        assignment.status = 'approved'
        assignment.approved_at = now
        assignment.approved_by_slack_id = approver_slack_id
        assignment.claimed_points_snapshot = assignment.claimed_points_snapshot or awarded_points
        assignment.awarded_points = awarded_points
        assignment.closed_reason = review_notes
        assignment.save(
            update_fields=[
                'assigned_user',
                'assigned_to_slack_id',
                'status',
                'approved_at',
                'approved_by_slack_id',
                'claimed_points_snapshot',
                'awarded_points',
                'closed_reason',
            ]
        )

        TaskService.create_activity(
            task=task,
            assignment=assignment,
            event_type='approved',
            actor_slack_id=approver_slack_id,
            summary='Task approved without submission',
            metadata={'points_awarded': awarded_points, 'created': created},
        )
        TaskService.sync_task_projection(task)
        return assignment, ledger

    @staticmethod
    @transaction.atomic
    def unclaim_task(task: Task, slack_user_id: str) -> TaskAssignment:
        """Release a claimed task only if no submission attempts exist."""
        task = Task.objects.select_for_update().get(pk=task.pk)
        assignment = task.get_active_assignment()
        if not assignment:
            raise ValueError('Task does not have an active assignment')
        if assignment.status != 'claimed':
            raise ValueError('Task can only be unclaimed before submission')
        if assignment.assigned_to_slack_id and assignment.assigned_to_slack_id != slack_user_id:
            raise PermissionDeniedError('Only the current assignee can unclaim this task')
        if assignment.submissions.exists():
            raise ValueError('Task cannot be unclaimed after a submission exists')

        assignment.status = 'released'
        assignment.released_at = timezone.now()
        assignment.save(update_fields=['status', 'released_at'])
        TaskService.create_activity(
            task=task,
            assignment=assignment,
            event_type='unclaimed',
            actor_slack_id=slack_user_id,
            summary='Task released back to the queue',
        )
        TaskService.sync_task_projection(task)
        return assignment

    @staticmethod
    @transaction.atomic
    def cancel_task(task: Task, actor_slack_id: str, *, reason: str = "") -> Tuple[Task, Optional[TaskAssignment]]:
        """Cancel a task and any active assignment while preserving audit history."""
        task = Task.objects.select_for_update().get(pk=task.pk)

        if task.status == 'cancelled':
            raise ValueError('Task is already cancelled')
        if task.status == 'approved':
            raise ValueError('Approved tasks cannot be cancelled')

        active_assignment = task.get_active_assignment()
        if active_assignment:
            active_assignment.status = 'cancelled'
            active_assignment.closed_reason = 'task_cancelled'
            active_assignment.save(update_fields=['status', 'closed_reason'])

        task.status = 'cancelled'
        task.closed_at = timezone.now()
        task.closed_by_user_id = actor_slack_id
        task.save(update_fields=['status', 'closed_at', 'closed_by_user_id'])

        TaskService.create_activity(
            task=task,
            assignment=active_assignment,
            event_type='cancelled',
            actor_slack_id=actor_slack_id,
            summary='Task cancelled',
            metadata={'reason': reason} if reason else {},
        )
        return task, active_assignment


class RewardsService:
    """
    Service class for reward redemptions.
    """
    
    @staticmethod
    def list_available(user: Optional[User] = None) -> list:
        """
        List available rewards.
        
        If user is provided, includes information about their eligibility.
        """
        rewards = RewardsCatalog.objects.filter(is_active=True)
        
        result = []
        for reward in rewards:
            item = {
                'code': reward.code,
                'name': reward.name,
                'description': reward.description,
                'cost_points': reward.cost_points,
                'fulfillment': reward.fulfillment,
                'stock_remaining': reward.stock_remaining,
            }
            
            if user:
                account = PointsService.get_or_create_account(user)
                item['can_afford'] = account.balance >= reward.cost_points
                item['user_balance'] = account.balance
            
            result.append(item)
        
        return result
    
    @staticmethod
    @transaction.atomic
    def request_redemption(
        user: User,
        reward_code: str,
        quantity: int = 1,
        notes: Optional[str] = None,
        slack_channel_id: Optional[str] = None,
        slack_thread_ts: Optional[str] = None,
    ) -> RewardRedemption:
        """
        Request a reward redemption.
        
        For AUTO fulfillment rewards, points are deducted immediately.
        For MANUAL fulfillment, points are deducted on approval.
        
        Args:
            user: The User requesting the reward
            reward_code: Code of the reward to redeem
            quantity: Number of rewards to redeem
            notes: Optional notes from the user
            slack_channel_id: Optional Slack channel
            slack_thread_ts: Optional Slack thread
            
        Returns:
            RewardRedemption instance
            
        Raises:
            ValueError: If reward not found or not active
            InsufficientBalanceError: If user can't afford the reward (AUTO only)
        """
        # Principal-first ordering keeps this points mutation compatible with
        # account merges, which lock User before any owned/catalog rows.
        user = User.objects.select_for_update().get(pk=user.pk)
        # Lock reward for update to handle stock concurrency
        try:
            reward = RewardsCatalog.objects.select_for_update().get(code=reward_code, is_active=True)
        except RewardsCatalog.DoesNotExist:
            raise ValueError(f"Reward {reward_code} not found or not available")
        
        total_cost = reward.cost_points * quantity
        
        # Check stock if applicable
        if reward.stock_remaining is not None:
            if reward.stock_remaining < quantity:
                raise ValueError(f"Insufficient stock (remaining: {reward.stock_remaining})")
        
        # Check max per user limit
        if reward.max_per_user:
            existing_count = RewardRedemption.objects.filter(
                user=user,
                reward=reward,
                status__in=['requested', 'approved', 'fulfilled']
            ).aggregate(total=models.Sum('quantity'))['total'] or 0
            
            if existing_count + quantity > reward.max_per_user:
                raise ValueError(
                    f"Exceeds maximum redemptions per user ({reward.max_per_user})"
                )
        
        redemption = RewardRedemption.objects.create(
            user=user,
            reward=reward,
            quantity=quantity,
            status='requested',
            notes=notes,
            slack_channel_id=slack_channel_id,
            slack_thread_ts=slack_thread_ts,
        )

        # Decrement stock
        if reward.stock_remaining is not None:
            reward.stock_remaining -= quantity
            reward.save()
        
        # For AUTO fulfillment, deduct points immediately
        if reward.fulfillment == 'auto':
            idempotency_key = f"reward_spend:{redemption.id}"
            
            ledger, _ = PointsService.spend(
                user=user,
                delta=total_cost,
                source='MERCH',  # or 'TOOLS' depending on reward type
                description=f"Redeemed reward: {reward.name} x{quantity}",
                created_by_slack_id=user.slack_id or '',
                idempotency_key=idempotency_key,
                reference_type='REWARD_REDEMPTION',
                reference_id=str(redemption.id),
            )
            
            redemption.ledger_entry = ledger
            redemption.status = 'approved'
            redemption.approved_at = timezone.now()
            redemption.save()
        
        return redemption
    
    @staticmethod
    @transaction.atomic
    def approve_redemption(
        redemption: RewardRedemption,
        approver_slack_id: str,
    ) -> RewardRedemption:
        """
        Approve a reward redemption and deduct points.
        
        Args:
            redemption: The RewardRedemption to approve
            approver_slack_id: Slack ID of the approver
            
        Returns:
            Updated RewardRedemption
            
        Raises:
            PermissionDeniedError: If approver is not an admin
            ValueError: If redemption is not in requested status
            InsufficientBalanceError: If user can't afford the reward
        """
        if not is_points_admin(approver_slack_id):
            raise PermissionDeniedError(
                f"{approver_slack_id} is not authorized to approve redemptions"
            )
        
        if redemption.status != 'requested':
            raise ValueError(
                f"Redemption is not in requested status (current: {redemption.status})"
            )
        
        reward = redemption.reward
        total_cost = reward.cost_points * redemption.quantity
        
        idempotency_key = f"reward_spend:{redemption.id}"
        
        ledger, _ = PointsService.spend(
            user=redemption.user,
            delta=total_cost,
            source='MERCH',
            description=f"Redeemed reward: {reward.name} x{redemption.quantity}",
            created_by_slack_id=approver_slack_id,
            idempotency_key=idempotency_key,
            reference_type='REWARD_REDEMPTION',
            reference_id=str(redemption.id),
        )
        
        redemption.ledger_entry = ledger
        redemption.status = 'approved'
        redemption.approved_at = timezone.now()
        redemption.approved_by_slack_id = approver_slack_id
        redemption.save()
        
        return redemption
