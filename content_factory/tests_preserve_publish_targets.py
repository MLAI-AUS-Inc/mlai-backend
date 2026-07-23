"""A routine scan that re-derives no publish target must not silently unlink an
already-registered one.

The scaffold registers a high-confidence publish target at setup time, but the
generic scan detector frequently cannot re-derive that target from the repo alone
(chicken-and-egg). Before this guard, such a scan posted ``publish_targets=[]`` and
the callback overwrote the stored target, killing publishing while ``/articles``
stayed live. ``preserve_registered_publish_targets`` keeps the registered target
when the surface is still present, while still honouring a genuine fresh detection
and a real removal.
"""
from django.test import SimpleTestCase

from content_factory.article_system import (
    is_bundle_only_fallback_target,
    is_directly_publishable_target,
    preserve_registered_publish_targets,
)

REGISTERED = [{"target_id": "react_article_system_articles_{slug}_tsx__tsx", "confidence": "high"}]
FRESH = [{"target_id": "fresh_target", "confidence": "high"}]
READY = {"state": "roo_scaffolded", "confidence": "high", "directory_path": "articles"}
EXISTING = {"state": "existing", "confidence": "high", "directory_path": "articles"}
GONE = {"state": "missing", "confidence": "high"}

# Targets that carry a real publish capability (the vyavos regression fixtures).
REGISTERED_DIRECT = [
    {
        "target_id": "react_article_system_src_pages_articles_{category}_{slug}_tsx__tsx",
        "kind": "react_article_system",
        "delivery_adapter": "react_component",
        "publish_capability": "direct",
    }
]
FRESH_DIRECT = [
    {
        "target_id": "react_article_system_fresh",
        "kind": "react_article_system",
        "publish_capability": "direct",
    }
]
BUNDLE_ONLY = [
    {
        "target_id": "bundle_only_article_directory_src_pages_articles_ts",
        "kind": "bundle_only_article_directory",
        "publish_capability": "bundle_only",
    }
]


class PreserveRegisteredPublishTargetsTests(SimpleTestCase):
    def test_empty_scan_keeps_registered_target_when_surface_ready(self):
        # The exact regression: scan finds the page but no target; keep the old one.
        targets, default_id = preserve_registered_publish_targets(
            incoming_targets=[],
            incoming_default_id=None,
            existing_targets=REGISTERED,
            existing_default_id="react_article_system_articles_{slug}_tsx__tsx",
            article_system=EXISTING,
        )
        self.assertEqual(targets, REGISTERED)
        self.assertEqual(default_id, "react_article_system_articles_{slug}_tsx__tsx")

    def test_empty_scan_keeps_registered_target_for_roo_scaffolded(self):
        targets, default_id = preserve_registered_publish_targets(
            incoming_targets=[],
            incoming_default_id=None,
            existing_targets=REGISTERED,
            existing_default_id="kept",
            article_system=READY,
        )
        self.assertEqual(targets, REGISTERED)
        self.assertEqual(default_id, "kept")

    def test_fresh_scan_target_wins(self):
        # A non-empty detection is authoritative and replaces the stored target.
        targets, default_id = preserve_registered_publish_targets(
            incoming_targets=FRESH,
            incoming_default_id="fresh_target",
            existing_targets=REGISTERED,
            existing_default_id="old",
            article_system=EXISTING,
        )
        self.assertEqual(targets, FRESH)
        self.assertEqual(default_id, "fresh_target")

    def test_surface_gone_clears_target(self):
        # Genuine removal: no surface, no incoming target -> clear.
        targets, default_id = preserve_registered_publish_targets(
            incoming_targets=[],
            incoming_default_id=None,
            existing_targets=REGISTERED,
            existing_default_id="old",
            article_system=GONE,
        )
        self.assertEqual(targets, [])
        self.assertIsNone(default_id)

    def test_no_existing_target_is_noop(self):
        targets, default_id = preserve_registered_publish_targets(
            incoming_targets=[],
            incoming_default_id=None,
            existing_targets=[],
            existing_default_id=None,
            article_system=EXISTING,
        )
        self.assertEqual(targets, [])
        self.assertIsNone(default_id)

    def test_bundle_only_rescan_keeps_registered_direct_target(self):
        # The vyavos regression: a re-scan can only re-derive a weaker bundle_only fallback for an
        # RR v6 Vite SPA, which would downgrade a live, directly-publishable surface to content_only.
        # Keep the registered direct target instead.
        targets, default_id = preserve_registered_publish_targets(
            incoming_targets=BUNDLE_ONLY,
            incoming_default_id="bundle_only_article_directory_src_pages_articles_ts",
            existing_targets=REGISTERED_DIRECT,
            existing_default_id="react_article_system_src_pages_articles_{category}_{slug}_tsx__tsx",
            article_system=EXISTING,
        )
        self.assertEqual(targets, REGISTERED_DIRECT)
        self.assertEqual(default_id, "react_article_system_src_pages_articles_{category}_{slug}_tsx__tsx")

    def test_capable_incoming_replaces_registered_direct_target(self):
        # A genuinely better detection (its own direct target) is still authoritative.
        targets, default_id = preserve_registered_publish_targets(
            incoming_targets=FRESH_DIRECT,
            incoming_default_id="react_article_system_fresh",
            existing_targets=REGISTERED_DIRECT,
            existing_default_id="old",
            article_system=EXISTING,
        )
        self.assertEqual(targets, FRESH_DIRECT)
        self.assertEqual(default_id, "react_article_system_fresh")

    def test_bundle_only_rescan_wins_when_existing_not_publish_capable(self):
        # No registered direct target to protect -> the bundle_only detection is accepted.
        targets, default_id = preserve_registered_publish_targets(
            incoming_targets=BUNDLE_ONLY,
            incoming_default_id="bundle_only_article_directory_src_pages_articles_ts",
            existing_targets=BUNDLE_ONLY,
            existing_default_id="bundle_only_article_directory_src_pages_articles_ts",
            article_system=EXISTING,
        )
        self.assertEqual(targets, BUNDLE_ONLY)

    def test_bundle_only_rescan_wins_when_surface_gone(self):
        # If the surface is genuinely gone, the registered direct target is stale -> accept incoming.
        targets, default_id = preserve_registered_publish_targets(
            incoming_targets=BUNDLE_ONLY,
            incoming_default_id="bundle_only_article_directory_src_pages_articles_ts",
            existing_targets=REGISTERED_DIRECT,
            existing_default_id="old",
            article_system=GONE,
        )
        self.assertEqual(targets, BUNDLE_ONLY)

    def test_is_directly_publishable_target_classification(self):
        self.assertTrue(is_directly_publishable_target(REGISTERED_DIRECT[0]))
        self.assertFalse(is_directly_publishable_target(BUNDLE_ONLY[0]))
        # A target with no capability marker is not assumed directly publishable.
        self.assertFalse(is_directly_publishable_target({"target_id": "x"}))

    def test_is_bundle_only_fallback_target_classification(self):
        # The denylist the readiness gate uses: bundle-only fallbacks are rejected,
        # but hook and capability-less legacy targets are NOT (they stay a publish path).
        self.assertTrue(is_bundle_only_fallback_target(BUNDLE_ONLY[0]))
        self.assertFalse(is_bundle_only_fallback_target(REGISTERED_DIRECT[0]))
        self.assertFalse(is_bundle_only_fallback_target({"kind": "hook_publish_target", "publish_capability": "hook"}))
        self.assertFalse(is_bundle_only_fallback_target({"target_id": "articles", "kind": "react_article_system"}))
        self.assertFalse(is_bundle_only_fallback_target(None))
