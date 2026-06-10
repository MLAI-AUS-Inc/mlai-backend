"""
Guards for content-factory callback deliveries.

content-factory stamps every callback with `event_id` (unique per event) and
`emitted_at` (UTC ISO) and retries failed deliveries from a durable outbox, so
the same event can arrive more than once and old events can arrive after newer
ones. The receiver must dedupe replays by event_id and skip syncing events
older than the run's last synced event, while staying fully compatible with
older content-factory versions that send neither field.
"""
import os
from datetime import datetime, timezone as datetime_timezone
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from rest_framework import status
from rest_framework.test import APIClient

from content_factory.models import ContentFactoryCallbackEvent
from workflow_runs.models import (
    ContentFactoryRun,
    ContentFactoryRunStatus,
    ContentFactoryStepStatus,
)

CALLBACK_URL = "/api/content-factory/callback/"


def _utc(*args):
    return datetime(*args, tzinfo=datetime_timezone.utc)


def _setup_progress_payload(run_id, *, step, event_id=None, emitted_at=None):
    payload = {
        "event_type": "article_system_setup_progress",
        "job_id": run_id,
        "run_id": run_id,
        "workflow": "article_system_setup",
        "domain": "mlai.au",
        "github_repo": "MLAI-AUS-Inc/mlai-au",
        "status": "running",
        "current_step": step,
        "article_system_setup": {
            "status": "running",
            "setup_run_id": run_id,
            "current_step": step,
        },
    }
    if event_id is not None:
        payload["event_id"] = event_id
    if emitted_at is not None:
        payload["emitted_at"] = emitted_at
    return payload


class ContentFactoryCallbackGuardTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.api_key = "test_roo_key"
        os.environ["ROO_API_KEY"] = self.api_key
        os.environ["INTERNAL_API_KEY"] = self.api_key

        from django.conf import settings

        settings.ROO_API_KEY = self.api_key
        settings.INTERNAL_API_KEY = self.api_key
        self.client.credentials(HTTP_X_API_KEY=self.api_key)


class CallbackEventIdIdempotencyTests(ContentFactoryCallbackGuardTestCase):
    def test_duplicate_event_id_returns_200_without_reprocessing(self):
        event_id = "f0e1d2c3b4a5968778695a4b3c2d1e0f"
        payload = _setup_progress_payload(
            "setup-dedupe-1",
            step="prepare_branch",
            event_id=event_id,
            emitted_at="2026-06-10T00:00:00+00:00",
        )

        first = self.client.post(CALLBACK_URL, payload, format="json")

        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(first.json()["status"], "received")
        run = ContentFactoryRun.objects.get(run_id="setup-dedupe-1")
        self.assertEqual(run.current_step, "prepare_branch")
        marker = ContentFactoryCallbackEvent.objects.get(event_id=event_id)
        self.assertEqual(marker.job_id, "setup-dedupe-1")
        self.assertEqual(marker.event_type, "article_system_setup_progress")
        self.assertEqual(marker.emitted_at, _utc(2026, 6, 10, 0, 0, 0))

        # Local state has moved on since the first delivery; a replay of the
        # acknowledged event must not roll it back.
        run.current_step = "await_review"
        run.save(update_fields=["current_step", "updated_at"])

        replay = self.client.post(CALLBACK_URL, payload, format="json")

        self.assertEqual(replay.status_code, status.HTTP_200_OK)
        self.assertEqual(replay.json()["status"], "duplicate")
        self.assertEqual(replay.json()["event_id"], event_id)
        run.refresh_from_db()
        self.assertEqual(run.current_step, "await_review")
        self.assertEqual(ContentFactoryCallbackEvent.objects.filter(event_id=event_id).count(), 1)

    def test_payload_without_event_id_processes_every_delivery(self):
        first_payload = _setup_progress_payload("setup-legacy-1", step="prepare_branch")
        first = self.client.post(CALLBACK_URL, first_payload, format="json")
        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(first.json()["status"], "received")

        second_payload = _setup_progress_payload("setup-legacy-1", step="create_pull_request")
        second = self.client.post(CALLBACK_URL, second_payload, format="json")
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertEqual(second.json()["status"], "received")

        run = ContentFactoryRun.objects.get(run_id="setup-legacy-1")
        self.assertEqual(run.current_step, "create_pull_request")
        self.assertIsNone(run.last_event_emitted_at)
        self.assertFalse(ContentFactoryCallbackEvent.objects.exists())

    def test_processing_failure_releases_event_id_so_retry_reprocesses(self):
        event_id = "retry-after-failure-1"
        payload = _setup_progress_payload("setup-failure-1", step="prepare_branch", event_id=event_id)

        with patch(
            "content_factory.service_views._sync_article_system_setup_callback_to_run",
            side_effect=RuntimeError("transient processing failure"),
        ):
            failed = self.client.post(CALLBACK_URL, payload, format="json")

        self.assertEqual(failed.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertFalse(ContentFactoryCallbackEvent.objects.filter(event_id=event_id).exists())

        retry = self.client.post(CALLBACK_URL, payload, format="json")

        self.assertEqual(retry.status_code, status.HTTP_200_OK)
        self.assertEqual(retry.json()["status"], "received")
        self.assertTrue(ContentFactoryRun.objects.filter(run_id="setup-failure-1").exists())
        self.assertTrue(ContentFactoryCallbackEvent.objects.filter(event_id=event_id).exists())

    def test_duplicate_unknown_event_type_is_still_deduped(self):
        payload = {
            "event_type": "some_future_event",
            "job_id": "future-job-1",
            "event_id": "future-event-1",
        }

        first = self.client.post(CALLBACK_URL, payload, format="json")
        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(first.json()["status"], "ignored")

        replay = self.client.post(CALLBACK_URL, payload, format="json")
        self.assertEqual(replay.status_code, status.HTTP_200_OK)
        self.assertEqual(replay.json()["status"], "duplicate")


class CallbackEmittedAtStalenessTests(ContentFactoryCallbackGuardTestCase):
    def test_stale_setup_callback_does_not_overwrite_newer_state(self):
        newer = _setup_progress_payload(
            "setup-stale-1",
            step="verify_directory_build",
            event_id="evt-newer-1",
            emitted_at="2026-06-10T01:00:05+00:00",
        )
        response = self.client.post(CALLBACK_URL, newer, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        run = ContentFactoryRun.objects.get(run_id="setup-stale-1")
        self.assertEqual(run.current_step, "verify_directory_build")
        self.assertEqual(run.last_event_emitted_at, _utc(2026, 6, 10, 1, 0, 5))

        late_retry = _setup_progress_payload(
            "setup-stale-1",
            step="prepare_branch",
            event_id="evt-older-1",
            emitted_at="2026-06-10T01:00:00+00:00",
        )
        response = self.client.post(CALLBACK_URL, late_retry, format="json")

        # 200 so the sender does not keep retrying the stale event.
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        run.refresh_from_db()
        self.assertEqual(run.current_step, "verify_directory_build")
        self.assertEqual(run.last_event_emitted_at, _utc(2026, 6, 10, 1, 0, 5))

    def test_newer_emitted_at_applies_and_advances_watermark(self):
        first = _setup_progress_payload(
            "setup-order-1",
            step="prepare_branch",
            event_id="evt-order-a",
            emitted_at="2026-06-10T01:00:00+00:00",
        )
        self.client.post(CALLBACK_URL, first, format="json")
        second = _setup_progress_payload(
            "setup-order-1",
            step="verify_directory_build",
            event_id="evt-order-b",
            emitted_at="2026-06-10T01:00:05+00:00",
        )
        self.client.post(CALLBACK_URL, second, format="json")

        run = ContentFactoryRun.objects.get(run_id="setup-order-1")
        self.assertEqual(run.current_step, "verify_directory_build")
        self.assertEqual(run.last_event_emitted_at, _utc(2026, 6, 10, 1, 0, 5))

    def test_legacy_payload_without_emitted_at_still_syncs(self):
        stamped = _setup_progress_payload(
            "setup-mixed-1",
            step="prepare_branch",
            event_id="evt-stamped-1",
            emitted_at="2026-06-10T01:00:05+00:00",
        )
        self.client.post(CALLBACK_URL, stamped, format="json")

        legacy = _setup_progress_payload("setup-mixed-1", step="create_pull_request")
        response = self.client.post(CALLBACK_URL, legacy, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        run = ContentFactoryRun.objects.get(run_id="setup-mixed-1")
        self.assertEqual(run.current_step, "create_pull_request")
        # The unstamped payload must not clear the watermark.
        self.assertEqual(run.last_event_emitted_at, _utc(2026, 6, 10, 1, 0, 5))


class GenerationCallbackStalenessTests(TestCase):
    def test_stale_generation_callback_does_not_overwrite_newer_state(self):
        from content_factory.service_views import _sync_generation_callback_to_run

        newer = {
            "run_id": "gen-stale-1",
            "job_id": "gen-stale-1",
            "workflow": "direct_generate",
            "domain": "mlai.au",
            "error": "verifier blocked",
            "blocked_step": "verify_article",
            "emitted_at": "2026-06-10T02:00:10+00:00",
        }
        run = _sync_generation_callback_to_run(
            data=newer,
            run_status=ContentFactoryRunStatus.BLOCKED,
            step_status=ContentFactoryStepStatus.BLOCKED,
        )
        self.assertEqual(run.status, ContentFactoryRunStatus.BLOCKED)
        self.assertEqual(run.last_event_emitted_at, _utc(2026, 6, 10, 2, 0, 10))

        stale = {
            "run_id": "gen-stale-1",
            "job_id": "gen-stale-1",
            "workflow": "direct_generate",
            "domain": "mlai.au",
            "error": "old transient failure",
            "failed_step": "draft_section",
            "emitted_at": "2026-06-10T02:00:00+00:00",
        }
        returned = _sync_generation_callback_to_run(
            data=stale,
            run_status=ContentFactoryRunStatus.FAILED,
            step_status=ContentFactoryStepStatus.FAILED,
        )

        self.assertEqual(returned.run_id, "gen-stale-1")
        run.refresh_from_db()
        self.assertEqual(run.status, ContentFactoryRunStatus.BLOCKED)
        self.assertEqual(run.error, "verifier blocked")
        self.assertEqual(run.last_event_emitted_at, _utc(2026, 6, 10, 2, 0, 10))
        self.assertFalse(run.steps.filter(step_key="draft_section").exists())

    def test_generation_callback_without_emitted_at_still_syncs(self):
        from content_factory.service_views import _sync_generation_callback_to_run

        stamped = {
            "run_id": "gen-mixed-1",
            "job_id": "gen-mixed-1",
            "workflow": "direct_generate",
            "domain": "mlai.au",
            "error": "verifier blocked",
            "blocked_step": "verify_article",
            "emitted_at": "2026-06-10T02:00:10+00:00",
        }
        _sync_generation_callback_to_run(
            data=stamped,
            run_status=ContentFactoryRunStatus.BLOCKED,
            step_status=ContentFactoryStepStatus.BLOCKED,
        )

        legacy = {
            "run_id": "gen-mixed-1",
            "job_id": "gen-mixed-1",
            "workflow": "direct_generate",
            "domain": "mlai.au",
            "error": "terminal failure",
            "failed_step": "verify_article",
        }
        run = _sync_generation_callback_to_run(
            data=legacy,
            run_status=ContentFactoryRunStatus.FAILED,
            step_status=ContentFactoryStepStatus.FAILED,
        )

        run.refresh_from_db()
        self.assertEqual(run.status, ContentFactoryRunStatus.FAILED)
        self.assertEqual(run.error, "terminal failure")
        # Legacy payload preserves the existing watermark.
        self.assertEqual(run.last_event_emitted_at, _utc(2026, 6, 10, 2, 0, 10))


class ScanCallbackStalenessTests(TestCase):
    def test_stale_scan_callback_skips_mutation(self):
        from content_factory.service_views import _sync_scan_callback_to_run

        newer = {
            "run_id": "scan-stale-1",
            "job_id": "scan-stale-1",
            "workflow": "repo_scan",
            "domain": "mlai.au",
            "github_repo": "MLAI-AUS-Inc/mlai-au",
            "emitted_at": "2026-06-10T03:00:10+00:00",
        }
        run = _sync_scan_callback_to_run(data=newer, approval_required=False)
        self.assertEqual(run.status, ContentFactoryRunStatus.COMPLETED)
        self.assertEqual(run.last_event_emitted_at, _utc(2026, 6, 10, 3, 0, 10))

        stale = {
            "run_id": "scan-stale-1",
            "job_id": "scan-stale-1",
            "workflow": "repo_scan",
            "domain": "mlai.au",
            "github_repo": "MLAI-AUS-Inc/mlai-au",
            "emitted_at": "2026-06-10T03:00:00+00:00",
        }
        returned = _sync_scan_callback_to_run(data=stale, approval_required=True)

        self.assertEqual(returned.run_id, "scan-stale-1")
        run.refresh_from_db()
        # A stale retry must not flip the completed scan back to awaiting confirmation.
        self.assertEqual(run.status, ContentFactoryRunStatus.COMPLETED)
        self.assertEqual(run.last_event_emitted_at, _utc(2026, 6, 10, 3, 0, 10))


class CallbackEventHelperTests(SimpleTestCase):
    def test_emitted_at_parsing_handles_supported_and_absent_values(self):
        from content_factory.service_views import _callback_event_emitted_at

        self.assertEqual(
            _callback_event_emitted_at({"emitted_at": "2026-06-10T01:02:03+00:00"}),
            _utc(2026, 6, 10, 1, 2, 3),
        )
        self.assertEqual(
            _callback_event_emitted_at({"emitted_at": "2026-06-10T01:02:03Z"}),
            _utc(2026, 6, 10, 1, 2, 3),
        )
        # Naive stamps are treated as UTC.
        self.assertEqual(
            _callback_event_emitted_at({"emitted_at": "2026-06-10T01:02:03"}),
            _utc(2026, 6, 10, 1, 2, 3),
        )
        self.assertIsNone(_callback_event_emitted_at({"emitted_at": "not-a-date"}))
        self.assertIsNone(_callback_event_emitted_at({"emitted_at": ""}))
        self.assertIsNone(_callback_event_emitted_at({}))
        self.assertIsNone(_callback_event_emitted_at(None))

    def test_staleness_comparison_edges(self):
        from content_factory.service_views import _callback_event_is_stale

        run = ContentFactoryRun(last_event_emitted_at=_utc(2026, 6, 10, 1, 0, 0))
        self.assertTrue(
            _callback_event_is_stale(existing_run=run, emitted_at=_utc(2026, 6, 10, 0, 59, 59))
        )
        # Equal timestamps are not stale: distinct events emitted in the same
        # instant still apply (exact replays are handled by event_id dedupe).
        self.assertFalse(
            _callback_event_is_stale(existing_run=run, emitted_at=_utc(2026, 6, 10, 1, 0, 0))
        )
        self.assertFalse(_callback_event_is_stale(existing_run=run, emitted_at=None))
        self.assertFalse(
            _callback_event_is_stale(existing_run=None, emitted_at=_utc(2026, 6, 10, 1, 0, 0))
        )
        # Runs that have never synced a stamped event have no watermark.
        self.assertFalse(
            _callback_event_is_stale(
                existing_run=ContentFactoryRun(), emitted_at=_utc(2026, 6, 10, 1, 0, 0)
            )
        )
