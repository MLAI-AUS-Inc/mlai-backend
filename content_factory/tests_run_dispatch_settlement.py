"""Actual withdrawal, original-charge and canonical scope transaction proofs."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
import json
from threading import Event
from unittest.mock import Mock, patch
from uuid import uuid4

from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from roo.models import Ledger
from roo.services import PointsService
from workflow_runs.models import ContentFactoryRun
from . import run_dispatch_settlement as settle
from . import run_reconciliation as reconcile
from .run_backend_facts import create_job_facts
from .run_ownership import (RECONCILIATION_RESULT_KEY, WIRE_EXECUTION_KEY, WIRE_REQUEST_KEY,
    current_execution_ticket, owner_receipt, reserve_admitted_run, user_principal, wire_identity)
from .self_heal_testing import make_p2_org
from .tests_run_event_authority import FLAGS, advance_ticket


@override_settings(**FLAGS, CONTENT_FACTORY_URL="https://cf-withdraw.fixture.invalid",
                   CONTENT_FACTORY_API_KEY="synthetic-withdraw-key")
class DispatchSettlementTests(TransactionTestCase):
    def setUp(self):
        fixture = make_p2_org()
        self.org, self.payer = fixture.organization, fixture.owner
        self.now = timezone.now()
        PointsService.credit_purchased_topup(user=self.payer, delta=100,
            description="Synthetic local original funding", idempotency_key="fund-" + uuid4().hex)

    def paid(self, *, topic=False):
        from integrations.services.article_generation import _ledger_reference_id
        key = "original-" + uuid4().hex
        prefix = "content_factory:topic_generation:charge:" if topic else "content_factory:charge:"
        spend, _ = PointsService.spend(user=self.payer, delta=12, source="CONTENT_FACTORY",
            description="Synthetic original debit", created_by_slack_id=f"mlai_user:{self.payer.pk}",
            idempotency_key=prefix + key, reference_type="CONTENT_FACTORY", reference_id=_ledger_reference_id(key))
        request = {"domain": self.org.domain, "client_request_id": key, "topic": "Original approved topic",
            "roo_points_ledger_id": spend.pk, "roo_points_cost": 12, "roo_points_billing_user_id": self.payer.pk,
            "roo_points_billing_status": "charged", "roo_points_authorized": True}
        if topic:
            request["roo_points_action"] = "content_island_topic_generation"
        run = reserve_admitted_run(organization=self.org, workflow="auto_discovery" if topic else "article_generation",
            principal=user_principal(self.payer.pk, f"mlai_user:{self.payer.pk}"), payload=request)
        ContentFactoryRun.objects.filter(pk=run.pk).update(created_at=self.now - timedelta(seconds=181))
        run.refresh_from_db()
        job = create_job_facts(job_id=run.run_id, domain=run.domain, slack_user_id=run.slack_user_id,
            status="queued", client_request_id=key, request_meta=deepcopy(run.run_request),
            billing_status="charged", billing_ledger_id=spend.pk, billing_amount=12)
        return run, job, spend

    def candidate(self, run):
        missing = reconcile.requests.Response()
        missing.status_code = 404
        missing._content = json.dumps({"detail": "client_request_id not found"}).encode()
        with patch.object(reconcile.requests, "request", return_value=missing) as get, \
                patch.object(settle.requests, "post", return_value=self.remote({}, status=409)):
            result = reconcile.reconcile_authoritative_run(run.run_id, now=self.now, refresh=True)
        self.assertEqual(result["outcome"], "dispatch_no_effect_proven")
        self.assertEqual(get.call_args.kwargs["params"], {"client_request_id": owner_receipt(run)["identity"]["dispatch_key"]})
        run.refresh_from_db()
        self.assertIsNotNone(reconcile.no_effect_dispatch_proof(run))

    def closed(self, run):
        body = {"schema_version": 1, "status": "closed_no_effects", "closed_at": self.now.isoformat(),
            "client_request_id": owner_receipt(run)["identity"]["dispatch_key"],
            WIRE_REQUEST_KEY: wire_identity(owner_receipt(run)), WIRE_EXECUTION_KEY: current_execution_ticket(run)}
        body["receipt_sha256"] = settle.withdrawal_digest(body)
        return body

    def remote(self, body, status=200):
        return Mock(status_code=status, iter_content=Mock(return_value=[json.dumps(body).encode()]))

    def child(self, source, spend):
        from .no_delivery_refunds import carry_charge
        from .run_ownership import seal_new_run_admission
        with transaction.atomic():
            child = ContentFactoryRun.objects.create(run_id="child-" + uuid4().hex, organization=self.org,
                domain=self.org.domain, workflow=source.workflow, current_step="queued",
                run_request={"domain": self.org.domain, "client_request_id": "child-key-" + uuid4().hex,
                             "topic": "The retained original continuation"})
            carry_charge(source_run=source, new_run=child)
            seal_new_run_admission(child, created=True,
                principal=user_principal(self.payer.pk, f"mlai_user:{self.payer.pk}"))
        ContentFactoryRun.objects.filter(pk=child.pk).update(created_at=self.now - timedelta(seconds=181))
        child.refresh_from_db()
        create_job_facts(job_id=child.run_id, domain=child.domain, slack_user_id=child.slack_user_id,
            status="queued", client_request_id=child.run_request["client_request_id"],
            request_meta=deepcopy(child.run_request), billing_status="reused" if spend.kind == "SPEND" else "entitled",
            billing_ledger_id=spend.pk, billing_amount=12 if spend.kind == "SPEND" else 0)
        return child

    def free(self, *, topic=False):
        from .first_article_entitlement import entitled_authorization, grant_topic_batch, reserve_entitlement
        from .models import OrganizationContentConfig
        key = "free-original-" + uuid4().hex
        config = OrganizationContentConfig.objects.get(organization=self.org)
        raw = {"domain": self.org.domain, "client_request_id": key, "topic": "Original free topic",
               "delivery_mode": "content_only", "delivery_mode_confirmed": True, "expected_cost_points": 0}
        with override_settings(ARTICLES_FREE_FIRST_ARTICLE_ENABLED=True, ARTICLES_FIRST_ARTICLE_GOAL_ENABLED=False):
            grant = (grant_topic_batch(organization=self.org, user=self.payer, client_request_id=key, requested_topic_count=4)
                if topic else reserve_entitlement(organization=self.org, user=self.payer,
                    client_request_id=key, data=raw, config=config))
        self.assertIsNotNone(grant)
        action = "content_island_topic_generation" if topic else "article_generation"
        raw.update(entitled_authorization(domain=self.org.domain, action=action, ledger=grant))
        run = reserve_admitted_run(organization=self.org, workflow="auto_discovery" if topic else "article_generation",
            principal=user_principal(self.payer.pk, f"mlai_user:{self.payer.pk}"), payload=raw)
        ContentFactoryRun.objects.filter(pk=run.pk).update(created_at=self.now - timedelta(seconds=181))
        run.refresh_from_db()
        create_job_facts(job_id=run.run_id, domain=run.domain, slack_user_id=run.slack_user_id,
            status="queued", client_request_id=key, request_meta=deepcopy(run.run_request),
            billing_status="entitled", billing_ledger_id=grant.pk, billing_amount=0)
        return run, grant

    def test_missing_key_get_plus_grace_alone_never_calls_financial_controller(self):
        run, _, spend = self.paid()
        self.candidate(run)
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        with patch.object(settle.requests, "post", return_value=self.remote({}, status=409)) as post:
            result = settle.settle_authoritative_dispatch(run.run_id, now=self.now)
        self.assertEqual(result["state"], "pending")
        self.assertEqual(post.call_count, 1)
        self.assertFalse(spend.service_refunds.exists())
        after = ContentFactoryRun.objects.values().get(pk=run.pk)
        for name in before.keys() - {"result"}:
            self.assertEqual(before[name], after[name], name)
        self.assertNotIn("dispatch_withdrawals", after["result"][RECONCILIATION_RESULT_KEY])

    def test_no_candidate_or_legacy_run_performs_zero_remote_or_financial_effects(self):
        run, _, spend = self.paid()
        with patch.object(settle.requests, "post") as post:
            self.assertEqual(settle.settle_authoritative_dispatch(run.run_id, now=self.now)["state"], "pending")
        post.assert_not_called()
        self.assertFalse(spend.service_refunds.exists())
        legacy = ContentFactoryRun.objects.create(run_id="legacy-untouched", domain=self.org.domain, organization=self.org)
        before = ContentFactoryRun.objects.values().get(pk=legacy.pk)
        with patch.object(settle.requests, "post") as post:
            self.assertEqual(settle.settle_authoritative_dispatch(legacy.run_id, now=self.now)["state"], "pending")
        post.assert_not_called()
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=legacy.pk), before)

    def test_current_ticket_change_during_http_cannot_apply_old_closure_or_refund(self):
        run, _, spend = self.paid()
        self.candidate(run)
        receipt = self.closed(run)
        def post(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            run.refresh_from_db()
            advance_ticket(run, generation=1)
            return self.remote(receipt)
        with patch.object(settle.requests, "post", side_effect=post):
            result = settle.settle_authoritative_dispatch(run.run_id, now=self.now)
        self.assertEqual(result["state"], "pending")
        run.refresh_from_db()
        self.assertNotIn("dispatch_withdrawals", run.result[RECONCILIATION_RESULT_KEY])
        self.assertFalse(spend.service_refunds.exists())

    def test_foreign_rehashed_receipt_and_stale_claim_hold_the_original_spend(self):
        run, _, spend = self.paid()
        self.candidate(run)
        receipt = self.closed(run)
        receipt[WIRE_REQUEST_KEY]["organization_id"] = "999999"
        receipt["receipt_sha256"] = settle.withdrawal_digest(receipt)
        with patch.object(settle.requests, "post", return_value=self.remote(receipt)):
            self.assertEqual(settle.settle_authoritative_dispatch(run.run_id, now=self.now)["state"], "pending")
        self.assertFalse(spend.service_refunds.exists())
        run.refresh_from_db()
        claimed, claim = settle._claim(run, self.now)
        run.refresh_from_db()
        meta = deepcopy(run.result[RECONCILIATION_RESULT_KEY])
        meta["withdrawal_claim"]["nonce"] = "newer-owner"
        ContentFactoryRun.objects.filter(pk=run.pk).update(result={**run.result, RECONCILIATION_RESULT_KEY: meta})
        with self.assertRaises(settle.DispatchSettlementPending):
            settle._accept(claimed, claim, self.closed(run))
        self.assertFalse(spend.service_refunds.exists())

    def test_two_concurrent_withdrawals_admit_one_post_and_keep_unknown_ack_retriable(self):
        run, _, spend = self.paid()
        self.candidate(run)
        entered, release = Event(), Event()
        def post(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            entered.set()
            release.wait(timeout=10)
            raise settle.requests.Timeout("synthetic lost closure acknowledgement")
        def execute():
            close_old_connections()
            try:
                return settle.settle_authoritative_dispatch(run.run_id, now=self.now)
            finally:
                close_old_connections()
        with patch.object(settle.requests, "post", side_effect=post) as post, ThreadPoolExecutor(2) as pool:
            first = pool.submit(execute)
            self.assertTrue(entered.wait(timeout=10))
            second = pool.submit(execute)
            self.assertEqual(second.result(timeout=10)["reason"], "withdrawal_claim_active")
            release.set()
            self.assertEqual(first.result(timeout=10)["state"], "pending")
        self.assertEqual(post.call_count, 1)
        self.assertFalse(spend.service_refunds.exists())
        run.refresh_from_db()
        self.assertNotIn("withdrawal_claim", run.result[RECONCILIATION_RESULT_KEY])
        claimed, claim = settle._claim(run, self.now)
        self.assertIsNotNone(claim)
        settle._accept(claimed, claim, self.closed(run))
        run.refresh_from_db()
        self.assertEqual(settle._closed(run), self.closed(run))

    def test_real_original_refund_lost_ack_replay_uses_no_new_spend_or_request_clock_mutation(self):
        run, job, spend = self.paid()
        self.candidate(run)
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        def post(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            self.assertEqual(kwargs["json"]["client_request_id"], before["run_request"]["client_request_id"])
            return self.remote(self.closed(run))
        with patch.object(settle.requests, "post", side_effect=post) as post:
            receipt = settle.settle_authoritative_dispatch(run.run_id, now=self.now)
            replay = settle.settle_authoritative_dispatch(run.run_id, now=self.now)
        self.assertEqual(receipt["state"], "refunded")
        self.assertEqual(receipt, replay)
        self.assertEqual(post.call_count, 1)
        refund = Ledger.objects.get(pk=receipt["effect_id"])
        self.assertEqual((refund.refund_of_id, refund.user_id, refund.delta_microroo), (spend.pk, self.payer.pk, 12_000_000))
        self.assertEqual(spend.service_refunds.count(), 1)
        self.assertEqual(Ledger.objects.filter(kind="SPEND").count(), 1)
        self.payer.points_account.refresh_from_db()
        self.assertEqual(self.payer.points_account.balance, 100)
        after = ContentFactoryRun.objects.values().get(pk=run.pk)
        for name in ("run_request", "created_at", "updated_at", "last_event_emitted_at", "reconciled_at", "status", "current_step"):
            self.assertEqual(after[name], before[name], name)
        job.refresh_from_db()
        self.assertEqual(job.billing_status, "refunded")

    def test_active_or_delivered_carried_sibling_prevents_refunding_closed_parent(self):
        run, _, spend = self.paid()
        child = self.child(run, spend)
        self.candidate(run)
        for delivered in (False, True):
            if delivered:
                ContentFactoryRun.objects.filter(pk=child.pk).update(result={**child.result, "draft_ready_v1": {"ready": True}})
            with patch.object(settle.requests, "post", return_value=self.remote(self.closed(run))):
                result = settle.settle_authoritative_dispatch(run.run_id, now=self.now)
            self.assertEqual(result["state"], "pending")
            self.assertFalse(spend.service_refunds.exists())
        run.refresh_from_db()
        self.assertIsNotNone(settle._closed(run))

    def test_closed_child_different_logical_key_uses_original_parent_debit_and_updates_both_fact_projections(self):
        from .models import ContentFactoryJob
        run, _, spend = self.paid()
        child = self.child(run, spend)
        self.candidate(run)
        self.candidate(child)
        before = {row.pk: ContentFactoryRun.objects.values().get(pk=row.pk) for row in (run, child)}
        with patch.object(settle.requests, "post", return_value=self.remote(self.closed(run))):
            self.assertEqual(settle.settle_authoritative_dispatch(run.run_id, now=self.now)["state"], "pending")
        with patch.object(settle.requests, "post", return_value=self.remote(self.closed(child))):
            receipt = settle.settle_authoritative_dispatch(child.run_id, now=self.now)
        self.assertEqual(receipt["state"], "refunded")
        refund = Ledger.objects.get(pk=receipt["effect_id"])
        self.assertEqual(refund.refund_of_id, spend.pk)
        self.assertEqual(refund.idempotency_key, spend.idempotency_key.replace("content_factory:charge:", "content_factory:refund:", 1))
        for row in (run, child):
            after = ContentFactoryRun.objects.values().get(pk=row.pk)
            self.assertEqual(after["billing_status"], "refunded")
            self.assertEqual(ContentFactoryJob.objects.get(job_id=row.run_id).billing_ledger_id, refund.pk)
            for name in ("run_request", "created_at", "updated_at", "last_event_emitted_at", "reconciled_at"):
                self.assertEqual(after[name], before[row.pk][name], name)
        self.assertEqual(spend.service_refunds.count(), 1)

    def test_refund_authority_is_absent_outside_scope_and_bound_to_exact_original_payer_key_domain(self):
        from core.models import User
        run, _, spend = self.paid()
        self.candidate(run)
        claimed, claim = settle._claim(run, self.now)
        run = settle._accept(claimed, claim, self.closed(run))
        request = {**run.run_request, "roo_points_ledger_id": str(spend.pk)}
        args = {"user": self.payer, "article_request": request, "resolved_domain": run.domain}
        self.assertFalse(settle.refund_authority_matches(**args))
        with self.assertRaises(settle.DispatchSettlementPending):
            with settle.original_dispatch_refund_authority(run, closed_receipt=self.closed(run), original_spend=spend):
                self.fail("A SQL transaction is mandatory")
        foreign = User.objects.create_user(email="foreign-refund-payer@synthetic.example.test")
        with transaction.atomic(), settle.original_dispatch_refund_authority(run, closed_receipt=self.closed(run), original_spend=spend):
            self.assertTrue(settle.refund_authority_matches(**args))
            for changes in ({"user": foreign}, {"resolved_domain": "foreign.example"},
                            {"article_request": {**request, "client_request_id": "foreign-key"}},
                            {"article_request": {**request, "roo_points_ledger_id": spend.pk + 1}},
                            {"article_request": {**request, "roo_points_billing_user_id": foreign.pk}}):
                self.assertFalse(settle.refund_authority_matches(**{**args, **changes}))
        self.assertFalse(settle.refund_authority_matches(**args))
        self.assertFalse(spend.service_refunds.exists())

    def test_public_consumer_refuses_http_inside_any_existing_transaction(self):
        run, _, spend = self.paid()
        with transaction.atomic(), patch.object(settle.requests, "post") as post, self.assertRaises(settle.DispatchSettlementPending):
            settle.settle_authoritative_dispatch(run.run_id, now=self.now)
        post.assert_not_called()
        self.assertFalse(spend.service_refunds.exists())

    def test_actual_original_free_article_releases_grant_only_after_durable_closure_with_flags_off(self):
        from .first_article_entitlement import entitlement_payload
        run, grant = self.free()
        self.candidate(run)
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        with override_settings(ARTICLES_FREE_FIRST_ARTICLE_ENABLED=False), \
                patch.object(settle.requests, "post", return_value=self.remote(self.closed(run))):
            receipt = settle.settle_authoritative_dispatch(run.run_id, now=self.now)
        self.assertEqual(receipt["state"], "released")
        run.refresh_from_db()
        self.assertEqual(entitlement_payload(run)["entitlementStatus"], "released")
        self.assertEqual(run.run_request, before["run_request"])
        self.assertEqual((run.created_at, run.updated_at), (before["created_at"], before["updated_at"]))
        self.assertEqual(run.billing_ledger_id, grant.pk)
        self.assertFalse(Ledger.objects.filter(kind__in=["SPEND", "REFUND"]).exists())

    def test_actual_free_initial_topic_batch_release_preserves_one_retry_policy(self):
        from .first_article_entitlement import _rows
        run, grant = self.free(topic=True)
        self.candidate(run)
        with patch.object(settle.requests, "post", return_value=self.remote(self.closed(run))) as post:
            receipt = settle.settle_authoritative_dispatch(run.run_id, now=self.now)
            self.assertEqual(settle.settle_authoritative_dispatch(run.run_id, now=self.now), receipt)
        self.assertEqual(receipt["state"], "released")
        self.assertEqual(post.call_count, 1)
        rows = _rows(self.org.pk)
        self.assertEqual(rows.topics.pk, grant.pk)
        self.assertTrue(rows.topics_released)
        self.assertIsNone(rows.topics_retry)
        self.assertFalse(Ledger.objects.filter(kind__in=["SPEND", "REFUND"]).exists())

    def test_free_sibling_requires_exact_durable_closures_then_releases_original_without_status_fabrication(self):
        from .first_article_entitlement import entitlement_payload
        run, grant = self.free()
        child = self.child(run, grant)
        self.candidate(run)
        self.candidate(child)
        before = {row.pk: ContentFactoryRun.objects.values().get(pk=row.pk) for row in (run, child)}
        with patch.object(settle.requests, "post", return_value=self.remote(self.closed(run))):
            self.assertEqual(settle.settle_authoritative_dispatch(run.run_id, now=self.now)["state"], "pending")
        run.refresh_from_db()
        self.assertEqual(entitlement_payload(run)["entitlementStatus"], "reserved")
        with patch.object(settle.requests, "post", return_value=self.remote(self.closed(child))):
            result = settle.settle_authoritative_dispatch(child.run_id, now=self.now)
        self.assertEqual(result["state"], "released")
        self.assertEqual(entitlement_payload(run)["entitlementStatus"], "released")
        for row in (run, child):
            after = ContentFactoryRun.objects.values().get(pk=row.pk)
            for name in ("status", "current_step", "run_request", "created_at", "updated_at"):
                self.assertEqual(before[row.pk][name], after[name], name)
        self.assertFalse(Ledger.objects.filter(kind__in=["SPEND", "REFUND"]).exists())

    def test_real_scheduled_reconciliation_handoff_refunds_only_after_authenticated_atomic_withdrawal(self):
        run, _, spend = self.paid()
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        missing = reconcile.requests.Response()
        missing.status_code = 404
        missing._content = json.dumps({"detail": "client_request_id not found"}).encode()
        with override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=False, ARTICLES_RUN_FACT_AUTHORITY_ENABLED=False,
                               ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=False), \
                patch.object(reconcile.requests, "request", return_value=missing) as get, \
                patch.object(settle.requests, "post", return_value=self.remote(self.closed(run))) as post:
            result = reconcile.run_authoritative_reconciliation_sweep(now=self.now)
        self.assertEqual(result["dispatch_no_effect_proven"], 1)
        self.assertEqual((get.call_count, post.call_count), (1, 1))
        run.refresh_from_db()
        self.assertEqual(run.result[RECONCILIATION_RESULT_KEY]["dispatch_settlement"]["state"], "refunded")
        self.assertEqual(spend.service_refunds.count(), 1)
        self.assertEqual(Ledger.objects.filter(kind="SPEND").count(), 1)
        for name in ("run_request", "status", "created_at", "updated_at", "reconciled_at"):
            self.assertEqual(getattr(run, name), before[name], name)

    def test_real_unstamped_free_grant_is_valid_but_another_company_grant_never_replaces_it(self):
        from .first_article_entitlement import reserve_entitlement
        from .models import ContentFactoryJob
        from .run_backend_facts import BackendFactConflict, save_job_facts
        run, grant = self.free()
        run.refresh_from_db()
        self.assertNotIn("roo_points_billing_user_id", run.run_request)
        self.assertEqual(run.billing_ledger_id, grant.pk)
        job = ContentFactoryJob.objects.get(job_id=run.run_id)
        job.billing_ledger = grant
        save_job_facts(job, update_fields=["billing_ledger"])
        foreign = make_p2_org()
        with override_settings(ARTICLES_FREE_FIRST_ARTICLE_ENABLED=True, ARTICLES_FIRST_ARTICLE_GOAL_ENABLED=False):
            other = reserve_entitlement(organization=foreign.organization, user=foreign.owner,
                client_request_id="foreign-entitlement", data={"delivery_mode": "content_only",
                    "delivery_mode_confirmed": True, "expected_cost_points": 0}, config=foreign.config)
        self.assertIsNotNone(other)
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        job.billing_ledger = other
        with self.assertRaises(BackendFactConflict):
            save_job_facts(job, update_fields=["billing_ledger"])
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=run.pk), before)
