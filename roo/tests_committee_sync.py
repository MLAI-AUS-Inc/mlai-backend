import json
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from .models import PointsAdmin


User = get_user_model()


class SyncCommitteePointsAdminsTests(TestCase):
    def run_command(self, *args):
        out = StringIO()
        call_command("sync_committee_points_admins", *args, stdout=out)
        return json.loads(out.getvalue())

    def test_dry_run_reports_without_writing(self):
        User.objects.create_user(email="sam@mlai.au", slack_id="U05QPB483K9")

        result = self.run_command()

        self.assertFalse(result["applied"])
        self.assertIn("Dr Sam Donegan", [e["name"] for e in result["created"]])
        self.assertEqual(PointsAdmin.objects.count(), 0)

    def test_apply_creates_missing_row_with_standard_allowance(self):
        User.objects.create_user(email="pegah@bookiewand.ai", slack_id="UPEGAH")

        self.run_command("--apply")

        admin = PointsAdmin.objects.get(slack_user_id="UPEGAH")
        self.assertEqual(admin.role, "committee")
        self.assertTrue(admin.is_active)
        self.assertEqual(admin.weekly_allowance, 100)

    def test_apply_is_idempotent(self):
        User.objects.create_user(email="pegah@bookiewand.ai", slack_id="UPEGAH")

        self.run_command("--apply")
        second = self.run_command("--apply")

        self.assertEqual(second["created"], [])
        self.assertEqual([e["slack_user_id"] for e in second["unchanged"]], ["UPEGAH"])
        self.assertEqual(PointsAdmin.objects.count(), 1)

    def test_normalises_a_drifted_allowance(self):
        user = User.objects.create_user(email="sam@mlai.au", slack_id="USAM")
        PointsAdmin.objects.create(
            slack_user_id="USAM", user=user, role="committee", weekly_allowance=2000
        )

        result = self.run_command("--apply")

        self.assertEqual(PointsAdmin.objects.get(slack_user_id="USAM").weekly_allowance, 100)
        self.assertEqual(result["updated"][0]["was"], {"weekly_allowance": 2000})

    def test_reactivates_and_recasts_an_existing_row(self):
        user = User.objects.create_user(email="sam@mlai.au", slack_id="USAM")
        PointsAdmin.objects.create(
            slack_user_id="USAM",
            user=user,
            role="partner",
            is_active=False,
            weekly_allowance=100,
        )

        result = self.run_command("--apply")

        admin = PointsAdmin.objects.get(slack_user_id="USAM")
        self.assertEqual(admin.role, "committee")
        self.assertTrue(admin.is_active)
        self.assertEqual(result["updated"][0]["was"], {"role": "partner", "is_active": False})

    def test_email_match_is_case_insensitive(self):
        User.objects.create_user(email="Sam@MLAI.au", slack_id="USAM")

        self.run_command("--apply")

        self.assertTrue(PointsAdmin.objects.filter(slack_user_id="USAM").exists())

    def test_user_without_slack_id_is_blocked_not_created(self):
        User.objects.create_user(email="jkchangworks@gmail.com")

        result = self.run_command("--apply")

        reasons = {e["email"]: e["reason"] for e in result["blocked"]}
        self.assertEqual(reasons["jkchangworks@gmail.com"], "user has no slack_id")
        self.assertEqual(PointsAdmin.objects.count(), 0)

    def test_missing_user_is_blocked(self):
        result = self.run_command()

        reasons = {e["email"]: e["reason"] for e in result["blocked"]}
        self.assertEqual(reasons["sam@mlai.au"], "no user account")

    def test_off_roster_admin_is_deactivated_whatever_its_role(self):
        stranger = User.objects.create_user(email="partner@example.com", slack_id="UPARTNER")
        PointsAdmin.objects.create(
            slack_user_id="UPARTNER", user=stranger, role="partner", is_active=True
        )

        result = self.run_command("--apply")

        self.assertEqual([e["slack_user_id"] for e in result["deactivated"]], ["UPARTNER"])
        self.assertFalse(PointsAdmin.objects.get(slack_user_id="UPARTNER").is_active)

    def test_dry_run_does_not_deactivate(self):
        stranger = User.objects.create_user(email="partner@example.com", slack_id="UPARTNER")
        PointsAdmin.objects.create(
            slack_user_id="UPARTNER", user=stranger, role="partner", is_active=True
        )

        result = self.run_command()

        self.assertEqual([e["slack_user_id"] for e in result["deactivated"]], ["UPARTNER"])
        self.assertTrue(PointsAdmin.objects.get(slack_user_id="UPARTNER").is_active)

    def test_roster_member_is_never_deactivated(self):
        user = User.objects.create_user(email="sam@mlai.au", slack_id="USAM")
        PointsAdmin.objects.create(slack_user_id="USAM", user=user, role="committee")

        result = self.run_command("--apply")

        self.assertEqual(result["deactivated"], [])
        self.assertTrue(PointsAdmin.objects.get(slack_user_id="USAM").is_active)
