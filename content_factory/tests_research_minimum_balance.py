"""A18: paid research is refused before dispatch when Content Factory would refuse it.

Content Factory gates topic research that is not island research on a 6-point
floor, read after the backend's charge. The backend now checks that floor
before charging and before any run row exists, except for the granted free
first topic batch, which Content Factory accepts at any balance. A refusal
Content Factory still returns keeps its own code on the run.
"""
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from integrations.services.article_generation import (
    InsufficientRooPointsError,
    charge_content_factory_topic_generation_for_user,
)
from roo.models import Ledger
from roo.services import PointsService
from workflow_runs.models import ContentFactoryRun

from . import first_article_entitlement as first_article
from . import vibe_marketing_views as views
from .billing import research_needs_minimum_balance
from .founder_journey_testing import ALL_P1_FLAGS_ON
from .self_heal_testing import make_p2_org
from .tests_run_event_authority import FLAGS

SETTINGS = dict(**FLAGS, **ALL_P1_FLAGS_ON, CONTENT_FACTORY_URL="https://topic.fixture.invalid",
    CONTENT_FACTORY_API_KEY="synthetic-topic-key", ARTICLES_FREE_FIRST_ARTICLE_ENABLED=True,
    ARTICLES_FREE_FIRST_ARTICLE_DAILY_CAP=0, ARTICLES_FREE_FIRST_ARTICLE_SPIKE_THRESHOLD=25,
    ARTICLES_RUN_CONTROL_MODEL_USD="5", ARTICLES_RUN_CONTROL_MODEL_TOKENS=100000)


def worker_response(status_code, body):
    import requests
    response = requests.Response()
    response.status_code = status_code
    response._content = body
    response._content_consumed = True
    return response


@override_settings(**SETTINGS)
class ResearchMinimumBalanceTests(TransactionTestCase):
    def setUp(self):
        self.fixture = make_p2_org("minimum-balance.fixture.test")
        self.owner = self.fixture.owner

    def fund(self, points, key="funding"):
        PointsService.credit_purchased_topup(user=self.owner, delta=points, description="Synthetic funding",
            idempotency_key=f"{key}:{self.owner.pk}")

    def use_free_batch(self):
        grant = first_article.grant_topic_batch(organization=self.fixture.organization, user=self.owner,
            client_request_id="earlier-free-batch", requested_topic_count=3)
        self.assertIsNotNone(grant)

    def research(self, key, *, expected=None, remote=None, topics=3):
        from integrations import http_client
        body = {"companyId": str(self.fixture.company.pk), "clientRequestId": key, "requestedTopicCount": topics}
        if expected is not None:
            body["expectedCostPoints"] = expected
        request = APIRequestFactory().post("/api/v1/vibe-marketing/discovery/", body, format="json")
        force_authenticate(request, user=self.owner)
        remote = remote or (lambda url, **kwargs: worker_response(202, b'{"run_id":"worker-run","status":"queued"}'))
        with patch.object(http_client, "post", side_effect=remote) as post:
            response = views.VibeMarketingDiscoveryView.as_view()(request)
        return response, post

    def spends(self):
        return Ledger.objects.filter(user=self.owner, kind="SPEND")

    def test_zero_points_with_the_free_batch_available_runs_at_cost_zero(self):
        response, post = self.research("free-first-topics", expected=0)
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(post.call_count, 1)
        sent = post.call_args.kwargs["json"]
        self.assertEqual((sent["roo_points_billing_status"], sent["roo_points_cost"], sent["roo_points_action"]),
            ("entitled", 0, "topic_discovery"))
        self.assertFalse(self.spends().exists())

    def test_zero_points_with_the_batch_used_returns_402_and_writes_no_run(self):
        self.use_free_batch()
        response, post = self.research("paid-topics")
        self.assertEqual(response.status_code, 402, response.data)
        self.assertEqual(response.data["error_code"], "INSUFFICIENT_ROO_POINTS")
        post.assert_not_called()
        self.assertFalse(ContentFactoryRun.objects.filter(workflow="auto_discovery").exists())
        self.assertFalse(self.spends().exists())

    def test_a_balance_that_pays_but_falls_under_the_floor_is_refused_before_the_charge(self):
        self.use_free_batch()
        self.fund(8)
        response, post = self.research("paid-topics-short")
        self.assertEqual(response.status_code, 402, response.data)
        self.assertEqual((response.data["cost_points"], response.data["required_points"],
            response.data["current_balance"], response.data["shortfall_points"]), (3, 9, 8, 1))
        self.assertIn("needs 6 points left to start", response.data["detail"])
        post.assert_not_called()
        self.assertFalse(ContentFactoryRun.objects.filter(workflow="auto_discovery").exists())
        self.assertFalse(self.spends().exists())

    def test_a_balance_that_keeps_the_floor_after_the_charge_dispatches(self):
        self.use_free_batch()
        self.fund(9)
        response, post = self.research("paid-topics-enough")
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs["json"]["roo_points_billing_status"], "charged")
        self.assertEqual(self.spends().count(), 1)

    def test_a_replay_of_the_granted_batch_keeps_its_zero_quote(self):
        payload = {"requested_topic_count": 3, "client_request_id": "free-replay"}
        context = SimpleNamespace(organization=self.fixture.organization, company=self.fixture.company)
        request = SimpleNamespace(user=self.owner, data={"expectedCostPoints": 0})
        for _ in range(2):
            attempt = dict(payload)
            _user, _request, error = views._charge_roo_points_for_content_island_topic_generation(
                request, context=context, payload=attempt, first_topic_batch=True)
            self.assertIsNone(error)
            self.assertEqual((attempt["roo_points_billing_status"], attempt["roo_points_cost"]), ("entitled", 0))
        self.assertFalse(self.spends().exists())

    @override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=False, ARTICLES_RUN_FACT_AUTHORITY_ENABLED=False,
                       ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=False)
    def test_cf_refusal_code_is_the_run_code_for_registry_and_journey(self):
        # Without Run-first admission the refusal is stored as the blocked run
        # (an admitted run keeps a keyed refusal pending resolution instead).
        from . import registry_policy
        from .staff_alert_rules import failure_code
        self.use_free_batch()
        self.fund(50)
        body = (b'{"detail":{"error":"This AI action requires at least 6 Roo points before it can start.",'
                b'"error_code":"INSUFFICIENT_ROO_POINTS","required_points":6,"current_balance":4,"cost_points":0,'
                b'"retryable":false}}')
        response, _post = self.research("refused-topics", remote=lambda url, **kwargs: worker_response(402, body))
        self.assertEqual(response.status_code, 503, response.data)
        run = ContentFactoryRun.objects.get(workflow="auto_discovery")
        self.assertEqual(run.result["error_code"], "INSUFFICIENT_ROO_POINTS")
        self.assertEqual(failure_code(run), "INSUFFICIENT_ROO_POINTS")
        self.assertEqual(registry_policy.failure_class(failure_code(run)), "needs_user")
        self.assertEqual(registry_policy.founder_copy_id(failure_code(run)), "ask_buy_points")


@override_settings(**SETTINGS)
class ResearchFloorScopeTests(TransactionTestCase):
    def setUp(self):
        self.fixture = make_p2_org("floor-scope.fixture.test")
        self.owner = self.fixture.owner
        self.actor = views.founder_actor_id_for_user(self.owner)
        PointsService.credit_purchased_topup(user=self.owner, delta=5, description="Synthetic funding",
            idempotency_key=f"floor-scope:{self.owner.pk}")

    def charge(self, request):
        return charge_content_factory_topic_generation_for_user(user=self.owner, actor_id=self.actor,
            article_request=request, resolved_domain=self.fixture.organization.domain)

    def test_island_research_keeps_its_paid_gate_without_the_floor(self):
        for label, extra in (("island_topics", {"content_island_slug": "pet-insurance"}),
                             ("island_research", {"island_research_brief": {"topic": "Pet insurance"}})):
            with self.subTest(label):
                request = {"client_request_id": f"floor-{label}", "requested_topic_count": 1, **extra}
                self.assertFalse(research_needs_minimum_balance(request))
                _user, ledger, cost = self.charge(request)
                self.assertEqual(cost, 1)
                self.assertIsNotNone(ledger)

    def test_topic_research_and_scheduled_research_need_the_floor_after_the_charge(self):
        request = {"client_request_id": "floor-topics", "requested_topic_count": 3}
        self.assertTrue(research_needs_minimum_balance(request))
        with self.assertRaises(InsufficientRooPointsError) as refused:
            self.charge(request)
        self.assertEqual((refused.exception.payload["required_points"], refused.exception.payload["current_balance"]),
            (9, 5))
        self.assertFalse(Ledger.objects.filter(user=self.owner, kind="SPEND").exists())

    def test_a_replay_of_a_charged_request_needs_only_the_floor(self):
        PointsService.credit_purchased_topup(user=self.owner, delta=4, description="Synthetic funding",
            idempotency_key=f"floor-scope-more:{self.owner.pk}")
        request = {"client_request_id": "floor-replay", "requested_topic_count": 3}
        _user, first, _cost = self.charge(request)
        # 9 - 3 leaves exactly the 6-point floor: a replay needs only the floor,
        # not the price again (cost + floor would be 9 and refuse it).
        _user, again, _cost = self.charge(dict(request))
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(Ledger.objects.filter(user=self.owner, kind="SPEND").count(), 1)


class RefusalCodeTests(TransactionTestCase):
    def test_code_is_read_from_every_refusal_shape(self):
        for body in ({"error_code": "INSUFFICIENT_ROO_POINTS"}, {"detail": {"error_code": "INSUFFICIENT_ROO_POINTS"}},
                     {"error": {"error_code": "INSUFFICIENT_ROO_POINTS"}}):
            with self.subTest(body=body):
                self.assertEqual(views.content_factory_refusal_code(body), "INSUFFICIENT_ROO_POINTS")
        for body in (None, "text", {"detail": "plain"}, {"detail": {"error_code": ""}}):
            self.assertEqual(views.content_factory_refusal_code(body), "")

    def test_stored_error_object_lifts_its_code_without_replacing_one(self):
        lifted = views._run_result_from_remote({"status": "blocked", "error": {"error_code": "INSUFFICIENT_ROO_POINTS"}})
        self.assertEqual(lifted["error_code"], "INSUFFICIENT_ROO_POINTS")
        kept = views._run_result_from_remote({"status": "failed", "error_code": "ORIGINAL",
            "error": {"error_code": "INSUFFICIENT_ROO_POINTS"}})
        self.assertEqual(kept["error_code"], "ORIGINAL")
