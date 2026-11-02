"""Canonical original and separately approved charge read proofs."""
from copy import deepcopy
from uuid import uuid4

from django.db import transaction
from django.test import TransactionTestCase, override_settings

from core.models import User
from roo.models import Ledger
from roo.services import PointsService
from workflow_runs.models import ContentFactoryRun
from .billing_lineage import original_article_spend_for_run, original_spend_for_job, original_spend_for_run
from .no_delivery_refunds import carry_charge
from .run_backend_facts import create_job_facts, logical_client_request_id, save_job_facts
from .run_ownership import bind_admitted_run, reserve_admitted_run, seal_new_run_admission, user_principal
from .self_heal_testing import make_p2_org
from .tests_run_event_authority import FLAGS


@override_settings(**FLAGS)
class OwnedBillingLineageTests(TransactionTestCase):
    def setUp(self):
        self.fixture = make_p2_org()
        self.org, self.payer = self.fixture.organization, self.fixture.owner
        PointsService.credit_purchased_topup(user=self.payer, delta=100,
            description="Synthetic local original-charge funding", idempotency_key="fund-" + uuid4().hex)

    def job(self, run, spend, *, status="charged"):
        return create_job_facts(job_id=run.run_id, domain=run.domain, slack_user_id=run.slack_user_id,
            status="queued", client_request_id=run.run_request["client_request_id"],
            billing_ledger=spend, billing_amount=12, billing_status=status,
            billing_source_job_id=run.run_request.get("original_billing_source_run_id") or run.run_id,
            request_meta=deepcopy(run.run_request))

    def paid(self, key=None, *, topic=False):
        from integrations.services.article_generation import _ledger_reference_id
        key = key or "original-" + uuid4().hex
        prefix = "content_factory:topic_generation:charge:" if topic else "content_factory:charge:"
        spend, _ = PointsService.spend(user=self.payer, delta=12, source="CONTENT_FACTORY",
            description="Synthetic original debit", created_by_slack_id=f"mlai_user:{self.payer.pk}",
            idempotency_key=prefix + key, reference_type="CONTENT_FACTORY", reference_id=_ledger_reference_id(key))
        request = {"domain": self.org.domain, "client_request_id": key, "topic": "The original approved topic",
            "roo_points_ledger_id": spend.pk, "roo_points_cost": 12, "roo_points_billing_user_id": self.payer.pk,
            "roo_points_billing_status": "charged", "roo_points_authorized": True}
        if topic:
            request["roo_points_action"] = "content_island_topic_generation"
        run = reserve_admitted_run(organization=self.org, workflow="auto_discovery" if topic else "article_generation",
            principal=user_principal(self.payer.pk, f"mlai_user:{self.payer.pk}"), payload=request)
        run = bind_admitted_run(run, "worker-" + uuid4().hex)
        return run, self.job(run, spend), spend

    def child(self, source, *, requester=None, source_override=None):
        requester = requester or self.payer
        with transaction.atomic():
            child = ContentFactoryRun.objects.create(run_id="child-" + uuid4().hex, organization=self.org,
                domain=self.org.domain, workflow=source.workflow, current_step="queued",
                run_request={"domain": self.org.domain, "client_request_id": "child-key-" + uuid4().hex,
                             "topic": "A separately admitted continuation"})
            carry_charge(source_run=source, new_run=child)
            if source_override is not None:
                ContentFactoryRun.objects.filter(pk=child.pk).update(run_request={**child.run_request,
                    "original_billing_source_run_id": source_override.run_id})
                child.refresh_from_db()
            seal_new_run_admission(child, created=True,
                principal=user_principal(requester.pk, f"mlai_user:{requester.pk}"))
        spend = original_spend_for_run(source)
        return child, self.job(child, spend, status="reused")

    def test_distinct_owned_child_key_resolves_its_original_payers_real_spend(self):
        source, _, spend = self.paid()
        requester = User.objects.create_user(email="different-requester@synthetic.example.test")
        child, job = self.child(source, requester=requester)
        self.assertNotEqual(logical_client_request_id(job), source.run_request["client_request_id"])
        self.assertEqual(original_spend_for_job(job).pk, spend.pk)
        self.assertEqual(original_article_spend_for_run(child).pk, spend.pk)
        self.assertEqual(original_spend_for_job(job).user_id, self.payer.pk)
        self.assertNotEqual(self.payer.pk, requester.pk)
        self.assertEqual(Ledger.objects.filter(kind="SPEND", source="CONTENT_FACTORY").count(), 1)

    def test_long_logical_key_proof_survives_alias_projection_and_new_admission_flags_off(self):
        key = "logical-" + "x" * 152
        source, job, spend = self.paid(key)
        before = ContentFactoryRun.objects.values().get(pk=source.pk)
        self.assertLessEqual(len(job.client_request_id), 100)
        self.assertEqual(logical_client_request_id(job), key)
        with override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=False, ARTICLES_RUN_FACT_AUTHORITY_ENABLED=False,
                               ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=False):
            self.assertEqual(original_spend_for_job(job).pk, spend.pk)
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=source.pk), before)

    def test_foreign_or_unrelated_lineage_does_not_prove_a_carried_original_charge(self):
        first, _, _ = self.paid()
        unrelated, _, _ = self.paid()
        child, job = self.child(first, source_override=unrelated)
        self.assertIsNone(original_spend_for_job(job))
        self.assertIsNone(original_article_spend_for_run(child))
        from organizations.models import Organization
        foreign = Organization.objects.create(name="Foreign synthetic tenant", domain="foreign-lineage.example")
        ContentFactoryRun.objects.filter(pk=unrelated.pk).update(organization=foreign)
        self.assertIsNone(original_spend_for_job(job))

    def test_refund_retains_historical_spend_proof_but_original_authorized_flag_never_allows_reuse(self):
        source, source_job, spend = self.paid()
        child, child_job = self.child(source)
        original_request = deepcopy(source.run_request)
        refund, _ = PointsService.refund(user=self.payer, delta=12, source="CONTENT_FACTORY",
            description="Synthetic legacy-key reversal", created_by_slack_id="system",
            original_spend_key=spend.idempotency_key, idempotency_key="legacy-refund-" + uuid4().hex)
        source_job.billing_status, source_job.billing_ledger_id = "refunded", refund.pk
        save_job_facts(source_job, update_fields=["billing_status", "billing_ledger_id"])
        self.assertEqual(original_spend_for_job(source_job).pk, spend.pk)
        self.assertEqual(original_spend_for_job(child_job).pk, spend.pk)
        source.refresh_from_db()
        self.assertEqual(source.run_request, original_request)
        self.assertTrue(source.run_request["roo_points_authorized"])
        target = ContentFactoryRun.objects.create(run_id="refused-reuse", organization=self.org, domain=self.org.domain)
        with self.assertRaisesRegex(ValueError, "refunded charge"):
            carry_charge(source_run=source, new_run=target)
        self.assertEqual(spend.service_refunds.count(), 1)

    def test_topic_charge_is_distinct_from_article_payment_and_uses_same_canonical_lineage(self):
        source, job, spend = self.paid(topic=True)
        self.assertEqual(original_spend_for_job(job).pk, spend.pk)
        self.assertEqual(original_spend_for_run(source).pk, spend.pk)
        self.assertIsNone(original_article_spend_for_run(source))

    def confirmed_article(self, source, job):
        from integrations.services.article_generation import _ledger_reference_id
        from .run_article_charge import record_article_charge
        key = "confirmed-" + uuid4().hex
        spend, _ = PointsService.spend(user=self.payer, delta=12, source="CONTENT_FACTORY",
            description="Synthetic separately approved article", created_by_slack_id=f"mlai_user:{self.payer.pk}",
            idempotency_key="content_factory:charge:" + key, reference_type="CONTENT_FACTORY",
            reference_id=_ledger_reference_id(key))
        job.request_meta = {**job.request_meta, "client_request_id": key,
            "roo_points_action": "article_generation", "roo_points_ledger_id": spend.pk}
        with transaction.atomic():
            receipt = record_article_charge(job, ledger=spend, cost_points=12, billing_user=self.payer,
                requester=self.payer, article_request_key=key, confirmed_keyword="Approved article", custom_title=None)
            job.billing_ledger_id, job.billing_amount = spend.pk, 12
            save_job_facts(job, update_fields=["request_meta", "billing_ledger_id", "billing_amount"])
        return receipt, spend

    def test_separately_approved_article_is_current_job_charge_but_initial_research_read_is_preserved(self):
        source, job, research = self.paid(topic=True)
        before = ContentFactoryRun.objects.values().get(pk=source.pk)
        receipt, article = self.confirmed_article(source, job)
        source.refresh_from_db()
        self.assertEqual(original_spend_for_job(job).pk, article.pk)
        self.assertEqual(original_spend_for_run(source).pk, research.pk)
        self.assertEqual(source.run_request, before["run_request"])
        self.assertEqual((source.created_at, source.updated_at), (before["created_at"], before["updated_at"]))
        self.assertEqual(logical_client_request_id(job), source.run_request["client_request_id"])
        self.assertNotEqual(receipt["article_request_key"], logical_client_request_id(job))

    def test_corrupt_or_missing_seal_never_substitutes_initial_research_debit(self):
        from .run_ownership import ARTICLE_CHARGE_RESULT_KEY
        source, job, research = self.paid(topic=True)
        receipt, article = self.confirmed_article(source, job)
        source.refresh_from_db()
        saved = deepcopy(source.result)
        for broken in (None, {**receipt, "ledger_id": research.pk}):
            result = deepcopy(saved)
            if broken is None:
                result.pop(ARTICLE_CHARGE_RESULT_KEY)
            else:
                result[ARTICLE_CHARGE_RESULT_KEY] = broken
            ContentFactoryRun.objects.filter(pk=source.pk).update(result=result)
            self.assertIsNone(original_spend_for_job(job))
        ContentFactoryRun.objects.filter(pk=source.pk).update(result=saved)
        Ledger.objects.filter(pk=article.pk).update(user=User.objects.create_user(email="foreign-payer@synthetic.example.test"))
        self.assertIsNone(original_spend_for_job(job))
