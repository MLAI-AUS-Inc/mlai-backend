from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from unittest import skipUnless
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from core.models import User

from .models import (
    CoworkingBooking,
    CoworkingDayCapacity,
    OfficeManagerAssignment,
    OfficeManagerDay,
    PointsAccount,
)
from .office_manager import (
    NO_FOOD_REMINDER,
    OfficeManagerClaimError,
    OfficeManagerService,
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


@override_settings(
    OFFICE_MANAGER_ENABLED=True,
    OFFICE_MANAGER_SLACK_CHANNEL_ID="CCOWORK",
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

    def test_existing_four_point_booking_refunds_four(self):
        booking = CoworkingBooking.objects.create(
            user=self.user,
            date=self.now.date(),
            status="booked",
            points_cost=4,
            booking_source="points",
        )
        PointsService.award(
            user=self.user,
            delta=1,
            source="MANUAL",
            description="Create account",
            created_by_slack_id="UADMIN",
            idempotency_key="four-point-refund-setup",
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
        self.assertIn(f"<@{self.user.slack_id}>", payload["text"])
        self.assertIn("Office Manager of the Day", payload["text"])
        self.assertIn("without deducting Roo points", payload["text"])
        self.assertIn(NO_FOOD_REMINDER, payload["text"])
        result.assignment.refresh_from_db()
        self.assertEqual(
            result.assignment.winner_channel_announcement_status,
            "sent",
        )

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

    @patch("roo.office_manager.SlackService.get_user_profile")
    def test_new_member_uses_separate_slack_side_identity(self, get_profile):
        get_profile.return_value = {
            "real_name": "Slack Member",
            "email": "founder@example.com",
            "is_bot": False,
            "deleted": False,
        }
        founder = User.objects.create_user(email="founder@example.com")

        result = OfficeManagerService.claim(
            slack_user_id="UNEWMEMBER",
            booking_date=self.now.date(),
            now=self.now,
        )

        self.assertNotEqual(result.booking.user_id, founder.id)
        self.assertEqual(result.booking.user.slack_id, "UNEWMEMBER")
        self.assertTrue(
            result.booking.user.email.endswith("@users.mlai.internal")
        )
        founder.refresh_from_db()
        self.assertIsNone(founder.slack_id)

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


@override_settings(
    OFFICE_MANAGER_ENABLED=True,
    OFFICE_MANAGER_SLACK_CHANNEL_ID="CCOWORK",
    OFFICE_MANAGER_TIMEZONE="Australia/Melbourne",
)
class OfficeManagerSchedulerTests(TestCase):
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
        ):
            first = run_office_manager_scheduler(now=now)
            second = run_office_manager_scheduler(now=now)

        self.assertTrue(first["announcement_sent"])
        self.assertNotIn("announcement_sent", second)
        self.assertEqual(fake_client.chat_postMessage.call_count, 1)
        payload = fake_client.chat_postMessage.call_args.kwargs
        blocks_text = json_text(payload["blocks"])
        self.assertIn("Volunteer for today", blocks_text)
        self.assertIn("No channel or thread reply is needed", blocks_text)
        self.assertIn(NO_FOOD_REMINDER, blocks_text)

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


def json_text(value):
    import json

    return json.dumps(value)


@override_settings(
    ROO_API_KEY="office-manager-test-key",
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

    def test_strict_roo_key_is_required(self):
        response = self.client.post(
            self.url,
            {"slack_user_id": self.user.slack_id, "date": self.now.date()},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

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
            {"slack_user_id": self.user.slack_id, "date": self.now.date()},
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


@skipUnless(
    connection.vendor == "postgresql",
    "PostgreSQL row-lock behavior is not provided by SQLite",
)
@override_settings(OFFICE_MANAGER_TIMEZONE="Australia/Melbourne")
class OfficeManagerPostgresConcurrencyTests(TransactionTestCase):
    reset_sequences = True

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
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(claim, [user.id for user in users]))

        self.assertCountEqual(outcomes, ["claimed", "already_claimed"])
        self.assertEqual(
            OfficeManagerAssignment.objects.filter(status="active").count(),
            1,
        )
        self.assertEqual(CoworkingBooking.objects.count(), 1)
