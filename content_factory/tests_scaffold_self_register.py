"""A fresh articles scaffold self-registers its durable publish target on approve/merge.

`_link_built_scaffold_publish_target` synthesizes + persists the react_article_system target from
the built scaffold cache; the approve (`pr_created`) and merge mark helpers call it, so a scaffold
becomes publishable the moment it's approved — no manual Accept.
"""
from django.test import TestCase

from content_factory.models import Organization, OrganizationContentConfig
from content_factory.article_system import is_directly_publishable_target
from content_factory.vibe_marketing_views import (
    _link_built_scaffold_publish_target,
    _mark_pending_article_system_setup_merged,
    _mark_pending_article_system_setup_pr_created,
)

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

    def test_link_helper_registers_direct_target_from_cache(self):
        self.assertTrue(_link_built_scaffold_publish_target(self.config))
        self.config.refresh_from_db()
        direct = self._direct_target()
        self.assertEqual(len(direct), 1)
        self.assertEqual(direct[0]["content_path_pattern"], "src/pages/articles/{category}/{slug}.tsx")
        self.assertEqual(self.config.article_path_pattern, "src/pages/articles/{category}/{slug}.tsx")
        self.assertEqual(self.config.registry_path, "src/pages/articles/registry.ts")
        self.assertEqual(self.config.article_system.get("system_type"), "react_article_system")
        self.assertTrue(self.config.articles_scaffolded)

    def test_link_helper_clears_unlink_watermark(self):
        self.config.article_system = {"state": "existing", "publish_disconnected_at": "2026-06-28T00:00:00Z"}
        self.config.save(update_fields=["article_system"])
        self.assertTrue(_link_built_scaffold_publish_target(self.config))
        self.config.refresh_from_db()
        self.assertNotIn("publish_disconnected_at", self.config.article_system)

    def test_link_helper_noop_without_built_cache(self):
        self.config.article_system_setup_cache = {}
        self.config.save(update_fields=["article_system_setup_cache"])
        self.assertFalse(_link_built_scaffold_publish_target(self.config))
        self.config.refresh_from_db()
        self.assertEqual(self.config.publish_targets, [])

    def test_pr_created_mark_self_registers_target(self):
        # The scaffold-approve path (PR created) self-registers without a manual Accept.
        class _Run:
            run_id = "setup-run-1"
        _mark_pending_article_system_setup_pr_created(self.config, run=_Run(), result={})
        self.config.refresh_from_db()
        self.assertEqual(len(self._direct_target()), 1)
        self.assertEqual(
            self.config.article_system.get("pending_article_system_setup", {}).get("status"), "pr_created"
        )

    def test_merge_mark_self_registers_target(self):
        class _Run:
            run_id = "setup-run-1"
        _mark_pending_article_system_setup_merged(self.config, run=_Run(), result={})
        self.config.refresh_from_db()
        self.assertEqual(len(self._direct_target()), 1)
        self.assertTrue(self.config.articles_scaffolded)
