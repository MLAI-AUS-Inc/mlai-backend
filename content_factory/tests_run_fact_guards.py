"""Real ORM/collector retention fences and typed compatibility output proofs."""
from uuid import uuid4

from django.db import transaction
from django.test import TransactionTestCase, override_settings

from core.models import User
from organizations.models import Organization
from roo.models import Ledger
from roo.services import PointsService
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStep, ContentFactoryRunStepAttempt
from .backend_fact_projection import backend_fact_projection
from .models import ContentFactoryJob
from .run_backend_facts import create_job_facts, save_job_facts
from .run_fact_guards import RunFactHistoryProtected
from .run_ownership import bind_admitted_run, reserve_admitted_run, user_principal
from .tests_run_backend_facts import FLAGS


@override_settings(**FLAGS)
class RunFactGuardTests(TransactionTestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic retained facts", domain="retained.example")
        self.run = self.admit()

    def admit(self, *, key=None, ledger=None, workflow="article_generation"):
        payload = {"domain": self.org.domain, "client_request_id": key or "key-" + uuid4().hex}
        if ledger is not None:
            payload["roo_points_ledger_id"] = str(ledger.pk)
        run = reserve_admitted_run(organization=self.org, principal=user_principal(123, "user:123"),
            workflow=workflow, payload=payload)
        return bind_admitted_run(run, "worker-" + uuid4().hex)

    def job(self, run=None, **fields):
        run = run or self.run
        return create_job_facts(job_id=run.run_id, domain=run.domain, slack_user_id="U_SYNTHETIC",
            status="queued", request_meta={"synthetic": True}, **fields)

    def ledger(self, *, refund_of=None):
        # Retention-only rows never credit/debit an account or invoke a provider.
        return Ledger.objects.create(kind="REFUND" if refund_of else "SPEND", source="CONTENT_FACTORY",
            delta=12 if refund_of else -12, delta_microroo=12_000_000 if refund_of else -12_000_000,
            refund_of=refund_of, idempotency_key="retention-" + uuid4().hex)

    def snapshot(self, run=None):
        return ContentFactoryRun.objects.values().get(pk=(run or self.run).pk)

    def test_raw_owned_create_refuses_but_typed_projection_succeeds(self):
        before = self.snapshot()
        with self.assertRaises(RunFactHistoryProtected):
            ContentFactoryJob.objects.create(job_id=self.run.run_id, domain=self.org.domain, slack_user_id="U")
        self.assertFalse(ContentFactoryJob.objects.filter(job_id=self.run.run_id).exists())
        self.assertEqual(self.snapshot(), before)
        job = self.job()
        job.status = "needs_review"
        save_job_facts(job, update_fields=["status"])
        self.assertEqual(ContentFactoryJob.objects.get(pk=job.pk).status, "needs_review")
        self.assertEqual(self.snapshot()["delivery_phase"], "needs_review")
        for clock in ("created_at", "updated_at", "last_event_emitted_at", "reconciled_at"):
            self.assertEqual(self.snapshot()[clock], before[clock])

    def test_raw_saved_owned_rename_cannot_escape_saved_identity(self):
        job = self.job()
        before = self.snapshot()
        job.job_id, job.status = "pretend-legacy", "error"
        with self.assertRaises(RunFactHistoryProtected):
            job.save(update_fields=["job_id", "status"])
        job.refresh_from_db()
        self.assertEqual((job.job_id, job.status), (self.run.run_id, "queued"))
        self.assertEqual(self.snapshot(), before)

    def test_proposed_owned_identity_cannot_be_written_through_a_legacy_job(self):
        job = ContentFactoryJob.objects.create(job_id="unowned-output", domain=self.org.domain, slack_user_id="U")
        job.job_id = self.run.run_id
        with self.assertRaises(RunFactHistoryProtected):
            job.save(update_fields=["job_id"])
        job.refresh_from_db()
        self.assertEqual(job.job_id, "unowned-output")

    def test_long_original_logical_identity_is_guarded_before_column_coercion(self):
        logical = "long-" + "x" * 250
        run = self.admit(key=logical)
        self.assertLessEqual(len(run.run_id), 100)
        with self.assertRaises(RunFactHistoryProtected):
            ContentFactoryJob.objects.create(job_id=logical, domain=self.org.domain, slack_user_id="U")
        job = self.job(run)
        self.assertEqual(job.job_id, run.run_id)

    def test_model_and_queryset_job_delete_preserve_run_and_compatibility_row(self):
        job = self.job()
        before = self.snapshot()
        for remove in (job.delete, lambda: ContentFactoryJob.objects.filter(pk=job.pk).delete()):
            with self.subTest(remove=remove), self.assertRaises(RunFactHistoryProtected):
                remove()
            self.assertTrue(ContentFactoryJob.objects.filter(pk=job.pk).exists())
            self.assertEqual(self.snapshot(), before)

    def test_only_private_projection_context_can_write_compatibility_output(self):
        job = self.job()
        job.progress_message_ts = "synthetic-projection-output"
        with self.assertRaises(RunFactHistoryProtected):
            job.save(update_fields=["progress_message_ts"])
        before = self.snapshot()
        with backend_fact_projection():
            job.save(update_fields=["progress_message_ts"])
        self.assertEqual(self.snapshot(), before)

    def test_canonical_run_delete_refuses_even_with_mutated_instance_and_projection_context(self):
        before = self.snapshot()
        self.run.result = {}
        for remove in (self.run.delete, lambda: ContentFactoryRun.objects.filter(pk=self.run.pk).delete()):
            with self.subTest(remove=remove), backend_fact_projection(), self.assertRaises(RunFactHistoryProtected):
                remove()
            self.assertEqual(self.snapshot(), before)

    def test_flags_off_never_release_existing_owned_job_or_run_history(self):
        job = self.job()
        with override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=False, ARTICLES_RUN_FACT_AUTHORITY_ENABLED=False,
                               ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=False):
            job.status = "error"
            with self.assertRaises(RunFactHistoryProtected):
                job.save(update_fields=["status"])
            with self.assertRaises(RunFactHistoryProtected):
                self.run.delete()
            save_job_facts(job, update_fields=["status"])
            self.assertEqual(self.snapshot()["delivery_phase"], "error")

    def test_original_string_pin_and_refunds_survive_after_current_billing_fk_moves(self):
        original, current = self.ledger(), self.ledger()
        run = self.admit(ledger=original)
        job = self.job(run, billing_ledger=current)
        refund = self.ledger(refund_of=original)
        before = self.snapshot(run)
        for row in (original, current, refund):
            with self.subTest(kind=row.kind), self.assertRaises(RunFactHistoryProtected):
                Ledger.objects.filter(pk=row.pk).delete()
            self.assertTrue(Ledger.objects.filter(pk=row.pk).exists())
            self.assertEqual(self.snapshot(run), before)
            self.assertEqual(ContentFactoryJob.objects.get(pk=job.pk).billing_ledger_id, current.pk)

    def test_original_numeric_pin_is_guarded_without_a_current_foreign_key(self):
        original = self.ledger()
        payload = {"domain": self.org.domain, "client_request_id": "numeric-pin", "roo_points_ledger_id": original.pk}
        run = reserve_admitted_run(organization=self.org, principal=user_principal(123, "user:123"),
            workflow="article_generation", payload=payload)
        self.assertIsNone(run.billing_ledger_id)
        with self.assertRaises(RunFactHistoryProtected):
            original.delete()

    def test_fresh_refund_link_is_used_instead_of_mutated_in_memory_attributes(self):
        original = self.ledger()
        self.admit(ledger=original)
        refund = self.ledger(refund_of=original)
        refund.refund_of = None
        with self.assertRaises(RunFactHistoryProtected):
            refund.delete()
        self.assertEqual(Ledger.objects.get(pk=refund.pk).refund_of_id, original.pk)

    def test_collector_cannot_null_owned_job_and_run_billing_foreign_keys(self):
        ledger = self.ledger()
        job = self.job(billing_ledger=ledger)
        before = self.snapshot()
        with self.assertRaises(RunFactHistoryProtected):
            ledger.delete()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(ContentFactoryJob.objects.get(pk=job.pk).billing_ledger_id, ledger.pk)

    def test_sealed_article_charge_and_its_refund_remain_pinned_when_current_fk_moves(self):
        from .run_article_charge import record_article_charge
        user = User.objects.create_user(email="synthetic-retention@example.test")
        PointsService.credit_purchased_topup(user=user, delta=20, description="Synthetic local funding",
                                            idempotency_key="guard-funding")
        spend, _ = PointsService.spend(user=user, delta=12, source="CONTENT_FACTORY", description="Synthetic article",
            created_by_slack_id="system", idempotency_key="content_factory:charge:sealed-guard",
            reference_type="CONTENT_FACTORY", reference_id="sealed-guard")
        job = self.job(billing_ledger=spend)
        with transaction.atomic():
            record_article_charge(job, ledger=spend, cost_points=12, billing_user=user, requester=user,
                article_request_key="sealed-guard", confirmed_keyword="Approved synthetic topic", custom_title=None)
        job.billing_ledger = self.ledger()
        save_job_facts(job, update_fields=["billing_ledger"])
        refund = self.ledger(refund_of=spend)
        before = self.snapshot()
        for row in (spend, refund):
            with self.subTest(kind=row.kind), self.assertRaises(RunFactHistoryProtected):
                row.delete()
            self.assertEqual(self.snapshot(), before)

    def test_unmarked_historical_jobs_runs_and_ledgers_keep_original_behavior(self):
        ledger = self.ledger()
        run = ContentFactoryRun.objects.create(run_id="unmarked-run", domain="history.example", billing_ledger=ledger)
        job = ContentFactoryJob.objects.create(job_id="unmarked-job", domain="history.example", slack_user_id="U",
                                               billing_ledger=ledger)
        job.status = "generating"
        job.save(update_fields=["status"])
        ledger.delete()
        job.refresh_from_db()
        run.refresh_from_db()
        self.assertIsNone(job.billing_ledger_id)
        self.assertIsNone(run.billing_ledger_id)
        job.delete()
        run.delete()
        self.assertFalse(ContentFactoryJob.objects.filter(pk=job.pk).exists())

    def history(self, run=None):
        from .run_event_authority import receive_authoritative_run_events
        from .tests_run_event_authority import batch
        run = run or self.run
        response = receive_authoritative_run_events(batch(run, [
            {"type": "step_started", "step": "write", "attempt": 1},
            {"type": "step_done", "step": "write", "attempt": 1, "status": "completed"}]))
        self.assertEqual(response.status_code, 200)
        step = run.steps.get(step_key="write")
        return step, step.attempt_history.get(attempt=1)

    def legacy_history(self, key="legacy-history"):
        run = ContentFactoryRun.objects.create(run_id=key, workflow="article_generation", domain="history.example")
        step = ContentFactoryRunStep.objects.create(run=run, step_key="write")
        attempt = ContentFactoryRunStepAttempt.objects.create(step=step, attempt=1)
        return run, step, attempt

    def test_owned_step_and_attempt_direct_queryset_deletes_preserve_real_event_history(self):
        step, attempt = self.history()
        before = self.snapshot()
        for remove in (step.delete, lambda: ContentFactoryRunStep.objects.filter(pk=step.pk).delete(),
                       attempt.delete, lambda: ContentFactoryRunStepAttempt.objects.filter(pk=attempt.pk).delete()):
            with self.subTest(remove=remove), backend_fact_projection(), self.assertRaises(RunFactHistoryProtected):
                remove()
            self.assertEqual(self.snapshot(), before)
            self.assertTrue(ContentFactoryRunStep.objects.filter(pk=step.pk).exists())
            self.assertTrue(ContentFactoryRunStepAttempt.objects.filter(pk=attempt.pk).exists())

    def test_step_and_attempt_deletion_rechecks_saved_parent_after_in_memory_parent_changes(self):
        step, attempt = self.history()
        legacy, legacy_step, _ = self.legacy_history()
        before = self.snapshot()
        step.run, attempt.step = legacy, legacy_step
        for row in (step, attempt):
            with self.subTest(model=type(row).__name__), self.assertRaises(RunFactHistoryProtected):
                row.delete()
        self.assertEqual(ContentFactoryRunStep.objects.get(pk=step.pk).run_id, self.run.pk)
        self.assertEqual(ContentFactoryRunStepAttempt.objects.get(pk=attempt.pk).step_id, step.pk)
        self.assertEqual(self.snapshot(), before)

    def test_existing_steps_cannot_reparent_from_or_into_a_canonical_fact_owner(self):
        step, _ = self.history()
        legacy, legacy_step, _ = self.legacy_history()
        step.run, legacy_step.run = legacy, self.run
        for row in (step, legacy_step):
            with self.subTest(proposed=row.run_id), self.assertRaises(RunFactHistoryProtected):
                row.save(update_fields=["run"])
        self.assertEqual(ContentFactoryRunStep.objects.get(pk=step.pk).run_id, self.run.pk)
        self.assertEqual(ContentFactoryRunStep.objects.get(pk=legacy_step.pk).run_id, legacy.pk)

    def test_existing_attempts_cannot_reparent_from_or_into_owned_steps(self):
        step, attempt = self.history()
        _, legacy_step, legacy_attempt = self.legacy_history()
        attempt.step, legacy_attempt.step = legacy_step, step
        for row in (attempt, legacy_attempt):
            with self.subTest(proposed=row.step_id), self.assertRaises(RunFactHistoryProtected):
                row.save(update_fields=["step"])
        self.assertEqual(ContentFactoryRunStepAttempt.objects.get(pk=attempt.pk).step_id, step.pk)
        self.assertEqual(ContentFactoryRunStepAttempt.objects.get(pk=legacy_attempt.pk).step_id, legacy_step.pk)

    def test_flag_off_still_retains_owned_attempt_and_step_but_unmarked_cascade_stays_legacy(self):
        step, attempt = self.history()
        legacy, legacy_step, legacy_attempt = self.legacy_history()
        with override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=False, ARTICLES_RUN_FACT_AUTHORITY_ENABLED=False,
                               ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=False):
            for row in (step, attempt):
                with self.subTest(model=type(row).__name__), self.assertRaises(RunFactHistoryProtected):
                    row.delete()
            legacy.delete()
        self.assertTrue(ContentFactoryRunStep.objects.filter(pk=step.pk).exists())
        self.assertTrue(ContentFactoryRunStepAttempt.objects.filter(pk=attempt.pk).exists())
        self.assertFalse(ContentFactoryRunStep.objects.filter(pk=legacy_step.pk).exists())
        self.assertFalse(ContentFactoryRunStepAttempt.objects.filter(pk=legacy_attempt.pk).exists())

    def test_new_current_ticket_events_still_append_history_and_keep_prior_attempts(self):
        from .run_event_authority import receive_authoritative_run_events
        from .tests_run_event_authority import advance_ticket, batch
        step, first = self.history()
        self.run.refresh_from_db()
        ticket = advance_ticket(self.run, generation=1)
        payload = batch(self.run, [{"seq": 3, "state_version": 3, "type": "step_started", "step": "write", "attempt": 1},
            {"seq": 4, "state_version": 4, "type": "step_done", "step": "write", "attempt": 1, "status": "completed"}], ticket)
        self.assertEqual(receive_authoritative_run_events(payload).status_code, 200)
        self.assertEqual(list(step.attempt_history.order_by("attempt").values_list("attempt", "status")),
                         [(1, "completed"), (2, "completed")])
        self.assertTrue(ContentFactoryRunStepAttempt.objects.filter(pk=first.pk).exists())
