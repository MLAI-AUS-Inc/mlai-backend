from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from integrations.models import (
    ExternalServiceConnection,
    ExternalServiceConnectionStatus,
    ExternalServiceProvider,
    GitHubInstallation,
    GoogleConnection,
    UserIntegration,
)
from integrations.services.gmail_scopes import GMAIL_READONLY_SCOPE
from organizations.models import Organization
from startup_updates.models import (
    MonthlyUpdateDraft,
    MonthlyUpdateDraftStatus,
    UserStartupBinding,
)
from startup_updates.services import STARTUP_UPDATE_WORKFLOW
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStatus

User = get_user_model()

OVERVIEW_URL = "/api/v1/vibe-raising/admin/overview/"
USAGE_URL = "/api/v1/vibe-raising/admin/monthly-update-usage/"
UPDATES_URL = "/api/v1/vibe-raising/admin/updates/"


class VibeRaisingAdminEndpointsTests(TestCase):
    def setUp(self):
        self.client = APIClient()

        self.admin = User.objects.create_user(email="admin@example.com")
        self.admin.is_superuser = True
        self.admin.save(update_fields=["is_superuser"])

        self.founder = User.objects.create_user(
            email="founder@example.com", first_name="Fiona", last_name="Founder"
        )
        self.profile = VibeRaisingProfile.objects.create(
            user=self.founder,
            role=VibeRaisingProfile.ROLE_FOUNDER,
            organization_name="Acme",
        )
        self.org = Organization.objects.create(name="Acme", domain="acme.example")
        self.company = VibeRaisingCompany.objects.create(
            profile=self.profile,
            organization=self.org,
            name="Acme Inc",
            registered=True,
            avatar_url="https://img.example/acme.png",
        )
        self.draft = MonthlyUpdateDraft.objects.create(
            organization=self.org,
            month=date(2026, 6, 1),
            status=MonthlyUpdateDraftStatus.NEEDS_REVIEW,
            structured_memo={"summary": "We shipped a lot."},
        )

    # --- gating -----------------------------------------------------------
    def test_overview_rejects_non_admin(self):
        self.client.force_authenticate(self.founder)
        self.assertEqual(self.client.get(OVERVIEW_URL).status_code, 403)

    def test_overview_rejects_anonymous(self):
        self.assertIn(self.client.get(OVERVIEW_URL).status_code, (401, 403))

    def test_updates_rejects_non_admin(self):
        self.client.force_authenticate(self.founder)
        self.assertEqual(self.client.get(UPDATES_URL).status_code, 403)

    # --- overview ---------------------------------------------------------
    def test_overview_returns_contract_shape(self):
        self.client.force_authenticate(self.admin)
        response = self.client.get(OVERVIEW_URL)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        for key in (
            "stats",
            "updatesOverTime",
            "updatesByStage",
            "updatesByIndustry",
            "recentUpdates",
            "reviewCount",
        ):
            self.assertIn(key, body)
        self.assertEqual(body["reviewCount"], 1)
        self.assertIn("updatesCreated", {stat["key"] for stat in body["stats"]})

        summary = next(u for u in body["recentUpdates"] if u["id"] == str(self.draft.id))
        # IDs must be strings or the frontend normalizer drops the whole summary.
        self.assertIsInstance(summary["id"], str)
        self.assertEqual(summary["startupName"], "Acme Inc")
        self.assertEqual(summary["founderName"], "Fiona Founder")
        self.assertEqual(summary["companyId"], str(self.company.id))
        self.assertEqual(summary["status"], MonthlyUpdateDraftStatus.NEEDS_REVIEW)
        self.assertEqual(summary["updateMonth"], "June 2026")

    # --- list + filters ---------------------------------------------------
    def test_updates_list_filters_and_search(self):
        self.client.force_authenticate(self.admin)

        body = self.client.get(UPDATES_URL).json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(len(body["updates"]), 1)
        self.assertEqual(body["updates"][0]["startupName"], "Acme Inc")
        self.assertIsInstance(body["updates"][0]["id"], str)
        self.assertFalse(body["hasNext"])
        self.assertFalse(body["hasPrevious"])

        # "review" alias maps onto needs_review and matches our draft.
        self.assertEqual(self.client.get(UPDATES_URL + "?status=review").json()["total"], 1)
        # "ready" does not match a needs_review draft.
        self.assertEqual(self.client.get(UPDATES_URL + "?status=ready").json()["total"], 0)
        # search by founder name and by company name.
        self.assertEqual(self.client.get(UPDATES_URL + "?q=Fiona").json()["total"], 1)
        self.assertEqual(self.client.get(UPDATES_URL + "?q=Acme").json()["total"], 1)
        self.assertEqual(self.client.get(UPDATES_URL + "?q=nomatch").json()["total"], 0)

    # --- detail -----------------------------------------------------------
    def test_update_detail_returns_summary_update_and_founder(self):
        self.client.force_authenticate(self.admin)
        response = self.client.get(f"{UPDATES_URL}{self.draft.id}/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["summary"]["startupName"], "Acme Inc")
        self.assertEqual(body["company"]["name"], "Acme Inc")
        self.assertEqual(body["founder"]["email"], "founder@example.com")
        self.assertIsNotNone(body["update"])
        self.assertEqual(body["update"]["status"], MonthlyUpdateDraftStatus.NEEDS_REVIEW)

    def test_update_detail_missing_returns_404(self):
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.get(f"{UPDATES_URL}999999/").status_code, 404)


class VibeRaisingAdminMonthlyUpdateUsageTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_superuser(email="usage-admin@example.com")
        self.founder = User.objects.create_user(email="usage-founder@example.com")
        self.organization = Organization.objects.create(
            name="Usage Startup", domain="usage-startup.example"
        )
        self.binding = UserStartupBinding.objects.create(
            user=self.founder,
            organization=self.organization,
        )

    def _external_connection(self, *, user=None, organization=None, provider, status):
        return ExternalServiceConnection.objects.create(
            user=user or self.founder,
            organization=organization or self.organization,
            provider=provider,
            status=status,
            external_account_id=f"{provider}-{ExternalServiceConnection.objects.count()}",
        )

    def _ai_draft(self, *, user=None, organization=None, binding=None, workflow=STARTUP_UPDATE_WORKFLOW):
        user = user or self.founder
        organization = organization or self.organization
        binding = binding or self.binding
        run = ContentFactoryRun.objects.create(
            run_id=f"usage-run-{ContentFactoryRun.objects.count()}",
            workflow=workflow,
            domain=organization.domain,
            organization=organization,
            slack_user_id=str(user.id),
            status=ContentFactoryRunStatus.COMPLETED,
            run_request={"binding_id": binding.id},
        )
        return MonthlyUpdateDraft.objects.create(
            organization=organization,
            run=run,
            month=date(2026, 7, 1),
            model_name="gpt-5",
        )

    def test_usage_rejects_non_admin_and_anonymous(self):
        self.client.force_authenticate(self.founder)
        self.assertEqual(self.client.get(USAGE_URL).status_code, 403)
        self.client.force_authenticate(user=None)
        self.assertIn(self.client.get(USAGE_URL).status_code, (401, 403))

    def test_usage_counts_distinct_users_and_completed_ai_drafts(self):
        GoogleConnection.objects.create(
            user=self.founder,
            organization=self.organization,
            google_email="usage-founder@gmail.com",
            refresh_token="refresh-token",
            scope=GMAIL_READONLY_SCOPE,
        )
        self._external_connection(
            provider=ExternalServiceProvider.SLACK,
            status=ExternalServiceConnectionStatus.CONNECTED,
        )
        self._external_connection(
            provider=ExternalServiceProvider.LINEAR,
            status=ExternalServiceConnectionStatus.SYNCING,
        )
        self._ai_draft()

        self.client.force_authenticate(self.admin)
        response = self.client.get(USAGE_URL)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["connectedSourceUsers"], 1)
        self.assertEqual(body["aiAssistedUpdateUsers"], 1)
        self.assertEqual(body["connectedAndAiAssistedUsers"], 1)
        self.assertIn("asOf", body)
        source_counts = {item["provider"]: item["users"] for item in body["sources"]}
        self.assertEqual(source_counts["gmail"], 1)
        self.assertEqual(source_counts[ExternalServiceProvider.SLACK], 1)
        self.assertEqual(source_counts[ExternalServiceProvider.LINEAR], 1)

    def test_usage_excludes_marketing_unbound_and_inactive_connections(self):
        marketing_user = User.objects.create_user(
            email="marketing-only@example.com", slack_id="UMARKETING"
        )
        marketing_org = Organization.objects.create(
            name="Marketing Only", domain="marketing-only.example"
        )
        UserStartupBinding.objects.create(user=marketing_user, organization=marketing_org)

        # Vibe Marketing's website baseline can create a GoogleConnection, but
        # Search Console scope alone is not a monthly-update Gmail source.
        GoogleConnection.objects.create(
            user=marketing_user,
            organization=marketing_org,
            google_email="marketing@gmail.com",
            refresh_token="refresh-token",
            scope="https://www.googleapis.com/auth/webmasters.readonly",
        )
        UserIntegration.objects.create(
            slack_user_id=marketing_user.slack_id,
            github_repo="mlai/marketing-site",
        )
        GitHubInstallation.objects.create(
            user=marketing_user,
            installation_id="12345",
            account_login="marketing-only",
        )

        self._external_connection(
            provider=ExternalServiceProvider.NOTION,
            status=ExternalServiceConnectionStatus.DISCONNECTED,
        )
        self._external_connection(
            provider=ExternalServiceProvider.GOOGLE_DRIVE,
            status=ExternalServiceConnectionStatus.ERROR,
        )
        unbound_org = Organization.objects.create(name="Unbound", domain="unbound.example")
        self._external_connection(
            organization=unbound_org,
            provider=ExternalServiceProvider.STRIPE,
            status=ExternalServiceConnectionStatus.CONNECTED,
        )

        # Manual drafts and non-startup content-factory workflows are not AI
        # assisted monthly-update adoption.
        MonthlyUpdateDraft.objects.create(
            organization=self.organization,
            month=date(2026, 6, 1),
            model_name="vibe-raising-manual",
        )
        other_org = Organization.objects.create(name="Marketing Run", domain="marketing-run.example")
        other_binding = UserStartupBinding.objects.create(user=marketing_user, organization=other_org)
        self._ai_draft(
            user=marketing_user,
            organization=other_org,
            binding=other_binding,
            workflow="article_generation",
        )

        self.client.force_authenticate(self.admin)
        body = self.client.get(USAGE_URL).json()

        self.assertEqual(body["connectedSourceUsers"], 0)
        self.assertEqual(body["aiAssistedUpdateUsers"], 0)
        self.assertEqual(body["connectedAndAiAssistedUsers"], 0)
        self.assertTrue(all(item["users"] == 0 for item in body["sources"]))

    def test_usage_counts_one_user_across_multiple_companies_once(self):
        second_org = Organization.objects.create(name="Second Startup", domain="second-startup.example")
        second_binding = UserStartupBinding.objects.create(user=self.founder, organization=second_org)
        self._external_connection(
            provider=ExternalServiceProvider.NOTION,
            status=ExternalServiceConnectionStatus.CONNECTED,
        )
        self._external_connection(
            organization=second_org,
            provider=ExternalServiceProvider.NOTION,
            status=ExternalServiceConnectionStatus.CONNECTED,
        )
        self._ai_draft()
        self._ai_draft(organization=second_org, binding=second_binding)

        self.client.force_authenticate(self.admin)
        body = self.client.get(USAGE_URL).json()

        self.assertEqual(body["connectedSourceUsers"], 1)
        self.assertEqual(body["aiAssistedUpdateUsers"], 1)
        self.assertEqual(body["connectedAndAiAssistedUsers"], 1)
        notion = next(item for item in body["sources"] if item["provider"] == ExternalServiceProvider.NOTION)
        self.assertEqual(notion["users"], 1)
