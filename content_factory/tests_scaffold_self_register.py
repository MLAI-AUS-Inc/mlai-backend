"""Setup cache and PR labels cannot replace a verified native target."""
from django.contrib.auth import get_user_model
from django.test import TestCase

from content_factory.models import Organization, OrganizationContentConfig
from content_factory.article_system import is_directly_publishable_target
from content_factory.vibe_marketing_views import (
    _link_built_scaffold_publish_target,
    _mark_pending_article_system_setup_merged,
    _mark_pending_article_system_setup_pr_created,
)
from tests.website_fixtures import bind_verified_target_fixture

SETUP_CACHE = {
    "content_root": "articles",
    "route_path": "/articles",
    "managed_files": [
        "src/App.tsx",
        "src/pages/articles/registry.ts",
        "src/pages/articles/authors.ts",
        "src/pages/articles/authorRegistry.ts",
        "src/pages/articles/seo.ts",
        "src/pages/articles/index.tsx",
    ],
}


class ScaffoldSelfRegisterTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(domain="vyavos.com", name="Vyavos")
        self.config = OrganizationContentConfig.objects.create(
            organization=self.organization,
            github_repo="Vyavos/vyavos-website",
            github_installation_id="142050561",
            article_system_setup_cache=SETUP_CACHE,
            publish_targets=[],
            article_delivery_mode="content_only",
        )

    def _direct_target(self):
        return [t for t in (self.config.publish_targets or []) if is_directly_publishable_target(t)]

    def test_link_helper_selects_current_verified_target(self):
        user = get_user_model().objects.create_user(email='scaffold-owner@example.test')
        bind_verified_target_fixture(self.config, user=user)
        expected = list(self.config.publish_targets)
        self.config.publish_targets = []
        self.config.default_publish_target_id = None
        self.config.save(update_fields=['publish_targets', 'default_publish_target_id'])
        self.assertTrue(_link_built_scaffold_publish_target(self.config))
        self.config.refresh_from_db()
        self.assertEqual(self._direct_target(), expected)
        self.assertEqual(self.config.default_publish_target_id, expected[0]['target_id'])

    def test_cache_without_current_verification_cannot_clear_unlink_watermark(self):
        self.config.article_system = {"state": "existing", "publish_disconnected_at": "2026-06-28T00:00:00Z"}
        self.config.save(update_fields=["article_system"])
        self.assertFalse(_link_built_scaffold_publish_target(self.config))
        self.config.refresh_from_db()
        self.assertIn("publish_disconnected_at", self.config.article_system)
        self.assertEqual(self.config.publish_targets, [])

    def test_link_helper_noop_without_built_cache(self):
        self.config.article_system_setup_cache = {}
        self.config.save(update_fields=["article_system_setup_cache"])
        self.assertFalse(_link_built_scaffold_publish_target(self.config))
        self.config.refresh_from_db()
        self.assertEqual(self.config.publish_targets, [])

    def test_pr_created_label_does_not_certify_a_publish_target(self):
        class _Run:
            run_id = "setup-run-1"
        _mark_pending_article_system_setup_pr_created(self.config, run=_Run(), result={})
        self.config.refresh_from_db()
        self.assertEqual(self._direct_target(), [])
        self.assertEqual(
            self.config.article_system.get("pending_article_system_setup", {}).get("status"), "pr_created"
        )

    def test_merge_label_does_not_certify_a_publish_target(self):
        class _Run:
            run_id = "setup-run-1"
        _mark_pending_article_system_setup_merged(self.config, run=_Run(), result={})
        self.config.refresh_from_db()
        self.assertEqual(self._direct_target(), [])
        self.assertTrue(self.config.articles_scaffolded)
