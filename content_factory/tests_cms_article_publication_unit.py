"""Actual native publication control flow uses synthetic provider boundaries."""

from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.test import SimpleTestCase, override_settings

from . import cms_article_publication as publishing
from . import cms_article_approval as approvals
from . import cms_publication_policy as policies
from . import cms_artifact_review as reviews
from . import cms_owned_media as media
from . import tests_cms_article_approval_unit as fixtures
from . import tests_channel_artifacts_unit as artifact_fixtures
from .cms_api_transport import CmsApiError
from .cms_connection_contract import seal_credential
from .cms_media_transport import UploadedImage
from .cms_native_html import native_fragment, profile_receipt, preview_document
from .channel_artifact_contract import digest
from .tests_cms_post_contract_unit import response as native_response, collection
from .website_connections import contract_for
from .website_contract import WebsiteAuthorityError
from .website_models import WebsiteConnectionOperation
from .cms_goal_effects import CmsGoalWorkDenied

PROSE = " This synthetic article preserves the original approved explanation, concrete examples, useful evidence, clear next steps and every reviewed image. It contains enough readable words to exercise public article verification against a native theme."


def reserve(test):
    if test.platform == "wordpress": return fixtures.reserve(test)
    test.connection.save = Mock()
    test.binding["platform"] = "ghost"
    test.private.payload = {"schema_version": 1, "binding": deepcopy(test.binding), "sealed_credential": seal_credential(test.binding,
        secret="a" * 24 + ":" + "b" * 64)}
    test.config.article_system["publishing_channel"]["selection"]["platform"] = "ghost"
    def relation(**filters):
        return SimpleNamespace(first=lambda: test.private) if filters.get("action") == "channel-credential" else test.manager.filter(**filters)
    test.rows.filter.side_effect = relation
    test.rows.exclude.return_value.only.return_value.order_by.return_value.first.return_value = None
    with patch("content_factory.website_connections.require_unlocked_remote_call"), patch.object(policies, "request_json",
            return_value=({"users": [{"id": "c" * 24, "name": "Existing author", "status": "active"}]}, {})):
        test.policy = policies.configure(test.context, test.config, user=test.user, data={**contract_for(test.connection),
            "client_request_id": str(uuid4()), "review_policy": test.mode, "publication_confirmed": True,
            "native_defaults_confirmed": True, "expected_policy_id": None})
    return artifact_fixtures.ChannelArtifactTests.reserve(test)


def set_handoff(test, value):
    article = value["article"]
    article["html"] = native_fragment(article["html"].replace("</article>", "<p>" + PROSE + "</p></article>"), title=article["title"])
    article["profile"] = profile_receipt(article["html"], title=article["title"])
    value["editorial_acceptance"]["transport_sha256"] = article["profile"]["html_sha256"]
    value["preview_html"] = preview_document(article["html"], title=article["title"])
    fixtures.CmsArticleApprovalTests.set_handoff(test, value)


def acknowledge(test, binding, credential, *, upload_id, image, **kwargs):
    if test.platform == "wordpress":
        return fixtures.CmsArticleApprovalTests.acknowledge(test, binding, credential, upload_id=upload_id, image=image, **kwargs)
    test.assertEqual(test.depth, 0)
    return UploadedImage("ghost", None, upload_id, image.sha256, binding["public_origin"] + "content/images/owned.png")


def prepare(test):
    fixtures.prepare(test, "auto")
    test.approval = approvals._start(test.stage, basis="owner_setup_auto", expected_revision=test.data["expected_revision"])
    approvals.advance_approval(test.approval.pk, now=test.now)
    test.assertEqual(test.approval.state, "completed")
    test.operation = publishing.start_publication(test.approval)
    test.created_posts = []
    def request(binding, credential, **effect):
        test.assertEqual(test.depth, 0)
        test.assertEqual(effect["resource"], "posts")
        value = test.operation.payload["intent"]
        if effect["method"] == "POST":
            test.assertEqual(test.operation.payload["post_state"]["phase"], "create_issued")
            test.assertEqual(test.operation.payload["post_state"]["create_count"], 1)
            test.assertTrue(test.operation.receipt["outcome_unknown"])
            row = native_response(value)
            test.created_posts.append(row)
            return (row if test.platform == "wordpress" else {"posts": [row]}), {}
        if effect["identifier"] is not None:
            test.assertEqual(effect["identifier"], test.operation.payload["post_state"]["provider_id"])
            row = test.created_posts[0]
            return (row if test.platform == "wordpress" else {"posts": [row]}), {}
        return collection(value, deepcopy(test.created_posts)), {}
    def public_html(intent, state):
        test.assertEqual(test.depth, 0)
        return '<html><head><link rel="canonical" href="' + state["public_url"] + '"></head><body><h1>' + intent["title"] + '</h1>' + intent["html"] + '</body></html>'
    test.request_effect = request
    test.public_effect = public_html
    test.http_patch = patch.object(publishing, "request_json", side_effect=request)
    test.http = test.http_patch.start(); test.addCleanup(test.http_patch.stop)
    test.public_patch = patch.object(publishing.public, "fetch_public_article", side_effect=public_html)
    test.public_get = test.public_patch.start(); test.addCleanup(test.public_patch.stop)
    test.fetch.reset_mock()


@override_settings(ARTICLES_CHANNEL_DISCOVERY_ENABLED=True, ARTICLES_PUBLISHING_CHANNELS_ENABLED=True,
    ARTICLES_CMS_CHANNEL_ENABLED=True, ARTICLES_CHANNEL_ARTIFACTS_ENABLED=True,
    CONNECTOR_CREDENTIAL_KEYS=json.dumps({"test": fixtures.fixtures.TEST_KEY}), CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID="test")
class CmsArticlePublicationTests(SimpleTestCase):
    platform = "wordpress"
    reserve, set_handoff, acknowledge = reserve, set_handoff, acknowledge

    def setUp(self): prepare(self)

    def advance(self):
        return publishing.advance_publication(self.operation.pk, now=(self.operation.next_attempt_at or self.now) + timedelta(seconds=1))

    def test_one_create_then_exact_provider_read_and_public_body_proof_complete_the_same_operation(self):
        self.assertIs(publishing.start_publication(self.approval), self.operation)
        self.advance(); self.assertEqual(self.operation.payload["post_state"]["phase"], "create_ready")
        self.advance(); self.assertEqual(self.operation.payload["post_state"]["phase"], "provider_confirmed")
        self.public_get.assert_not_called()
        self.assertIs(self.operation.receipt["publishing_verified"], False)
        self.advance(); self.assertEqual(self.operation.state, "completed")
        source, intent, state, proof = publishing.current_publication(self.operation)
        self.assertEqual((proof["status"], source.billing["spend_ledger_id"]), ("body_verified", "31"))
        self.assertEqual(intent["html"], self.stage.payload["staged"]["article"]["html"])
        self.assertEqual(state["provider_id"], self.created_posts[0]["id"])
        self.assertEqual(self.operation.receipt["publicUrl"], state["public_url"])
        self.assertFalse(self.operation.receipt["fallback_safe"])
        self.assertEqual([call.kwargs["method"] for call in self.http.call_args_list], ["GET", "POST", "GET"])
        self.assertEqual(self.provider_post.call_count, 1)
        self.assertEqual(self.connection.verified_sha, "")
        self.assertIs(publishing.start_publication(self.approval), self.operation)
        self.assertIs(publishing.advance_publication(self.operation.pk), self.operation)

    def test_lost_create_reply_recovers_by_unique_slug_without_another_post(self):
        self.advance()
        def lose(*args, **kwargs):
            result = self.request_effect(*args, **kwargs)
            if kwargs["method"] == "POST": raise CmsApiError("transient")
            return result
        self.http.side_effect = lose
        self.advance()
        self.assertEqual(self.operation.payload["post_state"]["phase"], "create_issued")
        self.assertIsNone(self.operation.receipt["provider_modified"])
        self.assertFalse(self.operation.receipt["fallback_safe"])
        self.advance(); self.advance()
        self.assertEqual(self.operation.state, "completed")
        self.assertEqual(sum(call.kwargs["method"] == "POST" for call in self.http.call_args_list), 1)

    def test_stopped_goal_before_allowance_cannot_create_a_provider_post(self):
        self.advance()
        with patch.object(publishing, "require_work", side_effect=CmsGoalWorkDenied()): self.advance()
        self.assertEqual((self.operation.state, self.operation.payload["post_state"]["create_count"]), ("blocked", 0))
        self.assertEqual(self.operation.receipt["status"], "goal_work_stopped")
        self.assertTrue(self.operation.receipt["fallback_safe"])
        self.assertEqual(self.created_posts, [])

    def test_stop_racing_reserved_allowance_keeps_unknown_journal_and_only_get_recovery(self):
        self.advance()
        with patch.object(publishing, "require_work", side_effect=[None, CmsGoalWorkDenied()]): self.advance()
        self.assertEqual((self.operation.state, self.operation.payload["post_state"]["create_count"]), ("pending", 1))
        self.assertTrue(self.operation.receipt["outcome_unknown"])
        with patch.object(publishing, "require_work", side_effect=CmsGoalWorkDenied()) as work:
            for _ in range(8): self.advance()
        work.assert_not_called()
        self.assertEqual(self.operation.state, "blocked")
        self.assertFalse(self.operation.receipt["fallback_safe"])
        self.assertTrue(all(call.kwargs["method"] == "GET" for call in self.http.call_args_list))

    def test_stop_after_actual_create_keeps_exact_provider_and_public_readback(self):
        self.advance(); self.advance()
        with patch.object(publishing, "require_work", side_effect=CmsGoalWorkDenied()) as work: self.advance()
        work.assert_not_called()
        self.assertEqual((self.operation.state, len(self.created_posts)), ("completed", 1))

    def test_stale_public_proof_rechecks_same_post_without_a_new_allowance_or_approval(self):
        self.advance(); self.advance(); self.advance()
        from datetime import datetime
        proof = self.operation.payload["public_observation"]
        proof["checked_at"] = (datetime.fromisoformat(proof["checked_at"]) - timedelta(hours=1)).isoformat()
        proof["digest"] = digest({key: value for key, value in proof.items() if key != "digest"})
        self.operation.receipt = publishing._receipt(self.operation, proof)
        original = deepcopy(self.operation.payload["post_state"])
        self.http.reset_mock()
        publishing.request_public_recheck(self.operation)
        self.assertEqual((self.operation.state, self.operation.payload["public_observation"]), ("pending", None))
        publishing.request_public_recheck(self.operation)
        self.assertEqual(self.operation.payload["post_state"], original)
        with patch.object(publishing, "require_work", side_effect=CmsGoalWorkDenied()) as work: self.advance()
        work.assert_not_called()
        self.assertEqual((self.operation.state, len(self.created_posts)), ("completed", 1))
        self.assertEqual(self.operation.payload["post_state"], original)
        self.assertEqual([call.kwargs["method"] for call in self.http.call_args_list], ["GET"])
        publishing.current_publication(self.operation)

    def test_fresh_public_proof_recheck_is_inert_and_corrupt_receipt_cannot_be_requeued(self):
        self.advance(); self.advance(); self.advance()
        self.http.reset_mock(); self.public_get.reset_mock()
        self.assertIs(publishing.request_public_recheck(self.operation), self.operation)
        self.assertEqual(self.operation.state, "completed")
        self.http.assert_not_called(); self.public_get.assert_not_called()
        self.operation.receipt["publicProofDigest"] = "f" * 64
        with self.assertRaises(WebsiteAuthorityError): publishing.request_public_recheck(self.operation)

    def test_malformed_success_reply_is_unknown_until_get_recovers_the_original_resource(self):
        self.advance()
        def malformed(*args, **kwargs):
            result = self.request_effect(*args, **kwargs)
            return ({"unexpected": "reply"}, {}) if kwargs["method"] == "POST" else result
        self.http.side_effect = malformed
        self.advance(); self.assertEqual(self.operation.state, "pending")
        self.assertTrue(self.operation.receipt["outcome_unknown"])
        self.advance(); self.advance()
        self.assertEqual(self.operation.state, "completed")
        self.assertEqual(len(self.created_posts), 1)

    def test_worker_crash_after_allowance_never_issues_another_create_even_if_nothing_was_sent(self):
        self.advance()
        self.http.side_effect = RuntimeError("synthetic worker crash")
        with self.assertRaises(RuntimeError): self.advance()
        self.assertEqual(self.operation.payload["post_state"]["create_count"], 1)
        self.http.side_effect = self.request_effect
        self.advance()
        self.assertEqual(self.operation.payload["post_state"]["probe_count"], 1)
        self.assertEqual(len(self.created_posts), 0)
        for _ in range(7): self.advance()
        self.assertEqual(self.operation.state, "blocked")
        self.assertTrue(self.operation.receipt["outcome_unknown"])
        self.assertFalse(self.operation.receipt["fallback_safe"])

    def test_transient_cf_read_before_create_does_not_consume_the_provider_allowance(self):
        self.advance()
        self.fetch.side_effect = reviews.ArtifactReadError("unavailable")
        self.advance()
        self.assertEqual((self.operation.state, self.operation.payload["post_state"]["create_count"]), ("pending", 0))
        self.assertTrue(self.operation.receipt["fallback_safe"])
        self.fetch.side_effect = None
        self.advance(); self.advance()
        self.assertEqual(self.operation.state, "completed")

    def test_expired_media_reuses_original_approval_and_refreshes_existing_images_before_any_post(self):
        fixtures.CmsArticleApprovalTests.age_media(self)
        self.advance()
        self.assertEqual(self.operation.receipt["status"], "publication_waiting_media")
        self.assertEqual(self.operation.payload["post_state"]["create_count"], 0)
        self.http.assert_not_called()
        media.advance_media(self.media.pk, now=self.now + timedelta(seconds=2))
        self.advance(); self.advance(); self.advance()
        self.assertEqual(self.operation.state, "completed")
        self.assertEqual(self.provider_post.call_count, 1)

    def test_refund_during_create_discards_reply_but_retains_uncertain_allowance_and_blocks_fallback(self):
        self.advance()
        def refund(*args, **kwargs):
            result = self.request_effect(*args, **kwargs)
            if kwargs["method"] == "POST": self.refund.return_value.exists.return_value = True
            return result
        self.http.side_effect = refund
        self.advance()
        self.assertEqual((self.operation.state, self.operation.payload["post_state"]["create_count"]), ("cancelled", 1))
        self.assertTrue(self.operation.receipt["outcome_unknown"])
        self.assertFalse(self.operation.receipt["fallback_safe"])
        self.public_get.assert_not_called()

    def test_source_change_after_preflight_cancels_before_post(self):
        self.advance()
        self.fetch.return_value = {**deepcopy(self.value), "review_revision": "f" * 64}
        self.advance()
        self.assertEqual(self.operation.state, "cancelled")
        self.assertEqual(len(self.created_posts), 0)
        self.assertTrue(self.operation.receipt["fallback_safe"])

    def test_occupied_slug_never_gets_overwritten_or_adopted(self):
        self.created_posts.append(native_response(self.operation.payload["intent"]))
        self.advance()
        self.assertEqual(self.operation.state, "blocked")
        self.assertEqual(self.operation.payload["post_state"]["create_count"], 0)
        self.public_get.assert_not_called()

    def test_modified_acknowledged_provider_post_is_not_publicly_verified_or_rewritten(self):
        self.advance(); self.advance()
        self.created_posts[0]["slug"] = "foreign-post"
        self.advance()
        self.assertEqual(self.operation.state, "blocked")
        self.assertIs(self.operation.receipt["provider_modified"], True)
        self.assertFalse(self.operation.receipt["fallback_safe"])
        self.public_get.assert_not_called()

    def test_cdn_wait_retries_public_proof_without_recreating_or_claiming_live_early(self):
        self.advance(); self.advance()
        self.public_get.side_effect = CmsApiError("public_article_unavailable")
        self.advance()
        self.assertEqual(self.operation.state, "pending")
        self.assertIs(self.operation.receipt["publishing_verified"], False)
        self.public_get.side_effect = self.public_effect
        self.advance()
        self.assertEqual(self.operation.state, "completed")
        self.assertEqual(len(self.created_posts), 1)

    def test_different_public_copy_or_wrong_canonical_url_cannot_complete_the_operation(self):
        self.advance(); self.advance()
        self.public_get.side_effect = lambda intent, state: self.public_effect(intent, state).replace("concrete", "different")
        self.advance()
        self.assertEqual(self.operation.state, "blocked")
        self.assertIs(self.operation.receipt["publishing_verified"], False)
        self.assertIsNone(self.operation.payload["public_observation"])

    def test_late_provider_reply_cannot_overwrite_owner_cancellation_or_newer_lease(self):
        self.advance()
        def replace(*args, **kwargs):
            result = self.request_effect(*args, **kwargs)
            self.operation.payload["lease_id"] = "newer-worker"
            self.operation.receipt = {"status": "newer-owner-or-worker"}
            return result
        self.http.side_effect = replace
        self.advance()
        self.assertEqual(self.operation.receipt, {"status": "newer-owner-or-worker"})
        self.public_get.assert_not_called()

    def test_closed_live_receipt_and_intent_reject_copied_or_rechecksummed_source_substitutions(self):
        self.advance(); self.advance(); self.advance()
        self.operation.receipt["publicUrl"] = "https://fixture.test/other/"
        with self.assertRaises(WebsiteAuthorityError): publishing.current_publication(self.operation)
        self.operation.payload["intent"]["approval_sha256"] = "f" * 64
        with self.assertRaises((WebsiteAuthorityError, CmsApiError)): publishing.current_publication(self.operation)

    def test_disabled_publication_does_not_read_private_rows_or_call_provider(self):
        with override_settings(ARTICLES_CHANNEL_ARTIFACTS_ENABLED=False), patch.object(WebsiteConnectionOperation.objects, "filter") as rows:
            self.assertIsNone(publishing.advance_publication(self.operation.pk))
            self.assertEqual(publishing.ensure_publications(), 0)
            with self.assertRaises(WebsiteAuthorityError): publishing.start_publication(self.approval)
        rows.assert_not_called(); self.http.assert_not_called(); self.public_get.assert_not_called()


@override_settings(ARTICLES_CHANNEL_DISCOVERY_ENABLED=True, ARTICLES_PUBLISHING_CHANNELS_ENABLED=True,
    ARTICLES_CMS_CHANNEL_ENABLED=True, ARTICLES_CHANNEL_ARTIFACTS_ENABLED=True,
    CONNECTOR_CREDENTIAL_KEYS=json.dumps({"test": fixtures.fixtures.TEST_KEY}), CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID="test")
class CmsGhostArticlePublicationTests(SimpleTestCase):
    platform = "ghost"
    reserve, set_handoff, acknowledge = reserve, set_handoff, acknowledge
    advance = CmsArticlePublicationTests.advance

    def setUp(self): prepare(self)

    def test_ghost_native_create_confirmation_and_public_proof(self):
        CmsArticlePublicationTests.test_one_create_then_exact_provider_read_and_public_body_proof_complete_the_same_operation(self)

    def test_ghost_lost_post_reply_reconciles_by_slug_without_repeating_post(self):
        CmsArticlePublicationTests.test_lost_create_reply_recovers_by_unique_slug_without_another_post(self)
