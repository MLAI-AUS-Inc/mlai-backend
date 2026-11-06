"""Private confirmation charge proof is atomic and never replaces research input."""
from copy import deepcopy
from unittest.mock import patch

from django.db import transaction
from django.test import TestCase, override_settings

from roo.models import Ledger
from roo.services import PointsService
from workflow_runs.models import ContentFactoryRun
from integrations import tests_article as fixtures
from .run_article_charge import article_charge_receipt, record_article_charge, require_article_charge_choice
from .run_backend_facts import BackendFactConflict, create_job_facts, save_job_facts
from .run_ownership import ARTICLE_CHARGE_RESULT_KEY, bind_admitted_run, public_run_value, reserve_admitted_run, user_principal
from .website_connections import contract_for


@override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=True, ARTICLES_RUN_FACT_AUTHORITY_ENABLED=True,
                   ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=True)
class ArticleChargeReceiptTests(TestCase):
    _own_verified_website = fixtures.ArticleGenerationServiceTest._own_verified_website

    def setUp(self):
        fixtures.ArticleGenerationServiceTest.setUp(self)
        self.research_key, self.article_key = "owned-research-original", "owned-confirmed-article-original"
        self.research_ledger, _ = PointsService.spend(user=self.user, delta=3, source="CONTENT_FACTORY",
            description="Synthetic original research", created_by_slack_id=self.slack_user_id,
            idempotency_key="content_factory:topic_generation:charge:" + self.research_key,
            reference_type="CONTENT_FACTORY", reference_id=self.research_key)
        request = {**contract_for(self.config.website_connection), "domain": self.org.domain,
            "github_repo": self.repo_name, "client_request_id": self.research_key,
            "roo_points_action": "content_island_topic_generation", "roo_points_ledger_id": str(self.research_ledger.pk)}
        self.source = reserve_admitted_run(organization=self.org, principal=user_principal(self.user.pk, self.slack_user_id),
                                          workflow="auto_discovery", payload=request)
        self.source = bind_admitted_run(self.source, "owned-paid-research")
        self.job = create_job_facts(job_id=self.source.run_id, domain=self.org.domain, slack_user_id=self.slack_user_id,
            status="awaiting_confirmation", request_meta=request, client_request_id=self.research_key,
            billing_status="charged", billing_ledger_id=self.research_ledger.pk, billing_amount=3)

    def seal(self, *, project=True):
        ledger, _ = PointsService.spend(user=self.user, delta=12, source="CONTENT_FACTORY",
            description="Synthetic approved article charge", created_by_slack_id=self.slack_user_id,
            idempotency_key="content_factory:charge:" + self.article_key, reference_type="CONTENT_FACTORY",
            reference_id=self.article_key)
        self.job.request_meta = {**self.job.request_meta, "client_request_id": self.article_key,
                                 "roo_points_action": "article_generation", "roo_points_ledger_id": str(ledger.pk)}
        receipt = record_article_charge(self.job, ledger=ledger, cost_points=12, billing_user=self.user,
            requester=self.user, article_request_key=self.article_key, confirmed_keyword="Agentic article", custom_title=None)
        if project:
            self.job.billing_ledger, self.job.billing_amount = ledger, 12
            save_job_facts(self.job, update_fields=["request_meta", "billing_ledger", "billing_amount", "updated_at"])
        return receipt

    def test_sealed_actual_debit_and_projection_keep_initial_research_authority_and_clocks(self):
        before = ContentFactoryRun.objects.values().get(pk=self.source.pk)
        with transaction.atomic():
            receipt = self.seal()
        self.source.refresh_from_db()
        self.assertEqual(article_charge_receipt(self.source), receipt)
        self.assertEqual(self.source.run_request, before["run_request"])
        self.assertEqual((self.source.created_at, self.source.updated_at), (before["created_at"], before["updated_at"]))
        self.assertEqual(self.source.billing_ledger_id, receipt["ledger_id"])
        self.assertEqual(self.source.client_request_id, self.research_key)
        self.assertEqual(Ledger.objects.filter(kind="SPEND").count(), 2)
        self.assertNotIn(ARTICLE_CHARGE_RESULT_KEY, public_run_value(self.source.result))

    def test_projection_failure_rolls_receipt_and_actual_debit_back_together(self):
        before = ContentFactoryRun.objects.values().get(pk=self.source.pk)
        with self.assertRaises(BackendFactConflict), transaction.atomic(), \
                patch("content_factory.run_backend_facts._project", side_effect=BackendFactConflict("synthetic projection failure")):
            self.seal()
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.source.pk), before)
        self.assertEqual(Ledger.objects.filter(kind="SPEND").count(), 1)
        self.user.points_account.refresh_from_db()
        self.assertEqual(self.user.points_account.balance, 17)

    def test_research_or_unrelated_amount_ledger_cannot_prove_the_article_charge(self):
        before = ContentFactoryRun.objects.values().get(pk=self.source.pk)
        with self.assertRaises(BackendFactConflict), transaction.atomic():
            record_article_charge(self.job, ledger=self.research_ledger, cost_points=12, billing_user=self.user,
                requester=self.user, article_request_key=self.article_key, confirmed_keyword="Agentic article", custom_title=None)
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.source.pk), before)

    def test_same_paid_choice_and_key_replay_but_changed_confirmation_refuses(self):
        with transaction.atomic():
            receipt = self.seal()
        self.source.refresh_from_db()
        self.assertEqual(require_article_charge_choice(self.job, keyword="Agentic article", custom_title=None,
                                                     request_key=self.article_key), receipt)
        for changes in ({"keyword": "Other"}, {"custom_title": "Other"}, {"request_key": "another-key"}):
            args = {"keyword": "Agentic article", "custom_title": None, "request_key": self.article_key, **changes}
            with self.subTest(changes=changes), self.assertRaises(BackendFactConflict):
                require_article_charge_choice(self.job, **args)
        corrupt = deepcopy(receipt)
        corrupt["ledger_id"] = self.research_ledger.pk
        ContentFactoryRun.objects.filter(pk=self.source.pk).update(result={**self.source.result, ARTICLE_CHARGE_RESULT_KEY: corrupt})
        self.source.refresh_from_db()
        with self.assertRaises(BackendFactConflict):
            article_charge_receipt(self.source)
        from .website_contract import evidence_digest
        corrupt = deepcopy(receipt)
        corrupt["cost_points"], corrupt["request"]["cost_points"] = 1, True
        corrupt["request_digest"] = evidence_digest(corrupt["request"])
        ContentFactoryRun.objects.filter(pk=self.source.pk).update(result={**self.source.result, ARTICLE_CHARGE_RESULT_KEY: corrupt})
        self.source.refresh_from_db()
        with self.assertRaises(BackendFactConflict):
            article_charge_receipt(self.source)
