"""A timed-out create never becomes another POST or an unrelated page overwrite."""

from copy import deepcopy
import hashlib
from uuid import uuid4

from django.test import SimpleTestCase

from . import cms_post_contract as posts
from .cms_api_transport import CmsApiError
from .tests_cms_inventory_contract_unit import binding


def intent(platform="wordpress", *, status="published", **changes):
    values = {"binding": binding(platform), "operation_id": str(uuid4()), "source_run_id": "original-writing-run",
        "run_generation": 1, "artifact_sha256": "a" * 64, "editorial_sha256": "b" * 64,
        "approval_sha256": "c" * 64, "title": "Approved native article", "slug_seed": "approved-native-article",
        "html": '<article data-cf-article-body><h1>Approved native article</h1><p>The exact approved body.</p></article>',
        "status": status, "native_fields": {"author": 7, "categories": [2, 3], "tags": [4], "featured_media": 0}
            if platform == "wordpress" else {"authors": ["a" * 24], "tags": ["b" * 24]}}
    return posts.make_intent(**{**values, **changes})


def response(value, **changes):
    if value["binding"]["platform"] == "wordpress":
        return {"id": 42, "type": "post", "title": {"raw": value["title"]}, "slug": value["slug"],
            "content": {"raw": posts.marked_html(value), "protected": False}, "password": "",
            "status": "publish" if value["status"] == "published" else "draft", **deepcopy(value["native_fields"]),
            "link": "https://fixture.test/articles/" + value["slug"] + "/", **changes}
    return {"id": "d" * 24, "title": value["title"], "slug": value["slug"], "status": value["status"],
        "html": posts.marked_html(value), "visibility": "public", "url": "https://fixture.test/" + value["slug"] + "/",
        **{name: [{"id": item} for item in refs] for name, refs in value["native_fields"].items()}, **changes}


def collection(value, rows):
    return rows if value["binding"]["platform"] == "wordpress" else {"posts": rows}


def issued(value):
    state = posts.new_post_state(value)
    ready = posts.observe_posts(value, state, collection(value, []))
    return posts.issue_create(value, ready)


class CmsPostContractTests(SimpleTestCase):
    def test_intent_binds_original_artifact_approval_editorial_and_current_scope(self):
        value = intent()
        self.assertEqual(value["slug"], "approved-native-article-" + value["operation_id"].replace("-", ""))
        self.assertEqual(value["body_sha256"], hashlib.sha256(value["html"].encode()).hexdigest())
        self.assertEqual(posts.validate_intent(value), value)
        for changes in ({"html": "Changed body"}, {"binding": {**value["binding"], "generation": 2}},
                        {"source_run_id": "other"}, {"artifact_sha256": "d" * 64}, {"approval_sha256": "d" * 64},
                        {"editorial_sha256": "d" * 64}, {"native_fields": {**value["native_fields"], "author": 99}}):
            with self.subTest(changes=tuple(changes)), self.assertRaises(CmsApiError):
                posts.validate_intent({**value, **changes})

    def test_closed_typed_intent_rejects_unknown_authority_fields_and_malformed_native_ids(self):
        for changes in ({"run_generation": True}, {"operation_id": "invalid"}, {"status": "publish"},
                        {"title": "<script>bad</script>"}, {"slug_seed": "other/path"}, {"artifact_sha256": True},
                        {"title": "x" * 201}, {"slug_seed": "x" * 151},
                        {"native_fields": {"author": True, "categories": [], "tags": [], "featured_media": 0}},
                        {"native_fields": {"author": 7, "categories": [2, 2], "tags": [], "featured_media": 0}}):
            with self.subTest(changes=tuple(changes)), self.assertRaises(CmsApiError): intent(**changes)
        value = intent()
        with self.assertRaises(CmsApiError): posts.validate_intent({**value, "approved": True})
        with self.assertRaises(CmsApiError): intent("ghost", native_fields={"authors": ["a" * 23], "tags": []})

    def test_active_html_obfuscated_urls_duplicate_attributes_and_marker_spoofing_are_rejected(self):
        for body in ('<script>publish()</script>', '<iframe src="https://fixture.test/"></iframe>',
                     '<p onclick="publish()">Text</p>', '<div style="background:url(private)">Text</div>',
                     '<a href="java&#115;cript:publish()">Text</a>', '<img src="data:image/svg+xml,private">',
                     '<a href="https://user:password@fixture.test/">Text</a>', '<img src="//foreign.test/x">',
                     '<a href="/safe" href="javascript:other">Text</a>', '<!--mlai-post:spoof--><p>Text</p>',
                     '<!--kg-card-begin: html--><p>Text</p><!--kg-card-end: html-->', '<!doctype html><p>Text</p>'):
            with self.subTest(body=body), self.assertRaises(CmsApiError): intent(html=body)
        value = intent(html='<article data-cf-article-body><p><a href="https://example.test/source" rel="noreferrer">Citation</a></p><img src="/media/owned.jpg" alt="Owned image" loading="lazy"></article>')
        self.assertEqual(posts.validate_intent(value), value)

    def test_create_is_only_available_after_empty_lookup_and_always_has_exact_owned_content(self):
        for platform in ("wordpress", "ghost"):
            value = intent(platform)
            initial = posts.new_post_state(value)
            lookup = posts.lookup_effect(value, initial)
            self.assertEqual(lookup.method, "GET")
            self.assertIn(value["slug"], str(lookup.query))
            with self.assertRaises(CmsApiError): posts.issue_create(value, initial)
            state, effect = issued(value)
            self.assertEqual((state["phase"], state["create_count"], state["outcome_unknown"]), ("create_issued", 1, True))
            self.assertEqual((effect.method, effect.resource, effect.identifier), ("POST", "posts", None))
            self.assertIn(posts.marked_html(value), str(effect.payload))
            self.assertNotIn("The exact approved body", repr(effect))
            if platform == "ghost":
                self.assertEqual(effect.query, {"source": "html", "formats": "html"})
                self.assertEqual(effect.payload["posts"][0]["authors"], [{"id": "a" * 24}])
            else:
                self.assertEqual(effect.payload["status"], "publish")
                self.assertEqual(effect.payload["author"], 7)

    def test_lost_create_reply_reconciles_one_owned_resource_and_never_reposts(self):
        for platform in ("wordpress", "ghost"):
            value = intent(platform)
            state, _ = issued(value)
            for _ in range(3):
                self.assertEqual(posts.lookup_effect(value, state).method, "GET")
                with self.assertRaises(CmsApiError): posts.issue_create(value, state)
                state = posts.observe_posts(value, state, collection(value, []))
            result = posts.observe_posts(value, state, collection(value, [response(value)]))
            self.assertEqual(result["phase"], "provider_confirmed")
            self.assertEqual(result["create_count"], 1)
            self.assertIs(result["outcome_unknown"], False)
            self.assertIsNotNone(result["provider_id"])
            self.assertNotIn("live", result)
            self.assertNotIn("merge", result)
            posts.validate_post_state(result, value)

    def test_acknowledged_create_and_later_lookup_identify_the_same_provider_post(self):
        for platform in ("wordpress", "ghost"):
            value = intent(platform)
            state, _ = issued(value)
            ack = response(value) if platform == "wordpress" else {"posts": [response(value)]}
            acknowledged = posts.observe_posts(value, state, ack, lookup=False)
            recovered = posts.observe_posts(value, state, collection(value, [response(value)]))
            self.assertEqual(acknowledged, recovered)
            with self.assertRaises(CmsApiError): posts.issue_create(value, acknowledged)
            with self.assertRaises(CmsApiError): posts.lookup_effect(value, acknowledged)

    def test_preexisting_unissued_slug_even_with_matching_marker_is_never_adopted_or_overwritten(self):
        for platform in ("wordpress", "ghost"):
            value = intent(platform)
            initial = posts.new_post_state(value)
            with self.assertRaises(CmsApiError) as error:
                posts.observe_posts(value, initial, collection(value, [response(value)]))
            self.assertEqual(error.exception.category, "post_ownership_conflict")

    def test_repeated_missing_lookup_outcome_remains_unknown_without_another_create(self):
        value = intent()
        state, _ = issued(value)
        for _ in range(8): state = posts.observe_posts(value, state, [])
        self.assertEqual((state["phase"], state["probe_count"], state["create_count"]), ("reconcile_exhausted", 8, 1))
        self.assertIs(state["outcome_unknown"], True)
        posts.validate_post_state(state, value)
        with self.assertRaises(CmsApiError): posts.issue_create(value, state)
        with self.assertRaises(CmsApiError): posts.lookup_effect(value, state)

    def test_changed_body_title_status_author_metadata_or_url_cannot_certify_ownership(self):
        value = intent()
        state, _ = issued(value)
        for changes in ({"title": {"raw": "Edited title"}}, {"content": {"raw": "Changed body"}},
                        {"content": {"raw": posts.marked_html(value) + "<p>Customer edit</p>"}},
                        {"status": "draft"}, {"slug": "unrelated"}, {"id": True}, {"author": 99},
                        {"categories": [2]}, {"tags": [True]}, {"featured_media": 12},
                        {"content": {"raw": posts.marked_html(value), "protected": 0}},
                        {"content": {"raw": posts.marked_html(value), "protected": []}}, {"categories": [{}]},
                        {"password": "private"}, {"link": "https://other.test/article/"}):
            with self.subTest(changes=tuple(changes)), self.assertRaises(CmsApiError):
                posts.observe_posts(value, state, [response(value, **changes)])

    def test_ghost_lossless_card_wrappers_are_explicit_and_unknown_extra_content_is_denied(self):
        value = intent("ghost")
        state, _ = issued(value)
        marker = posts.marked_html(value)
        for body in (marker, '<div class="kg-card kg-html-card">' + marker + '</div>',
                     '<!--kg-card-begin: html-->' + marker + '<!--kg-card-end: html-->'):
            result = posts.observe_posts(value, state, {"posts": [response(value, html=body)]})
            self.assertEqual(result["phase"], "provider_confirmed")
        for changes in ({"html": "<p>Customer edit</p>" + marker}, {"visibility": "members"}, {"authors": [{"id": "e" * 24}]},
                        {"tags": []}, {"status": "draft"}):
            with self.subTest(changes=tuple(changes)), self.assertRaises(CmsApiError):
                posts.observe_posts(value, state, {"posts": [response(value, **changes)]})

    def test_draft_acknowledgement_never_claims_a_public_url_or_live_result(self):
        for platform in ("wordpress", "ghost"):
            value = intent(platform, status="draft")
            state, effect = issued(value)
            result = posts.observe_posts(value, state, collection(value, [response(value)]))
            self.assertEqual(result["provider_status"], "draft")
            self.assertIsNone(result["public_url"])
            self.assertEqual(effect.payload["status"] if platform == "wordpress" else effect.payload["posts"][0]["status"], "draft")

    def test_confirmed_state_cannot_swap_provider_id_digest_or_context(self):
        value = intent()
        state, _ = issued(value)
        result = posts.observe_posts(value, state, [response(value)])
        for changes in ({"provider_id": 99}, {"observation_digest": "f" * 64}, {"outcome_unknown": True},
                        {"create_count": True}, {"probe_count": True}, {"intent_digest": "d" * 64}, {"public_url": "https://other.test/"}):
            with self.subTest(changes=tuple(changes)), self.assertRaises(CmsApiError):
                posts.validate_post_state({**result, **changes}, value)
        other = intent()
        with self.assertRaises(CmsApiError): posts.validate_post_state(result, other)

    def test_multiple_matches_empty_ack_and_unexpected_shapes_are_not_success(self):
        for platform in ("wordpress", "ghost"):
            value = intent(platform)
            state, _ = issued(value)
            for rows in ([response(value), response(value)], None, [None]):
                with self.assertRaises(CmsApiError): posts.observe_posts(value, state, collection(value, rows))
            with self.assertRaises(CmsApiError): posts.observe_posts(value, state, collection(value, []), lookup=False)
