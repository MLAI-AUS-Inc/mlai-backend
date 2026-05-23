import os
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import OperationalError
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from content_factory.models import ContentFactoryJob
from organizations.models import Organization
from startup_updates.models import UserStartupBinding
from workflow_runs.models import ContentFactoryRun

User = get_user_model()


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
        self.user = User.objects.create_user(email="test@example.com", password="password")

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

    def test_run_sync_retries_transient_sqlite_lock(self):
        payload = {
            "run_id": "run-sync-lock-1",
            "workflow": "repo_scan",
            "status": "queued",
            "step_states": {},
        }

        original_update_or_create = ContentFactoryRun.objects.update_or_create
        attempts = {"count": 0}

        def flaky_update_or_create(*args, **kwargs):
            if attempts["count"] == 0:
                attempts["count"] += 1
                raise OperationalError("database is locked")
            return original_update_or_create(*args, **kwargs)

        with patch("content_factory.service_views.ContentFactoryRun.objects.update_or_create", side_effect=flaky_update_or_create):
            with patch("content_factory.service_views.time.sleep"):
                response = self.client.put(
                    "/api/content-factory/runs/run-sync-lock-1/",
                    payload,
                    format="json",
                )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(ContentFactoryRun.objects.filter(run_id="run-sync-lock-1").exists())

    def test_run_sync_identical_payload_does_not_bump_updated_at(self):
        payload = {
            "run_id": "run-sync-unchanged-1",
            "workflow": "article_system_setup",
            "status": "blocked",
            "current_step": "verify_directory_browser",
            "artifact_root": "/tmp/content-factory-runs/run-sync-unchanged-1",
            "step_order": ["verify_directory_browser"],
            "acceptance_summary": {},
            "verification_summary": {},
            "approval_state": "not_required",
            "resume_available": True,
            "error": "Directory browser verification failed before setup approval.",
            "result": {
                "status": "preview_failed",
                "error": "Directory browser verification failed before setup approval.",
                "article_system_setup": {"status": "preview_failed"},
            },
            "run_request": {"domain": "mlai.au", "github_repo": "MLAI-AUS-Inc/mlai-au"},
            "step_states": {
                "verify_directory_browser": {
                    "name": "verify_directory_browser",
                    "required": True,
                    "status": "blocked",
                    "attempts": 1,
                    "message": "Directory browser verification failed.",
                    "attempt_history": [],
                }
            },
        }

        first = self.client.put(
            "/api/content-factory/runs/run-sync-unchanged-1/",
            payload,
            format="json",
        )
        run = ContentFactoryRun.objects.get(run_id="run-sync-unchanged-1")
        updated_at = run.updated_at
        second = self.client.put(
            "/api/content-factory/runs/run-sync-unchanged-1/",
            payload,
            format="json",
        )
        run.refresh_from_db()

        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertEqual(second.data["sync_status"], "unchanged")
        self.assertEqual(run.updated_at, updated_at)

    def test_callback_job_sync_retries_transient_sqlite_lock(self):
        original_update_or_create = ContentFactoryJob.objects.update_or_create
        attempts = {"count": 0}

        def flaky_update_or_create(*args, **kwargs):
            if attempts["count"] == 0:
                attempts["count"] += 1
                raise OperationalError("database is locked")
            return original_update_or_create(*args, **kwargs)

        with patch("content_factory.models.ContentFactoryJob.objects.update_or_create", side_effect=flaky_update_or_create):
            with patch("content_factory.service_views.time.sleep"):
                response = self.client.post(
                    "/api/content-factory/callback/",
                    {
                        "event_type": "discovery_progress",
                        "job_id": "callback-lock-1",
                        "domain": "acme.com",
                        "slack_user_id": "U123",
                        "milestone_key": "research",
                        "message": "Researching topics.",
                    },
                    format="json",
                )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(ContentFactoryJob.objects.filter(job_id="callback-lock-1").exists())

    def test_draft_results_returns_retryable_response_for_transient_sqlite_lock(self):
        organization = Organization.objects.create(name="Acme", domain="acme.com")
        binding = UserStartupBinding.objects.create(
            user=self.user,
            organization=organization,
            is_default_for_gmail=True,
        )
        ContentFactoryRun.objects.create(
            run_id="startup-update-lock-1",
            workflow="startup_monthly_update",
            domain="acme.com",
            status="running",
            current_step="draft_generation",
            step_order=["profile_resolution", "draft_generation", "groundedness_review"],
            run_request={
                "organization_id": organization.id,
                "binding_id": binding.id,
                "draft_months": ["2026-03-01"],
                "current_month": "2026-03-01",
            },
            result={},
        )

        with patch(
            "startup_updates.api_views.upsert_monthly_update_draft",
            side_effect=OperationalError("database is locked"),
        ):
            response = self.client.post(
                "/api/v1/integrations/startup-updates/runs/startup-update-lock-1/draft-results",
                {
                    "drafts": [
                        {
                            "month": "2026-03-01",
                            "status": "ready",
                            "structured_memo": {"title": "Acme Investor Update"},
                        }
                    ]
                },
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(response.data["error"], "transient_database_lock")
        self.assertTrue(response.data["retryable"])
        self.assertGreaterEqual(response.data["retry_after_seconds"], 1)
