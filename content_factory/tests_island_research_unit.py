"""Database-free contract tests. Run with python -m unittest (no migrations)."""
import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from django.conf import settings
if not settings.configured:
    settings.configure(SECRET_KEY="island-research-unit-only", USE_TZ=True, USE_I18N=False,
        DATABASES={"default": {"ENGINE": "django.db.backends.dummy"}},
        REST_FRAMEWORK={"UNAUTHENTICATED_USER": None, "DEFAULT_AUTHENTICATION_CLASSES": []})
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate
from .island_research import validate_research_brief, research_request_key, proposal_for_adoption, refund_empty_or_failed_research
from .island_research_views import ContentIslandResearchView, ContentIslandResearchAdoptView

BRIEF = {"subject": "AI integration in small businesses", "searchIntent": "informational", "clientRequestId": "request-12345"}


def module(name, **attrs):
    value = ModuleType(name)
    value.__dict__.update(attrs)
    return value


class IslandResearchTests(unittest.TestCase):
    def test_words_description_unicode_and_noncommercial_topics_are_supported(self):
        for subject in ["AI", "園芸", "Local volunteering", "Show people how to grow food on a small balcony"]:
            brief = validate_research_brief({"subject": subject})
            self.assertEqual(brief["subject"], subject)
            self.assertEqual(brief["intent"], "any")
            self.assertNotIn("name", brief)
            self.assertNotIn("keyword", brief)

    def test_invalid_brief_rejected_before_billing(self):
        for bad in [{"subject": " "}, {"subject": {}}, {"subject": "AI", "searchIntent": []},
                    {"subject": "AI", "searchIntent": "custom"}, {"subject": "AI", "audience": "x"*301}]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_research_brief(bad)

    def test_idempotency_is_bound_to_company_payer_brief_and_nonce(self):
        brief = validate_research_brief(BRIEF)
        key = research_request_key("org", "user", "nonce-1234", brief)
        self.assertLessEqual(len(key), 100)
        self.assertEqual(key, research_request_key("org", "user", "nonce-1234", dict(reversed(list(brief.items())))))
        for org, user, nonce, changed in [("other", "user", "nonce-1234", brief), ("org", "other", "nonce-1234", brief),
            ("org", "user", "nonce-5678", brief), ("org", "user", "nonce-1234", {**brief, "subject": "Birds"})]:
            self.assertNotEqual(key, research_request_key(org, user, nonce, changed))

    def setUp(self):
        self.org = SimpleNamespace(pk="org", domain="example.test")
        self.context = SimpleNamespace(organization=self.org)
        self.user = SimpleNamespace(pk="user", is_authenticated=True)
        self.manager = Mock()
        self.manager.filter.return_value.first.return_value = None
        self.charge = Mock(return_value=(self.user, {"client_request_id": "key"}, None))
        self.queue = Mock(return_value=SimpleNamespace(status="queued", run_id="run-one"))
        self.views = module("content_factory.vibe_marketing_views",
            _resolve_context_or_response=Mock(return_value=(self.context, None)),
            _run_belongs_to_context=Mock(return_value=True),
            founder_actor_id_for_user=lambda user: "web:user", CONTENT_FACTORY_REQUEST_SOURCE="roo_slackbot",
            CONTENT_FACTORY_ACTION_CONTENT_ISLAND_TOPIC_GENERATION="content_island_topic_generation",
            _charge_roo_points_for_content_island_topic_generation=self.charge, _queue_content_factory_run=self.queue,
            _get_config=lambda org: SimpleNamespace(), _run_start_payload=lambda run: {"runId": run.run_id, "status": run.status})
        self.modules = {"content_factory.vibe_marketing_views": self.views,
            "workflow_runs.models": module("workflow_runs.models", ContentFactoryRun=SimpleNamespace(objects=self.manager))}

    def post(self, body=None, authenticated=True, adopt=False):
        request = APIRequestFactory().post("/islands/research", body or BRIEF, format="json")
        if authenticated: force_authenticate(request, user=self.user)
        with patch.dict(sys.modules, self.modules), patch("content_factory.vibe_marketing_views", self.views, create=True):
            if adopt: return ContentIslandResearchAdoptView.as_view()(request, run_id="run-one")
            return ContentIslandResearchView.as_view()(request)

    def test_authenticated_paid_dispatch_carries_brief_without_creating_an_island(self):
        response = self.post()
        self.assertEqual(response.status_code, 202)
        kwargs = self.queue.call_args.kwargs
        self.assertEqual(kwargs["endpoint"], "island-research")
        self.assertEqual(kwargs["payload"]["island_research_brief"]["subject"], BRIEF["subject"])
        self.assertNotIn("content_island_slug", kwargs["payload"])
        self.assertIs(kwargs["billing_refund_context"]["charged_user"], self.user)
        self.charge.assert_called_once()

    def test_unauthenticated_invalid_company_and_insufficient_balance_never_queue(self):
        self.assertIn(self.post(authenticated=False).status_code, [401, 403])
        self.assertEqual(self.post({**BRIEF, "subject": " "}).status_code, 400)
        self.charge.assert_not_called()
        self.charge.return_value = (None, None, Response({"detail": "You need 1 Roo Point"}, status=402))
        self.assertEqual(self.post().status_code, 402)
        self.queue.assert_not_called()
        self.views._resolve_context_or_response.return_value = (None, Response({}, status=403))
        self.assertEqual(self.post().status_code, 403)
        self.queue.assert_not_called()

    def test_retry_reuses_durable_run_without_recharging_or_requeueing(self):
        self.manager.filter.return_value.first.return_value = SimpleNamespace(run_id="same-run", status="completed")
        self.assertEqual(self.post().data["runId"], "same-run")
        self.charge.assert_not_called()
        self.queue.assert_not_called()

    def test_adoption_uses_stored_proposal_and_checks_organization(self):
        proposal = {"id": "proposal-one", "name": "Measured theme", "keywords": [1, 2, 3], "centroid_embedding": [1, 0]}
        run = SimpleNamespace(status="completed", run_request={"island_research_brief": BRIEF},
            result={"island_research": True, "suggested_islands": [proposal]})
        self.assertIs(proposal_for_adoption(run, "proposal-one"), proposal)
        for invalid in ["made-up", None, {"id": "proposal-one"}]:
            with self.assertRaises(ValueError): proposal_for_adoption(run, invalid)
        run.status = "queued"
        with self.assertRaises(ValueError): proposal_for_adoption(run, "proposal-one")
        self.manager.filter.return_value.first.return_value = run
        self.views._run_belongs_to_context.return_value = False
        self.assertEqual(self.post({"proposalId": "proposal-one"}, adopt=True).status_code, 404)

    def test_terminal_failure_or_empty_result_refunds_actual_payer_once(self):
        payer = SimpleNamespace(pk="original-payer")
        ledger = SimpleNamespace(user=payer, delta=-1, source="CONTENT_FACTORY", created_by_slack_id="web:payer", reference_id="key")
        ledgers, points = Mock(), Mock()
        ledgers.select_related.return_value.filter.return_value.first.return_value = ledger
        run = SimpleNamespace(status="completed", domain="example.test", save=Mock(),
            run_request={"island_research_brief": BRIEF, "client_request_id": "key"},
            result={"island_research": True, "suggested_islands": []})
        modules = {"roo.models": module("roo.models", Ledger=SimpleNamespace(objects=ledgers)),
                   "roo.services": module("roo.services", PointsService=points)}
        with patch.dict(sys.modules, modules):
            refund_empty_or_failed_research(run)
            refund_empty_or_failed_research(run)
        points.refund.assert_called_once()
        self.assertIs(points.refund.call_args.kwargs["user"], payer)
        self.assertEqual(points.refund.call_args.kwargs["delta"], 1)
        self.assertTrue(run.result["island_research_refunded"])

    def test_ambiguous_dispatch_and_successful_research_are_never_refunded(self):
        points = Mock()
        with patch.dict(sys.modules, {"roo.models": module("roo.models", Ledger=Mock()),
                                     "roo.services": module("roo.services", PointsService=points)}):
            for status, request, result in [
                ("blocked", {"island_research_brief": BRIEF, "dispatch_pending_resolution": True}, {}),
                ("running", {"island_research_brief": BRIEF}, {}),
                ("completed", {"island_research_brief": BRIEF}, {"island_research": True, "suggested_islands": [{"id": "one"}]}),
                ("failed", {}, {}),
            ]:
                refund_empty_or_failed_research(SimpleNamespace(status=status, run_request=request, result=result))
        points.refund.assert_not_called()
