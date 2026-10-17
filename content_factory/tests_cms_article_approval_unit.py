"""Exact native consent survives proof refresh without another owner action."""

from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.test import SimpleTestCase, override_settings

from . import cms_article_approval as approvals
from . import cms_publication_policy as policies
from . import cms_owned_media as media
from . import cms_staged_review as stages
from . import cms_artifact_review as reviews
from . import tests_cms_staged_review_unit as fixtures
from . import tests_channel_artifacts_unit as artifact_fixtures
from .cms_api_transport import CmsApiError
from .channel_artifact_contract import digest
from .website_connections import contract_for
from .website_contract import WebsiteAuthorityError
from .website_models import WebsiteConnectionOperation


def reserve(test):
    test.connection.save = Mock()
    def relation(**filters):
        if filters.get("action") == "channel-credential": return SimpleNamespace(first=lambda: test.private)
        return test.manager.filter(**filters)
    test.rows.filter.side_effect = relation
    test.rows.exclude.return_value.only.return_value.order_by.return_value.first.return_value = None
    with patch("content_factory.website_connections.require_unlocked_remote_call"), patch.object(policies, "request_json", side_effect=[
            ({"id": 7, "name": "Existing author", "capabilities": {"edit_posts": True, "publish_posts": True}}, {}),
            ([{"id": 39, "taxonomy": "category", "name": "Existing category"}], {})]):
        test.policy = policies.configure(test.context, test.config, user=test.user, data={**contract_for(test.connection),
            "client_request_id": str(uuid4()), "review_policy": test.mode, "publication_confirmed": True,
            "native_defaults_confirmed": True, "expected_policy_id": None})
    return artifact_fixtures.ChannelArtifactTests.reserve(test)


def prepare(test, mode):
    test.mode = mode
    fixtures.CmsStagedReviewTests.setUp(test)
    test.stage = stages.start_stage(test.media)
    test.data = {**contract_for(test.connection), "stage_id": str(test.stage.pk),
        "expected_revision": test.stage.payload["staged"]["revision"], "publication_confirmed": True}
    test.fetch.reset_mock()


@override_settings(ARTICLES_CHANNEL_DISCOVERY_ENABLED=True, ARTICLES_PUBLISHING_CHANNELS_ENABLED=True,
    ARTICLES_CMS_CHANNEL_ENABLED=True, ARTICLES_CHANNEL_ARTIFACTS_ENABLED=True,
    CONNECTOR_CREDENTIAL_KEYS=json.dumps({"test": fixtures.TEST_KEY}), CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID="test")
class CmsArticleApprovalTests(SimpleTestCase):
    reserve = reserve
    set_handoff = fixtures.CmsStagedReviewTests.set_handoff
    acknowledge = fixtures.CmsStagedReviewTests.acknowledge

    def setUp(self): prepare(self, "review")

    def choose(self, **changes):
        return approvals.approve(self.context, self.config, user=self.user, data={**self.data, **changes})

    def approval_row(self): return next(row for row in self.saved if row.action == approvals.ACTION)

    def advance(self, **kwargs): return approvals.advance_approval(self.approval_row().pk, now=kwargs.get("now", self.now))

    def age_media(self):
        for state in self.media.payload["states"]:
            old = state["evidence"]
            old["checked_at"] = (self.now - timedelta(hours=1)).isoformat()
            old["digest"] = digest({key: item for key, item in old.items() if key != "digest"})

    def test_owner_choice_is_pending_until_fresh_cf_media_and_original_payment_are_rechecked(self):
        choice = self.choose()
        self.assertEqual((choice["status"], choice["approvalRecorded"]), ("pending", False))
        self.assertEqual(self.choose(), choice)
        self.fetch.assert_not_called()
        operation = self.advance()
        source, value, policy, receipt = approvals.current_approval(operation)
        self.assertEqual((operation.state, receipt["basis"]), ("completed", "owner_final_review"))
        self.assertEqual(receipt["staged_revision"], choice["revision"])
        self.assertEqual(receipt["html_sha256"], value["article"]["profile"]["html_sha256"])
        self.assertEqual(receipt["billing_digest"], source.billing["digest"])
        self.assertEqual(receipt["native_fields_sha256"], digest(policies.native_post_fields(policy["policy"])))
        self.assertIs(receipt["provider_modified"], False)
        self.assertIs(receipt["publishing_verified"], False)
        self.assertTrue(self.choose()["approvalRecorded"])
        self.assertEqual(self.fetch.call_count, 1)
        self.assertEqual(self.provider_post.call_count, 1)
        self.assertEqual(self.connection.verified_sha, "")

    def test_expired_media_refresh_resumes_original_choice_without_reupload_or_new_revision(self):
        self.age_media()
        self.choose(); operation = self.advance()
        self.assertEqual((operation.state, operation.receipt["status"]), ("pending", "approval_waiting_media"))
        self.assertEqual(self.media.payload["states"][0]["phase"], "public_recheck")
        self.fetch.assert_not_called()
        media.advance_media(self.media.pk, now=self.now)
        self.advance(now=operation.next_attempt_at + timedelta(seconds=1))
        self.assertEqual(operation.state, "completed")
        self.assertEqual(operation.receipt["staged_revision"], self.data["expected_revision"])
        self.assertEqual(self.provider_post.call_count, 1)

    def test_transient_public_recheck_keeps_historical_review_and_owner_choice_then_recovers(self):
        self.age_media(); self.choose(); operation = self.advance()
        with patch.object(media, "observe_owned_image", side_effect=CmsApiError("transient")):
            media.advance_media(self.media.pk, now=self.now)
        self.assertEqual(self.media.state, "pending")
        self.advance(now=operation.next_attempt_at + timedelta(seconds=1))
        self.assertEqual(operation.state, "pending")
        media.advance_media(self.media.pk, now=self.media.next_attempt_at + timedelta(seconds=1))
        self.advance(now=operation.next_attempt_at + timedelta(seconds=1))
        self.assertEqual(operation.state, "completed")
        self.assertEqual(self.provider_post.call_count, 1)

    def test_manual_action_requires_exact_revision_real_boolean_and_current_selected_owner(self):
        for changes in ({"expected_revision": "f" * 64}, {"publication_confirmed": 1}, {"connection_generation": 2}, {"stage_id": str(uuid4())}):
            with self.subTest(changes=changes), self.assertRaises(WebsiteAuthorityError): self.choose(**changes)
        self.profile.role = "investor"
        with patch.object(stages, "current_stage") as private, self.assertRaises(WebsiteAuthorityError): self.choose()
        private.assert_not_called(); self.fetch.assert_not_called()
        self.assertFalse(any(row.action == approvals.ACTION for row in self.saved))

    def test_foreign_preview_is_scoped_before_private_read_and_route_is_no_store(self):
        from . import website_views
        request = SimpleNamespace(user=self.user, data=self.data)
        with patch.object(website_views, "_context", return_value=(self.context, self.config, None)):
            response = website_views.WebsiteConnectionActionView().post(request, "cms-review-article")
            self.assertEqual(response.data["cmsArticleReview"]["revision"], self.data["expected_revision"])
            self.assertEqual(response["Cache-Control"], "private, no-store")
            response = website_views.WebsiteConnectionActionView().post(request, "cms-approve-article")
        self.assertEqual(response.status_code, 202)
        for private in ("api_origin", "credential_operation_id", "synthetic-application-password", "payer_user_id"):
            self.assertNotIn(private, json.dumps(response.data))
        foreign = deepcopy(self.stage); foreign.connection_id = uuid4()
        self.saved.remove(self.stage); self.saved.append(foreign)
        with self.assertRaises(WebsiteAuthorityError): self.choose()

    def test_changed_cf_bytes_or_refund_committing_during_http_cannot_record_approval(self):
        self.choose()
        self.fetch.side_effect = lambda *a, **k: {**deepcopy(self.value), "review_revision": "f" * 64}
        self.assertEqual(self.advance().state, "cancelled")
        self.saved.remove(self.approval_row()); self.choose()
        def refund(*args, **kwargs):
            self.refund.return_value.exists.return_value = True
            return deepcopy(self.value)
        self.fetch.side_effect = refund
        operation = self.advance()
        self.assertEqual(operation.state, "cancelled")
        self.assertIs(operation.receipt["approval_recorded"], False)

    def test_late_cf_reply_does_not_overwrite_cancellation_or_newer_lease(self):
        for outcome in ("cancelled", "running"):
            self.choose(); operation = self.approval_row()
            def replace(*args, **kwargs):
                operation.state = outcome
                operation.receipt = {"status": "newer_owner_or_worker"}
                operation.payload["lease_id"] = "newer-lease"
                return deepcopy(self.value)
            self.fetch.side_effect = replace
            self.advance()
            self.assertEqual(operation.receipt, {"status": "newer_owner_or_worker"})
            self.saved.remove(operation)

    def test_crashed_worker_waits_for_lease_then_recovers_without_second_owner_choice(self):
        self.choose(); operation = self.approval_row()
        self.fetch.side_effect = RuntimeError("synthetic worker crash")
        with self.assertRaises(RuntimeError): self.advance()
        self.assertEqual(operation.state, "running")
        self.fetch.reset_mock(); self.fetch.side_effect = None
        self.advance(); self.fetch.assert_not_called()
        self.advance(now=operation.next_attempt_at + timedelta(seconds=1))
        self.assertEqual(operation.state, "completed")

    def test_transient_cf_reads_are_bounded_and_copied_receipts_or_boolean_versions_are_not_authority(self):
        self.choose(); operation = self.approval_row()
        self.fetch.side_effect = reviews.ArtifactReadError("unavailable")
        for _ in range(5): self.advance(now=(operation.next_attempt_at or self.now) + timedelta(seconds=1))
        self.assertEqual(operation.state, "blocked")
        self.assertEqual(operation.receipt["failure_class"], "transient")
        self.saved.remove(operation); self.choose(); self.fetch.side_effect = None
        operation = self.advance()
        original = deepcopy(operation.receipt)
        operation.receipt["html_sha256"] = "f" * 64
        operation.receipt["digest"] = digest({k: v for k, v in operation.receipt.items() if k != "digest"})
        with self.assertRaises(WebsiteAuthorityError): approvals.current_approval(operation)
        operation.receipt = original; operation.payload["schema_version"] = True
        with self.assertRaises(WebsiteAuthorityError): approvals.current_approval(operation)

    def test_completed_choice_keeps_consent_but_publication_rechecks_media_payment_and_policy(self):
        self.choose(); operation = self.advance(); self.age_media()
        with self.assertRaises(CmsApiError): approvals.current_approval(operation)
        self.assertEqual(approvals.current_approval(operation, fresh_media=False)[3]["basis"], "owner_final_review")
        self.refund.return_value.exists.return_value = True
        with self.assertRaises(WebsiteAuthorityError): approvals.current_approval(operation, fresh_media=False)
        self.refund.return_value.exists.return_value = False
        self.connection.capabilities[policies.REFERENCE] = str(uuid4())
        with self.assertRaises(WebsiteAuthorityError): approvals.current_approval(operation, fresh_media=False)

    def test_disabled_approval_does_not_read_private_rows_or_remote_evidence(self):
        with override_settings(ARTICLES_CHANNEL_ARTIFACTS_ENABLED=False), patch.object(WebsiteConnectionOperation.objects, "filter") as rows:
            self.assertIsNone(approvals.advance_approval(self.stage.pk))
            self.assertEqual(approvals.ensure_approvals(), 0)
            with self.assertRaises(WebsiteAuthorityError): self.choose()
        rows.assert_not_called(); self.fetch.assert_not_called()


@override_settings(ARTICLES_CHANNEL_DISCOVERY_ENABLED=True, ARTICLES_PUBLISHING_CHANNELS_ENABLED=True,
    ARTICLES_CMS_CHANNEL_ENABLED=True, ARTICLES_CHANNEL_ARTIFACTS_ENABLED=True,
    CONNECTOR_CREDENTIAL_KEYS=json.dumps({"test": fixtures.TEST_KEY}), CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID="test")
class CmsAutomaticApprovalTests(SimpleTestCase):
    reserve = reserve
    set_handoff = fixtures.CmsStagedReviewTests.set_handoff
    acknowledge = fixtures.CmsStagedReviewTests.acknowledge

    def setUp(self): prepare(self, "auto")

    def test_only_original_private_auto_policy_can_approve_without_a_final_owner_action(self):
        operation = approvals._start(self.stage, basis="owner_setup_auto", expected_revision=self.data["expected_revision"])
        approvals.advance_approval(operation.pk, now=self.now)
        self.assertEqual(operation.state, "completed")
        self.assertEqual(approvals.current_approval(operation)[3]["basis"], "owner_setup_auto")
        self.assertEqual(self.value["envelope"]["review_policy"], "auto")

    def test_auto_policy_cannot_be_relabelled_as_a_manual_click_or_added_after_generation(self):
        with self.assertRaises(WebsiteAuthorityError): approvals.approve(self.context, self.config, user=self.user, data=self.data)
        self.parent.payload["policy"].pop("cms_publication_policy")
        with self.assertRaises(WebsiteAuthorityError):
            approvals._start(self.stage, basis="owner_setup_auto", expected_revision=self.data["expected_revision"])
