"""Actual owner HTTP research admission; only external worker transport is fake."""
from copy import deepcopy
import json
import os
from pathlib import Path
from unittest.mock import patch

from django.test import TransactionTestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from roo.models import Ledger
from roo.services import PointsService
from workflow_runs.models import ContentFactoryRun
from .founder_journey_testing import ALL_P1_FLAGS_ON
from .island_research_views import ContentIslandResearchView
from .self_heal_testing import make_p2_org
from .tests_run_event_authority import FLAGS


@override_settings(**FLAGS, **ALL_P1_FLAGS_ON, CONTENT_FACTORY_URL="https://topic.fixture.invalid",
                   CONTENT_FACTORY_API_KEY="synthetic-topic-key", ARTICLES_FREE_FIRST_ARTICLE_ENABLED=True,
                   ARTICLES_RUN_CONTROL_MODEL_USD="5", ARTICLES_RUN_CONTROL_MODEL_TOKENS=100000)
class TopicProducerTests(TransactionTestCase):
    def test_actual_free_first_topic_http_exports_unchanged_original_discovery_admission(self):
        import requests
        from integrations import http_client
        from .vibe_marketing_views import VibeMarketingDiscoveryView
        from .run_ownership import owner_receipt, wire_identity
        fixture = make_p2_org("actual-first-topics.fixture.test")
        request = APIRequestFactory().post("/api/v1/vibe-marketing/discovery/", {
            "companyId": str(fixture.company.pk), "clientRequestId": "actual-first-topics-20261103",
            "requestedTopicCount": 3, "expectedCostPoints": 0}, format="json")
        force_authenticate(request, user=fixture.owner)
        captured = []
        def post(url, *, json, **kwargs):
            self.assertEqual(url, "https://topic.fixture.invalid/api/runs/discovery")
            run = ContentFactoryRun.objects.get(result__run_owner_v1__identity__dispatch_key=json["client_request_id"])
            self.assertEqual(json["backend_run_admission_v1"], wire_identity(owner_receipt(run)))
            captured.append(deepcopy(json))
            response = requests.Response(); response.status_code = 202
            response._content = b'{"run_id":"r3-actual-first-topics-worker","status":"queued"}'
            response._content_consumed = True
            return response
        with patch.object(http_client, "post", side_effect=post) as remote:
            response = VibeMarketingDiscoveryView.as_view()(request)
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(remote.call_count, 1)
        run = ContentFactoryRun.objects.get(run_id="r3-actual-first-topics-worker")
        grant = Ledger.objects.get(kind="ADJUST")
        self.assertEqual(run.workflow, "auto_discovery")
        self.assertEqual(run.run_request["roo_points_action"], "topic_discovery")
        self.assertEqual(run.run_request["requested_topic_count"], 3)
        self.assertEqual(str(grant.pk), run.run_request["roo_points_ledger_id"])
        self.assertEqual(grant.delta_microroo, 0)
        self.assertFalse(Ledger.objects.filter(kind__in=["SPEND", "REFUND"]).exists())
        self.assertEqual(captured[0]["backend_run_admission_v1"], run.run_request["backend_run_admission_v1"])
        if os.environ.get("R3_EXPORT_FREE_HTTP_TOPIC_ADMISSION") == "1":
            value = {"schema_version": 1, "run_id": run.run_id, "run_request": run.run_request,
                "producer": "actual authenticated VibeMarketingDiscoveryView, only HTTP worker POST mocked",
                "original_ledger": {"id": grant.pk, "kind": grant.kind, "user_id": grant.user_id,
                    "source": grant.source, "idempotency_key": grant.idempotency_key,
                    "delta_microroo": grant.delta_microroo}}
            Path("/Users/samdonegan/Documents/Code/founder-journey-worktrees/recovery/R3_BE_ACTUAL_FREE_FIRST_TOPIC_HTTP_FOR_CF.json").write_text(json.dumps(value, indent=2) + "\n")

    def admit_paid_brief(self, admission=None):
        import requests
        from integrations import http_client
        from .run_ownership import owner_receipt, wire_identity
        if admission is not None:
            from django.contrib.auth import get_user_model
            from organizations.models import Organization
            from .tests_run_terminal_settlement import ActualPairedTerminalSettlementTests
            wire = admission["run_request"]["backend_run_admission_v1"]
            for model, value in ((Organization, int(wire["organization_id"])),
                    (get_user_model(), int(wire["principal"]["user_id"])), (Ledger, admission["original_ledger"]["id"] - 1)):
                ActualPairedTerminalSettlementTests.sequence(self, model, value)
        fixture = make_p2_org("actual-island-paid.fixture.test")
        self.fixture = fixture
        PointsService.credit_purchased_topup(user=fixture.owner, delta=100,
            description="Synthetic real brief HTTP funding", idempotency_key="actual-island-http-funding")
        request = APIRequestFactory().post("/api/v1/content-islands/research/", {
            "companyId": str(fixture.company.pk), "clientRequestId": "actual-paid-island-20261103",
            "subject": "Balcony gardens", "description": "Measured practical garden ideas",
            "audience": "Local gardeners", "expectedCostPoints": 1}, format="json")
        force_authenticate(request, user=fixture.owner)
        captured = []

        def post(url, *, json, **kwargs):
            self.assertEqual(url, "https://topic.fixture.invalid/api/runs/island-research")
            run = ContentFactoryRun.objects.get(result__run_owner_v1__identity__dispatch_key=json["client_request_id"])
            self.assertEqual(run.workflow, "island_refresh")
            self.assertEqual(json["backend_run_admission_v1"], wire_identity(owner_receipt(run)))
            captured.append(deepcopy(json))
            response = requests.Response(); response.status_code = 202
            response._content = b'{"run_id":"r3-actual-island-paid-worker","status":"queued"}'
            response._content_consumed = True
            return response

        with patch.object(http_client, "post", side_effect=post) as remote:
            response = ContentIslandResearchView.as_view()(request)
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(remote.call_count, 1)
        run = ContentFactoryRun.objects.get(run_id="r3-actual-island-paid-worker")
        spend = Ledger.objects.get(kind="SPEND")
        self.assertEqual(spend.delta_microroo, -1_000_000)
        self.assertEqual(run.run_request["roo_points_action"], "content_island_topic_generation")
        self.assertEqual(run.run_request["requested_topic_count"], 1)
        self.assertTrue(run.run_request["island_research_brief"])
        self.assertEqual(captured[0]["backend_run_admission_v1"], run.run_request["backend_run_admission_v1"])
        if admission is not None:
            self.assertEqual(run.run_request, admission["run_request"])
            self.assertEqual(run.run_id, admission["run_id"])
        if os.environ.get("R3_EXPORT_ISLAND_ADMISSION") == "1":
            value = {"schema_version": 1, "run_id": run.run_id, "run_request": run.run_request,
                "producer": "actual authenticated ContentIslandResearchView, only HTTP worker POST mocked",
                "original_ledger": {"id": spend.pk, "kind": spend.kind, "user_id": spend.user_id,
                    "source": spend.source, "idempotency_key": spend.idempotency_key,
                    "delta_microroo": spend.delta_microroo}}
            Path("/Users/samdonegan/Documents/Code/founder-journey-worktrees/recovery/R3_BE_ACTUAL_PAID_ISLAND_FOR_CF.json").write_text(json.dumps(value, indent=2) + "\n")
        return run, spend

    def test_actual_paid_brief_http_admits_original_research_before_one_worker_post(self):
        self.admit_paid_brief()

    def restore_free_http_pair(self, outcome):
        """Rebuild real grant/admission; compare the unchanged HTTP/CF wire tuple.

        The exported HTTP key is opaque input here. We never restamp worker
        identity or copy private owner receipts to make a fixture fit.
        """
        from django.contrib.auth import get_user_model
        from organizations.models import Organization
        from .tests_run_terminal_settlement import ActualPairedTerminalSettlementTests
        from .first_article_entitlement import grant_topic_batch, entitled_authorization
        from .run_ownership import WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY, BUDGET_KEY, reserve_admitted_run, bind_admitted_run, user_principal
        from .run_backend_facts import create_job_facts
        value = json.loads((Path(__file__).parent / "testdata/r3" /
            ("topic_actual_free_first_http_" + outcome + ".json")).read_text())
        original = value["original_backend_admission"]
        request, ledger = original["run_request"], original["original_ledger"]
        identity = request[WIRE_REQUEST_KEY]
        for model, target in ((Organization, int(identity["organization_id"])),
                (get_user_model(), int(identity["principal"]["user_id"])), (Ledger, ledger["id"])):
            ActualPairedTerminalSettlementTests.sequence(self, model, target)
        fixture = make_p2_org(identity["domain"])
        grant = grant_topic_batch(organization=fixture.organization, user=fixture.owner,
            client_request_id=identity["dispatch_key"], requested_topic_count=request["requested_topic_count"])
        stamp = entitled_authorization(domain=fixture.organization.domain, action="topic_discovery", ledger=grant)
        self.assertTrue(all(request[key] == item for key, item in stamp.items()))
        raw = {key: item for key, item in request.items() if key not in {WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY, BUDGET_KEY}}
        run = reserve_admitted_run(organization=fixture.organization,
            principal=user_principal(fixture.owner.pk, identity["principal"]["actor_id"]), workflow="auto_discovery", payload=raw)
        run = bind_admitted_run(run, value["run_id"])
        self.assertEqual(run.run_request, request)
        self.assertEqual({"id": grant.pk, "kind": grant.kind, "user_id": grant.user_id, "source": grant.source,
            "idempotency_key": grant.idempotency_key, "delta_microroo": grant.delta_microroo}, ledger)
        create_job_facts(job_id=run.run_id, domain=run.domain, slack_user_id=run.slack_user_id,
            status="queued", client_request_id=identity["dispatch_key"], request_meta=deepcopy(run.run_request),
            billing_status="entitled", billing_ledger_id=grant.pk, billing_amount=0)
        self.assertTrue(value["dispatch_replay"]["idempotent"])
        self.assertEqual(value["dispatch_replay"]["run_id"], value["admission_response"]["run_id"])
        self.assertEqual(value["callbacks"], [])
        from .run_event_authority import receive_authoritative_run_events
        for batch in value["history_before_cancel"]["batches"]:
            reply = receive_authoritative_run_events(batch)
            self.assertEqual(reply.status_code, 200, reply.data)
        run.refresh_from_db()
        return value, run, grant

    def test_actual_free_first_http_worker_choices_survive_cancel_without_callback_or_grant_release(self):
        from .run_topic_delivery import project_verified_topics, has_verified_topic_delivery
        from .run_event_authority import receive_authoritative_run_events
        from .run_terminal_settlement import settle_authoritative_terminal
        from .run_dispatch_settlement import _free_obligation_state
        from .models import ContentFactoryJob
        value, run, grant = self.restore_free_http_pair("delivery")
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        with patch("requests.get", side_effect=[self.response(value["delivery_evidence"]),
                self.response(value["canonical_topic_response"])]) as get:
            self.assertEqual(project_verified_topics(run.run_id)["verified"], ["topic_selection_v1"])
        self.assertEqual(get.call_count, 2)
        self.assertEqual(ContentFactoryJob.objects.get(job_id=run.run_id).selection_data,
            value["canonical_topic_response"]["topic_selection_v1"]["selection"])
        for batch in value["history"]["batches"]:
            reply = receive_authoritative_run_events(batch)
            self.assertEqual(reply.status_code, 200, reply.data)
        run.refresh_from_db()
        self.assertTrue(has_verified_topic_delivery(run))
        with patch("requests.post") as close:
            self.assertEqual(settle_authoritative_terminal(run.run_id)["state"], "kept")
        close.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.run_request, before["run_request"])
        self.assertEqual(run.created_at, before["created_at"])
        self.assertEqual(_free_obligation_state(run), "reserved")
        self.assertFalse(Ledger.objects.filter(kind__in=["SPEND", "REFUND"]).exists())

    def test_actual_free_first_http_worker_empty_failure_releases_only_unchanged_original_grant(self):
        from .run_terminal_settlement import settle_authoritative_terminal, terminal_execution_closed
        from .run_dispatch_settlement import _free_obligation_state
        value, run, grant = self.restore_free_http_pair("settlement")
        before = ContentFactoryRun.objects.values().get(pk=run.pk)
        with patch("requests.post", return_value=self.response(value["cancel_response"])) as close, \
                patch("requests.get", return_value=self.response(value["history"])):
            result = settle_authoritative_terminal(run.run_id)
            self.assertEqual(result["state"], "released", result)
            self.assertEqual(settle_authoritative_terminal(run.run_id), result)
        self.assertEqual(close.call_count, 1)
        self.assertEqual(close.call_args.kwargs["json"], value["cancel_request"])
        self.assertEqual(value["cancel_replay"]["terminal_topic_settlement_v1"], value["cancel_response"]["terminal_topic_settlement_v1"])
        run.refresh_from_db()
        self.assertTrue(terminal_execution_closed(run))
        self.assertEqual(_free_obligation_state(run), "released")
        self.assertEqual(run.run_request, before["run_request"])
        self.assertEqual(run.created_at, before["created_at"])
        self.assertFalse(Ledger.objects.filter(kind__in=["SPEND", "REFUND"]).exists())

    def response(self, value):
        import requests
        response = requests.Response(); response.status_code = 200
        response._content = json.dumps(value).encode(); response._content_consumed = True
        return response

    def paired(self, kind):
        from .run_event_authority import receive_authoritative_run_events
        value = json.loads((Path(__file__).parent / "testdata/r3" / ("topic_actual_paid_island_" + kind + ".json")).read_text())
        run, spend = self.admit_paid_brief(value["original_backend_admission"])
        for body in value["history_before_cancel"]["batches"]:
            response = receive_authoritative_run_events(body)
            self.assertEqual(response.status_code, 200, response.data)
        run.refresh_from_db()
        return value, run, spend

    def test_actual_paired_island_proposals_remain_measured_and_earned_after_cancellation(self):
        from .run_topic_delivery import project_verified_topics, has_verified_topic_delivery
        from .island_research import proposal_for_adoption
        from .run_terminal_settlement import settle_authoritative_terminal
        value, run, spend = self.paired("delivery")
        with patch("requests.get", side_effect=[self.response(value["delivery_evidence"]),
                self.response(value["canonical_topic_response"])]) as get:
            self.assertEqual(project_verified_topics(run.run_id)["verified"], ["island_proposals_v1"])
            self.assertEqual(project_verified_topics(run.run_id)["verified"], ["island_proposals_v1"])
        self.assertEqual(get.call_count, 2)
        run.refresh_from_db()
        self.assertTrue(has_verified_topic_delivery(run))
        expected = value["canonical_topic_response"]["island_proposals_v1"]["suggested_islands"]
        self.assertEqual(run.result["suggested_islands"], expected)
        self.assertEqual(proposal_for_adoption(run, expected[0]["id"]), expected[0])
        with patch("requests.post") as close:
            self.assertEqual(settle_authoritative_terminal(run.run_id)["state"], "kept")
        close.assert_not_called()
        self.assertFalse(spend.service_refunds.exists())

    def test_actual_paired_empty_island_failure_refunds_exact_original_one_point(self):
        from .run_terminal_settlement import settle_authoritative_terminal
        value, run, spend = self.paired("settlement")
        with patch("requests.post", return_value=self.response(value["cancel_response"])) as close, \
                patch("requests.get", return_value=self.response(value["history"])):
            result = settle_authoritative_terminal(run.run_id)
            self.assertEqual(result["state"], "refunded", result)
            self.assertEqual(settle_authoritative_terminal(run.run_id), result)
        self.assertEqual(close.call_args.kwargs["json"], value["cancel_request"])
        self.assertEqual(close.call_count, 1)
        self.assertEqual(spend.service_refunds.get().delta_microroo, 1_000_000)
