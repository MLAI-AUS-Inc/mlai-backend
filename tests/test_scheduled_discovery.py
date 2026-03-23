import json
import os
from datetime import date as calendar_date
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from requests import Response
from rest_framework import status
from rest_framework.test import APIClient

from core.models import (
    ContentFactoryJob,
    Organization,
    OrganizationContentConfig,
    ScheduledDiscoveryDispatch,
    ScheduledDiscoveryDispatchState,
    User,
)
from integrations.models import UserIntegration
from integrations.services.article_generation import ArticleGenerationError, confirm_topic
from integrations.services.daily_discovery import (
    DEFAULT_SCHEDULE_TIMEZONE,
    due_daily_discovery_targets,
    enqueue_scheduled_discovery,
    expire_stale_queued_dispatches,
    resolve_daily_discovery_timezone,
    run_daily_discovery_scheduler,
)
from roo.models import PointsAccount


class ScheduledDiscoveryServiceTests(TestCase):
    def setUp(self):
        self.integration = UserIntegration.objects.create(
            slack_user_id="U-SCHED",
            github_repo="owner/repo",
        )
        self.org = Organization.objects.create(
            name="Example",
            domain="example.com",
            competitors=["competitor-a.com"],
        )
        self.config = OrganizationContentConfig.objects.create(
            organization=self.org,
            github_repo="owner/repo",
            scan_summary="scan ready",
        )

    @staticmethod
    def _melbourne_due_now():
        return datetime(2026, 3, 23, 21, 2, tzinfo=dt_timezone.utc)

    @patch("integrations.services.daily_discovery.SlackService.get_user_profile")
    def test_due_targets_fire_at_local_8am(self, mock_get_user_profile):
        mock_get_user_profile.return_value = {"tz": "Australia/Melbourne"}

        targets = due_daily_discovery_targets(now=self._melbourne_due_now())

        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].slack_user_id, "U-SCHED")
        self.assertEqual(targets[0].domain, "example.com")
        self.assertEqual(targets[0].timezone_name, "Australia/Melbourne")

    @patch("integrations.services.daily_discovery.SlackService.get_user_profile")
    @patch("integrations.services.daily_discovery.trigger_article_generation")
    def test_run_scheduler_queues_multiple_domains_for_same_user(self, mock_trigger, mock_get_user_profile):
        mock_get_user_profile.return_value = {"tz": "Australia/Melbourne"}
        mock_trigger.side_effect = [
            {"job_id": "job-domain-1"},
            {"job_id": "job-domain-2"},
        ]
        other_org = Organization.objects.create(
            name="Other",
            domain="other-example.com",
            competitors=["competitor-b.com"],
        )
        OrganizationContentConfig.objects.create(
            organization=other_org,
            github_repo="owner/repo",
            scan_summary="scan ready",
        )

        result = run_daily_discovery_scheduler(now=self._melbourne_due_now())

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["queued"], 2)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(
            ScheduledDiscoveryDispatch.objects.filter(state=ScheduledDiscoveryDispatchState.QUEUED).count(),
            2,
        )

    @patch("integrations.services.daily_discovery.SlackService.get_user_profile")
    @patch("integrations.services.daily_discovery.trigger_article_generation")
    def test_enqueue_is_idempotent_for_same_day(self, mock_trigger, mock_get_user_profile):
        mock_get_user_profile.return_value = {"tz": "Australia/Melbourne"}
        mock_trigger.return_value = {"job_id": "job-123"}

        first = enqueue_scheduled_discovery(
            slack_user_id="U-SCHED",
            domain="example.com",
            now=self._melbourne_due_now(),
        )
        second = enqueue_scheduled_discovery(
            slack_user_id="U-SCHED",
            domain="example.com",
            now=self._melbourne_due_now(),
        )

        self.assertEqual(first["status"], "queued")
        self.assertEqual(second["status"], "skipped")
        self.assertEqual(ScheduledDiscoveryDispatch.objects.count(), 1)
        self.assertEqual(mock_trigger.call_count, 1)

    @patch("integrations.services.daily_discovery.SlackService.get_user_profile")
    @patch("integrations.services.daily_discovery.trigger_article_generation")
    def test_enqueue_skips_when_open_suggestion_exists_without_creating_new_dispatch(self, mock_trigger, mock_get_user_profile):
        mock_get_user_profile.return_value = {"tz": "Australia/Melbourne"}
        mock_trigger.return_value = {"job_id": "job-existing"}
        ScheduledDiscoveryDispatch.objects.create(
            slack_user_id="U-SCHED",
            domain="example.com",
            timezone="Australia/Melbourne",
            local_date=calendar_date(2026, 3, 23),
            state=ScheduledDiscoveryDispatchState.TOPIC_SELECTION_SENT,
            content_factory_job_id="job-existing",
        )

        result = enqueue_scheduled_discovery(
            slack_user_id="U-SCHED",
            domain="example.com",
            local_date=calendar_date(2026, 3, 24),
            now=self._melbourne_due_now(),
        )

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "open_suggestion_exists")
        self.assertEqual(ScheduledDiscoveryDispatch.objects.count(), 1)
        mock_trigger.assert_not_called()

    def test_expire_stale_queued_dispatches_marks_failed_timeout(self):
        dispatch = ScheduledDiscoveryDispatch.objects.create(
            slack_user_id="U-SCHED",
            domain="example.com",
            timezone="Australia/Melbourne",
            local_date=calendar_date(2026, 3, 24),
            state=ScheduledDiscoveryDispatchState.QUEUED,
        )
        stale_time = timezone.now() - timedelta(hours=3)
        ScheduledDiscoveryDispatch.objects.filter(pk=dispatch.pk).update(updated_at=stale_time)

        expired = expire_stale_queued_dispatches(now=timezone.now())

        self.assertEqual(expired, 1)
        dispatch.refresh_from_db()
        self.assertEqual(dispatch.state, ScheduledDiscoveryDispatchState.FAILED_TIMEOUT)

    @patch("integrations.services.daily_discovery.SlackService.get_user_profile")
    def test_timezone_resolution_falls_back_to_config_then_default(self, mock_get_user_profile):
        mock_get_user_profile.return_value = {}
        self.config.default_timezone = "America/Los_Angeles"
        self.config.save(update_fields=["default_timezone"])

        timezone_name = resolve_daily_discovery_timezone("U-SCHED", config=self.config)

        self.assertEqual(timezone_name, "America/Los_Angeles")

        self.config.default_timezone = ""
        self.config.save(update_fields=["default_timezone"])

        timezone_name = resolve_daily_discovery_timezone("U-SCHED", config=self.config)

        self.assertEqual(timezone_name, DEFAULT_SCHEDULE_TIMEZONE)


class ScheduledDiscoveryReplayEndpointTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.api_key = "test_roo_key"
        os.environ["ROO_API_KEY"] = self.api_key
        os.environ["INTERNAL_API_KEY"] = self.api_key
        from django.conf import settings

        settings.ROO_API_KEY = self.api_key
        settings.INTERNAL_API_KEY = self.api_key
        self.client.credentials(HTTP_X_API_KEY=self.api_key)

        self.org = Organization.objects.create(
            name="Replay Example",
            domain="replay-example.com",
            competitors=["competitor-a.com"],
        )
        OrganizationContentConfig.objects.create(
            organization=self.org,
            github_repo="owner/replay-repo",
            scan_summary="scan ready",
        )

    @patch("integrations.services.daily_discovery.trigger_article_generation")
    @patch("integrations.services.daily_discovery.SlackService.get_user_profile")
    def test_replay_endpoint_queues_specific_target(self, mock_get_user_profile, mock_trigger):
        mock_get_user_profile.return_value = {"tz": "Australia/Melbourne"}
        mock_trigger.return_value = {"job_id": "replay-job-1"}

        response = self.client.post(
            reverse("content_factory_scheduled_discovery_replay"),
            {
                "domain": "replay-example.com",
                "slack_user_id": "U-REPLAY",
                "local_date": "2026-03-24",
                "force": True,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(response.data["status"], "queued")
        dispatch = ScheduledDiscoveryDispatch.objects.get(
            slack_user_id="U-REPLAY",
            domain="replay-example.com",
        )
        self.assertEqual(dispatch.content_factory_job_id, "replay-job-1")


class DeferredScheduledConfirmRefundTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="scheduled@example.com",
            password="password",
            slack_id="U-CONFIRM",
        )
        PointsAccount.objects.create(user=self.user, balance=20)
        self.org = Organization.objects.create(
            name="Confirm Example",
            domain="confirm-example.com",
            competitors=["competitor-a.com"],
        )
        OrganizationContentConfig.objects.create(
            organization=self.org,
            github_repo="owner/confirm-repo",
            scan_summary="scan ready",
        )
        self.source_job = ContentFactoryJob.objects.create(
            job_id="scheduled-source-1",
            domain="confirm-example.com",
            slack_user_id="U-CONFIRM",
            status="awaiting_confirmation",
            selected_keyword="scheduled keyword",
            billing_status="deferred",
            request_meta={
                "domain": "confirm-example.com",
                "trigger_source": "scheduled_daily",
                "client_request_id": "scheduled-client-1",
            },
        )

    @staticmethod
    def _response(status_code, body):
        response = Response()
        response.status_code = status_code
        response._content = json.dumps(body).encode()
        response.headers["Content-Type"] = "application/json"
        return response

    @patch("integrations.services.article_generation.get_github_credentials_for_domain")
    @patch("integrations.services.article_generation.http_requests.post")
    def test_confirm_topic_refunds_deferred_scheduled_charge_on_downstream_failure(self, mock_post, mock_get_credentials):
        mock_get_credentials.return_value = {
            "token": "gh-token",
            "repo": "owner/confirm-repo",
            "source": "org",
        }
        mock_post.return_value = self._response(500, {"error": "Downstream failure"})

        with self.assertRaises(ArticleGenerationError):
            confirm_topic(
                domain="confirm-example.com",
                confirmed_keyword="scheduled keyword",
                slack_user_id="U-CONFIRM",
                source_run_id="scheduled-source-1",
            )

        self.source_job.refresh_from_db()
        self.user.points_account.refresh_from_db()
        self.assertEqual(self.source_job.billing_status, "refunded")
        self.assertEqual(self.user.points_account.balance, 20)
