from datetime import date, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import ContentFactoryRun, ContentFactoryRunStatus, Organization, OrganizationContentConfig
from integrations.models import GoogleConnection, MonthlyUpdateDraft, StartupProfile, UserStartupBinding
from integrations.services.startup_updates import create_startup_update_run
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

        self.assertEqual(company_response.status_code, 403)
        self.assertEqual(active_response.status_code, 403)

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
            reverse("vibe-raising-email-draft-status"),
            "/api/v1/vibe-raising/email-draft/status/",
        )
        self.assertEqual(
            reverse("vibe-raising-email-draft-latest"),
            "/api/v1/vibe-raising/email-draft/latest/",
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
                "lowlights": ["Hiring is still slow"],
                "asks": ["Customer intros"],
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
        GoogleConnection.objects.create(
            user=self.user,
            google_email="founder@gmail.com",
            refresh_token="refresh-token",
            scope="https://www.googleapis.com/auth/gmail.readonly",
        )

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
        self.assertEqual(second.status_code, 200)
        self.assertEqual(ContentFactoryRun.objects.filter(workflow="startup_monthly_update").count(), 1)
        self.assertEqual(first.data["runId"], second.data["runId"])
        mock_notify.assert_called_once()

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
        self.assertEqual(queued_response.data["stepStates"], {})

        run.status = ContentFactoryRunStatus.RUNNING
        run.current_step = "event_extraction"
        run.save(update_fields=["status", "current_step", "updated_at"])

        running_response = self.client.get("/api/v1/vibe-raising/email-draft/status/")

        self.assertEqual(running_response.status_code, 200)
        self.assertEqual(running_response.data["state"], "running")
        self.assertEqual(running_response.data["runId"], run.run_id)
        self.assertEqual(running_response.data["status"], ContentFactoryRunStatus.RUNNING)
        self.assertEqual(running_response.data["currentStep"], "event_extraction")

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
                "lowlights": ["Hiring is still slow"],
                "asks": ["Customer intros"],
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
        self.assertEqual([item["month"] for item in response.data["pastMonths"]], ["January", "February"])
        self.assertEqual(response.data["draft"]["month"], "March")
        self.assertEqual(response.data["draft"]["pastMonths"][0]["month"], "February 2026")

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
