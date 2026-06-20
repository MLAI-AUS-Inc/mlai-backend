"""A stray/abandoned article_system_setup run must not resurface as a phantom saved
setup when nothing is pinned (no pending). Previously the empty-id fallback in
_latest_article_system_setup_run / _latest_persisted_run_for_article_setup returned
"the latest setup run for the org", so an old preview_failed run kept reappearing as
setupRunId / setupBlocked and rendered the wizard's "saved articles/blogs setup" card.
"""
from django.test import TestCase

from content_factory.article_system import resolve_article_system
from content_factory.models import OrganizationContentConfig
from content_factory.vibe_marketing_views import (
    _article_setup_state_for_config,
    _article_system_setup_gate,
    _latest_article_system_setup_run,
)
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun

REPO = "The-Product-Bus/tpbnewsite"


class EmptyIdSetupFallbackTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(domain="theproductbus.com", name="TPB")
        self.cfg = OrganizationContentConfig.objects.create(organization=self.org, github_repo=REPO)

    def _setup_run(self, run_id, status="failed"):
        return ContentFactoryRun.objects.create(
            domain=self.org.domain,
            run_id=run_id,
            workflow="article_system_setup",
            github_repo=REPO,
            status=status,
            result={"article_system_setup": {"setup_run_id": run_id, "status": "preview_failed"}},
        )

    # --- the helper ---
    def test_empty_id_returns_none_even_with_runs_present(self):
        run = self._setup_run("stray-1")
        self.assertIsNone(_latest_article_system_setup_run([run], setup_run_id=""))

    def test_matched_pinned_id_returns_the_run(self):
        run = self._setup_run("pinned-1", status="running")
        self.assertEqual(_latest_article_system_setup_run([run], setup_run_id="pinned-1"), run)

    def test_unmatched_id_returns_none(self):
        run = self._setup_run("stray-1")
        self.assertIsNone(_latest_article_system_setup_run([run], setup_run_id="nope"))

    # --- the end-to-end cure ---
    def test_stray_run_no_pending_does_not_resurface(self):
        self._setup_run("a009c26c")  # the real-world phantom
        st = _article_setup_state_for_config(self.cfg, organization=self.org)
        self.assertIsNone(st.get("setupRunId"))
        self.assertFalse(st.get("setupBlocked"))

        latest = list(ContentFactoryRun.objects.filter(domain__iexact=self.org.domain))
        gate = _article_system_setup_gate(self.cfg, latest, resolve_article_system(self.cfg))
        self.assertIsNone(gate.get("setupRunId"))
        self.assertFalse(gate.get("setupBlocked"))

    def test_pinned_pending_setup_still_resolves(self):
        # A genuine in-progress setup (pinned in pending) must still surface — no regression.
        self._setup_run("active-1", status="running")
        self.cfg.article_system = {
            "state": "missing",
            "pending_article_system_setup": {
                "setupRunId": "active-1",
                "setup_run_id": "active-1",
                "status": "running",
            },
        }
        self.cfg.save(update_fields=["article_system", "updated_at"])
        st = _article_setup_state_for_config(self.cfg, organization=self.org)
        self.assertEqual(st.get("setupRunId"), "active-1")
