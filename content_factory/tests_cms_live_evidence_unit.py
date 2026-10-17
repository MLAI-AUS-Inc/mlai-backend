"""A native public post must match approved content, not just return HTTP 200."""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from django.test import SimpleTestCase

from . import cms_live_evidence as live
from .cms_api_transport import CmsApiError
from . import cms_post_contract as posts
from .tests_cms_post_contract_unit import intent, issued, response


def article(platform="wordpress", **changes):
    title = "Approved native article"
    body = '<article data-cf-article-body><h1>' + title + '</h1><p>' + " ".join(
        ["This practical article explains a concrete task with approved instructions, evidence, useful examples and clear next steps."] * 3
    ) + '</p><h2>The next step</h2><p><a href="/about/">Read about the team</a></p><img src="https://fixture.test/media/owned.png" alt="The approved image"></article>'
    value = intent(platform, html=body, **changes)
    state, _ = issued(value)
    ack = response(value)
    state = posts.observe_posts(value, state, ack if platform == "wordpress" else {"posts": [ack]}, lookup=False)
    return value, state


def document(value, state, *, body=None, canonical=None):
    canonical = '<link rel="canonical" href="' + state["public_url"] + '">' if canonical is None else canonical
    return '<html><head>' + canonical + '</head><body><article class="native-theme"><h1>Theme title outside the owned body</h1>' + (body or value["html"]) + '</article></body></html>'


class CmsLiveEvidenceTests(SimpleTestCase):
    def test_exact_owned_content_and_canonical_create_only_a_minimized_body_observation(self):
        for platform in ("wordpress", "ghost"):
            value, state = article(platform)
            proof = live.observe_public_article(value, state, document(value, state), checked_at=datetime(2026, 10, 17, tzinfo=timezone.utc))
            self.assertEqual((proof["kind"], proof["status"], proof["platform"]), ("cms_public_article_observation", "body_verified", platform))
            self.assertEqual((proof["provider_id"], proof["body_source_sha256"]), (state["provider_id"], value["body_sha256"]))
            self.assertEqual(proof["intent_digest"], value["digest"])
            self.assertEqual(proof["scope_sha256"], live.digest(value["binding"]))
            self.assertFalse({"approved", "live", "merge", "commit_sha", "credential", "html", "api_origin"} & proof.keys())
            self.assertNotIn("This practical article", str(proof))
            self.assertEqual(proof["digest"], live.digest({key: item for key, item in proof.items() if key != "digest"}))

    def test_native_wrappers_classes_srcset_and_absolute_equivalent_links_keep_exact_semantics(self):
        value, state = article()
        changed = value["html"].replace('<p>', '<p class="wp-block-paragraph">').replace('href="/about/"', 'href="https://fixture.test/about/"')
        changed = changed.replace('<img ', '<img loading="lazy" srcset="https://fixture.test/media/owned.png 1024w" ')
        self.assertEqual(live.observe_public_article(value, state, document(value, state, body=changed))["body_source_sha256"], value["body_sha256"])

    def test_copy_heading_links_and_images_cannot_be_substituted_by_a_successful_page(self):
        value, state = article()
        for body in (value["html"].replace("practical", "outdated", 1), value["html"].replace("<h2>", "<h3>").replace("</h2>", "</h3>"),
                value["html"].replace('href="/about/"', 'href="https://foreign.test/"'),
                value["html"].replace("/media/owned.png", "/media/other.png"), value["html"].replace("The approved image", "Different alternative text"),
                '<article data-cf-article-body><h1>Login</h1><p>Please log in.</p></article>'):
            with self.subTest(body=body[:40]), self.assertRaises(CmsApiError): live.observe_public_article(value, state, document(value, state, body=body))

    def test_duplicate_or_missing_body_marker_and_unapproved_active_content_do_not_verify(self):
        value, state = article()
        for body in (value["html"] * 2, value["html"].replace("data-cf-article-body", "data-other"),
                value["html"].replace("</article>", '<script>private()</script></article>'),
                value["html"].replace("</article>", '<iframe src="https://foreign.test/"></iframe></article>'),
                value["html"].replace("<p>", '<p onclick="private()">'),
                value["html"].replace("</article>", '<button></button></article>')):
            with self.subTest(body=body[:50]), self.assertRaises(CmsApiError): live.observe_public_article(value, state, document(value, state, body=body))

    def test_explicitly_hidden_or_inert_content_is_not_a_visible_article_observation(self):
        value, state = article()
        for attribute in ('hidden', 'inert', 'aria-hidden="true"', 'style="display: none"', 'style="visibility: hidden"'):
            body = '<div ' + attribute + '>' + value["html"] + '</div>'
            with self.subTest(attribute=attribute), self.assertRaises(CmsApiError): live.observe_public_article(value, state, document(value, state, body=body))

    def test_missing_duplicate_relative_false_or_foreign_canonical_cannot_pass(self):
        value, state = article()
        valid = '<link rel="canonical" href="' + state["public_url"] + '">'
        for canonical in ("", valid + valid, valid.replace("canonical", "notcanonical"), valid.replace(state["public_url"], "/article/"),
                valid.replace(state["public_url"], "https://foreign.test/article/")):
            with self.subTest(canonical=canonical), self.assertRaises(CmsApiError):
                live.observe_public_article(value, state, document(value, state, canonical=canonical))
        with self.assertRaises(CmsApiError):
            live.observe_public_article(value, state, '<html><head></head><body>' + valid + value["html"] + '</body></html>')

    def test_provider_draft_or_unissued_intent_cannot_fetch_or_produce_body_proof(self):
        value, state = article(status="draft")
        with patch.object(live, "public_addresses") as addresses:
            with self.assertRaises(CmsApiError): live.fetch_public_article(value, state)
            with self.assertRaises(CmsApiError): live.observe_public_article(value, state, document(value, {**state, "public_url": "https://fixture.test/"}))
            value, state = article()
            with self.assertRaises(CmsApiError): live.fetch_public_article(value, posts.new_post_state(value))
            addresses.assert_not_called()

    def fetch_setup(self, value, state):
        response = Mock(status=200)
        response.getheader.side_effect = lambda key, default=None: {"Content-Type": "text/html; charset=utf-8"}.get(key, default)
        response.read.return_value = document(value, state).encode()
        client = Mock()
        client.getresponse.return_value = response
        return client, response

    def test_public_fetch_pins_dns_and_carries_no_credential_cookie_or_referer(self):
        value, state = article()
        client, response = self.fetch_setup(value, state)
        with patch.object(live, "public_addresses", return_value=(urlsplit(state["public_url"]), ["8.8.8.8"])) as addresses, \
                patch.object(live, "PinnedHTTPSConnection", return_value=client):
            body = live.fetch_public_article(value, state)
        self.assertEqual(body, document(value, state))
        addresses.assert_called_once_with(state["public_url"], "fixture.test")
        self.assertEqual(set(client.request.call_args.kwargs["headers"]), {"Host", "Connection", "Accept", "Accept-Encoding"})
        response.read.assert_called_once_with(live.MAX_HTML_BYTES + 1)
        response.close.assert_called_once()
        client.close.assert_called_once()

    def test_wordpress_plain_permalink_query_is_bound_to_the_confirmed_post(self):
        value, _ = article()
        state, _ = issued(value)
        state = posts.observe_posts(value, state, response(value, link="https://fixture.test/?p=42"), lookup=False)
        client, _ = self.fetch_setup(value, state)
        with patch.object(live, "public_addresses", return_value=(urlsplit("https://fixture.test/"), ["8.8.8.8"])) as addresses, \
                patch.object(live, "PinnedHTTPSConnection", return_value=client):
            html = live.fetch_public_article(value, state)
        self.assertEqual(client.request.call_args.args, ("GET", "/?p=42"))
        addresses.assert_called_once_with("https://fixture.test/", "fixture.test")
        self.assertEqual(live.observe_public_article(value, state, html)["public_url"], state["public_url"])

    def test_redirect_login_error_html_content_type_encoding_and_size_are_not_live_proof(self):
        value, state = article()
        client, response = self.fetch_setup(value, state)
        with patch.object(live, "public_addresses", return_value=(urlsplit(state["public_url"]), ["8.8.8.8"])), \
                patch.object(live, "PinnedHTTPSConnection", return_value=client):
            for status in (301, 302, 307, 401, 404, 429, 503):
                response.status = status
                with self.subTest(status=status), self.assertRaises(CmsApiError): live.fetch_public_article(value, state)
            self.assertEqual(response.read.call_count, 0)
            response.status = 200
            for headers, body in (({"Content-Type": "application/json"}, b'{}'), ({"Content-Type": "text/html", "Content-Encoding": "gzip"}, b'private'),
                    ({"Content-Type": "text/html", "Content-Length": str(live.MAX_HTML_BYTES + 1)}, b'private'),
                    ({"Content-Type": "text/html"}, b'x' * (live.MAX_HTML_BYTES + 1)), ({"Content-Type": "text/html"}, b'\xff\xfe')):
                response.getheader.side_effect = lambda key, default=None: headers.get(key, default)
                response.read.return_value = body
                with self.subTest(headers=headers), self.assertRaises(CmsApiError): live.fetch_public_article(value, state)

    def test_changed_intent_state_or_unbounded_html_cannot_be_observed(self):
        value, state = article()
        for changed in ({**value, "html": "replaced"}, {**value, "binding": {**value["binding"], "generation": 2}}):
            with self.subTest(changed=tuple(changed)), self.assertRaises(CmsApiError): live.observe_public_article(changed, state, document(value, state))
        with self.assertRaises(CmsApiError): live.observe_public_article(value, {**state, "provider_id": 99}, document(value, state))
        with self.assertRaises(CmsApiError): live.observe_public_article(value, state, "x" * (live.MAX_HTML_BYTES + 1))
        with self.assertRaises(CmsApiError): live.observe_public_article(value, state, document(value, state), checked_at=datetime(2026, 10, 17))

    def test_private_observation_validation_refuses_stale_future_unknown_or_rebound_receipts(self):
        value, state = article()
        now = datetime(2026, 10, 17, tzinfo=timezone.utc)
        proof = live.observe_public_article(value, state, document(value, state), checked_at=now)
        self.assertEqual(live.validate_public_observation(proof, intent=value, state=state, now=now), proof)
        for changes in ({"schema_version": True}, {"run_generation": True}, {"provider_id": 99}, {"post_operation_id": "foreign"},
                {"public_url": "https://foreign.test/"}, {"text_sha256": "f" * 64}, {"approved": True},
                {"checked_at": (now - timedelta(minutes=16)).isoformat()}, {"checked_at": (now + timedelta(seconds=31)).isoformat()}):
            changed = {**proof, **changes}
            changed["digest"] = live.digest({key: item for key, item in changed.items() if key != "digest"})
            with self.subTest(fields=tuple(changes)), self.assertRaises(CmsApiError):
                live.validate_public_observation(changed, intent=value, state=state, now=now)
