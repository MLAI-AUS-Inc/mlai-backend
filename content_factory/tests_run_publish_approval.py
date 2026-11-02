"""Owned command approval never rewrites the original execution request."""
from copy import deepcopy
from django.test import TestCase, override_settings
from workflow_runs.models import ContentFactoryRun
from organizations.models import Organization
from .article_publish_approval import article_review_identity, article_publish_approval_receipt_matches
from .run_ownership import (OWNER_EXECUTION_KEY, ARTICLE_PUBLISH_APPROVAL_RESULT_KEY,
    bind_admitted_run, reserve_admitted_run, user_principal)
from .tests_article_publish_approval_unit import _review_run
from .vibe_marketing_views import (_require_article_publish_approval_receipt,
    _record_article_publish_approval_receipt, _article_publish_retry_authorized)


@override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=True, ARTICLES_RUN_FACT_AUTHORITY_ENABLED=True,
                   ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=True)
class OwnedPublishApprovalTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic consent", domain="consent.example.test")
        request = {"domain": self.org.domain, "client_request_id": "approval-original", "topic": "Synthetic article"}
        self.run = bind_admitted_run(reserve_admitted_run(organization=self.org,
            principal=user_principal(123, "user:123"), workflow="article_generation", payload=request), "approval-worker")
        ContentFactoryRun.objects.filter(pk=self.run.pk).update(result={**self.run.result, **_review_run().result},
            approval_state="approved")
        self.run.refresh_from_db()

    def test_current_approval_has_separate_protected_receipt_and_unchanged_original_request_clocks(self):
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        identity = article_review_identity(self.run)
        self.assertTrue(_require_article_publish_approval_receipt(self.run, expected_identity=identity))
        self.assertTrue(_record_article_publish_approval_receipt(self.run, actor_id="user:123", expected_identity=identity))
        self.run.refresh_from_db()
        self.assertTrue(article_publish_approval_receipt_matches(self.run))
        self.assertTrue(_article_publish_retry_authorized(self.run))
        after = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        for field in ("run_request", "created_at", "updated_at", "last_event_emitted_at", "reconciled_at"):
            self.assertEqual(after[field], before[field], field)
        from .run_ownership import public_run_value
        self.assertNotIn(ARTICLE_PUBLISH_APPROVAL_RESULT_KEY, public_run_value(self.run.result))

    def test_new_ticket_or_changed_review_cannot_reuse_old_command_consent(self):
        identity = article_review_identity(self.run)
        self.assertTrue(_record_article_publish_approval_receipt(self.run, actor_id="user:123", expected_identity=identity))
        original = deepcopy(self.run.result)
        self.run.result["article_preview_quality"]["inputs_sha256"] = "c" * 64
        self.assertFalse(article_publish_approval_receipt_matches(self.run))
        self.run.result = deepcopy(original)
        execution = self.run.result[OWNER_EXECUTION_KEY]
        old = execution["tickets"][execution["current_execution_id"]]
        execution["tickets"]["new-ticket"] = {**old, "execution_id": "new-ticket", "generation": 1}
        execution["current_execution_id"] = "new-ticket"
        self.assertFalse(article_publish_approval_receipt_matches(self.run))
        self.assertFalse(_article_publish_retry_authorized(self.run))
