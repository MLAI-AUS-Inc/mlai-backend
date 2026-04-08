import json
import os
from datetime import date as calendar_date
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.test import TestCase, override_settings
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
from integrations.services.article_generation import ArticleGenerationError, confirm_topic
from integrations.services.daily_discovery import (
    DEFAULT_SCHEDULE_TIMEZONE,
    enqueue_scheduled_discovery,
    resolve_daily_discovery_timezone,
    run_daily_discovery_scheduler,
)
from roo.models import PointsAccount


@override_settings(
    SCHEDULED_DISCOVERY_TIMEZONE="Australia/Melbourne",
    SCHEDULED_DISCOVERY_CHANNEL_NAME="vibe-marketing",
    SCHEDULED_DISCOVERY_SLOT_MINUTES=15,
    SCHEDULED_DISCOVERY_MAX_TARGETS=20,
)
class ScheduledDiscoveryServiceTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name="Alpha",
            domain="alpha.example.com",
            competitors=["competitor-a.com"],
            seed_keywords=["alpha keyword"],
        )
        self.config = OrganizationContentConfig.objects.create(
            organization=self.org,
            connected_slack_user_id="U-ALPHA",
            daily_discovery_enabled=True,
            daily_discovery_priority=0,
            github_repo="owner/alpha",
            scan_summary="scan ready",
        )

    @staticmethod
    def _utc(year, month, day, hour, minute):
        return datetime(year, month, day, hour, minute, tzinfo=dt_timezone.utc)

    @classmethod
    def _melbourne_8am(cls):
        return cls._utc(2026, 3, 23, 21, 0)

    @classmethod
    def _melbourne_807am(cls):
        return cls._utc(2026, 3, 23, 21, 7)

    @classmethod
    def _melbourne_816am(cls):
        return cls._utc(2026, 3, 23, 21, 16)

    @classmethod
    def _schedule_local_date(cls):
        return calendar_date(2026, 3, 24)

    @patch("integrations.services.daily_discovery.trigger_article_generation")
    def test_scheduler_builds_schedule_and_dispatches_first_slot_only(self, mock_trigger):
        mock_trigger.return_value = {"job_id": "job-alpha"}
        beta_org = Organization.objects.create(
            name="Beta",
            domain="beta.example.com",
            competitors=["competitor-b.com"],
            seed_keywords=["beta keyword"],
        )
        OrganizationContentConfig.objects.create(
            organization=beta_org,
            connected_slack_user_id="U-BETA",
            daily_discovery_enabled=True,
            daily_discovery_priority=10,
            github_repo="owner/beta",
            scan_summary="scan ready",
        )

        result = run_daily_discovery_scheduler(now=self._melbourne_807am())

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["schedule_result"]["status"], "scheduled")
        self.assertEqual(result["schedule_result"]["created"], 2)
        self.assertEqual(result["queued"], 1)
        self.assertEqual(result["failed"], 0)

        alpha_dispatch = ScheduledDiscoveryDispatch.objects.get(domain="alpha.example.com")
        beta_dispatch = ScheduledDiscoveryDispatch.objects.get(domain="beta.example.com")

        self.assertEqual(alpha_dispatch.state, ScheduledDiscoveryDispatchState.QUEUED)
        self.assertEqual(alpha_dispatch.slot_index, 0)
        self.assertEqual(alpha_dispatch.content_factory_job_id, "job-alpha")
        self.assertEqual(beta_dispatch.state, ScheduledDiscoveryDispatchState.SCHEDULED)
        self.assertEqual(beta_dispatch.slot_index, 1)
        self.assertEqual(
            beta_dispatch.scheduled_for_at,
            self._utc(2026, 3, 23, 21, 15),
        )

        mock_trigger.assert_called_once()
        self.assertEqual(mock_trigger.call_args[0][0], "U-ALPHA")
        payload = mock_trigger.call_args[0][1]
        self.assertEqual(payload["domain"], "alpha.example.com")
        self.assertEqual(payload["trigger_source"], "scheduled_daily")
        self.assertEqual(payload["scheduled_slot_index"], 0)
        self.assertEqual(payload["scheduled_channel_name"], "vibe-marketing")

    @patch("integrations.services.daily_discovery.trigger_article_generation")
    def test_failed_slot_does_not_block_next_due_slot(self, mock_trigger):
        first_dispatch = ScheduledDiscoveryDispatch.objects.create(
            slack_user_id="U-ALPHA",
            domain="alpha.example.com",
            timezone="Australia/Melbourne",
            local_date=self._schedule_local_date(),
            scheduled_for_at=self._melbourne_8am(),
            slot_index=0,
            trigger_source="daily_scheduler",
            state=ScheduledDiscoveryDispatchState.SCHEDULED,
        )
        second_dispatch = ScheduledDiscoveryDispatch.objects.create(
            slack_user_id="U-BETA",
            domain="beta.example.com",
            timezone="Australia/Melbourne",
            local_date=self._schedule_local_date(),
            scheduled_for_at=self._utc(2026, 3, 23, 21, 15),
            slot_index=1,
            trigger_source="daily_scheduler",
            state=ScheduledDiscoveryDispatchState.SCHEDULED,
        )
        mock_trigger.side_effect = [
            RuntimeError("boom"),
            {"job_id": "job-beta"},
        ]

        first_result = run_daily_discovery_scheduler(now=self._melbourne_8am())
        second_result = run_daily_discovery_scheduler(now=self._melbourne_816am())

        self.assertEqual(first_result["failed"], 1)
        self.assertEqual(second_result["queued"], 1)

        first_dispatch.refresh_from_db()
        second_dispatch.refresh_from_db()
        self.assertEqual(first_dispatch.state, ScheduledDiscoveryDispatchState.FAILED)
        self.assertEqual(second_dispatch.state, ScheduledDiscoveryDispatchState.QUEUED)
        self.assertEqual(second_dispatch.content_factory_job_id, "job-beta")

    @patch("integrations.services.daily_discovery.trigger_article_generation")
    def test_scheduler_expires_previous_day_open_dispatches_before_new_day_runs(self, mock_trigger):
        mock_trigger.return_value = {"job_id": "job-alpha"}
        stale_dispatch = ScheduledDiscoveryDispatch.objects.create(
            slack_user_id="U-ALPHA",
            domain="alpha.example.com",
            timezone="Australia/Melbourne",
            local_date=calendar_date(2026, 3, 23),
            scheduled_for_at=self._utc(2026, 3, 22, 21, 0),
            slot_index=0,
            trigger_source="daily_scheduler",
            state=ScheduledDiscoveryDispatchState.TOPIC_SELECTION_SENT,
            content_factory_job_id="old-job",
        )

        result = run_daily_discovery_scheduler(now=self._melbourne_8am())

        stale_dispatch.refresh_from_db()
        self.assertEqual(stale_dispatch.state, ScheduledDiscoveryDispatchState.EXPIRED)
        self.assertEqual(result["expired_dispatches"], 1)
        self.assertEqual(
            ScheduledDiscoveryDispatch.objects.filter(
                local_date=self._schedule_local_date(),
                state=ScheduledDiscoveryDispatchState.QUEUED,
            ).count(),
            1,
        )

    @patch("integrations.services.daily_discovery.trigger_article_generation")
    def test_enqueue_is_idempotent_for_same_day(self, mock_trigger):
        mock_trigger.return_value = {"job_id": "job-123"}

        first = enqueue_scheduled_discovery(
            slack_user_id="U-ALPHA",
            domain="alpha.example.com",
            now=self._melbourne_807am(),
        )
        second = enqueue_scheduled_discovery(
            slack_user_id="U-ALPHA",
            domain="alpha.example.com",
            now=self._melbourne_807am(),
        )

        self.assertEqual(first["status"], "queued")
        self.assertEqual(second["status"], "skipped")
        self.assertEqual(second["reason"], "dispatch_already_exists")
        self.assertEqual(ScheduledDiscoveryDispatch.objects.count(), 1)
        self.assertEqual(mock_trigger.call_count, 1)

    def test_timezone_resolution_uses_shared_schedule_timezone(self):
        self.config.default_timezone = "America/Los_Angeles"
        self.config.save(update_fields=["default_timezone"])

        timezone_name = resolve_daily_discovery_timezone("U-ALPHA", config=self.config)

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
            seed_keywords=["replay keyword"],
        )
        OrganizationContentConfig.objects.create(
            organization=self.org,
            github_repo="owner/replay-repo",
            scan_summary="scan ready",
        )

    @patch("integrations.services.daily_discovery.trigger_article_generation")
    def test_replay_endpoint_queues_specific_target(self, mock_trigger):
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
            seed_keywords=["confirm keyword"],
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
