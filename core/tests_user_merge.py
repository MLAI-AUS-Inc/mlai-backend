from datetime import date
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
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

    def test_commit_refuses_source_github_installation(self):
        from integrations.models import GitHubInstallation

        installation = GitHubInstallation.objects.create(
            user=self.source,
            installation_id="12345",
            account_login="source-owner",
        )

        with self.assertRaisesMessage(CommandError, "external identity state"):
            self.run_merge("--commit")

        self.assertTrue(User.objects.filter(pk=self.source.pk).exists())
        self.assertTrue(GitHubInstallation.objects.filter(pk=installation.pk).exists())

    def test_commit_refuses_source_content_factory_actor_references(self):
        from content_factory.models import ContentFactoryJob, OrganizationContentConfig
        from organizations.models import Organization

        actor_id = f"mlai_user:{self.source.pk}"
        organization = Organization.objects.create(
            name="Source Identity Co",
            domain="source-identity.example",
        )
        config = OrganizationContentConfig.objects.create(
            organization=organization,
            connected_slack_user_id=actor_id,
        )
        job = ContentFactoryJob.objects.create(
            job_id="source-identity-job",
            slack_user_id="unrelated",
            domain=organization.domain,
            request_meta={"nested": {"requested_by_slack_user_id": actor_id}},
        )

        with self.assertRaisesMessage(CommandError, "external identity state"):
            self.run_merge("--commit")

        self.assertTrue(User.objects.filter(pk=self.source.pk).exists())
        self.assertEqual(
            OrganizationContentConfig.objects.get(pk=config.pk).connected_slack_user_id,
            actor_id,
        )
        self.assertEqual(
            ContentFactoryJob.objects.get(pk=job.pk).request_meta["nested"][
                "requested_by_slack_user_id"
            ],
            actor_id,
        )

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
