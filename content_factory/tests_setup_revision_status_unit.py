"""A failed revision's saved terminal state wins over stale preview labels."""
from copy import deepcopy
from unittest.mock import patch

from django.test import SimpleTestCase
from django.utils import timezone
from workflow_runs.models import ContentFactoryRun

from . import vibe_marketing_views as views
from . import service_views


class SetupRevisionStatusTests(SimpleTestCase):
    def fixture_run(self, result=None):
        # Keep model defaults while avoiding any persistence or related reads.
        return ContentFactoryRun(run_id="revision-setup", workflow="article_system_setup", status="blocked",
            domain="fixture.test", current_step="revision_preview_building", error="Reconnect this website.",
            result=result or {"status": "revision_preview_building", "article_system_setup": {"status": "preview_building", "route_path": "/stories"}},
            created_at=timezone.now(), updated_at=timezone.now())

    def test_setup_metadata_reports_blocked_instead_of_preview_building(self):
        run = self.fixture_run()
        original = deepcopy(run.result)
        self.assertEqual(views._setup_metadata_from_run(run)["setupStatus"], "blocked")
        self.assertEqual(run.result, original)

    def test_browser_status_uses_terminal_step_without_rewriting_history(self):
        run = self.fixture_run()
        with patch.object(views, "_serialize_run_steps", return_value={}), patch.object(views, "_live_preview_from_run", return_value=None), \
                patch.object(views, "_workflow_progress_context", return_value=(None, None, [], {})):
            payload = views._serialize_run(run, mode="status", precomputed_article_setup_state={})
        self.assertEqual((payload["status"], payload["currentStep"]), ("blocked", "blocked"))
        self.assertEqual(run.current_step, "revision_preview_building")

    def test_worker_snapshot_also_projects_terminal_revision_phase(self):
        run = self.fixture_run()
        with patch.object(ContentFactoryRun, "steps", create=True) as steps:
            steps.order_by.return_value = []
            payload = service_views._serialize_content_factory_run(run)
        self.assertEqual(payload["current_step"], "blocked")
        self.assertEqual(payload["result"]["article_system_setup"]["status"], "blocked")

    def test_new_running_revision_retains_its_active_step(self):
        run = self.fixture_run()
        run.status = "running"
        self.assertEqual(views._setup_metadata_from_run(run)["setupStatus"], "running")
