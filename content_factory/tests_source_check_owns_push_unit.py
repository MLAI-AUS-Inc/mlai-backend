"""A customer push to a live-URL website is re-verified by its own source check.

MLAI (17 Nov 2026): each push reopened the finished preparation, which adopted
the push's source check, ran an inventory scan (no build proof) and then checked
the live page against the older verified commit. GitHub's branch had moved, so
it stopped with website_source_changed and verified_sha never advanced. These
run without a database or provider access.
"""
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from . import vibe_marketing_views  # noqa: F401 -- decorates with transaction.atomic at import; load before it is patched
from . import website_prepare as prepare
from . import website_reconciliation as reconciliation
from .tests_stopped_prepare_source_unit import SourceWakeTests


def connection(target):
    rows = Mock()
    rows.filter.return_value.first.return_value = target
    return SimpleNamespace(generation=3, targets=rows)


CONFIG = SimpleNamespace(default_publish_target_id="articles")


class SourceCheckOwnsPushTests(SimpleTestCase):
    def owns(self, state="completed", *, live=True, target=object(), config=CONFIG):
        latest = SimpleNamespace(state=state) if state else None
        with patch.object(reconciliation, "_renews_by_live_url", return_value=live) as renews:
            result = prepare._source_check_owns_push(connection(target), config, latest)
        return result, renews

    def test_a_finished_live_url_preparation_leaves_the_push_to_its_source_check(self):
        for state in ("completed", "needs_attention"):
            with self.subTest(state=state):
                self.assertTrue(self.owns(state)[0])

    def test_preparation_in_progress_still_reopens(self):
        for state in ("pending", "running", "needs_user", "stopped", "cancelled", None):
            with self.subTest(state=state):
                self.assertFalse(self.owns(state)[0])

    def test_ci_verified_websites_keep_the_preparation_recheck(self):
        self.assertFalse(self.owns(live=False)[0])

    def test_no_selected_target_keeps_the_preparation_recheck(self):
        self.assertFalse(self.owns(target=None)[0])
        result, renews = self.owns(config=SimpleNamespace(default_publish_target_id=""))
        self.assertFalse(result)
        renews.assert_not_called()

    @override_settings(ARTICLES_SOURCE_CHECK_OWNS_PUSH_ENABLED=False)
    def test_the_kill_switch_restores_the_reopen(self):
        self.assertFalse(self.owns()[0])


class SourceWakeLeavesCheckTests(SourceWakeTests):
    # Sticky stops (plan B3) hold this push earlier; this is #1365's own rule.
    @override_settings(ARTICLES_PREPARE_STICKY_STOP_ENABLED=False)
    def test_customer_push_does_not_reopen_or_adopt(self):
        self.op.state = "needs_attention"
        with patch.object(prepare, "_source_check_owns_push", return_value=True), \
                patch.object(prepare, "start_prepare") as start:
            with self.assertRaises(StopIteration):
                # No reopen update is attempted at all.
                self.wake()
        start.assert_not_called()
        self.website.operations.filter.assert_called_once_with(generation=3, action="prepare")
        self.website.operations.filter.return_value.update.assert_not_called()

    def test_mlai_setup_merge_still_reopens_to_verify_it(self):
        with patch.object(prepare, "_source_check_owns_push", return_value=True) as owns:
            result, lookups, values = self.wake("merged-setup")
        self.assertIs(result, self.op)
        self.assertEqual(values["receipt"]["merged_setup_run_id"], "merged-setup")
        owns.assert_not_called()
