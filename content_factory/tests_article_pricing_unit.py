"""Article quotes and debits without a database or network."""
from datetime import datetime, timezone
from types import SimpleNamespace as Obj
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from content_factory.activation import article_capabilities
from content_factory.billing import (
    build_roo_points_authorization_payload,
    build_roo_points_payload,
    get_content_factory_article_cost_points,
    get_content_factory_content_island_topic_cost_points,
)
from integrations.services import article_generation as service
from startup_updates.benefits import has_monthly_update_discount


class ArticlePricingTests(SimpleTestCase):
    def setUp(self):
        self.user = Obj(pk=7, email="pricing@example.test")

    def test_every_domain_has_twelve_point_base_price(self):
        for domain in ("example.com", "mlai.au", "https://www.mlai.au", ""):
            with self.subTest(domain=domain):
                self.assertEqual(get_content_factory_article_cost_points(domain), 12)

    def test_current_monthly_benefit_halves_every_domain_price(self):
        with patch("startup_updates.benefits.has_monthly_update_discount", return_value=True) as benefit:
            for domain in ("example.com", "mlai.au"):
                self.assertEqual(get_content_factory_article_cost_points(domain, user=self.user), 6)
            benefit.assert_called_with(self.user)

    def test_missing_or_expired_benefit_keeps_twelve_point_price(self):
        with patch("startup_updates.benefits.has_monthly_update_discount", return_value=False):
            self.assertEqual(get_content_factory_article_cost_points("mlai.au", user=self.user), 12)

    def test_shared_benefit_uses_existing_eligibility_and_melbourne_day(self):
        now = datetime(2026, 10, 18, 14, 0, tzinfo=timezone.utc)
        with patch("django.utils.timezone.now", return_value=now), \
             patch("roo.services.CoworkingService._has_ready_monthly_update", return_value=True) as eligible:
            self.assertTrue(has_monthly_update_discount(self.user))
        self.assertEqual(eligible.call_args.args, (self.user, datetime(2026, 10, 19).date()))

    def test_capabilities_quote_the_requesting_founders_price(self):
        with patch("startup_updates.benefits.has_monthly_update_discount", return_value=True):
            discounted = article_capabilities(None, domain="example.com", user=self.user)
        regular = article_capabilities(None, domain="example.com")
        self.assertEqual(discounted["prices"]["article"], 6)
        self.assertEqual(regular["prices"]["article"], 12)
        self.assertEqual(discounted["prices"]["research"], regular["prices"]["research"])

    def test_article_payload_has_no_free_domain_exception(self):
        payload = build_roo_points_payload(domain="mlai.au", action="article_generation", current_balance=5)
        self.assertEqual((payload["cost_points"], payload["required_points"], payload["free_domain"]), (12, 12, False))
        for cost in (6, 12):
            auth = build_roo_points_authorization_payload(domain="mlai.au", action="article_generation",
                cost_points=cost, billing_status="charged", ledger_id=9)
            self.assertEqual(auth["roo_points_required"], cost)
            self.assertFalse(auth["free_domain"])
        self.assertEqual(get_content_factory_content_island_topic_cost_points("mlai.au"), 0)

    def charge(self, *, price, expected, existing=None):
        with patch.object(service, "require_article_activation"), \
             patch("content_factory.company_billing.resolve_content_billing_user", return_value=self.user), \
             patch.object(service, "_original_content_charge_points", return_value=-existing.delta if existing is not None else 0), \
             patch.object(service, "get_content_factory_article_cost_points", return_value=price), \
             patch("content_factory.models.ContentFactoryJob.objects") as jobs, \
             patch("roo.services.PointsService.spend", return_value=(Obj(pk=9), existing is None)) as spend:
            self.last_spend = spend
            jobs.filter.return_value.order_by.return_value.first.return_value = None
            result = service._charge_content_factory_user(user=self.user, created_by_slack_id="mlai_user:7",
                article_request={"client_request_id": "article-7", "expectedCostPoints": expected}, resolved_domain="mlai.au")
        return result, spend

    def test_regular_and_discounted_quotes_are_the_actual_debit(self):
        for price in (6, 12):
            result, spend = self.charge(price=price, expected=price)
            self.assertEqual(result[2], price)
            self.assertEqual(spend.call_args.kwargs["delta"], price)

    def test_changed_quote_rejects_before_spending(self):
        with self.assertRaises(service.ArticleGenerationError) as error:
            self.charge(price=12, expected=6)
        self.assertEqual(error.exception.payload["costPoints"], 12)
        self.last_spend.assert_not_called()

    def test_retry_retains_original_discount_after_expiry(self):
        result, spend = self.charge(price=12, expected=6, existing=Obj(delta=-6))
        self.assertEqual(result[2], 6)
        self.assertEqual(spend.call_args.kwargs["delta"], 6)

    def test_research_confirmation_passes_its_article_quote_to_the_debit(self):
        for price in (6, 12):
            original = {"roo_points_action": "content_island_topic_generation", "expectedCostPoints": 3,
                "client_request_id": "research-key"}
            job = Obj(job_id="research-7", client_request_id="research-key", request_meta=original,
                billing_status="charged", save=Mock())
            with patch.object(service, "_charge_content_factory_request", return_value=(self.user, Obj(pk=9), price)) as charge:
                service._charge_deferred_discovery_job_if_needed(source_job=job, slack_user_id="mlai_user:7",
                    domain="mlai.au", confirmed_keyword="Founder tools", expected_cost_points=price)
            self.assertEqual(charge.call_args.args[1]["expected_cost_points"], price)
            self.assertEqual(job.billing_amount, price)
            self.assertEqual(original["expectedCostPoints"], 3)

    def test_confirmation_without_research_uses_paid_direct_generation(self):
        with patch.object(service, "require_article_activation"), \
             patch.object(service, "get_content_factory_article_cost_points", return_value=6), \
             patch.object(service, "trigger_article_generation", return_value={"job_id": "paid-article"}) as generate:
            result = service.confirm_topic(domain="mlai.au", confirmed_keyword="founder tools",
                slack_user_id="mlai_user:7", billing_user=self.user, expected_cost_points=6)
        self.assertEqual(result["job_id"], "paid-article")
        self.assertEqual(generate.call_args.args[1]["expected_cost_points"], 6)
        self.assertIs(generate.call_args.kwargs["billing_user"], self.user)

    def test_missing_source_run_cannot_create_a_new_paid_article(self):
        with patch.object(service, "require_article_activation"), \
             patch.object(service, "get_content_factory_article_cost_points", return_value=6), \
             patch("content_factory.models.ContentFactoryJob.objects") as jobs, \
             patch.object(service, "trigger_article_generation") as generate:
            jobs.filter.return_value.first.return_value = None
            with self.assertRaises(service.ArticleGenerationError):
                service.confirm_topic(domain="mlai.au", confirmed_keyword="founder tools",
                    slack_user_id="mlai_user:7", billing_user=self.user, source_run_id="missing")
        generate.assert_not_called()
