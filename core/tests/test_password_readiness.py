import json
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import TestCase


User = get_user_model()


class PasswordReadinessModelTests(TestCase):
    def test_user_manager_and_save_canonicalize_entire_email(self):
        user = User.objects.create_user(email="  Mixed.Case@Example.COM  ")

        self.assertEqual(user.email, "mixed.case@example.com")
        user.email = "  SECOND@Example.COM "
        user.save(update_fields=("email",))
        user.refresh_from_db()
        self.assertEqual(user.email, "second@example.com")

    def test_case_insensitive_email_constraint_rejects_duplicates(self):
        User.objects.create_user(email="person@example.com")

        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create_user(email="PERSON@EXAMPLE.COM")

    def test_password_metadata_distinguishes_usable_passwords(self):
        password_user = User.objects.create_user(
            email="password@example.com",
            password="StrongPassword123!",
        )
        passwordless_user = User.objects.create_user(email="passwordless@example.com")

        self.assertTrue(password_user.has_usable_password())
        self.assertIsNotNone(password_user.password_set_at)
        self.assertFalse(passwordless_user.has_usable_password())
        self.assertIsNone(passwordless_user.password_set_at)
        self.assertEqual(password_user.auth_version, 1)


class PasswordReadinessCommandTests(TestCase):
    def test_json_report_contains_only_aggregate_readiness_counts(self):
        User.objects.create_user(
            email="ready@example.com",
            password="StrongPassword123!",
            first_name="Ready",
        )
        User.objects.create_user(email="needs-setup@example.com")
        User.objects.create_user(
            email="U12345678@slack.placeholder.com",
            slack_id="U12345678",
        )
        output = StringIO()

        call_command("audit_password_readiness", "--json", stdout=output)

        report = json.loads(output.getvalue())
        self.assertEqual(report["total_accounts"], 3)
        self.assertEqual(report["usable_password_accounts"], 1)
        self.assertEqual(report["unusable_password_accounts"], 2)
        self.assertEqual(report["slack_placeholder_accounts"], 1)
        self.assertEqual(report["eligible_password_setup_accounts"], 1)
        self.assertNotIn("ready@example.com", output.getvalue())

    def test_fail_on_blockers_rejects_active_placeholder_accounts(self):
        User.objects.create_user(email="U12345678@slack.placeholder.com")

        with self.assertRaises(CommandError):
            call_command("audit_password_readiness", "--fail-on-blockers")
