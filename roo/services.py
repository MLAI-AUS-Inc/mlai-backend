"""
Points System Services - Business Logic Layer

This module provides safe, idempotent operations for the points system.
All write operations use database transactions and idempotency keys to prevent
race conditions and duplicate transactions.
"""
import calendar
from collections import OrderedDict
from datetime import date, datetime, timedelta
from typing import Optional, Tuple
from django.conf import settings
from django.db import transaction, IntegrityError, models
from django.db.models import Count
from django.utils import timezone

from .models import (
    PointsAccount, Ledger, Task, TaskAssignment, TaskSubmission, TaskActivity,
    CoworkingBooking, CoworkingDayCapacity,
    RewardsCatalog, RewardRedemption, PointsAdmin, PointsPurchase
)
from .permissions import (
    is_points_admin, require_admin, 
    PermissionDeniedError, InsufficientBalanceError
)
from core.models import User


class PointsPurchaseService:
    """Business rules for Top-up Roo Points purchases."""

    ROO_TOPUP_PACKS = {
        'topup_5': {
            'points': 5,
            'amount_cents': 1999,
            'currency': 'aud',
            'label': '5 Top-up Roo Points',
        },
        'topup_10': {
            'points': 10,
            'amount_cents': 3699,
            'currency': 'aud',
            'label': '10 Top-up Roo Points',
        },
        'topup_25': {
            'points': 25,
            'amount_cents': 6399,
            'currency': 'aud',
            'label': '25 Top-up Roo Points',
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
    @transaction.atomic
    def create_purchase(
        slack_user_id: str,
        pack_id: str,
        *,
        purchase_from: Optional[dict] = None,
        manual_balance_approval: bool = False,
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

        return PointsPurchase.objects.create(
            user=user,
            slack_user_id=cleaned_slack_user_id,
            pack_id=cleaned_pack_id,
            points_amount=pack['points'],
            amount_cents=pack['amount_cents'],
            currency=pack['currency'],
            purchase_from=origin,
        )

    @staticmethod
    def validate_purchase_limits(
        user: Optional[User],
        points_amount: int,
        *,
        now: Optional[datetime] = None,
        manual_balance_approval: bool = False,
    ) -> None:
        """
        Validate conservative purchase limits for the top-up MVP.

        Raises:
            PermissionDeniedError: when the caller is anonymous/guest-like.
            ValueError: when a policy limit is violated.
        """
        if user is None or not getattr(user, "is_authenticated", False):
            raise PermissionDeniedError("A linked user account is required for top-up purchases")

        if not getattr(user, "slack_id", None):
            raise PermissionDeniedError("Guest checkout is not supported for top-up purchases")

        if points_amount <= 0:
            raise ValueError("points_amount must be positive")
        if points_amount > PointsPurchaseService.MAX_POINTS_PER_PURCHASE:
            raise ValueError("Top-up purchase exceeds 25-point per-purchase limit")

        reference_now = now or timezone.now()
        account_age = reference_now - user.date_joined
        if account_age < timedelta(days=PointsPurchaseService.MIN_ACCOUNT_AGE_DAYS):
            raise ValueError("Top-up purchases require an account age of at least 7 days")

        window_start = reference_now - timedelta(days=PointsPurchaseService.LOOKBACK_DAYS)
        rolling_total = (
            PointsPurchase.objects.filter(
                user=user,
                status='paid',
                paid_at__gte=window_start,
            ).aggregate(total=models.Sum('points_amount'))['total'] or 0
        )
        if rolling_total + points_amount > PointsPurchaseService.MAX_POINTS_PER_ROLLING_YEAR:
            raise ValueError("Top-up purchases exceed the 50-point rolling 12-month limit")

        try:
            current_balance = user.points_account.balance
        except PointsAccount.DoesNotExist:
            current_balance = 0

        projected_balance = current_balance + points_amount
        if projected_balance > PointsPurchaseService.MAX_SPENDABLE_BALANCE and not manual_balance_approval:
            raise ValueError("Top-up purchase would exceed the 100-point spendable balance cap")


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
            'earned_balance': account.earned_balance,
            'purchased_topup_balance': account.purchased_topup_balance,
            'lifetime_earned': account.lifetime_earned,
            'lifetime_purchased_topup': account.lifetime_purchased_topup,
            'lifetime_spent': account.lifetime_spent,
            'expired_or_reversed_points': account.expired_or_reversed_points,
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
        account.earned_balance += delta
        account.lifetime_earned += delta
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
            return existing, False

        account.balance += delta
        account.purchased_topup_balance += delta
        account.lifetime_purchased_topup += delta
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
        PointsService._debit_account_balances(account, delta)
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
        account.earned_balance += delta
        # Note: Don't decrease lifetime_spent on refund - it's a historical record
        account.save()
        
        return ledger, True


class CoworkingService:
    """
    Service class for coworking bookings.
    """

    MAX_REPORT_DAYS = 366
    
    @staticmethod
    def get_coworking_cost() -> int:
        """
        Get the cost for a coworking day.
        
        Prioritizes 'COWORKING_DAY' reward from catalog.
        Falls back to settings or default of 4 points.
        """
        try:
            reward = RewardsCatalog.objects.get(code='COWORKING_DAY', is_active=True)
            return reward.cost_points
        except RewardsCatalog.DoesNotExist:
            return getattr(settings, 'COWORKING_DAY_COST_POINTS', 4)
    
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
        
        # Get cost
        cost = CoworkingService.get_coworking_cost()
        
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
        
        return booking, True
    
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
