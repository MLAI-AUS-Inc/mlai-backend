"""Queue errors cannot settle an admitted original obligation from status."""
from django.test import TestCase, override_settings

from integrations import tests_article as fixtures
from integrations.services.article_generation import (
    _ledger_reference_id, refund_content_factory_request_for_user,
    refund_content_factory_topic_generation_for_user,
)
from roo.services import PointsService
from workflow_runs.models import ContentFactoryRun
from .run_backend_facts import create_job_facts
from .run_ownership import bind_admitted_run, reserve_admitted_run, user_principal
from .website_connections import contract_for


@override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=True, ARTICLES_RUN_FACT_AUTHORITY_ENABLED=True,
                   ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=True)
class OwnedRefundBoundaryTests(TestCase):
    _own_verified_website = fixtures.ArticleGenerationServiceTest._own_verified_website

    def setUp(self):
        fixtures.ArticleGenerationServiceTest.setUp(self)

    def obligation(self, *, topic=False, owned=True, key="original-obligation"):
        prefix = "content_factory:topic_generation:charge:" if topic else "content_factory:charge:"
        amount = 3 if topic else 12
        spend, _ = PointsService.spend(user=self.user, delta=amount, source="CONTENT_FACTORY",
            description="Synthetic original obligation", created_by_slack_id=self.slack_user_id,
            idempotency_key=prefix + key, reference_type="CONTENT_FACTORY", reference_id=_ledger_reference_id(key))
        request = {**contract_for(self.config.website_connection), "domain": self.org.domain,
            "github_repo": self.repo_name, "client_request_id": key, "roo_points_ledger_id": str(spend.pk),
            "roo_points_billing_user_id": self.user.pk, "roo_points_billing_status": "charged",
            "roo_points_action": "content_island_topic_generation" if topic else "article_generation",
            "roo_points_cost": amount}
        if owned:
            run = reserve_admitted_run(organization=self.org, principal=user_principal(self.user.pk, self.slack_user_id),
                workflow="auto_discovery" if topic else "article_generation", payload=request)
            run = bind_admitted_run(run, "worker-" + key[:30])
            create_job_facts(job_id=run.run_id, domain=self.org.domain, slack_user_id=self.slack_user_id,
                client_request_id=key, request_meta=request, billing_status="charged", billing_amount=amount,
                billing_ledger_id=spend.pk)
        else:
            run = ContentFactoryRun.objects.create(run_id="legacy-refund", organization=self.org,
                workflow="article_generation", domain=self.org.domain, run_request=request)
        return run, spend, request

    def refund(self, request, *, topic=False):
        controller = refund_content_factory_topic_generation_for_user if topic else refund_content_factory_request_for_user
        return controller(user=self.user, actor_id=self.slack_user_id, article_request=request,
            resolved_domain=self.org.domain, reason="Synthetic failed queue response")

    def test_owned_article_queue_error_holds_charge_even_after_admission_switches_off(self):
        run, spend, request = self.obligation()
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        with override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=False):
            self.assertIsNone(self.refund(request))
        self.assertFalse(spend.service_refunds.exists())
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=run.pk), before)

    def test_owned_topic_queue_error_holds_original_charge(self):
        run, spend, request = self.obligation(topic=True)
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        self.assertIsNone(self.refund(request, topic=True))
        self.assertFalse(spend.service_refunds.exists())
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=run.pk), before)

    def test_long_original_key_cannot_bypass_owner_with_projection_alias(self):
        run, spend, request = self.obligation(key="long-" + "x" * 150)
        self.assertIsNone(self.refund({**request, "client_request_id": run.client_request_id}))
        self.assertFalse(spend.service_refunds.exists())

    def test_old_no_delivery_controller_keeps_owned_status_only_decision_pending(self):
        from .no_delivery_refunds import apply_refund_decision
        run, spend, request = self.obligation()
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        with override_settings(ARTICLES_NO_DELIVERY_REFUNDS_ENABLED=True):
            apply_refund_decision(run, {"action": "refund", "points": 12})
        self.assertFalse(spend.service_refunds.exists())
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=run.pk), before)

    def test_owned_terminal_credit_intent_keeps_execution_clocks_and_original_request(self):
        from types import SimpleNamespace
        from integrations.services.article_generation import _record_terminal_article_refund
        run, _, _ = self.obligation()
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        _record_terminal_article_refund(SimpleNamespace(job_id=run.run_id, domain=run.domain),
            status="pending", error_code="RUN_BUDGET_EXHAUSTED")
        after = ContentFactoryRun.objects.values().get(pk=run.pk)
        for field in before.keys() - {"result"}:
            self.assertEqual(after[field], before[field], field)
        for key, value in before["result"].items():
            self.assertEqual(after["result"][key], value)
        self.assertEqual(after["result"]["article_terminal_refund"]["status"], "pending")

    def test_unmarked_original_article_keeps_actual_legacy_refund_behavior(self):
        run, spend, request = self.obligation(owned=False)
        refund = self.refund(request)
        self.assertEqual(refund.refund_of_id, spend.pk)
        run.refresh_from_db()
        self.assertEqual(run.run_request["roo_points_billing_status"], "refunded")
