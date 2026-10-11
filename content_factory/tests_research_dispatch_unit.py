"""Database-free research spend/outbox, delivery and original-price refund fences."""
from contextlib import contextmanager
from types import SimpleNamespace as Obj
from unittest.mock import patch

from django.test import SimpleTestCase

from content_factory import dispatch_outbox as outbox, vibe_marketing_views as views
from content_factory.website_contract import WebsiteAuthorityError
from integrations.services import article_generation as billing


class ResearchDispatchTests(SimpleTestCase):
    def setUp(self):
        self.user = Obj(pk=7, id=7)
        self.company = Obj(pk="company-1")
        self.org = Obj(pk=9, id=9, domain="synthetic.example.test")
        self.context = Obj(organization=self.org, company=self.company)
        self.payload = {"domain": self.org.domain, "requested_topic_count": 3,
                        "custom_topic_title": "Original topic", "mcp_request_hash": "a" * 64,
                        "mcp_company_id": "company-1"}

    def _charge(self, reserve_failure=False):
        events = []
        active = {"transaction": False}
        @contextmanager
        def atomic():
            self.assertFalse(active["transaction"])
            active["transaction"] = True
            try:
                yield
            except Exception:
                events.append("rolled_back")
                raise
            else:
                events.append("committed")
            finally:
                active["transaction"] = False
        def charge(**kwargs):
            self.assertTrue(active["transaction"])
            events.append("charged")
            return self.user, Obj(id=123), 3
        def reserve(**kwargs):
            self.assertTrue(active["transaction"])
            events.append("reserved")
            self.assertEqual((kwargs["endpoint"], kwargs["workflow"]), ("discovery", "auto_discovery"))
            self.assertEqual(kwargs["billing_user_id"], 7)
            saved = kwargs["payload"]
            self.assertEqual(saved["roo_points_requested_by_user_id"], 7)
            self.assertEqual(saved["mcp_request_hash"], "a" * 64)
            self.assertEqual(saved["custom_topic_title"], "Original topic")
            self.assertEqual(saved["roo_points_ledger_id"], "123")
            if reserve_failure:
                raise RuntimeError("Synthetic receipt failure")
        with patch.object(views, "_quoted_price_response", return_value=None), \
             patch.object(views, "founder_actor_id_for_user", return_value="mlai_user:7"), \
             patch.object(views, "_roo_points_balance_for_user", return_value=7), \
             patch.object(views.transaction, "atomic", side_effect=atomic), \
             patch.object(views, "charge_content_factory_topic_generation_for_user", side_effect=charge), \
             patch.object(outbox, "reserve_dispatch", side_effect=reserve):
            if reserve_failure:
                with self.assertRaises(RuntimeError):
                    views._charge_roo_points_for_content_island_topic_generation(Obj(user=self.user, data={}),
                        context=self.context, payload=self.payload)
            else:
                self.assertIsNone(views._charge_roo_points_for_content_island_topic_generation(
                    Obj(user=self.user, data={}), context=self.context, payload=self.payload)[2])
        return events

    def test_research_spend_and_original_input_receipt_commit_together(self):
        self.assertEqual(self._charge(), ["charged", "reserved", "committed"])

    def test_receipt_failure_exits_spend_transaction_without_committing(self):
        self.assertEqual(self._charge(reserve_failure=True), ["charged", "reserved", "rolled_back"])

    def row(self):
        request = {**self.payload, "roo_points_requested_by_user_id": 7, "roo_points_cost": 3,
                   "client_request_id": "research-key"}
        return Obj(organization=self.org, organization_id=9, client_request_id="research-key",
            payload={"request": request, "endpoint": "discovery", "workflow": "auto_discovery",
                     "billing_user_id": "7", "actor_id": "mlai_user:7"})

    def test_research_delivery_uses_discovery_and_never_article_repository_gates(self):
        from django.contrib.auth import get_user_model
        from founder_tools.models import VibeRaisingCompany
        from integrations import http_client
        row = self.row()
        response = Obj(status_code=202, json=lambda: {"run_id": "research-1"})
        with patch.object(views, "_content_factory_remote_config", return_value={"enabled": True, "base_url": "https://worker.example.test"}), \
             patch.object(views, "_lookup_content_factory_dispatch_by_key", return_value=("absent", {})), \
             patch.object(views, "_content_factory_headers", return_value={}), \
             patch.object(get_user_model().objects, "get", return_value=self.user), \
             patch.object(VibeRaisingCompany.objects, "filter") as companies, \
             patch.object(views, "_get_config", return_value=Obj(pillar_strategy={})), \
             patch.object(views, "_refresh_article_editorial_payload") as article_policy, \
             patch.object(billing, "require_article_activation") as article_activation, \
             patch("content_factory.website_connections.authority_guard") as repo, \
             patch.object(http_client, "post", return_value=response) as post, \
             patch.object(outbox, "_accept_delivery", return_value=("delivered", "", "research-1")):
            companies.return_value.exists.return_value = True
            self.assertEqual(outbox._deliver(row), ("delivered", "", "research-1"))
        self.assertEqual(post.call_args.args[0], "https://worker.example.test/api/runs/discovery")
        self.assertEqual(post.call_args.kwargs["json"]["custom_topic_title"], "Original topic")
        self.assertEqual(companies.call_args.kwargs["pk"], "company-1")
        article_policy.assert_not_called()
        article_activation.assert_not_called()
        repo.assert_not_called()

    def test_changed_research_owner_or_domain_cannot_dispatch(self):
        from django.contrib.auth import get_user_model
        from founder_tools.models import VibeRaisingCompany
        from integrations import http_client
        for changed in ("owner", "domain"):
            row = self.row()
            if changed == "domain":
                row.payload["request"]["domain"] = "old.example.test"
            with self.subTest(changed=changed), \
                 patch.object(views, "_content_factory_remote_config", return_value={"enabled": True}), \
                 patch.object(views, "_lookup_content_factory_dispatch_by_key", return_value=("absent", {})), \
                 patch.object(get_user_model().objects, "get", return_value=self.user), \
                 patch.object(VibeRaisingCompany.objects, "filter") as companies, \
                 patch.object(http_client, "post") as post:
                companies.return_value.exists.return_value = False
                with self.assertRaises(WebsiteAuthorityError):
                    outbox._deliver(row)
            post.assert_not_called()

    def test_research_refund_uses_topic_ledger_family(self):
        from django.contrib.auth import get_user_model
        row = self.row()
        with patch.object(get_user_model().objects, "get", return_value=self.user), \
             patch.object(billing, "refund_content_factory_topic_generation_for_user") as research_refund, \
             patch.object(billing, "refund_content_factory_request_for_user") as article_refund:
            outbox.refund_dispatch(row, reason="dispatch_never_received")
        research_refund.assert_called_once()
        self.assertEqual(research_refund.call_args.kwargs["article_request"]["client_request_id"], "research-key")
        article_refund.assert_not_called()

    def test_changed_approved_audience_cannot_dispatch(self):
        from django.contrib.auth import get_user_model
        from founder_tools.models import VibeRaisingCompany
        from integrations import http_client
        row = self.row()
        row.payload["request"]["research_audience"] = {"id": "audience-1", "version": 1}
        with patch.object(views, "_content_factory_remote_config", return_value={"enabled": True}), \
             patch.object(views, "_lookup_content_factory_dispatch_by_key", return_value=("absent", {})), \
             patch.object(get_user_model().objects, "get", return_value=self.user), \
             patch.object(VibeRaisingCompany.objects, "filter") as companies, \
             patch.object(views, "_get_config", return_value=Obj(pillar_strategy={})), \
             patch("content_factory.editorial_catalog.discovery_audience_context", return_value={"id": "audience-1", "version": 2}), \
             patch.object(http_client, "post") as post:
            companies.return_value.exists.return_value = True
            with self.assertRaises(WebsiteAuthorityError):
                outbox._deliver(row)
        post.assert_not_called()


class OriginalResearchRefundTests(SimpleTestCase):
    def test_paid_to_free_and_free_to_paid_price_changes_use_original_charge(self):
        from roo.models import Ledger
        from roo.services import PointsService
        user = Obj(pk=7)
        for original, live_price in ((3, 0), (0, 3)):
            ledger = Obj(user_id=7, source=billing.CONTENT_FACTORY_LEDGER_SOURCE,
                         delta_microroo=-original * 1_000_000) if original else None
            with self.subTest(original=original, live_price=live_price), \
                 patch.object(Ledger.objects, "filter") as lookup, \
                 patch.object(billing, "get_content_factory_research_cost_points", return_value=live_price) as price, \
                 patch.object(PointsService, "refund", return_value=(Obj(pk=1), True)) as refund:
                lookup.return_value.first.return_value = ledger
                billing.refund_content_factory_topic_generation_for_user(user=user, actor_id="mlai_user:7",
                    article_request={"client_request_id": "research-key", "requested_topic_count": 3},
                    resolved_domain="synthetic.example.test", reason="Synthetic refusal")
                if original:
                    self.assertEqual(refund.call_args.kwargs["delta"], original)
                    self.assertEqual(refund.call_args.kwargs["original_spend_key"], "content_factory:topic_generation:charge:research-key")
                else:
                    refund.assert_not_called()
                price.assert_not_called()

    def test_refund_cannot_credit_another_payers_charge(self):
        from roo.models import Ledger
        from roo.services import PointsService
        with patch.object(Ledger.objects, "filter") as lookup, patch.object(PointsService, "refund") as refund:
            lookup.return_value.first.return_value = Obj(user_id=8, source=billing.CONTENT_FACTORY_LEDGER_SOURCE, delta_microroo=-3_000_000)
            with self.assertRaises(billing.ArticleGenerationError):
                billing.refund_content_factory_topic_generation_for_user(user=Obj(pk=7), actor_id="mlai_user:7",
                    article_request={"client_request_id": "research-key"}, resolved_domain="synthetic.example.test", reason="Synthetic")
        refund.assert_not_called()
