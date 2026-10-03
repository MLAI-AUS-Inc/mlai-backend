"""Django-aware unit checks; all persistence is mocked, no database is built."""

from types import SimpleNamespace
from datetime import timedelta
from io import StringIO
import json
import unittest
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings
from django.core.management.base import CommandError
from django.utils import timezone
from django.urls import resolve
from rest_framework.exceptions import PermissionDenied

from core.authentication import CustomJWTAuthentication
from hospital.authentication import CustomJWTAuthentication as HistoricalAuthentication
from founder_tools import services as founders
from integrations.services.financial_records import (
    FinancialRecordOwnershipError, upsert_financial_record,
)
from integrations.fields import CredentialEncryptionError, LegacyPlaintextEncryptedTextField


class AuthenticationAndRoutesTests(SimpleTestCase):
    def test_historical_auth_import_is_the_same_implementation(self):
        self.assertIs(CustomJWTAuthentication, HistoricalAuthentication)

    def test_home_resolves_to_canonical_view(self):
        from community_chat.home_views import CommunityHomeView
        self.assertIs(resolve("/api/v1/community-chat/home/").func.view_class, CommunityHomeView)

    @override_settings(CSRF_TRUSTED_ORIGINS=["https://example.test"])
    def test_cookie_mutation_still_requires_trusted_origin(self):
        for headers in ({}, {"Origin": "https://untrusted.test"}):
            with self.subTest(headers=headers), self.assertRaises(PermissionDenied):
                CustomJWTAuthentication._enforce_cookie_csrf_origin(
                    SimpleNamespace(method="POST", headers=headers)
                )
        CustomJWTAuthentication._enforce_cookie_csrf_origin(
            SimpleNamespace(method="POST", headers={"Origin": "https://example.test"})
        )


class TenantClaimTests(SimpleTestCase):
    @patch.object(founders, "organization_owner_user_id", return_value=None)
    def test_absent_company_owner_does_not_authorise_reassignment(self, owner):
        self.assertFalse(founders.user_may_use_organization(SimpleNamespace(id=2), object()))

    @patch.object(founders, "organization_owner_user_id", return_value=1)
    def test_existing_owner_keeps_access(self, owner):
        self.assertTrue(founders.user_may_use_organization(SimpleNamespace(id=1), object()))
        self.assertFalse(founders.user_may_use_organization(SimpleNamespace(id=2), object()))

    @patch.object(founders.Organization, "objects")
    def test_new_domain_remains_claimable(self, manager):
        manager.filter.return_value.first.return_value = None
        self.assertTrue(founders.domain_is_available_to(SimpleNamespace(id=1), "new.example"))


class FinancialOwnershipTests(SimpleTestCase):
    def setUp(self):
        self.connection = SimpleNamespace(pk=1, organization_id=10, user_id=100, provider="xero", external_account_id="account")
        self.record = SimpleNamespace(
            connection_id=1, organization_id=10, user_id=100,
            record_type="xero_invoice", save=Mock(),
        )
        self.defaults = {"connection": self.connection, "record_type": "xero_invoice", "amount": 12}

    def write(self, **kwargs):
        # Unit-test ownership decisions; database tests cover the transaction.
        return upsert_financial_record.__wrapped__(
            external_record_id="invoice", defaults=self.defaults, **kwargs,
        )

    @patch("integrations.services.financial_records.ExternalFinancialRecord.objects")
    def test_same_owner_can_refresh_values(self, manager):
        manager.select_for_update.return_value.get_or_create.return_value = (self.record, False)
        self.write()
        self.assertEqual(self.record.amount, 12)
        self.record.save.assert_called_once()

    @patch("integrations.services.financial_records.ExternalFinancialRecord.objects")
    def test_conflicting_connection_or_tenant_cannot_change_existing_record(self, manager):
        for field, value in (("connection_id", 2), ("organization_id", 20), ("user_id", 200)):
            with self.subTest(field=field):
                original = getattr(self.record, field)
                setattr(self.record, field, value)
                manager.select_for_update.return_value.get_or_create.return_value = (self.record, False)
                with self.assertRaises(FinancialRecordOwnershipError):
                    self.write()
                self.record.save.assert_not_called()
                setattr(self.record, field, original)

    @patch("integrations.services.financial_records.ExternalFinancialRecord.objects")
    def test_bank_record_uses_upstream_bank_account_identity(self, manager):
        manager.select_for_update.return_value.get_or_create.return_value = (self.record, True)
        self.write(external_account_id="bank-sub-account")
        kwargs = manager.select_for_update.return_value.get_or_create.call_args.kwargs
        self.assertEqual(kwargs["external_account_id"], "bank-sub-account")
        self.assertEqual(kwargs["defaults"]["organization_id"], 10)
        self.assertEqual(kwargs["defaults"]["connection_id"], 1)

    def test_provider_conflict_is_rejected_before_persistence(self):
        with self.assertRaises(FinancialRecordOwnershipError):
            self.write(provider="stripe")


@override_settings(
    IS_LOCAL_ENV=True, CONNECTOR_CREDENTIAL_KEYS="", FIELD_ENCRYPTION_KEY="",
    SECRET_KEY="synthetic-encryption-unit-test-key",
)
class PreparedCredentialFieldTests(SimpleTestCase):
    def test_legacy_plaintext_read_and_encrypted_write_round_trip(self):
        field = LegacyPlaintextEncryptedTextField()
        token = "synthetic-github-token"
        self.assertEqual(field.from_db_value(token, None, None), token)
        stored = field.get_prep_value(token)
        self.assertNotEqual(stored, token)
        self.assertTrue(stored.startswith("mlai-enc:v1:"))
        self.assertEqual(field.from_db_value(stored, None, None), token)

    def test_corrupt_envelope_never_falls_back_to_plaintext(self):
        field = LegacyPlaintextEncryptedTextField()
        for value in ("mlai-enc:v1:missing:broken", "mlai-enc:v2:broken", "gAAAA-corrupt"):
            with self.subTest(value=value), self.assertRaises(CredentialEncryptionError):
                field.from_db_value(value, None, None)

    def test_empty_credentials_remain_empty(self):
        field = LegacyPlaintextEncryptedTextField()
        for value in (None, ""):
            self.assertEqual(field.from_db_value(value, None, None), value)
            self.assertEqual(field.get_prep_value(value), value)


class JobsQueueingTests(SimpleTestCase):
    @override_settings(ROO_API_KEY="synthetic-jobs-roo-key", INTERNAL_API_KEY="", MLAI_API_KEY="")
    def test_service_auth_preserves_legacy_keys_and_rejects_missing_credentials(self):
        from jobs.views import DailyRunTriggerView
        from rest_framework.test import APIRequestFactory
        factory = APIRequestFactory()
        with patch("jobs.views.enqueue_run_from_request") as enqueue:
            enqueue.return_value = SimpleNamespace(
                run_id="synthetic-run", run_date="2026-09-14", status="queued", full_list_url="",
            )
            for headers in (
                {"HTTP_X_API_KEY": "synthetic-jobs-roo-key"},
                {"HTTP_AUTHORIZATION": "Api-Key synthetic-jobs-roo-key"},
            ):
                response = DailyRunTriggerView.as_view()(
                    factory.post("/api/v1/jobs/daily-run", {}, format="json", **headers)
                )
                self.assertEqual(response.status_code, 202)
            enqueue.reset_mock()
            response = DailyRunTriggerView.as_view()(
                factory.post("/api/v1/jobs/daily-run", {}, format="json")
            )
            self.assertEqual(response.status_code, 401)
            enqueue.assert_not_called()

    def test_scheduler_enqueues_without_executing_any_jobs(self):
        from jobs.services import job_pipeline as pipeline
        now = timezone.now()
        config = SimpleNamespace(
            jobs_scheduler_enabled=True, jobs_schedule_hour=0,
            jobs_schedule_minute=0, jobs_retry_attempts=3,
            jobs_scheduler_post_to_slack=False, jobs_scheduler_post_to_notion=False,
            jobs_scheduler_max_pages=1, jobs_scheduler_per_keyword_limit=10,
        )
        with (
            patch.object(pipeline, "settings", config),
            patch.object(pipeline, "_scheduler_config_errors", return_value=[]),
            patch.object(pipeline, "_jobs_schedule_local_now", return_value=now),
            patch.object(pipeline.JobRun, "objects") as manager,
            patch.object(pipeline, "create_run") as create,
            patch.object(pipeline, "process_next_queued_run") as consume,
            patch.object(pipeline, "run_daily_jobs") as execute,
        ):
            query = manager.filter.return_value
            query.exists.return_value = False
            query.order_by.return_value.values_list.return_value.distinct.return_value = []
            query.order_by.return_value.__iter__.return_value = iter([])
            create.return_value = SimpleNamespace(run_id="new-run", run_date=now.date().isoformat())
            result = pipeline.enqueue_daily_jobs(now=now)
            self.assertEqual(result["status"], "queued")
            self.assertEqual(result["run_id"], "new-run")
            create.assert_called_once()
            consume.assert_not_called()
            execute.assert_not_called()


class QueueHealthTests(SimpleTestCase):
    def run_health(self, *, old_job=False, expired_claims=0, fail=False):
        from core.management.commands import queue_health as health
        now = timezone.now()
        output = StringIO()
        command = health.Command(stdout=output)
        with (
            patch.object(health.timezone, "now", return_value=now),
            patch.object(health.PasswordResetEmailDelivery, "objects") as emails,
            patch.object(health.JobRun, "objects") as jobs,
        ):
            pending_email, stale_email = Mock(), Mock()
            emails.filter.side_effect = [pending_email, stale_email]
            pending_email.count.return_value = 0
            pending_email.order_by.return_value.values_list.return_value.first.return_value = None
            stale_email.count.return_value = expired_claims
            pending_jobs, running_jobs = Mock(), Mock()
            jobs.filter.side_effect = [pending_jobs, running_jobs]
            pending_jobs.count.return_value = int(old_job)
            pending_jobs.order_by.return_value.values_list.return_value.first.return_value = (
                now - timedelta(seconds=600) if old_job else None
            )
            running_jobs.count.return_value = 0
            if fail:
                with self.assertRaises(CommandError):
                    command.handle(max_pending_seconds=300, fail_on_degraded=True)
            else:
                command.handle(max_pending_seconds=300, fail_on_degraded=False)
        return json.loads(output.getvalue())

    def test_empty_queues_are_reported_without_payloads(self):
        result = self.run_health()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["jobs"], {"queued": 0, "running": 0, "oldest_queued_seconds": 0})

    def test_overdue_job_and_expired_email_claims_are_degraded(self):
        for values in ({"old_job": True}, {"expired_claims": 1}):
            with self.subTest(values=values):
                self.assertEqual(self.run_health(**values, fail=True)["status"], "degraded")
