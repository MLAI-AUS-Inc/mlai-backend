"""Real ORM/ownership checks. Running these requires migration approval.

Use an approved disposable test database and controlled test settings. The
isolated scripts/test_my_startup.py runner intentionally does not load this file.
"""

from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
from integrations.models import ExternalServiceConnection
from organizations.models import Organization
from roo.models import PointsPurchase
from workflow_runs.models import ContentFactoryRun

ORIGIN = "https://chat.mlai.test"
PREFIX = "/api/v1/my-startup/"


@override_settings(
    COMMUNITY_CHAT_FRONTEND_URL=ORIGIN,
    COMMUNITY_CHAT_ALLOWED_ORIGINS=[ORIGIN],
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
)
class StartupDatabaseTests(TestCase):
    def setUp(self):
        # Ownership is real; network identity reconciliation is outside this suite.
        for target, options in (
            ("founder_tools.services.reconcile_user_slack_id_from_email", {}),
            (
                "requests.sessions.Session.request",
                {"side_effect": AssertionError("Unexpected external HTTP request")},
            ),
        ):
            mock = patch(target, **options)
            mock.start()
            self.addCleanup(mock.stop)
        cache.clear()
        self.addCleanup(cache.clear)
        user_model = get_user_model()
        self.owner = user_model.objects.create_user(
            email="startup-owner@example.test", password="test-only", role="participant"
        )
        self.other = user_model.objects.create_user(
            email="startup-other@example.test", password="test-only", role="participant"
        )
        self.profile = VibeRaisingProfile.objects.create(
            user=self.owner, role="founder"
        )
        other_profile = VibeRaisingProfile.objects.create(
            user=self.other, role="founder"
        )
        self.organization = Organization.objects.create(name="Acme", domain="acme.test")
        self.company = VibeRaisingCompany.objects.create(
            profile=self.profile,
            organization=self.organization,
            name="Acme",
            domain="acme.test",
        )
        self.draft = VibeRaisingCompany.objects.create(
            profile=self.profile, name="Draft"
        )
        foreign_org = Organization.objects.create(name="Other", domain="other.test")
        self.foreign = VibeRaisingCompany.objects.create(
            profile=other_profile,
            organization=foreign_org,
            name="Other",
            domain="other.test",
        )
        self.profile.active_company = self.company
        self.profile.save(update_fields=["active_company"])
        self.run = ContentFactoryRun.objects.create(
            run_id="existing-research",
            organization=self.organization,
            domain="acme.test",
            workflow="island_refresh",
            status="running",
            run_request={"island_research_brief": {"subject": "Trees"}},
        )
        self.foreign_run = ContentFactoryRun.objects.create(
            run_id="foreign-research",
            organization=foreign_org,
            domain="other.test",
            workflow="island_refresh",
            status="running",
            run_request={"island_research_brief": {"subject": "Other"}},
        )
        self.client = APIClient()
        # Credential/origin selection is covered separately by tests.py. This
        # suite exercises the real downstream querysets as the selected account.
        self.client.force_authenticate(user=self.owner)

    def test_existing_companies_are_reused_without_exposing_another_account(self):
        for _ in range(2):
            response = self.client.get(PREFIX + "founder-tools/companies/")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                {row["id"] for row in response.data},
                {str(self.company.pk), str(self.draft.pk)},
            )
        self.assertEqual(VibeRaisingCompany.objects.count(), 3)

    def test_domainless_bootstrap_honors_explicit_company_without_switching_active(
        self,
    ):
        response = self.client.get(
            PREFIX + "vibe-marketing/bootstrap/", {"company_id": str(self.draft.pk)}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["company"]["id"], str(self.draft.pk))
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.active_company_id, self.company.pk)
        self.assertEqual(ContentFactoryRun.objects.count(), 2)

    def test_another_accounts_company_cannot_be_read_changed_or_deleted(self):
        response = self.client.get(
            PREFIX + "vibe-marketing/bootstrap/", {"company_id": str(self.foreign.pk)}
        )
        self.assertEqual(response.status_code, 404)
        response = self.client.post(
            PREFIX + "founder-tools/companies/",
            {"companyId": str(self.foreign.pk), "name": "Changed"},
            format="json",
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            self.client.delete(
                PREFIX + f"founder-tools/companies/{self.foreign.pk}/"
            ).status_code,
            404,
        )
        self.foreign.refresh_from_db()
        self.assertEqual(self.foreign.name, "Other")

    def test_private_preview_denies_foreign_run_and_conflicting_company(self):
        base = PREFIX + f"companies/{self.company.pk}/vibe-marketing/runs/"
        response = self.client.get(
            base + self.foreign_run.run_id + "/live-preview/proxy/"
        )
        self.assertEqual(response.status_code, 404)
        response = self.client.get(
            base + self.run.run_id + "/live-preview/proxy/",
            {"company_id": str(self.foreign.pk)},
        )
        self.assertEqual(response.status_code, 400)

    @patch(
        "founder_tools.my_startup.purchases.PointsPurchaseService.create_checkout_session"
    )
    def test_purchase_review_and_checkout_require_the_stored_owner(self, checkout):
        purchase = PointsPurchase.objects.create(
            user=self.owner,
            slack_user_id="",
            pack_id="test-pack",
            points_amount=100,
            amount_cents=1000,
            purchase_from={"surface": "my-startup"},
        )
        path = PREFIX + f"points/purchases/{purchase.pk}/"
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data["frontend_checkout_page_url"],
            ORIGIN + f"/my-startup/credits/{purchase.pk}",
        )
        self.client.force_authenticate(user=self.other)
        self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(
            self.client.post(path + "checkout/", {}, format="json").status_code, 404
        )
        checkout.assert_not_called()

    @patch(
        "founder_tools.my_startup.connectors.disconnect_external_connection",
        return_value=True,
    )
    def test_disconnect_enforces_real_owner_organization_and_provider(self, disconnect):
        connections = [
            ExternalServiceConnection.objects.create(
                user=user,
                organization=organization,
                provider=provider,
            )
            for user, organization, provider in [
                (self.other, self.organization, "slack"),
                (self.owner, self.foreign.organization, "slack"),
                (self.owner, self.organization, "xero"),
                (self.owner, self.organization, "slack"),
            ]
        ]
        for connection in connections[:-1]:
            response = self.client.delete(
                PREFIX
                + f"integrations/sources/connections/{connection.pk}?company_id={self.company.pk}"
            )
            self.assertEqual(response.status_code, 404)
        disconnect.assert_not_called()
        owned = connections[-1]
        path = PREFIX + f"integrations/sources/connections/{owned.pk}"
        self.assertEqual(self.client.delete(path).status_code, 400)
        self.assertEqual(
            self.client.delete(path + f"?company_id={self.company.pk}").status_code, 200
        )
        disconnect.assert_called_once_with(self.owner, owned.pk)

    def issue_handoff(self, run_id):
        return self.client.post(
            "/api/v1/founder-tools/my-startup-handoff/",
            {
                "companyId": str(self.company.pk),
                "path": "/founder-tools/marketing/create?step=topics",
                "research": {
                    "brief": {"subject": "Trees"},
                    "runId": run_id,
                    "requestId": "original-paid-request",
                    "step": 1,
                    "selected": [],
                },
            },
            format="json",
        )

    def test_handoff_rechecks_real_ownership_and_preserves_the_existing_run(self):
        issued = self.issue_handoff(self.run.run_id)
        self.assertEqual(issued.status_code, 201)
        token = parse_qs(urlsplit(issued.data["url"]).query)["token"][0]
        path = PREFIX + "handoff/redeem/"
        self.client.force_authenticate(user=self.other)
        self.assertEqual(
            self.client.post(path, {"token": token}, format="json").status_code, 404
        )
        self.client.force_authenticate(user=self.owner)
        restored = self.client.post(path, {"token": token}, format="json")
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.data["research"]["runId"], self.run.run_id)
        self.assertEqual(
            restored.data["research"]["requestId"], "original-paid-request"
        )
        self.assertEqual(
            self.client.post(path, {"token": token}, format="json").status_code, 404
        )
        self.assertEqual(ContentFactoryRun.objects.count(), 2)
        self.assertEqual(PointsPurchase.objects.count(), 0)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "running")

    def test_handoff_rejects_real_foreign_and_wrong_workflow_runs(self):
        self.assertEqual(self.issue_handoff(self.foreign_run.run_id).status_code, 404)
        self.run.workflow = "article_generation"
        self.run.save(update_fields=["workflow"])
        self.assertEqual(self.issue_handoff(self.run.run_id).status_code, 404)
