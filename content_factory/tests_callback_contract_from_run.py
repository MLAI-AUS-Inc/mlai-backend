"""A bound run's callback that omits its consent tuple is checked against the run's own saved consent."""
from django.test import TestCase, override_settings
from rest_framework.parsers import JSONParser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory

from workflow_runs.models import ContentFactoryRun
from .tests_website_connections import WebsiteDatabaseFixture
from .website_connections import fill_callback_contract_from_run, guarded_service_write


class Endpoint:
    calls = []

    @guarded_service_write("config_write", only_repository=True)
    def post(self, request):
        Endpoint.calls.append(dict(request.data))
        return Response({"saved": True})


class CallbackContractFromRunTests(WebsiteDatabaseFixture, TestCase):
    def setUp(self):
        super().setUp()
        Endpoint.calls = []
        self.run = ContentFactoryRun.objects.create(run_id="run-bound", organization=self.org, domain=self.org.domain,
            github_repo=self.website.github_repo, workflow="confirmed_topic", status="approval_required",
            run_request={**self.binding, "topic": "Home modifications"})

    def post(self, data):
        request = Request(APIRequestFactory().post("/api/content-factory/callback", data, format="json"), parsers=[JSONParser()])
        return Endpoint().post(request)

    def bare(self, **extra):
        # La Vie run 73887ea8 (18 Nov 2026): a review-time callback with no connection fields.
        return {"event": "article_review_preview_quality", "job_id": self.run.run_id, "domain": self.org.domain, **extra}

    def test_bare_review_callback_for_a_bound_run_is_accepted(self):
        response = self.post(self.bare())
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(Endpoint.calls), 1)

    @override_settings(ARTICLES_CALLBACK_CONTRACT_FROM_RUN_ENABLED=False)
    def test_kill_switch_restores_the_refusal(self):
        response = self.post(self.bare())
        # B12: the run is bound and its connection live, so this is the
        # retryable protocol fault, not "Reconnect this website".
        self.assertEqual(response.data.get("code"), "callback_contract_missing")
        self.assertEqual(response.data.get("legacy_code"), "website_connection_required")
        self.assertEqual(Endpoint.calls, [])

    def test_run_saved_for_an_older_generation_is_still_refused(self):
        self.website.generation += 1
        self.website.save(update_fields=["generation"])
        response = self.post(self.bare())
        self.assertGreaterEqual(response.status_code, 400)
        self.assertEqual(Endpoint.calls, [])

    def test_a_callback_naming_another_domain_is_not_bound(self):
        response = self.post(self.bare(domain="other.example.test"))
        self.assertEqual(response.data.get("code"), "website_scope_mismatch")
        self.assertEqual(Endpoint.calls, [])

    def test_a_run_without_saved_consent_is_not_bound(self):
        self.run.run_request = {"topic": "Unbound"}
        self.run.save(update_fields=["run_request"])
        response = self.post(self.bare())
        self.assertEqual(response.data.get("code"), "invalid_connection_contract")
        self.assertEqual(Endpoint.calls, [])

    def test_an_explicit_contract_is_never_replaced(self):
        claimed = {**self.binding, "connection_generation": self.website.generation + 5}
        self.assertEqual(fill_callback_contract_from_run(self.bare(**claimed), self.run.run_id)["connection_generation"],
                         self.website.generation + 5)
        response = self.post(self.bare(**claimed))
        self.assertGreaterEqual(response.status_code, 400)
        self.assertEqual(Endpoint.calls, [])

    def test_only_missing_keys_are_added(self):
        filled = fill_callback_contract_from_run(self.bare(), self.run.run_id)
        for key in ("website_connection_id", "connection_generation", "repository_id"):
            self.assertEqual(filled[key], self.binding[key])
        self.assertEqual(filled["event"], "article_review_preview_quality")
        self.assertEqual(fill_callback_contract_from_run(self.bare(), "missing-run"), self.bare())
