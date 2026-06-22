"""A setup-run reference can outlive its run: an article-setup teardown deletes the
``article_system_setup`` run, but its id survives inside the latest ``repo_scan``
run's result (e.g. ``article_system_setup.setup_run_id`` /
``article_system_setup_cache.setup_run_id``). The bootstrap gate seeds setup metadata
from that scan result, so the dead id reached the wizard, which navigated to
``/runs/<id>/status`` -> backend 404 -> the mlai.au run-status loader turned the 404
into a 500.

The fix drops a seeded ``setupRunId`` (and its ``livePreviewUrl``) when it resolves to
no live run AND no longer exists in the DB, while preserving ids whose run still exists.
"""
from django.test import TestCase

from content_factory.article_system import resolve_article_system
from content_factory.models import OrganizationContentConfig
from content_factory.vibe_marketing_views import (
    _article_setup_state_for_config,
    _article_system_setup_gate,
    _content_factory_run_exists,
)
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun

REPO = "woofya/woofya-articles"
DEAD_ID = "90cc7a45-a3a4-4d07-87ab-300fd1573d63"  # a deleted article_system_setup run
LIVE_ID = "11111111-2222-3333-4444-555555555555"


class DanglingSetupRunRefTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(domain="woofya.com", name="Woofya")
        self.cfg = OrganizationContentConfig.objects.create(organization=self.org, github_repo=REPO)

    def _scan_pointing_at(self, setup_run_id):
        # A repo_scan whose result carries a setup-run pointer plus a pending-generation
        # marker (requested_action), so the gate seeds setup metadata from it — exactly
        # how the production scan result that triggered the 500 was shaped.
        return ContentFactoryRun.objects.create(
            domain=self.org.domain,
            run_id="scan-1",
            workflow="repo_scan",
            github_repo=REPO,
            status="completed",
            result={
                "article_system_setup": {
                    "requested_action": "article_system_setup",
                    "setup_run_id": setup_run_id,
                    "live_preview_url": f"/api/runs/{setup_run_id}/live-preview",
                    "status": "preview_failed",
                }
            },
        )

    def _live_setup_run(self, run_id):
        return ContentFactoryRun.objects.create(
            domain=self.org.domain,
            run_id=run_id,
            workflow="article_system_setup",
            github_repo=REPO,
            status="running",
            result={"article_system_setup": {"setup_run_id": run_id, "status": "running"}},
        )

    # --- helper ---
    def test_run_exists_helper(self):
        self.assertFalse(_content_factory_run_exists(DEAD_ID))
        self.assertFalse(_content_factory_run_exists(""))
        self._live_setup_run(LIVE_ID)
        self.assertTrue(_content_factory_run_exists(LIVE_ID))

    # --- the cure: a scan-seeded id whose run no longer exists is dropped ---
    def test_dangling_setup_run_ref_is_dropped(self):
        self._scan_pointing_at(DEAD_ID)  # no article_system_setup run with DEAD_ID exists
        latest = list(ContentFactoryRun.objects.filter(domain__iexact=self.org.domain))

        gate = _article_system_setup_gate(self.cfg, latest, resolve_article_system(self.cfg))
        self.assertIsNone(gate.get("setupRunId"))
        self.assertIsNone(gate.get("livePreviewUrl"))

        st = _article_setup_state_for_config(self.cfg, latest_runs=latest, organization=self.org)
        self.assertIsNone(st.get("setupRunId"))
        self.assertIsNone(st.get("livePreviewUrl"))

    # --- no regression: a scan-seeded id whose run still exists is preserved ---
    def test_live_setup_run_ref_is_preserved(self):
        self._live_setup_run(LIVE_ID)
        self._scan_pointing_at(LIVE_ID)
        latest = list(ContentFactoryRun.objects.filter(domain__iexact=self.org.domain))

        gate = _article_system_setup_gate(self.cfg, latest, resolve_article_system(self.cfg))
        self.assertEqual(gate.get("setupRunId"), LIVE_ID)

    # --- no over-scrub: a run that exists but is absent from latest_runs is preserved ---
    def test_existing_run_absent_from_latest_runs_is_preserved(self):
        self._live_setup_run(LIVE_ID)
        scan = self._scan_pointing_at(LIVE_ID)
        # Pass only the scan in latest_runs; the live setup run exists in the DB but is
        # not in the list, so resolution-by-list misses it — the DB existence check must
        # still preserve the id rather than treating it as dangling.
        gate = _article_system_setup_gate(self.cfg, [scan], resolve_article_system(self.cfg))
        self.assertEqual(gate.get("setupRunId"), LIVE_ID)
