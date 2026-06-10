from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import resolve, reverse
from django.utils import timezone
from rest_framework.test import APIClient

from content_factory.models import OrganizationContentConfig
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStatus, ContentFactoryStepStatus
from integrations.models import ExternalServiceConnection, ExternalServiceProvider, GoogleConnection
from integrations.services.valley_harness import ValleyHarnessResult
from startup_updates.models import (
    MonthlyUpdateDraft,
    MonthlyUpdateDraftStatus,
    SlackChannelSelection,
    StartupEvent,
    StartupManualDocument,
    StartupMetricObservation,
    StartupProfile,
    UserStartupBinding,
)
from startup_updates.services import (
    DEFAULT_BACKFILL_MONTHS,
    SUPERSEDED_GMAIL_CONNECTION_ERROR,
    create_startup_update_run,
    resolve_or_create_profile,
    upsert_monthly_update_draft,
)
from .models import VibeRaisingCompany, VibeRaisingProfile


User = get_user_model()


class VibeRaisingApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="founder@example.com",
            password="password",
            first_name="Founder",
            last_name="User",
            role="participant",
        )

    def _create_founder_company(self, *, domain="acme.com", registered=True):
        profile = VibeRaisingProfile.objects.create(user=self.user, role=VibeRaisingProfile.ROLE_FOUNDER)
        company = VibeRaisingCompany.objects.create(
            profile=profile,
            name="Acme Inc.",
            domain=domain,
            registered=registered,
        )
        profile.active_company = company
        profile.save(update_fields=["active_company", "updated_at"])
        return profile, company

    def _create_google_connection(
        self,
        *,
        user=None,
        email="founder@gmail.com",
        refresh_token="refresh-token",
        scope="https://www.googleapis.com/auth/gmail.readonly",
    ):
        return GoogleConnection.objects.create(
            user=user or self.user,
            google_email=email,
            refresh_token=refresh_token,
            scope=scope,
        )

    def _create_manual_document(self, *, company=None, user=None, domain="acme.com", filename="memo.txt"):
        if company is None:
            _profile, company = self._create_founder_company(domain=domain, registered=True)
        organization, _startup_profile = resolve_or_create_profile(domain=domain)
        return StartupManualDocument.objects.create(
            organization=organization,
            company=company,
            created_by=user or self.user,
            original_filename=filename,
            content_type="text/plain",
            file_size_bytes=64,
            storage_path=f"vibe-raising/manual-documents/org-{organization.id}/company-{company.id}/user-{(user or self.user).id}/memo.txt",
            extraction_status="processed",
            extracted_text="Pilot conversion improved and onboarding risk remains open.",
            text_size_chars=56,
            parse_notes="text_parsed",
        )

    def _create_active_gmail_run_with_slack_selection(self):
        self._create_founder_company()
        google_connection = self._create_google_connection()
        organization, _profile = resolve_or_create_profile(domain="acme.com")
        binding = UserStartupBinding.objects.create(
            user=self.user,
            organization=organization,
            google_connection=google_connection,
            role="founder",
            is_default_for_gmail=True,
        )
        slack_connection = ExternalServiceConnection.objects.create(
            user=self.user,
            organization=organization,
            provider=ExternalServiceProvider.SLACK,
            account_label="Acme Slack",
        )
        SlackChannelSelection.objects.create(
            connection=slack_connection,
            user=self.user,
            organization=organization,
            channel_id="C123",
            channel_name="wins",
            selected=True,
        )
        run = create_startup_update_run(
            organization=organization,
            binding=binding,
            input_sources=["gmail"],
        )
        run.steps.update(
            status=ContentFactoryStepStatus.COMPLETED,
            attempts=2,
            completed_at=timezone.now(),
        )
        run.status = ContentFactoryRunStatus.RUNNING
        run.current_step = "timeline_merge"
        run.save(update_fields=["status", "current_step", "updated_at"])
        return organization, binding, run

    def test_profile_requires_authentication(self):
        response = self.client.get("/api/v1/vibe-raising/profile/")
        self.assertEqual(response.status_code, 401)

    def test_get_profile_returns_404_before_onboarding(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.get("/api/v1/vibe-raising/profile/")
        self.assertEqual(response.status_code, 404)

    def test_founder_profile_post_creates_profile(self):
        self.client.force_authenticate(user=self.user)

        response = self.client.post(
            "/api/v1/vibe-raising/profile/",
            {"role": "founder"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["role"], "founder")
        self.assertIsNone(response.data["organizationName"])
        self.assertEqual(response.data["companies"], [])
        self.assertIsNone(response.data["activeCompanyId"])

        profile = VibeRaisingProfile.objects.get(user=self.user)
        self.assertEqual(profile.role, VibeRaisingProfile.ROLE_FOUNDER)
        self.assertIsNone(profile.organization_name)

    def test_investor_profile_requires_organization_name(self):
        self.client.force_authenticate(user=self.user)

        response = self.client.post(
            "/api/v1/vibe-raising/profile/",
            {"role": "investor"},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("organizationName", response.data)

    def test_profile_put_matches_post_behavior(self):
        self.client.force_authenticate(user=self.user)

        response = self.client.put(
            "/api/v1/vibe-raising/profile/",
            {"role": "investor", "organizationName": "Alpha Ventures"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["role"], "investor")
        self.assertEqual(response.data["organizationName"], "Alpha Ventures")
        self.assertEqual(response.data["companies"], [])
        self.assertIsNone(response.data["activeCompanyId"])

    def test_founder_can_create_first_company_and_it_becomes_active(self):
        self.client.force_authenticate(user=self.user)
        VibeRaisingProfile.objects.create(user=self.user, role=VibeRaisingProfile.ROLE_FOUNDER)

        response = self.client.post(
            "/api/v1/vibe-raising/companies/",
            {
                "name": "Acme Inc.",
                "domain": "acme.com",
                "abn": "123",
                "registered": False,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["name"], "Acme Inc.")
        self.assertEqual(response.data["domain"], "acme.com")

        profile = VibeRaisingProfile.objects.get(user=self.user)
        self.assertIsNotNone(profile.active_company_id)
        self.assertEqual(str(profile.active_company_id), response.data["id"])

    def test_founder_can_update_owned_company_by_company_id(self):
        self.client.force_authenticate(user=self.user)
        profile = VibeRaisingProfile.objects.create(user=self.user, role=VibeRaisingProfile.ROLE_FOUNDER)
        company = VibeRaisingCompany.objects.create(
            profile=profile,
            name="Acme Inc.",
            domain="old.example",
            registered=False,
        )

        response = self.client.post(
            "/api/v1/vibe-raising/companies/",
            {
                "companyId": str(company.id),
                "name": "Acme Inc.",
                "domain": "new.example",
                "registered": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        company.refresh_from_db()
        self.assertEqual(company.domain, "new.example")
        self.assertTrue(company.registered)

    def test_retry_create_same_name_does_not_duplicate_company(self):
        self.client.force_authenticate(user=self.user)
        VibeRaisingProfile.objects.create(user=self.user, role=VibeRaisingProfile.ROLE_FOUNDER)

        first = self.client.post(
            "/api/v1/vibe-raising/companies/",
            {"name": "Acme Inc.", "domain": "first.example"},
            format="json",
        )
        second = self.client.post(
            "/api/v1/vibe-raising/companies/",
            {"name": "acme inc.", "domain": "second.example"},
            format="json",
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(VibeRaisingCompany.objects.count(), 1)

        company = VibeRaisingCompany.objects.get()
        self.assertEqual(company.domain, "second.example")

    def test_founder_can_save_monthly_update_for_active_company(self):
        self.client.force_authenticate(user=self.user)
        self._create_founder_company(domain="acme.com", registered=True)

        response = self.client.post(
            "/api/v1/vibe-raising/updates/",
            {
                "month": "March",
                "year": 2026,
                "highlights": "Closed two pilots\nHired first AE",
                "challenges": "Longer sales cycle",
                "asks": "Intros to health system buyers",
                "learnings": "Founder-led demos convert better with clinical operators",
                "next30Days": "Close the hospital pilot and finish onboarding analytics",
                "summary": "Strong month with enterprise momentum.",
                "sourceUrl": "https://example.com/march-update",
                "videoUrl": "https://storage.example.com/vibe-raising/demo.mp4",
                "videoStoragePath": "vibe-raising/update-videos/org-1/user-1/demo.mp4",
                "videoContentType": "video/mp4",
                "videoFileSizeBytes": 12345,
                "videoOriginalFilename": "demo.mp4",
                "metrics": {
                    "revenue": "50000",
                    "activeUsers": "3420",
                    "websiteVisitors": "1200",
                    "ignored": "noop",
                },
                "metricSuggestions": [
                    {"metricKey": "customerInterviews", "label": "Customer Interviews", "reason": "Track discovery."},
                    {"metricKey": "ignoredMetric", "label": "Ignored Metric", "reason": "Not curated."},
                ],
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        draft = MonthlyUpdateDraft.objects.get(organization__domain="acme.com", month=date(2026, 3, 1))
        self.assertEqual(draft.status, MonthlyUpdateDraftStatus.READY)
        self.assertEqual(draft.structured_memo["highlights"], ["Closed two pilots", "Hired first AE"])
        self.assertEqual(draft.structured_memo["lowlights"], ["Longer sales cycle"])
        self.assertEqual(draft.structured_memo["asks"], ["Intros to health system buyers"])
        self.assertEqual(draft.structured_memo["learnings"], ["Founder-led demos convert better with clinical operators"])
        self.assertEqual(draft.structured_memo["next_30_days"], ["Close the hospital pilot and finish onboarding analytics"])
        self.assertEqual(draft.structured_memo["summary"], "Strong month with enterprise momentum.")
        self.assertEqual(draft.structured_memo["source_url"], "https://example.com/march-update")
        self.assertEqual(draft.structured_memo["video_url"], "https://storage.example.com/vibe-raising/demo.mp4")
        self.assertEqual(draft.structured_memo["video"]["content_type"], "video/mp4")
        self.assertEqual(draft.structured_memo["video"]["file_size_bytes"], 12345)
        self.assertEqual(
            draft.structured_memo["metric_suggestions"],
            [{"metric_key": "customerInterviews", "label": "Customer Interviews", "reason": "Track discovery."}],
        )
        self.assertEqual(response.data["update"]["month"], "March 2026")
        self.assertEqual(response.data["update"]["summary"], "Strong month with enterprise momentum.")
        self.assertEqual(response.data["update"]["sourceUrl"], "https://example.com/march-update")
        self.assertEqual(response.data["update"]["videoUrl"], "https://storage.example.com/vibe-raising/demo.mp4")
        self.assertEqual(response.data["update"]["videoContentType"], "video/mp4")
        self.assertEqual(response.data["update"]["videoOriginalFilename"], "demo.mp4")
        self.assertEqual(response.data["update"]["learnings"], "Founder-led demos convert better with clinical operators")
        self.assertEqual(response.data["update"]["next30Days"], "Close the hospital pilot and finish onboarding analytics")
        self.assertEqual(response.data["update"]["metrics"]["revenue"], "50000")
        self.assertEqual(response.data["update"]["metrics"]["websiteVisitors"], "1200")
        self.assertEqual(
            response.data["update"]["metricSuggestions"],
            [{"metricKey": "customerInterviews", "label": "Customer Interviews", "reason": "Track discovery."}],
        )
        self.assertNotIn("ignored", response.data["update"]["metrics"])

    @patch("vibe_raising.views.upload_file_to_storage")
    def test_founder_can_upload_update_video(self, mock_upload):
        self.client.force_authenticate(user=self.user)
        self._create_founder_company(domain="acme.com", registered=True)
        mock_upload.return_value = "https://storage.example.com/vibe-raising/demo.mp4"

        response = self.client.post(
            "/api/v1/vibe-raising/uploads/video/",
            {"video": SimpleUploadedFile("demo.mp4", b"video-bytes", content_type="video/mp4")},
            format="multipart",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["videoUrl"], mock_upload.return_value)
        self.assertEqual(response.data["contentType"], "video/mp4")
        self.assertEqual(response.data["fileSizeBytes"], len(b"video-bytes"))
        self.assertEqual(response.data["originalFilename"], "demo.mp4")
        self.assertTrue(response.data["storagePath"].startswith("vibe-raising/update-videos/org-"))
        mock_upload.assert_called_once()

    @patch("vibe_raising.views.upload_file_to_storage")
    def test_founder_video_upload_accepts_common_formats_and_extension_fallbacks(self, mock_upload):
        self.client.force_authenticate(user=self.user)
        self._create_founder_company(domain="acme.com", registered=True)
        mock_upload.return_value = "https://storage.example.com/vibe-raising/demo-video"

        cases = [
            ("demo.mov", "video/quicktime", "video/quicktime"),
            ("demo.webm", "video/webm", "video/webm"),
            ("demo.mkv", "application/octet-stream", "video/x-matroska"),
            ("demo.avi", "", "video/x-msvideo"),
        ]
        for filename, content_type, expected_content_type in cases:
            with self.subTest(filename=filename):
                response = self.client.post(
                    "/api/v1/vibe-raising/uploads/video/",
                    {
                        "video": SimpleUploadedFile(
                            filename,
                            b"video-bytes",
                            content_type=content_type,
                        )
                    },
                    format="multipart",
                )

                self.assertEqual(response.status_code, 201)
                self.assertEqual(response.data["contentType"], expected_content_type)

        self.assertEqual(mock_upload.call_count, len(cases))

    @patch("vibe_raising.views.create_signed_upload_url")
    def test_founder_can_create_signed_video_upload_session(self, mock_signed_url):
        self.client.force_authenticate(user=self.user)
        self._create_founder_company(domain="acme.com", registered=True)
        mock_signed_url.return_value = "https://storage-upload.example.com/signed-put"

        response = self.client.post(
            "/api/v1/vibe-raising/uploads/video/session/",
            {
                "originalFilename": "demo.mp4",
                "contentType": "video/mp4",
                "fileSizeBytes": 12345,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["uploadUrl"], mock_signed_url.return_value)
        self.assertEqual(response.data["contentType"], "video/mp4")
        self.assertEqual(response.data["fileSizeBytes"], 12345)
        self.assertEqual(response.data["maxUploadBytes"], 250 * 1024 * 1024)
        self.assertEqual(response.data["requiredHeaders"]["Content-Type"], "video/mp4")
        self.assertTrue(response.data["storagePath"].startswith("vibe-raising/update-videos/org-"))
        mock_signed_url.assert_called_once()

    @patch("vibe_raising.views.create_signed_upload_url")
    def test_signed_video_upload_session_accepts_common_formats_and_extension_fallbacks(self, mock_signed_url):
        self.client.force_authenticate(user=self.user)
        self._create_founder_company(domain="acme.com", registered=True)
        mock_signed_url.return_value = "https://storage-upload.example.com/signed-put"

        cases = [
            ("demo.mov", "video/quicktime", "video/quicktime"),
            ("demo.webm", "video/webm", "video/webm"),
            ("demo.mkv", "application/octet-stream", "video/x-matroska"),
            ("demo.avi", "", "video/x-msvideo"),
        ]
        for filename, content_type, expected_content_type in cases:
            with self.subTest(filename=filename):
                response = self.client.post(
                    "/api/v1/vibe-raising/uploads/video/session/",
                    {
                        "originalFilename": filename,
                        "contentType": content_type,
                        "fileSizeBytes": 12345,
                    },
                    format="json",
                )

                self.assertEqual(response.status_code, 201)
                self.assertEqual(response.data["contentType"], expected_content_type)

        self.assertEqual(mock_signed_url.call_count, len(cases))

    def test_signed_video_upload_session_rejects_non_video_content(self):
        self.client.force_authenticate(user=self.user)
        self._create_founder_company(domain="acme.com", registered=True)

        response = self.client.post(
            "/api/v1/vibe-raising/uploads/video/session/",
            {
                "originalFilename": "notes.txt",
                "contentType": "text/plain",
                "fileSizeBytes": 12345,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["detail"], "Uploaded file must be a supported video.")

    def test_signed_video_upload_session_rejects_unsupported_video_format(self):
        self.client.force_authenticate(user=self.user)
        self._create_founder_company(domain="acme.com", registered=True)

        response = self.client.post(
            "/api/v1/vibe-raising/uploads/video/session/",
            {
                "originalFilename": "legacy.flv",
                "contentType": "video/x-flv",
                "fileSizeBytes": 12345,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["detail"], "Uploaded file must be a supported video.")

    @patch("vibe_raising.views.create_signed_upload_url")
    def test_founder_can_create_signed_manual_document_upload_session(self, mock_signed_url):
        self.client.force_authenticate(user=self.user)
        self._create_founder_company(domain="acme.com", registered=True)
        mock_signed_url.return_value = "https://storage-upload.example.com/signed-put"

        response = self.client.post(
            "/api/v1/vibe-raising/uploads/manual-documents/session/",
            {
                "originalFilename": "memo.pdf",
                "contentType": "application/pdf",
                "fileSizeBytes": 12345,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["uploadUrl"], mock_signed_url.return_value)
        self.assertEqual(response.data["contentType"], "application/pdf")
        self.assertEqual(response.data["fileSizeBytes"], 12345)
        self.assertEqual(response.data["maxUploadBytes"], 25 * 1024 * 1024)
        self.assertEqual(response.data["requiredHeaders"]["Content-Type"], "application/pdf")
        self.assertTrue(response.data["storagePath"].startswith("vibe-raising/manual-documents/org-"))
        self.assertIn("/user-", response.data["storagePath"])

    def test_signed_manual_document_upload_session_rejects_unsupported_documents(self):
        self.client.force_authenticate(user=self.user)
        self._create_founder_company(domain="acme.com", registered=True)

        response = self.client.post(
            "/api/v1/vibe-raising/uploads/manual-documents/session/",
            {
                "originalFilename": "logo.png",
                "contentType": "image/png",
                "fileSizeBytes": 12345,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["detail"], "Uploaded file must be a supported document.")

    @patch("vibe_raising.views.download_storage_object_bytes")
    @patch("vibe_raising.views.finalize_private_uploaded_storage_object")
    @patch("vibe_raising.views.create_signed_upload_url")
    def test_founder_can_complete_manual_document_upload(self, mock_signed_url, mock_finalize, mock_download):
        self.client.force_authenticate(user=self.user)
        self._create_founder_company(domain="acme.com", registered=True)
        mock_signed_url.return_value = "https://storage-upload.example.com/signed-put"

        session_response = self.client.post(
            "/api/v1/vibe-raising/uploads/manual-documents/session/",
            {
                "originalFilename": "memo.txt",
                "contentType": "text/plain",
                "fileSizeBytes": 33,
            },
            format="json",
        )
        storage_path = session_response.data["storagePath"]
        mock_finalize.return_value = {"contentType": "text/plain", "fileSizeBytes": 33, "updated": None}
        mock_download.return_value = b"Closed two pilots and reduced churn."

        response = self.client.post(
            "/api/v1/vibe-raising/uploads/manual-documents/complete/",
            {
                "storagePath": storage_path,
                "originalFilename": "memo.txt",
                "contentType": "text/plain",
                "fileSizeBytes": 33,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["document"]["originalFilename"], "memo.txt")
        self.assertEqual(response.data["document"]["extractionStatus"], "processed")
        self.assertNotIn("storagePath", response.data["document"])
        document = StartupManualDocument.objects.get(id=response.data["document"]["id"])
        self.assertEqual(document.extracted_text, "Closed two pilots and reduced churn.")
        self.assertEqual(document.storage_path, storage_path)

    @patch("vibe_raising.views.finalize_private_uploaded_storage_object")
    def test_manual_document_complete_rejects_wrong_storage_prefix(self, mock_finalize):
        self.client.force_authenticate(user=self.user)
        self._create_founder_company(domain="acme.com", registered=True)

        response = self.client.post(
            "/api/v1/vibe-raising/uploads/manual-documents/complete/",
            {
                "storagePath": "vibe-raising/manual-documents/org-999/company-other/user-999/memo.txt",
                "originalFilename": "memo.txt",
                "contentType": "text/plain",
                "fileSizeBytes": 10,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["detail"], "Invalid upload path.")
        mock_finalize.assert_not_called()

    @patch("vibe_raising.views.create_signed_read_url")
    def test_manual_document_list_download_and_delete_are_owner_scoped(self, mock_read_url):
        self.client.force_authenticate(user=self.user)
        _profile, company = self._create_founder_company(domain="acme.com", registered=True)
        document = self._create_manual_document(company=company)
        mock_read_url.return_value = "https://storage.example.com/read"

        list_response = self.client.get("/api/v1/vibe-raising/uploads/manual-documents/")
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(list_response.data["documents"][0]["id"], str(document.id))
        self.assertNotIn("storagePath", list_response.data["documents"][0])

        download_response = self.client.get(f"/api/v1/vibe-raising/uploads/manual-documents/{document.id}/download/")
        self.assertEqual(download_response.status_code, 200)
        self.assertEqual(download_response.data["downloadUrl"], mock_read_url.return_value)

        other_user = User.objects.create_user(email="other@example.com", password="password", role="participant")
        other_profile = VibeRaisingProfile.objects.create(user=other_user, role=VibeRaisingProfile.ROLE_FOUNDER)
        other_company = VibeRaisingCompany.objects.create(
            profile=other_profile,
            name="Other Co",
            domain="acme.com",
            registered=True,
        )
        other_profile.active_company = other_company
        other_profile.save(update_fields=["active_company", "updated_at"])
        self.client.force_authenticate(user=other_user)
        denied_response = self.client.get(f"/api/v1/vibe-raising/uploads/manual-documents/{document.id}/download/")
        self.assertEqual(denied_response.status_code, 404)

        admin_user = User.objects.create_superuser(email="admin@example.com", password="password")
        self.client.force_authenticate(user=admin_user)
        admin_response = self.client.get(f"/api/v1/vibe-raising/uploads/manual-documents/{document.id}/download/")
        self.assertEqual(admin_response.status_code, 200)

        self.client.force_authenticate(user=self.user)
        with patch("vibe_raising.views.delete_storage_object") as mock_delete:
            delete_response = self.client.delete(f"/api/v1/vibe-raising/uploads/manual-documents/{document.id}/")
        self.assertEqual(delete_response.status_code, 204)
        mock_delete.assert_called_once_with(document.storage_path)
        self.assertFalse(StartupManualDocument.objects.filter(id=document.id).exists())

    @patch("vibe_raising.views.notify_valley_run_created")
    def test_email_draft_start_accepts_manual_documents_without_gmail(self, mock_notify):
        self.client.force_authenticate(user=self.user)
        _profile, company = self._create_founder_company(domain="acme.com", registered=True)
        document = self._create_manual_document(company=company)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                "/api/v1/vibe-raising/email-draft/start/",
                {
                    "inputSources": ["manual_documents"],
                    "manualDocumentIds": [str(document.id)],
                    "manualSummary": "Founder-added investor memo.",
                    "targetMonth": "2026-03-01",
                },
                format="json",
            )

        self.assertEqual(response.status_code, 201)
        run = ContentFactoryRun.objects.get(run_id=response.data["runId"])
        self.assertEqual(run.run_request["input_sources"], ["manual_documents"])
        self.assertIsNone(run.run_request["google_connection_id"])
        self.assertEqual(run.run_request["manual_document_ids"], [str(document.id)])
        self.assertEqual(run.run_request["manual_summary"], "Founder-added investor memo.")
        manual_context = run.run_request["external_context"]["manual_documents"]
        self.assertEqual(manual_context["summary"], "Founder-added investor memo.")
        self.assertEqual(manual_context["documents"][0]["filename"], "memo.txt")
        self.assertIn("Pilot conversion improved", manual_context["documents"][0]["text_excerpt"])
        mock_notify.assert_called_once_with(run.run_id)

    @patch("vibe_raising.views.notify_valley_run_created")
    def test_email_draft_start_skips_gmail_when_scope_missing_and_other_sources_selected(self, mock_notify):
        self.client.force_authenticate(user=self.user)
        _profile, company = self._create_founder_company(domain="acme.com", registered=True)
        document = self._create_manual_document(company=company)
        self._create_google_connection(scope="openid https://www.googleapis.com/auth/userinfo.email")
        organization, _startup_profile = resolve_or_create_profile(domain="acme.com")
        ExternalServiceConnection.objects.create(
            user=self.user,
            organization=organization,
            provider=ExternalServiceProvider.XERO,
            account_label="Acme Xero",
            status="connected",
            last_synced_at=timezone.now(),
        )

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                "/api/v1/vibe-raising/email-draft/start/",
                {
                    "inputSources": ["gmail", "xero", "manual_documents"],
                    "manualDocumentIds": [str(document.id)],
                    "targetMonth": "2026-03-01",
                },
                format="json",
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["state"], "queued")
        self.assertTrue(response.data["gmailConnected"])
        self.assertFalse(response.data["hasGmailScope"])
        self.assertTrue(response.data["needsGmailReconnect"])
        run = ContentFactoryRun.objects.get(run_id=response.data["runId"])
        self.assertEqual(run.run_request["input_sources"], ["xero", "manual_documents"])
        self.assertIsNone(run.run_request["google_connection_id"])
        self.assertIn("xero", run.run_request["external_context"])
        self.assertIn("manual_documents", run.run_request["external_context"])
        self.assertEqual(
            run.run_request["external_context"]["gmail"]["warnings"],
            ["Reconnect Gmail to grant read access."],
        )
        mock_notify.assert_called_once_with(run.run_id)

    def test_email_draft_start_requires_reconnect_when_gmail_only_scope_missing(self):
        self.client.force_authenticate(user=self.user)
        self._create_founder_company(domain="acme.com", registered=True)
        self._create_google_connection(scope="openid https://www.googleapis.com/auth/userinfo.email")

        response = self.client.post(
            "/api/v1/vibe-raising/email-draft/start/",
            {"inputSources": ["gmail"], "targetMonth": "2026-03-01"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["state"], "auth_required")
        self.assertTrue(response.data["gmailConnected"])
        self.assertFalse(response.data["hasGmailScope"])
        self.assertTrue(response.data["needsGmailReconnect"])
        self.assertFalse(ContentFactoryRun.objects.filter(workflow="startup_monthly_update").exists())

    @patch("vibe_raising.views.MAX_VIBE_RAISING_VIDEO_SIZE_BYTES", 1024)
    def test_signed_video_upload_session_rejects_oversized_video(self):
        self.client.force_authenticate(user=self.user)
        self._create_founder_company(domain="acme.com", registered=True)

        response = self.client.post(
            "/api/v1/vibe-raising/uploads/video/session/",
            {
                "originalFilename": "oversized.mp4",
                "contentType": "video/mp4",
                "fileSizeBytes": 1025,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("maximum size", response.data["detail"])

    @patch("vibe_raising.views.finalize_uploaded_storage_object")
    def test_founder_can_complete_signed_video_upload(self, mock_finalize):
        self.client.force_authenticate(user=self.user)
        _profile, company = self._create_founder_company(domain="acme.com", registered=True)
        organization, _startup_profile = resolve_or_create_profile(domain=company.domain)
        storage_path = f"vibe-raising/update-videos/org-{organization.id}/user-{self.user.id}/demo.mp4"
        mock_finalize.return_value = {
            "url": "https://storage.example.com/vibe-raising/demo.mp4",
            "contentType": "video/mp4",
            "fileSizeBytes": 12345,
        }

        response = self.client.post(
            "/api/v1/vibe-raising/uploads/video/complete/",
            {
                "storagePath": storage_path,
                "originalFilename": "demo.mp4",
                "contentType": "video/mp4",
                "fileSizeBytes": 12345,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["videoUrl"], mock_finalize.return_value["url"])
        self.assertEqual(response.data["storagePath"], storage_path)
        self.assertEqual(response.data["contentType"], "video/mp4")
        self.assertEqual(response.data["fileSizeBytes"], 12345)
        self.assertEqual(response.data["originalFilename"], "demo.mp4")
        mock_finalize.assert_called_once_with(storage_path, content_type="video/mp4")

    @patch("vibe_raising.views.finalize_uploaded_storage_object")
    def test_complete_signed_video_upload_rejects_wrong_storage_path(self, mock_finalize):
        self.client.force_authenticate(user=self.user)
        self._create_founder_company(domain="acme.com", registered=True)

        response = self.client.post(
            "/api/v1/vibe-raising/uploads/video/complete/",
            {
                "storagePath": "vibe-raising/update-videos/org-999/user-999/demo.mp4",
                "originalFilename": "demo.mp4",
                "contentType": "video/mp4",
                "fileSizeBytes": 12345,
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["detail"], "Invalid upload path.")
        mock_finalize.assert_not_called()

    def test_founder_video_upload_rejects_non_video_content(self):
        self.client.force_authenticate(user=self.user)
        self._create_founder_company(domain="acme.com", registered=True)

        response = self.client.post(
            "/api/v1/vibe-raising/uploads/video/",
            {"video": SimpleUploadedFile("notes.txt", b"plain-text", content_type="text/plain")},
            format="multipart",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["detail"], "Uploaded file must be a supported video.")

    @patch("vibe_raising.views.MAX_VIBE_RAISING_VIDEO_SIZE_BYTES", 1024)
    def test_founder_video_upload_rejects_oversized_video(self):
        self.client.force_authenticate(user=self.user)
        self._create_founder_company(domain="acme.com", registered=True)

        response = self.client.post(
            "/api/v1/vibe-raising/uploads/video/",
            {
                "video": SimpleUploadedFile(
                    "oversized.mp4",
                    b"a" * 1025,
                    content_type="video/mp4",
                )
            },
            format="multipart",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("maximum size", response.data["detail"])

    def test_founder_can_list_monthly_updates_for_active_company(self):
        self.client.force_authenticate(user=self.user)
        self._create_founder_company(domain="acme.com", registered=True)
        organization = Organization.objects.create(name="Acme Inc.", domain="acme.com")
        MonthlyUpdateDraft.objects.create(
            organization=organization,
            month=date(2026, 2, 1),
            status=MonthlyUpdateDraftStatus.READY,
            structured_memo={
                "highlights": ["Closed a channel partnership", "Shipped onboarding refresh"],
                "lowlights": ["Sales cycle slipped"],
                "asks": ["Intros to Series A fintech funds"],
                "learnings": ["Channel partnerships convert faster with founder-led kickoff"],
                "next_30_days": ["Close two fintech pilots"],
                "summary": "February summary",
                "source_url": "https://example.com/feb-update",
                "video_url": "https://storage.example.com/feb.mp4",
                "video": {
                    "url": "https://storage.example.com/feb.mp4",
                    "content_type": "video/mp4",
                    "original_filename": "feb.mp4",
                    "storage_path": "vibe-raising/update-videos/org-1/user-1/feb.mp4",
                    "file_size_bytes": 45678,
                },
                "kpi_snapshot": [
                    {"metric_key": "revenue", "label": "Revenue", "value": "42000"},
                ],
            },
        )

        response = self.client.get("/api/v1/vibe-raising/updates/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["updates"]), 1)
        self.assertEqual(response.data["updates"][0]["month"], "February 2026")
        self.assertEqual(response.data["updates"][0]["summary"], "February summary")
        self.assertEqual(response.data["updates"][0]["sourceUrl"], "https://example.com/feb-update")
        self.assertEqual(response.data["updates"][0]["videoUrl"], "https://storage.example.com/feb.mp4")
        self.assertEqual(response.data["updates"][0]["videoContentType"], "video/mp4")
        self.assertEqual(response.data["updates"][0]["videoOriginalFilename"], "feb.mp4")
        self.assertEqual(
            response.data["updates"][0]["highlights"],
            "Closed a channel partnership\nShipped onboarding refresh",
        )
        self.assertEqual(response.data["updates"][0]["learnings"], "Channel partnerships convert faster with founder-led kickoff")
        self.assertEqual(response.data["updates"][0]["next30Days"], "Close two fintech pilots")
        self.assertEqual(response.data["updates"][0]["metrics"]["revenue"], "42000")

    def test_investor_gets_403_on_company_endpoints(self):
        self.client.force_authenticate(user=self.user)
        VibeRaisingProfile.objects.create(
            user=self.user,
            role=VibeRaisingProfile.ROLE_INVESTOR,
            organization_name="Alpha Ventures",
        )

        company_response = self.client.post(
            "/api/v1/vibe-raising/companies/",
            {"name": "Acme Inc."},
            format="json",
        )
        active_response = self.client.post(
            "/api/v1/vibe-raising/active-company/",
            {"companyId": "33f3e9c7-85b0-458b-a3ee-7bb8b9f0d4f8"},
            format="json",
        )
        updates_response = self.client.get("/api/v1/vibe-raising/updates/")

        self.assertEqual(company_response.status_code, 403)
        self.assertEqual(active_response.status_code, 403)
        self.assertEqual(updates_response.status_code, 403)

    def test_switching_to_unowned_company_returns_404(self):
        self.client.force_authenticate(user=self.user)
        profile = VibeRaisingProfile.objects.create(user=self.user, role=VibeRaisingProfile.ROLE_FOUNDER)
        other_user = User.objects.create_user(email="other@example.com", password="password")
        other_profile = VibeRaisingProfile.objects.create(user=other_user, role=VibeRaisingProfile.ROLE_FOUNDER)
        other_company = VibeRaisingCompany.objects.create(profile=other_profile, name="Other Co")

        response = self.client.post(
            "/api/v1/vibe-raising/active-company/",
            {"companyId": str(other_company.id)},
            format="json",
        )

        self.assertEqual(response.status_code, 404)
        profile.refresh_from_db()
        self.assertIsNone(profile.active_company_id)

    def test_startup_update_bootstrap_returns_oauth_url_and_creates_binding(self):
        self.client.force_authenticate(user=self.user)
        _profile, company = self._create_founder_company()

        response = self.client.post(
            "/api/v1/vibe-raising/startup-update/bootstrap/",
            {},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["company"]["id"], str(company.id))
        self.assertEqual(response.data["company"]["domain"], "acme.com")
        self.assertIn("/integrations/connect/google?next=", response.data["oauthUrl"])
        self.assertIn(
            "http%3A%2F%2Flocalhost%3A5173%2Fvibe-raising%2Fcreate-update%3Femail_draft%3D1",
            response.data["oauthUrl"],
        )
        organization = Organization.objects.get(domain="acme.com")
        self.assertEqual(organization.name, "Acme Inc.")
        binding = UserStartupBinding.objects.get(user=self.user, organization=organization)
        self.assertTrue(binding.is_default_for_gmail)
        self.assertEqual(binding.role, "founder")

    def test_email_draft_routes_are_registered(self):
        self.assertEqual(
            reverse("vibe-raising-email-draft-start"),
            "/api/v1/vibe-raising/email-draft/start/",
        )
        self.assertEqual(
            reverse("vibe-raising-video-upload-session"),
            "/api/v1/vibe-raising/uploads/video/session/",
        )
        self.assertEqual(
            reverse("vibe-raising-video-upload-complete"),
            "/api/v1/vibe-raising/uploads/video/complete/",
        )
        self.assertEqual(
            resolve("/api/v1/vibe-raising/uploads/video/session/").url_name,
            "vibe-raising-video-upload-session",
        )
        self.assertEqual(
            reverse("vibe-raising-email-draft-status"),
            "/api/v1/vibe-raising/email-draft/status/",
        )
        self.assertEqual(
            reverse("vibe-raising-email-draft-latest"),
            "/api/v1/vibe-raising/email-draft/latest/",
        )
        self.assertEqual(
            reverse("vibe-raising-email-draft-active-run"),
            "/api/v1/vibe-raising/email-draft/active-run/",
        )
        self.assertEqual(
            reverse("vibe-raising-email-draft-run-status", args=["run-123"]),
            "/api/v1/vibe-raising/email-draft/runs/run-123/status/",
        )
        self.assertEqual(
            reverse("vibe-raising-email-draft-draft-results-latest"),
            "/api/v1/vibe-raising/email-draft/draft-results/",
        )
        self.assertEqual(
            reverse("vibe-raising-email-draft-draft-results", args=["run-123"]),
            "/api/v1/vibe-raising/email-draft/runs/run-123/draft-results/",
        )

    def test_startup_update_bootstrap_returns_needs_domain_for_missing_domain(self):
        self.client.force_authenticate(user=self.user)
        self._create_founder_company(domain="")

        response = self.client.post(
            "/api/v1/vibe-raising/startup-update/bootstrap/",
            {},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["state"], "needs_domain")

    def test_startup_update_run_returns_needs_google_auth_until_connected(self):
        self.client.force_authenticate(user=self.user)
        self._create_founder_company()

        response = self.client.post(
            "/api/v1/vibe-raising/startup-update/run/",
            {},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["state"], "needs_google_auth")
        self.assertFalse(response.data["googleConnected"])
        self.assertIsNone(response.data["run"])
        self.assertEqual(UserStartupBinding.objects.count(), 1)

    def test_startup_update_run_creates_or_reuses_open_run(self):
        self.client.force_authenticate(user=self.user)
        self._create_founder_company()
        GoogleConnection.objects.create(
            user=self.user,
            google_email="founder@gmail.com",
            refresh_token="refresh-token",
            scope="https://www.googleapis.com/auth/gmail.readonly",
        )

        with patch("vibe_raising.views.notify_valley_run_created") as mock_notify:
            with self.captureOnCommitCallbacks(execute=True):
                first = self.client.post(
                    "/api/v1/vibe-raising/startup-update/run/",
                    {},
                    format="json",
                )
            second = self.client.post(
                "/api/v1/vibe-raising/startup-update/run/",
                {},
                format="json",
            )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.data["state"], "processing")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(ContentFactoryRun.objects.filter(workflow="startup_monthly_update").count(), 1)
        self.assertEqual(first.data["run"]["runId"], second.data["run"]["runId"])
        mock_notify.assert_called_once()

    def test_startup_update_run_returns_retryable_503_when_valley_dispatch_fails(self):
        self.client.force_authenticate(user=self.user)
        self._create_founder_company()
        GoogleConnection.objects.create(
            user=self.user,
            google_email="founder@gmail.com",
            refresh_token="refresh-token",
            scope="https://www.googleapis.com/auth/gmail.readonly",
        )

        with patch("vibe_raising.views.notify_valley_run_created") as mock_notify:
            mock_notify.return_value = ValleyHarnessResult(
                ok=False,
                failure_kind="dns",
                detail="Failed to resolve 'valley-api'",
            )
            response = self.client.post(
                "/api/v1/vibe-raising/startup-update/run/",
                {},
                format="json",
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["error"], "valley_dispatch_failed")
        self.assertEqual(response.data["run"]["runId"], response.data["runId"])
        run = ContentFactoryRun.objects.get(run_id=response.data["runId"])
        self.assertEqual(run.result["_valley_meta"]["dispatch_status"], "failed")

    def test_startup_update_run_reconciles_active_run_when_slack_is_added(self):
        self.client.force_authenticate(user=self.user)
        _organization, _binding, run = self._create_active_gmail_run_with_slack_selection()

        with patch("vibe_raising.views.notify_valley_run_created") as mock_notify:
            response = self.client.post(
                "/api/v1/vibe-raising/startup-update/run/",
                {"inputSources": ["gmail", "xero", "slack"]},
                format="json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["run"]["runId"], run.run_id)
        mock_notify.assert_not_called()

        run.refresh_from_db()
        self.assertEqual(run.run_request["input_sources"], ["gmail", "xero", "slack"])
        self.assertIn("slack_backfill", run.step_order)
        self.assertLess(run.step_order.index("slack_relevance_classification"), run.step_order.index("slack_event_extraction"))
        self.assertLess(run.step_order.index("slack_event_extraction"), run.step_order.index("timeline_merge"))
        self.assertNotIn("xero", run.step_order)
        self.assertEqual(run.current_step, "slack_backfill")
        self.assertEqual(run.run_request["slack_channel_ids"], ["C123"])
        self.assertEqual(run.run_request["external_context"]["slack"]["selected_channel_ids"], ["C123"])
        self.assertIn("xero", run.run_request["external_context"])

    def test_startup_update_status_returns_ready_with_form_shaped_draft(self):
        self.client.force_authenticate(user=self.user)
        _profile, company = self._create_founder_company()
        google_connection = GoogleConnection.objects.create(
            user=self.user,
            google_email="founder@gmail.com",
            refresh_token="refresh-token",
            scope="https://www.googleapis.com/auth/gmail.readonly",
        )
        organization = Organization.objects.create(name="Acme Inc.", domain="acme.com")
        UserStartupBinding.objects.create(
            user=self.user,
            organization=organization,
            google_connection=google_connection,
            role="founder",
            is_default_for_gmail=True,
        )
        MonthlyUpdateDraft.objects.create(
            organization=organization,
            month=date(2026, 3, 1),
            status="ready",
            structured_memo={
                "highlights": ["Closed two new pilots", "Revenue expanded"],
                "lowlights": ["Hiring is still slow", "Sales pipeline slipped"],
                "asks": ["Customer intros", "Hiring referrals"],
                "kpi_snapshot": [
                    {"label": "Revenue", "value": "$45,000"},
                    {"label": "Active Users", "value": "1250"},
                    {"label": "ARR", "value": "$500,000"},
                ],
            },
            rendered_markdown="# March Update",
        )
        MonthlyUpdateDraft.objects.create(
            organization=organization,
            month=date(2026, 2, 1),
            status="ready",
            structured_memo={
                "highlights": ["Launched v2"],
                "lowlights": ["Long onboarding"],
                "asks": ["Hiring referrals"],
                "kpi_snapshot": [{"label": "MRR", "value": "$10,000"}],
            },
            rendered_markdown="# February Update",
        )
        MonthlyUpdateDraft.objects.create(
            organization=organization,
            month=date(2026, 1, 1),
            status="ready",
            structured_memo={
                "highlights": ["Signed first customers"],
                "lowlights": ["Needed bug fixes"],
                "asks": ["Fundraising advice"],
                "kpi_snapshot": [{"label": "Runway", "value": "18 months"}],
            },
            rendered_markdown="# January Update",
        )

        response = self.client.get("/api/v1/vibe-raising/startup-update/status/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["state"], "ready")
        self.assertTrue(response.data["googleConnected"])
        self.assertEqual(response.data["company"]["id"], str(company.id))
        self.assertEqual(response.data["draft"]["month"], "March")
        self.assertEqual(response.data["draft"]["year"], 2026)
        self.assertEqual(response.data["draft"]["metrics"]["revenue"], "$45,000")
        self.assertEqual(response.data["draft"]["metrics"]["activeUsers"], "1250")
        self.assertNotIn("ARR", response.data["draft"]["metrics"])
        self.assertEqual(response.data["draft"]["highlights"], "Closed two new pilots\nRevenue expanded")
        self.assertEqual(response.data["draft"]["challenges"], "Hiring is still slow\nSales pipeline slipped")
        self.assertEqual(response.data["draft"]["asks"], "Customer intros\nHiring referrals")
        self.assertEqual(len(response.data["draft"]["pastMonths"]), 2)
        self.assertEqual(response.data["draft"]["pastMonths"][0]["month"], "February 2026")

    def test_email_draft_start_returns_auth_required_until_gmail_connected(self):
        self.client.force_authenticate(user=self.user)
        self._create_founder_company()

        response = self.client.post(
            "/api/v1/vibe-raising/email-draft/start/",
            {"targetMonth": "2026-03-01"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["state"], "auth_required")
        self.assertFalse(response.data["gmailConnected"])
        self.assertIn("/integrations/connect/google?next=", response.data["authUrl"])
        self.assertEqual(UserStartupBinding.objects.count(), 1)

    def test_email_draft_status_returns_auth_required_until_gmail_connected(self):
        self.client.force_authenticate(user=self.user)
        self._create_founder_company()

        response = self.client.get("/api/v1/vibe-raising/email-draft/status/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["state"], "auth_required")
        self.assertFalse(response.data["gmailConnected"])
        self.assertIn("/integrations/connect/google?next=", response.data["authUrl"])
        self.assertIsNone(response.data["runId"])
        self.assertEqual(response.data["pastMonths"], [])

    def test_email_draft_start_creates_or_reuses_open_run(self):
        self.client.force_authenticate(user=self.user)
        self._create_founder_company()
        google_connection = self._create_google_connection()

        with patch("vibe_raising.views.notify_valley_run_created") as mock_notify:
            with self.captureOnCommitCallbacks(execute=True):
                first = self.client.post(
                    "/api/v1/vibe-raising/email-draft/start/",
                    {},
                    format="json",
                )
            second = self.client.post(
                "/api/v1/vibe-raising/email-draft/start/",
                {},
                format="json",
            )

        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.data["state"], "queued")
        self.assertFalse(first.data["reusedExistingRun"])
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.data["reusedExistingRun"])
        self.assertEqual(ContentFactoryRun.objects.filter(workflow="startup_monthly_update").count(), 1)
        self.assertEqual(first.data["runId"], second.data["runId"])
        mock_notify.assert_called_once()

        run = ContentFactoryRun.objects.get(run_id=first.data["runId"])
        self.assertEqual(run.run_request["window_months"], DEFAULT_BACKFILL_MONTHS)
        self.assertEqual(run.run_request["google_connection_id"], google_connection.id)

    @patch("startup_updates.services.timezone.now")
    def test_email_draft_start_creates_single_target_month_run_with_partial_current_window(self, mock_now):
        mock_now.return_value = datetime(2026, 4, 26, 5, 30, tzinfo=dt_timezone.utc)
        self.client.force_authenticate(user=self.user)
        self._create_founder_company()
        self._create_google_connection()

        with patch("vibe_raising.views.notify_valley_run_created") as mock_notify:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    "/api/v1/vibe-raising/email-draft/start/",
                    {"targetMonth": "2026-04-01"},
                    format="json",
                )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["targetMonth"], "2026-04-01")
        run = ContentFactoryRun.objects.get(run_id=response.data["runId"])
        self.assertEqual(run.run_request["target_month"], "2026-04-01")
        self.assertEqual(run.run_request["current_month"], "2026-04-01")
        self.assertEqual(run.run_request["draft_months"], ["2026-04-01"])
        self.assertEqual(run.run_request["backfill_window_start"], "2026-04-01T00:00:00+00:00")
        self.assertEqual(run.run_request["backfill_window_end"], "2026-04-26T05:30:00+00:00")
        mock_notify.assert_called_once_with(run.run_id)

    @patch("startup_updates.services.timezone.now")
    def test_email_draft_start_rejects_future_target_month(self, mock_now):
        mock_now.return_value = datetime(2026, 4, 26, 5, 30, tzinfo=dt_timezone.utc)
        self.client.force_authenticate(user=self.user)
        self._create_founder_company()
        self._create_google_connection()

        response = self.client.post(
            "/api/v1/vibe-raising/email-draft/start/",
            {"targetMonth": "2026-05-01"},
            format="json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("future", response.data["error"])

    @patch("startup_updates.services.timezone.now")
    def test_email_draft_start_newer_draft_does_not_block_older_selected_month(self, mock_now):
        mock_now.return_value = datetime(2026, 4, 26, 5, 30, tzinfo=dt_timezone.utc)
        self.client.force_authenticate(user=self.user)
        self._create_founder_company()
        google_connection = self._create_google_connection()
        organization = Organization.objects.create(name="Acme Inc.", domain="acme.com")
        UserStartupBinding.objects.create(
            user=self.user,
            organization=organization,
            google_connection=google_connection,
            role="founder",
            is_default_for_gmail=True,
        )
        MonthlyUpdateDraft.objects.create(
            organization=organization,
            month=date(2026, 4, 1),
            status=MonthlyUpdateDraftStatus.READY,
            structured_memo={"highlights": ["April already exists"]},
        )

        with patch("vibe_raising.views.notify_valley_run_created") as mock_notify:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    "/api/v1/vibe-raising/email-draft/start/",
                    {"targetMonth": "2026-03-01"},
                    format="json",
                )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["targetMonth"], "2026-03-01")
        self.assertEqual(ContentFactoryRun.objects.filter(workflow="startup_monthly_update").count(), 1)
        run = ContentFactoryRun.objects.get(run_id=response.data["runId"])
        self.assertEqual(run.run_request["target_month"], "2026-03-01")
        self.assertEqual(run.run_request["draft_months"], ["2026-03-01"])
        mock_notify.assert_called_once_with(run.run_id)

    @patch("startup_updates.services.timezone.now")
    def test_email_draft_start_returns_conflict_for_open_run_in_different_month(self, mock_now):
        mock_now.return_value = datetime(2026, 4, 26, 5, 30, tzinfo=dt_timezone.utc)
        self.client.force_authenticate(user=self.user)
        self._create_founder_company()
        google_connection = self._create_google_connection()
        organization = Organization.objects.create(name="Acme Inc.", domain="acme.com")
        binding = UserStartupBinding.objects.create(
            user=self.user,
            organization=organization,
            google_connection=google_connection,
            role="founder",
            is_default_for_gmail=True,
        )
        active_run = create_startup_update_run(
            organization=organization,
            binding=binding,
            target_month=date(2026, 4, 1),
        )

        with patch("vibe_raising.views.notify_valley_run_created") as mock_notify:
            response = self.client.post(
                "/api/v1/vibe-raising/email-draft/start/",
                {"targetMonth": "2026-03-01"},
                format="json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["targetMonthConflict"])
        self.assertEqual(response.data["requestedTargetMonth"], "2026-03-01")
        self.assertEqual(response.data["activeTargetMonth"], "2026-04-01")
        self.assertEqual(response.data["runId"], active_run.run_id)
        self.assertEqual(ContentFactoryRun.objects.filter(workflow="startup_monthly_update").count(), 1)
        mock_notify.assert_not_called()

    def test_email_draft_start_returns_retryable_503_when_valley_dispatch_fails(self):
        self.client.force_authenticate(user=self.user)
        self._create_founder_company()
        self._create_google_connection()

        with patch("vibe_raising.views.notify_valley_run_created") as mock_notify:
            mock_notify.return_value = ValleyHarnessResult(
                ok=False,
                failure_kind="dns",
                detail="Failed to resolve 'valley-api'",
            )
            response = self.client.post(
                "/api/v1/vibe-raising/email-draft/start/",
                {},
                format="json",
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["error"], "valley_dispatch_failed")
        self.assertTrue(response.data["retryable"])
        run = ContentFactoryRun.objects.get(run_id=response.data["runId"])
        valley_meta = run.result["_valley_meta"]
        self.assertEqual(valley_meta["dispatch_status"], "failed")
        self.assertEqual(valley_meta["last_dispatch_error_kind"], "dns")
        self.assertIn("Failed to resolve", valley_meta["last_dispatch_error"])

        with patch("vibe_raising.views.notify_valley_run_created") as mock_retry_notify:
            mock_retry_notify.return_value = ValleyHarnessResult(
                ok=True,
                payload={"job_id": "job-123", "status": "queued"},
            )
            retry = self.client.post(
                "/api/v1/vibe-raising/email-draft/start/",
                {},
                format="json",
            )

        self.assertEqual(retry.status_code, 200)
        self.assertTrue(retry.data["reusedExistingRun"])
        self.assertEqual(retry.data["runId"], run.run_id)
        run.refresh_from_db()
        self.assertEqual(run.result["_valley_meta"]["dispatch_status"], "queued")
        mock_retry_notify.assert_called_once_with(run.run_id)

    def test_email_draft_start_regenerates_when_selected_sources_exceed_reusable_drafts(self):
        self.client.force_authenticate(user=self.user)
        self._create_founder_company()
        self._create_google_connection()

        with patch("vibe_raising.views.notify_valley_run_created"):
            with self.captureOnCommitCallbacks(execute=True):
                first = self.client.post(
                    "/api/v1/vibe-raising/email-draft/start/",
                    {},
                    format="json",
                )

        self.assertEqual(first.status_code, 201)
        original_run = ContentFactoryRun.objects.get(run_id=first.data["runId"])
        organization = Organization.objects.get(id=original_run.run_request["organization_id"])
        original_run.status = ContentFactoryRunStatus.COMPLETED
        original_run.save(update_fields=["status", "updated_at"])
        MonthlyUpdateDraft.objects.create(
            organization=organization,
            run=original_run,
            month=date(2026, 3, 1),
            status=MonthlyUpdateDraftStatus.READY,
            structured_memo={"summary": "Existing Gmail-only draft"},
        )

        with patch("vibe_raising.views.notify_valley_run_created") as mock_notify:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    "/api/v1/vibe-raising/email-draft/start/",
                    {"inputSources": ["gmail", "xero"]},
                    format="json",
                )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["state"], "queued")
        self.assertFalse(response.data["reusedExistingRun"])
        self.assertEqual(ContentFactoryRun.objects.filter(workflow="startup_monthly_update").count(), 2)
        regenerated_run = ContentFactoryRun.objects.get(run_id=response.data["runId"])
        self.assertEqual(regenerated_run.run_request["input_sources"], ["gmail", "xero"])
        self.assertNotEqual(regenerated_run.run_id, original_run.run_id)
        mock_notify.assert_called_once_with(regenerated_run.run_id)

    def test_email_draft_start_reconciles_active_run_when_slack_is_added(self):
        self.client.force_authenticate(user=self.user)
        _organization, _binding, run = self._create_active_gmail_run_with_slack_selection()

        with patch("vibe_raising.views.notify_valley_run_created") as mock_notify:
            response = self.client.post(
                "/api/v1/vibe-raising/email-draft/start/",
                {"inputSources": ["gmail", "xero", "slack"]},
                format="json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["reusedExistingRun"])
        self.assertEqual(response.data["runId"], run.run_id)
        mock_notify.assert_not_called()

        run.refresh_from_db()
        expected_step_order = [
            "profile_resolution",
            "gmail_backfill",
            "relevance_classification",
            "thread_hydration",
            "event_extraction",
            "slack_backfill",
            "slack_relevance_classification",
            "slack_event_extraction",
            "timeline_merge",
            "draft_generation",
            "groundedness_review",
        ]
        self.assertEqual(run.run_request["input_sources"], ["gmail", "xero", "slack"])
        self.assertEqual(run.step_order, expected_step_order)
        self.assertNotIn("xero", run.step_order)
        self.assertEqual(run.current_step, "slack_backfill")
        self.assertEqual(run.run_request["slack_channel_ids"], ["C123"])
        self.assertEqual(run.run_request["external_context"]["slack"]["selected_channel_ids"], ["C123"])
        self.assertIn("xero", run.run_request["external_context"])

        steps_by_key = {step.step_key: step for step in run.steps.all()}
        self.assertEqual(steps_by_key["slack_backfill"].status, ContentFactoryStepStatus.PENDING)
        self.assertEqual(steps_by_key["slack_relevance_classification"].status, ContentFactoryStepStatus.PENDING)
        self.assertEqual(steps_by_key["slack_event_extraction"].status, ContentFactoryStepStatus.PENDING)
        for step_key in ["timeline_merge", "draft_generation", "groundedness_review"]:
            self.assertEqual(steps_by_key[step_key].status, ContentFactoryStepStatus.PENDING)
            self.assertEqual(steps_by_key[step_key].attempts, 0)

    def test_email_draft_start_redispatches_stale_queued_run(self):
        self.client.force_authenticate(user=self.user)
        self._create_founder_company()
        google_connection = GoogleConnection.objects.create(
            user=self.user,
            google_email="founder@gmail.com",
            refresh_token="refresh-token",
            scope="https://www.googleapis.com/auth/gmail.readonly",
        )
        organization = Organization.objects.create(name="Acme Inc.", domain="acme.com")
        binding = UserStartupBinding.objects.create(
            user=self.user,
            organization=organization,
            google_connection=google_connection,
            role="founder",
            is_default_for_gmail=True,
        )
        run = create_startup_update_run(
            organization=organization,
            binding=binding,
        )
        ContentFactoryRun.objects.filter(pk=run.pk).update(
            updated_at=timezone.now() - timedelta(minutes=5),
        )

        with patch("vibe_raising.views.notify_valley_run_created") as mock_notify:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    "/api/v1/vibe-raising/email-draft/start/",
                    {},
                    format="json",
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["state"], "queued")
        self.assertEqual(response.data["runId"], run.run_id)
        self.assertEqual(ContentFactoryRun.objects.filter(workflow="startup_monthly_update").count(), 1)
        mock_notify.assert_called_once_with(run.run_id)

    def test_email_draft_start_supersedes_other_connection_open_run(self):
        self.client.force_authenticate(user=self.user)
        self._create_founder_company()
        google_connection = self._create_google_connection()
        organization = Organization.objects.create(name="Acme Inc.", domain="acme.com")
        UserStartupBinding.objects.create(
            user=self.user,
            organization=organization,
            google_connection=google_connection,
            role="founder",
            is_default_for_gmail=True,
        )

        other_user = User.objects.create_user(
            email="other-founder@example.com",
            password="password",
            first_name="Other",
            last_name="Founder",
            role="participant",
        )
        other_connection = self._create_google_connection(
            user=other_user,
            email="other-founder@gmail.com",
            refresh_token="other-refresh-token",
        )
        other_binding = UserStartupBinding.objects.create(
            user=other_user,
            organization=organization,
            google_connection=other_connection,
            role="founder",
            is_default_for_gmail=True,
        )
        other_run = create_startup_update_run(
            organization=organization,
            binding=other_binding,
        )

        with patch("vibe_raising.views.notify_valley_run_created") as mock_notify:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    "/api/v1/vibe-raising/email-draft/start/",
                    {},
                    format="json",
                )

        self.assertEqual(response.status_code, 201)
        new_run = ContentFactoryRun.objects.get(run_id=response.data["runId"])
        other_run.refresh_from_db()
        self.assertNotEqual(new_run.run_id, other_run.run_id)
        self.assertEqual(new_run.run_request["google_connection_id"], google_connection.id)
        self.assertEqual(other_run.status, ContentFactoryRunStatus.FAILED)
        self.assertEqual(other_run.error, SUPERSEDED_GMAIL_CONNECTION_ERROR)
        mock_notify.assert_called_once_with(new_run.run_id)

    def test_email_draft_status_reports_queued_then_running_for_open_run(self):
        self.client.force_authenticate(user=self.user)
        self._create_founder_company()
        google_connection = GoogleConnection.objects.create(
            user=self.user,
            google_email="founder@gmail.com",
            refresh_token="refresh-token",
            scope="https://www.googleapis.com/auth/gmail.readonly",
        )
        organization = Organization.objects.create(name="Acme Inc.", domain="acme.com")
        binding = UserStartupBinding.objects.create(
            user=self.user,
            organization=organization,
            google_connection=google_connection,
            role="founder",
            is_default_for_gmail=True,
        )
        run = create_startup_update_run(
            organization=organization,
            binding=binding,
        )

        queued_response = self.client.get("/api/v1/vibe-raising/email-draft/status/")

        self.assertEqual(queued_response.status_code, 200)
        self.assertEqual(queued_response.data["state"], "queued")
        self.assertEqual(queued_response.data["runId"], run.run_id)
        self.assertEqual(queued_response.data["status"], ContentFactoryRunStatus.QUEUED)
        self.assertEqual(
            list(queued_response.data["stepStates"].keys()),
            run.step_order,
        )

        run.status = ContentFactoryRunStatus.RUNNING
        run.current_step = "event_extraction"
        run.save(update_fields=["status", "current_step", "updated_at"])

        running_response = self.client.get("/api/v1/vibe-raising/email-draft/status/")

        self.assertEqual(running_response.status_code, 200)
        self.assertEqual(running_response.data["state"], "running")
        self.assertEqual(running_response.data["runId"], run.run_id)
        self.assertEqual(running_response.data["status"], ContentFactoryRunStatus.RUNNING)
        self.assertEqual(running_response.data["currentStep"], "event_extraction")

    def test_email_draft_status_ignores_open_run_for_other_connection(self):
        self.client.force_authenticate(user=self.user)
        self._create_founder_company()
        google_connection = self._create_google_connection()
        organization = Organization.objects.create(name="Acme Inc.", domain="acme.com")
        UserStartupBinding.objects.create(
            user=self.user,
            organization=organization,
            google_connection=google_connection,
            role="founder",
            is_default_for_gmail=True,
        )

        other_user = User.objects.create_user(
            email="other-founder-status@example.com",
            password="password",
            first_name="Other",
            last_name="Founder",
            role="participant",
        )
        other_connection = self._create_google_connection(
            user=other_user,
            email="other-founder-status@gmail.com",
            refresh_token="other-refresh-token-status",
        )
        other_binding = UserStartupBinding.objects.create(
            user=other_user,
            organization=organization,
            google_connection=other_connection,
            role="founder",
            is_default_for_gmail=True,
        )
        other_run = create_startup_update_run(
            organization=organization,
            binding=other_binding,
        )

        response = self.client.get("/api/v1/vibe-raising/email-draft/status/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["state"], "failed")
        self.assertIsNone(response.data["runId"])
        self.assertNotEqual(response.data["runId"], other_run.run_id)

    def test_email_draft_status_returns_completed_payload_with_editor_shape(self):
        self.client.force_authenticate(user=self.user)
        _profile, company = self._create_founder_company()
        google_connection = GoogleConnection.objects.create(
            user=self.user,
            google_email="founder@gmail.com",
            refresh_token="refresh-token",
            scope="https://www.googleapis.com/auth/gmail.readonly",
        )
        organization = Organization.objects.create(name="Acme Inc.", domain="acme.com")
        UserStartupBinding.objects.create(
            user=self.user,
            organization=organization,
            google_connection=google_connection,
            role="founder",
            is_default_for_gmail=True,
        )
        MonthlyUpdateDraft.objects.create(
            organization=organization,
            month=date(2026, 3, 1),
            status="ready",
            structured_memo={
                "highlights": ["Closed two new pilots", "Revenue expanded"],
                "lowlights": ["Hiring is still slow", "Sales pipeline slipped"],
                "asks": ["Customer intros", "Hiring referrals"],
                "kpi_snapshot": [
                    {"metric_key": "revenue", "label": "Revenue", "value": "$45,000"},
                    {"metric_key": "activeUsers", "label": "Active Users", "value": "1250"},
                ],
            },
            rendered_markdown="# March Update",
        )
        MonthlyUpdateDraft.objects.create(
            organization=organization,
            month=date(2026, 2, 1),
            status="ready",
            structured_memo={
                "highlights": ["Launched v2"],
                "lowlights": ["Long onboarding"],
                "asks": ["Hiring referrals"],
                "kpi_snapshot": [{"metric_key": "mrr", "label": "MRR", "value": "$10,000"}],
            },
            rendered_markdown="# February Update",
        )
        MonthlyUpdateDraft.objects.create(
            organization=organization,
            month=date(2026, 1, 1),
            status="ready",
            structured_memo={
                "highlights": ["Signed first customers"],
                "lowlights": ["Needed bug fixes"],
                "asks": ["Fundraising advice"],
                "kpi_snapshot": [{"metric_key": "runway", "label": "Runway", "value": "18 months"}],
            },
            rendered_markdown="# January Update",
        )

        response = self.client.get("/api/v1/vibe-raising/email-draft/status/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["state"], "completed")
        self.assertTrue(response.data["gmailConnected"])
        self.assertEqual(response.data["company"]["id"], str(company.id))
        self.assertEqual(response.data["currentMonth"]["month"], "March")
        self.assertEqual(response.data["currentMonth"]["year"], 2026)
        self.assertEqual(response.data["currentMonth"]["metrics"]["revenue"], "$45,000")
        self.assertEqual(response.data["currentMonth"]["metrics"]["activeUsers"], "1250")
        self.assertEqual(response.data["currentMonth"]["highlights"], "Closed two new pilots\nRevenue expanded")
        self.assertEqual(response.data["currentMonth"]["challenges"], "Hiring is still slow\nSales pipeline slipped")
        self.assertEqual(response.data["currentMonth"]["asks"], "Customer intros\nHiring referrals")
        self.assertEqual([item["month"] for item in response.data["pastMonths"]], ["January", "February"])
        self.assertEqual(response.data["draft"]["month"], "March")
        self.assertEqual(response.data["draft"]["highlights"], "Closed two new pilots\nRevenue expanded")
        self.assertEqual(response.data["draft"]["pastMonths"][0]["month"], "February 2026")

    def test_email_draft_active_run_returns_null_without_open_run(self):
        self.client.force_authenticate(user=self.user)
        self._create_founder_company()
        self._create_google_connection()

        response = self.client.get("/api/v1/vibe-raising/email-draft/active-run/")

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data)

    def test_email_draft_active_run_returns_progress_payload(self):
        self.client.force_authenticate(user=self.user)
        self._create_founder_company()
        google_connection = self._create_google_connection()
        organization = Organization.objects.create(name="Acme Inc.", domain="acme.com")
        binding = UserStartupBinding.objects.create(
            user=self.user,
            organization=organization,
            google_connection=google_connection,
            role="founder",
            is_default_for_gmail=True,
        )
        run = create_startup_update_run(
            organization=organization,
            binding=binding,
        )
        run.status = ContentFactoryRunStatus.RUNNING
        run.current_step = "gmail_backfill"
        run.result = {
            "_valley_meta": {"last_heartbeat_at": "2026-03-30T04:05:06+00:00"},
        }
        run.save(update_fields=["status", "current_step", "result", "updated_at"])

        response = self.client.get("/api/v1/vibe-raising/email-draft/active-run/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["state"], "running")
        self.assertEqual(response.data["runId"], run.run_id)
        self.assertEqual(response.data["displayStage"], "Scanning recent Gmail messages")
        self.assertEqual(response.data["lastHeartbeatAt"], "2026-03-30T04:05:06+00:00")
        self.assertEqual(response.data["binding"]["id"], binding.id)
        self.assertEqual(response.data["binding"]["googleConnectionId"], google_connection.id)

    @patch("vibe_raising.views.cancel_valley_run")
    def test_email_draft_cancel_marks_run_cancelled_and_clears_active_run(self, mock_cancel_valley_run):
        mock_cancel_valley_run.return_value = {
            "run_id": "ignored",
            "revoke_requested": True,
            "revoke_succeeded": True,
            "revoked_job_ids": ["job-1"],
            "missing_job_ids": [],
        }
        self.client.force_authenticate(user=self.user)
        self._create_founder_company()
        google_connection = self._create_google_connection()
        organization = Organization.objects.create(name="Acme Inc.", domain="acme.com")
        binding = UserStartupBinding.objects.create(
            user=self.user,
            organization=organization,
            google_connection=google_connection,
            role="founder",
            is_default_for_gmail=True,
        )
        run = create_startup_update_run(
            organization=organization,
            binding=binding,
        )
        run.status = ContentFactoryRunStatus.RUNNING
        run.current_step = "event_extraction"
        run.save(update_fields=["status", "current_step", "updated_at"])
        MonthlyUpdateDraft.objects.create(
            organization=organization,
            run=run,
            month=date(2026, 3, 1),
            status="draft",
            structured_memo={"highlights": ["Pending March update"]},
            rendered_markdown="",
        )
        StartupEvent.objects.create(
            organization=organization,
            run=run,
            canonical_key="march_customer_win",
            event_type="customer_win",
            title="Won a March customer",
            month_bucket=date(2026, 3, 1),
        )
        StartupMetricObservation.objects.create(
            organization=organization,
            run=run,
            metric_key="revenue",
            metric_name="Revenue",
            value_text="$45,000",
            period_month=date(2026, 3, 1),
        )

        response = self.client.post(
            f"/api/v1/vibe-raising/email-draft/runs/{run.run_id}/cancel/",
            {},
            format="json",
        )
        active_run_response = self.client.get("/api/v1/vibe-raising/email-draft/active-run/")
        status_response = self.client.get(
            f"/api/v1/vibe-raising/email-draft/runs/{run.run_id}/status/",
        )

        run.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], ContentFactoryRunStatus.CANCELLED)
        self.assertTrue(response.data["cancel_applied"])
        self.assertEqual(response.data["cleanup"]["drafts_deleted"], 1)
        self.assertEqual(response.data["cleanup"]["events_deleted"], 1)
        self.assertEqual(response.data["cleanup"]["metrics_deleted"], 1)
        self.assertTrue(response.data["revoke_requested"])
        self.assertTrue(response.data["revoke_succeeded"])
        self.assertEqual(run.status, ContentFactoryRunStatus.CANCELLED)
        self.assertFalse(MonthlyUpdateDraft.objects.filter(run=run).exists())
        self.assertFalse(StartupEvent.objects.filter(run=run).exists())
        self.assertFalse(StartupMetricObservation.objects.filter(run=run).exists())

        self.assertEqual(active_run_response.status_code, 200)
        self.assertIsNone(active_run_response.data)
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.data["state"], "cancelled")
        mock_cancel_valley_run.assert_called_once_with(run.run_id)

    def test_email_draft_run_status_and_results_are_scoped_to_requested_run(self):
        self.client.force_authenticate(user=self.user)
        self._create_founder_company()
        google_connection = self._create_google_connection()
        organization = Organization.objects.create(name="Acme Inc.", domain="acme.com")
        binding = UserStartupBinding.objects.create(
            user=self.user,
            organization=organization,
            google_connection=google_connection,
            role="founder",
            is_default_for_gmail=True,
        )
        older_run = create_startup_update_run(
            organization=organization,
            binding=binding,
        )
        older_run.status = ContentFactoryRunStatus.COMPLETED
        older_run.current_step = "groundedness_review"
        older_run.result = {
            "generated_draft_months": ["2026-03-01"],
            "_valley_meta": {"last_heartbeat_at": "2026-03-30T02:00:00+00:00"},
        }
        older_run.save(update_fields=["status", "current_step", "result", "updated_at"])
        MonthlyUpdateDraft.objects.create(
            organization=organization,
            run=older_run,
            month=date(2026, 3, 1),
            status="ready",
            structured_memo={
                "highlights": ["March highlight", "March second highlight"],
                "lowlights": ["March challenge", "March second challenge"],
                "asks": ["March ask", "March second ask"],
                "learnings": ["March learning"],
                "next_30_days": ["March next step"],
                "kpi_snapshot": [{"metric_key": "revenue", "label": "Revenue", "value": "$45,000"}],
            },
            rendered_markdown="# March Update",
        )

        newer_run = create_startup_update_run(
            organization=organization,
            binding=binding,
        )
        newer_run.status = ContentFactoryRunStatus.COMPLETED
        newer_run.current_step = "groundedness_review"
        newer_run.result = {
            "generated_draft_months": ["2026-04-01"],
        }
        newer_run.save(update_fields=["status", "current_step", "result", "updated_at"])
        MonthlyUpdateDraft.objects.create(
            organization=organization,
            run=newer_run,
            month=date(2026, 4, 1),
            status="ready",
            structured_memo={
                "highlights": ["April highlight"],
                "lowlights": ["April challenge"],
                "asks": ["April ask"],
                "kpi_snapshot": [{"metric_key": "revenue", "label": "Revenue", "value": "$52,000"}],
            },
            rendered_markdown="# April Update",
        )

        status_response = self.client.get(
            f"/api/v1/vibe-raising/email-draft/runs/{older_run.run_id}/status/",
        )
        results_response = self.client.get(
            f"/api/v1/vibe-raising/email-draft/runs/{older_run.run_id}/draft-results/",
        )

        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.data["state"], "completed")
        self.assertEqual(status_response.data["runId"], older_run.run_id)
        self.assertEqual(status_response.data["generatedDraftMonths"], ["2026-03-01"])
        self.assertEqual(status_response.data["currentMonth"]["month"], "March")
        self.assertEqual(status_response.data["currentMonth"]["metrics"]["revenue"], "$45,000")
        self.assertEqual(status_response.data["currentMonth"]["highlights"], "March highlight\nMarch second highlight")
        self.assertEqual(status_response.data["currentMonth"]["challenges"], "March challenge\nMarch second challenge")
        self.assertEqual(status_response.data["currentMonth"]["asks"], "March ask\nMarch second ask")
        self.assertEqual(status_response.data["currentMonth"]["learnings"], "March learning")
        self.assertEqual(status_response.data["currentMonth"]["next30Days"], "March next step")

        self.assertEqual(results_response.status_code, 200)
        self.assertEqual(results_response.data["runId"], older_run.run_id)
        self.assertEqual(results_response.data["currentMonth"]["month"], "March")
        self.assertEqual(results_response.data["currentMonth"]["highlights"], "March highlight\nMarch second highlight")
        self.assertEqual(results_response.data["draft"]["month"], "March")
        self.assertEqual(results_response.data["draft"]["metrics"]["revenue"], "$45,000")
        self.assertEqual(results_response.data["draft"]["highlights"], "March highlight\nMarch second highlight")
        self.assertEqual(results_response.data["draft"]["challenges"], "March challenge\nMarch second challenge")
        self.assertEqual(results_response.data["draft"]["asks"], "March ask\nMarch second ask")
        self.assertEqual(results_response.data["draft"]["learnings"], "March learning")
        self.assertEqual(results_response.data["draft"]["next30Days"], "March next step")
        self.assertNotIn("March learning", results_response.data["draft"]["highlights"])
        self.assertNotIn("March next step", results_response.data["draft"]["asks"])
        self.assertEqual(results_response.data["months"][0]["month"], "March")
        self.assertEqual(results_response.data["months"][0]["highlights"], "March highlight\nMarch second highlight")

    def test_email_draft_results_hydrate_revenue_from_xero_observations(self):
        self.client.force_authenticate(user=self.user)
        self._create_founder_company()
        google_connection = self._create_google_connection()
        organization = Organization.objects.create(name="Acme Inc.", domain="acme.com")
        binding = UserStartupBinding.objects.create(
            user=self.user,
            organization=organization,
            google_connection=google_connection,
            role="founder",
            is_default_for_gmail=True,
        )
        run = create_startup_update_run(
            organization=organization,
            binding=binding,
        )
        run.status = ContentFactoryRunStatus.COMPLETED
        run.current_step = "groundedness_review"
        run.result = {"generated_draft_months": ["2026-03-01", "2026-04-01"]}
        run.save(update_fields=["status", "current_step", "result", "updated_at"])
        MonthlyUpdateDraft.objects.create(
            organization=organization,
            run=run,
            month=date(2026, 4, 1),
            status="ready",
            structured_memo={
                "highlights": ["April highlight"],
                "kpi_snapshot": [{"metric_key": "activeUsers", "label": "Active Users", "value": "25"}],
            },
            rendered_markdown="# April Update",
        )
        MonthlyUpdateDraft.objects.create(
            organization=organization,
            run=run,
            month=date(2026, 3, 1),
            status="ready",
            structured_memo={
                "highlights": ["March highlight"],
                "kpi_snapshot": [{"metric_key": "activeUsers", "label": "Active Users", "value": "20"}],
            },
            rendered_markdown="# March Update",
        )
        StartupMetricObservation.objects.create(
            organization=organization,
            run=run,
            source_provider=ExternalServiceProvider.XERO,
            metric_key="revenue",
            metric_name="Revenue",
            value_text="AUD 3800.00",
            value_number=Decimal("3800.00"),
            unit="AUD",
            period_month=date(2026, 4, 1),
            confidence=1.0,
            source_metadata={"report_name": "ProfitAndLoss"},
        )
        StartupMetricObservation.objects.create(
            organization=organization,
            run=run,
            source_provider=ExternalServiceProvider.XERO,
            metric_key="revenue",
            metric_name="Revenue",
            value_text="AUD 2735.75",
            value_number=Decimal("2735.75"),
            unit="AUD",
            period_month=date(2026, 3, 1),
            confidence=1.0,
            source_metadata={"report_name": "ProfitAndLoss"},
        )

        response = self.client.get(
            f"/api/v1/vibe-raising/email-draft/runs/{run.run_id}/draft-results/",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["draft"]["metrics"]["revenue"], "AUD 3800.00")
        self.assertEqual(response.data["draft"]["metrics"]["activeUsers"], "25")
        self.assertEqual(response.data["draft"]["pastMonths"][0]["metrics"]["revenue"], "AUD 2735.75")
        self.assertEqual(response.data["currentMonth"]["metrics"]["revenue"], "AUD 3800.00")
        self.assertEqual(response.data["pastMonths"][0]["metrics"]["revenue"], "AUD 2735.75")
        stored_draft = MonthlyUpdateDraft.objects.get(organization=organization, month=date(2026, 4, 1))
        self.assertNotIn(
            "revenue",
            [item.get("metric_key") for item in stored_draft.structured_memo["kpi_snapshot"]],
        )

    def test_upsert_monthly_update_draft_replace_overwrites_prior_run_draft(self):
        self.client.force_authenticate(user=self.user)
        self._create_founder_company()
        google_connection = self._create_google_connection()
        organization = Organization.objects.create(name="Acme Inc.", domain="acme.com")
        binding = UserStartupBinding.objects.create(
            user=self.user,
            organization=organization,
            google_connection=google_connection,
            role="founder",
            is_default_for_gmail=True,
        )
        prior_run = create_startup_update_run(organization=organization, binding=binding)
        upsert_monthly_update_draft(
            organization=organization,
            month=date(2026, 5, 1),
            run=prior_run,
            structured_memo={
                "highlights": ["Stale alpha", "Shared gamma"],
                "asks": ["Old ask"],
            },
            model_name="prior-model",
        )
        prior_run.status = ContentFactoryRunStatus.COMPLETED
        prior_run.save(update_fields=["status", "updated_at"])

        fresh_run = create_startup_update_run(organization=organization, binding=binding)
        self.assertNotEqual(fresh_run.run_id, prior_run.run_id)

        # A forced regenerate (replace=True) from a *different* run overwrites the
        # stale draft outright instead of merging, so old dot points do not linger.
        replaced = upsert_monthly_update_draft(
            organization=organization,
            month=date(2026, 5, 1),
            run=fresh_run,
            structured_memo={"highlights": ["Fresh delta"]},
            model_name="fresh-model",
            replace=True,
        )

        self.assertEqual(replaced.run_id, fresh_run.pk)
        self.assertEqual(replaced.structured_memo.get("highlights"), ["Fresh delta"])
        self.assertNotIn("asks", replaced.structured_memo)
        self.assertNotIn("Stale alpha", str(replaced.structured_memo))

    def test_upsert_monthly_update_draft_without_replace_merges_prior_run_draft(self):
        self.client.force_authenticate(user=self.user)
        self._create_founder_company()
        google_connection = self._create_google_connection()
        organization = Organization.objects.create(name="Beta Inc.", domain="beta.com")
        binding = UserStartupBinding.objects.create(
            user=self.user,
            organization=organization,
            google_connection=google_connection,
            role="founder",
            is_default_for_gmail=True,
        )
        prior_run = create_startup_update_run(organization=organization, binding=binding)
        upsert_monthly_update_draft(
            organization=organization,
            month=date(2026, 5, 1),
            run=prior_run,
            structured_memo={"highlights": ["Stale alpha"]},
            model_name="prior-model",
        )
        prior_run.status = ContentFactoryRunStatus.COMPLETED
        prior_run.save(update_fields=["status", "updated_at"])

        fresh_run = create_startup_update_run(organization=organization, binding=binding)
        merged = upsert_monthly_update_draft(
            organization=organization,
            month=date(2026, 5, 1),
            run=fresh_run,
            structured_memo={"highlights": ["Fresh delta"]},
            model_name="fresh-model",
        )

        # Default behaviour (no force regenerate) still merges, preserving prior points.
        self.assertIn("Stale alpha", str(merged.structured_memo))
        self.assertIn("Fresh delta", str(merged.structured_memo))

    def test_email_draft_start_reuses_completed_draft_without_creating_run(self):
        self.client.force_authenticate(user=self.user)
        self._create_founder_company()
        google_connection = GoogleConnection.objects.create(
            user=self.user,
            google_email="founder@gmail.com",
            refresh_token="refresh-token",
            scope="https://www.googleapis.com/auth/gmail.readonly",
        )
        organization = Organization.objects.create(name="Acme Inc.", domain="acme.com")
        UserStartupBinding.objects.create(
            user=self.user,
            organization=organization,
            google_connection=google_connection,
            role="founder",
            is_default_for_gmail=True,
        )
        MonthlyUpdateDraft.objects.create(
            organization=organization,
            month=date(2026, 3, 1),
            status="ready",
            structured_memo={
                "highlights": ["Closed two new pilots"],
                "lowlights": ["Hiring is still slow"],
                "asks": ["Customer intros"],
                "kpi_snapshot": [{"metric_key": "revenue", "label": "Revenue", "value": "$45,000"}],
            },
            rendered_markdown="# March Update",
        )

        response = self.client.post(
            "/api/v1/vibe-raising/email-draft/start/",
            {"targetMonth": "2026-03-01"},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["state"], "completed")
        self.assertEqual(response.data["targetMonth"], "2026-03-01")
        self.assertFalse(ContentFactoryRun.objects.filter(workflow="startup_monthly_update").exists())
        self.assertEqual(response.data["currentMonth"]["metrics"]["revenue"], "$45,000")

        with patch("vibe_raising.views.notify_valley_run_created") as mock_notify:
            with self.captureOnCommitCallbacks(execute=True):
                forced_response = self.client.post(
                    "/api/v1/vibe-raising/email-draft/start/",
                    {"targetMonth": "2026-03-01", "forceRegenerate": True},
                    format="json",
                )

        self.assertEqual(forced_response.status_code, 201)
        self.assertEqual(forced_response.data["state"], "queued")
        self.assertEqual(forced_response.data["targetMonth"], "2026-03-01")
        forced_run = ContentFactoryRun.objects.get(run_id=forced_response.data["runId"])
        self.assertEqual(forced_run.run_request["target_month"], "2026-03-01")
        mock_notify.assert_called_once_with(forced_run.run_id)

    def test_email_draft_start_refreshes_xero_metrics_when_reusing_completed_draft(self):
        self.client.force_authenticate(user=self.user)
        self._create_founder_company()
        google_connection = self._create_google_connection()
        organization = Organization.objects.create(name="Acme Inc.", domain="acme.com")
        binding = UserStartupBinding.objects.create(
            user=self.user,
            organization=organization,
            google_connection=google_connection,
            role="founder",
            is_default_for_gmail=True,
        )
        ExternalServiceConnection.objects.create(
            user=self.user,
            organization=organization,
            provider=ExternalServiceProvider.XERO,
            external_account_id="tenant-123",
            account_label="Acme Xero",
            last_synced_at=timezone.now(),
        )
        run = create_startup_update_run(
            organization=organization,
            binding=binding,
        )
        run.status = ContentFactoryRunStatus.COMPLETED
        run.run_request = {
            **(run.run_request or {}),
            "input_sources": ["gmail", "xero"],
        }
        run.save(update_fields=["status", "run_request", "updated_at"])
        MonthlyUpdateDraft.objects.create(
            organization=organization,
            run=run,
            month=date(2026, 3, 1),
            status="ready",
            structured_memo={
                "highlights": ["Closed two new pilots"],
                "kpi_snapshot": [{"metric_key": "revenue", "label": "Revenue", "value": "$45,000"}],
            },
            rendered_markdown="# March Update",
        )

        with patch("vibe_raising.views.publish_xero_metric_observations") as mock_publish:
            mock_publish.return_value = {"warnings": [], "published_metric_count": 0}
            response = self.client.post(
                "/api/v1/vibe-raising/email-draft/start/",
                {"inputSources": ["gmail", "xero"], "targetMonth": "2026-03-01"},
                format="json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["state"], "completed")
        self.assertFalse(response.data["reusedExistingRun"])
        self.assertEqual(ContentFactoryRun.objects.filter(workflow="startup_monthly_update").count(), 1)
        mock_publish.assert_called_once()
        self.assertEqual(mock_publish.call_args.kwargs["organization"], organization)
        self.assertIsNone(mock_publish.call_args.kwargs["run"])

    def test_email_draft_start_merges_existing_startup_profile_context(self):
        self.client.force_authenticate(user=self.user)
        _profile, _company = self._create_founder_company()
        organization = Organization.objects.create(
            name="Acme Legacy",
            domain="acme.com",
            competitors=[{"name": "CompeteCo", "domain": "compete.co"}],
            seed_keywords=["workflow"],
        )
        OrganizationContentConfig.objects.create(
            organization=organization,
            brand_name="Acme AI",
            company_context="Acme AI automates operations for finance teams.",
        )
        StartupProfile.objects.create(
            organization=organization,
            company_aliases=["Manual Alias"],
            founder_names=["Existing Founder"],
            team_names=["Existing Founder"],
            notes="Manual note",
            stage="seed",
        )

        response = self.client.post(
            "/api/v1/vibe-raising/email-draft/start/",
            {},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["state"], "auth_required")
        profile = StartupProfile.objects.get(organization=organization)
        organization.refresh_from_db()
        self.assertEqual(organization.name, "Acme Inc.")
        self.assertEqual(profile.stage, "seed")
        self.assertIn("Manual Alias", profile.company_aliases)
        self.assertIn("Acme Inc.", profile.company_aliases)
        self.assertIn("Acme AI", profile.company_aliases)
        self.assertIn("Founder User", profile.founder_names)
        self.assertIn("Founder User", profile.team_names)
        self.assertIn("CompeteCo", profile.competitor_names)
        self.assertIn("compete.co", profile.competitor_domains)
        self.assertIn("workflow", profile.positive_keywords)
        self.assertIn("Manual note", profile.notes)
        self.assertIn("Acme AI automates operations for finance teams.", profile.notes)

    def test_investor_gets_403_on_startup_update_endpoints(self):
        self.client.force_authenticate(user=self.user)
        VibeRaisingProfile.objects.create(
            user=self.user,
            role=VibeRaisingProfile.ROLE_INVESTOR,
            organization_name="Alpha Ventures",
        )

        bootstrap_response = self.client.post(
            "/api/v1/vibe-raising/startup-update/bootstrap/",
            {},
            format="json",
        )
        run_response = self.client.post(
            "/api/v1/vibe-raising/startup-update/run/",
            {},
            format="json",
        )
        status_response = self.client.get("/api/v1/vibe-raising/startup-update/status/")

        self.assertEqual(bootstrap_response.status_code, 403)
        self.assertEqual(run_response.status_code, 403)
        self.assertEqual(status_response.status_code, 403)
