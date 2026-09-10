"""Compile the durable history retry gate without opening a database."""
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch


class SlackHistorySchedulingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mlai.settings")
        import django
        django.setup()

    def test_retry_and_processing_leases_expire_without_excluding_other_errors(self):
        from integrations.models import SlackDmMirrorConversation
        from integrations.services.slack_chat_refresh import exclude_recent_history_attempts

        now = datetime(2026, 9, 11, tzinfo=timezone.utc)
        with patch(
            "django.db.backends.base.base.BaseDatabaseWrapper.ensure_connection",
            side_effect=AssertionError("Scheduling tests must not open a database"),
        ):
            source = SlackDmMirrorConversation.objects.filter(
                grant_id=17, status="live", history_backfilled_at__isnull=True
            )
            query = exclude_recent_history_attempts(source, now=now)
            sql, params = query.query.sql_with_params()
            self.assertIn('NOT (', sql)
            self.assertIn(' OR ', sql)
            self.assertIn('"updated_at" >=', sql)
            self.assertIn('"history_backfilled_at" IS NULL', sql)
            self.assertIn(17, params)
            patterns = [value for value in params if isinstance(value, str) and value.endswith('%')]
            self.assertEqual(len(patterns), 2)
            self.assertTrue(any('processing:' in value for value in patterns))
            self.assertTrue(any('retry:' in value for value in patterns))
            self.assertIn('live', params)
            cutoff = now - timedelta(minutes=5)
            self.assertTrue(any(str(value).startswith(cutoff.strftime('%Y-%m-%d %H:%M:%S')) for value in params))

    def test_worker_records_retry_fence_without_marking_history_complete(self):
        from contextlib import nullcontext
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        from integrations.services import slack_dm_mirror as service
        from integrations.services.slack_chat_refresh import HISTORY_RETRY_PREFIX

        grant = SimpleNamespace(pk=17)
        conversation = SimpleNamespace(pk=42, grant_id=17, grant=grant, save=MagicMock())
        candidates = MagicMock()
        candidates.annotate.return_value.order_by.return_value.values.return_value.first.return_value = {
            "id": 42, "grant_id": 17,
        }
        with (
            patch("django.db.backends.base.base.BaseDatabaseWrapper.ensure_connection", side_effect=AssertionError("No database")),
            patch.object(service.transaction, "atomic", side_effect=lambda: nullcontext()),
            patch.object(service, "_history_scan_available_at", 0),
            patch.object(service, "recover_dead_backfill_deliveries", return_value=0),
            patch("integrations.services.slack_chat_refresh.prioritize_open_conversations", return_value=candidates),
            patch("integrations.services.slack_chat_refresh.exclude_recent_history_attempts", side_effect=lambda query, **kw: query),
            patch.object(service.SlackDmMirrorGrant, "objects") as grants,
            patch.object(service.SlackDmMirrorConversation, "objects") as conversations,
            patch.object(service, "_enqueue_history_page", side_effect=ValueError("source temporarily unavailable")),
            patch.object(service, "_apply_slack_retry_after"),
        ):
            grants.select_for_update.return_value.select_related.return_value.filter.return_value.first.return_value = grant
            grants.select_for_update.return_value.filter.return_value.first.return_value = grant
            conversations.select_for_update.return_value.filter.return_value.first.return_value = conversation
            self.assertEqual(service.process_due_history_backfills(limit=1), 0)
        self.assertTrue(conversation.last_error.startswith(HISTORY_RETRY_PREFIX))
        self.assertNotIn("history_backfilled_at", conversation.save.call_args.kwargs["update_fields"])
