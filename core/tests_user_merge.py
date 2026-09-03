from datetime import date, timedelta
from io import StringIO
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.db import OperationalError
from django.test import TestCase
from django.utils import timezone

from integrations.models import GoogleConnection

from roo.models import (
    BoostPostAdmission,
    CoworkingBooking,
    Ledger,
    MeetingRoom,
    MeetingRoomBooking,
    PointsAccount,
    PointsAdmin,
)


User = get_user_model()


class MergePairTests(TestCase):
    def setUp(self):
        self.source = User.objects.create_user(
            email="anu.ganugapati@gmail.com", slack_id="USOURCE", first_name="Anurag"
        )
        self.target = User.objects.create_user(
            email="anu@statdoctor.net", slack_id="UTARGET", first_name="Anu"
        )

    def run_merge(self, *args):
        out = StringIO()
        call_command(
            "cleanup_users",
            "--source-slack-id=USOURCE",
            "--target-slack-id=UTARGET",
            *args,
            stdout=out,
        )
        return out.getvalue()

    def make_ledger(self, user, key, delta=10):
        return Ledger.objects.create(
            user=user, delta=delta, kind="EARN", source="MANUAL", idempotency_key=key
        )

    def make_boost(self, user, suffix):
        return BoostPostAdmission.objects.create(
            submission_key="sub-%s" % suffix,
            workspace_id="T1",
            channel_id="C1",
            root_message_ts="ts-%s" % suffix,
            poster_slack_id=user.slack_id,
            user=user,
            social_post_url="https://example.com/%s" % suffix,
            status="approved",
        )

    def test_dry_run_changes_nothing(self):
        self.make_ledger(self.source, "k1")

        output = self.run_merge()

        self.assertIn("Dry run", output)
        self.assertTrue(User.objects.filter(slack_id="USOURCE").exists())
        self.assertEqual(Ledger.objects.filter(user=self.source).count(), 1)

    def test_dry_run_lists_related_rows(self):
        self.make_ledger(self.source, "k1")
        self.make_boost(self.source, "a")

        output = self.run_merge()

        self.assertIn("roo.Ledger: 1", output)
        self.assertIn("roo.BoostPostAdmission: 1", output)

    def test_commit_moves_ledger_and_deletes_source(self):
        self.make_ledger(self.source, "k1")
        self.make_ledger(self.source, "k2")

        self.run_merge("--commit")

        self.assertFalse(User.objects.filter(slack_id="USOURCE").exists())
        self.assertEqual(Ledger.objects.filter(user=self.target).count(), 2)

    def test_commit_moves_boost_post_admissions(self):
        boost = self.make_boost(self.source, "a")

        self.run_merge("--commit")

        boost.refresh_from_db()
        self.assertEqual(boost.user_id, self.target.id)

    def test_commit_moves_coworking_bookings(self):
        booking = CoworkingBooking.objects.create(
            user=self.source, date=date(2026, 9, 1), points_cost=8
        )

        self.run_merge("--commit")

        booking.refresh_from_db()
        self.assertEqual(booking.user_id, self.target.id)

    def test_points_accounts_are_summed(self):
        PointsAccount.objects.create(
            user=self.source,
            balance=140,
            lifetime_earned=188,
            balance_microroo=140_000_000,
            earned_balance_microroo=130_000_000,
            purchased_topup_balance_microroo=10_000_000,
            lifetime_earned_microroo=188_000_000,
            lifetime_purchased_topup_microroo=10_000_000,
            lifetime_spent_microroo=58_000_000,
            microroo_initialized=True,
        )
        PointsAccount.objects.create(
            user=self.target,
            balance=142,
            lifetime_earned=178,
            balance_microroo=142_000_000,
            earned_balance_microroo=122_000_000,
            purchased_topup_balance_microroo=20_000_000,
            lifetime_earned_microroo=178_000_000,
            lifetime_purchased_topup_microroo=20_000_000,
            lifetime_spent_microroo=56_000_000,
            microroo_initialized=True,
        )

        self.run_merge("--commit")

        account = PointsAccount.objects.get(user=self.target)
        self.assertEqual(account.balance, 282)
        self.assertEqual(account.lifetime_earned, 366)
        self.assertEqual(account.purchased_topup_balance_microroo, 30_000_000)
        self.assertEqual(PointsAccount.objects.count(), 1)

    def test_duplicate_points_admin_row_is_removed(self):
        PointsAdmin.objects.create(slack_user_id="USOURCE", user=self.source, role="committee")
        PointsAdmin.objects.create(slack_user_id="UTARGET", user=self.target, role="committee")

        self.run_merge("--commit")

        self.assertFalse(PointsAdmin.objects.filter(slack_user_id="USOURCE").exists())
        self.assertTrue(PointsAdmin.objects.filter(slack_user_id="UTARGET").exists())

    def test_target_keeps_its_own_slack_id(self):
        self.run_merge("--commit")

        self.target.refresh_from_db()
        self.assertEqual(self.target.slack_id, "UTARGET")

    def test_commit_refuses_unhandled_cascade_relation_without_partial_merge(self):
        room = MeetingRoom.objects.create(slug="merge-guard", name="Merge Guard")
        starts_at = timezone.now() + timedelta(days=1)
        booking = MeetingRoomBooking.objects.create(
            room=room,
            user=self.source,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(hours=1),
            points_cost=1,
            client_request_id=uuid4(),
            requested_by_slack_id="USOURCE",
        )
        self.make_ledger(self.source, "merge-guard-ledger")

        with self.assertRaisesMessage(
            CommandError,
            "roo.MeetingRoomBooking.user=1",
        ):
            self.run_merge("--commit")

        self.source.refresh_from_db()
        booking.refresh_from_db()
        self.assertEqual(self.source.slack_id, "USOURCE")
        self.assertEqual(booking.user_id, self.source.pk)
        self.assertEqual(Ledger.objects.filter(user=self.source).count(), 1)
        self.assertEqual(Ledger.objects.filter(user=self.target).count(), 0)

    def test_commit_refuses_unhandled_integration_relation(self):
        connection = GoogleConnection.objects.create(
            user=self.source,
            google_email="source@example.com",
            refresh_token="encrypted-test-token",
            scope="openid email",
        )

        with self.assertRaisesMessage(
            CommandError,
            "integrations.GoogleConnection.user=1",
        ):
            self.run_merge("--commit")

        self.assertTrue(User.objects.filter(pk=self.source.pk).exists())
        connection.refresh_from_db()
        self.assertEqual(connection.user_id, self.source.pk)

    def test_requires_both_ids(self):
        with self.assertRaises(CommandError):
            call_command("cleanup_users", "--source-slack-id=USOURCE", stdout=StringIO())

    def test_rejects_merging_a_user_into_itself(self):
        with self.assertRaises(CommandError):
            call_command(
                "cleanup_users",
                "--source-slack-id=USOURCE",
                "--target-slack-id=USOURCE",
                stdout=StringIO(),
            )

    def test_unknown_slack_id_is_an_error(self):
        with self.assertRaises(CommandError):
            call_command(
                "cleanup_users",
                "--source-slack-id=UNOPE",
                "--target-slack-id=UTARGET",
                stdout=StringIO(),
            )

    def test_merge_retries_whole_transaction_after_deadlock(self):
        from core.management.commands.cleanup_users import Command

        error = OperationalError("deadlock detected")
        error.pgcode = "40P01"
        command = Command()
        with (
            patch.object(
                command,
                "merge_users",
                side_effect=[error, None],
            ) as merge_users,
            patch("core.management.commands.cleanup_users.time.sleep") as sleep,
        ):
            command.merge_users_with_retry(
                source_id=self.source.pk,
                target_id=self.target.pk,
            )

        self.assertEqual(merge_users.call_count, 2)
        sleep.assert_called_once_with(0.05)


class SlackEmailReconciliationTests(TestCase):
    def test_commit_merges_slack_principal_into_matching_email_principal(self):
        source = User.objects.create_user(
            email="USOURCE@slack.placeholder.com",
            slack_id="USOURCE",
        )
        target = User.objects.create_user(email="member@example.com")
        booking = CoworkingBooking.objects.create(
            user=source,
            date=date(2026, 9, 2),
            points_cost=8,
        )

        with patch(
            "core.management.commands.reconcile_slack_users_by_email."
            "SlackService.get_user_profile",
            return_value={"email": target.email},
        ):
            call_command(
                "reconcile_slack_users_by_email",
                "--commit",
                stdout=StringIO(),
            )

        self.assertFalse(User.objects.filter(pk=source.pk).exists())
        target.refresh_from_db()
        booking.refresh_from_db()
        self.assertEqual(target.slack_id, "USOURCE")
        self.assertEqual(booking.user_id, target.pk)

    def test_commit_exits_nonzero_when_durable_merge_fails(self):
        User.objects.create_user(
            email="USOURCE@slack.placeholder.com",
            slack_id="USOURCE",
        )
        target = User.objects.create_user(email="member@example.com")

        with (
            patch(
                "core.management.commands.reconcile_slack_users_by_email."
                "SlackService.get_user_profile",
                return_value={"email": target.email},
            ),
            patch(
                "core.management.commands.reconcile_slack_users_by_email."
                "CleanupUsersCommand.merge_users_with_retry",
                side_effect=ValueError("ambiguous ownership"),
            ),
            self.assertRaises(CommandError),
        ):
            call_command(
                "reconcile_slack_users_by_email",
                "--commit",
                stdout=StringIO(),
            )

    def test_commit_refuses_duplicate_with_unhandled_history(self):
        source = User.objects.create_user(
            email="USOURCE@slack.placeholder.com",
            slack_id="USOURCE",
        )
        target = User.objects.create_user(email="member@example.com")
        connection = GoogleConnection.objects.create(
            user=source,
            google_email="source@example.com",
            refresh_token="encrypted-test-token",
            scope="openid email",
        )

        with (
            patch(
                "core.management.commands.reconcile_slack_users_by_email."
                "SlackService.get_user_profile",
                return_value={"email": target.email},
            ),
            self.assertRaisesMessage(
                CommandError,
                "integrations.GoogleConnection.user=1",
            ),
        ):
            call_command(
                "reconcile_slack_users_by_email",
                "--commit",
                stdout=StringIO(),
            )

        source.refresh_from_db()
        target.refresh_from_db()
        connection.refresh_from_db()
        self.assertEqual(source.slack_id, "USOURCE")
        self.assertIsNone(target.slack_id)
        self.assertEqual(connection.user_id, source.pk)

    def test_commit_exits_nonzero_on_conflicting_target_slack_identity(self):
        User.objects.create_user(
            email="USOURCE@slack.placeholder.com",
            slack_id="USOURCE",
        )
        target = User.objects.create_user(
            email="member@example.com",
            slack_id="UOTHER",
        )

        with (
            patch(
                "core.management.commands.reconcile_slack_users_by_email."
                "SlackService.get_user_profile",
                side_effect=lambda slack_id: {
                    "email": target.email
                    if slack_id == "USOURCE"
                    else "other@example.com"
                },
            ),
            self.assertRaises(CommandError),
        ):
            call_command(
                "reconcile_slack_users_by_email",
                "--commit",
                stdout=StringIO(),
            )
