"""Win 7: content-factory org-config resolution must not cross tenants.

The domain is the authoritative tenant key. github_repo used to be tried first
(and resolved non-deterministically), so a stale scan hint or a repo shared
across two companies could silently bind a run to a DIFFERENT company's org —
and its GitHub token + publish target. These tests pin: domain wins, a
conflicting repo is a structured 409, and the fuzzy-match domain list can be
scoped to a single owner.
"""

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from content_factory.models import OrganizationContentConfig
from organizations.models import Organization

API_KEY = "test-roo-key"
CONFIG_URL = "/api/content-factory/org/config"
DOMAINS_URL = "/api/content-factory/orgs/domains"


@override_settings(ROO_API_KEY=API_KEY)
class OrgConfigResolutionTenancyTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.org_a = Organization.objects.create(name="Acme", domain="acme.com")
        self.org_b = Organization.objects.create(name="Beta", domain="beta.com")
        self.config_a = OrganizationContentConfig.objects.create(
            organization=self.org_a, github_repo="acme/site", connected_slack_user_id="UA"
        )
        self.config_b = OrganizationContentConfig.objects.create(
            organization=self.org_b, github_repo="beta/site", connected_slack_user_id="UB"
        )

    def _get(self, **params):
        return self.client.get(CONFIG_URL, params, HTTP_X_API_KEY=API_KEY)

    def test_domain_wins_over_a_conflicting_repo(self):
        # domain names Acme, but the repo belongs to Beta → structured conflict,
        # never a silent switch to Beta's org/token.
        response = self._get(domain="acme.com", github_repo="beta/site")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "repo_domain_org_conflict")
        self.assertEqual(response.data["domain"], "acme.com")
        self.assertEqual(response.data["repo_org_domain"], "beta.com")

    def test_domain_resolves_when_repo_agrees(self):
        response = self._get(domain="acme.com", github_repo="acme/site")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["domain"], "acme.com")

    def test_domain_only_resolves(self):
        response = self._get(domain="https://www.acme.com/blog")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["domain"], "acme.com")

    def test_repo_only_resolves_deterministically(self):
        response = self._get(github_repo="beta/site")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["domain"], "beta.com")

    def test_unknown_domain_is_404(self):
        response = self._get(domain="nope.example")
        self.assertEqual(response.status_code, 404)

    def test_shared_repo_across_orgs_resolves_by_domain_not_arbitrary_repo(self):
        # Both orgs point at the same monorepo; the domain must decide which.
        self.config_b.github_repo = "acme/site"
        self.config_b.save(update_fields=["github_repo"])

        a = self._get(domain="acme.com", github_repo="acme/site")
        self.assertEqual(a.status_code, 200)
        self.assertEqual(a.data["domain"], "acme.com")

        b = self._get(domain="beta.com", github_repo="acme/site")
        self.assertEqual(b.status_code, 200)
        self.assertEqual(b.data["domain"], "beta.com")


@override_settings(ROO_API_KEY=API_KEY)
class OrgDomainsScopingTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.org_a = Organization.objects.create(name="Acme", domain="acme.com")
        self.org_b = Organization.objects.create(name="Beta", domain="beta.com")
        OrganizationContentConfig.objects.create(
            organization=self.org_a, connected_slack_user_id="owner-1"
        )
        OrganizationContentConfig.objects.create(
            organization=self.org_b, connected_slack_user_id="owner-2"
        )

    def test_global_list_returns_all_domains(self):
        response = self.client.get(DOMAINS_URL, HTTP_X_API_KEY=API_KEY)
        self.assertEqual(response.status_code, 200)
        self.assertIn("acme.com", response.data)
        self.assertIn("beta.com", response.data)

    def test_owner_scoped_list_returns_only_that_owners_domains(self):
        response = self.client.get(
            DOMAINS_URL, {"slack_user_id": "owner-1"}, HTTP_X_API_KEY=API_KEY
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.data), ["acme.com"])

    def test_owner_with_no_orgs_gets_empty_list(self):
        response = self.client.get(
            DOMAINS_URL, {"slack_user_id": "nobody"}, HTTP_X_API_KEY=API_KEY
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.data), [])
