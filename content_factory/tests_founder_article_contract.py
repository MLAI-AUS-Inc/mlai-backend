"""Real scoped journey/DTO projection, strict drift and historical boundaries."""
from copy import deepcopy
import json
import os
from pathlib import Path
from unittest.mock import patch

from django.http import Http404
from django.test import TestCase

from .founder_journey_contract import FounderContractMismatch, STEPS, validate_journey
from .tests_founder_journey_unit import JourneyFixture, make_run
from .founder_journey_testing import make_named_legacy_org
from . import founder_journey as journey


class FounderArticleContractTests(JourneyFixture, TestCase):
    def test_portable_first_article_never_requires_repository_capability(self):
        self.describe(self.one_liner, target_audience="Australian physiotherapy clinics")
        value = self.journey()
        self.assertEqual(value["contractVersion"], 2)
        self.assertEqual(value["deliveryMode"], "contentOnly")
        self.assertTrue(value["capabilities"]["canWriteArticle"])
        self.assertFalse(value["capabilities"]["canPublish"])
        self.assertIsNone(value["article"])
        self.assertEqual([step["id"] for step in value["steps"]], list(STEPS))
        self.assertEqual(value["steps"][4]["state"], "optional")
        self.assertEqual(validate_journey(value), value)

    def test_frozen_owner_does_not_receive_a_write_or_publish_capability(self):
        self.describe(self.one_liner, target_audience="Australian physiotherapy clinics")
        config = self.built.config
        config.pillar_strategy = {**config.pillar_strategy, "startup_team": {
            "version": 1, "owner_user_id": self.owner.pk, "frozen": True, "members": []}}
        config.save(update_fields=["pillar_strategy"])
        value = self.journey()
        self.assertFalse(value["capabilities"]["canWriteArticle"])
        self.assertFalse(value["capabilities"]["canPublish"])
        self.assertFalse(value["capabilities"]["canUpgradeWebsite"])

    def test_selected_run_cannot_be_replaced_by_the_latest_run(self):
        selected = make_run(self.org, status="running", step="research_topic")
        newer = make_run(self.org, status="running", step="assemble")
        value = self.journey(run_id=selected.run_id)
        self.assertEqual(value["runId"], selected.run_id)
        self.assertEqual(value["article"]["runId"], selected.run_id)
        self.assertEqual(value["article"]["currentStep"], "research_topic")
        self.assertNotEqual(value["article"]["runId"], newer.run_id)

    def test_missing_foreign_hidden_and_invalid_selected_runs_never_fall_back(self):
        other = make_named_legacy_org("la_vie")
        foreign = make_run(other.organization)
        hidden = make_run(self.org, status="cancelled", result={"cancellation_reason": "draft_deleted"})
        make_run(self.org)
        for identifier in ("missing-run", foreign.run_id, hidden.run_id, "../not-a-run", "a" * 101):
            with self.subTest(identifier=identifier), self.assertRaises(Http404):
                self.journey(run_id=identifier)

    def test_legacy_selector_returns_before_any_article_or_entitlement_read(self):
        old = make_named_legacy_org("la_vie")
        with patch("content_factory.first_article_entitlement.entitlement_state") as read:
            value = journey.project_founder_journey(old.organization, user=old.owner,
                company=old.company, run_id="missing-run")
        read.assert_not_called()
        self.assertEqual(value, journey.legacy_payload(old.company.pk))

    def test_failure_copy_never_uses_worker_text_and_preserves_execution(self):
        run = make_run(self.org, status="failed", result={"error_code": "UNREGISTERED_SYNTHETIC_CODE",
            "error": "sk-secret-synthetic do not render", "failure_class": "permanent"})
        before = type(run).objects.values().get(pk=run.pk)
        value = self.journey(run_id=run.run_id)
        self.assertEqual(value["failure"]["code"], "UNREGISTERED_FAILURE_CODE")
        self.assertNotIn("sk-secret", str(value))
        self.assertEqual(value["article"]["failure"], value["failure"])
        self.assertEqual(type(run).objects.values().get(pk=run.pk), before)

    def test_contract_rejects_aliases_wrong_types_unknown_fields_and_step_reordering(self):
        original = self.journey()
        variants = []
        value = deepcopy(original); value["company_id"] = value.pop("companyId"); variants.append(value)
        value = deepcopy(original); value["capabilities"]["canPublish"] = 1; variants.append(value)
        value = deepcopy(original); value["rawError"] = "synthetic-secret"; variants.append(value)
        value = deepcopy(original); value["steps"].reverse(); variants.append(value)
        value = deepcopy(original); value["contractVersion"] = 3; variants.append(value)
        for value in variants:
            with self.subTest(keys=sorted(value)), self.assertRaises(FounderContractMismatch) as error:
                validate_journey(value)
            self.assertNotIn("synthetic-secret", str(error.exception))

    def test_actual_selected_http_response_has_one_scoped_contract_without_writes(self):
        from rest_framework.test import APIRequestFactory, force_authenticate
        from community_chat.my_startup_journey_views import FounderJourneyView
        from workflow_runs.models import ContentFactoryRunStep
        run = make_run(self.org, status="running", step="research_topic")
        newer = make_run(self.org, status="running", step="assemble")
        step = ContentFactoryRunStep.objects.create(run=run, step_key="research_topic", status="running",
            message="Raw worker sentence must not become founder copy")
        before = type(run).objects.values().get(pk=run.pk)
        before_step = ContentFactoryRunStep.objects.values().get(pk=step.pk)
        request = APIRequestFactory().get("/api/v1/my-startup/journey/", {
            "company_id": str(self.company.pk), "runId": run.run_id})
        force_authenticate(request, user=self.owner)
        with patch("community_chat.throttles.StartupScopedThrottle.allow_request", return_value=True):
            response = FounderJourneyView.as_view()(request)
        self.assertEqual(response.status_code, 200, response.data)
        value = response.data
        self.assertEqual(value["article"]["runId"], run.run_id)
        self.assertNotEqual(value["article"]["runId"], newer.run_id)
        self.assertEqual(value["article"]["progress"][0]["id"], "research_topic")
        self.assertNotIn("Raw worker sentence", str(value))
        self.assertEqual(validate_journey(value), value)
        self.assertEqual(type(run).objects.values().get(pk=run.pk), before)
        self.assertEqual(ContentFactoryRunStep.objects.values().get(pk=step.pk), before_step)
        target = os.environ.get("FOUNDER_EXPORT_R5_JOURNEY")
        if target:
            Path(target).write_text(json.dumps({"schemaVersion": 1,
                "source": "actual authenticated in-process HTTP over synthetic real PostgreSQL Run/Step; no CF event or live provider claim",
                "request": {"companyId": str(self.company.pk), "runId": run.run_id},
                "payload": value}, indent=2) + "\n")
