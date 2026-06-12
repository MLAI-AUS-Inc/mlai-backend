"""
Article review preview lifecycle events from content-factory.

Previously these three events hit "Unknown event_type" and were silently
ignored — no job status update, no Slack notification — so preview failures
only surfaced when the founder happened to look at the run page. Each event
now updates the job, refreshes the live progress card, and notifies the
requester with the classified reason (and, for fallback previews, the
review-now-publish-later policy).
"""
import os
from unittest.mock import patch

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from content_factory.models import ContentFactoryJob

CALLBACK_URL = "/api/content-factory/callback/"


def _event_payload(event_type, job_id, **extra):
    payload = {
        "event_type": event_type,
        "job_id": job_id,
        "run_id": job_id,
        "workflow": "direct_generate",
        "domain": "mlai.au",
        "slack_user_id": "U123",
        "title": "How do AI detectors work?",
    }
    payload.update(extra)
    return payload


class ArticleReviewPreviewEventTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.api_key = "test_roo_key"
        os.environ["ROO_API_KEY"] = self.api_key
        os.environ["INTERNAL_API_KEY"] = self.api_key

        from django.conf import settings

        settings.ROO_API_KEY = self.api_key
        settings.INTERNAL_API_KEY = self.api_key
        self.client.credentials(HTTP_X_API_KEY=self.api_key)

        self.card_patcher = patch("content_factory.service_views.upsert_live_progress_card")
        self.card_mock = self.card_patcher.start()
        self.addCleanup(self.card_patcher.stop)
        self.dm_patcher = patch("integrations.services.slack.SlackService.send_dm")
        self.dm_mock = self.dm_patcher.start()
        self.addCleanup(self.dm_patcher.stop)
        self.message_patcher = patch("integrations.services.slack.SlackService.send_message")
        self.message_mock = self.message_patcher.start()
        self.addCleanup(self.message_patcher.stop)

    def test_fallback_ready_marks_job_reviewable_and_notifies(self):
        response = self.client.post(
            CALLBACK_URL,
            _event_payload(
                "article_review_preview_fallback_ready",
                "review-preview-fallback-1",
                fallback_preview_url="https://fallback.preview.pages.dev/articles/featured/x",
                preview_warning="Reviewing a fallback preview.",
            ),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["status"], "received")
        job = ContentFactoryJob.objects.get(job_id="review-preview-fallback-1")
        self.assertEqual(job.status, "needs_review")
        self.assertFalse(job.error_message)
        self.dm_mock.assert_called_once()
        text = self.dm_mock.call_args.args[1]
        self.assertIn("Fallback preview ready for review", text)
        self.assertIn("https://fallback.preview.pages.dev/articles/featured/x", text)
        self.assertIn("require", text)

    def test_preview_failed_blocks_job_with_classified_reason(self):
        response = self.client.post(
            CALLBACK_URL,
            _event_payload(
                "article_review_preview_failed",
                "review-preview-failed-1",
                error=(
                    'Could not resolve "../../../../components/articles/ArticleFAQ" from '
                    "app/articles/content/featured/x.tsx The failing file is part of the "
                    "generated article bundle; regenerating the article rebuilds it."
                ),
                error_code="preview_unresolved_import",
            ),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        job = ContentFactoryJob.objects.get(job_id="review-preview-failed-1")
        self.assertEqual(job.status, "blocked")
        self.assertIn("[preview_unresolved_import]", job.error_message)
        self.assertIn("Could not resolve", job.error_message)
        self.dm_mock.assert_called_once()
        text = self.dm_mock.call_args.args[1]
        self.assertIn("Article preview failed", text)
        self.assertIn("Could not resolve", text)
        self.assertIn("regenerate", text.lower())
        self.card_mock.assert_called_once()
        self.assertTrue(self.card_mock.call_args.kwargs.get("failed"))

    def test_preview_not_available_blocks_with_next_step(self):
        response = self.client.post(
            CALLBACK_URL,
            _event_payload(
                "article_review_preview_not_available",
                "review-preview-unavailable-1",
                error="Exact hosted article preview is not available for this run.",
                error_code="content_only_no_render_artifact",
                next_required_step="connect_repo_articles_location",
            ),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        job = ContentFactoryJob.objects.get(job_id="review-preview-unavailable-1")
        self.assertEqual(job.status, "blocked")
        self.assertIn("content_only_no_render_artifact", job.error_message)
        text = self.dm_mock.call_args.args[1]
        self.assertIn("preview unavailable", text.lower())
        self.assertIn("connect_repo_articles_location", text)

    def test_thread_context_preferred_over_dm(self):
        ContentFactoryJob.objects.create(
            job_id="review-preview-thread-1",
            domain="mlai.au",
            slack_user_id="U123",
            status="generating",
            slack_channel_id="C999",
            slack_thread_ts="171.001",
        )

        response = self.client.post(
            CALLBACK_URL,
            _event_payload(
                "article_review_preview_failed",
                "review-preview-thread-1",
                error="Hosted preview build failed.",
            ),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.message_mock.assert_called_once()
        self.assertEqual(self.message_mock.call_args.args[0], "C999")
        self.dm_mock.assert_not_called()

    def test_events_are_deduped_by_event_id(self):
        payload = _event_payload(
            "article_review_preview_failed",
            "review-preview-dedupe-1",
            error="Hosted preview build failed.",
            event_id="review-preview-event-1",
            emitted_at="2026-06-13T00:00:00+00:00",
        )

        first = self.client.post(CALLBACK_URL, payload, format="json")
        replay = self.client.post(CALLBACK_URL, payload, format="json")

        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(first.json()["status"], "received")
        self.assertEqual(replay.json()["status"], "duplicate")
        self.dm_mock.assert_called_once()
