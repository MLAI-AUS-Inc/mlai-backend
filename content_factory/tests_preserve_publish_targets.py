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

from content_factory.article_system import preserve_registered_publish_targets

REGISTERED = [{"target_id": "react_article_system_articles_{slug}_tsx__tsx", "confidence": "high"}]
FRESH = [{"target_id": "fresh_target", "confidence": "high"}]
READY = {"state": "roo_scaffolded", "confidence": "high", "directory_path": "articles"}
EXISTING = {"state": "existing", "confidence": "high", "directory_path": "articles"}
GONE = {"state": "missing", "confidence": "high"}


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
