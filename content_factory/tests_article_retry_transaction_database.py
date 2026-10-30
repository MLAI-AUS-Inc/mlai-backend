from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase

from core.models import User
from roo.models import Ledger, PointsAccount
from roo.permissions import InsufficientBalanceError
from roo.services import PointsService
from integrations.services.article_generation import InsufficientRooPointsError

from . import vibe_marketing_views as views
from .dispatch_models import ContentFactoryDispatchOutbox
from .tests_website_connections import WebsiteDatabaseFixture
from .website_models import WebsiteConnectionOperation, WebsiteConnectionTarget


class ArticleRetryTransactionTests(WebsiteDatabaseFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(email="article-owner@example.test")
        self.context = SimpleNamespace(organization=self.org)
        self.website.authorized_by = self.user
        self.website.verified_sha = "a" * 40
        self.website.save(update_fields=["authorized_by", "verified_sha"])
        self.config.default_publish_target_id = "native"
        self.config.save(update_fields=["default_publish_target_id"])
        WebsiteConnectionTarget.objects.create(connection=self.website, generation=self.website.generation,
            target_key="native", source_sha=self.website.verified_sha)
        PointsService.award(self.user, 8, "MANUAL", "Synthetic initial balance", "operator", "seed")
        self.enterContext(patch.object(views, "get_content_factory_article_cost_points", return_value=12))
        self.enterContext(patch.object(views, "_refresh_article_editorial_payload", return_value=None))
        self.enterContext(patch.object(views, "_validate_article_start_admission"))
        # Provider and integration-proof checks have their own regressions;
        # this test retains actual reservation, wallet and dispatch SQL.
        self.enterContext(patch.object(views, "_validate_article_start_reservation"))
        self.enterContext(patch.object(views, "_roo_points_balance_for_user",
            side_effect=lambda user: PointsAccount.objects.get(user=user).balance))
        self.enterContext(patch.object(views, "charge_content_factory_request_for_user", side_effect=self.debit))

    def debit(self, *, user, actor_id, article_request, **kwargs):
        try:
            ledger, _ = PointsService.spend(user=user, delta=12, source="CONTENT_FACTORY", description="Synthetic article",
                created_by_slack_id=actor_id, idempotency_key="content_factory:charge:" + article_request["client_request_id"],
                reference_type="CONTENT_FACTORY", reference_id=article_request["client_request_id"])
        except InsufficientBalanceError:
            raise InsufficientRooPointsError({"code": "roo_points_required", "requiredPoints": 12, "currentBalance": 8})
        return user, ledger, 12

    def start(self, *, topic="Synthetic article", key="stable-request"):
        random_id = str(uuid4())
        payload = {"domain": self.org.domain, "github_repo": self.website.github_repo,
            "topic": topic, "slack_user_id": views.founder_actor_id_for_user(self.user),
            "delivery_mode": "review_draft", "analytics_article_id": random_id,
            "analytics_config": {"analytics_article_id": random_id}, "client_request_id": key}
        result = views._charge_roo_points_for_article(
            SimpleNamespace(user=self.user, data={"expectedCostPoints": 12}), context=self.context, payload=payload)
        return result, payload

    def test_repeated_insufficient_balance_rolls_back_operation_and_outbox(self):
        for _ in range(2):
            result, _ = self.start()
            self.assertEqual(result[3].status_code, 402, result[3].data)
            self.assertEqual(result[3].data["requiredPoints"], 12)
            self.assertFalse(WebsiteConnectionOperation.objects.exists())
            self.assertFalse(ContentFactoryDispatchOutbox.objects.exists())
            self.assertFalse(Ledger.objects.filter(kind="SPEND").exists())
            self.assertEqual(PointsAccount.objects.get(user=self.user).balance, 8)

    def test_topup_then_replay_after_slack_linking_reserves_one_operation_and_charge(self):
        denied = self.start()[0][3]
        self.assertEqual(denied.status_code, 402, denied.data)
        PointsService.award(self.user, 24, "MANUAL", "Synthetic topup", "operator", "topup")
        first, original = self.start()
        self.assertIsNone(first[3], getattr(first[3], "data", None))
        self.user.slack_id = "U76543210"
        self.user.save(update_fields=["slack_id"])
        replay, repeated = self.start()
        self.assertIsNone(replay[3], getattr(replay[3], "data", None))
        self.assertEqual(original["analytics_article_id"], repeated["analytics_article_id"])
        self.assertEqual(original["slack_user_id"], repeated["slack_user_id"])
        self.assertEqual(WebsiteConnectionOperation.objects.count(), 1)
        self.assertEqual(ContentFactoryDispatchOutbox.objects.count(), 1)
        self.assertEqual(Ledger.objects.filter(kind="SPEND").count(), 1)
        self.assertEqual(PointsAccount.objects.get(user=self.user).balance, 20)
        conflict, _ = self.start(topic="Different article")
        self.assertEqual(conflict[3].status_code, 409)
        self.assertEqual(Ledger.objects.filter(kind="SPEND").count(), 1)
