"""Compact Pulse sync serialization without database access."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
from django.test import SimpleTestCase
from . import service_views as views


ACK_KEYS = {"run_id", "workflow", "domain", "status", "current_step", "step_order",
    "run_request", "error", "resume_available", "created_at", "updated_at"}


class PulseSyncAckTests(SimpleTestCase):
    def test_ack_has_only_control_fields_and_never_traverses_steps(self):
        now = datetime(2026, 10, 18, tzinfo=timezone.utc)
        run = SimpleNamespace(run_id="ack", workflow="startup_monthly_update", domain="example.invalid",
            status="running", current_step="gmail_backfill", step_order=["gmail_backfill"],
            run_request={"frozen": True}, error="", resume_available=False, created_at=now, updated_at=now,
            result={"private": "large source"}, steps=Mock())
        reply = views._serialize_content_factory_run_ack(run)
        self.assertEqual(set(reply), ACK_KEYS)
        self.assertEqual(reply["run_request"], {"frozen": True})
        self.assertIsNone(reply["error"])
        self.assertEqual(reply["updated_at"], now.isoformat())
        self.assertFalse(run.steps.mock_calls)

    def test_put_threads_ack_header_to_snapshot_receiver(self):
        # Exercise the real legacy method underneath its authority decorator;
        # endpoint authorization/cancellation is covered by the DB and guard suites.
        method = views.ContentFactoryRunView._put_legacy.__wrapped__
        for headers, expected in (({"X-Run-Sync-Response": "ack"}, True), ({}, False)):
            with self.subTest(headers=headers), patch.object(views, "_apply_run_snapshot") as apply:
                request = SimpleNamespace(data={"workflow": "startup_monthly_update"}, headers=headers)
                method(views.ContentFactoryRunView(), request, run_id="ack")
                apply.assert_called_once_with("ack", request.data, ack=expected)
