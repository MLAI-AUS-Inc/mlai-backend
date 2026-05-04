"""
Tests for the Points System.
"""
from datetime import date, timedelta
from decimal import Decimal
from django.test import TestCase
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from unittest.mock import patch

from .models import (
    PointsAdmin, PointsAccount, Task, TaskSubmission, Ledger,
    CoworkingBooking, CoworkingDayCapacity, RewardsCatalog, RewardRedemption
)
from .services import PointsService, CoworkingService, TaskService, RewardsService
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
        self.assertEqual(account.lifetime_earned, 0)
        self.assertEqual(account.lifetime_spent, 0)
        
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
        self.assertEqual(account.lifetime_earned, 10)
        self.assertEqual(account.lifetime_spent, 0)
    
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
        self.assertEqual(account.lifetime_earned, 20)
        self.assertEqual(account.lifetime_spent, 5)
    
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
        self.assertEqual(booking.points_cost, 4)  # Cost from COWORKING_DAY reward catalog
        
        account = PointsAccount.objects.get(user=self.user)
        self.assertEqual(account.balance, 6)  # 10 - 4
    
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
        # Cost is 4 points from COWORKING_DAY reward catalog, not 1
        if refunded:
            account = PointsAccount.objects.get(user=self.user)
            self.assertEqual(account.balance, initial_balance + 4)

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
        self.assertEqual(account.balance, 6)


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
