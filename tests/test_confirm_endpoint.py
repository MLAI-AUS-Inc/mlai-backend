import os
import uuid
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APIClient

from core.models import ContentFactoryJob, User
from roo.models import Ledger


@override_settings(ROO_API_KEY="test-key", INTERNAL_API_KEY="internal-key", MLAI_API_KEY="mlai-key")
class ContentJobConfirmTest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client.credentials(HTTP_X_API_KEY="test-key")
        self.user = User.objects.create_user(email="confirm@example.com", password="password")
        self.ledger = Ledger.objects.create(
            user=self.user,
            delta=-6,
            kind="SPEND",
            source="CONTENT_FACTORY",
            description="Content Factory charge",
            created_by_slack_id="U123",
            idempotency_key="content-factory-job-confirm-ledger",
        )
        self.job_id = str(uuid.uuid4())
        self.job = ContentFactoryJob.objects.create(
            job_id=self.job_id,
            domain="example.com",
            status="awaiting_confirmation",
            slack_user_id="U123",
            selected_keyword="existing keyword",
            client_request_id="content-factory-job-confirm",
            billing_source_job_id=self.job_id,
            billing_amount=6,
            billing_status="charged",
            billing_ledger=self.ledger,
            selection_data={
                "options": [
                    {"keyword": "existing keyword"},
                    {"keyword": "alternate keyword"},
                ]
            },
        )
        self.url = f"/api/v1/content/jobs/{self.job_id}/confirm"

    @patch("integrations.services.article_generation.confirm_topic")
    def test_confirm_success_reuses_existing_billing(self, mock_confirm_topic):
        mock_confirm_topic.return_value = {"job_id": "child-job-123", "status": "queued"}

        response = self.client.post(
            self.url,
            {
                "slack_user_id": "U999",
                "domain": "example.com",
                "option_index": 0,
                "request_source": "roo_slackbot",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "confirmed")
        self.assertEqual(response.data["job_id"], "child-job-123")

        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "confirmed")
        self.assertEqual(self.job.selected_keyword, "existing keyword")
        self.assertEqual(self.job.slack_user_id, "U999")

        child_job = ContentFactoryJob.objects.get(job_id="child-job-123")
        self.assertEqual(child_job.client_request_id, "content-factory-job-confirm")
        self.assertEqual(child_job.billing_source_job_id, self.job_id)
        self.assertEqual(child_job.billing_amount, 6)
        self.assertEqual(child_job.billing_status, "reused")
        self.assertEqual(child_job.billing_ledger_id, self.ledger.id)

        mock_confirm_topic.assert_called_once_with(
            domain="example.com",
            confirmed_keyword="existing keyword",
            slack_user_id="U999",
            custom_title=None,
            skip_alternatives=["alternate keyword"],
            source_run_id=self.job_id,
            slack_channel_id="",
            slack_thread_ts="",
            slack_root_message_ts="",
            progress_message_ts="",
            request_source="roo_slackbot",
        )

    def test_confirm_rejects_missing_request_source(self):
        response = self.client.post(
            self.url,
            {"slack_user_id": "U123"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_confirm_rejects_unbilled_job(self):
        self.job.billing_status = ""
        self.job.save(update_fields=["billing_status"])

        response = self.client.post(
            self.url,
            {
                "slack_user_id": "U123",
                "request_source": "roo_slackbot",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_confirm_job_not_found(self):
        response = self.client.post(
            "/api/v1/content/jobs/nonexistent/confirm",
            {
                "slack_user_id": "U123",
                "request_source": "roo_slackbot",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_confirm_rejects_non_roo_key(self):
        other_client = APIClient()
        other_client.credentials(HTTP_X_API_KEY="internal-key")

        response = other_client.post(
            self.url,
            {
                "slack_user_id": "U123",
                "request_source": "roo_slackbot",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
