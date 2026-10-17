"""Actual media staging control flow uses only fake provider/CF/ledger boundaries."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
import hashlib
import html
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from . import cms_owned_media as staging
from . import cms_media_contract as journal
from . import cms_media_evidence as pixels
from . import tests_cms_article_source_unit as source_fixtures
from .tests_cms_connection_contract_unit import TEST_KEY
from .tests_cms_media_contract_unit import row
from .tests_cms_media_evidence_unit import raster
from .cms_media_transport import UploadedImage, prepare_image
from .cms_native_html import native_fragment, preview_document, profile_receipt
from .channel_artifact_contract import digest
from .cms_goal_effects import CmsGoalWorkDenied


@override_settings(ARTICLES_CHANNEL_DISCOVERY_ENABLED=True, ARTICLES_PUBLISHING_CHANNELS_ENABLED=True,
    ARTICLES_CMS_CHANNEL_ENABLED=True, ARTICLES_CHANNEL_ARTIFACTS_ENABLED=True,
    CONNECTOR_CREDENTIAL_KEYS=source_fixtures.fixtures.json.dumps({"test": TEST_KEY}), CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID="test")
class CmsOwnedMediaTests(SimpleTestCase):
    reserve = source_fixtures.CmsArticleSourceTests.reserve

    def setUp(self):
        source_fixtures.CmsArticleSourceTests.setUp(self)
        # All controller claims and media proof reads in this fixture share the
        # supplied clock. Host contention must not make a newly reserved stage
        # fall after the fixture's one-second scheduling allowance.
        self.now = timezone.now()
        clock = patch("django.utils.timezone.now", side_effect=lambda: self.now)
        clock.start(); self.addCleanup(clock.stop)
        self.empty_value = deepcopy(self.value)
        self.image = prepare_image(raster())
        asset = {"component_id": "hero-image", "src": "https://firebasestorage.googleapis.com/v0/b/fixture/o/image.png?alt=media",
            "alt": "A blue image", "sha256": self.image.sha256, "width": 32, "height": 24}
        title = self.value["article"]["title"]
        fragment = self.value["article"]["html"].replace("</article>", '<img src="' + html.escape(asset["src"], quote=True) + '" alt="A blue image"></article>')
        fragment = native_fragment(fragment, title=title)
        self.value["images"] = [asset]
        self.value["article"].update(html=fragment, profile=profile_receipt(fragment, title=title))
        self.value["editorial_acceptance"]["transport_sha256"] = self.value["article"]["profile"]["html_sha256"]
        self.value["preview_html"] = preview_document(fragment, title=title)
        self.set_handoff(self.value)
        self.connection.capabilities["_cms_identity"] = {"can_upload_media": True}
        self.depth = 0
        @contextmanager
        def atomic(*args, **kwargs):
            self.depth += 1
            try: yield
            finally: self.depth -= 1
        def unlocked(): self.assertEqual(self.depth, 0)
        self.media_patches = [patch("django.db.transaction.atomic", side_effect=atomic),
            patch("content_factory.website_connections.require_unlocked_remote_call", side_effect=unlocked),
            patch.object(staging, "fetch_source_image", return_value=self.image),
            patch.object(staging, "request_json", return_value=([], {})),
            patch.object(staging, "upload_image", side_effect=self.acknowledge),
            patch.object(pixels, "_read_image", return_value=self.image)]
        values = [item.start() for item in self.media_patches]
        self.source_fetch, self.provider_get, self.provider_post = values[2:5]
        for item in self.media_patches: self.addCleanup(item.stop)

    def set_handoff(self, value):
        value["preview_sha256"] = hashlib.sha256(value["preview_html"].encode()).hexdigest()
        value["digest"] = digest({key: item for key, item in value.items() if key != "digest"})
        self.operation.payload["handoff"] = deepcopy(value)
        self.operation.receipt["artifactDigest"] = value["digest"]
        self.fetch.return_value = deepcopy(value)

    def acknowledge(self, binding, credential, *, upload_id, image, **kwargs):
        self.assertEqual(self.depth, 0)
        active = next(operation for operation in self.saved if operation.action == staging.ACTION)
        state = active.payload["states"][0]
        self.assertEqual((state["phase"], state["create_count"]), ("upload_issued", 1))
        return UploadedImage("wordpress", 42, upload_id, image.sha256,
            binding["public_origin"] + "wp-content/uploads/owned.png")

    def start(self): return staging.start_media(self.operation)

    def advance(self, operation, **kwargs):
        return staging.advance_media(operation.pk, now=kwargs.get("now", self.now))

    def test_source_preflight_upload_and_exact_public_bytes_finish_without_post_creation_or_approval(self):
        operation = self.start()
        self.assertIs(self.start(), operation)
        self.assertEqual(operation.payload["states"][0]["phase"], "reserved")
        self.advance(operation)
        self.assertEqual(operation.payload["states"][0]["phase"], "create_ready")
        self.advance(operation)
        self.assertEqual(operation.payload["states"][0]["phase"], "provider_ack")
        self.advance(operation)
        self.assertEqual((operation.state, operation.receipt["verified"]), ("completed", 1))
        self.assertIs(operation.receipt["approval_recorded"], False)
        self.assertNotIn("public_url", operation.receipt)
        self.assertEqual(operation.payload["states"][0]["evidence"]["public_sha256"], self.image.sha256)
        self.assertEqual(self.provider_post.call_count, 1)
        self.assertEqual(self.provider_get.call_args.kwargs["resource"], "media")
        self.assertEqual(self.connection.verified_sha, "")
        self.assertEqual(staging.completed_media(operation).handoff, self.value)
        with self.assertRaises(staging.CmsApiError):
            staging.completed_media(operation, now=self.now + timedelta(hours=1))

    def test_lost_upload_reply_recovers_marked_attachment_and_never_posts_again(self):
        operation = self.start(); self.advance(operation)
        self.provider_post.side_effect = staging.CmsApiError("transient")
        self.advance(operation)
        self.assertEqual((operation.state, operation.payload["states"][0]["phase"]), ("pending", "upload_issued"))
        self.assertIsNone(operation.receipt["provider_modified"])
        intent = operation.payload["intents"][0]
        self.provider_get.return_value = ([row(intent, self.image)], {})
        self.advance(operation, now=operation.next_attempt_at + timedelta(seconds=1))
        self.assertEqual(operation.payload["states"][0]["phase"], "provider_ack")
        self.advance(operation, now=operation.next_attempt_at + timedelta(seconds=1))
        self.assertEqual(operation.state, "completed")
        self.assertEqual(self.provider_post.call_count, 1)

    def test_stopped_goal_before_upload_allowance_does_not_create_native_media(self):
        operation = self.start(); self.advance(operation)
        with patch.object(staging, "require_work", side_effect=CmsGoalWorkDenied()): self.advance(operation)
        self.assertEqual((operation.state, operation.payload["states"][0]["create_count"]), ("blocked", 0))
        self.assertEqual(operation.receipt["status"], "goal_work_stopped")
        self.provider_post.assert_not_called()

    def test_stop_between_upload_reservation_and_http_preserves_get_only_recovery(self):
        operation = self.start(); self.advance(operation)
        with patch.object(staging, "require_work", side_effect=[None, CmsGoalWorkDenied()]): self.advance(operation)
        self.assertEqual((operation.state, operation.payload["states"][0]["create_count"]), ("pending", 1))
        self.assertEqual(operation.payload["states"][0]["phase"], "upload_issued")
        self.provider_post.assert_not_called()
        intent = operation.payload["intents"][0]
        self.provider_get.return_value = ([row(intent, self.image)], {})
        with patch.object(staging, "require_work", side_effect=CmsGoalWorkDenied()) as work:
            self.advance(operation, now=operation.next_attempt_at + timedelta(seconds=1))
            self.advance(operation, now=operation.next_attempt_at + timedelta(seconds=1))
        work.assert_not_called()
        self.assertEqual(operation.state, "completed")
        self.provider_post.assert_not_called()

    def test_worker_crash_after_issue_allowance_recovers_by_get_even_if_post_was_not_sent(self):
        operation = self.start(); self.advance(operation)
        self.provider_post.side_effect = RuntimeError("synthetic worker crash")
        with self.assertRaises(RuntimeError): self.advance(operation)
        self.assertEqual(operation.state, "running")
        self.assertEqual(operation.payload["states"][0]["create_count"], 1)
        self.advance(operation)
        self.assertEqual(self.provider_post.call_count, 1)
        self.advance(operation, now=operation.next_attempt_at + timedelta(seconds=1))
        self.assertEqual(operation.payload["states"][0]["probe_count"], 1)
        self.assertEqual(self.provider_post.call_count, 1)

    def test_refund_committing_during_upload_discards_acknowledgement_and_keeps_recovery_journal(self):
        operation = self.start(); self.advance(operation)
        def refund(*args, **kwargs):
            ack = self.acknowledge(*args, **kwargs)
            self.refund.return_value.exists.return_value = True
            return ack
        self.provider_post.side_effect = refund
        self.advance(operation)
        self.assertEqual(operation.state, "cancelled")
        self.assertEqual(operation.payload["states"][0]["phase"], "upload_issued")
        self.assertIsNone(operation.payload["states"][0]["acknowledgement"])
        self.assertIsNone(operation.receipt["provider_modified"])

    def test_current_cf_revision_is_rechecked_before_any_provider_effect(self):
        operation = self.start()
        self.fetch.return_value = {**deepcopy(self.value), "review_revision": "f" * 64}
        self.advance(operation)
        self.assertEqual(operation.state, "cancelled")
        self.provider_get.assert_not_called(); self.provider_post.assert_not_called()

    def test_zero_images_complete_without_remote_reads_or_uploads(self):
        self.set_handoff(self.empty_value)
        self.fetch.reset_mock()
        operation = self.start(); self.advance(operation)
        self.assertEqual((operation.state, operation.receipt["count"], operation.receipt["verified"]), ("completed", 0, 0))
        self.fetch.assert_not_called(); self.source_fetch.assert_not_called(); self.provider_post.assert_not_called()
        staging.completed_media(operation)

    def test_disabled_media_never_acquires_a_row_and_unknown_permissions_do_no_http(self):
        with override_settings(ARTICLES_CHANNEL_ARTIFACTS_ENABLED=False), patch.object(staging, "current_article") as current:
            self.assertIsNone(staging.advance_media(self.operation.pk))
            self.assertEqual(staging.ensure_media(), 0)
        current.assert_not_called()
        operation = self.start()
        self.connection.capabilities["_cms_identity"]["can_upload_media"] = False
        self.advance(operation)
        self.assertEqual(operation.state, "blocked")
        self.assertEqual(operation.receipt["failure_class"], "strategy")
        self.provider_get.assert_not_called(); self.provider_post.assert_not_called()
