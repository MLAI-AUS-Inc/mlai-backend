from datetime import date, timedelta
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
from startup_updates.models import (
    MonthlyUpdateDraft,
    MonthlyUpdateDraftStatus,
    SlackChannelSelection,
    StartupEvent,
    StartupMetricObservation,
    StartupProfile,
    UserStartupBinding,
)
from startup_updates.services import (
    DEFAULT_BACKFILL_MONTHS,
    SUPERSEDED_GMAIL_CONNECTION_ERROR,
    create_startup_update_run,
    resolve_or_create_profile,
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

    def _create_google_connection(self, *, user=None, email="founder@gmail.com", refresh_token="refresh-token"):
        return GoogleConnection.objects.create(
            user=user or self.user,
            google_email=email,
            refresh_token=refresh_token,
            scope="https://www.googleapis.com/auth/gmail.readonly",
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
                    "ignored": "noop",
                },
            },
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        draft = MonthlyUpdateDraft.objects.get(organization__domain="acme.com", month=date(2026, 3, 1))
        self.assertEqual(draft.status, MonthlyUpdateDraftStatus.READY)
        self.assertEqual(draft.structured_memo["highlights"], ["Closed two pilots", "Hired first AE"])
        self.assertEqual(draft.structured_memo["lowlights"], ["Longer sales cycle"])
        self.assertEqual(draft.structured_memo["asks"], ["Intros to health system buyers"])
        self.assertEqual(draft.structured_memo["summary"], "Strong month with enterprise momentum.")
        self.assertEqual(draft.structured_memo["source_url"], "https://example.com/march-update")
        self.assertEqual(draft.structured_memo["video_url"], "https://storage.example.com/vibe-raising/demo.mp4")
        self.assertEqual(draft.structured_memo["video"]["content_type"], "video/mp4")
        self.assertEqual(draft.structured_memo["video"]["file_size_bytes"], 12345)
        self.assertEqual(response.data["update"]["month"], "March 2026")
        self.assertEqual(response.data["update"]["summary"], "Strong month with enterprise momentum.")
        self.assertEqual(response.data["update"]["sourceUrl"], "https://example.com/march-update")
        self.assertEqual(response.data["update"]["videoUrl"], "https://storage.example.com/vibe-raising/demo.mp4")
        self.assertEqual(response.data["update"]["videoContentType"], "video/mp4")
        self.assertEqual(response.data["update"]["videoOriginalFilename"], "demo.mp4")
        self.assertEqual(response.data["update"]["metrics"]["revenue"], "50000")
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
            {},
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

        self.assertEqual(results_response.status_code, 200)
        self.assertEqual(results_response.data["runId"], older_run.run_id)
        self.assertEqual(results_response.data["currentMonth"]["month"], "March")
        self.assertEqual(results_response.data["currentMonth"]["highlights"], "March highlight\nMarch second highlight")
        self.assertEqual(results_response.data["draft"]["month"], "March")
        self.assertEqual(results_response.data["draft"]["metrics"]["revenue"], "$45,000")
        self.assertEqual(results_response.data["draft"]["highlights"], "March highlight\nMarch second highlight")
        self.assertEqual(results_response.data["draft"]["challenges"], "March challenge\nMarch second challenge")
        self.assertEqual(results_response.data["draft"]["asks"], "March ask\nMarch second ask")
        self.assertEqual(results_response.data["months"][0]["month"], "March")
        self.assertEqual(results_response.data["months"][0]["highlights"], "March highlight\nMarch second highlight")

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
            {},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["state"], "completed")
        self.assertFalse(ContentFactoryRun.objects.filter(workflow="startup_monthly_update").exists())
        self.assertEqual(response.data["currentMonth"]["metrics"]["revenue"], "$45,000")

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
