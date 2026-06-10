from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from core.models import ContentFactoryRun, Organization
from integrations.models import ExternalServiceConnection, ExternalServiceProvider
from integrations.services.finance import FINANCIAL_MONTHLY_METRICS_WORKFLOW
from startup_updates.models import UserStartupBinding

User = get_user_model()


class FinancialSyncDomainFallbackTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(email="founder@example.com", password="password")
        self.client.force_authenticate(user=self.user)

    def test_sync_without_domain_resolves_org_from_financial_connection(self):
        # Regression: the web client posts {providers} with no domain, which
        # previously always failed with "domain is required."
        organization = Organization.objects.create(name="Acme Inc.", domain="acme.com")
        ExternalServiceConnection.objects.create(
            user=self.user,
            organization=organization,
            provider=ExternalServiceProvider.XERO,
            external_account_id="tenant-123",
        )

        response = self.client.post("/api/v1/integrations/financial/sync", {}, format="json")

        self.assertIn(response.status_code, (200, 201))
        run = ContentFactoryRun.objects.filter(workflow=FINANCIAL_MONTHLY_METRICS_WORKFLOW).first()
        self.assertIsNotNone(run)
        self.assertEqual(run.domain, "acme.com")

    def test_sync_without_domain_resolves_org_from_binding(self):
        organization = Organization.objects.create(name="Beta Inc.", domain="beta.com")
        UserStartupBinding.objects.create(user=self.user, organization=organization, role="founder")

        response = self.client.post("/api/v1/integrations/financial/sync", {}, format="json")

        self.assertIn(response.status_code, (200, 201))

    def test_sync_without_domain_or_org_still_requires_domain(self):
        response = self.client.post("/api/v1/integrations/financial/sync", {}, format="json")

        self.assertEqual(response.status_code, 400)

    def test_sync_with_explicit_domain_still_works(self):
        organization = Organization.objects.create(name="Gamma Inc.", domain="gamma.com")
        UserStartupBinding.objects.create(user=self.user, organization=organization, role="founder")

        response = self.client.post(
            "/api/v1/integrations/financial/sync",
            {"domain": "gamma.com"},
            format="json",
        )

        self.assertIn(response.status_code, (200, 201))
