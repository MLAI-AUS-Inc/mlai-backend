"""A stop follow-up stops waiting on a preview teardown Content Factory never confirms.

TalaThrive (13-16 Oct): stopping preparation twice left cancel-operation follow-ups
d61fbc9f and a519a81b pending for 79 and 49 hourly attempts. Their setup runs
were already cancelled and their tokens revoked, but each static Pages preview
stop answered 202 ``cleanup_pending``: the Content Factory web service had no
``CLOUDFLARE_PAGES_PROJECT`` (``pages_cleanup_credentials_unavailable``). The
backend retried that preview stop forever. After seven days it now settles the
follow-up with a receipt naming the unconfirmed previews.
"""
from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
import uuid

from django.test import SimpleTestCase
from django.utils import timezone

RUN = "33adcd1c-e370-4983-99d3-e3698c25d610"
PENDING_STOP = {"status": "cleanup_pending", "cleanup_pending": True, "cleanup_success": False,
    "pages_cleanup": {"status": "cleanup_pending", "deployments": [{"status": "cleanup_pending",
        "reason": "pages_cleanup_credentials_unavailable"}]}}


class CancelPreviewStopDeadlineTests(SimpleTestCase):
    def setUp(self):
        self.now = timezone.now()

    def _operation(self, *, age, cancel_run_ids=(), **payload):
        return SimpleNamespace(pk=uuid.uuid4(), generation=3, action="cancel-operation", state="pending", attempts=48,
            next_attempt_at=None, created_at=self.now - age, updated_at=self.now, connection_id=uuid.uuid4(),
            connection=SimpleNamespace(pk=uuid.uuid4()), save=Mock(),
            payload={"cancel_run_ids": list(cancel_run_ids), "stop_preview_run_ids": [RUN], "purge_run_ids": [RUN],
                "cancelled_action": "prepare", "cancelled_operation_id": str(uuid.uuid4()), "previous_generation": 3,
                "disable_auto_merge_prs": [], "cancelled_child_operation_ids": [], **payload},
            receipt={"status": "cancelled_by_user", "message": "Website preparation stopped.", "tokens": {"pending": 0, "revoked": 0}})

    def _process(self, op, *, stop=PENDING_STOP, stop_status=202, cancel_status=200, tokens=None, remote_enabled=True):
        from integrations import http_client
        from . import vibe_marketing_views as views
        from . import website_connections as authority
        from . import website_reconciliation as reconciliation
        from . import website_support as support
        from . import website_tokens
        rows = Mock()
        rows.exists.return_value = False
        rows.filter.return_value = rows
        rows.exclude.return_value.order_by.return_value.values_list.return_value = [op.pk]
        rows.update.return_value = 1
        manager = Mock()
        manager.filter.return_value = rows
        manager.select_for_update.return_value.select_related.return_value.filter.return_value.first.return_value = op

        def post(url, **kwargs):
            if url.endswith("/preview/stop"):
                return SimpleNamespace(status_code=stop_status, json=lambda: stop)
            return SimpleNamespace(status_code=cancel_status, json=lambda: {"status": "cancelled"})

        with patch.object(reconciliation.transaction, "atomic", side_effect=lambda: nullcontext()), \
                patch.object(reconciliation.WebsiteConnectionOperation, "objects", manager), \
                patch.object(reconciliation, "monitor_cleanup_pull_requests"), \
                patch.object(reconciliation, "disable_pending_native_auto_merge", return_value=[]), \
                patch.object(support, "reconcile_support_verifications"), \
                patch.object(authority, "require_unlocked_remote_call"), \
                patch.object(website_tokens, "revoke_generation_tokens", return_value=tokens or {"revoked": 0, "pending": 0}), \
                patch.object(views, "_content_factory_remote_config", return_value={"enabled": remote_enabled, "base_url": "https://cf.test"}), \
                patch.object(views, "_content_factory_headers", return_value={}), \
                patch.object(http_client, "post", side_effect=post) as posted:
            reconciliation.process_website_connection_operations(now=self.now)
        written = rows.update.call_args.kwargs
        return written, posted

    def test_a_week_old_stop_settles_when_only_its_preview_teardown_is_unconfirmed(self):
        op = self._operation(age=timedelta(days=7, minutes=1))

        written, posted = self._process(op)

        self.assertTrue(posted.call_args.args[0].endswith(f"/api/runs/{RUN}/preview/stop"))
        self.assertEqual(written["state"], "completed")
        receipt = written["receipt"]
        self.assertEqual(receipt["status"], "timed_out")
        self.assertEqual(receipt["unconfirmed_preview_run_ids"], [RUN])
        self.assertTrue(receipt["preview_cleanup_pending"])
        self.assertTrue(receipt["remote_cleanup_pending"])
        self.assertEqual(receipt["message"], "Stopped. Content Factory did not confirm its preview was removed.")
        self.assertEqual(written["payload"]["stop_preview_run_ids"], [RUN])

    def test_a_younger_stop_keeps_retrying_its_preview_teardown(self):
        op = self._operation(age=timedelta(days=6, hours=23))

        written, _ = self._process(op)

        self.assertEqual(written["state"], "pending")
        self.assertEqual(written["receipt"]["status"], "cancelled_by_user")
        self.assertTrue(written["receipt"]["preview_cleanup_pending"])

    def test_a_confirmed_teardown_still_completes_cleanly(self):
        op = self._operation(age=timedelta(days=9))

        written, _ = self._process(op, stop={"status": "stopped", "cleanup_pending": False, "cleanup_success": True},
            stop_status=200)

        self.assertEqual(written["state"], "completed")
        self.assertEqual(written["receipt"]["status"], "completed")
        self.assertFalse(written["receipt"]["remote_cleanup_pending"])
        self.assertNotIn("unconfirmed_preview_run_ids", written["receipt"])

    def test_the_deadline_never_skips_unfinished_cancellation_or_token_revocation(self):
        for label, op, cancel_status, tokens in (
                ("run cancel unconfirmed", self._operation(age=timedelta(days=9), cancel_run_ids=[RUN]), 503, None),
                ("token revocation pending", self._operation(age=timedelta(days=9)), 200, {"revoked": 0, "pending": 1})):
            with self.subTest(label):
                written, _ = self._process(op, cancel_status=cancel_status, tokens=tokens)

                self.assertEqual(written["state"], "pending")
                self.assertNotIn("unconfirmed_preview_run_ids", written["receipt"])

    def test_the_deadline_needs_content_factory_to_have_been_asked(self):
        op = self._operation(age=timedelta(days=9))

        written, posted = self._process(op, remote_enabled=False)

        posted.assert_not_called()
        self.assertEqual(written["state"], "pending")

    def test_offboarding_cleanup_is_never_settled_by_the_deadline(self):
        op = self._operation(age=timedelta(days=9), purge_after_reconciliation=True)

        written, _ = self._process(op)

        self.assertEqual(written["state"], "pending")
