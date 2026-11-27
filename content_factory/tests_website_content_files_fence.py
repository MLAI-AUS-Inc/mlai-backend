"""A merged lower-tier setup callback never marks the site generation ready (service_views call site)."""
from unittest.mock import patch

from django.test import TestCase

from . import service_views


class MergedSetupCallbackFenceTests(TestCase):
    def _complete(self, run_id, setup):
        data = {"run_id": run_id, "domain": "fence.example.test", "github_repo": "acme/site",
                "merge_status": "merged", "status": "completed", "article_system_setup": setup,
                "pr_url": "https://github.com/acme/site/pull/3"}
        with patch.object(service_views, "_mark_article_system_setup_generation_ready_for_domain") as ready:
            run = service_views._sync_article_system_setup_callback_to_run(
                data=data, event_type="article_system_setup_completed")
        self.assertIsNotNone(run)
        return ready

    def test_a_merged_agentic_setup_is_not_marked_generation_ready(self):
        ladder = {"schema_version": 1, "tier": "agentic", "tier_number": 2, "outcome": "awaiting_review"}
        self._complete("fence-agentic", {"setup_ladder": ladder}).assert_not_called()

    def test_lower_tiers_behave_as_today(self):
        for tier in ("static_dropin", "content_files"):
            with self.subTest(tier=tier):
                self._complete(f"fence-{tier}", {"setup_ladder": {"tier": tier}}).assert_not_called()

    def test_a_merged_setup_with_no_ladder_receipt_is_marked_generation_ready_as_today(self):
        ready = self._complete("fence-plain", {"status": "merged"})
        ready.assert_called_once()
        self.assertEqual(ready.call_args.args, ("fence.example.test",))
