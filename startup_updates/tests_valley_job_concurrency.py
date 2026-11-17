"""Job bookkeeping must preserve a new worker's durable execution fence."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest import skipUnless
from unittest.mock import patch

from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase, override_settings
from rest_framework.test import APIRequestFactory

from content_factory import service_views
from startup_updates.execution import claim_execution
from workflow_runs.models import ContentFactoryRun


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locks")
@override_settings(ROO_API_KEY="synthetic-pulse-tracking-key")
class ValleyJobConcurrencyTests(TransactionTestCase):
    def test_finishing_job_cannot_overwrite_the_next_execution_claim(self):
        run = ContentFactoryRun.objects.create(
            run_id="synthetic-pulse-tracking-race",
            workflow="startup_monthly_update",
            status="running",
            result={
                "_pulse_execution": {"token": 1, "owner": "previous-worker"},
                "_valley_meta": {"tracked_job_ids": ["previous-job", "next-job"]},
            },
        )
        tracking_read = Event()
        release_tracking = Event()
        claim_started = Event()
        claim_finished = Event()
        read_meta = service_views._content_factory_run_meta

        def pause_after_tracking_read(current_run):
            meta = read_meta(current_run)
            tracking_read.set()
            if not release_tracking.wait(5):
                raise AssertionError("The tracking test was not released")
            return meta

        def finish_previous_job():
            close_old_connections()
            try:
                request = APIRequestFactory().post(
                    "/valley-jobs",
                    {"job_id": "previous-job", "transition": "finished"},
                    format="json",
                    HTTP_X_API_KEY="synthetic-pulse-tracking-key",
                )
                response = service_views.ContentFactoryRunValleyJobView.as_view()(
                    request, run_id=run.run_id
                )
                self.assertEqual(response.status_code, 200)
            finally:
                close_old_connections()

        def start_next_execution():
            close_old_connections()
            try:
                claim_started.set()
                with transaction.atomic():
                    current_run = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
                    claim_execution(current_run, owner="next-worker")
                claim_finished.set()
            finally:
                close_old_connections()

        with patch.object(service_views, "_content_factory_run_meta", pause_after_tracking_read):
            with ThreadPoolExecutor(max_workers=2) as pool:
                previous = pool.submit(finish_previous_job)
                try:
                    self.assertTrue(tracking_read.wait(5))
                    successor = pool.submit(start_next_execution)
                    self.assertTrue(claim_started.wait(5))
                    # Without the tracking row lock, the claim commits here and
                    # its token is then overwritten by the paused stale writer.
                    claim_finished.wait(1)
                finally:
                    release_tracking.set()
                previous.result(timeout=8)
                successor.result(timeout=8)

        run.refresh_from_db()
        self.assertEqual(run.result["_pulse_execution"]["token"], 2)
        self.assertEqual(run.result["_pulse_execution"]["owner"], "next-worker")
        self.assertEqual(run.result["_valley_meta"]["tracked_job_ids"], ["next-job"])
