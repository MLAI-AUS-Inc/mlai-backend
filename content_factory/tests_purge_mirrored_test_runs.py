from __future__ import annotations

from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStatus


class PurgeMirroredTestRunsCommandTest(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="MLAI", domain="mlai.au")

    def _run(self, run_id, *, status=ContentFactoryRunStatus.COMPLETED, result=None):
        return ContentFactoryRun.objects.create(
            run_id=run_id,
            workflow="article_generation",
            domain="mlai.au",
            status=status,
            result=result or {},
        )

    def test_dry_run_lists_but_deletes_nothing(self):
        # A leaked test run carrying simulated PR evidence (undeletable via UI).
        self._run(
            "run-publish-materialized-bundle-1",
            result={"pull_request_url": "https://github.com/x/y/pull/17"},
        )
        out = StringIO()
        call_command("purge_mirrored_test_runs", stdout=out)
        output = out.getvalue()
        self.assertIn("run-publish-materialized-bundle-1", output)
        self.assertIn("PR_EVIDENCE", output)
        self.assertIn("DRY RUN", output)
        self.assertTrue(
            ContentFactoryRun.objects.filter(run_id="run-publish-materialized-bundle-1").exists()
        )

    def test_apply_deletes_only_test_runs(self):
        self._run(
            "run-publish-materialized-bundle-1",
            result={"pull_request_url": "https://github.com/x/y/pull/17"},
        )
        # Production-shaped run ids must be left untouched.
        self._run("3b627eba-0000-4000-8000-000000000000")
        self._run("vibe-article:1:deadbeef")

        out = StringIO()
        call_command("purge_mirrored_test_runs", "--apply", stdout=out)

        self.assertFalse(
            ContentFactoryRun.objects.filter(run_id="run-publish-materialized-bundle-1").exists()
        )
        self.assertTrue(
            ContentFactoryRun.objects.filter(run_id="3b627eba-0000-4000-8000-000000000000").exists()
        )
        self.assertTrue(ContentFactoryRun.objects.filter(run_id="vibe-article:1:deadbeef").exists())

    def test_explicit_run_id_selection_overrides_prefix(self):
        self._run("run-a")
        self._run("run-b")
        out = StringIO()
        call_command("purge_mirrored_test_runs", "--run-id", "run-a", "--apply", stdout=out)
        self.assertFalse(ContentFactoryRun.objects.filter(run_id="run-a").exists())
        self.assertTrue(ContentFactoryRun.objects.filter(run_id="run-b").exists())

    def test_empty_prefix_is_refused(self):
        from django.core.management.base import CommandError

        self._run("run-a")
        with self.assertRaises(CommandError):
            call_command("purge_mirrored_test_runs", "--run-id-prefix", "", "--apply")
        self.assertTrue(ContentFactoryRun.objects.filter(run_id="run-a").exists())
