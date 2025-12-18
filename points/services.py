"""
Points System Services - Business Logic Layer

This module provides safe, idempotent operations for the points system.
All write operations use database transactions and idempotency keys to prevent
race conditions and duplicate transactions.
"""
from datetime import date, datetime, timedelta
from typing import Optional, Tuple
from django.conf import settings
from django.db import transaction, IntegrityError
from django.utils import timezone

from .models import (
    PointsAccount, Ledger, Task, TaskSubmission,
    CoworkingBooking, CoworkingDayCapacity,
    RewardsCatalog, RewardRedemption, PointsAdmin
)
from .permissions import (
    is_points_admin, require_admin, 
    PermissionDeniedError, InsufficientBalanceError
)
from core.models import User


class PointsService:
    """
    Service class for points operations.
    All methods are idempotent and transaction-safe.
    """
    
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
        return {
            'balance': account.balance,
            'lifetime_earned': account.lifetime_earned,
            'lifetime_spent': account.lifetime_spent,
        }
    
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
        
        # Calculate start of current ISO week (Monday)
        today = timezone.now().date()
        start_of_week = today - timedelta(days=today.weekday())
        
        # Sum points awarded by this admin this week
        used = Ledger.objects.filter(
            kind='EARN',
            created_by_slack_id=slack_id,
            created_at__date__gte=start_of_week
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
        
        # Check for existing entry with same idempotency key
        existing = Ledger.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            return existing, False
        
        # Lock the account row for update
        account = PointsAccount.objects.select_for_update().filter(user=user).first()
        if not account:
            account = PointsAccount.objects.create(user=user)
            # Re-fetch with lock
            account = PointsAccount.objects.select_for_update().get(user=user)
        
        # Create ledger entry
        try:
            ledger = Ledger.objects.create(
                user=user,
                delta=delta,
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
            return existing, False
        
        # Update account
        account.balance += delta
        account.lifetime_earned += delta
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
        
        # Check for existing entry with same idempotency key
        existing = Ledger.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            return existing, False
        
        # Lock the account row for update
        account = PointsAccount.objects.select_for_update().filter(user=user).first()
        if not account:
            account = PointsAccount.objects.create(user=user)
            account = PointsAccount.objects.select_for_update().get(user=user)
        
        # Check balance
        if account.balance < delta:
            raise InsufficientBalanceError(
                f"Insufficient balance: {account.balance} < {delta} required"
            )
        
        # Create ledger entry (negative delta)
        try:
            ledger = Ledger.objects.create(
                user=user,
                delta=-delta,
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
            return existing, False
        
        # Update account
        account.balance -= delta
        account.lifetime_spent += delta
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
    ) -> Tuple[Ledger, bool]:
        """
        Refund points to a user (restore previously spent points).
        
        Similar to award but uses REFUND kind for audit trail clarity.
        """
        if delta <= 0:
            raise ValueError("Refund delta must be positive")
        
        existing = Ledger.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            return existing, False
        
        account = PointsAccount.objects.select_for_update().filter(user=user).first()
        if not account:
            account = PointsAccount.objects.create(user=user)
            account = PointsAccount.objects.select_for_update().get(user=user)
        
        try:
            ledger = Ledger.objects.create(
                user=user,
                delta=delta,
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
            return existing, False
        
        account.balance += delta
        # Note: Don't decrease lifetime_spent on refund - it's a historical record
        account.save()
        
        return ledger, True


class CoworkingService:
    """
    Service class for coworking bookings.
    """
    
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
            return getattr(settings, 'DEFAULT_COWORKING_CAPACITY', 10)
    
    @staticmethod
    def get_booked_count(booking_date: date) -> int:
        """Get count of active bookings for a date."""
        return CoworkingBooking.objects.filter(
            date=booking_date,
            status='booked'
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
    ) -> CoworkingBooking:
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
        
        # Check if user already has a booking for this date
        existing = CoworkingBooking.objects.filter(
            user=user,
            date=booking_date,
            status='booked'
        ).first()
        if existing:
            raise ValueError("You already have a booking for this date")
        
        # Check availability
        available, capacity = CoworkingService.check_availability(booking_date)
        if available <= 0:
            raise ValueError(f"No availability for {booking_date} (capacity: {capacity})")
        
        # Get cost
        cost = getattr(settings, 'COWORKING_DAY_COST_POINTS', 1)
        
        # Create idempotency key
        idempotency_key = f"coworking_book:{user.id}:{booking_date}"
        
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
            user=user,
            date=booking_date,
            status='booked',
            points_cost=cost,
            ledger_entry=ledger,
            slack_channel_id=slack_channel_id,
        )
        
        return booking
    
    @staticmethod
    @transaction.atomic
    def cancel(
        booking_id: str,
        requester_slack_id: str,
    ) -> Tuple[CoworkingBooking, bool]:
        """
        Cancel a booking with conditional refund.
        
        Args:
            booking_id: UUID of the booking
            requester_slack_id: Slack ID of who is cancelling
            
        Returns:
            Tuple of (booking, refunded: bool)
            
        Raises:
            ValueError: If booking not found or already cancelled
        """
        try:
            booking = CoworkingBooking.objects.select_for_update().get(id=booking_id)
        except CoworkingBooking.DoesNotExist:
            raise ValueError(f"Booking {booking_id} not found")
        
        if booking.status == 'cancelled':
            raise ValueError("Booking is already cancelled")
        
        booking.status = 'cancelled'
        booking.cancelled_at = timezone.now()
        
        refunded = False
        
        # Check if refund is applicable
        if CoworkingService.is_refundable(booking.date):
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


class TaskService:
    """
    Service class for task operations.
    """
    
    @staticmethod
    @transaction.atomic
    def approve_submission(
        submission: TaskSubmission,
        approver_slack_id: str,
    ) -> Tuple[TaskSubmission, Ledger]:
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
        if not is_points_admin(approver_slack_id):
            raise PermissionDeniedError(f"{approver_slack_id} is not authorized to approve submissions")
        
        if submission.status != 'submitted':
            raise ValueError(f"Submission is not in submitted status (current: {submission.status})")
        
        task = submission.task
        user = submission.user
        
        # Create idempotency key
        idempotency_key = f"task_award:{task.id}:{user.id}"
        
        # Award points
        ledger, created = PointsService.award(
            user=user,
            delta=task.points,
            source='TASK',
            description=f"Completed task: {task.title}",
            created_by_slack_id=approver_slack_id,
            idempotency_key=idempotency_key,
            reference_type='TASK_SUBMISSION',
            reference_id=str(submission.id),
        )
        
        # Update submission
        submission.status = 'approved'
        submission.approved_by_slack_id = approver_slack_id
        submission.approved_at = timezone.now()
        submission.ledger_entry = ledger
        submission.save()
        
        # Update task
        task.status = 'approved'
        task.closed_by_slack_id = approver_slack_id
        task.closed_at = timezone.now()
        task.save()
        
        return submission, ledger


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
