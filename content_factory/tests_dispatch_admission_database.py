"""Original paid outbox admission, using disposable PostgreSQL CI only."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from roo.models import Ledger
from workflow_runs.models import ContentFactoryRun

from . import dispatch_outbox as outbox, tests_one_click_database as fixtures
from . import vibe_marketing_views as views, website_sealed_workflow as sealed
from .dispatch_models import ContentFactoryDispatchOutbox
from .website_contract import WebsiteAuthorityError
from .website_models import WebsiteConnectionOperation, WebsiteConnectionTarget


@override_settings(ARTICLES_PUBLISHING_INPUTS_V3_ENABLED=False, ARTICLES_WEBSITE_EVENT_WAKES_ENABLED=False)
class DispatchAdmissionDatabaseTests(fixtures.OneClickBillingFixture, TransactionTestCase):
    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("Requires disposable PostgreSQL CI")
        super().setUp()

    def charged_row(self):
        _, _, _, error = self.charge(self.payload())
        self.assertIsNone(error)
        self.assertEqual(Ledger.objects.filter(kind="SPEND").count(), 1)
        self.assertEqual(self.balance(self.payer), 8_000_000)
        return ContentFactoryDispatchOutbox.objects.get()

    @contextmanager
    def remote(self, *, known="unknown", admitted=None):
        # Provider admission is outside the transaction. Its subsequent stored
        # source/target/proof guard, operation authority and all SQL remain real.
        def admission(**_):
            self.assertFalse(connection.in_atomic_block)
            if admitted:
                admitted()
        def delivered(*_, **__):
            self.assertFalse(connection.in_atomic_block)
            return SimpleNamespace(status_code=202, json=lambda: {"run_id": "original-key-child"})
        with patch.object(views, "_content_factory_remote_config", return_value={"enabled": True, "base_url": "https://worker.invalid"}), \
                patch.object(views, "_content_factory_headers", return_value={}), \
                patch.object(views, "_lookup_content_factory_dispatch_by_key", return_value=(known, {"run_id": "original-key-child"})), \
                patch.object(views, "_refresh_article_editorial_payload", return_value=None), \
                patch("integrations.services.article_generation.require_article_activation", side_effect=admission) as activation, \
                patch("integrations.http_client.post", side_effect=delivered) as post:
            yield post, activation

    def assert_held(self, row, original):
        row.refresh_from_db()
        self.assertEqual(row.payload, original)
        self.assertFalse(Ledger.objects.filter(kind="REFUND").exists())
        self.assertEqual(Ledger.objects.filter(kind="SPEND").count(), 1)
        self.assertEqual(self.balance(self.payer), 8_000_000)

    def test_actual_scheduler_bounds_unknown_legacy_dispatch_without_post_or_refund(self):
        row = self.charged_row()
        row.payload["request"].pop("connection_target_id")
        row.payload["request"].pop("expected_source_sha")
        row.save(update_fields=["payload"])
        original = deepcopy(row.payload)
        with self.remote() as (post, _):
            for _ in range(outbox.MAX_ATTEMPTS):
                outbox.process_dispatch_outbox(now=row.next_attempt_at + timedelta(seconds=1))
                row.refresh_from_db()
            self.assertEqual(row.state, "awaiting_resolution")
            self.assertEqual(row.attempts, outbox.MAX_ATTEMPTS)
            outbox.process_dispatch_outbox(now=row.next_attempt_at + timedelta(seconds=1))
            row.refresh_from_db()
            self.assertEqual(row.attempts, outbox.MAX_ATTEMPTS)
            post.assert_not_called()
        self.assert_held(row, original)
        self.assertFalse(ContentFactoryRun.objects.filter(run_id="original-key-child").exists())

    def test_committed_default_switch_after_admission_cannot_post_original_target(self):
        row = self.charged_row()
        original = deepcopy(row.payload)
        def changed():
            target = WebsiteConnectionTarget.objects.get(connection=self.website)
            target.pk = None
            target.target_key = "other-ready-target"
            target.save()
            self.config.default_publish_target_id = target.target_key
            self.config.save(update_fields=["default_publish_target_id"])
        with self.remote(admitted=changed) as (post, _):
            with self.assertRaises(WebsiteAuthorityError):
                outbox._deliver(row)
            post.assert_not_called()
        self.assert_held(row, original)

    def test_saved_target_and_source_cannot_fill_missing_original_operation(self):
        row = self.charged_row()
        original = deepcopy(row.payload)
        for missing in ("operation_id", "operation_attempt", "deletion_epoch"):
            with self.subTest(missing=missing):
                row.payload = deepcopy(original)
                row.payload["request"].pop(missing)
                row.save(update_fields=["payload"])
                with self.remote() as (post, _):
                    with self.assertRaises(WebsiteAuthorityError):
                        outbox._deliver(row)
                    post.assert_not_called()
                self.assert_held(row, deepcopy(row.payload))

    def test_committed_source_change_after_admission_cannot_post_original_source(self):
        row = self.charged_row()
        original = deepcopy(row.payload)
        def changed():
            self.website.verified_sha = "b" * 40
            self.website.save(update_fields=["verified_sha"])
            WebsiteConnectionTarget.objects.filter(connection=self.website).update(source_sha=self.website.verified_sha)
        with self.remote(admitted=changed) as (post, _):
            with self.assertRaises(WebsiteAuthorityError):
                outbox._deliver(row)
            post.assert_not_called()
        self.assert_held(row, original)

    def test_committed_terminal_operation_cannot_start_another_child(self):
        row = self.charged_row()
        original = deepcopy(row.payload)
        def changed():
            WebsiteConnectionOperation.objects.filter(pk=original["request"]["operation_id"]).update(state="completed")
        with self.remote(admitted=changed) as (post, _):
            with self.assertRaises(WebsiteAuthorityError):
                outbox._deliver(row)
            post.assert_not_called()
        self.assert_held(row, original)

    def test_committed_pending_proof_after_admission_blocks_post_with_same_source(self):
        row = self.charged_row()
        original = deepcopy(row.payload)
        def changed():
            sealed.mark_required(WebsiteConnectionTarget.objects.get(connection=self.website))
        with self.remote(admitted=changed) as (post, _), CaptureQueriesContext(connection) as queries:
            with self.assertRaises(WebsiteAuthorityError):
                outbox._deliver(row)
            post.assert_not_called()
        self.assertTrue(any("FOR UPDATE" in query["sql"] for query in queries.captured_queries))
        self.assert_held(row, original)

    def test_original_exact_outbox_delivers_once_and_binds_existing_operation(self):
        row = self.charged_row()
        original = deepcopy(row.payload)
        with self.remote(known="not_received") as (post, _):
            self.assertEqual(outbox.process_dispatch_outbox(now=timezone.now() + timedelta(seconds=1))["delivered"], 1)
            row.refresh_from_db()
            self.assertEqual(outbox.process_dispatch_outbox(now=row.next_attempt_at + timedelta(minutes=1))["processed"], 0)
            post.assert_called_once()
            self.assertEqual(post.call_args.kwargs["json"], original["request"])
        self.assertEqual(row.state, "delivered")
        self.assertEqual(WebsiteConnectionOperation.objects.get(action="workflow").payload["run_id"], row.run_id)
        self.assertEqual(ContentFactoryRun.objects.get(run_id=row.run_id).run_request["expected_source_sha"], "a" * 40)
        self.assert_held(row, original)

    def test_portable_flags_do_not_bypass_a_saved_website_and_operation(self):
        row = self.charged_row()
        row.payload["request"].update(delivery_mode="content_only", delivery_mode_confirmed=True)
        row.save(update_fields=["payload"])
        original = deepcopy(row.payload)
        def changed():
            sealed.mark_required(WebsiteConnectionTarget.objects.get(connection=self.website))
        with self.remote(admitted=changed) as (post, _):
            with self.assertRaises(WebsiteAuthorityError):
                outbox._deliver(row)
            post.assert_not_called()
        self.assert_held(row, original)

    def test_exact_owned_child_recovery_remains_lookup_only_with_missing_history(self):
        row = self.charged_row()
        row.payload["request"].pop("connection_target_id")
        row.payload["request"].pop("expected_source_sha")
        row.save(update_fields=["payload"])
        original = deepcopy(row.payload)
        sealed.mark_required(WebsiteConnectionTarget.objects.get(connection=self.website))
        ContentFactoryRun.objects.create(run_id=row.client_request_id, organization=self.org, domain=self.org.domain,
            github_repo=self.website.github_repo, workflow="article_generation", status="queued", run_request=original["request"])
        with self.remote(known="started") as (post, activation):
            self.assertEqual(outbox.process_dispatch_outbox(now=timezone.now() + timedelta(seconds=1))["delivered"], 1)
            post.assert_not_called(); activation.assert_not_called()
        row.refresh_from_db()
        self.assertEqual(row.state, "delivered")
        self.assertEqual(ContentFactoryRun.objects.get().run_id, "original-key-child")
        self.assert_held(row, original)
