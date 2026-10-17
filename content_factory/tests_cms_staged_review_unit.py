"""Final native review changes verified image destinations without rewriting copy."""

from copy import deepcopy
from datetime import timedelta
import html
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from . import cms_staged_article as articles
from . import cms_staged_review as stages
from . import cms_owned_media as staging
from . import tests_cms_owned_media_unit as fixtures
from .channel_artifact_contract import digest
from .cms_api_transport import CmsApiError
from .cms_native_html import native_fragment
from .tests_cms_connection_contract_unit import TEST_KEY
from .website_contract import WebsiteAuthorityError


@override_settings(ARTICLES_CHANNEL_DISCOVERY_ENABLED=True, ARTICLES_PUBLISHING_CHANNELS_ENABLED=True,
    ARTICLES_CMS_CHANNEL_ENABLED=True, ARTICLES_CHANNEL_ARTIFACTS_ENABLED=True,
    CONNECTOR_CREDENTIAL_KEYS=fixtures.source_fixtures.fixtures.json.dumps({"test": TEST_KEY}), CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID="test")
class CmsStagedReviewTests(SimpleTestCase):
    reserve = fixtures.CmsOwnedMediaTests.reserve
    set_handoff = fixtures.CmsOwnedMediaTests.set_handoff
    acknowledge = fixtures.CmsOwnedMediaTests.acknowledge

    def setUp(self):
        fixtures.CmsOwnedMediaTests.setUp(self)
        self.media = staging.start_media(self.operation)
        for _ in range(3): staging.advance_media(self.media.pk, now=self.now)
        self.source = staging.completed_media(self.media)

    def build(self):
        return articles.build_staged_article(self.source, media_intents=self.media.payload["intents"], media_states=self.media.payload["states"])

    def test_final_preview_contains_owned_images_and_original_editorial_receipt_without_a_new_review_claim(self):
        before = deepcopy(self.source.handoff)
        value = self.build()
        self.assertIn("wp-content/uploads/owned.png", value["article"]["html"])
        self.assertNotIn("firebasestorage.googleapis.com", value["article"]["html"])
        self.assertEqual(value["original_editorial_acceptance"], before["editorial_acceptance"])
        self.assertNotEqual(value["article"]["profile"]["html_sha256"], before["article"]["profile"]["html_sha256"])
        self.assertEqual(value["original_editorial_acceptance"]["transport_sha256"], before["article"]["profile"]["html_sha256"])
        self.assertIn(value["article"]["html"], value["preview_html"])
        self.assertEqual(value["substitution_profile"], "cms_exact_media_substitution_v1")
        self.assertNotIn("approved", value)
        self.assertEqual(self.source.handoff, before)

    def test_lexical_src_ranges_preserve_code_crlf_unicode_citations_and_tables_byte_for_byte(self):
        asset = self.value["images"][0]
        title = self.value["article"]["title"]
        raw = '<article><h1>' + title + '</h1><pre><code>A\tB\r\n1\t2\r\n' + html.escape(asset["src"]) + '</code></pre>'
        raw += '<p>Exact prose 🙂 <a href="' + html.escape(asset["src"], quote=True) + '">Original image citation</a></p>'
        raw += '<table><tr><td>Exact cell</td></tr></table><img alt="' + asset["alt"] + '" src="' + html.escape(asset["src"], quote=True) + '"></article>'
        fragment = native_fragment(raw, title=title)
        destination = self.media.payload["states"][0]["acknowledgement"]["url"]
        actual = articles.replace_image_destinations(fragment, title=title, assets=[asset], destinations=[destination])
        expected = fragment.replace(' src="' + html.escape(asset["src"], quote=True) + '"', ' src="' + destination + '"')
        self.assertEqual(actual, expected)
        self.assertIn("A\tB\r\n1\t2\r\n", actual)
        self.assertIn('href="' + html.escape(asset["src"], quote=True) + '"', actual)

    def test_reobserving_identical_public_images_preserves_owner_revision_and_never_reuploads(self):
        stage = stages.start_stage(self.media)
        value = deepcopy(stage.payload["staged"])
        for state in self.media.payload["states"]:
            old = state["evidence"]
            old["checked_at"] = (self.now - timedelta(hours=1)).isoformat()
            old["digest"] = digest({key: item for key, item in old.items() if key != "digest"})
        with self.assertRaises(CmsApiError): stages.current_stage(stage)
        self.assertEqual(stages.current_stage(stage, fresh_media=False)[1], value)
        staging.request_public_recheck(self.media, now=self.now)
        self.assertEqual((self.media.state, self.media.payload["states"][0]["create_count"]), ("pending", 1))
        self.assertEqual(self.media.payload["states"][0]["phase"], "provider_ack")
        staging.advance_media(self.media.pk, now=self.now)
        self.assertEqual(self.media.state, "completed")
        self.assertEqual(stages.current_stage(stage)[1], value)
        self.assertIs(stages.start_stage(self.media), stage)
        self.assertEqual(self.provider_post.call_count, 1)

    def test_private_stage_replay_and_scope_are_checked_and_receipt_never_grants_approval(self):
        stage = stages.start_stage(self.media)
        self.assertIs(stages.start_stage(self.media), stage)
        self.assertEqual(stage.receipt["status"], "artifact_staged")
        for key in ("provider_modified", "approval_recorded", "publishing_verified"):
            self.assertIs(stage.receipt[key], False)
        self.assertNotIn("Fixture guide", str(stage.receipt))
        self.assertEqual(stages.current_stage(stage)[0].billing["spend_ledger_id"], "31")
        self.refund.return_value.exists.return_value = True
        with self.assertRaises(WebsiteAuthorityError): stages.current_stage(stage)

    def test_rechecksummed_owner_or_worker_changes_cannot_replace_final_body_or_native_scope(self):
        value = self.build()
        for changes in ({"approved": True}, {"schema_version": True}, {"source_generation": False},
                {"article": {**value["article"], "html": "<article>Other copy</article>"}},
                {"scope_sha256": "f" * 64}):
            changed = {**deepcopy(value), **changes}
            changed["revision"] = digest({key: item for key, item in changed.items() if key != "revision"})
            with self.subTest(changes=tuple(changes)), self.assertRaises(CmsApiError):
                articles.validate_staged_article(changed, self.source, media_intents=self.media.payload["intents"], media_states=self.media.payload["states"])

    def test_unverified_mismatched_or_unsafe_image_substitutions_fail_before_owner_review(self):
        for changes in ({"phase": "provider_ack", "evidence": None}, {"intent_digest": "f" * 64}):
            with self.assertRaises(CmsApiError):
                articles.build_staged_article(self.source, media_intents=self.media.payload["intents"],
                    media_states=[{**deepcopy(self.media.payload["states"][0]), **changes}])
        fragment = self.value["article"]["html"]
        for assets, destinations in (([], []), ([self.value["images"][0]], ["javascript:private()"]),
                ([{**self.value["images"][0], "alt": "Different image"}], ["https://fixture.test/owned.png"])):
            with self.assertRaises(CmsApiError):
                articles.replace_image_destinations(fragment, title=self.value["article"]["title"], assets=assets, destinations=destinations)

    def test_disabled_stage_reservations_do_not_scan_private_operations(self):
        with override_settings(ARTICLES_CHANNEL_ARTIFACTS_ENABLED=False), patch.object(stages, "completed_media") as source:
            self.assertEqual(stages.ensure_stages(), 0)
            with self.assertRaises(WebsiteAuthorityError): stages.start_stage(self.media)
        source.assert_not_called()
