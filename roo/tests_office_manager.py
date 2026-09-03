from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import io
import json
import uuid
from threading import Barrier, Event
from unittest import skipUnless
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from django.contrib.admin.sites import AdminSite
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import (
    IntegrityError,
    OperationalError,
    close_old_connections,
    connection,
    connections,
)
from django.db.models.deletion import ProtectedError
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase
from slack_sdk.errors import SlackApiError

from core.models import User

from .admin import (
    CoworkingBookingAdmin,
    OfficeManagerAssignmentAdmin,
    OfficeManagerDayAdmin,
)
from .models import (
    CoworkingBooking,
    CoworkingDayCapacity,
    Ledger,
    OfficeManagerAssignment,
    OfficeManagerClaimAttempt,
    OfficeManagerDay,
    OfficeManagerProvenanceBucketRepair,
    OfficeManagerProvenanceReconciliation,
    OfficeManagerRefundReversalProvenance,
    PointsAccount,
)
from .office_manager import (
    DELIVERY_LEASE_PREFIX,
    EXPIRED_DELIVERY_ERROR,
    RELINQUISHED_DELIVERY_ERROR,
    NO_FOOD_REMINDER,
    OFFICE_MANAGER_ACTION_ID,
    OfficeManagerClaimError,
    OfficeManagerService,
    _announcement_text,
    _finish_delivery_failure,
    _slack_client_msg_id,
    _winner_channel_client_msg_id,
    run_office_manager_scheduler,
)
from .permissions import PermissionDeniedError
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
        self.local_now_patcher = patch(
            "roo.office_manager._local_now",
            return_value=self.now,
        )
        self.local_now_patcher.start()
        self.addCleanup(self.local_now_patcher.stop)
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

    def test_claim_rechecks_cutoff_after_member_lookup_and_row_locks(self):
        before_cutoff = melbourne_at(2026, 8, 3, 9, 59)
        at_cutoff = melbourne_at(2026, 8, 3, 10)
        with (
            patch.object(
                OfficeManagerService,
                "resolve_member",
                return_value=self.user,
            ),
            patch(
                "roo.office_manager._local_now",
                side_effect=[before_cutoff, before_cutoff, at_cutoff],
            ),
            self.assertRaises(OfficeManagerClaimError) as raised,
        ):
            OfficeManagerService.claim(
                slack_user_id=self.user.slack_id,
                booking_date=self.now.date(),
                attempt_id=uuid.uuid4(),
            )

        self.assertEqual(raised.exception.code, "claim_closed")
        self.assertFalse(OfficeManagerAssignment.objects.exists())

    def test_claim_rechecks_locked_member_slack_identity(self):
        def change_identity_during_lookup(_slack_user_id):
            User.objects.filter(pk=self.user.pk).update(slack_id="UREPLACED")
            return self.user

        with (
            patch.object(
                OfficeManagerService,
                "resolve_member",
                side_effect=change_identity_during_lookup,
            ),
            self.assertRaises(OfficeManagerClaimError) as raised,
        ):
            OfficeManagerService.claim(
                slack_user_id="UVOLUNTEER",
                booking_date=self.now.date(),
                attempt_id=uuid.uuid4(),
                now=self.now,
            )

        self.assertEqual(raised.exception.code, "member_not_eligible")
        self.assertFalse(OfficeManagerAssignment.objects.exists())

    def test_office_manager_booking_requires_strict_roo_authority_to_cancel(self):
        result = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )

        with self.assertRaises(PermissionDeniedError):
            CoworkingService.cancel(
                str(result.booking.id),
                self.user.slack_id,
                office_manager_authorized=False,
            )

        result.booking.refresh_from_db()
        result.assignment.refresh_from_db()
        self.assertEqual(result.booking.status, "booked")
        self.assertEqual(result.assignment.status, "active")

    def test_cancellation_rechecks_locked_owner(self):
        result = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )
        other = User.objects.create_user(
            email="wrong-canceller@example.com",
            slack_id="UWRONGCANCELLER",
        )

        with self.assertRaises(PermissionDeniedError):
            CoworkingService.cancel(
                str(result.booking.id),
                other.slack_id,
                office_manager_authorized=True,
            )

        result.booking.refresh_from_db()
        self.assertEqual(result.booking.status, "booked")

    def test_cancellation_retries_whole_transaction_after_deadlock(self):
        expected = (Mock(), False)
        error = OperationalError("deadlock detected")
        error.pgcode = "40P01"

        with (
            patch.object(
                CoworkingService,
                "_cancel_once",
                side_effect=[error, expected],
            ) as cancel_once,
            patch("roo.services.time.sleep") as sleep,
        ):
            result = CoworkingService.cancel(
                "booking-id",
                self.user.slack_id,
                office_manager_authorized=True,
            )

        self.assertEqual(result, expected)
        self.assertEqual(cancel_once.call_count, 2)
        sleep.assert_called_once_with(0.05)

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

    def test_admin_cannot_bypass_booking_or_assignment_state_machines(self):
        booking_admin = CoworkingBookingAdmin(CoworkingBooking, AdminSite())
        assignment_admin = OfficeManagerAssignmentAdmin(
            OfficeManagerAssignment,
            AdminSite(),
        )
        day_admin = OfficeManagerDayAdmin(OfficeManagerDay, AdminSite())

        self.assertEqual(
            set(booking_admin.get_readonly_fields(None)),
            {field.name for field in CoworkingBooking._meta.fields},
        )
        self.assertFalse(booking_admin.has_add_permission(None))
        self.assertFalse(booking_admin.has_delete_permission(None))
        self.assertEqual(
            set(assignment_admin.get_readonly_fields(None)),
            {field.name for field in OfficeManagerAssignment._meta.fields},
        )
        self.assertEqual(
            set(day_admin.get_readonly_fields(None)),
            {field.name for field in OfficeManagerDay._meta.fields},
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
        CoworkingService.cancel(
            str(first.booking.id),
            self.user.slack_id,
            office_manager_authorized=True,
        )

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
            generation=2,
            now=self.now,
        )
        self.assertEqual(replacement.status, "claimed")
        self.assertNotEqual(replacement.assignment.id, first.assignment.id)
        self.assertEqual(
            OfficeManagerAssignment.objects.filter(status="active").count(),
            1,
        )

    @patch("roo.office_manager._local_now")
    def test_cancel_fences_unseen_old_button_before_reopened_claim(
        self,
        mocked_now,
    ):
        mocked_now.return_value = self.now
        first = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            attempt_id=uuid.uuid4(),
            generation=1,
            now=self.now,
        )
        CoworkingService.cancel(
            str(first.booking.id),
            self.user.slack_id,
            office_manager_authorized=True,
        )
        self.day.refresh_from_db()
        self.assertEqual(self.day.generation, 2)

        unseen_old_attempt = uuid.uuid4()
        with self.assertRaises(OfficeManagerClaimError) as stale:
            OfficeManagerService.claim(
                slack_user_id=self.user.slack_id,
                booking_date=self.now.date(),
                attempt_id=unseen_old_attempt,
                generation=1,
                now=self.now,
            )
        self.assertEqual(stale.exception.code, "attempt_superseded")
        self.assertEqual(
            OfficeManagerClaimAttempt.objects.get(pk=unseen_old_attempt).outcome,
            "attempt_superseded",
        )
        self.assertFalse(
            OfficeManagerAssignment.objects.filter(status="active").exists()
        )

        replacement = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            attempt_id=uuid.uuid4(),
            generation=2,
            now=self.now,
        )
        self.assertEqual(replacement.status, "claimed")

    def test_attempt_id_is_bound_to_announcement_generation(self):
        attempt_id = uuid.uuid4()
        OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            attempt_id=attempt_id,
            generation=1,
            now=self.now,
        )

        with self.assertRaises(OfficeManagerClaimError) as conflict:
            OfficeManagerService.claim(
                slack_user_id=self.user.slack_id,
                booking_date=self.now.date(),
                attempt_id=attempt_id,
                generation=2,
                now=self.now,
            )
        self.assertEqual(conflict.exception.code, "attempt_payload_conflict")

    def test_losing_attempt_insert_rolls_back_business_mutation_before_replay(
        self,
    ):
        attempt_id = uuid.uuid4()
        winning_attempt = Mock(attempt_id=attempt_id)
        replayed = Mock()
        attempt_query = Mock()
        attempt_query.filter.return_value.first.return_value = None
        attempt_query.get.return_value = winning_attempt

        with (
            patch.object(
                OfficeManagerClaimAttempt.objects,
                "select_related",
                return_value=attempt_query,
            ),
            patch.object(
                OfficeManagerService,
                "_persist_attempt",
                return_value=(winning_attempt, False),
            ),
            patch.object(
                OfficeManagerService,
                "_replay_attempt",
                return_value=replayed,
            ) as replay,
        ):
            result = OfficeManagerService.claim(
                slack_user_id=self.user.slack_id,
                booking_date=self.now.date(),
                attempt_id=attempt_id,
                generation=1,
                now=self.now,
            )

        self.assertIs(result, replayed)
        replay.assert_called_once_with(
            winning_attempt,
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            generation=1,
        )
        self.assertFalse(CoworkingBooking.objects.exists())
        self.assertFalse(OfficeManagerAssignment.objects.exists())
        self.day.refresh_from_db()
        self.assertEqual(self.day.status, "open")

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
        next_day = melbourne_at(2026, 8, 4, 0, 1)
        with (
            patch("roo.office_manager._local_now", return_value=next_day),
            self.assertRaises(OfficeManagerClaimError) as raised,
        ):
            OfficeManagerService.claim(
                slack_user_id=self.user.slack_id,
                booking_date=self.now.date(),
                now=next_day,
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
        # Historical bookings used either the local date or booking UUID as
        # the authoritative debit reference. Both must survive conversion and
        # cancellation under the same validation contract.
        booking.ledger_entry.reference_id = str(booking.pk)
        booking.ledger_entry.save(update_fields=["reference_id"])
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
            CoworkingService.cancel(
                str(booking.id),
                self.user.slack_id,
                office_manager_authorized=True,
            )
        account.refresh_from_db()
        self.assertEqual(account.purchased_topup_balance_microroo, 0)
        self.assertEqual(account.earned_balance_microroo, 0)

    def test_cancellation_refuses_unattested_legacy_refund_allocation(self):
        PointsService.credit_purchased_topup(
            user=self.user,
            delta=3,
            description="Purchased setup",
            idempotency_key="legacy-provenance-purchased",
        )
        PointsService.award(
            user=self.user,
            delta=5,
            source="MANUAL",
            description="Earned setup",
            created_by_slack_id="UADMIN",
            idempotency_key="legacy-provenance-earned",
        )
        with patch("roo.services.timezone.now", return_value=self.now):
            booking, _ = CoworkingService.book(
                user=self.user,
                booking_date=self.now.date(),
                created_by_slack_id=self.user.slack_id,
            )
        claimed = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )
        OfficeManagerAssignment.objects.filter(pk=claimed.assignment.pk).update(
            # Simulates the misleading default left by a committed 0036 when
            # the 0037 quarantine/attestation migration did not complete.
            purchased_points_refunded_microroo=0,
        )

        with self.assertRaisesMessage(
            ValueError,
            "authoritative ledger entry is unavailable",
        ):
            CoworkingService.cancel(
                str(booking.id),
                self.user.slack_id,
                office_manager_authorized=True,
            )

        booking.refresh_from_db()
        claimed.assignment.refresh_from_db()
        self.assertEqual(booking.status, "booked")
        self.assertEqual(claimed.assignment.status, "active")
        self.assertIsNone(claimed.assignment.refund_reversal_ledger_entry_id)

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

    def test_conversion_refuses_debit_from_a_different_booking_date(self):
        PointsService.award(
            user=self.user,
            delta=8,
            source="MANUAL",
            description="Setup",
            created_by_slack_id="UADMIN",
            idempotency_key="wrong-date-refund-setup",
        )
        spend_ledger, _ = PointsService.spend(
            user=self.user,
            delta=8,
            source="COWORKING",
            description="Different coworking booking",
            created_by_slack_id=self.user.slack_id,
            idempotency_key="wrong-date-booking-spend",
            reference_type="COWORKING_BOOKING",
            reference_id=(self.now.date() + timedelta(days=1)).isoformat(),
        )
        CoworkingBooking.objects.create(
            user=self.user,
            date=self.now.date(),
            status="booked",
            points_cost=8,
            booking_source="points",
            purchased_points_cost_microroo=0,
            ledger_entry=spend_ledger,
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
                office_manager_authorized=True,
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
                CoworkingService.cancel(
                    str(booking.id),
                    self.user.slack_id,
                    office_manager_authorized=True,
                )

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
                office_manager_authorized=True,
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
            office_manager_authorized=True,
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
            office_manager_authorized=True,
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
        self.assertIn("is no longer *Office Manager for 2026-08-03*", update_payload["text"])
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
            winner_channel_announcement_last_error=(
                f"{DELIVERY_LEASE_PREFIX}crashed:"
                f"{(timezone.now() - timedelta(minutes=6)).timestamp():.6f}"
            ),
        )
        CoworkingService.cancel(
            str(result.booking.id),
            self.user.slack_id,
            office_manager_authorized=True,
        )
        fake_client = Mock()
        fake_client.conversations_history.return_value = {
            "ok": True,
            "messages": [
                {
                    "client_msg_id": _winner_channel_client_msg_id(
                        result.assignment
                    ),
                    "ts": "recovered.123",
                }
            ],
        }
        fake_client.chat_update.return_value = {"ok": True}
        with patch(
            "roo.office_manager.SlackService.get_client",
            return_value=fake_client,
        ):
            retracted = OfficeManagerService.retract_winner_channel_announcement(
                result.assignment.id
            )

        self.assertTrue(retracted)
        fake_client.chat_postMessage.assert_not_called()
        fake_client.chat_update.assert_called_once()
        result.assignment.refresh_from_db()
        self.assertEqual(
            result.assignment.winner_channel_message_ts,
            "recovered.123",
        )
        self.assertFalse(result.assignment.winner_channel_retraction_pending)
        self.assertEqual(
            result.assignment.winner_channel_retraction_status,
            "sent",
        )

    @patch("roo.office_manager._local_now")
    def test_retraction_uses_its_own_recovery_budget_after_post_exhaustion(
        self,
        mocked_now,
    ):
        mocked_now.return_value = melbourne_at(2026, 8, 3, 9, 15)
        result = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )
        OfficeManagerAssignment.objects.filter(pk=result.assignment.id).update(
            winner_channel_announcement_status="failed",
            winner_channel_message_ts="",
            winner_channel_announcement_attempt_count=(
                OfficeManagerService.DELIVERY_MAX_ATTEMPTS
            ),
            winner_channel_announcement_last_error=(
                "exhausted:service_unavailable"
            ),
        )
        CoworkingService.cancel(
            str(result.booking.id),
            self.user.slack_id,
            office_manager_authorized=True,
        )
        fake_client = Mock()
        fake_client.conversations_history.return_value = {
            "ok": True,
            "messages": [
                {
                    "client_msg_id": _winner_channel_client_msg_id(
                        result.assignment
                    ),
                    "ts": "recovered.exhausted.123",
                }
            ],
        }
        fake_client.chat_update.return_value = {"ok": True}

        with patch(
            "roo.office_manager.SlackService.get_client",
            return_value=fake_client,
        ):
            retracted = OfficeManagerService.retract_winner_channel_announcement(
                result.assignment.id
            )

        self.assertTrue(retracted)
        fake_client.conversations_history.assert_called_once()
        fake_client.chat_update.assert_called_once()
        result.assignment.refresh_from_db()
        self.assertEqual(
            result.assignment.winner_channel_message_ts,
            "recovered.exhausted.123",
        )
        self.assertEqual(
            result.assignment.winner_channel_retraction_status,
            "sent",
        )

    @patch("roo.office_manager._local_now")
    def test_cancelling_at_cutoff_does_not_reopen_the_role(self, mocked_now):
        mocked_now.return_value = self.now
        result = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )

        mocked_now.return_value = melbourne_at(2026, 8, 3, 10)
        booking, refunded = CoworkingService.cancel(
            str(result.booking.id),
            self.user.slack_id,
            office_manager_authorized=True,
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
            f"space on {self.now.date().isoformat()} books through Roo",
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
        self.assertIn(
            f"Office Manager for {self.now.date().isoformat()}",
            payload["text"],
        )
        self.assertIn("without deducting Roo points", payload["text"])
        self.assertIn(self.now.date().isoformat(), payload["text"])
        self.assertIn(NO_FOOD_REMINDER, payload["text"])
        result.assignment.refresh_from_db()
        self.assertEqual(
            result.assignment.winner_channel_announcement_status,
            "sent",
        )
        self.assertEqual(result.assignment.winner_channel_message_ts, "789.012")

    def test_final_pre_slack_fence_terminalizes_all_prior_date_deliveries(self):
        result = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )
        OfficeManagerDay.objects.filter(pk=self.day.pk).update(
            announcement_status="pending",
            slack_message_ts="",
        )
        next_day = self.now + timedelta(days=1)
        fake_client = Mock()
        fake_client.conversations_open.return_value = {
            "ok": True,
            "channel": {"id": "DWINNER"},
        }

        with (
            patch("roo.office_manager._local_now", return_value=next_day),
            patch(
                "roo.office_manager.SlackService.get_client",
                return_value=fake_client,
            ),
        ):
            self.assertFalse(OfficeManagerService.post_announcement(self.day.pk))
            self.assertFalse(
                OfficeManagerService.deliver_winner_channel_announcement(
                    result.assignment.pk
                )
            )
            self.assertFalse(
                OfficeManagerService.deliver_winner_dm(result.assignment.pk)
            )
            self.assertFalse(
                OfficeManagerService.deliver_end_of_day_reminder(
                    result.assignment.pk
                )
            )

        fake_client.chat_postMessage.assert_not_called()
        self.day.refresh_from_db()
        result.assignment.refresh_from_db()
        self.assertEqual(self.day.announcement_status, "failed")
        self.assertEqual(self.day.announcement_last_error, EXPIRED_DELIVERY_ERROR)
        for status_value, error_value in (
            (
                result.assignment.winner_channel_announcement_status,
                result.assignment.winner_channel_announcement_last_error,
            ),
            (
                result.assignment.winner_dm_status,
                result.assignment.winner_dm_last_error,
            ),
            (
                result.assignment.end_of_day_reminder_status,
                result.assignment.end_of_day_reminder_last_error,
            ),
        ):
            self.assertEqual(status_value, "failed")
            self.assertEqual(error_value, EXPIRED_DELIVERY_ERROR)

    def test_transient_slack_failures_remain_recoverable_after_cancellation(self):
        result = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )
        response = Mock(status_code=500)
        response.get.side_effect = lambda key, default=None: (
            "internal_error" if key == "error" else default
        )
        fake_client = Mock()
        fake_client.conversations_open.return_value = {
            "ok": True,
            "channel": {"id": "DWINNER"},
        }
        fake_client.chat_postMessage.side_effect = SlackApiError(
            "accepted response unknown",
            response,
        )

        with patch(
            "roo.office_manager.SlackService.get_client",
            return_value=fake_client,
        ):
            self.assertFalse(
                OfficeManagerService.deliver_winner_channel_announcement(
                    result.assignment.pk
                )
            )
            self.assertFalse(
                OfficeManagerService.deliver_winner_dm(result.assignment.pk)
            )

        result.assignment.refresh_from_db()
        self.assertEqual(
            result.assignment.winner_channel_announcement_status,
            "unknown",
        )
        self.assertEqual(result.assignment.winner_dm_status, "unknown")

        CoworkingService.cancel(
            str(result.booking.pk),
            self.user.slack_id,
            office_manager_authorized=True,
        )
        result.assignment.refresh_from_db()
        self.assertTrue(result.assignment.winner_channel_retraction_pending)
        self.assertTrue(result.assignment.private_correction_pending)

    def test_exhausted_uncertain_private_delivery_is_corrected_after_cancellation(
        self,
    ):
        result = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )
        OfficeManagerAssignment.objects.filter(pk=result.assignment.pk).update(
            winner_dm_status="failed",
            winner_dm_attempt_count=OfficeManagerService.DELIVERY_MAX_ATTEMPTS,
            winner_dm_last_error="exhausted:worker_lease_expired",
        )

        CoworkingService.cancel(
            str(result.booking.pk),
            self.user.slack_id,
            office_manager_authorized=True,
        )
        result.assignment.refresh_from_db()
        self.assertTrue(result.assignment.private_correction_pending)

        fake_client = Mock()
        fake_client.conversations_open.return_value = {
            "ok": True,
            "channel": {"id": "DWINNER"},
        }
        fake_client.conversations_history.return_value = {
            "ok": True,
            "messages": [
                {
                    "client_msg_id": _slack_client_msg_id(
                        "winner-dm",
                        result.assignment.pk,
                    ),
                    "ts": "stale.final.123",
                }
            ],
            "response_metadata": {"next_cursor": ""},
        }
        fake_client.chat_update.return_value = {"ok": True}

        with patch(
            "roo.office_manager.SlackService.get_client",
            return_value=fake_client,
        ):
            self.assertTrue(
                OfficeManagerService.deliver_private_correction(
                    result.assignment.pk
                )
            )

        fake_client.chat_update.assert_called_once_with(
            channel="DWINNER",
            ts="stale.final.123",
            text=(
                "Your Office Manager assignment for 2026-08-03 has been "
                "cancelled. Please ignore any earlier winner or end-of-day "
                "message for that date."
            ),
        )

    def test_reopening_day_supersedes_losing_attempts(self):
        winning = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            attempt_id=uuid.uuid4(),
            now=self.now,
        )
        other = User.objects.create_user(
            email="losing-volunteer@example.com",
            slack_id="ULOSINGVOLUNTEER",
        )
        losing_attempt_id = uuid.uuid4()
        with self.assertRaisesMessage(
            OfficeManagerClaimError,
            "Another member has already been selected",
        ):
            OfficeManagerService.claim(
                slack_user_id=other.slack_id,
                booking_date=self.now.date(),
                attempt_id=losing_attempt_id,
                now=self.now,
            )

        CoworkingService.cancel(
            str(winning.booking.pk),
            self.user.slack_id,
            office_manager_authorized=True,
        )

        with self.assertRaises(OfficeManagerClaimError) as raised:
            OfficeManagerService.claim(
                slack_user_id=other.slack_id,
                booking_date=self.now.date(),
                attempt_id=losing_attempt_id,
                now=self.now,
            )
        self.assertEqual(raised.exception.code, "attempt_superseded")

    def test_late_terminal_attempt_insert_after_cancellation_is_superseded(self):
        winning = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            attempt_id=uuid.uuid4(),
            now=self.now,
        )
        other = User.objects.create_user(
            email="late-losing-volunteer@example.com",
            slack_id="ULATELOSING",
        )
        late_attempt_id = uuid.uuid4()

        def cancel_then_report_loser(**_kwargs):
            CoworkingService.cancel(
                str(winning.booking.pk),
                self.user.slack_id,
                office_manager_authorized=True,
            )
            raise OfficeManagerClaimError(
                "already_claimed",
                "Another member has already been selected",
                assignee_slack_user_id=self.user.slack_id,
            )

        with (
            patch.object(
                OfficeManagerService,
                "_claim_new_attempt",
                side_effect=cancel_then_report_loser,
            ),
            self.assertRaises(OfficeManagerClaimError) as raised,
        ):
            OfficeManagerService.claim(
                slack_user_id=other.slack_id,
                booking_date=self.now.date(),
                attempt_id=late_attempt_id,
                now=self.now,
            )

        self.assertEqual(raised.exception.code, "attempt_superseded")
        self.assertEqual(
            OfficeManagerClaimAttempt.objects.get(pk=late_attempt_id).outcome,
            "attempt_superseded",
        )

    def test_duplicate_public_winner_post_stays_recoverable(self):
        result = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )
        response = Mock(status_code=200)
        response.get.side_effect = lambda key, default=None: (
            "duplicate_message" if key == "error" else default
        )
        fake_client = Mock()
        fake_client.chat_postMessage.side_effect = SlackApiError(
            "duplicate",
            response,
        )

        with patch(
            "roo.office_manager.SlackService.get_client",
            return_value=fake_client,
        ):
            delivered = (
                OfficeManagerService.deliver_winner_channel_announcement(
                    result.assignment.id
                )
            )

        self.assertFalse(delivered)
        result.assignment.refresh_from_db()
        self.assertEqual(
            result.assignment.winner_channel_announcement_status,
            "unknown",
        )
        self.assertEqual(
            result.assignment.winner_channel_announcement_last_error,
            "duplicate_message",
        )
        self.assertIsNotNone(
            result.assignment.winner_channel_announcement_next_attempt_at
        )

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
        self.assertIn(self.now.date().isoformat(), fallback_text)
        self.assertIn(self.now.date().isoformat(), blocks_text)
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

    def test_duplicate_winner_dm_response_is_authoritative_success(self):
        result = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )
        response = Mock(status_code=200)
        response.get.side_effect = lambda key, default=None: (
            "duplicate_message" if key == "error" else default
        )
        fake_client = Mock()
        fake_client.conversations_open.return_value = {
            "ok": True,
            "channel": {"id": "DWINNER"},
        }
        fake_client.chat_postMessage.side_effect = SlackApiError(
            "duplicate",
            response,
        )

        with patch(
            "roo.office_manager.SlackService.get_client",
            return_value=fake_client,
        ):
            delivered = OfficeManagerService.deliver_winner_dm(
                result.assignment.id
            )

        self.assertTrue(delivered)
        result.assignment.refresh_from_db()
        self.assertEqual(result.assignment.winner_dm_status, "sent")
        self.assertEqual(result.assignment.winner_dm_last_error, "")

    def test_duplicate_end_of_day_dm_response_is_authoritative_success(self):
        result = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )
        response = Mock(status_code=200)
        response.get.side_effect = lambda key, default=None: (
            "duplicate_message" if key == "error" else default
        )
        fake_client = Mock()
        fake_client.conversations_open.return_value = {
            "ok": True,
            "channel": {"id": "DWINNER"},
        }
        fake_client.chat_postMessage.side_effect = SlackApiError(
            "duplicate",
            response,
        )

        with patch(
            "roo.office_manager.SlackService.get_client",
            return_value=fake_client,
        ):
            delivered = OfficeManagerService.deliver_end_of_day_reminder(
                result.assignment.id
            )

        self.assertTrue(delivered)
        result.assignment.refresh_from_db()
        self.assertEqual(result.assignment.end_of_day_reminder_status, "sent")
        self.assertEqual(result.assignment.end_of_day_reminder_last_error, "")

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

    @patch("roo.office_manager._local_now")
    def test_cancellation_during_winner_dm_send_corrects_stale_message(
        self,
        mocked_now,
    ):
        mocked_now.return_value = self.now
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
        fake_client.conversations_history.return_value = {
            "ok": True,
            "messages": [],
            "response_metadata": {"next_cursor": ""},
        }

        def cancel_after_slack_accepts(**_kwargs):
            CoworkingService.cancel(
                str(result.booking.id),
                self.user.slack_id,
                office_manager_authorized=True,
            )
            return {"ok": True, "ts": "stale.123"}

        fake_client.chat_postMessage.side_effect = cancel_after_slack_accepts
        fake_client.chat_update.return_value = {"ok": True}
        with patch(
            "roo.office_manager.SlackService.get_client",
            return_value=fake_client,
        ):
            delivered = OfficeManagerService.deliver_winner_dm(
                result.assignment.id
            )

        self.assertTrue(delivered)
        fake_client.chat_update.assert_called_once_with(
            channel="DWINNER",
            ts="stale.123",
            text=(
                "Your Office Manager assignment for 2026-08-03 has been "
                "cancelled. Please ignore any earlier winner or end-of-day "
                "message for that date."
            ),
        )
        result.assignment.refresh_from_db()
        self.assertEqual(result.assignment.status, "relinquished")
        self.assertFalse(result.assignment.private_correction_pending)
        self.assertEqual(result.assignment.private_correction_status, "sent")

    def test_private_correction_skips_deleted_message_and_updates_survivor(self):
        result = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )
        OfficeManagerAssignment.objects.filter(pk=result.assignment.pk).update(
            status="relinquished",
            winner_dm_status="sent",
            winner_dm_message_ts="deleted.1",
            end_of_day_reminder_status="sent",
            end_of_day_reminder_message_ts="live.2",
            private_correction_pending=True,
            private_correction_status="pending",
        )
        missing_response = Mock(status_code=404)
        missing_response.get.side_effect = lambda key, default=None: (
            "message_not_found" if key == "error" else default
        )
        fake_client = Mock()
        fake_client.conversations_open.return_value = {
            "ok": True,
            "channel": {"id": "DWINNER"},
        }
        fake_client.conversations_history.return_value = {
            "ok": True,
            "messages": [],
        }
        fake_client.chat_update.side_effect = [
            SlackApiError("deleted", missing_response),
            {"ok": True},
        ]

        with patch(
            "roo.office_manager.SlackService.get_client",
            return_value=fake_client,
        ):
            delivered = OfficeManagerService.deliver_private_correction(
                result.assignment.pk
            )

        self.assertTrue(delivered)
        self.assertEqual(fake_client.chat_update.call_count, 2)
        self.assertEqual(
            [call.kwargs["ts"] for call in fake_client.chat_update.call_args_list],
            ["deleted.1", "live.2"],
        )
        result.assignment.refresh_from_db()
        self.assertFalse(result.assignment.private_correction_pending)
        self.assertEqual(result.assignment.private_correction_status, "sent")

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
        ) as update_message:
            updated = OfficeManagerService.reconcile_message(
                result.assignment.day_id
            )

        self.assertTrue(updated)
        result.assignment.day.refresh_from_db()
        self.assertEqual(result.assignment.day.status, "open")
        self.assertFalse(result.assignment.day.message_update_pending)
        self.assertEqual(result.assignment.day.announcement_last_error, "")
        self.assertEqual(update_message.call_count, 2)
        self.assertIn(
            f"Volunteer to be Office Manager for {self.now.date().isoformat()}",
            update_message.call_args.args[2],
        )

    def test_reconcile_marks_permanent_slack_failure_terminal(self):
        result = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )
        response = Mock(status_code=400)
        response.get.side_effect = lambda key, default=None: (
            "channel_not_found" if key == "error" else default
        )

        with patch(
            "roo.office_manager.SlackService.update_message",
            side_effect=SlackApiError("chat.update failed", response),
        ):
            updated = OfficeManagerService.reconcile_message(
                result.assignment.day_id
            )

        self.assertFalse(updated)
        result.assignment.day.refresh_from_db()
        self.assertFalse(result.assignment.day.message_update_pending)
        self.assertEqual(
            result.assignment.day.announcement_last_error,
            "permanent:message_update:channel_not_found",
        )

    def test_reconcile_transient_failure_respects_backoff(self):
        result = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )

        with patch(
            "roo.office_manager.SlackService.update_message",
            side_effect=TimeoutError("Slack timed out"),
        ) as update_message:
            first = OfficeManagerService.reconcile_message(
                result.assignment.day_id
            )
            second = OfficeManagerService.reconcile_message(
                result.assignment.day_id
            )

        self.assertFalse(first)
        self.assertFalse(second)
        update_message.assert_called_once()
        result.assignment.day.refresh_from_db()
        self.assertTrue(result.assignment.day.message_update_pending)
        self.assertGreater(
            result.assignment.day.announcement_next_attempt_at,
            timezone.now(),
        )

    def test_reconcile_exhausts_transient_slack_failures(self):
        result = OfficeManagerService.claim(
            slack_user_id=self.user.slack_id,
            booking_date=self.now.date(),
            now=self.now,
        )

        with patch(
            "roo.office_manager.SlackService.update_message",
            side_effect=TimeoutError("Slack timed out"),
        ):
            outcomes = []
            for _ in range(OfficeManagerService.DELIVERY_MAX_ATTEMPTS):
                outcomes.append(
                    OfficeManagerService.reconcile_message(
                        result.assignment.day_id
                    )
                )
                OfficeManagerDay.objects.filter(
                    pk=result.assignment.day_id,
                    message_update_pending=True,
                ).update(
                    announcement_next_attempt_at=(
                        timezone.now() - timedelta(seconds=1)
                    )
                )

        self.assertEqual(outcomes, [False] * 5)
        result.assignment.day.refresh_from_db()
        self.assertFalse(result.assignment.day.message_update_pending)
        self.assertEqual(
            result.assignment.day.announcement_last_error,
            "exhausted:message_update:TimeoutError",
        )


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
        fake_client.conversations_history.return_value = {
            "ok": True,
            "messages": [],
            "response_metadata": {"next_cursor": ""},
        }
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
        self.assertIn(f"Volunteer for {now.date().isoformat()}", blocks_text)
        self.assertIn("No channel or thread reply is needed", blocks_text)
        self.assertIn(now.date().isoformat(), payload["text"])
        self.assertIn(now.date().isoformat(), blocks_text)
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
            {"date": "2026-08-03", "generation": 1},
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

    @override_settings(
        OFFICE_MANAGER_ENABLED=False,
        OFFICE_MANAGER_SLACK_BOT_TOKEN="",
    )
    def test_disabled_scheduler_without_backlog_does_not_require_slack_token(self):
        with patch(
            "roo.office_manager.SlackService.get_client"
        ) as get_client:
            result = run_office_manager_scheduler(
                now=melbourne_at(2026, 8, 3, 8, 30)
            )

        self.assertEqual(
            result,
            {"status": "skipped", "reason": "disabled"},
        )
        get_client.assert_not_called()
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
            first = run_office_manager_scheduler(now=now)
            OfficeManagerDay.objects.filter(pk=day.pk).update(
                announcement_next_attempt_at=timezone.now() - timedelta(seconds=1)
            )
            result = run_office_manager_scheduler(now=now)

        self.assertFalse(first["announcement_sent"])
        self.assertTrue(result["announcement_sent"])
        day.refresh_from_db()
        self.assertEqual(day.announcement_status, "sent")
        fake_client.chat_postMessage.assert_called_once()

    def test_announcement_coordinate_history_missing_scope_is_terminal(self):
        now = melbourne_at(2026, 8, 3, 8, 30)
        day = office_manager_day(now.date())
        OfficeManagerDay.objects.filter(pk=day.pk).update(
            announcement_status="unknown",
            slack_message_ts="",
            announcement_next_attempt_at=timezone.now() - timedelta(seconds=1),
            updated_at=timezone.now() - timedelta(minutes=6),
        )
        response = Mock(status_code=403)
        response.get.side_effect = lambda key, default=None: (
            "missing_scope" if key == "error" else default
        )
        fake_client = Mock()
        fake_client.conversations_history.side_effect = SlackApiError(
            "missing scope",
            response,
        )

        with patch(
            "roo.office_manager.SlackService.get_client",
            return_value=fake_client,
        ):
            self.assertFalse(
                OfficeManagerService.recover_announcement_coordinates(day.pk)
            )

        day.refresh_from_db()
        self.assertEqual(day.announcement_attempt_count, 1)
        self.assertEqual(day.announcement_status, "failed")
        self.assertEqual(day.announcement_last_error, "permanent:missing_scope")

    def test_winner_coordinate_history_5xx_exhausts_bounded_budget(self):
        now = melbourne_at(2026, 8, 3, 8, 45)
        day = office_manager_day(now.date())
        user = User.objects.create_user(
            email="winner-coordinate@example.com",
            slack_id="UWINNERCOORDINATE",
        )
        result = OfficeManagerService.claim(
            slack_user_id=user.slack_id,
            booking_date=now.date(),
            now=now,
        )
        OfficeManagerAssignment.objects.filter(pk=result.assignment.pk).update(
            winner_channel_announcement_status="unknown",
            winner_channel_announcement_next_attempt_at=(
                timezone.now() - timedelta(seconds=1)
            ),
        )
        response = Mock(status_code=503)
        response.get.side_effect = lambda key, default=None: (
            "service_unavailable" if key == "error" else default
        )
        fake_client = Mock()
        fake_client.conversations_history.side_effect = SlackApiError(
            "history unavailable",
            response,
        )

        with patch(
            "roo.office_manager.SlackService.get_client",
            return_value=fake_client,
        ):
            for attempt in range(OfficeManagerService.DELIVERY_MAX_ATTEMPTS):
                self.assertFalse(
                    OfficeManagerService.recover_winner_channel_coordinates(
                        result.assignment.pk
                    )
                )
                if attempt + 1 < OfficeManagerService.DELIVERY_MAX_ATTEMPTS:
                    OfficeManagerAssignment.objects.filter(
                        pk=result.assignment.pk
                    ).update(
                        winner_channel_announcement_next_attempt_at=(
                            timezone.now() - timedelta(seconds=1)
                        )
                    )

        result.assignment.refresh_from_db()
        self.assertEqual(
            result.assignment.winner_channel_announcement_attempt_count,
            OfficeManagerService.DELIVERY_MAX_ATTEMPTS,
        )
        self.assertEqual(
            result.assignment.winner_channel_announcement_status,
            "failed",
        )
        self.assertEqual(
            result.assignment.winner_channel_announcement_last_error,
            "exhausted:service_unavailable",
        )

    def test_announcement_coordinate_history_429_exhausts_bounded_budget(self):
        now = melbourne_at(2026, 8, 3, 8, 30)
        day = office_manager_day(now.date())
        OfficeManagerDay.objects.filter(pk=day.pk).update(
            announcement_status="unknown",
            slack_message_ts="",
            announcement_next_attempt_at=timezone.now() - timedelta(seconds=1),
        )
        response = Mock(status_code=429)
        response.get.side_effect = lambda key, default=None: (
            "ratelimited" if key == "error" else default
        )
        fake_client = Mock()
        fake_client.conversations_history.side_effect = SlackApiError(
            "rate limited",
            response,
        )
        with patch(
            "roo.office_manager.SlackService.get_client",
            return_value=fake_client,
        ):
            for attempt in range(OfficeManagerService.DELIVERY_MAX_ATTEMPTS):
                self.assertFalse(
                    OfficeManagerService.recover_announcement_coordinates(day.pk)
                )
                if attempt + 1 < OfficeManagerService.DELIVERY_MAX_ATTEMPTS:
                    OfficeManagerDay.objects.filter(pk=day.pk).update(
                        announcement_next_attempt_at=(
                            timezone.now() - timedelta(seconds=1)
                        )
                    )

        day.refresh_from_db()
        self.assertEqual(
            day.announcement_attempt_count,
            OfficeManagerService.DELIVERY_MAX_ATTEMPTS,
        )
        self.assertEqual(day.announcement_status, "failed")
        self.assertEqual(day.announcement_last_error, "exhausted:ratelimited")

    def test_expired_final_delivery_lease_checks_coordinates_then_dead_letters(self):
        now = melbourne_at(2026, 8, 3, 8, 30)
        day = office_manager_day(now.date())
        OfficeManagerDay.objects.filter(pk=day.pk).update(
            announcement_status="sending",
            slack_message_ts="",
            announcement_attempt_count=OfficeManagerService.DELIVERY_MAX_ATTEMPTS,
            announcement_last_error=(
                f"{DELIVERY_LEASE_PREFIX}dead:"
                f"{(timezone.now() - timedelta(minutes=6)).timestamp():.6f}"
            ),
            updated_at=timezone.now() - timedelta(minutes=6),
        )

        fake_client = Mock()
        fake_client.conversations_history.return_value = {
            "ok": True,
            "messages": [],
            "response_metadata": {"next_cursor": ""},
        }
        with patch(
            "roo.office_manager.SlackService.get_client",
            return_value=fake_client,
        ):
            result = run_office_manager_scheduler(now=now)

        day.refresh_from_db()
        self.assertEqual(day.announcement_status, "failed")
        self.assertEqual(
            day.announcement_last_error,
            "exhausted:coordinate_recovery:not_found",
        )
        self.assertEqual(
            result["delivery_failures"]["announcement_dead_letters"][0][
                "day_id"
            ],
            day.pk,
        )
        fake_client.conversations_history.assert_called_once()
        fake_client.chat_postMessage.assert_not_called()

    def test_expired_final_delivery_lease_recovers_accepted_message(self):
        now = melbourne_at(2026, 8, 3, 8, 30)
        day = office_manager_day(now.date())
        OfficeManagerDay.objects.filter(pk=day.pk).update(
            announcement_status="sending",
            slack_message_ts="",
            announcement_attempt_count=OfficeManagerService.DELIVERY_MAX_ATTEMPTS,
            announcement_last_error=(
                f"{DELIVERY_LEASE_PREFIX}dead:"
                f"{(timezone.now() - timedelta(minutes=6)).timestamp():.6f}"
            ),
            updated_at=timezone.now() - timedelta(minutes=6),
        )
        fake_client = Mock()
        fake_client.conversations_history.return_value = {
            "ok": True,
            "messages": [
                {
                    "client_msg_id": _slack_client_msg_id("daily", day.pk),
                    "ts": "accepted.final.123",
                }
            ],
            "response_metadata": {"next_cursor": ""},
        }

        with patch(
            "roo.office_manager.SlackService.get_client",
            return_value=fake_client,
        ):
            result = run_office_manager_scheduler(now=now)

        day.refresh_from_db()
        self.assertTrue(result["announcement_sent"])
        self.assertEqual(day.announcement_status, "sent")
        self.assertEqual(day.slack_message_ts, "accepted.final.123")
        fake_client.chat_postMessage.assert_not_called()

    def test_expired_final_retraction_lease_becomes_visible_dead_letter(self):
        now = melbourne_at(2026, 8, 3, 9)
        day = office_manager_day(now.date())
        user = User.objects.create_user(
            email="crashed-retraction@example.com",
            slack_id="UCRASHEDRETRACTION",
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
            winner_channel_retraction_pending=True,
            winner_channel_retraction_status="sending",
            winner_channel_retraction_attempt_count=(
                OfficeManagerService.RETRACTION_MAX_ATTEMPTS
            ),
            winner_channel_retraction_lease_token="dead-worker",
        )
        OfficeManagerAssignment.objects.filter(pk=assignment.pk).update(
            updated_at=timezone.now() - timedelta(minutes=6)
        )

        result = run_office_manager_scheduler(now=now)

        assignment.refresh_from_db()
        self.assertEqual(
            assignment.winner_channel_retraction_status,
            "exhausted",
        )
        self.assertFalse(assignment.winner_channel_retraction_pending)
        self.assertEqual(
            result["delivery_failures"]
            ["winner_channel_retraction_dead_letters"][0]["id"],
            assignment.pk,
        )

    def test_scheduler_recovers_daily_post_after_slack_response_loss(self):
        now = melbourne_at(2026, 8, 3, 8, 30)
        day = office_manager_day(now.date())
        OfficeManagerDay.objects.filter(pk=day.pk).update(
            announcement_status="unknown",
            slack_message_ts="",
            announcement_next_attempt_at=timezone.now() - timedelta(seconds=1),
            updated_at=timezone.now() - timedelta(minutes=6),
        )
        fake_client = Mock()
        fake_client.conversations_history.return_value = {
            "ok": True,
            "messages": [
                {
                    "client_msg_id": _slack_client_msg_id("daily", day.id),
                    "ts": "recovered.daily.123",
                }
            ],
        }

        with patch(
            "roo.office_manager.SlackService.get_client",
            return_value=fake_client,
        ):
            result = run_office_manager_scheduler(now=now)

        self.assertTrue(result["announcement_sent"])
        fake_client.chat_postMessage.assert_not_called()
        day.refresh_from_db()
        self.assertEqual(day.announcement_status, "sent")
        self.assertEqual(day.slack_message_ts, "recovered.daily.123")

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

        winner_channel.assert_called_once_with(assignment.id, now=now)
        winner_dm.assert_called_once_with(assignment.id, now=now)
        end_reminder.assert_called_once_with(assignment.id, now=now)

    def test_scheduler_skips_live_delivery_leases_without_reporting_failure(self):
        now = melbourne_at(2026, 8, 3, 16, 30)
        day = office_manager_day(now.date(), status_value="claimed")
        user = User.objects.create_user(
            email="live-delivery@example.com",
            slack_id="ULIVEDELIVERY",
        )
        booking = CoworkingBooking.objects.create(
            user=user,
            date=now.date(),
            points_cost=0,
            booking_source="office_manager",
        )
        lease_token = (
            f"{DELIVERY_LEASE_PREFIX}live:"
            f"{timezone.now().timestamp():.6f}"
        )
        assignment = OfficeManagerAssignment.objects.create(
            day=day,
            user=user,
            booking=booking,
            winner_channel_announcement_status="sending",
            winner_channel_announcement_last_error=lease_token,
            winner_dm_status="sending",
            winner_dm_last_error=lease_token,
            end_of_day_reminder_status="sending",
            end_of_day_reminder_last_error=lease_token,
            winner_channel_retraction_pending=True,
            winner_channel_retraction_status="sending",
            winner_channel_retraction_lease_token="live-retraction",
        )

        with (
            patch.object(
                OfficeManagerService,
                "deliver_winner_channel_announcement",
            ) as winner_channel,
            patch.object(
                OfficeManagerService,
                "deliver_winner_dm",
            ) as winner_dm,
            patch.object(
                OfficeManagerService,
                "deliver_end_of_day_reminder",
            ) as end_reminder,
            patch.object(
                OfficeManagerService,
                "retract_winner_channel_announcement",
            ) as retraction,
        ):
            result = run_office_manager_scheduler(now=now)

        winner_channel.assert_not_called()
        winner_dm.assert_not_called()
        end_reminder.assert_not_called()
        retraction.assert_not_called()
        self.assertNotIn("recovered_deliveries", result)
        self.assertNotIn("winner_channel_retractions", result)
        assignment.refresh_from_db()
        self.assertEqual(assignment.winner_dm_status, "sending")

    def test_scheduler_skips_live_daily_announcement_lease(self):
        now = melbourne_at(2026, 8, 3, 9, 0)
        lease_token = (
            f"{DELIVERY_LEASE_PREFIX}live:"
            f"{timezone.now().timestamp():.6f}"
        )
        day = OfficeManagerDay.objects.create(
            date=now.date(),
            status="open",
            slack_channel_id="CCOWORK",
            claim_cutoff_at=melbourne_at(2026, 8, 3, 10),
            announcement_status="sending",
            announcement_last_error=lease_token,
        )

        with patch.object(
            OfficeManagerService,
            "post_announcement",
        ) as post_announcement:
            result = run_office_manager_scheduler(now=now)

        post_announcement.assert_not_called()
        self.assertNotIn("recovered_deliveries", result)
        day.refresh_from_db()
        self.assertEqual(day.announcement_status, "sending")

    def test_retry_sweep_skips_older_live_row_and_processes_due_row(self):
        now = melbourne_at(2026, 8, 3, 16, 30)
        live_day = office_manager_day(
            now.date() - timedelta(days=1),
            status_value="claimed",
        )
        due_day = office_manager_day(now.date(), status_value="claimed")
        live_user = User.objects.create_user(
            email="live-first@example.com",
            slack_id="ULIVEFIRST",
        )
        due_user = User.objects.create_user(
            email="due-second@example.com",
            slack_id="UDUESECOND",
        )
        live_booking = CoworkingBooking.objects.create(
            user=live_user,
            date=live_day.date,
            points_cost=0,
            booking_source="office_manager",
        )
        due_booking = CoworkingBooking.objects.create(
            user=due_user,
            date=due_day.date,
            points_cost=0,
            booking_source="office_manager",
        )
        lease = (
            f"{DELIVERY_LEASE_PREFIX}live:"
            f"{timezone.now().timestamp():.6f}"
        )
        OfficeManagerAssignment.objects.create(
            day=live_day,
            user=live_user,
            booking=live_booking,
            winner_channel_announcement_status="sending",
            winner_channel_announcement_last_error=lease,
            winner_dm_status="sending",
            winner_dm_last_error=lease,
            end_of_day_reminder_status="sending",
            end_of_day_reminder_last_error=lease,
        )
        due_assignment = OfficeManagerAssignment.objects.create(
            day=due_day,
            user=due_user,
            booking=due_booking,
            winner_channel_announcement_status="sent",
            winner_dm_status="pending",
            end_of_day_reminder_status="sent",
        )

        with patch.object(
            OfficeManagerService,
            "deliver_winner_dm",
            return_value=True,
        ) as deliver:
            recovered = OfficeManagerService.retry_pending_deliveries(
                now=now,
                limit=1,
            )

        deliver.assert_called_once_with(due_assignment.id, now=now)
        self.assertEqual(recovered["winner_dm"], {due_assignment.id: True})

    def test_scheduler_surfaces_announcement_and_assignment_dead_letters(self):
        now = melbourne_at(2026, 8, 3, 16, 30)
        day = office_manager_day(now.date(), status_value="claimed")
        OfficeManagerDay.objects.filter(pk=day.pk).update(
            announcement_status="failed",
            announcement_last_error="permanent:channel_not_found",
        )
        user = User.objects.create_user(
            email="dead-letter@example.com",
            slack_id="UDEADLETTER",
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
            winner_channel_announcement_status="failed",
            winner_channel_announcement_last_error=(
                "permanent:channel_not_found"
            ),
            winner_dm_status="sent",
            end_of_day_reminder_status="sent",
        )

        result = run_office_manager_scheduler(now=now)

        failures = result["delivery_failures"]
        self.assertEqual(
            failures["announcement_dead_letters"][0]["day_id"],
            day.id,
        )
        self.assertEqual(
            failures["assignment_delivery_dead_letters"][0][
                "assignment_id"
            ],
            assignment.id,
        )

    @override_settings(OFFICE_MANAGER_ENABLED=False)
    def test_disabled_scheduler_reconciles_cancelled_public_message(self):
        now = melbourne_at(2026, 8, 3, 12)
        day = office_manager_day(now.date(), status_value="open")
        OfficeManagerDay.objects.filter(pk=day.pk).update(
            announcement_status="sent",
            slack_message_ts="123.456",
            message_update_pending=True,
        )

        with patch.object(
            OfficeManagerService,
            "reconcile_message",
            return_value=True,
        ) as reconcile:
            result = run_office_manager_scheduler(now=now)

        reconcile.assert_called_once_with(day.id)
        self.assertEqual(
            result["recovered_deliveries"]["message_update"],
            {day.id: True},
        )
        self.assertEqual(result["reason"], "disabled")

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
            get_client.return_value.conversations_history.return_value = {
                "ok": True,
                "messages": [],
            }
            result = run_office_manager_scheduler(now=now)

        self.assertEqual(result["reason"], "before_announcement")
        get_client.return_value.chat_postMessage.assert_not_called()
        get_client.return_value.chat_update.assert_not_called()
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

    def test_scheduler_recovers_prior_date_winner_coordinates_before_expiry(self):
        now = melbourne_at(2026, 8, 3, 8, 0)
        prior_date = now.date() - timedelta(days=1)
        day = office_manager_day(prior_date, status_value="claimed")
        user = User.objects.create_user(
            email="prior-response-loss@example.com",
            slack_id="UPRIORRESPONSELOSS",
        )
        booking = CoworkingBooking.objects.create(
            user=user,
            date=prior_date,
            points_cost=0,
            booking_source="office_manager",
        )
        attempt = OfficeManagerClaimAttempt.objects.create(
            attempt_id=uuid.uuid4(),
            slack_user_id=user.slack_id,
            booking_date=prior_date,
            outcome="claimed",
        )
        assignment = OfficeManagerAssignment.objects.create(
            day=day,
            user=user,
            booking=booking,
            winner_channel_announcement_status="unknown",
            winner_dm_status="sent",
            end_of_day_reminder_status="sent",
        )
        attempt.assignment = assignment
        attempt.save(update_fields=["assignment"])
        fake_client = Mock()
        fake_client.conversations_history.return_value = {
            "ok": True,
            "messages": [
                {
                    "client_msg_id": _slack_client_msg_id(
                        "winner", attempt.attempt_id
                    ),
                    "ts": "recovered.prior.123",
                }
            ],
        }

        with patch(
            "roo.office_manager.SlackService.get_client",
            return_value=fake_client,
        ):
            result = run_office_manager_scheduler(now=now)

        self.assertEqual(result["reason"], "before_announcement")
        assignment.refresh_from_db()
        self.assertEqual(
            assignment.winner_channel_announcement_status,
            "sent",
        )
        self.assertEqual(
            assignment.winner_channel_message_ts,
            "recovered.prior.123",
        )
        fake_client.chat_postMessage.assert_not_called()

    def test_committed_delivery_recovery_continues_while_disabled(self):
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
        ), override_settings(OFFICE_MANAGER_ENABLED=False):
            disabled_result = run_office_manager_scheduler(now=now)
            rollover_result = run_office_manager_scheduler(
                now=now + timedelta(days=1)
            )

        self.assertEqual(disabled_result["reason"], "disabled")
        self.assertEqual(rollover_result["reason"], "disabled")
        self.assertTrue(
            disabled_result["recovered_deliveries"]["winner_channel"][
                assignment.id
            ]
        )
        self.assertTrue(
            disabled_result["recovered_deliveries"]["winner_dm"][
                assignment.id
            ]
        )
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

    def test_stale_announcement_retry_recovers_by_slack_client_message_id(self):
        now = melbourne_at(2026, 8, 3, 8, 30)
        day = office_manager_day(now.date())
        OfficeManagerDay.objects.filter(pk=day.pk).update(
            announcement_status="sending",
            slack_message_ts="",
            updated_at=timezone.now() - timedelta(minutes=6),
        )
        fake_client = Mock()
        fake_client.conversations_history.return_value = {
            "ok": True,
            "messages": [],
            "response_metadata": {"next_cursor": ""},
        }
        fake_client.chat_postMessage.return_value = {
            "ok": True,
            "ts": "123.456",
        }

        with patch(
            "roo.office_manager.SlackService.get_client",
            return_value=fake_client,
        ):
            self.assertFalse(
                OfficeManagerService.post_announcement(day.id, now=now)
            )
            OfficeManagerDay.objects.filter(pk=day.pk).update(
                announcement_next_attempt_at=timezone.now() - timedelta(seconds=1),
            )
            self.assertTrue(
                OfficeManagerService.post_announcement(day.id, now=now)
            )
            fake_client.conversations_history.return_value = {
                "ok": True,
                "messages": [
                    {
                        "client_msg_id": _slack_client_msg_id("daily", day.id),
                        "ts": "123.456",
                    }
                ],
                "response_metadata": {"next_cursor": ""},
            }
            OfficeManagerDay.objects.filter(pk=day.pk).update(
                announcement_status="unknown",
                slack_message_ts="",
                announcement_next_attempt_at=timezone.now() - timedelta(seconds=1),
                updated_at=timezone.now() - timedelta(minutes=6),
            )
            self.assertTrue(
                OfficeManagerService.post_announcement(day.id, now=now)
            )

        self.assertEqual(fake_client.chat_postMessage.call_count, 1)
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

        self.assertIn(
            f"Office Manager for {day.date.isoformat()}: A member",
            text,
        )
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

    def test_scheduler_surfaces_terminal_message_update_failure(self):
        now = melbourne_at(2026, 8, 3, 9)
        day = office_manager_day(now.date())
        OfficeManagerDay.objects.filter(pk=day.pk).update(
            announcement_last_error=(
                "permanent:message_update:channel_not_found"
            ),
            message_update_pending=False,
        )

        result = run_office_manager_scheduler(now=now)

        self.assertEqual(
            result["delivery_failures"]["message_update_dead_letters"],
            [
                {
                    "day_id": day.id,
                    "date": now.date().isoformat(),
                    "error": "permanent:message_update:channel_not_found",
                }
            ],
        )

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
        self.assertIn(now.date().isoformat(), fallback_text)
        self.assertIn(now.date().isoformat(), blocks_text)
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
        deliver_reminder.assert_called_once_with(assignment.id, now=now)

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

    def test_exhausted_retraction_remains_visible_on_every_scheduler_tick(self):
        now = melbourne_at(2026, 8, 3, 9)
        day = office_manager_day(now.date())
        user = User.objects.create_user(
            email="dead-letter-retraction@example.com",
            slack_id="UDEADLETTERRETRACTION",
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
            winner_channel_retraction_pending=False,
            winner_channel_retraction_status="exhausted",
            winner_channel_retraction_attempt_count=5,
            winner_channel_retraction_last_error="message_not_found",
        )

        with patch.object(
            OfficeManagerService,
            "retry_pending_deliveries",
            return_value={
                "announcement": {},
                "winner_channel": {},
                "winner_dm": {},
                "end_of_day": {},
            },
        ):
            first = run_office_manager_scheduler(now=now)
            second = run_office_manager_scheduler(now=now)

        for result in (first, second):
            dead_letters = result["delivery_failures"][
                "winner_channel_retraction_dead_letters"
            ]
            self.assertEqual(dead_letters[0]["id"], assignment.id)
            self.assertEqual(
                dead_letters[0]["winner_channel_retraction_attempt_count"],
                5,
            )

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

    def test_preflight_proves_exact_isolated_roo_contract(self):
        url = reverse("coworking-office-manager-preflight")
        for api_key, expected_status in (
            (None, status.HTTP_401_UNAUTHORIZED),
            ("office-manager-internal-test-key", status.HTTP_401_UNAUTHORIZED),
            ("office-manager-mlai-test-key", status.HTTP_401_UNAUTHORIZED),
            ("office-manager-test-key", status.HTTP_200_OK),
        ):
            with self.subTest(api_key=api_key):
                headers = {} if api_key is None else {"HTTP_X_API_KEY": api_key}
                response = self.client.get(url, **headers)
                self.assertEqual(response.status_code, expected_status)
                if expected_status == status.HTTP_200_OK:
                    self.assertEqual(response.data["status"], "ok")
                    self.assertEqual(
                        response.data["contract"],
                        "office-manager-v1",
                    )
                    self.assertEqual(
                        response.data["credential_scope"],
                        "strict_roo",
                    )
                    self.assertTrue(
                        response.data["claim_generation_supported"]
                    )
                    self.assertTrue(
                        response.data["claim_generation_required"]
                    )
                    self.assertEqual(
                        response.data["timezone"],
                        "Australia/Melbourne",
                    )

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
                        "generation": 1,
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
            ("announcement_superseded", status.HTTP_409_CONFLICT),
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
                        "generation": 1,
                    },
                    format="json",
                    HTTP_X_API_KEY="office-manager-test-key",
                )

                self.assertEqual(response.status_code, expected_status)
                self.assertEqual(response.data["code"], code)
                self.assertEqual(response.data["attempt_id"], self.attempt_id)
                self.assertEqual(response.data["generation"], 1)

    def test_claim_rejects_noncanonical_generation(self):
        for generation in (
            True,
            0,
            -1,
            "01",
            "1",
            "1.0",
            "",
            2**31,
            "9" * 5000,
        ):
            with self.subTest(generation=generation):
                response = self.client.post(
                    self.url,
                    {
                        "slack_user_id": self.user.slack_id,
                        "date": self.now.date(),
                        "attempt_id": self.attempt_id,
                        "generation": generation,
                    },
                    format="json",
                    HTTP_X_API_KEY="office-manager-test-key",
                )
                self.assertEqual(
                    response.status_code,
                    status.HTTP_400_BAD_REQUEST,
                )
                self.assertEqual(response.data["code"], "invalid_request")

    def test_claim_rejects_noncanonical_date(self):
        for booking_date in ("20260803", "2026-W32-1"):
            with self.subTest(booking_date=booking_date):
                response = self.client.post(
                    self.url,
                    {
                        "slack_user_id": self.user.slack_id,
                        "date": booking_date,
                        "attempt_id": self.attempt_id,
                        "generation": 1,
                    },
                    format="json",
                    HTTP_X_API_KEY="office-manager-test-key",
                )
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(response.data["code"], "invalid_request")

    def test_claim_requires_generation(self):
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

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["code"], "invalid_request")
        self.assertIn("generation", response.data["error"])

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

    def test_date_only_cancel_is_rejected_before_booking_lookup(self):
        current = CoworkingBooking.objects.create(
            user=self.user,
            date=self.now.date(),
            status="booked",
            points_cost=0,
        )

        response = self.client.post(
            reverse("coworking-cancel"),
            {
                "slack_user_id": self.user.slack_id,
                "date": self.now.date().isoformat(),
            },
            format="json",
            HTTP_X_API_KEY="office-manager-test-key",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["code"], "booking_identity_required")
        current.refresh_from_db()
        self.assertEqual(current.status, "booked")

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
                "generation": 1,
            },
            format="json",
            HTTP_X_API_KEY="office-manager-test-key",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["points_charged"], 0)
        self.assertEqual(response.data["points_refunded"], 8)
        self.assertTrue(response.data["office_manager_free_day"])
        self.assertEqual(response.data["generation"], 1)
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
                "generation": 1,
            },
            format="json",
            HTTP_X_API_KEY="office-manager-test-key",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "already_claimed_by_you")
        self.assertEqual(response.data["points_refunded"], 4)


class OfficeManagerMigrationAuditInvariantTests(TestCase):
    def test_recovery_migration_schema_has_required_constraints(self):
        from core.management.commands.audit_office_manager_migrations import (
            _office_manager_recovery_schema_issues,
        )

        self.assertEqual(_office_manager_recovery_schema_issues(), [])

    def test_hardening_migration_schema_has_required_constraints(self):
        from core.management.commands.audit_office_manager_migrations import (
            _office_manager_hardening_schema_issues,
        )

        self.assertEqual(_office_manager_hardening_schema_issues(), [])

    def test_audit_rejects_unsuperseded_attempt_from_reopened_generation(self):
        user = User.objects.create_user(
            email="stale-attempt-audit@example.com",
            slack_id="USTALEATTEMPTAUDIT",
        )
        booking_date = melbourne_at(2026, 9, 7, 9).date()
        day = OfficeManagerDay.objects.create(
            date=booking_date,
            status="open",
            generation=2,
            slack_channel_id="CCOWORK",
            claim_cutoff_at=melbourne_at(2026, 9, 7, 10),
            announcement_status="sent",
            slack_message_ts="audit-reopened.123",
        )
        booking = CoworkingBooking.objects.create(
            user=user,
            date=booking_date,
            status="cancelled",
            points_cost=0,
            booking_source="office_manager",
        )
        OfficeManagerAssignment.objects.create(
            day=day,
            user=user,
            booking=booking,
            status="relinquished",
        )
        attempt = OfficeManagerClaimAttempt.objects.create(
            attempt_id=uuid.uuid4(),
            slack_user_id="UOLDLOSER",
            booking_date=booking_date,
            generation=1,
            outcome="already_claimed",
        )
        stdout = io.StringIO()

        with self.assertRaises(CommandError):
            call_command(
                "audit_office_manager_migrations",
                configured_office_manager_channel="CCOWORK",
                stdout=stdout,
            )

        report = json.loads(stdout.getvalue().splitlines()[0])
        self.assertEqual(
            report["data_invariants"]
            ["stale_reopened_office_manager_attempts"],
            [
                {
                    "attempt_id": str(attempt.attempt_id),
                    "date": booking_date.isoformat(),
                    "attempt_generation": 1,
                    "day_generation": 2,
                }
            ],
        )

    def test_reconciliation_command_persists_immutable_operator_evidence(self):
        user = User.objects.create_user(
            email="reconcile-provenance@example.com",
            slack_id="URECONCILEPROVENANCE",
        )
        booking_date = timezone.localdate() + timedelta(days=5)
        PointsService.award(
            user=user,
            delta=8,
            source="MANUAL",
            description="reconciliation setup",
            created_by_slack_id="UADMIN",
            idempotency_key="reconciliation-award",
        )
        booking, _ = CoworkingService.book(
            user=user,
            booking_date=booking_date,
            created_by_slack_id=user.slack_id,
        )
        day = OfficeManagerDay.objects.create(
            date=booking_date,
            status="claimed",
            slack_channel_id="CCOWORK",
            claim_cutoff_at=timezone.now() + timedelta(days=5),
        )
        refund, _ = PointsService.refund(
            user=user,
            delta=8,
            source="COWORKING",
            description="Office Manager booking refund",
            created_by_slack_id=user.slack_id,
            idempotency_key="reconciliation-refund",
            reference_type="OFFICE_MANAGER_ASSIGNMENT",
            reference_id=str(day.pk),
            purchased_delta_microroo=0,
            reverse_lifetime_spent=True,
        )
        booking.original_points_cost = 8
        booking.points_cost = 0
        booking.booking_source = "office_manager"
        booking.purchased_points_cost_microroo = None
        booking.refund_ledger_entry = refund
        booking.save()
        assignment = OfficeManagerAssignment.objects.create(
            day=day,
            user=user,
            booking=booking,
            points_refunded=8,
            purchased_points_refunded_microroo=None,
            refund_ledger_entry=refund,
        )

        dry_run = io.StringIO()
        call_command(
            "reconcile_office_manager_provenance",
            booking_id=str(booking.pk),
            purchased_microroo=0,
            reviewed_by="ops@example.com",
            stdout=dry_run,
        )
        booking.refresh_from_db()
        self.assertIsNone(booking.purchased_points_cost_microroo)
        self.assertFalse(
            OfficeManagerProvenanceReconciliation.objects.filter(
                booking=booking
            ).exists()
        )

        call_command(
            "reconcile_office_manager_provenance",
            booking_id=str(booking.pk),
            purchased_microroo=0,
            reviewed_by="ops@example.com",
            commit=True,
            stdout=io.StringIO(),
        )

        booking.refresh_from_db()
        assignment.refresh_from_db()
        evidence = OfficeManagerProvenanceReconciliation.objects.get(
            booking=booking
        )
        self.assertEqual(booking.purchased_points_cost_microroo, 0)
        self.assertEqual(assignment.purchased_points_refunded_microroo, 0)
        self.assertEqual(evidence.reviewed_by, "ops@example.com")
        self.assertEqual(evidence.debit_ledger_id, booking.ledger_entry_id)
        self.assertEqual(
            evidence.assignment_refund_snapshot[0]["assignment_id"],
            assignment.pk,
        )

    def test_reconciliation_reclassifies_legacy_purchased_refund_once(self):
        user = User.objects.create_user(
            email="legacy-purchased-refund@example.com",
            slack_id="ULEGACYPURCHASEDREFUND",
        )
        booking_date = timezone.localdate() + timedelta(days=5)
        PointsService.credit_purchased_topup(
            user=user,
            delta=8,
            description="Purchased setup",
            idempotency_key="legacy-purchased-refund-setup",
        )
        booking, _ = CoworkingService.book(
            user=user,
            booking_date=booking_date,
            created_by_slack_id=user.slack_id,
        )
        day = OfficeManagerDay.objects.create(
            date=booking_date,
            status="claimed",
            slack_channel_id="CCOWORK",
            claim_cutoff_at=timezone.now() + timedelta(days=5),
        )
        # This reproduces the pre-provenance behavior: a purchased debit was
        # refunded wholly into the earned bucket.
        refund, _ = PointsService.refund(
            user=user,
            delta=8,
            source="COWORKING",
            description="Legacy Office Manager refund",
            created_by_slack_id=user.slack_id,
            idempotency_key="legacy-purchased-refund",
            reference_type="OFFICE_MANAGER_ASSIGNMENT",
            reference_id=str(day.pk),
            reverse_lifetime_spent=True,
        )
        booking.original_points_cost = 8
        booking.points_cost = 0
        booking.booking_source = "office_manager"
        booking.purchased_points_cost_microroo = None
        booking.refund_ledger_entry = refund
        booking.save()
        assignment = OfficeManagerAssignment.objects.create(
            day=day,
            user=user,
            booking=booking,
            points_refunded=8,
            purchased_points_refunded_microroo=None,
            refund_ledger_entry=refund,
        )
        account = PointsAccount.objects.get(user=user)
        self.assertEqual(account.earned_balance_microroo, 8_000_000)
        self.assertEqual(account.purchased_topup_balance_microroo, 0)

        for _ in range(2):
            call_command(
                "reconcile_office_manager_provenance",
                booking_id=str(booking.pk),
                purchased_microroo=8_000_000,
                reviewed_by="ops@example.com",
                commit=True,
                stdout=io.StringIO(),
            )

        account.refresh_from_db()
        booking.refresh_from_db()
        assignment.refresh_from_db()
        self.assertEqual(account.earned_balance_microroo, 0)
        self.assertEqual(
            account.purchased_topup_balance_microroo,
            8_000_000,
        )
        self.assertEqual(booking.purchased_points_cost_microroo, 8_000_000)
        self.assertEqual(
            assignment.purchased_points_refunded_microroo,
            8_000_000,
        )
        repair = OfficeManagerProvenanceBucketRepair.objects.get(
            reconciliation__booking=booking
        )
        self.assertEqual(repair.purchased_microroo, 8_000_000)
        self.assertEqual(repair.ledger.delta_microroo, 0)
        self.assertEqual(
            Ledger.objects.filter(
                idempotency_key=(
                    f"office_manager_bucket_reclassification:{booking.pk}"
                )
            ).count(),
            1,
        )

    def test_reversed_legacy_refund_requires_exact_reversal_bucket_evidence(self):
        user = User.objects.create_user(
            email="legacy-reversed-refund@example.com",
            slack_id="ULEGACYREVERSEDREFUND",
        )
        booking_date = timezone.localdate() + timedelta(days=5)
        PointsService.credit_purchased_topup(
            user=user,
            delta=8,
            description="Purchased setup",
            idempotency_key="legacy-reversed-refund-setup",
        )
        booking, _ = CoworkingService.book(
            user=user,
            booking_date=booking_date,
            created_by_slack_id=user.slack_id,
        )
        day = OfficeManagerDay.objects.create(
            date=booking_date,
            status="closed",
            slack_channel_id="CCOWORK",
            claim_cutoff_at=timezone.now() + timedelta(days=5),
        )
        refund, _ = PointsService.refund(
            user=user,
            delta=8,
            source="COWORKING",
            description="Legacy Office Manager refund",
            created_by_slack_id=user.slack_id,
            idempotency_key="legacy-reversed-refund",
            reference_type="OFFICE_MANAGER_ASSIGNMENT",
            reference_id=str(day.pk),
            reverse_lifetime_spent=True,
        )
        booking.original_points_cost = 8
        booking.points_cost = 0
        booking.booking_source = "office_manager"
        booking.purchased_points_cost_microroo = None
        booking.refund_ledger_entry = refund
        booking.status = "cancelled"
        booking.save()
        assignment = OfficeManagerAssignment.objects.create(
            day=day,
            user=user,
            booking=booking,
            status="relinquished",
            points_refunded=8,
            purchased_points_refunded_microroo=None,
            refund_ledger_entry=refund,
        )
        reversal, _ = PointsService.spend(
            user=user,
            delta=8,
            source="COWORKING",
            description="Legacy refund reversal",
            created_by_slack_id=user.slack_id,
            idempotency_key="legacy-reversed-refund-spend",
            reference_type="OFFICE_MANAGER_REFUND_REVERSAL",
            reference_id=str(assignment.pk),
            purchased_delta_microroo=0,
        )
        assignment.refund_reversal_ledger_entry = reversal
        assignment.save(update_fields=["refund_reversal_ledger_entry"])

        with self.assertRaises(CommandError):
            call_command(
                "reconcile_office_manager_provenance",
                booking_id=str(booking.pk),
                purchased_microroo=8_000_000,
                reviewed_by="ops@example.com",
                commit=True,
                stdout=io.StringIO(),
            )

        call_command(
            "reconcile_office_manager_provenance",
            booking_id=str(booking.pk),
            purchased_microroo=8_000_000,
            reversal_purchased_microroo=[f"{assignment.pk}:0"],
            reviewed_by="ops@example.com",
            commit=True,
            stdout=io.StringIO(),
        )

        account = PointsAccount.objects.get(user=user)
        self.assertEqual(account.earned_balance_microroo, 0)
        self.assertEqual(account.purchased_topup_balance_microroo, 0)
        evidence = OfficeManagerRefundReversalProvenance.objects.get(
            assignment=assignment
        )
        self.assertEqual(evidence.reversal_ledger_id, reversal.pk)
        self.assertEqual(evidence.purchased_microroo, 0)
        self.assertFalse(
            OfficeManagerProvenanceBucketRepair.objects.filter(
                reconciliation__booking=booking
            ).exists()
        )
        call_command(
            "audit_office_manager_migrations",
            configured_office_manager_channel="CCOWORK",
            stdout=io.StringIO(),
        )

    def test_audit_rejects_purchased_reconciliation_without_bucket_repair(self):
        user = User.objects.create_user(
            email="unrepaired-bucket@example.com",
            slack_id="UUNREPAIREDBUCKET",
        )
        booking_date = melbourne_at(2026, 9, 4, 9).date()
        day = OfficeManagerDay.objects.create(
            date=booking_date,
            status="claimed",
            slack_channel_id="CCOWORK",
            claim_cutoff_at=melbourne_at(2026, 9, 4, 10),
        )
        booking = CoworkingBooking.objects.create(
            user=user,
            date=booking_date,
            status="booked",
            points_cost=0,
            original_points_cost=8,
            booking_source="office_manager",
            purchased_points_cost_microroo=8_000_000,
        )
        debit = Ledger.objects.create(
            user=user,
            delta=-8,
            delta_microroo=-8_000_000,
            kind="SPEND",
            source="COWORKING",
            reference_type="COWORKING_BOOKING",
            reference_id=str(booking.pk),
            idempotency_key="unrepaired-bucket-debit",
        )
        refund = Ledger.objects.create(
            user=user,
            delta=8,
            delta_microroo=8_000_000,
            kind="REFUND",
            source="COWORKING",
            reference_type="OFFICE_MANAGER_ASSIGNMENT",
            reference_id=str(day.pk),
            idempotency_key="unrepaired-bucket-refund",
        )
        booking.ledger_entry = debit
        booking.refund_ledger_entry = refund
        booking.save(update_fields=["ledger_entry", "refund_ledger_entry"])
        assignment = OfficeManagerAssignment.objects.create(
            day=day,
            user=user,
            booking=booking,
            points_refunded=8,
            purchased_points_refunded_microroo=8_000_000,
            refund_ledger_entry=refund,
        )
        OfficeManagerProvenanceReconciliation.objects.create(
            booking=booking,
            debit_ledger=debit,
            purchased_microroo=8_000_000,
            reviewed_by="ops@example.com",
            assignment_refund_snapshot=[
                {
                    "assignment_id": assignment.pk,
                    "refund_ledger_id": refund.pk,
                    "refund_microroo": 8_000_000,
                }
            ],
        )
        stdout = io.StringIO()

        with self.assertRaises(CommandError):
            call_command("audit_office_manager_migrations", stdout=stdout)

        report = json.loads(stdout.getvalue().splitlines()[0])
        self.assertEqual(
            report["data_invariants"]
            ["unrepaired_office_manager_refund_buckets"][0]["booking_id"],
            str(booking.pk),
        )

    def test_office_manager_booking_without_assignment_is_unsafe(self):
        booking = CoworkingBooking.objects.create(
            user=User.objects.create_user(
                email="orphan-office-manager@example.com",
                slack_id="UORPHANOFFICEMANAGER",
            ),
            date=melbourne_at(2026, 9, 2, 9).date(),
            points_cost=0,
            booking_source="office_manager",
        )
        stdout = io.StringIO()

        with self.assertRaises(CommandError):
            call_command("audit_office_manager_migrations", stdout=stdout)

        report = json.loads(stdout.getvalue().splitlines()[0])
        self.assertEqual(
            report["data_invariants"][
                "office_manager_bookings_without_assignment"
            ][0]["booking_id"],
            str(booking.id),
        )

    def test_refunded_assignment_without_authoritative_debit_is_unsafe(self):
        user = User.objects.create_user(
            email="missing-office-manager-debit@example.com",
            slack_id="UMISSINGOFFICEMANAGERDEBIT",
        )
        booking_date = melbourne_at(2026, 9, 5, 9).date()
        day = OfficeManagerDay.objects.create(
            date=booking_date,
            status="claimed",
            slack_channel_id="CCOWORK",
            claim_cutoff_at=melbourne_at(2026, 9, 5, 10),
        )
        booking = CoworkingBooking.objects.create(
            user=user,
            date=booking_date,
            points_cost=0,
            original_points_cost=8,
            booking_source="office_manager",
            purchased_points_cost_microroo=0,
        )
        refund = Ledger.objects.create(
            user=user,
            delta=8,
            delta_microroo=8_000_000,
            kind="REFUND",
            source="COWORKING",
            reference_type="OFFICE_MANAGER_ASSIGNMENT",
            reference_id=str(day.pk),
            idempotency_key="missing-office-manager-debit-refund",
        )
        assignment = OfficeManagerAssignment.objects.create(
            day=day,
            user=user,
            booking=booking,
            points_refunded=8,
            purchased_points_refunded_microroo=0,
            refund_ledger_entry=refund,
        )
        stdout = io.StringIO()

        with self.assertRaises(CommandError):
            call_command("audit_office_manager_migrations", stdout=stdout)

        report = json.loads(stdout.getvalue().splitlines()[0])
        self.assertEqual(
            report["data_invariants"][
                "invalid_office_manager_debit_ledgers"
            ][0]["assignment_id"],
            assignment.pk,
        )

    def test_future_paid_booking_without_bucket_provenance_is_unsafe(self):
        booking = CoworkingBooking.objects.create(
            user=User.objects.create_user(
                email="unknown-booking-provenance@example.com",
                slack_id="UUNKNOWNBOOKINGPROVENANCE",
            ),
            date=melbourne_at(2030, 9, 2, 9).date(),
            points_cost=8,
            booking_source="points",
            purchased_points_cost_microroo=None,
        )
        stdout = io.StringIO()

        with self.assertRaises(CommandError):
            call_command("audit_office_manager_migrations", stdout=stdout)

        report = json.loads(stdout.getvalue().splitlines()[0])
        self.assertEqual(
            report["data_invariants"]["unreconciled_paid_bookings"][0][
                "booking_id"
            ],
            str(booking.id),
        )

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
                    office_manager_authorized=True,
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
        # If cancellation wins the date lock, the concurrent generation-1
        # click is correctly fenced as belonging to the superseded announcement.
        self.assertTrue(
            set(outcomes).issubset(
                {
                    "cancelled",
                    "claimed",
                    "already_claimed",
                    "claim_closed",
                    "announcement_superseded",
                }
            )
        )
        self.assertLessEqual(
            OfficeManagerAssignment.objects.filter(status="active").count(),
            1,
        )

    def test_delivery_does_not_lock_user_ahead_of_cancellation(self):
        now = melbourne_at(2026, 8, 3, 8, 45)
        office_manager_day(now.date())
        user = User.objects.create_user(
            email="delivery-lock-order@example.com",
            slack_id="UDELIVERYLOCK",
        )
        claimed = OfficeManagerService.claim(
            slack_user_id=user.slack_id,
            booking_date=now.date(),
            now=now,
        )
        user_locked = Event()
        allow_cancellation = Event()
        original_lock_booking_date = CoworkingService._lock_booking_date

        def pause_after_user_lock(booking_date):
            user_locked.set()
            if not allow_cancellation.wait(timeout=10):
                raise RuntimeError("cancellation lock-order test timed out")
            return original_lock_booking_date(booking_date)

        def cancel_booking():
            close_old_connections()
            try:
                return CoworkingService.cancel(
                    str(claimed.booking.id),
                    user.slack_id,
                    office_manager_authorized=True,
                )
            finally:
                connections.close_all()

        def deliver_dm():
            close_old_connections()
            try:
                return OfficeManagerService.deliver_winner_dm(
                    claimed.assignment.id,
                    now=now,
                )
            finally:
                connections.close_all()

        fake_client = Mock()
        fake_client.chat_postMessage.return_value = {
            "ok": True,
            "ts": "delivery.lock.order",
        }
        with (
            patch.object(
                CoworkingService,
                "_lock_booking_date",
                side_effect=pause_after_user_lock,
            ),
            patch(
                "roo.office_manager._open_dm_channel",
                return_value="DDELIVERYLOCK",
            ),
            patch(
                "roo.office_manager.SlackService.get_client",
                return_value=fake_client,
            ),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            cancellation = executor.submit(cancel_booking)
            self.assertTrue(user_locked.wait(timeout=5))
            delivery = executor.submit(deliver_dm)
            try:
                self.assertTrue(delivery.result(timeout=5))
            finally:
                allow_cancellation.set()
            cancellation.result(timeout=10)
