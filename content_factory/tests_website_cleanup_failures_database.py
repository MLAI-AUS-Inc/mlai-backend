"""Persist cleanup progress without borrowing a newer connection's authority."""
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import TransactionTestCase
from django.utils import timezone
import requests

from .tests_website_connections import WebsiteDatabaseFixture
from .website_models import WebsiteConnectionOperation
from .website_reconciliation import process_website_connection_operations


class CleanupPersistenceTests(WebsiteDatabaseFixture, TransactionTestCase):
    def test_history_reads_retained_and_pending_outcomes_only_for_this_connection(self):
        from organizations.models import Organization
        from .website_models import WebsiteConnection
        from .website_journey import cleanup_history_for_connection
        retained = WebsiteConnectionOperation.objects.create(connection=self.website, generation=1, action="disconnect",
            idempotency_key="retained-history", state="completed", receipt={"remote_cleanup_pending": False,
                "repository_cleanup": {"status": "superseded", "retained": True}})
        pending = WebsiteConnectionOperation.objects.create(connection=self.website, generation=1, action="purge",
            idempotency_key="pending-history", receipt={"remote_cleanup_pending": True})
        WebsiteConnectionOperation.objects.create(connection=self.website, generation=1, action="purge",
            idempotency_key="finished-history", state="completed", receipt={"remote_cleanup_pending": True})
        current = WebsiteConnectionOperation.objects.create(connection=self.website, generation=2, action="prepare",
            idempotency_key="current-prepare", receipt={"remote_cleanup_pending": True})
        foreign_org = Organization.objects.create(domain="foreign-history.example.test", name="Foreign")
        foreign = WebsiteConnection.objects.create(organization=foreign_org, github_repo="example/foreign", repository_id=456)
        WebsiteConnectionOperation.objects.create(connection=foreign, generation=1, action="purge",
            idempotency_key="foreign-history", receipt={"remote_cleanup_pending": True})
        before = list(self.website.operations.values("id", "state", "receipt", "updated_at"))
        with self.assertNumQueries(1):
            result = cleanup_history_for_connection(self.website, current)
        self.assertEqual({row["id"] for row in result}, {str(retained.pk), str(pending.pk)})
        self.assertTrue(next(row for row in result if row["id"] == str(retained.pk))["repositoryRemovalRetained"])
        self.assertEqual(list(self.website.operations.values("id", "state", "receipt", "updated_at")), before)

    def operation(self, generation):
        return WebsiteConnectionOperation.objects.create(connection=self.website, generation=generation,
            action="disconnect", idempotency_key="synthetic-cleanup", payload={"remove_setup": True,
                "previous_generation": generation - 1, "refund_run_ids": [], "stop_preview_run_ids": ["old-preview"]},
            receipt={"completed_steps": ["refunds"]}, next_attempt_at=timezone.now())

    def process(self, response, *, now=None):
        with patch("content_factory.website_reconciliation.queue_verification_refreshes", return_value=[]), \
                patch("content_factory.vibe_marketing_views._content_factory_remote_config",
                    return_value={"enabled": True, "base_url": "https://worker.invalid"}), \
                patch("content_factory.vibe_marketing_views._content_factory_headers", return_value={}), \
                patch("integrations.http_client.post", return_value=response) as post, \
                patch("content_factory.website_tokens.revoke_generation_tokens", return_value={"pending": 0, "revoked": 0}) as revoke:
            process_website_connection_operations(connection_id=self.website.pk, now=now)
        return post, revoke

    def test_old_disconnect_finishes_service_cleanup_and_retains_repository_work(self):
        self.website.generation = 3
        self.website.save(update_fields=["generation"])
        op = self.operation(2)
        before = self.config.article_template
        with patch("content_factory.website_disconnect.close_owned_repository_refs", side_effect=AssertionError("No new repo authority")), \
                patch("content_factory.website_disconnect._open_cleanup", side_effect=AssertionError("No new removal PR")):
            post, revoke = self.process(SimpleNamespace(status_code=200, json=lambda: {"cleanup_success": True}))
        op.refresh_from_db()
        self.website.refresh_from_db()
        self.config.refresh_from_db()
        self.assertEqual(op.state, "completed")
        self.assertEqual(op.receipt["repository_cleanup"]["status"], "superseded")
        self.assertEqual(op.receipt["completed_steps"], ["refunds"])
        self.assertEqual(self.website.generation, 3)
        self.assertEqual(self.config.article_template, before)
        self.assertEqual(op.payload["stop_preview_run_ids"], [])
        self.assertTrue(post.call_args.args[0].endswith("/old-preview/preview/stop"))
        revoke.assert_called_once_with(self.website.pk, 1)

    def test_blocked_repository_waits_for_preview_confirmation_without_repeating_provider_failure(self):
        self.website.state = "disconnected"
        self.website.save(update_fields=["state"])
        op = self.operation(self.website.generation)
        provider_response = requests.Response()
        provider_response.status_code = 403
        error = requests.HTTPError("private provider body", response=provider_response)
        with patch("content_factory.website_disconnect.close_owned_repository_refs", side_effect=error) as refs:
            self.process(SimpleNamespace(status_code=200, json=lambda: {"cleanup_pending": True}))
            op.refresh_from_db()
            self.assertEqual(op.state, "pending")
            self.assertNotIn("private", str(op.receipt))
            self.process(SimpleNamespace(status_code=200, json=lambda: {"cleanup_success": True}), now=op.next_attempt_at)
            refs.assert_called_once()
        op.refresh_from_db()
        self.assertEqual(op.state, "needs_attention")
        self.assertIsNone(op.next_attempt_at)
        self.assertEqual(op.payload["stop_preview_run_ids"], [])

    def test_superseding_repository_work_counts_as_progress_before_preview_polling(self):
        self.website.generation = 3
        self.website.save(update_fields=["generation"])
        op = self.operation(2)
        op.receipt["cleanup_failures"] = 5
        op.save(update_fields=["receipt"])
        self.process(SimpleNamespace(status_code=200, json=lambda: {"cleanup_pending": True}))
        op.refresh_from_db()
        self.assertEqual(op.state, "pending")
        self.assertEqual(op.receipt["cleanup_failures"], 0)
        self.process(SimpleNamespace(status_code=200, json=lambda: {"cleanup_success": True}), now=op.next_attempt_at)
        op.refresh_from_db()
        self.assertEqual(op.state, "completed")
