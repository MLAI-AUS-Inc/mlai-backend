"""Owned command approval never rewrites the original execution request."""
from copy import deepcopy
from django.test import TransactionTestCase, override_settings
from workflow_runs.models import ContentFactoryRun
from organizations.models import Organization
from .article_publish_approval import article_review_identity, article_publish_approval_receipt_matches
from .run_ownership import (OWNER_EXECUTION_KEY, ARTICLE_PUBLISH_APPROVAL_RESULT_KEY,
    bind_admitted_run, reserve_admitted_run, user_principal)
from . import tests_run_current_review as current_review_tests
from .founder_journey_testing import ALL_P1_FLAGS_ON
from .vibe_marketing_views import (_require_article_publish_approval_receipt,
    _record_article_publish_approval_receipt, _article_publish_retry_authorized)


@override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=True, ARTICLES_RUN_FACT_AUTHORITY_ENABLED=True,
                   ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=True, **ALL_P1_FLAGS_ON,
                   ARTICLES_FREE_FIRST_ARTICLE_ENABLED=True, CONTENT_FACTORY_URL="https://cf.fixture.invalid",
                   CONTENT_FACTORY_API_KEY="synthetic-paired-approval-key")
class OwnedPublishApprovalTests(TransactionTestCase):
    reads = current_review_tests.CurrentReviewTests.reads

    def setUp(self):
        current_review_tests.CurrentReviewTests.setUp(self)
        from .run_verified_delivery import project_verified_delivery
        with self.reads():
            project_verified_delivery(self.run.run_id)
        self.run.refresh_from_db()

    def test_current_approval_has_separate_protected_receipt_and_unchanged_original_request_clocks(self):
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        identity = article_review_identity(self.run)
        self.assertTrue(_require_article_publish_approval_receipt(self.run, expected_identity=identity))
        self.assertTrue(_record_article_publish_approval_receipt(self.run, actor_id=f"mlai_user:{self.user.pk}", expected_identity=identity,
            accepted_control=self.fixture["approval_response"], user=self.user))
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
        self.assertTrue(_record_article_publish_approval_receipt(self.run, actor_id=f"mlai_user:{self.user.pk}", expected_identity=identity,
            accepted_control=self.fixture["approval_response"], user=self.user))
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
