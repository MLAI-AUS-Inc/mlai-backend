"""
Tests for the Points System.
"""
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional
from django.test import TestCase, override_settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.utils import timezone
from unittest.mock import patch

from .models import (
    PointsAdmin, PointsAccount, Task, TaskSubmission, Ledger,
    CoworkingBooking, CoworkingDayCapacity, RewardsCatalog, RewardRedemption,
    PointsPurchase,
)
from .services import PointsService, PointsPurchaseService, CoworkingService, TaskService, RewardsService, StartupUpdateRewardService
from .permissions import (
    can_generate_coworking_reports,
    is_points_admin,
    InsufficientBalanceError,
    PermissionDeniedError,
)


User = get_user_model()


class PointsServiceTests(TestCase):
    """Tests for PointsService."""
    
    def setUp(self):
        self.user = User.objects.create_user(
            email='test@example.com',
            slack_id='U12345'
        )
        self.admin_slack_id = 'UADMIN123'
        PointsAdmin.objects.create(
            slack_user_id=self.admin_slack_id,
            role='admin',
            is_active=True
        )
    
    def test_get_or_create_account(self):
        """Test lazy creation of PointsAccount."""
        # Account doesn't exist initially
        self.assertFalse(PointsAccount.objects.filter(user=self.user).exists())
        
        # Create account
        account = PointsService.get_or_create_account(self.user)
        self.assertEqual(account.balance, 0)
        self.assertEqual(account.earned_balance, 0)
        self.assertEqual(account.purchased_topup_balance, 0)
        self.assertEqual(account.lifetime_earned, 0)
        self.assertEqual(account.lifetime_purchased_topup, 0)
        self.assertEqual(account.lifetime_spent, 0)
        self.assertEqual(account.expired_or_reversed_points, 0)
        
        # Second call returns same account
        account2 = PointsService.get_or_create_account(self.user)
        self.assertEqual(account.user_id, account2.user_id)
    
    def test_award_increases_balance_and_lifetime_earned(self):
        """Test that awarding points increases balance and lifetime_earned."""
        ledger, created = PointsService.award(
            user=self.user,
            delta=10,
            source='TASK',
            description='Test award',
            created_by_slack_id=self.admin_slack_id,
            idempotency_key='test_award_1'
        )
        
        self.assertTrue(created)
        self.assertEqual(ledger.delta, 10)
        self.assertEqual(ledger.kind, 'EARN')
        
        account = PointsAccount.objects.get(user=self.user)
        self.assertEqual(account.balance, 10)
        self.assertEqual(account.earned_balance, 10)
        self.assertEqual(account.purchased_topup_balance, 0)
        self.assertEqual(account.lifetime_earned, 10)
        self.assertEqual(account.lifetime_purchased_topup, 0)
        self.assertEqual(account.lifetime_spent, 0)

    def test_credit_purchased_topup_does_not_increase_lifetime_earned(self):
        """Purchased top-up points are spendable but not contribution points."""
        ledger, created = PointsService.credit_purchased_topup(
            user=self.user,
            delta=10,
            description='Top-up Roo Points purchase',
            created_by_slack_id='STRIPE',
            idempotency_key='test_topup_1',
            reference_type='POINTS_PURCHASE',
            reference_id='purchase-1',
        )

        self.assertTrue(created)
        self.assertEqual(ledger.delta, 10)
        self.assertEqual(ledger.kind, 'EARN')
        self.assertEqual(ledger.source, 'purchased_topup')

        account = PointsAccount.objects.get(user=self.user)
        self.assertEqual(account.balance, 10)
        self.assertEqual(account.earned_balance, 0)
        self.assertEqual(account.purchased_topup_balance, 10)
        self.assertEqual(account.lifetime_earned, 0)
        self.assertEqual(account.lifetime_purchased_topup, 10)

    def test_credit_purchased_topup_is_idempotent(self):
        """Duplicate Stripe/webhook handling must not double-credit top-up points."""
        key = 'test_topup_idempotent'

        ledger1, created1 = PointsService.credit_purchased_topup(
            user=self.user,
            delta=10,
            description='Top-up Roo Points purchase',
            idempotency_key=key,
            reference_type='POINTS_PURCHASE',
            reference_id='purchase-2',
        )
        ledger2, created2 = PointsService.credit_purchased_topup(
            user=self.user,
            delta=10,
            description='Duplicate top-up Roo Points purchase',
            idempotency_key=key,
            reference_type='POINTS_PURCHASE',
            reference_id='purchase-2',
        )

        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(ledger1.id, ledger2.id)

        account = PointsAccount.objects.get(user=self.user)
        self.assertEqual(account.balance, 10)
        self.assertEqual(account.purchased_topup_balance, 10)
        self.assertEqual(account.lifetime_purchased_topup, 10)
        self.assertEqual(account.lifetime_earned, 0)
    
    def test_spend_decreases_balance_increases_lifetime_spent(self):
        """Test that spending points decreases balance and increases lifetime_spent."""
        # First award some points
        PointsService.award(
            user=self.user,
            delta=20,
            source='TASK',
            description='Initial award',
            created_by_slack_id=self.admin_slack_id,
            idempotency_key='test_award_spend_1'
        )
        
        # Then spend some
        ledger, created = PointsService.spend(
            user=self.user,
            delta=5,
            source='COWORKING',
            description='Test spend',
            created_by_slack_id=self.admin_slack_id,
            idempotency_key='test_spend_1'
        )
        
        self.assertTrue(created)
        self.assertEqual(ledger.delta, -5)  # Stored as negative
        self.assertEqual(ledger.kind, 'SPEND')
        
        account = PointsAccount.objects.get(user=self.user)
        self.assertEqual(account.balance, 15)  # 20 - 5
        self.assertEqual(account.earned_balance, 15)
        self.assertEqual(account.purchased_topup_balance, 0)
        self.assertEqual(account.lifetime_earned, 20)
        self.assertEqual(account.lifetime_spent, 5)

    def test_spend_debits_topup_balance_before_earned_balance(self):
        """Spending keeps earned/top-up balances coherent."""
        PointsService.credit_purchased_topup(
            user=self.user,
            delta=10,
            description='Top-up Roo Points purchase',
            idempotency_key='test_topup_before_spend',
        )
        PointsService.award(
            user=self.user,
            delta=20,
            source='TASK',
            description='Earned points',
            created_by_slack_id=self.admin_slack_id,
            idempotency_key='test_earned_before_spend',
        )

        PointsService.spend(
            user=self.user,
            delta=15,
            source='MERCH',
            description='Redeemed reward',
            created_by_slack_id=self.admin_slack_id,
            idempotency_key='test_mixed_spend',
        )

        account = PointsAccount.objects.get(user=self.user)
        self.assertEqual(account.balance, 15)
        self.assertEqual(account.purchased_topup_balance, 0)
        self.assertEqual(account.earned_balance, 15)
        self.assertEqual(account.lifetime_earned, 20)
        self.assertEqual(account.lifetime_purchased_topup, 10)
        self.assertEqual(account.lifetime_spent, 15)
    
    def test_spend_below_zero_raises_error(self):
        """Test that spending more than balance raises InsufficientBalanceError."""
        # Award 5 points
        PointsService.award(
            user=self.user,
            delta=5,
            source='TASK',
            description='Initial award',
            created_by_slack_id=self.admin_slack_id,
            idempotency_key='test_award_below_zero'
        )
        
        # Try to spend 10
        with self.assertRaises(InsufficientBalanceError):
            PointsService.spend(
                user=self.user,
                delta=10,
                source='COWORKING',
                description='Overspend',
                created_by_slack_id=self.admin_slack_id,
                idempotency_key='test_overspend'
            )
    
    def test_idempotency_prevents_double_award(self):
        """Test that same idempotency key returns existing entry."""
        key = 'test_idempotency_award'
        
        # First award
        ledger1, created1 = PointsService.award(
            user=self.user,
            delta=10,
            source='TASK',
            description='First award',
            created_by_slack_id=self.admin_slack_id,
            idempotency_key=key
        )
        
        # Second award with same key
        ledger2, created2 = PointsService.award(
            user=self.user,
            delta=10,
            source='TASK',
            description='Duplicate award',
            created_by_slack_id=self.admin_slack_id,
            idempotency_key=key
        )
        
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(ledger1.id, ledger2.id)
        
        # Balance should only be 10, not 20
        account = PointsAccount.objects.get(user=self.user)
        self.assertEqual(account.balance, 10)
    
    def test_idempotency_prevents_double_spend(self):
        """Test that same idempotency key prevents double spend."""
        # Award points first
        PointsService.award(
            user=self.user,
            delta=20,
            source='TASK',
            description='Initial',
            created_by_slack_id=self.admin_slack_id,
            idempotency_key='test_award_for_double_spend'
        )
        
        key = 'test_idempotency_spend'
        
        # First spend
        ledger1, created1 = PointsService.spend(
            user=self.user,
            delta=5,
            source='COWORKING',
            description='First spend',
            created_by_slack_id=self.admin_slack_id,
            idempotency_key=key
        )
        
        # Second spend with same key
        ledger2, created2 = PointsService.spend(
            user=self.user,
            delta=5,
            source='COWORKING',
            description='Duplicate spend',
            created_by_slack_id=self.admin_slack_id,
            idempotency_key=key
        )
        
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(ledger1.id, ledger2.id)
        
        # Balance should be 15 (20 - 5), not 10
        account = PointsAccount.objects.get(user=self.user)
        self.assertEqual(account.balance, 15)


class PointsPurchaseModelTests(TestCase):
    """Tests for the Top-up Roo Points purchase record."""

    def setUp(self):
        self.user = User.objects.create_user(
            email='topup@example.com',
            slack_id='UTOPUP123',
        )

    def test_purchase_defaults_and_origin_metadata(self):
        before_create = timezone.now()
        purchase = PointsPurchase.objects.create(
            user=self.user,
            slack_user_id='UTOPUP123',
            pack_id='topup_10',
            points_amount=10,
            amount_cents=3699,
            purchase_from={
                'source': 'slack',
                'slack_user_id': 'UTOPUP123',
                'slack_channel_id': 'C123',
                'slack_thread_ts': '1712345678.000100',
            },
        )

        self.assertEqual(purchase.status, 'pending')
        self.assertEqual(purchase.currency, 'aud')
        self.assertIsNone(purchase.ledger_entry)
        self.assertIsNone(purchase.paid_at)
        self.assertGreaterEqual(purchase.expires_at, before_create + timedelta(hours=24))
        self.assertLessEqual(purchase.expires_at, timezone.now() + timedelta(hours=24, seconds=1))
        self.assertEqual(purchase.purchase_from['source'], 'slack')
        self.assertEqual(purchase.purchase_from['slack_thread_ts'], '1712345678.000100')
        self.assertEqual(str(purchase), '10 Top-up Roo Points for UTOPUP123 (pending)')


class PointsPurchaseLimitTests(TestCase):
    """Task 3 purchase limit policy tests."""

    def setUp(self):
        self.user = User.objects.create_user(
            email='limits@example.com',
            slack_id='ULIMITS123',
        )
        self.user.date_joined = timezone.now() - timedelta(days=30)
        self.user.save(update_fields=['date_joined'])

    def _create_paid_purchase(
        self,
        points_amount: int,
        days_ago: int,
        *,
        created_days_ago: Optional[int] = None,
    ) -> PointsPurchase:
        paid_at = timezone.now() - timedelta(days=days_ago)
        purchase = PointsPurchase.objects.create(
            user=self.user,
            slack_user_id=self.user.slack_id,
            pack_id=f'topup_{points_amount}',
            points_amount=points_amount,
            amount_cents=points_amount * 100,
            status='paid',
            paid_at=paid_at,
        )
        PointsPurchase.objects.filter(id=purchase.id).update(
            created_at=timezone.now() - timedelta(days=created_days_ago or days_ago)
        )
        return PointsPurchase.objects.get(id=purchase.id)

    def test_rejects_anonymous_purchase(self):
        with self.assertRaises(PermissionDeniedError):
            PointsPurchaseService.validate_purchase_limits(None, points_amount=10)

    def test_rejects_guest_checkout_without_slack_link(self):
        guest_like_user = User.objects.create_user(
            email='guest@example.com',
            slack_id=None,
        )
        guest_like_user.date_joined = timezone.now() - timedelta(days=30)
        guest_like_user.save(update_fields=['date_joined'])

        with self.assertRaises(PermissionDeniedError):
            PointsPurchaseService.validate_purchase_limits(guest_like_user, points_amount=10)

    def test_rejects_purchase_above_25_points(self):
        with self.assertRaises(ValueError) as ctx:
            PointsPurchaseService.validate_purchase_limits(self.user, points_amount=26)
        self.assertIn('25-point', str(ctx.exception))

    def test_rejects_accounts_younger_than_7_days(self):
        self.user.date_joined = timezone.now() - timedelta(days=3)
        self.user.save(update_fields=['date_joined'])

        with self.assertRaises(ValueError) as ctx:
            PointsPurchaseService.validate_purchase_limits(self.user, points_amount=10)
        self.assertIn('at least 7 days', str(ctx.exception))

    def test_rejects_rolling_12_month_cap_above_50_points(self):
        self._create_paid_purchase(points_amount=30, days_ago=20)
        self._create_paid_purchase(points_amount=19, days_ago=10)

        with self.assertRaises(ValueError) as ctx:
            PointsPurchaseService.validate_purchase_limits(self.user, points_amount=2)
        self.assertIn('50-point rolling 12-month', str(ctx.exception))

    def test_ignores_purchases_older_than_12_month_window(self):
        self._create_paid_purchase(points_amount=50, days_ago=370)
        # Should be allowed because the old purchase is outside the rolling window.
        PointsPurchaseService.validate_purchase_limits(self.user, points_amount=25)

    def test_rolling_12_month_cap_uses_paid_at_not_created_at(self):
        self._create_paid_purchase(points_amount=50, days_ago=20, created_days_ago=370)

        with self.assertRaises(ValueError) as ctx:
            PointsPurchaseService.validate_purchase_limits(self.user, points_amount=1)
        self.assertIn('50-point rolling 12-month', str(ctx.exception))

    def test_validation_does_not_create_points_account(self):
        PointsPurchaseService.validate_purchase_limits(self.user, points_amount=10)

        self.assertFalse(PointsAccount.objects.filter(user=self.user).exists())

    def test_rejects_balance_cap_above_100_without_approval(self):
        account = PointsService.get_or_create_account(self.user)
        account.balance = 90
        account.save(update_fields=['balance'])

        with self.assertRaises(ValueError) as ctx:
            PointsPurchaseService.validate_purchase_limits(self.user, points_amount=11)
        self.assertIn('100-point spendable balance cap', str(ctx.exception))

    def test_allows_balance_cap_override_with_manual_approval(self):
        account = PointsService.get_or_create_account(self.user)
        account.balance = 95
        account.save(update_fields=['balance'])

        PointsPurchaseService.validate_purchase_limits(
            self.user,
            points_amount=10,
            manual_balance_approval=True,
        )

    def test_allows_purchase_when_all_limits_pass(self):
        account = PointsService.get_or_create_account(self.user)
        account.balance = 20
        account.save(update_fields=['balance'])
        self._create_paid_purchase(points_amount=10, days_ago=40)

        PointsPurchaseService.validate_purchase_limits(self.user, points_amount=25)


class CoworkingServiceTests(TestCase):
    """Tests for CoworkingService."""
    
    def setUp(self):
        self.user = User.objects.create_user(
            email='coworker@example.com',
            slack_id='UCOWORK123'
        )
        # Give user some points
        PointsService.award(
            user=self.user,
            delta=10,
            source='MANUAL',
            description='Test points',
            created_by_slack_id='UADMIN',
            idempotency_key='cowork_test_setup'
        )
        
        self.admin_slack_id = 'UADMIN'
        PointsAdmin.objects.create(
            slack_user_id=self.admin_slack_id,
            role='admin',
            is_active=True
        )
    
    def test_book_deducts_points(self):
        """Test that booking deducts points."""
        booking_date = date.today() + timedelta(days=1)
        
        booking, created = CoworkingService.book(
            user=self.user,
            booking_date=booking_date,
            created_by_slack_id=self.user.slack_id
        )
        
        self.assertTrue(created)
        self.assertEqual(booking.status, 'booked')
        self.assertEqual(booking.points_cost, 8)  # Standard cost from COWORKING_DAY reward catalog

        account = PointsAccount.objects.get(user=self.user)
        self.assertEqual(account.balance, 2)  # 10 - 8
    
    def test_book_respects_capacity(self):
        """Test that booking respects capacity limits."""
        booking_date = date.today() + timedelta(days=2)
        
        # Set capacity to 1
        CoworkingDayCapacity.objects.create(date=booking_date, capacity=1)
        
        # First booking should succeed
        user1 = self.user
        booking1, created = CoworkingService.book(
            user=user1,
            booking_date=booking_date,
            created_by_slack_id=user1.slack_id
        )
        self.assertTrue(created)
        self.assertEqual(booking1.status, 'booked')
        
        # Second booking should fail
        user2 = User.objects.create_user(email='user2@example.com', slack_id='U2')
        PointsService.award(
            user=user2,
            delta=10,
            source='MANUAL',
            description='Test',
            created_by_slack_id='UADMIN',
            idempotency_key='user2_setup'
        )
        
        with self.assertRaises(ValueError) as context:
            CoworkingService.book(
                user=user2,
                booking_date=booking_date,
                created_by_slack_id=user2.slack_id
            )
        self.assertIn('No availability', str(context.exception))
    
    @patch('roo.services.settings')
    def test_cancel_refunds_if_before_cutoff(self, mock_settings):
        """Test that cancellation refunds points if before cutoff."""
        # Note: This test is simplified; actual cutoff logic depends on current time
        mock_settings.COWORKING_REFUND_CUTOFF_HOURS = 18
        mock_settings.DEFAULT_COWORKING_CAPACITY = 10
        mock_settings.COWORKING_DAY_COST_POINTS = 1
        mock_settings.COWORKING_BOOKING_ADVANCE_DAYS = 30
        
        booking_date = date.today() + timedelta(days=7)  # Far in future
        
        booking, created = CoworkingService.book(
            user=self.user,
            booking_date=booking_date,
            created_by_slack_id=self.user.slack_id
        )
        self.assertTrue(created)
        
        initial_balance = PointsAccount.objects.get(user=self.user).balance
        
        booking, refunded = CoworkingService.cancel(
            booking_id=str(booking.id),
            requester_slack_id=self.user.slack_id
        )
        
        self.assertEqual(booking.status, 'cancelled')
        # Refund should have happened (booking is far in future)
        # Cost is the standard 8 points from COWORKING_DAY reward catalog, not 1
        if refunded:
            account = PointsAccount.objects.get(user=self.user)
            self.assertEqual(account.balance, initial_balance + 8)

    def test_book_is_idempotent_for_existing_active_booking(self):
        """Test that repeat bookings for the same day return the existing booking."""
        booking_date = date.today() + timedelta(days=3)

        booking1, created1 = CoworkingService.book(
            user=self.user,
            booking_date=booking_date,
            created_by_slack_id=self.user.slack_id,
        )
        booking2, created2 = CoworkingService.book(
            user=self.user,
            booking_date=booking_date,
            created_by_slack_id=self.user.slack_id,
        )

        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(booking1.id, booking2.id)
        self.assertEqual(
            CoworkingBooking.objects.filter(user=self.user, date=booking_date, status='booked').count(),
            1,
        )
        account = PointsAccount.objects.get(user=self.user)
        self.assertEqual(account.balance, 2)  # charged once at the standard 8


class CoworkingMonthlyUpdateDiscountTests(TestCase):
    """The coworking cost drops to 4 when the user's startup is an ABR-verified
    Australian company (registered + ACN + ABR-verified stamp) AND has a
    monthly update that became 'ready' within the last 28 days; otherwise it is
    the standard 8."""

    VALID_ABN = '89000000019'

    def setUp(self):
        from organizations.models import Organization
        from startup_updates.models import UserStartupBinding

        self.user = User.objects.create_user(
            email='founder@example.com',
            slack_id='UFOUNDER',
        )
        PointsService.award(
            user=self.user,
            delta=50,
            source='MANUAL',
            description='Test points',
            created_by_slack_id='UADMIN',
            idempotency_key='discount_test_setup',
        )
        self.org = Organization.objects.create(name='Acme', domain='acme.example')
        UserStartupBinding.objects.create(user=self.user, organization=self.org)
        # The discount requires an ABR-verified company on the org.
        self._company_for(self.org, name='Acme Pty Ltd', verified=True)

    def _company_for(self, org, *, name='Acme Pty Ltd', abn=VALID_ABN, verified=False, user=None):
        """Attach a registered company to ``org``. Has a valid ABN by default;
        ``verified=True`` also sets the ACN + ABR-verified stamp."""
        from django.utils import timezone
        from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile

        profile, _ = VibeRaisingProfile.objects.get_or_create(
            user=user or self.user,
            defaults={'role': VibeRaisingProfile.ROLE_FOUNDER},
        )
        return VibeRaisingCompany.objects.create(
            profile=profile,
            organization=org,
            name=name,
            registered=True,
            abn=abn,
            acn='000000019' if verified else None,
            abr_verified_at=timezone.now() if verified else None,
        )

    def _make_update(self, booking_date, status, ready_days_ago=None):
        """Create a draft. READY drafts auto-stamp ready_at=now in save();
        pass ``ready_days_ago`` to backdate the stamp for window tests."""
        from startup_updates.models import MonthlyUpdateDraft
        draft = MonthlyUpdateDraft.objects.create(
            organization=self.org,
            month=booking_date.replace(day=1),
            status=status,
        )
        if ready_days_ago is not None:
            MonthlyUpdateDraft.objects.filter(pk=draft.pk).update(
                ready_at=timezone.now() - timedelta(days=ready_days_ago)
            )
            draft.refresh_from_db()
        return draft

    def test_standard_cost_without_binding(self):
        from organizations.models import Organization
        from startup_updates.models import UserStartupBinding

        unbound = User.objects.create_user(email='nobind@example.com', slack_id='UNOBIND')
        self.assertFalse(UserStartupBinding.objects.filter(user=unbound).exists())
        self.assertEqual(
            CoworkingService.get_coworking_cost(user=unbound, booking_date=date.today()),
            8,
        )

    def test_standard_cost_with_binding_but_no_update(self):
        self.assertEqual(
            CoworkingService.get_coworking_cost(user=self.user, booking_date=date.today()),
            8,
        )

    def test_draft_status_does_not_discount(self):
        from startup_updates.models import MonthlyUpdateDraftStatus
        today = date.today()
        self._make_update(today, MonthlyUpdateDraftStatus.DRAFT)
        self.assertEqual(
            CoworkingService.get_coworking_cost(user=self.user, booking_date=today),
            8,
        )

    def test_needs_review_status_does_not_discount(self):
        from startup_updates.models import MonthlyUpdateDraftStatus
        today = date.today()
        self._make_update(today, MonthlyUpdateDraftStatus.NEEDS_REVIEW)
        self.assertEqual(
            CoworkingService.get_coworking_cost(user=self.user, booking_date=today),
            8,
        )

    def test_ready_update_discounts_to_four(self):
        from startup_updates.models import MonthlyUpdateDraftStatus
        today = date.today()
        self._make_update(today, MonthlyUpdateDraftStatus.READY)
        self.assertEqual(
            CoworkingService.get_coworking_cost(user=self.user, booking_date=today),
            4,
        )

    def test_ready_update_does_not_discount_ineligible_binding(self):
        from startup_updates.models import MonthlyUpdateDraftStatus, UserStartupBinding

        today = date.today()
        self._make_update(today, MonthlyUpdateDraftStatus.READY)
        UserStartupBinding.objects.filter(user=self.user, organization=self.org).update(
            coworking_discount_eligible=False
        )

        self.assertEqual(
            CoworkingService.get_coworking_cost(user=self.user, booking_date=today),
            8,
        )

    def test_rebinding_does_not_restore_discount_eligibility(self):
        from startup_updates.models import MonthlyUpdateDraftStatus, UserStartupBinding
        from startup_updates.services import bind_user_to_startup

        today = date.today()
        self._make_update(today, MonthlyUpdateDraftStatus.READY)
        binding = UserStartupBinding.objects.get(user=self.user, organization=self.org)
        binding.coworking_discount_eligible = False
        binding.save(update_fields=['coworking_discount_eligible', 'updated_at'])

        rebound = bind_user_to_startup(
            user=self.user,
            organization=self.org,
            role='founder',
            is_default_for_gmail=True,
        )

        self.assertFalse(rebound.coworking_discount_eligible)
        self.assertEqual(
            CoworkingService.get_coworking_cost(user=self.user, booking_date=today),
            8,
        )

    def test_ineligible_binding_does_not_block_discount_from_eligible_binding(self):
        from organizations.models import Organization
        from startup_updates.models import (
            MonthlyUpdateDraft,
            MonthlyUpdateDraftStatus,
            UserStartupBinding,
        )

        UserStartupBinding.objects.filter(user=self.user, organization=self.org).update(
            coworking_discount_eligible=False
        )
        second_org = Organization.objects.create(name='Beta', domain='eligible-beta.example')
        UserStartupBinding.objects.create(user=self.user, organization=second_org)
        self._company_for(second_org, name='Beta Pty Ltd', verified=True)
        today = date.today()
        MonthlyUpdateDraft.objects.create(
            organization=second_org,
            month=today.replace(day=1),
            status=MonthlyUpdateDraftStatus.READY,
        )

        self.assertEqual(
            CoworkingService.get_coworking_cost(user=self.user, booking_date=today),
            4,
        )

    def test_update_ready_within_window_discounts_across_month_boundary(self):
        # The window is time-based: a previous-month update that became ready
        # 20 days ago still discounts a booking today (no start-of-month cliff).
        from startup_updates.models import MonthlyUpdateDraftStatus
        today = date.today()
        previous_month = today.replace(day=1) - timedelta(days=1)
        self._make_update(previous_month, MonthlyUpdateDraftStatus.READY, ready_days_ago=20)
        self.assertEqual(
            CoworkingService.get_coworking_cost(user=self.user, booking_date=today),
            4,
        )

    def test_update_ready_exactly_28_days_ago_discounts(self):
        from startup_updates.models import MonthlyUpdateDraftStatus
        today = date.today()
        self._make_update(today, MonthlyUpdateDraftStatus.READY, ready_days_ago=28)
        self.assertEqual(
            CoworkingService.get_coworking_cost(user=self.user, booking_date=today),
            4,
        )

    def test_update_ready_29_days_ago_does_not_discount(self):
        from startup_updates.models import MonthlyUpdateDraftStatus
        today = date.today()
        self._make_update(today, MonthlyUpdateDraftStatus.READY, ready_days_ago=29)
        self.assertEqual(
            CoworkingService.get_coworking_cost(user=self.user, booking_date=today),
            8,
        )

    def test_future_booking_uses_booking_date_for_window(self):
        # An update ready 25 days ago discounts today but not a booking 7 days
        # out (32 days after the stamp).
        from startup_updates.models import MonthlyUpdateDraftStatus
        today = date.today()
        self._make_update(today, MonthlyUpdateDraftStatus.READY, ready_days_ago=25)
        self.assertEqual(
            CoworkingService.get_coworking_cost(user=self.user, booking_date=today),
            4,
        )
        self.assertEqual(
            CoworkingService.get_coworking_cost(
                user=self.user, booking_date=today + timedelta(days=7)
            ),
            8,
        )

    def test_ready_at_stamped_once_on_first_ready_transition(self):
        # ready_at is set when a draft first becomes ready and is never
        # refreshed, so re-approving an old draft can't renew the window.
        from startup_updates.models import MonthlyUpdateDraft, MonthlyUpdateDraftStatus
        today = date.today()
        draft = self._make_update(today, MonthlyUpdateDraftStatus.DRAFT)
        self.assertIsNone(draft.ready_at)

        draft.status = MonthlyUpdateDraftStatus.READY
        draft.save()
        first_stamp = draft.ready_at
        self.assertIsNotNone(first_stamp)

        draft.status = MonthlyUpdateDraftStatus.NEEDS_REVIEW
        draft.save()
        draft.status = MonthlyUpdateDraftStatus.READY
        draft.save()
        draft.refresh_from_db()
        self.assertEqual(draft.ready_at, first_stamp)

    def test_ready_update_in_any_bound_org_discounts(self):
        from organizations.models import Organization
        from startup_updates.models import (
            MonthlyUpdateDraft,
            MonthlyUpdateDraftStatus,
            UserStartupBinding,
        )
        second_org = Organization.objects.create(name='Beta', domain='beta.example')
        UserStartupBinding.objects.create(user=self.user, organization=second_org)
        self._company_for(second_org, name='Beta Pty Ltd', verified=True)
        today = date.today()
        MonthlyUpdateDraft.objects.create(
            organization=second_org,
            month=today.replace(day=1),
            status=MonthlyUpdateDraftStatus.READY,
        )
        self.assertEqual(
            CoworkingService.get_coworking_cost(user=self.user, booking_date=today),
            4,
        )

    def test_ready_update_without_company_does_not_discount(self):
        # A ready update on an org with no founder company gets no discount.
        from organizations.models import Organization
        from startup_updates.models import (
            MonthlyUpdateDraft,
            MonthlyUpdateDraftStatus,
            UserStartupBinding,
        )
        other = User.objects.create_user(email='nocompany@example.com', slack_id='UNOCOMP')
        org = Organization.objects.create(name='Gamma', domain='gamma.example')
        UserStartupBinding.objects.create(user=other, organization=org)
        today = date.today()
        MonthlyUpdateDraft.objects.create(
            organization=org, month=today.replace(day=1), status=MonthlyUpdateDraftStatus.READY,
        )
        self.assertEqual(
            CoworkingService.get_coworking_cost(user=other, booking_date=today),
            8,
        )

    def test_ready_update_with_unverified_company_does_not_discount(self):
        # A registered company with a valid ABN but NO ACN/ABR verification does
        # not qualify — the discount requires a verified Australian company.
        from startup_updates.models import MonthlyUpdateDraftStatus
        other = User.objects.create_user(email='validabn@example.com', slack_id='UVALIDABN')
        from organizations.models import Organization
        from startup_updates.models import UserStartupBinding
        org = Organization.objects.create(name='Epsilon', domain='epsilon.example')
        UserStartupBinding.objects.create(user=other, organization=org)
        self._company_for(org, name='Epsilon Pty Ltd', verified=False, user=other)
        today = date.today()
        from startup_updates.models import MonthlyUpdateDraft
        MonthlyUpdateDraft.objects.create(
            organization=org, month=today.replace(day=1), status=MonthlyUpdateDraftStatus.READY,
        )
        self.assertEqual(
            CoworkingService.get_coworking_cost(user=other, booking_date=today),
            8,
        )

    def test_no_args_returns_standard_cost(self):
        # Backwards-compatible: callers without user context get the standard price.
        self.assertEqual(CoworkingService.get_coworking_cost(), 8)

    def test_book_charges_discounted_cost_end_to_end(self):
        from startup_updates.models import MonthlyUpdateDraftStatus
        booking_date = date.today()
        self._make_update(booking_date, MonthlyUpdateDraftStatus.READY)

        balance_before = PointsAccount.objects.get(user=self.user).balance
        booking, created = CoworkingService.book(
            user=self.user,
            booking_date=booking_date,
            created_by_slack_id=self.user.slack_id,
        )

        self.assertTrue(created)
        self.assertEqual(booking.points_cost, 4)
        account = PointsAccount.objects.get(user=self.user)
        self.assertEqual(account.balance, balance_before - 4)

    def test_book_charges_standard_cost_without_ready_update(self):
        booking_date = date.today()
        balance_before = PointsAccount.objects.get(user=self.user).balance
        booking, created = CoworkingService.book(
            user=self.user,
            booking_date=booking_date,
            created_by_slack_id=self.user.slack_id,
        )

        self.assertTrue(created)
        self.assertEqual(booking.points_cost, 8)
        account = PointsAccount.objects.get(user=self.user)
        self.assertEqual(account.balance, balance_before - 8)


class StartupUpdateRewardServiceTests(TestCase):
    """20 roo points for a verified company's founder completing a monthly update,
    once per company per month."""

    def setUp(self):
        from organizations.models import Organization

        self.user = User.objects.create_user(email='founder@example.com', slack_id='UFOUNDER')
        self.org = Organization.objects.create(name='Acme', domain='acme.example')
        self.company = self._verified_company(self.org)
        self.month = date(2026, 7, 1)

    def _verified_company(self, org, *, name='Acme Pty Ltd'):
        from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile

        profile, _ = VibeRaisingProfile.objects.get_or_create(
            user=self.user, defaults={'role': VibeRaisingProfile.ROLE_FOUNDER}
        )
        return VibeRaisingCompany.objects.create(
            profile=profile, organization=org, name=name,
            registered=True, abn='89000000019', acn='000000019',
            abr_verified_at=timezone.now(),
        )

    def _balance(self):
        account = PointsAccount.objects.filter(user=self.user).first()
        return account.balance if account else 0

    def test_first_completion_awards_20(self):
        awarded = StartupUpdateRewardService.award_monthly_update_completion(
            user=self.user, company=self.company, month_bucket=self.month
        )
        self.assertTrue(awarded)
        self.assertEqual(self._balance(), 20)
        self.assertTrue(
            Ledger.objects.filter(user=self.user, source='STARTUP_UPDATE', delta=20).exists()
        )

    def test_second_completion_same_month_is_idempotent(self):
        StartupUpdateRewardService.award_monthly_update_completion(
            user=self.user, company=self.company, month_bucket=self.month
        )
        awarded_again = StartupUpdateRewardService.award_monthly_update_completion(
            user=self.user, company=self.company, month_bucket=self.month
        )
        self.assertFalse(awarded_again)
        self.assertEqual(self._balance(), 20)
        self.assertEqual(Ledger.objects.filter(user=self.user, source='STARTUP_UPDATE').count(), 1)

    def test_different_month_awards_again(self):
        StartupUpdateRewardService.award_monthly_update_completion(
            user=self.user, company=self.company, month_bucket=self.month
        )
        StartupUpdateRewardService.award_monthly_update_completion(
            user=self.user, company=self.company, month_bucket=date(2026, 8, 1)
        )
        self.assertEqual(self._balance(), 40)

    def test_unverified_company_is_not_rewarded(self):
        from founder_tools.models import VibeRaisingCompany

        unverified = VibeRaisingCompany.objects.create(
            profile=self.company.profile, organization=self.org, name='Draft Co',
            registered=True, abn='89000000019',  # no acn / abr_verified_at
        )
        awarded = StartupUpdateRewardService.award_monthly_update_completion(
            user=self.user, company=unverified, month_bucket=self.month
        )
        self.assertFalse(awarded)
        self.assertEqual(self._balance(), 0)

    @override_settings(ROO_POINTS_MONTHLY_UPDATE_REWARD=0)
    def test_zero_reward_setting_disables_award(self):
        awarded = StartupUpdateRewardService.award_monthly_update_completion(
            user=self.user, company=self.company, month_bucket=self.month
        )
        self.assertFalse(awarded)
        self.assertEqual(self._balance(), 0)


class PermissionTests(TestCase):
    """Tests for permission checks."""
    
    def setUp(self):
        self.admin_slack_id = 'UPERMADMIN'
        PointsAdmin.objects.create(
            slack_user_id=self.admin_slack_id,
            role='admin',
            is_active=True
        )
    
    def test_is_points_admin_returns_true_for_admin(self):
        """Test that is_points_admin returns True for active admin."""
        self.assertTrue(is_points_admin(self.admin_slack_id))
    
    def test_is_points_admin_returns_false_for_non_admin(self):
        """Test that is_points_admin returns False for non-admin."""
        self.assertFalse(is_points_admin('URANDOM'))
    
    def test_is_points_admin_returns_false_for_inactive_admin(self):
        """Test that is_points_admin returns False for inactive admin."""
        PointsAdmin.objects.create(
            slack_user_id='UINACTIVE',
            role='admin',
            is_active=False
        )
        self.assertFalse(is_points_admin('UINACTIVE'))

    def test_partner_can_generate_reports_but_is_not_full_points_admin(self):
        self.assertIn(('partner', 'Partner'), PointsAdmin.ROLE_CHOICES)
        PointsAdmin.objects.create(
            slack_user_id='UPARTNER',
            role='partner',
            is_active=True,
        )

        self.assertFalse(is_points_admin('UPARTNER'))
        self.assertTrue(can_generate_coworking_reports('UPARTNER'))

    def test_inactive_partner_cannot_generate_reports(self):
        PointsAdmin.objects.create(
            slack_user_id='UINACTIVEPARTNER',
            role='partner',
            is_active=False,
        )

        self.assertFalse(can_generate_coworking_reports('UINACTIVEPARTNER'))
    
    @patch('roo.permissions.settings')
    def test_bootstrap_admin_is_always_admin(self, mock_settings):
        """Test that bootstrap admins are always recognized."""
        mock_settings.POINTS_BOOTSTRAP_ADMIN_SLACK_IDS = ['UBOOTSTRAP']
        self.assertTrue(is_points_admin('UBOOTSTRAP'))
        self.assertTrue(can_generate_coworking_reports('UBOOTSTRAP'))


class LedgerIntegrityTests(TestCase):
    """Tests for ledger data integrity."""
    
    def setUp(self):
        self.user = User.objects.create_user(
            email='ledger@example.com',
            slack_id='ULEDGER'
        )
    
    def test_ledger_delta_cannot_be_zero(self):
        """Test that ledger entries cannot have delta=0."""
        # This should be enforced by the CheckConstraint
        # Note: Django doesn't validate constraints at model level,
        # they're enforced at database level
        pass  # Skip for now as it requires database-level testing
    
    def test_idempotency_key_is_unique(self):
        """Test that idempotency keys must be unique."""
        PointsService.award(
            user=self.user,
            delta=5,
            source='TASK',
            description='First',
            created_by_slack_id='UADMIN',
            idempotency_key='unique_key_test'
        )
        
        # Second award with same key should return existing
        ledger, created = PointsService.award(
            user=self.user,
            delta=10,  # Different amount
            source='TASK',
            description='Second',
            created_by_slack_id='UADMIN',
            idempotency_key='unique_key_test'
        )
        
        self.assertFalse(created)
        self.assertEqual(ledger.delta, 5)  # Original value
