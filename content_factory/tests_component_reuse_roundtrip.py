"""Scan-speedup Phase 0: close the component-reuse round-trip.

Every prod scan regenerated all ~29 article components (~7-11 min) because the
org-config GET withheld the stored component rows and dropped the scan context
fingerprint, so content-factory's reuse decision could never see the prior
generation. These tests pin the two halves of the round-trip:

- scan_artifact_cache (context fingerprint) survives PUT -> GET
- the full stored component rows are returned ONLY when the scan hydration
  explicitly asks (include_component_reuse=1); the default response stays lean
  and never exposes them under `generated_components` (whose dormant
  render-pipeline consumers must not activate).
"""

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from content_factory.models import GeneratedComponent, OrganizationContentConfig
from organizations.models import Organization

API_KEY = "test-roo-key"
CONFIG_URL = "/api/content-factory/org/config"


@override_settings(ROO_API_KEY=API_KEY)
class ComponentReuseRoundTripTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.org = Organization.objects.create(name="Acme", domain="acme.com")
        self.config = OrganizationContentConfig.objects.create(
            organization=self.org, github_repo="acme/site"
        )

    def _get(self, **params):
        return self.client.get(CONFIG_URL, params, HTTP_X_API_KEY=API_KEY)

    def _put(self, payload):
        return self.client.put(CONFIG_URL, payload, format="json", HTTP_X_API_KEY=API_KEY)

    def _create_component(self, name="ArticleHeroHeader"):
        return GeneratedComponent.objects.create(
            organization=self.org,
            name=name,
            content=f"export default function {name}() {{ return null; }}",
            source="generated",
            import_statement=f"import {name} from '@/components/articles/{name}';",
            metadata={"supported_section_types": ["hero"]},
        )

    def test_scan_artifact_cache_round_trips(self):
        cache = {"context_fingerprint": "abc123", "artifact_reuse": {"components": "refreshed"}}
        response = self._put({"domain": "acme.com", "scan_artifact_cache": cache})
        self.assertEqual(response.status_code, 200)

        fetched = self._get(domain="acme.com")
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.data["scan_artifact_cache"], cache)

    def test_reuse_inventory_requires_opt_in(self):
        self._create_component()
        response = self._get(domain="acme.com")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("component_reuse_inventory", response.data)
        # The dormant-consumer key must never carry the rows either.
        self.assertNotIn("generated_components", response.data)

    def test_reuse_inventory_returns_full_rows_when_requested(self):
        self._create_component("ArticleHeroHeader")
        self._create_component("QuoteBlock")

        response = self._get(domain="acme.com", include_component_reuse="1")
        self.assertEqual(response.status_code, 200)
        inventory = response.data["component_reuse_inventory"]
        self.assertEqual([item["name"] for item in inventory], ["ArticleHeroHeader", "QuoteBlock"])
        hero = inventory[0]
        self.assertIn("export default function ArticleHeroHeader", hero["content"])
        self.assertEqual(hero["source"], "generated")
        self.assertEqual(
            hero["import_statement"],
            "import ArticleHeroHeader from '@/components/articles/ArticleHeroHeader';",
        )
        self.assertEqual(hero["metadata"], {"supported_section_types": ["hero"]})

    def test_reuse_inventory_is_tenant_scoped(self):
        other_org = Organization.objects.create(name="Beta", domain="beta.com")
        OrganizationContentConfig.objects.create(organization=other_org, github_repo="beta/site")
        GeneratedComponent.objects.create(
            organization=other_org,
            name="BetaOnly",
            content="export default function BetaOnly() { return null; }",
            source="generated",
        )
        self._create_component("ArticleHeroHeader")

        response = self._get(domain="acme.com", include_component_reuse="true")
        self.assertEqual(response.status_code, 200)
        names = [item["name"] for item in response.data["component_reuse_inventory"]]
        self.assertEqual(names, ["ArticleHeroHeader"])
