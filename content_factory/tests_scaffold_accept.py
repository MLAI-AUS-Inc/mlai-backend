"""Link/accept an already-built articles scaffold + lightweight unlink (disconnect).

`react_article_system_target_from_setup_cache` synthesizes the durable publish target from a
built scaffold's managed files (deriving the REAL content root from the committed registry, not
the cache's logical name). The `publish_disconnected_at` watermark makes a user "unlink" stick so
a routine scan does not silently re-link.
"""
from django.test import SimpleTestCase

from content_factory.article_system import (
    is_directly_publishable_target,
    preserve_registered_publish_targets,
    react_article_system_target_from_setup_cache,
)

# vyavos-shaped cache: content_root is the LOGICAL name "articles", but the committed files live
# under src/pages/articles — the target must follow the real on-disk path.
SETUP_CACHE = {
    "content_root": "articles",
    "directory_name": "articles",
    "route_path": "/articles",
    "managed_files": [
        "src/App.tsx",
        "src/pages/articles/registry.ts",
        "src/pages/articles/authors.ts",
        "src/pages/articles/authorRegistry.ts",
        "src/pages/articles/seo.ts",
        "src/pages/articles/index.tsx",
        "src/pages/articles/resources.ts",
    ],
}

EXISTING = {"state": "existing", "confidence": "high", "directory_path": "src/pages/articles"}
BUNDLE_ONLY = [
    {
        "target_id": "bundle_only_article_directory_src_pages_articles_ts",
        "kind": "bundle_only_article_directory",
        "publish_capability": "bundle_only",
    }
]


class ReactArticleSystemTargetFromSetupCacheTests(SimpleTestCase):
    def test_synthesizes_target_from_real_committed_paths(self):
        bundle = react_article_system_target_from_setup_cache(SETUP_CACHE)
        self.assertTrue(bundle)
        target = bundle["publish_targets"][0]
        # Real on-disk root, not the logical "articles" name, and nested {category}/{slug}.
        self.assertEqual(target["content_path_pattern"], "src/pages/articles/{category}/{slug}.tsx")
        self.assertEqual(bundle["article_path_pattern"], "src/pages/articles/{category}/{slug}.tsx")
        self.assertEqual(bundle["registry_path"], "src/pages/articles/registry.ts")
        self.assertEqual(target["route_template"], "/articles/{category}/{slug}")
        self.assertEqual(target["kind"], "react_article_system")
        self.assertEqual(target["publish_capability"], "direct")
        self.assertTrue(is_directly_publishable_target(target))
        reg = target["registration_strategy"]
        self.assertEqual(reg["registry_path"], "src/pages/articles/registry.ts")
        self.assertEqual(reg["seo_config_path"], "src/pages/articles/seo.ts")
        self.assertEqual(reg["author_registry_path"], "src/pages/articles/authorRegistry.ts")
        self.assertEqual(reg["author_alias_path"], "src/pages/articles/authors.ts")
        self.assertEqual(bundle["default_publish_target_id"], target["target_id"])

    def test_returns_empty_without_a_committed_registry(self):
        # No registry.ts among the managed files -> nothing to anchor a publish target on.
        self.assertEqual(
            react_article_system_target_from_setup_cache({"managed_files": ["src/App.tsx"]}), {}
        )
        self.assertEqual(react_article_system_target_from_setup_cache({}), {})
        self.assertEqual(react_article_system_target_from_setup_cache(None), {})


class PublishDisconnectWatermarkTests(SimpleTestCase):
    def test_unlinked_state_blocks_scan_relink(self):
        # After a user unlink (watermark set + targets cleared), a scan's bundle_only fallback must
        # not silently re-link a target.
        targets, default_id = preserve_registered_publish_targets(
            incoming_targets=BUNDLE_ONLY,
            incoming_default_id="bundle_only_article_directory_src_pages_articles_ts",
            existing_targets=[],
            existing_default_id=None,
            article_system={**EXISTING, "publish_disconnected_at": "2026-06-28T00:00:00Z"},
        )
        self.assertEqual(targets, [])
        self.assertIsNone(default_id)

    def test_no_watermark_allows_normal_flow(self):
        # Without the watermark, an empty existing + incoming bundle_only behaves as before.
        targets, _ = preserve_registered_publish_targets(
            incoming_targets=BUNDLE_ONLY,
            incoming_default_id="x",
            existing_targets=[],
            existing_default_id=None,
            article_system=EXISTING,
        )
        self.assertEqual(targets, BUNDLE_ONLY)
