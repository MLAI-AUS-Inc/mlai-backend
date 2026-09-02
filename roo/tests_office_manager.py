from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import io
import json
import uuid
from threading import Barrier
from unittest import skipUnless
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from django.contrib.admin.sites import AdminSite
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, close_old_connections, connection, connections
from django.db.models.deletion import ProtectedError
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from core.models import User

from .admin import OfficeManagerDayAdmin
from .models import (
    CoworkingBooking,
    CoworkingDayCapacity,
    Ledger,
    OfficeManagerAssignment,
    OfficeManagerClaimAttempt,
    OfficeManagerDay,
    PointsAccount,
)
from .office_manager import (
    COWORKING_SELF_BOOK_REMINDER,
    DELIVERY_LEASE_PREFIX,
    EXPIRED_DELIVERY_ERROR,
    RELINQUISHED_DELIVERY_ERROR,
    NO_FOOD_REMINDER,
    OFFICE_MANAGER_ACTION_ID,
    OFFICE_MANAGER_BOOKING_RESPONSIBILITY,
    OfficeManagerClaimError,
    OfficeManagerService,
    _announcement_text,
    _finish_delivery_failure,
    _slack_client_msg_id,
    run_office_manager_scheduler,
)
from .services import CoworkingService, PointsService


MELBOURNE = ZoneInfo("Australia/Melbourne")


def melbourne_at(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=MELBOURNE)


def office_manager_day(day, *, status_value="open"):
    return OfficeManagerDay.objects.create(
        date=day,
        status=status_value,
        slack_channel_id="CCOWORK",
        claim_cutoff_at=melbourne_at(day.year, day.month, day.day, 10),
        announcement_status="sent",
        slack_message_ts="123.456",
    )


def active_slack_profile(slack_user_id, **_kwargs):
    return {
        "slack_id": slack_user_id,
        "real_name": "MLAI Member",
        "email": f"{slack_user_id.lower()}@example.com",
        "is_bot": False,
        "deleted": False,
        "is_restricted": False,
        "is_ultra_restricted": False,
    }


@override_settings(
    OFFICE_MANAGER_ENABLED=True,
    OFFICE_MANAGER_SLACK_CHANNEL_ID="CCOWORK",
    OFFICE_MANAGER_SLACK_BOT_TOKEN="office-manager-public-roo-test-token",
    OFFICE_MANAGER_TIMEZONE="Australia/Melbourne",
    OFFICE_MANAGER_WEEKDAYS="0,1,2,3,4",
    OFFICE_MANAGER_ANNOUNCEMENT_HOUR=8,
    OFFICE_MANAGER_ANNOUNCEMENT_MINUTE=30,
    OFFICE_MANAGER_CLAIM_CUTOFF_HOUR=10,
    OFFICE_MANAGER_CLAIM_CUTOFF_MINUTE=0,
    OFFICE_MANAGER_END_OF_DAY_REMINDER_HOUR=16,
    OFFICE_MANAGER_END_OF_DAY_REMINDER_MINUTE=30,
)
class OfficeManagerServiceTests(TestCase):
    def setUp(self):
        self.profile_patcher = patch(
            "roo.office_manager.SlackService.get_user_profile",
            side_effect=active_slack_profile,
        )
        self.get_profile = self.profile_patcher.start()
        self.addCleanup(self.profile_patcher.stop)
        self.now = melbourne_at(2026, 8, 3, 8, 45)
        self.day = office_manager_day(self.now.date())
        self.user = User.objects.create_user(
            email="volunteer@example.com",
            slack_id="UVOLUNTEER",
        )

    def test_first_claim_creates_zero_point_booking_without_spend(self):
        result = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )

        self.assertEqual(result.status, "claimed")
        self.assertEqual(result.booking.points_cost, 0)
        self.assertEqual(result.booking.booking_source, "office_manager")
        self.assertIsNone(result.booking.ledger_entry)
        self.assertFalse(result.existing_booking_converted)
        self.assertFalse(
            CoworkingService.monthly_update_discount_applied(result.booking)
        )
        self.assertEqual(
            OfficeManagerAssignment.objects.filter(status="active").count(),
            1,
        )

    def test_claimed_day_and_assignment_provenance_cannot_be_deleted(self):
        result = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )

        with self.assertRaises(ProtectedError):
            self.day.delete()

        self.assertTrue(OfficeManagerDay.objects.filter(pk=self.day.pk).exists())
        self.assertTrue(
            OfficeManagerAssignment.objects.filter(pk=result.assignment.pk).exists()
        )
        self.assertTrue(
            CoworkingBooking.objects.filter(pk=result.booking.pk).exists()
        )
        day_admin = OfficeManagerDayAdmin(OfficeManagerDay, AdminSite())
        self.assertFalse(day_admin.has_delete_permission(None, self.day))
        self.assertIn(
            'slack_channel_id',
            day_admin.get_readonly_fields(None, self.day),
        )

    @override_settings(OFFICE_MANAGER_ENABLED=False)
    def test_disabled_feature_rejects_claim_without_side_effects(self):
        with self.assertRaises(OfficeManagerClaimError) as raised:
            OfficeManagerService.claim(
                slack_user_id=self.user.slack_id,
                booking_date=self.now.date(),
                now=self.now,
            )

        self.assertEqual(raised.exception.code, "feature_disabled")
        self.get_profile.assert_not_called()
        self.assertFalse(CoworkingBooking.objects.exists())
        self.assertFalse(OfficeManagerAssignment.objects.exists())

    def test_same_member_claim_is_idempotent(self):
        first = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )
        second = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )

        self.assertEqual(second.status, "already_claimed_by_you")
        self.assertEqual(first.assignment.id, second.assignment.id)
        self.assertEqual(CoworkingBooking.objects.count(), 1)

    def test_exact_attempt_replays_original_result_after_midnight(self):
        attempt_id = uuid.uuid4()
        first = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            attempt_id=attempt_id,
            now=self.now,
        )

        replay = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            attempt_id=attempt_id,
            now=melbourne_at(2026, 8, 4, 0, 1),
        )

        self.assertEqual(first.status, "claimed")
        self.assertEqual(replay.status, "claimed")
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.assignment.id, first.assignment.id)
        self.assertEqual(
            OfficeManagerClaimAttempt.objects.filter(pk=attempt_id).count(),
            1,
        )

    @patch("roo.office_manager._local_now")
    def test_cancel_supersedes_old_attempt_and_new_attempt_can_claim(
        self,
        mocked_now,
    ):
        mocked_now.return_value = self.now
        first_attempt_id = uuid.uuid4()
        first = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            attempt_id=first_attempt_id,
            now=self.now,
        )
        CoworkingService.cancel(str(first.booking.id), self.user.slack_id)

        with self.assertRaises(OfficeManagerClaimError) as replayed:
            OfficeManagerService.claim(
                slack_user_id=self.user.slack_id,
                booking_date=self.now.date(),
                attempt_id=first_attempt_id,
                now=self.now,
            )
        self.assertEqual(replayed.exception.code, "attempt_superseded")

        replacement = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            attempt_id=uuid.uuid4(),
            now=self.now,
        )
        self.assertEqual(replacement.status, "claimed")
        self.assertNotEqual(replacement.assignment.id, first.assignment.id)
        self.assertEqual(
            OfficeManagerAssignment.objects.filter(status="active").count(),
            1,
        )

    def test_attempt_id_cannot_be_rebound_to_another_actor(self):
        attempt_id = uuid.uuid4()
        OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            attempt_id=attempt_id,
            now=self.now,
        )
        other = User.objects.create_user(
            email="attempt-other@example.com",
            slack_id="UATTEMPTOTHER",
        )

        with self.assertRaises(OfficeManagerClaimError) as raised:
            OfficeManagerService.claim(
                slack_user_id=other.slack_id,
                booking_date=self.now.date(),
                attempt_id=attempt_id,
                now=self.now,
            )

        self.assertEqual(raised.exception.code, "attempt_payload_conflict")
        self.assertFalse(CoworkingBooking.objects.filter(user=other).exists())

    def test_committed_claim_is_recovered_after_midnight(self):
        first = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )

        replay = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=melbourne_at(2026, 8, 4, 0, 1),
        )

        self.assertEqual(replay.status, "already_claimed_by_you")
        self.assertEqual(replay.assignment.id, first.assignment.id)
        self.assertEqual(CoworkingBooking.objects.count(), 1)
        self.assertEqual(OfficeManagerAssignment.objects.count(), 1)

    def test_uncommitted_stale_claim_remains_closed(self):
        with self.assertRaises(OfficeManagerClaimError) as raised:
            OfficeManagerService.claim(
                slack_user_id=self.user.slack_id,
                booking_date=self.now.date(),
                now=melbourne_at(2026, 8, 4, 0, 1),
            )

        self.assertEqual(raised.exception.code, "claim_closed")
        self.assertFalse(OfficeManagerAssignment.objects.exists())

    def test_committed_claim_is_recovered_after_feature_is_disabled(self):
        first = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )

        with override_settings(OFFICE_MANAGER_ENABLED=False):
            replay = OfficeManagerService.claim(
                slack_user_id=self.user.slack_id,
                booking_date=self.now.date(),
                now=self.now,
            )

        self.assertEqual(replay.status, "already_claimed_by_you")
        self.assertEqual(replay.assignment.id, first.assignment.id)

    def test_committed_claim_replay_skips_member_profile_lookup(self):
        first = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )
        self.get_profile.reset_mock()
        self.get_profile.side_effect = RuntimeError("Slack unavailable")

        replay = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )

        self.assertEqual(replay.status, "already_claimed_by_you")
        self.assertEqual(replay.assignment.id, first.assignment.id)
        self.get_profile.assert_not_called()

    def test_second_member_cannot_claim(self):
        other = User.objects.create_user(
            email="other@example.com",
            slack_id="UOTHER123",
        )
        OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )

        with self.assertRaises(OfficeManagerClaimError) as raised:
            OfficeManagerService.claim(
                slack_user_id=other.slack_id,
                booking_date=self.now.date(),
                now=self.now,
            )

        self.assertEqual(raised.exception.code, "already_claimed")
        self.assertEqual(
            raised.exception.assignee_slack_user_id,
            self.user.slack_id,
        )
        self.assertFalse(
            CoworkingBooking.objects.filter(user=other).exists()
        )

    def test_existing_paid_booking_is_refunded_once_and_converted(self):
        PointsService.award(
            user=self.user,
            delta=12,
            source="MANUAL",
            description="Setup",
            created_by_slack_id="UADMIN",
            idempotency_key="office-manager-refund-setup",
        )
        charged = CoworkingService.get_standard_coworking_cost()
        spend_ledger, _ = PointsService.spend(
            user=self.user,
            delta=charged,
            source="COWORKING",
            description="Existing coworking booking",
            created_by_slack_id=self.user.slack_id,
            idempotency_key="existing-office-manager-booking",
            reference_type="COWORKING_BOOKING",
            reference_id=self.now.date().isoformat(),
        )
        booking = CoworkingBooking.objects.create(
            user=self.user,
            date=self.now.date(),
            status="booked",
            points_cost=charged,
            booking_source="points",
            purchased_points_cost_microroo=0,
            ledger_entry=spend_ledger,
        )
        balance_after_booking = PointsAccount.objects.get(user=self.user).balance

        first = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )
        second = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )

        booking.refresh_from_db()
        account = PointsAccount.objects.get(user=self.user)
        self.assertEqual(first.assignment.points_refunded, charged)
        self.assertEqual(second.assignment.points_refunded, charged)
        self.assertEqual(account.balance, balance_after_booking + charged)
        self.assertEqual(booking.points_cost, 0)
        self.assertEqual(booking.original_points_cost, charged)
        self.assertEqual(booking.booking_source, "office_manager")
        self.assertEqual(
            account.user.points_ledger.filter(
                idempotency_key=f"office_manager_refund:{self.day.id}:{booking.id}"
            ).count(),
            1,
        )

    def test_conversion_and_reversal_preserve_original_point_buckets(self):
        PointsService.credit_purchased_topup(
            user=self.user,
            delta=3,
            description="Purchased setup",
            idempotency_key="office-manager-purchased-setup",
        )
        PointsService.award(
            user=self.user,
            delta=5,
            source="MANUAL",
            description="Earned setup",
            created_by_slack_id="UADMIN",
            idempotency_key="office-manager-earned-setup",
        )
        with patch("roo.services.timezone.now", return_value=self.now):
            booking, _ = CoworkingService.book(
                user=self.user,
                booking_date=self.now.date(),
                created_by_slack_id=self.user.slack_id,
            )
        self.assertEqual(booking.purchased_points_cost_microroo, 3_000_000)

        attempt_id = uuid.uuid4()
        claimed = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            attempt_id=attempt_id,
            now=self.now,
        )
        account = PointsAccount.objects.get(user=self.user)
        self.assertEqual(account.purchased_topup_balance_microroo, 3_000_000)
        self.assertEqual(account.earned_balance_microroo, 5_000_000)
        self.assertEqual(
            claimed.assignment.purchased_points_refunded_microroo,
            3_000_000,
        )

        with (
            patch("roo.services.timezone.now", return_value=self.now),
            patch("roo.office_manager._local_now", return_value=self.now),
        ):
            CoworkingService.cancel(str(booking.id), self.user.slack_id)
        account.refresh_from_db()
        self.assertEqual(account.purchased_topup_balance_microroo, 0)
        self.assertEqual(account.earned_balance_microroo, 0)

    def test_conversion_refuses_refund_without_authoritative_debit(self):
        CoworkingBooking.objects.create(
            user=self.user,
            date=self.now.date(),
            status="booked",
            points_cost=8,
            booking_source="points",
        )

        with self.assertRaises(OfficeManagerClaimError) as raised:
            OfficeManagerService.claim(
                slack_user_id=self.user.slack_id,
                booking_date=self.now.date(),
                attempt_id=uuid.uuid4(),
                now=self.now,
            )

        self.assertEqual(raised.exception.code, "refund_unavailable")
        self.assertFalse(OfficeManagerAssignment.objects.exists())
        self.assertFalse(Ledger.objects.filter(kind="REFUND").exists())

    def test_existing_four_point_booking_refunds_four(self):
        PointsService.award(
            user=self.user,
            delta=4,
            source="MANUAL",
            description="Create account",
            created_by_slack_id="UADMIN",
            idempotency_key="four-point-refund-setup",
        )
        spend_ledger, _ = PointsService.spend(
            user=self.user,
            delta=4,
            source="COWORKING",
            description="Discounted coworking booking",
            created_by_slack_id=self.user.slack_id,
            idempotency_key="four-point-booking-spend",
            reference_type="COWORKING_BOOKING",
            reference_id=self.now.date().isoformat(),
        )
        booking = CoworkingBooking.objects.create(
            user=self.user,
            date=self.now.date(),
            status="booked",
            points_cost=4,
            booking_source="points",
            purchased_points_cost_microroo=0,
            ledger_entry=spend_ledger,
        )
        starting_balance = PointsAccount.objects.get(user=self.user).balance

        result = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )

        booking.refresh_from_db()
        self.assertEqual(result.assignment.points_refunded, 4)
        self.assertEqual(
            PointsAccount.objects.get(user=self.user).balance,
            starting_balance + 4,
        )
        self.assertEqual(booking.original_points_cost, 4)

    def test_relinquishing_converted_booking_reverses_refund_and_rebooking_charges(self):
        PointsService.award(
            user=self.user,
            delta=24,
            source="MANUAL",
            description="Setup",
            created_by_slack_id="UADMIN",
            idempotency_key="office-manager-rebook-setup",
        )
        with patch("roo.services.timezone.now", return_value=self.now):
            original_booking, _ = CoworkingService.book(
                user=self.user,
                booking_date=self.now.date(),
                created_by_slack_id=self.user.slack_id,
            )
            original_spend_id = original_booking.ledger_entry_id
            balance_after_original_booking = PointsAccount.objects.get(
                user=self.user
            ).balance

            claim = OfficeManagerService.claim(
                slack_user_id=self.user.slack_id,
                booking_date=self.now.date(),
                now=self.now,
            )
            original_booking.refresh_from_db()
            self.assertEqual(
                PointsAccount.objects.get(user=self.user).balance,
                balance_after_original_booking + original_booking.original_points_cost,
            )

            cancelled, refunded = CoworkingService.cancel(
                str(original_booking.id),
                self.user.slack_id,
            )

            cancelled.refresh_from_db()
            claim.assignment.refresh_from_db()
            self.assertFalse(refunded)
            self.assertEqual(cancelled.status, "cancelled")
            self.assertEqual(cancelled.booking_source, "points")
            self.assertEqual(cancelled.points_cost, cancelled.original_points_cost)
            self.assertIsNotNone(
                claim.assignment.refund_reversal_ledger_entry_id
            )
            self.assertEqual(
                claim.assignment.refund_reversal_ledger_entry.delta,
                -cancelled.original_points_cost,
            )
            self.assertEqual(
                PointsAccount.objects.get(user=self.user).balance,
                balance_after_original_booking,
            )

            replacement, created = CoworkingService.book(
                user=self.user,
                booking_date=self.now.date(),
                created_by_slack_id=self.user.slack_id,
            )

        self.assertTrue(created)
        self.assertNotEqual(replacement.id, original_booking.id)
        self.assertNotEqual(replacement.ledger_entry_id, original_spend_id)
        self.assertEqual(
            PointsAccount.objects.get(user=self.user).balance,
            balance_after_original_booking - replacement.points_cost,
        )

    def test_relinquish_rolls_back_when_refunded_points_were_spent(self):
        PointsService.award(
            user=self.user,
            delta=8,
            source="MANUAL",
            description="Setup",
            created_by_slack_id="UADMIN",
            idempotency_key="office-manager-spent-refund-setup",
        )
        with patch("roo.services.timezone.now", return_value=self.now):
            booking, _ = CoworkingService.book(
                user=self.user,
                booking_date=self.now.date(),
                created_by_slack_id=self.user.slack_id,
            )
            claim = OfficeManagerService.claim(
                slack_user_id=self.user.slack_id,
                booking_date=self.now.date(),
                now=self.now,
            )
            PointsService.spend(
                user=self.user,
                delta=claim.assignment.points_refunded,
                source="MERCH",
                description="Spend returned points",
                created_by_slack_id=self.user.slack_id,
                idempotency_key="spend-office-manager-refund",
            )

            with self.assertRaisesMessage(
                ValueError,
                "previously refunded Roo points are no longer available",
            ):
                CoworkingService.cancel(str(booking.id), self.user.slack_id)

        booking.refresh_from_db()
        claim.assignment.refresh_from_db()
        self.day.refresh_from_db()
        self.assertEqual(booking.status, "booked")
        self.assertEqual(booking.booking_source, "office_manager")
        self.assertEqual(claim.assignment.status, "active")
        self.assertEqual(self.day.status, "claimed")
        self.assertFalse(
            Ledger.objects.filter(
                idempotency_key=(
                    f"office_manager_refund_reversal:{claim.assignment.id}"
                )
            ).exists()
        )

    def test_relinquishing_four_point_booking_reverses_four_points(self):
        PointsService.award(
            user=self.user,
            delta=4,
            source="MANUAL",
            description="Create account",
            created_by_slack_id="UADMIN",
            idempotency_key="four-point-reversal-setup",
        )
        spend_ledger, _ = PointsService.spend(
            user=self.user,
            delta=4,
            source="COWORKING",
            description="Discounted coworking booking",
            created_by_slack_id=self.user.slack_id,
            idempotency_key="four-point-reversal-booking-spend",
            reference_type="COWORKING_BOOKING",
            reference_id=self.now.date().isoformat(),
        )
        booking = CoworkingBooking.objects.create(
            user=self.user,
            date=self.now.date(),
            status="booked",
            points_cost=4,
            booking_source="points",
            purchased_points_cost_microroo=0,
            ledger_entry=spend_ledger,
        )
        claim = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )

        with patch("roo.services.timezone.now", return_value=self.now):
            cancelled, refunded = CoworkingService.cancel(
                str(booking.id),
                self.user.slack_id,
            )

        claim.assignment.refresh_from_db()
        self.assertFalse(refunded)
        self.assertEqual(cancelled.points_cost, 4)
        self.assertEqual(cancelled.booking_source, "points")
        self.assertEqual(
            claim.assignment.refund_reversal_ledger_entry.delta,
            -4,
        )
        self.assertEqual(
            PointsAccount.objects.get(user=self.user).balance,
            0,
        )

    def test_claim_allows_one_office_manager_overflow(self):
        CoworkingDayCapacity.objects.create(date=self.now.date(), capacity=1)
        other = User.objects.create_user(
            email="booked@example.com",
            slack_id="UBOOKED123",
        )
        CoworkingBooking.objects.create(
            user=other,
            date=self.now.date(),
            status="booked",
            points_cost=8,
        )

        OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )

        self.assertEqual(
            CoworkingBooking.objects.filter(
                date=self.now.date(),
                status="booked",
            ).count(),
            2,
        )
        available, capacity = CoworkingService.check_availability(self.now.date())
        self.assertEqual((available, capacity), (0, 1))

    def test_office_manager_does_not_consume_a_remaining_paid_slot(self):
        CoworkingDayCapacity.objects.create(date=self.now.date(), capacity=2)
        other = User.objects.create_user(
            email="one-paid-member@example.com",
            slack_id="UPAID1234",
        )
        CoworkingBooking.objects.create(
            user=other,
            date=self.now.date(),
            status="booked",
            points_cost=8,
        )

        OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )

        self.assertEqual(
            CoworkingService.check_availability(self.now.date()),
            (1, 2),
        )

    @patch("roo.office_manager._local_now")
    def test_cancelling_before_cutoff_reopens_the_role(self, mocked_now):
        mocked_now.return_value = melbourne_at(2026, 8, 3, 9, 15)
        result = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )

        booking, refunded = CoworkingService.cancel(
            str(result.booking.id),
            self.user.slack_id,
        )

        self.assertFalse(refunded)
        self.assertTrue(booking._office_manager_day_reopened)
        self.day.refresh_from_db()
        self.assertEqual(self.day.status, "open")
        self.assertFalse(
            OfficeManagerAssignment.objects.filter(status="active").exists()
        )

    @patch("roo.office_manager._local_now")
    def test_relinquished_winner_announcement_is_retracted(self, mocked_now):
        mocked_now.return_value = melbourne_at(2026, 8, 3, 9, 15)
        result = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )
        OfficeManagerAssignment.objects.filter(pk=result.assignment.id).update(
            winner_channel_announcement_status="sent",
            winner_channel_message_ts="789.012",
        )

        booking, _ = CoworkingService.cancel(
            str(result.booking.id),
            self.user.slack_id,
        )
        result.assignment.refresh_from_db()
        self.assertTrue(result.assignment.winner_channel_retraction_pending)
        self.assertEqual(
            booking._office_manager_assignment_id,
            result.assignment.id,
        )

        fake_client = Mock()
        fake_client.chat_update.return_value = {"ok": True}
        with patch(
            "roo.office_manager.SlackService.get_client",
            return_value=fake_client,
        ):
            retracted = OfficeManagerService.retract_winner_channel_announcement(
                result.assignment.id
            )

        self.assertTrue(retracted)
        fake_client.chat_update.assert_called_once()
        update_payload = fake_client.chat_update.call_args.kwargs
        self.assertEqual(update_payload["channel"], "CCOWORK")
        self.assertEqual(update_payload["ts"], "789.012")
        self.assertIn("is no longer today's", update_payload["text"])
        self.assertIn("current assignment", update_payload["text"])
        result.assignment.refresh_from_db()
        self.assertFalse(result.assignment.winner_channel_retraction_pending)

    @patch("roo.office_manager._local_now")
    def test_retraction_recovers_message_ts_after_post_crash(self, mocked_now):
        mocked_now.return_value = melbourne_at(2026, 8, 3, 9, 15)
        result = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )
        OfficeManagerAssignment.objects.filter(pk=result.assignment.id).update(
            winner_channel_announcement_status="sending",
            winner_channel_message_ts="",
        )
        CoworkingService.cancel(str(result.booking.id), self.user.slack_id)
        fake_client = Mock()
        with patch(
            "roo.office_manager.SlackService.get_client",
            return_value=fake_client,
        ):
            retracted = OfficeManagerService.retract_winner_channel_announcement(
                result.assignment.id
            )

        self.assertFalse(retracted)
        fake_client.chat_postMessage.assert_not_called()
        fake_client.chat_update.assert_not_called()
        result.assignment.refresh_from_db()
        self.assertEqual(result.assignment.winner_channel_message_ts, "")
        self.assertFalse(result.assignment.winner_channel_retraction_pending)
        self.assertEqual(
            result.assignment.winner_channel_retraction_status,
            "exhausted",
        )
        self.assertEqual(
            result.assignment.winner_channel_retraction_last_error,
            "message_coordinates_unavailable",
        )

    @patch("roo.office_manager._local_now")
    def test_cancelling_at_cutoff_does_not_reopen_the_role(self, mocked_now):
        mocked_now.side_effect = [
            self.now,
            melbourne_at(2026, 8, 3, 10),
        ]
        result = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )

        booking, refunded = CoworkingService.cancel(
            str(result.booking.id),
            self.user.slack_id,
        )

        self.assertFalse(refunded)
        self.assertFalse(booking._office_manager_day_reopened)
        self.day.refresh_from_db()
        self.assertEqual(self.day.status, "closed")
        self.assertIsNotNone(self.day.closed_at)

    def test_winner_dm_is_delivered_once(self):
        result = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )
        fake_client = Mock()
        fake_client.conversations_open.return_value = {
            "ok": True,
            "channel": {"id": "DWINNER"},
        }
        fake_client.chat_postMessage.return_value = {"ok": True, "ts": "456.789"}

        with patch(
            "roo.office_manager.SlackService.get_client",
            return_value=fake_client,
        ):
            first = OfficeManagerService.deliver_winner_dm(result.assignment.id)
            second = OfficeManagerService.deliver_winner_dm(result.assignment.id)

        self.assertTrue(first)
        self.assertTrue(second)
        fake_client.chat_postMessage.assert_called_once()
        self.assertIn(
            NO_FOOD_REMINDER,
            fake_client.chat_postMessage.call_args.kwargs["text"],
        )
        self.assertIn(
            OFFICE_MANAGER_BOOKING_RESPONSIBILITY,
            fake_client.chat_postMessage.call_args.kwargs["text"],
        )
        self.assertEqual(
            fake_client.chat_postMessage.call_args.kwargs["client_msg_id"],
            _slack_client_msg_id("winner-dm", result.assignment.id),
        )
        result.assignment.refresh_from_db()
        self.assertEqual(result.assignment.winner_dm_status, "sent")

    def test_winner_channel_announcement_tags_member_and_is_delivered_once(self):
        result = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )
        fake_client = Mock()
        fake_client.chat_postMessage.return_value = {"ok": True, "ts": "789.012"}

        with patch(
            "roo.office_manager.SlackService.get_client",
            return_value=fake_client,
        ):
            first = OfficeManagerService.deliver_winner_channel_announcement(
                result.assignment.id
            )
            second = OfficeManagerService.deliver_winner_channel_announcement(
                result.assignment.id
            )

        self.assertTrue(first)
        self.assertTrue(second)
        fake_client.chat_postMessage.assert_called_once()
        payload = fake_client.chat_postMessage.call_args.kwargs
        self.assertEqual(payload["channel"], self.day.slack_channel_id)
        self.assertEqual(
            payload["client_msg_id"],
            _slack_client_msg_id("winner", result.attempt_id),
        )
        self.assertIn(f"<@{self.user.slack_id}>", payload["text"])
        self.assertIn("Office Manager of the Day", payload["text"])
        self.assertIn("without deducting Roo points", payload["text"])
        self.assertIn(COWORKING_SELF_BOOK_REMINDER, payload["text"])
        self.assertIn(NO_FOOD_REMINDER, payload["text"])
        result.assignment.refresh_from_db()
        self.assertEqual(
            result.assignment.winner_channel_announcement_status,
            "sent",
        )
        self.assertEqual(result.assignment.winner_channel_message_ts, "789.012")

    def test_claimed_announcement_includes_booking_reminder_without_action(self):
        result = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )

        with patch(
            "roo.office_manager.SlackService.update_message",
            return_value=True,
        ) as update_message:
            updated = OfficeManagerService.reconcile_message(
                result.assignment.day_id
            )

        self.assertTrue(updated)
        update_message.assert_called_once()
        fallback_text = update_message.call_args.args[2]
        blocks_text = json_text(update_message.call_args.kwargs["blocks"])
        self.assertIn(COWORKING_SELF_BOOK_REMINDER, fallback_text)
        self.assertIn(COWORKING_SELF_BOOK_REMINDER, blocks_text)
        self.assertNotIn(OFFICE_MANAGER_ACTION_ID, blocks_text)

    def test_stale_winner_dm_delivery_lease_is_retried(self):
        result = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )
        OfficeManagerAssignment.objects.filter(pk=result.assignment.id).update(
            winner_dm_status="sending",
        )
        fake_client = Mock()
        fake_client.conversations_open.return_value = {
            "ok": True,
            "channel": {"id": "DWINNER"},
        }
        fake_client.chat_postMessage.return_value = {"ok": True, "ts": "456.789"}

        with patch(
            "roo.office_manager.SlackService.get_client",
            return_value=fake_client,
        ):
            current_lease = OfficeManagerService.deliver_winner_dm(
                result.assignment.id
            )
            OfficeManagerAssignment.objects.filter(
                pk=result.assignment.id
            ).update(
                updated_at=timezone.now() - timedelta(minutes=6),
            )
            stale_lease = OfficeManagerService.deliver_winner_dm(
                result.assignment.id
            )

        self.assertFalse(current_lease)
        self.assertTrue(stale_lease)
        fake_client.chat_postMessage.assert_called_once()

    def test_winner_dm_lease_age_is_independent_of_shared_updated_at(self):
        result = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )
        acquired_at = timezone.now() - timedelta(minutes=6)
        lease_token = (
            f"{DELIVERY_LEASE_PREFIX}expired-owner:"
            f"{acquired_at.timestamp():.6f}"
        )
        OfficeManagerAssignment.objects.filter(pk=result.assignment.id).update(
            winner_dm_status="sending",
            winner_dm_last_error=lease_token,
            # Simulate an unrelated delivery updating the shared model row.
            updated_at=timezone.now(),
        )
        fake_client = Mock()
        fake_client.conversations_open.return_value = {
            "ok": True,
            "channel": {"id": "DWINNER"},
        }
        fake_client.chat_postMessage.return_value = {
            "ok": True,
            "ts": "456.789",
        }

        with patch(
            "roo.office_manager.SlackService.get_client",
            return_value=fake_client,
        ):
            delivered = OfficeManagerService.deliver_winner_dm(
                result.assignment.id
            )

        self.assertTrue(delivered)
        fake_client.chat_postMessage.assert_called_once()

    def test_unknown_winner_dm_is_retried_with_same_message_identity(self):
        result = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )
        fake_client = Mock()
        fake_client.conversations_open.return_value = {
            "ok": True,
            "channel": {"id": "DWINNER"},
        }
        fake_client.chat_postMessage.side_effect = [
            RuntimeError("accepted response lost"),
            {"ok": True, "ts": "456.789"},
        ]

        with patch(
            "roo.office_manager.SlackService.get_client",
            return_value=fake_client,
        ):
            first = OfficeManagerService.deliver_winner_dm(
                result.assignment.id
            )
            result.assignment.refresh_from_db()
            self.assertEqual(result.assignment.winner_dm_status, "unknown")
            OfficeManagerAssignment.objects.filter(
                pk=result.assignment.id
            ).update(winner_dm_next_attempt_at=timezone.now() - timedelta(seconds=1))
            second = OfficeManagerService.deliver_winner_dm(
                result.assignment.id
            )

        self.assertFalse(first)
        self.assertTrue(second)
        client_message_ids = {
            call.kwargs["client_msg_id"]
            for call in fake_client.chat_postMessage.call_args_list
        }
        self.assertEqual(
            client_message_ids,
            {_slack_client_msg_id("winner-dm", result.assignment.id)},
        )
        result.assignment.refresh_from_db()
        self.assertEqual(result.assignment.winner_dm_status, "sent")

    def test_replaced_worker_cannot_fail_or_emit_for_live_delivery(self):
        result = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )
        replacement_token = (
            f"{DELIVERY_LEASE_PREFIX}replacement:"
            f"{timezone.now().timestamp():.6f}"
        )
        OfficeManagerAssignment.objects.filter(pk=result.assignment.id).update(
            winner_dm_status="sending",
            winner_dm_last_error=replacement_token,
        )

        for uncertain in (False, True):
            self.assertFalse(
                _finish_delivery_failure(
                    OfficeManagerAssignment,
                    result.assignment.id,
                    status_field="winner_dm_status",
                    error_field="winner_dm_last_error",
                    attempt_count_field="winner_dm_attempt_count",
                    next_attempt_field="winner_dm_next_attempt_at",
                    lease_token="stale-worker-token",
                    exc=RuntimeError("stale result"),
                    uncertain=uncertain,
                )
            )

        fake_client = Mock()
        with patch(
            "roo.office_manager.SlackService.get_client",
            return_value=fake_client,
        ) as get_client:
            self.assertFalse(
                OfficeManagerService.deliver_winner_dm(result.assignment.id)
            )

        get_client.assert_not_called()
        fake_client.chat_postMessage.assert_not_called()
        result.assignment.refresh_from_db()
        self.assertEqual(result.assignment.winner_dm_status, "sending")
        self.assertEqual(
            result.assignment.winner_dm_last_error,
            replacement_token,
        )

    @patch("roo.office_manager.SlackService.get_user_profile")
    def test_new_slack_identity_links_existing_member_by_profile_email(self, get_profile):
        get_profile.return_value = {
            "real_name": "Slack Member",
            "email": "founder@example.com",
            "is_bot": False,
            "deleted": False,
        }
        founder = User.objects.create_user(email="founder@example.com")
        PointsService.award(
            user=founder,
            delta=8,
            source="MANUAL",
            description="Existing founder booking setup",
            created_by_slack_id="UADMIN",
            idempotency_key="founder-booking-setup",
        )
        spend_ledger, _ = PointsService.spend(
            user=founder,
            delta=8,
            source="COWORKING",
            description="Existing founder booking",
            created_by_slack_id="UNEWMEMBER",
            idempotency_key="founder-booking-spend",
            reference_type="COWORKING_BOOKING",
            reference_id=self.now.date().isoformat(),
        )
        booking = CoworkingBooking.objects.create(
            user=founder,
            date=self.now.date(),
            status="booked",
            points_cost=8,
            booking_source="points",
            purchased_points_cost_microroo=0,
            ledger_entry=spend_ledger,
        )

        result = OfficeManagerService.claim(
            slack_user_id="UNEWMEMBER",
            booking_date=self.now.date(),
            now=self.now,
        )

        self.assertEqual(result.booking.id, booking.id)
        self.assertEqual(result.booking.user_id, founder.id)
        self.assertEqual(result.assignment.points_refunded, 8)
        founder.refresh_from_db()
        self.assertEqual(founder.slack_id, "UNEWMEMBER")
        self.assertEqual(User.objects.filter(email="founder@example.com").count(), 1)

    @patch("roo.office_manager.SlackService.get_user_profile")
    def test_conflicting_profile_email_is_rejected_without_duplicate(self, get_profile):
        get_profile.return_value = {
            "real_name": "Different Slack Member",
            "email": "already-linked@example.com",
            "is_bot": False,
            "deleted": False,
        }
        existing = User.objects.create_user(
            email="already-linked@example.com",
            slack_id="UOTHERLINK",
        )

        with self.assertRaises(OfficeManagerClaimError) as raised:
            OfficeManagerService.claim(
                slack_user_id="UNEWLINK99",
                booking_date=self.now.date(),
                now=self.now,
            )

        self.assertEqual(raised.exception.code, "member_not_eligible")
        self.assertEqual(User.objects.count(), 2)
        existing.refresh_from_db()
        self.assertEqual(existing.slack_id, "UOTHERLINK")

    @patch("roo.office_manager.User.objects.create_user")
    @patch("roo.office_manager.SlackService.get_user_profile")
    def test_identity_create_collision_returns_structured_error(
        self,
        get_profile,
        create_user,
    ):
        get_profile.return_value = {
            "real_name": "Racing Member",
            "email": "race-create@example.com",
            "is_bot": False,
            "deleted": False,
        }
        create_user.side_effect = IntegrityError("simulated identity race")

        with self.assertRaises(OfficeManagerClaimError) as raised:
            OfficeManagerService.resolve_member("URACECREATE")

        self.assertEqual(raised.exception.code, "member_not_eligible")

    @patch(
        "roo.office_manager.SlackService.get_user_profile",
        return_value={
            "slack_id": "UGUEST",
            "real_name": "Workspace Guest",
            "is_restricted": True,
        },
    )
    def test_workspace_guest_cannot_volunteer(self, _get_profile):
        with self.assertRaises(OfficeManagerClaimError) as raised:
            OfficeManagerService.claim(
                slack_user_id="UGUEST",
                booking_date=self.now.date(),
                now=self.now,
            )

        self.assertEqual(raised.exception.code, "member_not_eligible")
        self.assertFalse(User.objects.filter(slack_id="UGUEST").exists())

    @patch(
        "roo.office_manager.SlackService.get_user_profile",
        return_value={
            "slack_id": "ULINKEDGUEST",
            "real_name": "Linked Workspace Guest",
            "is_restricted": True,
        },
    )
    def test_prelinked_workspace_guest_is_reverified_and_rejected(self, get_profile):
        user = User.objects.create_user(
            email="linked-guest@example.com",
            slack_id="ULINKEDGUEST",
        )

        with self.assertRaises(OfficeManagerClaimError) as raised:
            OfficeManagerService.claim(
                slack_user_id=user.slack_id,
                booking_date=self.now.date(),
                now=self.now,
            )

        self.assertEqual(raised.exception.code, "member_not_eligible")
        get_profile.assert_called_once()
        self.assertFalse(CoworkingBooking.objects.filter(user=user).exists())

    def test_relinquished_assignment_does_not_receive_winner_dm(self):
        result = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )
        OfficeManagerAssignment.objects.filter(pk=result.assignment.id).update(
            status="relinquished"
        )
        fake_client = Mock()

        with patch(
            "roo.office_manager.SlackService.get_client",
            return_value=fake_client,
        ) as get_client:
            delivered = OfficeManagerService.deliver_winner_dm(
                result.assignment.id
            )

        self.assertFalse(delivered)
        get_client.assert_not_called()
        fake_client.chat_postMessage.assert_not_called()

    def test_winner_dm_rechecks_assignment_after_opening_dm(self):
        result = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )
        fake_client = Mock()

        def relinquish_before_send(**_kwargs):
            OfficeManagerAssignment.objects.filter(pk=result.assignment.id).update(
                status="relinquished"
            )
            return {"ok": True, "channel": {"id": "DWINNER"}}

        fake_client.conversations_open.side_effect = relinquish_before_send
        with patch(
            "roo.office_manager.SlackService.get_client",
            return_value=fake_client,
        ):
            delivered = OfficeManagerService.deliver_winner_dm(
                result.assignment.id
            )

        self.assertFalse(delivered)
        fake_client.chat_postMessage.assert_not_called()
        result.assignment.refresh_from_db()
        self.assertEqual(result.assignment.winner_dm_status, "failed")

    def test_reconcile_preserves_retry_when_state_changes_during_slack_update(self):
        result = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )

        def reopen_during_update(*_args, **_kwargs):
            OfficeManagerDay.objects.filter(pk=result.assignment.day_id).update(
                status="open",
                message_update_pending=True,
            )
            return True

        with patch(
            "roo.office_manager.SlackService.update_message",
            side_effect=reopen_during_update,
        ):
            updated = OfficeManagerService.reconcile_message(
                result.assignment.day_id
            )

        self.assertTrue(updated)
        result.assignment.day.refresh_from_db()
        self.assertEqual(result.assignment.day.status, "open")
        self.assertTrue(result.assignment.day.message_update_pending)


@override_settings(
    OFFICE_MANAGER_ENABLED=True,
    OFFICE_MANAGER_SLACK_CHANNEL_ID="CCOWORK",
    OFFICE_MANAGER_SLACK_BOT_TOKEN="office-manager-public-roo-test-token",
    OFFICE_MANAGER_TIMEZONE="Australia/Melbourne",
)
class OfficeManagerSchedulerTests(TestCase):
    def setUp(self):
        self.profile_patcher = patch(
            "roo.office_manager.SlackService.get_user_profile",
            side_effect=active_slack_profile,
        )
        self.profile_patcher.start()
        self.addCleanup(self.profile_patcher.stop)

    def test_scheduler_posts_once_with_button_and_no_reply_copy(self):
        now = melbourne_at(2026, 8, 3, 8, 30)
        fake_client = Mock()
        fake_client.chat_postMessage.return_value = {
            "ok": True,
            "ts": "123.456",
        }

        with patch(
            "roo.office_manager.SlackService.get_client",
            return_value=fake_client,
        ) as get_client:
            first = run_office_manager_scheduler(now=now)
            second = run_office_manager_scheduler(now=now)

        self.assertTrue(first["announcement_sent"])
        self.assertNotIn("announcement_sent", second)
        self.assertEqual(fake_client.chat_postMessage.call_count, 1)
        payload = fake_client.chat_postMessage.call_args.kwargs
        day = OfficeManagerDay.objects.get(date=now.date())
        self.assertEqual(
            payload["client_msg_id"],
            _slack_client_msg_id("daily", day.id),
        )
        blocks_text = json_text(payload["blocks"])
        self.assertIn("Volunteer for today", blocks_text)
        self.assertIn("No channel or thread reply is needed", blocks_text)
        self.assertIn(COWORKING_SELF_BOOK_REMINDER, payload["text"])
        self.assertIn(COWORKING_SELF_BOOK_REMINDER, blocks_text)
        self.assertIn(NO_FOOD_REMINDER, blocks_text)
        get_client.assert_called_once_with(
            bot_token="office-manager-public-roo-test-token"
        )
        action_block = next(
            block for block in payload["blocks"] if block["type"] == "actions"
        )
        volunteer_button = action_block["elements"][0]
        self.assertEqual(
            volunteer_button["action_id"],
            OFFICE_MANAGER_ACTION_ID,
        )
        self.assertEqual(
            json.loads(volunteer_button["value"]),
            {"date": "2026-08-03"},
        )

    @override_settings(OFFICE_MANAGER_SLACK_BOT_TOKEN="")
    def test_scheduler_fails_closed_without_public_roo_token(self):
        result = run_office_manager_scheduler(now=melbourne_at(2026, 8, 3, 8, 30))

        self.assertEqual(
            result,
            {
                "status": "failed",
                "reason": "slack_bot_token_not_configured",
            },
        )
        self.assertFalse(OfficeManagerDay.objects.exists())

    def test_scheduler_recovers_stale_sending_announcement(self):
        now = melbourne_at(2026, 8, 3, 8, 30)
        day = office_manager_day(now.date())
        OfficeManagerDay.objects.filter(pk=day.pk).update(
            announcement_status="sending",
            slack_message_ts="",
            updated_at=timezone.now() - timedelta(minutes=6),
        )
        fake_client = Mock()
        fake_client.chat_postMessage.return_value = {
            "ok": True,
            "ts": "123.456",
        }

        with patch(
            "roo.office_manager.SlackService.get_client",
            return_value=fake_client,
        ):
            result = run_office_manager_scheduler(now=now)

        self.assertTrue(result["announcement_sent"])
        day.refresh_from_db()
        self.assertEqual(day.announcement_status, "sent")
        fake_client.chat_postMessage.assert_called_once()

    def test_scheduler_reaches_all_stale_assignment_delivery_leases(self):
        now = melbourne_at(2026, 8, 3, 16, 30)
        day = office_manager_day(now.date(), status_value="claimed")
        user = User.objects.create_user(
            email="stale-delivery@example.com",
            slack_id="USTALEDELIVERY",
        )
        booking = CoworkingBooking.objects.create(
            user=user,
            date=now.date(),
            points_cost=0,
            booking_source="office_manager",
        )
        assignment = OfficeManagerAssignment.objects.create(
            day=day,
            user=user,
            booking=booking,
            winner_channel_announcement_status="sending",
            winner_dm_status="sending",
            end_of_day_reminder_status="sending",
        )
        OfficeManagerAssignment.objects.filter(pk=assignment.pk).update(
            updated_at=timezone.now() - timedelta(minutes=6)
        )

        with (
            patch.object(
                OfficeManagerService,
                "deliver_winner_channel_announcement",
                return_value=True,
            ) as winner_channel,
            patch.object(
                OfficeManagerService,
                "deliver_winner_dm",
                return_value=True,
            ) as winner_dm,
            patch.object(
                OfficeManagerService,
                "deliver_end_of_day_reminder",
                return_value=True,
            ) as end_reminder,
        ):
            run_office_manager_scheduler(now=now)

        winner_channel.assert_called_once_with(assignment.id)
        winner_dm.assert_called_once_with(assignment.id)
        end_reminder.assert_called_once_with(assignment.id)

    def test_scheduler_expires_prior_date_deliveries_without_slack_output(self):
        now = melbourne_at(2026, 8, 3, 8, 0)
        prior_date = now.date() - timedelta(days=1)
        day = OfficeManagerDay.objects.create(
            date=prior_date,
            status="claimed",
            slack_channel_id="CCOWORK",
            claim_cutoff_at=melbourne_at(2026, 8, 2, 10),
            announcement_status="unknown",
        )
        user = User.objects.create_user(
            email="prior-delivery@example.com",
            slack_id="UPRIORDELIVERY",
        )
        booking = CoworkingBooking.objects.create(
            user=user,
            date=prior_date,
            points_cost=0,
            booking_source="office_manager",
        )
        assignment = OfficeManagerAssignment.objects.create(
            day=day,
            user=user,
            booking=booking,
            winner_channel_announcement_status="unknown",
            winner_dm_status="sending",
            winner_dm_last_error=(
                f"{DELIVERY_LEASE_PREFIX}old:"
                f"{(timezone.now() - timedelta(minutes=6)).timestamp():.6f}"
            ),
            end_of_day_reminder_status="pending",
        )

        with patch(
            "roo.office_manager.SlackService.get_client"
        ) as get_client:
            result = run_office_manager_scheduler(now=now)

        self.assertEqual(result["reason"], "before_announcement")
        get_client.assert_not_called()
        day.refresh_from_db()
        assignment.refresh_from_db()
        self.assertEqual(day.announcement_status, "failed")
        self.assertEqual(day.announcement_last_error, EXPIRED_DELIVERY_ERROR)
        for status_value, error_value in (
            (
                assignment.winner_channel_announcement_status,
                assignment.winner_channel_announcement_last_error,
            ),
            (assignment.winner_dm_status, assignment.winner_dm_last_error),
            (
                assignment.end_of_day_reminder_status,
                assignment.end_of_day_reminder_last_error,
            ),
        ):
            self.assertEqual(status_value, "failed")
            self.assertEqual(error_value, EXPIRED_DELIVERY_ERROR)

    def test_delivery_recovery_pauses_while_disabled_then_resumes_same_day(self):
        now = melbourne_at(2026, 8, 3, 9, 0)
        day = office_manager_day(now.date(), status_value="claimed")
        user = User.objects.create_user(
            email="reenabled-delivery@example.com",
            slack_id="UREENABLEDELIVERY",
        )
        booking = CoworkingBooking.objects.create(
            user=user,
            date=now.date(),
            points_cost=0,
            booking_source="office_manager",
        )
        assignment = OfficeManagerAssignment.objects.create(
            day=day,
            user=user,
            booking=booking,
        )

        with override_settings(OFFICE_MANAGER_ENABLED=False):
            disabled_result = run_office_manager_scheduler(now=now)

        self.assertEqual(disabled_result["reason"], "disabled")
        assignment.refresh_from_db()
        self.assertEqual(
            assignment.winner_channel_announcement_status,
            "pending",
        )
        self.assertEqual(assignment.winner_dm_status, "pending")

        fake_client = Mock()
        fake_client.conversations_open.return_value = {
            "ok": True,
            "channel": {"id": "DREENABLED"},
        }
        fake_client.chat_postMessage.return_value = {
            "ok": True,
            "ts": "reenabled.123",
        }
        with patch(
            "roo.office_manager.SlackService.get_client",
            return_value=fake_client,
        ):
            enabled_result = run_office_manager_scheduler(now=now)

        self.assertTrue(enabled_result["winner_channel_announcement_sent"])
        self.assertTrue(enabled_result["winner_dm_sent"])
        self.assertEqual(fake_client.chat_postMessage.call_count, 2)
        assignment.refresh_from_db()
        self.assertEqual(
            assignment.winner_channel_announcement_status,
            "sent",
        )
        self.assertEqual(assignment.winner_dm_status, "sent")

    def test_scheduler_terminalizes_relinquished_private_deliveries(self):
        now = melbourne_at(2026, 8, 3, 9, 0)
        day = office_manager_day(now.date())
        user = User.objects.create_user(
            email="relinquished-pending@example.com",
            slack_id="URELINQUISHEDPENDING",
        )
        booking = CoworkingBooking.objects.create(
            user=user,
            date=now.date(),
            status="cancelled",
            points_cost=0,
            booking_source="office_manager",
        )
        assignment = OfficeManagerAssignment.objects.create(
            day=day,
            user=user,
            booking=booking,
            status="relinquished",
        )

        with patch(
            "roo.office_manager.SlackService.get_client"
        ) as get_client:
            run_office_manager_scheduler(now=now)

        get_client.assert_not_called()
        assignment.refresh_from_db()
        for status_value, error_value in (
            (
                assignment.winner_channel_announcement_status,
                assignment.winner_channel_announcement_last_error,
            ),
            (assignment.winner_dm_status, assignment.winner_dm_last_error),
            (
                assignment.end_of_day_reminder_status,
                assignment.end_of_day_reminder_last_error,
            ),
        ):
            self.assertEqual(status_value, "failed")
            self.assertEqual(error_value, RELINQUISHED_DELIVERY_ERROR)

    def test_stale_announcement_retry_reuses_slack_client_message_id(self):
        now = melbourne_at(2026, 8, 3, 8, 30)
        day = office_manager_day(now.date())
        OfficeManagerDay.objects.filter(pk=day.pk).update(
            announcement_status="sending",
            slack_message_ts="",
            updated_at=timezone.now() - timedelta(minutes=6),
        )
        fake_client = Mock()
        fake_client.chat_postMessage.return_value = {
            "ok": True,
            "ts": "123.456",
        }

        with patch(
            "roo.office_manager.SlackService.get_client",
            return_value=fake_client,
        ):
            self.assertTrue(OfficeManagerService.post_announcement(day.id))
            OfficeManagerDay.objects.filter(pk=day.pk).update(
                announcement_status="sending",
                slack_message_ts="",
                updated_at=timezone.now() - timedelta(minutes=6),
            )
            self.assertTrue(OfficeManagerService.post_announcement(day.id))

        self.assertEqual(fake_client.chat_postMessage.call_count, 2)
        client_message_ids = {
            call.kwargs["client_msg_id"]
            for call in fake_client.chat_postMessage.call_args_list
        }
        self.assertEqual(
            client_message_ids,
            {_slack_client_msg_id("daily", day.id)},
        )

    @override_settings(
        OFFICE_MANAGER_CLAIM_CUTOFF_HOUR=9,
        OFFICE_MANAGER_CLAIM_CUTOFF_MINUTE=15,
    )
    def test_announcement_uses_configured_claim_cutoff(self):
        now = melbourne_at(2026, 8, 3, 8, 30)
        fake_client = Mock()
        fake_client.chat_postMessage.return_value = {
            "ok": True,
            "ts": "123.456",
        }

        with patch(
            "roo.office_manager.SlackService.get_client",
            return_value=fake_client,
        ):
            run_office_manager_scheduler(now=now)

        blocks_text = json_text(
            fake_client.chat_postMessage.call_args.kwargs["blocks"]
        )
        self.assertIn("Volunteer before 9:15 AM", blocks_text)
        self.assertNotIn("10:00 AM", blocks_text)

    def test_claimed_fallback_text_never_invites_another_volunteer(self):
        now = melbourne_at(2026, 8, 3, 9)
        day = office_manager_day(now.date(), status_value="claimed")

        text = _announcement_text(day)

        self.assertIn("Office Manager of the Day: A member", text)
        self.assertNotIn("Volunteer to be", text)

    def test_scheduler_skips_weekends_and_before_announcement(self):
        weekend = run_office_manager_scheduler(
            now=melbourne_at(2026, 8, 2, 9),
        )
        early = run_office_manager_scheduler(
            now=melbourne_at(2026, 8, 3, 8, 29),
        )
        self.assertEqual(weekend["reason"], "weekday_not_configured")
        self.assertEqual(early["reason"], "before_announcement")
        self.assertFalse(OfficeManagerDay.objects.exists())

    @override_settings(OFFICE_MANAGER_WEEKDAYS="")
    def test_empty_weekday_configuration_pauses_announcements(self):
        result = run_office_manager_scheduler(
            now=melbourne_at(2026, 8, 3, 9),
        )

        self.assertEqual(result["reason"], "weekday_not_configured")
        self.assertFalse(OfficeManagerDay.objects.exists())

    @override_settings(OFFICE_MANAGER_WEEKDAYS="0,monday")
    def test_invalid_weekday_configuration_fails_closed(self):
        result = run_office_manager_scheduler(
            now=melbourne_at(2026, 8, 3, 9),
        )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "invalid_weekday_configuration")
        self.assertFalse(OfficeManagerDay.objects.exists())

    def test_scheduler_suppresses_capacity_zero_days(self):
        now = melbourne_at(2026, 8, 3, 8, 30)
        CoworkingDayCapacity.objects.create(date=now.date(), capacity=0)

        result = run_office_manager_scheduler(now=now)

        self.assertEqual(result["reason"], "coworking_closed")
        self.assertFalse(OfficeManagerDay.objects.exists())

    def test_scheduler_does_not_post_when_first_run_is_after_cutoff(self):
        fake_client = Mock()

        with patch(
            "roo.office_manager.SlackService.get_client",
            return_value=fake_client,
        ):
            result = run_office_manager_scheduler(
                now=melbourne_at(2026, 8, 3, 10),
            )

        self.assertEqual(result["reason"], "volunteer_window_closed")
        self.assertFalse(OfficeManagerDay.objects.exists())
        fake_client.chat_postMessage.assert_not_called()

    def test_scheduler_closes_existing_day_with_booking_reminder_and_no_action(self):
        now = melbourne_at(2026, 8, 3, 10)
        day = office_manager_day(now.date())
        day.announcement_status = "sent"
        day.slack_message_ts = "123.456"
        day.save(
            update_fields=[
                "announcement_status",
                "slack_message_ts",
                "updated_at",
            ]
        )

        with patch(
            "roo.office_manager.SlackService.update_message",
            return_value=True,
        ) as update_message:
            result = run_office_manager_scheduler(now=now)

        day.refresh_from_db()
        self.assertEqual(result["status"], "closed")
        self.assertEqual(day.status, "closed")
        update_message.assert_called_once()
        fallback_text = update_message.call_args.args[2]
        blocks_text = json_text(update_message.call_args.kwargs["blocks"])
        self.assertIn(COWORKING_SELF_BOOK_REMINDER, fallback_text)
        self.assertIn(COWORKING_SELF_BOOK_REMINDER, blocks_text)
        self.assertNotIn(OFFICE_MANAGER_ACTION_ID, blocks_text)

    def test_existing_claimed_day_still_sends_reminder_if_capacity_is_zero(self):
        now = melbourne_at(2026, 8, 3, 16, 30)
        day = office_manager_day(now.date(), status_value="claimed")
        user = User.objects.create_user(
            email="reminder@example.com",
            slack_id="UREMINDER",
        )
        booking = CoworkingBooking.objects.create(
            user=user,
            date=now.date(),
            points_cost=0,
            booking_source="office_manager",
        )
        assignment = OfficeManagerAssignment.objects.create(
            day=day,
            user=user,
            booking=booking,
        )
        CoworkingDayCapacity.objects.create(date=now.date(), capacity=0)

        with (
            patch.object(
                OfficeManagerService,
                "deliver_winner_channel_announcement",
                return_value=True,
            ),
            patch.object(
                OfficeManagerService,
                "deliver_winner_dm",
                return_value=True,
            ),
            patch.object(
                OfficeManagerService,
                "deliver_end_of_day_reminder",
                return_value=True,
            ) as deliver_reminder,
        ):
            result = run_office_manager_scheduler(now=now)

        self.assertTrue(result["end_of_day_reminder_sent"])
        deliver_reminder.assert_called_once_with(assignment.id)

    def test_scheduler_retries_relinquished_winner_message_retraction(self):
        now = melbourne_at(2026, 8, 3, 9)
        day = office_manager_day(now.date())
        user = User.objects.create_user(
            email="relinquished@example.com",
            slack_id="URELINQUISHED",
        )
        booking = CoworkingBooking.objects.create(
            user=user,
            date=now.date(),
            status="cancelled",
            points_cost=0,
            booking_source="office_manager",
        )
        assignment = OfficeManagerAssignment.objects.create(
            day=day,
            user=user,
            booking=booking,
            status="relinquished",
            winner_channel_announcement_status="sent",
            winner_channel_message_ts="789.012",
            winner_channel_retraction_pending=True,
        )

        fake_client = Mock()
        fake_client.chat_update.side_effect = [
            {"ok": False, "error": "ratelimited"},
            {"ok": True},
        ]
        with patch(
            "roo.office_manager.SlackService.get_client",
            return_value=fake_client,
        ):
            first = run_office_manager_scheduler(now=now)
            assignment.refresh_from_db()
            self.assertFalse(first["winner_channel_retractions"][0])
            self.assertTrue(assignment.winner_channel_retraction_pending)
            OfficeManagerAssignment.objects.filter(pk=assignment.pk).update(
                winner_channel_retraction_next_attempt_at=(
                    timezone.now() - timedelta(seconds=1)
                )
            )

            second = run_office_manager_scheduler(now=now)

        self.assertTrue(second["winner_channel_retractions"][0])
        self.assertEqual(fake_client.chat_update.call_count, 2)
        assignment.refresh_from_db()
        self.assertFalse(assignment.winner_channel_retraction_pending)

    def test_scheduler_retries_prior_day_retraction_before_daily_time_gate(self):
        now = melbourne_at(2026, 8, 3, 8, 29)
        prior_day = office_manager_day(now.date() - timedelta(days=1))
        user = User.objects.create_user(
            email="prior-day-relinquished@example.com",
            slack_id="UPRIORDAY",
        )
        booking = CoworkingBooking.objects.create(
            user=user,
            date=prior_day.date,
            status="cancelled",
            points_cost=0,
            booking_source="office_manager",
        )
        assignment = OfficeManagerAssignment.objects.create(
            day=prior_day,
            user=user,
            booking=booking,
            status="relinquished",
            winner_channel_announcement_status="sent",
            winner_channel_message_ts="prior.123",
            winner_channel_retraction_pending=True,
        )

        fake_client = Mock()
        fake_client.chat_update.return_value = {"ok": True}
        with patch(
            "roo.office_manager.SlackService.get_client",
            return_value=fake_client,
        ):
            result = run_office_manager_scheduler(now=now)

        self.assertEqual(result["reason"], "before_announcement")
        self.assertEqual(result["winner_channel_retractions"], [True])
        fake_client.chat_update.assert_called_once()
        self.assertEqual(
            fake_client.chat_update.call_args.kwargs["channel"],
            "CCOWORK",
        )
        self.assertEqual(
            fake_client.chat_update.call_args.kwargs["ts"],
            "prior.123",
        )
        assignment.refresh_from_db()
        self.assertFalse(assignment.winner_channel_retraction_pending)

    @override_settings(OFFICE_MANAGER_ENABLED=False)
    def test_scheduler_retries_retraction_while_feature_is_disabled(self):
        now = melbourne_at(2026, 8, 3, 9)
        prior_day = office_manager_day(now.date() - timedelta(days=1))
        user = User.objects.create_user(
            email="disabled-retraction@example.com",
            slack_id="UDISABLEDRETRACTION",
        )
        booking = CoworkingBooking.objects.create(
            user=user,
            date=prior_day.date,
            status="cancelled",
            points_cost=0,
            booking_source="office_manager",
        )
        assignment = OfficeManagerAssignment.objects.create(
            day=prior_day,
            user=user,
            booking=booking,
            status="relinquished",
            winner_channel_announcement_status="sent",
            winner_channel_message_ts="disabled.123",
            winner_channel_retraction_pending=True,
        )

        fake_client = Mock()
        fake_client.chat_update.return_value = {"ok": True}
        with patch(
            "roo.office_manager.SlackService.get_client",
            return_value=fake_client,
        ):
            result = run_office_manager_scheduler(now=now)

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "disabled")
        self.assertEqual(result["winner_channel_retractions"], [True])
        fake_client.chat_update.assert_called_once()
        assignment.refresh_from_db()
        self.assertFalse(assignment.winner_channel_retraction_pending)

    def test_bounded_retraction_sweep_rotates_failed_assignments(self):
        assignments = []
        for offset, suffix in enumerate(("OLD", "NEXT"), start=1):
            day = office_manager_day(
                melbourne_at(2026, 8, offset, 9).date()
            )
            user = User.objects.create_user(
                email=f"retraction-{suffix.lower()}@example.com",
                slack_id=f"URETRACTION{suffix}",
            )
            booking = CoworkingBooking.objects.create(
                user=user,
                date=day.date,
                status="cancelled",
                points_cost=0,
                booking_source="office_manager",
            )
            assignment = OfficeManagerAssignment.objects.create(
                day=day,
                user=user,
                booking=booking,
                status="relinquished",
                winner_channel_announcement_status="sent",
                winner_channel_message_ts=f"retraction.{offset}",
                winner_channel_retraction_pending=True,
            )
            assignments.append(assignment)

        old_timestamp = timezone.now() - timedelta(days=2)
        OfficeManagerAssignment.objects.filter(pk=assignments[0].pk).update(
            updated_at=old_timestamp,
        )
        OfficeManagerAssignment.objects.filter(pk=assignments[1].pk).update(
            updated_at=old_timestamp + timedelta(minutes=1),
        )

        fake_client = Mock()
        fake_client.chat_update.return_value = {
            "ok": False,
            "error": "ratelimited",
        }
        with patch(
            "roo.office_manager.SlackService.get_client",
            return_value=fake_client,
        ):
            first = OfficeManagerService.retry_pending_winner_retractions(limit=1)
            second = OfficeManagerService.retry_pending_winner_retractions(limit=1)

        self.assertEqual(first, [False])
        self.assertEqual(second, [False])
        self.assertEqual(
            [call.kwargs["ts"] for call in fake_client.chat_update.call_args_list],
            ["retraction.1", "retraction.2"],
        )

    @override_settings(OFFICE_MANAGER_SLACK_CHANNEL_ID="CNEWCHANNEL")
    def test_existing_day_keeps_original_channel_after_configuration_change(self):
        now = melbourne_at(2026, 8, 3, 9)
        day = office_manager_day(now.date())
        OfficeManagerDay.objects.filter(pk=day.pk).update(
            slack_channel_id="CORIGINAL",
            message_update_pending=True,
        )

        with patch(
            "roo.office_manager.SlackService.update_message",
            return_value=True,
        ) as update_message:
            result = run_office_manager_scheduler(now=now)

        self.assertTrue(result["message_updated"])
        day.refresh_from_db()
        self.assertEqual(day.slack_channel_id, "CORIGINAL")
        update_message.assert_called_once()
        self.assertEqual(update_message.call_args.args[:2], ("CORIGINAL", "123.456"))

    def test_fatal_office_manager_result_fails_scheduled_command(self):
        runner_names = (
            "run_daily_discovery_scheduler",
            "run_daily_jobs_scheduler",
            "run_research_automation_scheduler",
            "run_content_factory_reconciliation_sweep",
            "run_github_installation_reconciliation_sweep",
            "run_daily_payout_reconciliation",
            "run_scheduled_sim_conversation_cleanup",
            "run_daily_article_report_scheduler",
            "run_monthly_update_reminder_scheduler",
            "run_office_manager_scheduler",
        )
        command_module = "core.management.commands.run_scheduled_discovery"
        stdout = io.StringIO()

        with ExitStack() as stack:
            runners = {
                name: stack.enter_context(
                    patch(
                        f"{command_module}.{name}",
                        return_value={"status": "skipped"},
                    )
                )
                for name in runner_names
            }
            runners["run_office_manager_scheduler"].return_value = {
                "status": "failed",
                "reason": "channel_not_configured",
            }

            with self.assertRaisesMessage(CommandError, "office_manager"):
                call_command("run_scheduled_discovery", stdout=stdout)

        self.assertIn(
            '"office_manager": {"reason": "channel_not_configured", '
            '"status": "failed"}',
            stdout.getvalue(),
        )


def json_text(value):
    import json

    return json.dumps(value)


@override_settings(
    ROO_API_KEY="office-manager-test-key",
    INTERNAL_API_KEY="office-manager-internal-test-key",
    MLAI_API_KEY="office-manager-mlai-test-key",
    OFFICE_MANAGER_ENABLED=True,
    OFFICE_MANAGER_SLACK_BOT_TOKEN="office-manager-public-roo-test-token",
    OFFICE_MANAGER_TIMEZONE="Australia/Melbourne",
)
class OfficeManagerClaimApiTests(APITestCase):
    def setUp(self):
        self.now = melbourne_at(2026, 8, 3, 8, 45)
        self.day = office_manager_day(self.now.date())
        self.user = User.objects.create_user(
            email="api-volunteer@example.com",
            slack_id="UAPIVOL",
        )
        self.url = reverse("coworking-office-manager-claim")
        self.attempt_id = str(uuid.uuid4())

    def test_strict_roo_key_is_required(self):
        response = self.client.post(
            self.url,
            {
                "slack_user_id": self.user.slack_id,
                "date": self.now.date(),
                "attempt_id": self.attempt_id,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @override_settings(
        INTERNAL_API_KEY="legacy-internal-test-key",
        MLAI_API_KEY="legacy-mlai-test-key",
    )
    def test_broader_service_keys_cannot_claim_for_a_slack_member(self):
        for api_key in ("legacy-internal-test-key", "legacy-mlai-test-key"):
            with self.subTest(api_key=api_key):
                response = self.client.post(
                    self.url,
                    {
                        "slack_user_id": self.user.slack_id,
                        "date": self.now.date(),
                        "attempt_id": self.attempt_id,
                    },
                    format="json",
                    HTTP_X_API_KEY=api_key,
                )

                self.assertEqual(
                    response.status_code,
                    status.HTTP_401_UNAUTHORIZED,
                )

    def test_office_manager_cancel_requires_isolated_roo_key(self):
        booking = CoworkingBooking.objects.create(
            user=self.user,
            date=self.now.date(),
            points_cost=0,
            booking_source="office_manager",
        )
        url = reverse("coworking-cancel")
        payload = {
            "slack_user_id": self.user.slack_id,
            "booking_id": str(booking.id),
        }

        cases = (
            (None, status.HTTP_401_UNAUTHORIZED),
            ("office-manager-internal-test-key", status.HTTP_403_FORBIDDEN),
            ("office-manager-mlai-test-key", status.HTTP_403_FORBIDDEN),
        )
        for api_key, expected_status in cases:
            with self.subTest(api_key=api_key):
                headers = {} if api_key is None else {"HTTP_X_API_KEY": api_key}
                response = self.client.post(
                    url,
                    payload,
                    format="json",
                    **headers,
                )
                self.assertEqual(response.status_code, expected_status)
                booking.refresh_from_db()
                self.assertEqual(booking.status, "booked")

    @override_settings(
        ROO_API_KEY="aliased-office-manager-key",
        INTERNAL_API_KEY="aliased-office-manager-key",
    )
    def test_aliased_roo_key_cannot_cancel_office_manager_booking(self):
        booking = CoworkingBooking.objects.create(
            user=self.user,
            date=self.now.date(),
            points_cost=0,
            booking_source="office_manager",
        )
        response = self.client.post(
            reverse("coworking-cancel"),
            {
                "slack_user_id": self.user.slack_id,
                "booking_id": str(booking.id),
            },
            format="json",
            HTTP_X_API_KEY="aliased-office-manager-key",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        booking.refresh_from_db()
        self.assertEqual(booking.status, "booked")

    @override_settings(
        ROO_API_KEY="aliased-office-manager-key",
        INTERNAL_API_KEY="aliased-office-manager-key",
    )
    def test_aliased_roo_key_cannot_claim_office_manager_day(self):
        response = self.client.post(
            self.url,
            {
                "slack_user_id": self.user.slack_id,
                "date": self.now.date(),
                "attempt_id": self.attempt_id,
            },
            format="json",
            HTTP_X_API_KEY="aliased-office-manager-key",
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.day.refresh_from_db()
        self.assertEqual(self.day.status, "open")

    @patch("roo.views.OfficeManagerService.claim")
    def test_claim_rejection_code_contract(self, claim):
        cases = (
            ("feature_disabled", status.HTTP_503_SERVICE_UNAVAILABLE),
            ("member_not_eligible", status.HTTP_403_FORBIDDEN),
            ("office_manager_day_not_found", status.HTTP_404_NOT_FOUND),
            ("already_claimed", status.HTTP_409_CONFLICT),
            ("claim_closed", status.HTTP_409_CONFLICT),
            ("attempt_payload_conflict", status.HTTP_409_CONFLICT),
            ("attempt_superseded", status.HTTP_409_CONFLICT),
            ("attempt_unavailable", status.HTTP_503_SERVICE_UNAVAILABLE),
            ("refund_unavailable", status.HTTP_409_CONFLICT),
        )

        for code, expected_status in cases:
            with self.subTest(code=code):
                claim.side_effect = OfficeManagerClaimError(code, "claim rejected")
                response = self.client.post(
                    self.url,
                    {
                        "slack_user_id": self.user.slack_id,
                        "date": self.now.date(),
                        "attempt_id": self.attempt_id,
                    },
                    format="json",
                    HTTP_X_API_KEY="office-manager-test-key",
                )

                self.assertEqual(response.status_code, expected_status)
                self.assertEqual(response.data["code"], code)

    @patch("roo.views.OfficeManagerService.retract_winner_channel_announcement")
    @patch("roo.views.OfficeManagerService.reconcile_message")
    @patch("roo.views.CoworkingService.cancel")
    def test_cancel_reconciles_both_office_manager_channel_messages(
        self,
        cancel,
        reconcile_message,
        retract_winner,
    ):
        booking = CoworkingBooking.objects.create(
            user=self.user,
            date=self.now.date(),
            points_cost=0,
            booking_source="office_manager",
        )
        booking.status = "cancelled"
        booking._office_manager_day_id = self.day.id
        booking._office_manager_assignment_id = 4242
        booking._office_manager_day_reopened = True
        cancel.return_value = (booking, False)

        response = self.client.post(
            reverse("coworking-cancel"),
            {
                "slack_user_id": self.user.slack_id,
                "booking_id": str(booking.id),
            },
            format="json",
            HTTP_X_API_KEY="office-manager-test-key",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        reconcile_message.assert_called_once_with(self.day.id)
        retract_winner.assert_called_once_with(4242)

    @patch("roo.views.OfficeManagerService.retract_winner_channel_announcement")
    @patch("roo.views.OfficeManagerService.reconcile_message")
    def test_cancel_replay_repairs_messages_after_committed_response_loss(
        self,
        reconcile_message,
        retract_winner,
    ):
        booking = CoworkingBooking.objects.create(
            user=self.user,
            date=self.now.date(),
            status="cancelled",
            points_cost=0,
            booking_source="office_manager",
            cancelled_at=self.now,
        )
        assignment = OfficeManagerAssignment.objects.create(
            day=self.day,
            user=self.user,
            booking=booking,
            status="relinquished",
            relinquished_at=self.now,
            winner_channel_announcement_status="sent",
            winner_channel_message_ts="winner.456",
            winner_channel_retraction_pending=True,
        )
        ledger_count = Ledger.objects.count()

        response = self.client.post(
            reverse("coworking-cancel"),
            {
                "slack_user_id": self.user.slack_id,
                "booking_id": str(booking.id),
            },
            format="json",
            HTTP_X_API_KEY="office-manager-test-key",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["already_cancelled"])
        self.assertFalse(response.data["refunded"])
        self.assertEqual(Ledger.objects.count(), ledger_count)
        reconcile_message.assert_called_once_with(self.day.id)
        retract_winner.assert_called_once_with(assignment.id)

    def test_cancel_replay_does_not_duplicate_standard_refund(self):
        PointsService.award(
            user=self.user,
            delta=8,
            source="MANUAL",
            description="refund setup",
            created_by_slack_id="UADMIN",
            idempotency_key="api-standard-cancel-setup",
        )
        booking, _ = CoworkingService.book(
            user=self.user,
            booking_date=timezone.localdate() + timedelta(days=7),
            created_by_slack_id=self.user.slack_id,
        )
        request_data = {
            "slack_user_id": self.user.slack_id,
            "booking_id": str(booking.id),
        }

        first = self.client.post(
            reverse("coworking-cancel"),
            request_data,
            format="json",
            HTTP_X_API_KEY="office-manager-test-key",
        )
        balance_after_first = PointsAccount.objects.get(user=self.user).balance
        second = self.client.post(
            reverse("coworking-cancel"),
            request_data,
            format="json",
            HTTP_X_API_KEY="office-manager-test-key",
        )

        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertTrue(first.data["refunded"])
        self.assertFalse(first.data["already_cancelled"])
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertTrue(second.data["refunded"])
        self.assertTrue(second.data["already_cancelled"])
        self.assertEqual(
            PointsAccount.objects.get(user=self.user).balance,
            balance_after_first,
        )
        self.assertEqual(
            Ledger.objects.filter(
                idempotency_key=f"coworking_refund:{booking.id}"
            ).count(),
            1,
        )

    @patch(
        "roo.views.OfficeManagerService.deliver_winner_channel_announcement",
        return_value=True,
    )
    @patch("roo.views.OfficeManagerService.deliver_winner_dm", return_value=True)
    @patch("roo.views.OfficeManagerService.reconcile_message", return_value=True)
    @patch("roo.views.OfficeManagerService.claim")
    def test_claim_response_identifies_zero_point_source(
        self,
        claim,
        _reconcile_message,
        _deliver_winner_dm,
        _deliver_winner_channel_announcement,
    ):
        booking = CoworkingBooking.objects.create(
            user=self.user,
            date=self.now.date(),
            points_cost=0,
            booking_source="office_manager",
        )
        assignment = OfficeManagerAssignment.objects.create(
            day=self.day,
            user=self.user,
            booking=booking,
            points_refunded=8,
        )
        from .office_manager import OfficeManagerClaimResult

        claim.return_value = OfficeManagerClaimResult(
            assignment=assignment,
            booking=booking,
            status="claimed",
            existing_booking_converted=True,
        )

        response = self.client.post(
            self.url,
            {
                "slack_user_id": self.user.slack_id,
                "date": self.now.date(),
                "attempt_id": self.attempt_id,
            },
            format="json",
            HTTP_X_API_KEY="office-manager-test-key",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["points_charged"], 0)
        self.assertEqual(response.data["points_refunded"], 8)
        self.assertTrue(response.data["office_manager_free_day"])
        self.assertFalse(response.data["monthly_update_discount_applied"])
        self.assertFalse(response.data["booking"]["is_refundable"])
        self.assertEqual(
            response.data["booking"]["booking_source"],
            "office_manager",
        )
        _deliver_winner_channel_announcement.assert_called_once_with(
            assignment.id
        )

    @patch(
        "roo.views.OfficeManagerService.deliver_winner_channel_announcement",
        return_value=True,
    )
    @patch("roo.views.OfficeManagerService.deliver_winner_dm", return_value=True)
    @patch("roo.views.OfficeManagerService.reconcile_message", return_value=True)
    @patch("roo.views.OfficeManagerService.claim")
    def test_idempotent_claim_response_contract(
        self,
        claim,
        _reconcile_message,
        _deliver_winner_dm,
        _deliver_winner_channel_announcement,
    ):
        booking = CoworkingBooking.objects.create(
            user=self.user,
            date=self.now.date(),
            points_cost=0,
            booking_source="office_manager",
        )
        assignment = OfficeManagerAssignment.objects.create(
            day=self.day,
            user=self.user,
            booking=booking,
            points_refunded=4,
        )
        from .office_manager import OfficeManagerClaimResult

        claim.return_value = OfficeManagerClaimResult(
            assignment=assignment,
            booking=booking,
            status="already_claimed_by_you",
            existing_booking_converted=True,
        )

        response = self.client.post(
            self.url,
            {
                "slack_user_id": self.user.slack_id,
                "date": self.now.date(),
                "attempt_id": self.attempt_id,
            },
            format="json",
            HTTP_X_API_KEY="office-manager-test-key",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "already_claimed_by_you")
        self.assertEqual(response.data["points_refunded"], 4)


class OfficeManagerMigrationAuditInvariantTests(TestCase):
    def test_claimed_day_requires_exactly_one_active_assignment(self):
        day = office_manager_day(
            melbourne_at(2026, 8, 3, 9).date(),
            status_value="claimed",
        )
        stdout = io.StringIO()

        with self.assertRaises(CommandError):
            call_command("audit_office_manager_migrations", stdout=stdout)

        report = json.loads(stdout.getvalue().splitlines()[0])
        self.assertEqual(report["status"], "unsafe")
        self.assertEqual(
            report["data_invariants"][
                "claimed_days_without_exactly_one_active_assignment"
            ],
            [
                {
                    "active_assignment_count": 0,
                    "date": day.date.isoformat(),
                    "day_id": day.id,
                }
            ],
        )

    def test_active_assignment_requires_claimed_day(self):
        day = office_manager_day(melbourne_at(2026, 8, 3, 9).date())
        user = User.objects.create_user(
            email="audit-invariant@example.com",
            slack_id="UAUDITINVARIANT",
        )
        booking = CoworkingBooking.objects.create(
            user=user,
            date=day.date,
            points_cost=0,
            booking_source="office_manager",
        )
        assignment = OfficeManagerAssignment.objects.create(
            day=day,
            user=user,
            booking=booking,
        )
        stdout = io.StringIO()

        with self.assertRaises(CommandError):
            call_command("audit_office_manager_migrations", stdout=stdout)

        report = json.loads(stdout.getvalue().splitlines()[0])
        self.assertEqual(report["status"], "unsafe")
        self.assertEqual(
            report["data_invariants"][
                "active_assignments_on_non_claimed_days"
            ],
            [
                {
                    "assignment_id": assignment.id,
                    "date": day.date.isoformat(),
                    "day_id": day.id,
                    "day_status": "open",
                }
            ],
        )


@skipUnless(
    connection.vendor == "postgresql",
    "PostgreSQL row-lock behavior is not provided by SQLite",
)
@override_settings(
    OFFICE_MANAGER_ENABLED=True,
    OFFICE_MANAGER_SLACK_BOT_TOKEN="office-manager-public-roo-test-token",
    OFFICE_MANAGER_TIMEZONE="Australia/Melbourne",
)
class OfficeManagerPostgresConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.profile_patcher = patch(
            "roo.office_manager.SlackService.get_user_profile",
            side_effect=active_slack_profile,
        )
        self.profile_patcher.start()
        self.addCleanup(self.profile_patcher.stop)

    def test_two_simultaneous_claims_create_one_assignment(self):
        now = melbourne_at(2026, 8, 3, 8, 45)
        office_manager_day(now.date())
        users = [
            User.objects.create_user(
                email=f"race-{index}@example.com",
                slack_id=f"URACE{index:05d}",
            )
            for index in range(2)
        ]

        def claim(user_id):
            close_old_connections()
            user = User.objects.get(pk=user_id)
            try:
                result = OfficeManagerService.claim(
                    slack_user_id=user.slack_id,
                    booking_date=now.date(),
                    now=now,
                )
                return result.status
            except OfficeManagerClaimError as exc:
                return exc.code
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(claim, [user.id for user in users]))

        self.assertCountEqual(outcomes, ["claimed", "already_claimed"])
        self.assertEqual(
            OfficeManagerAssignment.objects.filter(status="active").count(),
            1,
        )
        self.assertEqual(CoworkingBooking.objects.count(), 1)

    @patch("roo.office_manager.SlackService.get_user_profile")
    def test_concurrent_identity_resolution_creates_one_linked_user(
        self,
        get_profile,
    ):
        get_profile.return_value = {
            "real_name": "Concurrent Member",
            "email": "concurrent-member@example.com",
            "is_bot": False,
            "deleted": False,
        }
        barrier = Barrier(2)

        def resolve_member():
            close_old_connections()
            try:
                barrier.wait()
                return OfficeManagerService.resolve_member("UCONCURRENT").id
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            user_ids = list(executor.map(lambda _: resolve_member(), range(2)))

        self.assertEqual(user_ids[0], user_ids[1])
        self.assertEqual(
            User.objects.filter(email="concurrent-member@example.com").count(),
            1,
        )
        self.assertEqual(
            User.objects.get(email="concurrent-member@example.com").slack_id,
            "UCONCURRENT",
        )

    def test_concurrent_relinquish_and_claim_do_not_deadlock(self):
        now = melbourne_at(2026, 8, 3, 8, 45)
        office_manager_day(now.date())
        first_user = User.objects.create_user(
            email="current-office-manager@example.com",
            slack_id="UCURRENTOM",
        )
        second_user = User.objects.create_user(
            email="next-office-manager@example.com",
            slack_id="UNEXTOM123",
        )
        current = OfficeManagerService.claim(
            slack_user_id=first_user.slack_id,
            booking_date=now.date(),
            now=now,
        )
        barrier = Barrier(2)

        def cancel_current():
            close_old_connections()
            try:
                barrier.wait()
                CoworkingService.cancel(
                    str(current.booking.id),
                    first_user.slack_id,
                )
                return "cancelled"
            finally:
                connections.close_all()

        def claim_next():
            close_old_connections()
            try:
                barrier.wait()
                try:
                    result = OfficeManagerService.claim(
                        slack_user_id=second_user.slack_id,
                        booking_date=now.date(),
                        now=now,
                    )
                    return result.status
                except OfficeManagerClaimError as exc:
                    return exc.code
            finally:
                connections.close_all()

        with patch("roo.office_manager._local_now", return_value=now):
            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = [
                    executor.submit(cancel_current),
                    executor.submit(claim_next),
                ]
                outcomes = [future.result(timeout=10) for future in outcomes]

        self.assertIn("cancelled", outcomes)
        self.assertTrue(
            set(outcomes).issubset(
                {"cancelled", "claimed", "already_claimed", "claim_closed"}
            )
        )
        self.assertLessEqual(
            OfficeManagerAssignment.objects.filter(status="active").count(),
            1,
        )
