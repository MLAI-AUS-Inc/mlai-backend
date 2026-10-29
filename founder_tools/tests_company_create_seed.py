"""Adding a startup records the member as founder 1 and the default author."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from content_factory.models import OrganizationContentConfig
from founder_tools.models import VibeRaisingCompany
from founder_tools.profile_fields import PROFILE_KEY, profile_details
from founder_tools.services import seed_new_company_founder_and_author
from startup_updates.models import StartupProfile

COMPANIES_URL = "/api/v1/founder-tools/companies/"


class CompanyCreateSeedTests(TestCase):
    def setUp(self):
        mock = patch("founder_tools.services.reconcile_user_slack_id_from_email")
        mock.start()
        self.addCleanup(mock.stop)
        self.user = get_user_model().objects.create_user(
            email="seed-founder@example.test", first_name="Ada", last_name="Lovelace", role="participant")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def create(self, **body):
        response = self.client.post(COMPANIES_URL, {"name": "Engines Pty", "createNew": True, **body}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        return response, VibeRaisingCompany.objects.select_related("organization").get(pk=response.data["id"])

    def test_create_seeds_founder_one_and_default_author(self):
        from content_factory.services.island_refresh_scheduler import _eligible_configs

        response, company = self.create(domain="engines.test")

        # The seeded founder is profile detail, not a strategy to cluster.
        self.assertNotIn(company.organization_id, [row.organization_id for row in _eligible_configs()])

        self.assertEqual(response.data["founderProfiles"], [{"name": "Ada Lovelace", "linkedinUrl": ""}])
        self.assertEqual(response.data["founderNames"], ["Ada Lovelace"])
        self.assertEqual(profile_details(company.organization)["founderProfiles"],
                         [{"name": "Ada Lovelace", "linkedinUrl": ""}])
        self.assertEqual(StartupProfile.objects.get(organization=company.organization).founder_names, ["Ada Lovelace"])
        config = OrganizationContentConfig.objects.get(organization=company.organization)
        self.assertEqual(config.default_author_id, f"member-{self.user.pk}")
        [author] = config.authors
        self.assertEqual((author["id"], author["name"], author["role"]),
                         (f"member-{self.user.pk}", "Ada Lovelace", "Founder"))

    def test_domainless_create_is_seeded_too(self):
        _, company = self.create()
        self.assertTrue(company.organization.domain.endswith(".invalid"))
        self.assertEqual(profile_details(company.organization)["founderProfiles"][0]["name"], "Ada Lovelace")

    def test_submitted_founders_are_kept(self):
        response, company = self.create(domain="engines.test", founderProfiles=[{"name": "Charles Babbage"}])
        self.assertEqual([row["name"] for row in response.data["founderProfiles"]], ["Charles Babbage"])
        self.assertEqual(StartupProfile.objects.get(organization=company.organization).founder_names,
                         ["Charles Babbage"])

    def test_existing_founders_and_authors_are_never_overwritten(self):
        _, company = self.create(domain="engines.test", founderProfiles=[{"name": "Charles Babbage"}])
        config = OrganizationContentConfig.objects.get(organization=company.organization)
        config.authors = [{"id": "charles", "name": "Charles Babbage"}]
        config.default_author_id = "charles"
        config.save()
        legacy = StartupProfile.objects.get(organization=company.organization)
        strategy = dict(config.pillar_strategy)
        strategy[PROFILE_KEY] = {}
        config.pillar_strategy = strategy
        config.save()
        legacy.founder_names = ["Legacy Founder"]
        legacy.save()

        seed_new_company_founder_and_author(user=self.user, company=company)

        config.refresh_from_db()
        legacy.refresh_from_db()
        self.assertEqual(config.authors, [{"id": "charles", "name": "Charles Babbage"}])
        self.assertEqual(config.default_author_id, "charles")
        self.assertEqual(legacy.founder_names, ["Legacy Founder"])
        self.assertIsNone(profile_details(company.organization).get("founderProfiles"))

    def test_updates_never_seed(self):
        _, company = self.create(domain="engines.test")
        config = OrganizationContentConfig.objects.get(organization=company.organization)
        config.authors = []
        config.default_author_id = ""
        strategy = dict(config.pillar_strategy)
        strategy[PROFILE_KEY] = {}
        config.pillar_strategy = strategy
        config.save()
        StartupProfile.objects.filter(organization=company.organization).update(founder_names=[])

        response = self.client.post(COMPANIES_URL, {"companyId": str(company.pk), "name": "Engines Pty",
                                                    "location": "Sydney"}, format="json")

        self.assertEqual(response.status_code, 200, response.data)
        config.refresh_from_db()
        self.assertEqual(config.authors, [])
        self.assertEqual(profile_details(company.organization).get("founderProfiles"), None)

    def test_blank_name_seeds_nothing_and_never_uses_the_email(self):
        self.user.first_name = self.user.last_name = ""
        self.user.save()

        response, company = self.create(domain="engines.test")

        self.assertFalse(response.data.get("founderProfiles"))
        config = OrganizationContentConfig.objects.get(organization=company.organization)
        self.assertEqual((config.authors, config.default_author_id), ([], ""))
        self.assertNotIn("seed-founder", str(profile_details(company.organization)))
