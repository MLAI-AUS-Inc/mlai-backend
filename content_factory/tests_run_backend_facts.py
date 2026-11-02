"""Actual canonical patches and compatibility output, without provider effects."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from unittest.mock import patch

from django.db import close_old_connections
from django.test import TransactionTestCase, override_settings

from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun
from .models import ContentFactoryJob
from .run_ownership import bind_admitted_run, reserve_admitted_run, user_principal
from .run_backend_facts import (BackendFactConflict, create_job_facts, fact_run_for_job,
    get_or_create_job_facts, job_client_key, logical_client_request_id, save_job_facts,
    update_job_facts, update_or_create_job_facts)
from .run_backend_facts import owned_job_transaction

FLAGS = {"ARTICLES_RUN_OWNERSHIP_ENABLED": True, "ARTICLES_RUN_FACT_AUTHORITY_ENABLED": True,
         "ARTICLES_RUN_EVENT_AUTHORITY_ENABLED": True}


@override_settings(**FLAGS)
class BackendFactTests(TransactionTestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic fact owner", domain="facts.example")
        self.run = self.admit()
        self.original = ContentFactoryRun.objects.values().get(pk=self.run.pk)

    def admit(self, key="facts-original"):
        payload = {"domain": self.org.domain, "client_request_id": key, "topic": "Original topic"}
        run = reserve_admitted_run(organization=self.org, principal=user_principal(123, "user:123"),
                                  workflow="article_generation", payload=payload)
        return bind_admitted_run(run, "worker-" + key[:30])

    def create(self, **kwargs):
        return create_job_facts(job_id=self.run.run_id, domain=self.org.domain, slack_user_id="U_SYNTHETIC",
            status="generating", request_meta={"section": {"a": 1, "b": 1}}, **kwargs)

    def assert_execution_unchanged(self):
        after = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        for key in ("created_at", "updated_at", "last_event_emitted_at", "reconciled_at", "status",
                    "current_step", "run_request", "approval_state", "result"):
            self.assertEqual(after[key], self.original[key], key)

    def test_run_facts_exist_before_compatibility_create_and_keep_execution_clocks(self):
        original_create = ContentFactoryJob.objects.create
        def observe(**fields):
            run = ContentFactoryRun.objects.get(pk=self.run.pk)
            self.assertEqual(run.delivery_phase, "generating")
            self.assertEqual(run.job_meta["section"], {"a": 1, "b": 1})
            return original_create(**fields)
        with patch.object(ContentFactoryJob.objects, "create", side_effect=observe):
            job = self.create()
        self.run.refresh_from_db()
        self.assertEqual((self.run.job_created_at, self.run.job_updated_at), (job.created_at, job.updated_at))
        self.assertEqual(self.run.job_domain, job.domain)
        self.assertEqual(logical_client_request_id(job), "facts-original")
        self.assert_execution_unchanged()

    def test_projection_failure_rolls_back_every_canonical_patch(self):
        with patch.object(ContentFactoryJob.objects, "create", side_effect=RuntimeError("synthetic output failure")), \
                self.assertRaises(RuntimeError):
            self.create()
        self.assertFalse(ContentFactoryJob.objects.exists())
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), self.original)

    def test_metadata_baselines_preserve_unrelated_concurrent_nested_keys(self):
        first = self.create()
        second = ContentFactoryJob.objects.get(pk=first.pk)
        first.request_meta["section"]["a"] = 2
        save_job_facts(first, update_fields=["request_meta"])
        second.request_meta["section"]["b"] = 3
        save_job_facts(second, update_fields=["request_meta"])
        self.run.refresh_from_db()
        self.assertEqual(self.run.job_meta["section"], {"a": 2, "b": 3})
        self.assert_execution_unchanged()

    def test_same_metadata_key_conflict_refuses_instead_of_overwriting_newer_value(self):
        first = self.create()
        second = ContentFactoryJob.objects.get(pk=first.pk)
        first.request_meta["section"]["a"] = 2
        save_job_facts(first, update_fields=["request_meta"])
        second.request_meta["section"]["a"] = 3
        with self.assertRaises(BackendFactConflict):
            save_job_facts(second, update_fields=["request_meta"])
        self.run.refresh_from_db()
        self.assertEqual(self.run.job_meta["section"]["a"], 2)

    def test_explicit_factory_metadata_delta_keeps_unspecified_nested_keys(self):
        self.create()
        job, created = update_or_create_job_facts(job_id=self.run.run_id,
            defaults={"request_meta": {"section": {"a": 7}}})
        self.assertFalse(created)
        self.assertEqual(job.request_meta["section"], {"a": 7, "b": 1})

    def test_distinct_fact_domains_update_concurrently_without_stale_full_row_copy(self):
        first = self.create()
        second = ContentFactoryJob.objects.get(pk=first.pk)
        barrier = Barrier(2)
        def write(job, name, value):
            close_old_connections()
            try:
                setattr(job, name, value)
                barrier.wait(timeout=10)
                save_job_facts(job, update_fields=[name])
            finally:
                close_old_connections()
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(write, first, "status", "needs_review"),
                       pool.submit(write, second, "progress_message_ts", "123.456")]
            for future in futures:
                future.result(timeout=20)
        self.run.refresh_from_db()
        self.assertEqual((self.run.delivery_phase, self.run.progress_message_ts), ("needs_review", "123.456"))
        self.assert_execution_unchanged()

    def test_owned_partial_save_preserves_job_ordering_unless_explicitly_requested(self):
        job = self.create()
        before = job.updated_at
        job.status = "error"
        save_job_facts(job, update_fields=["status"])
        self.assertEqual(job.updated_at, before)
        job.status = "needs_review"
        save_job_facts(job, update_fields=["status", "updated_at"])
        self.assertGreater(job.updated_at, before)
        self.assert_execution_unchanged()

    def test_full_owned_save_and_lifecycle_dialect_are_refused(self):
        job = self.create()
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        with self.assertRaises(BackendFactConflict):
            save_job_facts(job)
        job.status = "running"
        with self.assertRaises(BackendFactConflict):
            save_job_facts(job, update_fields=["status"])
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), before)

    def test_factories_and_bulk_updates_preserve_owner_even_when_flags_turn_off(self):
        job, created = get_or_create_job_facts(job_id=self.run.run_id,
            defaults={"domain": self.org.domain, "slack_user_id": "U_SYNTHETIC", "status": "queued"})
        self.assertTrue(created)
        with override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=False, ARTICLES_RUN_FACT_AUTHORITY_ENABLED=False):
            same, created = update_or_create_job_facts(job_id=self.run.run_id,
                defaults={"status": "generating", "request_meta": {"new": True}})
            self.assertFalse(created)
            self.assertEqual(same.pk, job.pk)
            count = update_job_facts(ContentFactoryJob.objects.filter(pk=job.pk), status="completed")
        self.assertEqual(count, 1)
        self.run.refresh_from_db()
        self.assertEqual(self.run.delivery_phase, "completed")
        self.assertTrue(self.run.job_meta["new"])

    def test_long_logical_key_has_bounded_projection_and_no_truncation(self):
        key = "long-original-" + "x" * 150
        run = self.admit(key)
        job = create_job_facts(job_id=run.run_id, domain=self.org.domain, slack_user_id="U_LONG")
        self.assertLessEqual(len(job.client_request_id), 100)
        self.assertEqual(job.client_request_id, job_client_key(key))
        self.assertEqual(logical_client_request_id(job), key)
        self.assertEqual(fact_run_for_job(key).pk, run.pk)
        self.assertEqual(job.request_meta["backend_client_request_id"], key)
        from .job_view import RunJobView
        run.refresh_from_db()
        self.assertEqual(RunJobView(run).client_request_id, key)

    def test_legacy_and_reservation_only_jobs_keep_existing_orm_behavior(self):
        legacy = create_job_facts(job_id="historical-only", domain=self.org.domain, slack_user_id="U_OLD")
        legacy.status = "running"  # Existing ORM save does not call full_clean.
        save_job_facts(legacy, update_fields=["status"])
        legacy.refresh_from_db()
        self.assertEqual(legacy.status, "running")
        self.assertEqual(ContentFactoryRun.objects.count(), 1)
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), self.original)

    def test_one_explicit_legacy_full_save_switch_does_not_expand_an_owned_patch(self):
        legacy = create_job_facts(job_id="legacy-full", domain=self.org.domain, slack_user_id="U_OLD")
        legacy.selection_reason = "Legacy field outside partial list"
        legacy.status = "completed"
        save_job_facts(legacy, update_fields=["status"], legacy_full_save=True)
        legacy.refresh_from_db()
        self.assertEqual(legacy.selection_reason, "Legacy field outside partial list")
        owned = self.create()
        owned.selection_reason = "Stale unrelated value must not be copied"
        owned.status = "completed"
        save_job_facts(owned, update_fields=["status"], legacy_full_save=True)
        self.assertIsNone(owned.selection_reason)

    def test_owned_transaction_repairs_corrupt_output_before_any_caller_reads(self):
        job = self.create()
        ContentFactoryJob.objects.filter(pk=job.pk).update(status="error", billing_amount=777,
            request_meta={"corrupt_output": True})
        with owned_job_transaction(job) as current:
            self.assertEqual(current.status, "generating")
            self.assertEqual(current.billing_amount, 0)
            self.assertNotIn("corrupt_output", current.request_meta)
        self.assert_execution_unchanged()

    def test_missing_projection_rebuild_preserves_canonical_identity_clocks(self):
        job = self.create()
        self.run.refresh_from_db()
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        # Simulate external output damage, bypassing ordinary deletion guards.
        ContentFactoryJob.objects.filter(pk=job.pk)._raw_delete("default")
        with owned_job_transaction(self.run.run_id) as rebuilt:
            self.assertEqual(rebuilt.created_at, before["job_created_at"])
            self.assertEqual(rebuilt.updated_at, before["job_updated_at"])
            self.assertEqual(rebuilt.status, "generating")
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), before)

    def test_transaction_cannot_initialize_an_untyped_empty_owner(self):
        with self.assertRaises(BackendFactConflict), owned_job_transaction(self.run.run_id):
            self.fail("An uninitialized admission is not a source of backend facts")
        self.assertFalse(ContentFactoryJob.objects.exists())

    def test_foreign_ledger_cannot_be_added_to_an_unpaid_admission(self):
        from core.models import User
        from roo.services import PointsService
        user = User.objects.create_user(email="unrelated-fact-ledger@example.test")
        ledger, _ = PointsService.credit_purchased_topup(user=user, delta=12,
            description="Unrelated synthetic credit", idempotency_key="unrelated-fact-credit")
        with self.assertRaises(BackendFactConflict):
            self.create(billing_ledger=ledger)
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), self.original)
