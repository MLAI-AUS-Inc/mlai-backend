from django.test import TestCase

from vibe_raising.views import _run_failure_message
from workflow_runs.models import (
    ContentFactoryRun,
    ContentFactoryRunStatus,
    ContentFactoryRunStep,
    ContentFactoryStepStatus,
)


class RunFailureMessageTests(TestCase):
    def _create_run(self, **overrides) -> ContentFactoryRun:
        defaults = {
            "run_id": "startup-update-test-run",
            "workflow": "startup_monthly_update",
            "domain": "acme.com",
            "slack_user_id": "1",
            "status": ContentFactoryRunStatus.FAILED,
            "current_step": "profile_resolution",
            "step_order": ["profile_resolution"],
            "run_request": {},
            "result": {},
        }
        defaults.update(overrides)
        return ContentFactoryRun.objects.create(**defaults)

    def test_uses_run_error_when_present(self):
        run = self._create_run(error="Startup 16 data is deleted; refusing to process run.")

        self.assertEqual(
            _run_failure_message(run),
            "Startup 16 data is deleted; refusing to process run.",
        )

    def test_falls_back_to_failed_step_error(self):
        run = self._create_run(error="")
        ContentFactoryRunStep.objects.create(
            run=run,
            step_key="profile_resolution",
            status=ContentFactoryStepStatus.FAILED,
            error="Run is missing required startup context: google_connection_id",
        )

        self.assertEqual(
            _run_failure_message(run),
            "Run is missing required startup context: google_connection_id",
        )

    def test_generic_fallback_when_no_error_recorded(self):
        run = self._create_run(error="")

        self.assertEqual(_run_failure_message(run), "Draft generation failed. Please try again.")
        self.assertEqual(_run_failure_message(None), "Draft generation failed. Please try again.")
