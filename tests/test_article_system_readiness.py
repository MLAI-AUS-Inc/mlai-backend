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
    article_setup_reset_marker,
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

# Tala Thrive prod shape (org 30): the scan found an article directory but could NOT
# confirm a safe publish route, so it emitted this manual-bundle fallback. Its own
# unsupported_reason says direct publish is not configured — it must never read as a
# live publishing surface.
TALA_DETECTION_REASON = (
    "Detected article surface at app/stories/page.tsx, "
    "but no safe publish target could be confirmed."
)
TALA_BUNDLE_ONLY_TARGET = {
    "kind": "bundle_only_article_directory",
    "source": "scan",
    "target_id": "bundle_only_article_directory_content_stories_json_json",
    "publish_capability": "bundle_only",
    "input_format": "content_bundle_v1",
    "delivery_adapter": "hook_bundle",
    "route_template": "/stories/{slug}",
    "content_path_pattern": "content/stories.json",
    "verification": {"mode": "bundle_only", "confidence": "medium", "preview_capable": False},
    "registration_strategy": {"type": "manual_bundle_delivery"},
    "unsupported_reason": (
        "Detected a json article directory at `content/stories.json`, but direct publish "
        "is not configured for this runtime. Add `.content-factory/target.yml` to enable "
        "hook-driven publish or use the generated bundle manually."
    ),
}
DIRECT_TARGET = {
    "target_id": "articles",
    "kind": "react_article_system",
    "publish_capability": "direct",
}


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

    def test_detected_surface_with_only_bundle_only_target_is_not_ready(self):
        """talathrive.com's shape: reset + rescan re-detects the repo's own /stories
        JSON system and emits a bundle_only fallback target whose unsupported_reason
        literally says direct publish is NOT configured. That target must not flip
        the wizard to "Publishing surface live" / hide the Build button."""
        self._set_article_system(
            reason=(
                "Detected article surface at app/stories/page.tsx, "
                "but no safe publish target could be confirmed."
            )
        )
        self.config.publish_targets = [
            {
                "target_id": "bundle_only_article_directory_content_stories_json_json",
                "kind": "bundle_only_article_directory",
                "delivery_adapter": "hook_bundle",
                "publish_capability": "bundle_only",
                "source": "scan",
                "unsupported_reason": (
                    "Detected a json article directory at `content/stories.json`, but direct "
                    "publish is not configured for this runtime."
                ),
            }
        ]
        self.config.save(update_fields=["publish_targets", "updated_at"])

        article_system = resolve_article_system(self.config)
        self.assertFalse(_article_system_is_published(self.config, article_system))

        readiness = compute_article_readiness(self.org, self.config, [])
        self.assertFalse(readiness["generation_ready"])
        self.assertFalse(readiness["proofs"]["article_system_published"])

        scaffold = _profile_checks(self.org, self.config, latest_runs=[])["scaffold"]
        self.assertFalse(scaffold["passed"])
        self.assertFalse(scaffold["generationReady"])
        self.assertFalse(scaffold["published"])

    def test_detected_surface_with_hook_target_stays_ready(self):
        # A hook target is a configured, automated publish route — unlike the
        # bundle_only fallback it must keep counting as a publish path.
        self._set_article_system()
        self.config.publish_targets = [
            {
                "target_id": "hook_articles",
                "kind": "hook_publish_target",
                "publish_capability": "hook",
                "source": "scan",
            }
        ]
        self.config.save(update_fields=["publish_targets", "updated_at"])

        article_system = resolve_article_system(self.config)
        self.assertTrue(_article_system_is_published(self.config, article_system))
        self.assertTrue(compute_article_readiness(self.org, self.config, [])["generation_ready"])

    def test_missing_state_with_only_bundle_only_target_is_not_published(self):
        # Same false positive through the non-detection branch (state "missing"
        # + a scan-sourced bundle_only fallback, birdpsychology's prod shape).
        self._set_article_system(state="missing", reason="")
        self.config.publish_targets = [
            {
                "target_id": "bundle_only_article_directory_content_articles_md",
                "kind": "bundle_only_article_directory",
                "publish_capability": "bundle_only",
                "source": "scan",
                "unsupported_reason": "direct publish is not configured for this runtime.",
            }
        ]
        self.config.save(update_fields=["publish_targets", "updated_at"])

        article_system = resolve_article_system(self.config)
        self.assertFalse(_article_system_is_published(self.config, article_system))

    def test_missing_state_with_scan_sourced_direct_target_stays_published(self):
        self._set_article_system(state="missing", reason="")
        self.config.publish_targets = [
            {
                "target_id": "articles",
                "kind": "react_article_system",
                "publish_capability": "direct",
                "source": "scan",
            }
        ]
        self.config.save(update_fields=["publish_targets", "updated_at"])

        article_system = resolve_article_system(self.config)
        self.assertTrue(_article_system_is_published(self.config, article_system))

    def test_roo_scaffolded_org_stays_ready(self):
        self._set_article_system(state="roo_scaffolded")
        self.config.articles_scaffolded = True
        self.config.save(update_fields=["articles_scaffolded", "updated_at"])

        article_system = resolve_article_system(self.config)
        self.assertTrue(_article_system_is_published(self.config, article_system))
        self.assertTrue(compute_article_readiness(self.org, self.config, [])["generation_ready"])

    def test_detected_surface_with_bundle_only_and_direct_targets_stays_ready(self):
        # A real direct target alongside the bundle-only fallback IS a publish path.
        self._set_article_system(reason=TALA_DETECTION_REASON)
        self.config.publish_targets = [dict(TALA_BUNDLE_ONLY_TARGET), dict(DIRECT_TARGET)]
        self.config.save(update_fields=["publish_targets", "updated_at"])

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


RESET_AT = "2026-07-08T14:28:17+00:00"


class ArticleSetupResetMarkerSuppressionTests(TestCase):
    """After a reset, a detection verdict alone must not re-complete the wizard: even
    if a rescan re-detects a genuinely direct-publishable surface, the reset watermark
    suppresses "published" until the founder explicitly builds or adopts. The
    articles_scaffolded guard exempts an org they already rebuilt, and the suppression
    is detection-only (explicit readiness verdicts keep their meaning)."""

    def setUp(self):
        self.org = Organization.objects.create(domain="talathrive.com", name="Tala Thrive")
        self.config = OrganizationContentConfig.objects.create(
            organization=self.org,
            github_repo="Gshah810/tala-main-website",
        )

    def _apply(self, article_system, *, publish_targets=None, articles_scaffolded=False):
        self.config.article_system = article_system
        self.config.publish_targets = publish_targets or []
        self.config.articles_scaffolded = articles_scaffolded
        self.config.save(
            update_fields=[
                "article_system",
                "publish_targets",
                "articles_scaffolded",
                "updated_at",
            ]
        )

    def test_reset_marker_suppresses_detection_only_published(self):
        self._apply(
            {
                "state": "existing",
                "source": "scan",
                "reason": TALA_DETECTION_REASON,
                "article_setup_reset_at": RESET_AT,
                "articleSetupResetAt": RESET_AT,
            },
            publish_targets=[dict(DIRECT_TARGET)],
        )
        article_system = resolve_article_system(self.config)
        self.assertFalse(_article_system_is_published(self.config, article_system))

    def test_reset_marker_does_not_suppress_scaffolded_org(self):
        self._apply(
            {
                "state": "existing",
                "source": "scan",
                "article_setup_reset_at": RESET_AT,
            },
            publish_targets=[dict(DIRECT_TARGET)],
            articles_scaffolded=True,
        )
        article_system = resolve_article_system(self.config)
        self.assertTrue(_article_system_is_published(self.config, article_system))

    def test_reset_marker_ignores_explicit_ready_states(self):
        # A non-detection published state (e.g. registry_driven_seo_ready) is an
        # explicit readiness verdict — the reset marker must not suppress it.
        article_system = {
            "state": "registry_driven_seo_ready",
            "source": "scan",
            "article_setup_reset_at": RESET_AT,
        }
        self._apply(article_system, publish_targets=[dict(DIRECT_TARGET)])
        # Pass the raw dict (normalize would coerce this state to "missing").
        self.assertTrue(_article_system_is_published(self.config, article_system))


class UseDetectedClearsResetMarkersTests(TestCase):
    """Adopting the detected system ("use_detected") is an explicit exit from a reset:
    it clears the watermark so the surface reads published again."""

    def setUp(self):
        self.client = APIClient()
        self.api_key = "test_roo_key"
        os.environ["ROO_API_KEY"] = self.api_key
        os.environ["INTERNAL_API_KEY"] = self.api_key

        from django.conf import settings

        settings.ROO_API_KEY = self.api_key
        settings.INTERNAL_API_KEY = self.api_key
        self.client.credentials(HTTP_X_API_KEY=self.api_key)

        self.org = Organization.objects.create(domain="talathrive.com", name="Tala Thrive")
        self.config = OrganizationContentConfig.objects.create(
            organization=self.org,
            github_repo="Gshah810/tala-main-website",
        )

    def test_use_detected_clears_reset_markers(self):
        # Reset stamps the watermark; a later rescan re-detects a direct-publishable
        # surface but the marker keeps it suppressed until the founder chooses.
        reset_article_setup_config(self.config, github_repo="Gshah810/tala-main-website")
        self.config.refresh_from_db()
        re_detected = dict(self.config.article_system)
        re_detected.update({"state": "existing", "source": "scan", "reason": TALA_DETECTION_REASON})
        self.config.article_system = re_detected
        self.config.publish_targets = [dict(DIRECT_TARGET)]
        self.config.save(update_fields=["article_system", "publish_targets", "updated_at"])

        self.assertTrue(article_setup_reset_marker(self.config.article_system))
        self.assertFalse(
            _article_system_is_published(self.config, resolve_article_system(self.config))
        )

        response = self.client.post(
            "/api/v1/content/article-system/decision",
            {
                "domain": "talathrive.com",
                "slack_user_id": "no-such-user",
                "decision": "use_detected",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)

        self.config.refresh_from_db()
        self.assertFalse(article_setup_reset_marker(self.config.article_system))
        self.assertTrue(
            _article_system_is_published(self.config, resolve_article_system(self.config))
        )
