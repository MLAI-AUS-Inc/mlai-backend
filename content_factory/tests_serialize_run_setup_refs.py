from django.test import TestCase

from content_factory.vibe_marketing_views import _strip_missing_setup_run_refs
from workflow_runs.models import ContentFactoryRun


class StripMissingSetupRunRefsTests(TestCase):
    """Guards the bootstrap/run serializer against handing the wizard a setup run id
    that has since been deleted (teardown/reset), which the frontend would poll
    /runs/<id>/status against -> 404 -> SSR 500."""

    def test_nulls_refs_to_missing_runs_without_mutating_input(self):
        result = {
            "scaffold_job_id": "dead-run",
            "result": {
                "setup_run_id": "dead-run",
                "live_preview_url": "/api/runs/dead-run/live-preview",
                "article_system_setup": {
                    "setup_run_id": "dead-run",
                    "live_preview_url": "/api/runs/dead-run/live-preview",
                },
            },
        }
        out = _strip_missing_setup_run_refs(result)

        self.assertIsNone(out["scaffold_job_id"])
        self.assertIsNone(out["result"]["setup_run_id"])
        self.assertIsNone(out["result"]["live_preview_url"])
        self.assertIsNone(out["result"]["article_system_setup"]["setup_run_id"])
        self.assertIsNone(out["result"]["article_system_setup"]["live_preview_url"])
        # Works on a copy: the live ORM result dict must be left untouched.
        self.assertEqual(result["scaffold_job_id"], "dead-run")
        self.assertEqual(result["result"]["setup_run_id"], "dead-run")

    def test_preserves_refs_to_existing_runs(self):
        ContentFactoryRun.objects.create(run_id="live-run", workflow="article_system_setup", status="completed")
        result = {"result": {"setup_run_id": "live-run", "scaffold_job_id": "live-run"}}
        out = _strip_missing_setup_run_refs(result)
        self.assertEqual(out["result"]["setup_run_id"], "live-run")
        self.assertEqual(out["result"]["scaffold_job_id"], "live-run")
        # Nothing missing -> returns the same object (no copy, no churn).
        self.assertIs(out, result)

    def test_mixed_existing_and_missing(self):
        ContentFactoryRun.objects.create(run_id="live-run", workflow="article_system_setup", status="completed")
        result = {"setup_run_id": "live-run", "scaffold_job_id": "dead-run"}
        out = _strip_missing_setup_run_refs(result)
        self.assertEqual(out["setup_run_id"], "live-run")
        self.assertIsNone(out["scaffold_job_id"])

    def test_no_setup_refs_returns_input_unchanged(self):
        result = {"preview_url": "https://x", "pr_url": "https://y"}
        out = _strip_missing_setup_run_refs(result)
        self.assertIs(out, result)

    def test_non_dict_input_is_passthrough(self):
        self.assertEqual(_strip_missing_setup_run_refs(None), None)
        self.assertEqual(_strip_missing_setup_run_refs("x"), "x")
