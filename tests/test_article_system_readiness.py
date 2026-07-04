"""Detection is not readiness.

A repo scan can report an article surface (`article_system.state == "existing"`)
while explicitly failing to confirm anywhere to publish into — skedy.io's shape:
state "existing", empty publish_targets, reason "Detected article surface at
app/(public)/resources/guides/page.tsx, but no safe publish target could be
confirmed." Readiness previously treated the state alone as published, so the
setup wizard skipped the "Build articles scaffold" step straight to topic
research (and the generation endpoint would then 409 the same org). These tests
pin the corrected semantics: detection-only states ("existing"/"detected") count
as published ONLY with a usable publish path (publish_targets, a
publish_mutation_target, or a registry); explicit verdict states and the
roo_scaffolded+articles_scaffolded path keep their meaning.
"""
import os

from django.test import TestCase
from rest_framework.test import APIClient

from content_factory.article_setup_reset import (
    carry_reset_markers,
    reset_article_setup_config,
)
from content_factory.article_system import resolve_article_system
from content_factory.models import OrganizationContentConfig
from content_factory.vibe_marketing_views import (
    _article_system_is_published,
    _profile_checks,
    compute_article_readiness,
)
from organizations.models import Organization


SKEDY_DETECTION_REASON = (
    "Detected article surface at app/(public)/resources/guides/page.tsx, "
    "but no safe publish target could be confirmed."
)


class ArticleSystemDetectionReadinessTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(domain="skedy.io", name="Skedy")
        self.config = OrganizationContentConfig.objects.create(
            organization=self.org,
            github_repo="mesieou/skedy-ai",
        )

    def _set_article_system(self, **overrides):
        article_system = {
            "state": "existing",
            "confidence": "medium",
            "source": "scan",
            "reason": SKEDY_DETECTION_REASON,
        }
        article_system.update(overrides)
        self.config.article_system = article_system
        self.config.save(update_fields=["article_system", "updated_at"])

    def test_detected_surface_without_publish_path_is_not_ready(self):
        self._set_article_system()

        article_system = resolve_article_system(self.config)
        self.assertFalse(_article_system_is_published(self.config, article_system))

        readiness = compute_article_readiness(self.org, self.config, [])
        self.assertFalse(readiness["generation_ready"])
        self.assertFalse(readiness["proofs"]["article_system_published"])

        scaffold = _profile_checks(self.org, self.config, latest_runs=[])["scaffold"]
        self.assertFalse(scaffold["passed"])
        self.assertFalse(scaffold["generationReady"])
        self.assertFalse(scaffold["published"])
        self.assertFalse(scaffold["setupMerged"])
        self.assertFalse(scaffold["setupBlocked"])

    def test_detected_surface_with_publish_targets_stays_ready(self):
        self._set_article_system()
        self.config.publish_targets = [{"target_id": "articles", "kind": "react_article_system"}]
        self.config.save(update_fields=["publish_targets", "updated_at"])

        article_system = resolve_article_system(self.config)
        self.assertTrue(_article_system_is_published(self.config, article_system))
        self.assertTrue(compute_article_readiness(self.org, self.config, [])["generation_ready"])

    def test_detected_surface_with_registry_stays_ready(self):
        self._set_article_system(registry={"path": "app/articles/registry.ts", "export_name": "ARTICLES"})

        article_system = resolve_article_system(self.config)
        self.assertTrue(_article_system_is_published(self.config, article_system))
        self.assertTrue(compute_article_readiness(self.org, self.config, [])["generation_ready"])

    def test_detected_surface_with_publish_mutation_target_stays_ready(self):
        self._set_article_system(publish_mutation_target="app/articles/registry.ts")

        article_system = resolve_article_system(self.config)
        self.assertTrue(_article_system_is_published(self.config, article_system))
        self.assertTrue(compute_article_readiness(self.org, self.config, [])["generation_ready"])

    def test_roo_scaffolded_org_stays_ready(self):
        self._set_article_system(state="roo_scaffolded")
        self.config.articles_scaffolded = True
        self.config.save(update_fields=["articles_scaffolded", "updated_at"])

        article_system = resolve_article_system(self.config)
        self.assertTrue(_article_system_is_published(self.config, article_system))
        self.assertTrue(compute_article_readiness(self.org, self.config, [])["generation_ready"])


class ArticleSetupResetMarkerDurabilityTests(TestCase):
    """The reset watermark + tombstones must survive scan-driven org-config writes."""

    def setUp(self):
        self.client = APIClient()
        self.api_key = "test_roo_key"
        os.environ["ROO_API_KEY"] = self.api_key
        os.environ["INTERNAL_API_KEY"] = self.api_key

        from django.conf import settings

        settings.ROO_API_KEY = self.api_key
        settings.INTERNAL_API_KEY = self.api_key
        self.client.credentials(HTTP_X_API_KEY=self.api_key)

        self.org = Organization.objects.create(domain="skedy.io", name="Skedy")
        self.config = OrganizationContentConfig.objects.create(
            organization=self.org,
            github_repo="mesieou/skedy-ai",
        )

    def test_org_config_put_preserves_reset_markers(self):
        reset_article_setup_config(self.config, github_repo="mesieou/skedy-ai")
        self.config.refresh_from_db()
        reset_marker = self.config.article_system.get("article_setup_reset_at")
        self.assertTrue(reset_marker)

        response = self.client.put(
            "/api/content-factory/org/config/",
            {
                "domain": "skedy.io",
                "article_system": {
                    "state": "existing",
                    "confidence": "medium",
                    "source": "scan",
                    "reason": SKEDY_DETECTION_REASON,
                },
                "publish_targets": [],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)

        self.config.refresh_from_db()
        article_system = self.config.article_system
        self.assertEqual(article_system.get("state"), "existing")
        self.assertEqual(article_system.get("article_setup_reset_at"), reset_marker)
        self.assertEqual(article_system.get("articleSetupResetAt"), reset_marker)
        self.assertTrue(isinstance(article_system.get("article_setup_reset"), dict))

    def test_carry_reset_markers_keeps_incoming_values(self):
        stored = {
            "article_setup_reset_at": "2026-07-01T00:00:00+00:00",
            "article_setup_reset": {"resetAt": "2026-07-01T00:00:00+00:00"},
        }
        replacement = {"state": "existing", "article_setup_reset_at": "2026-07-04T00:00:00+00:00"}

        merged = carry_reset_markers(stored, replacement)

        # An explicit incoming stamp wins; missing keys are filled from the store.
        self.assertEqual(merged["article_setup_reset_at"], "2026-07-04T00:00:00+00:00")
        self.assertEqual(merged["article_setup_reset"], {"resetAt": "2026-07-01T00:00:00+00:00"})
