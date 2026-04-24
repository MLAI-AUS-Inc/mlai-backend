from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace
from typing import Optional
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from core.models import (
    ContentFactoryApprovalState,
    ContentFactoryRun,
    ContentFactoryRunStatus,
    ContentFactoryRunStep,
    ContentFactoryRunStepAttempt,
    ContentFactoryStepStatus,
    Organization,
    OrganizationContentConfig,
)
from integrations.models import (
    ArtifactProcessingStatus,
    GmailAttachmentArtifact,
    GmailMessageArtifact,
    GmailRelevanceLabel,
    GmailSyncCursor,
    GmailThreadArtifact,
    GoogleConnection,
    MonthlyUpdateDraft,
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
from integrations.services.startup_updates import (
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

User = get_user_model()


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
    @patch("integrations.api_views_startup_updates.notify_valley_run_created")
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
        self.assertEqual(len(run.run_request["draft_months"]), 3)
        self.assertEqual(run.run_request["startup_context"]["stage"], "seed")
        self.assertEqual(run.run_request["startup_context"]["company_aliases"], ["Acme", "Acme AI"])
        self.assertTrue(
            GmailSyncCursor.objects.filter(
                organization=self.organization,
                google_connection=self.google_connection,
            ).exists()
        )
        mock_notify.assert_called_once_with(run.run_id)

    @patch("integrations.api_views_startup_updates.notify_valley_run_created")
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

    @patch("integrations.api_views_startup_updates.sync_message_metadata_page")
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

    @patch("integrations.api_views_startup_updates.sync_message_metadata_page")
    @patch("integrations.api_views_startup_updates.sync_history_metadata_page")
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

    @patch("integrations.api_views_startup_updates.hydrate_thread_artifact")
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

        self.assertEqual(draft_list.status_code, status.HTTP_200_OK)
        self.assertEqual(len(draft_list.data["drafts"]), 1)
        self.assertEqual(draft_detail.status_code, status.HTTP_200_OK)
        self.assertEqual(draft_detail.data["events"][0]["canonical_key"], event.canonical_key)
        self.assertEqual(draft_detail.data["metrics"][0]["metric_key"], metric.metric_key)


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
