from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from io import StringIO
import threading
import time
from types import SimpleNamespace
from typing import Optional
from unittest import skipUnless
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from googleapiclient.errors import HttpError
from rest_framework import status
from rest_framework.test import APIClient

from content_factory.models import OrganizationContentConfig
from integrations import http_client
from organizations.models import Organization
from integrations.services.valley_harness import ValleyHarnessResult
from workflow_runs.models import (
    ContentFactoryApprovalState,
    ContentFactoryRun,
    ContentFactoryRunStatus,
    ContentFactoryRunStep,
    ContentFactoryRunStepAttempt,
    ContentFactoryStepStatus,
)
from integrations.models import (
    ExternalServiceConnection,
    ExternalServiceConnectionStatus,
    ExternalServiceProvider,
    GoogleConnection,
)
from startup_updates.models import (
    ArtifactProcessingStatus,
    GmailAttachmentArtifact,
    GmailMessageArtifact,
    GmailRelevanceLabel,
    GmailSyncCursor,
    GmailThreadArtifact,
    GoogleAnalyticsPropertySelection,
    MonthlyUpdateDraft,
    MonthlyUpdateDraftStatus,
    SlackChannelSelection,
    SlackMessageArtifact,
    SlackThreadArtifact,
    StartupEvent,
    StartupMetricObservation,
    StartupProfile,
    UserStartupBinding,
)
from integrations.services.gmail import (
    StaleHistoryCursorError,
    build_backfill_query,
    clean_email_text,
    default_backfill_window,
)
from integrations.services.google_analytics import (
    GA_REPORT_SPECS,
    period_bounds_for_run,
)
from integrations.services.slack_dm_mirror import revoke_user_grant
from integrations.services.slack_oauth_authority import (
    connection_slack_oauth_generation,
    stamp_slack_oauth_generation,
)
from startup_updates.services import (
    STARTUP_UPDATE_WORKFLOW,
    build_cancel_backup_for_draft,
    build_cancel_backup_for_event,
    build_cancel_backup_for_metric,
    cancel_startup_update_run,
    create_startup_update_run,
    render_monthly_update_markdown,
    score_message_for_profile,
    set_startup_update_run_cancel_backups,
    sync_startup_profile_from_company,
)
from startup_updates.slack_lineage import (
    pin_slack_run_authority,
    slack_run_authority_for_connection,
)

User = get_user_model()


class FakeSlackResponse:
    def __init__(self, payload: dict, *, status_code: int = 200, headers: Optional[dict] = None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400 and self.status_code != 429:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class StartupUpdateApiTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.api_key = "startup-api-key"
        self.headers = {"HTTP_X_API_KEY": self.api_key}

        self.user = User.objects.create_user(
            email="founder@example.com",
            password="test1234",
        )
        self.google_connection = GoogleConnection.objects.create(
            user=self.user,
            google_email="founder@gmail.com",
            refresh_token="refresh-token",
            scope="https://www.googleapis.com/auth/gmail.readonly",
        )
        self.organization = Organization.objects.create(
            name="Acme",
            domain="acme.com",
            competitors=["compete.io"],
            seed_keywords=["acme", "b2b workflow"],
        )
        self.config = OrganizationContentConfig.objects.create(
            organization=self.organization,
            brand_name="Acme AI",
            company_context="Acme automates back-office workflows.",
        )

    def _with_key(self):
        return self.settings(INTERNAL_API_KEY=self.api_key)


class StartupProfileAndRunViewTest(StartupUpdateApiTestCase):
    @patch("startup_updates.api_views.notify_valley_run_created")
    def test_profile_upsert_and_run_creation(self, mock_notify):
        with self._with_key():
            response = self.client.post(
                reverse("startup_updates_profile"),
                {
                    "user_id": self.user.id,
                    "domain": self.organization.domain,
                    "company_aliases": ["Acme", "Acme AI"],
                    "product_names": ["FlowPilot"],
                        "founder_names": ["Alice Founder"],
                        "investor_domains": ["fund.example"],
                        "kpi_definitions": [{"key": "arr", "label": "ARR"}],
                        "stage": "seed",
                    },
                    format="json",
                    **self.headers,
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        profile = StartupProfile.objects.get(organization=self.organization)
        binding = UserStartupBinding.objects.get(user=self.user, organization=self.organization)
        self.assertEqual(profile.product_names, ["FlowPilot"])
        self.assertEqual(profile.stage, "seed")
        self.assertEqual(binding.google_connection, self.google_connection)
        self.assertTrue(binding.is_default_for_gmail)

        with self.captureOnCommitCallbacks(execute=True):
            with self._with_key():
                response = self.client.post(
                    reverse("startup_updates_run"),
                    {
                        "user_id": self.user.id,
                        "domain": self.organization.domain,
                        "window_months": 6,
                        "target_month": "2026-03-01",
                    },
                    format="json",
                    **self.headers,
                )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        run_id = response.data["run"]["run_id"]
        run = ContentFactoryRun.objects.get(run_id=run_id)
        self.assertEqual(run.workflow, "startup_monthly_update")
        self.assertEqual(run.run_request["google_connection_id"], self.google_connection.id)
        self.assertEqual(run.run_request["window_months"], 6)
        self.assertEqual(run.run_request["target_month"], "2026-03-01")
        self.assertEqual(run.run_request["current_month"], "2026-03-01")
        self.assertEqual(run.run_request["draft_months"], ["2026-03-01"])
        self.assertEqual(run.run_request["startup_context"]["stage"], "seed")
        self.assertEqual(run.run_request["startup_context"]["company_aliases"], ["Acme", "Acme AI"])
        self.assertTrue(
            GmailSyncCursor.objects.filter(
                organization=self.organization,
                google_connection=self.google_connection,
            ).exists()
        )
        mock_notify.assert_called_once_with(run.run_id)

    @patch("startup_updates.api_views.notify_valley_run_created")
    def test_run_creation_reports_reused_existing_run_and_active_run(self, mock_notify):
        binding = UserStartupBinding.objects.create(
            user=self.user,
            organization=self.organization,
            google_connection=self.google_connection,
            is_default_for_gmail=True,
        )

        with self.captureOnCommitCallbacks(execute=True):
            with self._with_key():
                first = self.client.post(
                    reverse("startup_updates_run"),
                    {
                        "user_id": self.user.id,
                        "domain": self.organization.domain,
                        "window_months": 6,
                    },
                    format="json",
                    **self.headers,
                )

        with self._with_key():
            second = self.client.post(
                reverse("startup_updates_run"),
                {
                    "user_id": self.user.id,
                    "domain": self.organization.domain,
                    "window_months": 6,
                },
                format="json",
                **self.headers,
            )
            active = self.client.get(
                reverse("startup_updates_active_run"),
                {
                    "domain": self.organization.domain,
                    "binding_id": binding.id,
                    "google_connection_id": self.google_connection.id,
                },
                **self.headers,
            )

        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertFalse(first.data["reused_existing_run"])
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertTrue(second.data["reused_existing_run"])
        self.assertEqual(first.data["run_id"], second.data["run_id"])
        self.assertEqual(active.status_code, status.HTTP_200_OK)
        self.assertEqual(active.data["run_id"], first.data["run_id"])
        self.assertEqual(active.data["status"], ContentFactoryRunStatus.QUEUED)
        self.assertEqual(active.data["display_stage"], "Preparing company context")
        mock_notify.assert_called_once()

    @patch("startup_updates.services.timezone.now")
    @patch("startup_updates.api_views.notify_valley_run_created")
    def test_run_creation_returns_conflict_for_open_run_in_different_month(self, mock_notify, mock_now):
        mock_now.return_value = datetime(2026, 4, 26, 5, 30, tzinfo=dt_timezone.utc)
        binding = UserStartupBinding.objects.create(
            user=self.user,
            organization=self.organization,
            google_connection=self.google_connection,
            is_default_for_gmail=True,
        )
        active_run = create_startup_update_run(
            organization=self.organization,
            binding=binding,
            target_month=date(2026, 4, 1),
        )

        with self._with_key():
            response = self.client.post(
                reverse("startup_updates_run"),
                {
                    "user_id": self.user.id,
                    "domain": self.organization.domain,
                    "target_month": "2026-03-01",
                },
                format="json",
                **self.headers,
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["target_month_conflict"])
        self.assertEqual(response.data["requested_target_month"], "2026-03-01")
        self.assertEqual(response.data["active_target_month"], "2026-04-01")
        self.assertEqual(response.data["run_id"], active_run.run_id)
        self.assertEqual(ContentFactoryRun.objects.filter(workflow="startup_monthly_update").count(), 1)
        mock_notify.assert_not_called()

    @patch("startup_updates.api_views.notify_valley_run_created")
    def test_run_creation_returns_503_when_valley_dispatch_fails(self, mock_notify):
        UserStartupBinding.objects.create(
            user=self.user,
            organization=self.organization,
            google_connection=self.google_connection,
            is_default_for_gmail=True,
        )
        mock_notify.return_value = ValleyHarnessResult(
            ok=False,
            failure_kind="dns",
            detail="Failed to resolve 'valley-api'",
        )

        with self._with_key():
            response = self.client.post(
                reverse("startup_updates_run"),
                {
                    "user_id": self.user.id,
                    "domain": self.organization.domain,
                    "window_months": 6,
                },
                format="json",
                **self.headers,
            )

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(response.data["error"], "valley_dispatch_failed")
        run = ContentFactoryRun.objects.get(run_id=response.data["run_id"])
        self.assertEqual(run.result["_valley_meta"]["dispatch_status"], "failed")
        self.assertEqual(run.result["_valley_meta"]["last_dispatch_error_kind"], "dns")

    def test_run_status_and_draft_results_getters(self):
        binding = UserStartupBinding.objects.create(
            user=self.user,
            organization=self.organization,
            google_connection=self.google_connection,
            is_default_for_gmail=True,
        )
        run = create_startup_update_run(
            organization=self.organization,
            binding=binding,
            window_months=6,
        )
        run.status = ContentFactoryRunStatus.COMPLETED
        run.current_step = "groundedness_review"
        run.result = {
            "generated_draft_months": ["2026-03-01", "2026-02-01"],
            "_valley_meta": {"last_heartbeat_at": "2026-03-30T04:10:11+00:00"},
        }
        run.save(update_fields=["status", "current_step", "result", "updated_at"])
        MonthlyUpdateDraft.objects.create(
            organization=self.organization,
            run=run,
            month=datetime(2026, 3, 1, tzinfo=dt_timezone.utc).date(),
            status="ready",
            structured_memo={
                "highlights": ["Closed March pilot", "Expanded March revenue"],
                "lowlights": ["March hiring slowed", "March sales cycle lengthened"],
                "asks": ["March intro", "March hiring referral"],
                "kpi_snapshot": [{"metric_key": "revenue", "label": "Revenue", "value": "$45,000"}],
            },
            rendered_markdown="# March Update",
        )
        MonthlyUpdateDraft.objects.create(
            organization=self.organization,
            run=run,
            month=datetime(2026, 2, 1, tzinfo=dt_timezone.utc).date(),
            status="ready",
            structured_memo={
                "highlights": ["Launched February feature", "Closed February renewal"],
                "lowlights": ["February challenge"],
                "asks": ["February intro"],
                "kpi_snapshot": [{"metric_key": "mrr", "label": "MRR", "value": "$12,000"}],
            },
            rendered_markdown="# February Update",
        )

        with self._with_key():
            status_response = self.client.get(
                reverse("startup_updates_run_status", args=[run.run_id]),
                **self.headers,
            )
            results_response = self.client.get(
                reverse("startup_updates_draft_results", args=[run.run_id]),
                **self.headers,
            )

        self.assertEqual(status_response.status_code, status.HTTP_200_OK)
        self.assertEqual(status_response.data["run_id"], run.run_id)
        self.assertEqual(status_response.data["status"], ContentFactoryRunStatus.COMPLETED)
        self.assertEqual(status_response.data["display_stage"], "Final review")
        self.assertEqual(status_response.data["last_heartbeat_at"], "2026-03-30T04:10:11+00:00")
        self.assertEqual(status_response.data["generated_draft_months"], ["2026-03-01", "2026-02-01"])
        self.assertEqual(status_response.data["terminal_state"], ContentFactoryRunStatus.COMPLETED)

        self.assertEqual(results_response.status_code, status.HTTP_200_OK)
        self.assertEqual(results_response.data["run_id"], run.run_id)
        self.assertEqual(results_response.data["current_month"]["month"], "March")
        self.assertEqual(results_response.data["current_month"]["metrics"]["revenue"], "$45,000")
        self.assertEqual(results_response.data["current_month"]["highlights"], "Closed March pilot\nExpanded March revenue")
        self.assertEqual(results_response.data["current_month"]["challenges"], "March hiring slowed\nMarch sales cycle lengthened")
        self.assertEqual(results_response.data["current_month"]["asks"], "March intro\nMarch hiring referral")
        self.assertEqual(results_response.data["past_months"][0]["month"], "February")
        self.assertEqual(
            results_response.data["past_months"][0]["highlights"],
            "Launched February feature\nClosed February renewal",
        )
        self.assertEqual(results_response.data["draft"]["month"], "March")
        self.assertEqual(results_response.data["draft"]["highlights"], "Closed March pilot\nExpanded March revenue")
        self.assertEqual(results_response.data["draft"]["challenges"], "March hiring slowed\nMarch sales cycle lengthened")
        self.assertEqual(results_response.data["draft"]["asks"], "March intro\nMarch hiring referral")
        self.assertEqual(results_response.data["draft"]["pastMonths"][0]["month"], "February 2026")
        self.assertEqual(
            results_response.data["draft"]["pastMonths"][0]["highlights"],
            "Launched February feature\nClosed February renewal",
        )


class StartupUpdateCancellationTest(StartupUpdateApiTestCase):
    def setUp(self):
        super().setUp()
        self.binding = UserStartupBinding.objects.create(
            user=self.user,
            organization=self.organization,
            google_connection=self.google_connection,
            is_default_for_gmail=True,
        )

    def test_cancel_service_restores_previous_outputs_and_deletes_current_only_rows(self):
        older_run = create_startup_update_run(
            organization=self.organization,
            binding=self.binding,
            window_months=6,
        )
        older_run.status = ContentFactoryRunStatus.COMPLETED
        older_run.save(update_fields=["status", "updated_at"])

        restored_draft = MonthlyUpdateDraft.objects.create(
            organization=self.organization,
            run=older_run,
            month=datetime(2026, 3, 1, tzinfo=dt_timezone.utc).date(),
            status="ready",
            structured_memo={"highlights": ["Older March highlight"]},
            rendered_markdown="# Older March",
        )
        restored_event = StartupEvent.objects.create(
            organization=self.organization,
            run=older_run,
            canonical_key="march_customer_win",
            event_type="customer_win",
            title="Original customer win",
            summary="Original summary",
            month_bucket=datetime(2026, 3, 1, tzinfo=dt_timezone.utc).date(),
        )
        restored_metric = StartupMetricObservation.objects.create(
            organization=self.organization,
            run=older_run,
            metric_key="revenue",
            metric_name="Revenue",
            value_text="$45,000",
            value_number=Decimal("45000"),
            period_month=datetime(2026, 3, 1, tzinfo=dt_timezone.utc).date(),
            summary="Original revenue snapshot",
        )

        backups = {
            "drafts": {restored_draft.month.isoformat(): build_cancel_backup_for_draft(restored_draft)},
            "events": {restored_event.canonical_key: build_cancel_backup_for_event(restored_event)},
            "metrics": {
                f"{restored_metric.metric_key}:{restored_metric.period_month.isoformat()}:{restored_metric.value_text}":
                    build_cancel_backup_for_metric(restored_metric)
            },
        }

        current_run = create_startup_update_run(
            organization=self.organization,
            binding=self.binding,
            window_months=6,
        )
        current_run.status = ContentFactoryRunStatus.RUNNING
        current_run.current_step = "draft_generation"
        set_startup_update_run_cancel_backups(current_run, backups)
        current_run.save(update_fields=["status", "current_step", "result", "updated_at"])

        restored_draft.run = current_run
        restored_draft.structured_memo = {"highlights": ["Cancelled March highlight"]}
        restored_draft.rendered_markdown = "# Cancelled March"
        restored_draft.save(update_fields=["run", "structured_memo", "rendered_markdown", "updated_at"])

        restored_event.run = current_run
        restored_event.title = "Cancelled customer win"
        restored_event.summary = "Cancelled summary"
        restored_event.save(update_fields=["run", "title", "summary", "updated_at"])

        restored_metric.run = current_run
        restored_metric.summary = "Cancelled revenue snapshot"
        restored_metric.save(update_fields=["run", "summary", "updated_at"])

        MonthlyUpdateDraft.objects.create(
            organization=self.organization,
            run=current_run,
            month=datetime(2026, 2, 1, tzinfo=dt_timezone.utc).date(),
            status="draft",
            structured_memo={"highlights": ["Current-only February draft"]},
            rendered_markdown="",
        )
        StartupEvent.objects.create(
            organization=self.organization,
            run=current_run,
            canonical_key="february_launch",
            event_type="product_milestone",
            title="Current-only launch",
            month_bucket=datetime(2026, 2, 1, tzinfo=dt_timezone.utc).date(),
        )
        StartupMetricObservation.objects.create(
            organization=self.organization,
            run=current_run,
            metric_key="mrr",
            metric_name="MRR",
            value_text="$12,000",
            period_month=datetime(2026, 2, 1, tzinfo=dt_timezone.utc).date(),
            summary="Current-only MRR",
        )

        result = cancel_startup_update_run(
            run_id=current_run.run_id,
            organization=self.organization,
            binding_id=self.binding.id,
            google_connection_id=self.google_connection.id,
            cancelled_by_user_id=self.user.id,
        )

        current_run.refresh_from_db()
        restored_draft.refresh_from_db()
        restored_event.refresh_from_db()
        restored_metric.refresh_from_db()

        self.assertTrue(result["cancel_applied"])
        self.assertEqual(result["cleanup"]["drafts_deleted"], 1)
        self.assertEqual(result["cleanup"]["events_deleted"], 1)
        self.assertEqual(result["cleanup"]["metrics_deleted"], 1)
        self.assertEqual(current_run.status, ContentFactoryRunStatus.CANCELLED)

        self.assertEqual(restored_draft.run_id, older_run.id)
        self.assertEqual(restored_draft.structured_memo["highlights"], ["Older March highlight"])
        self.assertEqual(restored_event.run_id, older_run.id)
        self.assertEqual(restored_event.title, "Original customer win")
        self.assertEqual(restored_metric.run_id, older_run.id)
        self.assertEqual(restored_metric.summary, "Original revenue snapshot")

        self.assertFalse(MonthlyUpdateDraft.objects.filter(run=current_run, month=date(2026, 2, 1)).exists())
        self.assertFalse(StartupEvent.objects.filter(run=current_run, canonical_key="february_launch").exists())
        self.assertFalse(
            StartupMetricObservation.objects.filter(
                run=current_run,
                metric_key="mrr",
                period_month=date(2026, 2, 1),
            ).exists()
        )

    def test_cancelled_run_rejects_late_draft_result_writes(self):
        run = create_startup_update_run(
            organization=self.organization,
            binding=self.binding,
            window_months=6,
        )
        run.status = ContentFactoryRunStatus.CANCELLED
        run.save(update_fields=["status", "updated_at"])

        with self._with_key():
            response = self.client.post(
                reverse("startup_updates_draft_results", args=[run.run_id]),
                {
                    "drafts": [
                        {
                            "month": "2026-03-01",
                            "structured_memo": {"highlights": ["Should be rejected"]},
                        }
                    ]
                },
                format="json",
                **self.headers,
            )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["error"], "run_cancelled")


class StartupUpdateIngestViewTest(StartupUpdateApiTestCase):
    def setUp(self):
        super().setUp()
        self.profile = StartupProfile.objects.create(
            organization=self.organization,
            company_aliases=["Acme"],
            domain_aliases=["acme.com"],
            positive_keywords=["acme", "pilot"],
        )
        self.binding = UserStartupBinding.objects.create(
            user=self.user,
            organization=self.organization,
            google_connection=self.google_connection,
            is_default_for_gmail=True,
        )
        self.run = create_startup_update_run(
            organization=self.organization,
            binding=self.binding,
            window_months=6,
        )

    @patch("startup_updates.api_views.sync_message_metadata_page")
    def test_ingest_next_page_updates_cursor(self, mock_sync):
        artifact = GmailMessageArtifact.objects.create(
            organization=self.organization,
            google_connection=self.google_connection,
            gmail_message_id="msg-1",
            gmail_thread_id="thread-1",
            internal_date=timezone.now(),
            subject="ACME pilot expansion",
            from_address="ceo@acme.com",
            relevance_label=GmailRelevanceLabel.RELEVANT,
            needs_thread_context=True,
        )
        mock_sync.return_value = {
            "query": "after:1 before:2 -in:spam -in:trash",
            "result_size_estimate": 1,
            "next_page_token": None,
            "artifacts": [artifact],
        }

        with self._with_key():
            response = self.client.post(
                reverse("startup_updates_ingest_next_page", args=[self.run.run_id]),
                {},
                format="json",
                **self.headers,
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        cursor = GmailSyncCursor.objects.get(
            organization=self.organization,
            google_connection=self.google_connection,
        )
        self.assertIsNotNone(cursor.backfill_completed_at)
        self.assertEqual(response.data["ingested_count"], 1)
        self.assertEqual(response.data["relevance_counts"]["relevant"], 1)

    def test_ingest_next_page_returns_source_unavailable_when_gmail_scope_missing(self):
        self.google_connection.scope = "openid https://www.googleapis.com/auth/userinfo.email"
        self.google_connection.save(update_fields=["scope", "updated_at"])

        with self._with_key():
            response = self.client.post(
                reverse("startup_updates_ingest_next_page", args=[self.run.run_id]),
                {},
                format="json",
                **self.headers,
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["sourceUnavailable"])
        self.assertEqual(response.data["source"], "gmail")
        self.assertEqual(response.data["code"], "gmail_insufficient_scope")
        self.assertFalse(response.data["retryable"])
        self.assertFalse(response.data["hasGmailScope"])
        self.assertTrue(response.data["needsGmailReconnect"])
        self.assertEqual(response.data["ingested_count"], 0)

    @patch("startup_updates.api_views.sync_message_metadata_page")
    def test_ingest_next_page_converts_gmail_403_to_source_unavailable(self, mock_sync):
        mock_sync.side_effect = HttpError(
            resp=SimpleNamespace(status=403, reason="insufficientPermissions"),
            content=b'{"error":{"message":"Request had insufficient authentication scopes."}}',
        )

        with self._with_key():
            response = self.client.post(
                reverse("startup_updates_ingest_next_page", args=[self.run.run_id]),
                {},
                format="json",
                **self.headers,
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["sourceUnavailable"])
        self.assertEqual(response.data["code"], "gmail_insufficient_scope")
        self.assertFalse(response.data["retryable"])

    @patch("startup_updates.api_views.sync_message_metadata_page")
    @patch("startup_updates.api_views.sync_history_metadata_page")
    def test_incremental_ingest_falls_back_when_history_cursor_is_stale(self, mock_history_sync, mock_backfill_sync):
        cursor = GmailSyncCursor.objects.get(
            organization=self.organization,
            google_connection=self.google_connection,
        )
        cursor.last_history_id = "12345"
        cursor.save(update_fields=["last_history_id", "updated_at"])

        artifact = GmailMessageArtifact.objects.create(
            organization=self.organization,
            google_connection=self.google_connection,
            gmail_message_id="msg-fallback",
            gmail_thread_id="thread-fallback",
            internal_date=timezone.now(),
            subject="Fallback window",
            from_address="ceo@acme.com",
            relevance_label=GmailRelevanceLabel.AMBIGUOUS,
        )
        mock_history_sync.side_effect = StaleHistoryCursorError("stale")
        mock_backfill_sync.return_value = {
            "mode": "backfill",
            "result_size_estimate": 1,
            "next_page_token": None,
            "artifacts": [artifact],
            "cursor_reset": True,
        }

        with self._with_key():
            response = self.client.post(
                reverse("startup_updates_ingest_next_page", args=[self.run.run_id]),
                {"mode": "incremental"},
                format="json",
                **self.headers,
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        cursor.refresh_from_db()
        self.assertEqual(response.data["mode"], "backfill")
        self.assertTrue(response.data["cursor_reset"])

    @patch("integrations.services.gmail.build_gmail_service")
    @patch("integrations.services.gmail.get_message_metadata")
    @patch("integrations.services.gmail.list_message_page")
    def test_ingest_next_page_persists_hard_filtered_messages_as_irrelevant(
        self,
        mock_list_message_page,
        mock_get_message_metadata,
        _mock_build_gmail_service,
    ):
        message_id = "msg-hard-filter"
        internal_date = timezone.now()
        mock_list_message_page.return_value = {
            "messages": [{"id": message_id}],
            "resultSizeEstimate": 1,
            "nextPageToken": None,
        }
        mock_get_message_metadata.return_value = {
            "id": message_id,
            "threadId": "thread-hard-filter",
            "historyId": "42",
            "internalDate": str(int(internal_date.timestamp() * 1000)),
            "labelIds": ["INBOX", "CATEGORY_PROMOTIONS"],
            "snippet": "Use this magic link to sign in.",
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "Magic link to sign in"},
                    {"name": "From", "value": "No Reply <noreply@example.com>"},
                    {"name": "List-Unsubscribe", "value": "<mailto:unsubscribe@example.com>"},
                    {"name": "Precedence", "value": "bulk"},
                ]
            },
        }

        with self._with_key():
            response = self.client.post(
                reverse("startup_updates_ingest_next_page", args=[self.run.run_id]),
                {},
                format="json",
                **self.headers,
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        artifact = GmailMessageArtifact.objects.get(
            organization=self.organization,
            gmail_message_id=message_id,
        )
        self.assertEqual(artifact.relevance_label, GmailRelevanceLabel.IRRELEVANT)
        self.assertFalse(artifact.needs_thread_context)
        self.assertIn("hard_filtered_gmail_category", artifact.heuristic_reasons)
        self.assertEqual(response.data["relevance_counts"]["irrelevant"], 1)

    @patch("integrations.services.gmail.build_gmail_service")
    @patch("integrations.services.gmail.get_message_metadata")
    @patch("integrations.services.gmail.list_message_page")
    def test_ingest_next_page_reuses_existing_metadata_without_refetch(
        self,
        mock_list_message_page,
        mock_get_message_metadata,
        _mock_build_gmail_service,
    ):
        artifact = GmailMessageArtifact.objects.create(
            organization=self.organization,
            google_connection=self.google_connection,
            gmail_message_id="msg-existing",
            gmail_thread_id="thread-existing",
            history_id="99",
            internal_date=timezone.now(),
            subject="Existing Acme update",
            from_address="founder@acme.com",
            relevance_label=GmailRelevanceLabel.RELEVANT,
            needs_thread_context=True,
            metadata_hydrated_at=timezone.now(),
        )
        mock_list_message_page.return_value = {
            "messages": [{"id": artifact.gmail_message_id}],
            "resultSizeEstimate": 1,
            "nextPageToken": None,
        }

        with self._with_key():
            response = self.client.post(
                reverse("startup_updates_ingest_next_page", args=[self.run.run_id]),
                {},
                format="json",
                **self.headers,
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["reused_existing_count"], 1)
        self.assertEqual(response.data["message_ids"], [artifact.gmail_message_id])
        mock_get_message_metadata.assert_not_called()


class StartupUpdateSlackBackfillViewTest(StartupUpdateApiTestCase):
    def setUp(self):
        super().setUp()
        self.profile = StartupProfile.objects.create(
            organization=self.organization,
            company_aliases=["Acme"],
            domain_aliases=["acme.com"],
        )
        self.binding = UserStartupBinding.objects.create(
            user=self.user,
            organization=self.organization,
            google_connection=self.google_connection,
            is_default_for_gmail=True,
        )
        self.connection = ExternalServiceConnection.objects.create(
            provider=ExternalServiceProvider.SLACK,
            user=self.user,
            organization=self.organization,
            access_token="xoxp-token",
            external_account_id="T123",
            account_label="Acme Slack",
        )
        self.selection = SlackChannelSelection.objects.create(
            connection=self.connection,
            user=self.user,
            organization=self.organization,
            channel_id="C123",
            channel_name="wins",
            selected=True,
        )
        self.run = create_startup_update_run(
            organization=self.organization,
            binding=self.binding,
            input_sources=["gmail", "slack"],
        )

    def _slack_message(self, ts: str, text: str, *, reply_count: int = 0, thread_ts: Optional[str] = None):
        payload = {
            "type": "message",
            "ts": ts,
            "user": "U123",
            "text": text,
            "user_profile": {"real_name": "Sam"},
        }
        if reply_count:
            payload["reply_count"] = reply_count
        if thread_ts:
            payload["thread_ts"] = thread_ts
        return payload

    def _thread_public_id(self, thread: SlackThreadArtifact) -> str:
        return slack_run_authority_for_connection(self.connection).thread_public_id(
            thread.channel_id,
            thread.thread_ts,
        )

    def test_slack_backfill_processes_one_history_page_and_resumes(self):
        first_page = FakeSlackResponse(
            {
                "ok": True,
                "messages": [self._slack_message("1770000000.000100", "First win")],
                "response_metadata": {"next_cursor": "cursor-2"},
            }
        )
        second_page = FakeSlackResponse(
            {
                "ok": True,
                "messages": [self._slack_message("1770000001.000100", "Second win")],
                "response_metadata": {"next_cursor": ""},
            }
        )

        with self.settings(INTERNAL_API_KEY=self.api_key, SLACK_SYNC_REPLY_PAGE_BUDGET=0):
            with patch("integrations.services.external_connectors.requests.get", side_effect=[first_page, second_page]) as mock_get:
                first_response = self.client.post(
                    reverse("startup_updates_slack_backfill", args=[self.run.run_id]),
                    {},
                    format="json",
                    **self.headers,
                )
                self.selection.refresh_from_db()
                second_response = self.client.post(
                    reverse("startup_updates_slack_backfill", args=[self.run.run_id]),
                    {},
                    format="json",
                    **self.headers,
                )

        self.assertEqual(first_response.status_code, status.HTTP_200_OK)
        self.assertTrue(first_response.data["has_more"])
        self.assertEqual(self.selection.sync_cursor["history_cursor"], "cursor-2")
        self.assertEqual(mock_get.call_args_list[0].kwargs["params"]["limit"], 100)
        self.assertEqual(second_response.status_code, status.HTTP_200_OK)
        self.assertFalse(second_response.data["has_more"])
        self.selection.refresh_from_db()
        self.assertTrue(self.selection.sync_cursor["run_backfill_complete"])
        self.assertEqual(SlackMessageArtifact.objects.filter(organization=self.organization).count(), 2)
        self.assertEqual(SlackThreadArtifact.objects.filter(organization=self.organization).count(), 2)

    def test_slack_backfill_processes_reply_cursor_across_calls(self):
        history_page = FakeSlackResponse(
            {
                "ok": True,
                "messages": [self._slack_message("1770000000.000100", "Root", reply_count=2)],
                "response_metadata": {"next_cursor": ""},
            }
        )
        first_replies = FakeSlackResponse(
            {
                "ok": True,
                "messages": [
                    self._slack_message("1770000000.000100", "Root", thread_ts="1770000000.000100"),
                    self._slack_message("1770000000.000200", "Reply 1", thread_ts="1770000000.000100"),
                ],
                "response_metadata": {"next_cursor": "reply-cursor-2"},
            }
        )
        second_replies = FakeSlackResponse(
            {
                "ok": True,
                "messages": [self._slack_message("1770000000.000300", "Reply 2", thread_ts="1770000000.000100")],
                "response_metadata": {"next_cursor": ""},
            }
        )

        with self.settings(INTERNAL_API_KEY=self.api_key, SLACK_SYNC_REPLY_PAGE_BUDGET=1):
            with patch("integrations.services.external_connectors.requests.get", side_effect=[history_page, first_replies, second_replies]):
                first_response = self.client.post(
                    reverse("startup_updates_slack_backfill", args=[self.run.run_id]),
                    {},
                    format="json",
                    **self.headers,
                )
                self.selection.refresh_from_db()
                second_response = self.client.post(
                    reverse("startup_updates_slack_backfill", args=[self.run.run_id]),
                    {},
                    format="json",
                    **self.headers,
                )

        self.assertEqual(first_response.status_code, status.HTTP_200_OK)
        self.assertTrue(first_response.data["has_more"])
        self.assertEqual(
            self.selection.sync_cursor["pending_replies"],
            [{"thread_ts": "1770000000.000100", "cursor": "reply-cursor-2"}],
        )
        self.assertEqual(second_response.status_code, status.HTTP_200_OK)
        self.assertFalse(second_response.data["has_more"])
        thread = SlackThreadArtifact.objects.get(organization=self.organization, thread_ts="1770000000.000100")
        self.assertEqual(thread.source_message_count, 3)

    def test_slack_backfill_timeout_returns_503_and_records_error(self):
        with self.settings(INTERNAL_API_KEY=self.api_key):
            with patch(
                "integrations.services.external_connectors.requests.get",
                side_effect=http_client.exceptions.Timeout("read timed out"),
            ):
                response = self.client.post(
                    reverse("startup_updates_slack_backfill", args=[self.run.run_id]),
                    {},
                    format="json",
                    **self.headers,
                )

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.status, ExternalServiceConnectionStatus.ERROR)
        self.assertIn("read timed out", self.connection.last_error)

    def test_slack_backfill_rate_limit_returns_retry_after_without_error(self):
        rate_limited = FakeSlackResponse(
            {"ok": False, "error": "rate_limited"},
            status_code=429,
            headers={"Retry-After": "17"},
        )

        with self.settings(INTERNAL_API_KEY=self.api_key):
            with patch("integrations.services.external_connectors.requests.get", return_value=rate_limited):
                response = self.client.post(
                    reverse("startup_updates_slack_backfill", args=[self.run.run_id]),
                    {},
                    format="json",
                    **self.headers,
                )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["has_more"])
        self.assertEqual(response.data["retry_after_seconds"], 17)
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.status, ExternalServiceConnectionStatus.SYNCING)
        self.assertEqual(self.connection.last_error, "")

    def test_slack_classification_batch_hard_filters_noise_and_returns_candidates(self):
        latest_message_at = timezone.now() - timedelta(minutes=1)
        noisy_thread = SlackThreadArtifact.objects.create(
            organization=self.organization,
            connection=self.connection,
            channel_id="C123",
            channel_name="wins",
            thread_ts="1770000000.000100",
            source_message_ids=["slack:C123:1770000000.000100"],
            source_message_count=1,
            cleaned_text="[2026-03-15T12:00:00+00:00] Standup Bot: daily standup reminder",
            message_payloads=[
                {
                    "message_id": "slack:C123:1770000000.000100",
                    "author_name": "Standup Bot",
                    "posted_at": latest_message_at.isoformat(),
                    "cleaned_text": "daily standup reminder",
                }
            ],
            latest_message_at=latest_message_at,
            extraction_status=ArtifactProcessingStatus.HYDRATED,
        )
        candidate_thread = SlackThreadArtifact.objects.create(
            organization=self.organization,
            connection=self.connection,
            channel_id="C123",
            channel_name="wins",
            thread_ts="1770000001.000100",
            source_message_ids=["slack:C123:1770000001.000100"],
            source_message_count=1,
            cleaned_text="[2026-03-15T12:01:00+00:00] Sam: Acme signed a $12k MRR pilot.",
            message_payloads=[
                {
                    "message_id": "slack:C123:1770000001.000100",
                    "author_name": "Sam",
                    "posted_at": latest_message_at.isoformat(),
                    "cleaned_text": "Acme signed a $12k MRR pilot.",
                }
            ],
            latest_message_at=latest_message_at,
            extraction_status=ArtifactProcessingStatus.HYDRATED,
        )

        with self.settings(INTERNAL_API_KEY=self.api_key):
            response = self.client.get(
                reverse("startup_updates_slack_classification_batch", args=[self.run.run_id]),
                {"limit": 10},
                **self.headers,
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(
            response.data["threads"][0]["slack_thread_id"],
            self._thread_public_id(candidate_thread),
        )
        noisy_thread.refresh_from_db()
        self.assertEqual(noisy_thread.relevance_label, GmailRelevanceLabel.IRRELEVANT)
        self.assertFalse(noisy_thread.needs_extraction)

    def test_slack_classification_results_gate_extraction_batch(self):
        latest_message_at = timezone.now() - timedelta(minutes=1)
        relevant_thread = SlackThreadArtifact.objects.create(
            organization=self.organization,
            connection=self.connection,
            channel_id="C123",
            channel_name="wins",
            thread_ts="1770000002.000100",
            source_message_ids=["slack:C123:1770000002.000100", "slack:C123:1770000002.000200"],
            source_message_count=2,
            cleaned_text="\n".join(
                [
                    "[2026-03-15T12:00:00+00:00] Sam: Acme signed a $12k MRR pilot.",
                    "[2026-03-15T12:01:00+00:00] Alex: thanks",
                ]
            ),
            message_payloads=[
                {
                    "message_id": "slack:C123:1770000002.000100",
                    "author_name": "Sam",
                    "posted_at": latest_message_at.isoformat(),
                    "cleaned_text": "Acme signed a $12k MRR pilot.",
                },
                {
                    "message_id": "slack:C123:1770000002.000200",
                    "author_name": "Alex",
                    "posted_at": latest_message_at.isoformat(),
                    "cleaned_text": "thanks",
                },
            ],
            latest_message_at=latest_message_at,
            extraction_status=ArtifactProcessingStatus.HYDRATED,
        )
        SlackThreadArtifact.objects.create(
            organization=self.organization,
            connection=self.connection,
            channel_id="C123",
            channel_name="wins",
            thread_ts="1770000003.000100",
            source_message_ids=["slack:C123:1770000003.000100"],
            source_message_count=1,
            cleaned_text="[2026-03-15T12:03:00+00:00] Alex: thanks",
            message_payloads=[
                {
                    "message_id": "slack:C123:1770000003.000100",
                    "author_name": "Alex",
                    "posted_at": latest_message_at.isoformat(),
                    "cleaned_text": "thanks",
                }
            ],
            latest_message_at=latest_message_at,
            extraction_status=ArtifactProcessingStatus.HYDRATED,
            relevance_label=GmailRelevanceLabel.IRRELEVANT,
            classified_at=timezone.now(),
        )

        with self.settings(INTERNAL_API_KEY=self.api_key):
            results_response = self.client.post(
                reverse("startup_updates_slack_classification_results", args=[self.run.run_id]),
                {
                    "results": [
                        {
                            "slack_thread_id": self._thread_public_id(relevant_thread),
                            "relevance_label": GmailRelevanceLabel.RELEVANT,
                            "relevance_score": 0.97,
                            "relevance_reason": "Customer and MRR update.",
                            "needs_extraction": True,
                            "extraction_hints": {
                                "important_message_ids": ["slack:C123:1770000002.000100"],
                                "extraction_hint": "Extract pilot conversion.",
                            },
                        }
                    ]
                },
                format="json",
                **self.headers,
            )
            batch_response = self.client.get(
                reverse("startup_updates_slack_extraction_batch", args=[self.run.run_id]),
                {"limit": 10},
                **self.headers,
            )

        self.assertEqual(results_response.status_code, status.HTTP_200_OK)
        self.assertEqual(batch_response.status_code, status.HTTP_200_OK)
        self.assertEqual(batch_response.data["count"], 1)
        bundle = batch_response.data["threads"][0]
        self.assertEqual(
            bundle["slack_thread_id"],
            self._thread_public_id(relevant_thread),
        )
        self.assertEqual(bundle["relevance_score"], 0.97)
        self.assertIn("compression", bundle["participant_summary"])

    def _create_extractable_slack_thread(
        self,
        *,
        thread_ts: str = "1770000010.000100",
    ) -> SlackThreadArtifact:
        return SlackThreadArtifact.objects.create(
            organization=self.organization,
            connection=self.connection,
            channel_id="C123",
            channel_name="wins",
            thread_ts=thread_ts,
            source_message_ids=[f"slack:C123:{thread_ts}"],
            source_message_count=1,
            cleaned_text="[2026-03-15T12:00:00+00:00] Sam: private launch detail",
            message_payloads=[
                {
                    "message_id": f"slack:C123:{thread_ts}",
                    "author_name": "Sam",
                    "cleaned_text": "private launch detail",
                }
            ],
            latest_message_at=timezone.now() - timedelta(minutes=1),
            relevance_label=GmailRelevanceLabel.RELEVANT,
            relevance_score=0.95,
            needs_extraction=True,
            extraction_status=ArtifactProcessingStatus.HYDRATED,
        )

    def _extract_thread(self, thread: SlackThreadArtifact) -> str:
        with self._with_key():
            batch = self.client.get(
                reverse(
                    "startup_updates_slack_extraction_batch",
                    args=[self.run.run_id],
                ),
                {"limit": 10},
                **self.headers,
            )
        self.assertEqual(batch.status_code, status.HTTP_200_OK)
        public_id = batch.data["threads"][0]["slack_thread_id"]
        with self._with_key():
            response = self.client.post(
                reverse(
                    "startup_updates_slack_extraction_results",
                    args=[self.run.run_id],
                ),
                {
                    "results": [
                        {
                            "slack_thread_id": public_id,
                            "events": [
                                {
                                    "canonical_key": "launch-win",
                                    "event_type": "product_milestone",
                                    "title": "Private launch",
                                    "summary": "Private Slack launch detail",
                                    "month_bucket": "2026-03-01",
                                    # The server must force exact lineage even
                                    # when the extractor omits it.
                                    "source_thread_ids": [],
                                    "confidence": 0.9,
                                }
                            ],
                            "metrics": [
                                {
                                    "metric_key": "private_pipeline",
                                    "metric_name": "Private pipeline",
                                    "value_text": "$12k",
                                    "period_month": "2026-03-01",
                                    "summary": "Private Slack metric",
                                }
                            ],
                        }
                    ]
                },
                format="json",
                **self.headers,
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return public_id

    def test_slack_authority_present_but_malformed_fails_closed(self):
        run_request = dict(self.run.run_request or {})
        external_context = dict(run_request.get("external_context") or {})
        external_context["slack"] = {
            "authority": {
                "version": 2,
                "connection_id": "corrupt",
            }
        }
        run_request["external_context"] = external_context
        self.run.run_request = run_request
        self.run.save(update_fields=["run_request", "updated_at"])

        with self._with_key():
            response = self.client.get(
                reverse(
                    "startup_updates_slack_classification_batch",
                    args=[self.run.run_id],
                ),
                {"limit": 10},
                **self.headers,
            )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.run.refresh_from_db()
        self.assertEqual(
            self.run.run_request["external_context"]["slack"]["authority"][
                "connection_id"
            ],
            "corrupt",
        )

    def test_slack_input_run_is_pinned_before_first_worker_callback(self):
        slack_context = self.run.run_request["external_context"]["slack"]

        self.assertEqual(
            slack_context["authority"]["connection_id"],
            self.connection.pk,
        )
        self.assertEqual(slack_context["authority"]["oauth_generation"], 0)
        self.assertEqual(self.run.run_request["slack_channel_ids"], ["C123"])

    @patch("integrations.services.slack_dm_mirror._revoke_remote_token")
    def test_disconnect_before_first_slack_worker_cancels_exact_run(
        self,
        _mock_remote_revoke,
    ):
        revoke_user_grant(self.user)

        self.run.refresh_from_db()
        self.assertEqual(self.run.status, ContentFactoryRunStatus.CANCELLED)
        self.assertEqual(
            self.run.run_request["external_context"]["slack"]["authority"][
                "oauth_generation"
            ],
            0,
        )

    @patch("integrations.services.slack_dm_mirror._revoke_remote_token")
    def test_disconnect_erases_orphaned_exact_outputs_and_transitive_draft(
        self,
        _mock_remote_revoke,
    ):
        authority = slack_run_authority_for_connection(self.connection)
        public_id = authority.thread_public_id("C123", "1770000040.000100")
        orphan_event = StartupEvent.objects.create(
            organization=self.organization,
            run=None,
            canonical_key="orphan-slack-event",
            event_type="product_milestone",
            title="Orphan private Slack event",
            month_bucket=date(2026, 3, 1),
            source_thread_ids=[public_id],
        )
        orphan_metric = StartupMetricObservation.objects.create(
            organization=self.organization,
            run=None,
            source_provider="slack",
            metric_key="orphan_slack_metric",
            metric_name="Orphan Slack metric",
            value_text="private",
            period_month=date(2026, 3, 1),
            source_metadata={
                "slack_connection_id": self.connection.pk,
                "slack_thread_id": public_id,
            },
        )
        orphan_draft = MonthlyUpdateDraft.objects.create(
            organization=self.organization,
            run=None,
            month=date(2026, 3, 1),
            structured_memo={"summary": "Orphan private Slack draft"},
            evidence_event_ids=[orphan_event.pk],
            evidence_metric_ids=[orphan_metric.pk],
        )

        revoke_user_grant(self.user)

        self.assertFalse(StartupEvent.objects.filter(pk=orphan_event.pk).exists())
        self.assertFalse(
            StartupMetricObservation.objects.filter(pk=orphan_metric.pk).exists()
        )
        self.assertFalse(
            MonthlyUpdateDraft.objects.filter(pk=orphan_draft.pk).exists()
        )

    @patch("integrations.services.slack_dm_mirror._revoke_remote_token")
    def test_missing_backup_run_fails_closed_for_private_draft_restore(
        self,
        _mock_remote_revoke,
    ):
        draft = MonthlyUpdateDraft.objects.create(
            organization=self.organization,
            run=self.run,
            month=date(2026, 3, 1),
            structured_memo={"summary": "Current private Slack draft"},
        )
        snapshot = build_cancel_backup_for_draft(draft)
        snapshot["run_id"] = "deleted-prior-run"
        snapshot["structured_memo"] = {
            "summary": "Private Slack content with missing provenance"
        }
        backups = {"drafts": {draft.month.isoformat(): snapshot}}
        set_startup_update_run_cancel_backups(self.run, backups)
        self.run.save(update_fields=["result", "updated_at"])

        revoke_user_grant(self.user)

        self.run.refresh_from_db()
        self.assertFalse(MonthlyUpdateDraft.objects.filter(pk=draft.pk).exists())
        self.assertNotIn("missing provenance", str(self.run.result))

    @patch("integrations.services.slack_dm_mirror._revoke_remote_token")
    def test_disconnect_erases_cross_run_draft_candidate_and_hidden_backup(
        self,
        _mock_remote_revoke,
    ):
        authority = slack_run_authority_for_connection(self.connection)
        public_id = authority.thread_public_id("C123", "1770000050.000100")
        slack_event = StartupEvent.objects.create(
            organization=self.organization,
            run=self.run,
            canonical_key="cross-run-slack-event",
            event_type="product_milestone",
            title="Private cross-run Slack event",
            month_bucket=date(2026, 3, 1),
            source_thread_ids=[public_id],
        )
        gmail_run = ContentFactoryRun.objects.create(
            run_id="gmail-only-derived-run",
            workflow=STARTUP_UPDATE_WORKFLOW,
            organization=self.organization,
            domain=self.organization.domain,
            run_request={
                "binding_id": self.binding.pk,
                "input_sources": ["gmail"],
            },
            result={
                "update_candidates": [
                    {
                        "event_id": slack_event.pk,
                        "summary": "Private Slack candidate copied cross-run",
                    }
                ]
            },
        )
        derived_draft = MonthlyUpdateDraft.objects.create(
            organization=self.organization,
            run=gmail_run,
            month=date(2026, 3, 1),
            structured_memo={"summary": "Private Slack draft copied cross-run"},
            evidence_event_ids=[slack_event.pk],
        )
        snapshot = build_cancel_backup_for_event(slack_event)
        current_event = slack_event
        current_event.run = gmail_run
        current_event.title = "Current Gmail-only replacement"
        current_event.source_thread_ids = ["gmail:replacement-thread"]
        current_event.evidence_message_ids = ["gmail:replacement-message"]
        current_event.save(
            update_fields=[
                "run",
                "title",
                "source_thread_ids",
                "evidence_message_ids",
                "updated_at",
            ]
        )
        set_startup_update_run_cancel_backups(
            gmail_run,
            {"events": {current_event.canonical_key: snapshot}},
        )
        gmail_run.save(update_fields=["result", "updated_at"])

        revoke_user_grant(self.user)

        gmail_run.refresh_from_db()
        current_event.refresh_from_db()
        self.assertNotEqual(gmail_run.status, ContentFactoryRunStatus.CANCELLED)
        self.assertEqual(current_event.title, "Current Gmail-only replacement")
        self.assertFalse(
            MonthlyUpdateDraft.objects.filter(pk=derived_draft.pk).exists()
        )
        self.assertNotIn("Private Slack", str(gmail_run.result))

    @patch("integrations.services.slack_dm_mirror._revoke_remote_token")
    def test_disconnect_erases_exact_slack_lineage_and_preserves_mixed_sources(
        self,
        _mock_remote_revoke,
    ):
        prior_run = ContentFactoryRun.objects.create(
            run_id="prior-gmail-run",
            workflow=STARTUP_UPDATE_WORKFLOW,
            organization=self.organization,
            domain=self.organization.domain,
            run_request={"input_sources": ["gmail"]},
        )
        StartupEvent.objects.create(
            organization=self.organization,
            run=prior_run,
            canonical_key="launch-win",
            event_type="product_milestone",
            title="Prior Gmail launch",
            month_bucket=date(2026, 3, 1),
            source_thread_ids=["gmail:thread-1"],
        )
        unrelated_event = StartupEvent.objects.create(
            organization=self.organization,
            run=self.run,
            canonical_key="gmail-only",
            event_type="product_milestone",
            title="Gmail-only event",
            month_bucket=date(2026, 3, 1),
            source_thread_ids=["gmail:thread-2"],
        )
        unrelated_metric = StartupMetricObservation.objects.create(
            organization=self.organization,
            run=self.run,
            source_provider="gmail",
            metric_key="gmail_metric",
            metric_name="Gmail metric",
            value_text="7",
            period_month=date(2026, 3, 1),
        )
        thread = self._create_extractable_slack_thread()
        public_id = self._extract_thread(thread)
        slack_event = StartupEvent.objects.get(canonical_key="launch-win")
        slack_metric = StartupMetricObservation.objects.get(
            metric_key="private_pipeline"
        )
        self.assertEqual(slack_event.source_thread_ids[0], public_id)
        self.assertEqual(
            slack_metric.source_metadata["slack_connection_id"],
            self.connection.pk,
        )
        draft = MonthlyUpdateDraft.objects.create(
            organization=self.organization,
            run=self.run,
            month=date(2026, 3, 1),
            structured_memo={"summary": "Private Slack launch detail"},
            rendered_markdown="Private Slack launch detail",
            evidence_event_ids=[slack_event.pk],
            evidence_metric_ids=[slack_metric.pk],
        )

        revoke_user_grant(self.user)

        self.connection.refresh_from_db()
        self.run.refresh_from_db()
        self.assertEqual(
            self.connection.status,
            ExternalServiceConnectionStatus.DISCONNECTED,
        )
        self.assertEqual(connection_slack_oauth_generation(self.connection), 1)
        restored_event = StartupEvent.objects.get(canonical_key="launch-win")
        self.assertEqual(restored_event.run, prior_run)
        self.assertEqual(restored_event.title, "Prior Gmail launch")
        self.assertTrue(StartupEvent.objects.filter(pk=unrelated_event.pk).exists())
        self.assertTrue(
            StartupMetricObservation.objects.filter(pk=unrelated_metric.pk).exists()
        )
        self.assertFalse(
            StartupMetricObservation.objects.filter(pk=slack_metric.pk).exists()
        )
        self.assertFalse(MonthlyUpdateDraft.objects.filter(pk=draft.pk).exists())
        self.assertFalse(SlackThreadArtifact.objects.filter(pk=thread.pk).exists())
        self.assertEqual(self.run.status, ContentFactoryRunStatus.CANCELLED)
        self.assertEqual(self.run.run_request["slack_channel_ids"], [])
        slack_context = self.run.run_request["external_context"]["slack"]
        self.assertTrue(slack_context["source_unavailable"])
        # The tombstone retains the generation the worker actually observed;
        # the connection row advances separately to generation 1.
        self.assertEqual(slack_context["authority"]["oauth_generation"], 0)
        with self._with_key():
            stale_draft_response = self.client.post(
                reverse(
                    "startup_updates_draft_results",
                    args=[self.run.run_id],
                ),
                {
                    "drafts": [
                        {
                            "month": "2026-03-01",
                            "structured_memo": {"summary": "stale Slack result"},
                        }
                    ]
                },
                format="json",
                **self.headers,
            )
        self.assertEqual(
            stale_draft_response.status_code,
            status.HTTP_409_CONFLICT,
        )
        self.assertFalse(MonthlyUpdateDraft.objects.filter(run=self.run).exists())

    @patch("integrations.services.slack_dm_mirror._revoke_remote_token")
    def test_disconnect_does_not_match_other_connection_legacy_ids(
        self,
        _mock_remote_revoke,
    ):
        thread = self._create_extractable_slack_thread(
            thread_ts="1770000020.000100"
        )
        other_user = User.objects.create_user(
            email="other-founder@example.com",
            password="test1234",
        )
        other_binding = UserStartupBinding.objects.create(
            user=other_user,
            organization=self.organization,
        )
        other_connection = ExternalServiceConnection.objects.create(
            provider=ExternalServiceProvider.SLACK,
            user=other_user,
            organization=self.organization,
            access_token="xoxp-other",
            external_account_id="T-OTHER",
        )
        other_run = ContentFactoryRun.objects.create(
            run_id="other-slack-run",
            workflow=STARTUP_UPDATE_WORKFLOW,
            organization=self.organization,
            domain=self.organization.domain,
            status=ContentFactoryRunStatus.RUNNING,
            run_request=pin_slack_run_authority(
                {
                    "binding_id": other_binding.pk,
                    "input_sources": ["slack"],
                },
                slack_run_authority_for_connection(other_connection),
            ),
        )
        colliding_event = StartupEvent.objects.create(
            organization=self.organization,
            run=other_run,
            canonical_key="other-workspace-event",
            event_type="product_milestone",
            title="Other workspace",
            month_bucket=date(2026, 3, 1),
            source_thread_ids=[f"slack:C123:{thread.thread_ts}"],
            evidence_message_ids=list(thread.source_message_ids),
        )

        revoke_user_grant(self.user)

        other_run.refresh_from_db()
        self.assertEqual(other_run.status, ContentFactoryRunStatus.RUNNING)
        self.assertTrue(StartupEvent.objects.filter(pk=colliding_event.pk).exists())

    @patch("integrations.services.slack_dm_mirror._revoke_remote_token")
    def test_old_public_id_cannot_target_reconnected_same_channel(
        self,
        _mock_remote_revoke,
    ):
        old_thread = self._create_extractable_slack_thread(
            thread_ts="1770000030.000100"
        )
        with self._with_key():
            batch = self.client.get(
                reverse(
                    "startup_updates_slack_extraction_batch",
                    args=[self.run.run_id],
                ),
                {"limit": 10},
                **self.headers,
            )
        old_public_id = batch.data["threads"][0]["slack_thread_id"]
        revoke_user_grant(self.user)

        replacement = ExternalServiceConnection.objects.create(
            provider=ExternalServiceProvider.SLACK,
            user=self.user,
            organization=self.organization,
            access_token="xoxp-reconnected",
            external_account_id="T123-RECONNECTED",
            provider_metadata=stamp_slack_oauth_generation({}, 1),
        )
        replacement_thread = SlackThreadArtifact.objects.create(
            organization=self.organization,
            connection=replacement,
            channel_id=old_thread.channel_id,
            channel_name="wins",
            thread_ts=old_thread.thread_ts,
            source_message_ids=[f"slack:C123:{old_thread.thread_ts}"],
            source_message_count=1,
            cleaned_text="replacement content",
            latest_message_at=timezone.now(),
            extraction_status=ArtifactProcessingStatus.HYDRATED,
        )

        with self._with_key():
            response = self.client.post(
                reverse(
                    "startup_updates_slack_classification_results",
                    args=[self.run.run_id],
                ),
                {
                    "results": [
                        {
                            "slack_thread_id": old_public_id,
                            "relevance_label": GmailRelevanceLabel.RELEVANT,
                        }
                    ]
                },
                format="json",
                **self.headers,
            )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        replacement_thread.refresh_from_db()
        self.assertEqual(
            replacement_thread.relevance_label,
            GmailRelevanceLabel.PENDING,
        )


@skipUnless(
    connection.vendor == "postgresql",
    "Requires PostgreSQL row-lock concurrency semantics.",
)
@override_settings(ROO_API_KEY="startup-api-key")
class StartupUpdateSlackLineagePostgresTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.api_key = "startup-api-key"
        self.user = User.objects.create_user(
            email="lineage-race@example.com",
            password="test1234",
        )
        self.google_connection = GoogleConnection.objects.create(
            user=self.user,
            google_email="lineage-race@gmail.com",
            refresh_token="refresh-token",
            scope="https://www.googleapis.com/auth/gmail.readonly",
        )
        self.organization = Organization.objects.create(
            name="Lineage Race",
            domain="lineage-race.example",
        )
        self.binding = UserStartupBinding.objects.create(
            user=self.user,
            organization=self.organization,
            google_connection=self.google_connection,
            is_default_for_gmail=True,
        )
        StartupProfile.objects.create(organization=self.organization)
        self.slack_connection = ExternalServiceConnection.objects.create(
            provider=ExternalServiceProvider.SLACK,
            user=self.user,
            organization=self.organization,
            access_token="xoxp-race",
            external_account_id="T-RACE",
        )
        self.run = create_startup_update_run(
            organization=self.organization,
            binding=self.binding,
            input_sources=["gmail", "slack"],
        )
        self.run.run_request = pin_slack_run_authority(
            self.run.run_request,
            slack_run_authority_for_connection(self.slack_connection),
        )
        self.run.status = ContentFactoryRunStatus.RUNNING
        self.run.save(update_fields=["run_request", "status", "updated_at"])

    def _backend_pid(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            return int(cursor.fetchone()[0])

    def _wait_for_blocker(self, backend_pid, *, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_blocking_pids(%s)", [backend_pid])
                blockers = cursor.fetchone()[0]
            if blockers:
                return blockers
        self.fail(f"backend {backend_pid} never waited for the Slack authority lock")

    @staticmethod
    def _thread_target(callback, errors, done):
        close_old_connections()
        try:
            callback()
        except Exception as exc:  # pragma: no cover - asserted by caller
            errors.append(exc)
        finally:
            connection.close()
            done.set()

    def test_delete_first_rejects_queued_downstream_draft_result(self):
        from startup_updates import services as startup_services

        original_cleanup = (
            startup_services.clear_slack_connection_startup_lineage_locked
        )
        delete_holds_authority = threading.Event()
        release_delete = threading.Event()
        delete_done = threading.Event()
        callback_ready = threading.Event()
        callback_done = threading.Event()
        delete_errors = []
        callback_errors = []
        callback_backend_pid = []
        callback_responses = []

        def blocking_cleanup(slack_connection):
            delete_holds_authority.set()
            if not release_delete.wait(10):
                raise TimeoutError("timed out waiting to release DELETE")
            return original_cleanup(slack_connection)

        def post_stale_draft():
            callback_backend_pid.append(self._backend_pid())
            callback_ready.set()
            client = APIClient()
            callback_responses.append(
                client.post(
                    reverse(
                        "startup_updates_draft_results",
                        args=[self.run.run_id],
                    ),
                    {
                        "drafts": [
                            {
                                "month": "2026-08-01",
                                "structured_memo": {
                                    "summary": "stale private Slack result"
                                },
                            }
                        ]
                    },
                    format="json",
                    HTTP_X_API_KEY=self.api_key,
                )
            )

        delete_thread = threading.Thread(
            target=self._thread_target,
            args=(
                lambda: revoke_user_grant(User.objects.get(pk=self.user.pk)),
                delete_errors,
                delete_done,
            ),
        )
        callback_thread = threading.Thread(
            target=self._thread_target,
            args=(post_stale_draft, callback_errors, callback_done),
        )

        with (
            patch(
                "startup_updates.services.clear_slack_connection_startup_lineage_locked",
                side_effect=blocking_cleanup,
            ),
            patch("integrations.services.slack_dm_mirror._revoke_remote_token"),
        ):
            delete_thread.start()
            self.assertTrue(delete_holds_authority.wait(5))
            callback_thread.start()
            self.assertTrue(callback_ready.wait(5))
            self._wait_for_blocker(callback_backend_pid[0])
            self.assertFalse(callback_done.is_set())
            release_delete.set()
            delete_thread.join(10)
            callback_thread.join(10)

        self.assertTrue(delete_done.is_set())
        self.assertTrue(callback_done.is_set())
        self.assertEqual(delete_errors, [])
        self.assertEqual(callback_errors, [])
        self.assertEqual(callback_responses[0].status_code, status.HTTP_409_CONFLICT)
        self.assertFalse(MonthlyUpdateDraft.objects.filter(run=self.run).exists())
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, ContentFactoryRunStatus.CANCELLED)
        self.assertNotIn("stale private Slack result", str(self.run.result))

    def test_delete_first_rejects_queued_gmail_result_on_mixed_source_run(self):
        from startup_updates import services as startup_services

        authority = slack_run_authority_for_connection(self.slack_connection)
        public_id = authority.thread_public_id("C-RACE", "1770000060.000100")
        private_event = StartupEvent.objects.create(
            organization=self.organization,
            run=self.run,
            canonical_key="mixed-source-race-event",
            event_type="product_milestone",
            title="Private Slack event before DELETE",
            month_bucket=date(2026, 8, 1),
            source_thread_ids=[public_id],
        )
        gmail_thread = GmailThreadArtifact.objects.create(
            organization=self.organization,
            google_connection=self.google_connection,
            gmail_thread_id="gmail-race-thread",
            source_message_ids=["gmail-race-message"],
            cleaned_text="Current Gmail-only replacement",
            hydration_status=ArtifactProcessingStatus.HYDRATED,
            extraction_status=ArtifactProcessingStatus.PENDING,
        )
        original_cleanup = (
            startup_services.clear_slack_connection_startup_lineage_locked
        )
        delete_holds_authority = threading.Event()
        release_delete = threading.Event()
        delete_done = threading.Event()
        callback_ready = threading.Event()
        callback_done = threading.Event()
        delete_errors = []
        callback_errors = []
        callback_backend_pid = []
        callback_responses = []

        def blocking_cleanup(slack_connection):
            delete_holds_authority.set()
            if not release_delete.wait(10):
                raise TimeoutError("timed out waiting to release DELETE")
            return original_cleanup(slack_connection)

        def post_stale_gmail_result():
            callback_backend_pid.append(self._backend_pid())
            callback_ready.set()
            client = APIClient()
            callback_responses.append(
                client.post(
                    reverse(
                        "startup_updates_extraction_results",
                        args=[self.run.run_id],
                    ),
                    {
                        "results": [
                            {
                                "gmail_thread_id": gmail_thread.gmail_thread_id,
                                "attachment_updates": [],
                                "events": [
                                    {
                                        "canonical_key": private_event.canonical_key,
                                        "event_type": "product_milestone",
                                        "title": "Stale Gmail overwrite after DELETE",
                                        "month_bucket": "2026-08-01",
                                        "source_thread_ids": ["gmail:race-thread"],
                                    }
                                ],
                                "metrics": [],
                            }
                        ]
                    },
                    format="json",
                    HTTP_X_API_KEY=self.api_key,
                )
            )

        delete_thread = threading.Thread(
            target=self._thread_target,
            args=(
                lambda: revoke_user_grant(User.objects.get(pk=self.user.pk)),
                delete_errors,
                delete_done,
            ),
        )
        callback_thread = threading.Thread(
            target=self._thread_target,
            args=(post_stale_gmail_result, callback_errors, callback_done),
        )

        with (
            patch(
                "startup_updates.services.clear_slack_connection_startup_lineage_locked",
                side_effect=blocking_cleanup,
            ),
            patch("integrations.services.slack_dm_mirror._revoke_remote_token"),
        ):
            delete_thread.start()
            self.assertTrue(delete_holds_authority.wait(5))
            callback_thread.start()
            self.assertTrue(callback_ready.wait(5))
            self._wait_for_blocker(callback_backend_pid[0])
            self.assertFalse(callback_done.is_set())
            release_delete.set()
            delete_thread.join(10)
            callback_thread.join(10)

        self.assertTrue(delete_done.is_set())
        self.assertTrue(callback_done.is_set())
        self.assertEqual(delete_errors, [])
        self.assertEqual(callback_errors, [])
        self.assertEqual(callback_responses[0].status_code, status.HTTP_409_CONFLICT)
        self.assertFalse(
            StartupEvent.objects.filter(pk=private_event.pk).exists()
        )
        gmail_thread.refresh_from_db()
        self.assertEqual(
            gmail_thread.extraction_status,
            ArtifactProcessingStatus.PENDING,
        )
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, ContentFactoryRunStatus.CANCELLED)
        self.assertNotIn("Private Slack", str(self.run.result))

    def test_gmail_result_first_holds_authority_until_delete_scrubs_lineage(self):
        from startup_updates import api_views as startup_api_views

        authority = slack_run_authority_for_connection(self.slack_connection)
        public_id = authority.thread_public_id("C-RACE", "1770000070.000100")
        private_event = StartupEvent.objects.create(
            organization=self.organization,
            run=self.run,
            canonical_key="mixed-source-api-first-event",
            event_type="product_milestone",
            title="Private Slack event before API-first race",
            month_bucket=date(2026, 8, 1),
            source_thread_ids=[public_id],
        )
        gmail_thread = GmailThreadArtifact.objects.create(
            organization=self.organization,
            google_connection=self.google_connection,
            gmail_thread_id="gmail-api-first-thread",
            source_message_ids=["gmail-api-first-message"],
            cleaned_text="Current Gmail-only replacement",
            hydration_status=ArtifactProcessingStatus.HYDRATED,
            extraction_status=ArtifactProcessingStatus.PENDING,
        )
        original_update_run_step = startup_api_views._update_run_step
        callback_holds_authority = threading.Event()
        release_callback = threading.Event()
        callback_done = threading.Event()
        delete_ready = threading.Event()
        delete_done = threading.Event()
        callback_errors = []
        delete_errors = []
        callback_responses = []
        delete_backend_pid = []

        def blocking_update_run_step(run, *, step_key):
            if run.pk == self.run.pk and step_key == "event_extraction":
                callback_holds_authority.set()
                if not release_callback.wait(10):
                    raise TimeoutError("timed out waiting to release Gmail callback")
            return original_update_run_step(run, step_key=step_key)

        def post_gmail_result():
            client = APIClient()
            callback_responses.append(
                client.post(
                    reverse(
                        "startup_updates_extraction_results",
                        args=[self.run.run_id],
                    ),
                    {
                        "results": [
                            {
                                "gmail_thread_id": gmail_thread.gmail_thread_id,
                                "attachment_updates": [],
                                "events": [
                                    {
                                        "canonical_key": private_event.canonical_key,
                                        "event_type": "product_milestone",
                                        "title": "Current Gmail replacement",
                                        "month_bucket": "2026-08-01",
                                        "source_thread_ids": ["gmail:api-first-thread"],
                                    }
                                ],
                                "metrics": [],
                            }
                        ]
                    },
                    format="json",
                    HTTP_X_API_KEY=self.api_key,
                )
            )

        def disconnect_slack():
            delete_backend_pid.append(self._backend_pid())
            delete_ready.set()
            revoke_user_grant(User.objects.get(pk=self.user.pk))

        callback_thread = threading.Thread(
            target=self._thread_target,
            args=(post_gmail_result, callback_errors, callback_done),
        )
        delete_thread = threading.Thread(
            target=self._thread_target,
            args=(disconnect_slack, delete_errors, delete_done),
        )

        with (
            patch(
                "startup_updates.api_views._update_run_step",
                side_effect=blocking_update_run_step,
            ),
            patch("integrations.services.slack_dm_mirror._revoke_remote_token"),
        ):
            callback_thread.start()
            self.assertTrue(callback_holds_authority.wait(5))
            delete_thread.start()
            self.assertTrue(delete_ready.wait(5))
            self._wait_for_blocker(delete_backend_pid[0])
            self.assertFalse(delete_done.is_set())
            release_callback.set()
            callback_thread.join(10)
            delete_thread.join(10)

        self.assertTrue(callback_done.is_set())
        self.assertTrue(delete_done.is_set())
        self.assertEqual(callback_errors, [])
        self.assertEqual(delete_errors, [])
        self.assertEqual(callback_responses[0].status_code, status.HTTP_200_OK)
        private_event.refresh_from_db()
        self.assertEqual(private_event.title, "Current Gmail replacement")
        self.assertEqual(private_event.source_thread_ids, ["gmail:api-first-thread"])
        gmail_thread.refresh_from_db()
        self.assertEqual(
            gmail_thread.extraction_status,
            ArtifactProcessingStatus.PROCESSED,
        )
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, ContentFactoryRunStatus.CANCELLED)
        self.assertNotIn("Private Slack", str(self.run.result))


class StartupUpdateResetCommandTest(StartupUpdateApiTestCase):
    def setUp(self):
        super().setUp()
        self.profile = StartupProfile.objects.create(
            organization=self.organization,
            company_aliases=["Acme"],
            domain_aliases=["acme.com"],
        )
        self.binding = UserStartupBinding.objects.create(
            user=self.user,
            organization=self.organization,
            google_connection=self.google_connection,
            is_default_for_gmail=True,
        )
        self.artifact = GmailMessageArtifact.objects.create(
            organization=self.organization,
            google_connection=self.google_connection,
            gmail_message_id="artifact-1",
            gmail_thread_id="thread-1",
            internal_date=timezone.now(),
            subject="Acme update",
            from_address="founder@acme.com",
            relevance_label=GmailRelevanceLabel.RELEVANT,
        )
        self.draft = MonthlyUpdateDraft.objects.create(
            organization=self.organization,
            month=timezone.now().date().replace(day=1),
            status="ready",
            structured_memo={"title": "Investor update"},
        )

    def _create_run(
        self,
        *,
        run_id: str,
        status: str = ContentFactoryRunStatus.RUNNING,
        domain: Optional[str] = None,
        result: Optional[dict] = None,
    ) -> ContentFactoryRun:
        run = ContentFactoryRun.objects.create(
            run_id=run_id,
            workflow=STARTUP_UPDATE_WORKFLOW,
            domain=domain or self.organization.domain,
            slack_user_id=str(self.user.id),
            status=status,
            current_step="gmail_backfill",
            approval_state=ContentFactoryApprovalState.NOT_REQUIRED,
            step_order=["profile_resolution", "gmail_backfill"],
            run_request={
                "organization_id": self.organization.id,
                "binding_id": self.binding.id,
                "google_connection_id": self.google_connection.id,
            },
            result=result
            or {
                "_valley_meta": {
                    "lease_owner": "worker-1",
                    "lease_expires_at": "2026-03-26T06:10:00+00:00",
                    "last_heartbeat_at": "2026-03-26T06:05:00+00:00",
                    "last_error": "timed out",
                    "dead_letters": [{"step_key": "gmail_backfill"}],
                }
            },
            error="timed out",
            resume_available=True,
        )
        step = ContentFactoryRunStep.objects.create(
            run=run,
            step_key="gmail_backfill",
            display_order=1,
            required=True,
            status=ContentFactoryStepStatus.RUNNING,
            attempts=1,
            message="Processing gmail_backfill",
            started_at=timezone.now(),
            error="",
        )
        ContentFactoryRunStepAttempt.objects.create(
            step=step,
            attempt=1,
            status=ContentFactoryStepStatus.RUNNING,
            message="attempt started",
            started_at=timezone.now(),
            error="",
        )
        return run

    def test_reset_command_dry_run_lists_matches_and_does_not_mutate(self):
        run = self._create_run(run_id="startup-update-reset-dry-run")

        out = StringIO()
        call_command(
            "reset_startup_update_runs",
            domain=self.organization.domain,
            stdout=out,
        )

        run.refresh_from_db()
        self.assertEqual(run.status, ContentFactoryRunStatus.RUNNING)
        self.assertIn(run.run_id, out.getvalue())
        self.assertIn("Dry run", out.getvalue())
        self.assertTrue(run.resume_available)
        self.assertEqual(run.steps.get(step_key="gmail_backfill").status, ContentFactoryStepStatus.RUNNING)

    def test_reset_command_apply_resets_stale_open_runs_and_preserves_artifacts(self):
        stale_run = self._create_run(run_id="startup-update-reset-stale")
        fresh_run = self._create_run(run_id="startup-update-reset-fresh")
        completed_run = self._create_run(
            run_id="startup-update-reset-completed",
            status=ContentFactoryRunStatus.COMPLETED,
        )
        stale_time = timezone.now() - timedelta(minutes=45)
        ContentFactoryRun.objects.filter(pk=stale_run.pk).update(updated_at=stale_time)

        out = StringIO()
        call_command(
            "reset_startup_update_runs",
            domain=self.organization.domain,
            older_than_minutes=30,
            apply=True,
            stdout=out,
        )

        stale_run.refresh_from_db()
        fresh_run.refresh_from_db()
        completed_run.refresh_from_db()

        self.assertEqual(stale_run.status, ContentFactoryRunStatus.FAILED)
        self.assertFalse(stale_run.resume_available)
        self.assertEqual(stale_run.error, "Locally reset stale startup-update run.")
        self.assertIsNone(stale_run.result["_valley_meta"]["lease_owner"])
        self.assertEqual(stale_run.result["_valley_meta"]["dead_letters"], [])

        stale_step = stale_run.steps.get(step_key="gmail_backfill")
        self.assertEqual(stale_step.status, ContentFactoryStepStatus.FAILED)
        self.assertEqual(stale_step.error, "Locally reset stale startup-update run.")
        self.assertIsNotNone(stale_step.completed_at)
        stale_attempt = stale_step.attempt_history.get(attempt=1)
        self.assertEqual(stale_attempt.status, ContentFactoryStepStatus.FAILED)
        self.assertEqual(stale_attempt.error, "Locally reset stale startup-update run.")
        self.assertIsNotNone(stale_attempt.completed_at)

        self.assertEqual(fresh_run.status, ContentFactoryRunStatus.RUNNING)
        self.assertEqual(completed_run.status, ContentFactoryRunStatus.COMPLETED)
        self.assertEqual(GmailMessageArtifact.objects.count(), 1)
        self.assertEqual(MonthlyUpdateDraft.objects.count(), 1)
        self.assertIn("Reset 1 startup-update run(s).", out.getvalue())

    def test_reset_command_run_id_selector_targets_only_requested_run(self):
        target_run = self._create_run(run_id="startup-update-reset-target")
        other_run = self._create_run(run_id="startup-update-reset-other")

        call_command(
            "reset_startup_update_runs",
            run_ids=[target_run.run_id],
            apply=True,
            stdout=StringIO(),
        )

        target_run.refresh_from_db()
        other_run.refresh_from_db()
        self.assertEqual(target_run.status, ContentFactoryRunStatus.FAILED)
        self.assertEqual(other_run.status, ContentFactoryRunStatus.RUNNING)

    @patch("integrations.management.commands.reset_startup_update_runs.cancel_valley_run")
    def test_reset_command_cancel_mode_marks_run_cancelled_and_cleans_outputs(self, mock_cancel_valley_run):
        mock_cancel_valley_run.return_value = {
            "revoke_requested": True,
            "revoke_succeeded": True,
            "revoked_job_ids": ["job-1"],
            "missing_job_ids": [],
        }
        run = self._create_run(run_id="startup-update-reset-cancel")
        draft = MonthlyUpdateDraft.objects.create(
            organization=self.organization,
            run=run,
            month=date(2026, 2, 1),
            status="draft",
            structured_memo={"title": "Cancelled draft"},
        )
        event = StartupEvent.objects.create(
            organization=self.organization,
            run=run,
            canonical_key="cancel-event",
            event_type="customer_win",
            title="Cancelled event",
            month_bucket=date(2026, 3, 1),
        )
        metric = StartupMetricObservation.objects.create(
            organization=self.organization,
            run=run,
            metric_key="revenue",
            metric_name="Revenue",
            value_text="$45,000",
            period_month=date(2026, 3, 1),
        )

        out = StringIO()
        call_command(
            "reset_startup_update_runs",
            run_ids=[run.run_id],
            apply=True,
            cancel=True,
            stdout=out,
        )

        run.refresh_from_db()
        step = run.steps.get(step_key="gmail_backfill")
        attempt = step.attempt_history.get(attempt=1)

        self.assertEqual(run.status, ContentFactoryRunStatus.CANCELLED)
        self.assertFalse(run.resume_available)
        self.assertEqual(step.status, ContentFactoryStepStatus.CANCELLED)
        self.assertEqual(attempt.status, ContentFactoryStepStatus.CANCELLED)
        self.assertFalse(MonthlyUpdateDraft.objects.filter(pk=draft.pk).exists())
        self.assertFalse(StartupEvent.objects.filter(pk=event.pk).exists())
        self.assertFalse(StartupMetricObservation.objects.filter(pk=metric.pk).exists())
        self.assertIn("Cancelled 1 startup-update run(s).", out.getvalue())
        mock_cancel_valley_run.assert_called_once_with(run.run_id)


class RelabelStartupUpdateMessagesCommandTest(StartupUpdateApiTestCase):
    def setUp(self):
        super().setUp()
        self.profile = StartupProfile.objects.create(
            organization=self.organization,
            company_aliases=["Acme", "Acme AI"],
            domain_aliases=["acme.com"],
            positive_keywords=["acme", "arr"],
            investor_domains=["fund.example"],
        )
        self.unclassified = GmailMessageArtifact.objects.create(
            organization=self.organization,
            google_connection=self.google_connection,
            gmail_message_id="bulk-1",
            gmail_thread_id="thread-bulk-1",
            internal_date=timezone.now(),
            subject="Weekly digest and magic link",
            from_address="noreply@news.example",
            to_addresses=["samdonegan@gmail.com"],
            header_values={
                "list-unsubscribe": "<mailto:unsubscribe@example.com>",
                "precedence": "bulk",
            },
            heuristic_score=61,
            heuristic_reasons=["matched_company_alias_or_positive_keyword"],
            relevance_label=GmailRelevanceLabel.AMBIGUOUS,
            needs_thread_context=True,
        )
        self.classified = GmailMessageArtifact.objects.create(
            organization=self.organization,
            google_connection=self.google_connection,
            gmail_message_id="classified-1",
            gmail_thread_id="thread-classified-1",
            internal_date=timezone.now(),
            subject="Weekly digest and magic link",
            from_address="noreply@news.example",
            to_addresses=["samdonegan@gmail.com"],
            header_values={
                "list-unsubscribe": "<mailto:unsubscribe@example.com>",
                "precedence": "bulk",
            },
            heuristic_score=88,
            heuristic_reasons=["matched_company_domain"],
            relevance_label=GmailRelevanceLabel.RELEVANT,
            needs_thread_context=True,
            classified_at=timezone.now(),
        )

    def test_relabel_command_dry_run_reports_changes_without_mutating(self):
        out = StringIO()

        call_command(
            "relabel_startup_update_messages",
            domain=self.organization.domain,
            stdout=out,
        )

        self.unclassified.refresh_from_db()
        self.classified.refresh_from_db()
        self.assertEqual(self.unclassified.relevance_label, GmailRelevanceLabel.AMBIGUOUS)
        self.assertTrue(self.unclassified.needs_thread_context)
        self.assertEqual(self.classified.relevance_label, GmailRelevanceLabel.RELEVANT)
        self.assertIn("Before:", out.getvalue())
        self.assertIn("After:", out.getvalue())
        self.assertIn("Would relabel 1 message(s).", out.getvalue())

    def test_relabel_command_apply_updates_unclassified_messages_only(self):
        out = StringIO()

        call_command(
            "relabel_startup_update_messages",
            domain=self.organization.domain,
            apply=True,
            stdout=out,
        )

        self.unclassified.refresh_from_db()
        self.classified.refresh_from_db()
        self.assertEqual(self.unclassified.relevance_label, GmailRelevanceLabel.IRRELEVANT)
        self.assertFalse(self.unclassified.needs_thread_context)
        self.assertIn("hard_filtered_bulk_header", self.unclassified.heuristic_reasons)
        self.assertEqual(self.classified.relevance_label, GmailRelevanceLabel.RELEVANT)
        self.assertEqual(self.classified.heuristic_reasons, ["matched_company_domain"])
        self.assertIn("Relabeled 1 message(s).", out.getvalue())


class StartupUpdateWorkflowViewsTest(StartupUpdateApiTestCase):
    def setUp(self):
        super().setUp()
        self.profile = StartupProfile.objects.create(
            organization=self.organization,
            company_aliases=["Acme", "Acme AI"],
            domain_aliases=["acme.com"],
            positive_keywords=["acme", "pilot", "arr"],
        )
        self.binding = UserStartupBinding.objects.create(
            user=self.user,
            organization=self.organization,
            google_connection=self.google_connection,
            is_default_for_gmail=True,
        )
        self.run = create_startup_update_run(
            organization=self.organization,
            binding=self.binding,
            window_months=6,
        )
        message_timestamp = timezone.now() - timedelta(minutes=5)
        self.message = GmailMessageArtifact.objects.create(
            organization=self.organization,
            google_connection=self.google_connection,
            gmail_message_id="msg-123",
            gmail_thread_id="thread-123",
            internal_date=message_timestamp,
            subject="ACME pilot converted to ARR contract",
            from_address="ceo@acme.com",
            to_addresses=["investor@fund.example"],
            snippet="Pilot converted to ARR contract",
            cleaned_text="We closed the pilot and now have $24000 ARR.",
            heuristic_score=74,
            heuristic_reasons=["matched_company_domain"],
            relevance_label=GmailRelevanceLabel.AMBIGUOUS,
            needs_thread_context=True,
            metadata_hydrated_at=timezone.now(),
        )
        self.thread = GmailThreadArtifact.objects.create(
            organization=self.organization,
            google_connection=self.google_connection,
            gmail_thread_id="thread-123",
            source_message_ids=["msg-123"],
            message_payloads=[{"message_id": "msg-123", "cleaned_text": "We closed the pilot and now have $24000 ARR."}],
            cleaned_text="We closed the pilot and now have $24000 ARR.",
            hydration_status=ArtifactProcessingStatus.HYDRATED,
            extraction_status=ArtifactProcessingStatus.PENDING,
            source_message_count=1,
            latest_message_internal_date=message_timestamp,
            hydrated_at=timezone.now(),
        )
        self.attachment = GmailAttachmentArtifact.objects.create(
            organization=self.organization,
            thread_artifact=self.thread,
            message_artifact=self.message,
            gmail_attachment_id="att-1",
            part_id="1.2",
            filename="metrics.pdf",
            mime_type="application/pdf",
            size_bytes=1024,
            raw_content_base64="cGRm",
            extraction_status=ArtifactProcessingStatus.HYDRATED,
            hydrated_at=timezone.now(),
        )
        self.thread.attachment_ids = [self.attachment.id]
        self.thread.save(update_fields=["attachment_ids", "updated_at"])

    def _create_secondary_connection(self):
        other_user = User.objects.create_user(
            email=f"other-{User.objects.count()}@example.com",
            password="test1234",
        )
        other_connection = GoogleConnection.objects.create(
            user=other_user,
            google_email=f"other-{GoogleConnection.objects.count()}@gmail.com",
            refresh_token="refresh-token-2",
            scope="https://www.googleapis.com/auth/gmail.readonly",
        )
        return other_user, other_connection

    def test_draft_results_merge_regenerated_bullets_without_duplicates(self):
        month_bucket = date(2026, 3, 1)
        MonthlyUpdateDraft.objects.create(
            organization=self.organization,
            month=month_bucket,
            run=None,
            status=MonthlyUpdateDraftStatus.READY,
            structured_memo={
                "title": "Acme March Update",
                "topline": "Existing topline.",
                "kpi_snapshot": [
                    {"metric_key": "mrr", "label": "MRR", "value": "$24,000"},
                ],
                "highlights": [
                    "Converted pilot to paid annual contract",
                    "Hired first support lead",
                ],
                "lowlights": ["Sales cycle slipped"],
                "asks": [
                    {"label": "Intro", "text": "Customer intros to seed investors"},
                ],
            },
            evidence_event_ids=[11],
            evidence_metric_ids=[21],
            carry_forward_event_ids=[31],
            groundedness_notes="Existing review notes.",
        )

        with self._with_key():
            response = self.client.post(
                reverse("startup_updates_draft_results", args=[self.run.run_id]),
                {
                    "drafts": [
                        {
                            "month": month_bucket.isoformat(),
                            "status": "ready",
                            "model_name": "gpt-5.4",
                            "groundedness_status": "passed",
                            "structured_memo": {
                                "title": "Acme March Update Refined",
                                "topline": "",
                                "kpi_snapshot": [
                                    {"metric_key": "mrr", "label": "MRR", "value": "$26,000"},
                                    {"metric_key": "cashCollected", "label": "Cash Collected", "value": "$20,000"},
                                ],
                                "metric_suggestions": [
                                    {
                                        "metric_key": "customerInterviews",
                                        "label": "Customer Interviews",
                                        "reason": "Useful before revenue is consistent.",
                                    }
                                ],
                                "highlights": [
                                    "Converted the pilot into a paid annual contract",
                                    "Launched onboarding refresh",
                                ],
                                "lowlights": ["Sales cycle slipped by two weeks"],
                                "asks": [
                                    {"label": "Intro", "text": "Customer introductions to seed investors"},
                                ],
                            },
                            "evidence_event_ids": [11, 12],
                            "evidence_metric_ids": [22],
                            "carry_forward_event_ids": [32],
                            "groundedness_notes": "New review notes.",
                        }
                    ]
                },
                format="json",
                **self.headers,
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(MonthlyUpdateDraft.objects.filter(organization=self.organization, month=month_bucket).count(), 1)
        draft = MonthlyUpdateDraft.objects.get(organization=self.organization, month=month_bucket)
        memo = draft.structured_memo
        self.assertEqual(memo["title"], "Acme March Update Refined")
        self.assertEqual(memo["topline"], "Existing topline.")
        self.assertEqual(
            memo["highlights"],
            [
                "Converted the pilot into a paid annual contract",
                "Hired first support lead",
                "Launched onboarding refresh",
            ],
        )
        self.assertEqual(memo["lowlights"], ["Sales cycle slipped by two weeks"])
        self.assertEqual(memo["asks"], [{"label": "Intro", "text": "Customer introductions to seed investors"}])
        self.assertEqual(
            {item["metric_key"]: item["value"] for item in memo["kpi_snapshot"]},
            {"mrr": "$26,000", "cashCollected": "$20,000"},
        )
        self.assertEqual(
            memo["metric_suggestions"],
            [
                {
                    "metric_key": "customerInterviews",
                    "label": "Customer Interviews",
                    "reason": "Useful before revenue is consistent.",
                }
            ],
        )
        self.assertEqual(set(draft.evidence_event_ids), {11, 12})
        self.assertEqual(set(draft.evidence_metric_ids), {21, 22})
        self.assertEqual(set(draft.carry_forward_event_ids), {31, 32})
        self.assertIn("3 bullets refreshed", draft.groundedness_notes)
        self.assertIn("1 added", draft.groundedness_notes)

    def test_draft_results_merge_xero_metrics_into_kpi_snapshot(self):
        month_bucket = date(2026, 3, 1)
        revenue_metric = StartupMetricObservation.objects.create(
            organization=self.organization,
            run=self.run,
            source_provider=ExternalServiceProvider.XERO,
            metric_key="revenue",
            metric_name="Revenue",
            value_text="AUD 4000.00",
            value_number=Decimal("4000.00"),
            unit="AUD",
            period_month=month_bucket,
            confidence=1.0,
            source_metadata={
                "report_name": "ProfitAndLoss",
                "report_start_date": "2026-03-01",
                "report_end_date": "2026-03-31",
                "calculation_basis": "profit_and_loss_total_income",
            },
        )
        burn_metric = StartupMetricObservation.objects.create(
            organization=self.organization,
            run=self.run,
            source_provider=ExternalServiceProvider.XERO,
            metric_key="burnRate",
            metric_name="Burn rate",
            value_text="AUD 1500.00",
            value_number=Decimal("1500.00"),
            unit="AUD",
            period_month=month_bucket,
            confidence=1.0,
            source_metadata={"report_name": "ProfitAndLoss"},
        )
        monthly_costs_metric = StartupMetricObservation.objects.create(
            organization=self.organization,
            run=self.run,
            source_provider=ExternalServiceProvider.XERO,
            metric_key="monthlyCosts",
            metric_name="Monthly costs",
            value_text="AUD 2500.00",
            value_number=Decimal("2500.00"),
            unit="AUD",
            period_month=month_bucket,
            confidence=1.0,
            source_metadata={
                "report_name": "ProfitAndLoss",
                "calculation_basis": "cost_of_sales_plus_operating_expenses_when_available_otherwise_total_expenses",
            },
        )

        with self._with_key():
            response = self.client.post(
                reverse("startup_updates_draft_results", args=[self.run.run_id]),
                {
                    "drafts": [
                        {
                            "month": month_bucket.isoformat(),
                            "status": "ready",
                            "model_name": "gpt-5.4",
                            "structured_memo": {
                                "title": "Acme March Update",
                                "kpi_snapshot": [
                                    {"metric_key": "revenue", "label": "Revenue", "value": "$1,000"},
                                    {"metric_key": "monthlyCosts", "label": "Monthly Costs", "value": "$500"},
                                    {"metric_key": "activeUsers", "label": "Active Users", "value": "240"},
                                ],
                                "highlights": ["Launched onboarding refresh"],
                            },
                            "evidence_metric_ids": [],
                        }
                    ]
                },
                format="json",
                **self.headers,
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        draft = MonthlyUpdateDraft.objects.get(organization=self.organization, month=month_bucket)
        snapshot = {item["metric_key"]: item for item in draft.structured_memo["kpi_snapshot"]}
        self.assertEqual(snapshot["revenue"]["value"], "AUD 4000.00")
        self.assertEqual(snapshot["revenue"]["source_provider"], ExternalServiceProvider.XERO)
        self.assertEqual(snapshot["revenue"]["source_metadata"]["report_name"], "ProfitAndLoss")
        self.assertEqual(snapshot["burnRate"]["value"], "AUD 1500.00")
        self.assertEqual(snapshot["monthlyCosts"]["value"], "AUD 2500.00")
        self.assertEqual(snapshot["monthlyCosts"]["source_provider"], ExternalServiceProvider.XERO)
        self.assertEqual(snapshot["activeUsers"]["value"], "240")
        self.assertEqual(
            set(draft.evidence_metric_ids),
            {revenue_metric.id, burn_metric.id, monthly_costs_metric.id},
        )

    def test_draft_results_get_hydrates_xero_metrics_without_saving_draft(self):
        current_month = date(2026, 4, 1)
        previous_month = date(2026, 3, 1)
        MonthlyUpdateDraft.objects.create(
            organization=self.organization,
            run=self.run,
            month=current_month,
            status=MonthlyUpdateDraftStatus.READY,
            structured_memo={
                "title": "Acme April Update",
                "kpi_snapshot": [{"metric_key": "activeUsers", "label": "Active Users", "value": "5"}],
                "highlights": ["April highlight"],
            },
        )
        MonthlyUpdateDraft.objects.create(
            organization=self.organization,
            run=self.run,
            month=previous_month,
            status=MonthlyUpdateDraftStatus.READY,
            structured_memo={
                "title": "Acme March Update",
                "kpi_snapshot": [{"metric_key": "activeUsers", "label": "Active Users", "value": "4"}],
                "highlights": ["March highlight"],
            },
        )
        StartupMetricObservation.objects.create(
            organization=self.organization,
            run=self.run,
            source_provider=ExternalServiceProvider.XERO,
            metric_key="revenue",
            metric_name="Revenue",
            value_text="AUD 3800.00",
            value_number=Decimal("3800.00"),
            unit="AUD",
            period_month=current_month,
            confidence=1.0,
            source_metadata={"report_name": "ProfitAndLoss"},
        )
        StartupMetricObservation.objects.create(
            organization=self.organization,
            run=self.run,
            source_provider=ExternalServiceProvider.XERO,
            metric_key="revenue",
            metric_name="Revenue",
            value_text="AUD 2735.75",
            value_number=Decimal("2735.75"),
            unit="AUD",
            period_month=previous_month,
            confidence=1.0,
            source_metadata={"report_name": "ProfitAndLoss"},
        )

        with self._with_key():
            response = self.client.get(
                reverse("startup_updates_draft_results", args=[self.run.run_id]),
                **self.headers,
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["draft"]["metrics"]["revenue"], "AUD 3800.00")
        self.assertEqual(response.data["draft"]["pastMonths"][0]["metrics"]["revenue"], "AUD 2735.75")
        self.assertEqual(response.data["current_month"]["metrics"]["revenue"], "AUD 3800.00")
        self.assertEqual(response.data["past_months"][0]["metrics"]["revenue"], "AUD 2735.75")
        stored_draft = MonthlyUpdateDraft.objects.get(organization=self.organization, month=current_month)
        self.assertNotIn(
            "revenue",
            [item.get("metric_key") for item in stored_draft.structured_memo["kpi_snapshot"]],
        )

    def test_hydration_candidates_endpoint_returns_unhydrated_threads(self):
        GmailThreadArtifact.objects.filter(pk=self.thread.pk).update(hydration_status=ArtifactProcessingStatus.PENDING)

        with self._with_key():
            response = self.client.get(
                reverse("startup_updates_hydration_candidates", args=[self.run.run_id]),
                {"limit": 10},
                **self.headers,
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["threads"][0]["gmail_thread_id"], self.thread.gmail_thread_id)

    def test_classification_batch_excludes_irrelevant_messages(self):
        GmailMessageArtifact.objects.create(
            organization=self.organization,
            google_connection=self.google_connection,
            gmail_message_id="msg-irrelevant",
            gmail_thread_id="thread-irrelevant",
            internal_date=timezone.now(),
            subject="Weekly digest",
            from_address="noreply@news.example",
            heuristic_score=0,
            heuristic_reasons=["hard_filtered_bulk_header"],
            relevance_label=GmailRelevanceLabel.IRRELEVANT,
            needs_thread_context=False,
            metadata_hydrated_at=timezone.now(),
        )

        with self._with_key():
            batch_response = self.client.get(
                reverse("startup_updates_classification_batch", args=[self.run.run_id]),
                {"limit": 10},
                **self.headers,
            )

        self.assertEqual(batch_response.status_code, status.HTTP_200_OK)
        self.assertEqual(batch_response.data["count"], 1)
        self.assertEqual(
            [item["gmail_message_id"] for item in batch_response.data["messages"]],
            [self.message.gmail_message_id],
        )

    def test_classification_batch_excludes_already_classified_ambiguous_messages(self):
        # Ambiguous is a terminal label for the snippet-only classifier: once a
        # message has been classified, re-serving it would loop the valley worker
        # on the same batch forever. Ambiguous messages stay eligible downstream
        # via EXTRACTABLE_RELEVANCE_LABELS instead.
        with self._with_key():
            results_response = self.client.post(
                reverse("startup_updates_classification_results", args=[self.run.run_id]),
                {
                    "results": [
                        {
                            "gmail_message_id": self.message.gmail_message_id,
                            "relevance_label": GmailRelevanceLabel.AMBIGUOUS,
                            "relevance_score": 0.4,
                            "relevance_reason": "Snippet alone is not enough to judge.",
                            "needs_thread_context": True,
                        }
                    ]
                },
                format="json",
                **self.headers,
            )

        self.assertEqual(results_response.status_code, status.HTTP_200_OK)
        self.message.refresh_from_db()
        self.assertEqual(self.message.relevance_label, GmailRelevanceLabel.AMBIGUOUS)
        self.assertIsNotNone(self.message.classified_at)

        with self._with_key():
            batch_response = self.client.get(
                reverse("startup_updates_classification_batch", args=[self.run.run_id]),
                {"limit": 10},
                **self.headers,
            )

        self.assertEqual(batch_response.status_code, status.HTTP_200_OK)
        self.assertEqual(batch_response.data["count"], 0)
        self.assertEqual(batch_response.data["messages"], [])

    def test_classification_batch_scopes_messages_to_run_connection(self):
        _other_user, other_connection = self._create_secondary_connection()
        GmailMessageArtifact.objects.create(
            organization=self.organization,
            google_connection=other_connection,
            gmail_message_id=self.message.gmail_message_id,
            gmail_thread_id="thread-other-123",
            internal_date=timezone.now() + timedelta(minutes=1),
            subject="Cross-connection duplicate",
            from_address="ceo@other.example",
            snippet="Should not be included for this run",
            cleaned_text="Should not be included for this run",
            heuristic_score=99,
            heuristic_reasons=["matched_company_domain"],
            relevance_label=GmailRelevanceLabel.AMBIGUOUS,
            needs_thread_context=True,
            metadata_hydrated_at=timezone.now(),
        )

        with self._with_key():
            batch_response = self.client.get(
                reverse("startup_updates_classification_batch", args=[self.run.run_id]),
                {"limit": 10},
                **self.headers,
            )

        self.assertEqual(batch_response.status_code, status.HTTP_200_OK)
        self.assertEqual(batch_response.data["count"], 1)
        self.assertEqual(batch_response.data["messages"][0]["gmail_thread_id"], self.message.gmail_thread_id)

    def test_classification_batch_scopes_messages_to_run_window(self):
        now = timezone.now()
        self.run.run_request["backfill_window_start"] = (now - timedelta(hours=1)).isoformat()
        self.run.run_request["backfill_window_end"] = (now + timedelta(hours=1)).isoformat()
        self.run.save(update_fields=["run_request", "updated_at"])

        GmailMessageArtifact.objects.create(
            organization=self.organization,
            google_connection=self.google_connection,
            gmail_message_id="msg-old-window",
            gmail_thread_id="thread-old-window",
            internal_date=now - timedelta(days=7),
            subject="Out-of-window message",
            from_address="ceo@acme.com",
            snippet="Should not be classified for this run",
            cleaned_text="Should not be classified for this run",
            heuristic_score=95,
            heuristic_reasons=["matched_company_domain"],
            relevance_label=GmailRelevanceLabel.AMBIGUOUS,
            needs_thread_context=True,
            metadata_hydrated_at=timezone.now(),
        )

        with self._with_key():
            batch_response = self.client.get(
                reverse("startup_updates_classification_batch", args=[self.run.run_id]),
                {"limit": 10},
                **self.headers,
            )

        self.assertEqual(batch_response.status_code, status.HTTP_200_OK)
        self.assertEqual(batch_response.data["count"], 1)
        self.assertEqual(
            [item["gmail_message_id"] for item in batch_response.data["messages"]],
            [self.message.gmail_message_id],
        )

    def test_classification_results_updates_only_pinned_connection_artifact(self):
        _other_user, other_connection = self._create_secondary_connection()
        other_message = GmailMessageArtifact.objects.create(
            organization=self.organization,
            google_connection=other_connection,
            gmail_message_id=self.message.gmail_message_id,
            gmail_thread_id="thread-other-123",
            internal_date=timezone.now() + timedelta(minutes=1),
            subject="Cross-connection duplicate",
            from_address="ceo@other.example",
            snippet="Should not be updated for this run",
            cleaned_text="Should not be updated for this run",
            heuristic_score=99,
            heuristic_reasons=["matched_company_domain"],
            relevance_label=GmailRelevanceLabel.AMBIGUOUS,
            needs_thread_context=True,
            metadata_hydrated_at=timezone.now(),
        )

        with self._with_key():
            classify_response = self.client.post(
                reverse("startup_updates_classification_results", args=[self.run.run_id]),
                {
                    "results": [
                        {
                            "gmail_message_id": self.message.gmail_message_id,
                            "relevance_label": GmailRelevanceLabel.RELEVANT,
                            "relevance_score": 0.93,
                            "relevance_reason": "Investor-facing revenue update",
                            "needs_thread_context": True,
                        }
                    ]
                },
                format="json",
                **self.headers,
            )

        self.assertEqual(classify_response.status_code, status.HTTP_200_OK)
        self.message.refresh_from_db()
        other_message.refresh_from_db()
        self.assertEqual(self.message.relevance_label, GmailRelevanceLabel.RELEVANT)
        self.assertEqual(other_message.relevance_label, GmailRelevanceLabel.AMBIGUOUS)

    @patch("startup_updates.api_views.hydrate_thread_artifact")
    def test_hydrate_threads_message_id_lookup_uses_run_connection(self, mock_hydrate_thread_artifact):
        _other_user, other_connection = self._create_secondary_connection()
        GmailMessageArtifact.objects.create(
            organization=self.organization,
            google_connection=other_connection,
            gmail_message_id=self.message.gmail_message_id,
            gmail_thread_id="thread-other-123",
            internal_date=timezone.now() + timedelta(minutes=1),
            subject="Cross-connection duplicate",
            from_address="ceo@other.example",
            relevance_label=GmailRelevanceLabel.RELEVANT,
            needs_thread_context=True,
            metadata_hydrated_at=timezone.now(),
        )
        mock_hydrate_thread_artifact.return_value = self.thread

        with self._with_key():
            response = self.client.post(
                reverse("startup_updates_hydrate_threads", args=[self.run.run_id]),
                {"message_ids": [self.message.gmail_message_id]},
                format="json",
                **self.headers,
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["hydrated_thread_ids"], [self.thread.gmail_thread_id])
        self.assertEqual(mock_hydrate_thread_artifact.call_args.kwargs["thread_id"], self.thread.gmail_thread_id)
        self.assertFalse(mock_hydrate_thread_artifact.call_args.kwargs["fetch_attachments"])

    def test_hydration_candidates_scope_to_run_window_and_top_threads(self):
        now = timezone.now()
        self.message.internal_date = now - timedelta(minutes=20)
        self.message.save(update_fields=["internal_date", "updated_at"])
        self.thread.hydration_status = ArtifactProcessingStatus.PENDING
        self.thread.latest_message_internal_date = now - timedelta(minutes=20)
        self.thread.save(update_fields=["hydration_status", "latest_message_internal_date", "updated_at"])
        self.run.run_request["backfill_window_start"] = (now - timedelta(hours=1)).isoformat()
        self.run.run_request["backfill_window_end"] = (now + timedelta(hours=1)).isoformat()
        self.run.run_request["max_source_threads"] = 1
        self.run.save(update_fields=["run_request", "updated_at"])

        GmailMessageArtifact.objects.create(
            organization=self.organization,
            google_connection=self.google_connection,
            gmail_message_id="msg-top-thread",
            gmail_thread_id="thread-top-thread",
            internal_date=now,
            subject="Newest in-window thread",
            from_address="founder@acme.com",
            snippet="Most recent relevant thread",
            cleaned_text="Most recent relevant thread",
            heuristic_score=88,
            heuristic_reasons=["matched_company_domain"],
            relevance_label=GmailRelevanceLabel.RELEVANT,
            needs_thread_context=True,
            metadata_hydrated_at=timezone.now(),
        )
        GmailThreadArtifact.objects.create(
            organization=self.organization,
            google_connection=self.google_connection,
            gmail_thread_id="thread-top-thread",
            source_message_ids=["msg-top-thread"],
            message_payloads=[{"message_id": "msg-top-thread"}],
            cleaned_text="Most recent relevant thread",
            hydration_status=ArtifactProcessingStatus.PENDING,
            extraction_status=ArtifactProcessingStatus.PENDING,
            source_message_count=1,
            latest_message_internal_date=now,
        )
        GmailMessageArtifact.objects.create(
            organization=self.organization,
            google_connection=self.google_connection,
            gmail_message_id="msg-outside-window",
            gmail_thread_id="thread-outside-window",
            internal_date=now - timedelta(days=14),
            subject="Outside-window thread",
            from_address="founder@acme.com",
            cleaned_text="Should not be hydrated in this run",
            heuristic_score=90,
            heuristic_reasons=["matched_company_domain"],
            relevance_label=GmailRelevanceLabel.RELEVANT,
            needs_thread_context=True,
            metadata_hydrated_at=timezone.now(),
        )

        with self._with_key():
            response = self.client.get(
                reverse("startup_updates_hydration_candidates", args=[self.run.run_id]),
                {"limit": 10},
                **self.headers,
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["threads"][0]["gmail_thread_id"], "thread-top-thread")

    @patch("integrations.services.gmail.get_attachment_payload")
    def test_extraction_batch_lazily_hydrates_missing_attachments(self, mock_get_attachment_payload):
        self.message.attachment_manifest = [
            {
                "part_id": "1.2",
                "filename": "metrics.txt",
                "mime_type": "text/plain",
                "attachment_id": "att-lazy",
                "size_bytes": 24,
                "content_disposition": "attachment",
            }
        ]
        self.message.save(update_fields=["attachment_manifest", "updated_at"])
        self.attachment.delete()
        self.thread.attachment_ids = []
        self.thread.save(update_fields=["attachment_ids", "updated_at"])
        mock_get_attachment_payload.return_value = {"data": "TVJSIHJlYWNoZWQgMTIwMDAgVVNE"}

        with self._with_key():
            response = self.client.get(
                reverse("startup_updates_extraction_batch", args=[self.run.run_id]),
                {"limit": 10},
                **self.headers,
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(len(response.data["threads"][0]["attachments"]), 1)
        hydrated_attachment = GmailAttachmentArtifact.objects.get(
            organization=self.organization,
            message_artifact=self.message,
            gmail_attachment_id="att-lazy",
        )
        self.thread.refresh_from_db()
        self.assertEqual(self.thread.attachment_ids, [hydrated_attachment.id])
        self.assertEqual(response.data["threads"][0]["attachments"][0]["id"], hydrated_attachment.id)
        mock_get_attachment_payload.assert_called_once_with(
            self.google_connection,
            self.message.gmail_message_id,
            "att-lazy",
        )

    @patch("integrations.services.gmail.get_attachment_payload")
    def test_extraction_batch_records_failed_attachment_hydration_without_failing(self, mock_get_attachment_payload):
        self.message.attachment_manifest = [
            {
                "part_id": "1.2",
                "filename": "metrics.txt",
                "mime_type": "text/plain",
                "attachment_id": "att-reset",
                "size_bytes": 24,
                "content_disposition": "attachment",
            }
        ]
        self.message.save(update_fields=["attachment_manifest", "updated_at"])
        self.attachment.delete()
        self.thread.attachment_ids = []
        self.thread.save(update_fields=["attachment_ids", "updated_at"])
        mock_get_attachment_payload.side_effect = ConnectionResetError("connection reset by peer")

        with self._with_key():
            response = self.client.get(
                reverse("startup_updates_extraction_batch", args=[self.run.run_id]),
                {"limit": 10},
                **self.headers,
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        attachment_payload = response.data["threads"][0]["attachments"][0]
        self.assertEqual(attachment_payload["extraction_status"], ArtifactProcessingStatus.ERROR)
        self.assertEqual(attachment_payload["parse_notes"], "gmail_attachment_hydration_failed")
        self.assertIn("ConnectionResetError", attachment_payload["last_error"])

        failed_attachment = GmailAttachmentArtifact.objects.get(
            organization=self.organization,
            message_artifact=self.message,
            gmail_attachment_id="att-reset",
        )
        self.assertEqual(failed_attachment.raw_content_base64, "")
        self.assertEqual(failed_attachment.extraction_status, ArtifactProcessingStatus.ERROR)
        self.assertEqual(failed_attachment.parse_notes, "gmail_attachment_hydration_failed")
        self.assertIn("ConnectionResetError", failed_attachment.last_error)
        self.thread.refresh_from_db()
        self.assertEqual(self.thread.attachment_ids, [failed_attachment.id])

    @patch("integrations.services.gmail.get_attachment_payload")
    def test_extraction_batch_reuses_failed_attachment_without_refetching(self, mock_get_attachment_payload):
        self.message.attachment_manifest = [
            {
                "part_id": "1.2",
                "filename": "metrics.txt",
                "mime_type": "text/plain",
                "attachment_id": "att-reset",
                "size_bytes": 24,
                "content_disposition": "attachment",
            }
        ]
        self.message.save(update_fields=["attachment_manifest", "updated_at"])
        self.attachment.delete()
        self.thread.attachment_ids = []
        self.thread.save(update_fields=["attachment_ids", "updated_at"])
        mock_get_attachment_payload.side_effect = ConnectionResetError("connection reset by peer")

        with self._with_key():
            first_response = self.client.get(
                reverse("startup_updates_extraction_batch", args=[self.run.run_id]),
                {"limit": 10},
                **self.headers,
            )
            second_response = self.client.get(
                reverse("startup_updates_extraction_batch", args=[self.run.run_id]),
                {"limit": 10},
                **self.headers,
            )

        self.assertEqual(first_response.status_code, status.HTTP_200_OK)
        self.assertEqual(second_response.status_code, status.HTTP_200_OK)
        self.assertEqual(mock_get_attachment_payload.call_count, 1)
        attachment_payload = second_response.data["threads"][0]["attachments"][0]
        self.assertEqual(attachment_payload["extraction_status"], ArtifactProcessingStatus.ERROR)

    def test_extraction_batch_compacts_quoted_gmail_history(self):
        self.thread.message_payloads = [
            {
                "message_id": "msg-123",
                "internal_date": timezone.now().isoformat(),
                "subject": "ACME pilot converted",
                "from_address": "ceo@acme.com",
                "cleaned_text": (
                    "We closed the pilot and now have $24000 ARR.\n\n"
                    "On Monday, someone wrote:\n> old quoted reply\n> repeated old thread"
                ),
            },
            {
                "message_id": "msg-noise",
                "internal_date": timezone.now().isoformat(),
                "subject": "FYI",
                "from_address": "ops@acme.com",
                "cleaned_text": "unsubscribe\nview in browser",
            },
        ]
        self.thread.source_message_ids = ["msg-123", "msg-noise"]
        self.thread.source_message_count = 2
        self.thread.cleaned_text = "\n\n".join(item["cleaned_text"] for item in self.thread.message_payloads)
        self.thread.save(
            update_fields=[
                "message_payloads",
                "source_message_ids",
                "source_message_count",
                "cleaned_text",
                "updated_at",
            ]
        )

        with self._with_key():
            response = self.client.get(
                reverse("startup_updates_extraction_batch", args=[self.run.run_id]),
                {"limit": 10},
                **self.headers,
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        bundle = response.data["threads"][0]
        self.assertIn("We closed the pilot", bundle["cleaned_text"])
        self.assertNotIn("old quoted reply", bundle["cleaned_text"])
        self.assertIn("compression", bundle["participant_summary"])

    @patch("integrations.services.gmail.get_attachment_payload")
    def test_extraction_batch_accepts_long_gmail_attachment_ids(self, mock_get_attachment_payload):
        long_attachment_id = "att-" + ("x" * 400)
        self.message.attachment_manifest = [
            {
                "part_id": "1.2",
                "filename": "metrics.txt",
                "mime_type": "text/plain",
                "attachment_id": long_attachment_id,
                "size_bytes": 24,
                "content_disposition": "attachment",
            }
        ]
        self.message.save(update_fields=["attachment_manifest", "updated_at"])
        self.attachment.delete()
        self.thread.attachment_ids = []
        self.thread.save(update_fields=["attachment_ids", "updated_at"])
        mock_get_attachment_payload.return_value = {"data": "TVJSIHJlYWNoZWQgMTIwMDAgVVNE"}

        with self._with_key():
            response = self.client.get(
                reverse("startup_updates_extraction_batch", args=[self.run.run_id]),
                {"limit": 10},
                **self.headers,
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        hydrated_attachment = GmailAttachmentArtifact.objects.get(
            organization=self.organization,
            message_artifact=self.message,
            gmail_attachment_id=long_attachment_id,
        )
        self.assertEqual(hydrated_attachment.gmail_attachment_id, long_attachment_id)
        mock_get_attachment_payload.assert_called_once_with(
            self.google_connection,
            self.message.gmail_message_id,
            long_attachment_id,
        )

    def test_extraction_batch_scopes_to_run_window_and_top_threads(self):
        now = timezone.now()
        self.message.internal_date = now - timedelta(minutes=20)
        self.message.save(update_fields=["internal_date", "updated_at"])
        self.thread.latest_message_internal_date = now - timedelta(minutes=20)
        self.thread.save(update_fields=["latest_message_internal_date", "updated_at"])
        self.run.run_request["backfill_window_start"] = (now - timedelta(hours=1)).isoformat()
        self.run.run_request["backfill_window_end"] = (now + timedelta(hours=1)).isoformat()
        self.run.run_request["max_source_threads"] = 1
        self.run.save(update_fields=["run_request", "updated_at"])

        GmailMessageArtifact.objects.create(
            organization=self.organization,
            google_connection=self.google_connection,
            gmail_message_id="msg-newest-thread",
            gmail_thread_id="thread-newest-thread",
            internal_date=now,
            subject="Newest thread in run window",
            from_address="founder@acme.com",
            cleaned_text="Newest thread in run window",
            heuristic_score=91,
            heuristic_reasons=["matched_company_domain"],
            relevance_label=GmailRelevanceLabel.RELEVANT,
            needs_thread_context=True,
            metadata_hydrated_at=timezone.now(),
        )
        GmailThreadArtifact.objects.create(
            organization=self.organization,
            google_connection=self.google_connection,
            gmail_thread_id="thread-newest-thread",
            source_message_ids=["msg-newest-thread"],
            message_payloads=[{"message_id": "msg-newest-thread"}],
            cleaned_text="Newest thread in run window",
            hydration_status=ArtifactProcessingStatus.HYDRATED,
            extraction_status=ArtifactProcessingStatus.PENDING,
            source_message_count=1,
            latest_message_internal_date=now,
            hydrated_at=timezone.now(),
        )
        GmailMessageArtifact.objects.create(
            organization=self.organization,
            google_connection=self.google_connection,
            gmail_message_id="msg-stale-thread",
            gmail_thread_id="thread-stale-thread",
            internal_date=now - timedelta(days=10),
            subject="Stale thread outside run window",
            from_address="founder@acme.com",
            cleaned_text="Stale thread outside run window",
            heuristic_score=80,
            heuristic_reasons=["matched_company_domain"],
            relevance_label=GmailRelevanceLabel.RELEVANT,
            needs_thread_context=True,
            metadata_hydrated_at=timezone.now(),
        )
        GmailThreadArtifact.objects.create(
            organization=self.organization,
            google_connection=self.google_connection,
            gmail_thread_id="thread-stale-thread",
            source_message_ids=["msg-stale-thread"],
            message_payloads=[{"message_id": "msg-stale-thread"}],
            cleaned_text="Stale thread outside run window",
            hydration_status=ArtifactProcessingStatus.HYDRATED,
            extraction_status=ArtifactProcessingStatus.PENDING,
            source_message_count=1,
            latest_message_internal_date=now - timedelta(days=10),
            hydrated_at=timezone.now(),
        )

        with self._with_key():
            response = self.client.get(
                reverse("startup_updates_extraction_batch", args=[self.run.run_id]),
                {"limit": 10},
                **self.headers,
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["threads"][0]["gmail_thread_id"], "thread-newest-thread")

    def test_hydration_candidates_ignore_threads_from_other_connections(self):
        GmailThreadArtifact.objects.filter(pk=self.thread.pk).update(hydration_status=ArtifactProcessingStatus.PENDING)
        _other_user, other_connection = self._create_secondary_connection()
        GmailMessageArtifact.objects.create(
            organization=self.organization,
            google_connection=other_connection,
            gmail_message_id="msg-other",
            gmail_thread_id="thread-other",
            internal_date=timezone.now() + timedelta(minutes=2),
            subject="Other connection relevant message",
            from_address="ceo@other.example",
            cleaned_text="Should not be considered for this run",
            heuristic_score=90,
            heuristic_reasons=["matched_company_domain"],
            relevance_label=GmailRelevanceLabel.RELEVANT,
            needs_thread_context=True,
            metadata_hydrated_at=timezone.now(),
        )
        GmailThreadArtifact.objects.create(
            organization=self.organization,
            google_connection=other_connection,
            gmail_thread_id="thread-other",
            source_message_ids=["msg-other"],
            message_payloads=[{"message_id": "msg-other"}],
            cleaned_text="Should not be considered for this run",
            hydration_status=ArtifactProcessingStatus.PENDING,
            extraction_status=ArtifactProcessingStatus.PENDING,
            source_message_count=1,
            latest_message_internal_date=timezone.now() + timedelta(minutes=2),
        )

        with self._with_key():
            response = self.client.get(
                reverse("startup_updates_hydration_candidates", args=[self.run.run_id]),
                {"limit": 10},
                **self.headers,
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["threads"][0]["gmail_thread_id"], self.thread.gmail_thread_id)

    def test_extraction_batch_and_results_scope_threads_to_run_connection(self):
        _other_user, other_connection = self._create_secondary_connection()
        other_thread = GmailThreadArtifact.objects.create(
            organization=self.organization,
            google_connection=other_connection,
            gmail_thread_id=self.thread.gmail_thread_id,
            source_message_ids=["msg-other"],
            message_payloads=[{"message_id": "msg-other"}],
            cleaned_text="Cross-connection thread",
            hydration_status=ArtifactProcessingStatus.HYDRATED,
            extraction_status=ArtifactProcessingStatus.PENDING,
            source_message_count=1,
            latest_message_internal_date=timezone.now() + timedelta(minutes=2),
            hydrated_at=timezone.now(),
        )

        with self._with_key():
            extraction_batch = self.client.get(
                reverse("startup_updates_extraction_batch", args=[self.run.run_id]),
                {"limit": 10},
                **self.headers,
            )

        self.assertEqual(extraction_batch.status_code, status.HTTP_200_OK)
        self.assertEqual(extraction_batch.data["count"], 1)
        self.assertEqual(extraction_batch.data["threads"][0]["gmail_thread_id"], self.thread.gmail_thread_id)
        self.assertEqual(extraction_batch.data["threads"][0]["source_message_ids"], self.thread.source_message_ids)

        with self._with_key():
            extraction_result = self.client.post(
                reverse("startup_updates_extraction_results", args=[self.run.run_id]),
                {
                    "results": [
                        {
                            "gmail_thread_id": self.thread.gmail_thread_id,
                            "extraction_status": ArtifactProcessingStatus.PROCESSED,
                            "attachment_updates": [],
                            "events": [],
                            "metrics": [],
                        }
                    ]
                },
                format="json",
                **self.headers,
            )

        self.assertEqual(extraction_result.status_code, status.HTTP_200_OK)
        self.thread.refresh_from_db()
        other_thread.refresh_from_db()
        self.assertEqual(self.thread.extraction_status, ArtifactProcessingStatus.PROCESSED)
        self.assertEqual(other_thread.extraction_status, ArtifactProcessingStatus.PENDING)

    def test_extraction_results_round_over_precise_metric_value_number(self):
        """An LLM may emit value_number with >4 decimal places; the shared
        MetricResultSerializer should round to 4 places instead of 400-ing the
        whole extraction batch."""
        month_bucket = timezone.now().date().replace(day=1)
        with self._with_key():
            response = self.client.post(
                reverse("startup_updates_extraction_results", args=[self.run.run_id]),
                {
                    "results": [
                        {
                            "gmail_thread_id": self.thread.gmail_thread_id,
                            "extraction_status": ArtifactProcessingStatus.PROCESSED,
                            "attachment_updates": [],
                            "events": [],
                            "metrics": [
                                {
                                    "metric_key": "gross_margin",
                                    "metric_name": "Gross margin",
                                    "value_text": "1/3",
                                    "value_number": "0.33333333",
                                    "unit": "ratio",
                                    "period_month": month_bucket.isoformat(),
                                }
                            ],
                        }
                    ]
                },
                format="json",
                **self.headers,
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        metric = StartupMetricObservation.objects.get(
            organization=self.organization, metric_key="gross_margin"
        )
        self.assertEqual(metric.value_number, Decimal("0.3333"))

    def test_extraction_results_still_reject_value_number_over_max_digits(self):
        """Rounding to 4dp must not bypass the max_digits guard."""
        month_bucket = timezone.now().date().replace(day=1)
        with self._with_key():
            response = self.client.post(
                reverse("startup_updates_extraction_results", args=[self.run.run_id]),
                {
                    "results": [
                        {
                            "gmail_thread_id": self.thread.gmail_thread_id,
                            "extraction_status": ArtifactProcessingStatus.PROCESSED,
                            "attachment_updates": [],
                            "events": [],
                            "metrics": [
                                {
                                    "metric_key": "too_big",
                                    "metric_name": "Too big",
                                    "value_text": "huge",
                                    "value_number": "1234567890123456789.5",
                                    "unit": "",
                                    "period_month": month_bucket.isoformat(),
                                }
                            ],
                        }
                    ]
                },
                format="json",
                **self.headers,
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_extraction_results_truncate_over_long_text_fields(self):
        """LLM free text can exceed DB varchar widths; the shared serializers
        truncate to the column width so persistence doesn't 500 on a DataError."""
        month_bucket = timezone.now().date().replace(day=1)
        with self._with_key():
            response = self.client.post(
                reverse("startup_updates_extraction_results", args=[self.run.run_id]),
                {
                    "results": [
                        {
                            "gmail_thread_id": self.thread.gmail_thread_id,
                            "extraction_status": ArtifactProcessingStatus.PROCESSED,
                            "attachment_updates": [],
                            "events": [
                                {
                                    "canonical_key": "evt-long-title",
                                    "event_type": "customer_win",
                                    "title": "T" * 300,
                                    "month_bucket": month_bucket.isoformat(),
                                }
                            ],
                            "metrics": [
                                {
                                    "metric_key": "arr",
                                    "metric_name": "M" * 300,
                                    "value_text": "V" * 300,
                                    "period_month": month_bucket.isoformat(),
                                }
                            ],
                        }
                    ]
                },
                format="json",
                **self.headers,
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        event = StartupEvent.objects.get(organization=self.organization, canonical_key="evt-long-title")
        self.assertEqual(len(event.title), 255)
        metric = StartupMetricObservation.objects.get(organization=self.organization, metric_key="arr")
        self.assertEqual(len(metric.metric_name), 255)
        self.assertEqual(len(metric.value_text), 255)

    def test_run_context_prefers_pinned_google_connection_over_binding_connection(self):
        _other_user, other_connection = self._create_secondary_connection()
        self.binding.google_connection = other_connection
        self.binding.save(update_fields=["google_connection", "updated_at"])
        GmailMessageArtifact.objects.create(
            organization=self.organization,
            google_connection=other_connection,
            gmail_message_id="msg-other-connection",
            gmail_thread_id="thread-other-connection",
            internal_date=timezone.now() + timedelta(minutes=2),
            subject="Other connection message",
            from_address="ceo@other.example",
            cleaned_text="Should not be considered for this pinned run",
            heuristic_score=99,
            heuristic_reasons=["matched_company_domain"],
            relevance_label=GmailRelevanceLabel.AMBIGUOUS,
            needs_thread_context=True,
            metadata_hydrated_at=timezone.now(),
        )

        with self._with_key():
            batch_response = self.client.get(
                reverse("startup_updates_classification_batch", args=[self.run.run_id]),
                {"limit": 10},
                **self.headers,
            )

        self.assertEqual(batch_response.status_code, status.HTTP_200_OK)
        self.assertEqual(batch_response.data["count"], 1)
        self.assertEqual(batch_response.data["messages"][0]["gmail_message_id"], self.message.gmail_message_id)

    def test_classification_extraction_timeline_and_drafts(self):
        with self._with_key():
            batch_response = self.client.get(
                reverse("startup_updates_classification_batch", args=[self.run.run_id]),
                {"limit": 10},
                **self.headers,
            )

        self.assertEqual(batch_response.status_code, status.HTTP_200_OK)
        self.assertEqual(batch_response.data["count"], 1)
        self.assertEqual(batch_response.data["messages"][0]["gmail_message_id"], self.message.gmail_message_id)

        with self._with_key():
            classify_response = self.client.post(
                reverse("startup_updates_classification_results", args=[self.run.run_id]),
                {
                    "results": [
                        {
                            "gmail_message_id": self.message.gmail_message_id,
                            "relevance_label": GmailRelevanceLabel.RELEVANT,
                            "relevance_score": 0.93,
                            "relevance_reason": "Investor-facing revenue update",
                            "needs_thread_context": True,
                        }
                    ]
                },
                format="json",
                **self.headers,
            )

        self.assertEqual(classify_response.status_code, status.HTTP_200_OK)
        self.message.refresh_from_db()
        self.assertEqual(self.message.relevance_label, GmailRelevanceLabel.RELEVANT)
        self.assertEqual(self.message.relevance_reason, "Investor-facing revenue update")

        with self._with_key():
            extraction_batch = self.client.get(
                reverse("startup_updates_extraction_batch", args=[self.run.run_id]),
                {"limit": 10},
                **self.headers,
            )

        self.assertEqual(extraction_batch.status_code, status.HTTP_200_OK)
        self.assertEqual(extraction_batch.data["count"], 1)
        self.assertEqual(extraction_batch.data["threads"][0]["attachments"][0]["id"], self.attachment.id)

        month_bucket = timezone.now().date().replace(day=1)
        with self._with_key():
            extraction_result = self.client.post(
                reverse("startup_updates_extraction_results", args=[self.run.run_id]),
                {
                    "results": [
                        {
                            "gmail_thread_id": self.thread.gmail_thread_id,
                            "attachment_updates": [
                                {
                                    "id": self.attachment.id,
                                    "extracted_text": "ARR is now 24000 USD",
                                    "extraction_status": ArtifactProcessingStatus.PROCESSED,
                                    "parse_notes": "pdf_parsed",
                                }
                            ],
                            "events": [
                                {
                                    "canonical_key": "evt-arr-upgrade",
                                    "event_type": "customer_win",
                                    "title": "Pilot converted to paid ARR contract",
                                    "summary": "A pilot converted to a paid annual plan.",
                                    "event_date": month_bucket.isoformat(),
                                    "month_bucket": month_bucket.isoformat(),
                                    "date_precision": "day",
                                    "investor_importance": 5,
                                    "quantitative_facts": [{"name": "arr", "value": 24000, "unit": "USD"}],
                                    "evidence_message_ids": [self.message.gmail_message_id],
                                    "evidence_attachment_ids": [self.attachment.id],
                                    "source_thread_ids": [self.thread.gmail_thread_id],
                                    "confidence": 0.88,
                                }
                            ],
                            "metrics": [
                                {
                                    "metric_key": "arr",
                                    "metric_name": "ARR",
                                    "value_text": "24000",
                                    "value_number": "24000.0000",
                                    "unit": "USD",
                                    "observed_at": timezone.now().isoformat(),
                                    "period_month": month_bucket.isoformat(),
                                    "confidence": 0.9,
                                    "evidence_message_ids": [self.message.gmail_message_id],
                                    "evidence_attachment_ids": [self.attachment.id],
                                    "summary": "ARR explicitly stated in investor update thread.",
                                }
                            ],
                        }
                    ]
                },
                format="json",
                **self.headers,
            )

        self.assertEqual(extraction_result.status_code, status.HTTP_200_OK)
        self.thread.refresh_from_db()
        self.attachment.refresh_from_db()
        self.assertEqual(self.thread.extraction_status, ArtifactProcessingStatus.PROCESSED)
        self.assertEqual(self.attachment.extracted_text, "ARR is now 24000 USD")
        event = StartupEvent.objects.get(organization=self.organization, canonical_key="evt-arr-upgrade")
        metric = StartupMetricObservation.objects.get(organization=self.organization, metric_key="arr")
        self.assertEqual(metric.value_number, Decimal("24000.0000"))

        with self._with_key():
            timeline_response = self.client.get(
                reverse("startup_updates_timeline", args=[self.run.run_id]),
                **self.headers,
            )
        self.assertEqual(timeline_response.status_code, status.HTTP_200_OK)
        self.assertIn(month_bucket.isoformat(), timeline_response.data["timeline"]["months"])

        with self._with_key():
            draft_response = self.client.post(
                reverse("startup_updates_draft_results", args=[self.run.run_id]),
                {
                    "drafts": [
                        {
                            "month": month_bucket.isoformat(),
                            "status": "ready",
                            "model_name": "gpt-5.4",
                            "groundedness_status": "passed",
                            "structured_memo": {
                                "title": "Acme Investor Update",
                                "topline": "Revenue converted from pilot to paid ARR.",
                                "kpi_snapshot": [{"label": "ARR", "value": "$24,000"}],
                                "asks": ["Customer intros"],
                                "highlights": ["Converted pilot to annual deal"],
                                "lowlights": ["None"],
                                "operations": ["Hiring one engineer"],
                                "learnings": ["Paid pilots convert faster when buyer scope is narrow"],
                                "next_30_days": ["Close two more pilots"],
                            },
                            "evidence_event_ids": [event.id],
                            "evidence_metric_ids": [metric.id],
                            "carry_forward_event_ids": [],
                            "groundedness_notes": "All claims evidence-backed.",
                        }
                    ]
                },
                format="json",
                **self.headers,
            )

        self.assertEqual(draft_response.status_code, status.HTTP_200_OK)
        draft = MonthlyUpdateDraft.objects.get(organization=self.organization, month=month_bucket)
        self.assertEqual(draft.status, "ready")
        self.assertIn("# Acme Investor Update", draft.rendered_markdown)

        with self._with_key():
            draft_list = self.client.get(
                reverse("startup_updates_draft_list"),
                {"domain": self.organization.domain},
                **self.headers,
            )
            draft_detail = self.client.get(
                reverse("startup_updates_draft_detail", args=[draft.id]),
                **self.headers,
            )
            draft_results = self.client.get(
                reverse("startup_updates_draft_results", args=[self.run.run_id]),
                **self.headers,
            )

        self.assertEqual(draft_list.status_code, status.HTTP_200_OK)
        self.assertEqual(len(draft_list.data["drafts"]), 1)
        self.assertEqual(draft_detail.status_code, status.HTTP_200_OK)
        self.assertEqual(draft_detail.data["events"][0]["canonical_key"], event.canonical_key)
        self.assertEqual(draft_detail.data["metrics"][0]["metric_key"], metric.metric_key)
        self.assertEqual(draft_results.status_code, status.HTTP_200_OK)
        self.assertEqual(
            draft_results.data["draft"]["highlights"],
            "Converted pilot to annual deal\nHiring one engineer",
        )
        self.assertEqual(draft_results.data["draft"]["learnings"], "Paid pilots convert faster when buyer scope is narrow")
        self.assertEqual(draft_results.data["draft"]["next30Days"], "Close two more pilots")
        self.assertNotIn("Learning:", draft_results.data["draft"]["highlights"])
        self.assertNotIn("Next 30 days:", draft_results.data["draft"]["asks"])


class StartupUpdateOpenRunsViewTest(StartupUpdateApiTestCase):
    def test_open_runs_endpoint_lists_active_startup_update_runs(self):
        binding = UserStartupBinding.objects.create(
            user=self.user,
            organization=self.organization,
            google_connection=self.google_connection,
            is_default_for_gmail=True,
        )
        create_startup_update_run(
            organization=self.organization,
            binding=binding,
            window_months=6,
        )

        with self._with_key():
            response = self.client.get(
                reverse("startup_updates_open_runs"),
                {"limit": 10},
                **self.headers,
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["runs"][0]["domain"], self.organization.domain)


class StartupUpdateServiceHelpersTest(TestCase):
    def test_default_backfill_window_defaults_to_30_days(self):
        now = datetime(2026, 3, 26, tzinfo=dt_timezone.utc)
        after_dt, before_dt = default_backfill_window(now=now)
        self.assertEqual(before_dt, now)
        self.assertEqual(after_dt, now - timedelta(days=30))

    def test_build_backfill_query_uses_unix_timestamps(self):
        after_dt = datetime(2026, 1, 1, tzinfo=dt_timezone.utc)
        before_dt = datetime(2026, 2, 1, tzinfo=dt_timezone.utc)
        query = build_backfill_query(after_dt=after_dt, before_dt=before_dt)
        self.assertIn("after:1767225600", query)
        self.assertIn("before:1769904000", query)
        self.assertIn("-in:spam -in:trash", query)
        self.assertIn("-category:promotions", query)
        self.assertIn("-category:social", query)
        self.assertIn("-category:forums", query)

    def test_clean_email_text_strips_reply_chain(self):
        raw = "Latest update line\n\nOn Tue, someone wrote:\n> older quote\n> more"
        self.assertEqual(clean_email_text(raw), "Latest update line")

    def test_render_monthly_update_markdown_is_deterministic(self):
        markdown = render_monthly_update_markdown(
            {
                "title": "Acme Investor Update",
                "topline": "Strong month.",
                "kpi_snapshot": [{"label": "ARR", "value": "$24,000"}],
                "asks": ["Intro to enterprise buyers"],
                "highlights": ["Pilot converted"],
                "lowlights": ["Long sales cycle"],
                "operations": ["Hired first AE"],
                "next_30_days": ["Close two more logos"],
            }
        )
        self.assertIn("## KPI Snapshot", markdown)
        self.assertIn("- **ARR:** $24,000", markdown)

    def test_score_message_for_profile_flags_relevant_company_mail(self):
        profile = SimpleNamespace(
            company_aliases=["Acme"],
            domain_aliases=["acme.com"],
            founder_names=["Alice Founder"],
            team_names=[],
            investor_domains=["fund.example"],
            investor_names=[],
            competitor_names=[],
            competitor_domains=[],
            customer_names=[],
            customer_domains=[],
            prospect_names=[],
            prospect_domains=[],
            positive_keywords=["pilot", "arr"],
            negative_keywords=[],
        )
        artifact = SimpleNamespace(
            subject="Acme pilot now at ARR",
            snippet="We signed the annual contract",
            cleaned_text="ARR is now 24000",
            from_address="alice@acme.com",
            to_addresses=["partner@fund.example"],
            cc_addresses=[],
        )
        score, reasons, label = score_message_for_profile(profile, artifact)
        self.assertGreaterEqual(score, 80)
        self.assertEqual(label, GmailRelevanceLabel.RELEVANT)
        self.assertIn("matched_company_domain", reasons)

    def test_score_message_for_profile_hard_filters_bulk_no_reply_mail(self):
        profile = SimpleNamespace(
            company_aliases=["Acme"],
            domain_aliases=["acme.com"],
            founder_names=[],
            team_names=[],
            investor_domains=[],
            investor_names=[],
            competitor_names=[],
            competitor_domains=[],
            customer_names=[],
            customer_domains=[],
            prospect_names=[],
            prospect_domains=[],
            positive_keywords=["acme"],
            negative_keywords=[],
        )
        artifact = SimpleNamespace(
            subject="Weekly digest and magic link",
            snippet="Use this magic link to sign in",
            cleaned_text="",
            from_address="noreply@news.example",
            to_addresses=["samdonegan@gmail.com"],
            cc_addresses=[],
            bcc_addresses=[],
            reply_to_addresses=[],
            header_values={
                "list-unsubscribe": "<mailto:unsubscribe@example.com>",
                "precedence": "bulk",
            },
            label_ids=["CATEGORY_PROMOTIONS"],
        )

        score, reasons, label = score_message_for_profile(profile, artifact)

        self.assertEqual(score, 0)
        self.assertEqual(label, GmailRelevanceLabel.IRRELEVANT)
        self.assertIn("hard_filtered_bulk_header", reasons)
        self.assertIn("hard_filtered_no_reply_sender", reasons)
        self.assertIn("hard_filtered_gmail_category", reasons)

    def test_score_message_for_profile_allowlist_overrides_hard_filter(self):
        profile = SimpleNamespace(
            company_aliases=["Acme"],
            domain_aliases=["acme.com"],
            founder_names=[],
            team_names=["Sam Donegan"],
            investor_domains=["fund.example"],
            investor_names=[],
            competitor_names=[],
            competitor_domains=[],
            customer_names=[],
            customer_domains=[],
            prospect_names=[],
            prospect_domains=[],
            positive_keywords=["acme", "arr"],
            negative_keywords=[],
        )
        artifact = SimpleNamespace(
            subject="Acme investor newsletter with ARR update",
            snippet="ARR is now 24000",
            cleaned_text="Investor update for Acme ARR",
            from_address="noreply@acme.com",
            to_addresses=["partner@fund.example"],
            cc_addresses=[],
            bcc_addresses=[],
            reply_to_addresses=[],
            header_values={"list-unsubscribe": "<mailto:unsubscribe@acme.com>"},
            label_ids=["CATEGORY_PROMOTIONS"],
        )

        score, reasons, label = score_message_for_profile(profile, artifact)

        self.assertGreater(score, 20)
        self.assertNotEqual(label, GmailRelevanceLabel.IRRELEVANT)
        self.assertIn("allowlist_override_hard_filter", reasons)
        self.assertIn("matched_company_domain", reasons)

    def test_score_message_for_profile_hard_filters_invites_receipts_and_auth_subjects(self):
        profile = SimpleNamespace(
            company_aliases=["Acme"],
            domain_aliases=["acme.com"],
            founder_names=[],
            team_names=[],
            investor_domains=[],
            investor_names=[],
            competitor_names=[],
            competitor_domains=[],
            customer_names=[],
            customer_domains=[],
            prospect_names=[],
            prospect_domains=[],
            positive_keywords=[],
            negative_keywords=[],
        )

        for subject in [
            "Calendar invitation",
            "Payment received",
            "Order confirmation",
            "Verification code",
        ]:
            artifact = SimpleNamespace(
                subject=subject,
                snippet="",
                cleaned_text="",
                from_address="notifications@example.com",
                to_addresses=["samdonegan@gmail.com"],
                cc_addresses=[],
                bcc_addresses=[],
                reply_to_addresses=[],
                header_values={},
                label_ids=[],
            )

            score, reasons, label = score_message_for_profile(profile, artifact)

            self.assertEqual(score, 0, msg=subject)
            self.assertEqual(label, GmailRelevanceLabel.IRRELEVANT, msg=subject)
            self.assertIn("hard_filtered_low_signal_pattern", reasons, msg=subject)

    def _own_domain_profile(self):
        return SimpleNamespace(
            company_aliases=["Acme"],
            domain_aliases=["acme.com"],
            founder_names=[],
            team_names=[],
            investor_domains=[],
            investor_names=[],
            competitor_names=[],
            competitor_domains=[],
            customer_names=[],
            customer_domains=["customer.example"],
            prospect_names=[],
            prospect_domains=[],
            positive_keywords=[],
            negative_keywords=[],
        )

    def test_score_message_for_profile_ignores_own_domain_among_recipients(self):
        # Every message in a founder's mailbox is addressed to the founder at
        # the company domain. Counting that as a signal would allowlist the
        # whole inbox and cancel out every hard filter.
        profile = self._own_domain_profile()
        artifact = SimpleNamespace(
            subject="Weekly digest",
            snippet="Top posts for you this week",
            cleaned_text="",
            from_address="noreply@news.example",
            to_addresses=["founder@acme.com"],
            cc_addresses=[],
            bcc_addresses=[],
            reply_to_addresses=[],
            header_values={"list-unsubscribe": "<mailto:unsubscribe@news.example>"},
            label_ids=["CATEGORY_PROMOTIONS"],
        )

        score, reasons, label = score_message_for_profile(profile, artifact)

        self.assertEqual(score, 0)
        self.assertEqual(label, GmailRelevanceLabel.IRRELEVANT)
        self.assertNotIn("allowlist_company_domain", reasons)
        self.assertNotIn("matched_company_domain", reasons)

    def test_score_message_for_profile_counts_company_domain_only_for_sender(self):
        profile = self._own_domain_profile()
        base = {
            "subject": "Quick question",
            "snippet": "Following up on the thing",
            "cleaned_text": "",
            "to_addresses": ["founder@acme.com"],
            "cc_addresses": [],
            "bcc_addresses": [],
            "reply_to_addresses": [],
            "header_values": {},
            "label_ids": [],
        }

        internal = SimpleNamespace(from_address="teammate@acme.com", **base)
        internal_score, internal_reasons, _ = score_message_for_profile(profile, internal)
        self.assertIn("matched_company_domain", internal_reasons)

        external = SimpleNamespace(from_address="someone@vendor.example", **base)
        external_score, external_reasons, _ = score_message_for_profile(profile, external)
        self.assertNotIn("matched_company_domain", external_reasons)

        # Internal mail is worth at least the domain bonus more. It also picks
        # up the company alias from the sender address itself, so the gap is
        # wider than 25 and the exact figure is not the point.
        self.assertGreaterEqual(internal_score - external_score, 25)
        self.assertEqual(external_score, 50)

    def test_score_message_for_profile_ignores_company_alias_in_own_address(self):
        # "acme" appears only inside the recipient's own address, which says
        # nothing about what the message is about.
        profile = self._own_domain_profile()
        artifact = SimpleNamespace(
            subject="Lunch on Thursday?",
            snippet="Are you free around one",
            cleaned_text="",
            from_address="friend@personal.example",
            to_addresses=["founder@acme.com"],
            cc_addresses=[],
            bcc_addresses=[],
            reply_to_addresses=[],
            header_values={},
            label_ids=[],
        )

        score, reasons, label = score_message_for_profile(profile, artifact)

        self.assertNotIn("matched_company_alias_or_positive_keyword", reasons)
        self.assertEqual(score, 50)
        self.assertEqual(label, GmailRelevanceLabel.AMBIGUOUS)

    def test_score_message_for_profile_still_credits_counterparty_domains(self):
        profile = self._own_domain_profile()
        artifact = SimpleNamespace(
            subject="Renewal paperwork",
            snippet="Sending the signed contract across",
            cleaned_text="",
            from_address="ap@customer.example",
            to_addresses=["founder@acme.com"],
            cc_addresses=[],
            bcc_addresses=[],
            reply_to_addresses=[],
            header_values={},
            label_ids=[],
        )

        score, reasons, label = score_message_for_profile(profile, artifact)

        self.assertIn("matched_customer_or_prospect_domain", reasons)
        self.assertGreaterEqual(score, 65)
        self.assertNotEqual(label, GmailRelevanceLabel.IRRELEVANT)

    def test_sync_startup_profile_from_company_merges_existing_org_context(self):
        user = User.objects.create_user(
            email="merge-founder@example.com",
            password="password123",
            first_name="Alicia",
            last_name="Founder",
        )
        organization = Organization.objects.create(
            name="Legacy Co",
            domain="legacy.example",
            competitors=[{"name": "RivalOne", "domain": "rival.one"}],
            seed_keywords=["workflow", "back office"],
        )
        OrganizationContentConfig.objects.create(
            organization=organization,
            brand_name="Legacy AI",
            company_context="Legacy AI automates back-office operations.",
        )
        profile = StartupProfile.objects.create(
            organization=organization,
            company_aliases=["Legacy Co"],
            founder_names=["Existing Founder"],
            team_names=["Existing Founder"],
            notes="Manual founder notes.",
            stage="series_a",
        )
        company = SimpleNamespace(name="Legacy Labs", domain="legacy.example")

        sync_startup_profile_from_company(
            startup_profile=profile,
            organization=organization,
            company=company,
            user=user,
        )

        profile.refresh_from_db()
        organization.refresh_from_db()
        self.assertEqual(organization.name, "Legacy Labs")
        self.assertEqual(profile.stage, "series_a")
        self.assertIn("Legacy Labs", profile.company_aliases)
        self.assertIn("Legacy AI", profile.company_aliases)
        self.assertIn("legacy.example", profile.domain_aliases)
        self.assertIn("Alicia Founder", profile.founder_names)
        self.assertIn("Alicia Founder", profile.team_names)
        self.assertIn("RivalOne", profile.competitor_names)
        self.assertIn("rival.one", profile.competitor_domains)
        self.assertIn("workflow", profile.positive_keywords)
        self.assertIn("Manual founder notes.", profile.notes)
        self.assertIn("Legacy AI automates back-office operations.", profile.notes)


def _ga_run_report_payload(body: dict) -> dict:
    metric_names = [metric["name"] for metric in (body.get("metrics") or [])]
    dimensions = [dimension["name"] for dimension in (body.get("dimensions") or [])]
    metric_headers = [{"name": name} for name in metric_names]
    totals = [{"metricValues": [{"value": str(1000 + index * 100)} for index in range(len(metric_names))]}]
    rows = []
    if dimensions:
        for row_index in range(2):
            rows.append(
                {
                    "dimensionValues": [{"value": f"value-{row_index}"}],
                    "metricValues": [{"value": str(100 + row_index)} for _ in metric_names],
                }
            )
    return {
        "dimensionHeaders": [{"name": dimension} for dimension in dimensions],
        "metricHeaders": metric_headers,
        "rows": rows,
        "totals": totals,
        "rowCount": len(rows),
    }


class FakeGaResponse:
    def __init__(self, payload: dict, *, status_code: int = 200, headers: Optional[dict] = None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.content = b"{}"

    def raise_for_status(self):
        if self.status_code >= 400 and self.status_code != 429:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class GoogleAnalyticsServiceUnitTest(TestCase):
    def test_period_bounds_uses_current_and_prior_month(self):
        bounds = period_bounds_for_run({"current_month": "2026-03-01"})
        self.assertEqual(bounds, ("2026-03-01", "2026-03-31", "2026-02-01", "2026-02-28"))

    def test_report_specs_cover_expected_report_types(self):
        report_types = {spec["report_type"] for spec in GA_REPORT_SPECS}
        self.assertEqual(
            report_types,
            {"traffic_overview", "acquisition_channels", "top_pages", "key_events", "engagement"},
        )


class StartupUpdateGoogleAnalyticsPipelineViewTest(StartupUpdateApiTestCase):
    def setUp(self):
        super().setUp()
        self.profile = StartupProfile.objects.create(
            organization=self.organization,
            company_aliases=["Acme"],
            domain_aliases=["acme.com"],
        )
        self.binding = UserStartupBinding.objects.create(
            user=self.user,
            organization=self.organization,
            google_connection=self.google_connection,
            is_default_for_gmail=True,
        )
        self.connection = ExternalServiceConnection.objects.create(
            provider=ExternalServiceProvider.GOOGLE_ANALYTICS,
            user=self.user,
            organization=self.organization,
            access_token="ga-access",
            refresh_token="ga-refresh",
            token_expires_at=timezone.now() + timedelta(hours=1),
            external_account_id="founder@gmail.com",
            account_label="founder@gmail.com",
        )
        self.selection = GoogleAnalyticsPropertySelection.objects.create(
            connection=self.connection,
            user=self.user,
            organization=self.organization,
            property_id="123456789",
            property_display_name="Acme Marketing Site",
            account_id="100",
            selected=True,
        )
        self.run = create_startup_update_run(
            organization=self.organization,
            binding=self.binding,
            input_sources=["gmail", "google_analytics"],
        )

    def _post(self, name, body=None):
        return self.client.post(
            reverse(name, args=[self.run.run_id]), body or {}, format="json", **self.headers
        )

    def _get(self, name, params=None):
        return self.client.get(reverse(name, args=[self.run.run_id]), params or {}, **self.headers)

    def test_run_request_pins_ga_connection_and_property_ids(self):
        self.run.refresh_from_db()
        self.assertEqual(self.run.run_request["google_analytics_connection_id"], self.connection.id)
        self.assertEqual(self.run.run_request["google_analytics_property_ids"], ["123456789"])
        self.assertIn("google_analytics_backfill", self.run.step_order)
        ga_context = self.run.run_request["external_context"]["google_analytics"]
        self.assertEqual(ga_context["selected_property_ids"], ["123456789"])
        self.assertIn("not financial truth", ga_context["warning"])

    def test_full_google_analytics_pipeline_persists_events_and_metrics(self):
        with self.settings(INTERNAL_API_KEY=self.api_key):
            with patch(
                "integrations.services.google_analytics.requests.post",
                side_effect=lambda url, headers=None, json=None, timeout=None: FakeGaResponse(
                    _ga_run_report_payload(json or {})
                ),
            ) as mock_post:
                backfill = self._post("startup_updates_google_analytics_backfill")

            self.assertEqual(backfill.status_code, status.HTTP_200_OK)
            self.assertEqual(backfill.data["reports_synced"], len(GA_REPORT_SPECS))
            self.assertEqual(backfill.data["properties_synced"], 1)
            self.assertFalse(backfill.data["has_more"])
            # 5 report specs x 2 date ranges (current + prior)
            self.assertEqual(mock_post.call_count, len(GA_REPORT_SPECS) * 2)

            classification_batch = self._get(
                "startup_updates_google_analytics_classification_batch", {"limit": 40}
            )
            self.assertEqual(classification_batch.status_code, status.HTTP_200_OK)
            self.assertEqual(classification_batch.data["count"], len(GA_REPORT_SPECS))
            report_ids = [report["ga_report_id"] for report in classification_batch.data["reports"]]
            self.assertTrue(all(rid.startswith("ga:property:123456789:") for rid in report_ids))

            classification_results = self._post(
                "startup_updates_google_analytics_classification_results",
                {
                    "results": [
                        {
                            "ga_report_id": report_id,
                            "relevance_label": "update_worthy",
                            "relevance_score": 0.9,
                            "relevance_reason": "Material traffic movement.",
                            "needs_extraction": True,
                            "extraction_hints": {"important_metric_keys": ["sessions"], "extraction_hint": ""},
                        }
                        for report_id in report_ids
                    ]
                },
            )
            self.assertEqual(classification_results.status_code, status.HTTP_200_OK)
            self.assertEqual(classification_results.data["updated_count"], len(GA_REPORT_SPECS))

            extraction_batch = self._get(
                "startup_updates_google_analytics_extraction_batch", {"limit": 40}
            )
            self.assertEqual(extraction_batch.status_code, status.HTTP_200_OK)
            self.assertEqual(extraction_batch.data["count"], len(GA_REPORT_SPECS))

            target_report_id = report_ids[0]
            extraction_results = self._post(
                "startup_updates_google_analytics_extraction_results",
                {
                    "results": [
                        {
                            "ga_report_id": target_report_id,
                            "extraction_status": "processed",
                            "events": [
                                {
                                    "canonical_key": "ga_sessions_tripled",
                                    "event_type": "product_milestone",
                                    "title": "Website sessions grew sharply",
                                    "summary": "Google Analytics reported strong session growth.",
                                    "month_bucket": "2026-03-01",
                                    "evidence_message_ids": [f"ga:metric:{target_report_id}:sessions"],
                                }
                            ],
                            "metrics": [
                                {
                                    "metric_key": "websiteVisitors",
                                    "metric_name": "Website sessions",
                                    "value_text": "8,400 sessions",
                                    "value_number": "8400",
                                    "period_month": "2026-03-01",
                                }
                            ],
                        }
                    ]
                },
            )

        self.assertEqual(extraction_results.status_code, status.HTTP_200_OK)
        self.assertEqual(extraction_results.data["event_count"], 1)
        self.assertEqual(extraction_results.data["metric_count"], 1)

        event = StartupEvent.objects.get(organization=self.organization, canonical_key="ga_sessions_tripled")
        self.assertEqual(event.event_type, "product_milestone")
        self.assertIn(f"google_analytics:property:123456789", event.source_thread_ids)

        metric = StartupMetricObservation.objects.get(
            organization=self.organization,
            source_provider=ExternalServiceProvider.GOOGLE_ANALYTICS,
            metric_key="websiteVisitors",
        )
        self.assertEqual(metric.value_text, "8,400 sessions")
        self.assertEqual(str(metric.value_number), "8400.0000")

        # Re-running extraction-batch should now exclude the extracted report.
        with self.settings(INTERNAL_API_KEY=self.api_key):
            second_batch = self._get(
                "startup_updates_google_analytics_extraction_batch", {"limit": 40}
            )
        self.assertEqual(second_batch.data["count"], len(GA_REPORT_SPECS) - 1)
