"""Actual concurrent HTTP receipts preserve the first successful acknowledgement."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from django.db import close_old_connections, connection
from django.test import TransactionTestCase, override_settings
from rest_framework.test import APIClient

from . import founder_draft_views as tools
from .founder_journey_testing import ALL_P1_FLAGS_ON, make_new_journey_org
from .tests_founder_draft_tools import _run
from workflow_runs.models import ContentFactoryRun
from roo.models import Ledger


@override_settings(**ALL_P1_FLAGS_ON)
class ConcurrentDraftReceiptTests(TransactionTestCase):
    def setUp(self):
        self.built = make_new_journey_org(domain="receipt-races.example.com")
        self.org = self.built.organization
        self.run = _run(self.org)
        self.before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        self.ledger_count = Ledger.objects.count()

    def post(self, action, data, operation_id=None):
        close_old_connections()
        try:
            client = APIClient()
            client.force_authenticate(self.built.owner)
            response = client.post(f"/api/v1/my-startup/vibe-marketing/runs/{self.run.run_id}/{action}/?companyId={self.built.company.pk}",
                data, format="json", **({"HTTP_IDEMPOTENCY_KEY": operation_id} if operation_id else {}))
            self.assertEqual(response.status_code, 200, response.data)
            return response.json()
        finally:
            close_old_connections()

    def assert_no_execution_or_money_effect(self):
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), self.before)
        self.assertEqual(Ledger.objects.count(), self.ledger_count)

    def test_simultaneous_copy_and_lost_ack_return_one_receipt_and_emit_once(self):
        barrier = Barrier(2)
        def export(*args):
            self.assertFalse(connection.in_atomic_block)
            barrier.wait(timeout=10)
            return {"status": "ready", "revision": "revision-one"}, None
        with patch.object(tools, "current_export", side_effect=export), \
                patch("content_factory.founder_funnel.emit_event") as event:
            with ThreadPoolExecutor(2) as pool:
                futures = [pool.submit(self.post, "copied", {"revision": "revision-one"}) for _ in range(2)]
                responses = [future.result(timeout=30) for future in futures]
            self.assertEqual(responses[0], responses[1])
            event.assert_called_once()
            # Simulate a lost acknowledgement: replay skips the worker entirely.
            with patch.object(tools, "current_export", side_effect=AssertionError("replay must use receipt")):
                replay = self.post("copied", {"revision": "revision-one"})
            self.assertEqual(replay, responses[0])
            event.assert_called_once()
        self.assert_no_execution_or_money_effect()

    def test_simultaneous_posted_link_and_replay_keep_first_check_and_timestamp(self):
        barrier = Barrier(2)
        def check(*args):
            self.assertFalse(connection.in_atomic_block)
            barrier.wait(timeout=10)
            return "matched"
        data = {"url": "https://receipt-races.example.com/articles/current"}
        with patch.object(tools, "own_site_host", return_value=True), \
                patch.object(tools, "current_export", return_value=({"status": "ready"}, None)), \
                patch.object(tools, "check_posted_link", side_effect=check) as fetch:
            with ThreadPoolExecutor(2) as pool:
                futures = [pool.submit(self.post, "posted-link", data, "same-posted-operation") for _ in range(2)]
                responses = [future.result(timeout=30) for future in futures]
            self.assertEqual(responses[0], responses[1])
            self.assertEqual(responses[0]["postedLink"]["check"], "matched")
            self.assertEqual(fetch.call_count, 2)
            record_before = tools.posted_record(tools._config(self.org), self.run.run_id)
            replay = self.post("posted-link", data, "same-posted-operation")
            self.assertEqual(replay, responses[0])
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(tools.posted_record(tools._config(self.org), self.run.run_id), record_before)
        self.assert_no_execution_or_money_effect()

    def test_old_link_command_replay_cannot_replace_newer_link_and_conflict_refuses(self):
        first_url = "https://medium.com/@synthetic/first"
        second_url = "https://medium.com/@synthetic/second"
        first = self.post("posted-link", {"url": first_url}, "first-link-command")
        self.post("posted-link", {"url": second_url}, "second-link-command")
        before = tools.posted_record(tools._config(self.org), self.run.run_id)
        self.assertEqual(before["url"], second_url)
        self.assertEqual(self.post("posted-link", {"url": first_url}, "first-link-command"), first)
        self.assertEqual(tools.posted_record(tools._config(self.org), self.run.run_id), before)
        with self.assertRaises(tools.DraftReceiptConflict):
            tools._store(self.org, self.run.run_id, {"url": second_url}, operation_id="first-link-command")
        self.assertEqual(tools.posted_record(tools._config(self.org), self.run.run_id), before)
        self.assert_no_execution_or_money_effect()
