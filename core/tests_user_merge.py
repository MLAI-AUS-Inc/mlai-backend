from datetime import date
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.db import OperationalError
from django.test import TestCase

from roo.models import (
    BoostPostAdmission,
    CoworkingBooking,
    Ledger,
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
        PointsAccount.objects.create(user=self.source, balance=140, lifetime_earned=188)
        PointsAccount.objects.create(user=self.target, balance=142, lifetime_earned=178)

        self.run_merge("--commit")

        account = PointsAccount.objects.get(user=self.target)
        self.assertEqual(account.balance, 282)
        self.assertEqual(account.lifetime_earned, 366)
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
