"""Actual paid research confirmation must preserve two distinct identities."""
from copy import deepcopy
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings

from integrations import tests_article as fixture
from integrations.services.article_generation import confirm_topic, _ledger_reference_id
from roo.models import Ledger
from roo.services import PointsService
from workflow_runs.models import ContentFactoryRun

from .models import ContentFactoryJob
from .run_backend_facts import create_job_facts
from .run_ownership import WIRE_REQUEST_KEY, bind_admitted_run, owner_receipt, reserve_admitted_run, user_principal
from .website_connections import contract_for


@override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=True, ARTICLES_RUN_FACT_AUTHORITY_ENABLED=True,
                   ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=True)
class ConfirmedArticleFactTests(TestCase):
    _own_verified_website = fixture.ArticleGenerationServiceTest._own_verified_website

    def setUp(self):
        fixture.ArticleGenerationServiceTest.setUp(self)
        self.research_key = "owned-research-original"
        self.article_key = "owned-confirmed-article-original"
        self.research_ledger, _ = PointsService.spend(user=self.user, delta=3, source="CONTENT_FACTORY",
            description="Synthetic original research", created_by_slack_id=self.slack_user_id,
            idempotency_key="content_factory:topic_generation:charge:" + self.research_key,
            reference_type="CONTENT_FACTORY", reference_id=_ledger_reference_id(self.research_key))
        request = {**contract_for(self.config.website_connection), "expected_source_sha": self.config.website_connection.verified_sha,
            "domain": self.org.domain, "github_repo": self.repo_name, "client_request_id": self.research_key,
            "request_source": "roo_slackbot", "roo_points_action": "content_island_topic_generation",
            "roo_points_ledger_id": str(self.research_ledger.pk), "roo_points_cost": 3,
            "roo_points_billing_status": "charged", "roo_points_billing_user_id": self.user.pk}
        self.source = reserve_admitted_run(organization=self.org, principal=user_principal(self.user.pk, self.slack_user_id),
                                          workflow="auto_discovery", payload=request)
        self.source = bind_admitted_run(self.source, "owned-paid-research")
        self.job = create_job_facts(job_id=self.source.run_id, domain=self.org.domain, slack_user_id=self.slack_user_id,
            status="awaiting_confirmation", request_meta=request, client_request_id=self.research_key,
            billing_status="charged", billing_ledger_id=self.research_ledger.pk, billing_amount=3)

    def tearDown(self):
        import os
        from pathlib import Path
        import json
        if os.environ.get("R3_CAPTURE_CHARGE_FIXTURE") == "1":
            self.source.refresh_from_db()
            receipt = self.source.result.get("run_owner_article_charge_v1")
            if receipt:
                fixture = {"schema_version": 1, "synthetic": True, "producer": "actual PostgreSQL paid-confirm route",
                    "source_run_request": self.source.run_request, "charge_receipt": receipt,
                    "canonical_job_meta": self.source.job_meta,
                    "ledger": Ledger.objects.filter(pk=receipt["ledger_id"]).values(
                        "id", "user_id", "kind", "source", "idempotency_key", "reference_type", "reference_id", "delta_microroo").first()}
                Path("/Users/samdonegan/Documents/Code/founder-journey-worktrees/recovery/R3_ARTICLE_CONFIRMATION_CHARGE.fixture.json").write_text(json.dumps(fixture, indent=2) + "\n")

    def test_actual_confirm_uses_new_article_admission_and_one_charge_with_immutable_research_request(self):
        before = ContentFactoryRun.objects.values().get(pk=self.source.pk)
        sent = []

        def post(url, *, json, **kwargs):
            child = ContentFactoryRun.objects.get(result__run_owner_v1__identity__dispatch_key=self.article_key)
            self.assertNotEqual(child.pk, self.source.pk)
            self.assertEqual(json[WIRE_REQUEST_KEY], child.run_request[WIRE_REQUEST_KEY])
            self.assertEqual(child.run_request["source_run_id"], self.source.run_id)
            self.assertEqual(child.run_request["client_request_id"], self.article_key)
            sent.append(deepcopy(json))
            response = Mock(status_code=202, content=b"{}")
            response.json.return_value = {"run_id": "owned-confirmed-worker", "status": "queued"}
            return response

        args = {"domain": self.org.domain, "confirmed_keyword": "Agentic article", "slack_user_id": self.slack_user_id,
                "source_run_id": self.source.run_id, "client_request_id": self.article_key, "billing_user": self.user}
        with patch("integrations.services.article_generation.http_requests.post", side_effect=post):
            first = confirm_topic(**args, expected_cost_points=12)
            replay = confirm_topic(**args)
        self.assertEqual(first["job_id"], replay["job_id"])
        self.assertEqual(len(sent), 1)
        self.assertEqual(ContentFactoryRun.objects.count(), 2)
        self.source.refresh_from_db()
        self.job.refresh_from_db()
        self.assertEqual(self.source.run_request, before["run_request"])
        self.assertEqual((self.source.created_at, self.source.updated_at), (before["created_at"], before["updated_at"]))
        self.assertEqual(owner_receipt(self.source)["identity"]["dispatch_key"], self.research_key)
        self.assertEqual(self.job.client_request_id, self.research_key)
        child = ContentFactoryJob.objects.get(job_id="owned-confirmed-worker")
        self.assertEqual(child.client_request_id, self.article_key)
        self.assertEqual(child.billing_ledger_id, self.job.billing_ledger_id)
        self.assertNotEqual(child.billing_ledger_id, self.research_ledger.pk)
        self.assertEqual(Ledger.objects.filter(kind="SPEND").count(), 2)
        self.assertEqual(Ledger.objects.filter(kind="REFUND").count(), 0)
        self.user.points_account.refresh_from_db()
        self.assertEqual(self.user.points_account.balance, 5)

    def test_real_refund_of_under_another_key_never_allows_article_payment_reuse(self):
        from integrations.services.article_generation import ArticleGenerationError, _require_article_job_payment
        response = Mock(status_code=202, content=b"{}")
        response.json.return_value = {"run_id": "owned-confirmed-worker", "status": "queued"}
        with patch("integrations.services.article_generation.http_requests.post", return_value=response):
            confirm_topic(domain=self.org.domain, confirmed_keyword="Agentic article", slack_user_id=self.slack_user_id,
                source_run_id=self.source.run_id, client_request_id=self.article_key, billing_user=self.user,
                expected_cost_points=12)
        child = ContentFactoryJob.objects.get(job_id="owned-confirmed-worker")
        charge = Ledger.objects.get(pk=child.billing_ledger_id)
        PointsService.refund(user=self.user, delta=12, source="CONTENT_FACTORY", description="Synthetic legitimate prior refund",
            created_by_slack_id=self.slack_user_id, idempotency_key="synthetic-legacy-refund-different-key",
            original_spend_key=charge.idempotency_key, reference_type="CONTENT_FACTORY",
            reference_id=_ledger_reference_id(self.article_key))
        self.assertTrue(charge.service_refunds.exists())
        self.assertEqual(child.billing_status, "reused")
        with self.assertRaises(ArticleGenerationError):
            _require_article_job_payment(child, user=self.user, domain=self.org.domain, expected_cost_points=12)
