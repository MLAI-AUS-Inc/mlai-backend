import os

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from core.models import ContentFactoryRun


class ContentFactoryRunSyncTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.api_key = "test_roo_key"
        os.environ["ROO_API_KEY"] = self.api_key
        os.environ["INTERNAL_API_KEY"] = self.api_key

        from django.conf import settings

        settings.ROO_API_KEY = self.api_key
        settings.INTERNAL_API_KEY = self.api_key
        self.client.credentials(HTTP_X_API_KEY=self.api_key)

    def test_run_sync_accepts_reduced_or_extended_payload(self):
        payload = {
            "run_id": "run-sync-1",
            "workflow": "repo_scan",
            "status": "queued",
            "current_step": "load_repo_context",
            "artifact_root": "/tmp/content-factory-runs/run-sync-1",
            "step_order": ["load_repo_context", "finalize"],
            "started_at": "2026-03-08T09:05:57+00:00",
            "updated_at": "2026-03-08T09:05:58+00:00",
            "acceptance_summary": {"build_verified": False},
            "verification_summary": {},
            "approval_state": "not_required",
            "resume_available": True,
            "error": None,
            "result": None,
            "run_request": {"domain": "mlai.au", "github_repo": "MLAI-AUS-Inc/mlai-au"},
            "run_request_path": "/tmp/content-factory-runs/run-sync-1/RUN_REQUEST.json",
            "step_states": {
                "load_repo_context": {
                    "name": "load_repo_context",
                    "required": True,
                    "status": "completed",
                    "attempts": 1,
                    "attempt_history": [],
                }
            },
        }

        response = self.client.put(
            "/api/content-factory/runs/run-sync-1/",
            payload,
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        run = ContentFactoryRun.objects.get(run_id="run-sync-1")
        self.assertEqual(run.workflow, "repo_scan")
        self.assertEqual(run.run_request.get("domain"), "mlai.au")
