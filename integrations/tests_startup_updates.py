from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from core.models import ContentFactoryRun, Organization, OrganizationContentConfig
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
)
from integrations.services.startup_updates import (
    create_startup_update_run,
    render_monthly_update_markdown,
    score_message_for_profile,
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
                },
                format="json",
                **self.headers,
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        profile = StartupProfile.objects.get(organization=self.organization)
        binding = UserStartupBinding.objects.get(user=self.user, organization=self.organization)
        self.assertEqual(profile.product_names, ["FlowPilot"])
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
        self.assertTrue(
            GmailSyncCursor.objects.filter(
                organization=self.organization,
                google_connection=self.google_connection,
            ).exists()
        )
        mock_notify.assert_called_once_with(run.run_id)


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
        self.assertEqual(cursor.last_history_id, "")


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
        self.message = GmailMessageArtifact.objects.create(
            organization=self.organization,
            google_connection=self.google_connection,
            gmail_message_id="msg-123",
            gmail_thread_id="thread-123",
            internal_date=timezone.now(),
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
            latest_message_internal_date=timezone.now(),
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
    def test_build_backfill_query_uses_unix_timestamps(self):
        after_dt = datetime(2026, 1, 1, tzinfo=dt_timezone.utc)
        before_dt = datetime(2026, 2, 1, tzinfo=dt_timezone.utc)
        query = build_backfill_query(after_dt=after_dt, before_dt=before_dt)
        self.assertIn("after:1767225600", query)
        self.assertIn("before:1769904000", query)
        self.assertIn("-in:spam -in:trash", query)

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
