import os

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from core.models import ContentFactoryHealingRecord, Organization


class ContentFactoryHealingRecordTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.api_key = "test_roo_key"
        os.environ["ROO_API_KEY"] = self.api_key
        os.environ["INTERNAL_API_KEY"] = self.api_key

        from django.conf import settings

        settings.ROO_API_KEY = self.api_key
        settings.INTERNAL_API_KEY = self.api_key
        self.client.credentials(HTTP_X_API_KEY=self.api_key)
        self.organization = Organization.objects.create(name="MLAI", domain="mlai.au")

    def test_healing_record_post_creates_and_updates(self):
        payload = {
            "domain": "mlai.au",
            "github_repo": "MLAI-AUS-Inc/mlai-au",
            "failure_kind": "repo_code_build",
            "failure_family_key": "vite_transform_tsx_module",
            "exact_signature": "sig-123",
            "summary": "Vite transform failures for TSX article modules.",
            "normalized_failure": {"stage": "build", "root_cause": "vite_transform_failure"},
            "changed_files": ["app/articles/content/startups/what-are-startups.tsx"],
            "patch_manifest": {"files": ["app/articles/content/startups/what-are-startups.tsx"]},
            "validation_results": {"verify_build": {"status": "passed"}},
            "evidence_artifacts": {"build_log_path": "/tmp/build.log"},
            "snippet_or_rule": "Render JSON-LD with dangerouslySetInnerHTML and plain-string FAQ answers.",
            "applies_to": ["article_module"],
            "promotion_state": "candidate",
            "latest_run_id": "run-123",
        }

        response = self.client.post(
            "/api/content-factory/healing-records/",
            payload,
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["sync_status"], "created")
        self.assertEqual(ContentFactoryHealingRecord.objects.count(), 1)

        update_response = self.client.post(
            "/api/content-factory/healing-records/",
            {
                **payload,
                "promotion_state": "promoted",
                "validation_results": {
                    "verify_build": {"status": "passed"},
                    "verify_browser": {"status": "passed"},
                },
            },
            format="json",
        )
        self.assertEqual(update_response.status_code, status.HTTP_200_OK)
        self.assertEqual(update_response.data["sync_status"], "updated")
        self.assertEqual(ContentFactoryHealingRecord.objects.count(), 1)

        record = ContentFactoryHealingRecord.objects.get()
        self.assertEqual(record.organization, self.organization)
        self.assertEqual(record.promotion_state, "promoted")
        self.assertEqual(record.validation_results["verify_browser"]["status"], "passed")

    def test_healing_record_get_filters_by_repo_and_state(self):
        ContentFactoryHealingRecord.objects.create(
            organization=self.organization,
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/mlai-au",
            failure_kind="repo_code_build",
            failure_family_key="vite_transform_tsx_module",
            promotion_state="promoted",
        )
        ContentFactoryHealingRecord.objects.create(
            organization=self.organization,
            domain="mlai.au",
            github_repo="MLAI-AUS-Inc/other-repo",
            failure_kind="repo_code_build",
            failure_family_key="other_family",
            promotion_state="candidate",
        )

        response = self.client.get(
            "/api/content-factory/healing-records/",
            {
                "domain": "mlai.au",
                "github_repo": "MLAI-AUS-Inc/mlai-au",
                "promotion_state": "promoted",
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["failure_family_key"], "vite_transform_tsx_module")
