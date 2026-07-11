"""The setup wizard's Publish step must read as an explicit call to action while
the setup PR is created-but-unmerged — NOT as a background "running/publishing on
its own" state.

Golden-repo baseline (2026-07-11, both legs): after "Approve setup and create PR",
the PR sits OPEN indefinitely while the wizard claimed it was "publishing... this
page updates on its own". The truth is the Publish button IS the merge trigger, so
a founder who trusts that copy waits forever. Before this fix, a ``pr_created`` /
``setup_pr_created`` setup fell through _build_workflow_progress's else-branch to
generate="running" ("preparing the preview and setup PR").
"""
from django.test import TestCase

from content_factory.models import OrganizationContentConfig
from content_factory.vibe_marketing_views import _workflow_progress
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun

REPO = "drsamdonegan/golden-next-baseline"
PR_URL = "https://github.com/drsamdonegan/golden-next-baseline/pull/1"


def _step(progress, step_id):
    return next(step for step in progress["steps"] if step["id"] == step_id)


class SetupPublishCardStateTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(domain="golden-next.example", name="Golden Next")
        self.cfg = OrganizationContentConfig.objects.create(organization=self.org, github_repo=REPO)

    def _pr_created_run(self, run_id="setup-pr-1", *, setup_status="pr_created", merge_status="not_merged", run_status="completed"):
        run = ContentFactoryRun.objects.create(
            domain=self.org.domain,
            run_id=run_id,
            workflow="article_system_setup",
            github_repo=REPO,
            status=run_status,
            current_step="create_pull_request",
            result={
                # Mirror the real stamps: the approve handler writes the run-level
                # status as "setup_pr_created" for a pr_created PR; the auto-merge /
                # checks-failed marks write the actual status through.
                "status": "setup_pr_created" if setup_status in {"pr_created", "setup_pr_created"} else setup_status,
                "setup_status": setup_status,
                "pr_url": PR_URL,
                "pr_number": 1,
                "article_system_setup": {
                    "setup_run_id": run_id,
                    "status": setup_status,
                    "setupStatus": setup_status,
                    "merge_status": merge_status,
                    "mergeStatus": merge_status,
                    "pr_url": PR_URL,
                    "pr_number": 1,
                    "current_step": "create_pull_request",
                },
            },
        )
        self.cfg.article_system = {
            "state": "missing",
            "pending_article_system_setup": {
                "setupRunId": run_id,
                "setup_run_id": run_id,
                "status": setup_status,
                "setupStatus": setup_status,
                "mergeStatus": merge_status,
                "merge_status": merge_status,
                "prUrl": PR_URL,
                "pr_url": PR_URL,
                "prNumber": 1,
                "pr_number": 1,
            },
        }
        self.cfg.save(update_fields=["article_system", "updated_at"])
        return run

    def test_pr_created_publish_step_is_ready_to_publish_not_running(self):
        run = self._pr_created_run()
        progress = _workflow_progress(run=run)

        publish = _step(progress, "publish")
        self.assertEqual(publish["status"], "needs_action")
        self.assertIn("Ready to publish", publish["summary"])
        self.assertIn("merge the setup PR", publish["summary"])
        self.assertNotIn("on its own", publish["summary"].lower())
        # The setup PR only moves when Publish is clicked — the action must lead there.
        self.assertTrue(publish.get("primaryAction"))

        # Generate/review are done — the old else-branch wrongly showed generate="running"
        # ("preparing the preview and setup PR") over an already-created PR.
        self.assertEqual(_step(progress, "generate")["status"], "complete")
        self.assertEqual(_step(progress, "review")["status"], "complete")

    def test_setup_pr_created_status_alias_also_ready(self):
        # content-factory stamps the run-level status as ``setup_pr_created`` while the
        # nested setup payload carries ``pr_created``; both must land on the same UI.
        run = self._pr_created_run(run_id="setup-pr-2", setup_status="setup_pr_created")
        progress = _workflow_progress(run=run)
        publish = _step(progress, "publish")
        self.assertEqual(publish["status"], "needs_action")
        self.assertIn("Ready to publish", publish["summary"])

    def test_publishing_status_reads_running_not_preparing(self):
        # Native auto-merge armed (merge_status "publishing") while the setup run is
        # still unsettled: this IS in flight (GitHub merges once checks pass), but the
        # old else-branch mislabeled it "preparing the preview and setup PR" (pre-PR
        # work). It must read as an honest publish-in-progress, not early setup.
        run = self._pr_created_run(
            run_id="setup-pub-1",
            setup_status="publishing",
            merge_status="publishing",
            run_status="awaiting_approval",
        )
        progress = _workflow_progress(run=run)
        publish = _step(progress, "publish")
        self.assertEqual(publish["status"], "running")
        self.assertIn("GitHub merges automatically", publish["summary"])
        # Not the else-branch "preparing the preview and setup PR" over generate.
        self.assertEqual(_step(progress, "generate")["status"], "complete")

    def test_checks_failed_reads_retry_not_running_spinner(self):
        # A created PR whose checks failed the merge is BLOCKED awaiting a manual retry,
        # not doing background work. The old else-branch showed a running "preparing the
        # setup PR" spinner on the dashboard while the run-page card correctly said
        # blocked/Retry — the same false-progress contradiction, one status over.
        run = self._pr_created_run(
            run_id="setup-cf-1",
            setup_status="checks_failed",
            merge_status="checks_failed",
            run_status="awaiting_approval",
        )
        progress = _workflow_progress(run=run)
        publish = _step(progress, "publish")
        self.assertEqual(publish["status"], "needs_action")
        self.assertNotIn("preparing", (_step(progress, "generate")["summary"] or "").lower())
        self.assertEqual(_step(progress, "generate")["status"], "complete")
