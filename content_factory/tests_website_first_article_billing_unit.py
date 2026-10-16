"""Original first-article billing policies with only persistence/provider fakes."""

from contextlib import ExitStack, nullcontext
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from . import website_first_article_billing as billing, website_goal as goals
from . import website_setup_consent as consent
from .tests_website_goal_unit import scope
from .tests_website_setup_consent_unit import answers
from .website_contract import WebsiteAuthorityError
from . import vibe_marketing_views as views


@override_settings(ARTICLES_FIRST_ARTICLE_GOAL_ENABLED=True, ARTICLES_SETUP_CONSENT_ENABLED=True,
    ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True, ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"],
    ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True, ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"])
class FirstArticleBillingTests(SimpleTestCase):
    def setUp(self):
        self.now = timezone.now()
        self.user = SimpleNamespace(pk=4, is_active=True, email="owner@example.test")
        self.payer = SimpleNamespace(pk=5, is_active=True)
        self.organization = SimpleNamespace(pk=7, domain="fixture.test")
        self.connection = SimpleNamespace(pk="website", organization_id=7, organization=self.organization,
            generation=3, repository_id="12", branch="main", app_root="", configuration_version=1,
            blockers=[], state="connected")
        goal = goals.new_goal("prepare", scope(), target="first_article_live", now=self.now)
        goal["consent"] = consent.seal(goal, answers(), user_id=4, now=self.now)
        self.op = SimpleNamespace(pk="prepare", action="prepare", state="pending", generation=3,
            connection=self.connection, payload={"company_id":"9", "requested_by_user_id":"4"},
            receipt={"goal":goal}, save=Mock())
        self.context = SimpleNamespace(company=SimpleNamespace(pk=9), organization=self.organization)
        self.ledger = SimpleNamespace(pk=11, id=11, user_id=5, user=self.payer, kind="SPEND", source="CONTENT_FACTORY",
            reference_type="CONTENT_FACTORY", reference_id=billing.request_key(goal),
            idempotency_key="content_factory:charge:"+billing.request_key(goal), delta_microroo=-6_000_000)
        self.connection.operations = Mock()
        self.connection.operations.select_for_update.return_value = self.connection.operations
        self.connection.operations.filter.return_value.order_by.return_value.first.return_value = self.op
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.spend = self.stack.enter_context(patch("roo.services.PointsService.spend", return_value=(self.ledger, True)))
        self.stack.enter_context(patch("content_factory.billing.get_content_factory_article_cost_points", return_value=6))
        self.stack.enter_context(patch("content_factory.company_billing.billing_user_for_organization", return_value=self.payer))
        self.stack.enter_context(patch("founder_tools.services.founder_actor_id_for_user", return_value="founder:4"))
        self.rows = self.stack.enter_context(patch("content_factory.website_models.WebsiteConnectionOperation.objects.select_for_update"))
        self.rows.return_value.select_related.return_value.filter.return_value.first.return_value = self.op
        self.ledger_rows = self.stack.enter_context(patch("roo.models.Ledger.objects.select_for_update"))
        self.ledger_rows.return_value.select_related.return_value.filter.return_value.first.return_value = self.ledger
        self.refunds = self.stack.enter_context(patch("roo.models.Ledger.objects.filter"))
        self.refunds.return_value.exists.return_value = False

    def reserve(self):
        return billing.reserve(self.op, user=self.user, now=self.now)

    def consume(self, **extra):
        payload = {"client_request_id": billing.request_key(self.op.receipt["goal"]), "delivery_mode":"publish_code",
            "website_connection_id":"website", "connection_generation":3, "repository_id":self.connection.repository_id,
            "branch":"main", "app_root":"", **extra}
        result = billing.consume(self.op.pk, context=self.context, user=self.user, payload=payload)
        return result, payload

    def test_acceptance_debits_once_and_consumption_keeps_payer_and_original_quote(self):
        held = self.reserve()
        self.assertEqual(self.reserve(), held)
        self.assertEqual(self.spend.call_count, 1)
        self.assertEqual(self.spend.call_args.kwargs["delta"], 6)
        self.assertEqual(self.spend.call_args.kwargs["user"], self.payer)
        with patch("content_factory.billing.get_content_factory_article_cost_points", return_value=99):
            (payer, ledger, amount), payload = self.consume()
            self.consume()
        self.assertEqual((payer, ledger, amount), (self.payer, self.ledger, 6))
        self.assertEqual(self.op.receipt["first_article_billing"]["status"], "reserved")
        self.assertEqual(payload["first_article_goal_id"], "prepare")
        self.assertEqual(self.spend.call_count, 1)

    def test_quote_change_and_insufficient_balance_do_not_leave_a_reservation(self):
        with patch("content_factory.billing.get_content_factory_article_cost_points", return_value=7):
            with self.assertRaises(WebsiteAuthorityError): self.reserve()
        self.spend.assert_not_called()
        from roo.permissions import InsufficientBalanceError
        self.spend.side_effect = InsufficientBalanceError("insufficient")
        with self.assertRaises(WebsiteAuthorityError) as error: self.reserve()
        self.assertEqual(error.exception.status, 402)
        self.assertNotIn("first_article_billing", self.op.receipt)

    def test_free_reservation_is_explicit_and_never_inferred_from_missing_spend(self):
        goal = self.op.receipt["goal"]
        goal["consent"] = consent.seal(goal, answers(billing={"policy":"reserved", "expected_points":0, "accepted":True}), user_id=4, now=self.now)
        with patch("content_factory.billing.get_content_factory_article_cost_points", return_value=0): self.reserve()
        self.spend.assert_not_called()
        from django.contrib.auth import get_user_model
        with patch.object(get_user_model().objects, "filter") as users:
            users.return_value.first.return_value = self.payer
            (_, ledger, amount), _ = self.consume()
        self.assertIsNone(ledger); self.assertEqual(amount, 0)
        self.op.receipt.pop("first_article_billing")
        with self.assertRaises(WebsiteAuthorityError): self.consume()

    def test_paid_receipt_requires_exact_original_ledger_and_no_prior_refund(self):
        self.reserve()
        for field, value in (("user_id", 99), ("kind", "GRANT"), ("source", "OTHER"),
            ("reference_id", "foreign"), ("reference_type", "OTHER"), ("idempotency_key", "foreign"),
            ("delta_microroo", -5_000_000)):
            previous = getattr(self.ledger, field); setattr(self.ledger, field, value)
            with self.subTest(field=field), self.assertRaises(WebsiteAuthorityError): self.consume()
            setattr(self.ledger, field, previous)
        self.refunds.return_value.exists.return_value = True
        with self.assertRaises(WebsiteAuthorityError): self.consume()

    def test_foreign_cancelled_deleted_or_superseded_goal_cannot_consume(self):
        self.reserve()
        for obj, field, value in ((self.user,"pk",99), (self.user,"is_active",False),
            (self.op,"state","cancelled"), (self.connection,"state","deleted"),
            (self.connection,"generation",4), (self.connection,"configuration_version",2)):
            previous = getattr(obj, field); setattr(obj, field, value)
            with self.subTest(field=field), self.assertRaises(WebsiteAuthorityError): self.consume()
            setattr(obj, field, previous)
        self.connection.operations.filter.return_value.order_by.return_value.first.return_value = SimpleNamespace(pk="newer")
        with self.assertRaises(WebsiteAuthorityError): self.consume()

    def test_source_only_consent_rebind_keeps_original_spend_but_site_change_does_not(self):
        held = self.reserve(); goal = self.op.receipt["goal"]
        new_scope = {**goal["scope"], "configuration_revision":2}
        goal["consent"] = consent.rebind_trusted_source(goal, new_scope)
        goal["scope"] = new_scope; self.connection.configuration_version = 2
        self.assertEqual(self.consume()[0][2], 6)
        self.assertEqual(self.op.receipt["first_article_billing"]["client_request_id"], held["client_request_id"])
        changed_scope = {**new_scope, "repository_id":"foreign"}
        goal["consent"] = consent.rebind_trusted_source(goal, changed_scope)
        goal["scope"] = changed_scope; self.connection.repository_id = "foreign"
        with self.assertRaises(WebsiteAuthorityError): self.consume()

    def test_body_or_payload_cannot_select_a_different_charge_or_free_price(self):
        self.reserve()
        with self.assertRaises(WebsiteAuthorityError): self.consume(client_request_id="body-selected")
        receipt = self.op.receipt["first_article_billing"]
        for field, value in (("cost_points",0), ("payer_user_id","99"), ("spend_ledger_id",None)):
            previous = receipt[field]; receipt[field] = value
            with self.subTest(field=field), self.assertRaises(WebsiteAuthorityError): self.consume()
            receipt[field] = previous

    def test_disabled_rollout_does_not_debit_or_consume(self):
        with override_settings(ARTICLES_FIRST_ARTICLE_GOAL_ENABLED=False):
            self.assertIsNone(self.reserve())
            with self.assertRaises(WebsiteAuthorityError): self.consume()
        self.spend.assert_not_called()

    def test_canonical_charge_uses_internal_reservation_and_retains_admission_and_outbox(self):
        from . import vibe_marketing_views as views
        self.reserve()
        payload = {"delivery_mode":"publish_code", "client_request_id":"untrusted", "website_connection_id":"website",
            "connection_generation":3, "repository_id":"12", "branch":"main", "app_root":""}
        request = SimpleNamespace(user=self.user, data={"expectedCostPoints":99})
        with patch.object(views.transaction, "atomic", side_effect=lambda: nullcontext()), \
            patch.object(views, "_refresh_article_editorial_payload", return_value=None), \
            patch.object(views, "_reserve_article_start_connection", return_value=None), \
            patch.object(views, "_validate_article_start_admission") as admission, \
            patch.object(views, "_validate_article_start_reservation") as revalidate, \
            patch.object(views, "_roo_points_balance_for_user", return_value=10), \
            patch.object(views, "charge_content_factory_request_for_user") as ordinary_charge, \
            patch("content_factory.dispatch_outbox.reserve_dispatch") as outbox:
            charged, ledger, _, error = views._charge_roo_points_for_article(request,
                context=self.context, payload=payload, goal_operation_id=self.op.pk)
        self.assertIsNone(error); self.assertEqual((charged,ledger),(self.payer,self.ledger))
        ordinary_charge.assert_not_called(); admission.assert_called_once(); revalidate.assert_called_once(); outbox.assert_called_once()
        self.assertEqual(payload["roo_points_cost"], 6)
        self.assertEqual(payload["first_article_goal_operation_id"], "prepare")
        self.assertEqual(self.spend.call_count, 1)

    def test_http_body_fields_do_not_activate_internal_reserved_billing(self):
        from . import vibe_marketing_views as views
        request = SimpleNamespace(user=self.user, data={"expectedCostPoints":99,
            "goal_operation_id":"prepare", "first_article_goal_operation_id":"prepare"})
        with patch.object(views, "get_content_factory_article_cost_points", return_value=6), \
            patch.object(billing, "generation_key") as lookup:
            _, _, _, error = views._charge_roo_points_for_article(request, context=self.context, payload={})
        self.assertEqual(error.status_code,409); lookup.assert_not_called(); self.spend.assert_not_called()

    def test_start_goal_acceptance_and_retry_reserve_the_same_payment(self):
        from . import website_goal_runner as runner
        self.connection.verified_sha = "a"*40
        first = runner.start_goal(self.connection, company_id=9, user=self.user, retry=False,
            target="first_article_live", now=self.now)
        again = runner.start_goal(self.connection, company_id=9, user=self.user, retry=True,
            target="first_article_live", now=self.now)
        self.assertIs(first, self.op); self.assertIs(again, self.op)
        self.assertEqual(self.spend.call_count,1)

    def test_only_the_active_original_paid_goal_defers_a_child_refund(self):
        receipt = self.reserve()
        request = {"first_article_goal_operation_id":self.op.pk, "client_request_id":receipt["client_request_id"],
            "roo_points_ledger_id":receipt["spend_ledger_id"], "roo_points_billing_user_id":5}
        with patch("content_factory.website_models.WebsiteConnectionOperation.objects.select_related") as rows:
            rows.return_value.filter.return_value.first.return_value = self.op
            self.assertTrue(billing.defer_child_refund(request,resolved_domain="fixture.test"))
            for field, value in (("client_request_id","foreign"),("roo_points_ledger_id","99"),("roo_points_billing_user_id",99)):
                self.assertFalse(billing.defer_child_refund({**request,field:value},resolved_domain="fixture.test"))
            self.assertFalse(billing.defer_child_refund(request,resolved_domain="foreign.test"))
            self.op.state="needs_attention"
            self.assertFalse(billing.defer_child_refund(request,resolved_domain="fixture.test"))

    def test_final_failure_refunds_original_allocation_once_even_after_rollout_disabled(self):
        from .models import ContentFactoryJob
        from workflow_runs.models import ContentFactoryRun
        receipt=self.reserve(); self.op.state="needs_attention"
        self.refunds.return_value.first.return_value=self.ledger
        refund=SimpleNamespace(id=22,pk=22)
        with override_settings(ARTICLES_FIRST_ARTICLE_GOAL_ENABLED=False), \
            patch("roo.services.PointsService.refund",return_value=(refund,True)) as credit, \
            patch.object(ContentFactoryJob.objects,"filter"), \
            patch.object(ContentFactoryRun.objects,"filter") as runs:
            runs.return_value.filter.return_value=[]
            billing.release(self.op,reason="Final technical failure")
            billing.release(self.op,reason="Replay")
        self.assertEqual(credit.call_count,1)
        self.assertEqual(credit.call_args.kwargs["user"],self.payer)
        self.assertEqual(credit.call_args.kwargs["delta"],6)
        self.assertEqual(credit.call_args.kwargs["original_spend_key"],"content_factory:charge:"+receipt["client_request_id"])
        self.assertEqual(self.op.receipt["first_article_billing"]["status"],"refunded")
        self.assertEqual(self.spend.call_count,1)

    def test_cancel_after_useful_draft_retains_charge_and_cancel_before_draft_refunds(self):
        from .models import ContentFactoryJob
        from workflow_runs.models import ContentFactoryRun
        self.reserve(); self.op.state="cancelled"
        draft=next(item for item in self.op.receipt["goal"]["milestones"] if item["name"]=="first_draft")
        draft["state"]="passed"
        draft["proof"]=goals.make_proof("first_draft","article_draft",scope=scope(),source_sha="a"*40,
            sha="a"*40,run_id="draft",evidence={"run_id":"draft"},at=self.now)
        with patch("roo.services.PointsService.refund") as credit:
            billing.release(self.op,reason="Cancelled")
        credit.assert_not_called()
        draft["state"]="pending"
        self.refunds.return_value.first.return_value=self.ledger
        with patch("roo.services.PointsService.refund",return_value=(SimpleNamespace(id=22,pk=22),True)) as credit, \
            patch.object(ContentFactoryJob.objects,"filter"),patch.object(ContentFactoryRun.objects,"filter") as runs:
            runs.return_value.filter.return_value=[]
            billing.release(self.op,reason="Cancelled")
        credit.assert_called_once()

    def test_terminal_job_refund_keeps_payment_while_goal_is_repairing(self):
        from integrations.services.article_generation import maybe_auto_refund_terminal_failure
        receipt=self.reserve()
        job=SimpleNamespace(domain="fixture.test",request_meta={"first_article_goal_operation_id":self.op.pk,
            "client_request_id":receipt["client_request_id"],"roo_points_ledger_id":receipt["spend_ledger_id"],
            "roo_points_billing_user_id":5})
        with patch("content_factory.website_models.WebsiteConnectionOperation.objects.select_related") as rows:
            rows.return_value.filter.return_value.first.return_value=self.op
            self.assertEqual(maybe_auto_refund_terminal_failure(job,error_code="ARTICLE_STYLE_FAILED",error_message="Failed",refundable=True),(False,0))

    def refund_row(self):
        return SimpleNamespace(pk="refund", action="goal-refund", state="pending", attempts=0,
            next_attempt_at=None, connection_id="website", generation=3,
            payload={"goal_operation_id":"prepare","goal_id":"prepare"}, receipt={}, save=Mock())

    def test_terminal_credit_outbox_survives_connection_change_and_disabled_rollout(self):
        from . import website_first_article_refunds as refunds
        from .models import ContentFactoryJob
        from workflow_runs.models import ContentFactoryRun
        self.reserve(); self.op.state="cancelled"; self.connection.state="disconnected"; self.connection.generation=4
        row=self.refund_row()
        self.rows.return_value.filter.return_value.first.return_value=row
        self.refunds.return_value.first.return_value=self.ledger
        with override_settings(ARTICLES_FIRST_ARTICLE_GOAL_ENABLED=False), \
            patch.object(refunds.transaction,"atomic",side_effect=lambda: nullcontext()), \
            patch("roo.services.PointsService.refund",return_value=(SimpleNamespace(id=22,pk=22),True)) as credit, \
            patch.object(ContentFactoryJob.objects,"filter"),patch.object(ContentFactoryRun.objects,"filter") as runs:
            runs.return_value.filter.return_value=[]
            self.assertEqual(refunds.advance_refund(row.pk,now=self.now),"completed")
            self.assertIsNone(refunds.advance_refund(row.pk,now=self.now))
        credit.assert_called_once(); self.assertEqual(row.receipt["status"],"credited")
        self.assertEqual(self.op.receipt["first_article_billing"]["status"],"refunded")

    def test_terminal_credit_database_failure_is_durable_without_undoing_cancellation(self):
        from . import website_first_article_refunds as refunds
        from .website_models import WebsiteConnectionOperation
        from django.db import OperationalError
        self.reserve(); self.op.state="cancelled"; row=self.refund_row()
        self.rows.return_value.filter.return_value.first.return_value=row
        self.refunds.return_value.first.return_value=self.ledger
        with patch.object(refunds.transaction,"atomic",side_effect=lambda: nullcontext()), \
            patch("roo.services.PointsService.refund",side_effect=OperationalError("synthetic failure")), \
            patch.object(WebsiteConnectionOperation.objects,"filter") as updates:
            self.assertEqual(refunds.advance_refund(row.pk,now=self.now),"pending")
        self.assertEqual(self.op.state,"cancelled")
        self.assertEqual(updates.return_value.update.call_args.kwargs["receipt"],
            {"status":"credit_pending","reason_code":"website_prepare_dependency_transient"})
        self.assertGreater(updates.return_value.update.call_args.kwargs["next_attempt_at"],self.now)

    def test_terminal_state_cas_loss_never_queues_a_credit(self):
        from . import website_prepare as prepare
        from .website_models import WebsiteConnectionOperation
        self.reserve(); self.op.state="needs_attention"
        with patch.object(prepare.transaction,"atomic",side_effect=lambda: nullcontext()), \
            patch("content_factory.website_notification_outbox.enabled",return_value=False), \
            patch.object(prepare,"_persist_prepare_state",return_value=None), \
            patch.object(WebsiteConnectionOperation.objects,"get_or_create") as reserve:
            self.assertIsNone(prepare._persist_prepare(self.op,now=self.now))
        reserve.assert_not_called()

    def test_lifecycle_cancellation_preserves_billing_and_queues_credit_without_provider_work(self):
        from .website_models import WebsiteConnectionOperation
        self.reserve(); before=deepcopy(self.op.receipt)
        self.rows.return_value.filter.return_value.exclude.return_value=[self.op]
        with patch.object(WebsiteConnectionOperation.objects,"get_or_create",return_value=(self.refund_row(),True)) as reserve, \
            patch("roo.services.PointsService.refund") as credit:
            billing.cancel_held_goals(self.connection)
        self.assertEqual(self.op.state,"cancelled")
        self.assertEqual(self.op.receipt["first_article_billing"],before["first_article_billing"])
        self.assertEqual(self.op.receipt["goal"],before["goal"])
        self.assertEqual(reserve.call_args.kwargs["defaults"]["action"],"goal-refund")
        credit.assert_not_called()

    def test_erasure_retains_only_scoped_payment_proof_until_credit_settles(self):
        from . import website_first_article_refunds as refunds
        self.reserve(); row=self.refund_row()
        self.op.payload["customer_article"]="private draft"
        self.op.receipt["customer_article"]="private draft"
        deletion=Mock();credits=MagicMock();credits.__iter__.return_value=[row]
        credits.exclude.return_value.exists.return_value=True
        def selected(*args,**kwargs):
            if kwargs.get("action")=="prepare":return [self.op]
            if kwargs.get("action")==refunds.ACTION:return credits
            return deletion
        self.connection.operations.filter.side_effect=selected
        self.assertTrue(refunds.retain_financial_refunds_for_erasure(self.connection))
        self.assertEqual(set(self.op.payload),{"company_id","requested_by_user_id"})
        self.assertEqual(set(self.op.receipt),{"goal","first_article_billing"})
        self.assertTrue(row.payload["purge_after_reconciliation"])
        self.assertEqual(row.payload["goal_operation_id"],"prepare")
        deletion.exclude.return_value.delete.assert_called_once()

    def test_erasure_shell_waits_for_credit_then_finishes_after_rollout_disabled(self):
        from . import website_first_article_refunds as refunds
        from .website_models import WebsiteConnection
        from organizations.models import Organization
        candidate=SimpleNamespace(organization_id=7,operations=Mock(),delete=Mock())
        candidate.operations.filter.return_value.exists.return_value=False
        outstanding=candidate.operations.filter.return_value.exclude.return_value.exists
        outstanding.return_value=True
        with override_settings(ARTICLES_FIRST_ARTICLE_GOAL_ENABLED=False), \
            patch.object(refunds.transaction,"atomic",side_effect=lambda:nullcontext()), \
            patch.object(Organization.objects,"select_for_update"), \
            patch.object(WebsiteConnection.objects,"filter") as selected, \
            patch.object(WebsiteConnection.objects,"select_for_update") as locked:
            selected.return_value.first.return_value=candidate
            locked.return_value.filter.return_value.first.return_value=candidate
            refunds.finish_purged_connection("website");candidate.delete.assert_not_called()
            outstanding.return_value=False
            refunds.finish_purged_connection("website");candidate.delete.assert_called_once()

    def test_changed_choices_with_no_spare_points_wait_then_rehold_original_credit(self):
        original=self.reserve();self.op.state="cancelled"
        goal=goals.new_goal("replacement",scope(),target="first_article_live",now=self.now)
        goal["consent"]=consent.seal(goal,answers(publish_mode="review"),user_id=4,now=self.now)
        replacement=SimpleNamespace(pk="replacement",connection=self.connection,generation=3,action="prepare",
            state="pending",payload={"company_id":"9","requested_by_user_id":"4"},receipt={"goal":goal},save=Mock())
        self.assertIsNone(billing.reserve_or_wait(replacement,user=self.user,now=self.now,predecessor=self.op))
        self.assertEqual(self.spend.call_count,1)
        self.assertFalse(billing.reservation_ready(replacement))
        self.assertEqual(self.op.state,"cancelled")
        # The credit worker runs both allocations inside its database transaction.
        billing._save(self.op,{**original,"status":"refunded"})
        self.rows.return_value.select_related.return_value.filter.return_value.order_by.return_value.first.return_value=replacement
        next_ledger=SimpleNamespace(**{**vars(self.ledger),"pk":12,"id":12,
            "reference_id":billing.request_key(goal),"idempotency_key":"content_factory:charge:"+billing.request_key(goal)})
        self.spend.return_value=(next_ledger,True)
        from django.contrib.auth import get_user_model
        with patch.object(get_user_model().objects,"filter") as users, \
            patch("django.db.transaction.atomic",side_effect=lambda:nullcontext()), \
            patch("content_factory.billing.get_content_factory_article_cost_points",return_value=99):
            users.return_value.first.return_value=self.user
            billing.reserve_replacement_after_credit(self.op,now=self.now)
        self.assertTrue(billing.reservation_ready(replacement))
        self.assertEqual(self.spend.call_count,2)
        self.assertNotEqual(original["client_request_id"],replacement.receipt["first_article_billing"]["client_request_id"])
        self.assertEqual(replacement.receipt["first_article_billing"]["cost_points"],6)
        self.rows.return_value.select_related.return_value.filter.return_value.first.return_value=replacement
        self.connection.operations.filter.return_value.order_by.return_value.first.return_value=replacement
        self.ledger_rows.return_value.select_related.return_value.filter.return_value.first.return_value=next_ledger
        payer,ledger,cost=billing.consume(replacement.pk,context=self.context,user=self.user,payload={
            "client_request_id":billing.request_key(goal),"delivery_mode":"review_draft","website_connection_id":"website",
            "connection_generation":3,"repository_id":"12","branch":"main","app_root":""})
        self.assertEqual((payer,ledger,cost),(self.payer,next_ledger,6))
