from datetime import date, datetime
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from organizations.models import Organization
from startup_updates.models import (
    MonthlyUpdateDraft,
    MonthlyUpdateDraftStatus,
    MonthlyUpdateReminderDelivery,
    MonthlyUpdateReminderKind,
    MonthlyUpdateReminderStatus,
    UserStartupBinding,
)
from startup_updates.monthly_update_reminders import (
    collect_monthly_update_reminder_targets,
    run_monthly_update_reminder_scheduler,
)


@override_settings(
    MONTHLY_UPDATE_REMINDERS_ENABLED=True,
    MONTHLY_UPDATE_REMINDER_TIMEZONE="Australia/Melbourne",
    MONTHLY_UPDATE_REMINDER_HOUR=9,
    MONTHLY_UPDATE_REMINDER_MINUTE=0,
    MONTHLY_UPDATE_REMINDERS_QUEUE_DRAFT=False,
    MONTHLY_UPDATE_REMINDER_APP_URL="https://mlai.au",
    CUSTOMERIO_API_KEY="test-key",
    CUSTOMERIO_MONTHLY_UPDATE_7D_TEMPLATE_ID="template-seven",
    CUSTOMERIO_MONTHLY_UPDATE_1D_TEMPLATE_ID="template-one",
)
class MonthlyUpdateReminderTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="founder@example.com",
            first_name="Alex",
        )
        self.organization = Organization.objects.create(name="Acme", domain="acme.example")
        self.profile = VibeRaisingProfile.objects.create(
            user=self.user,
            role=VibeRaisingProfile.ROLE_FOUNDER,
        )
        self.company = VibeRaisingCompany.objects.create(
            profile=self.profile,
            organization=self.organization,
            name="Acme Pty Ltd",
            domain="acme.example",
            registered=True,
            abn="89000000019",
            acn="000000019",
            abr_verified_at=datetime(2026, 7, 1, 0, 0, tzinfo=ZoneInfo("UTC")),
        )
        self.binding = UserStartupBinding.objects.create(
            user=self.user,
            organization=self.organization,
            coworking_discount_eligible=True,
        )
        self.update = MonthlyUpdateDraft.objects.create(
            organization=self.organization,
            month=date(2026, 7, 1),
            status=MonthlyUpdateDraftStatus.READY,
        )
        MonthlyUpdateDraft.objects.filter(pk=self.update.pk).update(
            ready_at=datetime(2026, 7, 1, 2, 0, tzinfo=ZoneInfo("UTC"))
        )
        self.update.refresh_from_db()

    @staticmethod
    def _melbourne_at(year, month, day, hour=9, minute=0):
        return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("Australia/Melbourne"))

    def test_collects_seven_day_target_with_company_aware_login_link(self):
        targets = collect_monthly_update_reminder_targets(date(2026, 7, 23))

        self.assertEqual(len(targets), 1)
        target = targets[0]
        self.assertEqual(target.reminder_kind, MonthlyUpdateReminderKind.SEVEN_DAY)
        self.assertEqual(target.valid_through, date(2026, 7, 29))
        self.assertEqual(target.expires_on, date(2026, 7, 30))
        self.assertIn("/platform/login?", target.update_url)
        self.assertIn("companyId%3D", target.update_url)
        self.assertIn(str(self.company.pk), target.update_url)

    @patch("startup_updates.monthly_update_reminders._customerio_client")
    def test_sends_once_with_subscription_safe_customerio_payload(self, mock_client_factory):
        client = Mock()
        client.send_email.return_value = {"delivery_id": "cio-123"}
        mock_client_factory.return_value = client
        now = self._melbourne_at(2026, 7, 23)

        first = run_monthly_update_reminder_scheduler(now=now)
        second = run_monthly_update_reminder_scheduler(now=now)

        self.assertEqual(first["status"], "completed")
        self.assertEqual(second["outcomes"][0]["reason"], "already_sent")
        client.send_email.assert_called_once()
        payload = client.send_email.call_args.args[0]
        self.assertEqual(payload["transactional_message_id"], "template-seven")
        self.assertEqual(payload["identifiers"], {"id": str(self.user.pk)})
        self.assertFalse(payload["send_to_unsubscribed"])
        self.assertFalse(payload["queue_draft"])
        self.assertEqual(payload["message_data"]["startups"][0]["name"], "Acme Pty Ltd")
        delivery = MonthlyUpdateReminderDelivery.objects.get()
        self.assertEqual(delivery.status, MonthlyUpdateReminderStatus.SENT)
        self.assertEqual(delivery.customerio_delivery_id, "cio-123")
        self.assertEqual(delivery.attempt_count, 1)

    def test_one_day_target_is_due_on_last_discount_day(self):
        targets = collect_monthly_update_reminder_targets(date(2026, 7, 29))

        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].reminder_kind, MonthlyUpdateReminderKind.ONE_DAY)

    def test_disabled_binding_and_invalid_registration_are_excluded(self):
        self.binding.coworking_discount_eligible = False
        self.binding.save(update_fields=["coworking_discount_eligible", "updated_at"])
        self.assertEqual(collect_monthly_update_reminder_targets(date(2026, 7, 23)), [])

        self.binding.coworking_discount_eligible = True
        self.binding.save(update_fields=["coworking_discount_eligible", "updated_at"])
        self.company.abr_verified_at = None
        self.company.save(update_fields=["abr_verified_at", "updated_at"])
        self.assertEqual(collect_monthly_update_reminder_targets(date(2026, 7, 23)), [])

    def test_newer_ready_update_suppresses_old_cycle(self):
        newer = MonthlyUpdateDraft.objects.create(
            organization=self.organization,
            month=date(2026, 8, 1),
            status=MonthlyUpdateDraftStatus.READY,
        )
        MonthlyUpdateDraft.objects.filter(pk=newer.pk).update(
            ready_at=datetime(2026, 7, 10, 1, 0, tzinfo=ZoneInfo("UTC"))
        )

        self.assertEqual(collect_monthly_update_reminder_targets(date(2026, 7, 23)), [])

    def test_bound_user_without_an_owned_company_is_not_sent_a_broken_link(self):
        second_user = get_user_model().objects.create_user(email="owner@example.com")
        second_organization = Organization.objects.create(name="Beta", domain="beta.example")
        second_profile = VibeRaisingProfile.objects.create(
            user=second_user,
            role=VibeRaisingProfile.ROLE_FOUNDER,
        )
        VibeRaisingCompany.objects.create(
            profile=second_profile,
            organization=second_organization,
            name="Beta Pty Ltd",
            registered=True,
            acn="000000027",
            abr_verified_at=datetime(2026, 7, 1, tzinfo=ZoneInfo("UTC")),
        )
        UserStartupBinding.objects.create(
            user=self.user,
            organization=second_organization,
            coworking_discount_eligible=True,
        )
        second_update = MonthlyUpdateDraft.objects.create(
            organization=second_organization,
            month=date(2026, 7, 1),
            status=MonthlyUpdateDraftStatus.READY,
        )
        MonthlyUpdateDraft.objects.filter(pk=second_update.pk).update(
            ready_at=datetime(2026, 7, 1, 2, 0, tzinfo=ZoneInfo("UTC"))
        )

        targets = collect_monthly_update_reminder_targets(date(2026, 7, 23))

        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].organization_id, self.organization.pk)

    @patch("startup_updates.monthly_update_reminders._customerio_client")
    def test_dry_run_is_read_only_even_when_a_target_is_due(self, mock_client_factory):
        result = run_monthly_update_reminder_scheduler(
            now=self._melbourne_at(2026, 7, 23),
            dry_run=True,
        )

        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["recipient_count"], 1)
        self.assertEqual(result["target_count"], 1)
        mock_client_factory.assert_not_called()
        self.assertFalse(MonthlyUpdateReminderDelivery.objects.exists())

    @override_settings(MONTHLY_UPDATE_REMINDERS_QUEUE_DRAFT=True)
    @patch("startup_updates.monthly_update_reminders._customerio_client")
    def test_rollout_can_queue_customerio_drafts(self, mock_client_factory):
        client = Mock()
        client.send_email.return_value = {"delivery_id": "draft-123"}
        mock_client_factory.return_value = client

        run_monthly_update_reminder_scheduler(now=self._melbourne_at(2026, 7, 23))

        payload = client.send_email.call_args.args[0]
        self.assertTrue(payload["queue_draft"])
        self.assertEqual(
            MonthlyUpdateReminderDelivery.objects.get().status,
            MonthlyUpdateReminderStatus.DRAFTED,
        )

    @override_settings(MONTHLY_UPDATE_REMINDERS_ENABLED=False)
    def test_scheduler_is_disabled_by_default_but_dry_run_still_works(self):
        skipped = run_monthly_update_reminder_scheduler(now=self._melbourne_at(2026, 7, 23))
        preview = run_monthly_update_reminder_scheduler(
            now=self._melbourne_at(2026, 7, 23),
            dry_run=True,
        )

        self.assertEqual(skipped, {"status": "skipped", "reason": "disabled"})
        self.assertEqual(preview["status"], "dry_run")
