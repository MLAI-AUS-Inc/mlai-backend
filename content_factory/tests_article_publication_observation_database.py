"""Real registration/claim/commit transactions in disposable PostgreSQL CI only."""
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import patch

from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory

from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun
from . import article_publication_observation as observer
from .article_publication_evidence import cancellation_result, refund_reconciliation_reason
from .models import OrganizationContentConfig
from .website_models import WebsiteConnection, WebsiteConnectionOperation, WebsiteRepositoryMutation
from .website_views import WebsiteMutationView


@override_settings(ROO_API_KEY="synthetic-test-key", INTERNAL_API_KEY="synthetic-test-key",
    ARTICLES_WEBSITE_EVENT_WAKES_ENABLED=False)
class PublicationObservationDatabaseTests(TransactionTestCase):
    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("Requires disposable PostgreSQL CI")
        self.fixture = json.loads((Path(__file__).parent / "test_fixtures/article-publication-observation-v1.json").read_text())
        intent = self.fixture["intent"]; scope = intent["scope"]
        self.org = Organization.objects.create(domain="fixture.test", name="Synthetic publication observation")
        self.website = WebsiteConnection.objects.create(id=scope["website_connection_id"], organization=self.org,
            generation=scope["connection_generation"], repository_id=scope["repository_id"], github_repo=scope["github_repo"],
            installation_id="45", branch="main", verified_sha=scope["expected_source_sha"],
            capabilities={"inventoryReady": True})
        self.website.refresh_from_db()
        OrganizationContentConfig.objects.create(organization=self.org, website_connection=self.website,
            github_repo=self.website.github_repo)
        self.original = WebsiteConnectionOperation.objects.create(id=scope["operation_id"], connection=self.website,
            generation=2, action="workflow", state="running", idempotency_key="original-publication",
            payload={"attempt": 1, "run_id": intent["run_id"], "workflow": "direct_generate", "deletion_epoch": 0})
        self.run = ContentFactoryRun.objects.create(run_id=intent["run_id"], organization=self.org, domain="fixture.test",
            github_repo="fixture/site", workflow="direct_generate", status="running",
            run_request=deepcopy(self.fixture["original_request"]), result={"generation": 0})

    def register(self):
        request = APIRequestFactory().post("/ledger", deepcopy(self.fixture["mutation"]), format="json",
            HTTP_X_API_KEY="synthetic-test-key")
        response = WebsiteMutationView.as_view()(request)
        self.assertIn(response.status_code, (200, 201), response.data)
        self.assertEqual(response.data["article_publication_intent_id"], self.fixture["intent"]["intent_id"])
        return WebsiteConnectionOperation.objects.get(action=observer.ACTION)

    def stop(self):
        self.run.result = cancellation_result(self.run, self.fixture["cancelled"])
        self.run.status = "cancelled"
        self.run.save(update_fields=["result", "status", "updated_at"])
        self.original.state = "cancelled"
        self.original.save(update_fields=["state", "updated_at"])

    def due(self, row):
        row.next_attempt_at = timezone.now()
        row.save(update_fields=["next_attempt_at", "updated_at"])

    def test_actual_producer_registration_replay_and_positive_commit_preserve_stop_and_charge(self):
        row = self.register()
        self.register()
        self.assertEqual(WebsiteConnectionOperation.objects.filter(action=observer.ACTION).count(), 1)
        self.stop(); self.due(row)
        original_stop = deepcopy(self.run.result["article_cancellation_v1"])
        def read(*_):
            self.assertFalse(connection.in_atomic_block)
            return deepcopy(self.fixture["observation"])
        def send(*_):
            self.assertFalse(connection.in_atomic_block)
            return deepcopy(self.fixture["acknowledged"])
        with patch.object(observer, "_observation", side_effect=read), patch.object(observer, "_send_observation", side_effect=send):
            self.assertEqual(observer.advance(row.pk), "completed")
            self.assertIsNone(observer.advance(row.pk))
        self.run.refresh_from_db(); row.refresh_from_db()
        self.assertEqual(row.attempts, 1)
        self.assertEqual(self.run.result["article_cancellation_v1"], original_stop)
        self.assertEqual(self.run.result["publication_evidence_v1"]["state"], "observed")
        self.assertEqual(refund_reconciliation_reason(self.run), "needs_reconciliation")
        self.assertEqual(self.run.run_request, self.fixture["original_request"])

    def test_original_operation_attempt_changes_during_read_cannot_apply_or_commit(self):
        row = self.register(); self.stop(); self.due(row)
        def rebind(*_):
            self.assertFalse(connection.in_atomic_block)
            self.original.payload = {**self.original.payload, "attempt": 2}
            self.original.save(update_fields=["payload", "updated_at"])
            return deepcopy(self.fixture["observation"])
        with patch.object(observer, "_observation", side_effect=rebind), patch.object(observer, "_send_observation") as send:
            self.assertEqual(observer.advance(row.pk), "pending")
        send.assert_not_called()
        self.run.refresh_from_db()
        self.assertEqual(self.run.result["publication_evidence_v1"]["state"], "pending")

    def test_active_writer_does_not_consume_provider_attempts_and_invalid_article_intent_rolls_back(self):
        row = self.register(); self.due(row)
        with patch.object(observer, "_observation") as read:
            self.assertEqual(observer.advance(row.pk), "pending")
        row.refresh_from_db(); read.assert_not_called()
        self.assertEqual(row.attempts, 0)
        body = deepcopy(self.fixture["mutation"])
        body["mutation_id"] = "f" * 64
        body["article_publication_intent_v1"]["scope"]["client_request_id"] = "foreign-payment"
        request = APIRequestFactory().post("/ledger", body, format="json", HTTP_X_API_KEY="synthetic-test-key")
        response = WebsiteMutationView.as_view()(request)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(WebsiteRepositoryMutation.objects.count(), 1)
        self.assertEqual(WebsiteConnectionOperation.objects.filter(action=observer.ACTION).count(), 1)

    def test_invalid_retained_original_operation_is_held_without_provider_work(self):
        row = self.register(); self.stop(); self.due(row)
        intent = deepcopy(row.payload["intent"])
        intent["scope"]["operation_id"] = "invalid-retained-uuid"
        intent["intent_id"] = observer.digest({key: value for key, value in intent.items() if key != "intent_id"})
        row.payload = {"intent": intent}
        row.save(update_fields=["payload", "updated_at"])
        with patch.object(observer, "_observation") as read:
            self.assertEqual(observer.advance(row.pk), "needs_attention")
        row.refresh_from_db(); read.assert_not_called()
        self.assertEqual(row.attempts, 0)

    def test_selected_website_change_during_provider_read_cannot_apply_old_observation(self):
        row = self.register(); self.stop(); self.due(row)
        def deselect(*_):
            self.assertFalse(connection.in_atomic_block)
            OrganizationContentConfig.objects.filter(organization=self.org).update(website_connection=None)
            return deepcopy(self.fixture["observation"])
        with patch.object(observer, "_observation", side_effect=deselect), patch.object(observer, "_send_observation") as send:
            self.assertEqual(observer.advance(row.pk), "pending")
        send.assert_not_called()
        self.run.refresh_from_db()
        self.assertEqual(self.run.result["publication_evidence_v1"]["state"], "pending")

    def test_new_observed_callback_during_private_acknowledgement_is_not_overwritten(self):
        row = self.register(); self.stop(); self.due(row)
        current = deepcopy(self.fixture["acknowledged"]["publication_evidence_v1"])
        current["head_sha"] = "d" * 40
        def callback(*_):
            self.assertFalse(connection.in_atomic_block)
            self.run.result["publication_evidence_v1"] = current
            self.run.save(update_fields=["result", "updated_at"])
            return deepcopy(self.fixture["acknowledged"])
        with patch.object(observer, "_observation", return_value=self.fixture["observation"]), \
                patch.object(observer, "_send_observation", side_effect=callback):
            self.assertEqual(observer.advance(row.pk), "pending")
        self.run.refresh_from_db()
        self.assertEqual(self.run.result["publication_evidence_v1"], current)
